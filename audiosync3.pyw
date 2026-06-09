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
# STARTUP DEPENDENCY CHECKS
# Fail fast with a clear message if FFmpeg or Python packages are missing,
# rather than crashing later with a cryptic traceback.
# ---------------------------------------------------------------------------
if not shutil.which("ffmpeg"):
    show_startup_error(
        "Missing FFmpeg",
        "FFmpeg was not found in PATH.\n\n"
        "Install FFmpeg, make sure ffmpeg.exe is available from PATH, and restart this app."
    )
    sys.exit(1)

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
            res = (f"⏱️ {target_name} IS DELAYED\nOffset: +{offset_ms} ms\n"
                   f"→ Advance {target_name} by {offset_ms} ms")
            color = "#ffa500"
        else:
            res = (f"⏱️ {target_name} IS AHEAD\nOffset: {offset_ms} ms\n"
                   f"→ Delay {target_name} by {abs(offset_ms)} ms")
            color = "#ff6b6b"

        label_result.config(text=res, fg=color)
    except ValueError:
        messagebox.showerror("Error", "Invalid offset value!")


def zoom_in() -> None:
    """
    Halve the current zoom window width, centred on the current view midpoint.
    No-op when already at the minimum zoom level or when no data is loaded.
    """
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

def analyze_sync() -> None:
    """
    Main analysis pipeline triggered by the ANALYZE button.

    Steps:
        1. Validate that both file paths are provided.
        2. Use FFmpeg to extract a mono 44.1 kHz WAV snippet from each file,
           applying the user-defined start time, duration, and per-file
           pre-offsets.  Temporary files are written to the system temp dir.
        3. Load the WAV files with scipy, flatten to mono if necessary, and
           convert to float64.
        4. Remove DC offset and normalise by RMS energy so that the correlation
           measures shape similarity rather than loudness difference.
        5. Apply a 4th-order Butterworth bandpass filter (100 Hz – 8 kHz) to
           both signals to focus on the speech/music band and reject rumble.
        6. Compute the full cross-correlation via FFT and find the lag of the
           absolute peak.
        7. Refine the integer lag with parabolic interpolation to achieve
           sub-sample (sub-millisecond) precision.
        8. Convert lag to milliseconds, add the pre-offset contribution, and
           display a human-readable result.
        9. Store the processed waveforms and offset in viz_data and trigger a
           chart redraw.

    All exceptions are caught and reported as error dialogs so that the GUI
    remains responsive on unexpected failures.
    """
    file1 = entry_file1.get()
    file2 = entry_file2.get()

    if not file1 or not file2:
        messagebox.showerror("Error", "Select both audio/video files!")
        return

    path_wav1 = os.path.join(tempfile.gettempdir(), "sync_temp_1.wav")
    path_wav2 = os.path.join(tempfile.gettempdir(), "sync_temp_2.wav")

    # Remove stale temporary files from a previous run to avoid reading old data
    for p in [path_wav1, path_wav2]:
        if os.path.exists(p):
            os.remove(p)

    label_status.config(text="● Extracting audio from files...", fg="#ffa500")
    root.update()

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

        subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, startupinfo=get_startupinfo())
        subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, startupinfo=get_startupinfo())

        label_status.config(text="● Analyzing synchronization (signal correlation)...", fg="#ffa500")
        root.update()

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
            res = "✓ Files perfectly synchronized!\nOffset: 0.00 ms"
            color = "#00ff88"
        elif offset_ms > 0:
            res = (f"⏱️ {target_name} IS DELAYED\nOffset: +{offset_ms} ms\n"
                   f"→ Advance {target_name} by {offset_ms} ms")
            color = "#ffa500"
        else:
            res = (f"⏱️ {target_name} IS AHEAD\nOffset: {offset_ms} ms\n"
                   f"→ Delay {target_name} by {abs(offset_ms)} ms")
            color = "#ff6b6b"

        # Append pre-offset summary and total offset when pre-offsets were used
        if preoffset1 != 0 or preoffset2 != 0:
            res += "\n\n📌 Pre-offsets:"
            if preoffset1 != 0:
                res += f"\n   File 1: +{preoffset1 * 1000:.0f} ms"
            if preoffset2 != 0:
                res += f"\n   File 2: +{preoffset2 * 1000:.0f} ms"

            res += "\n\n🎯 TOTAL OFFSET: "
            if total_offset_ms == 0:
                res += "0.00 ms"
            elif total_offset_ms > 0:
                res += f"+{total_offset_ms} ms"
            else:
                res += f"{total_offset_ms} ms"

        label_result.config(text=res, fg=color)
        label_status.config(text="✓ Analysis completed successfully", fg="#00ff88")

        # Store filtered waveforms and integer lag for the visualisation layer
        viz_data['data1'] = data1_filtered
        viz_data['data2'] = data2_filtered
        viz_data['sr'] = sr1
        viz_data['offset_samples'] = int(lag)
        viz_data['zoom_start'] = 0
        viz_data['zoom_end'] = len(data1_filtered)

        # Pre-fill the manual offset field with the detected offset so the user
        # can fine-tune it without having to type the value from scratch
        entry_manual_offset.delete(0, tk.END)
        entry_manual_offset.insert(0, str(offset_ms))

        update_visualization()

    except Exception as e:
        messagebox.showerror("Error", f"Analysis failed:\n{str(e)}")
        label_status.config(text="✗ Error occurred during analysis", fg="#ff4444")
        label_result.config(text="Could not analyze files.\nCheck if they are valid.", fg="#888888")

