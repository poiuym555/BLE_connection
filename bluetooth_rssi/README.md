# Bluetooth RSSI Recorder

ESP32와 jETPAK 사이의 Bluetooth 신호 세기(RSSI)를 지정한 시간 동안 기록하는 독립 GUI입니다.
Polyvidcam을 실행하거나 설치할 필요가 없습니다. Python 3.10을 사용합니다.

## 연결 방식

Polyvidcam의 녹화 화면과 동일한 `polysync` 패키지를 사용합니다.

- USB 포트 확인: `discovery.probe_polysync_port()`와 `SerialCommandTransport`
- BLE 검색: `PolySyncDevice.scan()`
- 연결 및 해제: `PolySyncDevice.connect_jetpak()` / `disconnect_jetpak()`
- 실제 신호 세기: `PolySyncDevice.read_rssi()`

PC의 Bluetooth 어댑터를 직접 사용하지 않습니다. PC는 USB로 ESP32에 명령을 보내고,
ESP32가 jETPAK과 BLE로 연결합니다. 카메라나 동기화 마커, jETPAK 녹화는 시작하지 않습니다.

## 설치

현재 수정한 `polysync` 저장소를 이 저장소 옆에 둡니다.
`polysync`에는 `read_rssi()`와 펌웨어의 `jetpak rssi` 명령이 포함되어 있어야 합니다.

```text
C:\kaleidoscope\
  bluetooth_rssi\
  polysync\
```

Anaconda Prompt에서 이 저장소 폴더로 이동한 뒤 실행합니다.

```bat
conda create -n bluetooth_rssi python=3.10 pip -y
conda activate bluetooth_rssi
python -m pip install -r requirements.txt
```

기존 환경이 있으면 환경 생성은 생략합니다. `requirements.txt`는 옆의 `polysync`와 이 GUI를
editable 모드로 설치하므로 두 저장소의 코드 변경이 각각 반영됩니다.

실시간 측정에는 **PolySync 펌웨어 1.0.6 이상**이 필요합니다. 펌웨어 설치·업로드는
[PolySync 안내](../polysync/README.md#install-the-firmware)를 따릅니다.

## 실행

`run_bluetooth_rssi.bat`을 더블클릭하거나, 전용 환경에서 실행합니다.

```bat
conda activate bluetooth_rssi
bluetooth-rssi
```

포트를 미리 지정할 수도 있습니다.

```bat
bluetooth-rssi --port COM5
```

1. PolySync 포트를 사용하는 다른 프로그램을 닫습니다.
2. 포트를 선택하고 **Open PolySync**를 누릅니다.
3. **Find devices**에서 jETPAK을 선택하고 **Connect**를 누릅니다.
4. **Duration (s)**, **Sample interval (s)**, 저장 폴더를 정합니다.
5. **Start recording**으로 시작합니다. 시간이 끝나면 자동 종료되며 **Stop**으로 중단할 수도 있습니다.

그래프에는 실시간 RSSI(dBm)가 표시됩니다. `bluetooth_rssi_날짜-시각.csv`에 UTC 응답 수신 시각,
경과 시간, BLE 주소, RSSI, 오류를 기록합니다. 파일은 덮어쓰지 않으며 매 샘플마다 출력 버퍼를 비웁니다.
연결이 끊기면 이전 샘플을 보존하고 기록을 중단합니다. 실제 측정 시각은 시리얼 응답 지연의 영향을 받습니다.
Stop과 창 닫기는 진행 중인 시리얼 명령의 정리를 기다릴 수 있습니다.

검색 목록의 RSSI는 발견 당시 값이며, CSV에는 별도로 조회한 실시간 RSSI만 저장합니다.

## 검사

```bat
conda activate bluetooth_rssi
python -m unittest discover -s tests -v
```

테스트는 모의 장치를 사용하며 실제 BLE 연결이나 카메라를 열지 않습니다.
