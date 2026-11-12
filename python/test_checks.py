"""Tests for checks.py.   Run:  python -m unittest test_checks -v"""

import tempfile
import unittest

import checks
import vehicle_profile as vp

FIERO = next(p for _, p in vp.list_profiles() if "Fiero" in p["vehicle"]["name"])
GTP = next(p for _, p in vp.list_profiles() if "GTP" in p["vehicle"]["name"])


class JudgeTests(unittest.TestCase):
    def test_profile_normals(self):
        fp = checks.BY_ID["fuel_pressure"]
        self.assertEqual(checks.judge(fp, "42", FIERO)[0], checks.OK)
        v, text = checks.judge(fp, "30", FIERO)
        self.assertEqual(v, checks.PROBLEM)
        self.assertIn("Fiero", text)
        self.assertEqual(checks.judge(fp, "42", GTP)[0], checks.PROBLEM)    # GTP wants 48-55

    def test_single_value_spec_allows_one_degree(self):
        bt = checks.BY_ID["base_timing"]
        self.assertEqual(checks.judge(bt, "11", FIERO)[0], checks.OK)
        self.assertEqual(checks.judge(bt, "6 BTDC", FIERO)[0], checks.PROBLEM)

    def test_general_rule_when_profile_has_none(self):
        v, text = checks.judge(checks.BY_ID["vacuum_gauge"], "14", FIERO)
        self.assertEqual(v, checks.PROBLEM)
        self.assertIn("general rule", text)

    def test_compression_spread(self):
        c = checks.BY_ID["compression"]
        self.assertEqual(checks.judge(c, "150,148,152,149,151,147")[0], checks.OK)
        v, text = checks.judge(c, "150, 148, 152, 110, 151, 147")
        self.assertEqual(v, checks.PROBLEM)
        self.assertIn("cylinder #4", text)

    def test_idle_and_leakdown_use_earlier_reading(self):
        earlier = {"fuel_pressure": 44}
        self.assertEqual(checks.judge(checks.BY_ID["fuel_pressure_idle"], "37", FIERO, earlier)[0], checks.OK)
        self.assertEqual(checks.judge(checks.BY_ID["fuel_pressure_idle"], "44", FIERO, earlier)[0], checks.WATCH)
        self.assertEqual(checks.judge(checks.BY_ID["fuel_pressure_hold"], "8", FIERO, earlier)[0], checks.PROBLEM)

    def test_kinds(self):
        self.assertIn("base_timing", [c.id for c in checks.for_kind("aldl")])
        self.assertNotIn("base_timing", [c.id for c in checks.for_kind("obd2")])


class StoreTests(unittest.TestCase):
    def test_saved_per_vehicle_and_text(self):
        d = tempfile.mkdtemp()
        s = checks.Store(d, FIERO["vehicle"]["name"])
        s.add("fuel_pressure", "44", profile=FIERO)
        e = s.add("fuel_pressure_idle", "43.5", profile=FIERO)
        self.assertEqual(e["verdict"], checks.WATCH)          # used the saved key-on reading
        s.add("note", "", note="EGR block-off gasket looked torn")
        self.assertEqual(len(s.entries()), 3)
        self.assertEqual(checks.Store(d, GTP["vehicle"]["name"]).entries(), [])
        text = s.text()
        self.assertIn("EGR block-off gasket looked torn", text)
        self.assertIn("Fuel pressure at idle", text)
        s.delete(0)
        self.assertEqual(len(s.entries()), 2)


if __name__ == "__main__":
    unittest.main()