# ===========================================================================
# GUI SETUP
# All widget construction happens at module level after the functions are
# defined.  Widgets that need to be referenced inside callbacks are stored in
# variables that are accessible to the whole module (entry_file1, ref_var, …).
# ===========================================================================
root = tk.Tk()
root.title("Audio Sync Finder - Precise Audio Offset Detection")
root.geometry("1600x900")
root.configure(bg="#1a1a1a")
root.resizable(False, False)

bg_main = "#1a1a1a"
bg_panel = "#252525"
bg_input = "#2d2d2d"
fg_main = "#ffffff"
fg_dim = "#aaaaaa"
fg_accent = "#4a9eff"
btn_primary = "#2a7de1"
btn_success = "#28a745"
btn_hover = "#3d8bff"

ref_var = tk.IntVar(value=1)

# Main container
main_container = tk.Frame(root, bg=bg_main)
main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

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

# === BOTTOM ROW: Chart + Control Panel ===
bottom_row = tk.Frame(main_container, bg=bg_main)
bottom_row.pack(fill=tk.BOTH, expand=True)

# CHART (LEFT)
chart_frame = tk.LabelFrame(bottom_row, text=" 📊 Waveform Visualization ", bg=bg_panel, fg=fg_accent,
                            font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2)
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

# CONTROL PANEL (RIGHT)
control_panel = tk.LabelFrame(bottom_row, text=" Controls ", bg=bg_panel, fg=fg_accent,
                              font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2, width=300)
control_panel.pack(side=tk.LEFT, fill=tk.Y)
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

# TIPS SECTION (expandable)
tips_frame = tk.LabelFrame(controls_inner, text=" 💡 Tips ", bg=bg_panel, fg=fg_accent,
                           font=("Segoe UI", 9, "bold"), relief=tk.GROOVE, bd=1)
tips_frame.pack(fill=tk.BOTH, expand=True)

tips_text = tk.Text(tips_frame, bg=bg_panel, fg="#999999",
                    font=("Segoe UI", 7), relief=tk.FLAT, wrap=tk.WORD,
                    borderwidth=0, highlightthickness=0, cursor="arrow")
tips_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
tips_text.insert(1.0, "SELECT clips with:\n• Clear speech/music\n• Single language\n• Sharp sounds (claps/hits)\n\nAVOID:\n• Multilingual sections\n• Background noise\n• Long pauses/silence")
tips_text.config(state=tk.DISABLED)

root.mainloop()
