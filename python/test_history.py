"""Tests for history.py.   Run:  python -m unittest test_history -v"""

import json
import os
import tempfile
import unittest

import checks
import history

FIERO = "1985-88 Pontiac Fiero 2.8 V6"
GTP = "2002 Pontiac Grand Prix GTP"


def save(folder, name, when, status, numbers, vehicle=FIERO, test_id="idle"):
    data = {"test": name, "id": test_id, "when": when, "status": status,
            "headline": f"{name} {status}", "numbers": numbers}
    if vehicle:
        data["vehicle"] = vehicle
    with open(os.path.join(folder, f"{test_id}-{when}.json"), "w") as f:
        json.dump(data, f)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tests, self.checks = tempfile.mkdtemp(), tempfile.mkdtemp()
        save(self.tests, "Warm idle", "20261101-100000", "fail", {"rpm_sd": 160.0, "iac": 55})
        save(self.tests, "Warm idle", "20261110-100000", "pass", {"rpm_sd": 12.0, "iac": 31})
        save(self.tests, "Old run", "20261020-100000", "watch", {"x": 1}, vehicle=None)
        save(self.tests, "OBD idle", "20261105-100000", "watch", {}, vehicle=GTP, test_id="obd_idle")
        save(self.tests, "Old OBD", "20261021-100000", "pass", {}, vehicle=None, test_id="obd_x")
        st = checks.Store(self.checks, FIERO)
        st.add("vacuum_gauge", "15", when="2026-11-01 09:00")
        st.add("vacuum_gauge", "19", when="2026-11-10 09:00")

    def test_filters_by_vehicle_and_sorts(self):
        ev = history.load(self.tests, self.checks, FIERO, is_obd=False)
        self.assertEqual([e.name for e in ev],
                         ["Old run", "Vacuum gauge at warm idle", "Warm idle",
                          "Vacuum gauge at warm idle", "Warm idle"])
        gtp = history.load(self.tests, self.checks, GTP, is_obd=True)
        self.assertEqual([e.name for e in gtp], ["Old OBD", "OBD idle"])

    def test_trends_and_changes(self):
        ev = history.load(self.tests, self.checks, FIERO)
        tr = {(n, k): (a, b) for n, k, a, b, *_ in history.trends(ev)}
        self.assertEqual(tr[("Warm idle", "rpm_sd")], (160.0, 12.0))
        self.assertEqual(tr[("Vacuum gauge at warm idle", "value")], (15, 19))
        self.assertEqual([c[:3] for c in history.status_changes(ev)], [("Warm idle", "fail", "pass")])
        text = history.text(ev)
        self.assertIn("Warm idle: fail -> pass", text)
        self.assertIn("rpm_sd: 160 -> 12", text)

    def test_empty(self):
        self.assertEqual(history.text(history.load(tempfile.mkdtemp(), tempfile.mkdtemp(), FIERO)), "")


if __name__ == "__main__":
    unittest.main()
