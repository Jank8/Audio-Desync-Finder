"""Audio Desync Finder 3.0 - PySide6 + Fluent, Mica backdrop.

The analysis engine lives in sync_engine.py; this file is only the UI.
Run: pythonw audiosync4.pyw
"""
# ---------------------------------------------------------------------------
# DEPENDENCY BOOTSTRAP - runs before the Qt imports below.
# A fresh Python installation has none of these; install them first so a
# double-click on the .pyw works out of the box.
# ---------------------------------------------------------------------------
import importlib.util
import subprocess
import sys

_REQUIRED = [
    ("PySide6", "PySide6"),
    ("qfluentwidgets", "PySide6-Fluent-Widgets"),
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("matplotlib", "matplotlib"),
]


def _missing_dependencies():
    missing = []
    for module, package in _REQUIRED:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except Exception:
            missing.append(package)
    return missing


def _install_dependencies(packages):
    """Install with a minimal Tk window - tkinter ships with Python."""
    import threading
    import tkinter as tk
    from tkinter import ttk

    win = tk.Tk()
    win.title("Audio Desync Finder - installing dependencies")
    win.geometry("560x150")
    win.configure(bg="#1c1c1c")
    win.resizable(False, False)
    tk.Label(win, text="Installing: " + ", ".join(packages), bg="#1c1c1c",
             fg="#4cc2ff", font=("Segoe UI", 10, "bold")).pack(
        anchor="w", padx=16, pady=(16, 4))
    state = tk.Label(win, text="starting pip…", bg="#1c1c1c", fg="#9aa4ae",
                     font=("Segoe UI", 8), anchor="w")
    state.pack(fill="x", padx=16)
    bar = ttk.Progressbar(win, mode="indeterminate")
    bar.pack(fill="x", padx=16, pady=14)
    bar.start(55)

    result = {"ok": False}

    def worker():
        cmd = [sys.executable, "-m", "pip", "install",
               "--disable-pip-version-check", "--no-input", *packages]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            result["ok"] = proc.returncode == 0
        except Exception:
            result["ok"] = False
        win.after(0, win.destroy)

    threading.Thread(target=worker, daemon=True).start()
    win.mainloop()
    return result["ok"]


_missing = _missing_dependencies()
if _missing:
    if not _install_dependencies(_missing):
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing libraries",
            "Could not install:\n  " + "\n  ".join(_missing) +
            "\n\nInstall them manually:\n" + sys.executable +
            " -m pip install " + " ".join(_missing))
        sys.exit(1)
    importlib.invalidate_caches()

import os
import queue
import sys
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout,
                               QLabel, QTableWidgetItem, QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CardWidget, ComboBox, FluentWindow,
                            InfoBar, InfoBarPosition, LineEdit, MessageBox,
                            PrimaryPushButton, ProgressBar, PushButton,
                            RadioButton, SpinBox, StrongBodyLabel, TableWidget,
                            TextEdit, Theme, setTheme, setThemeColor)

import shutil

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

import sync_engine as engine

APP_NAME = "Audio Desync Finder"
APP_VERSION = "3.0.3"

ACCENT = "#4cc2ff"


