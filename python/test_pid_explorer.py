"""Tests for pid_explorer.py against the simulated Grand Prix.   Run:  python -m unittest test_pid_explorer -v"""

import unittest

import elm327
import elm_sim
import pid_explorer as px
import vehicle_profile as vp
from test_obd import Clock


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(0)
        self.sim = elm_sim.SimElm(clock=self.clock)
        self.elm = elm327.Elm327(self.sim, timeout=1)
        self.elm.connect()

    def test_scan_finds_demo_pids_only(self):
        found = px.scan(self.elm, "1100", "12FF")
        self.assertEqual([p for p, _ in found], list(elm_sim.SimElm.DEMO_ENHANCED))
        self.assertEqual(len(dict(found)["1190"]), 2)
        with self.assertRaises(ValueError):
            px.scan(self.elm, "2000", "1000")

    def test_scan_can_stop(self):
        self.assertEqual(px.scan(self.elm, "1100", "12FF", stop=lambda: True), [])

    def test_watch_identifies_pids(self):
        found = dict(px.scan(self.elm, "1100", "12FF"))
        ex = px.Explorer({p: len(d) for p, d in found.items()})
        modes = [("idle", 0, 60), ("rpm2500", 60, 90), ("idle", 90, 140)]
        for t in range(0, 140):
            self.clock.t = t
            mode = next(m for m, a, b in modes if a <= t < b)
            self.sim.set_mode(mode)
            self.sim.demo_switch = 110 <= t < 125
            if t == 110:
                ex.start_action("A/C on", t)
            if t == 125:
                ex.stop_action(t)
            readings = {p: self.elm.mode22_raw(p) for p in found}
            std = {"Engine RPM": self.elm.read(0x0C), "Coolant temp": self.elm.read(0x05)}
            ex.add(t, readings, std)
        res = {f.pid: f for f in ex.results()}
        rpm = res["1190"]
        self.assertEqual(rpm.matches[0][0], "Engine RPM")
        self.assertEqual(rpm.matches[0][2], "(A*256+B)*0.25")
        cool = res["12C3"]
        self.assertEqual(cool.matches[0][0], "Coolant temp")
        self.assertTrue(cool.matches[0][2].startswith("A*1.8"), cool.matches)
        sw = res["1234"]
        self.assertEqual(sw.actions[0][0], "A/C on")
        self.assertIn("switch", sw.guess())
        self.assertFalse(res["11A1"].changes)
        self.assertEqual(res["11A1"].guess(), "never changed")

    def test_profile_entry_validates(self):
        gtp = next(p for _, p in vp.list_profiles() if "GTP" in p["vehicle"]["name"])
        prof = dict(gtp, enhanced_pids=[px.to_profile_entry("Engine speed (GM)", "1190", 2,
                                                             "(A*256+B)*0.25", "rpm")])
        vp.validate(prof)


if __name__ == "__main__":
    unittest.main()


class ProfileRoundTripTests(unittest.TestCase):
    def test_saved_copy_overrides_builtin_and_shows_up_live(self):
        import copy
        import json
        import os
        import tempfile
        import obd_source
        from test_obd import collect
        gtp = next(p for _, p in vp.list_profiles() if "GTP" in p["vehicle"]["name"])
        newp = copy.deepcopy(gtp)
        newp["enhanced_pids"] = [px.to_profile_entry("Engine speed (GM)", "1190", 2, "(A*256+B)*0.25", "rpm")]
        user = tempfile.mkdtemp()
        with open(os.path.join(user, vp.safe_filename(newp)), "w") as f:
            json.dump(newp, f)
        items = vp.list_profiles(user)
        names = [p["vehicle"]["name"] for _, p in items]
        self.assertEqual(names.count(gtp["vehicle"]["name"]), 1)
        mine = next(p for _, p in items if p["vehicle"]["name"] == gtp["vehicle"]["name"])
        self.assertTrue(mine["enhanced_pids"])
        clock = Clock(100)
        hist = collect(obd_source.ObdSource(elm_sim.SimElm(clock=clock), mine, status_every=0), 110, clock)
        vals = [v.get("Engine speed (GM)") for _, v in hist if v.get("Engine speed (GM)") is not None]
        self.assertTrue(vals, "enhanced PID never showed up")
        self.assertTrue(all(500 < x < 1000 for x in vals), vals[:5])
