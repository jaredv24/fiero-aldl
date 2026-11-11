"""Values calculated from the decoded data (not sent by the ECM directly).

Each function takes a history: a list of (t_seconds, {name: value, ...}) with
names from the Fiero A29 layout (definitions/1227170_fiero_v6.json). They
return None when the data can't support the calculation (missing values,
too few samples, engine not running), instead of guessing.

Limits worth knowing:
- The ECM sends one frame about every 1.5 s, so anything faster than about
  a 3 s cycle can't be seen in the readings (the O2 cross counter is the
  exception: it counts every switch between frames).
- MAP is in volts and the ECM's airflow tables aren't in the data stream, so
  load, airflow and fuel figures are relative indexes for comparing runs.
"""

import math
import statistics

RPM, IAC, MAP, TPS, O2 = "Engine RPM", "IAC position", "MAP", "TPS", "O2 sensor"
COOLANT, MAT, BATT, SPARK = "Coolant temp", "Manifold air temp", "Battery", "Spark advance"
BLM, INT, SPEED, EGR = "BLM (long trim)", "INT (short trim)", "Vehicle speed", "EGR duty cycle"
PW, CROSS = "Base pulse width (raw)", "O2 cross counter (raw)"


def series(hist, name):
    """[(t, value)] for samples where the value is present."""
    return [(t, v[name]) for t, v in hist if v.get(name) is not None]


def _running(hist):
    rpm = [x for _, x in series(hist, RPM)]
    return bool(rpm) and statistics.median(rpm) > 300


def sample_interval(hist):
    ts = [t for t, _ in hist]
    if len(ts) < 2:
        return None
    return statistics.median(b - a for a, b in zip(ts, ts[1:]))


# ----------------------------------------------------------------- O2 / fuel

def o2_switch_rate(hist):
    """O2 rich/lean switches per second, from the ECM's rollover counter.

    Exact even with slow frames: the counter counts every switch in between.
    Returns (switches_per_second, seconds_covered) or None.
    """
    pts = series(hist, CROSS)
    if len(pts) < 3 or not _running(hist):
        return None
    total = 0
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        d = (int(b) - int(a)) % 256          # counter rolls over 255 -> 0
        total += d
    secs = pts[-1][0] - pts[0][0]
    if secs <= 0:
        return None
    return total / secs, secs


def fuel_correction(hist):
    """Total fuel correction in counts: (BLM - 128) + (INT - 128), median.

    Positive = the ECM is adding fuel (it sees lean); negative = removing fuel.
    """
    vals = [v[BLM] - 128 + v[INT] - 128 for _, v in hist
            if v.get(BLM) is not None and v.get(INT) is not None]
    return statistics.median(vals) if vals else None


# ------------------------------------------------------------ idle rhythm

