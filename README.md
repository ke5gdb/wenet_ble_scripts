# Wenet Payload BLE Bridge
Receives BLE sensor data on the Wenet payload and forwards it to a Wenet Telemetry UDP listener for downlinking.

## Usage

### First time setup
Install the dependencies from apt and clone the repo. Both `bleak` and `cbor2` are packaged in Raspberry Pi OS Trixie, so no virtual environment is needed.
```
sudo apt update
sudo apt install -y python3-bleak python3-cbor2
git clone https://github.com/ke5gdb/wenet_payload_ble_bridge.git ~/ble_bridge
sudo rfkill unblock bluetooth
```

### Run
```
cd ~/ble_bridge
python3 wenet_ble_client.py
```

## Running as a service
`wenet_ble.service` starts the client at boot and restarts it if it exits. It assumes the repo is at `/home/pi/ble_bridge` and runs as the `pi` user — edit `User`, `Group`, `WorkingDirectory`, and `ExecStart` in the unit file if your setup differs.

```
sudo cp ~/ble_bridge/wenet_ble.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wenet_ble.service
```

Check status and follow the logs:
```
systemctl status wenet_ble.service
journalctl -f -u wenet_ble.service
```

Stop or disable it:
```
sudo systemctl stop wenet_ble.service
sudo systemctl disable wenet_ble.service
```

Decoded sensor data is written to a timestamped `sensor_data_*.log` in the working directory (`/home/pi/ble_bridge` under the service) in addition to the journal.
