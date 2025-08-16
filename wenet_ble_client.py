import argparse
import asyncio
import signal
import sys
import struct
import json
import datetime
import wenet_ble_udp as udp
from functools import partial
import logging

import bleak
from bleak import BleakClient, BleakScanner, uuids
from bleak.backends.characteristic import BleakGATTCharacteristic

# UUIDs to filter on
WENET_SERVICE_UUID = "fb63feb8-31ad-451d-a587-9fc20f9c8add"
WENET_SERVICE_UUID_SHORT = uuids.normalize_uuid_16(0x181C)
service_uuids = [WENET_SERVICE_UUID, WENET_SERVICE_UUID_SHORT]
WENET_SENSOR_CHAR = "3d235f0e-61f8-4455-89c6-2f7d73c33178"

# Queue for packets to send to the UDP server after processing into a JSON frame
packet_queue = asyncio.Queue(50)
# Queue for JSON packets to send to the UDP server
json_queue = asyncio.Queue(50)
# Queue for data to be written to a local file
file_queue = asyncio.Queue(50)
# Queue for devices found by the scanner
device_queue = asyncio.Queue(50)
# Lock for when a connection is being made
connection_lock = asyncio.Lock()

def process_single_packet(data: bytearray, address: str = "", name: str = ""):
    header = f"{name},{address}"
    payload = data
    cur_time = datetime.datetime.now(datetime.timezone.utc)
    calc_time = (cur_time.hour * 3600) + (cur_time.minute * 60) + cur_time.second
    calc_time = (calc_time * 1000) + (cur_time.microsecond // 1000)
    return f"{header},{calc_time},".encode("utf-8") + payload

def decode_packet(data: bytearray):
    header = '<BH'
    payload_len = len(data) - struct.calcsize(header)
    struct_format = f"{header}{payload_len}s"
    payload_id, sequence_num, payload = struct.unpack(struct_format, data)
    return f"{payload_id}, {sequence_num}, {payload}\n".encode()

def signal_handler(sig, frame):
    logging.info('Got CTRL-C, cleaning up and shutting down...')
    sys.exit(0)

def notify_handler(characteristic: BleakGATTCharacteristic, data: bytearray, address: str = "", name: str = ""):
    """Callback for when a notification is received."""
    packet_queue.put_nowait((process_single_packet(data, address, name), address, name))

    # Debug info
    logging.debug(decode_packet(data))

async def scanner(connection_cnt):
    while True:
        # Lock connecting while scanning
        await connection_lock.acquire()
        async with BleakScanner() as scanner:
            # Acquire a new connection
            await connection_cnt.acquire()
            logging.info("Scanner started...")
            try:
                async for (device, adv_data) in scanner.advertisement_data():
                    if(adv_data.rssi < -120):
                        continue
                    if(WENET_SERVICE_UUID in adv_data.service_uuids or WENET_SERVICE_UUID_SHORT in adv_data.service_uuids):
                        logging.info(f"Found {device}, rssi: {adv_data.rssi}")
                        device_queue.put_nowait(device)
                        break
            finally:
                logging.info("Scanner stopped")
                # Scanning is done, release lock to allow connections
                connection_cnt.release()
                connection_lock.release()
        # Wait to reaquire the lock
        await asyncio.sleep(1)

async def connect_device(connection_cnt):
    event = asyncio.Event()
    def disconnected(client):
        event.set()
    while True:
        event.clear()
        device = await device_queue.get()
        try:
            logging.info(f"Connecting to {device}")
            await connection_cnt.acquire()
            await connection_lock.acquire()
            async with BleakClient(device, timeout=10, disconnected_callback=disconnected) as client:
                logging.info(f"Connected to {device}")
                connection_lock.release()
                notify_handler_partial = partial(notify_handler, address=device.address, name=device.name)
                await client.start_notify(WENET_SENSOR_CHAR, notify_handler_partial)
                while True:
                    await event.wait()
                    # Debug
                    if(client.is_connected):
                        # Sometimes, the device disconnects but immediately reconnects
                        # Keep the connection alive in this case
                        event.clear()
                    else:
                        break
        except(TimeoutError, bleak.exc.BleakError):
            logging.info("Error connecting to device")
            connection_lock.release()
        finally:
            connection_cnt.release()
            logging.info(f"Disconnected from {device}")

async def process_json(timeout):
    message_count = 0

    while True:
        payload = bytearray()
        try:
            async with asyncio.timeout(timeout):
                (payload, address, name) = await packet_queue.get()
        except asyncio.TimeoutError:
            # Got nothing, go back to waiting
            continue

        # Build the binary payload
        # payload[0] = payload type, 0x00 for string (we're going comma separated here)
        # payload[1] = payload length
        # payload[2:3] = message count

        _PACKET_TYPE = 0x00
        _PACKET_LEN = len(payload)
        _PACKET_COUNTER = message_count

        binary_payload = struct.pack('>BBH', _PACKET_TYPE, _PACKET_LEN, _PACKET_COUNTER) + payload

        # Send the payload to the UDP server
        json_frame = json.dumps({'type': 'WENET_TX_SEC_PAYLOAD', 'id': 55, 'repeats': 1, 'packet': list(binary_payload)})
        json_queue.put_nowait(json_frame.encode())

        file_queue.put_nowait(payload)

        logging.info(f"Adding packet to queue from {name} ({address}): {payload}")

        message_count = (message_count + 1) % 65536

async def write_file():
    with open("ble_data.log", "ab") as f:
        while True:
            data = await file_queue.get() + b"\n"
            f.write(data)
            f.flush()

async def main(args: argparse.Namespace):
    connection_cnt = asyncio.Semaphore(args.device_count)
    tasks = []
    for i in range(args.device_count):
        tasks.append(asyncio.create_task(connect_device(connection_cnt)))
    tasks.append(asyncio.create_task(scanner(connection_cnt)))
    tasks.append(asyncio.create_task(process_json(args.timeout)))
    tasks.append(asyncio.create_task(write_file()))
    tasks.append(asyncio.create_task(udp.run_client(json_queue, 55674)))
    await asyncio.gather(*(tasks))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('device_count', type=int, help='Number of devices to connect to')
    parser.add_argument('--timeout', type=int, default=10, help='Timeout for processing packets')
    parser.add_argument("-v", "--verbose", action='store_true', help="Verbose")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    signal.signal(signal.SIGINT, signal_handler)
    asyncio.run(main(args))
