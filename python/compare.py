"""Before/after comparison of two logs: did the fix help?

Both logs are boiled down to the same set of warm-idle numbers (how steady the
idle is, where the computer holds the idle valve, fuel trims, O2 switching,
vacuum, stalls), then compared side by side. Works with Fiero logs and OBD-II
logs; readings that aren't in both logs are skipped.
"""

import statistics
from dataclasses import dataclass

import vacuum

BETTER, WORSE, SAME, NA = "better", "worse", "about the same", ""


@dataclass
class Row:
    name: str
    unit: str
    before: float
    after: float
    verdict: str
    note: str = ""


def _warm_idle(hist):
    out = []
    for t, v in hist:
        r = v.get("Engine RPM")
        if r is None or not 300 <= r <= 1500:
            continue
        if (v.get("Coolant temp") or 999) < 150:
            continue
        if (v.get("Vehicle speed") or 0) > 2:
            continue
        if (v.get("TPS") or 0) > 1.2 and v.get("TPS", 0) < 5.1 and "MAP" in v and v["MAP"] < 15:
            continue                                  # Fiero: throttle opened (TPS in volts)
        out.append((t, v))
    return out


def _series(rows, name):
    return [v[name] for _, v in rows if v.get(name) is not None]


def _o2_switches_per_10s(rows):
    key = "O2 sensor" if any(v.get("O2 sensor") is not None for _, v in rows) else "O2 B1S1 (upstream)"
    pts = [(t, v[key] / (1000 if key == "O2 sensor" else 1)) for t, v in rows if v.get(key) is not None]
    if len(pts) < 5:
        return None
    flips = sum(1 for (_, a), (_, b) in zip(pts, pts[1:]) if (a - 0.45) * (b - 0.45) < 0)
    span = pts[-1][0] - pts[0][0]
    return flips * 10 / span if span > 0 else None


def _stalls(hist):
    n, was = 0, False
    for _, v in hist:
        r = v.get("Engine RPM")
        if r is None:
            continue
        if was and r < 50:
            n += 1
        was = r > 300
    return n


def summarize(hist):
    """{metric: (value, unit)} for one log."""
    hist = list(hist)
    tr = vacuum.Tracker()
    for _, v in hist:
        if v.get(vacuum.NAME) is None:
            tr.update(v)
    idle = _warm_idle(hist)
    m = {"_idle_seconds": (idle[-1][0] - idle[0][0] if len(idle) > 1 else 0, "s")}
    rpm = _series(idle, "Engine RPM")
    if len(rpm) >= 3:
        m["Idle RPM (average)"] = (statistics.mean(rpm), "rpm")
        m["Idle RPM swing (std. dev.)"] = (statistics.pstdev(rpm), "rpm")
        m["Lowest idle RPM"] = (min(rpm), "rpm")
    for name, label, unit in (("IAC position", "Idle air valve (IAC)", "steps"),
                              ("BLM (long trim)", "BLM (long trim)", ""),
                              ("INT (short trim)", "INT (short trim)", ""),
                              ("Long trim B1", "Long trim B1", "%"),
                              ("Long trim B2", "Long trim B2", "%"),
                              ("Short trim B1", "Short trim B1", "%"),
                              ("Mass air flow", "Mass air flow", "g/s"),
                              ("Spark advance", "Spark advance", "deg"),
                              (vacuum.NAME, "Idle vacuum", "inHg")):
        s = _series(idle, name)
        if len(s) >= 3:
            m[label] = (statistics.mean(s), unit)
    vac = _series(idle, vacuum.NAME)
    if len(vac) >= 3:
        m["Idle vacuum swing (std. dev.)"] = (statistics.pstdev(vac), "inHg")
    sw = _o2_switches_per_10s(idle)
    if sw is not None:
        m["O2 switches per 10 s"] = (sw, "")
    if len(rpm) >= 3:
        m["Stalls"] = (_stalls(hist), "")
    return m


# How to judge a change: "lower", "higher", or a neutral point (closer is better).
RULES = {
    "Idle RPM swing (std. dev.)": ("lower", 15),
    "Lowest idle RPM": ("higher", 40),
    "Idle air valve (IAC)": (30, 5),         # Fiero warm idle is roughly 20-40 steps
    "BLM (long trim)": (128, 3),
    "INT (short trim)": (128, 3),
    "Long trim B1": (0, 2),
    "Long trim B2": (0, 2),
    "Short trim B1": (0, 2),
    "Idle vacuum": ("higher", 1),
    "Idle vacuum swing (std. dev.)": ("lower", 0.5),
    "O2 switches per 10 s": ("higher", 1),
    "Stalls": ("lower", 1),
}


def _verdict(name, before, after):
    rule = RULES.get(name)
    if rule is None:
        return NA
    how, tol = rule
    if how == "lower":
        d = before - after
    elif how == "higher":
        d = after - before
    else:
        d = abs(before - how) - abs(after - how)
    if abs(d) < tol:
        return SAME
    return BETTER if d > 0 else WORSE


def compare(hist_before, hist_after):
    a, b = summarize(hist_before), summarize(hist_after)
    rows = []
    for name in a:
        if name.startswith("_") or name not in b:
            continue
        rows.append(Row(name, a[name][1], a[name][0], b[name][0], _verdict(name, a[name][0], b[name][0])))
    notes = []
    for label, s in (("before", a), ("after", b)):
        secs = s["_idle_seconds"][0]
        if secs < 30:
            notes.append(f"The {label} log has only {secs:.0f} s of warm idle, so treat its numbers "
                         "as rough. A couple of minutes of warm idle in each log compares best.")
    return rows, notes


def verdict_text(rows):
    better = [r.name for r in rows if r.verdict == BETTER]
    worse = [r.name for r in rows if r.verdict == WORSE]
    if not rows:
        return "These two logs don't share enough warm-idle readings to compare."
    if better and not worse:
        return "The fix looks like it helped: " + ", ".join(better) + " improved and nothing got worse."
    if worse and not better:
        return "Things got worse after the change: " + ", ".join(worse) + "."
    if better and worse:
        return ("Mixed result. Better: " + ", ".join(better) + ". Worse: " + ", ".join(worse) + ".")
    return "No real difference between the two logs."


def fmt(x, unit):
    return f"{x:.0f}" if unit in ("rpm", "steps", "") and abs(x) >= 20 else f"{x:.1f}"
