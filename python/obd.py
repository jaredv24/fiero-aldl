"""OBD-II (SAE J1979) decoding: live data PIDs, trouble codes, readiness monitors.

Works for any 1996+ car. Conversions follow SAE J1979; display units are
converted to the app's units (F, mph, psi where relevant).
"""

# ------------------------------------------------------------------ mode 01

def _c_to_f(c):
    return c * 1.8 + 32


# pid: (signal id, display name, unit, bytes, decode(data) -> value, decimals, (lo, hi))
PIDS = {
    0x04: ("engine_load", "Calculated load", "%", 1, lambda d: d[0] * 100 / 255, 0, (0, 100)),
    0x05: ("coolant_temp", "Coolant temp", "F", 1, lambda d: _c_to_f(d[0] - 40), 0, (-40, 260)),
    0x06: ("stft_b1", "Short trim B1", "%", 1, lambda d: (d[0] - 128) * 100 / 128, 1, (-25, 25)),
    0x07: ("ltft_b1", "Long trim B1", "%", 1, lambda d: (d[0] - 128) * 100 / 128, 1, (-25, 25)),
    0x08: ("stft_b2", "Short trim B2", "%", 1, lambda d: (d[0] - 128) * 100 / 128, 1, (-25, 25)),
    0x09: ("ltft_b2", "Long trim B2", "%", 1, lambda d: (d[0] - 128) * 100 / 128, 1, (-25, 25)),
    0x0A: ("fuel_pressure_kpa", "Fuel pressure", "psi", 1, lambda d: d[0] * 3 * 0.145038, 0, (0, 80)),
    0x0B: ("map", "MAP", "kPa", 1, lambda d: d[0], 0, (0, 255)),
    0x0C: ("rpm", "Engine RPM", "rpm", 2, lambda d: (d[0] * 256 + d[1]) / 4, 0, (0, 7000)),
    0x0D: ("speed", "Vehicle speed", "mph", 1, lambda d: d[0] * 0.621371, 0, (0, 140)),
    0x0E: ("timing_advance", "Timing advance", "deg", 1, lambda d: d[0] / 2 - 64, 1, (-10, 50)),
    0x0F: ("intake_temp", "Intake air temp", "F", 1, lambda d: _c_to_f(d[0] - 40), 0, (-40, 250)),
    0x10: ("maf", "Mass air flow", "g/s", 2, lambda d: (d[0] * 256 + d[1]) / 100, 1, (0, 150)),
    0x11: ("tps", "Throttle position", "%", 1, lambda d: d[0] * 100 / 255, 0, (0, 100)),
    0x1F: ("run_time", "Run time", "s", 2, lambda d: d[0] * 256 + d[1], 0, None),
    0x21: ("distance_mil", "Distance with MIL on", "mi", 2, lambda d: (d[0] * 256 + d[1]) * 0.621371, 0, None),
    0x2F: ("fuel_level", "Fuel level", "%", 1, lambda d: d[0] * 100 / 255, 0, (0, 100)),
    0x33: ("baro", "Barometric pressure", "kPa", 1, lambda d: d[0], 0, (60, 110)),
    0x42: ("battery", "Module voltage", "V", 2, lambda d: (d[0] * 256 + d[1]) / 1000, 1, (10, 16)),
    0x46: ("ambient_temp", "Outside air temp", "F", 1, lambda d: _c_to_f(d[0] - 40), 0, (-40, 130)),
    0x5C: ("oil_temp", "Oil temp", "F", 1, lambda d: _c_to_f(d[0] - 40), 0, (-40, 300)),
}

# O2 sensor PIDs 0x14-0x1B: byte A = voltage / 200, byte B = trim. Which sensor each
# PID is depends on PID 0x13 (sensors present): bits 0-3 bank 1 sensors 1-4,
# bits 4-7 bank 2 sensors 1-4, in PID order 0x14..0x1B.
O2_PIDS = list(range(0x14, 0x1C))
O2_SLOTS = ["b1s1", "b1s2", "b1s3", "b1s4", "b2s1", "b2s2", "b2s3", "b2s4"]


def o2_param(pid):
    slot = O2_SLOTS[pid - 0x14]
    bank, sensor = slot[1], slot[3]
    where = "upstream" if sensor == "1" else "downstream" if sensor == "2" else f"sensor {sensor}"
    return (f"o2_{slot}", f"O2 B{bank}S{sensor} ({where})", "V", 2,
            lambda d: d[0] / 200, 2, (0, 1.1))


def pid_info(pid):
    if pid in PIDS:
        return PIDS[pid]
    if pid in O2_PIDS:
        return o2_param(pid)
    return None


def decode_pid(pid, data):
    info = pid_info(pid)
    if info is None or len(data) < info[3]:
        return None
    return info[4](data)


