"""Guided diagnostic tests: step-by-step procedures with a pass/watch/fail result.

Each test is a list of timed steps (what to do, how long) plus an evaluate()
that looks at the frames recorded during the test. The app's Tests tab runs
them; nothing here touches the GUI, so the logic is unit tested on its own.

The limits come from the Fiero vehicle profile, profiles/fiero-1985-88-2-8-v6.json (GM chart values where
found, otherwise GM figures for this era or Fiero owner reports, each with its
source). They say where to look; they aren't a diagnosis. Because the ECM only
sends a frame about every 1.5 s, steps that move a control (the throttle
sweep) ask for slow movements so enough frames are captured.
"""

import statistics
from dataclasses import dataclass, field

import analysis
import derived as d
import normals as nm

PASS, WATCH, FAIL, INCONCLUSIVE = "pass", "watch", "fail", "inconclusive"
ENGINE_OFF, IDLE, RPM_2000, DRIVING = "Engine off", "Engine idling", "Engine at 2000 rpm", "Driving"


@dataclass
class Step:
    text: str
    seconds: float
    demo_mode: str = "idle"       # how the built-in simulator behaves during this step


@dataclass
class Result:
    status: str
    headline: str
    details: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)


def _vals(samples, name, steps=None):
    return [v[name] for _, v, s in samples
            if v.get(name) is not None and (steps is None or s in steps)]


def _hist(samples, steps=None):
    return [(t, v) for t, v, s in samples if steps is None or s in steps]


def _med(xs):
    return statistics.median(xs) if xs else None


def _flag(samples, name, steps=None):
    return any((v.get("_flags") or {}).get(name) for _, v, s in samples
               if steps is None or s in steps)


def _codes(samples):
    seen = {}
    for _, v, _s in samples:
        for c, text in v.get("_codes") or []:
            seen[c] = text
    return seen


def _worst(*statuses):
    order = [PASS, INCONCLUSIVE, WATCH, FAIL]
    return max(statuses, key=order.index)


class GuidedTest:
    id = ""
    name = ""
    condition = ""
    summary = ""
    steps = []

    def stop_when(self, samples):
        """Return True to finish early (e.g. the engine stalled)."""
        return False

    def evaluate(self, samples, params, context):
        raise NotImplementedError


# ------------------------------------------------------------------ engine off

