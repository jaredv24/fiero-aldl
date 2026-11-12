"""Driver for ELM327-compatible OBD-II adapters (e.g. OBDLink SX USB).

The adapter is a serial device that takes text commands ("010C") and answers
with hex bytes ("41 0C 0B B8") followed by a ">" prompt. This module handles
setup, protocol detection and the replies' quirks (SEARCHING..., NO DATA,
several ECUs answering, multi-line replies), and turns them into bytes.

The transport is anything with write(bytes), read(n) -> bytes and close();
a pyserial port in the app, or SimElm (elm_sim.py) for the demo and tests.
"""

import re
import time

import obd

PROTOCOLS = {
    "1": "SAE J1850 PWM", "2": "SAE J1850 VPW", "3": "ISO 9141-2", "4": "ISO 14230-4 KWP (5 baud)",
    "5": "ISO 14230-4 KWP (fast)", "6": "ISO 15765-4 CAN 11-bit 500k", "7": "ISO 15765-4 CAN 29-bit 500k",
    "8": "ISO 15765-4 CAN 11-bit 250k", "9": "ISO 15765-4 CAN 29-bit 250k",
}
CAN_PROTOCOLS = {"6", "7", "8", "9"}


class ElmError(Exception):
    pass


class Elm327:
    def __init__(self, transport, timeout=6.0):
        self.t = transport
        self.timeout = timeout
        self.protocol = None
        self.version = ""
        self.supported = []

    # ------------------------------------------------------------- low level

    def command(self, cmd, timeout=None):
        """Send one command, return the reply lines (prompt and echo removed)."""
        self.t.write((cmd + "\r").encode("ascii"))
        deadline = time.time() + (timeout or self.timeout)
        buf = b""
        while time.time() < deadline:
            chunk = self.t.read(256)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
            else:
                time.sleep(0.005)
        else:
            raise ElmError(f"no reply to {cmd!r} (is the adapter connected and powered?)")
        text = buf.split(b">")[0].decode("ascii", errors="replace")
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]
        return [ln for ln in lines if ln.replace(" ", "") != cmd.replace(" ", "")]

    def request(self, cmd, timeout=None):
        """Send an OBD request; return the data lines as lists of bytes.

        Returns [] for NO DATA. Raises ElmError for adapter/bus errors.
        """
        lines = self.command(cmd, timeout)
        out = []
        for ln in lines:
            up = ln.upper()
            if up.startswith("SEARCHING") or up.startswith("BUS INIT"):
                continue
            if "NO DATA" in up:
                return []
            if up in ("?",) or "UNABLE TO CONNECT" in up or "ERROR" in up or "STOPPED" in up:
                raise ElmError(f"{cmd}: {ln}")
            ln = re.sub(r"^[0-9A-F]:", "", up.replace(" ", ""))     # CAN multi-frame "0:" prefix
            if re.fullmatch(r"[0-9A-F]{3}", ln):                       # CAN byte count line
                continue
            if not re.fullmatch(r"(?:[0-9A-F]{2})+", ln):
                continue
            out.append([int(ln[i:i + 2], 16) for i in range(0, len(ln), 2)])
        return out

    # ------------------------------------------------------------- setup

    def connect(self):
        """Reset the adapter, set it up and find the car's protocol."""
        self.command("ATZ", timeout=8)
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT1"):
            self.command(c)
        self.version = " ".join(self.command("ATI"))
        self.command("ATSP0")                   # automatic protocol search
        reply = self.request("0100", timeout=15)
        if not reply:
            raise ElmError("The car didn't answer. Is the ignition ON?")
        dp = " ".join(self.command("ATDPN")).upper().lstrip("A")
        self.protocol = dp[:1] if dp else None
        self.supported = self.supported_pids(first=reply)
        return self.protocol_name()

    def protocol_name(self):
        return PROTOCOLS.get(self.protocol, f"protocol {self.protocol}")

    def battery_voltage(self):
        """Voltage at the OBD port, measured by the adapter (ATRV), or None."""
        for ln in self.command("ATRV"):
            m = re.search(r"(\d+(?:\.\d+)?)\s*V", ln.upper())
            if m:
                return float(m.group(1))
        return None

    # ------------------------------------------------------------- mode 01

    @staticmethod
    def _payload(lines, header):
        """First reply line starting with header bytes -> the data after it."""
        for ln in lines:
            if ln[:len(header)] == header:
                return ln[len(header):]
        return None

    def supported_pids(self, first=None):
        pids, base = [], 0x00
        reply = first
        while True:
            if reply is None:
                reply = self.request(f"01{base:02X}")
            # Several modules (engine, transmission) can answer: merge their lists.
            got = []
            for ln in reply:
                if ln[:2] == [0x41, base] and len(ln) >= 6:
                    got += [p for p in obd.supported_from_bitmap(base, ln[2:6]) if p not in got]
            if not got:
                break
            pids += got
            if base + 0x20 not in got or base >= 0xC0:
                break
            base += 0x20
            reply = None
        return pids

    def mode01(self, pid):
        """Raw data bytes for a mode 01 PID, or None."""
        return self._payload(self.request(f"01{pid:02X}"), [0x41, pid])

    def read(self, pid):
        data = self.mode01(pid)
        return obd.decode_pid(pid, data) if data else None

    # ------------------------------------------------------------- codes etc

    def _dtcs(self, mode):
        resp = 0x40 + mode
        lines = self.request(f"{mode:02X}")
        data = []
        for ln in lines:
            if ln and ln[0] == resp:
                body = ln[1:]
                if self.protocol in CAN_PROTOCOLS and body:
                    body = body[1:]           # CAN: first byte is the code count
                data += body
            elif self.protocol in CAN_PROTOCOLS:
                data += ln                    # continuation frame
        return obd.dtcs_from_data(data)

    def stored_codes(self):
        return self._dtcs(0x03)

    def pending_codes(self):
        return self._dtcs(0x07)

    def permanent_codes(self):
        try:
            return self._dtcs(0x0A)
        except ElmError:
            return []                         # not supported before ~2010

    def clear_codes(self):
        """Mode 04: clears codes, freeze frame and readiness monitors."""
        lines = self.request("04", timeout=10)
        return any(ln and ln[0] == 0x44 for ln in lines)

    def status(self):
        data = self.mode01(0x01)
        return obd.decode_status(data) if data else None

    def fuel_status(self):
        data = self.mode01(0x03)
        return obd.FUEL_STATUS.get(data[0], f"status {data[0]}") if data else None

    def freeze_frame(self, pids):
        """Mode 02 frame 0: (code that stored it, {pid: value})."""
        dtc = self._payload(self.request("020200"), [0x42, 0x02, 0x00])
        code = obd.dtc_from_bytes(*dtc[:2]) if dtc and len(dtc) >= 2 else None
        values = {}
        if code:
            for pid in pids:
                data = self._payload(self.request(f"02{pid:02X}00"), [0x42, pid, 0x00])
                if data:
                    values[pid] = obd.decode_pid(pid, data)
        return code, values

    def vin(self):
        lines = self.request("0902", timeout=8)
        data = []
        for ln in lines:
            if ln[:2] == [0x49, 0x02]:
                data += ln[3:]                # 49 02 <frame#> data...
            else:
                data += ln                    # CAN continuation frames
        return obd.vin_from_bytes(data)

    def mode22_raw(self, pid_hex):
        """All data bytes of a maker-specific PID reply, or None if it doesn't answer."""
        hi, lo = int(pid_hex[:2], 16), int(pid_hex[2:], 16)
        data = self._payload(self.request(f"22{pid_hex}"), [0x62, hi, lo])
        return data or None

    def mode22(self, pid_hex, nbytes):
        """Maker-specific PID (mode 22). Returns the data bytes or None."""
        hi, lo = int(pid_hex[:2], 16), int(pid_hex[2:], 16)
        data = self._payload(self.request(f"22{pid_hex}"), [0x62, hi, lo])
        return data[:nbytes] if data and len(data) >= nbytes else None

    def close(self):
        try:
            self.t.close()
        except Exception:
            pass
