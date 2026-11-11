"""A simulated ELM327 adapter plugged into a 2002 Grand Prix GTP (J1850 VPW).

It answers the same text commands a real OBDLink does, so the real driver
(elm327.py) runs unchanged in Demo mode and in tests. The numbers are made up:
the demo car has a small vacuum leak (positive fuel trims at idle that drop
toward 0 at 2500 rpm) and stored lean codes P0171/P0174.
"""

import math
import random
import time

import obd

# What the demo engine computer supports (mode 01).
PCM_PIDS = [0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
            0x10, 0x11, 0x13, 0x14, 0x15, 0x18, 0x1C]
TCM_PIDS = [0x01, 0x0D]
O2_PRESENT = 0x13          # bank 1 sensors 1 and 2, bank 2 sensor 1
STORED = [(0x01, 0x71), (0x01, 0x74)]       # P0171, P0174
VIN = "1G2WR52102DEMO001"


def _bitmap(base, pids):
    bits = 0
    for p in pids:
        if base < p <= base + 32:
            bits |= 1 << (32 - (p - base))
    return bits.to_bytes(4, "big")


class SimElm:
    """Transport-compatible fake: write() commands, read() replies."""

    MODES = ("off", "idle", "rpm2500", "drive", "tps_up", "tps_floor", "tps_down", "stall")

    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.realtime = clock is None      # live demo: pace replies like a real adapter
        self.t0 = self.clock()
        self._in = b""
        self._out = b""
        self.mode, self.mode_t0 = "idle", 0.0
        self._coolant, self._last = None, None
        self.cleared = False
        self._stalled = False

    # -------------------------------------------------------------- transport

    def write(self, data):
        self._in += data
        while b"\r" in self._in:
            line, self._in = self._in.split(b"\r", 1)
            reply = self.handle(line.decode("ascii").strip().upper().replace(" ", ""))
            self._out += (reply + "\r\r>").encode("ascii")

    def read(self, n):
        out, self._out = self._out[:n], self._out[n:]
        return out

    def close(self):
        pass

    def set_mode(self, mode, t=None):
        if mode in self.MODES and mode != self.mode:
            self.mode = mode
            self.mode_t0 = (self.clock() - self.t0) if t is None else t
            if mode != "stall":
                self._stalled = False

    # ---------------------------------------------------------------- engine

    def state(self):
        t = self.clock() - self.t0
        mt = t - self.mode_t0
        running = self.mode in ("idle", "rpm2500", "drive", "stall")
        if self.mode == "stall":
            if self._stalled or mt > 25:
                self._stalled = True
                running = False
        if self._coolant is None:
            self._coolant = min(198.0, 70 + t * 3)
        elif self._last is not None:
            dt = max(0.0, t - self._last)
            if running:
                self._coolant = min(198.0, self._coolant + 3 * dt)
            else:
                self._coolant = max(70.0, self._coolant - 6 * dt)
        self._last = t
        ect = self._coolant + (1.5 * math.sin(t / 20) if self._coolant >= 196 else 0)
        warm = ect >= 150
        s = {"t": t, "running": running, "ect_f": ect, "iat_f": 86.0, "speed_mph": 0.0,
             "tps": 13.0, "closed": warm and running}
        if not running:
            s.update(rpm=0, maf=0.0, map=99, load=0, timing=0, stft=0.0, ltft=10.0 if not self.cleared else 0.0)
            if self.mode == "tps_up":
                s["tps"] = 13 + 72 * min(1.0, mt / 15)
            elif self.mode == "tps_floor":
                s["tps"] = 85.0
            elif self.mode == "tps_down":
                s["tps"] = 85 - 72 * min(1.0, mt / 15)
        elif self.mode == "rpm2500":
            s.update(rpm=2550 + random.uniform(-30, 30), maf=14.5, map=34, load=22, timing=34,
                     stft=1.5 * math.sin(t * 2.5), ltft=1.6, tps=19.0)
        elif self.mode == "drive":
            speed = 42 + 20 * math.sin(mt / 30)
            ratio = 60 if speed < 35 else 38
            s.update(speed_mph=speed, rpm=speed * ratio, maf=18 + 10 * math.sin(mt / 9), map=55,
                     load=35, timing=28, stft=2 * math.sin(t * 2), ltft=2.3, tps=22 + 8 * math.sin(mt / 9))
        else:                                   # idle (and stall run-up)
            rpm = 720 + 15 * math.sin(t * 0.7) + random.uniform(-10, 10)
            if self.mode == "stall":
                rpm -= 18 * mt
            s.update(rpm=max(rpm, 0), maf=4.2, map=31, load=19, timing=17,
                     stft=3 * math.sin(t * 2.2), ltft=11.7 if not self.cleared else 3.0)
        if not s["closed"]:
            s["stft"] = 0.0
        # O2: flat ~0.45 V until closed loop, then switching; downstream steady (good cat)
        if s["closed"]:
            s["o2_up1"] = 0.47 + 0.38 * math.tanh(3 * math.sin(t * 3.1))
            s["o2_up2"] = 0.47 + 0.38 * math.tanh(3 * math.sin(t * 2.9 + 1))
            s["o2_down"] = 0.66 + 0.04 * math.sin(t / 3)
        else:
            s["o2_up1"] = s["o2_up2"] = s["o2_down"] = 0.45
        s["battery"] = 13.9 if running else 12.5
        return s

    def pid_bytes(self, pid, s):
        def temp(f):
            return int(round((f - 32) / 1.8 + 40))

        def trim(pct):
            return max(0, min(255, int(round(pct * 128 / 100 + 128))))
        if pid == 0x01:
            mil = 0 if self.cleared else 0x80
            count = 0 if self.cleared else len(STORED)
            b = 0x07 | (0x00)                       # misfire/fuel/components supported, complete
            c = 0x01 | 0x04 | 0x20 | 0x40 | 0x80    # cat, evap, O2, O2 heater, EGR supported
            d = 0x04 if not self.cleared else 0xE5  # evap incomplete (all incomplete after clear)
            return [mil | count, b, c, d]
        if pid == 0x03:
            return [0x02 if s["closed"] else 0x01, 0x00]
        if pid == 0x04:
            return [int(s["load"] * 255 / 100)]
        if pid == 0x05:
            return [temp(s["ect_f"])]
        if pid in (0x06, 0x08):
            return [trim(s["stft"])]
        if pid in (0x07, 0x09):
            return [trim(s["ltft"] + (0.8 if pid == 0x09 else 0))]
        if pid == 0x0B:
            return [int(s["map"])]
        if pid == 0x0C:
            v = int(s["rpm"] * 4)
            return [v >> 8, v & 0xFF]
        if pid == 0x0D:
            return [int(round(s["speed_mph"] / 0.621371))]
        if pid == 0x0E:
            return [int((s["timing"] + 64) * 2)]
        if pid == 0x0F:
            return [temp(s["iat_f"])]
        if pid == 0x10:
            v = int(s["maf"] * 100)
            return [v >> 8, v & 0xFF]
        if pid == 0x11:
            return [int(s["tps"] * 255 / 100)]
        if pid == 0x13:
            return [O2_PRESENT]
        if pid == 0x14:
            return [int(s["o2_up1"] * 200), 0xFF]
        if pid == 0x15:
            return [int(s["o2_down"] * 200), 0xFF]
        if pid == 0x18:
            return [int(s["o2_up2"] * 200), 0xFF]
        if pid == 0x1C:
            return [0x01]                               # OBD-II as defined by CARB
        return None

    # -------------------------------------------------------------- commands

    def handle(self, cmd):
        if cmd.startswith("AT"):
            if cmd == "ATZ":
                return "\r\rELM327 v1.4b"
            if cmd == "ATI":
                return "ELM327 v1.4b"
            if cmd == "ATDPN":
                return "A2"
            if cmd == "ATRV":
                return f"{self.state()['battery'] + random.uniform(-0.05, 0.05):.1f}V"
            return "OK"
        if cmd.startswith("STI"):
            return "?"
        s = self.state()
        if cmd.startswith("01") and len(cmd) == 4:
            pid = int(cmd[2:], 16)
            if pid % 0x20 == 0:
                lines = []
                for pids, prefix in ((PCM_PIDS, ""), (TCM_PIDS, "")):
                    bm = _bitmap(pid, pids)
                    if any(b for b in bm) or pid == 0:
                        lines.append(prefix + f"41{pid:02X}" + bm.hex().upper())
                return "\r".join(lines) if lines else "NO DATA"
            if pid not in PCM_PIDS:
                return "NO DATA"
            data = self.pid_bytes(pid, s)
            if data is None:
                return "NO DATA"
            line = f"41{pid:02X}" + "".join(f"{b:02X}" for b in data)
            if pid == 0x0D:                             # TCM answers speed too
                line += "\r" + line
            return line
        if cmd == "03":
            if self.cleared:
                return "43000000000000"
            body = "".join(f"{a:02X}{b:02X}" for a, b in STORED)
            return "43" + body.ljust(12, "0")
        if cmd in ("07", "0A"):
            return "47000000000000" if cmd == "07" else "NO DATA"
        if cmd == "04":
            self.cleared = True
            return "44"
        if cmd == "020200":
            return "4202000000" if self.cleared else "4202000171"
        if cmd.startswith("02") and len(cmd) == 6:
            if self.cleared:
                return "NO DATA"
            pid = int(cmd[2:4], 16)
            frozen = {"running": True, "closed": True, "ect_f": 196, "iat_f": 84, "speed_mph": 0,
                      "tps": 13, "rpm": 760, "maf": 4.1, "map": 31, "load": 18, "timing": 16,
                      "stft": 6.3, "ltft": 16.4, "o2_up1": 0.18, "o2_up2": 0.2, "o2_down": 0.6,
                      "battery": 13.8}
            data = self.pid_bytes(pid, frozen)
            return "NO DATA" if data is None else (f"42{pid:02X}00" + "".join(f"{b:02X}" for b in data))
        if cmd == "0902":
            data = [0, 0, 0] + [ord(c) for c in VIN]
            lines = []
            for i in range(0, len(data), 4):
                lines.append(f"4902{i // 4 + 1:02X}" + "".join(f"{b:02X}" for b in data[i:i + 4]))
            return "\r".join(lines)
        return "NO DATA"
