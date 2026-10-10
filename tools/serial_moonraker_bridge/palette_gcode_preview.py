#!/usr/bin/env python3
"""Offline, read-only G-code extrusion preview for a future Palette planner.

WARNING: time is a lower-bound *nominal* travel estimate (constant speed, no
acceleration, cornering, pressure advance, firmware limits or splicing delays).
Do not use it yet to drive Palette or to modify a live Klipper print.
"""

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
PARAMETERS = re.compile(r"([XYZEFS])\s*(%s)" % NUMBER, flags=re.IGNORECASE)
COMMAND = re.compile(r"^\s*([GMT]\d+)(?:\s|$)", flags=re.IGNORECASE)


@dataclass
class Preview:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0
    f: float = 1200.0
    units: float = 1.0  # G21 mm, G20 inches
    abs_xyz: bool = True
    abs_e: bool = True
    speed_factor: float = 1.0
    time_s: float = 0.0
    extruded_mm: float = 0.0
    moves: int = 0
    commands: int = 0
    unknown_motion: int = 0
    max_flow_mm3_s: float = 0.0
    max_extrusion_mm_s: float = 0.0
    filament_mm: float = 1.75
    layer_z: float = 0.0
    comments: Dict[str, int] = field(default_factory=dict)

    @property
    def area(self):
        return math.pi * (self.filament_mm / 2.0) ** 2

    def process(self, line):
        """Return a CSV-compatible movement result or None."""
        line = line.split(';', 1)[0].split('(', 1)[0].strip()
        if not line:
            return None
        hit = COMMAND.match(line)
        if not hit:
            return None
        cmd = hit.group(1).upper()
        args = {k.upper(): float(v) for k, v in PARAMETERS.findall(line[hit.end():])}
        self.commands += 1
        if cmd == 'G20':
            self.units = 25.4
        elif cmd == 'G21':
            self.units = 1.0
        elif cmd == 'G90':
            self.abs_xyz = True
        elif cmd == 'G91':
            self.abs_xyz = False
        elif cmd == 'M82':
            self.abs_e = True
        elif cmd == 'M83':
            self.abs_e = False
        elif cmd == 'M220' and 'S' in args:
            self.speed_factor = max(args['S'], 0.0) / 100
        elif cmd == 'G92':
            for key, attr in (("X", "x"), ("Y", "y"), ("Z", "z"), ("E", "e")):
                if key in args:
                    setattr(self, attr, args[key] * self.units)
        elif cmd in ('G2', 'G3'):
            self.unknown_motion += 1
        elif cmd == 'G4':
            # G4 P uses milliseconds; S uses seconds. Do not count as extrusion.
            dwell = re.search(r'(?:^|\s)P\s*(%s)' % NUMBER, line[hit.end():], re.I)
            self.time_s += (float(dwell.group(1)) / 1000 if dwell else args.get('S', 0.0))
        elif cmd in ('G0', 'G1'):
            if 'F' in args:
                self.f = args['F'] * self.units
            old = (self.x, self.y, self.z, self.e)
            for key, attr in (("X", "x"), ("Y", "y"), ("Z", "z"), ("E", "e")):
                if key in args:
                    is_absolute = self.abs_e if key == 'E' else self.abs_xyz
                    value = args[key] * self.units
                    setattr(self, attr, value if is_absolute else getattr(self, attr) + value)
            dx, dy, dz = self.x-old[0], self.y-old[1], self.z-old[2]
            de = self.e-old[3]
            distance = math.sqrt(dx*dx+dy*dy+dz*dz)
            if distance <= 0:
                distance = abs(de)
            v_mm_s = self.f * self.speed_factor / 60.0
            if distance > 0 and v_mm_s <= 0:
                self.unknown_motion += 1
                return None
            delta_t = distance / v_mm_s if v_mm_s > 0 else 0.0
            self.time_s += delta_t
            self.moves += 1
            positive_de = max(0.0, de)
            self.extruded_mm += positive_de
            flow = positive_de / delta_t * self.area if delta_t > 0 else 0.0
            self.max_flow_mm3_s = max(self.max_flow_mm3_s, flow)
            self.max_extrusion_mm_s = max(self.max_extrusion_mm_s, positive_de / delta_t if delta_t else 0)
            return (self.commands, round(self.time_s, 4), round(self.z, 4),
                    round(self.extruded_mm, 4), round(self.extruded_mm * self.area, 4),
                    round(flow, 4), round(v_mm_s, 4), round(self.speed_factor*100, 2))
        return None

    def report(self):
        return {
            "kind": "offline_extrusion_preview_v0",
            "nominal_time_s_not_klipper_time": round(self.time_s, 2),
            "extruded_filament_mm": round(self.extruded_mm, 3),
            "extruded_volume_mm3": round(self.extruded_mm*self.area, 3),
            "max_nominal_flow_mm3_s": round(self.max_flow_mm3_s, 3),
            "max_nominal_extrusion_mm_s": round(self.max_extrusion_mm_s, 3),
            "movements": self.moves,
            "parsed_commands": self.commands,
            "unsupported_arc_or_invalid_speed_moves": self.unknown_motion,
            "caveats": [
                "No accelerations, junction speed, pressure advance or firmware limits",
                "No Palette splice time, buffer model or .mafx output",
                "G2/G3 arcs and proprietary commands are not modeled",
                "Speed factors only approximate the slicer's requested feed rate",
                "E extrusion is counted independently of retracts, purge and waste",
            ],
        }


def analyze(path: Path, filament_diameter=1.75, csv_path=None):
    state = Preview(filament_mm=filament_diameter)
    output = open(csv_path, 'w', newline='', encoding='utf-8') if csv_path else None
    try:
        writer = csv.writer(output) if output else None
        if writer:
            writer.writerow(["command_number", "nominal_time_s", "z_mm", "extrusion_cumulative_mm",
                             "volume_cumulative_mm3", "nominal_flow_mm3_s", "feed_mm_s", "speed_factor_percent"])
        with path.open('r', encoding='utf-8', errors='replace') as file:
            for line in file:
                row = state.process(line)
                if writer and row:
                    writer.writerow(row)
        return state.report()
    finally:
        if output:
            output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gcode", type=Path)
    parser.add_argument("--filament-diameter", type=float, default=1.75)
    parser.add_argument("--csv", type=Path, help="Optional point-by-point graph data")
    parser.add_argument("--json", type=Path, help="Write report to file as well as stdout")
    args = parser.parse_args()
    if args.filament_diameter <= 0:
        parser.error("filament-diameter must be positive")
    report = analyze(args.gcode, args.filament_diameter, args.csv)
    result = json.dumps(report, indent=2, ensure_ascii=False)
    print(result)
    if args.json:
        args.json.write_text(result+"\n", encoding='utf-8')


if __name__ == "__main__":
    main()
