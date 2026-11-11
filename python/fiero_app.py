#!/usr/bin/env python3
"""Fiero ALDL - desktop app (Mac first, also runs on Windows/Linux).

A window around the same tested code the command-line tools use:
  - connect to a USB ALDL cable, the Arduino interface, or a demo
  - check a USB cable and save the settings that work
  - live dashboard, live chart (made for watching a hunting idle)
  - raw bytes view where you assign bytes to RPM, IAC, ... (the byte layout)
  - record CSV logs + raw captures to Documents/Fiero ALDL/logs

Run from source:  python fiero_app.py
"""

import contextlib
import csv
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import aldl160
import aldl_logger as al
import analysis
import claude_assistant
import cable_check

APP_NAME = "Fiero ALDL"
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("FIERO_ALDL_DATA") or os.path.join(
    os.path.expanduser("~"), "Documents", APP_NAME)
LOG_DIR = os.path.join(DATA_DIR, "logs")
LAYOUT_PATH = os.path.join(DATA_DIR, "my_layout.json")
CABLE_SETTINGS = os.path.join(DATA_DIR, "cable_settings.json")
# GM data stream A29 (Fiero 2.8 V6, ECM 1227170). The demo uses the same layout.
TEMPLATE_DEF = os.path.join(RES_DIR, "definitions", "1227170_fiero_v6.json")
DEMO_DEF = TEMPLATE_DEF

SRC_DEMO, SRC_CABLE, SRC_ARDUINO = "Demo (no car)", "USB ALDL cable", "Arduino interface"
AUTO_PORT = "Automatic"
CHART_SECONDS = 60
CHART_COLORS = ("#2a6fdb", "#d1495b", "#2e9e5b")


def setup_data_dir():
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(LAYOUT_PATH) or _is_blank_layout(LAYOUT_PATH):
        shutil.copyfile(TEMPLATE_DEF, LAYOUT_PATH)
    cable_check.SETTINGS = CABLE_SETTINGS
    cable_check.LOGS = LOG_DIR


def _is_blank_layout(path):
    """True for the old blank template (nothing mapped): safe to replace."""
    try:
        with open(path, encoding="utf-8") as f:
            params = json.load(f).get("parameters", [])
    except (OSError, ValueError):
        return False
    return bool(params) and all(p.get("offset") is None for p in params)


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports() if not p.device.startswith("/dev/tty.")]


