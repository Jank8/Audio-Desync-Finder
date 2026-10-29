"""
Audio Desync Finder
===================
A GUI tool for detecting the exact audio offset (desynchronization) between
two audio or video files.

Workflow:
  1. The user selects two media files and sets a time range to compare.
  2. FFmpeg extracts a short mono WAV clip from each file at the specified range.
  3. Both clips are normalized and bandpass-filtered (100 Hz – 8 kHz).
  4. FFT-based cross-correlation finds the lag that aligns the two signals.
  5. Parabolic interpolation refines the lag to sub-sample precision.
  6. Results and aligned waveforms are shown in an interactive dark-themed GUI.

Dependencies:
    Python 3.x, numpy, scipy, matplotlib, FFmpeg.
    Missing Python packages are installed with pip on first launch and FFmpeg
    is installed with winget (Windows), so a plain Python installation is
    enough to start the program.
"""

# Audio Desync Finder - Copyright (C) 2026 Janusz Woźniak
# Licensed under the GNU General Public License v3 or later.
# See LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt - no warranty.

import glob
import importlib.util
import json
import os
import queue
import re
import sys
import subprocess
import tempfile
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

def _system_uses_light_theme() -> bool:
    """True when Windows is set to the light app theme."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except Exception:
        return False


def _apply_mica(window, light: bool) -> None:
    """Ask DWM for the Windows 11 Mica backdrop (silently ignored elsewhere)."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        backdrop = ctypes.c_int(2)          # DWMSBT_MAINWINDOW = Mica
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        mode = ctypes.c_int(0 if light else 1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(mode), ctypes.sizeof(mode))
    except Exception:
        pass


APP_LIGHT = _system_uses_light_theme()

try:
    import sv_ttk
except ImportError:
    sv_ttk = None

APP_NAME = "Audio Desync Finder"
APP_VERSION = "2.0.18"
_timeline_panel = None   # created when the files are loaded

# ---------------------------------------------------------------------------
# HIDE CONSOLE WINDOW
# On Windows, .pyw files may briefly flash a console. Suppress it explicitly
# via the Win32 API so the tool appears as a pure GUI application.
# ---------------------------------------------------------------------------
if os.name == 'nt':
    import ctypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd != 0:
        ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.kernel32.CloseHandle(hwnd)


def _apply_dark_titlebar(window) -> None:
    """
    Ask Windows to render the title bar in dark mode for *window*.

    Uses DwmSetWindowAttribute (DWMWA_USE_IMMERSIVE_DARK_MODE = 20 on
    Windows 11 / 10 build 18985+, attribute 19 on earlier 10 builds).
    Silently does nothing on non-Windows platforms or older builds that
    do not support the attribute.

    Must be called after the Tk window handle exists (i.e. after root.update()
    or inside a root.after() callback).
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        import ctypes.wintypes
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if result != 0:
            # Attribute 20 not supported (older Win10); try attribute 19
            DWMWA_USE_IMMERSIVE_DARK_MODE = 19
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except Exception:
        pass  # Non-fatal – title bar stays light on unsupported systems


def _run_ffmpeg(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run an ffmpeg command, registering the Popen object so
    _kill_child_processes() can terminate it immediately even while it is
    running in a thread."""
    proc = subprocess.Popen(cmd, **kwargs)
    _ffmpeg_procs.append(proc)
    try:
        proc.communicate()   # blocks thread but leaves proc killable from main thread
    finally:
        try:
            _ffmpeg_procs.remove(proc)
        except ValueError:
            pass
    # Allow killed exit codes (-9, -15 on Unix; 1 is ffmpeg's own error code)
    # On Windows a killed process returns a large negative number or non-zero.
    # We only raise on "real" non-zero exits, not on process being killed.
    killed_codes = {-9, -15, 1}
    if proc.returncode != 0 and proc.returncode not in killed_codes:
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return subprocess.CompletedProcess(cmd, proc.returncode)


def _run_ffmpeg_progress(cmd: list, total_s: float, pct_from: float,
                         pct_to: float, report=None, should_stop=None):
    """
    Run FFmpeg with live progress parsed from ``-progress pipe:1``.

    total_s is the expected output duration; the reported percentage is
    interpolated between pct_from and pct_to so a two-step export shows one
    continuous bar.  report(pct, text) is called from the worker thread – the
    UI layer is responsible for marshalling it (see _ui_queue).

    Raises RuntimeError when should_stop() asks to cancel.
    """
    full = [cmd[0], "-nostats", "-progress", "pipe:1"] + list(cmd[1:])
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL,
                            startupinfo=get_startupinfo())
    _ffmpeg_procs.append(proc)
    started = time.time()
    last_reported = pct_from
    cancelled = False

    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                fraction = 0.0 if total_s <= 0 else min(1.0, seconds / total_s)
                pct = pct_from + (pct_to - pct_from) * fraction
                if report is not None and pct - last_reported >= 0.4:
                    last_reported = pct
                    report(pct, f"{pct:.0f}%  –  {time.time() - started:.0f}s")
            if should_stop is not None and should_stop():
                cancelled = True
                proc.kill()
                break
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass

    rc = proc.wait()
    try:
        _ffmpeg_procs.remove(proc)
    except ValueError:
        pass

    if cancelled or (should_stop is not None and should_stop()):
        raise RuntimeError("Cancelled by the user")
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)
    if report is not None:
        report(pct_to, f"{pct_to:.0f}%")
    return rc


def get_startupinfo():
    """
    Return a STARTUPINFO object that hides subprocess console windows on Windows.
    Returns None on non-Windows platforms where STARTUPINFO is not available.
    """
    if os.name == 'nt':
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        return info
    return None


def show_startup_error(title, message):
    """
    Display a modal error dialog for failures that occur before the main window
    is created.  Uses a temporary hidden Tk root so that messagebox works even
    when the main GUI has not been initialised yet.
    Falls back silently if Tk itself is unavailable.
    """
    try:
        startup_root = tk.Tk()
        startup_root.withdraw()
        messagebox.showerror(title, message)
        startup_root.destroy()
    except Exception:
        pass


def dark_info_dialog(title: str, message: str) -> None:
    """Dark-themed modal info dialog to replace messagebox.showinfo."""
    popup = tk.Toplevel(root)
    popup.title(title)
    popup.configure(bg="#1a1a1a")
    popup.resizable(False, False)
    popup.update()
    _apply_dark_titlebar(popup)

    tk.Label(popup, text=title, bg="#1a1a1a", fg="#4a9eff",
             font=("Segoe UI", 11, "bold")).pack(padx=24, pady=(18, 10))

    tk.Label(popup, text=message, bg="#1a1a1a", fg="#cccccc",
             font=("Segoe UI", 9), justify=tk.LEFT, anchor=tk.W).pack(
             padx=24, pady=(0, 16), anchor=tk.W)

    tk.Button(popup, text="OK", command=popup.destroy,
              bg="#2a7de1", fg="#ffffff", relief=tk.FLAT,
              font=("Segoe UI", 9, "bold"), cursor="hand2",
              activebackground="#3d8bff",
              padx=30, pady=6).pack(pady=(0, 18))

    popup.update_idletasks()
    x = root.winfo_x() + (root.winfo_width()  - popup.winfo_width())  // 2
    y = root.winfo_y() + (root.winfo_height() - popup.winfo_height()) // 2
    popup.geometry(f"+{x}+{y}")
    popup.grab_set()


# ---------------------------------------------------------------------------
# AUTOMATIC PYTHON DEPENDENCY INSTALLATION
#
# numpy / scipy / matplotlib are the only third-party packages this program
# needs.  On a fresh Python installation they are usually absent, so instead of
# quitting with an error dialog we now install whatever is missing with pip
# before the GUI is created.  pip's output is streamed into a small progress
# window, so the user can watch the process and cancel it if desired.
#
# Install strategy - each attempt is tried only if the previous one failed:
#   1. pip install <pkgs>                         normal install (venv aware)
#   2. pip install --user <pkgs>                  no admin rights required
#   3. pip install --user --break-system-packages POSIX / PEP 668 only
#
# Set the environment variable ADS_SKIP_DEP_INSTALL=1 to disable this, e.g.
# when running from a read-only or offline environment.
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = [
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("matplotlib", "matplotlib"),
]

PIP_INSTALL_TIMEOUT_S = 900      # hard cap for the whole installation phase
_startup_messages: list = []     # installer log, replayed in the GUI console


def _find_missing_packages() -> list:
    """
    Return the pip names of the required packages that cannot be imported.

    Uses importlib.util.find_spec() instead of importing, so this is cheap and
    has no side effects (no Tk backend, no Matplotlib figure machinery).
    """
    missing = []
    for module_name, pip_name in REQUIRED_PACKAGES:
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, AttributeError):
            spec = None
        if spec is None:
            missing.append(pip_name)
    return missing


def _app_python() -> str:
    """Return the absolute path of the interpreter that should get the packages."""
    exe = sys.executable or ""
    if exe and os.path.isfile(exe):
        return exe
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _run_quiet(cmd: list, timeout: float):
    """Run *cmd* capturing output. Returns the CompletedProcess or None."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, startupinfo=get_startupinfo())
    except Exception:
        return None


def _pip_usable(exe: str) -> bool:
    """True when `exe -m pip` answers with a version string."""
    result = _run_quiet([exe, "-m", "pip", "--version"], 60)
    return bool(result and result.returncode == 0)


def _bootstrap_pip(exe: str) -> bool:
    """Try to create pip via the bundled ensurepip module."""
    result = _run_quiet([exe, "-m", "ensurepip", "--upgrade"], 300)
    return bool(result and result.returncode == 0)


def _in_virtualenv() -> bool:
    """True when the app runs inside a venv (where --user is not allowed)."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _looks_like_error(line: str) -> bool:
    """Very rough heuristic used only to colour pip output red."""
    low = line.lower()
    return "error" in low or "traceback" in low or "failed" in low