def system_accent_hex() -> str:
    """The active Windows accent colour (DWM stores it as 0xAABBGGRR)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\\Microsoft\\Windows\\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
        r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
        if (r, g, b) == (0, 0, 0):
            raise ValueError("black accent")
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#4cc2ff"


class Card(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(8)


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1240, 860)
        self.setMicaEffectEnabled(True)
        self.setMinimumSize(1000, 640)

        self.ref_path = ""
        self.tgt_path = ""
        self.plan = None
        self.points = []
        self.meta = None
        self.worker = None
        self.accent = system_accent_hex()
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()

        page = QWidget()
        page.setObjectName("page")
        self.addSubInterface(page, None, APP_NAME)
        # no navigation pane: this app is a single page
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.hide()
        self.titleBar.setDoubleClickEnabled(True)

        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 12, 20, 16)
        outer.setSpacing(12)

        # ---------------- files -----------------------------------------
        files = Card()
        fl = QVBoxLayout(files)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(8)
        self.edit_ref = self._file_row(fl, "File 1:", "ref", checked=True)
        self.edit_tgt = self._file_row(fl, "File 2:", "tgt", checked=False)
        outer.addWidget(files)

        # ---------------- scan settings ---------------------------------
        settings = Card()
        sl = QHBoxLayout(settings)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(10)
        sl.addWidget(StrongBodyLabel("Window (s)"))
        self.spin_window = SpinBox()
        self.spin_window.setRange(10, 180)
        self.spin_window.setValue(40)
        sl.addWidget(self.spin_window)
        sl.addWidget(StrongBodyLabel("Step (s)"))
        self.spin_step = SpinBox()
        self.spin_step.setRange(5, 180)
        self.spin_step.setValue(30)
        sl.addWidget(self.spin_step)
        sl.addWidget(StrongBodyLabel("Min confidence"))
        self.spin_conf = SpinBox()
        self.spin_conf.setRange(1, 50)
        self.spin_conf.setValue(2)
        sl.addWidget(self.spin_conf)
        sl.addWidget(StrongBodyLabel("Signal"))
        self.combo_signal = ComboBox()
        self.combo_signal.addItems(["Auto", "Waveform", "Envelope"])
        sl.addWidget(self.combo_signal)
        sl.addStretch(1)
        self.btn_scan = PrimaryPushButton("⚡  Auto-scan whole file")
        self.btn_scan.clicked.connect(self.start_scan)
        sl.addWidget(self.btn_scan)
        self.btn_stop = PushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)
        sl.addWidget(self.btn_stop)
        outer.addWidget(settings)

        # ---------------- chart -----------------------------------------
        chart = Card()
        cl = QVBoxLayout(chart)
        cl.setContentsMargins(10, 10, 10, 10)
        self.fig = Figure(figsize=(6, 3.2), facecolor="#202020")
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setStyleSheet("background:transparent;")
        row = QHBoxLayout()
        row.addWidget(self.canvas, 3)
        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            ["Time (s)", "Offset (ms)", "Conf", "Source"])
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(6)
        row.addWidget(self.table, 2)
        cl.addLayout(row)
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.btn_del = PushButton("Delete selected point")
        self.btn_del.clicked.connect(self.delete_point)
        bar.addWidget(self.btn_del)
        cl.addLayout(bar)
        outer.addWidget(chart, 3)

        # ---------------- model + log -----------------------------------
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        model_card = Card()
        ml = QVBoxLayout(model_card)
        ml.setContentsMargins(14, 10, 14, 12)
        ml.addWidget(StrongBodyLabel("Fitted model"))
        self.model_view = TextEdit()
        self.model_view.setReadOnly(True)
        self.model_view.setStyleSheet(
            "TextEdit{background:transparent;border:none;"
            "color:#cfd4da;font:9pt 'Consolas';}")
        ml.addWidget(self.model_view)
        bottom.addWidget(model_card, 1)

        log_card = Card()
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(14, 10, 14, 12)
        ll.addWidget(StrongBodyLabel("Log"))
        self.log_view = TextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "TextEdit{background:transparent;border:none;"
            "color:#9aa4ae;font:9pt 'Consolas';}")
        ll.addWidget(self.log_view)
        bottom.addWidget(log_card, 1)
        outer.addLayout(bottom, 2)

        # ---------------- footer ----------------------------------------
        footer = QHBoxLayout()
        self.progress = ProgressBar()
        self.progress.setValue(0)
        footer.addWidget(self.progress, 1)
        self.status = BodyLabel("Ready")
        footer.addWidget(self.status)
        self.btn_export = PrimaryPushButton("⬇  Export corrected audio…")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export_audio)
        footer.addWidget(self.btn_export)
        outer.addLayout(footer)

        self._draw()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain)
        self.timer.start(100)

    # ------------------------------------------------------------------ UI
    def _file_row(self, layout, label, which, checked=False):
        row = QHBoxLayout()
        row.addWidget(StrongBodyLabel(label))
        edit = LineEdit()
        edit.setPlaceholderText("choose a media file…")
        edit.setReadOnly(True)
        row.addWidget(edit, 1)
        browse = PushButton("Browse")
        browse.clicked.connect(lambda: self.pick_file(which))
        row.addWidget(browse)
        radio = RadioButton("Base")
        radio.setChecked(checked)
        if not hasattr(self, "base_group"):
            self.base_group = []
        self.base_group.append((which, radio))
        row.addWidget(radio)
        layout.addLayout(row)
        return edit

    def pick_file(self, which):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a media file", "",
            "Media (*.mp4 *.mkv *.avi *.mov *.mp3 *.wav *.flac *.m4a *.aac);;All files (*)")
        if not path:
            return
        if which == "ref":
            self.ref_path = path
            self.edit_ref.setText(os.path.basename(path))
        else:
            self.tgt_path = path
            self.edit_tgt.setText(os.path.basename(path))
        for key, radio in self.base_group:
            radio.setChecked(key == which)
        if self.ref_path and self.tgt_path:
            self._load_pair()

    def _load_pair(self):
        ref, tgt = self._ordered_paths()
        r = engine.probe_framerate(ref)
        t = engine.probe_framerate(tgt)
        note = ""
        if r and t:
            note = f"   [{r:g} fps  →  {t:g} fps"
            if abs(r - t) > 1e-6:
                note += f", atempo {r / t:.6f}"
            note += "]"
        self.status.setText(f"{os.path.basename(ref)}  →  {os.path.basename(tgt)}{note}")

    def _ordered_paths(self):
        base_is_ref = any(k == "ref" and rd.isChecked() for k, rd in self.base_group)
        return (self.ref_path, self.tgt_path) if base_is_ref else (self.tgt_path, self.ref_path)

    def _fps_hint(self):
        ref, tgt = self._ordered_paths()
        r, t = engine.probe_framerate(ref), engine.probe_framerate(tgt)
        if r > 0 and t > 0 and abs(r - t) > 1e-9:
            return engine.framerate_slope(r, t)
        return None

    # --------------------------------------------------------------- tasks
    def log(self, text):
        self.log_view.append(text)

    def start_scan(self):
        if not (self.ref_path and self.tgt_path):
            MessageBox("Select both files first.", "Audio Desync Finder", self).exec()
            return
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.worker = threading.Thread(target=self._scan_task, daemon=True)
        self.worker.start()

    def stop_scan(self):
        self.stop_event.set()
        self.status.setText("Stopping…")

    def _scan_task(self):
        try:
            ref_path, tgt_path = self._ordered_paths()
            self.ui_queue.put(("log", "── Scan started ─────────────"))
            ref, tgt, meta = engine.decode_pair(
                ref_path, tgt_path,
                progress=lambda m: self.ui_queue.put(("log", m)),
                on_fraction=lambda phase, f: self.ui_queue.put(
                    ("progress", (5 + (30 if phase == "ref" else 32) * f,
                                  f"Decoding {phase}… {f * 100:.0f}%"))))
            self.meta = meta

            hint = self._fps_hint()
            rate = 1.0 + hint / 1000.0 if hint and abs(hint) >= 0.3 else 1.0
            scan_tgt = engine.resample_track(tgt, rate) if rate != 1.0 else tgt
            points = engine.scan_arrays(
                ref, scan_tgt, window=float(self.spin_window.value()),
                step=float(self.spin_step.value()),
                mode=("auto" if self.combo_signal.currentIndex() == 0
                      else "waveform" if self.combo_signal.currentIndex() == 1
                      else "envelope"),
                progress=lambda done, total: self.ui_queue.put(
                    ("progress", (72 + 27 * done / max(total, 1),
                                  f"Measuring {done}/{total}"))),
                should_stop=self.stop_event.is_set)
            points = sorted(engine.compensate_points(points, rate), key=lambda p: p["t"])
            plan = engine.build_plan(points, meta["ref_duration"],
                                     min_conf=float(self.spin_conf.value()),
                                     slope_hint=hint)
            self.ui_queue.put(("points", points))
            self.ui_queue.put(("plan", plan))
        except Exception as exc:
            self.ui_queue.put(("log", f"✗ {exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    def export_audio(self):
        if not self.plan or not self.plan.get("segments"):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save corrected audio", "corrected.mka", "Matroska Audio (*.mka)")
        if not path:
            return
        path = engine.mka_path(path)
        self.btn_export.setEnabled(False)
        self.btn_stop.setEnabled(True)
        threading.Thread(target=self._export_task, args=(path,), daemon=True).start()

    def _export_task(self, path):
        try:
            _, tgt_path = self._ordered_paths()
            engine.export_plan(
                self.plan, tgt_path, path,
                progress=lambda p, t: self.ui_queue.put(("progress", (p, t))),
                log=lambda m, tag="info": self.ui_queue.put(("log", m)),
                should_stop=self.stop_event.is_set)
            self.ui_queue.put(("log", f"✓ Saved: {path}"))
            self.ui_queue.put(("notify", (path, True)))
            residual = engine.measure_residual(self._ordered_paths()[0], path)
            if residual:
                for line in engine.format_residual(residual).splitlines():
                    self.ui_queue.put(("log", line))
        except Exception as exc:
            self.ui_queue.put(("log", f"✗ Export failed: {exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    # ------------------------------------------------------------ pumping
    def _drain(self):
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(payload)
            elif kind == "progress":
                pct, text = payload
                self.progress.setValue(int(pct))
                self.status.setText(text)
            elif kind == "notify":
                path, ok = payload
                self._notify("Export complete", os.path.basename(path), ok)
            elif kind == "points":
                self.points = payload
                self._fill_table()
            elif kind == "plan":
                self.plan = payload
                self.model_view.setPlainText("\n".join(engine.plan_lines(payload)[:10]))
                self._draw()
                self.btn_export.setEnabled(bool(payload.get("segments")))
            elif kind == "idle":
                self.btn_scan.setEnabled(True)
                self.btn_stop.setEnabled(False)
                self.progress.setValue(100 if self.plan else 0)

    def check_ffmpeg(self):
        """Offer a one-click winget install when FFmpeg is missing."""
        if shutil.which("ffmpeg"):
            self.log("FFmpeg found")
            return
        box = MessageBox(
            "FFmpeg is required to extract audio and was not found.\n"
            "Install it now with winget?", "Audio Desync Finder", self)
        if not box.exec():
            self.log("FFmpeg missing - install it manually: winget install --id Gyan.FFmpeg -e")
            return
        self.log("Installing FFmpeg via winget…")
        threading.Thread(target=self._install_ffmpeg, daemon=True).start()

    def _install_ffmpeg(self):
        try:
            proc = subprocess.Popen(
                ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                 "--accept-source-agreements", "--accept-package-agreements"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self.ui_queue.put(("log", line))
            proc.wait()
            self.ui_queue.put(("log", "FFmpeg installed" if shutil.which("ffmpeg")
                               else "winget finished - restart the app if FFmpeg is still missing"))
        except Exception as exc:
            self.ui_queue.put(("log", f"winget failed: {exc}"))

    def _fill_table(self):
        self.table.setRowCount(len(self.points))
        for i, p in enumerate(self.points):
            source = "manual" if p.get("kind") == "manual" else (
                "wave" if p.get("mode") == "w" else "env")
            for col, text in enumerate((f"{p['t']:.1f}", f"{p['offset']:+.1f}",
                                        f"{p.get('conf', 0):.1f}", source)):
                self.table.setItem(i, col, QTableWidgetItem(text))

    def delete_point(self):
        row = self.table.currentRow()
        if not (0 <= row < len(self.points)) or not self.meta:
            return
        del self.points[row]
        plan = engine.build_plan(self.points, self.meta["ref_duration"],
                                 min_conf=float(self.spin_conf.value()),
                                 slope_hint=self._fps_hint())
        self.plan = plan
        self.model_view.setPlainText("\n".join(engine.plan_lines(plan)[:10]))
        self._draw()
        self._fill_table()
        self.btn_export.setEnabled(bool(plan.get("segments")))

    def _notify(self, title, content, ok=True):
        (InfoBar.success if ok else InfoBar.error)(
            title=title, content=content, orient=Qt.Horizontal,
            isClosable=True, position=InfoBarPosition.TOP_RIGHT,
            duration=4000, parent=self)

    def _draw(self):
        ax = self.ax
        ax.clear()
        ax.set_facecolor("#202020")
        if self.points:
            ax.scatter([p["t"] for p in self.points],
                       [p["offset"] for p in self.points],
                       s=8, color=self.accent, alpha=0.9)
        for seg in (self.plan or {}).get("segments", []):
            x0, x1 = seg["t_start"], seg["t_end"]
            y0 = seg["offset"]
            ax.plot([x0, x1], [y0, y0 + seg["slope"] * (x1 - x0)],
                    color="#3ddc97", lw=1.6)
        ax.axhline(0, color="#666", lw=0.7, ls=":")
        ax.tick_params(colors="#c9cdd2", labelsize=7)
        for spine in ax.spines.values():
            spine.set_color("#3a3f45")
        ax.grid(alpha=0.15, color="white")
        ax.set_xlabel("Time (s)", color="#c9cdd2", fontsize=8)
        ax.set_ylabel("Offset (ms)", color="#c9cdd2", fontsize=8)
        self.canvas.draw_idle()


def main():
    setTheme(Theme.DARK)
    setThemeColor(QColor(system_accent_hex()))
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    QTimer.singleShot(500, win.check_ffmpeg)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
