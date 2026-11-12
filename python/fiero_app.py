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
import derived
import guided_tests
import normals
import vehicle_profile
import elm_sim
import obd_analysis
import obd_source
import obd_tests
import obd
import cable_check
import checks
import compare
import history
import report
import leak_hunt
import vacuum

APP_NAME = "Fiero ALDL"
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("FIERO_ALDL_DATA") or os.path.join(
    os.path.expanduser("~"), "Documents", APP_NAME)
LOG_DIR = os.path.join(DATA_DIR, "logs")
TESTS_DIR = os.path.join(DATA_DIR, "tests")
VEHICLES_DIR = os.path.join(DATA_DIR, "vehicles")
CHECKS_DIR = os.path.join(DATA_DIR, "checks")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
BASELINE_PATH = os.path.join(DATA_DIR, "baseline.json")
LAYOUT_PATH = os.path.join(DATA_DIR, "my_layout.json")
CABLE_SETTINGS = os.path.join(DATA_DIR, "cable_settings.json")
# GM data stream A29 (Fiero 2.8 V6, ECM 1227170). The demo uses the same layout.
TEMPLATE_DEF = os.path.join(RES_DIR, "definitions", "1227170_fiero_v6.json")
DEMO_DEF = TEMPLATE_DEF

SRC_DEMO, SRC_CABLE, SRC_ARDUINO = "Demo: Fiero (no car)", "USB ALDL cable (Fiero)", "Arduino interface (Fiero)"
SRC_DEMO_OBD, SRC_OBD = "Demo: Grand Prix (no car)", "OBD-II adapter (OBDLink)"
SOURCES = [SRC_DEMO, SRC_DEMO_OBD, SRC_OBD, SRC_CABLE, SRC_ARDUINO]
AUTO_PORT = "Automatic"
CHART_SECONDS = 60
CHART_COLORS = ("#2a6fdb", "#d1495b", "#2e9e5b")


INK = "#1b1d21"          # text on the white "paper" panels, in light and dark mode


def is_dark_mode(root):
    """True when macOS is in Dark Mode (Tk draws standard widgets dark then)."""
    if sys.platform != "darwin":
        return False
    try:
        return bool(int(root.tk.call("::tk::unsupported::MacWindowStyle", "isdark", root)))
    except (tk.TclError, ValueError):
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=2).stdout
            return "Dark" in out
        except (OSError, subprocess.SubprocessError):
            return False


def label_colors(dark):
    """Colors for text drawn on the window background (not the white panels)."""
    if dark:
        return {"muted": "#aab0b8", "warn": "#f0b55a", "bad": "#ff7b6e", "good": "#6fd492"}
    return {"muted": "#5f6670", "warn": "#a15c00", "bad": "#c0392b", "good": "#1e6b3a"}


