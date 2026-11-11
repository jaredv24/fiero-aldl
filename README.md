# Fiero ALDL Reader

A DIY live-data reader for the **1988 Pontiac Fiero 2.8 V6** (GM OBD1, 160 baud ALDL
stream, ECM 1227170). An Arduino decodes the ECM's data line and sends it over USB to a
Python program that shows a live dashboard and logs to CSV. It's built to diagnose a
**hunting/stalling idle**: watch RPM, IAC position, MAP, O2 and fuel trims as the idle drops.

> **Status:** the decoder and software are tested against simulated signals only. They have
> not been run on a real car yet. The ECM's byte layout is **not filled in**; see
> [Step 5](#5-fill-in-the-byte-layout).

```
 Fiero ALDL ──► Arduino (decodes 160 baud) ──USB──► laptop: aldl_logger.py ──► dashboard + CSV
```

## Folder layout

| Path | What it is |
|---|---|
| `arduino/aldl160/` | Arduino sketch (`aldl160.ino`) and the decoder (`aldl_decoder.h`) |
| `arduino/test/` | PC test for the decoder |
| `python/aldl_logger.py` | Dashboard, logger, raw-byte viewer, signal checker |
| `python/definitions/` | Byte layouts: `1227170_template.json` (car), `demo.json` (simulator) |
| `WIRING.md` | Parts list, circuit, and safety checks |

## On a Mac (easiest)

Double-click **`run_mac.command`** and pick from the menu. The first run sets itself up
(needs internet), and it finds the Arduino's port automatically. Logs go in the `logs` folder.

- If macOS says it can't open the file, right-click it → **Open** → **Open**.
- Python 3: if it isn't installed, macOS offers to install it the first time, or get it
  from [python.org](https://www.python.org/downloads/).
- Official Arduino boards need no driver. Some cheap clone boards use a CH340 USB chip that
  may need a driver from the chip maker.
- Ports on a Mac look like `/dev/cu.usbmodem1101` or `/dev/cu.usbserial-110`. Use the
  `/dev/cu.` one, not `/dev/tty.`. `--port auto` does this for you.

### Using it with Claude

Install the **Claude desktop app** and use **Claude Code** in it, opened on this `fiero-aldl`
folder, with the Arduino plugged into the Mac. Claude can then run the logger itself, read the
live data, help fill in the byte layout, and analyse your idle logs. The Claude phone app and
claude.ai in a browser can't reach USB devices, but you can upload the CSV/TXT files from
`logs` there for analysis.

## 1. Try the software with no car

Install [Python 3](https://www.python.org/downloads/), then in the `python` folder:

```bash
pip install -r requirements.txt
```

```bash
python aldl_logger.py --simulate
```

You'll see a fake hunting idle: RPM falling and the IAC opening to catch it. Ctrl+C stops.

## 2. Build the interface

Follow **[WIRING.md](WIRING.md)**. It's an Arduino, two resistors and a zener diode.
Do the multimeter safety checks first.

## 3. Load the Arduino sketch

1. Install the [Arduino IDE](https://www.arduino.cc/en/software).
2. Open `arduino/aldl160/aldl160.ino` (keep `aldl_decoder.h` in the same folder).
3. Pick your board and port, then **Upload**.
4. Close the IDE's Serial Monitor before running the Python program (only one can use the port).

Find the port:

```bash
python aldl_logger.py --list-ports
```

## 4. Check the signal (key on)

```bash
python aldl_logger.py --port auto --mode pulses
```

`--port auto` finds the Arduino. If it picks wrong, give the port instead (e.g. `COM3` on Windows, `/dev/cu.usbmodem1101` on a Mac).
You want each line level to form two tight groups, with a low plus the following high
adding up to about 6250 µs (one bit at 160 baud). See WIRING.md if you see nothing.

Then look at raw frames:

```bash
python aldl_logger.py --port auto --mode raw --capture first_try.txt
```

If no frames show up, try again with `--invert` (the signal's active level is the other
way round), and then with `--lsb`. When it's right, frames arrive about once a second and are
**the same length every time**.

## 5. Fill in the byte layout

The ECM sends a frame of numbers, but which byte is RPM, which is coolant and so on depends
on the ECM's program. Those positions are left blank (`null`) in
`python/definitions/1227170_template.json` because I couldn't verify them, and a wrong
layout would show convincing but wrong numbers.

Two ways to fill them in:

- **From a published definition (best):** look for the **1227170** 160 baud ALDL data stream
  definition on Fiero and GM EFI forums, or the definition files used by TunerPro. Copy each
  item's byte number into `offset` (0 = first byte after sync) and check the conversion
  (`value = raw * scale + add`).
- **By experiment:** run `--mode raw` and watch which bytes change, highlighted in yellow:
  - rev the engine → the RPM byte jumps
  - press the throttle with the engine off → the TPS byte moves
  - as the engine warms → coolant moves steadily
  - the O2 byte swings rapidly once warm
  
  Ctrl+C prints a per-byte min/max summary.

Note that the published definition is still needed to get accurate units.

## 6. Diagnose the idle

```bash
python aldl_logger.py --port auto --log idle_cold.csv --capture idle_cold.txt
```

Let it hunt and stall a few times, then do the same warm. Open the CSV in Excel or Google
Sheets and chart RPM, IAC, MAP and O2 against `elapsed_s`. What to look for:

| What you see as RPM drops | Likely meaning |
|---|---|
| IAC very low or zero, RPM still too high/unstable | Extra air: **vacuum leak** or **EGR stuck open** |
| IAC climbing to its max and RPM still falling | Not enough air or fire: dirty throttle body/IAC, fuel, ignition |
| O2 stuck low (<300 mV), trims high | Lean → vacuum leak, low fuel pressure |
| O2 stuck high (>700 mV), trims low | Rich → leaking injector, bad coolant/MAP reading |
| MAP high or jumpy at idle | Vacuum leak, EGR, or MAP sensor/hose |
| TPS not steady and low at closed throttle | TPS fault or misadjustment |
| Coolant reading wrong for the actual temp | Bad coolant sensor |

A `--capture` file can be played back later, or shared:

```bash
python aldl_logger.py --replay idle_cold.txt
```

## Arduino serial commands

You can type these in the Arduino Serial Monitor at 115200 baud:
`p` = pulse mode, `d` = data mode, `i` = invert active level, `b` = swap bit order,
`u` = toggle the internal pull-up.

## Tests

```bash
cd python && python -m unittest test_aldl_logger -v
```

```bash
cd arduino/test && g++ -std=c++11 -I../aldl160 test_decoder.cpp -o test_decoder && ./test_decoder
```