class KeyOnEngineOff(GuidedTest):
    id = "koeo"
    name = "Key-on baseline"
    condition = ENGINE_OFF
    summary = ("Checks the sensors that should read fixed values with the key on and the "
               "engine off, and records atmospheric pressure for later tests.")
    steps = [Step("Turn the key to ON but don't start the engine. Keep your foot off the pedal.",
                  12, "off")]

    def evaluate(self, samples, params, context):
        rpm = _vals(samples, d.RPM)
        if len(rpm) < 3:
            return Result(INCONCLUSIVE, "Not enough data. Check the connection and try again.")
        if max(rpm) > 0:
            return Result(INCONCLUSIVE, "The engine seems to be running. Run this with it off.")
        out, status = [], PASS
        mapv, tps, batt = _med(_vals(samples, d.MAP)), _med(_vals(samples, d.TPS)), _med(_vals(samples, d.BATT))
        num = {"baro_map_v": mapv, "tps_closed_v": tps, "battery_v": batt}
        if mapv is not None:
            if 3.8 <= mapv <= 5.0:
                out.append(f"MAP {mapv:.2f} V: plausible for atmospheric pressure "
                           f"(about {nm.range_text('map_koeo')} near sea level, lower at altitude).")
            else:
                status = _worst(status, WATCH)
                out.append(f"MAP {mapv:.2f} V is outside the usual 3.8–5.0 V with the engine off. "
                           "Check the MAP sensor, its connector and its vacuum hose.")
        if tps is not None:
            lim = nm.get("tps_closed")["limits"]
            if lim["fail_low"] <= tps <= nm.get("tps_closed")["high"]:
                out.append(f"Throttle closed: {tps:.2f} V (owners see {nm.range_text('tps_closed')}; "
                           f"GM: under {lim['fail_high']} V).")
            elif tps < lim["fail_low"] or tps > lim["fail_high"]:
                status = _worst(status, FAIL)
                out.append(f"Throttle closed reads {tps:.2f} V: outside what the ECM expects. "
                           "Check the TPS, its adjustment and wiring.")
            else:
                status = _worst(status, WATCH)
                out.append(f"Throttle closed reads {tps:.2f} V: on the high side; the ECM may not "
                           "always recognise idle. Check the TPS adjustment and a sticking throttle.")
        if batt is not None:
            if batt >= 12.2:
                out.append(f"Battery {batt:.1f} V with the engine off: good.")
            elif batt >= 11.8:
                status = _worst(status, WATCH)
                out.append(f"Battery {batt:.1f} V: a little low. Charge it before judging other tests.")
            else:
                status = _worst(status, FAIL)
                out.append(f"Battery {batt:.1f} V: low. A weak battery upsets idle control; charge or test it.")
        clt, mat = _med(_vals(samples, d.COOLANT)), _med(_vals(samples, d.MAT))
        if clt is not None and mat is not None and clt < 120 and mat < 120:
            diff = abs(clt - mat)
            if diff <= 15:
                out.append(f"Cold engine: coolant {clt:.0f} °F and intake air {mat:.0f} °F agree.")
            else:
                status = _worst(status, WATCH)
                out.append(f"Cold engine: coolant {clt:.0f} °F vs intake air {mat:.0f} °F differ by "
                           f"{diff:.0f} °F. One sensor may be off; compare with the outside temperature.")
        codes = _codes(samples)
        if codes:
            status = _worst(status, FAIL)
            out.append("Trouble codes set: " + ", ".join(f"{c} ({t})" for c, t in sorted(codes.items())))
        else:
            out.append("No trouble codes set.")
        head = {PASS: "Baseline readings look normal.", WATCH: "One or more readings need a look.",
                FAIL: "Something here needs fixing first."}[status]
        return Result(status, head, out, num)


class ThrottleSweep(GuidedTest):
    id = "tps_sweep"
    name = "Throttle sensor sweep"
    condition = ENGINE_OFF
    summary = ("Watches the throttle position sensor while you press and release the pedal slowly. "
               "A worn sensor shows dropouts: the reading falls while the pedal is still going down.")
    steps = [
        Step("Key ON, engine OFF. Foot off the pedal.", 6, "off"),
        Step("Slowly press the pedal all the way to the floor, taking about 15 seconds.", 16, "tps_up"),
        Step("Hold the pedal on the floor.", 6, "tps_floor"),
        Step("Slowly let the pedal back up, taking about 15 seconds.", 16, "tps_down"),
        Step("Foot off the pedal.", 6, "off"),
    ]

    def evaluate(self, samples, params, context):
        if max(_vals(samples, d.RPM) or [0]) > 0:
            return Result(INCONCLUSIVE, "The engine seems to be running. Run this with it off.")
        closed, up, floor = _vals(samples, d.TPS, {0}), _vals(samples, d.TPS, {1}), _vals(samples, d.TPS, {2})
        down, end = _vals(samples, d.TPS, {3}), _vals(samples, d.TPS, {4})
        if len(up) < 5 or len(down) < 5:
            return Result(INCONCLUSIVE, "Too few readings during the pedal movement. Repeat the test, "
                          "moving the pedal more slowly (the ECM sends a reading about every 1.5 s).")
        c0, c1 = _med(closed), _med(end)
        wot = max(up + floor + down)
        drops = [(a, b) for a, b in zip(up, up[1:]) if a - b > 0.15]
        rises = [(a, b) for a, b in zip(down, down[1:]) if b - a > 0.15]
        num = {"closed_v": c0, "closed_after_v": c1, "full_v": wot,
               "dropouts_pressing": len(drops), "dropouts_releasing": len(rises)}
        out, status = [], PASS
        out.append(f"Closed {c0:.2f} V → full throttle {wot:.2f} V → closed again {c1:.2f} V.")
        if drops or rises:
            status = FAIL
            out.append(f"Reading went the wrong way {len(drops) + len(rises)} time(s) while the pedal was "
                       "moving steadily: a classic sign of a worn TPS track, which causes stumbles "
                       "and unsteady idle. Replace or re-check the TPS.")
        else:
            out.append("Reading rose and fell smoothly with the pedal.")
        full_lo = nm.get("tps_full")["low"]
        if wot < 3.0:
            status = _worst(status, FAIL)
            out.append(f"Full throttle only reaches {wot:.2f} V (expect about "
                       f"{nm.range_text('tps_full')}). Check the TPS and that the pedal reaches "
                       "wide open.")
        elif wot < full_lo:
            status = _worst(status, WATCH)
            out.append(f"Full throttle reaches {wot:.2f} V, a bit low (expect about "
                       f"{nm.range_text('tps_full')}).")
        if c0 is not None and c1 is not None and abs(c1 - c0) > 0.08:
            status = _worst(status, WATCH)
            out.append(f"Closed reading didn't return to where it started ({c0:.2f} → {c1:.2f} V): "
                       "check for a sticking throttle or cable.")
        gl = d.glitches(_hist(samples)).get(d.TPS)
        if gl:
            status = _worst(status, FAIL)
            out.append(f"{len(gl)} one-frame spike(s): check the TPS connector and wiring.")
        head = {PASS: "Throttle sensor tracks the pedal smoothly.",
                WATCH: "Throttle sensor works but needs a look.",
                FAIL: "Throttle sensor problem found."}[status]
        return Result(status, head, out, num)