def _stream_subprocess(cmd: list, emit, state: dict) -> int:
    """
    Run *cmd*, forward every output line to emit(line, tag) and honour
    state['cancel'] (set from the UI thread when the user presses Cancel).

    Returns the exit code, or -1 when the process could not be started.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            startupinfo=get_startupinfo(),
        )
    except Exception as exc:
        emit(f"Could not start process: {exc}", "error")
        return -1

    state["proc"] = proc
    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                emit(line, "error" if _looks_like_error(line) else "info")
            if state["cancel"]:
                break
    except Exception as exc:
        emit(f"Output stream error: {exc}", "warn")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass

    if state["cancel"] and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass

    try:
        return proc.wait(timeout=30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return -1


def _installer_log_tail(limit: int = 8) -> str:
    """Last few lines of the installer log, embedded in the fallback dialog."""
    if not _startup_messages:
        return ""
    lines = [msg for msg, _tag in _startup_messages[-limit:]]
    return "Last installer output:\n" + "\n".join(lines)


def _install_dependencies(missing: list, reason: str = "") -> bool:
    """
    Install *missing* (pip package names) while showing a live progress window.

    Returns True only when every package imports successfully afterwards.
    Never raises - all failures are logged and reported to the caller, which
    decides whether to abort or continue.
    """
    if not missing:
        return True

    def emit(msg: str, tag: str = "info") -> None:
        _startup_messages.append((msg, tag))

    if os.environ.get("ADS_SKIP_DEP_INSTALL") == "1":
        emit("ADS_SKIP_DEP_INSTALL=1 is set - skipping automatic installation.", "warn")
        return False

    if getattr(sys, "frozen", False):
        emit("Frozen build detected - pip cannot run from inside it.", "error")
        return False

    exe = _app_python()
    if not exe:
        emit("Could not locate a Python interpreter to run pip.", "error")
        return False

    # ---- progress window ---------------------------------------------------
    win = tk.Tk()
    win.title("Audio Sync Finder - installing dependencies")
    win.configure(bg="#1a1a1a")
    win.resizable(False, False)
    win.update()
    _apply_dark_titlebar(win)

    tk.Label(win, text="Installing required Python packages…",
             bg="#1a1a1a", fg="#4a9eff",
             font=("Segoe UI", 11, "bold")).pack(anchor=tk.W, padx=20, pady=(16, 2))
    tk.Label(win, text="  •  ".join(missing),
             bg="#1a1a1a", fg="#cccccc",
             font=("Segoe UI", 9)).pack(anchor=tk.W, padx=20)

    first_reason = (reason or "").strip().splitlines()[0] if reason else ""
    if first_reason:
        tk.Label(win, text=first_reason[:160], bg="#1a1a1a", fg="#888888",
                 font=("Segoe UI", 8)).pack(anchor=tk.W, padx=20, pady=(3, 0))

    progress = ttk.Progressbar(win, mode="indeterminate", length=560)
    progress.pack(padx=20, pady=(12, 8), fill=tk.X)
    progress.start(50)

    log_frame = tk.Frame(win, bg="#0d0d0d")
    log_frame.pack(padx=20, fill=tk.BOTH, expand=True)

    log_text = tk.Text(log_frame, width=84, height=14, bg="#0d0d0d", fg="#cccccc",
                       font=("Consolas", 8), relief=tk.FLAT, wrap=tk.CHAR,
                       borderwidth=0, highlightthickness=0, state=tk.DISABLED)
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    log_sb = tk.Scrollbar(log_frame, command=log_text.yview, width=10,
                          bg="#333333", troughcolor="#1a1a1a", bd=0, relief=tk.FLAT)
    log_sb.pack(side=tk.LEFT, fill=tk.Y)
    log_text.config(yscrollcommand=log_sb.set)

    for tag, colour in (("info", "#aaaaaa"), ("ok", "#00cc66"),
                        ("warn", "#ffaa44"), ("error", "#ff5555"),
                        ("cmd", "#4a9eff"), ("bold", "#ffffff")):
        log_text.tag_config(tag, foreground=colour)

    status_var = tk.StringVar(value="Preparing pip…")
    tk.Label(win, textvariable=status_var, bg="#1a1a1a", fg="#888888",
             font=("Segoe UI", 8), anchor=tk.W).pack(fill=tk.X, padx=20, pady=(6, 0))

    btn_cancel = tk.Button(win, text="Cancel", bg="#333333", fg="#ffffff",
                           relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
                           activebackground="#444444", padx=18, pady=4)
    btn_cancel.pack(pady=(8, 16))

    # ---- worker / message pump --------------------------------------------
    q = queue.Queue()
    state = {"cancel": False, "proc": None}

    def _append(msg: str, tag: str) -> None:
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, msg + "\n", tag)
        log_text.see(tk.END)
        log_text.config(state=tk.DISABLED)

    def _cancel() -> None:
        if state["cancel"]:
            return
        state["cancel"] = True
        status_var.set("Cancelling…")
        proc = state.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    btn_cancel.config(command=_cancel)
    win.protocol("WM_DELETE_WINDOW", _cancel)

    def _worker() -> None:
        try:
            q.put(("log", f"Interpreter: {exe}", "info"))
            q.put(("log", f"Python: {sys.version.split()[0]}", "info"))

            if not _pip_usable(exe):
                q.put(("log", "pip is not available - bootstrapping with ensurepip…", "warn"))
                if not _bootstrap_pip(exe) or not _pip_usable(exe):
                    q.put(("done", False,
                           "pip is missing and could not be bootstrapped automatically."))
                    return
                q.put(("log", "pip is ready.", "ok"))

            targets = list(missing)

            attempts = [("standard install", [])]
            if not _in_virtualenv():
                # --user needs no administrator rights and is the usual fix on
                # machines where site-packages is not writable.
                attempts.append(("user site, no admin rights", ["--user"]))
            if os.name != "nt":
                attempts.append(("PEP 668 override", ["--user", "--break-system-packages"]))

            last_error = ""
            for label, extra in attempts:
                if state["cancel"]:
                    q.put(("done", False, "Installation cancelled."))
                    return

                cmd = [exe, "-m", "pip", "install",
                       "--disable-pip-version-check", "--no-input",
                       *extra, *targets]
                q.put(("log", f"Attempt: {label}", "bold"))
                q.put(("log", "pip " + " ".join(cmd[3:]), "cmd"))

                rc = _stream_subprocess(
                    cmd, lambda msg, tag="info": q.put(("log", msg, tag)), state)

                if state["cancel"]:
                    q.put(("done", False, "Installation cancelled."))
                    return

                if rc == 0:
                    still_missing = _find_missing_packages()
                    if not still_missing:
                        q.put(("log", "All packages installed successfully.", "ok"))
                        q.put(("done", True, ""))
                        return
                    q.put(("log", "pip reported success, but these are still not "
                                  f"importable: {', '.join(still_missing)}", "warn"))
                    targets = still_missing
                    last_error = f"{', '.join(targets)} not importable after install"
                else:
                    last_error = f"pip exited with code {rc}"
                    q.put(("log", last_error, "error"))

            q.put(("done", False, last_error or "Installation failed."))
        except Exception as exc:                      # never let the thread die silently
            q.put(("done", False, f"Unexpected installer error: {exc}"))

    threading.Thread(target=_worker, daemon=True).start()

    deadline = time.time() + PIP_INSTALL_TIMEOUT_S
    cancel_deadline = None
    ok = False
    message = ""

    while True:
        if not state["cancel"] and time.time() > deadline:
            message = "Installation timed out."
            _cancel()
        if state["cancel"]:
            if cancel_deadline is None:
                cancel_deadline = time.time() + 10
            elif time.time() > cancel_deadline:
                message = message or "Installation cancelled."
                break

        try:
            item = q.get(timeout=0.05)
        except queue.Empty:
            try:
                win.update()
            except tk.TclError:            # window already gone
                break
            continue

        if item[0] == "log":
            _kind, msg, tag = item
            _startup_messages.append((msg, tag))
            _append(msg, tag)
            status_var.set(msg[:110])
        else:
            _kind, ok, message = item
            break

        try:
            win.update()
        except tk.TclError:
            break

    if (not ok and message
            and (not _startup_messages or _startup_messages[-1][0] != message)):
        _startup_messages.append((message, "error"))

    try:
        progress.stop()
        win.destroy()
    except Exception:
        pass

    return ok


def _import_scientific_stack() -> None:
    """
    Import numpy / scipy / matplotlib into the module namespace.

    Raises ImportError when anything is missing or broken, which is what the
    automatic installer below reacts to.
    """
    global np, signal, wavfile, FigureCanvasTkAgg, Figure

    import numpy as np
    import scipy.signal as signal
    from scipy.io import wavfile
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure


# ---------------------------------------------------------------------------
# PYTHON PACKAGE DEPENDENCY CHECK  (runs at import time, before the GUI)
# FFmpeg is checked later inside the GUI after startup so the user can see
# the install progress in the built-in console panel.
# ---------------------------------------------------------------------------
try:
    _import_scientific_stack()
except ImportError as _dep_error:
    _missing_pkgs = _find_missing_packages()
    if not _missing_pkgs:
        # Present but broken (typically a numpy ABI mismatch after an upgrade).
        # Reinstalling them is the cheapest reliable repair, so ask pip for all.
        _missing_pkgs = [pip_name for _module, pip_name in REQUIRED_PACKAGES]

    if _install_dependencies(_missing_pkgs, str(_dep_error)):
        importlib.invalidate_caches()
        try:
            _import_scientific_stack()
        except ImportError as _retry_error:
            show_startup_error(
                "Missing Python libraries",
                "The libraries were installed, but they still cannot be imported.\n\n"
                "Close this window and install them manually:\n"
                f"{sys.executable} -m pip install --upgrade numpy scipy matplotlib\n\n"
                f"Details: {_retry_error}\n\n"
                + _installer_log_tail()
            )
            sys.exit(1)
    else:
        show_startup_error(
            "Missing Python libraries",
            "Required Python libraries are missing and could not be installed "
            "automatically.\n\n"
            "Install them manually:\n"
            f"{sys.executable} -m pip install numpy scipy matplotlib\n\n"
            "If pip itself is outdated:\n"
            f"{sys.executable} -m pip install --upgrade pip\n\n"
            + _installer_log_tail()
        )
        sys.exit(1)

# ---------------------------------------------------------------------------
# GLOBAL STATE
# viz_data acts as a lightweight shared state container for the visualisation
# layer so that individual callbacks do not need to pass data as arguments.
# ---------------------------------------------------------------------------
viz_data = {
    'data1': None,           # Filtered waveform for File 1 (numpy array)
    'data2': None,           # Filtered waveform for File 2 (numpy array)
    'sr': None,              # Sample rate shared by both clips (Hz)
    'offset_samples': 0,     # Lag in samples to shift File 2 for visual alignment
    'zoom_start': 0,         # First sample index of the current zoom window
    'zoom_end': None,        # Last sample index of the current zoom window (None = full)
    'canvas': None,          # FigureCanvasTkAgg widget
    'fig': None,             # Matplotlib Figure
    'ax': None,              # Matplotlib Axes
    'scrollbar': None,       # Horizontal tk.Scale used as a scrollbar
    'scrollbar_frame': None, # Parent Frame of the scrollbar
    'updating_scroll': False # Reentrancy guard: True while the scrollbar is being set
                             # programmatically to prevent on_scroll feedback loops
}

# Minimum zoom window expressed in seconds.  Prevents the chart from becoming
# so narrow that rendering becomes meaningless or numerically unstable.
MIN_ZOOM_SECONDS = 0.01

# ---------------------------------------------------------------------------
# CONSOLE PANEL HELPERS
# _console_widgets holds every Text widget that should receive log output.
# Widgets are appended after creation; console_log() writes to all of them.
#
# Tk widgets may only be touched from the thread that owns them, and most of
# the logging happens inside FFmpeg worker threads.  Calls from other threads
# are therefore queued and flushed by _pump_ui() on the main thread; touching
# the widgets directly from a worker is what made the window look frozen.
# ---------------------------------------------------------------------------

_console_widgets: list = []  # populated after GUI widgets are created
_ui_queue: "queue.Queue" = queue.Queue()


def _is_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _write_console(msg: str, tag: str = "info") -> None:
    """Actually append a line to the console widgets (main thread only)."""
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}]  {msg}\n"
    for w in _console_widgets:
        try:
            w.config(state=tk.NORMAL)
            w.insert(tk.END, line, tag)
            w.see(tk.END)
            w.config(state=tk.DISABLED)
        except Exception:
            pass


def console_log(msg: str, tag: str = "info") -> None:
    """Append a timestamped line to the console panel (safe from any thread)."""
    if _is_main_thread():
        _write_console(msg, tag)
    else:
        _ui_queue.put(("log", (msg, tag)))


def _pump_ui() -> None:
    """
    Drain UI updates queued by worker threads.

    Runs on the Tk main loop every 80 ms; this is what keeps the console and
    progress bar alive while FFmpeg works in the background.
    """
    try:
        while True:
            kind, payload = _ui_queue.get_nowait()
            if kind == "log":
                _write_console(*payload)
            elif kind == "progress":
                pct, text = payload
                try:
                    progress_var.set(pct)
                    progress_label.config(text=text,
                                          fg="#aaaaaa" if pct < 100 else "#00cc66")
                except Exception:
                    pass
    except Exception:
        pass
    try:
        root.after(80, _pump_ui)
    except Exception:
        pass


def console_clear() -> None:
    """Clear all registered console Text widgets."""
    for w in _console_widgets:
        try:
            w.config(state=tk.NORMAL)
            w.delete("1.0", tk.END)
            w.config(state=tk.DISABLED)
        except Exception:
            pass


def flush_startup_messages() -> None:
    """Replay the pre-GUI installer log into the console panel."""
    if not _startup_messages:
        return
    console_log("── Dependency installer ──────────────────────", "bold")
    for msg, tag in _startup_messages:
        console_log(msg, tag)
    console_log("✓ Automatic dependency installation finished.", "ok")
    _startup_messages.clear()


# ---------------------------------------------------------------------------
# FFMPEG DETECTION AND AUTOMATIC INSTALLATION
#
# FFmpeg is a native binary that pip cannot provide, so on Windows it is
# installed with winget.  Detection runs at startup and again before every
# action that needs FFmpeg; the process PATH is re-read from the registry each
# time, so a binary that appeared a minute ago is usable without a restart.
# Everything is reported in the built-in console panel - there is no button.
# ---------------------------------------------------------------------------

ffmpeg_ready    = False   # True once FFmpeg has been confirmed usable
_ffmpeg_procs: list = []  # Running media-extraction (ffmpeg) processes
_install_procs: list = []  # Running winget installers

# ANSI escape sequences emitted by winget/ffmpeg when colour is forced on
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Places a winget or manual FFmpeg installation typically ends up in.
# %LOCALAPPDATA% / %ProgramFiles% are expanded before globbing.
_FFMPEG_SEARCH_GLOBS = (
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\ffmpeg.exe",
    r"%ProgramFiles%\ffmpeg*\bin\ffmpeg.exe",
    r"%ProgramFiles%\FFmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
)


def _refresh_path_from_registry() -> None:
    """
    Merge the user/system PATH stored in the registry into os.environ['PATH'].

    winget updates the PATH variable, but a process that is already running
    keeps the environment it started with, so ffmpeg.exe would stay invisible
    until the next launch.  Reading the registry directly makes a fresh
    installation usable immediately.
    """
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )

    registered = []
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                raw, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        expanded = os.path.expandvars(str(raw))
        registered.extend(part for part in expanded.split(os.pathsep) if part)

    if not registered:
        return

    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    known = {p.lower() for p in current}
    merged = current + [p for p in registered if p.lower() not in known]
    os.environ["PATH"] = os.pathsep.join(merged)


def _find_ffmpeg() -> str:
    """
    Return the absolute path of ffmpeg(.exe), or "" when it is unavailable.

    Checks the (refreshed) process PATH first, then the locations used by
    winget and by typical manual installs.
    """
    _refresh_path_from_registry()

    found = shutil.which("ffmpeg")
    if found:
        return found

    if os.name == "nt":
        for pattern in _FFMPEG_SEARCH_GLOBS:
            expanded = os.path.expandvars(pattern)
            try:
                matches = glob.glob(expanded, recursive=True)
            except OSError:
                continue
            for candidate in matches:
                if os.path.isfile(candidate):
                    return candidate
    return ""


def _register_ffmpeg(path: str) -> None:
    """
    Put the directory containing *path* first on the process PATH.

    The analysis code invokes plain ``ffmpeg`` / ``ffprobe``, so this is what
    actually makes a newly discovered binary usable.
    """
    folder = os.path.dirname(path)
    if not folder:
        return
    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if os.path.normcase(folder) in {os.path.normcase(p) for p in current}:
        return
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def _clean_process_line(raw: bytes) -> str:
    """
    Decode one subprocess output chunk and make it presentable.

    winget emits ANSI colour codes and carriage-return progress spinners; both
    would wreck the console panel, so they are stripped here.  A chunk may hold
    several spinner frames, in which case only the last one is kept.
    """
    text = raw.decode("utf-8", errors="replace")
    text = _ANSI_RE.sub("", text)
    parts = [part.strip() for part in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    parts = [part for part in parts if part]
    return parts[-1] if parts else ""


def _kill_child_processes() -> None:
    """Terminate every tracked child process (media extraction + installers)."""
    for proc in list(_ffmpeg_procs) + list(_install_procs):
        try:
            proc.kill()
        except Exception:
            pass
    _ffmpeg_procs.clear()
    _install_procs.clear()


def _on_close() -> None:
    """Window close handler – stop child processes, then destroy."""
    _kill_child_processes()
    root.destroy()


def _stream_process_to_console(proc: subprocess.Popen) -> int:
    """
    Read stdout+stderr from *proc* line-by-line and forward every line to the
    console panel via root.after() so the Tk event loop stays alive.

    Safe to call from a worker thread: it never touches Tk directly.
    Returns the process returncode.
    """
    for raw in iter(proc.stdout.readline, b""):
        line = _clean_process_line(raw)
        if line:
            root.after(0, console_log, line, "info")
    proc.wait()
    return proc.returncode


def _apply_ffmpeg_found(path: str) -> None:
    """Register a usable FFmpeg and unlock the UI (main thread only)."""
    global ffmpeg_ready
    _register_ffmpeg(path)
    ffmpeg_ready = True
    console_log(f"✓ FFmpeg ready: {path}", "ok")
    pass    # analyze button removed in 2.0


def _ffmpeg_install_failed(reason: str) -> None:
    """
    Report a failed automatic installation and leave the app usable.

    The Analyze button is re-enabled on purpose: check_ffmpeg_before_analyze()
    re-runs detection on every click, so a manual install is picked up without
    restarting the program.
    """
    console_log(f"✗ FFmpeg installation failed: {reason}", "error")
    console_log("   Analysis needs FFmpeg. Install it manually, then click ANALYZE again:", "warn")
    console_log("     winget install --id Gyan.FFmpeg -e", "cmd")
    console_log("   or download it from  https://ffmpeg.org/download.html", "info")
    pass    # analyze button removed in 2.0


def _install_ffmpeg_worker() -> None:
    """
    Install FFmpeg through winget, streaming the output into the console.

    Runs in a daemon thread; every UI interaction goes through root.after().
    """
    winget = shutil.which("winget")
    if not winget:
        root.after(0, console_log,
                   "winget is not available on this system – FFmpeg cannot be "
                   "installed automatically.", "error")
        root.after(0, _ffmpeg_install_failed, "winget not found")
        return

    cmd = [winget, "install", "--id", "Gyan.FFmpeg", "-e",
           "--accept-source-agreements", "--accept-package-agreements"]
    root.after(0, console_log, "winget " + " ".join(cmd[1:]), "cmd")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            startupinfo=get_startupinfo(),
        )
    except Exception as exc:
        root.after(0, _ffmpeg_install_failed, str(exc))
        return

    _install_procs.append(proc)
    try:
        rc = _stream_process_to_console(proc)
    finally:
        try:
            _install_procs.remove(proc)
        except ValueError:
            pass

    # winget may exit 0 before the PATH update is visible – look anyway.
    path = _find_ffmpeg()
    if path:
        root.after(0, _apply_ffmpeg_found, path)
    elif rc == 0:
        root.after(0, _ffmpeg_install_failed,
                   "winget reported success but ffmpeg.exe could not be located")
    else:
        root.after(0, _ffmpeg_install_failed, f"winget exited with code {rc}")


def ensure_ffmpeg() -> None:
    """
    Startup check: locate FFmpeg, or install it automatically via winget.

    Called once after the main window is shown, so the user can follow the
    whole installation in the console panel.
    """
    path = _find_ffmpeg()
    if path:
        _apply_ffmpeg_found(path)
        return

    console_log("⚠  FFmpeg was not found – installing it automatically via winget…", "warn")
    console_log("   FFmpeg is required to extract audio from media files.", "info")
    pass    # analyze button removed in 2.0
    threading.Thread(target=_install_ffmpeg_worker, daemon=True).start()


def check_ffmpeg_before_analyze() -> bool:
    """
    Guard used at the top of every action that needs FFmpeg.

    Detection is re-run on each call (PATH is refreshed from the registry), so
    an FFmpeg installed outside the program, or a winget run that finished a
    moment ago, is picked up without restarting the application.

    Returns True when FFmpeg is available, False otherwise.
    """
    global ffmpeg_ready
    if ffmpeg_ready:
        return True

    path = _find_ffmpeg()
    if path:
        _register_ffmpeg(path)
        ffmpeg_ready = True
        console_log(f"✓ FFmpeg ready: {path}", "ok")
        return True

    console_log("✗ FFmpeg is not installed yet.", "error")
    console_log("   Install it with:  winget install --id Gyan.FFmpeg -e", "cmd")
    return False


# ---------------------------------------------------------------------------
# DRIFT FIX  –  measure progressive desync and export a corrected audio file
# ---------------------------------------------------------------------------

# Stores the two offset measurements made on the Drift Fix tab.
# Each entry: {'time_s': float, 'offset_ms': float} or None
drift_points: list = [None, None]


def _extract_wav(src_file: str, start_s: float, duration: float,
                 out_path: str, video_track: bool = False) -> None:
    """Extract a mono 44.1 kHz WAV clip from src_file.
    video_track=True decodes the first video stream as audio for single-file
    A/V drift measurement (correlate video sound vs audio track).
    """
    si = get_startupinfo()
    if video_track:
        cmd = ["ffmpeg", "-y", "-ss", str(start_s), "-i", src_file,
               "-t", str(duration), "-map", "0:v:0",
               "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-ss", str(start_s), "-i", src_file,
               "-t", str(duration), "-vn", "-map", "0:a:0",
               "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", out_path]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, startupinfo=si)


def _correlate_wavs(path1: str, path2: str) -> float:
    """Bandpass-filter and cross-correlate two WAV files.
    Returns offset in ms: positive = path2 is delayed relative to path1.
    """
    sr1, d1 = wavfile.read(path1)
    _,   d2 = wavfile.read(path2)
    if len(d1.shape) > 1: d1 = np.mean(d1, axis=1)
    if len(d2.shape) > 1: d2 = np.mean(d2, axis=1)
    d1 = d1.astype(np.float64); d2 = d2.astype(np.float64)
    d1 -= np.mean(d1);  d2 -= np.mean(d2)
    d1 /= (np.sqrt(np.mean(d1**2)) + 1e-10)
    d2 /= (np.sqrt(np.mean(d2**2)) + 1e-10)
    nyq = sr1 / 2
    sos = signal.butter(4, [100/nyq, min(8000/nyq, 0.99)], btype='band', output='sos')
    d1f = signal.sosfilt(sos, d1); d2f = signal.sosfilt(sos, d2)
    corr = signal.correlate(d1f, d2f, mode='full', method='fft')
    lags = signal.correlation_lags(len(d1f), len(d2f), mode='full')
    mi = int(np.argmax(np.abs(corr))); lag = float(lags[mi])
    if 0 < mi < len(corr)-1:
        y1, y2, y3 = np.abs(corr[mi-1]), np.abs(corr[mi]), np.abs(corr[mi+1])
        lag += 0.5*(y3-y1)/(2*y2-y1-y3+1e-10)
    return round((-lag/sr1)*1000, 3)


def _run_correlation(file1: str, file2: str,
                     start1: float, start2: float,
                     duration: float) -> float:
    """Two-file mode: extract audio from both files and correlate."""
    tmp1 = os.path.join(tempfile.gettempdir(), "drift_tmp_1.wav")
    tmp2 = os.path.join(tempfile.gettempdir(), "drift_tmp_2.wav")
    for p in [tmp1, tmp2]:
        if os.path.exists(p): os.remove(p)
    _extract_wav(file1, start1, duration, tmp1, video_track=False)
    _extract_wav(file2, start2, duration, tmp2, video_track=False)
    return _correlate_wavs(tmp1, tmp2)



def drift_measure(point_index: int) -> None:
    """Measure A/V offset at one time point (two-file mode only)."""
    if not check_ffmpeg_before_analyze():
        return
    ref_path = entry_file1.get().strip()
    source   = entry_file2.get().strip()
    if not ref_path or not source:
        messagebox.showerror("Error", "Select both File 1 (reference) and File 2 (source) first.")
        return
    try:
        duration = float(drift_entry_duration.get())
        if point_index == 0:
            t_str = drift_entry_t1.get(); lbl = drift_lbl_result1
        else:
            t_str = drift_entry_t2.get(); lbl = drift_lbl_result2
        t = to_seconds(t_str)
        console_log(f"Drift point {point_index+1}: t={t:.1f}s...", "info")
        offset_ms = _run_correlation(ref_path, source, t, t, duration)
        drift_points[point_index] = {"time_s": t, "offset_ms": offset_ms}
        sign = "+" if offset_ms >= 0 else ""
        lbl.config(text=f"Offset: {sign}{offset_ms} ms", fg="#4a9eff")
        console_log(f"  Point {point_index+1}: {sign}{offset_ms} ms", "ok")
        _drift_update_calculated()
    except Exception as e:
        messagebox.showerror("Error", f"Measurement failed:\n{e}")
        console_log(f"Drift measurement failed: {e}", "error")


def _drift_update_calculated() -> None:
    """Recompute drift rate and atempo from the two measured points."""
    p1, p2 = drift_points[0], drift_points[1]
    if p1 is None or p2 is None:
        return
    dt_s  = p2["time_s"]    - p1["time_s"]
    d_off = p2["offset_ms"] - p1["offset_ms"]
    if abs(dt_s) < 1.0:
        drift_lbl_calc.config(text="Points too close - use a wider time gap.", fg="#ffaa44")
        return
    drift_rate = d_off / dt_s
    atempo     = round(1.0 - drift_rate/1000.0, 8)
    
    # If first point is near the start (t < 30s), use it directly as initial offset
    # Otherwise extrapolate back to t=0
    if p1["time_s"] < 30:
        init_off = round(p1["offset_ms"], 2)
        init_time_label = f"(at t={p1['time_s']:.1f}s)"
    else:
        init_off = round(p1["offset_ms"] - drift_rate * p1["time_s"], 2)
        init_time_label = "(extrapolated to t=0)"
    
    drift_lbl_calc.config(
        text=(f"Drift rate:  {drift_rate:+.4f} ms/s\n"
              f"atempo:      {atempo:.8f}\n"
              f"Init offset: {init_off:+.2f} ms  {init_time_label}"),
        fg="#00cc66")
    drift_lbl_calc._atempo         = atempo
    drift_lbl_calc._initial_offset = init_off
    console_log(f"Drift: {drift_rate:+.4f} ms/s  atempo={atempo:.8f}  init={init_off:+.2f} ms", "ok")
    df_entry_atempo.delete(0, tk.END);  df_entry_atempo.insert(0, f"{atempo:.8f}")
    df_entry_initoff.delete(0, tk.END); df_entry_initoff.insert(0, str(init_off))



# ---------------------------------------------------------------------------
# ZOOM / SCROLL HELPERS  (defined before GUI widgets that reference them)
# ---------------------------------------------------------------------------

def _get_file_duration(path: str) -> float:
    """Return duration of media file in seconds via ffprobe. Returns 0.0 on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, startupinfo=get_startupinfo()
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def to_seconds(t_str: str) -> float:
    """
    Parse a time string into a total number of seconds (float).

    Accepted formats:
        "30"          – plain seconds
        "1:30"        – MM:SS
        "1:02:30"     – HH:MM:SS
        "90.5"        – fractional seconds

    Args:
        t_str: Human-readable time string entered by the user.

    Returns:
        Equivalent time as a float number of seconds.
    """
    if ":" in t_str:
        parts = t_str.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    return float(t_str)


def get_min_zoom_samples() -> int:
    """
    Return the minimum allowed zoom window width in samples.

    Prevents the zoom level from going below MIN_ZOOM_SECONDS, which
    would result in an empty or numerically unstable chart view.
    Falls back to 1 sample when no audio is loaded yet.
    """
    sr = viz_data['sr'] or 0
    return max(1, int(sr * MIN_ZOOM_SECONDS))


