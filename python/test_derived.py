"""Tests for derived.py.   Run:  python -m unittest test_derived -v"""

import math
import unittest

import derived as d


def hist(fn, seconds=60, step=0.5):
    return [(i * step, fn(i * step)) for i in range(int(seconds / step))]


def base(t):
    return {d.RPM: 800, d.IAC: 30, d.MAP: 1.2, d.TPS: 0.5, d.O2: 450, d.COOLANT: 195,
            d.MAT: 86, d.BATT: 14.0, d.SPARK: 15, d.BLM: 128, d.INT: 128, d.SPEED: 0,
            d.PW: 1280, d.CROSS: 0, "_flags": {"Closed loop": True}}


class DerivedTests(unittest.TestCase):
    def test_o2_switch_rate_handles_rollover(self):
        def f(t):
            v = base(t)
            v[d.CROSS] = int(250 + 2 * t / 0.5) % 256   # 4 switches/s, rolls over
            return v
        rate, secs = d.o2_switch_rate(hist(f, seconds=10))
        self.assertAlmostEqual(rate, 4.0, places=5)

    def test_o2_rate_needs_running_engine(self):
        def f(t):
            v = base(t)
            v[d.RPM] = 0
            return v
        self.assertIsNone(d.o2_switch_rate(hist(f)))

    def test_fuel_correction(self):
        def f(t):
            v = base(t)
            v[d.BLM], v[d.INT] = 140, 132
            return v
        self.assertEqual(d.fuel_correction(hist(f)), 16)

    def test_oscillation_finds_6s_cycle(self):
        def f(t):
            v = base(t)
            v[d.RPM] = 900 + 250 * math.sin(2 * math.pi * t / 6)
            return v
        o = d.oscillation(hist(f))
        self.assertAlmostEqual(o["period_s"], 6.0, delta=0.5)
        self.assertGreater(o["strength"], 0.8)

    def test_oscillation_at_real_frame_rate_is_not_a_multiple(self):
        def f(t):
            v = base(t)
            v[d.RPM] = 900 + 250 * math.sin(2 * math.pi * t / 6)
            return v
        o = d.oscillation(hist(f, seconds=120, step=1.5))   # 4 frames per cycle
        self.assertAlmostEqual(o["period_s"], 6.0, delta=0.1)

    def test_no_cycle_in_steady_idle(self):
        import random
        rnd = random.Random(2)

        def f(t):
            v = base(t)
            v[d.RPM] = 800 + rnd.uniform(-15, 15)
            return v
        o = d.oscillation(hist(f))
        self.assertTrue(o is None or o["strength"] < 0.5)

    def test_lead_lag_iac_reacts_after_rpm(self):
        def f(t):
            v = base(t)
            v[d.RPM] = 900 + 250 * math.sin(2 * math.pi * t / 8)
            v[d.IAC] = 50 - 30 * math.sin(2 * math.pi * (t - 1.0) / 8)   # opposite, 1 s later
            return v
        ll = d.lead_lag(hist(f), d.RPM, d.IAC)
        self.assertLess(ll["r"], -0.9)
        self.assertAlmostEqual(ll["lag_s"], 1.0, delta=0.5)

    def test_gear_ratios_cluster(self):
        pts = []
        for i in range(30):
            v = base(0)
            v[d.SPEED] = 30 + (i % 5)
            v[d.RPM] = v[d.SPEED] * (110 if i < 15 else 70)
            pts.append((i, v))
        g = d.gear_ratios(pts)
        self.assertEqual([round(x["rpm_per_mph"]) for x in g], [110, 70])

    def test_glitch_detection(self):
        def f(t):
            v = base(t)
            if abs(t - 10) < 0.01:
                v[d.TPS] = 2.5    # one-frame spike while the foot is still
            return v
        g = d.glitches(hist(f))
        self.assertIn(d.TPS, g)
        self.assertEqual(len(g[d.TPS]), 1)
        self.assertNotIn(d.COOLANT, g)

    def test_warmup_and_closed_loop(self):
        def f(t):
            v = base(t)
            v[d.COOLANT] = min(195, 70 + t * 0.5)            # 30 F/min, then levels
            v["_flags"] = {"Closed loop": v[d.COOLANT] >= 160}
            return v
        h = hist(f, seconds=600, step=2)
        w = d.warmup(h)
        self.assertAlmostEqual(w["rate_f_per_min"], 30, delta=2)
        self.assertEqual(w["levelled_at_f"], 195)
        self.assertAlmostEqual(d.time_to_closed_loop(h), 180, delta=3)

    def test_knock_events(self):
        def f(t):
            v = base(t)
            v[d.RPM], v[d.MAP] = 2500, 3.0
            v[d.SPARK] = 10 if abs(t - 5) < 0.01 else 25
            return v
        self.assertEqual(len(d.knock_retard_events(hist(f, seconds=10))), 1)

    def test_relative_load_and_calculated_rows(self):
        h = hist(base)
        self.assertAlmostEqual(d.relative_load(h, 4.6), 1.2 / 4.6)
        labels = [r[0] for r in d.calculated(h, baro_v=4.6)]
        self.assertIn("Total fuel correction", labels)
        self.assertIn("Manifold pressure / atmosphere", labels)
        self.assertIn("Airflow index", labels)


if __name__ == "__main__":
    unittest.main()