class ColdSensorMatch(GuidedTest):
    id = "cold_sensors"
    name = "Cold-start sensor match"
    condition = ENGINE_OFF
    summary = ("After the car has sat for 6+ hours, the coolant and intake air sensors should read "
               "the same (outside) temperature. A mismatch points to a bad sensor, a common cause "
               "of poor cold running.")
    steps = [Step("Car cold (sat at least 6 hours). Key ON, engine OFF.", 8, "off")]

    def evaluate(self, samples, params, context):
        clt, mat = _med(_vals(samples, d.COOLANT)), _med(_vals(samples, d.MAT))
        if clt is None or mat is None:
            return Result(INCONCLUSIVE, "Coolant or intake air temperature missing from the data.")
        num = {"coolant_f": clt, "intake_air_f": mat, "difference_f": abs(clt - mat)}
        if clt > 120:
            return Result(INCONCLUSIVE, f"The engine is warm ({clt:.0f} °F). Run this after the car has "
                          "sat for at least 6 hours.", numbers=num)
        diff = abs(clt - mat)
        line = f"Coolant {clt:.0f} °F, intake air {mat:.0f} °F (difference {diff:.0f} °F)."
        if diff <= 10:
            return Result(PASS, "Both temperature sensors agree.", [line], num)
        tip = ("Compare both with the outside temperature: the one that's far off is the suspect. "
               "A coolant sensor reading too cold makes the engine run rich; too warm, lean and hard "
               "to start.")
        return Result(WATCH if diff <= 20 else FAIL, "The temperature sensors disagree.", [line, tip], num)


# ---------------------------------------------------------------- engine running

