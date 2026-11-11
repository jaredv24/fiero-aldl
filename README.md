# Fiero ALDL Reader

A DIY live-data reader for the **1988 Pontiac Fiero 2.8 V6** (GM OBD1, 160 baud ALDL
stream, ECM 1227170). It reads the ECM through either a **ready-made USB ALDL cable** or a
**DIY Arduino interface**, and shows a live dashboard and logs to CSV. It runs on a Mac
(also Windows and Linux). It's built to diagnose a
**hunting/stalling idle**: watch RPM, IAC position, MAP, O2 and fuel trims as the idle drops.

> **Status:** the decoder and software are tested against simulated signals only. They have
> not been run on a real car yet. The ECM's byte layout is **not filled in**; see
> [Step 5](#5-fill-in-the-byte-layout).

```
 Fiero ALDL ──► USB ALDL cable ──────────────────► Mac: aldl_logger.py --cable ──► dashboard + CSV
 Fiero ALDL ──► Arduino (decodes 160 baud) ──USB─► Mac: aldl_logger.py ─────────► dashboard + CSV
```

## The Mac app

**Fiero ALDL.app** puts everything in one window:

- **Connect to:** USB ALDL cable, the Arduino interface, or a demo with a fake hunting idle
- **Check cable…** finds the settings your cable needs (key ON) and saves them
- **Dashboard:** big live readouts for RPM, IAC, MAP, O2, trims, coolant and more
- **Chart:** the last 60 seconds of any three values, stacked, for watching the idle hunt
- **Analysis:** flags readings out of the normal range in plain language (idle hunting,
  RPM near stall, IAC shut or maxed out, low vacuum, TPS off, lean/rich O2, fuel trims,
  charging voltage, a dead coolant sensor) with what usually causes each one. Works live
  (last 30 s) or on a saved log. Limits are typical GM ranges of the era, not factory Fiero
  specs, so treat findings as where to look, not a diagnosis.
- **Raw bytes:** every byte the ECM sends; select one, pick what it is (RPM, TPS, …) and
  click **Assign** to build the byte layout
- **Record:** saves a CSV log plus a raw capture you can send to Claude

Your logs, byte layout (`my_layout.json`) and cable settings are kept in
**Documents → Fiero ALDL**.

### Download and first open

1. Open the repo's **Releases** page (or the latest **Actions → Build Mac app** run) and
   download the zip for your Mac: **apple-silicon** for M1 and newer, **intel** for older
   Macs ( → About This Mac shows which you have).
2. Double-click the zip, then drag **Fiero ALDL** into **Applications**.
3. The app isn't signed by Apple, so the first time macOS blocks it. Double-click it, click
   **Done** on the warning, then open **System Settings → Privacy & Security**, scroll down
   and click **Open Anyway** next to "Fiero ALDL" (on older macOS: right-click the app →
   **Open** → **Open**). After that it opens normally.

To run it from source instead: `python fiero_app.py` in the `python` folder
(needs `pip install pyserial`).

## Folder layout

| Path | What it is |
|---|---|
| `arduino/aldl160/` | Arduino sketch (`aldl160.ino`) and the decoder (`aldl_decoder.h`) |
| `arduino/test/` | PC test for the decoder |
| `python/fiero_app.py` | The desktop app (window, dashboard, chart, raw bytes, recording) |
| `.github/workflows/build-mac-app.yml` | Builds `Fiero ALDL.app` on GitHub's Macs |
| `python/analysis.py` | The checks behind the Analysis tab |
| `python/aldl_logger.py` | Command-line dashboard, logger, raw-byte viewer, signal checker |
| `python/aldl160.py` | 160 baud decoding for USB cables (`--cable`) |
| `python/cable_check.py` | Tests a USB cable and finds the settings that work |
| `run_mac.command` | Double-click menu for the Mac |
| `python/definitions/` | Byte layouts: `1227170_template.json` (car), `demo.json` (simulator) |
| `WIRING.md` | Parts list, circuit, and safety checks |

## On a Mac without the app (command line)

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

## Using a USB ALDL cable (no Arduino)

Buy a cable that says it supports **160 baud on pin E** (e.g. Red Devil River's GM 12-pin
ALDL to USB cable). Many "ALDL USB" cables only do 8192 baud and won't read a Fiero.

1. Plug the cable into the ALDL socket and the Mac. Key **ON** (engine off is fine).
2. Double-click `run_mac.command` → **2) Check the cable**. It listens at a few speeds,
   tries both signal polarities, and reports **SUCCESS** with the settings it saved, or what to
   try next. If the cable has a switch for the 10k resistor, try it in both positions.
3. Then use **3) Raw bytes** to see the data and fill in the byte layout (Step 5 below), and
   **4) Live dashboard + record a log** to diagnose the idle.

From a terminal it's the same:

```bash
python cable_check.py
```

```bash
python aldl_logger.py --cable --mode raw
```

**How it works and its limits:** the cable is read at 2400 baud, so every ECM pulse arrives
as one serial byte whose zero bits show how long the pulse was. This was tested with a
simulated serial chip, not a real cable yet. Cables that pass the signal straight through
(the usual design) work in simulation. Cables that invert the signal are **experimental**:
short pulses can be missed. Either way, the check saves everything it received in `logs/`.
If it doesn't report SUCCESS, send that file to Claude to adapt the decoder to your cable.

## 1. Try the software with no car

Install [Python 3](https://www.python.org/downloads/), then in the `python` folder:

```bash
pip install -r requirements.txt
```

```bash
python aldl_logger.py --simulate
```

You'll see a fake hunting idle: RPM falling and the IAC opening to catch it. Ctrl+C stops.

## 2. Build the interface (Arduino route only)

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
cd python && python -m unittest test_aldl_logger test_cable test_analysis -v
```

```bash
cd arduino/test && g++ -std=c++11 -I../aldl160 test_decoder.cpp -o test_decoder && ./test_decoder
```
