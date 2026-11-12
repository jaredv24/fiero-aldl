"""Rule-based checks that flag readings out of the normal range.

Works on a history of decoded frames: a list of (seconds, {name: value}).
A sample may also carry "_codes" ([(code, text)] trouble codes set in that
frame) and "_flags" ({flag name: bool}) when the byte layout defines them
(the Fiero A29 layout does).
Used by the app's Analysis tab, live (last 30 s) or on a saved CSV log.

The limits come from the Fiero vehicle profile (profiles/): GM chart values
where they could be found, otherwise GM figures for this era or consistent
Fiero owner reports (each value records its source and confidence). They point at where to look; they are not a
diagnosis. Each check only runs when the values it needs are mapped and in
the expected units, so a wrong byte layout doesn't produce confident nonsense.
"""

import csv
import math

import normals as nm
from dataclasses import dataclass

PROBLEM, WATCH, OK, INFO = "problem", "watch", "ok", "info"
LIVE_WINDOW_S = 30
IDLE_MAX_RPM = 1500      # samples above this aren't treated as idle
CLOSED_TPS_MAX_V = 1.0   # TPS above this means the throttle is open
WARM_F = 160


@dataclass
class Finding:
    severity: str        # problem / watch / ok / info
    title: str
    detail: str = ""
    hint: str = ""


def _median(vals):
    s = sorted(vals)
    n = len(s)
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) if n else None


def _crossings(vals, level):
    return sum(1 for a, b in zip(vals, vals[1:]) if (a - level) * (b - level) < 0)


class _Data:
    def __init__(self, history, params):
        self.history, self.params = history, params
        self.unmapped = []

    def series(self, name, units=None, samples=None):
        """Values for one parameter, or None if it can't be used."""
        p = self.params.get(name)
        if p is None or p.get("offset", 0) is None:
            if p is not None and name not in self.unmapped:
                self.unmapped.append(name)
            return None
        if units is not None and p.get("units") != units:
            return None
        rows = self.history if samples is None else samples
        return [v[name] for _, v in rows if v.get(name) is not None]


