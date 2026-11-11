"""End-to-end tests for the guided tests, driven by the simulator.

Each test is run step by step at the real car's frame rate (one frame about
every 1.5 s), with the simulator acting out each step's demo mode.
Run:  python -m unittest test_guided_tests -v
"""

import unittest

import aldl_logger as al
import guided_tests as gt

DEF = al.load_definition(al.DEFAULT_DEF)
PARAMS = {p["name"]: p for p in DEF["parameters"]}


def decode(frame):
    v = {n: x for n, x, _, _ in al.decode(frame, DEF)}
    v["_codes"], v["_flags"] = al.decode_status(frame, DEF)
    return v


def run(test, t_start=0.0, dt=1.5, cap_s=300, tamper=None):
    """Run a guided test against the simulator; returns (Result, Runner)."""
    sim = al.SimSource()
    runner = gt.Runner(test, t_start)
    t = t_start
    while not runner.done:
        step = runner.current
        sim.set_mode(step.demo_mode, t)
        if t - runner.step_start >= min(step.seconds, cap_s):
            runner.next_step(t)          # like clicking Next / Finish on a long step
            continue
        frame = bytearray(sim.frame(t))
        if tamper:
            tamper(runner, t, frame)
        runner.feed(t, decode(bytes(frame)))
        t += dt
        runner.tick(t)
    return runner.result(PARAMS), runner


class GuidedTestTests(unittest.TestCase):
    def test_key_on_baseline_passes(self):
        r, _ = run(gt.KeyOnEngineOff())
        self.assertEqual(r.status, gt.PASS, r.details)
        self.assertAlmostEqual(r.numbers["baro_map_v"], 4.6, delta=0.05)

    def test_key_on_baseline_reports_codes(self):
        def code(runner, t, frame):
            frame[12] |= 0x20                        # code 32
        r, _ = run(gt.KeyOnEngineOff(), tamper=code)
        self.assertEqual(r.status, gt.FAIL)
        self.assertTrue(any("32" in line for line in r.details))

    def test_throttle_sweep_passes(self):
        r, runner = run(gt.ThrottleSweep())
        self.assertEqual(r.status, gt.PASS, r.details)
        self.assertGreater(r.numbers["full_v"], 4.4)
        self.assertGreaterEqual(len([s for s in runner.samples if s[2] == 1]), 5)

    def test_throttle_sweep_catches_dropout(self):
        state = {"n": 0}

        def worn(runner, t, frame):                   # one dip mid-way up the sweep
            if runner.step == 1:
                state["n"] += 1
                if state["n"] == 5:
                    frame[8] = max(0, frame[8] - 40)  # -0.78 V
        r, _ = run(gt.ThrottleSweep(), tamper=worn)
        self.assertEqual(r.status, gt.FAIL)
        self.assertTrue(any("wrong way" in line for line in r.details))

    def test_cold_sensor_match(self):
        r, _ = run(gt.ColdSensorMatch())
        self.assertEqual(r.status, gt.PASS, r.details)

    def test_cold_sensor_match_needs_cold_engine(self):
        r, _ = run(gt.ColdSensorMatch(), t_start=200)
        self.assertEqual(r.status, gt.INCONCLUSIVE)

    def test_warmup(self):
        r, _ = run(gt.WarmUp(), cap_s=120)
        self.assertEqual(r.status, gt.PASS, r.details)
        self.assertIsNotNone(r.numbers["closed_loop_after_s"])

    def test_idle_quality_flags_hunting_and_rhythm(self):
        r, _ = run(gt.IdleQuality(), t_start=200)
        self.assertEqual(r.status, gt.FAIL)
        text = " ".join(r.details)
        self.assertIn("Idle is hunting", text)
        self.assertIn("cycles every", text)

    def test_load_response(self):
        r, _ = run(gt.LoadResponse(), t_start=200)
        self.assertIn(r.status, (gt.WATCH, gt.FAIL))
        self.assertIn("A/C on dip_rpm", r.numbers)

    def test_hold_2000(self):
        r, _ = run(gt.Hold2000(), t_start=200)
        self.assertIn(r.status, (gt.PASS, gt.WATCH), r.details)
        self.assertGreater(r.numbers["o2_switches_per_s_2000"], 0.7)

    def test_hold_2000_needs_rpm(self):
        test = gt.Hold2000()
        test.steps = [gt.Step(s.text, s.seconds, "idle") for s in test.steps]   # never revved
        r, _ = run(test, t_start=200)
        self.assertEqual(r.status, gt.INCONCLUSIVE)

    def test_charging(self):
        r, _ = run(gt.Charging(), t_start=200)
        self.assertEqual(r.status, gt.PASS, r.details)

    def test_stall_catcher_stops_and_explains(self):
        r, runner = run(gt.StallCatcher(), t_start=200)
        self.assertTrue(runner.done)
        self.assertEqual(r.status, gt.FAIL)
        self.assertTrue(any("Engine RPM" in line for line in r.details))

    def test_road_test(self):
        r, _ = run(gt.RoadTest(), t_start=200, cap_s=400)
        self.assertIn(r.status, (gt.PASS, gt.WATCH), r.details)
        self.assertTrue(any("Gears seen" in line for line in r.details))

    def test_every_test_has_steps_and_demo_modes(self):
        for t in gt.ALL_TESTS:
            self.assertTrue(t.steps, t.id)
            for s in t.steps:
                self.assertIn(s.demo_mode, al.SimSource.MODES, (t.id, s.demo_mode))


if __name__ == "__main__":
    unittest.main()
