"""Audio Desync Finder 3.0 - PySide6 + Fluent, Mica backdrop.

A single file: the user interface and the analysis engine live together.
Run: pythonw AudioDesyncFinder-Qt.pyw
"""

# Audio Desync Finder - Copyright (C) 2026 Janusz Woźniak
# Licensed under the GNU General Public License v3 or later.
# See LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt - no warranty.

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
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
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

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QIcon, QIntValidator
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog,
                               QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton,
                               QSlider, QTableWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import QUrl

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    _HAVE_PLAYER = True
except Exception:                      # a stripped PySide6 without Multimedia
    QAudioOutput = QMediaPlayer = QVideoWidget = None
    _HAVE_PLAYER = False

from qfluentwidgets import (BodyLabel, CardWidget, CheckBox, ComboBox,
                            FluentWindow, InfoBar, InfoBarPosition, LineEdit,
                            MessageBox, PrimaryPushButton, ProgressBar,
                            PushButton, RadioButton, SpinBox, StrongBodyLabel,
                            TableWidget, TextEdit, Theme, setTheme, setThemeColor)

import shutil

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MultipleLocator

# ---- engine (inlined, was sync_engine.py) ----
"""Sync engine: decoding, multi-point scanning, piecewise model and export.

Extracted from audiosync3.pyw so the Tk UI and the Qt UI can share it.
Pure numpy/scipy + ffmpeg, no GUI code.
"""
import hashlib
import json
from concurrent.futures.thread import ThreadPoolExecutor
import os
import subprocess
import tempfile
import time

import numpy as np
import scipy.signal as signal
from scipy.io import wavfile

_ffmpeg_procs = []
_DECODE_MODE = {"value": "?"}     # what the last _decode_track actually used


def acquire_single_instance(name: str = "AudioDesyncFinder"):
    """
    Hold a named Windows mutex for the life of the process.

    Returns the handle (keep it alive) or None when another instance already
    owns the name.  A second copy would fight the first one for the same files
    and the same decoded-audio cache, and none of the UI is built for two.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
        if not handle:
            return True                  # cannot tell - do not block the app
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return None
        return handle
    except Exception:
        return True


def show_message(title: str, text: str) -> None:
    """A message box for the moments before (or without) a GUI toolkit."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def get_startupinfo():
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        return info
    return None


def _run_ffmpeg_progress(cmd, total_s, pct_from, pct_to, report=None, should_stop=None):
    full = [cmd[0], "-nostats", "-stats_period", "0.05",
            "-progress", "pipe:1"] + list(cmd[1:])
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
                if report is not None and pct - last >= 0.01:
                    last = pct
                    report(pct, f"{pct:.3f}%  -  {time.time() - started:.0f}s")
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
        report(pct_to, f"{pct_to:.3f}%")
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

# A drift is only "noise" if it stays below this over the whole segment.  The
# limit cannot be a rate: 0.06 ms/s is nothing over a minute and 350 ms over a
# two-hour film.
FLAT_DRIFT_MS = 40.0

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
            capture_output=True, text=True, timeout=30,
            startupinfo=get_startupinfo())
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _decode_workers() -> int:
    """All cores but two, so the desktop stays usable while decoding."""
    return max(1, (os.cpu_count() or 4) - 2)


def _decode_parallel(path: str, sr: int, total: float, workers: int,
                     on_fraction=None, should_stop=None):
    """
    Decode the track in *workers* slices at once and concatenate the PCM.

    FFmpeg decodes a single stream on one core, so splitting the timeline is the
    only way to use the machine.  Each slice is decoded with half a second of
    overlap and trimmed to the exact sample count, so the joins are seamless.

    Every slice follows its own FFmpeg position (-progress on stderr, stdout
    carries the PCM): equal slices started together all finish at the same
    moment, so counting finished slices left the bar frozen until the very end.
    """
    # Python 3.15 turns concurrent.futures.ThreadPoolExecutor into a lazy-import
    # proxy that can stay unresolved inside this process (calling it raises
    # "'lazy_import' object is not callable"), so the executor comes straight
    # from its own module, which never goes through that proxy.
    from concurrent.futures import as_completed
    from concurrent.futures.thread import ThreadPoolExecutor

    chunk = total / workers
    parts = [None] * workers
    done = [0.0] * workers          # seconds of audio decoded inside each slice
    lock = threading.Lock()
    reported = [0.0]
    last_tick = [0.0]

    def report(force: bool = False) -> None:
        """Overall fraction from the slice positions, at most 30 times a second."""
        if on_fraction is None:
            return
        now = time.time()
        with lock:
            frac = min(1.0, sum(done) / total) if total > 0 else 0.0
            if not force and (frac - reported[0] < 0.0001
                              or now - last_tick[0] < 0.03):
                return
            reported[0] = frac
            last_tick[0] = now
        on_fraction(frac)

    def run(index: int) -> None:
        start = index * chunk
        length = chunk + (0.5 if index < workers - 1 else 0.0)
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-nostats",
               "-stats_period", "0.05", "-progress", "pipe:2",
               "-ss", f"{start:.3f}", "-i", path, "-t", f"{length:.3f}",
               "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(sr),
               "-f", "s16le", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL,
                                startupinfo=get_startupinfo())
        _ffmpeg_procs.append(proc)

        def follow() -> None:
            """Keep this slice's position in *done* while it decodes."""
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("out_time_us="):
                    continue
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                with lock:
                    done[index] = max(done[index], min(seconds, length))
                report()

        follower = threading.Thread(target=follow, daemon=True)
        follower.start()
        try:
            data = proc.stdout.read()
        finally:
            proc.wait()
            follower.join(timeout=10.0)
            try:
                _ffmpeg_procs.remove(proc)
            except ValueError:
                pass
            try:
                proc.stdout.close()
                proc.stderr.close()
            except OSError:
                pass
        piece = np.frombuffer(data, dtype=np.int16)
        if index < workers - 1:
            piece = piece[:int(round(chunk * sr))]
        parts[index] = piece
        with lock:
            done[index] = length
        report(force=True)

    # Stop has to bite while every slice is still reading its pipe: waiting for
    # the next completed future would keep the app busy for a whole slice.
    stop_watch = threading.Event()
    cancelled = threading.Event()

    def watch() -> None:
        """Kill the running slices as soon as the caller asks to stop."""
        while not stop_watch.wait(0.2):
            if should_stop is not None and should_stop():
                cancelled.set()
                for proc in list(_ffmpeg_procs):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = [pool.submit(run, i) for i in range(workers)]
            for fut in as_completed(pending):
                if cancelled.is_set() or (should_stop is not None and should_stop()):
                    for proc in list(_ffmpeg_procs):
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise RuntimeError("Cancelled by the user")
                fut.result()
    finally:
        stop_watch.set()
        watcher.join(timeout=1.0)

    usable = [p for p in parts if p is not None and len(p)]
    return np.concatenate(usable) if usable else np.zeros(0, dtype=np.int16)


def _decode_track(path: str, tag: str, sr: int = SCAN_SR, on_fraction=None,
                  should_stop=None, workers=None):
    """
    Decode the first audio stream of *path* to mono 16-bit PCM at *sr*.

    on_fraction(0..1) is called while FFmpeg works, so the caller can drive a
    progress bar – decoding a 20 GB remux takes minutes and used to look dead.

    Returns (sample_rate, int16 ndarray).  Raises RuntimeError with FFmpeg's
    own message when the file cannot be decoded.
    """
    out = os.path.join(tempfile.gettempdir(), f"ads_scan_{tag}_{os.getpid()}.wav")
    total = _get_file_duration(path) or 0.0

    workers = max(1, int(workers or _decode_workers()))
    if total >= 180 and workers > 1:
        data = _decode_parallel(path, sr, total, workers, on_fraction, should_stop)
        if len(data):
            _DECODE_MODE["value"] = f"parallel, {workers} slices"
            return sr, np.ascontiguousarray(data.astype(np.int16, copy=False))
    _DECODE_MODE["value"] = "sequential"
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-stats_period", "0.05",
           "-progress", "pipe:1",
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
                if should_stop is not None and should_stop():
                    proc.kill()
                    raise RuntimeError("Cancelled by the user")
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


def decode_pair(path_ref: str, path_target: str, progress=None, on_fraction=None,
                should_stop=None, workers=None):
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

    ref = _track_from_cache_or_disk(path_ref, "ref", "Reference", progress,
                                    report, should_stop, workers)
    tgt = _track_from_cache_or_disk(path_target, "tgt", "Target", progress,
                                    report, should_stop, workers)
    return ref, tgt, pair_meta(ref, tgt)


def _track_from_cache_or_disk(path, role, label, progress, report, should_stop,
                              workers):
    """One decoded track: from the cache when that file was decoded before."""
    key = track_key(path)
    started = time.time()
    cached = load_cached_track(key)
    if cached is not None:
        if progress:
            progress(f"{label}: {len(cached) / float(SCAN_SR):.0f} s of audio "
                     "from the cache")
        return cached
    if progress:
        progress(f"Decoding {label.lower()}…")
    _, data = _decode_track(path, role, on_fraction=report(role),
                            should_stop=should_stop, workers=workers)
    if progress:
        progress(f"{label}: {len(data) / float(SCAN_SR):.0f} s of audio in "
                 f"{time.time() - started:.1f} s ({_DECODE_MODE['value']})")
    save_cached_track(key, data)
    return data


def track_key(path: str):
    """
    Identity of one file on disk, so its decoded PCM can be reused.

    Decoding is the expensive part of a scan - minutes of reading a slow disk -
    and Window, Step, Min confidence, Signal or Cores do not change a single
    sample of it.  The cache is per file, not per pair: the same reference
    against a third target is still a cache hit.  Name, size and modification
    time are enough to notice that the file was replaced meanwhile.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


# Decoded audio outlives the process: two raw 16 kHz tracks on disk, so the
# next session does not go back to the remux.  Reading 700 MB sequentially
# beats decoding a 60 GB file by an order of magnitude.
CACHE_LIMIT_BYTES = 2 * 1024 ** 3        # 2 GB of decoded audio
CACHE_KEEP_FILES = 4                     # two pairs, per file


def _cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "AudioDesyncFinder", "cache")


def _cache_file(key) -> str:
    digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_cache_dir(), f"track-{digest}.pcm")


def pair_meta(ref, tgt) -> dict:
    """Durations of a decoded pair, shared by the decoder and a cache hit."""
    sr = SCAN_SR
    return {
        "sr": sr,
        "ref_duration": len(ref) / float(sr),
        "target_duration": len(tgt) / float(sr),
        "duration": min(len(ref), len(tgt)) / float(sr),
    }


def load_cached_track(key):
    """The decoded track an earlier scan wrote, or None."""
    if key is None:
        return None
    path = _cache_file(key)
    try:
        if not os.path.isfile(path):
            return None
        data = np.fromfile(path, dtype=np.int16)
    except OSError:
        return None
    if len(data) < SCAN_SR:
        return None
    try:
        os.utime(path, None)             # the pruner keeps what was used last
    except OSError:
        pass
    return data


def save_cached_track(key, data) -> None:
    """Write one decoded track for the next session and keep the cache under its cap."""
    if key is None or len(data) * 2 > CACHE_LIMIT_BYTES:
        return
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        path = _cache_file(key)
        temp = path + ".tmp"
        data.tofile(temp)
        os.replace(temp, path)
    except OSError:
        return
    _prune_cache()


def clear_decoded_cache():
    """
    Delete every cached decoded track, keeping the folder itself.

    Returns (bytes freed, files removed) so the caller can report what happened.
    """
    freed = 0
    removed = 0
    for folder, suffixes in ((_cache_dir(), (".pcm", ".tmp")),
                             (_result_dir(), (".json",))):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(suffixes):
                continue
            path = os.path.join(folder, name)
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue
            freed += size
            removed += 1
    return freed, removed


RESULT_KEEP = 20          # remembered scans on disk
RESULT_LIMIT_BYTES = 200 * 1024 ** 2


def _result_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "AudioDesyncFinder", "results")


def result_key(ref_path, tgt_path, window, step, min_conf, mode, allow_drift,
               hint):
    """
    Identity of one exact scan: both files and every setting that shapes it.

    Decoding already remembers each file; this remembers the *answer*, so asking
    the same question twice (same pair, same settings) does not measure the film
    again.  Cores are left out on purpose - they change the speed, not the result.
    """
    return [repr(track_key(ref_path)), repr(track_key(tgt_path)),
            round(float(window), 3), round(float(step), 3),
            round(float(min_conf), 3), str(mode), bool(allow_drift),
            None if hint is None else round(float(hint), 4)]


def _result_file(key) -> str:
    digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_result_dir(), f"scan-{digest}.json")


def load_result(key):
    """The points and plan of an identical earlier scan, or None."""
    if not key:
        return None
    path = _result_file(key)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if payload.get("key") != key:
        return None
    points = payload.get("points")
    plan = payload.get("plan")
    if not isinstance(points, list) or not isinstance(plan, dict):
        return None
    if not plan.get("segments"):
        return None
    try:
        os.utime(path, None)
    except OSError:
        pass
    return points, plan


def save_result(key, points, plan) -> None:
    """Remember this scan so the same question can be answered instantly."""
    if not key or not plan or not plan.get("segments"):
        return
    try:
        os.makedirs(_result_dir(), exist_ok=True)
        path = _result_file(key)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"key": key, "points": points, "plan": plan},
                      handle, default=float)
    except (OSError, ValueError, TypeError):
        return
    _prune_results()


def _prune_results() -> None:
    try:
        entries = []
        for name in os.listdir(_result_dir()):
            if not name.endswith(".json"):
                continue
            path = os.path.join(_result_dir(), name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
    except OSError:
        return
    entries.sort()
    total = sum(entry[1] for entry in entries)
    while entries and (len(entries) > RESULT_KEEP or total > RESULT_LIMIT_BYTES):
        _mtime, size, path = entries.pop(0)
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass

def _prune_cache() -> None:
    """Keep one pair: at most CACHE_KEEP_FILES files, under the byte limit."""
    try:
        entries = []
        for name in os.listdir(_cache_dir()):
            if not name.endswith(".pcm"):
                continue
            path = os.path.join(_cache_dir(), name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
    except OSError:
        return
    entries.sort()                       # oldest first
    total = sum(entry[1] for entry in entries)
    while entries and (len(entries) > CACHE_KEEP_FILES
                       or total > CACHE_LIMIT_BYTES):
        _, size, path = entries.pop(0)
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


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

    # square in floating point: int16 would overflow and turn the envelope
    # into NaN, which silently disabled the coarse alignment
    seg = np.asarray(seg, dtype=np.float64)
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
                       half_span: float = 60.0, window: float = 4.0,
                       step: float = 1.0, mode: str = "auto", should_stop=None,
                       workers: int = 1):
    """
    Re-measure around every detected jump with short windows, densely.

    A 40 s analysis window blurs the exact position of an edit to about half a
    window, and the coarse scan itself only lands every 30 s.  Two minutes either
    side are measured every second with 4 s windows, so the chart is packed where
    it matters and the breakpoint is visible to the second - which is what
    decides where the export cuts the audio.
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
                                 should_stop=should_stop, workers=workers))
    return extra