def analyze(history, params, window=LIVE_WINDOW_S):
    """Return a list of Findings, most serious first.

    history: [(t_seconds, {name: value or None})], oldest first.
    params:  {name: parameter dict from the definition (offset, units, ...)}
    window:  only look at the last N seconds (None = everything).
    """
    if window is not None and history:
        cutoff = history[-1][0] - window
        history = [h for h in history if h[0] >= cutoff]
    d = _Data(history, params)
    out = []

    rpm_all = d.series("Engine RPM", "rpm")
    if rpm_all is None:
        return [Finding(INFO, "Engine RPM isn't mapped yet",
                        "Almost every check needs RPM.",
                        "Use the Raw bytes tab: rev the engine and assign the byte that jumps.")]
    if len(rpm_all) < 5:
        return [Finding(INFO, "Collecting data…", f"{len(rpm_all)} readings so far.")]

    running = _median(rpm_all) > 300

    # ---- Trouble codes and ECM status (from the frame's flag bytes)
    seen = {}
    for _, v in history:
        for code, text in v.get("_codes") or []:
            seen[code] = text
    for code in sorted(seen):
        out.append(Finding(PROBLEM, f"ECM trouble code {code}: {seen[code]}",
                           "Reported by the ECM in the data stream.",
                           "Look this code up in the Fiero service manual's diagnostic chart; "
                           "fix codes first, they often explain the symptoms."))
    flag_rows = [v["_flags"] for _, v in history if v.get("_flags")]
    latest_flags = flag_rows[-1] if flag_rows else {}
    if latest_flags.get("Diagnostic (shorted) mode") or latest_flags.get("Factory test mode"):
        out.append(Finding(WATCH, "ECM is in diagnostic (A–B shorted) mode",
                           "In this mode the ECM changes how it runs the engine.",
                           "Don't judge the idle like this. Use the 10k (ALDL) mode instead: "
                           "the cable's 10k switch, not a jumper."))

    coolant = d.series("Coolant temp", "F")
    warm = coolant is not None and coolant and _median(coolant[-5:]) >= WARM_F

    # Idle samples: low RPM and (if known) closed throttle.
    tps_name_ok = d.series("TPS", "V") is not None
    idle_rows = []
    for t, v in history:
        r = v.get("Engine RPM")
        if r is None or r > IDLE_MAX_RPM or r < 1:
            continue
        if tps_name_ok and v.get("TPS") is not None and v["TPS"] > CLOSED_TPS_MAX_V:
            continue
        idle_rows.append((t, v))
    idling = running and len(idle_rows) >= 5

    # ---- Coolant sensor plausibility (works running or not)
    if coolant:
        lo, hi = min(coolant), max(coolant)
        if lo <= -30 or hi >= 270:
            out.append(Finding(PROBLEM, "Coolant temp reading is impossible",
                               f"Read {lo:.0f}–{hi:.0f} °F.",
                               "A reading near -40 °F usually means an open circuit or bad "
                               "coolant sensor. The ECM then runs very rich, which can cause "
                               "a rough or stalling idle."))
        elif running and hi >= nm.limit("coolant_warm", "overheat"):
            out.append(Finding(PROBLEM, "Engine running hot", f"Coolant reached {hi:.0f} °F.",
                               "The fan should come on around 235 °F. Check coolant level, the "
                               "fan and the thermostat."))
        elif running and hi > nm.limit("coolant_warm", "fan_on"):
            out.append(Finding(WATCH, "Coolant above the fan-on point",
                               f"Coolant reached {hi:.0f} °F (fan should come on around "
                               f"{nm.limit('coolant_warm', 'fan_on')} °F).",
                               "Check that the cooling fan runs."))

    # ---- Idle stability
    if idling:
        rpm = [v["Engine RPM"] for _, v in idle_rows]
        span, low = max(rpm) - min(rpm), min(rpm)
        if span > 300:
            out.append(Finding(PROBLEM, "Idle is hunting",
                               f"RPM swung between {min(rpm):.0f} and {max(rpm):.0f} "
                               f"({span:.0f} rpm) while idling.",
                               "The ECM can't hold idle. See the IAC, MAP and O2 checks below "
                               "for which way it's off."))
        elif span > 150:
            out.append(Finding(WATCH, "Idle a little unsteady",
                               f"RPM varied by {span:.0f} rpm at idle."))
        if low < 550:
            out.append(Finding(PROBLEM, "RPM dropping close to stalling",
                               f"Lowest idle reading {low:.0f} rpm."))

        iac = d.series("IAC position", "steps", idle_rows)
        if iac:
            if min(iac) <= nm.limit("iac_warm_idle", "too_much_air") and (span > 150 or _median(rpm) > 1000):
                out.append(Finding(PROBLEM, "IAC fully closed, idle still too high/unstable",
                                   f"IAC went down to {min(iac):.0f} steps.",
                                   "The ECM has shut off its idle air and the engine is still "
                                   "getting too much air: look for a vacuum leak, an EGR valve "
                                   "stuck open, or a throttle stop screw that's been turned."))
            high = nm.limit("iac_warm_idle", "idle_too_low")
            if max(iac) >= high:
                out.append(Finding(PROBLEM, "IAC very high",
                                   f"IAC reached {max(iac):.0f} steps (warm idle is normally "
                                   f"{nm.range_text('iac_warm_idle')}; {high}+ means idle too low).",
                                   "The ECM is adding a lot of idle air and it's still not enough: "
                                   "dirty throttle body or IAC passage, or a fuel or ignition "
                                   "problem."))

        map_kpa = d.series("MAP", "kPa", idle_rows)
        if map_kpa:
            m = _median(map_kpa)
            if m > 55:
                out.append(Finding(WATCH, "Low vacuum at idle (high MAP)",
                                   f"MAP about {m:.0f} kPa at idle (healthy is usually ~30–45).",
                                   "Possible vacuum leak, EGR open at idle, late timing, or a "
                                   "bad MAP sensor or hose."))
            if max(map_kpa) - min(map_kpa) > 15:
                out.append(Finding(WATCH, "MAP unsteady at idle",
                                   f"Varied {min(map_kpa):.0f}–{max(map_kpa):.0f} kPa.",
                                   "Follows the RPM hunt, or points to a leaking or loose "
                                   "MAP hose."))

        map_v = d.series("MAP", "V", idle_rows)
        if map_v:
            m = _median(map_v)
            if max(map_v) > nm.limit("map_idle", "code33_above"):
                out.append(Finding(PROBLEM, "MAP reading near atmospheric while running",
                                   f"MAP reached {max(map_v):.2f} V at idle (code 33 territory).",
                                   "Check the MAP vacuum hose (off, cracked or blocked) and the "
                                   "sensor."))
            elif m > nm.get("map_idle")["high"]:
                out.append(Finding(WATCH, "Low vacuum at idle (high MAP)",
                                   f"MAP about {m:.2f} V at idle (normally "
                                   f"{nm.range_text('map_idle')}).",
                                   "Possible vacuum leak, EGR open at idle, late timing, or a bad "
                                   "MAP sensor or hose."))

        tps = d.series("TPS", "V", idle_rows)
        if tps:
            m = _median(tps)
            lim = nm.get("tps_closed")["limits"]
            if m < lim["fail_low"]:
                out.append(Finding(PROBLEM, "TPS reading very low at idle",
                                   f"{m:.2f} V (normal closed: {nm.range_text('tps_closed')}).",
                                   "Possible wiring or sensor fault (code 22 territory)."))
            elif m > lim["fail_high"]:
                out.append(Finding(PROBLEM, "TPS too high at closed throttle",
                                   f"{m:.2f} V: GM's chart expects under {lim['fail_high']} V closed.",
                                   "The ECM may not recognise idle. Check for a sticking throttle "
                                   "or a bad TPS."))
            elif m > nm.get("tps_closed")["high"]:
                out.append(Finding(WATCH, "TPS on the high side at closed throttle",
                                   f"{m:.2f} V (owners see {nm.range_text('tps_closed')}).",
                                   "Check for a sticking throttle or cable."))

    # ---- Closed loop and idle load (status flags)
    recent_flags = flag_rows[-10:]
    open_loop = bool(running and warm and recent_flags
                     and all("Closed loop" in f and not f["Closed loop"] for f in recent_flags))
    if open_loop:
        out.append(Finding(WATCH, "ECM in open loop on a warm engine",
                           "It isn't using the O2 sensor to correct fuel.",
                           "Usually a bad O2 sensor or coolant reading, or it simply hasn't "
                           "switched yet right after warm-up. Idle fuel control is cruder "
                           "in open loop."))
    if idling and any(f.get("A/C clutch on") for f in recent_flags):
        out.append(Finding(INFO, "A/C compressor running",
                           "It adds load at idle; compare the idle with the A/C off."))

    # ---- O2 sensor (only meaningful warm and running)
    o2 = d.series("O2 sensor", "mV")
    if o2 and running and not open_loop:
        if not warm:
            out.append(Finding(INFO, "O2 checks wait for a warm engine",
                               "Needs coolant temp mapped and above 160 °F."))
        else:
            secs = max(history[-1][0] - history[0][0], 1)
            mean = sum(o2) / len(o2)
            per10 = _crossings(o2, 450) * 10 / secs
            if mean < nm.limit("o2", "stuck_lean_below") and per10 < 1:
                out.append(Finding(PROBLEM, "Running lean (O2 stuck low)",
                                   f"O2 averaged {mean:.0f} mV and rarely crossed 450 mV.",
                                   "Lean usually means a vacuum leak, low fuel pressure or a "
                                   "clogged injector."))
            elif mean > nm.limit("o2", "stuck_rich_above") and per10 < 1:
                out.append(Finding(PROBLEM, "Running rich (O2 stuck high)",
                                   f"O2 averaged {mean:.0f} mV.",
                                   "Rich usually means a leaking injector, high fuel pressure, "
                                   "or a wrong coolant/MAP reading."))
            elif per10 < 2:
                out.append(Finding(WATCH, "O2 switching slowly",
                                   f"About {per10:.1f} crossings of 450 mV per 10 s "
                                   "(a healthy warm sensor usually switches several times).",
                                   "A lazy O2 sensor, or the ECM isn't in closed loop."))

    # ---- Fuel trims (raw counts, 128 = no correction)
    for name, kind in (("BLM (long trim)", "long-term"), ("INT (short trim)", "short-term")):
        vals = d.series(name)
        if not vals or not running:
            continue
        m = _median(vals)
        key = "blm" if kind == "long-term" else "int"
        lo, hi = nm.get(key)["low"], nm.get(key)["high"]
        at_limit = kind == "long-term" and (m >= nm.limit("blm", "prom_max") - 2
                                            or m <= nm.limit("blm", "prom_min") + 1)
        if m > hi:
            out.append(Finding(PROBLEM if at_limit else WATCH,
                               f"ECM adding lots of fuel ({kind} trim {m:.0f})",
                               f"128 means no correction; healthy is {lo}–{hi}. Above that it's "
                               "correcting for lean"
                               + (" and is at the stock PROM's limit." if at_limit else "."),
                               "Points to a vacuum leak or low fuel pressure."))
        elif m < lo or at_limit:
            out.append(Finding(PROBLEM if at_limit else WATCH,
                               f"ECM pulling lots of fuel ({kind} trim {m:.0f})",
                               f"128 means no correction; healthy is {lo}–{hi}. Below that it's "
                               "correcting for rich.",
                               "Points to a leaking injector, high fuel pressure or a bad "
                               "sensor reading."))

    # ---- Charging / battery
    batt = d.series("Battery", "V")
    if batt:
        m = _median(batt)
        if running and m < 12.8:
            out.append(Finding(WATCH, "Low charging voltage", f"{m:.1f} V with the engine running.",
                               "Alternator or belt. Low voltage can also upset idle control."))
        elif running and m > 15.2:
            out.append(Finding(PROBLEM, "Charging voltage too high", f"{m:.1f} V.",
                               "Voltage regulator fault."))
        elif not running and m < 11.8:
            out.append(Finding(WATCH, "Battery low", f"{m:.1f} V with the engine off."))

    if not running:
        out.insert(0, Finding(INFO, "Engine not running",
                              "Idle, O2 and trim checks run once the engine is idling."))

    if not any(f.severity in (PROBLEM, WATCH) for f in out):
        out.append(Finding(OK, "Nothing out of the ordinary",
                           "All checked values are within typical ranges."))
    if d.unmapped:
        out.append(Finding(INFO, "Not checked yet (not mapped): " + ", ".join(d.unmapped),
                           "Assign these on the Raw bytes tab to enable more checks."))

    order = {PROBLEM: 0, WATCH: 1, OK: 2, INFO: 3}
    return sorted(out, key=lambda f: order[f.severity])