def open_folder(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # noqa: S606 (Windows only)
    else:
        subprocess.Popen(["xdg-open", path])


class QueueWriter:
    """File-like object that forwards print() output to the GUI log."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))

    def flush(self):
        pass


class Reader(threading.Thread):
    """Runs a data source in the background and passes its lines to the GUI."""

    def __init__(self, make_source, q):
        super().__init__(daemon=True)
        self.make_source, self.q = make_source, q
        self.stop_evt = threading.Event()
        self.src = None

    def run(self):
        try:
            self.src = self.make_source()
            if not self.stop_evt.is_set():
                self.q.put(("connected", None))
                for line in self.src.lines():
                    if self.stop_evt.is_set():
                        break
                    self.q.put(("line", line))
        except SystemExit as e:
            self.q.put(("error", str(e)))
        except Exception as e:  # serial errors, unplugged cable, ...
            if not self.stop_evt.is_set():
                self.q.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            self._close()
            self.q.put(("stopped", None))

    def _close(self):
        try:
            if self.src:
                self.src.close()
        except Exception:
            pass

    def stop(self):
        self.stop_evt.set()
        self._close()  # unblocks a waiting serial read


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.reader = None
        self.checking = False
        self.recording = None  # (csv_file, writer, names, capture_file, csv_path)
        self.definition = al.load_definition(DEMO_DEF)
        self.frames = 0
        self.last_frame = None
        self.byte_stats = {}
        self.history = deque()
        self.t0 = time.time()
        self.tiles = {}

        root.title(APP_NAME)
        root.geometry("1060x720")
        root.minsize(820, 560)
        self._build_toolbar()
        self._build_tabs()
        self._build_statusbar()
        self.refresh_ports()
        self.on_source_change()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        root.after(300, self.redraw_chart)
        root.after(1000, self.refresh_analysis)

    # ------------------------------------------------------------------ UI

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="Connect to:").pack(side="left")
        self.source_var = tk.StringVar(value=SRC_DEMO)
        src = ttk.Combobox(bar, textvariable=self.source_var, state="readonly", width=17,
                           values=[SRC_DEMO, SRC_CABLE, SRC_ARDUINO])
        src.pack(side="left", padx=(4, 10))
        src.bind("<<ComboboxSelected>>", lambda e: self.on_source_change())

        ttk.Label(bar, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=AUTO_PORT)
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, state="readonly", width=24)
        self.port_box.pack(side="left", padx=(4, 2))
        self.refresh_btn = ttk.Button(bar, text="↻", width=3, command=self.refresh_ports)
        self.refresh_btn.pack(side="left", padx=(0, 10))

        self.connect_btn = ttk.Button(bar, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=2)
        self.check_btn = ttk.Button(bar, text="Check cable…", command=self.check_cable)
        self.check_btn.pack(side="left", padx=2)
        self.record_btn = ttk.Button(bar, text="● Record", command=self.toggle_record)
        self.record_btn.pack(side="left", padx=2)
        ttk.Button(bar, text="Open logs folder", command=lambda: open_folder(LOG_DIR)).pack(
            side="left", padx=2)

        adv = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        adv.pack(fill="x")
        self.hint_var = tk.StringVar()
        ttk.Label(adv, textvariable=self.hint_var, foreground="#666").pack(side="left")
        self.invert_var = tk.BooleanVar(value=False)
        self.lsb_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(adv, text="LSB first", variable=self.lsb_var).pack(side="right")
        ttk.Checkbutton(adv, text="Invert signal", variable=self.invert_var).pack(
            side="right", padx=(0, 10))
        ttk.Label(adv, text="Only if the data looks wrong:", foreground="#666").pack(
            side="right", padx=(0, 8))

    def _build_tabs(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10)

        # Dashboard
        self.dash = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.dash, text="Dashboard")
        self.banner = ttk.Label(self.dash, foreground="#a15c00", wraplength=900)
        self.banner.pack(fill="x")
        self.ecm_status = ttk.Label(self.dash, font=("Helvetica", 13), wraplength=950)
        self.ecm_status.pack(fill="x", pady=(2, 8))
        self.tile_frame = ttk.Frame(self.dash)
        self.tile_frame.pack(fill="both", expand=True)

        # Chart
        chart_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(chart_tab, text="Chart")
        pick = ttk.Frame(chart_tab)
        pick.pack(fill="x")
        ttk.Label(pick, text=f"Last {CHART_SECONDS} seconds:").pack(side="left")
        self.chart_vars = []
        for i, default in enumerate(("Engine RPM", "IAC position", "MAP")):
            v = tk.StringVar(value=default)
            cb = ttk.Combobox(pick, textvariable=v, state="readonly", width=18)
            cb.pack(side="left", padx=6)
            self.chart_vars.append((v, cb))
        self.canvas = tk.Canvas(chart_tab, background="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(8, 0))

        # Analysis
        an_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(an_tab, text="Analysis")
        top = ttk.Frame(an_tab)
        top.pack(fill="x")
        self.an_title = ttk.Label(top, font=("Helvetica", 14, "bold"))
        self.an_title.pack(side="left")
        ttk.Button(top, text="Analyze a saved log…", command=self.analyze_log_file).pack(
            side="right")
        self.live_btn = ttk.Button(top, text="Back to live", command=self.analysis_back_to_live)
        ttk.Label(an_tab, foreground="#666", wraplength=950, text=(
            "Checks readings against typical ranges for GM engines of this era (not factory "
            "Fiero specs) and explains what's usually behind anything out of the norm. It "
            "points where to look; it isn't a diagnosis. Checks run only for values that "
            "are mapped and in real units.")).pack(fill="x", pady=(4, 6))
        self.an_text = tk.Text(an_tab, wrap="word", font=("Helvetica", 13), padx=12, pady=10,
                               background="white", relief="flat", cursor="arrow")
        self.an_text.pack(fill="both", expand=True)
        for sev, color, label in ((analysis.PROBLEM, "#c0392b", "PROBLEM"),
                                  (analysis.WATCH, "#b9770e", "WATCH"),
                                  (analysis.OK, "#1e8449", "OK"),
                                  (analysis.INFO, "#5d6d7e", "INFO")):
            self.an_text.tag_configure(sev, foreground=color,
                                       font=("Helvetica", 13, "bold"))
        self.an_text.tag_configure("title", font=("Helvetica", 14, "bold"))
        self.an_text.tag_configure("detail", lmargin1=24, lmargin2=24)
        self.an_text.tag_configure("hint", lmargin1=24, lmargin2=24, foreground="#444",
                                   font=("Helvetica", 13, "italic"))
        self.an_text.configure(state="disabled")
        self.an_log = None       # (name, findings) when showing a saved log
        self.an_last = None

        self._build_claude_tab()

        # Raw bytes
        raw_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(raw_tab, text="Raw bytes")
        ttk.Label(raw_tab, wraplength=950, text=(
            "Every byte the ECM sends, highlighted when it changes. To build the byte layout: "
            "rev the engine and see which byte jumps (RPM), press the throttle with the engine "
            "off (TPS), watch coolant climb as it warms. Select that byte, pick what it is, "
            "and click Assign. The layout is saved to Documents/Fiero ALDL/my_layout.json.")
        ).pack(fill="x", pady=(0, 6))
        cols = ("byte", "hex", "dec", "min", "max", "changes", "assigned")
        self.tree = ttk.Treeview(raw_tab, columns=cols, show="headings", height=16,
                                 selectmode="browse")
        for c, w in zip(cols, (60, 60, 60, 60, 60, 80, 260)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="w" if c == "assigned" else "center")
        self.tree.tag_configure("changed", background="#fff2a8")
        self.tree.pack(fill="both", expand=True)
        assign = ttk.Frame(raw_tab)
        assign.pack(fill="x", pady=(6, 0))
        ttk.Label(assign, text="Selected byte is:").pack(side="left")
        self.assign_var = tk.StringVar()
        self.assign_box = ttk.Combobox(assign, textvariable=self.assign_var, state="readonly",
                                       width=24)
        self.assign_box.pack(side="left", padx=6)
        self.assign_btn = ttk.Button(assign, text="Assign", command=self.assign_byte)
        self.assign_btn.pack(side="left")
        self.clear_btn = ttk.Button(assign, text="Clear", command=self.clear_byte)
        self.clear_btn.pack(side="left", padx=4)
        self.assign_note = ttk.Label(assign, foreground="#666")
        self.assign_note.pack(side="left", padx=10)

        # Messages
        log_tab = ttk.Frame(self.nb, padding=10)
        self.log_tab = log_tab
        self.nb.add(log_tab, text="Messages")
        self.log_text = tk.Text(log_tab, wrap="word", height=10, font=("Menlo", 11),
                                background="#fafafa")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.rec_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.rec_var, foreground="#c0392b").pack(side="right")

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text if text.endswith("\n") else text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------ settings

    def is_demo(self):
        return self.source_var.get() == SRC_DEMO

    def load_current_definition(self):
        path = DEMO_DEF if self.is_demo() else LAYOUT_PATH
        try:
            self.definition = al.load_definition(path)
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_NAME, f"Couldn't read the byte layout:\n{path}\n\n{e}")
            self.definition = al.load_definition(TEMPLATE_DEF)
        self.build_tiles()
        names = [p["name"] for p in self.definition["parameters"]]
        self.assign_box.configure(values=names)
        for v, cb in self.chart_vars:
            cb.configure(values=names)
        self.update_assigned_column()

    def on_source_change(self):
        demo = self.is_demo()
        self.port_box.configure(state="disabled" if demo else "readonly")
        self.refresh_btn.configure(state="disabled" if demo else "normal")
        self.check_btn.configure(
            state="normal" if self.source_var.get() == SRC_CABLE else "disabled")
        state = "disabled" if demo else "normal"
        self.assign_btn.configure(state=state)
        self.clear_btn.configure(state=state)
        self.assign_note.configure(
            text="(the demo uses a fixed layout)" if demo else "")
        self.hint_var.set({
            SRC_DEMO: "Demo: a fake ECM with a hunting idle, no hardware needed.",
            SRC_CABLE: ("USB cable: turn its 10k switch ON (ALDL mode), key ON, run "
                        "'Check cable…' once, then Connect."),
            SRC_ARDUINO: "Arduino: plug in the interface, key ON, then Connect.",
        }[self.source_var.get()])
        self.load_current_definition()

    def refresh_ports(self):
        ports = list_serial_ports()
        self.port_box.configure(values=[AUTO_PORT] + ports)
        if self.port_var.get() not in [AUTO_PORT] + ports:
            self.port_var.set(AUTO_PORT)

    def chosen_port(self):
        port = self.port_var.get()
        return al.find_port() if port in ("", AUTO_PORT) else port

    def cable_settings(self):
        try:
            with open(CABLE_SETTINGS, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # ---------------------------------------------------------- connecting

    def make_source_factory(self):
        source = self.source_var.get()
        invert, lsb = self.invert_var.get(), self.lsb_var.get()
        if source == SRC_DEMO:
            return al.SimSource
        if source == SRC_CABLE:
            settings = self.cable_settings()
            if not settings:
                self.log("No saved cable settings yet. Using defaults; "
                         "run 'Check cable…' with the key ON to find the right ones.")
            baud = settings.get("baud", aldl160.DEFAULT_CABLE_BAUD)
            inv = invert or settings.get("invert", False)

            def make():
                port = self.chosen_port()
                self.q.put(("log", f"Opening cable on {port} at {baud} baud"
                                   f"{' (inverted)' if inv else ''}\n"))
                return aldl160.CableSource(port, baud, inv, msb_first=not lsb)
            return make

        def make_arduino():
            port = self.chosen_port()
            self.q.put(("log", f"Opening Arduino on {port}\n"))
            src = al.SerialSource(port)
            if invert:
                src.send("i")
            if lsb:
                src.send("b")
            return src
        return make_arduino

    def toggle_connect(self):
        if self.reader:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if self.checking:
            return
        self.load_current_definition()
        self.frames, self.last_frame = 0, None
        self.byte_stats.clear()
        self.history.clear()
        self.tree.delete(*self.tree.get_children())
        self.ecm_status.configure(text="")
        self.t0 = time.time()
        self.reader = Reader(self.make_source_factory(), self.q)
        self.reader.start()
        self.connect_btn.configure(text="Disconnect")
        self.status_var.set(f"Connecting to {self.source_var.get()}…")

    def disconnect(self):
        if self.reader:
            self.reader.stop()
            self.reader = None
        if self.recording:
            self.toggle_record()
        self.connect_btn.configure(text="Connect")
        self.status_var.set("Not connected")

    # --------------------------------------------------------- cable check

    def check_cable(self):
        if self.checking:
            return
        if self.reader:
            self.disconnect()
        self.checking = True
        self.check_btn.configure(state="disabled")
        self.connect_btn.configure(state="disabled")
        self.nb.select(self.log_tab)
        self.log("\n=== Cable check (key must be ON; takes about 30 seconds) ===")
        port_choice = self.port_var.get()

        def work():
            best = None
            with contextlib.redirect_stdout(QueueWriter(self.q)):
                try:
                    port = al.find_port() if port_choice in ("", AUTO_PORT) else port_choice
                    print(f"Cable on {port}.")
                    caps = cable_check.record(port, cable_check.BAUDS, 8)
                    path = os.path.join(LOG_DIR,
                                        f"cable-check-{datetime.now():%Y%m%d-%H%M%S}.txt")
                    cable_check.save_capture(path, caps)
                    print(f"Saved everything received to:\n  {path}")
                    best = cable_check.analyse(caps)
                    if best:
                        with open(CABLE_SETTINGS, "w", encoding="utf-8") as f:
                            json.dump({"baud": best["baud"], "invert": best["invert"]}, f,
                                      indent=2)
                except SystemExit as e:
                    print(str(e))
                except Exception as e:
                    print(f"{type(e).__name__}: {e}")
            self.q.put(("check_done", best))

        threading.Thread(target=work, daemon=True).start()

    def check_finished(self, best):
        self.checking = False
        self.connect_btn.configure(state="normal")
        self.on_source_change()
        if best:
            msg = (f"Success: clean {best['length']}-byte frames at {best['baud']} baud"
                   f"{', inverted' if best['invert'] else ''}. "
                   + cable_check.length_note(best["length"]) + "\n\nSettings saved. "
                   "Click Connect, then use the Raw bytes tab.")
            if best["invert"]:
                msg += ("\n\nNote: inverted cables are experimental. If values look wrong, "
                        "send the cable-check file in the logs folder to Claude.")
            self.log(msg)
            messagebox.showinfo(APP_NAME, msg)
        else:
            msg = ("No clean frames found.\n\nCheck the key is ON. If the cable has a switch "
                   "for the 10k resistor, flip it and check again. If it still fails, send "
                   "the cable-check file from the logs folder to Claude.")
            self.log(msg)
            messagebox.showwarning(APP_NAME, msg)

    # ----------------------------------------------------------- recording

    def toggle_record(self):
        if self.recording:
            f, _, _, cap, path = self.recording
            f.close()
            cap.close()
            self.recording = None
            self.record_btn.configure(text="● Record")
            self.rec_var.set("")
            self.log(f"Saved log: {path}")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        csv_path = os.path.join(LOG_DIR, f"log-{stamp}.csv")
        cap_path = os.path.join(LOG_DIR, f"cap-{stamp}.txt")
        f = open(csv_path, "w", newline="", encoding="utf-8")
        cap = open(cap_path, "w", encoding="ascii")
        names = [p["name"] for p in self.definition["parameters"]]
        writer = csv.writer(f)
        writer.writerow(["time", "elapsed_s"]
                        + [al.column_name(p) for p in self.definition["parameters"]]
                        + ["trouble_codes", "status_flags", "raw_hex"])
        self.recording = (f, writer, names, cap, csv_path)
        self.record_btn.configure(text="■ Stop")
        self.rec_var.set(f"● Recording to {os.path.basename(csv_path)}")

    # ------------------------------------------------------------ data in

    def poll(self):
        try:
            for _ in range(500):
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self.handle_line(payload)
                elif kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload)
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "connected":
                    self.status_var.set(f"Connected: {self.source_var.get()}, waiting for data…")
                elif kind == "error":
                    self.log("Error: " + payload)
                    self.status_var.set("Error: " + payload.splitlines()[0])
                    if self.reader:
                        self.reader = None
                        self.connect_btn.configure(text="Connect")
                elif kind == "stopped":
                    pass
                elif kind == "check_done":
                    self.check_finished(payload)
                elif kind == "claude_text":
                    self.claude_stream_text(payload)
                elif kind == "claude_done":
                    self.claude_finished(*payload)
                elif kind == "claude_error":
                    self.cl_linebuf = ""
                    self.claude_write("\n\n" + payload + "\n", "error")
                    self.claude_set_busy(False)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def handle_line(self, line):
        if self.recording:
            self.recording[3].write(line if line.endswith("\n") else line + "\n")
        msg = al.parse_line(line)
        if not msg:
            return
        kind, payload = msg
        if kind == "I":
            self.log(payload)
        elif kind == "D":
            self.handle_frame(payload)

    def handle_frame(self, frame):
        self.frames += 1
        elapsed = time.time() - self.t0
        rows = al.decode(frame, self.definition)
        values = {name: value for name, value, _, _ in rows}
        codes, flags = al.decode_status(frame, self.definition)
        if self.definition.get("trouble_codes") or flags:
            values["_codes"], values["_flags"] = codes, flags
        self.show_ecm_status(frame, codes, flags)
        for name, value, units, p in rows:
            tile = self.tiles.get(name)
            if not tile:
                continue
            tile["value"].configure(text=al.fmt_value(value, p))
            rng = p.get("range")
            if rng and value is not None and rng[1] > rng[0]:
                frac = max(0.0, min(1.0, (value - rng[0]) / (rng[1] - rng[0])))
                tile["bar"].configure(value=frac * 1000)
        self.history.append((elapsed, values))
        while self.history and elapsed - self.history[0][0] > CHART_SECONDS:
            self.history.popleft()
        self.update_raw(frame)
        if self.recording:
            _, writer, names, _, _ = self.recording
            writer.writerow([datetime.now().isoformat(timespec="milliseconds"), f"{elapsed:.2f}"]
                            + ["" if values.get(n) is None else f"{values[n]:.3f}" for n in names]
                            + [al.codes_text(codes), al.flags_text(flags), frame.hex().upper()])
            self.recording[0].flush()
        self.status_var.set(f"Connected: {self.source_var.get()}   frames: {self.frames}   "
                            f"frame length: {len(frame)} bytes")
        self.last_frame = frame

    # -------------------------------------------------------- dashboard

    def show_ecm_status(self, frame, codes, flags):
        parts = []
        exp = self.definition.get("expected_length")
        if exp and len(frame) != exp:
            self.ecm_status.configure(foreground="#c0392b", text=(
                f"Frames are {len(frame)} bytes but this ECM sends {exp}: the readings below "
                "are probably wrong. Re-run Check cable… or try 'Invert signal'."))
            return
        if self.definition.get("trouble_codes"):
            parts.append("Trouble codes: " + (", ".join(f"{c} ({t})" for c, t in codes)
                                              if codes else "none"))
        if "Closed loop" in flags:
            parts.append("Closed loop" if flags["Closed loop"] else "Open loop")
        for name in ("Park/Neutral", "A/C clutch on", "Diagnostic (shorted) mode"):
            if flags.get(name):
                parts.append(name)
        self.ecm_status.configure(foreground="#c0392b" if codes else "#1e5631",
                                  text="   •   ".join(parts))

    def build_tiles(self):
        for w in self.tile_frame.winfo_children():
            w.destroy()
        self.tiles = {}
        params = [p for p in self.definition["parameters"] if p.get("range")]
        cols = 4
        for i, p in enumerate(params):
            box = ttk.LabelFrame(self.tile_frame, text=p["name"], padding=(10, 4))
            box.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)
            row = ttk.Frame(box)
            row.pack(fill="x")
            value = ttk.Label(row, text="--", font=("Helvetica", 28, "bold"))
            value.pack(side="left")
            ttk.Label(row, text=" " + p["units"], font=("Helvetica", 14)).pack(
                side="left", anchor="s", pady=(0, 6))
            bar = ttk.Progressbar(box, maximum=1000, mode="determinate")
            bar.pack(fill="x", pady=(4, 2))
            if p["offset"] is None:
                ttk.Label(box, text="not mapped yet", foreground="#999").pack(anchor="w")
            self.tiles[p["name"]] = {"value": value, "bar": bar}
        for c in range(cols):
            self.tile_frame.columnconfigure(c, weight=1)
        missing = sum(p["offset"] is None for p in self.definition["parameters"])
        if missing:
            self.banner.configure(text=(
                f"{missing} of {len(self.definition['parameters'])} values aren't mapped to a "
                "byte yet, so they show --. Connect to the car and use the Raw bytes tab to "
                "assign them (or fill in my_layout.json from a published 1227170 definition)."))
        else:
            self.banner.configure(text="")

    # ------------------------------------------------------------- chart

    def redraw_chart(self):
        try:
            self._draw_chart()
        finally:
            self.root.after(300, self.redraw_chart)

    def _draw_chart(self):
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 150)
        left, right, gap = 70, 16, 14
        strip_h = (h - gap * 4) / 3
        params = {p["name"]: p for p in self.definition["parameters"]}
        now = self.history[-1][0] if self.history else 0
        for i, (var, _) in enumerate(self.chart_vars):
            name = var.get()
            top = gap + i * (strip_h + gap)
            bottom = top + strip_h
            c.create_rectangle(left, top, w - right, bottom, outline="#ddd")
            p = params.get(name)
            pts = [(t, v.get(name)) for t, v in self.history if v.get(name) is not None]
            c.create_text(left + 6, top + 4, anchor="nw", fill=CHART_COLORS[i],
                          text=f"{name}" + (f" ({p['units']})" if p and p["units"] else ""),
                          font=("Helvetica", 12, "bold"))
            if not p or not pts:
                c.create_text((left + w - right) / 2, (top + bottom) / 2, fill="#aaa",
                              text="no data" if p else "pick a value above")
                continue
            vals = [v for _, v in pts]
            lo, hi = min(vals), max(vals)
            if p.get("range"):
                lo, hi = min(lo, p["range"][0]), max(hi, p["range"][1])
            if hi - lo < 1e-9:
                hi = lo + 1
            dec = p.get("decimals", 0)
            c.create_text(left - 6, top, anchor="ne", text=f"{hi:.{dec}f}", fill="#666")
            c.create_text(left - 6, bottom, anchor="se", text=f"{lo:.{dec}f}", fill="#666")
            c.create_text(w - right - 6, top + 4, anchor="ne", fill=CHART_COLORS[i],
                          text=f"now: {vals[-1]:.{dec}f}", font=("Helvetica", 12, "bold"))

            def xy(t, v):
                x = left + (w - right - left) * (1 - (now - t) / CHART_SECONDS)
                y = bottom - (bottom - top) * (v - lo) / (hi - lo)
                return x, y
            coords = [n for t, v in pts for n in xy(t, v)]
            if len(coords) >= 4:
                c.create_line(*coords, fill=CHART_COLORS[i], width=2)
            else:
                x, y = xy(*pts[-1])
                c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=CHART_COLORS[i], outline="")

    # ---------------------------------------------------------- analysis

    def params_by_name(self):
        return {p["name"]: p for p in self.definition["parameters"]}

    def refresh_analysis(self):
        try:
            if self.an_log is None:
                if self.history:
                    findings = analysis.analyze(list(self.history), self.params_by_name())
                    title = f"Live: last {analysis.LIVE_WINDOW_S} seconds"
                else:
                    findings = [analysis.Finding(
                        analysis.INFO, "No data yet",
                        "Connect to the car (or the demo) and checks will appear here.")]
                    title = "Live"
                self.show_findings(title, findings)
        finally:
            self.root.after(2000, self.refresh_analysis)

    def show_findings(self, title, findings):
        key = (title, [(f.severity, f.title, f.detail) for f in findings])
        if key == self.an_last:
            return
        self.an_last = key
        self.an_title.configure(text=title)
        t = self.an_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for f in findings:
            t.insert("end", f" {f.severity.upper()} ", f.severity)
            t.insert("end", "  " + f.title + "\n", "title")
            if f.detail:
                t.insert("end", f.detail + "\n", "detail")
            if f.hint:
                t.insert("end", f.hint + "\n", "hint")
            t.insert("end", "\n")
        t.configure(state="disabled")

    def analyze_log_file(self, path=None):
        path = path or filedialog.askopenfilename(
            title="Choose a log to analyze", initialdir=LOG_DIR,
            filetypes=[("CSV logs", "*.csv"), ("All files", "*")])
        if not path:
            return
        try:
            hist, params = analysis.load_log_params(path, fallback=self.params_by_name())
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror(APP_NAME, f"Couldn't read that log:\n{e}")
            return
        findings = analysis.analyze(hist, params, window=None)
        secs = hist[-1][0] - hist[0][0] if hist else 0
        self.an_log = path
        self.an_last = None
        self.show_findings(f"Log: {os.path.basename(path)}  ({secs:.0f} s)", findings)
        self.live_btn.pack(side="right", padx=6)
        self.nb.select(self.an_text.master)

    def analysis_back_to_live(self):
        self.an_log = None
        self.an_last = None
        self.live_btn.pack_forget()

    # -------------------------------------------------------- Ask Claude

    def _build_claude_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.claude_tab = tab
        self.nb.add(tab, text="Ask Claude")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.cl_live_btn = ttk.Button(top, text="Ask about live data",
                                      command=self.claude_ask_live)
        self.cl_live_btn.pack(side="left")
        self.cl_log_btn = ttk.Button(top, text="Ask about a saved log…",
                                     command=self.claude_ask_log)
        self.cl_log_btn.pack(side="left", padx=4)
        self.cl_new_btn = ttk.Button(top, text="New conversation", command=self.claude_reset)
        self.cl_new_btn.pack(side="left", padx=4)
        ttk.Button(top, text="API key…", command=self.claude_key_dialog).pack(side="right")
        self.cl_cost = ttk.Label(top, foreground="#666")
        self.cl_cost.pack(side="right", padx=10)
        ttk.Label(tab, foreground="#666", wraplength=950, text=(
            "Sends a summary of your readings (values, trouble codes, ECM status and the "
            "Analysis findings) to Anthropic's Claude API and shows its diagnosis. Needs your "
            "own API key from console.anthropic.com; each question is billed to that key, "
            "usually a few cents. Nothing is sent until you click a button. Claude can be "
            "wrong: double-check before replacing parts.")).pack(fill="x", pady=(4, 6))
        box = ttk.Frame(tab)
        box.pack(fill="both", expand=True)
        self.cl_text = tk.Text(box, wrap="word", font=("Helvetica", 13), padx=12, pady=10,
                               background="white", relief="flat")
        sb = ttk.Scrollbar(box, command=self.cl_text.yview)
        self.cl_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cl_text.pack(side="left", fill="both", expand=True)
        self.cl_text.tag_configure("you", foreground="#2a6fdb",
                                   font=("Helvetica", 13, "bold"))
        self.cl_text.tag_configure("note", foreground="#666", font=("Helvetica", 12, "italic"))
        self.cl_text.tag_configure("error", foreground="#c0392b")
        self.cl_text.tag_configure("h", font=("Helvetica", 15, "bold"), spacing1=6)
        self.cl_text.tag_configure("b", font=("Helvetica", 13, "bold"))
        self.cl_text.tag_configure("li", lmargin1=12, lmargin2=28)
        self.cl_linebuf = ""
        self.cl_text.configure(state="disabled")
        bottom = ttk.Frame(tab)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, text="Follow-up:").pack(side="left")
        self.cl_entry = ttk.Entry(bottom)
        self.cl_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.cl_entry.bind("<Return>", lambda e: self.claude_send_followup())
        self.cl_send_btn = ttk.Button(bottom, text="Send", command=self.claude_send_followup)
        self.cl_send_btn.pack(side="left")
        self.claude_conv = None
        self.claude_busy = False
        self.claude_write("Ask Claude to look at your live data or a saved log. "
                          "Then ask follow-up questions below.\n", "note")

    def claude_write(self, text, tag=None):
        t = self.cl_text
        t.configure(state="normal")
        t.insert("end", text, tag or ())
        t.see("end")
        t.configure(state="disabled")

    def claude_stream_text(self, chunk):
        """Show streamed markdown, formatting each line once it's complete."""
        self.cl_linebuf += chunk
        while "\n" in self.cl_linebuf:
            line, self.cl_linebuf = self.cl_linebuf.split("\n", 1)
            self.claude_md_line(line)

    def claude_md_line(self, line, end="\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            self.claude_write(stripped.lstrip("#").strip() + end, "h")
            return
        tags = ()
        if stripped[:2] in ("- ", "* "):
            line, tags = "• " + stripped[2:], ("li",)
        elif stripped[:1].isdigit() and ". " in stripped[:4]:
            line, tags = stripped, ("li",)
        parts = line.split("**")
        for i, part in enumerate(parts):   # odd parts were between ** markers
            if part:
                self.claude_write(part, tags + (("b",) if i % 2 else ()))
        self.claude_write(end, tags)

    def claude_set_busy(self, busy):
        self.claude_busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.cl_live_btn, self.cl_log_btn, self.cl_new_btn, self.cl_send_btn):
            b.configure(state=state)

    def claude_api_key(self):
        return os.environ.get("ANTHROPIC_API_KEY") or claude_assistant.load_api_key()

    def claude_start(self, shown, prompt):
        if self.claude_busy:
            return
        key = self.claude_api_key()
        if not key:
            if messagebox.askyesno(APP_NAME, "Ask Claude needs an Anthropic API key "
                                   "(from console.anthropic.com). Add one now?"):
                self.claude_key_dialog()
            return
        if self.claude_conv is None:
            try:
                self.claude_conv = claude_assistant.Conversation(api_key=key)
            except Exception as e:  # SDK missing or bad configuration
                messagebox.showerror(APP_NAME, f"Couldn't start the Claude client:\n{e}")
                return
        self.claude_set_busy(True)
        self.claude_write(f"\nYou: {shown}\n", "you")
        self.claude_write("Claude is thinking… (this can take a minute)\n\n", "note")
        conv = self.claude_conv

        def work():
            try:
                result = conv.ask(prompt, lambda chunk: self.q.put(("claude_text", chunk)))
                self.q.put(("claude_done", result))
            except Exception as e:
                self.q.put(("claude_error", claude_assistant.describe_error(e)))

        threading.Thread(target=work, daemon=True).start()

    def claude_finished(self, answer, stop_reason, cost):
        if self.cl_linebuf:
            self.claude_md_line(self.cl_linebuf, end="")
            self.cl_linebuf = ""
        if stop_reason == "refusal":
            self.claude_write("\n\n(Claude declined to answer this one.)\n", "note")
        elif stop_reason == "max_tokens":
            self.claude_write("\n\n(The answer was cut off for length. Ask it to continue.)\n",
                              "note")
        self.claude_write("\n")
        self.cl_cost.configure(text=f"Last answer ≈ ${cost:.3f}   •   this conversation ≈ "
                                    f"${self.claude_conv.total_cost:.2f}")
        self.claude_set_busy(False)

    def claude_ask_live(self):
        if not self.history:
            messagebox.showinfo(APP_NAME, "No live data yet. Connect to the car (or the demo) "
                                          "first and let it run for half a minute.")
            return
        summary = claude_assistant.summary_for_live(self.history, self.params_by_name())
        self.claude_start(f"[live data: last {len(self.history)} frames] What do you see, and "
                          "what should I check?", claude_assistant.first_question(summary))

    def claude_ask_log(self):
        path = filedialog.askopenfilename(
            title="Choose a log for Claude", initialdir=LOG_DIR,
            filetypes=[("CSV logs", "*.csv"), ("All files", "*")])
        if not path:
            return
        try:
            summary = claude_assistant.summary_for_log(path, self.params_by_name())
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror(APP_NAME, f"Couldn't read that log:\n{e}")
            return
        self.claude_start(f"[log: {os.path.basename(path)}] What do you see, and what should "
                          "I check?", claude_assistant.first_question(summary))

    def claude_send_followup(self):
        text = self.cl_entry.get().strip()
        if not text or self.claude_busy:
            return
        if self.claude_conv is None or not self.claude_conv.messages:
            messagebox.showinfo(APP_NAME, "Start with 'Ask about live data' or 'Ask about a "
                                          "saved log' so Claude has the readings.")
            return
        self.cl_entry.delete(0, "end")
        self.claude_start(text, text)

    def claude_reset(self):
        if self.claude_busy:
            return
        self.claude_conv = None
        self.cl_cost.configure(text="")
        self.claude_write("\n— New conversation —\n", "note")

    def claude_key_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Anthropic API key")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        have = bool(claude_assistant.load_api_key())
        ttk.Label(frm, wraplength=420, text=(
            "Create a key at console.anthropic.com (Settings → API keys) and paste it below. "
            "It's stored in your Mac's Keychain, not in a file. "
            + ("A key is saved already." if have else "No key is saved yet."))).pack(
            fill="x", pady=(0, 8))
        var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=var, show="•", width=48)
        entry.pack(fill="x")
        entry.focus_set()
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(10, 0))

        def save():
            key = var.get().strip()
            if not key:
                return
            try:
                claude_assistant.save_api_key(key)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Couldn't save the key to the Keychain:\n{e}",
                                     parent=win)
                return
            self.claude_conv = None   # next question uses the new key
            win.destroy()

        def remove():
            claude_assistant.delete_api_key()
            self.claude_conv = None
            win.destroy()

        ttk.Button(row, text="Save", command=save).pack(side="right")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        if have:
            ttk.Button(row, text="Remove saved key", command=remove).pack(side="left")
        entry.bind("<Return>", lambda e: save())

    # ---------------------------------------------------------- raw bytes

    def update_raw(self, frame):
        prev = self.last_frame
        for i, b in enumerate(frame):
            s = self.byte_stats.setdefault(i, [b, b, 0])
            s[0], s[1] = min(s[0], b), max(s[1], b)
            changed = prev is not None and i < len(prev) and prev[i] != b
            if changed:
                s[2] += 1
            iid = f"b{i}"
            vals = (i, f"{b:02X}", b, s[0], s[1], s[2], self.assigned_names(i))
            if self.tree.exists(iid):
                self.tree.item(iid, values=vals, tags=("changed",) if changed else ())
            else:
                self.tree.insert("", "end", iid=iid, values=vals)

    def assigned_names(self, offset):
        return ", ".join(p["name"] for p in self.definition["parameters"]
                         if p["offset"] is not None
                         and p["offset"] <= offset < p["offset"] + p.get("size", 1))

    def update_assigned_column(self):
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            vals[6] = self.assigned_names(int(vals[0]))
            self.tree.item(iid, values=vals)

    def selected_byte(self):
        sel = self.tree.selection()
        return int(self.tree.item(sel[0], "values")[0]) if sel else None

    def save_layout(self, change):
        with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
            layout = json.load(f)
        change(layout["parameters"])
        with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
            json.dump(layout, f, indent=2)
        self.load_current_definition()

    def assign_byte(self):
        offset, name = self.selected_byte(), self.assign_var.get()
        if offset is None or not name:
            messagebox.showinfo(APP_NAME, "Select a byte in the list and pick what it is first.")
            return

        def change(params):
            for p in params:
                if p["name"] == name:
                    p["offset"] = offset
        self.save_layout(change)
        self.log(f"Assigned byte {offset} to {name}.")

    def clear_byte(self):
        offset = self.selected_byte()
        if offset is None:
            return

        def change(params):
            for p in params:
                if p.get("offset") == offset:
                    p["offset"] = None
        self.save_layout(change)
        self.log(f"Cleared byte {offset}.")

    # ---------------------------------------------------------------- end

    def on_close(self):
        self.disconnect()
        self.root.destroy()