def supported_from_bitmap(base, data):
    """PIDs supported according to a 0x00/0x20/0x40... reply (4 bytes)."""
    out = []
    for i in range(32):
        if data[i // 8] & (0x80 >> (i % 8)):
            out.append(base + i + 1)
    return out


def fuel_system_closed_loop(data):
    """PID 0x03 byte A: 2 = closed loop (using O2 feedback)."""
    return bool(data) and data[0] == 0x02


FUEL_STATUS = {1: "Open loop (warming up)", 2: "Closed loop", 4: "Open loop (load or decel)",
               8: "Open loop (system fault)", 16: "Closed loop with a fault"}


# ------------------------------------------------------------- monitors

CONTINUOUS = ["Misfire", "Fuel system", "Components"]
NONCONTINUOUS_SPARK = ["Catalyst", "Heated catalyst", "Evaporative system", "Secondary air",
                       "A/C refrigerant", "O2 sensor", "O2 sensor heater", "EGR system"]


def decode_status(data):
    """PID 0x01: MIL, stored code count and readiness monitors.

    Returns {"mil": bool, "dtc_count": int, "monitors": [(name, "complete"|"not complete")]}
    Only monitors the car supports are listed.
    """
    a, b, c, d = (list(data) + [0, 0, 0, 0])[:4]
    out = {"mil": bool(a & 0x80), "dtc_count": a & 0x7F, "monitors": []}
    for i, name in enumerate(CONTINUOUS):
        if b & (1 << i):
            out["monitors"].append((name, "not complete" if b & (1 << (i + 4)) else "complete"))
    if not b & 0x08:            # spark ignition
        for i, name in enumerate(NONCONTINUOUS_SPARK):
            if c & (1 << i):
                out["monitors"].append((name, "not complete" if d & (1 << i) else "complete"))
    return out


# ------------------------------------------------------------------ DTCs

def dtc_from_bytes(hi, lo):
    """Two bytes -> 'P0171'. Returns None for 0000 (padding)."""
    if hi == 0 and lo == 0:
        return None
    letter = "PCBU"[hi >> 6]
    return f"{letter}{(hi >> 4) & 0x3}{hi & 0xF:X}{lo >> 4:X}{lo & 0xF:X}"


def dtcs_from_data(data):
    codes = []
    for i in range(0, len(data) - 1, 2):
        c = dtc_from_bytes(data[i], data[i + 1])
        if c and c not in codes:
            codes.append(c)
    return codes


# Generic (SAE J2012) powertrain code descriptions, the common ones. Codes that
# aren't here (including maker-specific P1xxx codes) are shown without a
# description; Ask Claude can explain them.
DTC_TEXT = {
    "P0010": "Camshaft position actuator circuit (bank 1)",
    "P0011": "Camshaft position timing over-advanced (bank 1)",
    "P0030": "O2 sensor heater control circuit (bank 1 sensor 1)",
    "P0036": "O2 sensor heater control circuit (bank 1 sensor 2)",
    "P0100": "Mass air flow circuit",
    "P0101": "Mass air flow circuit range/performance",
    "P0102": "Mass air flow circuit low input",
    "P0103": "Mass air flow circuit high input",
    "P0105": "MAP/barometric pressure circuit",
    "P0106": "MAP/barometric pressure circuit range/performance",
    "P0107": "MAP/barometric pressure circuit low input",
    "P0108": "MAP/barometric pressure circuit high input",
    "P0110": "Intake air temperature circuit",
    "P0112": "Intake air temperature circuit low input",
    "P0113": "Intake air temperature circuit high input",
    "P0115": "Engine coolant temperature circuit",
    "P0116": "Engine coolant temperature circuit range/performance",
    "P0117": "Engine coolant temperature circuit low input",
    "P0118": "Engine coolant temperature circuit high input",
    "P0120": "Throttle position sensor circuit",
    "P0121": "Throttle position sensor circuit range/performance",
    "P0122": "Throttle position sensor circuit low input",
    "P0123": "Throttle position sensor circuit high input",
    "P0125": "Insufficient coolant temperature for closed loop fuel control",
    "P0128": "Coolant thermostat (coolant temperature below thermostat regulating temperature)",
    "P0130": "O2 sensor circuit (bank 1 sensor 1)",
    "P0131": "O2 sensor circuit low voltage (bank 1 sensor 1)",
    "P0132": "O2 sensor circuit high voltage (bank 1 sensor 1)",
    "P0133": "O2 sensor circuit slow response (bank 1 sensor 1)",
    "P0134": "O2 sensor circuit no activity detected (bank 1 sensor 1)",
    "P0135": "O2 sensor heater circuit (bank 1 sensor 1)",
    "P0136": "O2 sensor circuit (bank 1 sensor 2)",
    "P0137": "O2 sensor circuit low voltage (bank 1 sensor 2)",
    "P0138": "O2 sensor circuit high voltage (bank 1 sensor 2)",
    "P0140": "O2 sensor circuit no activity detected (bank 1 sensor 2)",
    "P0141": "O2 sensor heater circuit (bank 1 sensor 2)",
    "P0150": "O2 sensor circuit (bank 2 sensor 1)",
    "P0151": "O2 sensor circuit low voltage (bank 2 sensor 1)",
    "P0152": "O2 sensor circuit high voltage (bank 2 sensor 1)",
    "P0153": "O2 sensor circuit slow response (bank 2 sensor 1)",
    "P0154": "O2 sensor circuit no activity detected (bank 2 sensor 1)",
    "P0155": "O2 sensor heater circuit (bank 2 sensor 1)",
    "P0171": "System too lean (bank 1)",
    "P0172": "System too rich (bank 1)",
    "P0174": "System too lean (bank 2)",
    "P0175": "System too rich (bank 2)",
    "P0200": "Injector circuit",
    "P0201": "Injector circuit (cylinder 1)",
    "P0202": "Injector circuit (cylinder 2)",
    "P0203": "Injector circuit (cylinder 3)",
    "P0204": "Injector circuit (cylinder 4)",
    "P0205": "Injector circuit (cylinder 5)",
    "P0206": "Injector circuit (cylinder 6)",
    "P0230": "Fuel pump primary circuit",
    "P0300": "Random/multiple cylinder misfire detected",
    "P0301": "Cylinder 1 misfire detected",
    "P0302": "Cylinder 2 misfire detected",
    "P0303": "Cylinder 3 misfire detected",
    "P0304": "Cylinder 4 misfire detected",
    "P0305": "Cylinder 5 misfire detected",
    "P0306": "Cylinder 6 misfire detected",
    "P0307": "Cylinder 7 misfire detected",
    "P0308": "Cylinder 8 misfire detected",
    "P0325": "Knock sensor 1 circuit (bank 1)",
    "P0327": "Knock sensor 1 circuit low input (bank 1)",
    "P0328": "Knock sensor 1 circuit high input (bank 1)",
    "P0335": "Crankshaft position sensor A circuit",
    "P0336": "Crankshaft position sensor A circuit range/performance",
    "P0340": "Camshaft position sensor circuit",
    "P0400": "Exhaust gas recirculation flow",
    "P0401": "Exhaust gas recirculation flow insufficient detected",
    "P0402": "Exhaust gas recirculation flow excessive detected",
    "P0410": "Secondary air injection system",
    "P0420": "Catalyst system efficiency below threshold (bank 1)",
    "P0430": "Catalyst system efficiency below threshold (bank 2)",
    "P0440": "Evaporative emission control system",
    "P0441": "Evaporative emission control system incorrect purge flow",
    "P0442": "Evaporative emission control system leak detected (small leak)",
    "P0446": "Evaporative emission control system vent control circuit",
    "P0449": "Evaporative emission control system vent valve/solenoid circuit",
    "P0455": "Evaporative emission control system leak detected (large leak)",
    "P0456": "Evaporative emission control system leak detected (very small leak)",
    "P0461": "Fuel level sensor circuit range/performance",
    "P0500": "Vehicle speed sensor",
    "P0505": "Idle control system",
    "P0506": "Idle control system RPM lower than expected",
    "P0507": "Idle control system RPM higher than expected",
    "P0560": "System voltage",
    "P0562": "System voltage low",
    "P0563": "System voltage high",
    "P0600": "Serial communication link",
    "P0601": "Internal control module memory check sum error",
    "P0700": "Transmission control system (see transmission codes)",
    "P0705": "Transmission range sensor circuit (PRNDL input)",
    "P0711": "Transmission fluid temperature sensor circuit range/performance",
    "P0741": "Torque converter clutch circuit performance or stuck off",
    "P0742": "Torque converter clutch circuit stuck on",
    "P0751": "Shift solenoid A performance or stuck off",
    "P0756": "Shift solenoid B performance or stuck off",
}


def describe_dtc(code):
    if code in DTC_TEXT:
        return DTC_TEXT[code]
    if code[:2] in ("P1", "P3") or code[0] in "BCU":
        return "Maker-specific code (not in the app's list)"
    return "Not in the app's list"


# ------------------------------------------------------------- VIN

def vin_from_bytes(data):
    """Mode 09 PID 02 payload -> 17-character VIN (drops leading padding)."""
    chars = "".join(chr(b) for b in data if 0x20 < b < 0x7F)
    return chars[-17:] if len(chars) >= 17 else (chars or None)
