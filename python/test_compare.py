"""Tests for compare.py.   Run:  python -m unittest test_compare -v"""

import csv
import math
import os
import tempfile
import unittest

import analysis
import compare


def fiero_log(hunting, n=120, dt=1.25):
    hist = []
    for i in range(n):
        t = i * dt
        phase = (t % 6) / 6
        if hunting:
            rpm, iac, map_v, blm = 1150 - 550 * phase, 15 + 80 * phase, 1.2 + 0.5 * phase, 140
        else:
            rpm, iac, map_v, blm = 850 + 10 * math.sin(t), 30, 1.25, 129
        if hunting and i == 100:
            rpm = 0
        hist.append((t, {"Engine RPM": rpm, "IAC position": iac, "MAP": map_v, "BLM (long trim)": blm,
                         "INT (short trim)": 128, "Coolant temp": 195, "Vehicle speed": 0,
                         "TPS": 0.52, "O2 sensor": 800 if i % 2 else 150}))
    return hist


class CompareTests(unittest.TestCase):
    def test_fix_that_helped(self):
        rows, notes = compare.compare(fiero_log(True), fiero_log(False))
        by = {r.name: r for r in rows}
        self.assertEqual(by["Idle RPM swing (std. dev.)"].verdict, compare.BETTER)
        self.assertEqual(by["Idle air valve (IAC)"].verdict, compare.BETTER)
        self.assertEqual(by["BLM (long trim)"].verdict, compare.BETTER)
        self.assertEqual(by["Stalls"].verdict, compare.BETTER)
        self.assertIn("Idle vacuum", by)
        self.assertEqual(notes, [])
        self.assertTrue(compare.verdict_text(rows).startswith("The fix looks like it helped"))

    def test_reverse_is_worse(self):
        rows, _ = compare.compare(fiero_log(False), fiero_log(True))
        self.assertTrue(compare.verdict_text(rows).startswith("Things got worse"))

    def test_short_log_note_and_missing_data(self):
        rows, notes = compare.compare(fiero_log(True, n=10), fiero_log(False))
        self.assertTrue(any("before log has only" in n for n in notes))
        self.assertEqual(compare.compare([], [])[0], [])
        self.assertIn("don't share", compare.verdict_text([]))

    def test_obd_trims(self):
        def obd(ltft):
            return [(i, {"Engine RPM": 740, "Long trim B1": ltft, "Coolant temp": 198,
                         "MAP": 31, "Barometric pressure": 100,
                         "O2 B1S1 (upstream)": 0.8 if i % 2 else 0.1}) for i in range(60)]
        by = {r.name: r for r in compare.compare(obd(11.7), obd(2.0))[0]}
        self.assertEqual(by["Long trim B1"].verdict, compare.BETTER)
        self.assertAlmostEqual(by["Idle vacuum"].after, 20.38, places=1)

    def test_reads_saved_logs(self):
        d = tempfile.mkdtemp()
        paths = []
        for name, hunting in (("before.csv", True), ("after.csv", False)):
            p = os.path.join(d, name)
            hist = fiero_log(hunting)
            cols = list(hist[0][1])
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "elapsed_s"] + cols)
                for t, v in hist:
                    w.writerow(["", f"{t:.2f}"] + [v[c] for c in cols])
            paths.append(p)
        rows, _ = compare.compare(analysis.load_log(paths[0]), analysis.load_log(paths[1]))
        self.assertIn("BLM (long trim)", [r.name for r in rows])
        self.assertTrue(compare.verdict_text(rows).startswith("The fix looks like it helped"))


if __name__ == "__main__":
    unittest.main()


class ColumnNameTests(unittest.TestCase):
    def test_names_with_parentheses_survive(self):
        f = analysis._split_column
        self.assertEqual(f("BLM (long trim)"), ("BLM (long trim)", None))
        self.assertEqual(f("Coolant temp (F)"), ("Coolant temp", "F"))
        self.assertEqual(f("O2 B1S1 (upstream) (V)"), ("O2 B1S1 (upstream)", "V"))
        self.assertEqual(f("O2 B1S1 (upstream)"), ("O2 B1S1 (upstream)", None))
        self.assertEqual(f("Vacuum (est.) (inHg)"), ("Vacuum (est.)", "inHg"))
