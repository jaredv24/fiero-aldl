"""Tests for vacuum.py and leak_hunt.py.   Run:  python -m unittest test_vacuum -v"""

import json
import unittest

import aldl_logger as al
import elm_sim
import leak_hunt as lh
import obd_source
import vacuum
import vehicle_profile as vp
from test_obd import Clock, collect

GTP = next(p for _, p in vp.list_profiles() if "GTP" in p["vehicle"]["name"])


class VacuumMathTests(unittest.TestCase):
    def test_kpa_and_boost(self):
        self.assertAlmostEqual(vacuum.from_kpa(31, 101), 20.67, places=2)
        self.assertLess(vacuum.from_kpa(150, 100), 0)                 # supercharger boost
        self.assertAlmostEqual(vacuum.boost_psi(vacuum.from_kpa(150, 100)), 7.25, places=1)

    def test_volts_estimate(self):
        self.assertAlmostEqual(vacuum.from_volts(4.6, 4.6), 0)
        self.assertAlmostEqual(vacuum.from_volts(1.38, 4.6), 20.9, places=1)
        self.assertIsNone(vacuum.from_volts(1.4, None))

    def test_tracker_learns_baro_with_engine_off(self):
        t = vacuum.Tracker()
        v = {"MAP": 4.4, "Engine RPM": 0}
        t.update(v)
        self.assertEqual(t.baro_v, 4.4)
        self.assertAlmostEqual(v[vacuum.NAME], 0)
        v = {"MAP": 1.32, "Engine RPM": 800}
        t.update(v)
        self.assertAlmostEqual(v[vacuum.NAME], 20.9, places=1)
        v = {"MAP": 31, "Engine RPM": 760, "Barometric pressure": 101}
        t.update(v)
        self.assertAlmostEqual(v[vacuum.NAME], 20.67, places=2)

    def test_add_param_only_with_map(self):
        d = al.load_definition(al.DEFAULT_DEF)
        vacuum.add_param(d)
        vacuum.add_param(d)
        self.assertEqual(sum(p["name"] == vacuum.NAME for p in d["parameters"]), 1)
        self.assertEqual(vacuum.add_param({"parameters": []})["parameters"], [])


class ClassifyTests(unittest.TestCase):
    def test_patterns(self):
        steady = [19 + 0.2 * (i % 2) for i in range(20)]
        self.assertEqual(vacuum.classify(steady)[0], "healthy")
        self.assertEqual(vacuum.classify([13 + 0.2 * (i % 2) for i in range(20)])[0], "low")
        rpm = [800 + 150 * ((i % 10) - 5) / 5 for i in range(30)]
        drift = [18 + (r - 800) / 50 for r in rpm]
        self.assertEqual(vacuum.classify(drift, rpm)[0], "drifting")
        flicker = [18 + (3 if i % 2 else -3) for i in range(20)]
        self.assertEqual(vacuum.classify(flicker)[0], "fluctuating")
        self.assertIsNone(vacuum.classify([18] * 3))

    def test_altitude_scales_normal(self):
        # 15 inHg is low at sea level but fine around 5,000 ft (baro ~84 kPa)
        steady = [15.0 + 0.1 * (i % 2) for i in range(20)]
        self.assertEqual(vacuum.classify(steady)[0], "low")
        self.assertEqual(vacuum.classify(steady, baro_kpa=84)[0], "healthy")


class DemoFindingTests(unittest.TestCase):
    def test_fiero_demo_hunt_drifts(self):
        sim = al.SimSource()
        d = vacuum.add_param(al.load_definition(al.DEMO_DEF))
        tr = vacuum.Tracker()
        hist, t = [], 0.0
        for _ in range(60):
            v = {n: x for n, x, _, _ in al.decode(sim.frame(t), d)}
            v["Coolant temp"] = 190
            tr.update(v)
            hist.append((t, v))
            t += 1.5
        f = vacuum.findings(hist)
        self.assertTrue(f, "no vacuum finding")
        self.assertIn("drifts", f[0].title)
        self.assertIn("estimated", f[0].title)

    def test_obd_demo_reads_vacuum(self):
        clock = Clock(100)
        hist = collect(obd_source.ObdSource(elm_sim.SimElm(clock=clock), GTP, status_every=0),
                       160, clock)
        tr = vacuum.Tracker()
        for _, v in hist:
            tr.update(v)
        idle = [v[vacuum.NAME] for _, v in hist if v.get("Engine RPM", 0) > 400]
        self.assertTrue(idle)
        self.assertTrue(all(15 < x < 25 for x in idle), idle[:5])