def _corr(a, b):
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def oscillation(hist, name=RPM, min_samples=12):
    """Find a repeating cycle in a value (autocorrelation).

    Returns {"period_s", "strength" (0-1, how regular), "swing" (peak to
    peak), "interval_s"} or None if there's no clear cycle or too few samples.
    Cycles shorter than about two frame intervals can't be detected.
    """
    pts = series(hist, name)
    dt = sample_interval(hist)
    if len(pts) < min_samples or not dt:
        return None
    x = [v for _, v in pts]
    n = len(x)
    rs = [_corr(x[:-lag], x[lag:]) for lag in range(1, n // 2)]
    # The cycle is the FIRST peak after the correlation has gone negative:
    # later peaks (2, 3 cycles) can score as high, and would give a multiple.
    best = None
    seen_negative = False
    for i, r in enumerate(rs):
        if r < 0:
            seen_negative = True
            continue
        nxt = rs[i + 1] if i + 1 < len(rs) else -1
        if seen_negative and r > 0.3 and r >= nxt:
            best = (i + 1, r)
            break
    if best is None:
        return None
    return {"period_s": best[0] * dt, "strength": best[1],
            "swing": max(x) - min(x), "interval_s": dt}


def lead_lag(hist, a, b, max_lag_s=6.0):
    """How b moves relative to a: best correlation over time shifts.

    Returns {"lag_s", "r"}: lag_s > 0 means b follows a by that long,
    lag_s < 0 means b moves first; r < 0 means they move in opposite
    directions. None if there's not enough data.
    """
    pa, pb = dict(series(hist, a)), dict(series(hist, b))
    ts = [t for t, _ in hist if t in pa and t in pb]
    dt = sample_interval(hist)
    if len(ts) < 12 or not dt:
        return None
    xa, xb = [pa[t] for t in ts], [pb[t] for t in ts]
    # In a repeating cycle, "opposite, 1 s later" and "same way, half a cycle
    # earlier" look identical. Only search shifts under half a cycle.
    osc = oscillation(hist, a)
    if osc:
        max_lag_s = min(max_lag_s, 0.45 * osc["period_s"])
    k_max = max(1, int(round(max_lag_s / dt)))
    scores = []
    for k in range(-k_max, k_max + 1):
        if k >= 0:
            r = _corr(xa[:len(xa) - k], xb[k:])
        else:
            r = _corr(xa[-k:], xb[:len(xb) + k])
        scores.append((k, r))
    top = max(abs(r) for _, r in scores)
    # Among shifts that fit almost equally well, the shortest delay is the
    # physical explanation.
    k, r = min(((k, r) for k, r in scores if abs(r) >= top - 0.05), key=lambda kr: abs(kr[0]))
    return {"lag_s": k * dt, "r": r}


# ------------------------------------------------- load, airflow, fuel use

def relative_load(hist, baro_v):
    """MAP as a share of atmospheric pressure (median), using the key-on,
    engine-off MAP reading as the atmospheric reference."""
    vals = [x for _, x in series(hist, MAP)]
    if not vals or not baro_v:
        return None
    return statistics.median(vals) / baro_v


def airflow_index(hist):
    """RPM x MAP / intake air temperature (K), median. Relative only."""
    vals = []
    for _, v in hist:
        if None in (v.get(RPM), v.get(MAP), v.get(MAT)):
            continue
        kelvin = (v[MAT] - 32) / 1.8 + 273.15
        vals.append(v[RPM] * v[MAP] / kelvin)
    return statistics.median(vals) if vals else None


def fuel_flow_index(hist):
    """Base pulse width x RPM, median: proportional to fuel flow. Relative."""
    vals = [v[PW] * v[RPM] / 1000 for _, v in hist
            if v.get(PW) is not None and v.get(RPM) is not None]
    return statistics.median(vals) if vals else None


def economy_index(hist):
    """Speed / fuel flow while moving (median). Relative MPG index."""
    vals = [v[SPEED] / (v[PW] * v[RPM] / 1000) for _, v in hist
            if v.get(SPEED) and v.get(PW) and v.get(RPM) and v[SPEED] >= 10]
    return statistics.median(vals) if vals else None


# ------------------------------------------------------- driving and parts

def gear_ratios(hist, min_mph=10, min_samples=3):
    """Group RPM/MPH while moving into clusters, one per gear in use.

    Returns [{"rpm_per_mph", "samples"}] from low to high ratio... sorted by
    ratio descending (1st gear first).
    """
    ratios = sorted(v[RPM] / v[SPEED] for _, v in hist
                    if v.get(SPEED) and v.get(RPM) and v[SPEED] >= min_mph)
    if not ratios:
        return []
    groups, cur = [], [ratios[0]]
    for r in ratios[1:]:
        if r > cur[-1] * 1.12:
            groups.append(cur)
            cur = [r]
        else:
            cur.append(r)
    groups.append(cur)
    out = [{"rpm_per_mph": statistics.median(g), "samples": len(g)}
           for g in groups if len(g) >= min_samples]
    return sorted(out, key=lambda g: -g["rpm_per_mph"])


GLITCH_LIMITS = {          # a single-frame jump bigger than this, which then
    TPS: 0.8,              # jumps straight back, isn't physically possible
    MAP: 1.2,              # for that sensor: it points to a bad connection.
    COOLANT: 25,
    MAT: 25,
    BATT: 2.0,
}


def glitches(hist):
    """Single-frame spikes per value: {name: [(t, value, neighbours_avg)]}."""
    out = {}
    for name, limit in GLITCH_LIMITS.items():
        pts = series(hist, name)
        hits = []
        for (t0, a), (t1, b), (t2, c) in zip(pts, pts[1:], pts[2:]):
            if abs(b - a) > limit and abs(b - c) > limit and abs(a - c) < limit / 2:
                hits.append((t1, b, (a + c) / 2))
        if hits:
            out[name] = hits
    return out


def warmup(hist):
    """Coolant warm-up: {"rate_f_per_min", "start_f", "max_f", "levelled_at_f"}.

    rate is over the rising part; levelled_at_f is where the temperature
    stopped climbing (the thermostat opening), if it did.
    """
    pts = series(hist, COOLANT)
    if len(pts) < 6:
        return None
    t0, c0 = pts[0]
    cmax = max(c for _, c in pts)
    rising = [(t, c) for t, c in pts if c <= cmax - 3] or pts
    t_end = rising[-1][0] if len(rising) > 1 else pts[-1][0]
    rate = ((rising[-1][1] - c0) / ((t_end - t0) / 60)) if t_end > t0 else None
    # Levelled: the last 25% of the samples stay within 4 F.
    tail = [c for _, c in pts[int(len(pts) * 0.75):]]
    level = statistics.median(tail) if tail and max(tail) - min(tail) <= 4 else None
    return {"rate_f_per_min": rate, "start_f": c0, "max_f": cmax, "levelled_at_f": level}


def time_to_closed_loop(hist):
    """Seconds from the first sample until the ECM entered closed loop."""
    if not hist:
        return None
    t0 = hist[0][0]
    for t, v in hist:
        if (v.get("_flags") or {}).get("Closed loop"):
            return t - t0
    return None


def knock_retard_events(hist, drop_deg=6, rpm_tol=150, map_tol=0.15):
    """Times when spark advance fell sharply while RPM and load held steady."""
    out = []
    for (t0, a), (t1, b) in zip(hist, hist[1:]):
        if None in (a.get(SPARK), b.get(SPARK), a.get(RPM), b.get(RPM), a.get(MAP), b.get(MAP)):
            continue
        if (a[SPARK] - b[SPARK] >= drop_deg and abs(a[RPM] - b[RPM]) <= rpm_tol
                and abs(a[MAP] - b[MAP]) <= map_tol and b[RPM] > 1000):
            out.append((t1, a[SPARK] - b[SPARK]))
    return out


# --------------------------------------------------------------- summary

def calculated(hist, baro_v=None):
    """Plain list of (label, value text, explanation) for display and Claude."""
    rows = []
    # Engine-running calculations only use frames where it was running, so a
    # window that spans key-on/engine-off doesn't produce nonsense.
    run = [(t, v) for t, v in hist if (v.get(RPM) or 0) > 300]
    sw = o2_switch_rate(run)
    if sw:
        rows.append(("O2 switches per second", f"{sw[0]:.2f}",
                     f"Exact, from the ECM's switch counter over {sw[1]:.0f} s."))
    fc = fuel_correction(run)
    if fc is not None:
        side = "adding fuel (sees lean)" if fc > 0 else "removing fuel (sees rich)" if fc < 0 else "no correction"
        rows.append(("Total fuel correction", f"{fc:+.0f} counts",
                     f"BLM + INT relative to 128: the ECM is {side}."))
    osc = oscillation(run) if len(run) >= 12 else None
    if osc and osc["swing"] > 100:
        rows.append(("RPM cycle", f"every {osc['period_s']:.1f} s",
                     f"Regularity {osc['strength']:.2f} (1 = perfectly regular), "
                     f"swing {osc['swing']:.0f} rpm."))
    ll = lead_lag(run, RPM, IAC) if len(run) >= 12 else None
    if ll and abs(ll["r"]) >= 0.4:
        rows.append(("IAC vs RPM", f"r = {ll['r']:+.2f}, lag {ll['lag_s']:+.1f} s",
                     "Negative r with IAC lagging = the ECM reacting to RPM changes."))
    lo2 = lead_lag(run, O2, RPM) if len(run) >= 12 else None
    if lo2 and abs(lo2["r"]) >= 0.4:
        rows.append(("RPM vs O2", f"r = {lo2['r']:+.2f}, lag {lo2['lag_s']:+.1f} s",
                     "Positive lag = RPM follows O2 changes."))
    rl = relative_load(run, baro_v) if run else None
    if rl is not None:
        rows.append(("Manifold pressure / atmosphere", f"{rl:.0%}",
                     "From MAP volts vs the key-on engine-off baseline. Lower = more vacuum."))
    af = airflow_index(run)
    if af is not None:
        rows.append(("Airflow index", f"{af:.1f}", "RPM x MAP / intake temp. Relative: compare runs."))
    ff = fuel_flow_index(run)
    if ff is not None:
        rows.append(("Fuel flow index", f"{ff:.0f}", "Pulse width x RPM. Relative: compare runs."))
    ec = economy_index(hist)
    if ec is not None:
        rows.append(("Economy index", f"{ec:.2f}", "Speed / fuel flow while moving. Relative."))
    gears = gear_ratios(hist)
    if gears:
        rows.append(("Gears seen (RPM per mph)", ", ".join(f"{g['rpm_per_mph']:.0f}" for g in gears),
                     "One cluster per gear used while driving."))
    wu = warmup(hist)
    if wu and wu["rate_f_per_min"] and wu["max_f"] - wu["start_f"] >= 10:
        rows.append(("Warm-up rate", f"{wu['rate_f_per_min']:.1f} °F/min",
                     f"{wu['start_f']:.0f} → {wu['max_f']:.0f} °F"
                     + (f", levelled at {wu['levelled_at_f']:.0f} °F" if wu["levelled_at_f"] else "")))
    knock = knock_retard_events(hist)
    if knock:
        rows.append(("Spark pulled (possible knock)", f"{len(knock)} time(s)",
                     "Spark advance dropped sharply at steady RPM and load."))
    gl = glitches(hist)
    for name, hits in gl.items():
        rows.append((f"{name} glitches", f"{len(hits)}",
                     "Single-frame spikes that snap back: check that sensor's connector and wiring."))
    return rows