class WarmUp(GuidedTest):
    id = "warmup"
    name = "Warm-up and thermostat"
    condition = IDLE
    summary = ("Logs a cold start until the engine is warm: how fast it warms, when the ECM goes "
               "into closed loop, and where the temperature levels off (the thermostat).")
    steps = [Step("Start the engine from COLD and let it idle in Park/Neutral. Click Finish once the "
                  "cooling fan has come on, or after about 20 minutes.", 1500, "idle")]

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        w = d.warmup(h)
        if not w:
            return Result(INCONCLUSIVE, "Not enough data. Let it run longer.")
        if w["start_f"] > 140:
            return Result(INCONCLUSIVE, f"The engine started warm ({w['start_f']:.0f} °F). "
                          "Run this from a cold start.")
        cl = d.time_to_closed_loop(h)
        mins = (h[-1][0] - h[0][0]) / 60
        num = {"start_f": w["start_f"], "max_f": w["max_f"], "rate_f_per_min": w["rate_f_per_min"],
               "levelled_at_f": w["levelled_at_f"], "closed_loop_after_s": cl, "minutes": mins}
        out, status = [], PASS
        out.append(f"Warmed from {w['start_f']:.0f} to {w['max_f']:.0f} °F in {mins:.1f} min"
                   + (f" ({w['rate_f_per_min']:.1f} °F/min)." if w["rate_f_per_min"] else "."))
        if cl is not None:
            out.append(f"Closed loop after {cl / 60:.1f} min.")
        elif w["max_f"] >= 160:
            status = FAIL
            out.append("The ECM never entered closed loop even though the engine got warm: suspect "
                       "the O2 sensor (or its wiring) or the coolant sensor.")
        stat = nm.limit("coolant_warm", "thermostat")
        if w["levelled_at_f"] is not None:
            if w["levelled_at_f"] >= stat - 10:
                out.append(f"Temperature levelled at about {w['levelled_at_f']:.0f} °F: the thermostat "
                           "is opening normally.")
            else:
                status = _worst(status, WATCH)
                out.append(f"Temperature levelled at only {w['levelled_at_f']:.0f} °F (the stock "
                           f"thermostat is {stat} °F): the thermostat may "
                           "be stuck open, keeping the engine too cool (rich running, poor idle).")
        elif mins >= 10 and w["max_f"] < 160:
            status = _worst(status, WATCH)
            out.append("Still under 160 °F after 10 minutes: a thermostat stuck open is likely.")
        if w["max_f"] >= nm.limit("coolant_warm", "overheat"):
            status = FAIL
            out.append(f"Reached {w['max_f']:.0f} °F: overheating. Stop and check coolant, fans and "
                       "thermostat.")
        head = {PASS: "Warm-up looks normal.", WATCH: "Warm-up needs a look.",
                FAIL: "Warm-up problem found."}[status]
        return Result(status, head, out, num)


class IdleQuality(GuidedTest):
    id = "idle"
    name = "Idle quality"
    condition = IDLE
    summary = ("Two minutes of warm idle with no load, analysed in depth: stability, the hunt's "
               "rhythm, what moves first, idle air, fuel trims and O2 switching.")
    steps = [Step("Warm engine idling in Park/Neutral. A/C off, lights off, hands and feet off.",
                  120, "idle")]

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        if len(h) < 20:
            return Result(INCONCLUSIVE, "Not enough data. Let it run the full two minutes.")
        rpm = _vals(samples, d.RPM)
        if _med(rpm) is None or _med(rpm) < 300:
            return Result(INCONCLUSIVE, "The engine doesn't seem to be running.")
        findings = analysis.analyze(h, params, window=None)
        out = []
        status = PASS
        for f in findings:
            if f.severity == analysis.PROBLEM:
                status = _worst(status, FAIL)
            elif f.severity == analysis.WATCH:
                status = _worst(status, WATCH)
            if f.severity in (analysis.PROBLEM, analysis.WATCH):
                out.append(f"{f.title}. {f.detail} {f.hint}".strip())
        osc = d.oscillation(h)
        num = {"rpm_median": _med(rpm), "rpm_min": min(rpm), "rpm_max": max(rpm)}
        rn, iac_n = nm.get("rpm_warm_idle"), nm.get("iac_warm_idle")
        iac_med = _med(_vals(samples, d.IAC))
        out.append(f"Idle speed: median {_med(rpm):.0f} rpm (owners report about "
                   f"{nm.range_text('rpm_warm_idle')}).")
        if iac_med is not None:
            num["iac_median"] = iac_med
            where = ("normal" if iac_n["low"] <= iac_med <= iac_n["high"]
                     else "low: extra air is getting in" if iac_med < iac_n["low"]
                     else "high: the ECM needs extra air to hold idle")
            out.append(f"IAC: median {iac_med:.0f} steps, {where} (warm idle is normally "
                       f"{nm.range_text('iac_warm_idle')}).")
            if iac_med > iac_n["high"] or iac_med < iac_n["low"]:
                status = _worst(status, WATCH)
        if osc and osc["swing"] > 100:
            num["cycle_s"] = osc["period_s"]
            kind = ("a slow, regular cycle: typical of the idle control chasing an air problem "
                    "(vacuum leak, EGR, dirty IAC or throttle body)"
                    if osc["period_s"] >= 3 and osc["strength"] >= 0.6
                    else "irregular: more typical of misfire, ignition or fuel delivery")
            out.append(f"RPM cycles every {osc['period_s']:.1f} s (regularity {osc['strength']:.2f}): "
                       f"{kind}.")
        ll = d.lead_lag(h, d.RPM, d.IAC)
        if ll and ll["r"] <= -0.5 and ll["lag_s"] >= 0:
            when = (f"{ll['lag_s']:.1f} s after RPM drops" if ll["lag_s"] > 0
                    else "as RPM drops (within one frame)")
            out.append(f"The IAC opens {when}: the ECM is reacting to the dips, which is normal "
                       "control; the cause is upstream.")
        lo = d.lead_lag(h, d.O2, d.RPM)
        if lo and lo["r"] >= 0.5 and lo["lag_s"] > 0:
            out.append(f"RPM dips follow lean O2 readings by about {lo['lag_s']:.1f} s: the dips look "
                       "fuel/air related (lean), pointing to a vacuum leak or fuel delivery.")
        sw = d.o2_switch_rate(h)
        if sw:
            num["o2_switches_per_s"] = sw[0]
        fc = d.fuel_correction(h)
        if fc is not None:
            num["fuel_correction_counts"] = fc
            if abs(fc) >= 15:
                status = _worst(status, WATCH)
                out.append(f"Total fuel correction {fc:+.0f} counts: the ECM is "
                           f"{'adding' if fc > 0 else 'removing'} a lot of fuel at idle.")
        if not out:
            out.append("Idle is steady and every checked value is in the typical range.")
        head = {PASS: "Idle looks healthy.", WATCH: "Idle has something worth checking.",
                FAIL: "Idle problem found."}[status]
        return Result(status, head, out, num)