def hist_from(fn, n=40, dt=1.0):
    return [(i * dt, fn(i * dt)) for i in range(n)]


class LeakHuntTests(unittest.TestCase):
    def fiero(self, t, leak_at=None):
        rich = leak_at is not None and leak_at + 1 <= t <= leak_at + 6
        return {"O2 sensor": 850 if rich else (200 if int(t) % 2 else 700),
                "INT (short trim)": 110 if rich else 128,
                "Engine RPM": 900 if rich else 800}

    def test_reacting_spot_scores_yes(self):
        hist = hist_from(lambda t: self.fiero(t, leak_at=20))
        v = lh.judge(hist, lh.Mark(20, "EGR block-off plate"))
        self.assertEqual(v.reacted, "yes", v.details)
        quiet = lh.judge(hist, lh.Mark(8, "PCV hose"))
        self.assertEqual(quiet.reacted, "no", quiet.details)
        text = lh.summary([v, quiet])
        self.assertIn("Most likely leak: EGR block-off plate", text)

    def test_waiting_until_enough_data(self):
        hist = hist_from(lambda t: self.fiero(t), n=10)
        self.assertEqual(lh.judge(hist, lh.Mark(8, "x")).reacted, "waiting")

    def test_obd_trims(self):
        def obd(t):
            rich = 21 <= t <= 26
            return {"O2 B1S1 (upstream)": 0.85 if rich else (0.2 if int(t) % 2 else 0.7),
                    "Short trim B1": -12 if rich else 2, "Short trim B2": -10 if rich else 1,
                    "Engine RPM": 760}
        v = lh.judge(hist_from(obd), lh.Mark(20, "Intake gasket, driver side"))
        self.assertEqual(v.reacted, "yes", v.details)
        self.assertTrue(any("pulled fuel" in d for d in v.details))

    def test_no_spots(self):
        self.assertEqual(lh.summary([]), "No spots marked yet.")


if __name__ == "__main__":
    unittest.main()


class DemoLeakTests(unittest.TestCase):
    def test_fiero_demo_reacts_only_at_its_leak(self):
        sim = al.SimSource()
        d = al.load_definition(al.DEMO_DEF)
        sim.spray("EGR block-off plate (valve end)", t=50)
        sim.spray("Brake booster hose", t=80)
        hist, t = [], 30.0
        while t < 100:
            hist.append((t, {n: x for n, x, _, _ in al.decode(sim.frame(t), d)}))
            t += 1.25
        good = lh.judge(hist, lh.Mark(50, "EGR block-off plate (valve end)"))
        other = lh.judge(hist, lh.Mark(80, "Brake booster hose"))
        self.assertEqual(good.reacted, "yes", good.details)
        self.assertEqual(other.reacted, "no", other.details)

    def test_gtp_demo_reacts_only_at_its_leak(self):
        clock = Clock(100)
        sim = elm_sim.SimElm(clock=clock)
        src = obd_source.ObdSource(sim, GTP, status_every=0)
        hist = collect(src, 140, clock)          # warm up first
        src.spray("Lower intake gasket, rear bank")
        t_leak = clock.t
        hist += collect(src, clock.t + 15, clock)
        src.spray("PCV hose and valve")
        t_pcv = clock.t
        hist += collect(src, clock.t + 15, clock)
        self.assertEqual(lh.judge(hist, lh.Mark(t_leak, "rear")).reacted, "yes")
        self.assertEqual(lh.judge(hist, lh.Mark(t_pcv, "pcv")).reacted, "no")
