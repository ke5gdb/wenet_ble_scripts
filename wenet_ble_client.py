import argparse
import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
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

BLUEZ_SERVICE    = "org.bluez"
DEVICE_INTERFACE = "org.bluez.Device1"

TX_SENTINEL            = '/tmp/sstv_tx' # do not scan while SSTV is transmitting via Bluetooth to prevent choppy audio
MAX_CONNECTIONS        = 10
MAX_RECONNECT_ATTEMPTS = 10
SCAN_PAUSE_S           = 1.0
RETRY_DELAY_S          = 2.0
SHUTDOWN_TIMEOUT_S     = 10.0
UDP_PAYLOAD_LEN        = 254

# Replace each packet's 'time' with this host's clock before forwarding. Sensor payloads
# without a working RTC report wildly wrong times; disable with --no-rewrite-time to
# downlink exactly what was received.
rewrite_time = True

packet_queue: asyncio.Queue[bytearray] = asyncio.Queue(100)
json_queue:   asyncio.Queue[bytes]     = asyncio.Queue(50)
device_queue: asyncio.Queue            = asyncio.Queue(MAX_CONNECTIONS * 2)
managed_addresses: set[str]            = set()


def _packet_timestamp(received_at: datetime) -> str:
    """Host time in the same ISO 8601 basic format the sensor firmware emits."""
    return f"{received_at.strftime('%Y%m%dT%H%M%S')}.{received_at.microsecond // 1000:03d}Z"


def _log_sensor_data(decoded: dict, received_at: datetime) -> None:
    # Leading column is the host clock, not the payload's — sensor RTCs are often unset
    # or wrong. The payload's own 'time' still rides along as a key/value pair, so drift
    # stays visible instead of being silently discarded.
    time_str = received_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    parts = [time_str, str(decoded['count']), str(decoded['id'])]
    for key, value in decoded.items():
        if key in ('count', 'id'):
            continue
        parts += [key, f"{value:.3f}" if isinstance(value, float) else str(value)]
    _data_logger.info(','.join(parts))


def _apply_host_time(decoded: dict, received_at: datetime, original: bytes) -> bytes:
    """Swaps the packet's 'time' for the host's, falling back to the original packet.

    The timestamp is patched directly into the encoded bytes rather than re-encoding the
    map. The firmware packs floats as CBOR single-precision, while CPython's cbor2 emits
    doubles — a full re-encode would silently add 4 bytes per float and push real packets
    past the 254-byte frame. Patching in place leaves every other byte untouched.
    """
    original_time = decoded.get('time')
    if not isinstance(original_time, str):
        logger.warning("Packet from %s has no string 'time' — forwarding as-is",
                       decoded.get('id'))
        return original

    expected = {**decoded, 'time': _packet_timestamp(received_at)}
    try:
        old_encoded = cbor2.dumps(original_time)
        new_encoded = cbor2.dumps(expected['time'])
        if len(old_encoded) == len(new_encoded) and original.count(old_encoded) == 1:
            patched = original.replace(old_encoded, new_encoded, 1)
            # Cheap guard against a coincidental byte match elsewhere in the packet.
            if cbor2.loads(patched) == expected:
                return patched

        # Timestamp length changed, or the patch didn't verify — fall back to a full
        # re-encode and only use it if it still fits the frame.
        reencoded = cbor2.dumps(expected)
        if len(reencoded) <= UDP_PAYLOAD_LEN:
            return reencoded
        logger.warning(
            "Re-encoded packet is %d bytes (limit %d) — forwarding original",
            len(reencoded), UDP_PAYLOAD_LEN,
        )
    except Exception as e:
        logger.warning("Could not apply host time: %s", e)
    return original


def notify_handler(characteristic: BleakGATTCharacteristic, data: bytearray):
    received_at = datetime.now(timezone.utc)
    payload = bytes(data)

    try:
        decoded = cbor2.loads(data)
    except Exception as e:
        logger.warning("Could not decode sensor data: %s", e)
        decoded = None

    # A packet we can't parse is still worth downlinking — log the failure and forward
    # it untouched rather than losing telemetry to our own decoder.
    if isinstance(decoded, dict):
        try:
            _log_sensor_data(decoded, received_at)
        except Exception as e:
            logger.warning("Could not log sensor data: %s", e)
        if rewrite_time:
            payload = _apply_host_time(decoded, received_at, payload)

    try:
        packet_queue.put_nowait(bytearray(payload))
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


