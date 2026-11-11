#!/usr/bin/env python3
"""Check a USB ALDL cable and find the settings that read your ECM.

Plug the cable into the car and the Mac, turn the key ON (engine off is fine),
then run:
    python cable_check.py                 # finds the cable's port itself
    python cable_check.py --port /dev/cu.usbserial-1234

It listens at a few serial speeds, tries both signal polarities, and tells you
which combination gives clean ALDL frames. The working settings are saved to
cable_settings.json, which aldl_logger.py --cable uses automatically.
Everything received is saved to a capture file you can send for help:
    python cable_check.py --file ../logs/cable-check-....txt   # re-analyse
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import aldl160

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "cable_settings.json")
LOGS = os.path.join(HERE, "..", "logs")
BAUDS = [4800, 2400, 1200]
FIERO_FRAME_LEN = 25   # GM data stream A29 (Fiero 2.8 V6, ECM 1227170)


def length_note(length):
    if length == FIERO_FRAME_LEN:
        return "That's the 25-byte frame GM specifies for the Fiero 2.8 V6 (A29). "
    return (f"Note: the Fiero 2.8 V6 should send {FIERO_FRAME_LEN}-byte frames, not {length}. "
            "If this is a Fiero V6, values may be wrong; try the other signal polarity. ")


def record(port, bauds, seconds):
    """Listen at each baud. Returns {baud: [chunk bytes or None for a gap]}."""
    import serial
    result = {}
    for baud in bauds:
        print(f"  listening at {baud} baud for {seconds} s...", flush=True)
        chunks, got = [], 0
        with serial.Serial(port, baud, timeout=0.2) as ser:
            ser.reset_input_buffer()
            end = time.time() + seconds
            while time.time() < end:
                data = ser.read(256)
                if data:
                    chunks.append(bytes(data))
                    got += len(data)
                else:
                    chunks.append(None)
        print(f"    received {got} bytes")
        result[baud] = chunks
    return result


def save_capture(path, captures):
    with open(path, "w", encoding="ascii") as f:
        f.write("# ALDL cable capture\n")
        for baud, chunks in captures.items():
            f.write(f"B,{baud}\n")
            for c in chunks:
                f.write("G\n" if c is None else f"R,{c.hex().upper()}\n")


def load_capture(path):
    captures, baud = {}, None
    with open(path, "r", encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if line.startswith("B,"):
                baud = int(line[2:])
                captures[baud] = []
            elif line == "G" and baud:
                captures[baud].append(None)
            elif line.startswith("R,") and baud:
                captures[baud].append(bytes.fromhex(line[2:]))
    return captures


def analyse(captures):
    """Try every baud/polarity. Returns the best result dict, or None."""
    results = []
    for baud, chunks in captures.items():
        data = b"".join(c for c in chunks if c)
        if data:
            common = {}
            for b in data:
                common[b] = common.get(b, 0) + 1
            top = sorted(common.items(), key=lambda kv: -kv[1])[:6]
            print(f"\n{baud} baud: {len(data)} bytes, most common values: "
                  + ", ".join(f"{b:02X}x{n}" for b, n in top))
        else:
            print(f"\n{baud} baud: nothing received")
            continue
        for invert in (False, True):
            frames = aldl160.decode_capture(chunks, baud, invert)
            length, share = aldl160.frame_quality(frames)
            label = "inverted" if invert else "normal  "
            print(f"   {label}: {len(frames):3d} frames"
                  + (f", {share:4.0%} are {length} bytes long" if frames else ""))
            results.append({"baud": baud, "invert": invert, "frames": len(frames),
                            "length": length, "share": share})
    good = [r for r in results if r["frames"] >= 3 and r["share"] >= 0.8 and r["length"] >= 4]
    if not good:
        return None
    return max(good, key=lambda r: (r["share"], r["frames"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="auto", help="cable's serial port (default: find it)")
    ap.add_argument("--seconds", type=int, default=8, help="listening time per speed")
    ap.add_argument("--file", help="re-analyse a saved capture instead of listening")
    args = ap.parse_args(argv)

    if args.file:
        captures = load_capture(args.file)
    else:
        try:
            import serial  # noqa: F401
        except ImportError:
            sys.exit("pyserial is not installed. Run:  pip install pyserial")
        from aldl_logger import find_port
        port = find_port() if args.port == "auto" else args.port
        print(f"Cable on {port}. Key must be ON.")
        captures = record(port, BAUDS, args.seconds)
        os.makedirs(LOGS, exist_ok=True)
        path = os.path.abspath(os.path.join(
            LOGS, f"cable-check-{datetime.now():%Y%m%d-%H%M%S}.txt"))
        save_capture(path, captures)
        print(f"\nSaved everything received to:\n  {path}")

    best = analyse(captures)
    print()
    if best:
        settings = {"baud": best["baud"], "invert": best["invert"]}
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        print(f"SUCCESS: clean {best['length']}-byte frames at {best['baud']} baud"
              f"{', inverted' if best['invert'] else ''}.")
        print(length_note(best["length"]))
        if best["invert"]:
            print("Note: inverted cable support is experimental. If values look wrong or")
            print("frames drop out, send the capture file above to Claude.")
        print("Saved these settings; now run:  python aldl_logger.py --cable --mode raw")
        return 0

    print("No clean frames found.")
    if not any(any(c for c in ch) for ch in captures.values()):
        print(" - Nothing was received at all. Is the key ON? If the cable has a switch for")
        print("   the 10k resistor (\"ALDL mode\"), flip it and run this again.")
    else:
        print(" - Data arrived but didn't decode. Flip the cable's 10k switch (if it has")
        print("   one) and run again. If that doesn't help, send the capture file above")
        print("   to Claude so the cable mode can be adapted to this cable.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
