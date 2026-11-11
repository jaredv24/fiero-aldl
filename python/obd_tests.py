"""Guided tests for OBD-II cars (any 1996+ car), judged against its profile.

Same structure as guided_tests.py (timed steps, a pass/watch/fail Result,
the same Runner), using standard OBD-II readings. context["profile"] is the
selected vehicle profile, or None for a car without one (general limits).
Demo modes are the simulated adapter's (elm_sim.SimElm.MODES).
"""

import statistics

import derived as d
import obd_analysis as oa
from guided_tests import (DRIVING, ENGINE_OFF, FAIL, IDLE, INCONCLUSIVE, PASS, WATCH,
                          GuidedTest, Result, Runner, Step, _codes, _hist, _med, _vals, _worst)

RPM_2500 = "Engine at 2500 rpm"
RPM, ECT, IAT, TPS, SPEED, MAF = (oa.RPM, oa.ECT, "Intake air temp", oa.TPS, oa.SPEED, oa.MAF)


def _batt(samples, steps=None):
    for name in oa.BATTERY:
        v = _vals(samples, name, steps)
        if v:
            return v
    return []


def _trim_total(samples, bank, steps):
    st, lt = oa.TRIMS[bank]
    rows = [v[st] + v[lt] for _, v, s in samples
            if s in steps and v.get(st) is not None and v.get(lt) is not None]
    return _med(rows)


def _profile(context):
    return (context or {}).get("profile")


class ObdKeyOn(GuidedTest):
    id = "obd_koeo"
    name = "Key-on baseline"
    condition = ENGINE_OFF
    summary = ("With the key on and the engine off: battery voltage, a steady closed throttle, "
               "coolant and intake air agreeing on a cold engine, and any stored codes.")
    steps = [Step("Turn the key to ON without starting the engine. Foot off the pedal.", 10, "off")]

    def evaluate(self, samples, params, context):
        if max(_vals(samples, RPM) or [0]) > 0:
            return Result(INCONCLUSIVE, "The engine seems to be running. Run this with it off.")
        out, status, num = [], PASS, {}
        b = _med(_batt(samples))
        if b is not None:
            num["battery_v"] = b
            if b >= 12.4:
                out.append(f"Battery {b:.1f} V: good.")
            elif b >= 12.0:
                status = WATCH
                out.append(f"Battery {b:.1f} V: a little low; charge it before other tests.")
            else:
                status = FAIL
                out.append(f"Battery {b:.1f} V: low. Charge or test it first.")
        tps = _vals(samples, TPS)
        if tps:
            num["tps_closed_pct"] = _med(tps)
            if max(tps) - min(tps) > 3:
                status = _worst(status, WATCH)
                out.append(f"Throttle reading wandered {min(tps):.0f}–{max(tps):.0f}% with the "
                           "pedal still: check the TPS and its connector.")
            else:
                out.append(f"Throttle closed reads a steady {_med(tps):.0f}%.")
        ect, iat = _med(_vals(samples, ECT)), _med(_vals(samples, IAT))
        if ect is not None and iat is not None and ect < 120:
            diff = abs(ect - iat)
            num["coolant_vs_intake_f"] = diff
            if diff <= 15:
                out.append(f"Cold engine: coolant {ect:.0f} °F and intake air {iat:.0f} °F agree.")
            else:
                status = _worst(status, WATCH)
                out.append(f"Cold engine: coolant {ect:.0f} °F vs intake air {iat:.0f} °F: one sensor "
                           "may be off.")
        codes = _codes(samples)
        if codes:
            status = FAIL
            out.append("Stored codes: " + ", ".join(f"{c} ({t})" for c, t in sorted(codes.items())))
        else:
            out.append("No stored codes.")
        head = {PASS: "Baseline looks normal.", WATCH: "Something needs a look.",
                FAIL: "Fix these first."}[status]
        return Result(status, head, out, num)