def set_zoom_window(center: int, desired_range: int) -> None:
    """
    Update viz_data zoom pointers to a window of *desired_range* samples
    centred on *center*, clamped to the waveform boundaries.

    The window is also prevented from going below get_min_zoom_samples() so
    that the chart always has something meaningful to display.

    Args:
        center:        Sample index around which to centre the new window.
        desired_range: Requested window width in samples before clamping.
    """
    total_len = len(viz_data['data1'])
    min_range = min(total_len, get_min_zoom_samples())
    desired_range = max(min_range, min(total_len, int(desired_range)))
    max_start = max(0, total_len - desired_range)
    start = int(center - desired_range / 2)
    start = max(0, min(max_start, start))

    viz_data['zoom_start'] = start
    viz_data['zoom_end'] = start + desired_range


def update_visualization() -> None:
    """
    Redraw both filtered waveforms on the Matplotlib axes.

    File 2 is shifted horizontally by viz_data['offset_samples'] so that
    the user can visually confirm the alignment suggested by the correlation.
    After redrawing, the scrollbar state is refreshed to match the current
    zoom window.

    Does nothing if audio data has not been loaded yet.
    """
    if viz_data['data1'] is None or viz_data['data2'] is None:
        return

    ax = viz_data['ax']
    ax.clear()

    data1, data2 = viz_data['data1'], viz_data['data2']
    sr, offset_samples = viz_data['sr'], viz_data['offset_samples']
    start = viz_data['zoom_start']
    end = viz_data['zoom_end'] if viz_data['zoom_end'] else len(data1)

    # Build the time axis for the visible slice of data1
    time1 = np.arange(start, min(end, len(data1))) / sr

    # Shift data2 so it visually aligns with data1 given the detected lag.
    # Positive offset_samples: data2 is delayed  → pad zeros at the front.
    # Negative offset_samples: data2 is ahead    → drop leading samples.
    if offset_samples >= 0:
        data2_shifted = np.pad(data2, (offset_samples, 0), 'constant')[start:end]
    else:
        data2_shifted = data2[-offset_samples:][start:end]

    # Guard against edge cases where shifting produces arrays of different lengths
    min_len = min(len(time1), len(data1[start:end]), len(data2_shifted))
    time1 = time1[:min_len]
    plot_data1 = data1[start:start + min_len]
    plot_data2 = data2_shifted[:min_len]

    file1_label = 'File 1 (base)' if ref_var.get() == 1 else 'File 1 (target)'
    file2_label = 'File 2 (target)' if ref_var.get() == 1 else 'File 2 (base)'

    ax.plot(time1, plot_data1, color='#4a9eff', linewidth=0.8, alpha=0.7, label=file1_label)
    ax.plot(time1, plot_data2, color='#ff6b6b', linewidth=0.8, alpha=0.7, label=file2_label)

    ax.set_xlabel('Time (s)', color='white', fontsize=9)
    ax.set_ylabel('Amplitude', color='white', fontsize=9)
    ax.legend(loc='upper right', fontsize=8, framealpha=0.8)
    ax.grid(True, alpha=0.2, color='white')
    ax.set_facecolor('#1a1a1a')
    ax.tick_params(colors='white', labelsize=8)

    viz_data['fig'].patch.set_facecolor('#1a1a1a')
    viz_data['canvas'].draw()

    update_scrollbar()


def update_scrollbar() -> None:
    """
    Synchronise the horizontal scrollbar with the current zoom window.

    When the view is zoomed in (window < full waveform), the scrollbar is
    enabled and its thumb position reflects zoom_start.  When the full
    waveform is visible, the scrollbar is disabled.
    """
    if viz_data['data1'] is None or viz_data['scrollbar'] is None:
        return

    total_len = len(viz_data['data1'])
    if viz_data['zoom_end'] is None:
        viz_data['zoom_end'] = total_len
    min_range = min(total_len, get_min_zoom_samples())
    current_range = min(total_len, max(min_range, viz_data['zoom_end'] - viz_data['zoom_start']))

    if current_range < total_len:
        # Zoomed in: enable scrolling and position the thumb
        viz_data['scrollbar'].config(to=total_len - current_range, state=tk.NORMAL)
        # Set the reentrancy guard so that the Scale's command callback
        # (on_scroll) does not trigger a redundant redraw while we move it.
        viz_data['updating_scroll'] = True
        viz_data['scrollbar'].set(viz_data['zoom_start'])
        viz_data['updating_scroll'] = False
    else:
        # Full view: keep the scrollbar visible but inactive
        viz_data['scrollbar'].config(to=1, state=tk.DISABLED)
        viz_data['scrollbar'].set(0)


def on_scroll(value: str) -> None:
    """
    Callback invoked by the horizontal Scale widget when the user drags it.

    Translates the raw Scale value into a new zoom_start/zoom_end pair and
    triggers a chart redraw.  The reentrancy guard (updating_scroll) prevents
    this callback from firing during programmatic scrollbar updates in
    update_scrollbar().

    Args:
        value: String representation of the new Scale position (provided by Tk).
    """
    if viz_data['data1'] is None or viz_data['updating_scroll']:
        return

    total_len = len(viz_data['data1'])
    min_range = min(total_len, get_min_zoom_samples())
    current_range = min(total_len, max(min_range, viz_data['zoom_end'] - viz_data['zoom_start']))

    max_start = max(0, total_len - current_range)
    viz_data['zoom_start'] = max(0, min(max_start, int(float(value))))
    viz_data['zoom_end'] = min(total_len, viz_data['zoom_start'] + current_range)

    # Clamp to avoid overrunning the end of the waveform
    if viz_data['zoom_end'] >= total_len:
        viz_data['zoom_end'] = total_len
        viz_data['zoom_start'] = max(0, total_len - current_range)

    update_visualization()


def apply_manual_offset() -> None:
    """
    Apply a user-supplied offset (in milliseconds) to the visualisation
    without re-running the FFmpeg extraction or cross-correlation.

    Reads the value from entry_manual_offset, converts it to samples, stores
    it in viz_data['offset_samples'], and redraws the chart.  Also updates
    the result label with a plain-language description of the new offset.

    Errors (no data loaded, non-numeric input) are shown in a dialog.
    """
    try:
        if viz_data['data1'] is None or viz_data['sr'] is None:
            messagebox.showerror("Error", "Run analysis before applying a manual offset.")
            return

        manual_ms = float(entry_manual_offset.get())
        manual_samples = int((manual_ms / 1000.0) * viz_data['sr'])
        # Sign convention matches analyze_sync: positive ms means target is delayed.
        viz_data['offset_samples'] = -manual_samples if ref_var.get() == 1 else manual_samples
        update_visualization()

        offset_ms = round(manual_ms, 2)
        ref_file = ref_var.get()
        target_name = "File 2" if ref_file == 1 else "File 1"

        if offset_ms == 0:
            res, color = "✓ Files perfectly synchronized!\nOffset: 0.00 ms", "#00ff88"
        elif offset_ms > 0:
            res = (f"⏱️ {target_name} IS DELAYED by {offset_ms} ms\n"
                   f"→ Delay {target_name}: -{offset_ms} ms")
            color = "#ffa500"
        else:
            res = (f"⏱️ {target_name} IS AHEAD by {abs(offset_ms)} ms\n"
                   f"→ Delay {target_name}: +{abs(offset_ms)} ms")
            color = "#ff6b6b"

        label_result.config(text=res, fg=color)
    except ValueError:
        messagebox.showerror("Error", "Invalid offset value!")




def export_drift_corrected() -> None:
    """
    Export drift-corrected audio in two steps to avoid container duration bugs:
      Step 1: atempo + adelay/trim → lossless WAV (no duration metadata issue)
      Step 2: WAV → original codec (AC3/AAC/etc.) at original bitrate

    The final file has correct duration metadata and is accepted without
    warnings.  Only one lossy encode is performed.
    """
    if not check_ffmpeg_before_analyze():
        return

    # Need drift result – check attributes set by analysis or manual drift calc
    try:
        atempo   = label_result._atempo
        init_off = label_result._init_off   # ms, signed
        offset_ms_start = label_result._offset_ms  # initial static offset
    except AttributeError:
        messagebox.showerror("Error",
            "No drift values available.\nRun analysis with 'Check drift' or use Manual drift.")
        return

    ref_file    = ref_var.get()
    target_path = entry_file2.get().strip() if ref_file == 1 else entry_file1.get().strip()
    # Fallback: if target slot is empty, use whichever file is filled in
    if not target_path:
        target_path = entry_file1.get().strip() or entry_file2.get().strip()
    if not target_path:
        messagebox.showerror("Error", "No file selected.")
        return

    # Detect original codec, bitrate, channels and sample rate via ffprobe
    detected_codec      = "ac3"
    detected_bitrate    = "256k"
    detected_channels   = 2       # Default to stereo if detection fails
    detected_samplerate = 48000   # Default to 48kHz if detection fails
    
    try:
        import json
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate,channels,sample_rate",
             "-of", "json", target_path],
            capture_output=True, text=True, startupinfo=get_startupinfo()
        )
        info = json.loads(probe.stdout)
        stream = info.get("streams", [{}])[0]
        if stream.get("codec_name"):
            detected_codec = stream["codec_name"].lower()
        if stream.get("bit_rate") and stream["bit_rate"] != "N/A":
            br = int(stream["bit_rate"])
            detected_bitrate = f"{br // 1000}k"
        if stream.get("channels"):
            detected_channels = int(stream["channels"])
        if stream.get("sample_rate"):
            detected_samplerate = int(stream["sample_rate"])
    except Exception as e:
        console_log(f"Warning: Could not detect audio parameters, using defaults. {e}", "warn")

    # Map codec → ffmpeg encoder and container extension
    _codec_map = {
        "ac3":      ("ac3",           ".ac3"),
        "eac3":     ("eac3",          ".eac3"),
        "aac":      ("aac",           ".aac"),
        "mp3":      ("libmp3lame",    ".mp3"),
        "opus":     ("libopus",       ".opus"),
        "flac":     ("flac",          ".flac"),
        "vorbis":   ("libvorbis",     ".ogg"),
        "dts":      ("dca",           ".dts"),
    }
    encoder, ext = _codec_map.get(detected_codec, ("ac3", ".ac3"))

    import os as _os
    base = _os.path.splitext(_os.path.basename(target_path))[0]
    out_path = filedialog.asksaveasfilename(
        title="Save drift-corrected audio as…",
        defaultextension=ext,
        initialfile=f"{base}_driftfixed{ext}",
        filetypes=[
            ("AC-3",  "*.ac3"), ("E-AC-3", "*.eac3"),
            ("AAC",   "*.aac"), ("MP3",    "*.mp3"),
            ("FLAC",  "*.flac"),("Opus",   "*.opus"),
            ("All files", "*.*"),
        ]
    )
    if not out_path:
        return

    import tempfile as _tmp
    import os as _os2
    tmp_wav = _os.path.join(_tmp.gettempdir(), "driftfix_tmp.wav")

    # Always preserve original channel count and sample rate
    _preserve = ["-ac", str(detected_channels), "-ar", str(detected_samplerate)]

    # Two-pass: decode → atempo → lossless WAV, then WAV → target codec
    # WAV intermediate ensures atempo operates on clean PCM without codec
    # buffering artifacts that can affect timing precision.
    # init_off applies the static offset at t=0 (capped at ±60s to guard against bad values)
    _safe_init_off = init_off if abs(init_off) <= 60000 else 0
    
    # Only apply atempo if it's significantly different from 1.0
    # (drift rate >= 0.1 ms/s, which is the threshold used during analysis)
    apply_atempo = abs(atempo - 1.0) >= 0.0001  # corresponds to ~0.1 ms/s drift
    
    if _safe_init_off > 0:
        # Positive offset: add delay at the start
        delay_ms = int(round(_safe_init_off))
        # Build adelay filter with correct number of channels
        # adelay syntax: delay_ch0|delay_ch1|... (one value per channel)
        delay_spec = "|".join([str(delay_ms)] * detected_channels)
        if apply_atempo:
            af1 = f"adelay={delay_spec},atempo={atempo}"
        else:
            af1 = f"adelay={delay_spec}"
    elif _safe_init_off < 0:
        # Negative offset: trim from start (handled via -ss flag below)
        if apply_atempo:
            af1 = f"atempo={atempo}"
        else:
            af1 = None  # No filters needed
    else:
        # No offset correction needed
        if apply_atempo:
            af1 = f"atempo={atempo}"
        else:
            af1 = None  # No filters needed

    # Build the complete command for step 1
    # Order matters: input → map → decode params (ac/ar) → filters → encode
    if _safe_init_off < 0:
        # Negative offset: seek to start point
        cmd1 = ["ffmpeg", "-y", "-ss", str(abs(_safe_init_off) / 1000.0), "-i", target_path,
                "-vn", "-map", "0:a:0"]
    else:
        # Positive or zero offset: start from beginning
        cmd1 = ["ffmpeg", "-y", "-i", target_path, "-vn", "-map", "0:a:0"]
    
    # Add channel/samplerate preservation (must be before filters)
    cmd1 += _preserve
    
    # Add audio filter if needed
    if af1:
        cmd1 += ["-af", af1]
    
    # Final output codec
    cmd1 += ["-c:a", "pcm_s16le", tmp_wav]

    if encoder in ("flac", "pcm_s16le"):
        cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + _preserve + ["-c:a", encoder, out_path]
    elif encoder in ("libopus", "libvorbis"):
        cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + _preserve + ["-c:a", encoder,
                "-b:a", detected_bitrate, out_path]
    else:
        cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + _preserve + ["-c:a", encoder,
                "-b:a", detected_bitrate, out_path]

    _ch_str = f"  {detected_channels}ch"
    _sr_str = f"  {detected_samplerate}Hz"
    _off_str = f"  offset={_safe_init_off:+.0f}ms" if _safe_init_off != 0 else "  no offset"
    _atempo_str = f"  atempo={atempo}" if apply_atempo else "  no tempo change"
    
    console_log(f"Step 1/2{_off_str}{_atempo_str} → WAV", "bold")
    console_log("ffmpeg " + " ".join(cmd1[1:]), "cmd")
    console_log(f"Step 2/2  WAV → {detected_codec} @ {detected_bitrate}{_ch_str}{_sr_str}", "bold")
    console_log("ffmpeg " + " ".join(cmd2[1:]), "cmd")

    def _do_export():
        try:
            span = _get_file_duration(target_path) or 0.0
            set_progress(1, "Step 1/2 – applying correction…")
            _run_ffmpeg_progress(cmd1, span, 1, 55, report=set_progress)
            console_log("Step 1 done", "ok")
            set_progress(56, "Step 2/2 – encoding…")
            _run_ffmpeg_progress(cmd2, span, 56, 99, report=set_progress)
            try:
                _os.remove(tmp_wav)
            except Exception:
                pass
            set_progress(100, "Export done")
            console_log(f"✓ Saved: {out_path}", "ok")
            
            _off_info = f"Offset correction: {_safe_init_off:+.0f} ms" if _safe_init_off != 0 else "No offset correction"
            _drift_info = f"Drift correction: atempo={atempo}" if apply_atempo else "No drift correction (negligible)"
            
            root.after(0, lambda: dark_info_dialog("Export complete",
                f"File saved:\n{out_path}\n\n"
                f"Codec: {detected_codec}  Bitrate: {detected_bitrate}\n"
                f"Channels: {detected_channels}  Sample rate: {detected_samplerate}Hz\n"
                f"{_off_info}\n"
                f"{_drift_info}\n\n"
                f"Ready to mux – no further delay needed."))
        except subprocess.CalledProcessError as e:
            _rc = e.returncode
            console_log(f"Export failed (exit {_rc})", "error")
            root.after(0, lambda: (
                messagebox.showerror("Export failed", f"FFmpeg exit {_rc}"),
                clear_progress()
            ))

    import threading as _thr
    _thr.Thread(target=_do_export, daemon=True).start()

def zoom_in() -> None:
    """Halve the zoom window width, centred on the current midpoint."""
    if viz_data['data1'] is None:
        return
    current_range = viz_data['zoom_end'] - viz_data['zoom_start']
    if current_range <= get_min_zoom_samples():
        return
    new_range = max(get_min_zoom_samples(), int(current_range * 0.5))
    center = (viz_data['zoom_start'] + viz_data['zoom_end']) // 2
    set_zoom_window(center, new_range)
    update_visualization()


def zoom_out() -> None:
    """
    Double the current zoom window width, centred on the current view midpoint.
    No-op when already showing the full waveform or when no data is loaded.
    """
    if viz_data['data1'] is None:
        return
    current_range = viz_data['zoom_end'] - viz_data['zoom_start']
    if current_range >= len(viz_data['data1']):
        return
    new_range = min(len(viz_data['data1']), int(current_range * 2))
    center = (viz_data['zoom_start'] + viz_data['zoom_end']) // 2
    set_zoom_window(center, new_range)
    update_visualization()


def reset_zoom() -> None:
    """
    Reset the zoom window to show the entire loaded waveform.
    No-op when no data is loaded.
    """
    if viz_data['data1'] is None:
        return
    viz_data['zoom_start'] = 0
    viz_data['zoom_end'] = len(viz_data['data1'])
    update_visualization()



def _maybe_load_timeline() -> None:
    """(Re)build the timeline as soon as both files are chosen."""
    if entry_file1.get().strip() and entry_file2.get().strip():
        try:
            load_timeline(force=True)
        except Exception:
            pass

def select_file1() -> None:
    """Open a file-chooser dialog and populate the File 1 path entry widget."""
    f = filedialog.askopenfilename(
        title="Select first audio/video file",
        filetypes=[
            ("Audio/Video files",
             "*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.webm "
             "*.mp3 *.wav *.flac *.aac *.ogg *.m4a *.wma *.opus"),
            ("All files", "*.*")
        ]
    )
    if f:
        entry_file1.delete(0, tk.END)
        entry_file1.insert(0, f)
        _maybe_load_timeline()


def select_file2() -> None:
    """Open a file-chooser dialog and populate the File 2 path entry widget."""
    f = filedialog.askopenfilename(
        title="Select second audio/video file",
        filetypes=[
            ("Audio/Video files",
             "*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.webm "
             "*.mp3 *.wav *.flac *.aac *.ogg *.m4a *.wma *.opus"),
            ("All files", "*.*")
        ]
    )
    if f:
        entry_file2.delete(0, tk.END)
        entry_file2.insert(0, f)
        _maybe_load_timeline()

def _run_analyze_sync_thread() -> None:
    """Worker thread for analyze_sync – runs FFmpeg and correlation off the main thread."""
    try:
        _analyze_sync_impl()
    except Exception as e:
        root.after(0, lambda: (
            messagebox.showerror("Error", f"Analysis failed:\n{e}"),
            label_status.config(text="✗ Error occurred during analysis", fg="#ff4444"),
            label_result.config(text="Could not analyze files.\nCheck if they are valid.", fg="#888888"),
            console_log(f"✗ Analysis failed: {e}", "error"),
            None,   # analyze button removed in 2.0
            clear_progress()
        ))


def analyze_sync() -> None:
    """Launch the analysis pipeline in a background thread to keep UI responsive."""
    if not check_ffmpeg_before_analyze():
        return
    file1 = entry_file1.get()
    file2 = entry_file2.get()
    if not file1 or not file2:
        messagebox.showerror("Error", "Select both audio/video files!")
        return
    pass    # analyze button removed in 2.0
    import threading
    threading.Thread(target=_run_analyze_sync_thread, daemon=True).start()


