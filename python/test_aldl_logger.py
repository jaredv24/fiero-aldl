"""Tests for aldl_logger.py.  Run:  python -m unittest test_aldl_logger -v"""

import os
import unittest

import aldl_logger as al


class ParseTests(unittest.TestCase):
    def test_data_line(self):
        self.assertEqual(al.parse_line("D,3,0A1BFF\r\n"), ("D", bytes([0x0A, 0x1B, 0xFF])))

    def test_length_mismatch_rejected(self):
        self.assertIsNone(al.parse_line("D,4,0A1BFF"))

    def test_bad_hex_rejected(self):
        self.assertIsNone(al.parse_line("D,1,ZZ"))

    def test_pulse_and_status(self):
        self.assertEqual(al.parse_line("P,0,4410"), ("P", (0, 4410)))
        self.assertEqual(al.parse_line("S,frames=3"), ("S", "frames=3"))
        self.assertIsNone(al.parse_line("garbage"))


class DecodeTests(unittest.TestCase):
    def test_demo_definition_round_trip(self):
        d = al.load_definition(al.DEMO_DEF)
        sim = al.SimSource()
        rows = {name: v for name, v, _, _ in al.decode(sim.frame(3.0), d)}
        self.assertTrue(500 < rows["Engine RPM"] < 1300)
        self.assertTrue(13.5 < rows["Battery"] < 14.5)

    def test_two_byte_and_missing(self):
        d = {"parameters": [
            {"name": "a", "offset": 1, "size": 2, "scale": 0.5, "add": 1},
            {"name": "b", "offset": None},
            {"name": "c", "offset": 5},
        ]}
        for p in d["parameters"]:
            p.setdefault("size", 1); p.setdefault("scale", 1); p.setdefault("add", 0)
            p.setdefault("units", ""); p.setdefault("signed", False)
        rows = al.decode(bytes([0, 0x01, 0x00, 9]), d)
        self.assertEqual(rows[0][1], 256 * 0.5 + 1)
        self.assertIsNone(rows[1][1])
        self.assertIsNone(rows[2][1])  # offset past end of frame

    def test_template_loads(self):
        d = al.load_definition(al.DEFAULT_DEF)
        self.assertTrue(all(p["offset"] is None for p in d["parameters"]))


class LiveModeTest(unittest.TestCase):
    def test_csv_log_from_replay(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        cap = os.path.join(tmp, "cap.txt")
        sim = al.SimSource()
        with open(cap, "w") as f:
            f.write("I,hello\n")
            for t in (0.0, 1.0, 2.0):
                fr = sim.frame(t)
                f.write(f"D,{len(fr)},{fr.hex().upper()}\n")
        log = os.path.join(tmp, "out.csv")
        src = al.ReplaySource(cap, speed=0)
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            al.run_live(src, al.load_definition(al.DEMO_DEF), log, None)
        with open(log) as f:
            lines = f.read().strip().splitlines()
        self.assertEqual(len(lines), 4)  # header + 3 frames
        self.assertTrue(lines[0].startswith("time,elapsed_s,Engine RPM"))


if __name__ == "__main__":
    unittest.main()
