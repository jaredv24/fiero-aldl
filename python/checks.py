"""Hands-on checks: numbers measured with tools the computer can't see.

A fuel pressure gauge, a timing light, a compression tester, a vacuum gauge,
a multimeter. Each entry is judged against the vehicle profile's normals
where the profile has one, otherwise against a general mechanic's rule of
thumb (and says so). Entries are saved per vehicle so they show up in Claude's
answers and the session report.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass

OK, WATCH, PROBLEM, INFO = "ok", "watch", "problem", "info"


@dataclass
class Check:
    id: str
    name: str
    unit: str
    how: str
    normal_key: str = None      # key in the vehicle profile's normals
    general: tuple = None       # (low, high, text) when the profile has no number
    kinds: tuple = ("aldl", "obd2")


CHECKS = [
    Check("fuel_pressure", "Fuel pressure, key on engine off", "psi",
          "Gauge on the fuel rail test port. Turn the key on (don't start) so the pump primes, "
          "read the gauge.", "fuel_pressure"),
    Check("fuel_pressure_idle", "Fuel pressure at idle", "psi",
          "Same gauge with the engine idling. With the regulator's vacuum hose connected it "
          "should read a few psi below the key-on number.",
          general=(None, None, "a few psi (roughly 3–10) below key-on engine-off pressure")),
    Check("fuel_pressure_hold", "Fuel pressure 5 minutes after shut-off", "psi",
          "Shut the engine off and read the gauge again after 5 minutes. A fast drop points to a "
          "leaking injector, regulator or pump check valve.",
          general=(None, None, "should hold most of its pressure; a drop to near zero is a problem")),
    Check("vacuum_gauge", "Vacuum gauge at warm idle", "inHg",
          "Mechanical vacuum gauge on a manifold port, engine warm and idling.",
          general=(17, 21, "17–21 inHg steady near sea level (general rule; less at altitude)")),
    Check("base_timing", "Base ignition timing", "deg BTDC",
          "Timing light, engine warm, ECM in base timing mode (Fiero: jumper ALDL A–B or "
          "disconnect the EST bypass connector per the service manual).",
          "base_timing", kinds=("aldl",)),
    Check("compression", "Compression, every cylinder", "psi",
          "Engine warm, all plugs out, throttle wide open. Enter each cylinder separated by "
          "commas, e.g. 150, 148, 152, 149, 151, 147.",
          general=(None, None, "lowest within about 10% of the highest (general rule)")),
    Check("battery_resting", "Battery voltage, engine off", "V",
          "Multimeter across the battery posts, engine off for at least an hour.",
          "battery_koeo", general=(12.4, 12.8, "12.4–12.8 V for a charged battery")),
    Check("charging", "Charging voltage at idle", "V",
          "Multimeter across the battery posts with the engine idling.",
          "battery_running", general=(13.5, 15.0, "13.5–15 V")),
    Check("note", "Something I found or fixed", "",
          "A note for the record, e.g. 'replaced cracked PCV hose' or 'EGR block-off gasket "
          "looked torn'. No number needed."),
]
BY_ID = {c.id: c for c in CHECKS}


def for_kind(kind):
    return [c for c in CHECKS if kind in c.kinds]


def _range(check, profile):
    n = (profile or {}).get("normals", {}).get(check.normal_key) if check.normal_key else None
    if n and (n.get("low") is not None or n.get("high") is not None):
        lo, hi = n.get("low"), n.get("high")
        if lo is not None and lo == hi:          # a single spec value: allow +/- 1
            return lo - 1, hi + 1, f"{lo} {n.get('unit', check.unit)} ({profile['vehicle']['name']} profile)"
        text = f"{lo}–{hi} {n.get('unit', check.unit)}" if lo != hi else f"{lo} {n.get('unit', check.unit)}"
        return lo, hi, f"{text} ({profile['vehicle']['name']} profile)"
    if check.general:
        return check.general
    return None, None, ""


def numbers(text):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


def judge(check, value_text, profile=None, earlier=None):
    """(verdict, explanation) for one entry. `earlier` = {check id: latest number}."""
    earlier = earlier or {}
    if check.id == "note":
        return INFO, "Noted."
    nums = numbers(value_text)
    if not nums:
        return INFO, "No number entered."
    lo, hi, normal = _range(check, profile)
    if check.id == "compression":
        if len(nums) < 2:
            return INFO, "Enter every cylinder to compare them."
        spread = (max(nums) - min(nums)) / max(nums) * 100
        low_cyl = nums.index(min(nums)) + 1
        text = f"Spread {spread:.0f}% (lowest is cylinder #{low_cyl} in the order entered); normal: {normal}."
        if spread > 15:
            return PROBLEM, text + (" One low cylinder points to rings, valves or a head gasket; "
                                    "a wet test (squirt of oil) tells rings from valves.")
        return (WATCH if spread > 10 else OK), text
    x = nums[0]
    if check.id == "fuel_pressure_idle" and "fuel_pressure" in earlier:
        drop = earlier["fuel_pressure"] - x
        text = f"{drop:.0f} psi below key-on ({earlier['fuel_pressure']:.0f} psi); normal: {normal}."
        if drop < 2:
            return WATCH, text + " No drop: check the regulator's vacuum hose for a leak or blockage."
        return (OK if drop <= 12 else WATCH), text
    if check.id == "fuel_pressure_hold" and "fuel_pressure" in earlier:
        lost = earlier["fuel_pressure"] - x
        text = f"Lost {lost:.0f} psi in 5 minutes; normal: {normal}."
        if x < earlier["fuel_pressure"] * 0.5:
            return PROBLEM, text + (" Leak-down: a leaking injector floods the engine at start "
                                    "and a leaking check valve causes long cranking.")
        return OK, text
    if lo is None and hi is None:
        return INFO, f"Recorded. Normal: {normal}." if normal else "Recorded."
    if lo is not None and x < lo:
        return PROBLEM, f"{x:g} {check.unit} is below normal ({normal})."
    if hi is not None and x > hi:
        return PROBLEM, f"{x:g} {check.unit} is above normal ({normal})."
    return OK, f"{x:g} {check.unit} is normal ({normal})."


# ------------------------------------------------------------ storage

def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "vehicle").lower()).strip("-")


class Store:
    def __init__(self, folder, vehicle_name):
        self.vehicle = vehicle_name
        self.path = os.path.join(folder, slug(vehicle_name) + ".json")

    def entries(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f).get("entries", [])
        except (OSError, ValueError):
            return []

    def latest_numbers(self):
        out = {}
        for e in self.entries():
            nums = numbers(e.get("value"))
            if nums:
                out[e["check"]] = nums[0]
        return out

    def add(self, check_id, value_text, note="", profile=None, when=None):
        check = BY_ID[check_id]
        verdict, text = judge(check, value_text, profile, self.latest_numbers())
        entry = {"check": check_id, "name": check.name, "value": value_text, "unit": check.unit,
                 "note": note, "verdict": verdict, "explanation": text,
                 "when": when or time.strftime("%Y-%m-%d %H:%M")}
        data = {"vehicle": self.vehicle, "entries": self.entries() + [entry]}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return entry

    def delete(self, index):
        entries = self.entries()
        if 0 <= index < len(entries):
            del entries[index]
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"vehicle": self.vehicle, "entries": entries}, f, indent=2)

    def text(self, limit=20):
        """Plain text for Claude and the session report."""
        entries = self.entries()[-limit:]
        if not entries:
            return ""
        lines = [f"Hands-on checks recorded by the owner ({self.vehicle}):"]
        for e in entries:
            val = f"{e['value']} {e['unit']}".strip()
            note = f" Note: {e['note']}" if e.get("note") else ""
            lines.append(f"- {e['when']} {e['name']}: {val} [{e['verdict']}] {e['explanation']}{note}")
        return "\n".join(lines)


def as_dict(c):
    return asdict(c)
