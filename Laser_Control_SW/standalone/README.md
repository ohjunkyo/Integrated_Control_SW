# Tamadenshi LD 보드 — 독립 실행형 Linux 제어 소프트웨어

Tamadenshi(玉電子) 피코초 레이저 다이오드 보드를 제어하는 독립 실행형
소프트웨어입니다. GUI, CLI, 드라이버 라이브러리를 모두 포함합니다.

이 디렉토리 하나만 복사하면 어떤 Linux PC에서도 동작합니다.
`Integrated_Control_SW`에 대한 의존성이 없고, 절대경로를 사용하지 않으며,
설치 디렉토리 바깥에는 아무것도 쓰지 않습니다.

```
standalone/
├── laser_driver.py         드라이버 라이브러리 (직접 import 해서 사용 가능)
├── laser_cli.py            커맨드라인 도구
├── laser_gui.py            tkinter GUI
├── run_gui.sh              GUI 실행 스크립트 (venv 사용)
├── install.sh              최초 1회 설치
├── make_installer.sh       배포용 단일 파일 생성
├── 99-tamadenshi.rules     비-root USB 접근용 udev 규칙
├── laser_env.sh.example    선택적 환경설정 예시
├── requirements.txt
└── log/                    CSV 로그 (최초 기록 시 자동 생성)
```

## 다른 PC에 배포하기 (파일 1개)

배포용 자동 압축 해제 설치 파일을 생성합니다.

```bash
./make_installer.sh
```

**`laser_control_installer.sh`** (약 40 KB) 파일 하나가 생성됩니다.
**이 파일 하나만** 전달하면 됩니다. 대상 PC에서:

```bash
chmod +x laser_control_installer.sh && ./laser_control_installer.sh
```

`~/laser_control`에 압축이 풀리고 설치가 자동으로 진행됩니다. 옵션:

| 옵션 | 동작 |
|---|---|
| `--dir /opt/laser` | 다른 경로에 설치 |
| `--no-gui` | 드라이버 + CLI만 설치 (matplotlib/pandas/tk 생략) |
| `--no-sudo` | apt·udev 단계 생략 |
| `--extract-only` | 압축만 풀고 설치는 실행하지 않음 |

기존 설치본이 있으면 **덮어쓰기 전에 반드시 확인을 요청**하며,
`log/`와 `venv/`는 보존됩니다.

### 또는 이 디렉토리에서 직접 설치

```bash
./install.sh
```

**어느 방식이든 설치 후 레이저의 USB 케이블을 뽑았다가 다시 꽂아야 합니다.**
새로 등록된 udev 규칙이 적용되려면 필요한 과정입니다.

## GUI 실행

```bash
./run_gui.sh
```

`install.sh`를 실행하면 응용프로그램 메뉴에 **"Laser Control"** 항목도
등록됩니다.

GUI에서 확인할 수 있는 항목: LD/TEC 상태, 온도, Bias/Pulse 설정값,
Pulse Width, 포토다이오드 측정값.
제어할 수 있는 항목: LD, TEC, 트리거 소스, 내부 주파수, 구동 전류,
Pulse Width. 저장된 CSV를 그래프로 볼 수도 있습니다.

> **USB 장치는 한 번에 하나의 프로세스만 점유할 수 있습니다.**
> `Integrated_Control_SW` 메인 앱이 실행 중이고 연결되어 있으면 이 GUI는
> 연결할 수 없으며, 그 반대도 마찬가지입니다.

## CLI 실행

```bash
source venv/bin/activate
```

| 명령 | 동작 |
|---|---|
| `list` | 연결된 보드 목록 (인덱스, 시리얼, USB 경로) |
| `status` | 온도·전류·Pulse Width·포토다이오드 상태를 1회 출력 |
| `monitor --interval 2` | 주기적으로 상태를 출력하고 CSV에 기록 |
| `set --bias 20 --pulse 145` | 구동 전류 설정 (합산 제한 적용됨) |
| `set --temp 25 --tec on` | TEC 목표 온도 설정 및 켜기 |
| `set --trigger ext` | 트리거 소스: `pg1` / `pg2` / `ext` / `off` |
| `on` / `off` | 레이저 다이오드 켜기/끄기 |
| `pulse-width` | Pulse Width 읽기, `--set 680`으로 쓰기 |

하드웨어의 실제 출력을 바꾸는 동작은 모두 확인을 거칩니다.
스크립트에서는 `-y`로 생략할 수 있습니다.

## 보드 선택

보드가 1개면 자동으로 선택되므로 신경 쓸 필요가 없습니다.
여러 개일 때는 GUI에서 드롭다운으로 고르고, CLI에서는 다음과 같이 지정합니다.

```bash
./laser_cli.py --path 1-3.4.1:1.0 status
```

**이 보드들은 시리얼 번호를 제공하지 않습니다.** `list`를 실행하면 모두
시리얼이 `(none)`이고 제품명도 "Simple HID Device Demo"로 동일하게 나오므로,
**USB 경로가 보드를 구분하는 유일한 수단**입니다. 경로는 물리적으로 어느
포트에 꽂혀 있는지를 나타내므로 (4구 허브라면 `1-3.4.1:1.0` … `1-3.4.4:1.0`),
**어느 파장이 어느 포트에 연결되어 있는지 기록해 두고 배선을 고정**해야
합니다. `--index`는 열거 순서라서 재부팅 시 바뀔 수 있으니, 스크립트에서는
`--path`를 사용하십시오.

## 로그

드라이버는 **LD가 켜져 있는 동안** 10초마다 `log/laser_data_YYYYMMDD.csv`에
한 줄씩 기록합니다.

```
timestamp,ld_on,tec_on,temp_c,bias_ma,pulse_ma,pulse_width_ps,pd_raw,pd_current
```

