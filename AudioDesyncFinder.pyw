"""Audio Desync Finder 3.0 - PySide6 + Fluent, Mica backdrop.

The analysis engine lives in sync_py; this file is only the UI.
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
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog,
                               QHBoxLayout, QLabel, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CardWidget, ComboBox, FluentWindow,
                            InfoBar, InfoBarPosition, LineEdit, MessageBox,
                            PrimaryPushButton, ProgressBar, PushButton,
                            RadioButton, SpinBox, StrongBodyLabel, TableWidget,
                            TextEdit, Theme, setTheme, setThemeColor)

import shutil

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

# ---- engine (inlined, was sync_engine.py) ----
"""Sync engine: decoding, multi-point scanning, piecewise model and export.

Extracted from audiosync3.pyw so the Tk UI and the Qt UI can share it.
Pure numpy/scipy + ffmpeg, no GUI code.
"""
import json
import os
import subprocess
import tempfile
import time

import numpy as np
import scipy.signal as signal
from scipy.io import wavfile

_ffmpeg_procs = []


def get_startupinfo():
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        return info
    return None


def _run_ffmpeg(cmd, **kwargs):
    proc = subprocess.Popen(cmd, **kwargs)
    _ffmpeg_procs.append(proc)
    try:
        proc.communicate()
    finally:
        try:
            _ffmpeg_procs.remove(proc)
        except ValueError:
            pass
    if proc.returncode != 0 and proc.returncode not in (-9, -15, 1):
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return subprocess.CompletedProcess(cmd, proc.returncode)


def _run_ffmpeg_progress(cmd, total_s, pct_from, pct_to, report=None, should_stop=None):
    full = [cmd[0], "-nostats", "-progress", "pipe:1"] + list(cmd[1:])
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, startupinfo=get_startupinfo())
    _ffmpeg_procs.append(proc)
    started = time.time()
    last = pct_from
    cancelled = False
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                frac = 0.0 if total_s <= 0 else min(1.0, seconds / total_s)
                pct = pct_from + (pct_to - pct_from) * frac
                if report is not None and pct - last >= 0.4:
                    last = pct
                    report(pct, f"{pct:.0f}%  -  {time.time() - started:.0f}s")
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


def _get_file_duration(path: str) -> float:
    """Media duration in seconds via ffprobe (0.0 when it cannot be read)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, startupinfo=get_startupinfo())
        return float(result.stdout.strip())
    except Exception:
        return 0.0


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

# ---- end engine ----

APP_NAME = "Audio Desync Finder"
APP_VERSION = "3.0.13"
LIGHT = False

ACCENT = "#4cc2ff"


