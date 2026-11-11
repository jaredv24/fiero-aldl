"""Checks for OBD-II cars, judged against the car's vehicle profile.

Same idea as analysis.py (which handles the Fiero's ALDL data), but works on
standard OBD-II readings: trouble codes, fuel trims per bank, MAF, O2
sensors before and after the catalyst, idle stability, cooling and charging.
Limits come from the profile's normals (matched by "signal"); where a profile
doesn't have a value, general OBD-II rules of thumb are used and say so.
"""

import statistics

from analysis import INFO, OK, PROBLEM, WATCH, Finding

RPM, ECT, SPEED, MAF, TPS = "Engine RPM", "Coolant temp", "Vehicle speed", "Mass air flow", "Throttle position"
TRIMS = {"B1": ("Short trim B1", "Long trim B1"), "B2": ("Short trim B2", "Long trim B2")}
O2_UP = ["O2 B1S1 (upstream)", "O2 B2S1 (upstream)"]
O2_DOWN = ["O2 B1S2 (downstream)", "O2 B2S2 (downstream)"]
BATTERY = ["Module voltage", "Battery (OBD port)"]


def normal(profile, signal):
    for n in ((profile or {}).get("normals") or {}).values():
        if n.get("signal") == signal:
            return n
    return None


def _vals(hist, name):
    return [v[name] for _, v in hist if v.get(name) is not None]


def _crossings(vals, level):
    return sum(1 for a, b in zip(vals, vals[1:]) if (a - level) * (b - level) < 0)


def known_issue_hint(profile, *words):
    for k in (profile or {}).get("known_issues", []) or []:
        text = " ".join(str(k.get(f, "")) for f in ("title", "symptoms", "what_data_shows")).lower()
        if any(w.lower() in text for w in words):
            return f"Known for this car: {k['title']}. {k.get('checks', '')}".strip()
    return ""


