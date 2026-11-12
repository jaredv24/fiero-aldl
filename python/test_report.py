"""Tests for report.py.   Run:  python -m unittest test_report -v"""

import tempfile
import unittest
from datetime import datetime

import report


class ReportTests(unittest.TestCase):
    def data(self):
        return {"vehicle": "1985-88 Pontiac Fiero 2.8 V6", "source": "Demo", "when": datetime(2026, 11, 12, 9, 30),
                "findings": [("problem", "Idle hunts <badly>", "RPM swings 600-1150", "Check for leaks")],
                "charts": [("Engine RPM", "rpm", [(i, 800 + (i % 6) * 50) for i in range(60)]),
                           ("Empty", "", [(0, None)])],
                "codes": "Current codes: 32 (EGR)", "tests": [("Warm idle", "FAIL", "Hunting", ["sd 158"])],
                "leak": [("EGR block-off plate", "yes", "O2 went rich")], "leak_summary": "Most likely leak: EGR",
                "compare": ("Helped", [("Idle RPM swing", "158", "9", "rpm", "better")], ["short log"]),
                "history": [("2026-11-01 10:00", "test", "Warm idle", "fail", "")], "trends": ["Warm idle: FAIL → PASS"],
                "claude": [("What do you see?", "Probably a vacuum leak.\nCheck the EGR plate.")]}

    def test_sections_and_escaping(self):
        page = report.build(self.data())
        for text in ("<svg", "Idle hunts &lt;badly&gt;", "32 (EGR)", "Most likely leak: EGR",
                     "Idle RPM swing", "Warm idle: FAIL → PASS", "Probably a vacuum leak.",
                     "prefers-color-scheme: dark", "November 12, 2026"):
            self.assertIn(text, page)
        self.assertNotIn("<badly>", page)
        self.assertEqual(page.count("<svg"), 1)           # chart with no data is skipped

    def test_minimal_and_save(self):
        page = report.build({"vehicle": "Car"})
        self.assertIn("<h1>Car</h1>", page)
        path = report.save(tempfile.mkdtemp(), self.data())
        self.assertTrue(path.endswith("-20261112-093000.html"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("<!doctype html>", f.read())


if __name__ == "__main__":
    unittest.main()