def system_is_light() -> bool:
    """True when Windows uses the light app theme (env var overrides it)."""
    forced = os.environ.get("ADS_THEME", "").lower()
    if forced in ("light", "dark"):
        return forced == "light"
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except Exception:
        return False


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
        self.chart_bg = "#ffffff" if LIGHT else "#202020"
        self.chart_fg = "#39404a" if LIGHT else "#c9cdd2"
        self.chart_grid = "#00000022" if LIGHT else "#ffffff22"
        self.chart_spine = "#c9ccd1" if LIGHT else "#3a3f45"
        self.log_fg = "#39404a" if LIGHT else "#9aa4ae"
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
        self.fps_label = BodyLabel("")
        fl.addWidget(self.fps_label)
        outer.addWidget(files)

        # ---------------- scan settings ---------------------------------
        settings = Card()
        sl = QHBoxLayout(settings)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(10)
        sl.addWidget(StrongBodyLabel("Window (s)"))
        self.entry_window = LineEdit()
        self.entry_window.setText("40")
        self.entry_window.setFixedWidth(110)
        self.entry_window.setAlignment(Qt.AlignCenter)
        self.entry_window.setValidator(QIntValidator(5, 300, self))
        sl.addWidget(self.entry_window)
        sl.addWidget(StrongBodyLabel("Step (s)"))
        self.entry_step = LineEdit()
        self.entry_step.setText("30")
        self.entry_step.setFixedWidth(110)
        self.entry_step.setAlignment(Qt.AlignCenter)
        self.entry_step.setValidator(QIntValidator(2, 300, self))
        sl.addWidget(self.entry_step)
        sl.addWidget(StrongBodyLabel("Min confidence"))
        self.entry_conf = LineEdit()
        self.entry_conf.setText("2")
        self.entry_conf.setFixedWidth(110)
        self.entry_conf.setAlignment(Qt.AlignCenter)
        self.entry_conf.setValidator(QIntValidator(1, 60, self))
        sl.addWidget(self.entry_conf)
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
        self.fig = Figure(figsize=(6, 3.2), facecolor=self.chart_bg)
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
        self.summary = StrongBodyLabel("")
        ml.addWidget(self.summary)
        self.model_view = TextEdit()
        self.model_view.setReadOnly(True)
        self.model_view.setStyleSheet(
            f"TextEdit{{background:transparent;border:none;"
            f"color:{self.chart_fg};font:9pt 'Consolas';}}")
        ml.addWidget(self.model_view)
        bottom.addWidget(model_card, 1)

        log_card = Card()
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(14, 10, 14, 12)
        ll.addWidget(StrongBodyLabel("Log"))
        self.log_view = TextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            f"TextEdit{{background:transparent;border:none;"
            f"color:{self.log_fg};font:9pt 'Consolas';}}")
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
        if not hasattr(self, "base_buttons"):
            self.base_buttons = QButtonGroup(self)   # exactly one Base at a time
        self.base_buttons.addButton(radio)
        if which == "ref":
            self.radio_ref = radio
        else:
            self.radio_tgt = radio
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
        if self.ref_path and self.tgt_path:
            self._load_pair()

    def _load_pair(self):
        ref, tgt = self._ordered_paths()
        r = probe_framerate(ref)
        t = probe_framerate(tgt)
        info = f"Reference: {os.path.basename(ref)}"
        if r:
            info += f"   [{r:g} fps]"
        info += "      →      Target: " + os.path.basename(tgt)
        if t:
            info += f"   [{t:g} fps]"
        if r and t and abs(r - t) > 1e-6:
            info += f"      atempo {r / t:.6f}"
        self.fps_label.setText(info)
        self.status.setText("Ready")

    def _ordered_paths(self):
        base_is_ref = getattr(self, "radio_ref", None) is None or self.radio_ref.isChecked()
        return (self.ref_path, self.tgt_path) if base_is_ref else (self.tgt_path, self.ref_path)

    def _fps_hint(self):
        ref, tgt = self._ordered_paths()
        r, t = probe_framerate(ref), probe_framerate(tgt)
        if r > 0 and t > 0 and abs(r - t) > 1e-9:
            return framerate_slope(r, t)
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
            ref, tgt, meta = decode_pair(
                ref_path, tgt_path,
                progress=lambda m: self.ui_queue.put(("log", m)),
                on_fraction=lambda phase, f: self.ui_queue.put(
                    ("progress", (5 + (30 if phase == "ref" else 32) * f,
                                  f"Decoding {phase}… {f * 100:.0f}%"))))
            self.meta = meta

            hint = self._fps_hint()
            rate = 1.0 + hint / 1000.0 if hint and abs(hint) >= 0.3 else 1.0
            scan_tgt = resample_track(tgt, rate) if rate != 1.0 else tgt
            points = scan_arrays(
                ref, scan_tgt, window=self._num(self.entry_window, 40),
                step=self._num(self.entry_step, 30),
                mode=("auto" if self.combo_signal.currentIndex() == 0
                      else "waveform" if self.combo_signal.currentIndex() == 1
                      else "envelope"),
                progress=lambda done, total: self.ui_queue.put(
                    ("progress", (72 + 27 * done / max(total, 1),
                                  f"Measuring {done}/{total}"))),
                should_stop=self.stop_event.is_set)
            points = sorted(compensate_points(points, rate), key=lambda p: p["t"])
            plan = build_plan(points, meta["ref_duration"],
                                     min_conf=self._num(self.entry_conf, 2),
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
        path = mka_path(path)
        self.btn_export.setEnabled(False)
        self.btn_stop.setEnabled(True)
        threading.Thread(target=self._export_task, args=(path,), daemon=True).start()

    def _export_task(self, path):
        try:
            _, tgt_path = self._ordered_paths()
            export_plan(
                self.plan, tgt_path, path,
                progress=lambda p, t: self.ui_queue.put(("progress", (p, t))),
                log=lambda m, tag="info": self.ui_queue.put(("log", m)),
                should_stop=self.stop_event.is_set)
            self.ui_queue.put(("log", f"✓ Saved: {path}"))
            self.ui_queue.put(("notify", (path, True)))
            residual = measure_residual(self._ordered_paths()[0], path)
            if residual:
                for line in format_residual(residual).splitlines():
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
                self.model_view.setPlainText("\n".join(plan_lines(payload)[:10]))
                self._update_summary()
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

    def _update_summary(self) -> None:
        """Headline numbers for the current model, in the accent colour."""
        segments = (self.plan or {}).get("segments") or []
        if not segments:
            self.summary.setText("")
            return
        first = segments[0]
        if len(segments) == 1:
            text = (f"offset {first['offset']:+.2f} ms      "
                    f"drift {first['slope']:+.3f} ms/s      "
                    f"atempo {first['atempo']:.6f}      "
                    f"RMS {first['rms']:.0f} ms")
        else:
            text = (f"{len(segments)} segments       "
                    f"first offset {first['offset']:+.2f} ms       "
                    f"drift {first['slope']:+.3f} ms/s       "
                    f"RMS {first['rms']:.0f} ms")
        self.summary.setText(text)
        self.summary.setStyleSheet(
            f"color: {self.accent}; font: 600 11pt 'Segoe UI';")

    def apply_palette(self) -> None:
        """Recompute the chart/text colours for the current theme."""
        self.chart_bg = "#ffffff" if LIGHT else "#202020"
        self.chart_fg = "#39404a" if LIGHT else "#c9cdd2"
        self.chart_grid = "#00000022" if LIGHT else "#ffffff22"
        self.chart_spine = "#c9ccd1" if LIGHT else "#3a3f45"
        self.log_fg = "#39404a" if LIGHT else "#9aa4ae"
        self.accent = system_accent_hex()
        self._update_summary()
        try:
            self.fig.patch.set_facecolor(self.chart_bg)
            self.ax.set_facecolor(self.chart_bg)
            self.model_view.setStyleSheet(
                f"TextEdit{{background:transparent;border:none;"
                f"color:{self.chart_fg};font:9pt 'Consolas';}}")
            self.log_view.setStyleSheet(
                f"TextEdit{{background:transparent;border:none;"
                f"color:{self.log_fg};font:9pt 'Consolas';}}")
        except Exception:
            pass

    def _reload_theme(self) -> None:
        """Windows told us the theme changed while the app was running."""
        global LIGHT
        new_light = system_is_light()
        if new_light == LIGHT:
            return
        LIGHT = new_light
        setTheme(Theme.LIGHT if LIGHT else Theme.DARK)
        setThemeColor(QColor(system_accent_hex()))
        self.apply_palette()
        self._draw()
        self.log("Theme switched to " + ("light" if LIGHT else "dark"))

    def nativeEvent(self, eventType, message):
        """Catch WM_SETTINGCHANGE so the theme follows Windows live."""
        try:
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x001A:          # WM_SETTINGCHANGE
                QTimer.singleShot(350, self._reload_theme)
        except Exception:
            pass
        return super().nativeEvent(eventType, message)

    def _num(self, edit, default):
        """Read an int from a text field, falling back to the default."""
        try:
            value = int(str(edit.text()).strip())
            return float(value)
        except (TypeError, ValueError):
            return float(default)

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
        plan = build_plan(self.points, self.meta["ref_duration"],
                                 min_conf=self._num(self.entry_conf, 2),
                                 slope_hint=self._fps_hint())
        self.plan = plan
        self.model_view.setPlainText("\n".join(plan_lines(plan)[:10]))
        self._update_summary()
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
        ax.set_facecolor(self.chart_bg)
        if self.points:
            ax.scatter([p["t"] for p in self.points],
                       [p["offset"] for p in self.points],
                       s=8, color=self.accent, alpha=0.9)
        for seg in (self.plan or {}).get("segments", []):
            x0, x1 = seg["t_start"], seg["t_end"]
            y0 = seg["offset"]
            ax.plot([x0, x1], [y0, y0 + seg["slope"] * (x1 - x0)],
                    color="#3ddc97", lw=1.6)
        ax.axhline(0, color=self.chart_spine, lw=0.7, ls=":")
        ax.tick_params(colors=self.chart_fg, labelsize=7)
        for spine in ax.spines.values():
            spine.set_color(self.chart_spine)
        ax.grid(alpha=0.25, color=self.chart_grid)
        ax.set_xlabel("Time (s)", color=self.chart_fg, fontsize=8)
        ax.set_ylabel("Offset (ms)", color=self.chart_fg, fontsize=8)
        self.canvas.draw_idle()


def main():
    global LIGHT
    LIGHT = system_is_light()
    setTheme(Theme.LIGHT if LIGHT else Theme.DARK)
    setThemeColor(QColor(system_accent_hex()))
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    QTimer.singleShot(500, win.check_ffmpeg)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
