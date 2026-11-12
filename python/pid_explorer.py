"""PID explorer: find the maker-specific (mode 22) readings a car's computer
answers, and work out what they are.

Read-only: it only sends "22 xxxx" read requests. Nothing is written to the car.

1. Scan a range of PIDs and keep the ones that answer.
2. Watch them while the engine runs and while the owner does things (rev it,
   switch the A/C on, turn the wheel), alongside the standard readings.
3. For each byte, report which standard reading it moves with (and a formula
   that converts it) and which action flips it.

Treat what it finds as a lead: give each PID a name only once its behavior
clearly matches, and check it against a factory scan tool data list if you have one.
"""

import math
import statistics
from dataclasses import dataclass, field

DEFAULT_RANGE = ("1100", "12FF")
STANDARD = ("Engine RPM", "Coolant temp", "Vehicle speed", "Throttle position", "MAP",
            "Intake air temp", "Mass air flow", "Timing advance", "Battery (OBD port)")


def scan(elm, start, end, progress=None, stop=None):
    """[(pid_hex, [bytes])] for every PID in start..end (hex strings) that answers."""
    a, b = int(start, 16), int(end, 16)
    if not (0 <= a <= b <= 0xFFFF):
        raise ValueError("Range must be 0000-FFFF with start <= end.")
    found = []
    for n in range(a, b + 1):
        if stop and stop():
            break
        pid = f"{n:04X}"
        data = elm.mode22_raw(pid)
        if data:
            found.append((pid, data))
        if progress and (n - a) % 16 == 0:
            progress(n - a + 1, b - a + 1, len(found))
    return found


def _corr(xs, ys):
    n = len(xs)
    if n < 5:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _fit(xs, ys):
    """Least squares y = k*x + c."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return k, my - k * mx


def _nice(k):
    """Round a scale factor to something formula-like (4, 0.25, 0.392...)."""
    for cand in (1, 2, 4, 8, 10, 100, 0.5, 0.25, 0.125, 0.1, 0.01, 100 / 255, 5 / 255, 1.8, 0.555):
        if abs(k - cand) <= abs(cand) * 0.03:
            return cand
    return round(k, 4)


@dataclass
class Finding:
    pid: str
    form: str                    # "A", "B", "A*256+B" ...
    changes: bool
    matches: list = field(default_factory=list)   # (signal, r, formula, unit)
    actions: list = field(default_factory=list)   # (action, before, during)
    last: float = None

    def guess(self):
        if self.matches:
            sig, r, formula, unit = self.matches[0]
            return f"moves with {sig} (r={r:.2f}); try {formula}"
        if self.actions:
            act, before, during = self.actions[0]
            return f"changes with '{act}' ({before:g} → {during:g}): likely a switch or status"
        return "changes, but not with anything measured yet" if self.changes else "never changed"


class Explorer:
    def __init__(self, pids):
        self.pids = dict(pids)              # pid -> number of bytes
        self.samples = []                   # (t, {pid: [bytes]}, {standard: value})
        self.actions = []                   # (name, t0, t1 or None)

    def add(self, t, readings, standard):
        self.samples.append((t, readings, {k: v for k, v in standard.items() if v is not None}))

    def start_action(self, name, t):
        self.stop_action(t)
        self.actions.append([name, t, None])

    def stop_action(self, t):
        if self.actions and self.actions[-1][2] is None:
            self.actions[-1][2] = t

    def _forms(self, n):
        forms = [(chr(65 + i), lambda d, i=i: d[i]) for i in range(min(n, 4))]
        if n >= 2:
            forms.append(("A*256+B", lambda d: d[0] * 256 + d[1]))
        return forms

    def results(self):
        out = []
        for pid, n in self.pids.items():
            rows = [(t, d[pid], std) for t, d, std in self.samples if d.get(pid) and len(d[pid]) >= n]
            for name, fn in self._forms(n):
                xs = [fn(d) for _, d, _ in rows]
                if not xs:
                    continue
                f = Finding(pid, name, len(set(xs)) > 1, last=xs[-1])
                if f.changes:
                    for sig in STANDARD:
                        pair = [(fn(d), std[sig]) for _, d, std in rows if sig in std]
                        if len(pair) < 8:
                            continue
                        a, b = zip(*pair)
                        r = _corr(a, b)
                        if abs(r) >= 0.9:
                            k, c = _fit(a, b)
                            raw_k, k = k, _nice(k)
                            nice = k != round(raw_k, 4) or k in (1, 2, 4, 10)
                            c = round(statistics.mean(b) - k * statistics.mean(a), 1)
                            base = f"({name})" if "+" in name else name
                            formula = f"{base}*{k:g}" + (f"{c:+g}" if abs(c) >= max(0.5, 0.02 * abs(statistics.mean(b))) else "")
                            f.matches.append((sig, abs(r), formula, "", nice))
                    # Near-ties (signals that rise together): prefer a clean scale factor.
                    f.matches.sort(key=lambda m: (-round(m[1], 2), not m[4]))
                    f.matches = [m[:4] for m in f.matches]
                    for act, t0, t1 in self.actions:
                        during = [fn(d) for t, d, _ in rows if t0 <= t <= (t1 if t1 is not None else 1e18)]
                        before = [fn(d) for t, d, _ in rows if t0 - 15 <= t < t0]
                        if during and before:
                            mb, md = statistics.median(before), statistics.median(during)
                            if mb != md and abs(md - mb) >= max(1, statistics.pstdev(before) * 3):
                                f.actions.append((act, mb, md))
                out.append(f)
        # Keep the most useful view of each PID: a matched form, else an action form, else the first.
        best = {}
        for f in out:
            score = (2 if f.matches else 1 if f.actions else 0.5 if f.changes else 0,
                     (f.matches[0][1] + (0.04 if "+" in f.form else 0)) if f.matches else 0)
            if f.pid not in best or score > best[f.pid][0]:
                best[f.pid] = (score, f)
        return [best[p][1] for p in sorted(best)]


def to_profile_entry(name, pid, nbytes, formula, unit=""):
    """An enhanced_pids entry for a vehicle profile (formula uses A-D only)."""
    return {"name": name, "mode": "22", "pid": pid.upper(), "bytes": int(nbytes),
            "formula": formula, "unit": unit, "confidence": "app",
            "note": "Found with the PID explorer; not checked against a factory data list."}
