"""Stall capture: keep the last half minute of readings, and when the engine
dies, save that window and say what kind of death it looks like.

How the patterns are told apart (from the readings the computer sends):

- Reference lost (ignition pickup / module): the computer's RPM reading drops
  to zero within one reading while the manifold still shows strong vacuum, so
  the engine was still turning when the computer stopped seeing ignition
  reference pulses. A failing pickup coil or ignition module, often when hot.
- Lean, then died (fuel delivery): the O2 sensor goes and stays lean and the
  computer adds fuel (short-term trim climbs) a second or more before RPM
  falls. Fuel pump, pump relay or its wiring, or a fuel pressure drop.
  (Misfires also read lean, so this needs the lean reading to come FIRST.)
- Power / ground dropout: the data stops or battery voltage dips just before
  the death. The computer lost power or ground.
- Slow sag (idle air or mixture): RPM sinks over several seconds while the
  computer opens the idle air valve and vacuum fades. Air, idle control or
  mixture, not a sudden cutoff.

These are leads from slow data (the Fiero sends about one reading a second),
not proof. Repeat captures that agree are worth much more than one.
"""

import csv
import json
import os
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime

PRE_S, POST_S = 30.0, 4.0
RUNNING_RPM, DEAD_RPM = 350, 50

REFERENCE, FUEL, POWER, SAG, UNCLEAR = (
    "reference lost", "lean then died", "power dropout", "slow sag", "unclear")
WHAT_TO_CHECK = {
    REFERENCE: "Ignition: the distributor's pickup coil and ignition module (heat-test the module, "
               "check the pickup's resistance hot and cold), and the reference wiring to the computer.",
    FUEL: "Fuel delivery: fuel pressure at the moment it dies (gauge taped to the windshield), the "
          "fuel pump relay and its connector, pump wiring and grounds.",
    POWER: "Power and grounds: the computer's fuses and grounds (engine-block and body grounds), the "
           "ignition switch feed, battery cables.",
    SAG: "Air and idle control: vacuum leaks, throttle body and idle air passage, base timing, "
         "and anything making the computer's idle control run out of range.",
    UNCLEAR: "Capture a few more deaths; the pattern usually shows once there are several.",
}


@dataclass
class Capture:
    when: str
    vehicle: str
    verdict: str
    evidence: list
    check: str
    t_dead: float
    samples: list = field(default_factory=list)     # [(t, {name: value})]

    def summary(self):
        return f"{self.when}: {self.verdict}. " + " ".join(self.evidence)


def _o2v(v):
    if v.get("O2 sensor") is not None:
        return v["O2 sensor"] / 1000
    return v.get("O2 B1S1 (upstream)")


def _trim(v):
    if v.get("INT (short trim)") is not None:
        return (v["INT (short trim)"] - 128) * 100 / 128
    return v.get("Short trim B1")


def _vacuum_strong(v):
    m = v.get("MAP")
    if m is None:
        return None
    return m < 2.5 if m < 15 else m < 65          # Fiero volts / OBD kPa


