import time, queue, os
from multiprocessing import Process, Queue
import numpy as np

POLL_INTERVAL = 2.0   # s between read cycles
READERR_RECONNECT_POLLS = 15   # consecutive all-channel READERR polls before the link is reopened
READERR_RECONNECT_COOLDOWN = 120.0   # s between such forced reopenings


def _close(device):
    if device:
        try: device.close()
        except Exception: pass


def caen_worker_process(cmd_q: Queue, data_q: Queue, config: dict):
    params = config['parameters']; is_dual_current = 'i_mon_low' in params
    device, hv = None, None
    readerr_polls, last_forced = 0, 0.0   # stale-link detection
    paused, poll_now, deferred = False, False, []   # paused = operator closed the link
    parent = os.getppid()
    print(f"[Process-{os.getpid()}] CAEN worker process started.")

    def orphaned():
        # The app died without sending stop: release the crate instead of
        # holding its connection (and the app's instance lock) forever.
        return os.getppid() != parent

    def handle(cmd):
        """Run one command; return True when it is the stop request."""
        nonlocal device, paused, poll_now
        if cmd['type'] == 'stop': return True
        if cmd['type'] == 'disconnect':
            _close(device); device = None; paused = True
            data_q.put({'type': 'status', 'msg': "HV Status: Disconnected by operator (HV output unchanged)"})
            return False
        if cmd['type'] == 'connect':
            paused = False; poll_now = True
            return False
        if cmd['type'] == 'poll_now':
            poll_now = True
            return False
        if paused:
            if cmd['type'] == 'set_param' and cmd['param_name'] == params['pw']:
                paused = False; deferred.append(cmd)   # power commands must never be lost
                data_q.put({'type': 'status', 'msg': "HV Status: Reconnecting for a power command..."})
            else:
                data_q.put({'type': 'feedback', 'msg': "Error: HV link is disconnected (press HV Connect)"})
            return False
        if not device: return False
        if cmd['type'] == 'set_param':
            try:
                device.set_ch_param(cmd['slot'], cmd['ch_list'], cmd['param_name'], cmd['value'])
                data_q.put({'type': 'feedback', 'msg': f"Success: Ch{cmd['ch_list'][0]} {cmd['param_name']} set to {cmd['value']}"})
            except hv.Error as e:
                data_q.put({'type': 'feedback', 'msg': f"Error on Set: {e}"})
        elif cmd['type'] == 'fetch_settings':
            try:
                settings = {}
                for ch in cmd['ch_list']:
                    v_set_prop = device.get_ch_param_prop(cmd['slot'], ch, params['v_set'])
                    i_set_prop = device.get_ch_param_prop(cmd['slot'], ch, params['i_set'])
                    v_val = device.get_ch_param(cmd['slot'], [ch], params['v_set'])[0] if v_set_prop.mode.name != 'WRONLY' else device.get_ch_param(cmd['slot'], [ch], params['v_mon'])[0]
                    i_val = device.get_ch_param(cmd['slot'], [ch], params['i_set'])[0] if i_set_prop.mode.name != 'WRONLY' else device.get_ch_param(0, [ch], params.get('i_mon_high', params.get('i_mon')))[0]
                    settings[ch] = {'v_set': v_val, 'i_set': i_val}
                data_q.put({'type': 'initial_settings', 'data': settings})
            except hv.Error as e:
                data_q.put({'type': 'feedback', 'msg': f"Error fetching settings: {e}"})
        elif cmd['type'] == 'clear_alarm':
            print("\n[CAEN Worker] ======= ALARM CLEAR ATTEMPT =======")
            try:
                device.set_bd_param([cmd['slot']], 'ClrAlarm', 1)
                print("-> device.set_bd_param([slot], 'ClrAlarm', 1) : SUCCESS!")
                data_q.put({'type': 'feedback', 'msg': "Success: Alarm Cleared"})
            except Exception as e:
                print(f"-> FAILED ({type(e).__name__}: {e})")
                if "WRITEERR" in str(e):
                    data_q.put({'type': 'feedback', 'msg': "Fail: HW Interlock is still active! Check magnet/door."})
                else:
                    data_q.put({'type': 'feedback', 'msg': "Error: Failed to clear alarm."})
            print("[CAEN Worker] ===================================\n")
        return False

    def wait_for_commands(seconds):
        """Sleep, but act on commands (and stop) the moment they arrive."""
        nonlocal poll_now
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if orphaned(): return True
            if poll_now:
                poll_now = False
                return False
            if left <= 0: return False
            try: cmd = cmd_q.get(timeout=min(left, 1.0))
            except queue.Empty: continue
            if handle(cmd): return True

    while True:
        try:
            # 1. 장비 연결 시도
            if device is None and not paused:
                if not hv:
                    from caen_libs import caenhvwrapper; hv = caenhvwrapper
                data_q.put({'type': 'status', 'msg': f"Connecting to HV ({config.get('connection_argument', '')})..."})
                device = hv.Device.open(hv.SystemType[config['system_type']], hv.LinkType[config['link_type']], config.get('connection_argument', ''), config.get('username', ''), config.get('password', ''))
                data_q.put({'type': 'status', 'msg': "HV Status: Connection Successful!"})
                print("[CAEN Worker] Connected to HV successfully.")
                while deferred: handle(deferred.pop(0))

            if orphaned():
                print("[CAEN Worker] Parent app is gone -- closing the HV connection.")
                _close(device); return

            # 2. 버튼/명령 큐 처리
            while not cmd_q.empty():
                if handle(cmd_q.get()):
                    _close(device); return

            # 3. 데이터 읽기
            if device:
                results = []
                n_readerr = 0
                for ch_mon in config['channels_to_monitor']:
                    if not cmd_q.empty(): break
                    try:
                        vmon = device.get_ch_param(0, [ch_mon], params['v_mon'])[0]
                        if is_dual_current:
                            imon_l = device.get_ch_param(0, [ch_mon], params['i_mon_low'])[0]
                            imon_h = device.get_ch_param(0, [ch_mon], params['i_mon_high'])[0]
                        else:
                            imon = device.get_ch_param(0, [ch_mon], params['i_mon'])[0]

                        try: status_val = device.get_ch_param(0, [ch_mon], 'ChStatus')[0] 
                        except: status_val = float('nan')   # unreadable, not OFF
                        try: vset = device.get_ch_param(0, [ch_mon], params['v_set'])[0]   # logged beside VMon
                        except Exception: vset = None
                            
                        if is_dual_current: results.append({'ch': ch_mon, 'v': vmon, 'il': imon_l, 'ih': imon_h, 'stat': status_val, 'vset': vset}) 
                        else: results.append({'ch': ch_mon, 'v': vmon, 'i': imon, 'stat': status_val, 'vset': vset}) 

                    except hv.Error as e:
                        err_str = str(e).upper()
                        # [수정] 진짜 통신 단절(COMMERR)과 인터락 읽기 실패(READERR) 분리!
                        if "READERR" in err_str:
                            n_readerr += 1
                            if is_dual_current: results.append({'ch': ch_mon, 'v': 0.0, 'il': 0.0, 'ih': 0.0, 'stat': 4096})
                            else: results.append({'ch': ch_mon, 'v': 0.0, 'i': 0.0, 'stat': 4096})
                        elif "COMMERR" in err_str:
                            raise e # 장비가 꺼졌으면 위로 던져서 재연결 루프로 보냄
                        else:
                            pass
    

                if results:
                    data_q.put({'type': 'data', 'data': results})

                # READERR on every channel for a long time = stale link (a manual reconnect clears it)
                if results and n_readerr == len(results): readerr_polls += 1
                else: readerr_polls = 0
                if readerr_polls >= READERR_RECONNECT_POLLS and time.time() - last_forced > READERR_RECONNECT_COOLDOWN:
                    print("[CAEN Worker] Persistent READERR on all channels -- reopening the HV link.")
                    data_q.put({'type': 'status', 'msg': "HV Status: Reconnecting... (persistent read error)"})
                    _close(device); device = None
                    readerr_polls, last_forced = 0, time.time()

        except hv.Error as e:
            data_q.put({'type': 'status', 'msg': f"HV Status: Reconnecting... ({e})"})
            if device:
                try: device.close()
                except: pass
            device = None
            if wait_for_commands(1): return

        except Exception as e: 
            print(f"[CAEN Worker Critical Error] {e}")
            if wait_for_commands(1):
                _close(device); return

        if wait_for_commands(POLL_INTERVAL):
            _close(device)
            print(f"[Process-{os.getpid()}] CAEN worker process finished.")
            return