async def release_stale_links():
    """Drops Wenet links bluetoothd is still holding on our behalf from a previous run.

    The BLE link lives in the controller and in bluetoothd, not in this process. If we
    died without unwinding the BleakClient context — SIGKILL, an unhandled crash, a
    stopped service — the ACL stays up and bluetoothd keeps it alive. The sensor firmware
    advertises only while disconnected, so that half-dead link makes the sensor invisible
    to scanner() forever: it never re-advertises, we never see it, and the payload goes
    quiet until someone runs `bluetoothctl disconnect` by hand. Dropping the link here
    lets the sensor start advertising again within a supervision timeout.

    Only devices exposing the Wenet service UUID are touched. The payload's Bluetooth
    audio link carries SSTV and must survive this.
    """
    # Linux-only, and only reachable at all because bleak pulls dbus_fast in on this
    # platform. Anywhere else there is no BlueZ to clean up after.
    try:
        from dbus_fast import BusType, Message, MessageType, unpack_variants
        from dbus_fast.aio import MessageBus
    except ImportError:
        logger.info("dbus_fast unavailable — skipping stale link check")
        return

    def _checked(reply, what):
        if reply is None or reply.message_type is MessageType.ERROR:
            raise RuntimeError(f"{what} failed: {getattr(reply, 'body', ['no reply'])[0]}")
        return reply

    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        # Raw messages rather than proxy objects, the way bleak's own BlueZ backend does
        # it — no introspection round trip needed just to read properties.
        reply = _checked(await bus.call(Message(
            destination=BLUEZ_SERVICE,
            path="/",
            interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects",
        )), "GetManagedObjects")

        for path, interfaces in reply.body[0].items():
            props = unpack_variants(interfaces.get(DEVICE_INTERFACE, {}))
            if not props.get("Connected"):
                continue
            # Filter on the Wenet UUID: the payload's Bluetooth audio link carries SSTV
            # and must survive this.
            if WENET_SERVICE_UUID not in [u.lower() for u in props.get("UUIDs", [])]:
                continue

            address = props.get("Address", path)
            logger.warning("Stale link to %s from a previous run — disconnecting", address)
            _checked(await bus.call(Message(
                destination=BLUEZ_SERVICE,
                path=path,
                interface=DEVICE_INTERFACE,
                member="Disconnect",
            )), f"Disconnect {address}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(
            "Stale link check failed (%s) — if a sensor never appears in the scan, "
            "disconnect it manually with `bluetoothctl disconnect`", e,
        )
    finally:
        if bus is not None:
            bus.disconnect()


async def main(args: argparse.Namespace):
    await release_stale_links()

    tasks = [
        asyncio.create_task(scanner(), name="scanner"),
        *[
            asyncio.create_task(connect_device(), name=f"connect-{i}")
            for i in range(MAX_CONNECTIONS)
        ],
        asyncio.create_task(process_packets(),    name="process_packets"),
        asyncio.create_task(udp.run_client(json_queue, 55674), name="udp"),
    ]

    # SIGTERM is how systemd stops us, and its default disposition kills the process
    # outright — no context managers unwound, so every BleakClient link is left open for
    # the next run to trip over (see release_stale_links). Catching it turns a stop into
    # an ordinary cancellation, which does run the disconnects.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):
            pass  # non-POSIX; KeyboardInterrupt still covers SIGINT
    stop_task = asyncio.create_task(stop.wait(), name="stop")

    try:
        done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            logger.info("Stop requested — disconnecting")
        for t in done:
            if t is not stop_task and not t.cancelled() and t.exception() is not None:
                logger.error("Task %s failed: %s", t.get_name(), t.exception())
    finally:
        for t in (*tasks, stop_task):
            t.cancel()
        # Bounded: the disconnects run during this wait, but a wedged one must not hold
        # the service open until systemd's stop timeout fires and SIGKILLs us.
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, stop_task, return_exceptions=True), timeout=SHUTDOWN_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out after %.0fs — links may be left open",
                           SHUTDOWN_TIMEOUT_S)
        logger.info("Shutdown complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bridges Wenet BLE sensor payloads to the Wenet telemetry UDP listener.",
    )
    parser.add_argument(
        "--no-rewrite-time",
        dest="rewrite_time",
        action="store_false",
        help="Downlink packets exactly as received instead of replacing each packet's "
             "'time' field with this host's UTC clock (default: rewrite).",
    )
    args = parser.parse_args()

    rewrite_time = args.rewrite_time
    logger.info(
        "Downlink timestamps: %s",
        "host UTC clock" if rewrite_time else "payload RTC (unmodified)",
    )

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass