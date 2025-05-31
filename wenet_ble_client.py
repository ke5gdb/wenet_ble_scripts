import argparse
import asyncio
import signal
import sys
import struct
import json
import datetime
import wenet_ble_udp as udp

import bleak
from bleak import BleakClient, BleakScanner, uuids
from bleak.backends.characteristic import BleakGATTCharacteristic

# UUIDs to filter on
WENET_SERVICE_UUID = "fb63feb8-31ad-451d-a587-9fc20f9c8add"
service_uuids = [WENET_SERVICE_UUID]
WENET_SENSOR_CHAR = "3d235f0e-61f8-4455-89c6-2f7d73c33178"

# Queue for packets to send to the UDP server after processing into a JSON frame
packet_queue = asyncio.Queue(50)
# Queue for JSON packets to send to the UDP server
json_queue = asyncio.Queue(50)
# Queue for devices found by the scanner
device_queue = asyncio.Queue(50)
# Lock for when a connection is being made
connection_lock = asyncio.Lock()

def process_single_packet(data: bytearray):
    header = data[0:3]
    payload = data[3:].ljust(16, b'\xff')
    cur_time = datetime.datetime.now(datetime.timezone.utc)
    calc_time = (cur_time.hour * 3600) + (cur_time.minute * 60) + cur_time.second
    calc_time = (calc_time * 1000) + (cur_time.microsecond // 1000)
    calc_time = struct.pack('<I', calc_time)
    return header + calc_time + payload

def decode_packet(data: bytearray):
    header = '<BH'
    payload_len = len(data) - struct.calcsize(header)
    struct_format = f"{header}{payload_len}s"
    payload_id, sequence_num, payload = struct.unpack(struct_format, data)
    return f"{payload_id}, {sequence_num}, {payload}\n".encode()

def signal_handler(sig, frame):
    print('Got CTRL-C, cleaning up and shutting down...')
    sys.exit(0)

def notify_handler(characteristic: BleakGATTCharacteristic, data: bytearray):
    """Callback for when a notification is received."""
    packet_queue.put_nowait(process_single_packet(data))

    # Debug info
    # print(decode_packet(data))

async def scanner(connection_cnt):
    while True:
        # Lock connecting while scanning
        await connection_lock.acquire()
        async with BleakScanner() as scanner:
            # Acquire a new connection
            await connection_cnt.acquire()
            print("Scanner started...")
            try:
                async for (device, adv_data) in scanner.advertisement_data():
                    if(adv_data.rssi < -100):
                        continue
                    if(WENET_SERVICE_UUID in adv_data.service_uuids):
                        print(f"Found {device}, rssi: {adv_data.rssi}")
                        device_queue.put_nowait(device)
                        break
            finally:
                print("Scanner stopped")
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
            print(f"Connecting to {device}")
            await connection_cnt.acquire()
            await connection_lock.acquire()
            async with BleakClient(device, timeout=10, disconnected_callback=disconnected) as client:
                print(f"Connected to {device}")
                connection_lock.release()
                await client.start_notify(WENET_SENSOR_CHAR, notify_handler)
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
            print("Error connecting to device")
            connection_lock.release()
        finally:
            connection_cnt.release()
            print(f"Disconnected from {device}")

async def process_json(timeout):
    while True:
        payload = bytearray()
        try:
            async with asyncio.timeout(timeout):
                for i in range(11):
                    payload = payload + await packet_queue.get()
        except asyncio.TimeoutError:
            # Timeout, send whatever we have
            if(len(payload) == 0):
                # Got nothing, go back to waiting
                continue

        assert(len(payload) % 23 == 0)
        # Calculate the number of packets
        num_packets = len(payload) // 23

        # Build the binary payload
        payload = payload.ljust(253, b'\x00')
        assert(len(payload) == 253)
        binary_payload = struct.pack('>B', num_packets) + payload
        assert(len(binary_payload) == 254)

        # Send the payload to the UDP server
        json_frame = json.dumps({'type': 'WENET_TX_SEC_PAYLOAD', 'id': 55, 'repeats': 1, 'packet': list(binary_payload)})
        json_queue.put_nowait(json_frame.encode())
        
async def main(args: argparse.Namespace):
    connection_cnt = asyncio.Semaphore(args.device_count)
    tasks = []
    for i in range(args.device_count):
        tasks.append(asyncio.create_task(connect_device(connection_cnt)))
    tasks.append(asyncio.create_task(scanner(connection_cnt)))
    tasks.append(asyncio.create_task(process_json(args.timeout)))
    tasks.append(asyncio.create_task(udp.run_client(json_queue, 8888)))
    await asyncio.gather(*(tasks))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('device_count', type=int, help='Number of devices to connect to')
    parser.add_argument('--timeout', type=int, default=10, help='Timeout for processing packets')
    args = parser.parse_args()
    signal.signal(signal.SIGINT, signal_handler)
    asyncio.run(main(args))