def analyze(history, profile=None, window=30):
    if window is not None and history:
        cutoff = history[-1][0] - window
        history = [h for h in history if h[0] >= cutoff]
    out = []
    rpm = _vals(history, RPM)
    if len(rpm) < 3:
        return [Finding(INFO, "Collecting data…", f"{len(rpm)} readings so far.")]
    running = statistics.median(rpm) > 300
    last = history[-1][1]
    flags = last.get("_flags") or {}

    # ---- trouble codes and the check engine light
    seen = {}
    for _, v in history:
        for c, text in v.get("_codes") or []:
            seen[c] = text
    for code in sorted(seen):
        hint = known_issue_hint(profile, code)
        out.append(Finding(PROBLEM, f"Trouble code {code}: {seen[code]}",
                           "Stored by the engine computer.",
                           hint or "Fix codes first: they often explain the symptoms. "
                                   "The Codes tab shows its freeze frame."))
    if flags.get("Check engine light on"):
        out.append(Finding(WATCH, "Check engine light is on", "The engine computer has the MIL on."))

    # ---- coolant
    ect = _vals(history, ECT)
    cn = normal(profile, "coolant_temp")
    lim = (cn or {}).get("limits", {})
    if ect:
        hi = max(ect)
        if min(ect) <= -30 or hi >= 270:
            out.append(Finding(PROBLEM, "Coolant reading is impossible", f"{min(ect):.0f}–{hi:.0f} °F.",
                               "Check the coolant temperature sensor and its wiring."))
        elif running and hi >= lim.get("overheat", 245):
            out.append(Finding(PROBLEM, "Engine running hot", f"Coolant reached {hi:.0f} °F.",
                               "Check coolant level, the fans and the thermostat."))
        elif running and "fan_high_on" in lim and hi > lim["fan_high_on"] + 5:
            out.append(Finding(WATCH, "Coolant above the high-speed fan point",
                               f"{hi:.0f} °F; the fans should be on by about {lim['fan_high_on']} °F.",
                               "Check that both fan speeds work."))
    warm = bool(ect) and statistics.median(ect[-5:]) >= 160

    if running and warm and flags.get("Closed loop") is False:
        out.append(Finding(WATCH, "Open loop on a warm engine",
                           "The computer isn't using the O2 sensors to correct fuel.",
                           "Usually an O2 sensor, its heater, or a coolant reading problem."))

    idle = [(t, v) for t, v in history
            if (v.get(RPM) or 0) > 300 and (v.get(SPEED) or 0) < 2
            and (v.get(TPS) is None or v[TPS] < 25)]

    # ---- idle stability and MAF at idle
    if running and len(idle) >= 5:
        r = [v[RPM] for _, v in idle]
        span = max(r) - min(r)
        rn = normal(profile, "rpm")
        if span > 350:
            out.append(Finding(PROBLEM, "Idle is hunting", f"RPM swung {min(r):.0f}–{max(r):.0f}."))
        elif span > 200:
            out.append(Finding(WATCH, "Idle a little unsteady", f"RPM varied by {span:.0f}."))
        if rn and rn.get("low") is not None and min(r) < rn["low"] - 100:
            out.append(Finding(PROBLEM, "Idle dropping low", f"Lowest {min(r):.0f} rpm "
                               f"(normal warm idle {rn['low']:g}–{rn['high']:g})."))
        mn = normal(profile, "maf")
        maf = [v[MAF] for _, v in idle if v.get(MAF) is not None]
        if mn and maf and warm:
            m = statistics.median(maf)
            if mn.get("low") is not None and m < mn["low"]:
                out.append(Finding(WATCH, "MAF reads low at idle",
                                   f"{m:.1f} g/s (normal {mn['low']:g}–{mn['high']:g} g/s).",
                                   "Air getting in after the MAF (vacuum leak, intake boot) or a "
                                   "dirty MAF sensor. " + known_issue_hint(profile, "vacuum")))
            elif mn.get("high") is not None and m > mn["high"] * 1.3:
                out.append(Finding(WATCH, "MAF reads high at idle",
                                   f"{m:.1f} g/s (normal {mn['low']:g}–{mn['high']:g} g/s).",
                                   "Extra load at idle, or a MAF reading high."))

    # ---- fuel trims per bank
    ln = normal(profile, "ltft_b1") or {}
    tl = ln.get("limits", {})
    watch_beyond, problem_beyond = tl.get("watch_beyond", 10), tl.get("problem_beyond", 20)
    lean_at_idle = []
    for bank, (st, lt) in TRIMS.items():
        rows = [(v[st] + v[lt]) for _, v in history
                if v.get(st) is not None and v.get(lt) is not None and (v.get(RPM) or 0) > 300]
        if not rows or not running:
            continue
        total = statistics.median(rows)
        if abs(total) > watch_beyond:
            side = "adding fuel (it sees lean)" if total > 0 else "removing fuel (it sees rich)"
            hint = (known_issue_hint(profile, "vacuum", "lean") or
                    "Lean: vacuum leak, weak fuel pump or clogged injectors.") if total > 0 else \
                   "Rich: leaking injector, high fuel pressure, or a sensor reading wrong."
            out.append(Finding(PROBLEM if abs(total) > problem_beyond else WATCH,
                               f"Fuel trim {bank} {total:+.0f}%",
                               f"Short + long term trims: the computer is {side}. Normal is "
                               f"within about ±{watch_beyond}%.", hint))
            if total > 0:
                lean_at_idle.append(bank)
    if lean_at_idle:
        out.append(Finding(INFO, "Leak or fuel? Run the 2500 rpm test",
                           "Lean at idle that goes away at 2500 rpm points to a vacuum leak; lean "
                           "at both points to fuel delivery. The Tests tab has this test."))

    # ---- O2 sensors (warm, closed loop)
    if running and warm and flags.get("Closed loop") is not False:
        secs = max(history[-1][0] - history[0][0], 1)
        on = normal(profile, "o2_b1s1") or {}
        olim = on.get("limits", {})
        for name in O2_UP:
            o2 = _vals(history, name)
            if len(o2) < 8:
                continue
            mean, per10 = sum(o2) / len(o2), _crossings(o2, 0.45) * 10 / secs
            if mean < olim.get("stuck_lean_below", 0.3) and per10 < 1:
                out.append(Finding(PROBLEM, f"{name} stuck lean", f"Averaged {mean:.2f} V."))
            elif mean > olim.get("stuck_rich_above", 0.8) and per10 < 1:
                out.append(Finding(PROBLEM, f"{name} stuck rich", f"Averaged {mean:.2f} V."))
            elif per10 < 2:
                out.append(Finding(WATCH, f"{name} switching slowly",
                                   f"About {per10:.1f} crossings per 10 s.", "Aging sensor."))
        up = _vals(history, O2_UP[0])
        for name in O2_DOWN:
            down = _vals(history, name)
            if len(down) >= 8 and len(up) >= 8:
                cu, cd = _crossings(up, 0.45), _crossings(down, 0.45)
                if cu >= 4 and cd / cu > 0.6 and max(down) - min(down) > 0.4:
                    out.append(Finding(WATCH, "Catalyst may be weak",
                                       f"The downstream O2 switches almost as much as the upstream "
                                       f"one ({cd} vs {cu} crossings).",
                                       "A healthy catalyst keeps the downstream sensor steady. "
                                       "Confirm with the Catalyst check test at 2500 rpm."))

    # ---- charging / battery
    for name in BATTERY:
        b = _vals(history, name)
        if not b:
            continue
        m = statistics.median(b)
        if running and m < 13.2:
            out.append(Finding(WATCH, "Low charging voltage", f"{m:.1f} V running.",
                               known_issue_hint(profile, "battery") or "Alternator, belt or connections."))
        elif running and m > 15.2:
            out.append(Finding(PROBLEM, "Charging voltage too high", f"{m:.1f} V.", "Regulator fault."))
        elif not running and m < 12.2:
            out.append(Finding(WATCH, "Battery low", f"{m:.1f} V engine off."))
        break

    if not running:
        out.insert(0, Finding(INFO, "Engine not running", "Idle, trim and O2 checks need it running."))
    if not any(f.severity in (PROBLEM, WATCH) for f in out):
        out.append(Finding(OK, "Nothing out of the ordinary",
                           "Checked values are within this car's normal ranges."))
    order = {PROBLEM: 0, WATCH: 1, OK: 2, INFO: 3}
    return sorted(out, key=lambda f: order[f.severity])