def self_test(root, app, seconds=4.0):
    """Used by the build: connect to the demo, check data arrives, then quit."""
    result = {}

    def start():
        app.source_var.set(SRC_DEMO)
        app.on_source_change()
        app.connect()
        app.toggle_record()
        root.after(int(seconds * 1000), finish)

    def finish():
        rpm = app.tiles.get("Engine RPM")
        shown = rpm["value"].cget("text") if rpm else "--"
        csv_path = app.recording[4]
        app.toggle_record()
        with open(csv_path, encoding="utf-8") as f:
            rows = len(f.read().strip().splitlines()) - 1
        app._draw_chart()
        app.analyze_log_file(csv_path)
        analysed = app.an_title.cget("text").startswith("Log:") and app.an_last
        try:
            import anthropic  # noqa: F401  (must be bundled in the app)
            import keyring
            backend = type(keyring.get_keyring()).__module__
            # On a Mac the Keychain backend must be bundled, not keyring's do-nothing one.
            ok_backend = sys.platform != "darwin" or backend.endswith("macOS")
            streamed = claude_assistant.offline_check() == "offline check ok"
            sdk = (f"{'yes' if ok_backend and streamed else 'no'} (keyring: {backend}, "
                   f"offline stream: {'ok' if streamed else 'FAILED'})")
        except Exception as e:
            sdk = f"no ({e})"
        summary = claude_assistant.summary_for_live(app.history, app.params_by_name())
        result["ok"] = (app.frames >= 3 and shown not in ("--", "") and rows >= 2
                        and bool(analysed) and sdk.startswith("yes")
                        and "Engine RPM:" in summary)
        print(f"self-test: frames={app.frames} rpm_tile={shown} logged_rows={rows} "
              f"log_analysis={'yes' if analysed else 'no'} claude_sdk={sdk} "
              f"-> {'OK' if result['ok'] else 'FAILED'}")
        app.on_close()

    root.after(200, start)
    root.mainloop()
    return 0 if result.get("ok") else 1


def main():
    setup_data_dir()
    root = tk.Tk()
    app = App(root)
    if "--self-test" in sys.argv:
        sys.exit(self_test(root, app))
    root.mainloop()


if __name__ == "__main__":
    main()
