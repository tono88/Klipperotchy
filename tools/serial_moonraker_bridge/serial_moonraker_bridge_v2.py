#!/usr/bin/env python3
"""Experimental serial Palette -> Klipper/Moonraker bridge.

Runs alongside (not inside) the legacy bridge. Stop the legacy bridge before
using it on the SAME physical serial port. No firmware changes required.

The bridge never acknowledges a command until Moonraker's RPC has responded;
a missing/error response deliberately halts forwarding. There is no unsafe
background replay of in-flight commands after an ambiguous connection loss.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger("klipperotchy.bridge_v2")
_SPEED = re.compile(r"^\s*M220(?:\s|$)", re.IGNORECASE)
_SPEED_VALUE = re.compile(r"(?:^|\s)S\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


class BridgeError(Exception):
    """Transport/RPC failed; the sender must not be falsely acknowledged."""


@dataclass
class Command:
    line: str
    started: float
    sequence: int


class JsonlRecorder:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        payload = {"timestamp_unix": time.time(), "event": event, **fields}
        LOG.debug("%s", payload)
        if self.path:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(payload, separators=(",", ":")) + "\n")


class PaletteMoonrakerBridge:
    """Four bounded tasks: serial RX, Moonraker RPC, WS RX and serial TX.

    A single writer owns serial writes to prevent interleaved response bytes.
    The worker preserves G-code ordering (M220 MUST NOT overtake earlier G1s).
    """

    def __init__(
        self,
        serial_device: Any,
        websocket: Any,
        *,
        recorder: Optional[JsonlRecorder] = None,
        rpc_timeout: float = 15.0,
    ) -> None:
        self.serial = serial_device
        self.ws = websocket
        self.recorder = recorder or JsonlRecorder()
        self.rpc_timeout = rpc_timeout
        self.pending: Dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.sequence = 0
        self.commands: asyncio.Queue[Command] = asyncio.Queue(maxsize=8)
        self.output: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.send_lock = asyncio.Lock()

    async def serial_reader(self) -> None:
        while True:
            # pyserial's timeout must be finite. This read runs outside the event
            # loop to avoid starving WebSocket notifications and M220 processing.
            line_bytes = await asyncio.to_thread(self.serial.readline, 4096)
            if not line_bytes:
                continue
            if len(line_bytes) >= 4096 and not line_bytes.endswith(b"\n"):
                raise BridgeError("Serial line >4096 bytes; refusing truncated G-code")
            try:
                line = line_bytes.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise BridgeError("Invalid UTF-8 from serial device") from exc
            self.sequence += 1
            command = Command(line, time.monotonic(), self.sequence)
            self.recorder.record("serial_rx", sequence=command.sequence,
                                 command=line if _SPEED.match(line) else None)
            await self.commands.put(command)

    async def serial_writer(self) -> None:
        while True:
            payload, command = await self.output.get()
            try:
                # Explicitly await the actual serial write; acknowledgements are
                # measured at the device boundary, not merely when queued.
                await asyncio.to_thread(self.serial.write, payload)
                if command is not None:
                    self.recorder.record(
                        "ack_written", sequence=command.sequence,
                        duration_ms=round((time.monotonic()-command.started)*1000, 3),
                        is_m220=bool(_SPEED.match(command.line)),
                    )
            finally:
                self.output.task_done()

    async def websocket_reader(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    LOG.warning("Moonraker sent non-JSON data")
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and msg["id"] in self.pending:
                    future = self.pending.pop(msg["id"])
                    if not future.done():
                        future.set_result(msg)
                elif msg.get("method") == "notify_gcode_response":
                    # Moonraker broadcasts responses from *other* clients too.
                    # The legacy bridge forwarded them; preserve non-ack replies
                    # for compatibility while filtering exact duplicated 'ok'.
                    for item in msg.get("params", []):
                        if not isinstance(item, str):
                            continue
                        if item.strip().lower() == "ok":
                            continue
                        await self.output.put(((item.rstrip("\r\n") + "\n").encode(), None))
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(BridgeError("Moonraker WebSocket disconnected"))
            self.pending.clear()
        raise BridgeError("Moonraker WebSocket disconnected")

    async def rpc(self, method: str, params: dict) -> dict:
        self.next_id += 1
        req_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = future
        try:
            async with self.send_lock:
                await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                               "method": method, "params": params}))
            response = await asyncio.wait_for(future, timeout=self.rpc_timeout)
        except (Exception, asyncio.CancelledError):
            self.pending.pop(req_id, None)
            raise
        if "error" in response:
            raise BridgeError("Moonraker RPC returned error: %s" % response["error"])
        return response

    async def command_worker(self) -> None:
        while True:
            command = await self.commands.get()
            try:
                if command.line == "___START_JOB___":
                    await self.rpc("printer.print.start", {"filename": "RemotePrint.gcode"})
                elif command.line:
                    if _SPEED.match(command.line):
                        value = _SPEED_VALUE.search(command.line)
                        self.recorder.record("m220_received", sequence=command.sequence,
                                             target_percent=float(value.group(1)) if value else None)
                    await self.rpc("printer.gcode.script", {"script": command.line})
                # Empty lines can be acknowledged locally; no G-code is issued.
                await self.output.put((b"ok\n", command))
            except Exception as exc:
                self.recorder.record("command_failure", sequence=command.sequence,
                                     error=str(exc))
                # Stop immediately; do NOT send 'ok' for failed/uncertain RPC.
                raise BridgeError("Failed command #%s: %s" % (command.sequence, exc)) from exc
            finally:
                self.commands.task_done()

    async def serve(self, stop_event: Optional[asyncio.Event] = None) -> None:
        tasks = [asyncio.create_task(fn(), name=fn.__name__) for fn in (
            self.serial_reader, self.serial_writer, self.websocket_reader, self.command_worker)]
        stopper = asyncio.create_task(stop_event.wait(), name="stopper") if stop_event else None
        try:
            watch = tasks + ([stopper] if stopper else [])
            finished, _ = await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                if task is stopper:
                    continue
                if not task.cancelled():
                    error = task.exception()
                    if error:
                        raise error
                    raise BridgeError("Bridge worker terminated unexpectedly")
        finally:
            for task in tasks + ([stopper] if stopper else []):
                task.cancel()
            await asyncio.gather(*tasks, *([stopper] if stopper else []),
                                 return_exceptions=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="/dev/ttyGS0")
    parser.add_argument("--baudrate", type=int, default=250000)
    parser.add_argument("--moonraker", default="localhost:7125", help="host:port or ws://host:port/websocket")
    parser.add_argument("--rpc-timeout", type=float, default=15.0)
    parser.add_argument("--diagnostics", help="Local JSONL file for latency/splicing/M220 timing")
    parser.add_argument("--log", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)
    if not (0 < args.baudrate <= 1000000 and args.rpc_timeout > 0):
        parser.error("baudrate and rpc-timeout must be positive")
    return args


async def async_main(args):
    try:
        import serial
        import websockets
    except ImportError as exc:
        raise SystemExit("Install python3-serial and python3-websockets first") from exc
    url = args.moonraker if args.moonraker.startswith(("ws://", "wss://")) else "ws://%s/websocket" % args.moonraker
    LOG.info("Connecting to Moonraker at %s", url)
    # A stale/failed request is NOT retried automatically: printing requires a
    # deliberate operator decision after any ambiguous in-flight command.
    async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                  max_size=2**20) as ws:
        with serial.Serial(args.serial, args.baudrate, timeout=0.2, write_timeout=2) as device:
            LOG.info("Connected: serial=%s baudrate=%s", args.serial, args.baudrate)
            bridge = PaletteMoonrakerBridge(device, ws, recorder=JsonlRecorder(args.diagnostics),
                                           rpc_timeout=args.rpc_timeout)
            await bridge.serve()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log), format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOG.info("Stopped by operator")
    except Exception as exc:
        LOG.error("Bridge stopped safely: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
