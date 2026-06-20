# main_test.py — TEST MODE 진입점 (thin shim)
#
# [중요] 이 파일은 더 이상 main.py 의 복사본이 아니다.
#
# 과거에는 main_test.py 가 main.py 를 통째로 복사한 별도 파일이었으나, 두 파일이
# 서로 드리프트하면서(예: 한쪽에만 버그 수정/기능 추가가 들어감) 유지보수 부담과
# 버그의 원인이 되었다. 실제 "시뮬레이션/테스트" 동작은 별도 파일이 아니라
# 앱 안의 'TEST RUN (Simulation Mode)' 체크박스(auto_ui.dummy_var)로 제어되므로,
# TEST MODE 역시 동일한 App 을 그대로 실행하면 된다.
#
# 따라서 이 파일은 main.py 의 진입점(launch)을 호출하기만 한다. 이렇게 하면
#   - main.py 에 가한 모든 변경(배너/콘솔/버그 수정 등)이 TEST MODE 에도 자동 반영되고,
#   - launcher 의 'Start DAQ (TEST MODE)' 버튼과 프로덕션/테스트 상호 배제(pgrep)
#     로직이 파일명 기준으로 그대로 동작한다.
#
# 옛 구현이 필요하면 git 히스토리에서 복구할 수 있다.

from main import launch

if __name__ == "__main__":
    launch()
