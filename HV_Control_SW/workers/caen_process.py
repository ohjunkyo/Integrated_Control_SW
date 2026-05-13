import time, queue, os
from multiprocessing import Process, Queue
import numpy as np

def caen_worker_process(cmd_q: Queue, data_q: Queue, config: dict):
    params = config['parameters']; is_dual_current = 'i_mon_low' in params
    device, hv = None, None
    print(f"[Process-{os.getpid()}] CAEN worker process started.")
    
    while True:
        try:
            # 1. 장비 연결 시도
            if device is None:
                if not hv:
                    from caen_libs import caenhvwrapper; hv = caenhvwrapper
                data_q.put({'type': 'status', 'msg': f"Connecting to HV ({config.get('connection_argument', '')})..."})
                device = hv.Device.open(hv.SystemType[config['system_type']], hv.LinkType[config['link_type']], config.get('connection_argument', ''), config.get('username', ''), config.get('password', ''))
                data_q.put({'type': 'status', 'msg': "HV Status: Connection Successful!"})
                print("[CAEN Worker] Connected to HV successfully.")

            # 2. 버튼/명령 큐 처리
            while not cmd_q.empty():
                cmd = cmd_q.get()
                if cmd['type'] == 'stop': return
                
                if device:
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
                            # 1을 보내야만 해제가 '실행'됩니다.
                            device.set_bd_param([cmd['slot']], 'ClrAlarm', 1)
                            print("-> device.set_bd_param([slot], 'ClrAlarm', 1) : SUCCESS!")
                            data_q.put({'type': 'feedback', 'msg': "Success: Alarm Cleared"})
                        except Exception as e:
                            print(f"-> FAILED ({type(e).__name__}: {e})")
                            # 장비가 위험하다고 판단해 해제를 거부한 경우
                            if "WRITEERR" in str(e):
                                data_q.put({'type': 'feedback', 'msg': "Fail: HW Interlock is still active! Check magnet/door."})
                            else:
                                data_q.put({'type': 'feedback', 'msg': "Error: Failed to clear alarm."})
                        print("[CAEN Worker] ===================================\n")

            # 3. 데이터 읽기
            if device:
                results = []
                for ch_mon in config['channels_to_monitor']:
                    try:
                        vmon = device.get_ch_param(0, [ch_mon], params['v_mon'])[0]
                        if is_dual_current:
                            imon_l = device.get_ch_param(0, [ch_mon], params['i_mon_low'])[0]
                            imon_h = device.get_ch_param(0, [ch_mon], params['i_mon_high'])[0]
                        else:
                            imon = device.get_ch_param(0, [ch_mon], params['i_mon'])[0]

                        try: status_val = device.get_ch_param(0, [ch_mon], 'ChStatus')[0] 
                        except: status_val = 0 
                            
                        if is_dual_current: results.append({'ch': ch_mon, 'v': vmon, 'il': imon_l, 'ih': imon_h, 'stat': status_val}) 
                        else: results.append({'ch': ch_mon, 'v': vmon, 'i': imon, 'stat': status_val}) 

                    except hv.Error as e:
                        err_str = str(e).upper()
                        # [수정] 진짜 통신 단절(COMMERR)과 인터락 읽기 실패(READERR) 분리!
                        if "READERR" in err_str:
                            if is_dual_current: results.append({'ch': ch_mon, 'v': 0.0, 'il': 0.0, 'ih': 0.0, 'stat': 4096})
                            else: results.append({'ch': ch_mon, 'v': 0.0, 'i': 0.0, 'stat': 4096})
                        elif "COMMERR" in err_str:
                            raise e # 장비가 꺼졌으면 위로 던져서 재연결 루프로 보냄
                        else:
                            pass
    

                if results:
                    data_q.put({'type': 'data', 'data': results})

        except hv.Error as e:
            data_q.put({'type': 'status', 'msg': f"HV Status: Reconnecting... ({e})"})
            if device:
                try: device.close()
                except: pass
            device = None
            time.sleep(1) 

        except Exception as e: 
            print(f"[CAEN Worker Critical Error] {e}")
            time.sleep(1)
            
        time.sleep(2)

    if device:
        try: device.close()
        except: pass
    print(f"[Process-{os.getpid()}] CAEN worker process finished.")