def _analyze_sync_impl() -> None:
    """Run the full sync analysis pipeline. Called from a background thread."""

    file1 = entry_file1.get()
    file2 = entry_file2.get()

    path_wav1 = os.path.join(tempfile.gettempdir(), "sync_temp_1.wav")
    path_wav2 = os.path.join(tempfile.gettempdir(), "sync_temp_2.wav")

    # Remove stale temporary files from a previous run to avoid reading old data
    for p in [path_wav1, path_wav2]:
        if os.path.exists(p):
            os.remove(p)

    root.after(0, lambda: (label_status.config(text="● Extracting audio from files...", fg="#ffa500"), set_progress(5, "Extracting...")))
    console_log("── Starting analysis ──────────────────────────", "bold")
    console_log(f"File 1: {file1}", "info")
    console_log(f"File 2: {file2}", "info")

    try:
        start_time = to_seconds(entry_start.get())
        duration = float(entry_duration.get())

        # Pre-offsets shift the extraction window independently for each file.
        # This is useful when the two recordings started at different real-world
        # times that the user already knows (e.g. timecode differences).
        preoffset1 = float(entry_preoffset1.get())
        preoffset2 = float(entry_preoffset2.get())

        start_time1 = start_time + preoffset1
        start_time2 = start_time + preoffset2

        # Convert both sources to comparable mono WAV snippets before signal analysis.
        cmd1 = ["ffmpeg", "-y", "-ss", str(start_time1), "-i", file1,
                "-t", str(duration), "-vn", "-map", "0:a:0",
                "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", path_wav1]
        cmd2 = ["ffmpeg", "-y", "-ss", str(start_time2), "-i", file2,
                "-t", str(duration), "-vn", "-map", "0:a:0",
                "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", path_wav2]

        console_log(f"ffmpeg  File 1  (start={start_time1:.3f}s  dur={duration}s)", "cmd")
        _run_ffmpeg(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=get_startupinfo())
        console_log("File 1 extracted OK", "ok")
        root.after(0, lambda: set_progress(25, "File 1 extracted..."))

        console_log(f"ffmpeg  File 2  (start={start_time2:.3f}s  dur={duration}s)", "cmd")
        _run_ffmpeg(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=get_startupinfo())
        console_log("File 2 extracted OK", "ok")
        root.after(0, lambda: set_progress(50, "File 2 extracted..."))

        root.after(0, lambda: set_progress(55, "Correlating..."))
        console_log("Running cross-correlation…", "info")
        sr1, data1 = wavfile.read(path_wav1)
        sr2, data2 = wavfile.read(path_wav2)

        # Flatten multi-channel audio that FFmpeg may have left as stereo
        if len(data1.shape) > 1:
            data1 = np.mean(data1, axis=1)
        if len(data2.shape) > 1:
            data2 = np.mean(data2, axis=1)

        data1 = data1.astype(np.float64)
        data2 = data2.astype(np.float64)

        # Center and normalise both signals so correlation depends on shape, not loudness.
        data1 = data1 - np.mean(data1)
        data2 = data2 - np.mean(data2)

        # RMS normalisation – small epsilon avoids division by zero on silent clips
        data1 = data1 / (np.sqrt(np.mean(data1 ** 2)) + 1e-10)
        data2 = data2 / (np.sqrt(np.mean(data2 ** 2)) + 1e-10)

        # Focus correlation on the speech/music band and reduce low-frequency rumble.
        nyquist = sr1 / 2
        low_cut = 100 / nyquist
        high_cut = min(8000 / nyquist, 0.99)
        sos = signal.butter(4, [low_cut, high_cut], btype='band', output='sos')
        data1_filtered = signal.sosfilt(sos, data1)
        data2_filtered = signal.sosfilt(sos, data2)

        # Correlation lag is the visual shift to apply to File 2 so it aligns with File 1.
        corr = signal.correlate(data1_filtered, data2_filtered, mode="full", method="fft")
        lags = signal.correlation_lags(len(data1_filtered), len(data2_filtered), mode="full")

        max_idx = np.argmax(np.abs(corr))
        lag = lags[max_idx]

        # Parabolic interpolation: fit a parabola through the peak and its two
        # immediate neighbours to estimate the true continuous-domain peak position.
        # This pushes accuracy below the 1/44100 s (~0.022 ms) sample-rate limit.
        if 0 < max_idx < len(corr) - 1:
            y1 = np.abs(corr[max_idx - 1])
            y2 = np.abs(corr[max_idx])
            y3 = np.abs(corr[max_idx + 1])
            delta = 0.5 * (y3 - y1) / (2 * y2 - y1 - y3 + 1e-10)
            lag = lag + delta

        ref_file = ref_var.get()
        target_name = "File 2" if ref_file == 1 else "File 1"

        # Positive target_offset_samples means the target is delayed relative to the base.
        target_offset_samples = -lag if ref_file == 1 else lag
        offset_ms = round((target_offset_samples / sr1) * 1000, 2)

        # Re-read pre-offsets (already parsed above, but kept explicit for clarity)
        preoffset1 = float(entry_preoffset1.get())
        preoffset2 = float(entry_preoffset2.get())

        # The extraction pre-offsets already compensated part of the real-world offset.
        # Add that back to report the true total offset needed in the media player.
        if ref_file == 1:
            preoffset_diff_ms = (preoffset2 - preoffset1) * 1000
        else:
            preoffset_diff_ms = (preoffset1 - preoffset2) * 1000

        total_offset_ms = round(offset_ms + preoffset_diff_ms, 2)

        # Build the human-readable result string
        if offset_ms == 0:
            res   = "✓ In sync"
            color = "#00ff88"
        elif offset_ms > 0:
            res   = f"▶ {target_name} late by {offset_ms} ms"
            color = "#ffa500"
        else:
            res   = f"◀ {target_name} early by {abs(offset_ms)} ms"
            color = "#ff6b6b"

        # Pre-offset correction
        if preoffset1 != 0 or preoffset2 != 0:
            res += f"\n  (total: {total_offset_ms:+.2f} ms)"

        # Delay instruction
        if offset_ms != 0:
            mkv_delay = round(-offset_ms if offset_ms > 0 else abs(offset_ms))
            mkv_sign  = "+" if mkv_delay >= 0 else ""
            res += f"\n\nDelay {target_name}: {mkv_sign}{mkv_delay} ms"
            res += f"\n(muxer delay if not exporting)"

        # ── Optional drift check ─────────────────────────────────────────────
        if check_drift_var.get():
            try:
                base_file = file1 if ref_file == 1 else file2
                total_dur = _get_file_duration(base_file)
                if total_dur > 120:  # Need at least 2 minutes for meaningful multi-point measurement
                    # Use the user-configured duration for ALL drift measurements
                    clip_dur = float(entry_duration.get())
                    preoffset1_v = float(entry_preoffset1.get())
                    preoffset2_v = float(entry_preoffset2.get())
                    
                    # Calculate measurement points
                    # Start: t=10s to avoid intro issues
                    # End: two points - one at 95% and one at the very end (last clip_dur seconds)
                    # (subtitles/outros often have silence or just music)
                    t_start = 10.0
                    t_end_safe = total_dur * 0.95 - clip_dur  # 95% of duration
                    t_end_last = total_dur - clip_dur         # Last clip_dur seconds of the file
                    
                    usable_dur = t_end_safe - t_start
                    if usable_dur < 60:
                        res += "\n\nDrift check skipped (usable range too short)."
                        console_log("Drift check: usable range < 60s", "warn")
                        _drift_atempo_tmp  = 1.0
                        _drift_initoff_tmp = offset_ms
                    else:
                        # Determine number of intermediate points based on duration
                        # For every 10 minutes, add one measurement point
                        num_intermediate = min(4, max(0, int((usable_dur / 60) / 2.5)))  # 0-4 intermediate points
                        
                        # Build list of measurement times
                        measurement_times = [t_start]
                        if num_intermediate > 0:
                            step = usable_dur / (num_intermediate + 1)
                            for i in range(1, num_intermediate + 1):
                                measurement_times.append(t_start + i * step)
                        
                        # Add two end points: 95% and last clip_dur seconds
                        measurement_times.append(t_end_safe)
                        measurement_times.append(t_end_last)
                        
                        console_log(f"Drift check: measuring at {len(measurement_times)} points across {total_dur:.1f}s file...", "bold")
                        
                        # Measure offset at each point
                        offsets = []
                        times = []
                        base_progress = 65
                        progress_step = 25 / len(measurement_times)
                        
                        for idx, t in enumerate(measurement_times):
                            # Label the measurement point appropriately
                            if idx == 0:
                                label = "start"
                            elif idx == len(measurement_times) - 1:
                                label = "end"
                            elif idx == len(measurement_times) - 2:
                                label = "95%"
                            else:
                                label = f"{int(100*t/total_dur)}%"
                            
                            console_log(f"  Point {idx+1}/{len(measurement_times)}: t={t:.1f}s ({label})...", "info")
                            root.after(0, lambda lbl=label: label_status.config(text=f"● Drift check: {lbl}...", fg="#ffa500"))
                            root.after(0, lambda p=base_progress + idx*progress_step: set_progress(p, f"Drift: {label}..."))
                            
                            offset_raw = _run_correlation(file1, file2, t + preoffset1_v, t + preoffset2_v, clip_dur)
                            offset_corrected = round(-offset_raw if ref_file == 1 else offset_raw, 2)
                            offsets.append(offset_corrected)
                            times.append(t)
                            console_log(f"    → {offset_corrected:+.2f} ms", "ok")
                        
                        # Linear regression to find best-fit drift rate
                        # This averages out measurement noise better than just using first/last points
                        times_arr = np.array(times)
                        offsets_arr = np.array(offsets)
                        
                        # Detect outliers using IQR method
                        q1, q3 = np.percentile(offsets_arr, [25, 75])
                        iqr = q3 - q1
                        outlier_threshold = 3.0  # How many IQRs away to consider outlier
                        lower_bound = q1 - outlier_threshold * iqr
                        upper_bound = q3 + outlier_threshold * iqr
                        
                        # Filter out outliers
                        valid_mask = (offsets_arr >= lower_bound) & (offsets_arr <= upper_bound)
                        outliers = []
                        for i, (offset, valid) in enumerate(zip(offsets, valid_mask)):
                            if not valid:
                                outliers.append((i+1, times[i], offset))
                        
                        if outliers:
                            console_log(f"WARNING: {len(outliers)} outlier(s) detected and IGNORED:", "warn")
                            for pt_num, t, offset in outliers:
                                console_log(f"  Point {pt_num} (t={t:.1f}s): {offset:+.2f} ms - EXCLUDED from regression", "warn")
                            console_log("  Possible silence, noise, or poor correlation at these points", "warn")
                        
                        # Use only valid (non-outlier) points for regression
                        times_valid = times_arr[valid_mask]
                        offsets_valid = offsets_arr[valid_mask]
                        
                        if len(times_valid) < 2:
                            raise Exception(f"Too many outliers detected ({len(outliers)}/{len(offsets)}). Cannot calculate drift.")
                        
                        # y = drift_rate * x + intercept
                        # Using least squares fit for drift rate (only on valid points)
                        A = np.vstack([times_valid, np.ones(len(times_valid))]).T
                        drift_rate, intercept = np.linalg.lstsq(A, offsets_valid, rcond=None)[0]
                        
                        # Initial offset should be taken from the FIRST measurement (at t=10s)
                        # not from the regression intercept (which would be extrapolated to t=0)
                        # BUT: only if first point is not an outlier
                        if valid_mask[0]:
                            init_off = offsets[0]  # First measurement at start point
                        else:
                            # First point is outlier - use regression intercept at first valid time
                            init_off = drift_rate * times_valid[0] + intercept
                            console_log(f"Note: First point was outlier, using interpolated init offset", "warn")
                        
                        atempo = round(1.0 - drift_rate / 1000.0, 8)
                        
                        # Calculate R² to show fit quality (only for valid points)
                        residuals = offsets_valid - (drift_rate * times_valid + intercept)
                        ss_res = np.sum(residuals**2)
                        ss_tot = np.sum((offsets_valid - np.mean(offsets_valid))**2)
                        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 1.0
                        
                        console_log(f"Linear fit: drift={drift_rate:+.4f} ms/s, R²={r_squared:.4f} ({len(times_valid)}/{len(times)} points)", "ok")
                        console_log(f"Initial offset: {init_off:+.2f} ms (measured at t={times[0]:.1f}s)", "ok")
                        
                        # Warn if R² is too low (poor linear fit) even after removing outliers
                        if r_squared < 0.8:
                            console_log(f"WARNING: Low R² ({r_squared:.3f}) even after removing outliers", "warn")
                            res += f"\n\n⚠️ WARNING: Low fit quality (R²={r_squared:.3f})"
                            res += f"\nDrift may not be linear. Results may be unreliable."
                        
                        if abs(drift_rate) < 0.1:
                            res += f"\n\nNo significant drift (< 0.1 ms/s)."
                            res += f"\nStatic offset: {round(init_off):+d} ms (at t={times[0]:.0f}s)"
                            res += f"\n({len(measurement_times)} points measured, R²={r_squared:.3f})"
                            console_log(f"Drift: {drift_rate:+.4f} ms/s – negligible", "ok")
                            _drift_atempo_tmp  = 1.0
                            _drift_initoff_tmp = init_off
                        else:
                            res += (
                                f"\n\nDrift detected: {drift_rate:+.4f} ms/s"
                                f"\natempo: {atempo:.8f}"
                                f"\nInitial offset: {round(init_off):+d} ms (at t={times[0]:.0f}s)"
                                f"\n({len(measurement_times)} points, R²={r_squared:.3f})"
                                f"\n→ Use Export to apply both corrections"
                            )
                            console_log(f"Drift: {drift_rate:+.4f} ms/s  atempo={atempo:.8f}", "warn")
                            _drift_atempo_tmp  = atempo
                            _drift_initoff_tmp = init_off
                else:
                    res += "\n\nDrift check skipped (file < 2 min)."
                    _drift_atempo_tmp  = 1.0
                    _drift_initoff_tmp = offset_ms
            except Exception as drift_err:
                res += f"\n\nDrift check failed: {drift_err}"
                console_log(f"Drift check error: {drift_err}", "error")
                _drift_atempo_tmp  = 1.0
                _drift_initoff_tmp = offset_ms

        # Store drift values on label_result for export_drift_corrected()
        label_result._offset_ms = offset_ms
        try:
            label_result._atempo   = _drift_atempo_tmp
            label_result._init_off = _drift_initoff_tmp
        except NameError:
            label_result._atempo   = 1.0
            label_result._init_off = float(offset_ms)

        def _finish_ui(_res=res, _color=color):
            label_result._offset_ms = offset_ms
            try:
                label_result._atempo   = _drift_atempo_tmp
                label_result._init_off = _drift_initoff_tmp
            except NameError:
                label_result._atempo   = 1.0
                label_result._init_off = float(offset_ms)
            label_result.config(text=_res, fg=_color)
            label_status.config(text="✓ Analysis completed successfully", fg="#00ff88")
            pass    # analyze button removed in 2.0
            set_progress(100, "Done")
            entry_manual_offset.delete(0, tk.END)
            entry_manual_offset.insert(0, str(offset_ms))
            root.after(50, update_visualization)
        # Store filtered waveforms and integer lag for the visualisation layer
        viz_data['data1'] = data1_filtered
        viz_data['data2'] = data2_filtered
        viz_data['sr'] = sr1
        viz_data['offset_samples'] = int(lag)
        viz_data['zoom_start'] = 0
        viz_data['zoom_end'] = len(data1_filtered)

        console_log(f"Result: offset = {offset_ms} ms  (total = {total_offset_ms} ms)", "ok")
        console_log("── Analysis complete ───────────────────────────", "bold")
        root.after(0, _finish_ui)
        return  # prevent falling into the except block

    except Exception as e:
        _err = str(e)
        root.after(0, lambda: (
            messagebox.showerror("Error", f"Analysis failed:\n{_err}"),
            label_status.config(text="✗ Error occurred during analysis", fg="#ff4444"),
            label_result.config(text="Could not analyze files.\nCheck if they are valid.", fg="#888888"),
            console_log(f"✗ Analysis failed: {_err}", "error"),
            None,   # analyze button removed in 2.0
            clear_progress()
        ))

# ===========================================================================
# TIMELINE ENGINE  (pure numpy/scipy – no Tk, testable on its own)
#
# The single-point workflow answers "what is the offset here?".  Real releases
# often need more: the target drifts against the reference, has a different
# intro, a missing scene or a re-timed recap.
#
# This engine measures the offset at many positions and fits a piecewise
# linear model
#
#     offset(t) = offset_i + slope_i * (t - t_start_i)      for segment i
#
#   slope != 0  -> tempo difference   (fixed with atempo)
#   jump        -> edit in the target (fixed by skipping or padding audio)
#
# Sign convention (same as the rest of the program):
#   offset > 0  -> the target is LATE at that moment
#   slope  > 0  -> the target is progressively later (it plays slower)
#   therefore the correction is atempo = 1 + slope/1000
#
# Both files are decoded once at SCAN_SR, so hundreds of measurements cost a
# couple of seconds instead of one FFmpeg run each.
# ===========================================================================

SCAN_SR = 16000               # working rate; 1 sample = 0.0625 ms
SCAN_BAND_LOW = 100.0
SCAN_BAND_HIGH = 7000.0
MIN_WINDOW_S = 4.0            # shorter windows cannot produce a stable peak
_SCAN_SOS_CACHE: dict = {}

# Envelope mode – the default for two different language versions, where the
# dialogue is re-recorded and only music / effects / line timing are shared.
ENVELOPE_HOP_S = 0.010        # 10 ms resolution before interpolation
ENVELOPE_WIN_S = 0.050        # 50 ms energy integration window
AUTO_WAVEFORM_MIN_CONF = 5.0  # above this, the waveform result is trusted
ENVELOPE_CONF_SCALE = 2.0     # envelope peaks are broader, so their prominence
                              # is smaller – scaled to keep one threshold valid
                              # for both modes

# codec -> (ffmpeg encoder, container extension)
_CODEC_MAP = {
    "ac3":    ("ac3",        ".ac3"),
    "eac3":   ("eac3",       ".eac3"),
    "aac":    ("aac",        ".aac"),
    "mp3":    ("libmp3lame", ".mp3"),
    "opus":   ("libopus",    ".opus"),
    "flac":   ("flac",       ".flac"),
    "vorbis": ("libvorbis",  ".ogg"),
    "dts":    ("dca",        ".dts"),
    "truehd": ("truehd",     ".thd"),
    "pcm_s16le": ("pcm_s16le", ".wav"),
    "pcm_s24le": ("pcm_s24le", ".wav"),
    "pcm_f32le": ("pcm_s16le", ".wav"),
}


def _scan_sos(sr: int = SCAN_SR):
    """Cached Butterworth band-pass used for every correlation window."""
    sos = _SCAN_SOS_CACHE.get(sr)
    if sos is None:
        nyq = sr / 2.0
        high = min(SCAN_BAND_HIGH / nyq, 0.99)
        sos = signal.butter(4, [SCAN_BAND_LOW / nyq, high],
                            btype="band", output="sos")
        _SCAN_SOS_CACHE[sr] = sos
    return sos


def _envelope_sos(sr: int = SCAN_SR):
    """
    Wider band for envelope analysis (40 Hz – 7 kHz).

    Bass and low midrange carry most of the shared music / effects energy, so
    keeping 40–100 Hz in the envelope helps a lot when the dialogue differs.
    """
    key = ("env", sr)
    sos = _SCAN_SOS_CACHE.get(key)
    if sos is None:
        nyq = sr / 2.0
        sos = signal.butter(4, [40.0 / nyq, min(SCAN_BAND_HIGH / nyq, 0.99)],
                            btype="band", output="sos")
        _SCAN_SOS_CACHE[key] = sos
    return sos


