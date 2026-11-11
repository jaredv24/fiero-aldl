#!/usr/bin/env python3
"""Live viewer and logger for GM 160 baud ALDL data (1988 Fiero 2.8 V6).

Reads frames from the Arduino interface (see ../arduino) and shows them as a
live dashboard, logs them to CSV, or helps you work out which byte is which.

Examples:
    python aldl_logger.py --simulate                 # try it with no car
    python aldl_logger.py --list-ports               # find your Arduino
    python aldl_logger.py --port auto                # find it automatically
    python aldl_logger.py --cable --mode raw         # USB ALDL cable, no Arduino
                                                     # (run cable_check.py first)
    python aldl_logger.py --port COM3 --mode pulses  # check the signal
    python aldl_logger.py --port COM3 --mode raw     # see raw bytes change
    python aldl_logger.py --port COM3 --log idle.csv # dashboard + CSV log
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DEF = os.path.join(HERE, "definitions", "1227170_template.json")
DEMO_DEF = os.path.join(HERE, "definitions", "demo.json")

GREEN, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


# --------------------------------------------------------------------------
# Parsing and decoding
# --------------------------------------------------------------------------

def parse_line(line):
    """Parse one line from the Arduino. Returns (kind, payload) or None.

    D,<len>,<hex>  -> ("D", bytes)
    P,<level>,<us> -> ("P", (level, us))
    S,... / I,...  -> ("S"/"I", text)
    """
    line = line.strip()
    if len(line) < 2 or line[1] != ",":
        return None
    kind, rest = line[0], line[2:]
    try:
        if kind == "D":
            length, hexdata = rest.split(",", 1)
            data = bytes.fromhex(hexdata)
            if len(data) != int(length):
                return None
            return ("D", data)
        if kind == "P":
            level, us = rest.split(",", 1)
            return ("P", (int(level), int(us)))
        if kind in ("S", "I"):
            return (kind, rest)
    except ValueError:
        return None
    return None


def load_definition(path):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    for p in d.get("parameters", []):
        p.setdefault("size", 1)
        p.setdefault("scale", 1.0)
        p.setdefault("add", 0.0)
        p.setdefault("units", "")
        p.setdefault("decimals", 1)
        p.setdefault("signed", False)
        p.setdefault("offset", None)
    return d


def decode(frame, definition):
    """Return a list of (name, value_or_None, units, param) for a frame."""
    out = []
    for p in definition["parameters"]:
        off, size = p["offset"], p["size"]
        if off is None or off + size > len(frame):
            out.append((p["name"], None, p["units"], p))
            continue
        raw = int.from_bytes(frame[off:off + size], "big", signed=p["signed"])
        out.append((p["name"], raw * p["scale"] + p["add"], p["units"], p))
    return out


def fmt_value(value, p):
    if value is None:
        return "--" if p["offset"] is None else "(short frame)"
    return f"{value:.{p['decimals']}f}"


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------

def load_cable_settings():
    path = os.path.join(HERE, "cable_settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def find_port():
    """Pick the Arduino's serial port automatically (Mac, Windows or Linux)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        sys.exit("pyserial is not installed. Run:  pip install pyserial")
    ports = list(list_ports.comports())
    hints = ("arduino", "usbmodem", "usbserial", "wchusbserial", "ch340", "ttyacm", "ttyusb")
    found = [p for p in ports
             if any(h in (p.device + " " + (p.description or "")).lower() for h in hints)]
    # On a Mac, /dev/cu.* is the right one to open (/dev/tty.* can hang).
    found = [p for p in found if not p.device.startswith("/dev/tty.")] or found
    if len(found) == 1:
        return found[0].device
    if not found:
        sys.exit("No Arduino found. Is it plugged in? Run with --list-ports to see all ports.")
    sys.exit("More than one possible port:\n  " + "\n  ".join(p.device for p in found)
             + "\nPick one with --port.")


