# Fiero ALDL Reader

A DIY live-data reader for the **1988 Pontiac Fiero 2.8 V6** (GM OBD1, 160 baud ALDL
stream, ECM 1227170). It reads the ECM through either a **ready-made USB ALDL cable** or a
**DIY Arduino interface**, and shows a live dashboard and logs to CSV. It runs on a Mac
(also Windows and Linux). It's built to diagnose a
**hunting/stalling idle**: watch RPM, IAC position, MAP, O2 and fuel trims as the idle drops.

> **Status:** the Fiero V6 byte layout and signal timing come from GM's own data stream
> specification (A29), see [Step 5](#5-the-byte-layout-built-in). The software is tested
> against simulated signals built to that spec; it has not been run on a real car yet.

```
 Fiero ALDL ──► USB ALDL cable ──────────────────► Mac: aldl_logger.py --cable ──► dashboard + CSV
 Fiero ALDL ──► Arduino (decodes 160 baud) ──USB─► Mac: aldl_logger.py ─────────► dashboard + CSV
```

## The Mac app

**Fiero ALDL.app** puts everything in one window:

- **Connect to:** USB ALDL cable, the Arduino interface, or a demo with a fake hunting idle
- **Check cable…** finds the settings your cable needs (key ON) and saves them
- **Dashboard:** big live readouts for RPM, IAC, MAP, O2, trims, coolant, spark, EGR and more,
  plus the ECM's **trouble codes** and status (closed/open loop, Park/Neutral, A/C), all
  decoded from GM's Fiero V6 data stream spec
- **Chart:** the last 60 seconds of any three values, stacked, for watching the idle hunt
- **Analysis:** flags readings out of the normal range in plain language (idle hunting,
  RPM near stall, IAC shut or maxed out, TPS off, lean/rich O2, fuel trims, open loop when
  warm, A/C load, charging voltage, a dead coolant sensor, ECM trouble codes, diagnostic
  mode left on) with what usually causes each one. Works live
  (last 30 s) or on a saved log. Limits are typical GM ranges of the era, not factory Fiero
  specs, so treat findings as where to look, not a diagnosis.
- **Ask Claude:** sends a summary of your readings (live, or a saved log) to Claude and
  shows its diagnosis, with follow-up questions in the same conversation (see below)
- **Raw bytes:** every byte the ECM sends, with what each one is; you can reassign bytes
  if something ever looks wrong
- **Record:** saves a CSV log plus a raw capture you can send to Claude

Your logs, byte layout (`my_layout.json`) and cable settings are kept in
**Documents → Fiero ALDL**.

### Ask Claude (optional)

The **Ask Claude** tab sends Claude (model `claude-opus-5-5`) a compact summary of the data:
min/median/max of each value, a time series (up to 240 rows), trouble codes, ECM status flags
and the Analysis findings. The answer streams in; ask follow-ups below it.

- **Needs your own Anthropic API key**: create one at console.anthropic.com → API keys, then
  click **API key…** in the tab. The key is stored in the **macOS Keychain**, not in a file.
  (An `ANTHROPIC_API_KEY` environment variable also works.)
- **Billed to that key**, separately from any Claude subscription. Each answer shows its rough
  cost; a question about a minute of data is usually a few cents.
- **Nothing is sent** until you click a button, and only engine data is sent.
- Claude can be wrong. Treat its answer like advice from a knowledgeable friend: verify before
  buying parts.

### Download and first open

1. Open the **[Releases page](https://github.com/jaredv24/fiero-aldl/releases/latest)** and
   download the zip for your Mac: **apple-silicon** for M1 and newer, **intel** for older
   Macs ( → About This Mac shows which you have).
2. Double-click the zip, then drag **Fiero ALDL** into **Applications**.
3. The app isn't signed by Apple, so the first time macOS blocks it. Double-click it, click
   **Done** on the warning, then open **System Settings → Privacy & Security**, scroll down
   and click **Open Anyway** next to "Fiero ALDL" (on older macOS: right-click the app →
   **Open** → **Open**). After that it opens normally.

To run it from source instead: `python fiero_app.py` in the `python` folder
(needs `pip install -r requirements.txt`).

## Folder layout

| Path | What it is |
|---|---|
| `arduino/aldl160/` | Arduino sketch (`aldl160.ino`) and the decoder (`aldl_decoder.h`) |
| `arduino/test/` | PC test for the decoder |
| `python/fiero_app.py` | The desktop app (window, dashboard, chart, raw bytes, recording) |
| `.github/workflows/build-mac-app.yml` | Builds `Fiero ALDL.app` on GitHub's Macs |
| `python/analysis.py` | The checks behind the Analysis tab |
| `python/claude_assistant.py` | The Ask Claude tab's data summary and Claude API calls |
| `python/aldl_logger.py` | Command-line dashboard, logger, raw-byte viewer, signal checker |
| `python/aldl160.py` | 160 baud decoding for USB cables (`--cable`) |
| `python/cable_check.py` | Tests a USB cable and finds the settings that work |
| `run_mac.command` | Double-click menu for the Mac |
| `python/definitions/1227170_fiero_v6.json` | Fiero 2.8 V6 byte layout from GM data stream A29 (also used by the demo) |
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
live data, check the readings, and analyse your idle logs. The Claude phone app and
claude.ai in a browser can't reach USB devices, but you can upload the CSV/TXT files from
`logs` there for analysis.

## Using a USB ALDL cable (no Arduino)

Buy a cable that says it supports **160 baud on pin E** (e.g. Red Devil River's GM 12-pin
ALDL to USB cable). Many "ALDL USB" cables only do 8192 baud and won't read a Fiero.

1. Plug the cable into the ALDL socket and the Mac. If it has a 10k switch, turn it **on**
   (GM's spec has the Fiero send data in 10K mode). Key **ON** (engine off is fine).
2. Double-click `run_mac.command` → **2) Check the cable**. It listens at a few speeds,
   tries both signal polarities, and reports **SUCCESS** with the settings it saved, or what to
   try next. It should find 25-byte frames.
3. Then use **4) Live dashboard + record a log** to diagnose the idle (or **3) Raw bytes**
   to see every byte).

