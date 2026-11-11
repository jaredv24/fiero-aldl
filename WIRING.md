# Wiring the ALDL interface

## Parts (about $15–25)

| Part | Notes |
|---|---|
| Arduino **Uno** or **Nano** (5 V version) | A 3.3 V board needs a different input circuit, so stick to 5 V. |
| USB cable for the Arduino | Also powers it. No car power needed. |
| 10 kΩ resistor, ×2 | One protects the input, one is for "ALDL mode" if needed. |
| 5.1 V zener diode (e.g. 1N4733A) | Clamps spikes on the data line. |
| Jumper wires + a few male pin terminals | Pins push into the ALDL socket. A proper 12-pin ALDL plug is neater. |
| Small toggle switch (optional) | Switches the A–B 10 kΩ resistor in and out. |
| Multimeter | For the safety checks below. |

## The ALDL connector

The 12-pin ALDL socket is under the dash. Its pins are lettered **A–M** (no I), and the
letters are usually molded into the face of the connector. **Go by those letters**, not by a
diagram, since how you look at it (from the front or the wire side) flips it.

You only need three pins:

| ALDL pin | What it is | Connects to |
|---|---|---|
| **A** | Ground | Arduino **GND** |
| **E** | Serial data from the ECM | 10 kΩ resistor → Arduino **D2** |
| **B** | Diagnostic enable | Only via the optional 10 kΩ resistor to **A** (see below) |

**Do not connect anything else.** Some ALDL pins carry 12 V or control the fuel pump.

## Circuit

```
 ALDL E  o────[ 10kΩ ]────┬──────────── Arduino D2
                          │
                         ─┴─  5.1V zener
                         /▲\  (stripe/cathode toward D2)
                          │
 ALDL A  o────────────────┴──────────── Arduino GND

 Optional "ALDL mode":
 ALDL B  o────[ 10kΩ ]────o switch o─── ALDL A
```

## Safety checks before connecting the Arduino

1. Key **off**. Probe ALDL **A** to car body ground: should read ~0 Ω.
2. Key **on**, engine off. Measure DC volts from **A** (black lead) to **E** (red lead).
   You should see somewhere between 0 and 5 V, possibly flickering.
   **If you see more than about 6 V, stop.** You're on the wrong pin, or this ECM's
   line is different, and it would damage the Arduino.
3. Run the laptop on **battery** while connected, not from an inverter or 12 V charger.
   This avoids ground-loop problems.

## Getting the data stream

1. Leave B disconnected at first. Many 160 baud ECMs send data on pin E on their own.
2. Run `python aldl_logger.py --port <PORT> --mode pulses` with the key on.
   - **No pulses at all:** try typing `u` (internal pull-up) in the Arduino Serial Monitor,
     or run with the switch closed (10 kΩ between A and B).
   - **Pulses but messy/random widths:** check the ground (A) and the D2 connection.
3. If you need the 10 kΩ A–B resistor to get data, note that diagnostic modes **may change
   how the ECM runs the engine** on some GM computers (e.g. fixed timing or idle behaviour).
   Compare the idle with the switch on and off, so you don't chase a symptom that the
   diagnostic mode itself causes.

**Never** short A to B directly with the engine running unless you mean to. That's
"field service mode," which changes engine behaviour. Shorting A to B with the key on and
the engine **off** is the normal way to flash trouble codes.