class LoadResponse(GuidedTest):
    id = "load"
    name = "Idle load response"
    condition = IDLE
    summary = ("Adds load at idle (A/C, power steering, Drive) and checks that the ECM catches "
               "each one without the idle sagging too far.")
    steps = [
        Step("Warm engine idling in Park/Neutral, A/C off.", 20, "idle"),
        Step("Turn the A/C ON (fan on, A/C button on).", 25, "ac_on"),
        Step("Turn the A/C OFF.", 15, "idle"),
        Step("Slowly turn the steering wheel from lock to lock.", 20, "steer"),
        Step("Automatic: foot firmly on the brake, shift into Drive. Manual: just wait.", 20, "drive_idle"),
        Step("Back to Park/Neutral.", 10, "idle"),
    ]

    def evaluate(self, samples, params, context):
        base = _med(_vals(samples, d.RPM, {0}))
        if base is None or base < 300:
            return Result(INCONCLUSIVE, "The engine doesn't seem to be running.")
        out, status, num = [], PASS, {"idle_rpm": base}
        for step, label in ((1, "A/C on"), (3, "Steering"), (4, "In gear")):
            r = _vals(samples, d.RPM, {step})
            if not r:
                continue
            dip = base - min(r)
            iac_change = (_med(_vals(samples, d.IAC, {step})) or 0) - (_med(_vals(samples, d.IAC, {0})) or 0)
            num[f"{label} dip_rpm"] = dip
            line = (f"{label}: lowest {min(r):.0f} rpm ({dip:.0f} below idle), "
                    f"IAC {iac_change:+.0f} steps.")
            if min(r) < 500:
                status = FAIL
                line += " Close to stalling."
            elif dip > 250:
                status = _worst(status, WATCH)
                line += " A big sag: the idle air system struggles with load."
            out.append(line)
        if not _flag(samples, "A/C clutch on", {1}):
            out.append("The A/C clutch never engaged, so the A/C load wasn't tested "
                       "(low refrigerant or an A/C fault).")
        head = {PASS: "Idle holds up under load.", WATCH: "Idle sags under some loads.",
                FAIL: "Idle can't handle some loads."}[status]
        return Result(status, head, out, num)


