"""Tests for OBD-II: decoding, the ELM327 driver (against the simulated adapter),
the data source, analysis and the guided tests.   Run:  python -m unittest test_obd -v
"""

import json
import unittest

import elm327
import elm_sim
import guided_tests as gt
import obd
import obd_analysis as oa
import obd_source
import obd_tests
import vehicle_profile as vp

GTP = next(p for _, p in vp.list_profiles() if "GTP" in p["vehicle"]["name"])


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class DecodeTests(unittest.TestCase):
    def test_standard_conversions(self):
        self.assertEqual(obd.decode_pid(0x0C, [0x0B, 0xB8]), 750)            # rpm
        self.assertAlmostEqual(obd.decode_pid(0x05, [0x7B]), 181.4, places=1)  # 83 C -> F
        self.assertAlmostEqual(obd.decode_pid(0x07, [0x90]), 12.5)             # +12.5 %
        self.assertAlmostEqual(obd.decode_pid(0x14, [0x5A, 0xFF]), 0.45)       # O2 volts
        self.assertAlmostEqual(obd.decode_pid(0x10, [0x01, 0xA4]), 4.2)        # MAF g/s

    def test_dtc_decoding(self):
        self.assertEqual(obd.dtc_from_bytes(0x01, 0x71), "P0171")
        self.assertEqual(obd.dtc_from_bytes(0x43, 0x00), "C0300")
        self.assertEqual(obd.dtc_from_bytes(0xC1, 0x23), "U0123")
        self.assertIsNone(obd.dtc_from_bytes(0, 0))
        self.assertEqual(obd.describe_dtc("P0420"), "Catalyst system efficiency below threshold (bank 1)")
        self.assertIn("Maker-specific", obd.describe_dtc("P1416"))

    def test_status_and_monitors(self):
        st = obd.decode_status([0x82, 0x07, 0xE5, 0x04])
        self.assertTrue(st["mil"])
        self.assertEqual(st["dtc_count"], 2)
        mon = dict(st["monitors"])
        self.assertEqual(mon["Evaporative system"], "not complete")
        self.assertEqual(mon["Catalyst"], "complete")

    def test_supported_bitmap(self):
        self.assertEqual(obd.supported_from_bitmap(0, [0x80, 0, 0, 0x01]), [0x01, 0x20])


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.sim = elm_sim.SimElm(clock=Clock(100))
        self.e = elm327.Elm327(self.sim, timeout=1)
        self.proto = self.e.connect()

    def test_connect_detects_vpw_and_merges_modules(self):
        self.assertEqual(self.proto, "SAE J1850 VPW")
        for pid in (0x0C, 0x10, 0x14, 0x15, 0x18):
            self.assertIn(pid, self.e.supported)

    def test_codes_vin_freeze_and_clear(self):
        self.assertEqual(self.e.stored_codes(), ["P0171", "P0174"])
        self.assertEqual(self.e.vin(), elm_sim.VIN)
        code, vals = self.e.freeze_frame([0x0C, 0x07])
        self.assertEqual(code, "P0171")
        self.assertEqual(vals[0x0C], 760)
        self.assertTrue(self.e.clear_codes())
        self.assertEqual(self.e.stored_codes(), [])
        self.assertFalse(self.e.status()["mil"])

    def test_no_data_and_errors(self):
        self.assertIsNone(self.e.mode01(0x5C))       # not supported -> NO DATA
        self.assertIsNone(self.e.mode22("4321", 1))      # a PID the demo car doesn't answer

    def test_can_style_replies_parse(self):
        lines = ["014", "0: 49 02 01 31 47 32", "1: 57 52 35 32 31 30 32", "2: 44 45 4D 4F 30 30 31"]

        class Fake:
            def __init__(self):
                self.out = b""

            def write(self, data):
                self.out = ("\r".join(lines) + "\r\r>").encode()

            def read(self, n):
                o, self.out = self.out[:n], self.out[n:]
                return o

            def close(self):
                pass
        e = elm327.Elm327(Fake(), timeout=1)
        e.protocol = "6"
        self.assertEqual(e.vin(), "1G2WR52102DEMO001")


def collect(src, seconds, clock, step=1.0):
    hist = []
    for line in src.lines():
        if line.startswith("J,"):
            j = json.loads(line[2:])
            v = dict(j["values"])
            v["_codes"], v["_flags"] = [tuple(c) for c in j["codes"]], j["flags"]
            hist.append((clock.t, v))
            clock.t += step
            if clock.t > seconds:
                break
    return hist


