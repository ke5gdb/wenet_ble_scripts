import argparse
import asyncio
import json
import logging
import os
from datetime import datetime
import cbor2
import wenet_ble_udp as udp

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# Separate logger for decoded sensor data — CSV to both stdout and a timestamped file
_data_logger = logging.getLogger("sensor_data")
_data_logger.setLevel(logging.INFO)
_data_logger.propagate = False
_log_filename = datetime.now().strftime("sensor_data_%Y-%m-%d_%H-%M-%S.log")
for _handler in (logging.FileHandler(_log_filename), logging.StreamHandler()):
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _data_logger.addHandler(_handler)
logger.info("Sensor data log: %s", _log_filename)

WENET_SERVICE_UUID = "fb63feb8-31ad-451d-a587-9fc20f9c8add"
WENET_SENSOR_CHAR  = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # NUS TX

TX_SENTINEL            = '/tmp/sstv_tx'
MAX_CONNECTIONS        = 10
MAX_RECONNECT_ATTEMPTS = 10
SCAN_PAUSE_S           = 1.0
RETRY_DELAY_S          = 2.0
UDP_PAYLOAD_LEN        = 254

packet_queue: asyncio.Queue[bytearray] = asyncio.Queue(100)
json_queue:   asyncio.Queue[bytes]     = asyncio.Queue(50)
device_queue: asyncio.Queue            = asyncio.Queue(MAX_CONNECTIONS * 2)
managed_addresses: set[str]            = set()


def _log_sensor_data(data: bytearray) -> None:
    try:
        decoded = cbor2.loads(data)
        time_str = datetime.fromisoformat(decoded['time']).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = [time_str, str(decoded['count']), str(decoded['id'])]
        for key, value in decoded.items():
            if key in ('time', 'count', 'id'):
                continue
            parts += [key, f"{value:.3f}" if isinstance(value, float) else str(value)]
        _data_logger.info(','.join(parts))
    except Exception as e:
        logger.warning("Could not decode sensor data: %s", e)


def notify_handler(characteristic: BleakGATTCharacteristic, data: bytearray):
    _log_sensor_data(data)
    try:
        packet_queue.put_nowait(bytearray(data))
    except asyncio.QueueFull:
        logger.warning("Packet queue full — packet dropped")


async def _wait_while_tx() -> None:
    if os.path.exists(TX_SENTINEL):
        logger.info("TX in progress — scan paused")
        while os.path.exists(TX_SENTINEL):
            await asyncio.sleep(0.5)
        logger.info("TX complete — resuming scan")


async def scanner():
    """Scans for Wenet sensors and enqueues unmanaged devices, one per pass."""
    while True:
        await _wait_while_tx()
        try:
            async with BleakScanner() as s:
                logger.info("BLE scan started")
                async for device, adv_data in s.advertisement_data():
                    if os.path.exists(TX_SENTINEL):
                        logger.info("TX started — stopping scan")
                        break
                    if adv_data.rssi < -100:
                        continue
                    if WENET_SERVICE_UUID not in adv_data.service_uuids:
                        continue
                    if device.address in managed_addresses:
                        continue
                    logger.info("Found %s (RSSI %d)", device, adv_data.rssi)
                    try:
                        device_queue.put_nowait(device)
                    except asyncio.QueueFull:
                        pass
                    break  # one device per pass; restart after pause
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Scanner error: %s", e)
        await asyncio.sleep(SCAN_PAUSE_S)


async def connect_device():
    """Owns the connection lifecycle for one device, retrying on disconnect or error."""
    loop = asyncio.get_running_loop()
    while True:
        device = await device_queue.get()
        addr   = device.address
        managed_addresses.add(addr)
        logger.info("Managing %s", addr)

        failures = 0
        while failures < MAX_RECONNECT_ATTEMPTS:
            disconnect_event = asyncio.Event()

            def on_disconnect(client, _ev=disconnect_event):
                loop.call_soon_threadsafe(_ev.set)

            logger.info("Connecting to %s", addr)
            try:
                async with BleakClient(device, timeout=10, disconnected_callback=on_disconnect) as client:
                    logger.info("Connected to %s", addr)
                    await client.start_notify(WENET_SENSOR_CHAR, notify_handler)
                    failures = 0
                    await disconnect_event.wait()
                    logger.info("Disconnected from %s", addr)
            except asyncio.CancelledError:
                managed_addresses.discard(addr)
                raise
            except Exception as e:
                failures += 1
                logger.error(
                    "Connection error for %s (%d/%d): %s",
                    addr, failures, MAX_RECONNECT_ATTEMPTS, e,
                )

            if failures < MAX_RECONNECT_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_S)

        logger.warning("Giving up on %s after %d consecutive failures", addr, failures)
        managed_addresses.discard(addr)


async def process_packets():
    """Forwards each sensor packet as its own 254-byte UDP frame (matches wenet_modem scheme)."""
    while True:
        data = await packet_queue.get()
        if len(data) > UDP_PAYLOAD_LEN:
            logger.error("Packet too long (%d bytes) — discarding", len(data))
            continue
        padded = bytes(data).ljust(UDP_PAYLOAD_LEN, b'\x00')
        frame = json.dumps({
            'type':    'WENET_TX_SEC_PAYLOAD',
            'id':      55,
            'repeats': 1,
            'packet':  list(padded),
        })
        try:
            json_queue.put_nowait(frame.encode())
        except asyncio.QueueFull:
            logger.warning("JSON queue full — frame dropped")


async def main(args: argparse.Namespace):
    tasks = [
        asyncio.create_task(scanner(), name="scanner"),
        *[
            asyncio.create_task(connect_device(), name=f"connect-{i}")
            for i in range(MAX_CONNECTIONS)
        ],
        asyncio.create_task(process_packets(),    name="process_packets"),
        asyncio.create_task(udp.run_client(json_queue, 55674), name="udp"),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Shutdown complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass