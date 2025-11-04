import time, queue, os
from multiprocessing import Process, Queue
import numpy as np

def caen_worker_process(cmd_q: Queue, data_q: Queue, config: dict):
    params = config['parameters']; is_dual_current = 'i_mon_low' in params
    device, hv = None, None
    print(f"[Process-{os.getpid()}] CAEN worker process started.")
    while True:
        try:
            if not cmd_q.empty():
                cmd = cmd_q.get()
                if cmd['type'] == 'stop': break
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

            if device is None:
                if not hv:
                    from caen_libs import caenhvwrapper; hv = caenhvwrapper
                data_q.put({'type': 'status', 'msg': f"Connecting to HV ({config.get('connection_argument', '')})..."})
                device = hv.Device.open(hv.SystemType[config['system_type']], hv.LinkType[config['link_type']], config.get('connection_argument', ''), config.get('username', ''), config.get('password', ''))
                data_q.put({'type': 'status', 'msg': "HV Status: Connection Successful!"})
            
            results = []
            for ch_mon in config['channels_to_monitor']:
                vmon = device.get_ch_param(0, [ch_mon], params['v_mon'])[0]
                if is_dual_current:
                    imon_l = device.get_ch_param(0, [ch_mon], params['i_mon_low'])[0]; imon_h = device.get_ch_param(0, [ch_mon], params['i_mon_high'])[0]
                    results.append({'ch': ch_mon, 'v': vmon, 'il': imon_l, 'ih': imon_h})
                else:
                    imon = device.get_ch_param(0, [ch_mon], params['i_mon'])[0]
                    results.append({'ch': ch_mon, 'v': vmon, 'i': imon})
            data_q.put({'type': 'data', 'data': results})
        except hv.Error as e:
            data_q.put({'type': 'status', 'msg': f"HV Status: Connection Failed. Retrying..."})
            if device:
                try: device.close()
                except hv.Error: pass
            device = None
        except Exception as e: data_q.put({'type': 'status', 'msg': f"Worker Error: {e}"}); break
        time.sleep(2)
    if device:
        try: device.close()
        except: pass
    print(f"[Process-{os.getpid()}] CAEN worker process finished.")
