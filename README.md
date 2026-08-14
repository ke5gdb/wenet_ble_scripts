# Wenet Payload BLE Bridge
Receives BLE sensor data on the Wenet payload and forwards it to a Wenet Telemetry UDP listener for downlinking.

**NOTE**: This service runs on the Wenet payload and collects data from individual BLE sensor payloads. Firmware for those sensor payloads lives at [ke5gdb/Wenet_BLE_Sensor](https://github.com/ke5gdb/Wenet_BLE_Sensor).

## Usage

### First time setup
Install the dependencies from apt and clone the repo. Both `bleak` and `cbor2` are packaged in Raspberry Pi OS Trixie, so no virtual environment is needed.
```
sudo apt update
sudo apt install -y python3-bleak python3-cbor2
git clone https://github.com/ke5gdb/wenet_payload_ble_bridge.git ~/ble_bridge
sudo rfkill unblock bluetooth
```

**NOTE**: The version of `bleak` available in Pi OS **Bookworm** is not compatible. The newer versions available in Trixie or `pip` are required. If you are attempting to install this on a Bookworm instance, it is recommended to install the packages in a venv using `pip install bleak cbor2`. The service file will need to be appropriately updated to use the Python binary from the venv.

### Run
```
cd ~/ble_bridge
python3 wenet_ble_client.py
```

## Running as a service
`wenet_ble.service` starts the client at boot and restarts it if it exits. It assumes the repo is at `/home/pi/ble_bridge` and runs as the `pi` user — edit `User`, `Group`, `WorkingDirectory`, and `ExecStart` in the unit file if your setup differs. Command-line flags such as `--no-rewrite-time` go on the end of `ExecStart`.

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

## Theory of Operation

This service performs Bluetooth Low Energy ("BLE") scans and looks for a Wenet-specific service UUID, `fb63feb8-31ad-451d-a587-9fc20f9c8add`. When a device presents
that UUID, this service will attempt to connect to it, and read data via the Nordic RF UART Service ("NUS") BLE characteristic. Data conveyed over this link is 
formatted using CBOR and the Python `cbor2` library. 

This data is then emitted locally on port 55674 so the primary Wenet transmit service can propagate the data as through the radio link as
[secondary telemetry](https://github.com/projecthorus/wenet/wiki/Modem-&-Packet-Format-Details#0x03---secondary-payload-telemetry). 

### Packet Format

Each packet is a CBOR-encoded map, sent whole as one BLE notification and forwarded as one 254-byte Wenet secondary telemetry frame (shorter packets are zero-padded). The sensor firmware asserts the encoded packet is ≤254 bytes and warns once it passes 228.

A sensor payload runs one task per sensor group, and each task emits its own packet on its own cadence, so a single device produces multiple packet types. These three fields are present in every packet regardless of task or hardware:

| Field | Type | Description |
| --- | --- | --- |
| `time` | string | Timestamp from the Pico RTC, ISO 8601 basic format — `20260726T130405.123Z`. If a PCF8523 RTC is on the I2C bus, the Pico clock is synced from it at boot; otherwise this counts from whenever the Pico powered on. |
| `id` | string | `<payload_name>_<task_name>`, e.g. `RAB_HAT_ENV`. `payload_name` defaults to `RAB_HAT` and is set per-payload via `config.json` (max 32 bytes). The BLE advertised name is this truncated to 8 bytes, so it will not always match. |
| `count` | int | Per-task packet counter, wrapping at 65536. |

Consumers should treat the field set as open: decode the map, pull `time`/`id`/`count`, and iterate the rest rather than assuming a fixed schema. That is what [wenet_ble_client.py](wenet_ble_client.py#L46-L57) does when writing the CSV log.

#### Timestamps

Sensor payloads without a PCF8523 (or with a dead backup cell) come up with an unset RTC, so their `time` field can be years off. This bridge uses its own clock instead, in both directions:

- **CSV log** — written as `<host time>,<count>,<id>,<key>,<value>,...`. The leading column is the host system clock at the moment the packet arrived. The payload's own `time` is preserved as a regular key/value pair, so RTC drift stays visible and diagnosable rather than being silently discarded.
- **Downlink** — each packet's `time` is replaced with the host's UTC clock before the packet is framed for RF, in the same `20260726T130405.123Z` format the firmware uses. Pass `--no-rewrite-time` to downlink packets exactly as received.

The rewrite patches the timestamp directly into the encoded CBOR rather than re-encoding the map. This matters: the firmware packs floats as CBOR single-precision, while CPython's `cbor2` emits doubles, so a naive re-encode silently adds 4 bytes per float — about +32 bytes on a typical `_ENV` packet, which would eventually push a well-populated payload past the 254-byte frame. Patching in place leaves every other byte identical and keeps the packet exactly the same size.

Packets that fail to decode, or that carry no usable `time`, are forwarded untouched rather than dropped — a decoder problem on this end should never cost telemetry.