class Hold2000(GuidedTest):
    id = "hold2000"
    name = "2000 rpm hold (leak vs fuel)"
    condition = RPM_2000
    summary = ("Compares fuel trims at idle and at 2000 rpm. Lean only at idle points to a vacuum "
               "leak; lean at both points to fuel delivery. Also checks O2 sensor switching.")
    steps = [
        Step("Warm engine idling in Park/Neutral.", 30, "idle"),
        Step("Hold the engine steady at 2000–2500 rpm with the pedal.", 40, "rpm2000"),
        Step("Let it idle.", 20, "idle"),
    ]

    def evaluate(self, samples, params, context):
        r2k = _med(_vals(samples, d.RPM, {1}))
        if r2k is None or not 1700 <= r2k <= 2900:
            return Result(INCONCLUSIVE, "RPM wasn't held near 2000–2500 during the hold step. "
                          "Repeat and keep it steady.")
        h_idle, h_2k = _hist(samples, {0, 2}), _hist(samples, {1})
        if not _flag(samples, "Closed loop", {1}):
            return Result(INCONCLUSIVE, "The ECM was in open loop. Warm the engine fully and repeat.")
        ci, c2 = d.fuel_correction(h_idle), d.fuel_correction(h_2k)
        sw = d.o2_switch_rate(h_2k)
        num = {"rpm_hold": r2k, "fuel_correction_idle": ci, "fuel_correction_2000": c2,
               "o2_switches_per_s_2000": sw[0] if sw else None}
        out, status = [], PASS
        if ci is not None and c2 is not None:
            out.append(f"Fuel correction: {ci:+.0f} counts at idle, {c2:+.0f} at 2000 rpm "
                       "(0 = none, + = adding fuel).")
            if ci >= 10 and c2 < 6:
                status = WATCH
                out.append("Lean at idle but normal at 2000 rpm: the classic sign of a VACUUM LEAK "
                           "(the leak is a big share of the small idle airflow). Check hoses, the "
                           "intake gaskets, PCV, brake booster line and the EGR valve.")
            elif ci >= 10 and c2 >= 10:
                status = WATCH
                out.append("Lean at idle and at 2000 rpm: points to FUEL DELIVERY (weak pump, "
                           "clogged filter, pressure regulator) or a MAP sensor reading wrong.")
            elif ci <= -10 and c2 <= -10:
                status = WATCH
                out.append("Rich at both: leaking injector, high fuel pressure or a bad coolant/MAP "
                           "reading.")
            else:
                out.append("Fuel trims are close to normal at both speeds.")
        if sw:
            if sw[0] >= nm.get("o2_switch_rate")["low"]:
                out.append(f"O2 switches {sw[0]:.1f} times per second at 2000 rpm: responsive.")
            elif sw[0] >= 0.3:
                status = _worst(status, WATCH)
                out.append(f"O2 switches only {sw[0]:.1f} times per second at 2000 rpm: getting lazy.")
            else:
                status = FAIL
                out.append(f"O2 switches {sw[0]:.2f} times per second at 2000 rpm: the sensor is "
                           "very slow or dead.")
        head = {PASS: "Fuel control looks healthy.", WATCH: "Fuel control points to a cause.",
                FAIL: "Fuel control problem found."}[status]
        return Result(status, head, out, num)


