"""Vacuum leak hunt: which sprayed spot made the engine react?

How it works: with the engine idling you spray carb cleaner (or propane
from an unlit torch) at one spot at a time and mark it in the app. If that
spot leaks, the engine sucks the spray in: the mixture goes rich, so the O2
sensor jumps high, the computer pulls fuel (short-term trim drops) and the
idle usually changes. For each mark this compares the few seconds after the
spray with the few seconds before.

Works for the Fiero (O2 in mV, INT trim in counts) and OBD-II cars (O2 in
volts, short-term trims in percent). Readings arrive every ~1.5 s on the
Fiero, so spray in a short burst and wait a few seconds between spots.
"""

import statistics
from dataclasses import dataclass, field

BEFORE_S = 6.0
AFTER_S = (1.0, 9.0)       # look at this window after the mark


@dataclass
class Mark:
    t: float
    spot: str


@dataclass
class Verdict:
    spot: str
    t: float
    score: float               # 0 = no reaction ... 1+ = clear reaction
    reacted: str               # "yes" / "maybe" / "no" / "waiting"
    details: list = field(default_factory=list)


def _o2_volts(v):
    if v.get("O2 sensor") is not None:              # Fiero, mV
        return v["O2 sensor"] / 1000
    ups = [v[k] for k in ("O2 B1S1 (upstream)", "O2 B2S1 (upstream)") if v.get(k) is not None]
    return max(ups) if ups else None


def _short_trim(v):
    if v.get("INT (short trim)") is not None:       # Fiero counts: 128 = none; ~1 count ≈ 0.8%
        return (v["INT (short trim)"] - 128) * 100 / 128
    trims = [v[k] for k in ("Short trim B1", "Short trim B2") if v.get(k) is not None]
    return statistics.mean(trims) if trims else None


def _window(hist, t0, t1, fn):
    vals = [fn(v) for t, v in hist if t0 <= t <= t1]
    return [x for x in vals if x is not None]


def judge(hist, mark, now=None):
    """Verdict for one mark from the history [(t, values)]."""
    now = hist[-1][0] if hist else mark.t
    if now < mark.t + AFTER_S[1]:
        return Verdict(mark.spot, mark.t, 0.0, "waiting", ["Collecting readings after the spray…"])
    b = (mark.t - BEFORE_S, mark.t)
    a = (mark.t + AFTER_S[0], mark.t + AFTER_S[1])
    score, details = 0.0, []
    o2b, o2a = _window(hist, *b, _o2_volts), _window(hist, *a, _o2_volts)
    if o2b and o2a:
        rise = max(o2a) - statistics.mean(o2b)
        rich_share = sum(1 for x in o2a if x > 0.6) / len(o2a)
        base_share = sum(1 for x in o2b if x > 0.6) / len(o2b)
        if rise > 0.15 and rich_share - base_share >= 0.3:
            score += min(1.0, (rich_share - base_share) + rise)
            details.append(f"O2 went rich ({statistics.mean(o2b):.2f} → up to {max(o2a):.2f} V).")
    tb, ta = _window(hist, *b, _short_trim), _window(hist, *a, _short_trim)
    if tb and ta:
        drop = statistics.mean(tb) - min(ta)
        if drop >= 4:
            score += min(0.8, drop / 15)
            details.append(f"The computer pulled fuel (short trim {statistics.mean(tb):+.0f}% → "
                           f"{min(ta):+.0f}%).")
    rb = _window(hist, *b, lambda v: v.get("Engine RPM"))
    ra = _window(hist, *a, lambda v: v.get("Engine RPM"))
    if rb and ra:
        change = statistics.mean(ra) - statistics.mean(rb)
        steadier = (statistics.pstdev(ra) < statistics.pstdev(rb) * 0.6
                    if len(ra) > 2 and len(rb) > 2 and statistics.pstdev(rb) > 30 else False)
        if abs(change) >= 40:
            score += min(0.6, abs(change) / 200)
            details.append(f"Idle changed {change:+.0f} rpm.")
        if steadier:
            score += 0.3
            details.append("The idle smoothed out while the spray was being drawn in.")
    if not (o2b and o2a) and not (tb and ta):
        return Verdict(mark.spot, mark.t, 0.0, "no", ["No O2 or trim readings to judge from."])
    reacted = "yes" if score >= 0.8 else "maybe" if score >= 0.4 else "no"
    if not details:
        details.append("No change after the spray.")
    return Verdict(mark.spot, mark.t, round(score, 2), reacted, details)


def summary(verdicts):
    hits = sorted((v for v in verdicts if v.reacted in ("yes", "maybe")), key=lambda v: -v.score)
    if not verdicts:
        return "No spots marked yet."
    if not hits:
        return ("None of the sprayed spots made the engine react. If the idle still points to a "
                "leak, try spots you haven't sprayed yet (underneath hoses, the intake gaskets, "
                "throttle body base), or a smoke test.")
    first = hits[0]
    text = f"Most likely leak: {first.spot}."
    if len(hits) > 1:
        text += " Also reacted: " + ", ".join(v.spot for v in hits[1:]) + "."
    text += (" Confirm by spraying that spot again, and look closely at the hose or gasket there. "
             "Spray drawn into the intake can also come from a nearby spot, so check neighbours.")
    return text
