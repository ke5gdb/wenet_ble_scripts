# Wenet BLE Scripts
Scripts for recieving BLE sensor data and forwarding to a Wenet Telemetry UDP listener for downlinking.

## Usage

### First time setup
Install the dependencies from apt and clone the repo. Both `bleak` and `cbor2` are packaged in Raspberry Pi OS Trixie, so no virtual environment is needed.
```
sudo apt update
sudo apt install -y python3-bleak python3-cbor2
git clone https://github.com/ke5gdb/wenet_ble_scripts.git ~/wenet_ble_scripts
```

### Run
```
cd ~/wenet_ble_scripts
python3 wenet_ble_client.py
```

## Running as a service
`wenet_ble.service` starts the client at boot and restarts it if it exits. It assumes the repo is at `/home/pi/wenet_ble_scripts` and runs as the `pi` user — edit `User`, `Group`, `WorkingDirectory`, and `ExecStart` in the unit file if your setup differs.

```
sudo cp ~/wenet_ble_scripts/wenet_ble.service /etc/systemd/system/
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

Decoded sensor data is written to a timestamped `sensor_data_*.log` in the working directory (`/home/pi/wenet_ble_scripts` under the service) in addition to the journal.
