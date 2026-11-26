import sys, json, os

try:
	from caen_libs import caenhvwrapper as hv
except ImportError:
	print(" ERROR: 'caen_libs' 라이브러리를 찾을 수 없습니다."); sys.exit(1)

def load_config(filename):
	if not os.path.exists(filename):
		raise FileNotFoundError(f"설정 파일 '{filename}'을 찾을 수 없습니다.")
	with open(filename, 'r', encoding='utf-8') as f:
		return json.load(f)

if __name__ == "__main__":
	print("--- CAEN HV 고급 파라미터 진단 시작 ---")
	config_file = sys.argv[1] if len(sys.argv) > 1 else '../config_precal.json'
	slot_to_check = 0
	channel_to_check = 0

	try:
		print(f"Loading '{config_file}'...")
		config = load_config(config_file)
		hv_cfg = config['caen_hv_settings']

		system_type = hv.SystemType[hv_cfg['system_type']]
		link_type = hv.LinkType[hv_cfg['link_type']]

		print(f"'{hv_cfg['connection_argument']}' ({hv_cfg['system_type']})에 연결을 시도합니다...")
		with hv.Device.open(system_type, link_type, hv_cfg['connection_argument'], hv_cfg['username'], hv_cfg['password']) as device:
			print("연결 성공!")
			param_list = device.get_ch_param_info(slot_to_check, channel_to_check)
			print(f"\n--- Slot {slot_to_check}, Channel {channel_to_check} 파라미터 속성 ---")

			for param_name in param_list:
				try:
					prop = device.get_ch_param_prop(slot_to_check, channel_to_check, param_name)
					print(f"- {param_name:<10} | Type: {prop.type.name:<10} | Mode: {prop.mode.name:<10}")
				except hv.Error as e:
					print(f"- {param_name:<10} | Error getting properties: {e}")
			print("------------------------------------------")

	except Exception as e:
		print(f"\nERROR: {e}")

	print("\n--- 진단 스크립트 종료 ---")
