"""OBD-II data source for the app: streams live readings from an ELM327 adapter.

It speaks the same line protocol as the other sources (see aldl_logger.parse_line):
  I,<text>              info
  M,<json definition>   the readings this car supports (sent once after connecting)
  J,<json sample>       one round of readings: {"values", "codes", "flags"}
Requests from the app (read codes, clear codes, freeze frame) are queued with
call() and run between rounds, so only one command is on the bus at a time.
"""

import json
import queue
import time

import elm327
import obd
import vehicle_profile

SKIP = {0x01, 0x03, 0x13, 0x1C, 0x1F, 0x21}         # status-type PIDs, read separately
BATTERY_NAME = "Battery (OBD port)"


class SerialTransport:
    """pyserial port with the non-blocking read the driver expects."""

    def __init__(self, port, baud):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0.05)

    def write(self, data):
        self.ser.write(data)

    def read(self, n):
        return self.ser.read(n)

    def close(self):
        self.ser.close()


def open_adapter(port):
    """Open an ELM327 adapter, trying the usual speeds (OBDLink SX: 115200)."""
    last = None
    for baud in (115200, 38400, 9600):
        t = SerialTransport(port, baud)
        try:
            e = elm327.Elm327(t, timeout=2)
            reply = " ".join(e.command("ATZ", timeout=3)).upper()
            if "ELM" in reply or "STN" in reply or "OBDLINK" in reply:
                return t
        except elm327.ElmError as ex:
            last = ex
        t.close()
    raise elm327.ElmError(f"No OBD adapter answered on {port}. {last or ''}".strip())


class ObdSource:
    def __init__(self, transport, profile=None, status_every=10.0):
        self.elm = elm327.Elm327(transport)
        self.transport = transport
        self.profile = profile
        self.status_every = status_every
        self.calls = queue.Queue()
        self.pids = []
        self.enhanced = []
        self.has_module_voltage = False
        self._closed = False

    # ------------------------------------------------------------- requests

    def call(self, fn, done):
        """Run fn(elm) in the reading thread; done(result_or_exception) afterwards."""
        self.calls.put((fn, done))

    def set_mode(self, mode):
        if hasattr(self.transport, "set_mode"):
            self.transport.set_mode(mode)

    def spray(self, spot):
        if hasattr(self.transport, "spray"):
            self.transport.spray(spot)

    def send(self, chars):
        pass

    # -------------------------------------------------------------- reading

    def definition(self):
        params = []
        for pid in self.pids:
            sig, name, unit, _b, _f, dec, rng = obd.pid_info(pid)
            p = {"name": name, "unit": unit, "units": unit, "decimals": dec, "offset": 0,
                 "signal": sig, "pid": pid}
            if rng:
                p["range"] = list(rng)
            params.append(p)
        if not self.has_module_voltage:
            params.append({"name": BATTERY_NAME, "unit": "V", "units": "V", "decimals": 1,
                           "offset": 0, "signal": "battery", "range": [10, 16]})
        for e in self.enhanced:
            params.append({"name": e["name"], "unit": e.get("unit", ""), "units": e.get("unit", ""),
                           "decimals": 1, "offset": 0, "signal": None, "enhanced": e["pid"]})
        return {"type": "obd2", "name": f"OBD-II · {self.elm.protocol_name()}",
                "protocol": self.elm.protocol_name(), "parameters": params,
                "trouble_codes": [], "flags": []}

    def _status(self):
        codes, flags = [], {}
        try:
            st = self.elm.status()
            if st:
                flags["Check engine light on"] = st["mil"]
            codes = [(c, obd.describe_dtc(c)) for c in self.elm.stored_codes()]
            fs = self.elm.fuel_status()
            if fs:
                flags["Closed loop"] = fs.startswith("Closed loop")
        except elm327.ElmError:
            pass
        return codes, flags

    def lines(self):
        yield "I,Connecting to the OBD-II adapter…\n"
        proto = self.elm.connect()
        yield f"I,Connected: {proto} ({self.elm.version})\n"
        self.has_module_voltage = 0x42 in self.elm.supported
        self.pids = [p for p in self.elm.supported if obd.pid_info(p) and p not in SKIP]
        if self.profile:
            for e in self.profile.get("enhanced_pids", []) or []:
                e = dict(e)
                e["_code"] = vehicle_profile.check_formula(e["formula"])
                self.enhanced.append(e)
        yield "M," + json.dumps(self.definition()) + "\n"
        codes, flags = self._status()
        last_status = time.time()
        while not self._closed:
            while not self.calls.empty():
                fn, done = self.calls.get()
                try:
                    done(fn(self.elm))
                except Exception as ex:          # reported to the app, reading continues
                    done(ex)
            values = {}
            for pid in self.pids:
                try:
                    v = self.elm.read(pid)
                except elm327.ElmError:
                    v = None
                if v is not None:
                    values[obd.pid_info(pid)[1]] = v
            if not self.has_module_voltage:
                try:
                    values[BATTERY_NAME] = self.elm.battery_voltage()
                except elm327.ElmError:
                    pass
            for e in self.enhanced:
                try:
                    data = self.elm.mode22(e["pid"], int(e.get("bytes", 1)))
                    if data:
                        values[e["name"]] = vehicle_profile.eval_formula(e["_code"], data)
                except elm327.ElmError:
                    pass
            if time.time() - last_status >= self.status_every:
                codes, flags = self._status()
                last_status = time.time()
            yield "J," + json.dumps({"values": values, "codes": codes, "flags": flags}) + "\n"
            if getattr(self.transport, "realtime", False):
                time.sleep(0.4)                   # live demo: roughly a real adapter's pace

    def close(self):
        self._closed = True
        self.elm.close()


# ------------------------------------------------------------ report helpers

def codes_report(elm):
    """Everything for the Codes tab, read in one go (runs in the reading thread)."""
    rep = {"status": elm.status(), "fuel_status": elm.fuel_status(),
           "stored": [(c, obd.describe_dtc(c)) for c in elm.stored_codes()],
           "pending": [(c, obd.describe_dtc(c)) for c in elm.pending_codes()],
           "permanent": [(c, obd.describe_dtc(c)) for c in elm.permanent_codes()],
           "protocol": elm.protocol_name()}
    try:
        rep["vin"] = elm.vin()
    except elm327.ElmError:
        rep["vin"] = None
    ff_pids = [p for p in (0x0C, 0x05, 0x04, 0x06, 0x07, 0x08, 0x09, 0x0D, 0x0B, 0x10, 0x11)
               if p in elm.supported]
    try:
        code, vals = elm.freeze_frame(ff_pids)
        rep["freeze"] = {"code": code, "values": {obd.pid_info(p)[1]: v for p, v in vals.items()}}
    except elm327.ElmError:
        rep["freeze"] = {"code": None, "values": {}}
    return rep
