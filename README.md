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

**`_ENV` task** — always runs, default every 500 ms. Adds these regardless of what sensors are attached:

| Field | Type | Description |
| --- | --- | --- |
| `v_in` | float | Input/battery voltage in volts, from ADC3 through the on-board 3:1 divider. |
| `pi_temp` | int | Pico core temperature in °C, from the RP2040/RP2350 internal diode on ADC4. Truncated to a whole degree, and only good to a few °C — it is a die sensor, not an ambient one. |
| `adc0`, `adc1`, `adc2` | int | Raw 12-bit readings (0–4095) from the three exposed ADC pins. Unscaled — apply whatever conversion your analog front end needs. |

**`_LSM6DSOX` task** — only runs if an LSM6DSOX IMU is detected, but then always sends all of:

| Field | Type | Description |
| --- | --- | --- |
| `a_x`, `a_y`, `a_z` | float | Accelerometer, m/s². |
| `g_x`, `g_y`, `g_z` | float | Gyroscope, deg/s. |
| `imu_temp` | float | IMU die temperature in °C. |
| `fall_cnt` | int | Running count of free-fall events flagged by the LSM6DSOX since boot. |

Everything else is conditional on I2C/OneWire probing at boot, and a field is simply absent if its sensor is missing or errored on that read. All of these ride in the `_ENV` packet:

| Sensor | Fields |
| --- | --- |
| LIS3MDL magnetometer | `mag_x`, `mag_y`, `mag_z` |
| BMP280 | `bmp_temp`, `bmp_pres` |
| BME280 | `bme280_temp`, `bme280_pres`, `bme280_humi` |
| BME680 | `bme_temp`, `bme_pres`, `bme_humi` |
| HDC302x | `hdc_temp`, `hdc_humi` |
| MAX31725 | `max31725_temp` |
| Honeywell HSC | `hsc_temp`, `hsc_pres` |
| DS18x20 (OneWire) | `temp-<addr>`, one per probe, where `<addr>` is the last two bytes of the ROM ID in hex |

Consumers should treat the field set as open: decode the map, pull `time`/`id`/`count`, and iterate the rest rather than assuming a fixed schema. That is what [wenet_ble_client.py](wenet_ble_client.py#L46-L57) does when writing the CSV log.