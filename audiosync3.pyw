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
    Python 3.x, FFmpeg (in PATH), numpy, scipy, matplotlib
"""

import os
import sys
import subprocess
import tempfile
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

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
    """Run an ffmpeg command, registering the Popen object so _kill_ffmpeg()
    can terminate it immediately even while it is running in a thread."""
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


# ---------------------------------------------------------------------------
# PYTHON PACKAGE DEPENDENCY CHECK
# FFmpeg is checked later inside the GUI after startup so the user can see
# the install progress in the built-in console panel.
# ---------------------------------------------------------------------------
try:
    import numpy as np
    import scipy.signal as signal
    from scipy.io import wavfile
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError as e:
    show_startup_error(
        "Missing Python libraries",
        "Required Python libraries are missing.\n\n"
        f"Install them with:\n{sys.executable} -m pip install numpy scipy matplotlib\n\n"
        f"Details: {e}"
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
# ---------------------------------------------------------------------------

_console_widgets: list = []  # populated after GUI widgets are created


def console_log(msg: str, tag: str = "info") -> None:
    """Append a timestamped line to every registered console Text widget."""
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


def console_clear() -> None:
    """Clear all registered console Text widgets."""
    for w in _console_widgets:
        try:
            w.config(state=tk.NORMAL)
            w.delete("1.0", tk.END)
            w.config(state=tk.DISABLED)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# FFMPEG INSTALL FLOW
# Runs entirely inside the GUI after the main window is visible so the user
# can read progress in the console panel.
# ---------------------------------------------------------------------------

ffmpeg_ready    = False  # Set to True once FFmpeg is confirmed present
_ffmpeg_procs: list = []  # All running ffmpeg subprocesses


def _kill_ffmpeg() -> None:
    """Kill all tracked ffmpeg processes. Called on window close."""
    for proc in list(_ffmpeg_procs):
        try:
            proc.kill()
        except Exception:
            pass
    _ffmpeg_procs.clear()


def _on_close() -> None:
    """Window close handler – kill ffmpeg then destroy."""
    _kill_ffmpeg()
    root.destroy()


def _stream_process_to_console(proc: subprocess.Popen) -> int:
    """
    Read stdout+stderr from *proc* line-by-line and write each line to the
    console widget via root.after() so Tk stays responsive.

    Returns the process returncode.
    """
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            root.after(0, console_log, line, "info")
        root.update_idletasks()
    proc.wait()
    return proc.returncode


def _do_install_ffmpeg() -> None:
    """
    Background-safe installer: runs winget, streams output to console,
    then updates the UI depending on success or failure.
    Called from a daemon thread so the Tk event loop stays alive.
    """
    global ffmpeg_ready

    def ui(fn):
        root.after(0, fn)

    root.after(0, console_log, "Running: winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements", "cmd")

    try:
        proc = subprocess.Popen(
            ["winget", "install", "--id", "Gyan.FFmpeg",
             "-e", "--accept-source-agreements", "--accept-package-agreements"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=get_startupinfo(),
        )
        rc = _stream_process_to_console(proc)
    except FileNotFoundError:
        root.after(0, console_log, "ERROR: winget not found. Install FFmpeg manually and restart.", "error")
        root.after(0, _set_install_failed)
        return

    if rc == 0:
        # winget succeeded; verify ffmpeg.exe is now discoverable.
        # winget may install to a location that requires a new PATH lookup,
        # so we also check common default paths directly.
        import os
        extra_paths = [
            r"C:\Program Files\FFmpeg\bin",
            r"C:\ffmpeg\bin",
        ]
        found_path = shutil.which("ffmpeg")
        if not found_path:
            for ep in extra_paths:
                candidate = os.path.join(ep, "ffmpeg.exe")
                if os.path.isfile(candidate):
                    found_path = candidate
                    # Extend PATH for this process so subsequent subprocess calls work
                    os.environ["PATH"] = ep + os.pathsep + os.environ.get("PATH", "")
                    break

        if found_path:
            ffmpeg_ready = True
            root.after(0, console_log, f"FFmpeg found at: {found_path}", "ok")
            root.after(0, console_log, "✓ FFmpeg installed successfully. You can now run analysis.", "ok")
            root.after(0, _set_install_ok)
        else:
            root.after(0, console_log, "winget finished but ffmpeg.exe was not found in PATH.", "warn")
            root.after(0, console_log, "Restart the application after adding FFmpeg to PATH.", "warn")
            root.after(0, _set_install_failed)
    else:
        root.after(0, console_log, f"winget exited with code {rc}. Installation may have failed.", "error")
        root.after(0, _set_install_failed)


def _set_install_ok() -> None:
    """Re-enable the Analyze button and update the install button after success."""
    btn_analyze.config(state=tk.NORMAL)
    btn_install_ffmpeg.config(text="✓ FFmpeg ready", state=tk.DISABLED,
                              bg="#1a4a2a", fg="#00ff88")


def _set_install_failed() -> None:
    """Re-enable the install button so the user can retry."""
    btn_install_ffmpeg.config(text="⬇ Install FFmpeg (retry)", state=tk.NORMAL, bg="#7a2a1a")


def prompt_ffmpeg_install() -> None:
    """
    Called once after the main window is shown.  If FFmpeg is already present,
    logs an OK message and returns.  Otherwise, logs a warning and shows the
    install button, waiting for the user to confirm.
    """
    global ffmpeg_ready
    if shutil.which("ffmpeg"):
        ffmpeg_ready = True
        console_log("✓ FFmpeg found in PATH – ready.", "ok")
        # FFmpeg present: hide the install banner entirely
        install_bar.pack_forget()
        return

    # FFmpeg missing – reveal the install banner and disable analysis
    console_log("⚠  FFmpeg was not found in PATH.", "warn")
    console_log("   FFmpeg is required to extract audio from media files.", "info")
    console_log("   Press  [ Install FFmpeg ]  to install it automatically via winget.", "info")
    console_log("   If you prefer a manual install:", "info")
    console_log("     winget install --id Gyan.FFmpeg -e", "cmd")
    console_log("   or download from  https://ffmpeg.org/download.html", "info")
    install_bar.pack(fill=tk.X, padx=10, pady=(0, 6))
    btn_analyze.config(state=tk.DISABLED)


def on_install_ffmpeg_click() -> None:
    """
    Triggered by the Install FFmpeg button.
    Locks the button to prevent double-clicks and launches the installer thread.
    """
    btn_install_ffmpeg.config(state=tk.DISABLED, text="Installing…")
    console_log("Starting FFmpeg installation via winget…", "bold")
    import threading
    t = threading.Thread(target=_do_install_ffmpeg, daemon=True)
    t.start()


def check_ffmpeg_before_analyze() -> bool:
    """
    Guard used at the top of analyze_sync to give a clear console message
    if somehow analysis is triggered without FFmpeg (e.g. via keyboard).

    Returns True if FFmpeg is available, False otherwise.
    """
    if ffmpeg_ready or shutil.which("ffmpeg"):
        return True
    console_log("✗ FFmpeg is not installed. Install it first.", "error")
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
    init_off   = round(p1["offset_ms"] - drift_rate*p1["time_s"], 2)
    drift_lbl_calc.config(
        text=(f"Drift rate:  {drift_rate:+.4f} ms/s\n"
              f"atempo:      {atempo:.8f}\n"
              f"Init offset: {init_off:+.2f} ms  (at t=0)"),
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

    # Need drift result – check label has the values
    res_text = label_result.cget("text")
    if "atempo" not in res_text and "stretch" not in res_text.lower():
        messagebox.showerror("Error",
            "Run analysis with \'Check drift\' enabled first.")
        return

    # Read atempo and initial offset from the result label via stored attributes
    # They were set during drift calculation in _drift_update_calculated or
    # directly in analyze_sync. We store them on label_result for retrieval.
    try:
        atempo   = label_result._atempo
        init_off = label_result._init_off   # ms, signed
        offset_ms_start = label_result._offset_ms  # initial static offset
    except AttributeError:
        messagebox.showerror("Error",
            "Drift values not available.\nRun analysis with \'Check drift\' enabled.")
        return

    ref_file   = ref_var.get()
    target_path = entry_file2.get() if ref_file == 1 else entry_file1.get()
    if not target_path:
        messagebox.showerror("Error", "No target file selected.")
        return

    # Detect original codec, bitrate, channels and sample rate via ffprobe
    detected_codec      = "ac3"
    detected_bitrate    = "256k"
    detected_channels   = None   # None = let ffmpeg decide (preserves source)
    detected_samplerate = None
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
    except Exception:
        pass

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
    tmp_wav = _os.path.join(_tmp.gettempdir(), "driftfix_tmp.wav")

    # ── Step 1: atempo + offset → lossless WAV ───────────────────────────────
    if init_off >= 0:
        delay_ms = int(round(init_off))
        af1 = f"adelay={delay_ms}|{delay_ms},atempo={atempo}" if delay_ms > 0 else f"atempo={atempo}"
        cmd1 = ["ffmpeg", "-y", "-i", target_path,
                "-vn", "-map", "0:a:0", "-af", af1,
                "-c:a", "pcm_s16le", tmp_wav]
    else:
        trim_s = abs(init_off) / 1000.0
        af1 = f"atempo={atempo}"
        cmd1 = ["ffmpeg", "-y", "-ss", str(trim_s), "-i", target_path,
                "-vn", "-map", "0:a:0", "-af", af1,
                "-c:a", "pcm_s16le", tmp_wav]

    # Build extra args to preserve original channel count and sample rate
    _preserve = []
    if detected_channels:
        _preserve += ["-ac", str(detected_channels)]
    if detected_samplerate:
        _preserve += ["-ar", str(detected_samplerate)]

    if encoder in ("flac", "pcm_s16le"):
        cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + _preserve + ["-c:a", encoder, out_path]
    elif encoder in ("libopus", "libvorbis"):
        cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + _preserve + ["-c:a", encoder,
                "-b:a", detected_bitrate, out_path]
    else:
        cmd2 = ["ffmpeg", "-y", "-i", tmp_wav] + _preserve + ["-c:a", encoder,
                "-b:a", detected_bitrate, out_path]

    _ch_str = f"  {detected_channels}ch" if detected_channels else ""
    _sr_str = f"  {detected_samplerate}Hz" if detected_samplerate else ""
    console_log(f"Step 1/2  atempo={atempo}  init_off={init_off:+.1f} ms → WAV", "bold")
    console_log("ffmpeg " + " ".join(cmd1[1:]), "cmd")
    console_log(f"Step 2/2  WAV → {detected_codec} @ {detected_bitrate}{_ch_str}{_sr_str}", "bold")
    console_log("ffmpeg " + " ".join(cmd2[1:]), "cmd")

    def _do_export():
        try:
            root.after(0, lambda: set_progress(10, "Step 1: applying atempo..."))
            _run_ffmpeg(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=get_startupinfo())
            console_log("Step 1 done", "ok")
            root.after(0, lambda: set_progress(60, "Step 2: encoding..."))
            _run_ffmpeg(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=get_startupinfo())
            _os.remove(tmp_wav)
            root.after(0, lambda: set_progress(100, "Export done"))
            console_log(f"✓ Saved: {out_path}", "ok")
            root.after(0, lambda: messagebox.showinfo("Done",
                f"Drift-corrected audio saved:\n{out_path}\n\n"
                f"Codec: {detected_codec}  Bitrate: {detected_bitrate}\n"
                f"atempo: {atempo}  delay: {init_off:+.1f} ms"))
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
            btn_analyze.config(state=tk.NORMAL),
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
    btn_analyze.config(state=tk.DISABLED)
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
        offset_display = offset_ms
        if offset_ms == 0:
            res   = "✓ Files perfectly synchronized"
            color = "#00ff88"
        elif offset_ms > 0:
            res   = f"● {target_name} delayed by {offset_ms} ms"
            color = "#ffa500"
        else:
            res   = f"● {target_name} ahead by {abs(offset_ms)} ms"
            color = "#ff6b6b"

        # Pre-offset correction
        if preoffset1 != 0 or preoffset2 != 0:
            res += f"\n  (total with pre-offsets: {total_offset_ms:+.2f} ms)"

        # MKVToolNix instruction for the offset
        if offset_ms != 0:
            mkv_delay = -offset_ms if offset_ms > 0 else abs(offset_ms)
            mkv_sign  = "+" if mkv_delay >= 0 else ""
            res += f"\n\nDelay {target_name}:  {mkv_sign}{mkv_delay} ms"

        # ── Optional drift check ─────────────────────────────────────────────
        if check_drift_var.get():
            try:
                base_file = file1 if ref_file == 1 else file2
                total_dur = _get_file_duration(base_file)
                if total_dur > 60:
                    clip_dur = float(entry_duration.get())
                    t2 = total_dur - clip_dur - 5
                    t1 = start_time
                    if t2 > t1 + 30:
                        preoffset1_v = float(entry_preoffset1.get())
                        preoffset2_v = float(entry_preoffset2.get())
                        console_log(f"Drift check: measuring at t={t2:.1f}s (near end)...", "info")
                        root.after(0, lambda: label_status.config(text="● Drift check: measuring near end of file...", fg="#ffa500"))
                        root.after(0, lambda: set_progress(72, "Drift check..."))
                        offset_ms_t2_raw = _run_correlation(file1, file2, t2 + preoffset1_v, t2 + preoffset2_v, clip_dur)
                        offset_ms_t2 = round(-offset_ms_t2_raw if ref_file == 1 else offset_ms_t2_raw, 2)
                        dt_s       = t2 - t1
                        d_off      = offset_ms_t2 - offset_ms
                        drift_rate  = d_off / dt_s
                        atempo      = round(1.0 - drift_rate / 1000.0, 8)
                        stretch_pct = round((atempo - 1.0) * 100, 6)
                        init_off    = round(offset_ms - drift_rate * t1, 2)

                        if abs(drift_rate) < 0.05:
                            res += "\n\n● No significant drift detected."
                            console_log(f"Drift: {drift_rate:+.4f} ms/s – negligible", "ok")
                        else:
                            mkv_delay_str = f"{'-' if offset_ms > 0 else '+'}{abs(offset_ms)}"
                            res += (
                                f"\n\n● Drift detected  ({drift_rate:+.4f} ms/s)"
                                f"\n\nDelay {target_name}:"
                                f"\n  delay:          {mkv_delay_str} ms"
                                f"\n  stretch factor: {atempo:.8f}"
                                f"\n  stretch %:      {stretch_pct:+.6f}%"
                            )
                            console_log(f"Drift: {drift_rate:+.4f} ms/s  stretch={atempo:.8f}", "warn")
                            _drift_atempo_tmp   = atempo
                            _drift_initoff_tmp  = init_off if "init_off" in dir() else float(offset_ms)
                    else:
                        res += "\n\n● Drift check skipped (points too close)."
                else:
                    res += "\n\n● Drift check skipped (file too short)."
            except Exception as drift_err:
                res += f"\n\n● Drift check failed: {drift_err}"
                console_log(f"Drift check error: {drift_err}", "error")

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
            btn_analyze.config(state=tk.NORMAL)
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
            btn_analyze.config(state=tk.NORMAL),
            clear_progress()
        ))

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
root.title("Audio Sync Finder - Precise Audio Offset Detection")
root.geometry("1800x950")
root.configure(bg="#1a1a1a")
root.resizable(False, False)

# Apply dark title bar as soon as the window handle is available
root.update()
_apply_dark_titlebar(root)
root.protocol("WM_DELETE_WINDOW", _on_close)

bg_main = "#1a1a1a"
bg_panel = "#252525"
bg_input = "#2d2d2d"
fg_main = "#ffffff"
fg_dim = "#aaaaaa"
fg_accent = "#4a9eff"
btn_primary = "#2a7de1"
btn_success = "#28a745"
btn_hover = "#3d8bff"

ref_var         = tk.IntVar(value=1)
check_drift_var = tk.BooleanVar(value=False)


# Progress bar – packed at the very bottom of the root window, full width
progress_var = tk.DoubleVar(value=0)

# Bottom status row: spinner + label + progress bar
_status_row = tk.Frame(root, bg="#111111", height=22)
_status_row.pack(side=tk.BOTTOM, fill=tk.X)
_status_row.pack_propagate(False)

progress_label = tk.Label(_status_row, text="", bg="#111111", fg="#888888",
                           font=("Segoe UI", 8), anchor=tk.W)
progress_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

progress_bar = ttk.Progressbar(root, variable=progress_var,
                                maximum=100, mode='determinate')
progress_bar.pack(side=tk.BOTTOM, fill=tk.X)



def set_progress(pct: float, text: str = "") -> None:
    """Update progress bar from any thread via root.after."""
    def _update():
        progress_var.set(pct)
        progress_label.config(text=text, fg="#aaaaaa" if pct < 100 else "#00cc66")
    root.after(0, _update)

def clear_progress() -> None:
    root.after(0, lambda: (progress_var.set(0), progress_label.config(text="")))

# ---------------------------------------------------------------------------
# MAIN CONTAINER + TOP ROW (files + parameters)
# ---------------------------------------------------------------------------
main_container = tk.Frame(root, bg=bg_main)
main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=(20, 4))

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

# PARAMETERS SECTION
params_frame = tk.LabelFrame(top_row, text=" Analysis Parameters ", bg=bg_panel, fg=fg_accent,
                              font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2)
params_frame.pack(side=tk.LEFT, fill=tk.Y)

params_inner = tk.Frame(params_frame, bg=bg_panel)
params_inner.pack(padx=20, pady=15)

# Start time
start_container = tk.Frame(params_inner, bg=bg_panel)
start_container.pack(side=tk.LEFT, padx=(0, 15))
tk.Label(start_container, text="Start time:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9)).pack()
entry_start = tk.Entry(start_container, bg=bg_input, fg=fg_main, insertbackground="white",
                       relief=tk.FLAT, font=("Segoe UI", 10), justify=tk.CENTER, width=12)
entry_start.insert(0, "00:00:00")
entry_start.pack(ipady=5, pady=3)
tk.Label(start_container, text="(HH:MM:SS)", bg=bg_panel, fg="#666666",
         font=("Segoe UI", 7)).pack()

# Duration
duration_container = tk.Frame(params_inner, bg=bg_panel)
duration_container.pack(side=tk.LEFT)
tk.Label(duration_container, text="Duration (sec):", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9)).pack()
entry_duration = tk.Entry(duration_container, bg=bg_input, fg=fg_main, insertbackground="white",
                          relief=tk.FLAT, font=("Segoe UI", 10), justify=tk.CENTER, width=12)
entry_duration.insert(0, "30")
entry_duration.pack(ipady=5, pady=3)
tk.Label(duration_container, text="(30-60 rec.)", bg=bg_panel, fg="#666666",
         font=("Segoe UI", 7)).pack()

# Drift check option
drift_check_container = tk.Frame(params_inner, bg=bg_panel)
drift_check_container.pack(side=tk.LEFT, padx=(15, 0))
tk.Checkbutton(drift_check_container, text="Check drift", variable=check_drift_var,
               bg=bg_panel, fg=fg_dim, selectcolor=bg_input,
               activebackground=bg_panel, activeforeground=fg_main,
               font=("Segoe UI", 9)).pack(anchor=tk.W)

# === BOTTOM ROW: Chart | Controls | Console ===
bottom_row = tk.Frame(main_container, bg=bg_main)
bottom_row.pack(fill=tk.BOTH, expand=True)

# ── LEFT: Waveform chart ────────────────────────────────────────────────────
chart_frame = tk.LabelFrame(bottom_row, text=" 📊 Waveform Visualization ", bg=bg_panel,
                            fg=fg_accent, font=("Segoe UI", 10, "bold"),
                            relief=tk.GROOVE, bd=2)
chart_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

canvas_container = tk.Frame(chart_frame, bg=bg_panel)
canvas_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

fig = Figure(figsize=(10, 6), dpi=90, facecolor='#1a1a1a')
ax = fig.add_subplot(111)
ax.set_facecolor('#1a1a1a')
canvas = FigureCanvasTkAgg(fig, master=canvas_container)
canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

# Horizontal scrollbar for zoomed view
scrollbar_frame = tk.Frame(chart_frame, bg=bg_panel, height=25)
scrollbar_frame.pack(fill=tk.X, padx=10, pady=(5, 10))
scrollbar_frame.pack_propagate(False)

scrollbar = tk.Scale(scrollbar_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                     command=on_scroll, showvalue=False, bg=bg_panel, fg=fg_accent,
                     troughcolor=bg_input, highlightthickness=0, relief=tk.FLAT,
                     activebackground=fg_accent, sliderrelief=tk.FLAT, bd=0)
scrollbar.pack(fill=tk.BOTH, expand=True)

viz_data['fig'] = fig
viz_data['ax'] = ax
viz_data['canvas'] = canvas
viz_data['scrollbar'] = scrollbar
viz_data['scrollbar_frame'] = scrollbar_frame

# ── MIDDLE: Controls panel ──────────────────────────────────────────────────
control_panel = tk.LabelFrame(bottom_row, text=" Controls ", bg=bg_panel, fg=fg_accent,
                              font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2, width=300)
control_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
control_panel.pack_propagate(False)

controls_inner = tk.Frame(control_panel, bg=bg_panel)
controls_inner.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

# ANALYZE BUTTON
btn_analyze = tk.Button(controls_inner, text="⚡ ANALYZE", command=analyze_sync,
                        bg=btn_success, fg=fg_main, relief=tk.FLAT, cursor="hand2",
                        font=("Segoe UI", 12, "bold"), activebackground="#23923d")
btn_analyze.pack(fill=tk.X, ipady=15, pady=(0, 15))

# RESULTS SECTION
result_section = tk.Frame(controls_inner, bg=bg_panel)
result_section.pack(fill=tk.X, pady=(0, 15))

label_status = tk.Label(result_section, text="● Ready to analyze", bg=bg_panel, fg="#888888",
                        font=("Segoe UI", 9), anchor=tk.W)
label_status.pack(fill=tk.X, pady=(0, 8))

label_result = tk.Label(result_section, text="Waiting for analysis...", bg=bg_panel, fg=fg_dim,
                        font=("Segoe UI", 9, "bold"), anchor=tk.W, wraplength=260, justify=tk.LEFT)
label_result.pack(fill=tk.X)

# SEPARATOR
tk.Frame(controls_inner, bg="#444444", height=1).pack(fill=tk.X, pady=15)

# ZOOM CONTROLS
zoom_section = tk.Frame(controls_inner, bg=bg_panel)
zoom_section.pack(fill=tk.X, pady=(0, 15))

tk.Label(zoom_section, text="🔍 Zoom:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 8))

zoom_buttons = tk.Frame(zoom_section, bg=bg_panel)
zoom_buttons.pack(fill=tk.X)

tk.Button(zoom_buttons, text="Zoom +", command=zoom_in, bg="#333333", fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
          activebackground="#444444").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3), ipady=5)
tk.Button(zoom_buttons, text="Zoom -", command=zoom_out, bg="#333333", fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
          activebackground="#444444").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 3), ipady=5)
tk.Button(zoom_buttons, text="Reset", command=reset_zoom, bg="#333333", fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
          activebackground="#444444").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0), ipady=5)

# SEPARATOR
tk.Frame(controls_inner, bg="#444444", height=1).pack(fill=tk.X, pady=15)

# MANUAL OFFSET
manual_section = tk.Frame(controls_inner, bg=bg_panel)
manual_section.pack(fill=tk.X, pady=(0, 15))

tk.Label(manual_section, text="✏️ Manual offset:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 8))

manual_controls = tk.Frame(manual_section, bg=bg_panel)
manual_controls.pack(fill=tk.X)

entry_manual_offset = tk.Entry(manual_controls, bg=bg_input, fg=fg_main, insertbackground="white",
                               relief=tk.FLAT, font=("Segoe UI", 10), justify=tk.CENTER)
entry_manual_offset.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 5))

tk.Button(manual_controls, text="Apply", command=apply_manual_offset, bg=btn_primary, fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9, "bold"),
          activebackground=btn_hover, width=8).pack(side=tk.LEFT, ipady=6)

tk.Label(manual_section, text="(in milliseconds)", bg=bg_panel, fg="#666666",
         font=("Segoe UI", 7)).pack(anchor=tk.W, pady=(3, 0))

# SEPARATOR
tk.Frame(controls_inner, bg="#444444", height=1).pack(fill=tk.X, pady=15)

# EXPORT DRIFT-CORRECTED AUDIO
tk.Button(controls_inner, text="⬇ Export Drift-Corrected Audio",
          command=export_drift_corrected,
          bg="#1a3a5a", fg="#ffffff", relief=tk.FLAT, cursor="hand2",
          font=("Segoe UI", 9, "bold"),
          activebackground="#2a5a8a").pack(fill=tk.X, ipady=8, pady=(0, 15))

# TIPS BUTTON
def show_tips():
    """Show a popup with usage tips."""
    popup = tk.Toplevel(root)
    popup.title("Tips")
    popup.configure(bg="#1a1a1a")
    popup.resizable(False, False)
    popup.update()
    _apply_dark_titlebar(popup)

    tk.Label(popup, text="💡 Tips for best results",
             bg="#1a1a1a", fg="#4a9eff",
             font=("Segoe UI", 11, "bold")).pack(padx=20, pady=(16, 8))

    tips_content = (
        "SELECT clips with:\n"
        "  • Clear speech or music\n"
        "  • Single language\n"
        "  • Sharp transients (claps, hits, drums)\n"
        "  • 30–60 seconds for best accuracy\n"
        "\n"
        "AVOID:\n"
        "  • Long silences or ambient noise only\n"
        "  • Multilingual or heavily overlapping voices\n"
        "  • Very quiet sections\n"
        "\n"
        "DRIFT CHECK:\n"
        "  • Uses the start clip + last 30s of the file\n"
        "  • Stretch factor corrects progressive desync\n"
        "  • Values < 1.0 slow the audio, > 1.0 speed it up\n"
        "\n"
        "OFFSET SIGN CONVENTION:\n"
        "  • Positive offset → target is delayed\n"
        "    set a negative delay\n"
        "  • Negative offset → target is ahead\n"
        "    set a positive delay"
    )

    txt = tk.Label(popup, text=tips_content,
                   bg="#1a1a1a", fg="#aaaaaa",
                   font=("Segoe UI", 9), justify=tk.LEFT, anchor=tk.W)
    txt.pack(padx=20, pady=(0, 8), anchor=tk.W)

    tk.Button(popup, text="Close", command=popup.destroy,
              bg="#333333", fg="#ffffff", relief=tk.FLAT,
              font=("Segoe UI", 9), cursor="hand2",
              activebackground="#444444",
              padx=20, pady=6).pack(pady=(4, 16))

    # Centre popup on main window
    popup.update_idletasks()
    x = root.winfo_x() + (root.winfo_width()  - popup.winfo_width())  // 2
    y = root.winfo_y() + (root.winfo_height() - popup.winfo_height()) // 2
    popup.geometry(f"+{x}+{y}")
    popup.grab_set()

tk.Button(controls_inner, text="💡 Tips", command=show_tips,
          bg="#333333", fg=fg_dim, relief=tk.FLAT, cursor="hand2",
          font=("Segoe UI", 9), activebackground="#444444").pack(fill=tk.X, ipady=6)

# ── RIGHT: Console / log panel ──────────────────────────────────────────────
console_panel = tk.LabelFrame(bottom_row, text=" 🖥 Console ", bg=bg_panel, fg=fg_accent,
                              font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2, width=340)
console_panel.pack(side=tk.LEFT, fill=tk.Y)
console_panel.pack_propagate(False)

console_inner = tk.Frame(console_panel, bg=bg_panel)
console_inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 8))

# FFmpeg install bar – hidden until needed (pack_forget in prompt_ffmpeg_install)
install_bar = tk.Frame(console_inner, bg="#3a1a0a", relief=tk.FLAT)
# (not packed yet; prompt_ffmpeg_install decides visibility)

tk.Label(install_bar, text="⚠  FFmpeg not found", bg="#3a1a0a", fg="#ffaa44",
         font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(8, 12))
btn_install_ffmpeg = tk.Button(
    install_bar, text="⬇ Install FFmpeg", command=on_install_ffmpeg_click,
    bg="#c05010", fg=fg_main, relief=tk.FLAT, cursor="hand2",
    font=("Segoe UI", 9, "bold"), activebackground="#d06020",
    padx=10, pady=4
)
btn_install_ffmpeg.pack(side=tk.LEFT, pady=4)
tk.Label(install_bar, text="(via winget)", bg="#3a1a0a", fg="#888888",
         font=("Segoe UI", 7)).pack(side=tk.LEFT, padx=(6, 0))

install_bar.pack(fill=tk.X, padx=0, pady=(0, 6))   # visible by default; hidden if ffmpeg ok

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
# Post-startup FFmpeg check (runs after mainloop starts via `after`)
# ---------------------------------------------------------------------------
root.after(200, prompt_ffmpeg_install)

root.mainloop()