def scan_arrays(a, b, window: float = 40.0, step: float = 30.0,
                t_from: float = 0.0, t_to=None, mode: str = "auto",
                progress=None, should_stop=None, workers: int = 1):
    """
    Measure the offset at regular positions across the timeline.

    mode is forwarded to measure_pair(): "auto", "waveform" or "envelope".

    The windows are independent, so they run in a thread pool: the correlation
    itself is scipy's FFT, which releases the GIL, and eight threads measure a
    film about two and a half times faster than one.  Results come back in
    timeline order whatever order they finish in.

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
    starts = []
    t = t_from
    while t + MIN_WINDOW_S <= t_to:
        w = min(window, t_to - t)
        if w < MIN_WINDOW_S:
            break
        starts.append((t, w))
        t += step
    total = len(starts)
    if not total:
        return []

    def measure(item):
        start, length = item
        found = measure_pair(a, b, start, length, sos, mode=mode)
        if found is not None:
            found["t"] = round(start + length / 2.0, 2)
            found["window"] = round(length, 2)
            found["kind"] = "auto"
        return found

    points = []
    done = 0
    if int(workers) > 1:
        with ThreadPoolExecutor(max_workers=int(workers)) as pool:
            index = 0
            batch = max(1, int(workers) * 4)   # keeps memory and progress sane
            while index < total:
                if should_stop and should_stop():
                    return points
                chunk = starts[index:index + batch]
                for result in pool.map(measure, chunk):
                    done += 1
                    if progress:
                        progress(done, total)
                    if result is not None:
                        points.append(result)
                index += batch
    else:
        for item in starts:
            result = measure(item)
            done += 1
            if progress:
                progress(done, total)
            if result is not None:
                points.append(result)
            if should_stop and should_stop():
                break
    points.sort(key=lambda p: p["t"])
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
                     window: float = 40.0, step: float = 120.0, ref_array=None):
    """
    Measure what is left between the reference and a produced file.

    Decodes both, measures the offset along the timeline (envelope mode – the
    languages differ) and returns (median_ms, mad_ms, points).  Positive means
    the produced file is late.  Returns None when nothing usable was measured.

    ref_array lets the caller hand over the reference it already has (the scan
    keeps it in memory and on disk): re-reading a three-hour remux just to
    verify an export is the slowest thing the program could do.
    """
    if ref_array is None:
        ref, produced, _meta = decode_pair(ref_path, out_path)
    else:
        ref = ref_array
        _, produced = _decode_track(out_path, "res")
    points = scan_arrays(ref, produced, window=window, step=step, mode="envelope")
    del produced
    if ref_array is None:
        del ref

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


def _weighted_median(values, weights) -> float:
    """Median that lets the confident measurements decide."""
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if not len(values):
        return 0.0
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    total = float(weights.sum())
    if total <= 0.0:
        return float(np.median(values))
    index = int(np.searchsorted(np.cumsum(weights), total / 2.0))
    return float(values[min(index, len(values) - 1)])


def _confidence_weights(pts):
    """
    How much each measurement should count, from how clean its match was.

    A window of identical music correlates sharply and gets a high confidence;
    a window of re-recorded dialogue correlates vaguely and gets a low one.
    Squaring that turns "we found it" into "we found it unambiguously", which
    is what the offset should be built from.
    """
    return np.array([max(0.5, float(p.get("conf", p.get("ncc", 1.0)))) ** 2
                     for p in pts], dtype=float)

def _fit_group(group, fallback_slope: float, outlier_ms: float,
               allow_slope: bool = True):
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
    weights = _confidence_weights(pts)

    span = float(ts.max() - ts.min()) if len(ts) else 0.0
    # A short group cannot carry its own slope: two points always look
    # perfectly straight and five noisy ones invent a tempo.  Anything shorter
    # than ten minutes takes the file's trend and fits only its own level.
    # allow_slope=False (equal framerates, Tempo unticked) forces a level.
    if allow_slope and len(pts) >= 6 and span > 600.0:
        slopes = []
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                dt = ts[b] - ts[a]
                if abs(dt) > 0.5:
                    slopes.append((ys[b] - ys[a]) / dt)
        slope = float(np.median(slopes)) if slopes else float(fallback_slope)
        intercept = _weighted_median(ys - slope * ts, weights)
    else:
        slope = float(fallback_slope)
        intercept = _weighted_median(ys - slope * ts, weights)

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
        weights = _confidence_weights(pts)
        if float(ts.max() - ts.min()) <= 0.5:
            break
        if allow_slope:
            slope = float(np.polyfit(ts, ys, 1, w=np.sqrt(weights))[0])
            intercept = _weighted_median(ys - slope * ts, weights)
        else:
            # the refit after dropping outliers must respect the same rule:
            # fitting a slope here silently undid the forced level
            slope = float(fallback_slope)
            intercept = _weighted_median(ys - slope * ts, weights)
        resid = ys - (slope * ts + intercept)

    rms = float(np.sqrt(np.mean(resid ** 2))) if len(resid) else 0.0
    return float(slope), float(intercept), pts, rms


COARSE_MARGIN = 1.15     # a shift must beat "already in sync" by this much


def format_timecode(seconds: float) -> str:
    """Film time as minutes:seconds (or h:mm:ss past the hour)."""
    total = int(round(float(seconds)))
    hours, rest = divmod(max(total, 0), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_timecode(text: str):
    """Seconds from "2:34", "1:07:15", "95" or "95.5"; None when unreadable."""
    if text is None:
        return None
    cleaned = str(text).strip().replace(",", ".")
    if not cleaned:
        return None
    values = []
    for part in cleaned.split(":"):
        part = part.strip()
        if part == "":
            return None
        try:
            values.append(float(part))
        except ValueError:
            return None
    if not values or len(values) > 3:
        return None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60.0 + value
    return seconds


def time_tick_step(duration: float) -> float:
    """Tick spacing that puts a dozen labels on the time axis."""
    for step in (15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if float(duration) / step <= 18:
            return float(step)
    return 3600.0

def coarse_offset(a, b) -> float:
    """
    Rough whole-track offset in milliseconds.

    A single window can only resolve offsets up to its own length, so a target
    that starts minutes late would never be found by the scan.  Correlating the
    full-length energy envelopes finds any offset, accurate to a few ms.

    Two corrections matter more than the peak itself:

    * the sum is divided by the number of overlapping samples, otherwise a lag
      with a long overlap always wins and a short one can never compete;
    * only positive correlation counts - two energy envelopes cannot be
      "anti-aligned", so an absolute value would happily pick a trough.

    And a shift has to be clearly better than doing nothing.  Two dubs of the
    same film share their music and effects, so a spurious peak seconds away
    used to be trusted and dragged every later measurement with it; a real
    intro difference (24 s, say) beats zero alignment by a wide margin, a
    coincidence does not.
    """
    env_a, env_b = _envelope_of(a, SCAN_SR), _envelope_of(b, SCAN_SR)
    if len(env_a) < 10 or len(env_b) < 10:
        return 0.0
    corr = signal.correlate(env_a, env_b, mode="full", method="fft")
    lags = signal.correlation_lags(len(env_a), len(env_b), mode="full")
    overlap = np.minimum(np.minimum(len(env_a), len(env_b)),
                         np.minimum(len(env_a) - lags, len(env_b) + lags))
    score = corr / np.maximum(overlap, 1)
    # a handful of samples at a wild lag can score +-1 by accident, so only
    # lags that still overlap the two tracks properly are allowed to win
    valid = overlap >= 0.4 * min(len(env_a), len(env_b))
    masked = np.where(valid, score, -np.inf)
    at_zero = float(score[int(np.where(lags == 0)[0][0])])
    best = int(np.argmax(masked))
    best_lag = float(lags[best])
    best_score = float(masked[best])
    # A real offset aligns the shared music and effects and scores 0.9+; noise
    # and coincidence stay under 0.1.  Both tests have to pass, otherwise two
    # dubs of the same cut get dragged a minute and a half apart.
    if best_score < 0.15 or best_score < at_zero * COARSE_MARGIN:
        return 0.0
    return float(-best_lag * ENVELOPE_HOP_S * 1000.0)


def shift_track_by_ms(track, delta_ms: float):
    """Advance the track by delta_ms; positive values cut from the start."""
    n = int(round(float(delta_ms) / 1000.0 * SCAN_SR))
    if n > 0:
        if n >= len(track) - SCAN_SR:
            return track
        return track[n:]
    if n < 0:
        return np.concatenate([np.zeros(-n, dtype=track.dtype), track])
    return track


FPS_SAME_TOLERANCE = 0.01     # 23.976000 vs 23.976024 is the same rate


def framerate_differs(fps_ref: float, fps_tgt: float) -> bool:
    """
    True when the two rates differ by more than floating-point noise.

    Two releases of the same film both show "23.976", but one may report
    23.976000 and the other 23.976024 - a difference of 0.00002 fps, or
    0.0001 %.  Treating that as a tempo difference fits a drift out of noise.
    The cut-off is 0.01 fps (0.04 %), still far below anything audible.
    """
    if not fps_ref or not fps_tgt:
        return False
    return abs(float(fps_ref) - float(fps_tgt)) >= FPS_SAME_TOLERANCE


def speed_hint(ref_path, tgt_path, fps_ref=None, fps_tgt=None):
    """
    Drift the durations imply, cross-checked against the framerates.

    A real speed change is one of a few known ratios (25/23.976 PAL, 24/23.976,
    25/24); anything else that makes the running times differ is a different
    edit - a longer intro, added credits - and must not become a drift hint.
    The framerates only confirm it, and a 2x ratio means a TV rip with doubled
    frames, where the length ratio is the only usable signal.
    """
    known = (25.0 / 23.976, 24.0 / 23.976, 25.0 / 24.0)
    if fps_ref is None or fps_tgt is None:
        fps_ref, fps_tgt = probe_framerate(ref_path), probe_framerate(tgt_path)
    dur_ref = _get_file_duration(ref_path)
    dur_tgt = _get_file_duration(tgt_path)

    if not (dur_ref > 60 and dur_tgt > 60):
        if framerate_differs(fps_ref, fps_tgt):
            return framerate_slope(fps_ref, fps_tgt)
        return None

    length_ratio = dur_ref / dur_tgt
    if abs(length_ratio - 1.0) > 0.08:
        return None                      # far too big for a speed change
    # A 0.1 % difference (24 vs 23.976) is six seconds across a film, so it
    # is not "the same running time": the framerate check below decides
    # whether it is a speed change or a different edit.

    from_ratio = (length_ratio - 1.0) * 1000.0
    if not framerate_differs(fps_ref, fps_tgt):
        return None                      # same framerate: the gap is an edit
    fps_ratio = fps_ref / fps_tgt
    if abs(length_ratio - fps_ratio) < 0.02:
        return framerate_slope(fps_ref, fps_tgt)
    if 0.45 < fps_ratio < 0.55 or 1.95 < fps_ratio < 2.05:
        if any(abs(length_ratio - k) < 0.01 for k in known):
            return from_ratio            # TV rip with doubled frames
    return None


def _drop_local_outliers(points, limit_ms: float = 400.0, radius: int = 4,
                         max_gap_s: float = 240.0):
    """
    Drop measurements that disagree with their neighbours in time.

    A correlation that locks onto the wrong peak lands hundreds of milliseconds
    or seconds away, and one such point is enough to split the model in three:
    the jump detector sees a step and the fit sees a slope, so a flat +690 ms
    offset turns into twenty segments and a drift that does not exist.

    Comparing each point with the median of the points around it keeps the junk
    out of the model without touching a real edit, where a whole run of points
    moves at once.  Points further apart than max_gap_s are not neighbours.
    """
    if len(points) < 5:
        return points
    kept = []
    for i, point in enumerate(points):
        t = float(point["t"])
        near = [p for p in points[max(0, i - radius): i + radius + 1]
                if p is not point and abs(float(p["t"]) - t) <= max_gap_s]
        if len(near) < 2:
            kept.append(point)
            continue
        values = np.array([float(p["offset"]) for p in near], dtype=float)
        med = float(np.median(values))
        mad = float(np.median(np.abs(values - med)))
        # the band has to scale with the local scatter, or a drifting stretch
        # would look like a queue of outliers
        span = max(float(limit_ms), 8.0 * 1.4826 * mad)
        if abs(float(point["offset"]) - med) <= span:
            kept.append(point)
    return kept if len(kept) >= 3 else points


def _merge_same_line(segments, merge_ms: float):
    """
    Join neighbouring segments that describe the same straight line.

    Used twice: once after the groups are fitted, and once after the rescue
    pass, because splitting a segment can leave two pieces on the same line -
    a leftover piece two points wide produces a 0.6 ms "jump" and a pointless
    audio cut at the seam.
    """
    if len(segments) < 2:
        return segments
    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        boundary = seg["t_start"]
        prev_at = prev["offset"] + prev["slope"] * (boundary - prev["t_start"])
        if (abs(seg["offset"] - prev_at) < merge_ms
                and abs(seg["slope"] - prev["slope"]) < 0.05
                and not seg.get("weak") and not prev.get("weak")):
            prev["t_end"] = seg["t_end"]
            prev["n"] += seg["n"]
            prev["rms"] = round(max(prev["rms"], seg["rms"]), 2)
        else:
            merged.append(seg)
    return merged

def build_plan(points, duration: float, min_conf: float = 2.0,
               jump_ms: float = 150.0, outlier_ms: float = 120.0,
               merge_ms: float = 60.0, slope_hint: float = None,
               allow_drift: bool = True):
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

    # ---- 0. throw out measurements that disagree with their neighbours ----
    cleaned = _drop_local_outliers(usable)
    dropped = len(usable) - len(cleaned)
    if dropped:
        warnings.append(f"Dropped {dropped} measurement(s) that disagreed with "
                        "their neighbours.")
        usable = cleaned

    # ---- 0b. judge every point against the file's own trend --------------
    # A single deviation means nothing on its own: a burst of failed
    # correlations sits three seconds away while the film's real offset walks
    # a straight line.  So the trend is fitted first - Theil-Sen on a sample,
    # which is immune to a minority of junk - and each point is judged by its
    # distance from that line, not by its neighbours.
    #
    # A long run far from the trend is not a burst: a re-timed recap or a
    # second master really does sit somewhere else for twenty minutes, so runs
    # of FAR_RUN_POINTS consecutive points are kept whatever the band says.
    # The filter can therefore only ever remove short excursions.
    FAR_RUN_POINTS = 20                     # about ten minutes at the usual step
    trend_slope = None                      # the file's own drift, once fitted
    trend_mad = None                        # ... and how much its points scatter
    if len(usable) >= 8:
        times = np.array([float(p["t"]) for p in usable])
        offsets = np.array([float(p["offset"]) for p in usable])
        sample = np.linspace(0, len(times) - 1,
                             min(len(times), 400)).astype(int)
        sample_t, sample_o = times[sample], offsets[sample]
        left, right = np.triu_indices(len(sample_t), k=1)
        deltas = sample_t[right] - sample_t[left]
        usable_pairs = np.abs(deltas) > 1.0
        slope = float(np.median(
            (sample_o[right] - sample_o[left])[usable_pairs] / deltas[usable_pairs]))
        intercept = float(np.median(offsets - slope * times))
        trend_slope = slope
        residual = offsets - (slope * times + intercept)
        centre = float(np.median(residual))
        spread = float(np.median(np.abs(residual - centre)))
        trend_mad = spread
        band = max(350.0, 6.0 * 1.4826 * spread)
        far = [abs(float(value) - centre) > band for value in residual]
        if any(far) and not all(far):
            keep = [not flag for flag in far]
            index = 0
            while index < len(usable):
                if not far[index]:
                    index += 1
                    continue
                end = index
                while end < len(usable) and far[end]:
                    end += 1
                if end - index >= FAR_RUN_POINTS:
                    for position in range(index, end):
                        keep[position] = True
                index = end
            inside = [p for p, wanted in zip(usable, keep) if wanted]
            if 3 <= len(inside) < len(usable):
                warnings.append(
                    f"Dropped {len(usable) - len(inside)} measurement(s) that do "
                    f"not follow the file's trend ({slope:+.3f} ms/s).")
                usable = inside

    # ---- 1. the trend is the drift estimate -------------------------------
    # Theil-Sen already answered this above; the median of gap slopes is only
    # the fallback for a file too short to fit a line.  A real PAL drift reads
    # +41.7 ms/s, so only something beyond 100 ms/s (10 % tempo) is nonsense.
    #
    # allow_drift=False means both files report the same framerate: a tempo
    # difference has nowhere to come from, so the model is forced to levels and
    # only the offset (plus real edits) survives.  The Tempo checkbox turns
    # this back on for the rare case that ignores that rule.
    base_slope = trend_slope if allow_drift else 0.0
    if not allow_drift:
        warnings.append("Tempo not fitted - both files report the same framerate "
                        "(tick Tempo to look for a drift anyway).")
        if trend_slope is not None and abs(trend_slope) * float(duration) > 200.0:
            warnings.append(
                f"... but the points drift {trend_slope * float(duration):+.0f} ms "
                "over the film - tick Tempo if that is real.")
    if base_slope is None:
        gap_slopes = []
        for p, q in zip(usable, usable[1:]):
            dt = float(q["t"]) - float(p["t"])
            if dt > 0.5:
                gap_slopes.append((float(q["offset"]) - float(p["offset"])) / dt)
        base_slope = float(np.median(gap_slopes)) if gap_slopes else 0.0
    if abs(base_slope) > 100.0:
        warnings.append("Global slope is implausible for drift (>100 ms/s) – ignored.")
        base_slope = 0.0
    elif abs(base_slope) > 25.0:
        warnings.append(f"Very large drift ({base_slope:+.3f} ms/s) – "
                        "check the points if that surprises you.")

    # ---- 2. breakpoints: offset jumps the drift cannot explain -------
    # Two noisy measurements must not split the film.  Different dubs scatter
    # by 100-150 ms, which is the size of a real edit, so the comparison runs
    # on a five-point running median (noise shrinks, a real step stays a step)
    # and the threshold grows with the measured scatter of the file.
    if len(usable) >= 5:
        raw_offsets = np.array([float(p["offset"]) for p in usable])
        smooth = np.array([
            float(np.median(raw_offsets[max(0, i - 2): i + 3]))
            for i in range(len(raw_offsets))
        ])
    else:
        smooth = np.array([float(p["offset"]) for p in usable])
    jump_limit = max(jump_ms, 4.0 * (trend_mad or 0.0))
    breaks = []
    for i in range(1, len(usable)):
        dt = float(usable[i]["t"]) - float(usable[i - 1]["t"])
        delta = float(smooth[i]) - float(smooth[i - 1])
        if abs(delta - base_slope * dt) > jump_limit:
            breaks.append(i)

    # A level change can be smaller than the scatter of the whole film and still
    # be plain on the chart: the first hour sits at +105, the rest at -20.  The
    # test above misses it because jump_limit grows with that global scatter, so
    # every point is compared with the median of its own neighbourhood instead -
    # six measurements either side, and the step has to beat their own spread.
    if len(usable) >= 14:
        offsets = np.array([float(p["offset"]) for p in usable])
        candidates = []
        for i in range(10, len(usable) - 10):
            before = offsets[i - 10:i]
            after = offsets[i:i + 10]
            med_before = float(np.median(before))
            med_after = float(np.median(after))
            spread = max(float(np.median(np.abs(before - med_before))),
                         float(np.median(np.abs(after - med_after))))
            step = abs(med_after - med_before - base_slope * (
                float(usable[i]["t"]) - float(usable[i - 1]["t"])))
            # 125 ms of level change is within what a single delay still
            # handles, so the bar sits at 150 ms and four spreads
            if step >= max(150.0, 4.0 * spread):
                candidates.append((step, i))
        if candidates:
            # one break per cluster of neighbouring candidates, the strongest one
            candidates.sort(key=lambda item: item[1])
            best = candidates[0]
            for step, index in candidates[1:]:
                if index - best[1] <= 3:
                    if step > best[0]:
                        best = (step, index)
                else:
                    breaks.append(best[1])
                    best = (step, index)
            breaks.append(best[1])
            breaks = sorted(set(breaks))

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
        if folded and len(group) < 3:
            folded[-1].extend(group)
        else:
            folded.append(list(group))
    if len(folded) > 1 and len(folded[0]) < 3:
        folded[1] = folded[0] + folded[1]
        folded.pop(0)
    groups = folded

    # ---- 3. fit each group, then merge groups that describe the same line -
    fits = []
    for group in groups:
        slope, intercept, kept, rms = _fit_group(group, base_slope, outlier_ms,
                                                 allow_drift)
        if len(kept) < len(group):
            # the range, not the first point: the outliers are spread over the
            # whole group, and "near t=20s" made it look local
            warnings.append(
                f"Dropped {len(group) - len(kept)} outlier(s) in "
                f"{group[0]['t']:.0f}–{group[-1]['t']:.0f}s.")
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
                    combined, left["slope"], outlier_ms, allow_drift)
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

    # ---- 4b. a segment the measurements do not support is not a segment --
    # A burst of bad correlations survives the local outlier pass because its
    # neighbours are just as bad; the fit that comes out of it has a huge RMS
    # or an impossible slope.  Dropping those segments (and handing their time
    # range to the neighbour that does fit) turns a 25-segment mess built on
    # noise into the one flat line the film actually has.
    def plausible(seg) -> bool:
        if seg["n"] < 2 or seg["rms"] > 3.0 * outlier_ms:
            return False
        # a real PAL drift has a large slope but a small RMS, so an impossible
        # slope only condemns a segment whose points do not sit on it either
        if abs(seg["slope"]) > 8.0 and seg["rms"] > outlier_ms:
            return False
        return True

    if len(segments) > 1:
        cleaned = []
        dropped = 0
        for seg in segments:
            if plausible(seg):
                cleaned.append(seg)
            elif cleaned:
                cleaned[-1]["t_end"] = seg["t_end"]       # absorb forwards
                dropped += 1
            else:
                dropped += 1
        if cleaned:
            if cleaned[0]["t_start"] > 0.0:
                cleaned[0]["t_start"] = 0.0               # leading junk
            cleaned[-1]["t_end"] = float(duration)
        if dropped:
            warnings.append(
                f"Dropped {dropped} segment(s) the measurements do not support.")
            segments = cleaned


    # ---- 4c. say it out loud when almost nothing survived ---------------
    # Two points can be fitted perfectly and still mean nothing.  A high
    # count is not required (Min confidence 4 legitimately keeps a fifth of
    # the measurements), only enough of them to make the fit an average.
    fitted_points = sum(int(seg["n"]) for seg in segments)
    if fitted_points < 10:
        warnings.append(
            f"The model rests on {fitted_points} of {len(points)} measurements "
            "- lower Min confidence or switch Signal before trusting it.")

    # ---- 5. optional: lock the drift to a known framerate ratio -----------
    if slope_hint is not None:
        tolerance = max(0.5, abs(slope_hint) * 0.10)
        locked = 0
        for seg in segments:
            # a segment resting on a handful of points cannot justify its
            # own slope, so the known ratio wins over the fitted one
            weak = seg["n"] < 20
            if abs(seg["slope"] - slope_hint) <= tolerance or weak:
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
    segments = _merge_same_line(segments, merge_ms)

    # ---- 5c. tiny drifts are noise: keep the segment flat ----------------
    # The cut-off is the drift accumulated over the whole segment, not a rate:
    # 0.06 ms/s is invisible over a minute and 350 ms across a two-hour film.
    for seg in segments:
        if seg.get("locked"):
            continue
        span = max(1.0, float(seg["t_end"]) - float(seg["t_start"]))
        if abs(float(seg["slope"])) * span < FLAT_DRIFT_MS:
            seg["slope"] = 0.0
            seg["atempo"] = 1.0

    # ---- 5f. no segment may claim time it has no measurements for --------
    # A group can absorb the ones after it, so a segment's t_end can reach past
    # its last measurement: the chart then draws a line where nothing supports
    # it and the export applies a level nobody measured.  The tail gets its own
    # segment from the points that do exist there - including the ones below
    # Min confidence, which are still good enough to place a level.
    weak_pool = [p for p in points
                 if p.get("enabled", True) and float(p.get("conf", 0.0)) < min_conf]
    tails = []
    for seg in segments:
        own = [p for p in usable if seg["t_start"] <= p["t"] <= seg["t_end"]]
        if not own:
            continue
        last = max(float(p["t"]) for p in own)
        if seg["t_end"] - last <= 120.0:
            continue
        tail = [p for p in weak_pool if last < p["t"] <= seg["t_end"]]
        values = np.array([float(p["offset"]) for p in tail]) if tail else np.array([])
        level = float(np.median(values)) if len(values) else 0.0
        spread = float(np.median(np.abs(values - level))) if len(values) else 1e9
        at_model = float(seg["offset"]) + float(seg["slope"]) * (last - float(seg["t_start"]))
        # a difference smaller than the scatter of the segment is not a level
        noise = max(40.0, 2.0 * float(seg["rms"]))
        if (len(tail) < 8 or spread > 50.0
                or abs(level - at_model) < noise
                or float(tail[-1]["t"]) - float(tail[0]["t"]) < 120.0):
            # nothing good enough to place a level: leave the segment covering
            # the film - a truncated model would cut the exported audio
            continue
        start = last + 30.0
        seg["t_end"] = start
        tails.append({"t_start": start, "t_end": float(tail[-1]["t"]) + 30.0,
                      "offset": level, "slope": 0.0, "atempo": 1.0,
                      "n": len(tail), "rms": round(spread, 2), "weak": True})
    # The last segment can also cover a stretch where the weak measurements sit
    # on a level of their own: a dub that changes speed or splices in a reel at
    # the end leaves the strong points agreeing with the old line and the rest
    # of the film half a frame away.  If those weak points are tight and long
    # enough, they are a segment too - the model is meant to be complete.
    if segments and weak_pool:
        last_seg = segments[-1]
        span = max(1.0, float(last_seg["t_end"]) - float(last_seg["t_start"]))
        mid = float(last_seg["t_start"]) + 0.75 * span
        run = [p for p in weak_pool
               if mid <= p["t"] <= last_seg["t_end"]]
        if len(run) >= 8 and float(run[-1]["t"]) - float(run[0]["t"]) >= 120.0:
            values = np.array([float(p["offset"]) for p in run])
            level = float(np.median(values))
            spread = float(np.median(np.abs(values - level)))
            at_start = float(last_seg["offset"]) + float(last_seg["slope"]) * (
                float(run[0]["t"]) - float(last_seg["t_start"]))
            noise = max(25.0, 2.0 * float(last_seg["rms"]))
            if spread <= 50.0 and abs(level - at_start) >= noise:
                start = float(run[0]["t"])
                tail_seg = {"t_start": start,
                            "t_end": float(last_seg["t_end"]),
                            "offset": level, "slope": 0.0, "atempo": 1.0,
                            "n": len(run), "rms": round(spread, 2), "weak": True}
                last_seg["t_end"] = start
                segments.append(tail_seg)
                tails.append(tail_seg)
                warnings.append(
                    f"Split {1} unsupported tail(s) off using the measurements "
                    "below Min confidence.")

    if tails:
        segments = sorted(segments + [s for s in tails if s not in segments],
                          key=lambda s: float(s["t_start"]))

    # ---- 5d. rescue long runs of measurements the fit threw away ---------
    # A dub that is re-cut late in the film appears as a tight band of points a
    # second or two away from the model.  The segment fit calls them outliers,
    # so the model keeps drawing the old line to the end and the export drifts
    # for the rest of the film.  A run that is long, tight and far from every
    # segment is not noise: it is a segment of its own, and the segment that
    # covers it gets split around it.
    if segments and len(usable) >= 12:
        def _model_at(point):
            for seg in segments:
                if seg["t_start"] - 1.0 <= point["t"] <= seg["t_end"] + 1.0:
                    return seg["offset"] + seg["slope"] * (point["t"] - seg["t_start"])
            nearest = min(segments, key=lambda s: abs(point["t"] - s["t_start"]))
            return nearest["offset"]

        unexplained = [p for p in usable
                       if abs(p["offset"] - _model_at(p)) > outlier_ms]
        runs, current = [], []
        for point in unexplained:
            if current and point["t"] - current[-1]["t"] > 240.0:
                runs.append(current)
                current = []
            current.append(point)
        if current:
            runs.append(current)

        rescued = 0
        for run in runs:
            values = np.array([float(p["offset"]) for p in run])
            level = float(np.median(values))
            spread = float(np.median(np.abs(values - level)))
            start, end = float(run[0]["t"]), float(run[-1]["t"])
            if (len(run) < 12 or end - start < 240.0 or spread > 50.0
                    or len(run) < 0.15 * len(usable)):
                continue
            for index, seg in enumerate(segments):
                if not (seg["t_start"] + 1.0 <= start and end <= seg["t_end"] - 1.0):
                    continue
                middle = {"t_start": start, "t_end": end, "offset": level,
                          "slope": 0.0, "atempo": 1.0, "n": len(run),
                          "rms": round(spread, 2), "locked": True}
                # A piece that has no measurements of its own must not inherit
                # the parent line: that is how the model ended up drawing 13778
                # over the last fifteen minutes while the points sat at 16000.
                def _piece(from_t, to_t, is_middle):
                    own = [p for p in usable if from_t <= p["t"] <= to_t]
                    if not is_middle and len(own) < 2:
                        return None
                    if is_middle:
                        return dict(middle, t_start=from_t, t_end=to_t)
                    values = np.array([float(p["offset"]) for p in own])
                    piece_level = float(np.median(values))
                    return {"t_start": from_t, "t_end": to_t, "offset": piece_level,
                            "slope": float(seg["slope"]),
                            "atempo": 1.0 + float(seg["slope"]) / 1000.0,
                            "n": len(own),
                            "rms": round(float(np.median(np.abs(values - piece_level))), 2)}

                head = _piece(seg["t_start"], start, False)
                tail = _piece(end, seg["t_end"], False)
                middle["t_start"] = seg["t_start"] if head is None else start
                middle["t_end"] = seg["t_end"] if tail is None else end
                pieces = [piece for piece in (head, middle, tail) if piece is not None]
                segments[index:index + 1] = pieces
                rescued += 1
                break
        if rescued:
            warnings.append(
                f"Added {rescued} segment(s) from measurements the fit had dropped "
                "(a re-cut or a different dub later in the film).")

        # a split leaves the pieces to be rejoined when they landed on the same
        # line, otherwise the leftover produces a 0.6 ms jump and an audio cut
        segments = _merge_same_line(segments, merge_ms)

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
    used_points = sum(int(seg["n"]) for seg in segments)
    if len(segments) > 3 and used_points < 6 * len(segments):
        warnings.append(
            f"{len(segments)} segments for {used_points} points – the measurements "
            "are probably noisy; raise Min confidence or delete the outliers.")

    # ---- 7. does the target still look like the same cut? ----------------
    # Two edits in one film, or a drift of seconds across it, is not a badly
    # timed dub - it is a different cut, a re-timed master or a track that was
    # rebuilt for another release.  No offset model repairs that, so the UI says
    # so in large type instead of leaving it in the small print.
    fitted_drift = sum(
        abs(float(seg["slope"])) * max(1.0, float(seg["t_end"]) - float(seg["t_start"]))
        for seg in segments)
    trend_drift = abs(float(trend_slope or 0.0)) * float(duration)
    drift_ms = max(fitted_drift, trend_drift)
    worst_rms = max((float(seg["rms"]) for seg in segments), default=0.0)
    # A jump that needs seconds of padding means the target is missing content,
    # not that it is shifted: the export would put a hole in the film.  That is
    # worse than any offset, so it is reported as a mismatch on its own.
    gap_ms, gap_at = 0.0, 0.0
    for jump in jumps:
        size = abs(float(jump["delta"]))
        if jump.get("mode") == "pad" and size >= 500.0 and size > gap_ms:
            gap_ms, gap_at = size, float(jump["t"])
    if gap_ms >= 500.0:
        warnings.append(
            f"Jump at {format_timecode(gap_at)} needs {gap_ms / 1000.0:.1f} s of "
            "silence: the target is missing that content, not shifted.")
    # A wide RMS is what a different dub looks like: the envelope of the other
    # language scatters by design.  It belongs in the report, not in a claim
    # that the release is wrong - the cuts, the drift and the missing seconds
    # are the real evidence.
    if len(jumps) >= 2 or drift_ms >= 1500.0 or gap_ms >= 500.0:
        plan_mismatch = {"edits": len(jumps), "drift_ms": round(drift_ms, 1),
                         "rms": round(worst_rms, 1), "gap_ms": round(gap_ms, 1),
                         "gap_at": round(gap_at, 1)}
    else:
        plan_mismatch = None

    return {
        "segments": segments,
        "jumps": jumps,
        "initial_offset": segments[0]["offset"] if segments else 0.0,
        "warnings": warnings,
        "duration": float(duration),
        "mismatch": plan_mismatch,
    }


def mismatch_text(plan) -> str:
    """
    One loud sentence when the target is not the same cut, else "".

    Kept apart from build_plan so both UIs print the same words, and so the
    text can be shown in large type instead of the 9 pt model report.
    """
    info = (plan or {}).get("mismatch")
    if not info:
        return ""
    parts = []
    edits = int(info.get("edits") or 0)
    if edits:
        parts.append(f"{edits} cut{'s' if edits > 1 else ''}")
    drift = float(info.get("drift_ms") or 0.0)
    if abs(drift) >= 200.0:
        parts.append(f"{drift / 1000.0:+.1f} s of drift")
    gap = float(info.get("gap_ms") or 0.0)
    if gap >= 500.0:
        parts.append(f"{gap / 1000.0:.1f} s missing at "
                     f"{format_timecode(float(info.get('gap_at') or 0.0))}")
    if float(info.get("rms") or 0.0) > 80.0:
        parts.append(f"RMS {float(info['rms']):.0f} ms")
    detail = " (" + ", ".join(parts) + ")" if parts else ""
    return ("The target's audio differs a lot from the reference" + detail +
            ". A different release of the target is recommended.")




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


def mux_delay_ms(plan):
    """
    Delay a muxer needs to fix *plan* by hand, or None when nothing can.

    A constant delay only repairs a constant offset, so the plan has to be one
    straight line: one segment, no drift, no edit points.  The sign is flipped
    on purpose - the model stores an offset (positive = the target runs late),
    a muxer takes a delay (positive = push the audio later).
    """
    plan = plan or {}
    segments = plan.get("segments") or []
    if len(segments) != 1 or plan.get("jumps"):
        return None
    segment = segments[0]
    span = max(1.0, float(segment["t_end"]) - float(segment["t_start"]))
    # the same rule the model uses to flatten a segment: a drift that adds up
    # to less than FLAT_DRIFT_MS over the whole file is invisible, and a single
    # delay covers it.  Anything above that needs the exported file.
    if abs(float(segment["slope"])) * span >= FLAT_DRIFT_MS:
        return None
    return -float(plan.get("initial_offset", segment["offset"]))


def mux_hint(plan) -> str:
    """
    One line telling the user what to type into a muxer instead of exporting.

    Muxing the untouched audio is lossless and beats the exported .mka, so the
    number is offered whenever it is valid - and replaced by the reason it is
    not when the plan needs atempo or edits.
    """
    plan = plan or {}
    segments = plan.get("segments") or []
    if not segments:
        return ""

    delay = mux_delay_ms(plan)
    if delay is None:
        blockers = []
        if len(segments) > 1:
            blockers.append(f"{len(segments)} segments")
        jumps = plan.get("jumps") or []
        if jumps:
            blockers.append(f"{len(jumps)} edit point{'s' if len(jumps) > 1 else ''}")
        if not blockers:
            # one straight line, but it drifts too far for a constant delay:
            # say how far, instead of claiming there is no line at all
            segment = segments[0]
            span = max(1.0, float(segment["t_end"]) - float(segment["t_start"]))
            total = abs(float(segment["slope"])) * span
            blockers.append(f"drift {float(segment['slope']):+.3f} ms/s, "
                            f"{total:.0f} ms over the film")
        return ("Mux by hand: no single delay can fix this ("
                + ", ".join(blockers) + ") - export the corrected audio.")

    if abs(delay) < 0.5:
        return "Mux by hand: already in sync - delay 0 ms."
    action = "delay the audio" if delay > 0 else "advance the audio"
    return (f"Mux by hand: delay {delay:+.0f} ms ({action}) - type it into "
            "MKVToolNix 'Delay (in ms)' on the corrected track.")


def chart_ylim(points, minimum_span: float = 60.0):
    """
    Y range for the offset chart that a few wild measurements cannot stretch.

    The plot used to autoscale to whatever the worst correlation produced - one
    point at +/-20 s flattened the whole cloud and made a normal offset look
    like a straight line at zero.  The range comes from the 5th..95th
    percentile of the measured offsets instead, so the points that matter fill
    the chart and the junk is clipped away.
    """
    values = np.asarray([float(p["offset"]) for p in points], dtype=float)
    values = values[np.isfinite(values)]      # one NaN would poison the axis
    if values.size == 0:
        return None
    lo, hi = (float(v) for v in np.percentile(values, [5.0, 95.0]))
    # zero is the reference track: it has to stay on screen, otherwise a film
    # that is a second late looks like a flat line with no distance to anything
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = max(float(minimum_span), hi - lo)
    mid = 0.5 * (lo + hi)
    pad = max(20.0, span * 0.12)
    return mid - span / 2.0 - pad, mid + span / 2.0 + pad


def plan_report_lines(plan) -> list:
    """
    What a finished scan writes to the log: the head of the model, then the
    manual-mux advice.

    Shared verbatim by both builds so the two logs cannot drift apart - only
    the panel around them differs.
    """
    if not (plan or {}).get("segments"):
        return []
    lines = ["    " + line for line in plan_lines(plan)[:5]]
    hint = mux_hint(plan)
    if hint:
        lines.append("    " + hint)
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
            capture_output=True, text=True, timeout=30,
            startupinfo=get_startupinfo())
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


PREVIEW_SECONDS = 20.0


def preview_clip(ref_path: str, tgt_path: str, plan: dict, start_s: float,
                 span_s: float = PREVIEW_SECONDS, out_path: str = None):
    """
    Write a short mp4: video from the reference, audio from the target, corrected.

    The result is played by the system player, which is the only way to judge a
    mux by eye and ear without building a player into the app.  The correction
    comes from the same plan the export would use, so what you see is what you
    would get.
    """
    segments = (plan or {}).get("segments") or []
    if not segments:
        return None
    segment = None
    for candidate in segments:
        if float(candidate["t_start"]) - 1.0 <= start_s <= float(candidate["t_end"]) + 1.0:
            segment = candidate
            break
    if segment is None:
        segment = segments[0]
    offset = float(segment["offset"]) + float(segment["slope"]) * (
        start_s - float(segment["t_start"]))
    rate = float(segment.get("atempo") or 1.0)
    rate = min(2.0, max(0.5, rate))
    audio_start = max(0.0, start_s + offset / 1000.0)
    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(), "ads_preview.mp4")
    # a reference without a picture still makes a useful audio preview; asking
    # ffmpeg to map a video stream that is not there fails the whole render
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", ref_path],
            capture_output=True, text=True, timeout=30,
            startupinfo=get_startupinfo())
        has_video = bool(probe.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        has_video = True
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start_s:.3f}", "-t", f"{span_s:.3f}", "-i", ref_path,
        "-ss", f"{audio_start:.3f}", "-t", f"{span_s:.3f}", "-i", tgt_path,
    ]
    if has_video:
        command += ["-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    else:
        command += ["-map", "1:a:0", "-vn"]
    command += [
        "-af", f"atempo={rate:.8f},aresample=48000",
        "-c:a", "aac", "-b:a", "192k", "-shortest", out_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            startupinfo=get_startupinfo(), timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400] or "ffmpeg failed")
    return out_path

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

    try:
        probe_dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", target_path],
            capture_output=True, text=True, timeout=30,
            startupinfo=get_startupinfo())
        target_duration = float(probe_dur.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        target_duration = float(plan.get("duration", 0.0))

    parts, out_label, used, notes = plan_to_filter(plan, target_duration)
    graph = ";".join(parts)

    # unique per process: two exports at once must not write the same file
    tmp_wav = os.path.join(tempfile.gettempdir(),
                           f"ads_plan_export_{os.getpid()}_{time.time_ns()}.wav")
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
APP_VERSION = "3.1.13"
REPO_URL = "https://github.com/Jank8/Audio-Desync-Finder"
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


def build_icon(size: int = 256) -> QIcon:
    """
    Neon waveform icon drawn entirely in code - no image files.

    Deep navy square, faint grid, two smooth glowing waves (blue above, magenta
    below), two dashed guides and a correction arrow.
    """
    import math

    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import (QBrush, QLinearGradient, QPainter, QPainterPath,
                               QPen, QPixmap)

    icon = QIcon()
    for px in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        u = px / 256.0

        grad = QLinearGradient(0, 0, 0, px)
        grad.setColorAt(0.0, QColor("#0a1330"))
        grad.setColorAt(1.0, QColor("#141f4d"))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(1, 1, px - 2, px - 2), 56 * u, 56 * u)

        pen = QPen(QColor(255, 255, 255, 20))
        pen.setWidthF(max(0.6, px * 0.004))
        p.setPen(pen)
        for i in range(1, 6):
            p.drawLine(QPointF(px * i / 6.0, px * 0.10),
                       QPointF(px * i / 6.0, px * 0.90))
        for i in range(1, 5):
            p.drawLine(QPointF(px * 0.05, px * i / 5.0),
                       QPointF(px * 0.95, px * i / 5.0))

        def draw_wave(centre_y, amplitude, colour):
            path = QPainterPath()
            first = True
            steps = max(24, int(px * 0.90))
            for k in range(steps + 1):
                x = px * 0.05 + (px * 0.90) * k / steps
                t = x / px
                env = math.exp(-((t - 0.50) / 0.30) ** 2)
                y = centre_y - amplitude * env * (
                    math.sin(t * 17.0 + 0.4) + 0.35 * math.sin(t * 33.0 + 2.0))
                if first:
                    path.moveTo(x, y)
                    first = False
                else:
                    path.lineTo(x, y)
            p.setBrush(Qt.NoBrush)
            for width, alpha in ((0.100, 26), (0.065, 56), (0.036, 130),
                                 (0.020, 255)):
                glow = QColor(colour)
                glow.setAlpha(alpha)
                pen = QPen(glow)
                pen.setWidthF(max(0.8, px * width))
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                p.drawPath(path)

        draw_wave(px * 0.345, px * 0.125, "#2fa8ff")
        draw_wave(px * 0.630, px * 0.120, "#c14dff")

        for x, colour in ((px * 0.455, "#e6e9f2"), (px * 0.578, "#b45cff")):
            pen = QPen(QColor(colour))
            pen.setWidthF(max(1.0, px * 0.013))
            pen.setDashPattern([1.8, 1.8])
            p.setPen(pen)
            p.drawLine(QPointF(x, px * 0.12), QPointF(x, px * 0.80))

        y = px * 0.805
        x0, x1 = px * 0.455, px * 0.578
        head = px * 0.040
        p.setPen(Qt.NoPen)
        left = QPainterPath()
        left.moveTo(x0, y)
        left.lineTo(x0 + head, y - head)
        left.lineTo(x0 + head, y + head)
        left.closeSubpath()
        p.fillPath(left, QColor("#39d7ff"))
        right = QPainterPath()
        right.moveTo(x1, y)
        right.lineTo(x1 - head, y - head)
        right.lineTo(x1 - head, y + head)
        right.closeSubpath()
        p.fillPath(right, QColor("#d05cff"))
        pen = QPen(QColor("#59c8ff"))
        pen.setWidthF(max(1.0, px * 0.017))
        p.setPen(pen)
        p.drawLine(QPointF(x0 + head * 0.9, y), QPointF(px * 0.516, y))
        pen.setColor(QColor("#c46bff"))
        p.setPen(pen)
        p.drawLine(QPointF(px * 0.516, y), QPointF(x1 - head * 0.9, y))

        p.end()
        icon.addPixmap(pm)
    return icon

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


class DropLineEdit(LineEdit):
    """Line edit that accepts a media file dropped onto it."""

    def __init__(self, on_drop, parent=None):
        super().__init__(parent)
        self._on_drop = on_drop
        self.setAcceptDrops(True)

    def set_drop_hint(self, on: bool) -> None:
        self.setStyleSheet("LineEdit { border: 1px solid #4cc2ff; }" if on else "")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.set_drop_hint(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.set_drop_hint(False)
        event.accept()

    def dropEvent(self, event):
        self.set_drop_hint(False)
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self._on_drop(url.toLocalFile())
                break
        event.acceptProposedAction()


class Card(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(8)


class JumpSlider(QSlider):
    """A slider that jumps to the clicked position instead of paging."""

    clickedAt = Signal(int)

    def mousePressEvent(self, event):          # noqa: N802 - Qt naming
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        try:
            x = int(event.position().x())
            width = max(1, int(self.width()))
            value = int(self.style().sliderValueFromPosition(
                self.minimum(), self.maximum(), x, width))
        except Exception:
            super().mousePressEvent(event)
            return
        self.setValue(value)
        self.clickedAt.emit(value)


class PreviewDialog(QDialog):
    """
    Reference picture with the target sound, played live from both files.

    Two players run at once: the reference with the picture (muted) and the
    target as sound only, seeked to where the plan says its audio is.  A timer
    nudges the sound back into place every few hundred milliseconds, and the
    plan's atempo becomes the sound player's playback rate, so nothing has to be
    rendered first.  Rendering stays as a fallback for the files Qt cannot
    decode, and gives the exact bytes the export would write.
    """

    SPAN = 30.0

    def __init__(self, parent, ref_path: str, tgt_path: str, plan_getter,
                 start_s: float):
        super().__init__(parent)
        self.setWindowTitle("Preview - reference picture, target sound")
        self.resize(980, 620)
        self.ref_path = ref_path
        self.tgt_path = tgt_path
        self.plan_getter = plan_getter
        self.start = max(0.0, float(start_s))
        self._live = _HAVE_PLAYER
        self._offset0 = 0.0
        self._slope = 0.0
        self._t0 = 0.0
        self._atempo = 1.0

        layout = QVBoxLayout(self)
        self.video = QVideoWidget(self)
        self.video.setMinimumHeight(420)
        layout.addWidget(self.video, 1)

        self.ref_player = QMediaPlayer(self)
        self.ref_audio = QAudioOutput(self)
        self.ref_audio.setVolume(0.0)          # the reference only gives picture
        self.ref_player.setAudioOutput(self.ref_audio)
        self.ref_player.setVideoOutput(self.video)

        self.tgt_player = QMediaPlayer(self)
        self.tgt_audio = QAudioOutput(self)
        self.tgt_audio.setVolume(1.0)
        self.tgt_player.setAudioOutput(self.tgt_audio)

        self.slider = JumpSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 1000)
        layout.addWidget(self.slider)

        # a time you can type: 2:34 or 1:07:15 or 95
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Go to"))
        self.time_edit = QLineEdit()
        self.time_edit.setPlaceholderText("1:07:15")
        self.time_edit.setFixedWidth(100)
        self.time_edit.returnPressed.connect(self._jump_to_text)
        time_row.addWidget(self.time_edit)
        self.btn_go = QPushButton("Go")
        self.btn_go.clicked.connect(self._jump_to_text)
        time_row.addWidget(self.btn_go)
        time_row.addStretch(1)
        layout.addLayout(time_row)

        row = QHBoxLayout()
        self.btn_back = QPushButton("⏪ 10 min")
        self.btn_play = QPushButton("⏸ Pause")
        self.btn_fwd = QPushButton("10 min ⏩")
        self.btn_close = QPushButton("Close")
        for button in (self.btn_back, self.btn_play, self.btn_fwd):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)
        self.status = QLabel("", self)
        layout.addWidget(self.status)

        self.btn_back.clicked.connect(lambda: self._seek_to(self.start - 600.0))
        self.btn_fwd.clicked.connect(lambda: self._seek_to(self.start + 600.0))
        self.btn_close.clicked.connect(self.close)
        self.btn_play.clicked.connect(self._toggle)
        self.slider.sliderMoved.connect(self._scrub)
        # a click is not a drag: it seeks and plays from there
        self.slider.clickedAt.connect(
            lambda value: self._seek_to(self._total_duration() * value / 1000.0))
        self.slider.sliderPressed.connect(self._scrub_start)
        self.slider.sliderReleased.connect(self._scrub_end)
        self.ref_player.positionChanged.connect(self._on_position)

        self._sync = QTimer(self)
        self._sync.timeout.connect(self._keep_sync)

        if self._live:
            self.ref_player.setSource(QUrl.fromLocalFile(ref_path))
            self.tgt_player.setSource(QUrl.fromLocalFile(tgt_path))
            self.ref_player.errorOccurred.connect(self._on_error)
            self.tgt_player.errorOccurred.connect(self._on_error)
            self._seek_to(self.start)
        else:
            self.status.setText(
                "QtMultimedia is missing - this build cannot preview.")

    # ---- the plan decides where the sound has to start and how fast --------
    def _segment_at(self, t):
        plan = self.plan_getter() or {}
        segments = plan.get("segments") or []
        if not segments:
            return None
        for segment in segments:
            if segment["t_start"] - 1.0 <= t <= segment["t_end"] + 1.0:
                return segment
        return segments[0]

    def _apply_plan_at(self, t):
        segment = self._segment_at(t)
        if segment is None:
            self._offset0, self._slope, self._t0, self._atempo = 0.0, 0.0, t, 1.0
            return
        self._offset0 = float(segment["offset"]) + float(segment["slope"]) * (
            t - float(segment["t_start"]))
        self._slope = float(segment["slope"])
        self._t0 = t
        self._atempo = min(2.0, max(0.5, float(segment.get("atempo") or 1.0)))

    def _offset_at(self, t):
        return self._offset0 + self._slope * (t - self._t0)

    # ---- live playback -----------------------------------------------------
    def _seek_to(self, t):
        """
        Jump somewhere and play from there.

        The players are paused before the position changes and resumed after:
        seeking a playing stream with two players at once is what makes the
        picture stall, while a seek from pause is cheap.  The playback rate is
        only touched when it actually changes, which is another known hiccup.
        """
        self.start = max(0.0, min(float(t), self._total_duration()))
        if self._live:
            self._sync.stop()
            self.ref_player.pause()
            self.tgt_player.pause()
            self._apply_plan_at(self.start)
            self.ref_player.setPosition(int(self.start * 1000))
            audio_at = max(0.0, self.start + self._offset_at(self.start) / 1000.0)
            self.tgt_player.setPosition(int(audio_at * 1000))
            if abs(self._atempo - getattr(self, "_rate_applied", 0.0)) > 1e-9:
                try:
                    self.tgt_player.setPlaybackRate(self._atempo)
                    self._rate_applied = self._atempo
                except Exception:
                    pass
            self.ref_player.play()
            self.tgt_player.play()
            self._sync.start(400)
            self.status.setText(
                f"{format_timecode(self.start)} / {format_timecode(self._total_duration())}"
                f"  ·  offset {self._offset_at(self.start):+.0f} ms"
                f"  ·  atempo {self._atempo:.6f}")
        else:
            self.status.setText(
                "QtMultimedia is missing - this build cannot preview.")

    def _keep_sync(self):
        if getattr(self, "_scrubbing", False):
            return
        if not self._live or self.ref_player.playbackState() != QMediaPlayer.PlayingState:
            return
        t = self.ref_player.position() / 1000.0
        expected = (t + self._offset_at(t) / 1000.0) * 1000.0
        if abs(self.tgt_player.position() - expected) > 150.0:
            self.tgt_player.setPosition(int(max(0.0, expected)))

    def _on_position(self, position):
        if self.slider.isSliderDown() or getattr(self, "_scrubbing", False):
            return
        now = position / 1000.0
        duration = self._total_duration()
        self.slider.setValue(int(max(0.0, min(1.0, now / duration)) * 1000))
        self.status.setText(
            f"{format_timecode(now)} / {format_timecode(duration)}  ·  "
            f"offset {self._offset_at(now):+.0f} ms  ·  "
            f"atempo {self._atempo:.6f}")

    def _total_duration(self):
        position_based = self.ref_player.duration() / 1000.0
        if position_based > 1.0:
            return position_based
        plan = self.plan_getter() or {}
        return max(1.0, float(plan.get("duration") or 1.0))

    def _jump_to_text(self):
        target = parse_timecode(self.time_edit.text())
        if target is None:
            self.status.setText("Type a time like 2:34 or 1:07:15.")
            return
        duration = self._total_duration()
        self._seek_to(min(max(0.0, target), duration))

    def _scrub_start(self):
        """Take the players off the clock while the handle is being dragged."""
        self._scrubbing = True
        self._was_playing = (self.ref_player.playbackState()
                             == QMediaPlayer.PlayingState)
        self._sync.stop()
        self.ref_player.pause()
        self.tgt_player.pause()

    def _scrub(self, value):
        """Moving the handle only moves the position - no play, no re-sync."""
        self._seek_only(self._total_duration() * value / 1000.0)

    def _scrub_end(self):
        self._scrubbing = False
        if getattr(self, "_was_playing", False):
            self.ref_player.play()
            self.tgt_player.play()
            self._sync.start(400)

    def _seek_only(self, t):
        self.start = max(0.0, min(float(t), self._total_duration()))
        self._apply_plan_at(self.start)
        self.ref_player.setPosition(int(self.start * 1000))
        audio_at = max(0.0, self.start + self._offset_at(self.start) / 1000.0)
        self.tgt_player.setPosition(int(audio_at * 1000))
        self.status.setText(
            f"{format_timecode(self.start)} / "
            f"{format_timecode(self._total_duration())}  ·  "
            f"offset {self._offset_at(self.start):+.0f} ms")

    def _toggle(self):
        playing = self.ref_player.playbackState() == QMediaPlayer.PlayingState
        if playing:
            self.ref_player.pause()
            self.tgt_player.pause()
            self._sync.stop()
            self.btn_play.setText("▶ Play")
        else:
            self.ref_player.play()
            self.tgt_player.play()
            self._sync.start(400)
            self.btn_play.setText("⏸ Pause")

    def _shutdown(self):
        """Stop both players and detach the sources before the window goes."""
        try:
            self._sync.stop()
        except Exception:
            pass
        for player in (self.ref_player, self.tgt_player):
            try:
                player.stop()
                player.setSource(QUrl())
            except Exception:
                pass

    def closeEvent(self, event):
        self._shutdown()
        super().closeEvent(event)

    def reject(self):
        self._shutdown()
        super().reject()

    def done(self, result):
        self._shutdown()
        super().done(result)

    def _on_error(self, *_args):
        self._live = False
        self._sync.stop()
        self.status.setText(
            "Qt cannot play this file - preview needs the FFmpeg media backend.")

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(build_icon(256))
        try:
            self.titleBar.hBoxLayout.setSpacing(6)
            self.titleBar.titleLabel.setContentsMargins(0, 0, 0, 0)
            self.titleBar.iconLabel.setContentsMargins(0, 0, 0, 0)
        except Exception:
            pass
        self.resize(1500, 860)
        self.setMicaEffectEnabled(True)
        self.setAcceptDrops(True)      # a drop anywhere fills the first empty field
        self.setMinimumSize(1000, 640)

        self.ref_path = ""
        self.tgt_path = ""
        self.plan = None
        self.points = []
        self.meta = None
        self.decoded = {}          # last decoded pair, reused when only settings change
        self.worker = None
        self.accent = system_accent_hex()
        self.chart_bg = "#ffffff" if LIGHT else "#202020"
        self.chart_fg = "#39404a" if LIGHT else "#c9cdd2"
        self.chart_grid = "#00000022" if LIGHT else "#ffffff22"
        self.chart_spine = "#c9ccd1" if LIGHT else "#3a3f45"
        self.log_fg = "#39404a" if LIGHT else "#9aa4ae"
        try:
            version = QLabel(
                f'<a href="{REPO_URL}" style="color:{self.accent};'
                f'text-decoration:none;">{APP_VERSION}</a>')
            version.setOpenExternalLinks(True)
            version.setToolTip("Open the repository")
            version.setCursor(Qt.PointingHandCursor)
            version.setStyleSheet("font: 600 9pt 'Segoe UI';")
            version.setContentsMargins(0, 0, 12, 0)
            bar = self.titleBar.hBoxLayout
            bar.insertWidget(max(0, bar.count() - 1), version)
        except Exception:
            pass
        self.settings = QSettings("AudioDesync", "AudioDesyncFinder")
        self.last_dir = str(self.settings.value("last_dir", "") or "")
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
        self.entry_window.setText("80")
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
        self.entry_conf.setValidator(QDoubleValidator(0.5, 60.0, 2, self))
        sl.addWidget(self.entry_conf)
        sl.addWidget(StrongBodyLabel("Cores"))
        import os as _os
        cores = max(1, (_os.cpu_count() or 4) - 2)
        self.entry_cores = LineEdit()
        self.entry_cores.setText(str(cores))
        self.entry_cores.setFixedWidth(80)
        self.entry_cores.setAlignment(Qt.AlignCenter)
        self.entry_cores.setValidator(QIntValidator(1, max(1, _os.cpu_count() or 1), self))
        self.entry_cores.setToolTip(
            "Decoding slices and measurement threads; the default is every core "
            "but two")
        sl.addWidget(self.entry_cores)
        sl.addWidget(StrongBodyLabel("Signal"))
        self.combo_signal = ComboBox()
        self.combo_signal.addItems(["Auto", "Waveform", "Envelope"])
        sl.addWidget(self.combo_signal)
        self.chk_tempo = CheckBox("Tempo")
        self.chk_tempo.setToolTip(
            "Fit a drift even when both files report the same framerate")
        sl.addWidget(self.chk_tempo)
        sl.addStretch(1)
        self.btn_scan = PrimaryPushButton("⚡  Auto-scan whole file")
        self.btn_scan.clicked.connect(self.start_scan)
        sl.addWidget(self.btn_scan)
        self.btn_stop = PushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)
        sl.addWidget(self.btn_stop)
        self.btn_clear_cache = PushButton("Clear cache")
        self.btn_clear_cache.setToolTip(
            "Delete the decoded audio kept on disk between sessions")
        self.btn_clear_cache.clicked.connect(self.clear_cache)
        sl.addWidget(self.btn_clear_cache)
        outer.addWidget(settings)

        # ---------------- chart -----------------------------------------
        chart = Card()
        cl = QVBoxLayout(chart)
        cl.setContentsMargins(10, 10, 10, 10)
        self.fig = Figure(figsize=(6, 3.2), facecolor=self.chart_bg)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.mpl_connect("scroll_event", self._zoom_at)
        self.canvas.mpl_connect("button_press_event", self._pan_start)
        self.canvas.mpl_connect("motion_notify_event", self._pan_move)
        self.canvas.mpl_connect("button_release_event", self._pan_end)
        self.canvas.setStyleSheet("background:transparent;")
        row = QHBoxLayout()
        row.addWidget(self.canvas, 3)
        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Offset (ms)", "Conf", "Source"])
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(6)
        row.addWidget(self.table, 2)
        cl.addLayout(row)
        # the legend lives outside the canvas, in the strip under the plot, so
        # the chart itself is only data
        legend_row = QHBoxLayout()
        legend_row.setSpacing(16)
        legend_row.setContentsMargins(6, 0, 0, 0)
        for colour, text in (("#4a9eff", "target (measured)"),
                             ("#3ddc97", "fitted model"),
                             ("#c8c8c8", "reference (0 ms)"),
                             ("#ff5a5f", "on the model")):
            dot = BodyLabel("●")
            dot.setStyleSheet(f"BodyLabel{{color:{colour};font-size:11px;}}")
            legend_row.addWidget(dot)
            caption = BodyLabel(text)
            caption.setStyleSheet(
                f"BodyLabel{{color:{self.chart_fg};font-size:8pt;}}")
            legend_row.addWidget(caption)
        legend_row.addStretch(1)
        cl.addLayout(legend_row)
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
        # loud, because "use another release" is not advice that belongs in 9 pt
        self.mismatch_label = BodyLabel("")
        self.mismatch_label.setWordWrap(True)
        self.mismatch_label.setStyleSheet(
            f"BodyLabel{{color:{system_accent_hex()};font-size:15px;"
            "font-weight:600;padding:6px 0px;}")
        self.mismatch_label.hide()
        ml.addWidget(self.mismatch_label)
        self.summary = StrongBodyLabel("")
        ml.addWidget(self.summary)
        self.mux_label = BodyLabel("")
        self.mux_label.setWordWrap(True)
        self.mux_label.setStyleSheet(
            f"BodyLabel{{color:{system_accent_hex()};font-size:13px;"
            "font-weight:600;padding:2px 0px 4px 0px;}")
        ml.addWidget(self.mux_label)
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
        self.progress.setRange(0, 1000)      # 0.1 % steps, so the bar creeps
        self.progress.setValue(0)
        footer.addWidget(self.progress, 1)
        self.status = BodyLabel("Ready")
        footer.addWidget(self.status)
        self.btn_preview = PushButton("▶  Preview")
        self.btn_preview.setToolTip(
            "Play the reference picture with the target sound, corrected")
        self.btn_preview.setEnabled(_HAVE_PLAYER)
        self.btn_preview.clicked.connect(self.preview)
        footer.addWidget(self.btn_preview)
        self.btn_export = PrimaryPushButton("⬇  Export corrected audio…")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export_audio)
        footer.addWidget(self.btn_export)
        outer.addLayout(footer)

        self._draw()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain)
        self.timer.start(33)

    # ------------------------------------------------------------------ UI
    def _file_row(self, layout, label, which, checked=False):
        row = QHBoxLayout()
        row.addWidget(StrongBodyLabel(label))
        edit = DropLineEdit(lambda p, w=which: self.set_path(w, p))
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
            self, "Select a media file", self.last_dir,
            "Media (*.mp4 *.mkv *.avi *.mov *.mp3 *.wav *.flac *.m4a *.aac);;All files (*)")
        if not path:
            return
        self._remember_dir(path)
        self.set_path(which, path)

    def set_path(self, which, path):
        """Assign a file to File 1 or File 2 - used by Browse and by drop."""
        path = str(path)
        if which == "ref":
            self.ref_path = path
            self.edit_ref.setText(os.path.basename(path))
        else:
            self.tgt_path = path
            self.edit_tgt.setText(os.path.basename(path))
        if self.ref_path and self.tgt_path:
            self._load_pair()

    def _remember_dir(self, path):
        """Keep the folder of the last chosen file for the next dialog."""
        folder = os.path.dirname(path)
        if folder and folder != self.last_dir:
            self.last_dir = folder
            self.settings.setValue("last_dir", folder)

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
        hint = speed_hint(ref, tgt)
        if hint is not None:
            info += f"      drift {hint:+.1f} ms/s      atempo {1.0 + hint / 1000.0:.6f}"
        self.fps_label.setText(info)
        self.status.setText("Ready")

    def _ordered_paths(self):
        base_is_ref = getattr(self, "radio_ref", None) is None or self.radio_ref.isChecked()
        return (self.ref_path, self.tgt_path) if base_is_ref else (self.tgt_path, self.ref_path)

    def _fps_hint(self):
        ref, tgt = self._ordered_paths()
        if not (ref and tgt):
            return None
        return speed_hint(ref, tgt)

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

    def clear_cache(self):
        """Throw away the decoded audio kept between sessions."""
        freed, removed = clear_decoded_cache()
        self.decoded = {}          # the copy in RAM would hide the clear
        if removed:
            self.log(f"Cache cleared: {removed} file(s), {freed / 1e6:.0f} MB freed.")
        else:
            self.log("Cache is already empty.")

    def showEvent(self, event):
        """Centre the window once, after the frame size is known."""
        super().showEvent(event)
        if not getattr(self, "_centred", False):
            self._centred = True
            QTimer.singleShot(0, self._centre_on_screen)

    def _centre_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())

    def closeEvent(self, event):
        """
        Take the workers and FFmpeg down with the window.

        Without this the child processes outlive the app: closing during a
        decode left ten FFmpeg instances reading a 60 GB file in the
        background, with no window left to stop them.
        """
        self.stop_event.set()
        try:
            self.timer.stop()
        except Exception:
            pass
        for proc in list(_ffmpeg_procs):
            try:
                proc.kill()
            except Exception:
                pass
        super().closeEvent(event)

    def _scan_task(self):
        try:
            ref_path, tgt_path = self._ordered_paths()
            self.ui_queue.put(("log", "── Scan started ─────────────"))
            scan_t0 = time.time()
            # the settings first: an identical earlier scan can be replayed
            # without decoding anything
            mode = ("auto" if self.combo_signal.currentIndex() == 0
                    else "waveform" if self.combo_signal.currentIndex() == 1
                    else "envelope")
            step = self._num(self.entry_step, 30)
            window_size = self._num(self.entry_window, 80)
            hint = self._fps_hint()
            # a tempo difference needs a framerate difference to exist at all;
            # the checkbox covers the rare file that ignores that rule
            fps_ref, fps_tgt = probe_framerate(ref_path), probe_framerate(tgt_path)
            allow_drift = (self.chk_tempo.isChecked()
                           or framerate_differs(fps_ref, fps_tgt))
            self.allow_drift = allow_drift
            result_key_value = result_key(ref_path, tgt_path, window_size, step,
                                          self._num(self.entry_conf, 2), mode,
                                          allow_drift, hint)
            stored = load_result(result_key_value)
            if stored is not None and not self.stop_event.is_set():
                points, plan = stored
                self.ui_queue.put(("log", "Loaded the stored result - same pair, "
                                          "same settings."))
                self.ui_queue.put(("points", points))
                self.ui_queue.put(("plan", plan))
                self.ui_queue.put(("progress", (100, "Measuring done")))
                return
            pair_key = (track_key(ref_path), track_key(tgt_path))
            if self.decoded.get("key") == pair_key:
                ref, tgt, meta = (self.decoded["ref"], self.decoded["tgt"],
                                  self.decoded["meta"])
                self.ui_queue.put(("log", "Reusing the audio decoded for the "
                                          "previous scan - only the settings changed."))
                self.ui_queue.put(("progress", (68, "Using the cached audio…")))
            else:
                self.decoded = {}          # free the old pair before the new one lands
                ref, tgt, meta = decode_pair(
                    ref_path, tgt_path,
                    progress=lambda m: self.ui_queue.put(("log", m)),
                    on_fraction=lambda phase, f: self.ui_queue.put(
                        ("progress", (5 + (30 if phase == "ref" else 32) * f,
                                      f"Decoding {phase}… {f * 100:.3f}% · "
                                      f"{time.time() - scan_t0:.0f} s"))),
                    should_stop=self.stop_event.is_set,
                    workers=int(self._num(self.entry_cores, _decode_workers())))
                # decode_pair reads and writes the per-file cache itself, so a
                # file already decoded for any earlier pair is never read again
                self.decoded = {"key": pair_key, "ref": ref, "tgt": tgt,
                                "meta": meta}
            if self.stop_event.is_set():
                self.ui_queue.put(("log", "Stopped by the user."))
                return
            self.meta = meta

            rate = 1.0 + hint / 1000.0 if hint and abs(hint) >= 0.3 else 1.0
            if rate != 1.0:
                self.ui_queue.put(("progress", (
                    68, f"Framerate hint {hint:+.3f} ms/s - pre-warping the target…")))
                scan_tgt = resample_track(tgt, rate)
            else:
                self.ui_queue.put(("progress", (
                    68, f"Measuring every {step:.0f}s ({mode}) on "
                        f"{self._num(self.entry_cores, _decode_workers()):.0f} "
                        "threads…")))
                scan_tgt = tgt
            coarse = 0.0
            try:
                coarse = coarse_offset(ref, scan_tgt)
            except Exception:
                coarse = 0.0
            # A whole-track offset has to show up in the durations: a file that
            # starts 66 s late is also 66 s shorter.  Without this test a dub of
            # the very same cut was shifted by a minute and a half before the
            # scan, and every measurement afterwards agreed with the shift.
            duration_gap = abs(meta["ref_duration"] - meta["target_duration"])
            if abs(coarse) > 30000 and abs(coarse) > duration_gap + 20.0:
                self.ui_queue.put(("log", (
                    f"Coarse alignment {coarse:+.0f} ms ignored - the files differ "
                    f"by {duration_gap:.0f} s, so that is not a whole-track offset")))
                coarse = 0.0
            if abs(coarse) > 30000:
                self.ui_queue.put(("log", f"Coarse alignment {coarse:+.0f} ms - "
                                          f"shifting the target before the scan"))
                scan_tgt = shift_track_by_ms(scan_tgt, coarse)
            else:
                coarse = 0.0
            points = scan_arrays(
                ref, scan_tgt, window=self._num(self.entry_window, 80),
                step=step, mode=mode,
                workers=int(self._num(self.entry_cores, _decode_workers())),
                progress=lambda done, total: self.ui_queue.put(
                    ("progress", (72 + 27 * done / max(total, 1),
                                  f"Measuring {done}/{total}"))),
                should_stop=self.stop_event.is_set)
            points = sorted(compensate_points(points, rate), key=lambda p: p["t"])
            if self.stop_event.is_set():
                self.ui_queue.put(("log", "Stopped by the user."))
                return
            if coarse:
                for point in points:
                    point["offset"] = round(point["offset"] + coarse, 2)
            plan = build_plan(points, meta["ref_duration"],
                                     min_conf=self._num(self.entry_conf, 2),
                                     slope_hint=hint, allow_drift=allow_drift)
            if plan.get("jumps") and not self.stop_event.is_set():
                # the 40 s window blurs where an edit is; 6 s windows pin the
                # cut down to a couple of seconds, and the cut is where the
                # exported audio is spliced
                self.ui_queue.put(("progress", (
                    71, f"Refining {len(plan['jumps'])} jump position(s)…")))
                extra_points = refine_breakpoints(
                    ref, scan_tgt, plan, meta["ref_duration"], mode=mode,
                    should_stop=self.stop_event.is_set,
                    workers=int(self._num(self.entry_cores, _decode_workers())))
                if extra_points and coarse:
                    for point in extra_points:
                        point["offset"] = round(point["offset"] + coarse, 2)
                if extra_points:
                    extra_points = compensate_points(extra_points, rate)
                    points = sorted(points + extra_points, key=lambda p: p["t"])
                    self.ui_queue.put(("points", points))
                    plan = build_plan(points, meta["ref_duration"],
                                      min_conf=self._num(self.entry_conf, 2),
                                      slope_hint=hint, allow_drift=allow_drift)
            self.ui_queue.put(("points", points))
            self.ui_queue.put(("plan", plan))
            for line in plan_report_lines(plan):
                self.ui_queue.put(("log", line))
            if not self.stop_event.is_set():
                save_result(result_key_value, points, plan)
            self.ui_queue.put(("progress", (100, "Measuring done")))
        except Exception as exc:
            self.ui_queue.put(("log", f"✗ {exc}"))
        finally:
            self.ui_queue.put(("idle", None))

    def _preview_plan(self):
        """
        The plan the preview plays, or a flat zero plan before a scan.

        Previewing before measuring is the point: listen to the two files with no
        correction at all and decide whether they are worth scanning.
        """
        if (self.plan or {}).get("segments"):
            return self.plan
        duration = float((self.meta or {}).get("ref_duration") or 0.0)
        return {"segments": [{"t_start": 0.0, "t_end": max(duration, 1.0),
                              "offset": 0.0, "slope": 0.0, "atempo": 1.0,
                              "n": 0, "rms": 0.0}],
                "jumps": [], "initial_offset": 0.0, "duration": duration,
                "warnings": []}

    def preview(self):
        """Open the player at the middle of what the chart currently shows."""
        if not _HAVE_PLAYER:
            show_message("Preview", "This build has no QtMultimedia player.")
            return
        if not (self.ref_path and self.tgt_path):
            show_message("Preview", "Pick both files first.")
            return
        ref_path, tgt_path = self._ordered_paths()
        x0, x1 = self.ax.get_xlim()
        centre = max(0.0, 0.5 * (float(x0) + float(x1)))
        dialog = PreviewDialog(self, ref_path, tgt_path,
                               self._preview_plan, centre)
        dialog.exec()

    def export_audio(self):
        if not self.plan or not self.plan.get("segments"):
            return
        start = os.path.join(self.last_dir, "corrected.mka") if self.last_dir else "corrected.mka"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save corrected audio", start, "Matroska Audio (*.mka)")
        if not path:
            return
        self._remember_dir(path)
        path = mka_path(path)
        self.stop_event.clear()          # a previous Stop must not cancel this
        self.btn_export.setEnabled(False)
        self.btn_stop.setEnabled(True)
        threading.Thread(target=self._export_task, args=(path,), daemon=True).start()

    def _export_task(self, path):
        try:
            ref_path, tgt_path = self._ordered_paths()
            export_plan(
                self.plan, tgt_path, path,
                progress=lambda p, t: self.ui_queue.put(("progress", (p, t))),
                log=lambda m, tag="info": self.ui_queue.put(("log", m)),
                should_stop=self.stop_event.is_set)
            self.ui_queue.put(("log", f"✓ Saved: {path}"))
            self.ui_queue.put(("notify", (path, True)))
            # the reference is already decoded - hand it over instead of
            # reading the remux from disk again just to verify the export
            cached = self.decoded if self.decoded.get("key") == (
            track_key(ref_path), track_key(tgt_path)) else {}
            ref_array = cached.get("ref")
            self.ui_queue.put(("log", "Checking the alignment of the exported file…"))
            residual = None
            try:
                residual = measure_residual(ref_path, path, ref_array=ref_array)
            except Exception as exc:
                self.ui_queue.put(("log", f"Verification skipped: {exc}"))
            # 3 ms is below what anyone hears, and a simple offset/tempo export
            # is meant to be exactly that - shifting it again contradicts the
            # checkboxes and re-encodes the whole film for nothing
            if (residual and not self.stop_event.is_set()
                    and abs(residual[0]) >= 10.0):
                delta = residual[0]
                self.ui_queue.put(("log", f"Residual {delta:+.1f} ms - re-exporting aligned…"))
                adjusted = shift_plan(self.plan, delta)
                export_plan(
                    adjusted, tgt_path, path,
                    progress=lambda p, t: self.ui_queue.put(("progress", (p, t))),
                    log=lambda m, tag="info": self.ui_queue.put(("log", m)),
                    should_stop=self.stop_event.is_set)
                self.plan = adjusted
                self.ui_queue.put(("plan", adjusted))
                try:
                    residual = measure_residual(ref_path, path, ref_array=ref_array)
                except Exception as exc:
                    self.ui_queue.put(("log", f"Verification skipped: {exc}"))
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
                self.progress.setValue(int(pct * 10))
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
                notice = mismatch_text(payload)
                self.mismatch_label.setText(notice)
                self.mismatch_label.setVisible(bool(notice))
                if not payload.get("segments"):
                    best = max((float(p.get("conf", 0.0)) for p in self.points), default=0.0)
                    self.model_view.setPlainText(
                        f"No point passed the confidence filter (best seen: {best:.1f}).\n"
                        "Lower Min confidence, raise Window or switch Signal.")
                self._update_summary()
                self._draw()
                self.btn_export.setEnabled(bool(payload.get("segments")))

            elif kind == "idle":
                self.btn_scan.setEnabled(True)
                self.btn_stop.setEnabled(False)
                if self.stop_event.is_set():
                    # the worker stopped, so "Stopping…" must not stay on screen
                    self.status.setText("Stopped")
                    self.progress.setValue(0)
                else:
                    self.progress.setValue(1000 if self.plan else 0)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for field in (self.edit_ref, self.edit_tgt):
                field.set_drop_hint(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        for field in (self.edit_ref, self.edit_tgt):
            field.set_drop_hint(False)
        event.accept()

    def dropEvent(self, event):
        for field in (self.edit_ref, self.edit_tgt):
            field.set_drop_hint(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        for path in paths:
            if not self.ref_path:
                self.set_path("ref", path)
            elif not self.tgt_path:
                self.set_path("tgt", path)
        event.acceptProposedAction()

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
            self.mux_label.setText("")
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
        self._update_mux_hint()

    def _update_mux_hint(self) -> None:
        """Manual-mux advice under the headline: the delay, or why there is none."""
        plan = self.plan or {}
        self.mux_label.setText(mux_hint(plan))
        if mux_delay_ms(plan) is not None:
            colour = self.accent
        else:
            colour = "#b05a00" if LIGHT else "#f0a040"     # same amber as the chart
        self.mux_label.setStyleSheet(
            f"color: {colour}; font: 600 9pt 'Segoe UI';")

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
        """Read a number from a text field, falling back to the default."""
        try:
            return float(str(edit.text()).strip().replace(",", "."))
        except (TypeError, ValueError):
            return float(default)

    def _fill_table(self):
        self.table.setRowCount(len(self.points))
        for i, p in enumerate(self.points):
            source = "manual" if p.get("kind") == "manual" else (
                "wave" if p.get("mode") == "w" else "env")
            for col, text in enumerate((format_timecode(p["t"]),
                                        f"{p['offset']:+.1f}",
                                        f"{p.get('conf', 0):.1f}", source)):
                self.table.setItem(i, col, QTableWidgetItem(text))

    def delete_point(self):
        row = self.table.currentRow()
        if not (0 <= row < len(self.points)) or not self.meta:
            return
        del self.points[row]
        plan = build_plan(self.points, self.meta["ref_duration"],
                                 min_conf=self._num(self.entry_conf, 2),
                                 slope_hint=self._fps_hint(),
                                 allow_drift=getattr(self, "allow_drift", True))
        self.plan = plan
        self.model_view.setPlainText("\n".join(plan_lines(plan)[:10]))
        notice = mismatch_text(plan)
        self.mismatch_label.setText(notice)
        self.mismatch_label.setVisible(bool(notice))
        self._update_summary()
        self._draw()
        self._fill_table()
        self.btn_export.setEnabled(bool(plan.get("segments")))
        self.btn_preview.setEnabled(_HAVE_PLAYER)

    def _notify(self, title, content, ok=True):
        (InfoBar.success if ok else InfoBar.error)(
            title=title, content=content, orient=Qt.Horizontal,
            isClosable=True, position=InfoBarPosition.TOP_RIGHT,
            duration=4000, parent=self)

    def _segment_at(self, t):
        for seg in (self.plan or {}).get("segments", []):
            if seg["t_start"] - 1.0 <= t <= seg["t_end"] + 1.0:
                return seg
        return None

    def _point_colour(self, point):
        """Red when the measurement sits on the model, accent when it does not."""
        seg = self._segment_at(point["t"])
        if seg is None:
            return self.accent
        model = float(seg["offset"]) + float(seg["slope"]) * (
            float(point["t"]) - float(seg["t_start"]))
        # how close counts as "on the line" follows the fit's own scatter
        tolerance = max(8.0, min(30.0, 0.5 * float(seg.get("rms") or 0.0)))
        if abs(float(point["offset"]) - model) <= tolerance:
            return "#ff5a5f"
        return self.accent

    def _draw(self):
        ax = self.ax
        ax.clear()
        ax.set_facecolor(self.chart_bg)
        if self.points:
            ax.scatter([p["t"] for p in self.points],
                       [p["offset"] for p in self.points],
                       s=10, c=[self._point_colour(p) for p in self.points],
                       alpha=0.9, label="target (measured)")
        first = True
        for seg in (self.plan or {}).get("segments", []):
            x0, x1 = seg["t_start"], seg["t_end"]
            y0 = seg["offset"]
            ax.plot([x0, x1], [y0, y0 + seg["slope"] * (x1 - x0)],
                    color="#3ddc97", lw=1.6,
                    label="fitted model" if first else None)
            first = False
        ax.axhline(0, color="#c8c8c8", lw=1.3, ls="--", alpha=0.9,
                   label="reference (0 ms)")
        # the legend is a row of labels under the canvas (see the layout above)
        limits = chart_ylim(self.points) if self.points else None
        if limits:
            ax.set_ylim(*limits)
        ax.tick_params(colors=self.chart_fg, labelsize=7, labelrotation=25)
        for spine in ax.spines.values():
            spine.set_color(self.chart_spine)
        ax.grid(alpha=0.25, color=self.chart_grid)
        span = 0.0
        if self.points:
            span = max(float(p["t"]) for p in self.points)
        elif self.plan:
            span = float(self.plan.get("duration") or 0.0)
        if span > 0:
            ax.xaxis.set_major_locator(MultipleLocator(time_tick_step(span)))
            ax.xaxis.set_major_formatter(FuncFormatter(
                lambda value, _pos: format_timecode(value)))
        ax.set_xlabel("Film time", color=self.chart_fg, fontsize=8)
        # rotated timecode labels need their own strip, otherwise the axis
        # title is clipped by the canvas edge
        try:
            self.canvas.figure.subplots_adjust(bottom=0.22, left=0.10,
                                               right=0.985, top=0.97)
        except Exception:
            pass
        ax.set_ylabel("Offset (ms)", color=self.chart_fg, fontsize=8)
        self.canvas.draw_idle()

    def _pan_start(self, event):
        """Left button down: remember the pixels and the view at that moment."""
        if getattr(event, "button", None) != 1 or event.inaxes is None:
            return
        bbox = self.ax.bbox
        if bbox.width < 1 or bbox.height < 1:
            return
        self._pan = {"px": float(event.x), "py": float(event.y),
                     "xlim": self.ax.get_xlim(), "ylim": self.ax.get_ylim(),
                     "w": float(bbox.width), "h": float(bbox.height)}

    def _pan_move(self, event):
        """
        Mouse drag moves the plot with the pointer.

        The delta is measured in pixels against the view captured at the press.
        Measuring it in data units feeds the movement back into the mapping, so
        every pixel of travel changes how far the next pixel goes and the drag
        stutters.
        """
        pan = getattr(self, "_pan", None)
        if pan is None:
            return
        x0, x1 = pan["xlim"]
        y0, y1 = pan["ylim"]
        dx = (float(event.x) - pan["px"]) / pan["w"] * (x1 - x0)
        dy = (float(event.y) - pan["py"]) / pan["h"] * (y1 - y0)
        self.ax.set_xlim(x0 - dx, x1 - dx)
        self.ax.set_ylim(y0 - dy, y1 - dy)
        self.canvas.draw_idle()

    def _pan_end(self, event):
        self._pan = None

    def _zoom_at(self, event):
        """Ctrl + wheel zooms the plot around the pointer."""
        if event.inaxes is None or event.xdata is None or event.ydata is None:
            return
        if not (QApplication.keyboardModifiers() & Qt.ControlModifier):
            return
        step = getattr(event, "step", 0)
        factor = 0.8 if step > 0 else 1.25
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        x, y = float(event.xdata), float(event.ydata)
        self.ax.set_xlim([x - (x - xlim[0]) * factor, x + (xlim[1] - x) * factor])
        self.ax.set_ylim([y - (y - ylim[0]) * factor, y + (ylim[1] - y) * factor])
        self.canvas.draw_idle()


try:
    import ctypes as _ctypes
    _ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "Jank8.AudioDesyncFinder")
except Exception:
    pass


_INSTANCE = None      # single-instance mutex handle, held until the process ends


def main():
    global LIGHT
    instance = acquire_single_instance()
    if instance is None:
        show_message("Audio Desync Finder",
                     "Audio Desync Finder is already running.\n"
                     "Close the other window first.")
        return 0
    LIGHT = system_is_light()
    setTheme(Theme.LIGHT if LIGHT else Theme.DARK)
    setThemeColor(QColor(system_accent_hex()))
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    QTimer.singleShot(500, win.check_ffmpeg)
    global _INSTANCE
    _INSTANCE = instance          # kept alive for the life of the process
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
