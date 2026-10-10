# Palette 3 Pro + Creality K1: experimental low-latency bridge

> **Status: development/prototype.** This does not prove lower latency on the
> actual Raspberry Pi or Creality K1. No firmware modifications. The original
> bridge, legacy Flask app, and Klipper configuration remain untouched.

## Baseline

The original `SerialMoonrakerBridge/SerialMoonrakerBridge.py` and
`tools/serial_moonraker_bridge/serial_moonraker_bridge.py` open a fresh
`HTTPConnection` for each serial G-code command, wait synchronously for
Moonraker, and close it. They also have no detailed M220 timing trace.
In the Flask-backed version, `.is_alive` was assigned instead of calling
`.is_alive()`, and broad exception handling hides diagnostic details.

## What this prototype changes

- Persistent `Moonraker` WebSocket with JSON-RPC `printer.gcode.script`.
- Bounded serial RX, command, Moonraker WS RX and serial TX worker queues.
- Exactly one serial writer; **G-code order is preserved**.
- `ok` is sent only after a successful Moonraker RPC response. An error or
  lost WebSocket **stops** the bridge; ambiguous in-flight commands are not
  replayed automatically. An RPC response is *not* confirmation that all
  already-buffered moves have physically finished.
- Exact standalone duplicate `ok` notifications are dropped. Other G-code
  response notifications are passed through for legacy compatibility.
- Diagnostic JSONL events for received `M220`, serial reception, completed
  acknowledgements, and command failures. Nothing modifies print speed on
  its own yet.
- A separate **offline/read-only** G-code preview produces CSV data for
  plotting requested extrusion/flow. It does not yet model Palette splice
  timing, Klipper queueing, acceleration, or physically accurate print time.

### Limitations and known uncertainties

1. Palette-specific handshake and asynchronous response behavior must be
   validated using **captured serial logs**, especially `M105`, `M114`,
   `M220`, and start/end sequences. Moonraker's notifications may include
   messages initiated by other clients. The prototype is **not certified**
   for uninterrupted production prints.
2. `printer.gcode.script` waits for its RPC response; removing per-command
   HTTP setup should reduce overhead, but **does not eliminate blocking
   processing of commands**. A future G-code-file-based workflow or a
   carefully validated flow-controlled pipeline is needed for big gains.
3. `M220` affects the Klipper speed factor, but queued/planned motion may
   delay the physical effect. We must measure the state and actual timing.
4. The original Mosaic Palette 3 Pro firmware was **not flashed** or edited.
   The update archive stays local and is not committed to GitHub.

## On a non-printing test setup

Stop existing bridge first; **never use two processes on one serial device**:

```bash
sudo systemctl stop serial_moonraker_bridge.service
sudo apt-get install python3-serial python3-websockets
python3 tools/serial_moonraker_bridge/serial_moonraker_bridge_v2.py \
  --serial /dev/ttyGS0 --baudrate 250000 \
  --moonraker <K1_IP>:7125 \
  --diagnostics ./palette-bridge-events.jsonl --log INFO
```

On systems with two USB-serial converters use the actual serial device
(e.g. `/dev/ttyUSB0`). This is **opt-in**; there is no automatic service
installation. Check Moonraker is reachable and backed up before testing.
Use only a short supervised test print after dry serial tests succeed.

Stop V2 with Ctrl+C. To restore the previous service:

```bash
sudo systemctl start serial_moonraker_bridge.service
```

## Local tests (no printer required)

```bash
python3 -m unittest discover -s tests -v
```

## Offline nominal G-code preview (never sends printer commands)

```bash
python3 tools/serial_moonraker_bridge/palette_gcode_preview.py example.gcode \
  --csv extrusion-preview.csv --json extrusion-preview.json
```

The CSV columns include nominal time, Z, cumulative filament usage,
volumetric flow and requested speed factor. The tool supports common
`G0/G1`, `G20/G21`, `G90/G91`, `M82/M83`, `G92`, `M220` and `G4` commands.
**Not yet supported:** arcs, vendor macros, complete dynamics, `.mafx`/splice
plan, Palette buffer sensors, actual closed-loop control. The metrics are
*preliminary indicators*, not a predictive speed-control output.

## Hardware validation checklist

1. Record firmware version, Palette slow-down percentage, speed factor in
   Fluidd/Moonraker and serial RX `M220` messages during a real splice.
2. Compare old bridge against V2 with a fixed short test print and record
   median/P95 command acknowledgement time, max CPU usage, under-runs,
   pauses and actual splice behavior.
3. Verify that both `M220 S50` and restoration to the previous speed are
   observed in the serial stream and by Klipper (`gcode_move.speed_factor`).
4. Confirm behavior on Moonraker disconnect/restart, malformed G-code and
   interruption during a splice. Test stop/recovery without a print first.
5. Only after a stable baseline, add slicer-specific planned extrusion
   lookahead, Palette splice/buffer telemetry and closed-loop control.