class ObdThrottleSweep(GuidedTest):
    id = "obd_tps_sweep"
    name = "Throttle sensor sweep"
    condition = ENGINE_OFF
    summary = ("Watches the throttle position while you press and release the pedal slowly. A worn "
               "sensor shows the reading falling while the pedal is still going down.")
    steps = [Step("Key ON, engine OFF. Foot off the pedal.", 5, "off"),
             Step("Slowly press the pedal all the way to the floor, taking about 15 seconds.", 16, "tps_up"),
             Step("Hold it on the floor.", 5, "tps_floor"),
             Step("Slowly let it back up, taking about 15 seconds.", 16, "tps_down"),
             Step("Foot off the pedal.", 5, "off")]

    def evaluate(self, samples, params, context):
        up, down = _vals(samples, TPS, {1}), _vals(samples, TPS, {3})
        if len(up) < 5 or len(down) < 5:
            return Result(INCONCLUSIVE, "Too few readings during the pedal movement; move it more slowly.")
        c0, c1 = _med(_vals(samples, TPS, {0})), _med(_vals(samples, TPS, {4}))
        full = max(up + _vals(samples, TPS, {2}) + down)
        drops = sum(1 for a, b in zip(up, up[1:]) if a - b > 5)
        rises = sum(1 for a, b in zip(down, down[1:]) if b - a > 5)
        out, status = [f"Closed {c0:.0f}% → full {full:.0f}% → closed {c1:.0f}%."], PASS
        if drops or rises:
            status = FAIL
            out.append(f"The reading went the wrong way {drops + rises} time(s) while the pedal moved "
                       "steadily: a worn throttle position sensor.")
        else:
            out.append("The reading followed the pedal smoothly.")
        if full < 60:
            status = _worst(status, WATCH)
            out.append(f"Full throttle only reached {full:.0f}% (usually well above 70%). Check that "
                       "the throttle opens fully.")
        if c0 is not None and c1 is not None and abs(c1 - c0) > 2:
            status = _worst(status, WATCH)
            out.append("Closed reading didn't return to where it started: sticking throttle or cable?")
        head = {PASS: "Throttle sensor tracks the pedal smoothly.", WATCH: "Throttle needs a look.",
                FAIL: "Throttle sensor problem found."}[status]
        return Result(status, head, out, {"closed_pct": c0, "full_pct": full,
                                          "wrong_way": drops + rises})


class ObdWarmUp(GuidedTest):
    id = "obd_warmup"
    name = "Warm-up and thermostat"
    condition = IDLE
    summary = ("From a cold start: how fast it warms, when it enters closed loop, where the "
               "temperature levels off (thermostat), and whether it overheats.")
    steps = [Step("Start the engine from COLD and let it idle. Click Finish once the cooling fan "
                  "comes on, or after about 20 minutes.", 1500, "idle")]

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        ect = _vals(samples, ECT)
        if len(ect) < 6:
            return Result(INCONCLUSIVE, "Not enough data; let it run longer.")
        if ect[0] > 140:
            return Result(INCONCLUSIVE, f"The engine started warm ({ect[0]:.0f} °F). Run from cold.")
        lim = (oa.normal(_profile(context), "coolant_temp") or {}).get("limits", {})
        stat = lim.get("thermostat", 195)
        mins = (h[-1][0] - h[0][0]) / 60
        tail = ect[int(len(ect) * 0.75):]
        level = statistics.median(tail) if max(tail) - min(tail) <= 6 else None
        cl = next((t - h[0][0] for t, v in h if (v.get("_flags") or {}).get("Closed loop")), None)
        out, status = [f"Warmed {ect[0]:.0f} → {max(ect):.0f} °F in {mins:.1f} min."], PASS
        if cl is not None:
            out.append(f"Closed loop after {cl / 60:.1f} min.")
        elif max(ect) >= 160:
            status = FAIL
            out.append("Never entered closed loop on a warm engine: O2 sensor/heater or coolant sensor.")
        if level is not None:
            if level >= stat - 10:
                out.append(f"Levelled at about {level:.0f} °F (thermostat {stat} °F): normal.")
            else:
                status = _worst(status, WATCH)
                out.append(f"Levelled at only {level:.0f} °F with a {stat} °F thermostat: it may be stuck "
                           "open (code P0128 territory).")
        if max(ect) >= lim.get("overheat", 245):
            status = FAIL
            out.append(f"Reached {max(ect):.0f} °F: overheating.")
        head = {PASS: "Warm-up looks normal.", WATCH: "Warm-up needs a look.", FAIL: "Warm-up problem."}[status]
        return Result(status, head, out, {"start_f": ect[0], "max_f": max(ect), "levelled_f": level,
                                          "closed_loop_s": cl})


