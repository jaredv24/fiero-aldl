"""Sourced normal values for the Fiero 2.8 V6, from its vehicle profile
(profiles/fiero-1985-88-2-8-v6.json)."""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "profiles", "fiero-1985-88-2-8-v6.json")

CONFIDENCE_LABEL = {
    "gm": "GM spec",
    "gm_general": "GM, this era",
    "oem": "factory spec",
    "oem_general": "maker, this era",
    "owners": "owners",
    "app": "app estimate",
}

_cache = None


def load(path=PATH):
    global _cache
    if _cache is None or path != PATH:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        values = data.get("normals") or data.get("values")
        if path != PATH:
            return values
        _cache = values
    return _cache


def get(key):
    return load()[key]


def limit(key, name):
    return load()[key].get("limits", {})[name]


def range_text(key):
    """'20–40 steps (warm idle in Park/Neutral, A/C off) · Fiero owners'."""
    v = get(key)
    lo, hi, unit = v.get("low"), v.get("high"), v.get("unit", "")
    if lo is not None and hi is not None and lo != hi:
        rng = f"{lo:g}–{hi:g}"
    elif lo is not None and hi is None:
        rng = f"≥ {lo:g}"
    else:
        rng = f"{lo:g}"
    unit = {"F": "°F"}.get(unit, unit)
    return f"{rng} {unit}".strip()


def summary_lines():
    """One line per value, for Ask Claude's context."""
    out = []
    for key, v in load().items():
        out.append(f"- {v['label']}: {range_text(key)} ({v['condition']}). "
                   f"[{CONFIDENCE_LABEL.get(v['confidence'], v['confidence'])}] {v['note']}")
    return out


# Which dashboard value each normal applies to (and when).
DASHBOARD = {
    "Engine RPM": "rpm_warm_idle",
    "IAC position": "iac_warm_idle",
    "MAP": "map_idle",
    "TPS": "tps_closed",
    "O2 sensor": "o2",
    "BLM (long trim)": "blm",
    "INT (short trim)": "int",
    "Coolant temp": "coolant_warm",
    "Battery": "battery_running",
}
