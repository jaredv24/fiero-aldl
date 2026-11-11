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
from tkinter import messagebox, ttk

import aldl160
import aldl_logger as al
import cable_check

APP_NAME = "Fiero ALDL"
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("FIERO_ALDL_DATA") or os.path.join(
    os.path.expanduser("~"), "Documents", APP_NAME)
LOG_DIR = os.path.join(DATA_DIR, "logs")
LAYOUT_PATH = os.path.join(DATA_DIR, "my_layout.json")
CABLE_SETTINGS = os.path.join(DATA_DIR, "cable_settings.json")
TEMPLATE_DEF = os.path.join(RES_DIR, "definitions", "1227170_template.json")
DEMO_DEF = os.path.join(RES_DIR, "definitions", "demo.json")

SRC_DEMO, SRC_CABLE, SRC_ARDUINO = "Demo (no car)", "USB ALDL cable", "Arduino interface"
AUTO_PORT = "Automatic"
CHART_SECONDS = 60
CHART_COLORS = ("#2a6fdb", "#d1495b", "#2e9e5b")


def setup_data_dir():
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(LAYOUT_PATH):
        shutil.copyfile(TEMPLATE_DEF, LAYOUT_PATH)
    cable_check.SETTINGS = CABLE_SETTINGS
    cable_check.LOGS = LOG_DIR


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
        self.banner.pack(fill="x", pady=(0, 8))
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
            SRC_CABLE: "USB cable: plug in, key ON, run 'Check cable…' once, then Connect.",
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
        self.nb.select(3)
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
                   f"{', inverted' if best['invert'] else ''}.\n\nSettings saved. "
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
        writer.writerow(["time", "elapsed_s"] + names + ["raw_hex"])
        self.recording = (f, writer, names, cap, csv_path)
        self.record_btn.configure(text="■ Stop recording")
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
                            + [frame.hex().upper()])
            self.recording[0].flush()
        self.status_var.set(f"Connected: {self.source_var.get()}   frames: {self.frames}   "
                            f"frame length: {len(frame)} bytes")
        self.last_frame = frame

    # -------------------------------------------------------- dashboard

    def build_tiles(self):
        for w in self.tile_frame.winfo_children():
            w.destroy()
        self.tiles = {}
        params = [p for p in self.definition["parameters"] if p.get("range")]
        cols = 3
        for i, p in enumerate(params):
            box = ttk.LabelFrame(self.tile_frame, text=p["name"], padding=(10, 4))
            box.grid(row=i // cols, column=i % cols, sticky="nsew", padx=6, pady=6)
            row = ttk.Frame(box)
            row.pack(fill="x")
            value = ttk.Label(row, text="--", font=("Helvetica", 30, "bold"))
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
        result["ok"] = app.frames >= 3 and shown not in ("--", "") and rows >= 2
        print(f"self-test: frames={app.frames} rpm_tile={shown} logged_rows={rows} "
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