class ObdIdle(GuidedTest):
    id = "obd_idle"
    name = "Idle quality"
    condition = IDLE
    summary = "Two minutes of warm idle checked against this car's normals: stability, MAF, trims, O2."
    steps = [Step("Warm engine idling in Park/Neutral. A/C off, lights off, hands and feet off.", 120, "idle")]

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        if len(h) < 15 or (_med(_vals(samples, RPM)) or 0) < 300:
            return Result(INCONCLUSIVE, "Not enough idle data; let it run the full two minutes.")
        prof = _profile(context)
        out, status = [], PASS
        for f in oa.analyze(h, prof, window=None):
            if f.severity == oa.PROBLEM:
                status = _worst(status, FAIL)
            elif f.severity == oa.WATCH:
                status = _worst(status, WATCH)
            if f.severity in (oa.PROBLEM, oa.WATCH):
                out.append(f"{f.title}. {f.detail} {f.hint}".strip())
        rpm = _vals(samples, RPM)
        out.insert(0, f"Idle: median {_med(rpm):.0f} rpm, {min(rpm):.0f}–{max(rpm):.0f}.")
        maf = _vals(samples, MAF)
        if maf:
            out.insert(1, f"MAF at idle: {_med(maf):.1f} g/s.")
        osc = d.oscillation(h)
        if osc and osc["swing"] > 100:
            out.append(f"RPM cycles every {osc['period_s']:.1f} s (regularity {osc['strength']:.2f}).")
        head = {PASS: "Idle looks healthy.", WATCH: "Idle has something to check.",
                FAIL: "Idle problem found."}[status]
        return Result(status, head, out, {"rpm_median": _med(rpm), "maf_median": _med(maf)})


class ObdHold2500(GuidedTest):
    id = "obd_hold2500"
    name = "2500 rpm hold (leak vs fuel)"
    condition = RPM_2500
    summary = ("Compares fuel trims at idle and at 2500 rpm on each bank. Lean only at idle points to "
               "a vacuum leak; lean at both points to fuel delivery.")
    steps = [Step("Warm engine idling in Park/Neutral.", 30, "idle"),
             Step("Hold the engine steady at about 2500 rpm with the pedal.", 40, "rpm2500"),
             Step("Let it idle.", 15, "idle")]

    def evaluate(self, samples, params, context):
        r = _med(_vals(samples, RPM, {1}))
        if r is None or not 2100 <= r <= 3000:
            return Result(INCONCLUSIVE, "RPM wasn't held near 2500. Repeat and keep it steady.")
        out, status, num = [], PASS, {"rpm_hold": r}
        verdicts = []
        for bank in ("B1", "B2"):
            ti, th = _trim_total(samples, bank, {0, 2}), _trim_total(samples, bank, {1})
            if ti is None or th is None:
                continue
            num[f"trim_{bank}_idle"], num[f"trim_{bank}_2500"] = ti, th
            out.append(f"Bank {bank[1]}: {ti:+.0f}% at idle, {th:+.0f}% at 2500 rpm.")
            if ti >= 8 and th < 5:
                verdicts.append("leak")
            elif ti >= 8 and th >= 8:
                verdicts.append("fuel")
            elif ti <= -8 and th <= -8:
                verdicts.append("rich")
        if "leak" in verdicts:
            status = WATCH
            out.append("Lean at idle but close to normal at 2500 rpm: a VACUUM LEAK is most likely "
                       "(the leak is a big share of the small idle airflow). "
                       + oa.known_issue_hint(_profile(context), "vacuum"))
        elif "fuel" in verdicts:
            status = WATCH
            out.append("Lean at idle and at 2500 rpm: points to FUEL DELIVERY (pump, filter, regulator) "
                       "or a MAF reading low.")
        elif "rich" in verdicts:
            status = WATCH
            out.append("Rich at both: leaking injector, high fuel pressure or a sensor reading wrong.")
        else:
            out.append("Fuel trims are close to normal at both speeds.")
        head = {PASS: "Fuel control looks healthy.", WATCH: "Fuel trims point to a cause."}[status]
        return Result(status, head, out, num)