def _split_column(col):
    """'Coolant temp (F)' -> ('Coolant temp', 'F'). Parts of a name that look like
    '(long trim)' or '(raw)' are kept as part of the name, not read as units."""
    if col.endswith(")") and " (" in col:
        name, units = col[:-1].rsplit(" (", 1)
        if " " not in units and units not in ("raw", "upstream", "downstream", "est."):
            return name, units
    return col, None


def load_log_params(path, fallback=None):
    """Parameters for the columns of a log that actually contain data.

    Units come from the column headers; logs without them borrow units from
    `fallback` ({name: param}) when given.
    """
    hist = load_log(path)
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    params = {}
    for col in header:
        if col in ("time", "elapsed_s", "raw_hex", "trouble_codes", "status_flags"):
            continue
        name, units = _split_column(col)
        if not any(v.get(name) is not None for _, v in hist):
            params[name] = {"name": name, "offset": None, "units": units or ""}
            continue
        if units is None:
            units = (fallback or {}).get(name, {}).get("units", "")
        params[name] = {"name": name, "offset": 0, "units": units}
    return hist, params


def load_log(path):
    """Read a CSV log written by the app or aldl_logger.py into a history."""
    history = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = [n for n in reader.fieldnames or []
                if n not in ("time", "elapsed_s", "raw_hex", "trouble_codes", "status_flags")]
        for row in reader:
            try:
                t = float(row["elapsed_s"])
            except (KeyError, ValueError):
                continue
            vals = {}
            for col in cols:
                name = _split_column(col)[0]
                try:
                    v = float(row[col])
                    vals[name] = v if math.isfinite(v) else None
                except (TypeError, ValueError):
                    vals[name] = None
            if row.get("trouble_codes"):
                vals["_codes"] = [(int(c), txt) for c, _, txt in
                                  (x.strip().partition(":")
                                   for x in row["trouble_codes"].split(";")) if c.isdigit()]
            if row.get("status_flags"):
                vals["_flags"] = {n.strip(): v.strip() == "1" for n, _, v in
                                  (x.partition("=") for x in row["status_flags"].split(";"))
                                  if n.strip()}
            history.append((t, vals))
    return history
