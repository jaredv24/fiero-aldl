"""Vehicle history: every guided test and hands-on check for one car, over time.

Guided-test results are saved as JSON in the tests folder (with the vehicle's
name since this version); checks come from checks.Store. Trends compare the
numbers a test measured the first time with the latest run.
"""

import glob
import json
import os
from dataclasses import dataclass
from datetime import datetime

import checks


@dataclass
class Event:
    when: datetime
    kind: str           # "test" / "check"
    name: str
    status: str
    summary: str
    numbers: dict


def _parse_when(text):
    for fmt in ("%Y%m%d-%H%M%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except (TypeError, ValueError):
            pass
    return datetime.min


def _belongs(data, vehicle, is_obd):
    if data.get("vehicle"):
        return data["vehicle"] == vehicle
    # Results saved before vehicles were recorded: match on the kind of test.
    return data.get("id", "").startswith("obd_") == is_obd


def load(tests_dir, checks_dir, vehicle, is_obd=False):
    events = []
    for path in glob.glob(os.path.join(tests_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not _belongs(data, vehicle, is_obd):
            continue
        nums = {k: v for k, v in (data.get("numbers") or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
        events.append(Event(_parse_when(data.get("when")), "test", data.get("test", "?"),
                            data.get("status", ""), data.get("headline", ""), nums))
    for e in checks.Store(checks_dir, vehicle).entries():
        nums = checks.numbers(e.get("value"))
        expl = e.get("explanation", "")
        if expl in ("Noted.", "Recorded.", "No number entered."):
            expl = ""
        summary = " — ".join(x for x in (e.get("note", ""), expl) if x)
        events.append(Event(_parse_when(e.get("when")), "check", e.get("name", "?"),
                            e.get("verdict", ""), summary,
                            {"value": nums[0]} if len(nums) == 1 else {}))
    events.sort(key=lambda ev: ev.when)
    return events


def trends(events):
    """[(name, metric, first, last, n, first_when, last_when)] for numbers seen 2+ times."""
    series = {}
    for ev in events:
        for k, v in ev.numbers.items():
            series.setdefault((ev.name, k), []).append((ev.when, v))
    out = []
    for (name, k), pts in series.items():
        if len(pts) >= 2 and pts[0][1] != pts[-1][1]:
            out.append((name, k, pts[0][1], pts[-1][1], len(pts), pts[0][0], pts[-1][0]))
    return sorted(out)


def status_changes(events):
    """Tests whose result changed between the first and the latest run."""
    by = {}
    for ev in events:
        if ev.kind == "test":
            by.setdefault(ev.name, []).append(ev)
    out = []
    for name, evs in by.items():
        if len(evs) >= 2 and evs[0].status != evs[-1].status:
            out.append((name, evs[0].status, evs[-1].status, evs[0].when, evs[-1].when))
    return out


def text(events, limit=30):
    """Plain text for Claude and the report."""
    if not events:
        return ""
    lines = ["Vehicle history (oldest first):"]
    for ev in events[-limit:]:
        lines.append(f"- {ev.when:%Y-%m-%d %H:%M} {ev.kind}: {ev.name} [{ev.status}] {ev.summary}")
    ch = status_changes(events)
    if ch:
        lines.append("Results that changed: " + "; ".join(
            f"{n}: {a} -> {b}" for n, a, b, _, _ in ch))
    tr = trends(events)
    if tr:
        lines.append("Measured numbers over time: " + "; ".join(
            f"{n} {k}: {a:g} -> {b:g} ({c} runs)" for n, k, a, b, c, _, _ in tr))
    return "\n".join(lines)
