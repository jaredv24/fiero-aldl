"""Engine vacuum from the MAP sensor, and the classic vacuum-gauge readings.

A MAP sensor measures absolute manifold pressure, so vacuum is just
atmospheric pressure minus MAP:
- OBD-II cars report MAP in kPa (and often barometric pressure too), so the
  result is direct: inHg = (baro - MAP) kPa x 0.2953.
- The Fiero's ECM only gives MAP sensor volts. Using the key-on engine-off
  reading as "atmosphere" and treating the sensor as proportional to
  pressure gives an ESTIMATE: inHg ~= baro_inHg x (1 - MAP_V / baro_V).
  GM's 1-bar sensor isn't exactly proportional, so read it as a guide.

Negative vacuum means the manifold is above atmospheric: boost on a
supercharged engine like the Grand Prix GTP.

Vacuum-gauge patterns (warm idle, near sea level; general mechanic's rules,
not maker specs): steady 17-21 inHg is healthy; low and steady points to a
vacuum leak or late ignition timing; a slow drift in time with an idle hunt
points to a leak or mixture problem; quick irregular flicker points to
misfires, ignition or valves.
"""

import math
import statistics

INHG_PER_KPA = 0.2953
SEA_LEVEL_INHG = 29.92
SEA_LEVEL_KPA = 101.3
NORMAL_IDLE_INHG = (17.0, 21.0)   # at sea level; scales with atmospheric pressure
NAME = "Vacuum (est.)"


def from_kpa(map_kpa, baro_kpa=None):
    """inHg of vacuum (negative = boost) from OBD-II MAP and baro in kPa."""
    return ((baro_kpa or SEA_LEVEL_KPA) - map_kpa) * INHG_PER_KPA


def from_volts(map_v, baro_v):
    """Estimated inHg of vacuum from MAP volts and the engine-off MAP volts."""
    if not baro_v:
        return None
    return SEA_LEVEL_INHG * (1 - map_v / baro_v)


def boost_psi(inhg):
    return -inhg * 0.491 if inhg < 0 else 0.0


def _corr(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return 0.0 if sa == 0 or sb == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def classify(vac, rpm=None, baro_kpa=None):
    """Read a run of warm-idle vacuum values like a mechanic reads a gauge.

    Returns (pattern, explanation) or None if there's too little data.
    """
    if len(vac) < 8:
        return None
    scale = (baro_kpa or SEA_LEVEL_KPA) / SEA_LEVEL_KPA
    lo_ok, hi_ok = NORMAL_IDLE_INHG[0] * scale, NORMAL_IDLE_INHG[1] * scale
    mean = statistics.mean(vac)
    sd = statistics.pstdev(vac)
    swing = max(vac) - min(vac)
    steps = [abs(b - a) for a, b in zip(vac, vac[1:])]
    jitter = statistics.mean(steps) if steps else 0
    with_rpm = rpm is not None and len(rpm) == len(vac) and abs(_corr(vac, rpm)) >= 0.6
    rng = f"{min(vac):.1f}–{max(vac):.1f} inHg (average {mean:.1f})"
    normal = f"healthy is a steady {lo_ok:.0f}–{hi_ok:.0f} inHg here"
    if swing >= 3 and with_rpm and jitter < swing / 2:
        return ("drifting", f"Vacuum drifts {rng} in step with the RPM hunt; {normal}. A slow "
                            "drift with the idle points to a vacuum leak or a mixture problem.")
    if sd >= 1.2 and jitter >= swing / 3:
        return ("fluctuating", f"Vacuum flickers irregularly, {rng}; {normal}. Quick irregular "
                               "movement points to misfires, ignition or valve problems.")
    if mean < lo_ok - 2:
        return ("low", f"Vacuum is low but steady, {rng}; {normal}. Low steady vacuum points to "
                       "a vacuum leak or ignition timing that's too late.")
    if lo_ok - 2 <= mean <= hi_ok + 1 and sd < 1.2:
        return ("healthy", f"Vacuum is steady, {rng}; {normal}.")
    return ("unclear", f"Vacuum {rng}; {normal}.")
