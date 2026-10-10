import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "serial_moonraker_bridge"))
from palette_gcode_preview import Preview, analyze


class PreviewTests(unittest.TestCase):
    def test_absolute_and_relative_extrusion_and_retraction(self):
        s = Preview()
        for line in ["G21", "G90", "M82", "G1 X10 E1 F600", "G1 X20 E2", "G1 E1", "M83", "G1 X30 E0.5"]:
            s.process(line)
        self.assertAlmostEqual(s.extruded_mm, 2.5)
        self.assertAlmostEqual(s.time_s, 10/10 + 10/10 + 1/10 + 10/10)
        self.assertAlmostEqual(s.report()['extruded_volume_mm3'], 2.5*math.pi*(1.75/2)**2, places=2)

    def test_m220_changes_nominal_speed(self):
        s = Preview()
        s.process('G1 X10 E1 F600')
        s.process('M220 S50')
        row = s.process('G1 X20 E2')
        self.assertEqual(row[7], 50)
        self.assertAlmostEqual(s.time_s, 3.0)

    def test_analysis_csv(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d)/'test.gcode', Path(d)/'preview.csv'
            src.write_text('G21\nG1 X10 E1 F600\nG1 X20 E2\n', encoding='utf-8')
            report = analyze(src, csv_path=out)
            self.assertEqual(report['movements'], 2)
            self.assertEqual(len(out.read_text().splitlines()), 3)


if __name__ == '__main__':
    unittest.main()
