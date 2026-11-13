"""Tests for stall_capture.py.   Run:  python -m unittest test_stall_capture -v"""

import os
import random
import tempfile
import unittest

import aldl_logger as al
import analysis
import stall_capture as sc


def run_demo(mode, seconds=40, dt=1.25, start_at=15, seed=1, t0=100.0):
    random.seed(seed)
    sim = al.SimSource()
    d = al.load_definition(al.DEMO_DEF)
    w = sc.Watcher("Fiero")
    t = t0                          # t0=100: warm engine; t0=0: cold, open loop
    caps = []
    while t < t0 + seconds:
        if t >= t0 + start_at:
            sim.set_mode(mode, t=t0 + start_at)
        v = {n: x for n, x, _, _ in al.decode(sim.frame(t), d)}
        c = w.feed(t, v)
        if c:
            caps.append(c)
        t += dt
    return caps


class DemoStallTests(unittest.TestCase):
    def test_reference_loss(self):
        for seed in range(5):
            caps = run_demo("stall_ref", seed=seed)
            self.assertEqual(len(caps), 1)
            self.assertEqual(caps[0].verdict, sc.REFERENCE, (seed, caps[0].evidence))

    def test_fuel_starvation(self):
        for seed in range(5):
            caps = run_demo("stall_fuel", seed=seed)
            self.assertEqual(len(caps), 1)
            self.assertEqual(caps[0].verdict, sc.FUEL, (seed, caps[0].evidence))
            self.assertIn("lean", caps[0].evidence[0])

    def test_cold_engine_fuel_starvation(self):
        for dt in (0.5, 1.25):
            caps = run_demo("stall_fuel", t0=0.0, dt=dt)
            self.assertEqual(caps[0].verdict, sc.FUEL, (dt, caps[0].evidence))

    def test_slow_sag(self):
        caps = run_demo("stall", seconds=60)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].verdict, sc.SAG, caps[0].evidence)

    def test_no_capture_while_idling_or_off(self):
        self.assertEqual(run_demo("idle"), [])
        self.assertEqual(run_demo("off", start_at=0), [])


class SyntheticTests(unittest.TestCase):
    def idle(self, t):
        return {"Engine RPM": 800, "MAP": 1.3, "O2 sensor": 800 if int(t) % 2 else 150,
                "INT (short trim)": 128, "Battery": 13.9, "IAC position": 30}

    def test_power_dropout_from_gap(self):
        w = sc.Watcher()
        caps = []
        for t in range(0, 20):
            caps.append(w.feed(t, self.idle(t)))
        dead = dict(self.idle(0), **{"Engine RPM": 0, "MAP": 4.6})
        for t in (26, 27, 28, 29, 30, 31):
            caps.append(w.feed(t, dead))
        cap = next(c for c in caps if c)
        self.assertEqual(cap.verdict, sc.POWER)
        self.assertIn("data stopped", cap.evidence[0])

    def test_power_dropout_from_voltage(self):
        samples = [(t, self.idle(t)) for t in range(20)]
        samples[-1][1]["Battery"] = 9.8
        samples.append((20, dict(self.idle(0), **{"Engine RPM": 0, "MAP": 4.6})))
        self.assertEqual(sc.classify(samples, 20)[0], sc.POWER)

    def test_save_and_load(self):
        caps = run_demo("stall_ref")
        folder = tempfile.mkdtemp()
        path = sc.save(folder, caps[0])
        self.assertTrue(path.endswith(".json"))
        got = sc.load_all(folder, "Fiero")
        self.assertEqual(got[0]["verdict"], sc.REFERENCE)
        self.assertTrue(os.path.exists(got[0]["csv"]))
        hist = analysis.load_log(got[0]["csv"])
        self.assertTrue(any(v.get("Engine RPM") == 0 for _, v in hist))
        self.assertEqual(sc.tally(got), "1 stall captured: 1 reference lost")
        self.assertEqual(sc.load_all(folder, "Other car"), [])


if __name__ == "__main__":
    unittest.main()