class ObdCatalyst(GuidedTest):
    id = "obd_catalyst"
    name = "Catalyst check"
    condition = RPM_2500
    summary = ("At a steady 2500 rpm a healthy catalytic converter keeps the downstream O2 sensor "
               "steady while the upstream one switches. A downstream sensor that mirrors the "
               "upstream one means a weak catalyst (P0420/P0430).")
    steps = [Step("Fully warm engine in Park/Neutral. Hold a steady 2500 rpm.", 45, "rpm2500")]

    def evaluate(self, samples, params, context):
        up, down = _vals(samples, oa.O2_UP[0]), _vals(samples, oa.O2_DOWN[0])
        if len(up) < 10 or len(down) < 10:
            return Result(INCONCLUSIVE, "This car doesn't report both O2 sensors, or too few readings.")
        cu, cd = oa._crossings(up, 0.45), oa._crossings(down, 0.45)
        if cu < 4:
            return Result(INCONCLUSIVE, "The upstream O2 wasn't switching (not warm / not closed loop yet).")
        ratio = cd / cu
        swing = max(down) - min(down)
        out = [f"Upstream switched {cu} times, downstream {cd} times (ratio {ratio:.2f}); "
               f"downstream ranged {min(down):.2f}–{max(down):.2f} V."]
        num = {"upstream_crossings": cu, "downstream_crossings": cd, "ratio": ratio}
        if ratio <= 0.3 and swing < 0.35:
            out.append("The downstream sensor stays steady: the catalyst is storing oxygen normally.")
            return Result(PASS, "Catalyst looks healthy.", out, num)
        if ratio >= 0.7:
            out.append("The downstream sensor mirrors the upstream one: the catalyst is weak.")
            return Result(FAIL, "Catalyst looks weak.", out, num)
        out.append("Somewhere in between: re-check after a longer drive.")
        return Result(WATCH, "Catalyst is borderline.", out, num)


class ObdCharging(GuidedTest):
    id = "obd_charging"
    name = "Charging system"
    condition = RPM_2500
    summary = "Battery voltage at idle and at about 2000 rpm with electrical loads on."
    steps = [Step("Engine idling, accessories off.", 20, "idle"),
             Step("Headlights on, heater fan on high, rear defrost on; hold about 2000 rpm.", 30, "rpm2500"),
             Step("Let it idle, then turn the loads off.", 10, "idle")]

    def evaluate(self, samples, params, context):
        bi, bh = _med(_batt(samples, {0})), _med(_batt(samples, {1}))
        if bi is None or bh is None or (_med(_vals(samples, RPM, {1})) or 0) < 1500:
            return Result(INCONCLUSIVE, "RPM wasn't raised during the load step, or no voltage reading.")
        n = oa.normal(_profile(context), "battery") or {"low": 13.5, "high": 15.0}
        out = [f"{bi:.1f} V at idle, {bh:.1f} V at 2000 rpm with loads."]
        num = {"idle_v": bi, "loaded_v": bh}
        if n["low"] <= bh <= n["high"]:
            return Result(PASS, "Charging system is healthy.", out, num)
        if bh > 15.2:
            return Result(FAIL, "Overcharging.", out + ["Voltage regulator fault."], num)
        if bh < 13.0:
            return Result(FAIL, "Undercharging.", out + ["Alternator, belt or connections."], num)
        return Result(WATCH, "Charging is marginal.", out, num)