class SerialSource:
    def __init__(self, port, baud=115200):
        try:
            import serial  # pyserial
        except ImportError:
            sys.exit("pyserial is not installed. Run:  pip install pyserial")
        self.ser = serial.Serial(port, baud, timeout=0.5)
        time.sleep(2.0)  # most Arduinos reset when the port opens
        self.ser.reset_input_buffer()

    def send(self, chars):
        self.ser.write(chars.encode("ascii"))

    def lines(self):
        while True:
            raw = self.ser.readline()
            if raw:
                yield raw.decode("ascii", errors="replace")

    def close(self):
        self.ser.close()


class ReplaySource:
    """Plays back a capture file written with --capture."""

    def __init__(self, path, speed=1.0):
        self.path, self.speed = path, speed

    def send(self, chars):
        pass

    def lines(self):
        with open(self.path, "r", encoding="ascii", errors="replace") as f:
            for line in f:
                if self.speed > 0 and line.startswith("D,"):
                    time.sleep(1.2 / self.speed)
                yield line

    def close(self):
        pass


class SimSource:
    """Fake ECM that has a hunting idle, for trying the software without a car.

    Frames follow definitions/demo.json, not the real ECM layout.
    """

    def __init__(self, frame_len=20, interval=0.5):
        self.frame_len, self.interval = frame_len, interval
        self.t0 = time.time()
        self.pulse_mode = False

    def send(self, chars):
        for c in chars:
            if c == "p":
                self.pulse_mode = True
            elif c == "d":
                self.pulse_mode = False

    def frame(self, t):
        # Idle drops toward stall, ECM opens the IAC, rpm kicks back up.
        phase = (t % 6.0) / 6.0
        rpm = 1150 - 550 * phase + random.uniform(-25, 25)
        iac = 15 + 80 * phase
        coolant_f = min(195.0, 70 + t * 3)
        o2_mv = 450 + 400 * math.sin(t * 3.0)
        map_kpa = 34 + 8 * phase
        spark_deg = 12 + 6 * phase
        # Byte values are the inverse of the formulas in definitions/demo.json.
        vals = {
            0: rpm / 25,
            1: (coolant_f + 40) / 1.8,
            2: 0.52 / 0.0196,
            3: (map_kpa - 10.35) / 0.369,
            4: o2_mv / 4.44,
            5: iac,
            6: 128 + 10 * phase,
            7: 128 + 6 * math.sin(t),
            8: spark_deg / 0.352,
            9: 13.9 / 0.1,
        }
        frame = bytearray(random.randrange(256) for _ in range(self.frame_len))
        for off, v in vals.items():
            frame[off] = max(0, min(255, int(round(v))))
        return bytes(frame)

    def lines(self):
        yield "I,simulator ready\n"
        while True:
            if self.pulse_mode:
                for _ in range(40):
                    bit = random.random() < 0.5
                    on = (4400 if bit else 420) + random.randint(-150, 150)
                    yield f"P,0,{on}\n"
                    yield f"P,1,{6250 - on}\n"
                time.sleep(0.25)
            else:
                time.sleep(self.interval)
                f = self.frame(time.time() - self.t0)
                yield f"D,{len(f)},{f.hex().upper()}\n"

    def close(self):
        pass


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def clear():
    sys.stdout.write("\033[H\033[2J")


def bar(value, lo, hi, width=24):
    if value is None or hi <= lo:
        return ""
    frac = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    n = int(round(frac * width))
    return "[" + "#" * n + "." * (width - n) + "]"