def classify(samples, t_dead):
    """(verdict, [evidence]) for samples [(t, values)] around the death at t_dead."""
    before = [(t, v) for t, v in samples if t < t_dead and v.get("Engine RPM") is not None]
    if len(before) < 4:
        return UNCLEAR, ["Not enough readings before it died."]
    gaps = [b[0] - a[0] for a, b in zip(before, before[1:])]
    step = statistics.median(gaps) if gaps else 1.0
    # "Normal" RPM from well before the death, so a slow sag doesn't drag it down.
    early = [v["Engine RPM"] for t, v in before if t < t_dead - 10 and v["Engine RPM"] >= RUNNING_RPM]
    running = early or [v["Engine RPM"] for t, v in before if v["Engine RPM"] >= RUNNING_RPM]
    base_rpm = statistics.median(running) if running else 0
    dead = next(((t, v) for t, v in samples if t >= t_dead), None)
    last_t, last = before[-1]
    ev = []

    # Power: a gap in the data just before the death, or the voltage sagging.
    gap = (dead[0] - last_t) if dead else 0
    volts = [v.get("Battery") or v.get("Battery (OBD port)") for t, v in before[-4:]]
    volts = [x for x in volts if x is not None]
    if gap > max(3 * step, 2.5):
        ev.append(f"The data stopped for {gap:.1f} s right as it died (the computer may have lost power).")
        return POWER, ev
    if volts and min(volts) < 11.0:
        ev.append(f"Battery voltage fell to {min(volts):.1f} V just before it died.")
        return POWER, ev

    # Where did RPM start falling? Walk back while it's below 85% of normal.
    i = len(before) - 1
    while i > 0 and before[i][1]["Engine RPM"] < 0.85 * base_rpm:
        i -= 1
    fall_start = before[i + 1][0] if i + 1 < len(before) else last_t
    fall_time = (dead[0] if dead else last_t) - fall_start

    # Reference lost: RPM gone in one reading while vacuum is still strong.
    if dead and last[ "Engine RPM"] >= 0.7 * base_rpm and _vacuum_strong(dead[1]):
        ev.append(f"RPM went from {last['Engine RPM']:.0f} to 0 between two readings "
                  f"({dead[0] - last_t:.1f} s apart) while the manifold still showed strong vacuum "
                  f"(MAP {dead[1]['MAP']:.2f}): the engine was still turning when the computer "
                  "stopped seeing ignition reference pulses.")
        return REFERENCE, ev

    # Lean first: an unbroken run of lean O2 readings leading into the RPM fall,
    # after readings that weren't lean, and the computer adding fuel.
    upto = [(t, v) for t, v in before if t <= fall_start + step / 2 and _o2v(v) is not None]
    run = []
    for t, v in reversed(upto):
        if _o2v(v) < 0.3:
            run.append((t, v))
        else:
            break
    run.reverse()
    prior = [(t, v) for t, v in upto if not run or t < run[0][0]]
    o2p = [_o2v(v) for _, v in prior[-6:]]
    trp = [_trim(v) for _, v in prior[-6:] if _trim(v) is not None]
    trr = [_trim(v) for _, v in run if _trim(v) is not None]
    lean_first = len(run) >= 2 and run[0][0] < fall_start and o2p and statistics.mean(o2p) > 0.3
    trim_up = trr and trp and max(trr) - statistics.mean(trp) >= 5
    if lean_first:
        ev.append(f"The O2 sensor went lean ({max(_o2v(v) for _, v in run):.2f} V or less) "
                  f"{fall_start - run[0][0]:.1f} s before RPM started falling")
        if trim_up:
            ev[-1] += f", and the computer added fuel (short trim up to {max(trr):+.0f}%)"
        ev[-1] += ": it ran out of fuel rather than spark."
        return FUEL, ev

    # Slow sag: a gradual fall over several seconds.
    recent = [v["Engine RPM"] for t, v in before if t >= t_dead - 6]
    sagging = recent and statistics.mean(recent) < 0.8 * base_rpm
    if fall_time >= 3 or sagging:
        fall_time = max(fall_time, 6 if sagging else fall_time)
        iac = [v.get("IAC position") for t, v in before if t >= fall_start - 2]
        iac = [x for x in iac if x is not None]
        ev.append(f"RPM sank over several seconds (average {statistics.mean(recent):.0f} rpm in the last "
                  f"6 s against a normal {base_rpm:.0f}) instead of cutting off.")
        if len(iac) >= 2 and iac[-1] - iac[0] >= 10:
            ev.append(f"The computer opened the idle air valve from {iac[0]:.0f} to {iac[-1]:.0f} "
                      "steps trying to catch it.")
        return SAG, ev

    ev.append(f"RPM fell from about {base_rpm:.0f} to 0 in {max(fall_time, step):.1f} s; no clear "
              "fuel, spark or power signature in this capture.")
    if dead and _vacuum_strong(dead[1]) is False:
        ev.append("Vacuum had already collapsed by the first zero-RPM reading.")
    return UNCLEAR, ev


class Watcher:
    """Feed every sample; returns a Capture once a stall's window is complete."""

    def __init__(self, vehicle="", pre_s=PRE_S, post_s=POST_S):
        self.vehicle, self.pre_s, self.post_s = vehicle, pre_s, post_s
        self.buf = deque()
        self.running_since = None
        self.t_dead = None

    def feed(self, t, values):
        self.buf.append((t, {k: v for k, v in values.items() if not k.startswith("_")}))
        while self.buf and t - self.buf[0][0] > self.pre_s + self.post_s + 5:
            self.buf.popleft()
        rpm = values.get("Engine RPM")
        if self.t_dead is not None:
            if t - self.t_dead >= self.post_s:
                return self._finish()
            return None
        if rpm is None:
            return None
        if rpm >= RUNNING_RPM:
            if self.running_since is None:
                self.running_since = t
        elif rpm <= DEAD_RPM and self.running_since is not None and t - self.running_since >= 5:
            self.t_dead = t
        elif rpm <= DEAD_RPM:
            self.running_since = None
        return None

    def _finish(self):
        t_dead = self.t_dead
        samples = [(t, v) for t, v in self.buf if t >= t_dead - self.pre_s]
        verdict, ev = classify(samples, t_dead)
        self.t_dead, self.running_since = None, None
        return Capture(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.vehicle, verdict, ev,
                       WHAT_TO_CHECK[verdict], t_dead, samples)


# ------------------------------------------------------------ storage

def save(folder, cap):
    os.makedirs(folder, exist_ok=True)
    stamp = cap.when.replace("-", "").replace(":", "").replace(" ", "-")
    base = os.path.join(folder, f"stall-{stamp}")
    names = []
    for _, v in cap.samples:
        names += [k for k in v if k not in names]
    with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "elapsed_s"] + names)
        for t, v in cap.samples:
            w.writerow(["", f"{t - cap.t_dead:.2f}"] + ["" if v.get(n) is None else v[n] for n in names])
    meta = asdict(cap)
    meta.pop("samples")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return base + ".json"


def load_all(folder, vehicle=None):
    out = []
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if vehicle and meta.get("vehicle") not in (vehicle, "", None):
            continue
        meta["csv"] = os.path.join(folder, name[:-5] + ".csv")
        out.append(meta)
    return out


def tally(captures):
    """'3 stalls: 2 reference lost, 1 slow sag' style summary."""
    if not captures:
        return ""
    counts = {}
    for c in captures:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    parts = ", ".join(f"{n} {v}" for v, n in sorted(counts.items(), key=lambda x: -x[1]))
    return f"{len(captures)} stall{'s' if len(captures) != 1 else ''} captured: {parts}"