class Charging(GuidedTest):
    id = "charging"
    name = "Charging system"
    condition = RPM_2000
    summary = "Checks alternator output at idle and at 2000 rpm with electrical loads on."
    steps = [
        Step("Engine idling, accessories off.", 20, "idle"),
        Step("Turn on the headlights, heater fan on high and rear defrost, then hold about 2000 rpm.",
             30, "rpm2000"),
        Step("Let it idle, then turn the loads off.", 10, "idle"),
    ]

    def evaluate(self, samples, params, context):
        bi, b2 = _med(_vals(samples, d.BATT, {0})), _med(_vals(samples, d.BATT, {1}))
        r2k = _med(_vals(samples, d.RPM, {1}))
        if bi is None or b2 is None or r2k is None or r2k < 1500:
            return Result(INCONCLUSIVE, "RPM wasn't raised during the load step. Repeat.")
        num = {"battery_idle_v": bi, "battery_2000_loaded_v": b2}
        out = [f"{bi:.1f} V at idle, {b2:.1f} V at about {r2k:.0f} rpm with loads on."]
        lo, hi = nm.get("battery_running")["low"], nm.get("battery_running")["high"]
        if lo <= b2 <= hi:
            out.append(f"The alternator keeps up (normal {nm.range_text('battery_running')}).")
            return Result(PASS, "Charging system is healthy.", out, num)
        if b2 > 15.2:
            out.append("Too high: the voltage regulator is likely faulty, which can damage the battery.")
            return Result(FAIL, "Overcharging.", out, num)
        if b2 < 13.0:
            out.append("Too low: the alternator isn't keeping up. Check the belt, connections and "
                       "alternator. Low voltage also upsets idle control.")
            return Result(FAIL, "Undercharging.", out, num)
        out.append("A little low under load. Check the belt tension and battery connections.")
        return Result(WATCH, "Charging is marginal.", out, num)


# ------------------------------------------------------------------- driving

class RoadTest(GuidedTest):
    id = "road"
    name = "Road test"
    condition = DRIVING
    summary = ("Records a normal drive and looks for trouble codes, a missing speed signal, spark "
               "pulled for knock, EGR use, fuel trims at cruise and the gears in use.")
    steps = [Step("Drive normally for at least 5 minutes: some steady cruising, a few gentle "
                  "accelerations and one firm one. Click Finish when you're back. (Have a "
                  "passenger watch the screen.)", 2400, "drive")]

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        speeds = _vals(samples, d.SPEED)
        if len(h) < 30:
            return Result(INCONCLUSIVE, "Not enough data. Drive for at least 5 minutes.")
        out, status, num = [], PASS, {"minutes": (h[-1][0] - h[0][0]) / 60,
                                      "max_mph": max(speeds) if speeds else None}
        codes = _codes(samples)
        if codes:
            status = FAIL
            out.append("Trouble codes set during the drive: "
                       + ", ".join(f"{c} ({t})" for c, t in sorted(codes.items())))
        no_speed = sum(1 for _, v in h if (v.get(d.RPM) or 0) > 2000 and (v.get(d.TPS) or 0) > 1.0
                       and not v.get(d.SPEED))
        if speeds and max(speeds) == 0 and no_speed >= 5:
            status = _worst(status, WATCH)
            out.append("No vehicle speed reported while driving: check the speed sensor (code 24).")
        gears = d.gear_ratios(h)
        if gears:
            out.append("Gears seen (RPM per mph): " + ", ".join(f"{g['rpm_per_mph']:.0f}" for g in gears)
                       + ". Fewer clusters than gears used, or a scatter, can mean clutch or "
                       "converter slip.")
        knock = d.knock_retard_events(h)
        num["knock_events"] = len(knock)
        if len(knock) >= 3:
            status = _worst(status, WATCH)
            out.append(f"Spark was pulled {len(knock)} times at steady load: possible knock. Try "
                       "fresh higher-octane fuel; check timing and for carbon build-up.")
        elif knock:
            out.append(f"Spark pulled {len(knock)} time(s): occasional, usually harmless.")
        egr = _vals(samples, d.EGR)
        if egr:
            share = sum(1 for e in egr if e > 0) / len(egr)
            num["egr_active_share"] = share
            out.append(f"EGR commanded on {share:.0%} of the drive.")
        cruise = [(t, v) for t, v in h if (v.get(d.SPEED) or 0) >= 35]
        fc = d.fuel_correction(cruise)
        if fc is not None:
            num["fuel_correction_cruise"] = fc
            out.append(f"Fuel correction at cruise: {fc:+.0f} counts.")
            if abs(fc) >= 15:
                status = _worst(status, WATCH)
        ec = d.economy_index(h)
        if ec is not None:
            num["economy_index"] = ec
        head = {PASS: "Nothing unusual on the drive.", WATCH: "The drive turned up something to check.",
                FAIL: "The drive found a problem."}[status]
        return Result(status, head, out, num)


