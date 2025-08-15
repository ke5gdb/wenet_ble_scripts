import sys
import asyncio
import logging

class UDPClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_con_lost):
        self.transport = None
        self.on_con_lost = on_con_lost

    def connection_made(self, transport):
        self.transport = transport
        logging.info('UDP connection established')

    def error_received(self, exc):
        logging.error('Error received:', exc)

    def send_packet(self, data):
        if self.transport is not None:
            self.transport.sendto(data)
        else:
            logging.error("Transport is not available")

    def close_conn(self):
        if self.transport is not None:
            logging.info("Closing UDP connection")
            self.transport.close()
        else:
            logging.error("Transport is not available")
            
    def connection_lost(self, exc):
        self.on_con_lost.set_result(True)
        logging.error("UDP connection closed")

async def run_client(queue, port):
    loop = asyncio.get_event_loop()
    on_con_lost = loop.create_future()
    logging.info("Starting UDP client")
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: UDPClientProtocol(on_con_lost),
        remote_addr=('127.0.0.1', port)
    )
    try:
        while True:
            data = await queue.get()
            protocol.send_packet(data)
    finally:
        protocol.close_conn()
        await on_con_lost

if __name__ == "__main__":
    print("This script is not meant to be run directly.")
    sys.exit(1)