class ObdStall(GuidedTest):
    id = "obd_stall"
    name = "Stall catcher"
    condition = IDLE
    summary = "Records until the engine stalls, then shows what happened in the last 20 seconds."
    steps = [Step("Let it idle (or drive) as usual. The test stops by itself when the engine stalls.",
                  3600, "stall")]

    def stop_when(self, samples):
        ran = False
        for _, v, _s in samples:
            r = v.get(RPM)
            if r is None:
                continue
            if r > 400:
                ran = True
            elif ran and r == 0:
                return True
        return False

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        stall = next((t for (t, v), (_, p) in zip(h[1:], h) if v.get(RPM) == 0 and (p.get(RPM) or 0) > 0), None)
        if stall is None:
            return Result(INCONCLUSIVE, "No stall was recorded.")
        before = [(t, v) for t, v in h if stall - 20 <= t < stall]
        out = []
        for name, unit, dec in ((RPM, "rpm", 0), (MAF, "g/s", 1), ("Long trim B1", "%", 1),
                                ("Short trim B1", "%", 1), (oa.O2_UP[0], "V", 2), (TPS, "%", 0)):
            vals = [v[name] for _, v in before if v.get(name) is not None]
            if len(vals) >= 2:
                out.append(f"{name}: {vals[0]:.{dec}f} → {vals[-1]:.{dec}f} {unit}.")
        b = [v.get(oa.BATTERY[1]) or v.get(oa.BATTERY[0]) for _, v in before]
        b = [x for x in b if x is not None]
        if b and min(b) < 12.5:
            out.append(f"Voltage dipped to {min(b):.1f} V before the stall.")
        return Result(FAIL, "Stall recorded. What happened just before it:", out, {})


class ObdRoad(GuidedTest):
    id = "obd_road"
    name = "Road test"
    condition = DRIVING
    summary = "A normal drive: codes, speed signal, trims at cruise, and the gears in use."
    steps = [Step("Drive normally for at least 5 minutes, including steady cruising. Click Finish "
                  "when you're back. (Have a passenger watch the screen.)", 2400, "drive")]

    def evaluate(self, samples, params, context):
        h = _hist(samples)
        if len(h) < 30:
            return Result(INCONCLUSIVE, "Not enough data; drive for at least 5 minutes.")
        out, status = [], PASS
        codes = _codes(samples)
        if codes:
            status = FAIL
            out.append("Codes during the drive: " + ", ".join(f"{c} ({t})" for c, t in sorted(codes.items())))
        speeds = _vals(samples, SPEED)
        if speeds and max(speeds) == 0 and (_med(_vals(samples, RPM)) or 0) > 1500:
            status = _worst(status, WATCH)
            out.append("No vehicle speed while driving: check the speed sensor.")
        gears = d.gear_ratios(h)
        if gears:
            out.append("Gears seen (RPM per mph): " + ", ".join(f"{g['rpm_per_mph']:.0f}" for g in gears))
        for bank in ("B1", "B2"):
            st, lt = oa.TRIMS[bank]
            rows = [v[st] + v[lt] for _, v in h if (v.get(SPEED) or 0) >= 35
                    and v.get(st) is not None and v.get(lt) is not None]
            if rows:
                m = statistics.median(rows)
                out.append(f"Fuel trim at cruise, bank {bank[1]}: {m:+.0f}%.")
                if abs(m) > 10:
                    status = _worst(status, WATCH)
        head = {PASS: "Nothing unusual on the drive.", WATCH: "The drive turned up something to check.",
                FAIL: "The drive found a problem."}[status]
        return Result(status, head, out, {"max_mph": max(speeds) if speeds else None})


ALL_TESTS = [ObdKeyOn(), ObdThrottleSweep(), ObdWarmUp(), ObdIdle(), ObdHold2500(),
             ObdCatalyst(), ObdCharging(), ObdStall(), ObdRoad()]

__all__ = ["ALL_TESTS", "Runner", "RPM_2500"]
