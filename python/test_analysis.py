"""Tests for analysis.py.   Run:  python -m unittest test_analysis -v"""

import math
import os
import tempfile
import unittest

import aldl_logger as al
import analysis as an


def params(**units):
    """Parameter dicts like the definition's, all mapped, with these units."""
    base = {"Engine RPM": "rpm", "Coolant temp": "F", "TPS": "V", "MAP": "kPa",
            "O2 sensor": "mV", "IAC position": "steps", "BLM (long trim)": "",
            "INT (short trim)": "", "Battery": "V"}
    base.update(units)
    return {n: {"name": n, "offset": i, "units": u} for i, (n, u) in enumerate(base.items())}


def history(fn, seconds=30, step=0.5):
    return [(i * step, fn(i * step)) for i in range(int(seconds / step))]


def healthy(t):
    return {"Engine RPM": 800 + 20 * math.sin(t), "Coolant temp": 195, "TPS": 0.5,
            "MAP": 35, "O2 sensor": 450 + 400 * math.sin(t * 2), "IAC position": 30,
            "BLM (long trim)": 128, "INT (short trim)": 128, "Battery": 14.0}


def titles(findings, severity=None):
    return [f.title for f in findings if severity is None or f.severity == severity]


class AnalysisTests(unittest.TestCase):
    def test_healthy_idle_is_ok(self):
        f = an.analyze(history(healthy), params())
        self.assertEqual(titles(f, an.PROBLEM) + titles(f, an.WATCH), [])
        self.assertIn("Nothing out of the ordinary", titles(f, an.OK))

    def test_hunting_with_closed_iac_points_to_air_leak(self):
        def leak(t):
            v = healthy(t)
            v["Engine RPM"] = 900 + 350 * math.sin(t)   # 550..1250
            v["IAC position"] = 0
            v["O2 sensor"] = 200
            v["BLM (long trim)"] = 156
            return v
        f = an.analyze(history(leak), params())
        p = titles(f, an.PROBLEM)
        self.assertIn("Idle is hunting", p)
        self.assertIn("IAC fully closed, idle still too high/unstable", p)
        self.assertIn("Running lean (O2 stuck low)", p)
        self.assertTrue(any("adding lots of fuel" in t for t in p))
        self.assertEqual(f[0].severity, an.PROBLEM)  # problems sorted first

    def test_iac_maxed(self):
        def starved(t):
            v = healthy(t)
            v["IAC position"] = 150
            return v
        self.assertIn("IAC very high", titles(an.analyze(history(starved), params())))

    def test_dead_coolant_sensor(self):
        def cold(t):
            v = healthy(t)
            v["Coolant temp"] = -40
            return v
        self.assertIn("Coolant temp reading is impossible",
                      titles(an.analyze(history(cold), params()), an.PROBLEM))

    def test_rpm_unmapped(self):
        p = params()
        p["Engine RPM"]["offset"] = None
        f = an.analyze(history(healthy), p)
        self.assertEqual(f[0].title, "Engine RPM isn't mapped yet")

    def test_wrong_units_are_not_judged(self):
        # Template coolant is 'raw' until converted: must not be judged as °F.
        def raw(t):
            v = healthy(t)
            v["Coolant temp"] = -40
            return v
        f = an.analyze(history(raw), params(**{"Coolant temp": "raw"}))
        self.assertNotIn("Coolant temp reading is impossible", titles(f))

    def test_unmapped_listed_and_engine_off(self):
        p = params()
        p["TPS"]["offset"] = None

        def off(t):
            v = healthy(t)
            v["Engine RPM"] = 0
            v["Battery"] = 11.5
            return v
        f = an.analyze(history(off), p)
        t = titles(f)
        self.assertIn("Engine not running", t)
        self.assertIn("Battery low", t)
        self.assertTrue(any(x.startswith("Not checked yet") and "TPS" in x for x in t))

    def test_window_only_uses_recent_data(self):
        def early_dip(t):
            v = healthy(t)
            if t < 10:
                v["Engine RPM"] = 400
            return v
        h = history(early_dip, seconds=60)
        self.assertNotIn("RPM dropping close to stalling", titles(an.analyze(h, params())))
        self.assertIn("RPM dropping close to stalling",
                      titles(an.analyze(h, params(), window=None)))

    def test_trouble_codes_diag_mode_and_open_loop(self):
        def coded(t):
            v = healthy(t)
            v["_codes"] = [(32, "Exhaust gas recirculation (EGR) failure")]
            v["_flags"] = {"Closed loop": False, "Diagnostic (shorted) mode": True,
                           "A/C clutch on": True}
            return v
        f = an.analyze(history(coded), params())
        t = titles(f)
        self.assertIn("ECM trouble code 32: Exhaust gas recirculation (EGR) failure", t)
        self.assertIn("ECM is in diagnostic (A–B shorted) mode", t)
        self.assertIn("ECM in open loop on a warm engine", t)
        self.assertIn("A/C compressor running", t)
        self.assertFalse(any("O2" in x and "switching" in x for x in t))  # skipped in open loop

    def test_codes_and_flags_survive_csv(self):
        d = al.load_definition(al.DEFAULT_DEF)
        frame = bytearray(al.SimSource().frame(100.0))
        frame[12] |= 0x20        # code 32
        frame[14] &= 0x7F        # open loop
        rows = al.decode(bytes(frame), d)
        codes, flags = al.decode_status(bytes(frame), d)
        path = os.path.join(tempfile.mkdtemp(), "log.csv")
        import csv
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "elapsed_s"] + [al.column_name(p) for p in d["parameters"]]
                       + ["trouble_codes", "status_flags", "raw_hex"])
            for i in range(30):
                w.writerow(["x", i * 0.5] + [f"{v:.3f}" for _, v, _, _ in rows]
                           + [al.codes_text(codes), al.flags_text(flags), "00"])
        h, p = an.load_log_params(path)
        self.assertEqual(h[0][1]["_codes"], [(32, "Exhaust gas recirculation (EGR) failure")])
        self.assertFalse(h[0][1]["_flags"]["Closed loop"])
        t = titles(an.analyze(h, p, window=None))
        self.assertIn("ECM trouble code 32: Exhaust gas recirculation (EGR) failure", t)
        self.assertIn("ECM in open loop on a warm engine", t)

    def test_load_log_round_trip(self):
        d = al.load_definition(al.DEMO_DEF)
        sim = al.SimSource()
        path = os.path.join(tempfile.mkdtemp(), "log.csv")
        names = [p["name"] for p in d["parameters"]]
        with open(path, "w", newline="") as f:
            import csv
            w = csv.writer(f)
            w.writerow(["time", "elapsed_s"]
                       + [al.column_name(p) for p in d["parameters"]] + ["raw_hex"])
            for i in range(40):
                rows = al.decode(sim.frame(i * 0.5), d)
                w.writerow(["x", i * 0.5] + [f"{v:.3f}" for _, v, _, _ in rows] + ["00"])
        h, p = an.load_log_params(path)
        self.assertEqual(len(h), 40)
        self.assertEqual(p["Coolant temp"]["units"], "F")   # units read from the header
        f = an.analyze(h, p, window=None)
        self.assertIn("Idle is hunting", titles(f, an.PROBLEM))

    def test_old_log_without_units_uses_fallback(self):
        path = os.path.join(tempfile.mkdtemp(), "old.csv")
        with open(path, "w") as f:
            f.write("time,elapsed_s,Engine RPM,TPS,raw_hex\n")
            for i in range(20):
                f.write(f"x,{i},{800 + (300 if i % 2 else -300)},,00\n")
        h, p = an.load_log_params(path, fallback={"Engine RPM": {"units": "rpm"}})
        self.assertEqual(p["Engine RPM"]["units"], "rpm")
        self.assertIsNone(p["TPS"]["offset"])               # empty column = not mapped
        self.assertIn("Idle is hunting", titles(an.analyze(h, p, window=None)))


if __name__ == "__main__":
    unittest.main()