class SourceAndAnalysisTests(unittest.TestCase):
    def test_source_definition_and_values(self):
        clock = Clock(100)
        src = obd_source.ObdSource(elm_sim.SimElm(clock=clock), GTP, status_every=0)
        lines = []
        for line in src.lines():
            lines.append(line)
            if line.startswith("J,"):
                break
        m = json.loads(next(ln for ln in lines if ln.startswith("M,"))[2:])
        names = [p["name"] for p in m["parameters"]]
        self.assertIn("Engine RPM", names)
        self.assertIn("Battery (OBD port)", names)       # no PID 0x42 -> adapter voltage
        j = json.loads(lines[-1][2:])
        self.assertEqual([c[0] for c in j["codes"]], ["P0171", "P0174"])
        self.assertTrue(j["flags"]["Check engine light on"])

    def test_calls_run_between_rounds(self):
        clock = Clock(100)
        src = obd_source.ObdSource(elm_sim.SimElm(clock=clock), GTP, status_every=0)
        got = []
        src.call(obd_source.codes_report, got.append)
        for i, line in enumerate(src.lines()):
            if got or i > 20:
                break
        rep = got[0]
        self.assertEqual([c for c, _ in rep["stored"]], ["P0171", "P0174"])
        self.assertEqual(rep["vin"], elm_sim.VIN)
        self.assertEqual(rep["freeze"]["code"], "P0171")

    def test_analysis_links_codes_to_known_issue(self):
        clock = Clock(100)
        hist = collect(obd_source.ObdSource(elm_sim.SimElm(clock=clock), GTP, status_every=0), 160, clock)
        titles = [f.title for f in oa.analyze(hist, GTP)]
        self.assertIn("Trouble code P0171: System too lean (bank 1)", titles)
        self.assertTrue(any(t.startswith("Fuel trim B1 +") for t in titles))
        hints = " ".join(f.hint for f in oa.analyze(hist, GTP))
        self.assertIn("Intake gasket and PCV vacuum leaks", hints)


def run_test(test, t_start=100.0, dt=1.0, cap_s=200):
    clock = Clock(t_start)
    sim = elm_sim.SimElm(clock=clock)
    src = obd_source.ObdSource(sim, GTP, status_every=0)
    runner = gt.Runner(test, clock.t)
    gen = src.lines()
    while not runner.done:
        step = runner.current
        sim.set_mode(step.demo_mode, clock.t - sim.t0)
        if clock.t - runner.step_start >= min(step.seconds, cap_s):
            runner.next_step(clock.t)
            continue
        line = next(gen)
        if not line.startswith("J,"):
            continue
        j = json.loads(line[2:])
        v = dict(j["values"])
        v["_codes"], v["_flags"] = [tuple(c) for c in j["codes"]], j["flags"]
        runner.feed(clock.t, v)
        clock.t += dt
        runner.tick(clock.t)
    return runner.result({}, {"profile": GTP})


class ObdGuidedTestTests(unittest.TestCase):
    def test_key_on_reports_codes(self):
        r = run_test(obd_tests.ObdKeyOn())
        self.assertEqual(r.status, gt.FAIL)
        self.assertTrue(any("P0171" in x for x in r.details))

    def test_throttle_sweep(self):
        r = run_test(obd_tests.ObdThrottleSweep())
        self.assertEqual(r.status, gt.PASS, r.details)

    def test_hold_2500_finds_vacuum_leak(self):
        r = run_test(obd_tests.ObdHold2500())
        self.assertEqual(r.status, gt.WATCH, r.details)
        self.assertTrue(any("VACUUM LEAK" in x for x in r.details), r.details)

    def test_catalyst_healthy(self):
        r = run_test(obd_tests.ObdCatalyst())
        self.assertEqual(r.status, gt.PASS, r.details)

    def test_charging(self):
        r = run_test(obd_tests.ObdCharging())
        self.assertEqual(r.status, gt.PASS, r.details)

    def test_idle_flags_trims(self):
        r = run_test(obd_tests.ObdIdle())
        self.assertIn(r.status, (gt.WATCH, gt.FAIL))
        self.assertTrue(any("Fuel trim" in x for x in r.details), r.details)

    def test_warmup_from_cold(self):
        r = run_test(obd_tests.ObdWarmUp(), t_start=0.0, cap_s=120)
        self.assertEqual(r.status, gt.PASS, r.details)

    def test_stall(self):
        r = run_test(obd_tests.ObdStall())
        self.assertEqual(r.status, gt.FAIL)

    def test_road(self):
        r = run_test(obd_tests.ObdRoad(), cap_s=300)
        self.assertIn(r.status, (gt.FAIL, gt.WATCH, gt.PASS))
        self.assertTrue(any("Gears seen" in x for x in r.details), r.details)

    def test_demo_modes_exist(self):
        for t in obd_tests.ALL_TESTS:
            for s in t.steps:
                self.assertIn(s.demo_mode, elm_sim.SimElm.MODES, (t.id, s.demo_mode))


if __name__ == "__main__":
    unittest.main()