def _decode_track(path: str, tag: str, sr: int = SCAN_SR, on_fraction=None):
    """
    Decode the first audio stream of *path* to mono 16-bit PCM at *sr*.

    on_fraction(0..1) is called while FFmpeg works, so the caller can drive a
    progress bar – decoding a 20 GB remux takes minutes and used to look dead.

    Returns (sample_rate, int16 ndarray).  Raises RuntimeError with FFmpeg's
    own message when the file cannot be decoded.
    """
    out = os.path.join(tempfile.gettempdir(), f"ads_scan_{tag}_{os.getpid()}.wav")
    total = _get_file_duration(path) or 0.0
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
           "-i", path, "-vn", "-map", "0:a:0",
           "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", out]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL,
                                startupinfo=get_startupinfo())
        _ffmpeg_procs.append(proc)
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if on_fraction and total > 0 and line.startswith("out_time_us="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        continue
                    on_fraction(max(0.0, min(1.0, seconds / total)))
            stderr = proc.stderr.read()
        finally:
            try:
                _ffmpeg_procs.remove(proc)
            except ValueError:
                pass
            try:
                proc.stdout.close()
                proc.stderr.close()
            except OSError:
                pass
        rc = proc.wait()
        if rc != 0 or not os.path.isfile(out):
            details = stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(
                f"FFmpeg could not decode {os.path.basename(path)}"
                + (f": {details[-1]}" if details else ""))
        rate, data = wavfile.read(out)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass

    if data.ndim > 1:
        data = data.mean(axis=1)
    return rate, np.ascontiguousarray(data.astype(np.int16, copy=False))


def decode_pair(path_ref: str, path_target: str, progress=None, on_fraction=None):
    """
    Decode both files once and return (ref, target, meta).

    meta holds the sample rate and the durations used by the rest of the
    pipeline.  The arrays stay alive between measurements, which is what makes
    the interactive "measure here" button instant.

    on_fraction(phase, 0..1) drives the progress bar: phase is "ref" or "target".
    """
    def report(phase):
        if not on_fraction:
            return None
        return lambda frac: on_fraction(phase, frac)

    if progress:
        progress("Decoding reference…")
    _, ref = _decode_track(path_ref, "ref", on_fraction=report("ref"))
    if progress:
        progress("Decoding target…")
    _, tgt = _decode_track(path_target, "tgt", on_fraction=report("target"))
    sr = SCAN_SR
    meta = {
        "sr": sr,
        "ref_duration": len(ref) / float(sr),
        "target_duration": len(tgt) / float(sr),
        "duration": min(len(ref), len(tgt)) / float(sr),
    }
    return ref, tgt, meta


def _correlate_peak(x, y, hop_s: float, guard_s: float = 0.060):
    """
    Correlate two equally sampled signals and refine the peak.

    Works for both raw waveforms (hop = 1/sr) and energy envelopes
    (hop = 10 ms), which is why the time base is a parameter.

    guard_s is how far from the main peak a rival must be to count as one.
    Waveform peaks are a few samples wide, envelope peaks are tens of
    milliseconds wide, hence the different default values.

    Returns (offset_ms, confidence, ncc) with the same sign convention as the
    rest of the program: offset > 0 means y is late.
    """
    corr = signal.correlate(x, y, mode="full", method="fft")
    lags = signal.correlation_lags(len(x), len(y), mode="full")
    mi = int(np.argmax(np.abs(corr)))
    peak = float(corr[mi])
    lag = float(lags[mi])

    if 0 < mi < len(corr) - 1:
        y1, y2, y3 = abs(corr[mi - 1]), abs(corr[mi]), abs(corr[mi + 1])
        denom = 2.0 * y2 - y1 - y3
        if abs(denom) > 1e-12:
            lag += 0.5 * (y3 - y1) / denom

    guard = max(1, int(round(guard_s / hop_s)))
    masked = np.abs(corr).copy()
    masked[max(0, mi - guard): min(len(masked), mi + guard + 1)] = 0.0
    rival = float(masked.max()) if masked.size else 0.0
    prominence = abs(peak) / (rival + 1e-12)

    energy = float(np.sqrt(np.sum(x * x) * np.sum(y * y))) + 1e-12
    return float(-lag * hop_s * 1000.0), float(min(prominence, 99.0)), \
        float(abs(peak) / energy)


def _envelope_of(seg, sr: int, hop_s: float = ENVELOPE_HOP_S,
                 win_s: float = ENVELOPE_WIN_S):
    """
    Short-time RMS envelope of *seg*, log-compressed and mean/std normalised.

    Comparing envelopes instead of waveforms is what makes two different
    language versions comparable: the dialogue is re-recorded, so the waveform
    is unrelated, but the music, the effects and the rhythm of the lines are
    the same, and those all survive in the energy envelope.
    """
    win = max(2, int(round(win_s * sr)))
    hop = max(1, int(round(hop_s * sr)))
    if len(seg) < win + hop:
        return np.zeros(0)

    squares = np.concatenate(([0.0], np.cumsum(seg * seg)))
    starts = np.arange(0, len(seg) - win, hop)
    env = np.sqrt((squares[starts + win] - squares[starts]) / float(win))
    env = np.log1p(env)
    env -= env.mean()
    spread = env.std()
    return env / spread if spread > 1e-12 else np.zeros(0)


def measure_pair(a, b, t: float, window: float, sos=None, mode: str = "auto"):
    """
    Correlate the same [t, t+window] slice of both decoded tracks.

    mode:
        "waveform" – raw sample correlation (best for same-language sources)
        "envelope" – energy-envelope correlation (best for different dubs)
        "auto"     – waveform when it is convincing, envelope otherwise

    Returns {'offset', 'conf', 'ncc', 'mode'} or None for silent slices.
      offset – target delay in ms (positive = target late)
      conf   – peak prominence (best peak / strongest rival peak)
      ncc    – normalised correlation coefficient (informational, waveform only)
    """
    sr = SCAN_SR
    sos = sos if sos is not None else _scan_sos(sr)
    i0 = int(round(max(0.0, t) * sr))
    n = int(round(window * sr))
    i1 = i0 + n
    if n < int(MIN_WINDOW_S * sr) or i1 > len(a) or i1 > len(b):
        return None

    x = a[i0:i1].astype(np.float64)
    y = b[i0:i1].astype(np.float64)
    if x.std() < 1e-9 or y.std() < 1e-9:
        return None

    x -= x.mean()
    y -= y.mean()
    x /= (np.sqrt(np.mean(x * x)) + 1e-12)
    y /= (np.sqrt(np.mean(y * y)) + 1e-12)

    wave = None
    if mode in ("auto", "waveform"):
        xf = signal.sosfilt(sos, x)
        yf = signal.sosfilt(sos, y)
        offset, conf, ncc = _correlate_peak(xf, yf, 1.0 / sr)
        wave = {"offset": round(offset, 2), "conf": round(conf, 2),
                "ncc": round(ncc, 3), "mode": "w"}

    if mode == "waveform":
        return wave
    if mode == "auto" and wave is not None and wave["conf"] >= AUTO_WAVEFORM_MIN_CONF:
        return wave

    env_x = _envelope_of(signal.sosfilt(_envelope_sos(sr), x), sr)
    env_y = _envelope_of(signal.sosfilt(_envelope_sos(sr), y), sr)
    env = None
    if len(env_x) > 8 and len(env_y) > 8:
        offset, conf, _ncc = _correlate_peak(env_x, env_y, ENVELOPE_HOP_S,
                                             guard_s=0.400)
        env = {"offset": round(offset, 2),
               "conf": round(min(conf * ENVELOPE_CONF_SCALE, 99.0), 2),
               "ncc": 0.0, "mode": "e"}

    if mode == "envelope":
        return env
    if env is None:
        return wave
    if wave is None:
        return env
    return env if env["conf"] > wave["conf"] else wave


def refine_breakpoints(a, b, plan, duration: float,
                       half_span: float = 45.0, window: float = 6.0,
                       step: float = 2.0, mode: str = "auto", should_stop=None):
    """
    Re-measure around every detected jump with short windows.

    A 40 s analysis window blurs the exact position of an edit to about
    half a window.  Scanning the neighbourhood with 6 s windows pins the
    breakpoint down to a couple of seconds, which is what decides where the
    export cuts the audio.
    """
    extra = []
    for jump in plan.get("jumps") or []:
        if should_stop and should_stop():
            break
        low = max(0.0, float(jump["t"]) - half_span)
        high = min(float(duration), float(jump["t"]) + half_span)
        if high - low < window:
            continue
        extra.extend(scan_arrays(a, b, window=window, step=step,
                                 t_from=low, t_to=high, mode=mode,
                                 should_stop=should_stop))
    return extra


def scan_arrays(a, b, window: float = 40.0, step: float = 30.0,
                t_from: float = 0.0, t_to=None, mode: str = "auto",
                progress=None, should_stop=None):
    """
    Measure the offset at regular positions across the timeline.

    mode is forwarded to measure_pair(): "auto", "waveform" or "envelope".

    Returns a list of points: {'t','offset','conf','ncc','mode','window','kind'}.
    't' is the centre of the analysed window, which is where a drifting signal
    gives an unbiased reading.
    """
    duration = min(len(a), len(b)) / float(SCAN_SR)
    t_to = duration if t_to is None else min(float(t_to), duration)
    t_from = max(0.0, float(t_from))
    window = max(MIN_WINDOW_S, float(window))
    step = max(1.0, float(step))
    if t_to - t_from < MIN_WINDOW_S:
        return []

    sos = _scan_sos()
    total = max(1, int(np.ceil((t_to - t_from) / step)))
    points = []
    done = 0
    t = t_from

    while t + MIN_WINDOW_S <= t_to:
        w = min(window, t_to - t)
        if w < MIN_WINDOW_S:
            break
        result = measure_pair(a, b, t, w, sos, mode=mode)
        if result is not None:
            result["t"] = round(t + w / 2.0, 2)
            result["window"] = round(w, 2)
            result["kind"] = "auto"
            points.append(result)
        done += 1
        if progress:
            progress(done, total)
        if should_stop and should_stop():
            break
        t += step

    return points


def shift_plan(plan: dict, delta_ms: float) -> dict:
    """
    Return a copy of *plan* with every offset moved by *delta_ms*.

    Used by the post-export calibration: the model is shifted as a whole, the
    drift (slope) and the jumps stay untouched.
    """
    import copy
    out = copy.deepcopy(plan)
    for segment in out.get("segments", []):
        segment["offset"] = round(float(segment["offset"]) + delta_ms, 2)
    out["initial_offset"] = round(float(out.get("initial_offset", 0.0)) + delta_ms, 2)
    out["calibration_ms"] = round(float(plan.get("calibration_ms", 0.0)) + delta_ms, 2)
    return out


def measure_residual(ref_path: str, out_path: str,
                     window: float = 40.0, step: float = 120.0):
    """
    Measure what is left between the reference and a produced file.

    Decodes both, measures the offset along the timeline (envelope mode – the
    languages differ) and returns (median_ms, mad_ms, points).  Positive means
    the produced file is late.  Returns None when nothing usable was measured.
    """
    ref, produced, _meta = decode_pair(ref_path, out_path)
    points = scan_arrays(ref, produced, window=window, step=step, mode="envelope")
    del ref, produced

    offsets = [float(p["offset"]) for p in points if p.get("conf", 0) >= 1.0]
    if not offsets:
        return None
    values = np.array(offsets)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return median, mad, len(values)


def framerate_slope(fps_ref: float, fps_target: float) -> float:
    """
    Drift implied by a framerate pair, in ms per second.

    A moment τ of the reference appears in the target at τ · fps_ref/fps_target
    (a PAL 25 fps version of a 23.976 fps master runs 4.27 % fast), so the
    offset grows by (fps_ref / fps_target − 1) seconds per second.

    The exact correction factor used by the export is therefore
    1 + slope/1000 = fps_ref / fps_target.
    """
    if fps_ref <= 0 or fps_target <= 0:
        return 0.0
    return (fps_ref / fps_target - 1.0) * 1000.0


def format_residual(residual) -> str:
    """
    Sign-explicit, human-readable description of a residual measurement.

    The raw number is an offset (positive = the produced audio is late), while
    what you type into a muxer is a *delay* (positive = push the audio later),
    which is the opposite sign.  Both are printed so there is no guessing.
    """
    if not residual:
        return ""
    median, mad, count = residual
    if abs(median) < 1.0:
        state = "in sync"
    elif median < 0:
        state = f"audio EARLY by {abs(median):.0f} ms"
    else:
        state = f"audio LATE by {median:.0f} ms"

    text = (f"offset {median:+.1f} ms → {state} "
            f"(spread {mad:.0f} ms, {count} points)")
    if abs(median) >= 1.0:
        delay = -median
        action = "delay the audio" if delay > 0 else "advance the audio"
        text += f"\nmux delay {delay:+.0f} ms  ({action})"
    return text


def plan_slope(plan) -> float:
    """Representative drift rate of a plan (median of the segment slopes)."""
    segments = plan.get("segments") or []
    if not segments:
        return 0.0
    return float(np.median([float(s["slope"]) for s in segments]))


def resample_track(data, rate: float):
    """
    Resample a whole track by *rate* (> 1 = faster / shorter).

    Used to remove a measured drift before scanning again: once the target is
    played at the reference speed, what is left is a constant offset, and the
    correlation peak stops being smeared by the tempo difference.
    """
    if abs(rate - 1.0) < 1e-6 or len(data) == 0:
        return data
    out_len = max(1, int(round(len(data) / rate)))
    source = np.arange(len(data), dtype=np.float64)
    target = np.arange(out_len, dtype=np.float64) * rate
    warped = np.interp(target, source, data.astype(np.float32))
    return warped.astype(np.int16)


def compensate_points(points, rate: float):
    """
    Convert offsets measured on a resampled target back to the original one.

    With r = 1 + slope/1000 the warped measurement is   o'(t) = o0 / r,  so
    the true model is  o(t) = o'(t) * r + (r - 1) * 1000 * t.
    """
    if abs(rate - 1.0) < 1e-6:
        return points
    out = []
    for point in points:
        copy = dict(point)
        copy["offset"] = round(
            float(point["offset"]) * rate
            + (rate - 1.0) * 1000.0 * float(point["t"]), 2)
        out.append(copy)
    return out


def _fit_group(group, fallback_slope: float, outlier_ms: float):
    """
    Robust straight line through one group of points.

    The previous version used a plain least-squares fit with a fixed 120 ms
    rejection radius, so a cluster of noisy measurements (50-150 ms off, which
    happens with different dubs) dragged the line and produced fictitious jumps.

    Now: Theil-Sen for the initial slope (median of pairwise slopes, immune to
    outliers), then least squares on inliers whose radius adapts to the measured
    spread (5 x MAD), never wider than the caller's limit.
    """
    pts = [dict(p) for p in group]
    ts = np.array([p["t"] for p in pts], dtype=float)
    ys = np.array([p["offset"] for p in pts], dtype=float)

    if len(pts) >= 3 and float(ts.max() - ts.min()) > 0.5:
        slopes = []
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                dt = ts[b] - ts[a]
                if abs(dt) > 0.5:
                    slopes.append((ys[b] - ys[a]) / dt)
        slope = float(np.median(slopes)) if slopes else float(fallback_slope)
        intercept = float(np.median(ys - slope * ts))
    else:
        slope = float(fallback_slope)
        intercept = float(ys[0] - slope * ts[0])

    resid = ys - (slope * ts + intercept)
    for _ in range(5):
        mad = float(np.median(np.abs(resid - np.median(resid))))
        radius = min(outlier_ms, max(12.0, 5.0 * mad))
        keep = np.abs(resid) <= radius
        if bool(keep.all()) or int(keep.sum()) < 2:
            break
        pts = [p for p, k in zip(pts, keep) if k]
        ts = np.array([p["t"] for p in pts], dtype=float)
        ys = np.array([p["offset"] for p in pts], dtype=float)
        if float(ts.max() - ts.min()) <= 0.5:
            break
        slope, intercept = np.polyfit(ts, ys, 1)
        resid = ys - (slope * ts + intercept)

    rms = float(np.sqrt(np.mean(resid ** 2))) if len(resid) else 0.0
    return float(slope), float(intercept), pts, rms


def build_plan(points, duration: float, min_conf: float = 2.0,
               jump_ms: float = 150.0, outlier_ms: float = 120.0,
               merge_ms: float = 60.0, slope_hint: float = None):
    """
    Fit a piecewise-linear offset model to the measured points.

    min_conf is the minimum peak prominence (see measure_pair) a point needs to
    take part in the fit.

    Returns:
        {
          "segments": [{"t_start","t_end","offset","slope","atempo","n","rms"}],
          "jumps":    [{"t","delta","mode"}],     mode: skip (delta>0) / pad
          "initial_offset": float,
          "warnings": [str],
          "duration": float,
        }
    """
    warnings = []
    usable = [dict(p) for p in points
              if p.get("enabled", True)
              and float(p.get("conf", p.get("ncc", 0.0))) >= min_conf]
    usable.sort(key=lambda p: float(p["t"]))

    if not usable:
        return {"segments": [], "jumps": [], "initial_offset": 0.0,
                "warnings": ["No points above the confidence threshold."],
                "duration": duration}

    # ---- 1. robust drift estimate over all consecutive gaps ---------------
    gap_slopes = []
    for p, q in zip(usable, usable[1:]):
        dt = float(q["t"]) - float(p["t"])
        if dt > 0.5:
            gap_slopes.append((float(q["offset"]) - float(p["offset"])) / dt)
    base_slope = float(np.median(gap_slopes)) if gap_slopes else 0.0
    if abs(base_slope) > 25.0:
        explained = (slope_hint is not None
                     and abs(base_slope - slope_hint) <= max(2.0, abs(slope_hint) * 0.2))
        if not explained:
            # 25 ms/s is 2.5 % tempo – a real drift never gets that far.
            warnings.append("Global slope is implausible for drift (>25 ms/s) – ignored.")
            base_slope = 0.0

    # ---- 2. breakpoints: offset jumps that the drift cannot explain -------
    breaks = []
    for i in range(1, len(usable)):
        dt = float(usable[i]["t"]) - float(usable[i - 1]["t"])
        delta = float(usable[i]["offset"]) - float(usable[i - 1]["offset"])
        if abs(delta - base_slope * dt) > jump_ms:
            breaks.append(i)

    groups, start = [], 0
    for i in breaks:
        groups.append(usable[start:i])
        start = i
    groups.append(usable[start:])

    # A lone point cannot define a segment – far more often it is a bad
    # measurement that merely looked like a jump.  Fold such groups into their
    # neighbour; the outlier rejection in _fit_group drops them afterwards.
    folded = []
    for group in groups:
        if folded and len(group) < 2:
            folded[-1].extend(group)
        else:
            folded.append(list(group))
    if len(folded) > 1 and len(folded[0]) < 2:
        folded[1] = folded[0] + folded[1]
        folded.pop(0)
    groups = folded

    # ---- 3. fit each group, then merge groups that describe the same line -
    fits = []
    for group in groups:
        slope, intercept, kept, rms = _fit_group(group, base_slope, outlier_ms)
        if len(kept) < len(group):
            warnings.append(
                f"Dropped {len(group) - len(kept)} outlier(s) near t={group[0]['t']:.0f}s.")
        fits.append({"slope": slope, "intercept": intercept, "kept": kept,
                     "rms": rms, "n": len(kept)})

    merged = True
    while merged and len(fits) > 1:
        merged = False
        for i in range(len(fits) - 1):
            left, right = fits[i], fits[i + 1]
            if not left["kept"] or not right["kept"]:
                continue
            boundary = 0.5 * (left["kept"][-1]["t"] + right["kept"][0]["t"])
            left_at = left["slope"] * boundary + left["intercept"]
            right_at = right["slope"] * boundary + right["intercept"]
            if (abs(right_at - left_at) < merge_ms
                    and abs(right["slope"] - left["slope"]) < 0.2):
                combined = left["kept"] + right["kept"]
                slope, intercept, kept, rms = _fit_group(
                    combined, left["slope"], outlier_ms)
                fits[i:i + 2] = [{"slope": slope, "intercept": intercept,
                                  "kept": kept, "rms": rms, "n": len(kept)}]
                merged = True
                break

    # ---- 4. turn the fits into segments spanning the whole duration -------
    segments = []
    for idx, fit in enumerate(fits):
        t_start = 0.0 if idx == 0 else 0.5 * (
            fits[idx - 1]["kept"][-1]["t"] + fit["kept"][0]["t"])
        t_end = (0.5 * (fit["kept"][-1]["t"] + fits[idx + 1]["kept"][0]["t"])
                 if idx + 1 < len(fits) else float(duration))
        if t_end <= t_start:
            t_end = min(float(duration), t_start + 1.0)
        if idx == 0:
            t_start = 0.0
        offset_at_start = fit["slope"] * t_start + fit["intercept"]
        segments.append({
            "t_start": round(float(t_start), 3),
            "t_end": round(float(t_end), 3),
            "offset": round(float(offset_at_start), 2),
            "slope": round(fit["slope"], 5),
            "atempo": round(1.0 + fit["slope"] / 1000.0, 8),
            "n": fit["n"],
            "rms": round(fit["rms"], 2),
        })

    # ---- 5. optional: lock the drift to a known framerate ratio -----------
    if slope_hint is not None:
        tolerance = max(0.5, abs(slope_hint) * 0.10)
        locked = 0
        for seg in segments:
            if abs(seg["slope"] - slope_hint) <= tolerance:
                # keep the value at the segment centre, rotate the line to the
                # exact ratio: the centre is the most reliable point of a fit
                centre = 0.5 * (seg["t_start"] + seg["t_end"])
                centre_value = seg["offset"] + seg["slope"] * (centre - seg["t_start"])
                seg["slope"] = round(float(slope_hint), 5)
                seg["offset"] = round(centre_value - slope_hint * (centre - seg["t_start"]), 2)
                seg["atempo"] = round(1.0 + slope_hint / 1000.0, 8)
                seg["locked"] = True
                locked += 1
        if locked:
            warnings.append(
                f"Drift locked to the framerate ratio ({slope_hint:+.4f} ms/s → "
                f"atempo {1.0 + slope_hint / 1000.0:.8f}) in {locked} segment(s).")

    # ---- 5b. drop segments that are just one line split in three ---------
    # Locking the slope can make neighbouring segments identical; keeping them
    # apart would add pointless audio cuts at the seams.
    if len(segments) > 1:
        merged_segments = [segments[0]]
        for seg in segments[1:]:
            prev = merged_segments[-1]
            boundary = seg["t_start"]
            prev_at = prev["offset"] + prev["slope"] * (boundary - prev["t_start"])
            if (abs(seg["offset"] - prev_at) < merge_ms
                    and abs(seg["slope"] - prev["slope"]) < 0.05):
                prev["t_end"] = seg["t_end"]
                prev["n"] += seg["n"]
                prev["rms"] = round(max(prev["rms"], seg["rms"]), 2)
            else:
                merged_segments.append(seg)
        segments = merged_segments

    # ---- 6. jumps + sanity warnings --------------------------------------
    jumps = []
    for prev, cur in zip(segments, segments[1:]):
        boundary = cur["t_start"]
        before = prev["offset"] + prev["slope"] * (boundary - prev["t_start"])
        delta = cur["offset"] - before
        jumps.append({"t": boundary, "delta": round(delta, 2),
                      "mode": "skip" if delta > 0 else "pad"})

    for seg in segments:
        if seg["n"] < 2:
            warnings.append(
                f"Segment {seg['t_start']:.0f}–{seg['t_end']:.0f}s rests on a "
                "single point – drift there is an assumption.")
        if abs(seg["slope"]) > 8.0 and not seg.get("locked"):
            warnings.append(
                f"Very large drift in {seg['t_start']:.0f}–{seg['t_end']:.0f}s "
                f"({seg['slope']:+.2f} ms/s) – check the points there.")
        if seg["rms"] > outlier_ms:
            warnings.append(
                f"Poor fit in {seg['t_start']:.0f}–{seg['t_end']:.0f}s "
                f"(RMS {seg['rms']:.0f} ms).")
    if usable and usable[-1]["t"] < duration - 90:
        warnings.append(
            f"Last measurement is at {usable[-1]['t']:.0f}s – the final "
            f"{duration - usable[-1]['t']:.0f}s is not covered.")

    return {
        "segments": segments,
        "jumps": jumps,
        "initial_offset": segments[0]["offset"] if segments else 0.0,
        "warnings": warnings,
        "duration": float(duration),
    }


def plan_lines(plan) -> list:
    """Human-readable lines describing a plan (console + summary panel)."""
    lines = []
    segs = plan.get("segments") or []
    if not segs:
        return ["(empty plan)"]
    lines.append(f"Initial offset : {plan.get('initial_offset', 0.0):+.2f} ms")
    for i, s in enumerate(segs):
        drift = s["slope"]
        tempo = "(none)" if abs(drift) < 0.05 else f"atempo {s['atempo']:.8f}"
        lines.append(
            f"Seg {i + 1}: {s['t_start']:7.1f}–{s['t_end']:7.1f}s  "
            f"offset {s['offset']:+8.2f} ms  drift {drift:+7.3f} ms/s  "
            f"{tempo}  [{s['n']} pts, RMS {s['rms']:.0f} ms]")
    for j in plan.get("jumps") or []:
        lines.append(f"Jump  @ {j['t']:7.1f}s : {j['delta']:+.1f} ms "
                     f"→ {'skip audio' if j['mode'] == 'skip' else 'pad silence'}")
    for warning in plan.get("warnings") or []:
        lines.append(f"! {warning}")
    return lines


def mka_path(path: str) -> str:
    """Force a Matroska Audio extension – the only container this tool writes.

    MKA keeps the real duration in the container and muxes without surprises,
    unlike bare .aac (ADTS) or .ac3 files.
    """
    root, ext = os.path.splitext(path)
    if ext.lower() == ".mka":
        return path
    return (root if ext else path) + ".mka"


def probe_framerate(path: str) -> float:
    """
    First video stream framerate, or 0.0 when it cannot be read.

    ffprobe reports it as a rational ("24000/1001"), so it is converted to a
    float; this is what fills the framerate hint automatically.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
            startupinfo=get_startupinfo())
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if "/" in line:
                num, den = line.split("/", 1)
                if float(den) > 0:
                    return round(float(num) / float(den), 6)
            else:
                return round(float(line), 6)
    except Exception:
        pass
    return 0.0


def _probe_audio_stream(path: str) -> dict:
    """
    Read codec / bitrate / channels / sample rate of the first audio stream.

    Falls back to sane AC-3 defaults so an export can always be attempted.
    """
    info = {"codec": "ac3", "bitrate": "256k", "channels": 2, "sample_rate": 48000}
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate,channels,sample_rate",
             "-of", "json", path],
            capture_output=True, text=True, startupinfo=get_startupinfo())
        stream = (json.loads(probe.stdout).get("streams") or [{}])[0]
        if stream.get("codec_name"):
            info["codec"] = stream["codec_name"].lower()
        if stream.get("bit_rate") and stream["bit_rate"] != "N/A":
            info["bitrate"] = f"{int(stream['bit_rate']) // 1000}k"
        if stream.get("channels"):
            info["channels"] = int(stream["channels"])
        if stream.get("sample_rate"):
            info["sample_rate"] = int(stream["sample_rate"])
    except Exception:
        pass
    return info


def plan_to_filter(plan: dict, target_duration: float):
    """
    Turn a plan into an FFmpeg filter chain that retimes the target audio.

    For every segment the target is cut at the position the model asks for,
    played at the segment's atempo and concatenated back together.  A negative
    jump (the target is missing content) becomes silence padding; a positive
    jump (the target has extra content) simply skips it.

    Returns (filter_parts, output_label, used_segments, notes).
    """
    segments = plan.get("segments") or []
    if not segments:
        raise ValueError("The plan has no segments.")

    parts = []
    used = []
    notes = []
    prev_end = 0.0

    for seg in segments:
        a0, a1 = float(seg["t_start"]), float(seg["t_end"])
        span = a1 - a0
        if span < 0.05:
            continue

        rate = 1.0 + float(seg["slope"]) / 1000.0
        rate = min(max(rate, 0.5), 2.0)
        model_b = a0 + float(seg["offset"]) / 1000.0

        pad = 0.0
        b0 = model_b
        if b0 < prev_end:                    # cannot rewind – pad the gap
            pad = prev_end - b0
            notes.append(f"{a0:.1f}s: needs audio before {b0:.2f}s → "
                         f"{pad * 1000:.0f} ms of silence")
            b0 = prev_end
        elif b0 < 0:                         # target starts later than the model
            pad = -b0
            b0 = 0.0

        b1 = b0 + (span - pad) * rate
        b0 = min(b0, target_duration)
        b1 = min(max(b1, b0 + 0.02), target_duration)

        if b1 <= b0 + 0.01:                  # target exhausted – pure silence
            pad = span
            b1 = b0

        used.append({"a0": a0, "a1": a1, "b0": b0, "b1": b1,
                     "rate": rate, "pad_ms": pad * 1000.0})
        prev_end = b1

    if not used:
        raise ValueError("Nothing to export – the plan covers no audio.")

    # pad the tail when the target runs out before the reference does
    covered = 0.0
    for u in used:
        covered += u["pad_ms"] / 1000.0 + (u["b1"] - u["b0"]) / u["rate"]
    tail_pad = max(0.0, (used[-1]["a1"] - used[0]["a0"]) - covered)
    if tail_pad > 0.01:
        notes.append(f"{tail_pad * 1000:.0f} ms of silence appended at the end")

    for i, u in enumerate(used):
        chain = (f"[0:a]atrim=start={u['b0']:.6f}:end={u['b1']:.6f},"
                 f"asetpts=N/SR/TB,atempo={u['rate']:.8f}")
        if u["pad_ms"] > 0.5:
            chain += f",adelay={int(round(u['pad_ms']))}:all=1"
        if i == len(used) - 1 and tail_pad > 0.01:
            chain += f",apad=pad_dur={tail_pad:.3f}"
        parts.append(chain + f"[s{i}]")

    if len(used) == 1:
        parts.append("[s0]anull[aout]")
    else:
        labels = "".join(f"[s{i}]" for i in range(len(used)))
        parts.append(f"{labels}concat=n={len(used)}:v=0:a=1[aout]")

    return parts, "[aout]", used, notes


def export_plan(plan: dict, target_path: str, out_path: str,
                progress=None, log=None, should_stop=None) -> None:
    """
    Apply *plan* to *target_path* and write *out_path*.

    Two passes, exactly like the single-point exporter: filters → lossless WAV,
    then WAV → the original codec at the original bitrate.  That keeps the
    container duration metadata correct and performs a single lossy encode.

    Progress is read from FFmpeg itself (-progress pipe:1), so the bar moves
    smoothly through both passes and the elapsed time is shown.  should_stop
    is polled so the user can cancel a long export.
    """
    info = _probe_audio_stream(target_path)
    encoder, _ext = _CODEC_MAP.get(info["codec"], ("ac3", ".ac3"))
    channels, sample_rate = info["channels"], info["sample_rate"]

    probe_dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", target_path],
        capture_output=True, text=True, startupinfo=get_startupinfo())
    try:
        target_duration = float(probe_dur.stdout.strip())
    except ValueError:
        target_duration = float(plan.get("duration", 0.0))

    parts, out_label, used, notes = plan_to_filter(plan, target_duration)
    graph = ";".join(parts)

    tmp_wav = os.path.join(tempfile.gettempdir(), "ads_plan_export.wav")
    cmd1 = ["ffmpeg", "-y", "-i", target_path, "-vn",
            "-filter_complex", graph, "-map", out_label,
            "-ac", str(channels), "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", tmp_wav]
    preserve = ["-ac", str(channels), "-ar", str(sample_rate)]
    cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + preserve + [
        "-c:a", encoder, "-b:a", info["bitrate"], out_path]

    if log:
        log(f"Applying {len(used)} segment(s) → {info['codec']} "
            f"{info['bitrate']} {channels}ch {sample_rate}Hz", "bold")
        for note in notes:
            log(f"  {note}", "warn")
        log("ffmpeg " + " ".join(cmd1[1:]), "cmd")

    span = float(plan.get("duration") or 0.0)
    try:
        if progress:
            progress(1, "Step 1/2 – applying corrections…")
        _run_ffmpeg_progress(cmd1, span, 1, 55, report=progress,
                             should_stop=should_stop)
        if progress:
            progress(56, "Step 2/2 – encoding…")
        _run_ffmpeg_progress(cmd2, span, 56, 99, report=progress,
                             should_stop=should_stop)
    finally:
        try:
            os.remove(tmp_wav)
        except OSError:
            pass
    if progress:
        progress(100, "Export done")

    if log:
        log(f"✓ Saved: {out_path}", "ok")


# ===========================================================================
# TIMELINE WINDOW
#
# Interactive front-end for the engine above: scan the whole file, look at the
# offset curve, add measurements by hand (or with a single click on the chart),
# inspect the fitted segments and export the corrected audio.
# ===========================================================================

BG_DARK = "#1a1a1a"
BG_PANEL = "#252525"
BG_INPUT = "#2d2d2d"
FG_DIM = "#aaaaaa"
FG_ACCENT = "#4a9eff"


class TimelinePanel:
    """Non-modal window for multi-point offset analysis."""

    def __init__(self, parent, ref_path: str, target_path: str):
        self.parent = parent
        self.ref_path = ref_path
        self.target_path = target_path

        self.ref_data = None          # decoded 16 kHz int16 arrays
        self.tgt_data = None
        self.meta = None
        self.scan_rate = 1.0          # 1.0, or the drift compensation factor
        self.points = []              # measurement points
        self.last_residual = None     # measured offset of the last export
        self.plan = None
        self.worker = None
        self.stop_event = threading.Event()
        self.queue = queue.Queue()

        self._build_ui()
        self._poll()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        win = tk.Frame(self.parent, bg=BG_DARK)
        self.frame = win
        win.pack(fill=tk.BOTH, expand=True)

        # (file names are shown by the Timeline tab header)

        # ---- scan bar ----------------------------------------------------
        bar = tk.LabelFrame(win, text=" Scan ", bg=BG_PANEL, fg=FG_ACCENT,
                            font=("Segoe UI", 9, "bold"), relief=tk.GROOVE, bd=2)
        bar.pack(fill=tk.X, padx=14, pady=(0, 8))

        row = tk.Frame(bar, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=12, pady=8)

        def _spin(parent, label, value, width=6, inc=1.0):
            tk.Label(parent, text=label, bg=BG_PANEL, fg=FG_DIM,
                     font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 3))
            var = tk.StringVar(value=str(value))
            tk.Spinbox(parent, from_=1, to=100000, increment=inc, width=width,
                       textvariable=var, bg=BG_INPUT, fg="#ffffff",
                       insertbackground="white", relief=tk.FLAT,
                       font=("Segoe UI", 9), justify=tk.CENTER,
                       buttonbackground="#3a3a3a").pack(side=tk.LEFT, padx=(0, 12))
            return var

        self.var_window = _spin(row, "Window (s):", 40, 6, 5)
        self.var_step = _spin(row, "Step (s):", 30, 6, 5)
        self.var_conf = _spin(row, "Min confidence:", 2.0, 5, 0.5)

        tk.Label(row, text="Signal:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 3))
        self.var_signal = tk.StringVar(value="Auto")
        signal_menu = tk.OptionMenu(row, self.var_signal,
                                    "Auto", "Waveform", "Envelope (dubs)")
        signal_menu.config(bg=BG_INPUT, fg="#ffffff", relief=tk.FLAT,
                           highlightthickness=0, font=("Segoe UI", 8),
                           activebackground="#3a3a3a", activeforeground="#ffffff",
                           width=15, anchor=tk.W)
        signal_menu["menu"].config(bg=BG_INPUT, fg="#ffffff",
                                   activebackground="#2a7de1",
                                   activeforeground="#ffffff",
                                   font=("Segoe UI", 8))
        signal_menu.pack(side=tk.LEFT, padx=(0, 12))

        # optional framerate hint – turns a noisy measured drift into the exact
        # ratio and lets the scan start already compensated (PAL/NTSC cases)
        fps_row = tk.Frame(bar, bg=BG_PANEL)
        fps_row.pack(fill=tk.X, padx=12, pady=(0, 4))

        tk.Label(fps_row, text="Framerate (optional) – reference:", bg=BG_PANEL,
                 fg=FG_DIM, font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self.var_fps_ref = tk.StringVar(value="")
        tk.Entry(fps_row, textvariable=self.var_fps_ref, width=7, bg=BG_INPUT,
                 fg="#ffffff", insertbackground="white", relief=tk.FLAT,
                 font=("Segoe UI", 9), justify=tk.CENTER).pack(
            side=tk.LEFT, padx=(4, 6), ipady=2)
        tk.Label(fps_row, text="target:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self.var_fps_tgt = tk.StringVar(value="")
        tk.Entry(fps_row, textvariable=self.var_fps_tgt, width=7, bg=BG_INPUT,
                 fg="#ffffff", insertbackground="white", relief=tk.FLAT,
                 font=("Segoe UI", 9), justify=tk.CENTER).pack(
            side=tk.LEFT, padx=(4, 6), ipady=2)
        tk.Label(fps_row, text="fps   e.g. 23.976 / 25 – locks atempo to the exact ratio",
                 bg=BG_PANEL, fg="#666666",
                 font=("Segoe UI", 7)).pack(side=tk.LEFT, padx=(4, 0))

        self.btn_scan = tk.Button(row, text="⚡ Auto-scan whole file", command=self.auto_scan,
                                  bg="#28a745", fg="#ffffff", relief=tk.FLAT,
                                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                                  activebackground="#23923d", padx=14, pady=4)
        self.btn_scan.pack(side=tk.LEFT, padx=(4, 6))

        self.btn_stop = tk.Button(row, text="Stop", command=self.stop_scan,
                                  bg="#7a2a1a", fg="#ffffff", relief=tk.FLAT,
                                  font=("Segoe UI", 9), cursor="hand2",
                                  activebackground="#8a3a2a", padx=10, pady=4,
                                  state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready. Run an auto-scan, or click on the chart.")

        self.progress_var = tk.DoubleVar(value=0)
        # detect the framerates so the drift hint is never forgotten
        ref_fps = probe_framerate(self.ref_path)
        tgt_fps = probe_framerate(self.target_path)
        if ref_fps > 0:
            self.var_fps_ref.set(f"{ref_fps:g}")
        if tgt_fps > 0:
            self.var_fps_tgt.set(f"{tgt_fps:g}")


        # ---- body --------------------------------------------------------
        body = tk.Frame(win, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=14)

        left = tk.LabelFrame(body, text=" Measurement points ", bg=BG_PANEL,
                             fg=FG_ACCENT, font=("Segoe UI", 9, "bold"),
                             relief=tk.GROOVE, bd=2, width=380)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        left.pack_propagate(False)
        self.points_frame = left

        cols = ("idx", "t", "off", "conf", "kind")
        self.tree = ttk.Treeview(left, columns=cols, show="headings",
                                 style="Ads.Treeview", height=5)
        for col, head, width, anchor in (
                ("idx", "#", 30, tk.CENTER),
                ("t", "Time (s)", 80, tk.CENTER),
                ("off", "Offset (ms)", 100, tk.E),
                ("conf", "Conf", 50, tk.E),
                ("kind", "Source", 80, tk.CENTER)):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, anchor=anchor, stretch=False)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

        pts_btns = tk.Frame(left, bg=BG_PANEL)
        pts_btns.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Button(pts_btns, text="Enable/Disable", command=self.toggle_selected,
                  bg="#333333", fg="#ffffff", relief=tk.FLAT, font=("Segoe UI", 8),
                  cursor="hand2", activebackground="#444444").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3), ipady=3)
        tk.Button(pts_btns, text="Delete", command=self.delete_selected,
                  bg="#7a2a1a", fg="#ffffff", relief=tk.FLAT, font=("Segoe UI", 8),
                  cursor="hand2", activebackground="#8a3a2a").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0), ipady=3)

        manual = tk.Frame(left, bg=BG_PANEL)
        manual.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Label(manual, text="At", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8)).grid(row=0, column=0, sticky=tk.W)
        self.entry_time = tk.Entry(manual, bg=BG_INPUT, fg="#ffffff", width=10,
                                   insertbackground="white", relief=tk.FLAT,
                                   font=("Segoe UI", 9), justify=tk.CENTER)
        self.entry_time.insert(0, "0:00")
        self.entry_time.grid(row=0, column=1, padx=4, pady=2, sticky=tk.EW)
        tk.Label(manual, text="offset (ms)", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8)).grid(row=0, column=2, padx=(6, 2))
        self.entry_offset = tk.Entry(manual, bg=BG_INPUT, fg="#ffffff", width=8,
                                     insertbackground="white", relief=tk.FLAT,
                                     font=("Segoe UI", 9), justify=tk.CENTER)
        self.entry_offset.insert(0, "0")
        self.entry_offset.grid(row=0, column=3, padx=(0, 4), pady=2, sticky=tk.EW)

        tk.Button(manual, text="Add manual point", command=self.add_manual_point,
                  bg="#2a7de1", fg="#ffffff", relief=tk.FLAT, font=("Segoe UI", 8),
                  cursor="hand2", activebackground="#3d8bff").grid(
            row=1, column=0, columnspan=4, sticky=tk.EW, pady=(4, 2), ipady=3)

        tk.Button(manual, text="Measure here (uses Window setting)",
                  command=self.measure_from_entry, bg="#333333", fg="#ffffff",
                  relief=tk.FLAT, font=("Segoe UI", 8), cursor="hand2",
                  activebackground="#444444").grid(
            row=2, column=0, columnspan=4, sticky=tk.EW, ipady=3)
        manual.columnconfigure(1, weight=1)
        manual.columnconfigure(3, weight=1)

        # chart
        chart_box = tk.LabelFrame(body, text=" Offset over time ", bg=BG_PANEL,
                                  fg=FG_ACCENT, font=("Segoe UI", 9, "bold"),
                                  relief=tk.GROOVE, bd=2)
        chart_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.fig = Figure(figsize=(7, 2.4), dpi=90, facecolor="#1a1a1a")
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor("#1a1a1a")
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_box)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.canvas.mpl_connect("button_press_event", self._on_chart_click)
        self._draw()

        # ---- segments ----------------------------------------------------
        seg_box = tk.LabelFrame(win, text=" Fitted model ", bg=BG_PANEL,
                                fg=FG_ACCENT, font=("Segoe UI", 9, "bold"),
                                relief=tk.GROOVE, bd=2)
        seg_box.pack(fill=tk.X, padx=14, pady=(10, 0))

        self.seg_tree = ttk.Treeview(
            seg_box, columns=("seg", "from", "to", "off", "drift", "atempo", "n"),
            show="headings", style="Ads.Treeview", height=2)
        for col, head, width, anchor in (
                ("seg", "#", 40, tk.CENTER),
                ("from", "From (s)", 90, tk.E),
                ("to", "To (s)", 90, tk.E),
                ("off", "Offset (ms)", 110, tk.E),
                ("drift", "Drift (ms/s)", 100, tk.E),
                ("atempo", "atempo", 110, tk.E),
                ("n", "Points", 60, tk.CENTER)):
            self.seg_tree.heading(col, text=head)
            self.seg_tree.column(col, width=width, anchor=anchor, stretch=False)
        self.seg_tree.pack(fill=tk.X, padx=8, pady=6)

        self.summary_var = tk.StringVar(value="")
        tk.Label(seg_box, textvariable=self.summary_var, bg=BG_PANEL, fg="#999999",
                 font=("Segoe UI", 8), justify=tk.LEFT, anchor=tk.W).pack(
            fill=tk.X, padx=10, pady=(0, 8))

        # ---- footer ------------------------------------------------------
        footer = tk.Frame(win, bg=BG_DARK)
        footer.pack(fill=tk.X, padx=14, pady=(10, 12))

        self.btn_export = tk.Button(footer, text="⬇ Export corrected audio…",
                                    command=self.export_audio, bg="#1a3a5a",
                                    fg="#ffffff", relief=tk.FLAT, state=tk.DISABLED,
                                    font=("Segoe UI", 9, "bold"), cursor="hand2",
                                    activebackground="#2a5a8a", padx=16, pady=8)
        self.btn_export.pack(side=tk.LEFT)

        self.verify_var = tk.BooleanVar(value=True)
        tk.Checkbutton(footer, text="verify & auto-align", variable=self.verify_var,
                       bg=BG_DARK, fg=FG_DIM, selectcolor=BG_INPUT, bd=0,
                       highlightthickness=0, activebackground=BG_DARK,
                       activeforeground="#ffffff",
                       font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(14, 0))

        for label, cmd in (("Save plan…", self.save_plan),
                           ("Load plan…", self.load_plan),
                           ("Clear all", self.clear_all),
                           ("Close", self.close)):
            tk.Button(footer, text=label, command=cmd, bg="#333333",
                      fg="#ffffff", relief=tk.FLAT, font=("Segoe UI", 9),
                      cursor="hand2", activebackground="#444444",
                      padx=12, pady=8).pack(side=tk.RIGHT, padx=(6, 0))

        win.update_idletasks()
        self._apply_style()
        self._centre()

    def _apply_style(self) -> None:
        style = ttk.Style(self.frame)
        try:
            style.theme_use("default")
        except tk.TclError:
            pass
        style.configure("Ads.Treeview", background="#1e1e1e",
                        fieldbackground="#1e1e1e", foreground="#cccccc",
                        borderwidth=0, rowheight=20, font=("Segoe UI", 8))
        style.configure("Ads.Treeview.Heading", background="#2d2d2d",
                        foreground=FG_ACCENT, font=("Segoe UI", 8, "bold"))
        style.map("Ads.Treeview", background=[("selected", "#2a7de1")],
                  foreground=[("selected", "#ffffff")])

    def _centre(self) -> None:
        """No-op: the panel lives inside a notebook tab now."""
        return

    # ----------------------------------------------------------- plumbing --
    def _post(self, kind: str, payload=None) -> None:
        self.queue.put((kind, payload))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.frame.after(60, self._poll)
        except tk.TclError:
            pass

    def _handle(self, kind: str, payload) -> None:
        if kind == "status":
            self.status_var.set(str(payload))
            console_log(str(payload), "info")
            set_progress(self.progress_var.get(), str(payload))
        elif kind == "progress":
            pct, text = payload
            self.progress_var.set(pct)
            set_progress(pct, text)
            if text:
                self.status_var.set(text)
        elif kind == "scan_points":
            self.points = payload
            self._refresh_points()
        elif kind == "new_point":
            self.points.append(payload)
            self.points.sort(key=lambda p: p["t"])
            self._refresh_points()
        elif kind == "plan":
            self.plan = payload
            self._refresh_plan()
        elif kind == "error":
            self.status_var.set(f"✗ {payload}")
            messagebox.showerror("Timeline error", str(payload), parent=self.frame)
        elif kind == "idle":
            self._set_busy(False)
        elif kind == "export_done":
            self._set_busy(False)
            self.status_var.set(f"✓ Saved: {payload}")
            message = (f"Corrected audio written to:\n{payload}\n\n"
                       "The applied model is listed in the segments table.")
            residual = format_residual(getattr(self, "last_residual", None))
            if residual:
                message += "\n\n" + residual
            dark_info_dialog("Export complete", message)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.btn_scan.config(state=state)
        self.btn_export.config(state=tk.DISABLED if (busy or not self.plan) else tk.NORMAL)
        self.btn_stop.config(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self.progress_var.set(0)
            if text:
                self.status_var.set(text)

    def _start(self, fn, text: str) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self._set_busy(True, text)
        self.worker = threading.Thread(target=self._guard, args=(fn,), daemon=True)
        self.worker.start()

    def _guard(self, fn) -> None:
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001 - reported in UI
            self._post("error", exc)
            self._post("idle", None)

    # ------------------------------------------------------------- values --
    def _num(self, var, default: float, lo: float, hi: float) -> float:
        try:
            value = float(var.get())
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, value))

    def _window(self) -> float:
        return self._num(self.var_window, 40.0, MIN_WINDOW_S, 180.0)

    def _step(self) -> float:
        return self._num(self.var_step, 30.0, 2.0, 180.0)

    def _conf(self) -> float:
        return self._num(self.var_conf, 2.0, 0.5, 50.0)

    def _mode(self) -> str:
        """Selected comparison mode: auto / waveform / envelope."""
        choice = self.var_signal.get()
        if choice.startswith("Waveform"):
            return "waveform"
        if choice.startswith("Envelope"):
            return "envelope"
        return "auto"

    def _fps_slope(self):
        """
        Drift implied by the two framerate fields, or None when unavailable.

        Returns None unless both fields hold a positive number and the two
        differ – equal framerates simply mean "no prior".
        """
        def _parse(var):
            text = var.get().strip().replace(",", ".")
            if not text:
                return 0.0
            try:
                return float(text)
            except ValueError:
                return 0.0

        ref, target = _parse(self.var_fps_ref), _parse(self.var_fps_tgt)
        if ref <= 0 or target <= 0 or abs(ref - target) < 1e-9:
            return None
        return framerate_slope(ref, target)

    # -------------------------------------------------------------- tasks --
    def _decode_fraction(self, phase: str, fraction: float) -> None:
        """Live progress while either file is being decoded."""
        base, span = (3.0, 35.0) if phase == "ref" else (38.0, 32.0)
        name = "reference" if phase == "ref" else "target"
        self._post("progress",
                   (base + span * fraction,
                    f"Decoding {name}… {fraction * 100:.0f}%"))


    def _run_scan(self, ref, tgt, rate, meta, window, step, mode, hint,
                  label="Measuring"):
        """
        One full pass over the timeline.

        The target may already be warped by *rate* (drift / framerate hint), so
        the measured offsets are converted back to the original timeline before
        the model is fitted.
        """
        raw = scan_arrays(
            ref, tgt, window=window, step=step, mode=mode,
            progress=lambda done, total: self._post(
                "progress", (72.0 + 28.0 * done / max(total, 1),
                             f"{label} {done}/{total}")),
            should_stop=self.stop_event.is_set)
        points = sorted(compensate_points(raw, rate), key=lambda p: p["t"])
        self._post("scan_points", points)
        # After compensation the target can span the whole reference timeline
        # even though the file itself is shorter (PAL runs ~4 % fast).  Using
        # min(duration) here would cut the last minutes off the exported audio.
        span = meta["ref_duration"] if rate != 1.0 else meta["duration"]
        self.plan_duration = span
        plan = build_plan(points, span, min_conf=self._conf(), slope_hint=hint)
        return points, plan

    def _task_scan(self) -> None:
        console_log("── Timeline scan started ─────────────────────", "bold")
        self._post("status", "Decoding audio…")
        ref, tgt, meta = decode_pair(
            self.ref_path, self.target_path,
            progress=lambda msg: self._post("status", msg),
            on_fraction=self._decode_fraction)
        self.ref_data, self.tgt_data, self.meta = ref, tgt, meta
        self.scan_rate = 1.0

        window, step, mode = self._window(), self._step(), self._mode()
        hint = self._fps_slope()

        # A known framerate ratio lets the scan start already compensated.
        # Without it a PAL-style 43 ms/s difference smears the peak so badly
        # that the first pass cannot lock at all.
        scan_tgt, scan_rate = tgt, 1.0
        if hint is not None and abs(hint) >= 0.3:
            scan_rate = 1.0 + hint / 1000.0
            scan_tgt = resample_track(tgt, scan_rate)
            self._post("status",
                       f"Framerate hint {hint:+.3f} ms/s – pre-warping target "
                       f"(atempo {scan_rate:.8f}) and scanning…")
        else:
            self._post("status", f"Measuring every {step:.0f}s ({mode})…")

        points, plan = self._run_scan(ref, scan_tgt, scan_rate, meta,
                                      window, step, mode, hint)

        # if the hint was wrong the pre-warped scan finds nothing – retry plain
        usable = sum(1 for p in points if p.get("conf", 0) >= self._conf())
        if hint is not None and abs(hint) >= 0.3 and usable < 3 \
                and not self.stop_event.is_set():
            self._post("status",
                       "The framerate hint did not fit – re-scanning without it…")
            scan_tgt, scan_rate = tgt, 1.0
            points, plan = self._run_scan(ref, scan_tgt, scan_rate, meta,
                                          window, step, mode, hint)

        # Refine with the measured drift: play the target at the reference
        # speed once more so the correlation peaks become sharp.
        drift = plan_slope(plan)
        if abs(drift) >= 0.3 and not self.stop_event.is_set():
            extra = (1.0 + drift / 1000.0) / scan_rate
            if abs(extra - 1.0) > 5e-5:
                self._post("status",
                           f"Compensating drift {drift:+.3f} ms/s "
                           f"(extra rate {extra:.6f}) and re-scanning…")
                scan_tgt = resample_track(scan_tgt, extra)
                scan_rate *= extra
                points, plan = self._run_scan(ref, scan_tgt, scan_rate, meta,
                                              window, step, mode, hint,
                                              label="Re-measuring")
                self._post("status",
                           f"Drift compensation applied – residual "
                           f"{plan_slope(plan):+.3f} ms/s.")

        self.tgt_data, self.scan_rate = scan_tgt, scan_rate
        tgt = scan_tgt

        if plan.get("jumps") and not self.stop_event.is_set():
            self._post("status",
                       f"Refining {len(plan['jumps'])} jump position(s)…")
            extra_points = refine_breakpoints(ref, tgt, plan, self.plan_duration,
                                              mode=mode,
                                              should_stop=self.stop_event.is_set)
            if extra_points:
                extra_points = compensate_points(extra_points, scan_rate)
                points = sorted(points + extra_points, key=lambda p: p["t"])
                self._post("scan_points", points)
                plan = build_plan(points, self.plan_duration,
                                  min_conf=self._conf(), slope_hint=hint)

        used_env = sum(1 for p in points if p.get("mode") == "e")
        for line in plan_lines(plan)[:5]:
            console_log("    " + line, "ok")
        self._post("plan", plan)
        self._post("status",
                   f"Scan finished – {len(points)} points "
                   f"({len(points) - used_env} waveform, {used_env} envelope), "
                   f"atempo {1.0 + plan_slope(plan) / 1000.0:.8f}.")
        self._post("idle", None)

    def _task_measure(self, t_center: float) -> None:
        if self.ref_data is None:
            self._post("status", "Decoding audio…")
            self.ref_data, self.tgt_data, self.meta = decode_pair(
                self.ref_path, self.target_path,
                progress=lambda msg: self._post("status", msg))

        # keep the point inside the track
        duration = self.plan_duration
        half = self._window() / 2.0
        start = max(0.0, min(t_center - half, duration - self._window()))
        result = measure_pair(self.ref_data, self.tgt_data, start, self._window(),
                              mode=self._mode())
        if result is None:
            self._post("status", "Not enough clean audio at that position.")
            self._post("idle", None)
            return

        result["t"] = round(start + self._window() / 2.0, 2)
        result["window"] = self._window()
        result["kind"] = "manual"
        if self.scan_rate != 1.0:
            # the arrays may hold the drift-compensated target
            result = compensate_points([result], self.scan_rate)[0]
        self._post("new_point", result)
        self._post("plan", build_plan(self.points, duration, min_conf=self._conf(), slope_hint=self._fps_slope()))
        self._post("status",
                   f"Measured at {result['t']:.1f}s: {result['offset']:+.2f} ms "
                   f"(confidence {result['conf']:.1f}, "
                   f"{'envelope' if result.get('mode') == 'e' else 'waveform'})")
        self._post("idle", None)

    def _task_export(self, out_path: str) -> None:
        def report(pct, text):
            self._post("progress", (pct, text))

        plan = self.plan
        export_plan(plan, self.target_path, out_path, progress=report,
                    log=console_log, should_stop=self.stop_event.is_set)

        if not self.verify_var.get():
            self._post("export_done", out_path)
            return
        if self.stop_event.is_set():
            raise RuntimeError("Cancelled")

        # ---- measure the produced file and align it automatically ----------
        # The model is a fit, so a few milliseconds can be left over.  Instead
        # of telling the user to type a delay into the muxer, shift the plan by
        # exactly that amount and export once more.  No mux-time correction is
        # ever needed.
        self.last_residual = None
        self._post("status", "Checking the alignment of the exported file…")
        try:
            residual = measure_residual(self.ref_path, out_path)
        except Exception as exc:
            console_log(f"Verification skipped: {exc}", "warn")
            residual = None

        if residual and abs(residual[0]) >= 3.0 and not self.stop_event.is_set():
            delta = residual[0]          # positive = produced audio is late
            console_log(f"Residual {delta:+.1f} ms – re-exporting aligned…", "warn")
            self._post("status", f"Aligning: shifting by {delta:+.1f} ms and re-exporting…")
            adjusted = shift_plan(plan, delta)
            export_plan(adjusted, self.target_path, out_path, progress=report,
                        log=console_log, should_stop=self.stop_event.is_set)
            self.plan = adjusted
            self._post("plan", adjusted)
            try:
                residual = measure_residual(self.ref_path, out_path)
            except Exception:
                pass

        self.last_residual = residual
        if residual:
            for line in format_residual(residual).splitlines():
                console_log(line, "ok" if abs(residual[0]) < 15 else "warn")
        self._post("export_done", out_path)


    # ------------------------------------------------------------ actions --
    def auto_scan(self) -> None:
        self._start(self._task_scan, "Starting scan…")

    def stop_scan(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping…")

    def measure_from_entry(self) -> None:
        try:
            t = to_seconds(self.entry_time.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("Timeline", "Invalid time value.", parent=self.frame)
            return
        self._start(lambda: self._task_measure(t), f"Measuring at {t:.1f}s…")

    def add_manual_point(self) -> None:
        try:
            t = to_seconds(self.entry_time.get().strip())
            offset = float(self.entry_offset.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("Timeline",
                                 "Time and offset must be numbers.\n"
                                 "Time accepts 90, 1:30 or 1:02:30.",
                                 parent=self.frame)
            return
        self.points.append({"t": round(t, 2), "offset": round(offset, 2),
                            "conf": 99.0, "ncc": 1.0, "kind": "manual",
                            "enabled": True})
        self.points.sort(key=lambda p: p["t"])
        self._refresh_points()
        if self.meta:
            self.plan = build_plan(self.points, self.plan_duration, slope_hint=self._fps_slope(),
                                   min_conf=self._conf())
            self._refresh_plan()
        self.status_var.set(f"Added manual point at {t:.2f}s: {offset:+.2f} ms")

    def toggle_selected(self) -> None:
        for item in self.tree.selection():
            idx = int(self.tree.item(item, "values")[0]) - 1
            if 0 <= idx < len(self.points):
                self.points[idx]["enabled"] = not self.points[idx].get("enabled", True)
        self._refresh_points()
        self._rebuild_plan()

    def delete_selected(self) -> None:
        drop = {int(self.tree.item(i, "values")[0]) - 1 for i in self.tree.selection()}
        self.points = [p for i, p in enumerate(self.points) if i not in drop]
        self._refresh_points()
        self._rebuild_plan()

    def clear_all(self) -> None:
        self.points = []
        self.plan = None
        self._refresh_points()
        self._refresh_plan()
        self._draw()
        self.status_var.set("Cleared.")

    def _rebuild_plan(self) -> None:
        if self.meta is None:
            return
        self.plan = build_plan(self.points, self.plan_duration, slope_hint=self._fps_slope(),
                               min_conf=self._conf())
        self._refresh_plan()

    def export_audio(self) -> None:
        if not self.plan or not self.plan.get("segments"):
            messagebox.showwarning("Timeline", "Run a scan first – no plan yet.",
                                   parent=self.frame)
            return
        info = _probe_audio_stream(self.target_path)
        base = os.path.splitext(os.path.basename(self.target_path))[0]
        # Matroska Audio only – it carries proper duration metadata, muxes
        # cleanly and does not need bare .aac/.ac3 containers.
        out = filedialog.asksaveasfilename(
            parent=self.frame, title="Save corrected audio as…",
            defaultextension=".mka", initialfile=f"{base}_corrected.mka",
            filetypes=[("Matroska audio", "*.mka"), ("All files", "*.*")])
        if not out:
            return
        out = mka_path(out)
        self._start(lambda: self._task_export(out), "Exporting…")

    def save_plan(self) -> None:
        if not self.plan:
            messagebox.showwarning("Timeline", "Nothing to save yet.", parent=self.frame)
            return
        path = filedialog.asksaveasfilename(
            parent=self.frame, title="Save sync plan", defaultextension=".json",
            initialfile="sync_plan.json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        payload = {"version": 1, "reference": self.ref_path,
                   "target": self.target_path,
                   "duration": (self.meta or {}).get("duration"),
                   "points": self.points, "plan": self.plan}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        self.status_var.set(f"Plan saved: {path}")

    def load_plan(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.frame, title="Load sync plan",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            messagebox.showerror("Timeline", f"Cannot read plan:\n{exc}", parent=self.frame)
            return
        self.points = [p for p in payload.get("points", []) if "t" in p]
        self.plan = payload.get("plan")
        duration = payload.get("duration") or (self.plan or {}).get("duration") or 0.0
        self.meta = self.meta or {"duration": duration, "sr": SCAN_SR}
        self._refresh_points()
        self._refresh_plan()
        self._draw()
        self.status_var.set(f"Plan loaded: {os.path.basename(path)}")

    # ------------------------------------------------------------ display --
    def _refresh_points(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, point in enumerate(self.points):
            enabled = point.get("enabled", True)
            if point.get("kind") == "manual":
                source = "manual"
            else:
                source = "wave" if point.get("mode") == "w" else "env"
            self.tree.insert("", tk.END, values=(
                i + 1,
                f"{point['t']:.2f}",
                f"{point['offset']:+.1f}",
                f"{point.get('conf', point.get('ncc', 0)):.1f}",
                source + ("" if enabled else " (off)"),
            ), tags=("off",) if not enabled else ())
        self.tree.tag_configure("off", foreground="#666666")

    def _refresh_plan(self) -> None:
        self.seg_tree.delete(*self.seg_tree.get_children())
        plan = self.plan or {}
        for i, seg in enumerate(plan.get("segments") or []):
            self.seg_tree.insert("", tk.END, values=(
                i + 1, f"{seg['t_start']:.1f}", f"{seg['t_end']:.1f}",
                f"{seg['offset']:+.1f}", f"{seg['slope']:+.3f}",
                f"{seg['atempo']:.6f}", seg["n"]))

        lines = plan_lines(plan) if plan.get("segments") else []
        self.summary_var.set("\n".join(lines[:9]))
        self.btn_export.config(
            state=tk.NORMAL if (plan.get("segments") and
                                not (self.worker and self.worker.is_alive()))
            else tk.DISABLED)
        self._draw()

    def _draw(self) -> None:
        ax = self.ax
        try:
            ax.clear()
            points = [p for p in self.points]
            if points:
                min_conf = self._conf()
                colours = []
                for p in points:
                    if not p.get("enabled", True):
                        colours.append("#555555")          # disabled by the user
                    elif float(p.get("conf", p.get("ncc", 0))) < min_conf:
                        colours.append("#b05a00")          # measured, but rejected
                    else:
                        colours.append("#4a9eff")          # used by the model
                ax.scatter([p["t"] for p in points], [p["offset"] for p in points],
                           s=28, zorder=3, c=colours, edgecolors="none")

            for seg in (self.plan or {}).get("segments", []):
                x0, x1 = seg["t_start"], seg["t_end"]
                y0 = seg["offset"]
                y1 = y0 + seg["slope"] * (x1 - x0)
                ax.plot([x0, x1], [y0, y1], color="#00cc66", lw=1.8, zorder=2)

            for jump in (self.plan or {}).get("jumps", []):
                ax.axvline(jump["t"], color="#ffaa44", ls="--", lw=0.9, alpha=0.85)

            ax.axhline(0.0, color="#666666", lw=0.8, ls=":")
            ax.set_xlabel("Time (s)", color="white", fontsize=8)
            ax.set_ylabel("Offset (ms)", color="white", fontsize=8)
            ax.tick_params(colors="white", labelsize=7)
            ax.grid(True, alpha=0.2, color="white")
            ax.set_facecolor("#1a1a1a")
            self.fig.patch.set_facecolor("#1a1a1a")
            self.canvas.draw_idle()
        except tk.TclError:
            pass

    def _on_chart_click(self, event) -> None:
        """Single click on the chart measures the offset at that timestamp."""
        if event.inaxes is not self.ax or event.xdata is None:
            return
        if self.worker is not None and self.worker.is_alive():
            return
        self._start(lambda: self._task_measure(float(event.xdata)),
                    f"Measuring at {event.xdata:.1f}s…")

    def apply_responsive(self, width: int, height: int) -> None:
        """Reflow the panel when the main window changes size."""
        try:
            self.points_frame.config(width=max(280, min(460, int(width * 0.20))))
            # fewer point rows when the window is short, more when it is tall
            rows = max(4, min(8, int((height - 560) / 30)))
            self.tree.config(height=rows)
        except tk.TclError:
            pass

    # -------------------------------------------------------------- close --
    def close(self) -> None:
        self.stop_event.set()
        try:
            self.frame.destroy()
        except tk.TclError:
            pass


# ===========================================================================
# GUI SETUP
# Layout: 1800 × 1020 px, three-column bottom row:
#   Left   – waveform chart (expands)
#   Middle – controls panel (fixed 300 px)
#   Right  – console / log panel (fixed 340 px)
#
# An install-bar is packed above the console initially hidden; it becomes
# visible when FFmpeg is not found at startup.
# ===========================================================================
root = tk.Tk()
if sv_ttk is not None:
    sv_ttk.set_theme("light" if APP_LIGHT else "dark")
root.title(f"{APP_NAME} {APP_VERSION} - Tk")
root.geometry("1680x940")
root.minsize(1180, 700)
root.configure(bg="#1a1a1a")
root.resizable(True, True)

# Apply dark title bar as soon as the window handle is available
root.update()
if not APP_LIGHT:
    _apply_dark_titlebar(root)
_apply_mica(root, APP_LIGHT)
root.protocol("WM_DELETE_WINDOW", _on_close)

if APP_LIGHT:
    bg_main, bg_panel, bg_input = "#f3f3f3", "#ffffff", "#ffffff"
    fg_main, fg_dim, fg_accent = "#1a1a1a", "#5a5a5a", "#0067c0"
    btn_primary, btn_success, btn_hover = "#0067c0", "#0f7b0f", "#1a86d9"
else:
    bg_main, bg_panel, bg_input = "#1a1a1a", "#252525", "#2d2d2d"
    fg_main, fg_dim, fg_accent = "#ffffff", "#aaaaaa", "#4a9eff"
    btn_primary, btn_success, btn_hover = "#2a7de1", "#28a745", "#3d8bff"

# the timeline panel uses its own names for the same palette
BG_DARK, BG_PANEL, BG_INPUT = bg_main, bg_panel, bg_input
FG_DIM, FG_ACCENT = fg_dim, fg_accent

ref_var         = tk.IntVar(value=1)
check_drift_var = tk.BooleanVar(value=False)


# Progress bar – packed at the very bottom of the root window, full width
progress_var = tk.DoubleVar(value=0)

# Bottom status row: spinner + label + progress bar
_status_row = tk.Frame(root, bg="#101010", height=26)
_status_row.pack(side=tk.BOTTOM, fill=tk.X)
_status_row.pack_propagate(False)

progress_label = tk.Label(_status_row, text="", bg="#111111", fg="#888888",
                           font=("Segoe UI", 8), anchor=tk.W)
progress_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

# Dark-themed progress bar via ttk.Style
_pb_style = ttk.Style(root)
_pb_style.theme_use("default")
_pb_style.configure(
    "Dark.Horizontal.TProgressbar",
    troughcolor="#1a1a1a",
    background="#2a7de1",
    bordercolor="#1a1a1a",
    lightcolor="#2a7de1",
    darkcolor="#2a7de1",
    thickness=6,
)

progress_bar = ttk.Progressbar(root, variable=progress_var,
                                maximum=100, mode='determinate',
                                style="Dark.Horizontal.TProgressbar")
progress_bar.pack(side=tk.BOTTOM, fill=tk.X)



def set_progress(pct: float, text: str = "") -> None:
    """Update the progress bar; safe from any thread."""
    if _is_main_thread():
        progress_var.set(pct)
        progress_label.config(text=text, fg="#aaaaaa" if pct < 100 else "#00cc66")
    else:
        _ui_queue.put(("progress", (pct, text)))

def clear_progress() -> None:
    """Reset the progress bar; safe from any thread."""
    if _is_main_thread():
        progress_var.set(0)
        progress_label.config(text="")
    else:
        _ui_queue.put(("progress", (0, "")))

# ---------------------------------------------------------------------------
# SHELL: shared bottom area
# The file pickers live in the Quick tab; the console and the progress bar are
# shared by both tabs so long jobs look the same everywhere.
# ---------------------------------------------------------------------------
console_host = tk.Frame(root, bg="#0d0d0d", height=132)
console_host.pack(side=tk.BOTTOM, fill=tk.X)
console_host.pack_propagate(False)

main_area = tk.Frame(root, bg=bg_main)
main_area.pack(fill=tk.BOTH, expand=True)


# ---------------------------------------------------------------------------
# MAIN CONTAINER + TOP ROW (files + parameters)
# ---------------------------------------------------------------------------
main_container = tk.Frame(main_area, bg=bg_main)
main_container.pack(fill=tk.X, padx=20, pady=(16, 6))

# === TOP ROW: Files + Parameters ===
top_row = tk.Frame(main_container, bg=bg_main)
top_row.pack(fill=tk.X, pady=(0, 15))

# FILES SECTION
files_frame = tk.LabelFrame(top_row, text=" Audio/Video Files ", bg=bg_panel, fg=fg_accent,
                             font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2)
files_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

# File 1
file1_container = tk.Frame(files_frame, bg=bg_panel)
file1_container.pack(fill=tk.X, padx=15, pady=(12, 8))

tk.Label(file1_container, text="File 1:", bg=bg_panel, fg=fg_main,
         font=("Segoe UI", 9, "bold"), width=6).pack(side=tk.LEFT)
entry_file1 = tk.Entry(file1_container, bg=bg_input, fg=fg_main, insertbackground="white",
                       relief=tk.FLAT, font=("Segoe UI", 9))
entry_file1.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(5, 5))
tk.Button(file1_container, text="Browse", command=select_file1, bg=btn_primary, fg=fg_main,
          relief=tk.FLAT, font=("Segoe UI", 9), cursor="hand2", width=8,
          activebackground=btn_hover).pack(side=tk.LEFT, padx=(0, 5))
tk.Label(file1_container, text="Pre-offset:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(5, 2))
entry_preoffset1 = tk.Entry(file1_container, bg=bg_input, fg=fg_main, insertbackground="white",
                             relief=tk.FLAT, font=("Segoe UI", 9), width=6, justify=tk.CENTER)
entry_preoffset1.insert(0, "0")
entry_preoffset1.pack(side=tk.LEFT, ipady=3, padx=(0, 5))
tk.Label(file1_container, text="s", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 5))
tk.Radiobutton(file1_container, text="Base", variable=ref_var, value=1, bg=bg_panel,
               fg=fg_dim, selectcolor=bg_panel, activebackground=bg_panel,
               font=("Segoe UI", 9)).pack(side=tk.LEFT)

# File 2
file2_container = tk.Frame(files_frame, bg=bg_panel)
file2_container.pack(fill=tk.X, padx=15, pady=(0, 12))

tk.Label(file2_container, text="File 2:", bg=bg_panel, fg=fg_main,
         font=("Segoe UI", 9, "bold"), width=6).pack(side=tk.LEFT)
entry_file2 = tk.Entry(file2_container, bg=bg_input, fg=fg_main, insertbackground="white",
                       relief=tk.FLAT, font=("Segoe UI", 9))
entry_file2.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(5, 5))
tk.Button(file2_container, text="Browse", command=select_file2, bg=btn_primary, fg=fg_main,
          relief=tk.FLAT, font=("Segoe UI", 9), cursor="hand2", width=8,
          activebackground=btn_hover).pack(side=tk.LEFT, padx=(0, 5))
tk.Label(file2_container, text="Pre-offset:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(5, 2))
entry_preoffset2 = tk.Entry(file2_container, bg=bg_input, fg=fg_main, insertbackground="white",
                             relief=tk.FLAT, font=("Segoe UI", 9), width=6, justify=tk.CENTER)
entry_preoffset2.insert(0, "0")
entry_preoffset2.pack(side=tk.LEFT, ipady=3, padx=(0, 5))
tk.Label(file2_container, text="s", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 5))
tk.Radiobutton(file2_container, text="Base", variable=ref_var, value=2, bg=bg_panel,
               fg=fg_dim, selectcolor=bg_panel, activebackground=bg_panel,
               font=("Segoe UI", 9)).pack(side=tk.LEFT)

console_panel = tk.LabelFrame(console_host, text=" Console ", bg=bg_panel, fg=fg_accent,
                              font=("Segoe UI", 9, "bold"), relief=tk.GROOVE, bd=2)
console_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=15, pady=(2, 4))

console_inner = tk.Frame(console_panel, bg=bg_panel)
console_inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))

# Text only – mousewheel scrolling, no scrollbar
console_text_frame = tk.Frame(console_inner, bg="#0d0d0d")
console_text_frame.pack(fill=tk.BOTH, expand=True)

console_text = tk.Text(
    console_text_frame,
    bg="#0d0d0d", fg="#cccccc",
    font=("Consolas", 8),
    relief=tk.FLAT, wrap=tk.CHAR,
    borderwidth=0, highlightthickness=0,
    cursor="arrow",
    state=tk.DISABLED,
)
console_text.pack(fill=tk.BOTH, expand=True)

# Colour tags for console_log()
console_text.tag_config("info",  foreground="#aaaaaa")
console_text.tag_config("ok",    foreground="#00cc66")
console_text.tag_config("warn",  foreground="#ffaa44")
console_text.tag_config("error", foreground="#ff5555")
console_text.tag_config("cmd",   foreground="#4a9eff")
console_text.tag_config("bold",    foreground="#ffffff")

# Register this widget so console_log() writes to it
_console_widgets.append(console_text)

# Clear button – below the text frame, never overlapping the scrollbar
tk.Button(console_inner, text="Clear", command=console_clear,
          bg="#333333", fg=fg_dim, relief=tk.FLAT, cursor="hand2",
          font=("Segoe UI", 7), activebackground="#444444",
          pady=2).pack(anchor=tk.E, pady=(4, 0))



# ---------------------------------------------------------------------------
# TIMELINE TAB CONTENT
# ---------------------------------------------------------------------------
def timeline_files():
    """Return (reference, target) from the file bar, or (None, None)."""
    file1 = entry_file1.get().strip()
    file2 = entry_file2.get().strip()
    if not file1 or not file2:
        return None, None
    if ref_var.get() == 1:
        return file1, file2
    return file2, file1


def load_timeline(force: bool = False) -> None:
    """(Re)build the Timeline panel for the files chosen above."""
    global _timeline_panel
    ref, target = timeline_files()
    if not ref:
        messagebox.showerror("Timeline", "Select both files first.")
        return
    if _timeline_panel is not None and not force:
        return
    if _timeline_panel is not None:
        try:
            _timeline_panel.close()
            _timeline_panel.frame.destroy()
        except Exception:
            pass
        _timeline_panel = None
    _timeline_panel = TimelinePanel(tl_body, ref, target)
    ref_fps = _timeline_panel.var_fps_ref.get()
    tgt_fps = _timeline_panel.var_fps_tgt.get()
    ref_tag = f"  [{ref_fps} fps]" if ref_fps else ""
    tgt_tag = f"  [{tgt_fps} fps]" if tgt_fps else ""
    speed_note = ""
    try:
        ratio = float(ref_fps) / float(tgt_fps)
        if abs(ratio - 1.0) > 1e-5:
            speed_note = f"        atempo {ratio:.6f}"
    except (ValueError, ZeroDivisionError):
        pass
    tl_info.config(text=f"Reference: {os.path.basename(ref)}{ref_tag}"
                        f"    \u2192    Target: {os.path.basename(target)}{tgt_tag}"
                        f"{speed_note}")
    console_log(f"Timeline loaded: {os.path.basename(ref)} \u2192 "
                f"{os.path.basename(target)}", "info")


tl_header = tk.Frame(main_area, bg=bg_main)
tl_header.pack(fill=tk.X, padx=16, pady=(12, 6))

tl_info = tk.Label(tl_header,
                   text="Choose both files above — the timeline loads automatically.",
                   bg=bg_main, fg="#888888", font=("Segoe UI", 8),
                   anchor=tk.W, justify=tk.LEFT)
tl_info.pack(side=tk.LEFT, fill=tk.X, expand=True)


tl_body = tk.Frame(main_area, bg=bg_main)
tl_body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# RESPONSIVE LAYOUT
# Fixed pixel widths broke at small window sizes; these factors keep the
# console, the controls column and the timeline list proportional.
# ---------------------------------------------------------------------------
_resize_job = {"id": None}


def _apply_responsive_layout() -> None:
    _resize_job["id"] = None
    try:
        width = max(1, root.winfo_width())
        height = max(1, root.winfo_height())
    except tk.TclError:
        return
    try:
        console_host.config(height=max(92, min(200, int(height * 0.15))))
    except tk.TclError:
        pass
    if _timeline_panel is not None:
        try:
            _timeline_panel.apply_responsive(width, height)
        except Exception:
            pass


def _on_window_configure(event=None) -> None:
    """Debounce: <Configure> fires dozens of times while dragging."""
    if event is not None and event.widget is not root:
        return
    if _resize_job["id"] is not None:
        try:
            root.after_cancel(_resize_job["id"])
        except tk.TclError:
            pass
    _resize_job["id"] = root.after(80, _apply_responsive_layout)


root.bind("<Configure>", _on_window_configure)
root.after(200, _apply_responsive_layout)

# Post-startup checks (run after mainloop starts via `after`)
# ---------------------------------------------------------------------------
root.after(80, _pump_ui)                  # drain UI updates from worker threads
root.after(120, flush_startup_messages)   # replay auto-installer log, if any
root.after(300, ensure_ffmpeg)            # detect FFmpeg or install it via winget

root.mainloop()