**별도 설정이 필요하지 않습니다.** 최초 기록 시점에 설치 디렉토리 안에
자동으로 생성됩니다 (인스톨러로 설치했다면 `~/laser_control/log/`).

다른 경로로 바꾸려면 `laser_env.sh.example`을 `laser_env.sh`로 복사한 뒤
`LASER_LOG_DIR`을 지정하십시오. `run_gui.sh`가 이 파일을 읽어들이므로
**바탕화면 메뉴로 GUI를 실행해도 설정이 적용됩니다.** (`~/.bashrc`에
`export`를 넣는 방식은 메뉴 실행 시 적용되지 않습니다.)
CLI에서는 `source laser_env.sh`를 직접 실행하거나, 해당 셸에서
`export LASER_LOG_DIR=/data/laser_logs`를 지정하면 됩니다.

`pd_current`는 보드의 포토다이오드가 죽어 있는 경우 0이 아니라 **빈 값**으로
기록됩니다(아래 하드웨어 참고 사항 참조). 이렇게 해야 드리프트 분석 시 해당
행을 건너뛰고, 평평한 가짜 직선을 피팅하는 일이 생기지 않습니다.

## 드라이버를 직접 사용하기

```python
from laser_driver import TamadenshiLaser, list_devices

dev = list_devices()[0]
with TamadenshiLaser(name="405nm") as laser:
    laser.connect(dev["path"])
    ok, msg = laser.set_currents(bias_ma=20, pulse_ma=145)   # 합산 제한 적용
    laser.update_status()
    print(laser.status["ld_temp"], laser.status["pd_current"])
```

`with` 블록을 쓰는 것이 중요합니다. hidapi의 libusb 백엔드는 장치를 열 때
커널의 `usbhid` 드라이버를 분리(detach)하는데, 프로세스가 `close()` 없이
죽으면 장치가 분리된 상태로 남아 **케이블을 다시 꽂기 전까지 인식되지
않습니다.**

## 하드웨어 참고 사항

**합산 전류 제한.** Bias와 Pulse 전류는 물리적으로 하나의 구동 경로를
공유하므로, 매뉴얼의 200 mA 상한은 **두 값의 합**에 적용됩니다.
`set_currents()`가 이를 강제합니다. 개별 함수인 `set_bias_current()` /
`set_pulse_current()`는 합계를 알 수 없으므로 `set_currents()`를 사용하십시오.
GUI는 실시간 합계를 표시하며, GUI와 CLI 모두 제한을 넘는 값은 거부합니다.

**포토다이오드 모니터.** `pd_current`는 이 보드에서 유일하게 **실제로
측정되는** 광학량입니다. `bias`와 `pulse`는 우리가 써넣은 DAC 설정값을 그대로
되돌려 읽는 것이라 LD가 꺼져 있어도 명령한 값이 나오므로, 구조상 드리프트를
보여줄 수 없습니다.

본 사이트의 보드 4대를 측정한 결과(2026-08-09), **450 nm 보드의 모니터
포토다이오드만 정상 동작**했습니다. 375/405/473 nm는 발광이 확인된 상태에서도
정확히 0을 반환했으며, 이는 ADC 노이즈조차 없다는 뜻이므로 해당 PD 입력이
고장났거나 실장되지 않은 것으로 판단됩니다. 값을 신뢰하기 전에
`status["pd_valid"]`를 확인하십시오. 이는 보드별 특성이지 드라이버의 한계가
아니므로, **새 하드웨어에서는 다시 확인**해야 합니다.

**Pulse Width.** 읽기는 EEPROM 슬롯 `address + 128`을 사용하며, 실제 출력을
건드리지 않으므로 발광 중에도 안전합니다. 반면 슬롯 0에 **쓰기**를 하면
방출되는 펄스가 즉시 바뀌고 EEPROM에도 저장됩니다. 유효 범위는 100–10230 ps.

**바이트 오프셋.** `update_status()`의 온도·포토다이오드 필드 오프셋은
직접 작성한 값이 틀린 것으로 드러난 뒤(상온의 보드가 3.2 °C로 표시됨)
제조사의 `tmHIDLD.dll`을 디컴파일하여 복원한 것입니다. 코드의 주석에 그 근거가
기록되어 있으니 **임의로 정리하거나 삭제하지 마십시오.**

## 문제 해결

**보드를 찾을 수 없음 / 권한 거부** — udev 규칙이 적용되지 않은 경우입니다.
규칙을 다시 설치하고 케이블을 뽑았다 꽂으십시오.

```bash
sudo cp 99-tamadenshi.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger
```

**비정상 종료 후 보드가 사라짐** — 커널 드라이버가 분리된 채로 남아 있는
상태입니다. 케이블을 다시 꽂거나 아래 명령으로 재바인딩하십시오.

```bash
sudo udevadm trigger --subsystem-match=usb --action=add
```

**GUI는 연결이 안 되는데 CLI는 되는 경우(또는 반대)** — 다른 쪽이 아직 장치를
점유하고 있습니다. 한 번에 하나만 사용할 수 있습니다.

**`ImportError: No module named tkinter`** — tkinter는 pip 패키지가 아니라
시스템 패키지입니다.

```bash
sudo apt install python3-tk
```

**Pulse Width 읽기가 가끔 실패함** — 다른 명령 직후에 읽으면 보드가 이전
응답을 반환하는 경우가 있습니다. 드라이버는 범위를 벗어난 값을 캐싱하지 않고
실패로 처리하므로, 다시 시도하면 됩니다.

**`ImportError: No module named hid`** — venv를 활성화하거나
(`source venv/bin/activate`) `./install.sh`를 다시 실행하십시오.