From a terminal it's the same:

```bash
python cable_check.py
```

```bash
python aldl_logger.py --cable --mode raw
```

**How it works and its limits:** the cable is read at 4800 baud, so every ECM pulse arrives
as one serial byte whose zero bits show how long the pulse was (4800 rather than 2400 so
GM's shortest 0.2 ms pulses aren't missed). This was tested with a
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

## 5. The byte layout (built in)

The Fiero V6's data layout comes from GM's own **Data Stream A29** specification, which covers
the **2.8L PFI (L44, VIN 9), 1985–88**, ECM 1227170: data on **pin E**, **160 baud**,
**25 bytes** per frame, sent in **10K mode** (10 kΩ across ALDL A–B) or shorted mode. It's in
`python/definitions/1227170_fiero_v6.json` and decodes:

| GM byte | Value | Conversion (GM) |
|---|---|---|
| 4 | IAC position | steps |
| 5 | Coolant temp | °C = N × 0.75 − 40 (shown in °F) |
| 6 | Vehicle speed | mph = N |
| 7 | MAP | volts = N × 0.0196 (GM gives volts, not kPa) |
| 8 | Engine RPM | N × 25 |
| 9 | TPS | volts = N × 0.0196 |
| 10 | Integrator (short-term trim) | 128 = no correction |
| 11 | O2 sensor | mV = N × 4.44 |
| 12–14 | **Trouble codes** 12–55 | one bit per code |
| 15–17 | Status: closed loop, rich/lean, Park/Neutral, A/C, power steering… | bits |
| 18 | Battery | volts = N × 0.1 |
| 19 | Block learn multiplier (long-term trim) | 128 = no correction |
| 20 | O2 cross counter | rolls over 0–255 |
| 21 | Spark advance | degrees = N × 90/256 |
| 22 | EGR duty cycle | % = N / 2.56 |
| 23 | Manifold air temp | GM lookup table (shown in °F) |
| 24–25 | Base pulse width | raw (units not given) |
| 1, 2–3 | Mode word, PROM ID | |

Source: GM's A29 spec, as published in the
[Arduino-ALDL-160-baud project's data stream files](https://github.com/rchipka/Arduino-ALDL-160-baud/tree/master/datastreams)
(`A029.DS`). The app's **Check cable…** confirms your car sends 25-byte frames.

The decoder follows GM's 160 baud timing from the same source (`Aldl.ds`): active-low pulses,
a 0 bit low for 0.2–1.4 ms and a 1 bit low for 2.4–5.7 ms (decided at 1.9 ms), highest bit
first, sync = nine 1s then a 0.

If a value ever looks wrong on your car, the **Raw bytes** tab still lets you reassign bytes;
changes are saved to `Documents/Fiero ALDL/my_layout.json`.

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
| MAP high or jumpy at idle (the Fiero reports MAP in volts) | Vacuum leak, EGR, or MAP sensor/hose |
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
cd python && python -m unittest test_aldl_logger test_cable test_analysis test_claude_assistant -v
```

```bash
cd arduino/test && g++ -std=c++11 -I../aldl160 test_decoder.cpp -o test_decoder && ./test_decoder
```
