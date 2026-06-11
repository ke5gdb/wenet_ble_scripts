# Wenet BLE Scripts
Scripts for recieving BLE sensor data and forwarding to a Wenet Telemetry UDP listener for downlinking.
## Usage
### First time setup
Clone, create a python virutal enviroment, and install requirements.txt
```
git clone https://github.com/ke5gdb/wenet_ble_scripts.git
cd wenet_ble_scripts
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
### Run
```
source .venv/bin/activate
python3 wenet_ble_client.py
```

## Raspberry Pi3/Zero Instructions
The RPi 3 and RPi Zero only have two uarts, the second (miniuart) being much weaker than the primary. By default, the BLE chip uses the primary UART. For the Wenet modem to work a full speed, the miniuart should be assigned to BLE so Wenet can use the primary UART.

Add to config.txt in bootfs
```
dtoverlay=disable-bt
core_freq=250
```