def run_live(src, definition, log_path, capture):
    writer, logf = None, None
    names = [p["name"] for p in definition["parameters"]]
    if log_path:
        logf = open(log_path, "w", newline="", encoding="utf-8")
        writer = csv.writer(logf)
        writer.writerow(["time", "elapsed_s"] + names + ["raw_hex"])
    t0, frames, status, last = time.time(), 0, "", None
    try:
        for line in src.lines():
            if capture:
                capture.write(line if line.endswith("\n") else line + "\n")
            msg = parse_line(line)
            if not msg:
                continue
            kind, payload = msg
            if kind in ("S", "I"):
                status = payload
                continue
            if kind != "D":
                continue
            frames += 1
            elapsed = time.time() - t0
            rows = decode(payload, definition)
            if writer:
                writer.writerow([datetime.now().isoformat(timespec="milliseconds"),
                                 f"{elapsed:.2f}"]
                                + ["" if v is None else f"{v:.3f}" for _, v, _, _ in rows]
                                + [payload.hex().upper()])
                logf.flush()
            clear()
            print(f"{BOLD}{definition.get('name', 'ALDL')}{RESET}")
            print(f"{DIM}frames: {frames}   elapsed: {elapsed:6.1f}s   "
                  f"frame length: {len(payload)} bytes   {status}{RESET}\n")
            for name, value, units, p in rows:
                rng = p.get("range")
                b = bar(value, rng[0], rng[1]) if rng else ""
                print(f"  {name:<24}{fmt_value(value, p):>10} {units:<6} {b}")
            print()
            print(f"{DIM}raw:{RESET}")
            print("  " + hex_row(payload, last))
            print(f"\n{DIM}Ctrl+C to stop" + (f"   logging to {log_path}" if log_path else "") + RESET)
            last = payload
    finally:
        if logf:
            logf.close()


def hex_row(frame, prev):
    cells = []
    for i, b in enumerate(frame):
        changed = prev is not None and i < len(prev) and prev[i] != b
        cells.append(f"{YELLOW if changed else ''}{b:02X}{RESET if changed else ''}")
    return " ".join(cells)


def run_raw(src, capture):
    """Show raw frames with changed bytes highlighted, plus per-byte stats."""
    stats, last, n = {}, None, 0
    print(f"{DIM}byte #  " + " ".join(f"{i:02d}" for i in range(32)) + RESET)
    try:
        for line in src.lines():
            if capture:
                capture.write(line if line.endswith("\n") else line + "\n")
            msg = parse_line(line)
            if not msg:
                continue
            kind, payload = msg
            if kind in ("S", "I"):
                print(f"{DIM}{kind}: {payload}{RESET}")
                continue
            if kind != "D":
                continue
            n += 1
            print(f"{n:6d}  " + hex_row(payload, last))
            for i, b in enumerate(payload):
                s = stats.setdefault(i, [b, b, 0, b])  # min, max, changes, last
                s[0], s[1] = min(s[0], b), max(s[1], b)
                if b != s[3]:
                    s[2] += 1
                s[3] = b
            last = payload
    except KeyboardInterrupt:
        pass
    print(f"\n{BOLD}Per-byte summary over {n} frames{RESET}")
    print("  byte   min   max  changes")
    for i in sorted(stats):
        mn, mx, ch, _ = stats[i]
        flag = "  <- moving" if ch > 0 else ""
        print(f"  {i:4d}  {mn:4d}  {mx:4d}  {ch:7d}{flag}")


def run_pulses(src, report_every=200):
    """Histogram of pulse widths per line level, to check wiring and polarity."""
    src.send("p")
    widths = {0: [], 1: []}
    print("Collecting pulses... (Ctrl+C to stop)")
    try:
        for line in src.lines():
            msg = parse_line(line)
            if not msg or msg[0] != "P":
                if msg and msg[0] in ("S", "I"):
                    print(f"{DIM}{msg[0]}: {msg[1]}{RESET}")
                continue
            level, us = msg[1]
            widths[level].append(us)
            if sum(len(v) for v in widths.values()) >= report_every:
                print_pulse_report(widths)
                widths = {0: [], 1: []}
    except KeyboardInterrupt:
        pass
    finally:
        src.send("d")