class StallCatcher(GuidedTest):
    id = "stall"
    name = "Stall catcher"
    condition = IDLE
    summary = ("Records until the engine stalls, then looks at the last 20 seconds before it died: "
               "what was falling, what the ECM was doing, and any glitches.")
    steps = [Step("Let the engine idle (or drive) as usual. The test stops by itself when the "
                  "engine stalls.", 3600, "stall")]

    def stop_when(self, samples):
        ran = False
        for _, v, _s in samples:
            r = v.get(d.RPM)
            if r is None:
                continue
            if r > 400:
                ran = True
            elif ran and r == 0:
                return True
        return False

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        stall_t = next((t for (t, v), (_, prev) in zip(h[1:], h)
                        if v.get(d.RPM) == 0 and (prev.get(d.RPM) or 0) > 0), None)
        if stall_t is None:
            return Result(INCONCLUSIVE, "No stall was recorded.")
        before = [(t, v) for t, v in h if stall_t - 20 <= t < stall_t]
        if len(before) < 4:
            return Result(INCONCLUSIVE, "Too little data before the stall.")
        out, num = [], {"stall_at_s": stall_t - h[0][0]}

        def trend(name, unit, dec=0):
            vals = [v[name] for _, v in before if v.get(name) is not None]
            if len(vals) >= 2:
                out.append(f"{name}: {vals[0]:.{dec}f} → {vals[-1]:.{dec}f} {unit} over the last "
                           f"{before[-1][0] - before[0][0]:.0f} s.")
                return vals
            return None

        trend(d.RPM, "rpm")
        iac = trend(d.IAC, "steps")
        o2 = trend(d.O2, "mV")
        trend(d.MAP, "V", 2)
        trend(d.TPS, "V", 2)
        trend(d.BATT, "V", 1)
        if iac and iac[-1] >= 120:
            out.append("The ECM had opened the idle air valve wide trying to save it: not enough air "
                       "(dirty throttle body/IAC) or a fuel/ignition cut-out.")
        if iac and iac[-1] <= 10:
            out.append("The idle air valve was nearly shut as it died: too much air elsewhere (vacuum "
                       "leak, EGR open) made the ECM close it.")
        if o2 and statistics.mean(o2[-3:]) < 250:
            out.append("O2 read lean just before the stall: fuel starvation or a big air leak.")
        gl = d.glitches(h)
        for name, hits in gl.items():
            out.append(f"{name} jumped for a single frame {len(hits)} time(s): check that sensor's "
                       "wiring; an intermittent signal can kill the idle.")
        codes = _codes(samples)
        if codes:
            out.append("Trouble codes: " + ", ".join(f"{c} ({t})" for c, t in sorted(codes.items())))
        return Result(FAIL, "Stall recorded. What happened just before it:", out, num)


ALL_TESTS = [KeyOnEngineOff(), ColdSensorMatch(), ThrottleSweep(), WarmUp(), IdleQuality(),
             LoadResponse(), Hold2000(), Charging(), StallCatcher(), RoadTest()]


class Runner:
    """Runs one test: tracks the current step and collects frames."""

    def __init__(self, test, t0):
        self.test = test
        self.samples = []            # [(t, values, step_index)]
        self.step = 0
        self.step_start = t0
        self.done = False

    @property
    def current(self):
        return self.test.steps[self.step]

    def time_left(self, now):
        return max(0.0, self.current.seconds - (now - self.step_start))

    def feed(self, t, values):
        if self.done:
            return
        self.samples.append((t, values, self.step))
        if self.test.stop_when(self.samples):
            self.done = True

    def tick(self, now):
        """Advance to the next step when this one's time is up."""
        if not self.done and self.time_left(now) <= 0:
            self.next_step(now)

    def next_step(self, now):
        if self.step + 1 < len(self.test.steps):
            self.step += 1
            self.step_start = now
        else:
            self.done = True

    def result(self, params, context=None):
        return self.test.evaluate(self.samples, params, context or {})
