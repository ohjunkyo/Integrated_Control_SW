# -*- coding: utf-8 -*-
"""
CAEN HV Power Supply Parameter Diagnostic Script

- 목적: CAEN HV 모듈에 연결하여 특정 채널에서 사용 가능한 
        파라미터 이름의 정확한 목록을 가져옵니다.
- 사용법: python3 hv_diagnostic.py [config_file.json]
- 최종 수정일: 2025-09-18
"""

import sys
import json
import os

try:
    from caen_libs import caenhvwrapper as hv
except ImportError:
    print(" ERROR: 'caen_libs' 라이브러리를 찾을 수 없습니다.")
    print("CAEN HV Wrapper 라이브러리가 올바르게 설치되었는지 확인하세요.")
    sys.exit(1)

def load_config(filename):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"설정 파일 '{filename}'을 찾을 수 없습니다.")
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)

if __name__ == "__main__":
    print("--- CAEN HV 파라미터 진단 시작 ---")

    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.json'
    
    slot_to_check = 0
    channel_to_check = 0
    
    try:
        print(f"Loading '{config_file}'...")
        config = load_config(config_file)
        hv_cfg = config['caen_hv_settings']
        print("설정 파일을 성공적으로 로드했습니다.")

        system_type_str = hv_cfg['system_type']
        link_type_str = hv_cfg['link_type']
        connection_arg = hv_cfg['connection_argument']
        username = hv_cfg['username']
        password = hv_cfg['password']
        
        system_type = hv.SystemType[system_type_str]
        link_type = hv.LinkType[link_type_str]

        print(f"'{connection_arg}' ({system_type_str})에 연결을 시도합니다...")
        
        with hv.Device.open(system_type, link_type, connection_arg, username, password) as device:
            print(f"연결에 성공했습니다!")
            print(f"\nSlot {slot_to_check}, Channel {channel_to_check}의 파라미터 목록을 요청합니다...")
            
            param_list = device.get_ch_param_info(slot_to_check, channel_to_check)
            
            print("\n--- 진단 결과 ---")
            print(f"🔬 사용 가능한 파라미터: \n{param_list}")
            print("-----------------")
            
            print("\n[ 조치 사항 ]")
            print(f"'{config_file}' 파일의 'caen_hv_settings' 섹션 안에 있는 'parameters' 객체의 값들을 위 목록과 일치시키십시오.")
            print("예: N1470 모델의 전압 설정 파라미터가 'V0Set'이라면, 'v_set': 'V0Set' 으로 수정해야 합니다.")

    except FileNotFoundError as e:
        print(f" ERROR: {e}")
    except KeyError as e:
        print(f" ERROR: '{config_file}' 파일에서 '{e}' 키를 찾을 수 없습니다. 파일 구조를 확인해주세요.")
    except hv.Error as e:
        print(f" CAEN HV ERROR: 장비에 연결하거나 파라미터를 가져올 수 없습니다. 상세 정보: {e}")
    except Exception as e:
        print(f" 예기치 않은 오류가 발생했습니다: {e}")

    print("\n--- 진단 스크립트 종료 ---")