def print_pulse_report(widths):
    print(f"\n{BOLD}Pulse widths (microseconds){RESET}")
    buckets = [0, 250, 750, 1500, 2500, 3500, 4500, 5500, 6500, 10000, 10**9]
    for level in (0, 1):
        vals = widths[level]
        name = "LOW " if level == 0 else "HIGH"
        if not vals:
            print(f"  {name}: no pulses")
            continue
        print(f"  {name}: {len(vals)} pulses, min {min(vals)}, max {max(vals)}")
        for lo, hi in zip(buckets, buckets[1:]):
            c = sum(lo <= v < hi for v in vals)
            if c:
                label = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
                print(f"    {label:>11} us  {'#' * min(50, c)} {c}")
    print(f"{DIM}  Good signal: each level forms two tight groups, and a LOW plus the"
          f" next HIGH add up to about 6250 us.\n"
          f"  Polarity: run --mode raw. If no frames appear (or 'S,frames=' stays 0),"
          f" add --invert and try again.{RESET}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port of the Arduino, e.g. COM3, /dev/cu.usbmodem1101, "
                                   "or 'auto' to find it")
    ap.add_argument("--simulate", action="store_true", help="use a fake ECM (no hardware)")
    ap.add_argument("--replay", metavar="FILE", help="play back a --capture file")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--mode", choices=["live", "raw", "pulses"], default="live")
    ap.add_argument("--def", dest="definition", help="definition JSON (byte layout)")
    ap.add_argument("--log", metavar="CSV", help="write decoded values to a CSV file")
    ap.add_argument("--capture", metavar="TXT", help="save everything received, for --replay")
    ap.add_argument("--cable", action="store_true",
                    help="read a USB ALDL cable directly instead of the Arduino "
                         "(uses the settings saved by cable_check.py)")
    ap.add_argument("--cable-baud", type=int, help="serial speed for --cable (default 2400)")
    ap.add_argument("--invert", action="store_true", help="the signal's active level is the other way round")
    ap.add_argument("--lsb", action="store_true", help="bytes are sent least significant bit first")
    args = ap.parse_args(argv)

    if os.name == "nt":
        os.system("")  # turn on ANSI colours in the Windows console

    if args.list_ports:
        try:
            from serial.tools import list_ports
        except ImportError:
            sys.exit("pyserial is not installed. Run:  pip install pyserial")
        for p in list_ports.comports():
            print(f"{p.device:<16} {p.description}")
        return

    if args.simulate:
        src = SimSource()
        default_def = DEMO_DEF
    elif args.replay:
        src = ReplaySource(args.replay)
        default_def = DEFAULT_DEF
    elif args.cable:
        import aldl160
        settings = load_cable_settings()
        baud = args.cable_baud or settings.get("baud", aldl160.DEFAULT_CABLE_BAUD)
        invert = args.invert or settings.get("invert", False)
        port = find_port() if args.port in (None, "auto") else args.port
        print(f"Using cable on {port} at {baud} baud{' (inverted)' if invert else ''}")
        if args.mode == "pulses":
            ap.error("--mode pulses is for the Arduino; for a cable run cable_check.py")
        src = aldl160.CableSource(port, baud, invert, msb_first=not args.lsb)
        default_def = DEFAULT_DEF
    elif args.port:
        port = find_port() if args.port == "auto" else args.port
        print(f"Using serial port {port}")
        src = SerialSource(port)
        default_def = DEFAULT_DEF
    else:
        ap.error("give --port, --cable, --simulate or --replay (use --list-ports to find the port)")

    if args.invert and not args.cable:
        src.send("i")
    if args.lsb and not args.cable:
        src.send("b")

    definition = load_definition(args.definition or default_def)
    capture = open(args.capture, "w", encoding="ascii") if args.capture else None
    try:
        if args.mode == "pulses":
            run_pulses(src)
        elif args.mode == "raw":
            run_raw(src, capture)
        else:
            run_live(src, definition, args.log, capture)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if capture:
            capture.close()
        src.close()


if __name__ == "__main__":
    main()
