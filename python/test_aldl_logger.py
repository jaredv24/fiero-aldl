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

    def test_fiero_a29_definition_matches_gm_spec(self):
        """Byte numbers/formulas from GM data stream A29 (GM byte N = offset N-1)."""
        d = al.load_definition(al.DEFAULT_DEF)
        self.assertEqual(d["expected_length"], 25)
        frame = bytearray(25)
        frame[3] = 40            # GM byte 4: IAC position
        frame[4] = 160           # GM byte 5: coolant, C = N*.75-40 -> 80 C
        frame[5] = 55            # GM byte 6: MPH = N
        frame[6] = 100           # GM byte 7: MAP volts = N*.0196
        frame[7] = 32            # GM byte 8: RPM = N*25
        frame[8] = 26            # GM byte 9: TPS volts = N*.0196
        frame[10] = 100          # GM byte 11: O2 mV = N*4.44
        frame[17] = 140          # GM byte 18: battery = N*.1
        frame[18] = 131          # GM byte 19: BLM
        frame[20] = 64           # GM byte 21: spark = N*90/256
        frame[21] = 128          # GM byte 22: EGR % = N/2.56
        frame[22] = 152          # GM byte 23: MAT table -> 40 C
        frame[23], frame[24] = 1, 2  # GM bytes 24-25: base pulse width MSB/LSB
        v = {n: x for n, x, _, _ in al.decode(bytes(frame), d)}
        self.assertEqual(v["IAC position"], 40)
        self.assertAlmostEqual(v["Coolant temp"], 80 * 1.8 + 32)
        self.assertEqual(v["Vehicle speed"], 55)
        self.assertAlmostEqual(v["MAP"], 1.96)
        self.assertEqual(v["Engine RPM"], 800)
        self.assertAlmostEqual(v["TPS"], 26 * 0.0196)
        self.assertAlmostEqual(v["O2 sensor"], 444)
        self.assertAlmostEqual(v["Battery"], 14.0)
        self.assertEqual(v["BLM (long trim)"], 131)
        self.assertAlmostEqual(v["Spark advance"], 22.5)
        self.assertAlmostEqual(v["EGR duty cycle"], 50)
        self.assertAlmostEqual(v["Manifold air temp"], 40 * 1.8 + 32)
        self.assertEqual(v["Base pulse width (raw)"], 258)

    def test_fiero_codes_and_flags(self):
        d = al.load_definition(al.DEFAULT_DEF)
        frame = bytearray(25)
        frame[11] = 0x80         # MALFFLG1 bit 7: code 12
        frame[12] = 0x20         # MALFFLG2 bit 5: code 32 EGR
        frame[13] = 0x40         # MALFFLG3 bit 6: code 44
        frame[14] = 0x80         # MWAF1 bit 7: closed loop
        frame[15] = 0x00         # MCU1IO bit 5 = 0 means A/C requested (inverted)
        frame[16] = 0x01         # MCU2IO bit 0: A/C clutch
        codes, flags = al.decode_status(bytes(frame), d)
        self.assertEqual([c for c, _ in codes], [12, 32, 44])
        self.assertTrue(flags["Closed loop"])
        self.assertTrue(flags["A/C requested"])
        self.assertTrue(flags["A/C clutch on"])
        self.assertFalse(flags["Park/Neutral"])
        self.assertEqual(al.codes_text(codes).split(";")[1].strip(),
                         "32:Exhaust gas recirculation (EGR) failure")


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