def setup_data_dir():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(TESTS_DIR, exist_ok=True)
    os.makedirs(VEHICLES_DIR, exist_ok=True)
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
    return bool(params) and all(p.get("offset") is None for p in params if not p.get("derived"))


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
        self.definition = vacuum.add_param(al.load_definition(DEMO_DEF))
        self.vac = vacuum.Tracker()
        self.frames = 0
        self.last_frame = None
        self.byte_stats = {}
        self.history = deque()
        self.t0 = time.time()
        self.tiles = {}

        root.title(APP_NAME)
        root.update_idletasks()
        self.c = label_colors(is_dark_mode(root))
        root.geometry("1060x720")
        root.minsize(820, 560)
        self._build_toolbar()
        self._build_tabs()
        self._build_statusbar()
        self.refresh_ports()
        self.vehicle_var.set(self.active_vehicle_name())
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
        src = ttk.Combobox(bar, textvariable=self.source_var, state="readonly", width=23,
                           values=SOURCES)
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
        self.vehicle_var = tk.StringVar()
        ttk.Label(adv, text="Vehicle:").pack(side="left")
        ttk.Label(adv, textvariable=self.vehicle_var, font=("Helvetica", 13, "bold")).pack(
            side="left", padx=(4, 6))
        ttk.Button(adv, text="Vehicles…", command=self.open_vehicles).pack(side="left", padx=(0, 6))
        ttk.Button(adv, text="Save report", command=self.save_report).pack(side="left", padx=(0, 12))
        self.hint_var = tk.StringVar()
        ttk.Label(adv, textvariable=self.hint_var, foreground=self.c["muted"]).pack(side="left")
        self.invert_var = tk.BooleanVar(value=False)
        self.lsb_var = tk.BooleanVar(value=False)
        # Signal options only matter for the Fiero's ALDL cable and Arduino.
        self.signal_opts = ttk.Frame(adv)
        ttk.Label(self.signal_opts, text="Only if the data looks wrong:",
                  foreground=self.c["muted"]).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(self.signal_opts, text="Invert signal", variable=self.invert_var).pack(
            side="left", padx=(0, 10))
        ttk.Checkbutton(self.signal_opts, text="LSB first", variable=self.lsb_var).pack(side="left")

    def _build_tabs(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10)

        # Dashboard
        self.dash = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.dash, text="Dashboard")
        self.banner = ttk.Label(self.dash, foreground=self.c["warn"], wraplength=900)
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
        ttk.Button(top, text="Compare before/after…", command=self.compare_logs).pack(
            side="right", padx=6)
        self.live_btn = ttk.Button(top, text="Back to live", command=self.analysis_back_to_live)
        self.an_intro = ttk.Label(an_tab, foreground=self.c["muted"], wraplength=950)
        self.an_intro.pack(fill="x", pady=(4, 6))
        self.an_text = tk.Text(an_tab, wrap="word", foreground=INK, insertbackground=INK, font=("Helvetica", 13), padx=12, pady=10,
                               background="white", relief="flat", cursor="arrow")
        self.an_text.pack(fill="both", expand=True)
        for sev, color, label in ((analysis.PROBLEM, "#c0392b", "PROBLEM"),
                                  (analysis.WATCH, "#b9770e", "WATCH"),
                                  (analysis.OK, "#1e8449", "OK"),
                                  (analysis.INFO, "#5d6d7e", "INFO")):
            self.an_text.tag_configure(sev, foreground=color,
                                       font=("Helvetica", 13, "bold"))
        self.an_text.tag_configure("title", font=("Helvetica", 14, "bold"))
        self.an_text.tag_configure("calc_k", lmargin1=24, font=("Helvetica", 13, "bold"))
        self.an_text.tag_configure("calc_v", font=("Menlo", 13))
        self.an_text.tag_configure("detail", lmargin1=24, lmargin2=24)
        self.an_text.tag_configure("hint", lmargin1=24, lmargin2=24, foreground="#444",
                                   font=("Helvetica", 13, "italic"))
        self.an_text.configure(state="disabled")
        self.an_log = None       # (name, findings) when showing a saved log
        self.an_last = None

        self._build_codes_tab()
        self._build_tests_tab()
        self._build_leak_tab()
        self._build_checks_tab()
        self._build_history_tab()
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
        self.tree.tag_configure("changed", background="#fff2a8", foreground=INK)
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
        self.assign_note = ttk.Label(assign, foreground=self.c["muted"])
        self.assign_note.pack(side="left", padx=10)

        # Messages
        log_tab = ttk.Frame(self.nb, padding=10)
        self.log_tab = log_tab
        self.nb.add(log_tab, text="Messages")
        self.log_text = tk.Text(log_tab, wrap="word", foreground=INK, insertbackground=INK, height=10, font=("Menlo", 11),
                                background="#fafafa")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.rec_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.rec_var, foreground=self.c["bad"]).pack(side="right")

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text if text.endswith("\n") else text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------ settings

    def is_demo(self):
        return self.source_var.get() in (SRC_DEMO, SRC_DEMO_OBD)

    def is_obd(self):
        return self.source_var.get() in (SRC_DEMO_OBD, SRC_OBD)

    def vehicle_profile(self):
        """The profile that goes with the current source (None: generic OBD-II)."""
        items = vehicle_profile.list_profiles(VEHICLES_DIR)
        if self.source_var.get() == SRC_DEMO_OBD:
            return next((p for _, p in items if "Grand Prix GTP" in p["vehicle"]["name"]), None)
        if self.is_obd():
            name = self.active_vehicle_name()
            prof = next((p for _, p in items if p["vehicle"]["name"] == name), None)
            return prof if prof and prof["connection"]["type"] == "obd2" else None
        return next((p for _, p in items if p["connection"]["type"] == "aldl"), None)

    def load_current_definition(self):
        if self.is_obd():
            # The car's readings arrive from the adapter after connecting ("M" line).
            self.definition = {"type": "obd2", "name": "OBD-II", "parameters": [],
                               "trouble_codes": [], "flags": []}
            self.build_tiles()
            for v, cb in self.chart_vars:
                cb.configure(values=[])
            return
        path = DEMO_DEF if self.source_var.get() == SRC_DEMO else LAYOUT_PATH
        try:
            self.definition = vacuum.add_param(al.load_definition(path))
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
        state = "disabled" if demo or self.is_obd() else "normal"
        self.assign_btn.configure(state=state)
        self.clear_btn.configure(state=state)
        self.assign_note.configure(
            text="(the demo uses a fixed layout)" if demo else "")
        self.hint_var.set({
            SRC_DEMO_OBD: "Demo: a simulated 2002 Grand Prix GTP with a small vacuum leak.",
            SRC_OBD: ("OBD-II: plug the OBDLink into the car (under the dash), key ON, then "
                      "Connect. Pick your car under Vehicles… for its normal values."),
            SRC_DEMO: "Demo: a fake Fiero ECM with a hunting idle, no hardware needed.",
            SRC_CABLE: ("USB cable: turn its 10k switch ON (ALDL mode), key ON, run "
                        "'Check cable…' once, then Connect."),
            SRC_ARDUINO: "Arduino: plug in the interface, key ON, then Connect.",
        }[self.source_var.get()])
        self.claude_conv = None                  # vehicle may have changed
        if self.source_var.get() in (SRC_CABLE, SRC_ARDUINO):
            self.signal_opts.pack(side="right")
        else:
            self.signal_opts.pack_forget()
        prof = self.vehicle_profile()
        car = (prof or {}).get("vehicle", {}).get("name") or "OBD-II car (no profile chosen)"
        self.vehicle_var.set(car)
        self.an_intro.configure(text=(
            f"Checks readings against researched normal values for the {car} (each with its "
            "source; see Vehicles…) and explains what's usually behind anything out of the "
            "norm. It points where to look; it isn't a diagnosis." if prof else
            "No vehicle profile chosen, so general OBD-II rules of thumb are used. Pick or "
            "research your car under Vehicles… for its own normal values."))
        self.load_current_definition()
        if hasattr(self, "test_tree"):
            self.populate_tests()
        self.update_leak_spots()
        self.refresh_checks()
        self.refresh_history()

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
        if source in (SRC_DEMO_OBD, SRC_OBD):
            profile = self.vehicle_profile()
            if source == SRC_DEMO_OBD:
                return lambda: obd_source.ObdSource(elm_sim.SimElm(), profile)

            def make_obd():
                port = self.chosen_port()
                self.q.put(("log", f"Opening OBD-II adapter on {port}\n"))
                return obd_source.ObdSource(obd_source.open_adapter(port), profile)
            return make_obd
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
                elif kind == "obd_result":
                    self.codes_result(*payload)
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
        elif kind == "M":
            self.set_obd_definition(payload)
        elif kind == "J":
            vals = dict(payload.get("values", {}))
            codes = [tuple(c) for c in payload.get("codes", [])]
            flags = payload.get("flags", {})
            vals["_codes"], vals["_flags"] = codes, flags
            self._ingest(vals, codes, flags, None)

    def set_obd_definition(self, d):
        for p in d["parameters"]:
            p.setdefault("decimals", 1)
            p.setdefault("offset", 0)
            p.setdefault("units", p.get("unit", ""))
        self.definition = vacuum.add_param(d)
        self.vac = vacuum.Tracker()
        self.build_tiles()
        names = [p["name"] for p in d["parameters"]]
        for v, cb in self.chart_vars:
            cb.configure(values=names)
        defaults = [n for n in ("Engine RPM", "Long trim B1", "O2 B1S1 (upstream)") if n in names]
        for (v, _cb), name in zip(self.chart_vars, defaults):
            v.set(name)
        self.log(f"{d['name']}: {len(names)} readings available.")

    def handle_frame(self, frame):
        rows = al.decode(frame, self.definition)
        values = {name: value for name, value, _, _ in rows}
        codes, flags = al.decode_status(frame, self.definition)
        if self.definition.get("trouble_codes") or flags:
            values["_codes"], values["_flags"] = codes, flags
        self._ingest(values, codes, flags, frame)

    def _ingest(self, values, codes, flags, frame):
        """One sample from any source: update tiles, history, tests, recording."""
        self.frames += 1
        elapsed = time.time() - self.t0
        if not self.vac.checked_baseline and self.definition.get("type") != "obd2":
            self.vac.checked_baseline = True
            self.vac.baro_v = self.vac.baro_v or self.baseline_baro()
        self.vac.update(values)
        vnow = values.get(vacuum.NAME)
        if vnow is not None and hasattr(self, "leak_vac"):
            self.leak_vac.set(f"Vacuum now: {vnow:.1f} inHg" + (" (est.)" if (values.get("MAP") or 99) < 15 else ""))
        self.show_ecm_status(frame, codes, flags)
        params = {p["name"]: p for p in self.definition["parameters"]}
        rows = [(n, values.get(n), p.get("units", ""), p) for n, p in params.items()]
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
        if getattr(self, "leak_marks", None):
            self.refresh_leaks()
        while self.history and elapsed - self.history[0][0] > CHART_SECONDS:
            self.history.popleft()
        if frame is not None:
            self.update_raw(frame)
        if self.runner is not None and not self.runner.done:
            self.runner.feed(elapsed, values)
        if self.recording:
            _, writer, names, _, _ = self.recording
            writer.writerow([datetime.now().isoformat(timespec="milliseconds"), f"{elapsed:.2f}"]
                            + ["" if values.get(n) is None else f"{values[n]:.3f}" for n in names]
                            + [al.codes_text(codes), al.flags_text(flags),
                               frame.hex().upper() if frame is not None else ""])
            self.recording[0].flush()
        what = (f"frame length: {len(frame)} bytes" if frame is not None
                else self.definition.get("protocol", ""))
        self.status_var.set(f"Connected: {self.source_var.get()}   readings: {self.frames}   {what}")
        if frame is not None:
            self.last_frame = frame

    # -------------------------------------------------------- dashboard

    def show_ecm_status(self, frame, codes, flags):
        parts = []
        exp = self.definition.get("expected_length")
        if frame is not None and exp and len(frame) != exp:
            self.ecm_status.configure(foreground=self.c["bad"], text=(
                f"Frames are {len(frame)} bytes but this ECM sends {exp}: the readings below "
                "are probably wrong. Re-run Check cable… or try 'Invert signal'."))
            return
        if self.definition.get("trouble_codes") or self.definition.get("type") == "obd2":
            parts.append("Trouble codes: " + (", ".join(f"{c} ({t})" for c, t in codes)
                                              if codes else "none"))
        if flags.get("Check engine light on"):
            parts.append("Check engine light ON")
        if "Closed loop" in flags:
            parts.append("Closed loop" if flags["Closed loop"] else "Open loop")
        for name in ("Park/Neutral", "A/C clutch on", "Diagnostic (shorted) mode"):
            if flags.get(name):
                parts.append(name)
        self.ecm_status.configure(foreground=self.c["bad"] if codes else self.c["good"],
                                  text="   •   ".join(parts))

    def tile_normal(self, p):
        """'normal 4–6 g/s · warm idle' for a dashboard tile, from the vehicle's profile."""
        if p["name"] == vacuum.NAME:
            lo, hi = vacuum.NORMAL_IDLE_INHG
            return f"normal {lo:.0f}–{hi:.0f} inHg · warm idle, sea level"
        if self.definition.get("type") == "obd2":
            n = obd_analysis.normal(self.vehicle_profile(), p.get("signal"))
            if n and (n.get("low") is not None or n.get("high") is not None):
                return f"normal {vehicle_profile_range(n)} · {n.get('short', '')}"
            return None
        nkey = normals.DASHBOARD.get(p["name"])
        if nkey and p.get("offset") is not None:
            return f"normal {normals.range_text(nkey)} · {normals.get(nkey).get('short', '')}"
        return None

    def build_tiles(self):
        for w in self.tile_frame.winfo_children():
            w.destroy()
        self.tiles = {}
        params = [p for p in self.definition["parameters"] if p.get("range")]
        cols = 5 if len(params) > 13 else 4
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
            ntext = self.tile_normal(p)
            if ntext:
                ttk.Label(box, foreground=self.c["muted"], font=("Helvetica", 11),
                          text=ntext).pack(anchor="w")
            if p["offset"] is None and not p.get("derived"):
                ttk.Label(box, text="not mapped yet", foreground=self.c["muted"]).pack(anchor="w")
            self.tiles[p["name"]] = {"value": value, "bar": bar}
        for c in range(cols):
            self.tile_frame.columnconfigure(c, weight=1)
        missing = sum(p["offset"] is None and not p.get("derived")
                      for p in self.definition["parameters"])
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

    def baseline_baro(self):
        try:
            with open(BASELINE_PATH, encoding="utf-8") as f:
                return json.load(f).get("baro_map_v")
        except (OSError, ValueError):
            return None

    def refresh_analysis(self):
        try:
            if self.an_log is None:
                if self.history:
                    hist = list(self.history)
                    if self.definition.get("type") == "obd2":
                        findings = obd_analysis.analyze(hist, self.vehicle_profile())
                    else:
                        findings = analysis.analyze(hist, self.params_by_name())
                    findings = findings + vacuum.findings(hist)
                    self.an_calc = derived.calculated(hist, self.baseline_baro())
                    title = f"Live: last {analysis.LIVE_WINDOW_S} seconds"
                else:
                    findings = [analysis.Finding(
                        analysis.INFO, "No data yet",
                        "Connect to the car (or the demo) and checks will appear here.")]
                    title = "Live"
                    self.an_calc = []
                self.show_findings(title, findings)
        finally:
            self.root.after(2000, self.refresh_analysis)

    def show_findings(self, title, findings):
        calc = getattr(self, "an_calc", []) or []
        key = (title, [(f.severity, f.title, f.detail) for f in findings], calc)
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
        if calc:
            t.insert("end", "Calculated from the data\n", "title")
            for label, value, note in calc:
                t.insert("end", f"{label}: ", "calc_k")
                t.insert("end", f"{value}\n", "calc_v")
                t.insert("end", note + "\n", "hint")
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
        if any(n in params for n in ("Long trim B1", "Mass air flow", "O2 B1S1 (upstream)")):
            findings = obd_analysis.analyze(hist, self.vehicle_profile(), window=None)
        else:
            findings = analysis.analyze(hist, params, window=None)
        findings = findings + vacuum.findings(hist)
        self.an_calc = derived.calculated(hist, self.baseline_baro())
        secs = hist[-1][0] - hist[0][0] if hist else 0
        self.an_log = path
        self.an_last = None
        self.show_findings(f"Log: {os.path.basename(path)}  ({secs:.0f} s)", findings)
        self.live_btn.pack(side="right", padx=6)
        self.nb.select(self.an_text.master)

    def compare_logs(self, before=None, after=None):
        before = before or filedialog.askopenfilename(
            title="Choose the BEFORE log (recorded before the fix)", initialdir=LOG_DIR,
            filetypes=[("CSV logs", "*.csv"), ("All files", "*")])
        if not before:
            return
        after = after or filedialog.askopenfilename(
            title="Choose the AFTER log (recorded after the fix)", initialdir=LOG_DIR,
            filetypes=[("CSV logs", "*.csv"), ("All files", "*")])
        if not after:
            return
        try:
            hb, ha = analysis.load_log(before), analysis.load_log(after)
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror(APP_NAME, f"Couldn't read a log:\n{e}")
            return
        rows, notes = compare.compare(hb, ha)
        self.an_log = ("compare", before, after)
        self.an_last = None
        self.last_compare = (os.path.basename(before), os.path.basename(after), rows, notes)
        t = self.an_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        self.an_title.configure(text="Before / after comparison")
        self.an_intro.configure(text=(
            f"Before: {os.path.basename(before)}    After: {os.path.basename(after)}. "
            "Both logs are boiled down to warm-idle numbers and compared."))
        t.insert("end", compare.verdict_text(rows) + "\n\n", "title")
        tag = {compare.BETTER: analysis.OK, compare.WORSE: analysis.PROBLEM,
               compare.SAME: analysis.INFO}
        for r in rows:
            t.insert("end", f"{r.name}: ", "calc_k")
            t.insert("end", f"{compare.fmt(r.before, r.unit)} → {compare.fmt(r.after, r.unit)} "
                            f"{r.unit}  ", "calc_v")
            if r.verdict:
                t.insert("end", r.verdict.upper(), tag[r.verdict])
            t.insert("end", "\n")
        for n in notes:
            t.insert("end", "\n" + n + "\n", "hint")
        t.configure(state="disabled")
        self.live_btn.pack(side="right", padx=6)
        self.nb.select(self.an_text.master)

    def compare_text(self):
        """Plain-text comparison for Claude and the report."""
        if not getattr(self, "last_compare", None):
            return ""
        b, a, rows, notes = self.last_compare
        lines = [f"Before/after comparison ({b} vs {a}): {compare.verdict_text(rows)}"]
        lines += [f"- {r.name}: {compare.fmt(r.before, r.unit)} -> {compare.fmt(r.after, r.unit)} "
                  f"{r.unit} ({r.verdict or 'n/a'})" for r in rows]
        return "\n".join(lines + notes)

    def analysis_back_to_live(self):
        self.an_log = None
        self.an_last = None
        self.live_btn.pack_forget()

    # --------------------------------------------------------- Vehicles

    def settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_settings(self, **changes):
        data = self.settings()
        data.update(changes)
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            self.log(f"Couldn't save settings: {e}")

    def active_vehicle_name(self):
        name = self.settings().get("vehicle")
        names = [p["vehicle"]["name"] for _, p in vehicle_profile.list_profiles(VEHICLES_DIR)]
        if name in names:
            return name
        return next((n for n in names if "Fiero" in n), names[0] if names else "")

    def open_vehicles(self):
        VehiclesWindow(self)

    # ------------------------------------------------------------ Codes

    def _build_codes_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.codes_tab = tab
        self.nb.add(tab, text="Codes")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.codes_read_btn = ttk.Button(top, text="Read codes & readiness", command=self.read_codes)
        self.codes_read_btn.pack(side="left")
        self.codes_clear_btn = ttk.Button(top, text="Clear codes…", command=self.ask_clear_codes)
        self.codes_clear_btn.pack(side="left", padx=6)
        self.codes_confirm = ttk.Frame(tab)
        ttk.Label(self.codes_confirm, foreground=self.c["bad"], wraplength=900, text=(
            "Clearing erases the stored codes and freeze frame and resets all readiness monitors: "
            "the car won't pass an emissions inspection until they complete again (usually after "
            "a few days of normal driving). Only clear after a repair, or to see if a code returns.")
                  ).pack(anchor="w", pady=(6, 4))
        row = ttk.Frame(self.codes_confirm)
        row.pack(anchor="w")
        ttk.Button(row, text="Yes, clear the codes", command=self.clear_codes).pack(side="left")
        ttk.Button(row, text="Cancel", command=lambda: self.codes_confirm.pack_forget()).pack(
            side="left", padx=6)
        self.codes_text = tk.Text(tab, wrap="word", font=("Helvetica", 13), padx=12, pady=10,
                                  background="white", foreground=INK, relief="flat", cursor="arrow")
        self.codes_text.pack(fill="both", expand=True, pady=(8, 0))
        t = self.codes_text
        t.tag_configure("h", font=("Helvetica", 15, "bold"), spacing1=8)
        t.tag_configure("bad", foreground="#c0392b", font=("Helvetica", 13, "bold"))
        t.tag_configure("good", foreground="#1e8449", font=("Helvetica", 13, "bold"))
        t.tag_configure("warn", foreground="#b9770e", font=("Helvetica", 13, "bold"))
        t.tag_configure("muted", foreground="#5f6670")
        t.tag_configure("code", font=("Menlo", 13, "bold"))
        self.codes_write([("Connect to a car (or a demo), then click Read codes & readiness.\n", "muted")])

    def codes_write(self, parts, clear=True):
        t = self.codes_text
        t.configure(state="normal")
        if clear:
            t.delete("1.0", "end")
        for text, tag in parts:
            t.insert("end", text, tag or ())
        t.configure(state="disabled")

    def _obd_src(self):
        src = getattr(self.reader, "src", None) if self.reader else None
        return src if isinstance(src, obd_source.ObdSource) else None

    def read_codes(self):
        src = self._obd_src()
        if src is None:
            if self.reader is not None and not self.is_obd():
                codes = (self.history[-1][1].get("_codes") if self.history else None) or []
                self.codes_write([("Fiero (ALDL)\n", "h"),
                                  ("Current codes from the data stream: ", None),
                                  (", ".join(f"{c} ({x})" for c, x in codes) or "none", "code"),
                                  ("\n\nThe 1980s ECM sends its codes live, so they're always on the "
                                   "Dashboard and in Analysis. It has no clear command over ALDL; codes "
                                   "clear themselves after the fault is gone and the engine has been "
                                   "started a number of times, or when the ECM loses power.\n", "muted")])
            else:
                messagebox.showinfo(APP_NAME, "Connect to an OBD-II car (or the Grand Prix demo) first.")
            return
        self.codes_write([("Reading codes, readiness, freeze frame and VIN…\n", "muted")])
        src.call(obd_source.codes_report, lambda r: self.q.put(("obd_result", ("report", r))))

    def ask_clear_codes(self):
        if self._obd_src() is None:
            messagebox.showinfo(APP_NAME, "Clearing codes needs an OBD-II connection.")
            return
        self.codes_confirm.pack(fill="x", after=self.codes_read_btn.master)

    def clear_codes(self):
        self.codes_confirm.pack_forget()
        src = self._obd_src()
        if src is None:
            return
        self.log("Clearing trouble codes…")
        src.call(lambda elm: elm.clear_codes(), lambda r: self.q.put(("obd_result", ("clear", r))))

    def codes_result(self, kind, r):
        if isinstance(r, Exception):
            self.codes_write([(f"Couldn't read from the car: {r}\n", "bad")])
            return
        if kind == "clear":
            self.log("Codes cleared." if r else "The car didn't confirm clearing the codes.")
            self.read_codes()
            return
        self.last_codes_report = r
        prof = self.vehicle_profile()
        st = r.get("status") or {}
        parts = [(((prof or {}).get("vehicle", {}).get("name") or "OBD-II car") + "\n", "h"),
                 (f"VIN {r.get('vin') or 'not reported'} · {r.get('protocol', '')}\n", "muted")]
        if st:
            parts.append(("Check engine light: ", None))
            parts.append(("ON\n" if st["mil"] else "off\n", "bad" if st["mil"] else "good"))
        if r.get("fuel_status"):
            parts.append((f"Fuel system: {r['fuel_status']}\n", "muted"))
        for title, key, tag in (("Stored codes", "stored", "bad"), ("Pending codes", "pending", "warn"),
                                ("Permanent codes", "permanent", "warn")):
            codes = r.get(key) or []
            if key == "permanent" and not codes:
                continue
            parts.append((f"\n{title}\n", "h"))
            if not codes:
                parts.append(("None.\n", "good"))
            for code, text in codes:
                parts.append((f"{code}  ", "code"))
                parts.append((f"{text}\n", tag))
                hint = obd_analysis.known_issue_hint(prof, code)
                if hint:
                    parts.append((f"    {hint}\n", "muted"))
        ff = r.get("freeze") or {}
        if ff.get("code"):
            parts.append((f"\nFreeze frame (when {ff['code']} was stored)\n", "h"))
            for name, val in ff.get("values", {}).items():
                parts.append((f"{name}: {val:.1f}\n", None))
        mons = st.get("monitors") or []
        if mons:
            parts.append(("\nReadiness monitors\n", "h"))
            for name, state in mons:
                parts.append((f"{name}: ", None))
                parts.append((f"{state}\n", "good" if state == "complete" else "warn"))
            incomplete = [n for n, s in mons if s != "complete" and n not in obd.CONTINUOUS]
            if st.get("mil"):
                verdict, tag = "Not ready: the check engine light is on.", "bad"
            elif not incomplete:
                verdict, tag = "Ready: every supported monitor is complete.", "good"
            elif len(incomplete) == 1:
                verdict, tag = ("Probably ready: one monitor incomplete (" + incomplete[0] + "). Many "
                                "US inspection programs allow one for 2001+ cars."), "warn"
            else:
                verdict, tag = (f"Not ready yet: {len(incomplete)} monitors incomplete. Drive normally "
                                "for a few days and check again."), "bad"
            parts.append(("\nEmissions inspection: ", "h"))
            parts.append((verdict + "\n", tag))
            parts.append(("Rules vary by state; check yours.\n", "muted"))
        self.codes_write(parts)

    # ------------------------------------------------------------ Checks

    def vehicle_name(self):
        prof = self.vehicle_profile()
        if prof:
            return prof["vehicle"]["name"]
        return "OBD-II car" if self.is_obd() else "Fiero"

    def checks_store(self):
        return checks.Store(CHECKS_DIR, self.vehicle_name())

    def _build_checks_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="Checks")
        self.checks_intro = ttk.Label(tab, wraplength=950, justify="left")
        self.checks_intro.pack(anchor="w")
        form = ttk.Frame(tab)
        form.pack(fill="x", pady=8)
        ttk.Label(form, text="Check:").grid(row=0, column=0, sticky="w")
        self.check_box = ttk.Combobox(form, state="readonly", width=40)
        self.check_box.grid(row=0, column=1, sticky="w", padx=6)
        self.check_box.bind("<<ComboboxSelected>>", lambda e: self._check_how())
        ttk.Label(form, text="Reading:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.check_value = ttk.Entry(form, width=28)
        self.check_value.grid(row=0, column=3, sticky="w", padx=6)
        self.check_unit = ttk.Label(form, width=9)
        self.check_unit.grid(row=0, column=4, sticky="w")
        ttk.Label(form, text="Note:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.check_note = ttk.Entry(form, width=70)
        self.check_note.grid(row=1, column=1, columnspan=3, sticky="we", padx=6, pady=(6, 0))
        ttk.Button(form, text="Save", command=self.save_check).grid(row=1, column=4, sticky="w",
                                                                    pady=(6, 0))
        self.check_how = ttk.Label(tab, wraplength=950, justify="left", foreground=self.c["muted"])
        self.check_how.pack(anchor="w")
        cols = ("when", "check", "reading", "verdict", "explanation")
        self.check_tree = ttk.Treeview(tab, columns=cols, show="headings", height=12)
        for c, w, text in (("when", 130, "When"), ("check", 230, "Check"), ("reading", 150, "Reading"),
                           ("verdict", 80, "Result"), ("explanation", 420, "What it means")):
            self.check_tree.heading(c, text=text)
            self.check_tree.column(c, width=w, anchor="w")
        self.check_tree.pack(fill="both", expand=True, pady=6)
        self.check_tree.bind("<<TreeviewSelect>>", lambda e: self._show_check())
        ttk.Button(tab, text="Delete selected", command=self.delete_check).pack(anchor="e")
        self.refresh_checks()

    def _check_list(self):
        return checks.for_kind("obd2" if self.is_obd() else "aldl")

    def _check_how(self):
        c = next((c for c in self._check_list() if c.name == self.check_box.get()), None)
        self.check_how.configure(text=c.how if c else "")
        self.check_unit.configure(text=c.unit if c else "")

    def _show_check(self):
        sel = self.check_tree.selection()
        if sel:
            v = self.check_tree.item(sel[0], "values")
            self.check_how.configure(text=f"{v[1]}: {v[2]}  →  {v[3]}. {v[4]}")

    def refresh_checks(self):
        if not hasattr(self, "check_tree"):
            return
        names = [c.name for c in self._check_list()]
        self.check_box.configure(values=names)
        if self.check_box.get() not in names:
            self.check_box.set(names[0])
        self._check_how()
        self.checks_intro.configure(text=(
            f"Readings you take by hand for the {self.vehicle_name()}: fuel pressure, timing, "
            "compression, a vacuum gauge, a multimeter. Each is compared with this car's normal "
            "values and saved, so Claude and the session report can use them."))
        self.check_tree.delete(*self.check_tree.get_children())
        for e in self.checks_store().entries():
            reading = " — ".join(x for x in (f"{e['value']} {e['unit']}".strip() if e["value"] else "",
                                              e.get("note", "")) if x)
            self.check_tree.insert("", "end", values=(e["when"], e["name"], reading,
                                                      e["verdict"].upper(), e["explanation"]))

    def save_check(self):
        c = next((c for c in self._check_list() if c.name == self.check_box.get()), None)
        value, note = self.check_value.get().strip(), self.check_note.get().strip()
        if c is None or (not value and not note):
            return
        if c.id == "note" and not note:
            note, value = value, ""
        entry = self.checks_store().add(c.id, value, note, self.vehicle_profile())
        self.log(f"Check saved: {entry['name']} {entry['value']} -> {entry['verdict']}")
        self.check_value.delete(0, "end")
        self.check_note.delete(0, "end")
        self.refresh_checks()
        self.refresh_history()

    def delete_check(self):
        sel = self.check_tree.selection()
        if not sel:
            return
        index = self.check_tree.index(sel[0])
        self.checks_store().delete(index)
        self.refresh_checks()

    # ------------------------------------------------------------ Report

    def report_data(self):
        hist = list(self.history)
        prof = self.vehicle_profile()
        if hist:
            if self.is_obd():
                found = obd_analysis.analyze(hist, prof, window=None)
            else:
                found = analysis.analyze(hist, self.params_by_name(), window=None)
            found = found + vacuum.findings(hist)
        else:
            found = []
        names = [v.get() for v, _cb in self.chart_vars if v.get()]
        for extra in ("Engine RPM", vacuum.NAME):
            if extra not in names:
                names.append(extra)
        units = {p["name"]: p.get("units", "") for p in self.definition["parameters"]}
        charts = [(n, units.get(n, ""), [(t, v.get(n)) for t, v in hist]) for n in names]
        codes = ""
        if hasattr(self, "codes_text"):
            codes = self.codes_text.get("1.0", "end").strip()
            if codes.startswith(("Connect to", "Reading codes")):
                codes = ""
        if not codes and hist:
            cur = hist[-1][1].get("_codes") or []
            codes = "Current codes: " + (", ".join(f"{c} ({x})" for c, x in cur) or "none")
        tests = [(name, res.status.upper(), res.headline, res.details)
                 for name, res, _h in self.test_results.values()]
        done = [v for v in self.leak_verdicts if v.reacted != "waiting"]
        leak = [(v.spot, v.reacted, " ".join(v.details)) for v in done]
        comp = None
        if getattr(self, "last_compare", None):
            b, a, rows, notes = self.last_compare
            comp = (f"{b} → {a}: " + compare.verdict_text(rows),
                    [(r.name, compare.fmt(r.before, r.unit), compare.fmt(r.after, r.unit), r.unit,
                      r.verdict if r.verdict in (compare.BETTER, compare.WORSE) else "")
                     for r in rows], notes)
        events = self.history_events()
        hist_rows = [(f"{e.when:%Y-%m-%d %H:%M}" if e.when.year > 1 else "?", e.kind, e.name,
                      e.status, e.summary) for e in events]
        trends = [f"{n}: {a.upper()} → {b.upper()}" for n, a, b, _, _ in history.status_changes(events)]
        trends += [f"{n} ({k}): {a:g} → {b:g} over {c} runs" for n, k, a, b, c, _, _ in history.trends(events)]
        return {"vehicle": self.vehicle_name(), "source": self.source_var.get(),
                "findings": [(f.severity, f.title, f.detail, f.hint) for f in found],
                "charts": charts if hist else [], "codes": codes, "tests": tests,
                "leak": leak, "leak_summary": leak_hunt.summary(done) if done else "",
                "compare": comp, "history": hist_rows, "trends": trends,
                "claude": list(self.claude_log)}

    def save_report(self, open_it=True):
        try:
            path = report.save(REPORTS_DIR, self.report_data())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save the report:\n{e}")
            return None
        self.log(f"Saved report: {path}")
        if open_it:
            open_folder(path)
        return path

    # ------------------------------------------------------------ History

    def _build_history_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="History")
        self.hist_title = ttk.Label(tab, font=("Helvetica", 14, "bold"))
        self.hist_title.pack(anchor="w")
        ttk.Label(tab, foreground=self.c["muted"], wraplength=950, justify="left", text=(
            "Every guided test and hands-on check for this car, oldest first. Run the same "
            "tests after a repair to see what changed.")).pack(anchor="w", pady=(2, 6))
        cols = ("when", "kind", "name", "status", "summary")
        self.hist_tree = ttk.Treeview(tab, columns=cols, show="headings", height=12)
        for c, w, t in (("when", 130, "When"), ("kind", 60, "Type"), ("name", 230, "Test / check"),
                        ("status", 80, "Result"), ("summary", 480, "Summary")):
            self.hist_tree.heading(c, text=t)
            self.hist_tree.column(c, width=w, anchor="w")
        self.hist_tree.pack(fill="both", expand=True)
        self.hist_trends = tk.Text(tab, height=8, wrap="word", foreground=INK, background="white",
                                   relief="flat", font=("Helvetica", 13), padx=10, pady=8)
        self.hist_trends.pack(fill="x", pady=(8, 0))
        self.hist_trends.tag_configure("b", font=("Helvetica", 13, "bold"))
        self.refresh_history()

    def history_events(self):
        return history.load(TESTS_DIR, CHECKS_DIR, self.vehicle_name(), self.is_obd())

    def history_text(self):
        return history.text(self.history_events())

    def refresh_history(self):
        if not hasattr(self, "hist_tree"):
            return
        events = self.history_events()
        self.hist_title.configure(text=f"History: {self.vehicle_name()}  ({len(events)} entries)")
        self.hist_tree.delete(*self.hist_tree.get_children())
        for ev in events:
            self.hist_tree.insert("", "end", values=(
                ev.when.strftime("%Y-%m-%d %H:%M") if ev.when.year > 1 else "?", ev.kind,
                ev.name, ev.status.upper(), ev.summary))
        if events:
            self.hist_tree.see(self.hist_tree.get_children()[-1])
        t = self.hist_trends
        t.configure(state="normal")
        t.delete("1.0", "end")
        changes, trends = history.status_changes(events), history.trends(events)
        if not changes and not trends:
            t.insert("end", "Trends show up here once a test or check has been done more than once.")
        for name, a, b, wa, wb in changes:
            t.insert("end", f"{name}: ", "b")
            t.insert("end", f"{a.upper()} on {wa:%b %d} → {b.upper()} on {wb:%b %d}\n")
        for name, k, a, b, n, wa, wb in trends:
            t.insert("end", f"{name} ({k}): ", "b")
            t.insert("end", f"{a:g} → {b:g} over {n} runs ({wa:%b %d} to {wb:%b %d})\n")
        t.configure(state="disabled")

    # ------------------------------------------------------------ Leak hunt

    def _build_leak_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="Leak hunt")
        self.leak_marks = []
        self.leak_verdicts = []
        ttk.Label(tab, wraplength=900, justify="left", text=(
            "Find a vacuum leak with the engine idling warm. Spray a short burst of carb cleaner "
            "(or propane from an unlit torch) at ONE spot, then press 'I sprayed here'. Wait "
            "about 10 seconds before the next spot. If the engine sucks the spray in, the O2 "
            "sensor goes rich, the computer pulls fuel and the idle changes; the app checks "
            "each spot for that. Keep spray away from hot exhaust parts and anything sparking, "
            "and have a fire extinguisher handy.")).pack(anchor="w")
        self.leak_vac = tk.StringVar(value="Vacuum now: --")
        ttk.Label(tab, textvariable=self.leak_vac, font=("Helvetica", 16, "bold")).pack(
            anchor="w", pady=(8, 4))
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Spot:").pack(side="left")
        self.leak_spot = ttk.Combobox(row, width=42)
        self.leak_spot.pack(side="left", padx=6)
        ttk.Button(row, text="I sprayed here", command=self.mark_spray).pack(side="left")
        ttk.Button(row, text="Start over", command=self.clear_leaks).pack(side="left", padx=6)
        cols = ("spot", "when", "result", "details")
        self.leak_tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
        for c, w, text in (("spot", 260, "Spot"), ("when", 80, "Sprayed"),
                           ("result", 90, "Reacted?"), ("details", 520, "What changed")):
            self.leak_tree.heading(c, text=text)
            self.leak_tree.column(c, width=w, anchor="w")
        self.leak_tree.pack(fill="both", expand=True, pady=6)
        self.leak_summary = tk.StringVar(value=leak_hunt.summary([]))
        ttk.Label(tab, textvariable=self.leak_summary, wraplength=900, justify="left",
                  font=("Helvetica", 13)).pack(anchor="w")
        self.update_leak_spots()

    def update_leak_spots(self):
        if hasattr(self, "leak_spot"):
            self.leak_spot.configure(values=leak_hunt.SPOTS["obd2" if self.is_obd() else "aldl"])

    def mark_spray(self):
        spot = self.leak_spot.get().strip()
        if not spot:
            self.leak_summary.set("Pick or type the spot you sprayed first.")
            return
        if not self.reader:
            self.leak_summary.set("Connect to the car (or a demo) first.")
            return
        t = time.time() - self.t0
        self.leak_marks.append(leak_hunt.Mark(t, spot))
        src = getattr(self.reader, "src", None)
        if src is not None and hasattr(src, "spray"):
            src.spray(spot)                 # demo engines react to their leaky spot
        self.log(f"Leak hunt: sprayed {spot}")
        self.refresh_leaks()

    def clear_leaks(self):
        self.leak_marks, self.leak_verdicts = [], []
        self.refresh_leaks()

    def refresh_leaks(self):
        hist = list(self.history)
        self.leak_verdicts = [leak_hunt.judge(hist, m) for m in self.leak_marks]
        self.leak_tree.delete(*self.leak_tree.get_children())
        for v in self.leak_verdicts:
            self.leak_tree.insert("", "end", values=(
                v.spot, time.strftime("%H:%M:%S", time.localtime(self.t0 + v.t)),
                {"yes": "YES", "maybe": "maybe", "no": "no", "waiting": "…"}[v.reacted],
                " ".join(v.details)))
        done = [v for v in self.leak_verdicts if v.reacted != "waiting"]
        waiting = len(self.leak_verdicts) - len(done)
        text = leak_hunt.summary(done) if done else leak_hunt.summary([])
        if waiting:
            text = "Watching the last spray… " + ("" if not done else text)
        self.leak_summary.set(text)

    def leak_hunt_text(self):
        """Plain-text results for Claude and the report."""
        done = [v for v in self.leak_verdicts if v.reacted != "waiting"]
        if not done:
            return ""
        lines = [f"- {v.spot}: {v.reacted} ({' '.join(v.details)})" for v in done]
        return "Leak hunt (spray test) results:\n" + "\n".join(lines) + "\n" + leak_hunt.summary(done)

    # ------------------------------------------------------------ Tests

    def _build_tests_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.tests_tab = tab
        self.nb.add(tab, text="Tests")
        self.runner = None
        self.test_results = {}          # test id -> (name, Result, history)
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Guided tests", font=("Helvetica", 14, "bold")).pack(anchor="w")
        self.test_tree = ttk.Treeview(left, show="tree", height=18, selectmode="browse")
        self.test_tree.column("#0", width=250)
        self.test_tree.pack(fill="y", expand=True, pady=(6, 0))
        self.test_tree.bind("<<TreeviewSelect>>", lambda e: self.show_test())

        right = ttk.Frame(tab, padding=(16, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self.test_title = ttk.Label(right, font=("Helvetica", 16, "bold"))
        self.test_title.pack(anchor="w")
        self.test_cond = ttk.Label(right, foreground=self.c["muted"])
        self.test_cond.pack(anchor="w")
        self.test_summary = ttk.Label(right, wraplength=640, justify="left")
        self.test_summary.pack(anchor="w", pady=(6, 8), fill="x")
        btns = ttk.Frame(right)
        btns.pack(fill="x")
        self.test_start = ttk.Button(btns, text="Start test", command=self.start_test)
        self.test_start.pack(side="left")
        self.test_next = ttk.Button(btns, text="Next step", command=self.next_test_step)
        self.test_next.pack(side="left", padx=6)
        self.test_finish = ttk.Button(btns, text="Finish", command=self.finish_test)
        self.test_finish.pack(side="left")
        self.test_cancel = ttk.Button(btns, text="Cancel", command=self.cancel_test)
        self.test_cancel.pack(side="left", padx=6)
        self.test_ask = ttk.Button(btns, text="Ask Claude about this result",
                                   command=self.ask_claude_about_test)
        self.test_ask.pack(side="right")
        self.test_progress = ttk.Label(right, font=("Helvetica", 13, "bold"), foreground=self.c["warn"])
        self.test_progress.pack(anchor="w", pady=(10, 4))
        self.test_text = tk.Text(right, wrap="word", foreground=INK, insertbackground=INK, font=("Helvetica", 13), padx=12, pady=10,
                                 background="white", relief="flat", height=16, cursor="arrow")
        self.test_text.pack(fill="both", expand=True)
        for sev, color in ((guided_tests.PASS, "#1e8449"), (guided_tests.WATCH, "#b9770e"),
                           (guided_tests.FAIL, "#c0392b"), (guided_tests.INCONCLUSIVE, "#5d6d7e")):
            self.test_text.tag_configure(sev, foreground=color, font=("Helvetica", 14, "bold"))
        self.test_text.tag_configure("cur", background="#fff2a8", foreground=INK,
                                     font=("Helvetica", 13, "bold"))
        self.test_text.tag_configure("done", foreground="#8a8f98")
        self.test_text.tag_configure("h", font=("Helvetica", 13, "bold"), spacing1=6)
        self.test_text.tag_configure("li", lmargin1=14, lmargin2=26)
        self.test_text.configure(state="disabled")
        self.populate_tests()
        self.root.after(250, self.test_tick)

    def current_tests(self):
        return obd_tests.ALL_TESTS if self.is_obd() else guided_tests.ALL_TESTS

    def populate_tests(self):
        if self.runner is not None and not self.runner.done:
            return
        self.test_tree.delete(*self.test_tree.get_children())
        groups = {}
        for t in self.current_tests():
            if t.condition not in groups:
                groups[t.condition] = self.test_tree.insert("", "end", text=t.condition, open=True)
            self.test_tree.insert(groups[t.condition], "end", iid=t.id, text=t.name)
        self.test_tree.selection_set(self.current_tests()[0].id)
        self.show_test()

    def selected_test(self):
        sel = self.test_tree.selection()
        return next((t for t in self.current_tests() if sel and t.id == sel[0]), None)

    def _test_buttons(self):
        running = self.runner is not None and not self.runner.done
        self.test_start.configure(state="disabled" if running else "normal")
        for b in (self.test_next, self.test_finish, self.test_cancel):
            b.configure(state="normal" if running else "disabled")
        t = self.selected_test()
        self.test_ask.configure(state="normal" if (t and t.id in self.test_results
                                                   and not running) else "disabled")

    def show_test(self):
        if self.runner is not None and not self.runner.done:
            return
        t = self.selected_test()
        if t is None:
            return
        self.test_title.configure(text=t.name)
        self.test_cond.configure(text=f"{t.condition} · {len(t.steps)} step"
                                      f"{'s' if len(t.steps) > 1 else ''}")
        self.test_summary.configure(text=t.summary)
        self.test_progress.configure(text="")
        self.render_test_text(t, None, self.test_results.get(t.id, (None, None, None))[1])
        self._test_buttons()

    def render_test_text(self, test, current_step, result):
        tx = self.test_text
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        tx.insert("end", "Steps\n", "h")
        for i, step in enumerate(test.steps):
            tag = ("cur" if i == current_step
                   else "done" if current_step is not None and i < current_step else "li")
            tx.insert("end", f"{i + 1}. {step.text}\n", (tag, "li") if tag != "li" else "li")
        if result is not None:
            tx.insert("end", "\nResult\n", "h")
            tx.insert("end", f" {result.status.upper()} ", result.status)
            tx.insert("end", f"  {result.headline}\n", "h")
            for line in result.details:
                tx.insert("end", f"• {line}\n", "li")
        tx.configure(state="disabled")

    def start_test(self):
        t = self.selected_test()
        if t is None:
            return
        if self.reader is None:
            messagebox.showinfo(APP_NAME, "Connect to the car (or the demo) first, then start "
                                          "the test.")
            return
        now = time.time() - self.t0
        self.runner = guided_tests.Runner(t, now)
        self._demo_mode(t.steps[0].demo_mode)
        self.log(f"Started test: {t.name}")
        self.render_test_text(t, 0, None)
        self._test_buttons()

    def _demo_mode(self, mode):
        src = getattr(self.reader, "src", None) if self.reader else None
        if src is not None and hasattr(src, "set_mode"):
            src.set_mode(mode)

    def next_test_step(self):
        if self.runner and not self.runner.done:
            self.runner.next_step(time.time() - self.t0)
            self._after_step_change()

    def finish_test(self):
        if self.runner and not self.runner.done:
            self.runner.done = True
            self._after_step_change()

    def cancel_test(self):
        if self.runner and not self.runner.done:
            self.log(f"Cancelled test: {self.runner.test.name}")
            self.runner = None
            self._demo_mode("idle")
            self.show_test()

    def _after_step_change(self):
        r = self.runner
        if r.done:
            self.complete_test()
        else:
            self._demo_mode(r.current.demo_mode)
            self.render_test_text(r.test, r.step, None)

    def test_tick(self):
        try:
            r = self.runner
            if r is not None and not r.done:
                now = time.time() - self.t0
                before = r.step
                r.tick(now)
                if r.done or r.step != before:
                    self._after_step_change()
                else:
                    left = r.time_left(now)
                    self.test_progress.configure(
                        text=f"Step {r.step + 1} of {len(r.test.steps)} · "
                             f"{int(left // 60)}:{int(left % 60):02d} left · "
                             f"{len(r.samples)} readings recorded")
            elif r is not None and r.done and r.samples and getattr(r, "_completed", False) is False:
                self.complete_test()          # finished by stop_when (e.g. a stall)
        finally:
            self.root.after(250, self.test_tick)

    def complete_test(self):
        r = self.runner
        if r is None or getattr(r, "_completed", False):
            return
        r._completed = True
        result = r.result(self.params_by_name(), {"profile": self.vehicle_profile()})
        hist = [(t, v) for t, v, _s in r.samples]
        self.test_results[r.test.id] = (r.test.name, result, hist)
        self._demo_mode("idle")
        if r.test.id == "koeo" and result.numbers.get("baro_map_v"):
            try:
                with open(BASELINE_PATH, "w", encoding="utf-8") as f:
                    json.dump({"baro_map_v": result.numbers["baro_map_v"],
                               "saved": datetime.now().isoformat(timespec="seconds")}, f, indent=2)
            except OSError:
                pass
        self.save_test_result(r.test, result, r.samples)
        self.test_progress.configure(text=f"Finished · {len(r.samples)} readings")
        self.log(f"Test finished: {r.test.name}: {result.status.upper()} — {result.headline}")
        self.test_tree.selection_set(r.test.id)
        self.render_test_text(r.test, None, result)
        self._test_buttons()

    def save_test_result(self, test, result, samples):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(TESTS_DIR, f"{test.id}-{stamp}.json")
        rows = [{"t": round(t, 2), "step": s,
                 **{k: v for k, v in vals.items() if not k.startswith("_")},
                 "codes": [c for c, _ in vals.get("_codes") or []],
                 "flags": {k: v for k, v in (vals.get("_flags") or {}).items() if v}}
                for t, vals, s in samples]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"test": test.name, "id": test.id, "when": stamp,
                           "vehicle": self.vehicle_name(),
                           "status": result.status, "headline": result.headline,
                           "details": result.details, "numbers": result.numbers,
                           "steps": [st.text for st in test.steps], "samples": rows},
                          f, indent=1, default=str)
            self.log(f"Saved test result: {path}")
            self.refresh_history()
        except OSError as e:
            self.log(f"Couldn't save the test result: {e}")

    def recent_results(self):
        return [(name, res) for name, res, _h in self.test_results.values()]

    def ask_claude_about_test(self):
        t = self.selected_test()
        if t is None or t.id not in self.test_results:
            return
        name, result, hist = self.test_results[t.id]
        summary = claude_assistant.summary_for_test(name, result, hist, self.params_by_name(),
                                                    self.baseline_baro())
        summary += self.extra_context()
        self.nb.select(self.claude_tab)
        self.claude_start(f"[test: {name}, {result.status.upper()}] Explain this result.",
                          claude_assistant.test_question(summary, name))

    # -------------------------------------------------------- Ask Claude

    def _build_claude_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.claude_tab = tab
        self.claude_log = []                     # (question shown, answer) for the report
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
        self.cl_cost = ttk.Label(top, foreground=self.c["muted"])
        self.cl_cost.pack(side="right", padx=10)
        ttk.Label(tab, foreground=self.c["muted"], wraplength=950, text=(
            "Sends a summary of your readings (values, trouble codes, ECM status and the "
            "Analysis findings) to Anthropic's Claude API and shows its diagnosis. Needs your "
            "own API key from console.anthropic.com; each question is billed to that key, "
            "usually a few cents. Nothing is sent until you click a button. Claude can be "
            "wrong: double-check before replacing parts.")).pack(fill="x", pady=(4, 6))
        box = ttk.Frame(tab)
        box.pack(fill="both", expand=True)
        self.cl_text = tk.Text(box, wrap="word", foreground=INK, insertbackground=INK, font=("Helvetica", 13), padx=12, pady=10,
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
                prof = self.vehicle_profile()
                self.claude_conv = claude_assistant.Conversation(
                    api_key=key, system=claude_assistant.system_prompt_for(prof, self.is_obd()))
            except Exception as e:  # SDK missing or bad configuration
                messagebox.showerror(APP_NAME, f"Couldn't start the Claude client:\n{e}")
                return
        self.claude_set_busy(True)
        self.claude_shown = shown
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
        if answer:
            self.claude_log.append((getattr(self, "claude_shown", ""), answer))
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
        summary = claude_assistant.summary_for_live(self.history, self.params_by_name(),
                                                    self.baseline_baro(), self.recent_results())
        summary += self.extra_context()
        self.claude_start(f"[live data: last {len(self.history)} frames] What do you see, and "
                          "what should I check?", claude_assistant.first_question(summary, self._car_name()))

    def claude_ask_log(self):
        path = filedialog.askopenfilename(
            title="Choose a log for Claude", initialdir=LOG_DIR,
            filetypes=[("CSV logs", "*.csv"), ("All files", "*")])
        if not path:
            return
        try:
            summary = claude_assistant.summary_for_log(path, self.params_by_name(),
                                                       self.baseline_baro(), self.recent_results())
            summary += self.extra_context()
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror(APP_NAME, f"Couldn't read that log:\n{e}")
            return
        self.claude_start(f"[log: {os.path.basename(path)}] What do you see, and what should "
                          "I check?", claude_assistant.first_question(summary, self._car_name()))

    def extra_context(self):
        """Hands-on checks, leak hunt and comparison results, for Claude."""
        parts = [x for x in (self.history_text(), self.leak_hunt_text(),
                             self.compare_text()) if x]
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def _car_name(self):
        if not self.is_obd():
            return None
        return (self.vehicle_profile() or {}).get("vehicle", {}).get("name") or "car"

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


class VehiclesWindow:
    """Browse vehicle profiles, research a new one with Claude, or import one."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Vehicles")
        self.win.geometry("980x640")
        self.win.transient(app.root)
        outer = ttk.Frame(self.win, padding=12)
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Vehicle profiles", font=("Helvetica", 14, "bold")).pack(anchor="w")
        self.lst = tk.Listbox(left, width=34, height=14, foreground=INK, background="white",
                              exportselection=False)
        self.lst.pack(fill="y", expand=True, pady=6)
        self.lst.bind("<<ListboxSelect>>", lambda e: self.show())
        for text, cmd in (("Use this vehicle", self.use),
                          ("Research a vehicle with Claude…", self.research),
                          ("Import a profile file…", self.import_file),
                          ("Paste a profile from Claude…", self.paste),
                          ("Copy research request for Claude chat…", self.copy_prompt),
                          ("Open profiles folder", lambda: open_folder(VEHICLES_DIR))):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=2)
        right = ttk.Frame(outer, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self.txt = tk.Text(right, wrap="word", font=("Helvetica", 13), padx=12, pady=10,
                           background="white", foreground=INK, relief="flat")
        sb = ttk.Scrollbar(right, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("h1", font=("Helvetica", 17, "bold"))
        self.txt.tag_configure("h2", font=("Helvetica", 14, "bold"), spacing1=10)
        self.txt.tag_configure("k", font=("Helvetica", 13, "bold"))
        self.txt.tag_configure("muted", foreground="#5f6670")
        self.txt.tag_configure("conf", foreground="#2a6fdb", font=("Helvetica", 11, "bold"))
        self.txt.tag_configure("src", foreground="#2a6fdb", font=("Helvetica", 11))
        self.reload()

    def reload(self, select_name=None):
        self.items = vehicle_profile.list_profiles(VEHICLES_DIR)
        self.lst.delete(0, "end")
        current = self.app.active_vehicle_name()
        for path, p in self.items:
            mark = "✓ " if p["vehicle"]["name"] == current else "   "
            mine = "" if path.startswith(vehicle_profile.BUILTIN_DIR) else "  (imported)"
            self.lst.insert("end", f"{mark}{p['vehicle']['name']}{mine}")
        want = select_name or current
        idx = next((i for i, (_, p) in enumerate(self.items) if p["vehicle"]["name"] == want), 0)
        if self.items:
            self.lst.selection_set(idx)
            self.show()

    def selected(self):
        sel = self.lst.curselection()
        return self.items[sel[0]][1] if sel else None

    def show(self):
        p = self.selected()
        if not p:
            return
        t = self.txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        v, c = p["vehicle"], p["connection"]
        t.insert("end", v["name"] + "\n", "h1")
        t.insert("end", f"{v.get('engine', '')} {('· ' + v['engine_code']) if v.get('engine_code') else ''}\n", "muted")
        t.insert("end", f"Connection: {c.get('protocol', c['type'])}\n", "muted")
        if c.get("notes"):
            t.insert("end", c["notes"] + "\n", "muted")
        if c["type"] == "obd2":
            t.insert("end", "\nLive OBD-II connection for this vehicle is being added next; "
                            "the profile is ready for it.\n", "k")
        t.insert("end", "\nNormal values\n", "h2")
        for key, n in p["normals"].items():
            rng = vehicle_profile_range(n)
            t.insert("end", f"{n['label']}: ", "k")
            t.insert("end", f"{rng}  ({n['condition']})  ")
            t.insert("end", vehicle_profile.CONFIDENCE.get(n["confidence"], n["confidence"]) + "\n", "conf")
            t.insert("end", n["note"] + "\n", "muted")
            for s in n.get("sources", []):
                t.insert("end", "  " + s + "\n", "src")
        if p.get("known_issues"):
            t.insert("end", "\nKnown problems\n", "h2")
            for k in p["known_issues"]:
                t.insert("end", k["title"] + "\n", "k")
                for label, field in (("Symptoms", "symptoms"), ("In the data", "what_data_shows"),
                                     ("Check", "checks")):
                    if k.get(field):
                        t.insert("end", f"{label}: {k[field]}\n")
                for s in k.get("sources", []):
                    t.insert("end", "  " + s + "\n", "src")
        if p.get("enhanced_pids"):
            t.insert("end", "\nExtra data codes (mode 22)\n", "h2")
            for e in p["enhanced_pids"]:
                t.insert("end", f"{e['name']}: 22 {e['pid']} → {e['formula']} {e.get('unit', '')}\n")
        r = p.get("research", {})
        if r:
            t.insert("end", "\nResearch notes\n", "h2")
            t.insert("end", f"{r.get('by', '')} {r.get('date', '')}: {r.get('notes', '')}\n", "muted")
        t.configure(state="disabled")

    def use(self):
        p = self.selected()
        if not p:
            return
        app = self.app
        app.save_settings(vehicle=p["vehicle"]["name"])
        if app.reader is None:              # pick the matching kind of connection
            if p["connection"]["type"] == "obd2" and not app.is_obd():
                app.source_var.set(SRC_OBD)
            elif p["connection"]["type"] == "aldl" and app.is_obd():
                app.source_var.set(SRC_CABLE)
        app.on_source_change()
        self.reload()

    def _saved(self, dest, prof):
        self.app.log(f"Saved vehicle profile: {dest}")
        self.reload(select_name=prof["vehicle"]["name"])

    def import_file(self):
        path = filedialog.askopenfilename(parent=self.win, title="Choose a vehicle profile",
                                          filetypes=[("Vehicle profile", "*.json"), ("All files", "*")])
        if not path:
            return
        try:
            dest, prof = vehicle_profile.import_profile(path, VEHICLES_DIR)
        except (OSError, vehicle_profile.ProfileError) as e:
            messagebox.showerror(APP_NAME, f"That file isn't a valid vehicle profile:\n\n{e}",
                                 parent=self.win)
            return
        self._saved(dest, prof)

    def paste(self):
        win = tk.Toplevel(self.win)
        win.title("Paste a vehicle profile")
        win.geometry("720x480")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, wraplength=680, text="Paste Claude's whole reply (or just the JSON) "
                  "below, then click Import.").pack(anchor="w")
        box = tk.Text(frm, wrap="word", font=("Menlo", 11), background="white", foreground=INK)
        box.pack(fill="both", expand=True, pady=8)

        def do_import():
            try:
                prof = vehicle_profile.validate(vehicle_profile.extract_json(box.get("1.0", "end")))
            except vehicle_profile.ProfileError as e:
                messagebox.showerror(APP_NAME, f"That isn't a valid vehicle profile:\n\n{e}",
                                     parent=win)
                return
            dest = os.path.join(VEHICLES_DIR, vehicle_profile.safe_filename(prof))
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(prof, f, indent=2)
            win.destroy()
            self._saved(dest, prof)
        ttk.Button(frm, text="Import", command=do_import).pack(anchor="e")

    def _car_fields(self, parent):
        fields = {}
        for label, key, width in (("Year", "year", 8), ("Make", "make", 14), ("Model", "model", 22),
                                  ("Engine", "engine", 22)):
            ttk.Label(parent, text=label).pack(side="left")
            e = ttk.Entry(parent, width=width)
            e.pack(side="left", padx=(4, 10))
            fields[key] = e
        return fields

    def copy_prompt(self):
        win = tk.Toplevel(self.win)
        win.title("Research request for Claude chat")
        win.geometry("820x560")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        row = ttk.Frame(frm)
        row.pack(fill="x")
        f = self._car_fields(row)
        ttk.Label(frm, wraplength=780, foreground="#5f6670", text=(
            "Fill in the car, click Make request, then Copy. Paste it into a Claude chat "
            "(claude.ai, with web search on). When Claude replies, use 'Paste a profile from "
            "Claude…' to import it.")).pack(anchor="w", pady=6)
        box = tk.Text(frm, wrap="word", font=("Menlo", 11), background="white", foreground=INK)
        box.pack(fill="both", expand=True)

        def make():
            box.delete("1.0", "end")
            box.insert("1.0", vehicle_profile.research_prompt(
                f["year"].get().strip(), f["make"].get().strip(), f["model"].get().strip(),
                f["engine"].get().strip()))

        def copy():
            win.clipboard_clear()
            win.clipboard_append(box.get("1.0", "end").strip())
            copy_btn.configure(text="Copied")
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Make request", command=make).pack(side="left")
        copy_btn = ttk.Button(btns, text="Copy", command=copy)
        copy_btn.pack(side="left", padx=6)

    def research(self):
        key = self.app.claude_api_key()
        if not key:
            if messagebox.askyesno(APP_NAME, "Researching a vehicle uses Claude and needs your "
                                   "Anthropic API key. Add one now?", parent=self.win):
                self.app.claude_key_dialog()
            return
        win = tk.Toplevel(self.win)
        win.title("Research a vehicle with Claude")
        win.geometry("760x300")
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        row = ttk.Frame(frm)
        row.pack(fill="x")
        f = self._car_fields(row)
        ttk.Label(frm, wraplength=720, foreground="#5f6670", text=(
            "Claude searches the web for this car's normal values, known problems and OBD "
            "details, with sources, and builds a profile. It takes a few minutes and is billed "
            "to your API key (usually under $2).")).pack(anchor="w", pady=8)
        status = tk.StringVar(value="")
        ttk.Label(frm, textvariable=status, font=("Helvetica", 13, "bold")).pack(anchor="w")
        go = ttk.Button(frm, text="Start research")
        go.pack(anchor="e", pady=(10, 0))
        q = queue.Queue()

        def start():
            year, make_, model = (f[k].get().strip() for k in ("year", "make", "model"))
            if not (year and make_ and model):
                messagebox.showinfo(APP_NAME, "Enter at least the year, make and model.", parent=win)
                return
            go.configure(state="disabled")
            status.set("Starting…")
            engine = f["engine"].get().strip()

            def work():
                try:
                    import anthropic
                    client = anthropic.Anthropic(api_key=key)
                    prof, cost = vehicle_profile.research_with_claude(
                        client, year, make_, model, engine, on_progress=lambda t: q.put(("note", t)))
                    q.put(("done", (prof, cost)))
                except vehicle_profile.ProfileError as e:
                    q.put(("error", f"Claude's profile couldn't be used: {e}"))
                except Exception as e:  # noqa: BLE001 - shown to the user
                    q.put(("error", claude_assistant.describe_error(e)))
            threading.Thread(target=work, daemon=True).start()
            poll()

        def poll():
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "note":
                        status.set(val)
                    elif kind == "error":
                        status.set("")
                        go.configure(state="normal")
                        messagebox.showerror(APP_NAME, val, parent=win)
                        return
                    elif kind == "done":
                        prof, cost = val
                        dest = os.path.join(VEHICLES_DIR, vehicle_profile.safe_filename(prof))
                        with open(dest, "w", encoding="utf-8") as fh:
                            json.dump(prof, fh, indent=2)
                        win.destroy()
                        self._saved(dest, prof)
                        messagebox.showinfo(APP_NAME, f"Profile for {prof['vehicle']['name']} "
                                            f"saved (about ${cost:.2f}). Check the values and "
                                            "sources before relying on them.", parent=self.win)
                        return
            except queue.Empty:
                pass
            win.after(250, poll)
        go.configure(command=start)


def vehicle_profile_range(n):
    lo, hi, unit = n.get("low"), n.get("high"), n.get("unit", "")
    unit = {"F": "°F"}.get(unit, unit)
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} {unit}" if lo != hi else f"{lo:g} {unit}"
    if lo is not None:
        return f"≥ {lo:g} {unit}"
    if hi is not None:
        return f"≤ {hi:g} {unit}"
    return "see note"


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
        profiles = len(vehicle_profile.list_profiles(VEHICLES_DIR))
        result["ok"] = (app.frames >= 3 and shown not in ("--", "") and rows >= 2
                        and bool(analysed) and sdk.startswith("yes")
                        and "Engine RPM:" in summary and profiles >= 2)
        result["line"] = (f"self-test: frames={app.frames} rpm_tile={shown} logged_rows={rows} "
                          f"log_analysis={'yes' if analysed else 'no'} claude_sdk={sdk} "
                          f"profiles={profiles}")
        # Run one guided test end to end through the Tests tab.
        app.test_tree.selection_set("cold_sensors")
        app.show_test()
        app.start_test()
        root.after(10000, finish_test)

    def finish_test():
        res = app.test_results.get("cold_sensors")
        result["status"] = res[1].status if res else "not finished"
        result["ok"] = result["ok"] and result["status"] == guided_tests.PASS
        # OBD-II: the Grand Prix demo through the real ELM327 driver.
        app.disconnect()
        app.source_var.set(SRC_DEMO_OBD)
        app.on_source_change()
        app.connect()
        root.after(8000, obd_read_codes)

    def obd_read_codes():
        app.read_codes()
        root.after(3000, finish_obd)

    def finish_obd():
        rpm = app.tiles.get("Engine RPM")
        shown = rpm["value"].cget("text") if rpm else "--"
        codes = app.codes_text.get("1.0", "end")
        obd_ok = shown not in ("--", "") and "P0171" in codes and "Readiness monitors" in codes
        rpath = app.save_report(open_it=False)
        with open(rpath, encoding="utf-8") as f:
            page = f.read()
        rep_ok = "<svg" in page and "P0171" in page and "Grand Prix" in page
        result["ok"] = result["ok"] and obd_ok and rep_ok
        print(f"{result['line']} guided_test={result['status']} "
              f"obd_demo={'ok' if obd_ok else 'FAILED'} (rpm {shown}) "
              f"report={'ok' if rep_ok else 'FAILED'} "
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
