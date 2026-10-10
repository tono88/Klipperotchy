"""Unprivileged simulated Moonraker/serial tests (no Raspberry/K1 needed)."""
import asyncio
import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "serial_moonraker_bridge"))
from serial_moonraker_bridge_v2 import BridgeError, JsonlRecorder, PaletteMoonrakerBridge


class FakeSerial:
    def __init__(self, messages):
        self.inbound = queue.Queue()
        for message in messages:
            self.inbound.put(message.encode() + b"\n")
        self.output = []
        self.lock = threading.Lock()

    def readline(self, size):
        try:
            return self.inbound.get(timeout=0.05)[:size]
        except queue.Empty:
            return b""

    def write(self, payload):
        with self.lock:
            self.output.append(payload)
        return len(payload)


class FakeWebSocket:
    def __init__(self, *, fail_on=None):
        self.inbound = asyncio.Queue()
        self.requests = []
        self.fail_on = fail_on

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.inbound.get()

    async def send(self, raw):
        obj = json.loads(raw)
        self.requests.append(obj)
        # Deliberately send a gcode 'ok' notification to test deduplication.
        await self.inbound.put(json.dumps({"method": "notify_gcode_response", "params": ["ok"]}))
        reply = {"id": obj["id"], "jsonrpc": "2.0", "result": "ok"}
        if self.fail_on and self.fail_on in obj["params"].get("script", ""):
            reply = {"id": obj["id"], "error": {"message": "rejected"}}
        await self.inbound.put(json.dumps(reply))


async def wait_for(condition, timeout=2):
    start = time.monotonic()
    while not condition():
        if time.monotonic() - start > timeout:
            raise AssertionError("Timed out waiting for fake serial output")
        await asyncio.sleep(0.005)


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_ordered_gcode_and_m220_ack(self):
        serial = FakeSerial(["G1 X20 F900", "M220 S50", "M220 S100"])
        websocket = FakeWebSocket()
        bridge = PaletteMoonrakerBridge(serial, websocket)
        stop = asyncio.Event()
        task = asyncio.create_task(bridge.serve(stop))
        await wait_for(lambda: len(serial.output) == 3)
        stop.set()
        await asyncio.wait_for(task, 2)
        self.assertEqual(serial.output, [b"ok\n"] * 3)
        self.assertEqual([r["method"] for r in websocket.requests], ["printer.gcode.script"] * 3)
        self.assertEqual([r["params"]["script"] for r in websocket.requests],
                         ["G1 X20 F900", "M220 S50", "M220 S100"])

    async def test_special_start_command(self):
        serial = FakeSerial(["___START_JOB___"])
        websocket = FakeWebSocket()
        bridge = PaletteMoonrakerBridge(serial, websocket)
        stop = asyncio.Event()
        task = asyncio.create_task(bridge.serve(stop))
        await wait_for(lambda: serial.output)
        stop.set()
        await task
        self.assertEqual(websocket.requests[0]["method"], "printer.print.start")
        self.assertEqual(websocket.requests[0]["params"], {"filename": "RemotePrint.gcode"})

    async def test_failed_rpc_never_acks(self):
        serial = FakeSerial(["G1 X20", "M220 S50"])
        websocket = FakeWebSocket(fail_on="G1 X20")
        bridge = PaletteMoonrakerBridge(serial, websocket)
        with self.assertRaises(BridgeError):
            await asyncio.wait_for(bridge.serve(), 2)
        self.assertEqual(serial.output, [])
        self.assertEqual(len(websocket.requests), 1)

    async def test_notifications_forwarded_once(self):
        serial = FakeSerial(["M105"])
        websocket = FakeWebSocket()
        bridge = PaletteMoonrakerBridge(serial, websocket)
        stop = asyncio.Event()
        task = asyncio.create_task(bridge.serve(stop))
        await wait_for(lambda: serial.output)
        await websocket.inbound.put(json.dumps({"method": "notify_gcode_response",
                                                "params": ["T:210.0 /210.0"]}))
        await wait_for(lambda: len(serial.output) == 2)
        stop.set()
        await task
        self.assertEqual(serial.output, [b"ok\n", b"T:210.0 /210.0\n"])

    async def test_diagnostics_m220(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            recorder = JsonlRecorder(str(Path(temp)/"events.jsonl"))
            serial = FakeSerial(["M220 S45"])
            websocket = FakeWebSocket()
            bridge = PaletteMoonrakerBridge(serial, websocket, recorder=recorder)
            stop = asyncio.Event()
            task = asyncio.create_task(bridge.serve(stop))
            await wait_for(lambda: serial.output)
            stop.set()
            await task
            events = [json.loads(x) for x in (Path(temp)/"events.jsonl").read_text().splitlines()]
            self.assertEqual([e["target_percent"] for e in events if e["event"] == "m220_received"], [45.0])
            self.assertEqual(len([e for e in events if e["event"] == "ack_written"]), 1)


if __name__ == "__main__":
    unittest.main()
