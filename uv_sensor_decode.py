import sys
import asyncio
import argparse
import struct
import json

def decode_uv_packet(data):
    packet = json.loads(data)
    if packet['type'] != 'WENET':
        return
    
    payload = packet['packet']
    payload_type = payload[0]
    payload_id = payload[1]
    num_packets = payload[2]

    if(payload_type != 0x3):
        return

    for i in range(num_packets):
        offset = 3 + i * 23
        sensor_id = payload[offset]

        sequence_num = bytearray(payload[offset + 1 : offset + 3])
        timestamp = bytearray(payload[offset + 3 : offset + 7])
        payload_data = bytearray(payload[offset + 7 : offset + 15])

        sequence_num = struct.unpack('<H', sequence_num)[0]
        timestamp = struct.unpack('<I', timestamp)[0]
        temp, uva, uvb, uvc = struct.unpack('<HHHH', payload_data)
        print(f"Sensor ID: {sensor_id}, Sequence: {sequence_num}, Timestamp: {timestamp}")
        print(f"Temperature: {temp*0.05-66.9:.2f} °C, UVA: {uva}, UVB: {uvb}, UVC: {uvc}")

class UDPClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_con_lost):
        self.transport = None
        self.on_con_lost = on_con_lost

    def connection_made(self, transport):
        self.transport = transport
        print('UDP connection established')

    def datagram_received(self, data, addr):
        # print("Received:", data.decode())
        decode_uv_packet(data)

    def error_received(self, exc):
        print('Error received:', exc)

    def close_conn(self):
        if self.transport is not None:
            print("Closing UDP connection")
            self.transport.close()
        else:
            print("Transport is not available")
            
    def connection_lost(self, exc):
        self.on_con_lost.set_result(True)
        print("UDP connection closed")

async def run_client(args: argparse.Namespace):
    loop = asyncio.get_event_loop()
    on_con_lost = loop.create_future()
    print(f"Starting UDP client on port {args.port}")
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPClientProtocol(on_con_lost),
        local_addr=('255.255.255.255', args.port),
    )
    try:
        while True:
            await asyncio.sleep(10)  # Keep the client running
    finally:
        protocol.close_conn()
        await on_con_lost

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('port', type=int, help='Port to read Wenet UDP packets from')
    # parser.add_argument('--timeout', type=int, default=10, help='Timeout for processing packets')
    args = parser.parse_args()
    asyncio.run(run_client(args))
