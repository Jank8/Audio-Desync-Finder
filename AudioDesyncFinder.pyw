"""Audio Desync Finder 0.1 - rebuilt from scratch.

The interface is the old shell (PySide6 + Fluent, Mica backdrop); everything
under it is new.  The idea it is built around:

    A dub is a different recording of the dialogue and the *same* recording of
    the music and the effects.  So the timeline is measured block by block, each
    block is asked "is this the same audio at all?", and only the blocks that
    answer yes are allowed to shape the model.  Dialogue blocks are not junk and
    not cuts - they are simply not evidence, and the offset across them is
    interpolated from the music on either side.

Nothing is ever split because dialogue does not match, and no silence is
inserted unless a real, supported structural change demands it.

Run: pythonw AudioDesyncFinder.pyw
"""

# Audio Desync Finder 0.1 - Copyright (C) 2026 Janusz Woźniak
# Licensed under the MIT licence; see LICENSE.

import importlib
import os
import queue
import subprocess
import sys
import threading

PACKAGES = ["PySide6", "qfluentwidgets", "numpy", "scipy", "matplotlib"]


def _missing_dependencies():
    missing = []
    for name in PACKAGES:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(name)
    return missing


def _install_dependencies(packages):
    import tkinter as tk
    from tkinter import ttk

    win = tk.Tk()
    win.title("Audio Desync Finder - installing libraries")
    win.geometry("520x150")
    win.configure(bg="#101010")
    ttk.Label(win, text="Installing: " + ", ".join(packages),
              background="#101010", foreground="#ffffff").pack(pady=18)
    bar = ttk.Progressbar(win, mode="indeterminate", length=420)
    bar.pack(pady=6)
    bar.start(12)
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

import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from concurrent.futures import as_completed  # noqa: E402
from concurrent.futures.thread import ThreadPoolExecutor  # noqa: E402

import numpy as np  # noqa: E402
from scipy import signal  # noqa: E402

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QIntValidator  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog,  # noqa: E402
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QSlider, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CardWidget, CheckBox, ComboBox,  # noqa: E402
                            FluentWindow,
                            LineEdit, MessageBox, PrimaryPushButton, ProgressBar,
                            PushButton, RadioButton, StrongBodyLabel, TableWidget,
                            TextEdit, Theme, setTheme, setThemeColor)


try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_PLAYER = True
except Exception:                          # a stripped PySide6 without Multimedia
    QAudioOutput = QMediaPlayer = QVideoWidget = None
    HAVE_PLAYER = False

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter, MultipleLocator  # noqa: E402

APP_NAME = "Audio Desync Finder"
APP_VERSION = "4.0.9"
REPO_URL = "https://github.com/Jank8/Audio-Desync-Finder"

# ---- engine -----------------------------------------------------------------

SCAN_SR = 16000           # everything is analysed at this rate
HOP_MS = 10               # envelope frame, 10 ms
FFT_WINDOW_S = 80.0       # the correlation window used when measuring a block
BLOCK_S = 80.0            # one verdict per block of the film
# 80 s measured best on a real pair: 53 of 171 blocks carried the same audio and
# the model's RMS was 25 ms, against 34 ms at 40 s and 27 ms at 120 s.
# The window is clamped to the block size: a window longer than the block would
# average a re-recorded stretch together with the music around it and call the
# whole thing "the same audio", which is exactly what must not happen.
LAG_SEARCH_S = 90.0       # how far a block may look for its lag: a real pair
                          # can be several seconds off from the very start, and a
                          # narrow window centred on the previous block then finds
                          # nothing and labels half the film "dialogue"
WIDE_SEARCH_S = 90.0      # re-anchor range when a block loses the lock
SUPPORT_MIN = 0.30        # a block must correlate at least this well
CONTRAST_MIN = 0.10       # ... and beat its best rival by this much
STEP_MIN_MS = 300.0       # a structural change worth a segment
SPLIT_GAIN_MS = 40.0      # RMS improvement a split must buy


def _data_dir(*parts):
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "AudioDesyncFinder2", *parts)
    os.makedirs(path, exist_ok=True)
    return path


def _cache_dir():
    return _data_dir("cache")


def clear_cache():
    """Everything the app wrote: decoded tracks and nothing else."""
    folder = _cache_dir()
    freed = 0
    files = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            freed += os.path.getsize(path)
            os.remove(path)
            files += 1
        except OSError:
            pass
    return files, freed


def _ffmpeg(name="ffmpeg"):
    for folder in (os.path.dirname(sys.executable), ""):
        candidate = shutil.which(name) if not folder else os.path.join(folder, name + ".exe")
        if candidate and os.path.exists(candidate):
            return candidate
    found = shutil.which(name)
    if found:
        return found
    for folder in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"):
        candidate = os.path.join(folder, name + ".exe")
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("ffmpeg not found - install it or put it on PATH")


def _run(cmd, **kwargs):
    flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
    return subprocess.run(cmd, creationflags=flags, **kwargs)


def probe(path):
    """Duration and framerate, in one ffprobe call."""
    out = _run([_ffmpeg("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate,duration",
                "-show_entries", "format=duration", "-of", "json", path],
               capture_output=True, text=True)
    info = {"duration": 0.0, "fps": None}
    try:
        data = json.loads(out.stdout or "{}")
    except Exception:
        data = {}
    streams = data.get("streams") or []
    if streams:
        rate = str(streams[0].get("r_frame_rate") or "")
        if "/" in rate:
            try:
                num, den = rate.split("/")
                info["fps"] = float(num) / float(den) if float(den) else None
            except Exception:
                info["fps"] = None
        try:
            info["duration"] = float(streams[0].get("duration") or 0.0)
        except Exception:
            info["duration"] = 0.0
    if not info["duration"]:
        try:
            info["duration"] = float((data.get("format") or {}).get("duration") or 0.0)
        except Exception:
            info["duration"] = 0.0
    return info


def _track_key(path):
    stat = os.stat(path)
    raw = f"{os.path.abspath(path)}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


_ffmpeg_procs = []
_instance_handle = None


def get_startupinfo():
    """Keep the console window of every ffmpeg out of sight on Windows."""
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        return info
    return None


def acquire_single_instance(name: str = "AudioDesyncFinder"):
    """
    Hold a named Windows mutex for the life of the process.

    Returns the handle (keep it alive) or None when another instance already owns
    the name.  A second copy fights the first one for the same files and the same
    decoded-audio cache, and none of the UI is built for two.
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


def _decode_parallel(path: str, total: float, workers: int,
                     on_fraction=None, should_stop=None):
    """
    Decode the track in *workers* slices at once and concatenate the PCM.

    FFmpeg decodes a single stream on one core, so splitting the timeline is the
    only way to use the machine.  Each slice is decoded with half a second of
    overlap and trimmed to the exact sample count, so the joins are seamless.

    Every slice follows its own FFmpeg position (-progress on stderr, stdout
    carries the PCM): equal slices started together all finish at the same
    moment, so counting finished slices leaves the bar frozen until the very end.
    """
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
        cmd = [_ffmpeg(), "-v", "error", "-nostdin", "-nostats",
               "-stats_period", "0.05", "-progress", "pipe:2",
               "-ss", f"{start:.3f}", "-i", path, "-t", f"{length:.3f}",
               "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(SCAN_SR),
               "-f", "s16le", "-"]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL,
                                   startupinfo=get_startupinfo())
        _ffmpeg_procs.append(process)

        def follow() -> None:
            """Keep this slice's position in *done* while it decodes."""
            for raw in iter(process.stderr.readline, b""):
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
            data = process.stdout.read()
        finally:
            process.wait()
            follower.join(timeout=10.0)
            try:
                _ffmpeg_procs.remove(process)
            except ValueError:
                pass
            try:
                process.stdout.close()
                process.stderr.close()
            except OSError:
                pass
        piece = np.frombuffer(data, dtype=np.int16)
        if index < workers - 1:
            piece = piece[:int(round(chunk * SCAN_SR))]
        parts[index] = piece
        with lock:
            done[index] = length
        report(force=True)

    stop_watch = threading.Event()
    cancelled = threading.Event()

    def watch() -> None:
        """Kill the running slices as soon as the caller asks to stop."""
        while not stop_watch.wait(0.2):
            if should_stop is not None and should_stop():
                cancelled.set()
                for process in list(_ffmpeg_procs):
                    try:
                        process.kill()
                    except Exception:
                        pass
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = [pool.submit(run, index) for index in range(workers)]
            for future in as_completed(pending):
                if cancelled.is_set() or (should_stop is not None and should_stop()):
                    for process in list(_ffmpeg_procs):
                        try:
                            process.kill()
                        except Exception:
                            pass
                    raise RuntimeError("Cancelled by the user")
                future.result()
    finally:
        stop_watch.set()
        watcher.join(timeout=1.0)

    usable = [piece for piece in parts if piece is not None and len(piece)]
    return np.concatenate(usable) if usable else np.zeros(0, dtype=np.int16)


def _decode_sequential(path: str, total: float, on_fraction=None,
                       should_stop=None):
    """One ffmpeg for the whole file, with its own progress on stderr."""
    cmd = [_ffmpeg(), "-v", "error", "-nostdin", "-nostats",
           "-stats_period", "0.05", "-progress", "pipe:2",
           "-i", path, "-vn", "-map", "0:a:0", "-ac", "1", "-ar", str(SCAN_SR),
           "-f", "s16le", "-"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL,
                               startupinfo=get_startupinfo())
    _ffmpeg_procs.append(process)
    reported = [0.0]

    def follow() -> None:
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("out_time_us="):
                continue
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000.0
            except ValueError:
                continue
            if on_fraction and total > 0:
                frac = min(1.0, seconds / total)
                if frac - reported[0] >= 0.0005:
                    reported[0] = frac
                    on_fraction(frac)

    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    try:
        data = process.stdout.read()
    finally:
        process.wait()
        follower.join(timeout=10.0)
        try:
            _ffmpeg_procs.remove(process)
        except ValueError:
            pass
        try:
            process.stdout.close()
            process.stderr.close()
        except OSError:
            pass
    if process.returncode != 0 or not data:
        raise RuntimeError(f"could not decode {os.path.basename(path)}")
    return np.frombuffer(data, dtype=np.int16).copy()


def decode(path, progress=None, should_stop=None, workers: int = 1,
           on_fraction=None):
    """
    The file as 16 kHz mono int16, from the cache when possible.

    The mechanism is the one that worked in 3.1.x: slices decode in parallel, each
    follows its own FFmpeg position and the bar is driven by the sum of those
    positions, so it moves the whole time instead of jumping when a slice ends.
    """
    cache_path = os.path.join(_cache_dir(), f"track-{_track_key(path)}.pcm")
    if os.path.exists(cache_path):
        data = np.fromfile(cache_path, dtype=np.int16)
        if data.size:
            if on_fraction:
                on_fraction(1.0)
            return data
    if progress:
        progress(f"Decoding {os.path.basename(path)}…")
    duration = 0.0
    try:
        duration = float(probe(path).get("duration") or 0.0)
    except Exception:
        duration = 0.0
    workers = max(1, int(workers))
    if duration >= 180.0 and workers > 1:
        data = _decode_parallel(path, duration, workers, on_fraction, should_stop)
    else:
        data = _decode_sequential(path, duration, on_fraction, should_stop)
    if not len(data):
        raise RuntimeError(f"could not decode {os.path.basename(path)}")
    try:
        data.tofile(cache_path)
    except OSError:
        pass
    if on_fraction:
        on_fraction(1.0)
    return data


def envelope(track):
    """10 ms log-RMS envelope, zero mean and unit variance."""
    hop = SCAN_SR * HOP_MS // 1000
    frames = len(track) // hop
    if frames < 4:
        return np.zeros(4, dtype=np.float32)
    block = np.asarray(track[:frames * hop], dtype=np.float32).reshape(frames, hop)
    rms = np.sqrt(np.mean(block * block, axis=1) + 1e-9)
    env = np.log1p(rms * 200.0)
    env -= env.mean()
    std = env.std()
    return (env / std if std > 1e-6 else env).astype(np.float32)


def _block_scores(env_a, env_b, start, length, lag_max):
    """Correlation of one reference block against every candidate position."""
    if start + length > len(env_a):
        return None
    ref = env_a[start:start + length]
    norm = float(np.linalg.norm(ref))
    if norm < 1e-6:
        return None
    lo = max(0, start - lag_max)
    hi = min(len(env_b) - length, start + lag_max)
    if hi <= lo:
        return None
    candidates = env_b[lo:hi + length].astype(np.float64)
    corr = np.correlate(candidates, ref.astype(np.float64) / norm, mode="valid")
    squares = np.concatenate(([0.0], np.cumsum(candidates * candidates)))
    energy = np.sqrt(np.maximum(squares[length:] - squares[:-length], 1e-9))
    return corr / energy, lo


def measure_block(env_a, env_b, t: float, lag_prior: float = 0.0,
                  window: float = FFT_WINDOW_S, lag_search: float = LAG_SEARCH_S):
    """
    One block's verdict: the lag it prefers, how well it correlates and how
    clearly it beats the next best lag.

    A block of music answers with a sharp peak; a block of re-recorded dialogue
    answers with noise and a score close to zero, which is exactly the signal we
    need to tell "the dub is different here" from "the audio is broken here".
    """
    hop_s = HOP_MS / 1000.0
    length = max(8, int(window / hop_s))
    start = int(round(t / hop_s))
    if start < 0 or start + length > len(env_a):
        return None
    lag_max = int(lag_search / hop_s)
    prior = int(round(lag_prior / 1000.0 / hop_s))
    scored = _block_scores(env_a, env_b, start + prior, length, lag_max)
    if scored is None:
        return None
    scores, lo = scored
    best = int(np.argmax(scores))
    best_score = float(scores[best])
    rival = 0.0
    guard = int(0.5 / hop_s)
    for index in range(0, len(scores), 1):
        if abs(index - best) >= guard and scores[index] > rival:
            rival = float(scores[index])
    lag_frames = (lo + best) - (start + prior)
    return {"t": round(t + window / 2.0, 2),
            "offset": round(lag_frames * hop_s * 1000.0, 2),
            "support": round(best_score, 4),
            "contrast": round(best_score - rival, 4),
            "window": window}


def _reliable_threshold(points) -> float:
    """The support a block needs: absolute floor, or relative to the film."""
    if not points:
        return SUPPORT_MIN
    supports = sorted(p["support"] for p in points)
    median = supports[len(supports) // 2]
    return max(SUPPORT_MIN, 0.55 * median)


def measure_block_waveform(ref, tgt, t: float, window: float = 40.0,
                           lag_search: float = LAG_SEARCH_S):
    """
    The same block measured on the waveform itself.

    Only useful when both files share the language (the same mix of music, voice
    and effects): then the samples line up and the correlation is sharper than
    any envelope.  It is decimated four times so a block costs milliseconds.
    """
    step = 4
    start = int(t * SCAN_SR)
    length = int(window * SCAN_SR)
    if start < 0 or start + length > len(ref):
        return None
    a = np.asarray(ref[start:start + length:step], dtype=np.float64)
    lag = int(lag_search * SCAN_SR)
    lo = max(0, start - lag)
    hi = min(len(tgt) - length, start + lag)
    if hi <= lo:
        return None
    b = np.asarray(tgt[lo:hi + length:step], dtype=np.float64)
    if a.std() < 1e-6 or b.std() < 1e-6:
        return None
    a = a - a.mean()
    b = b - b.mean()
    corr = np.correlate(b, a / np.linalg.norm(a), mode="valid")
    squares = np.concatenate(([0.0], np.cumsum(b * b)))
    energy = np.sqrt(np.maximum(squares[len(a):] - squares[:-len(a)], 1e-9))
    scores = corr / energy
    best = int(np.argmax(scores))
    best_score = float(scores[best])
    rival = 0.0
    guard = int(0.5 * SCAN_SR / step)
    for index in range(0, len(scores), 1):
        if abs(index - best) >= guard and scores[index] > rival:
            rival = float(scores[index])
    lag_frames = (lo + best * step) - start
    return {"t": round(t + window / 2.0, 2),
            "offset": round(lag_frames / SCAN_SR * 1000.0, 2),
            "support": round(best_score, 4),
            "contrast": round(best_score - rival, 4),
            "window": window, "mode": "waveform"}


def scan(env_a, env_b, block: float = BLOCK_S, window: float = FFT_WINDOW_S,
         lag_search: float = LAG_SEARCH_S, duration: float = 0.0,
         progress=None, should_stop=None, mode: str = "auto"):
    """
    Walk the film and measure every block, following the lag as it moves.

    The search is centred on the previous *reliable* lag, so a steady drift is
    followed instead of lost, while a block of dialogue simply does not shift
    the centre.
    """
    if duration <= 0:
        duration = min(len(env_a), len(env_b)) * HOP_MS / 1000.0
    points = []
    prior = 0.0
    t = 0.0
    total = max(1, int(duration // block))
    done = 0
    while t + window <= duration:
        if should_stop and should_stop():
            break
        result = measure_block(env_a, env_b, t, prior, window, lag_search)
        if result is not None and mode == "waveform":
            sharper = measure_block_waveform(env_a, env_b, t, window, lag_search)
            if sharper is not None and sharper["support"] > result["support"]:
                result = sharper
        if result is not None and result["support"] < SUPPORT_MIN:
            # A long stretch of re-recorded dialogue loses the lock completely:
            # the search is centred on the last good block, so once the offset
            # has moved further than +-lag_search nothing can be found again and
            # the rest of the film looks like "dialogue".  A wide re-anchor on
            # the block that failed brings the lock back.
            wide = measure_block(env_a, env_b, t, 0.0, window,
                                 max(lag_search, WIDE_SEARCH_S))
            if wide is not None and wide["support"] > (result["support"] + 0.15):
                wide["reanchored"] = True
                result = wide
        done += 1
        if progress:
            progress(done, total)
        if result is not None:
            points.append(result)
        t += block
    threshold = _reliable_threshold(points)
    for result in points:
        result["reliable"] = (result["support"] >= threshold
                              and result["contrast"] >= CONTRAST_MIN)
    # the prior follows the reliable blocks, in timeline order
    prior = 0.0
    for result in points:
        if result["reliable"]:
            prior = float(result["offset"])
    return points


def _theil_sen(times, offsets):
    """Median of pairwise slopes - the line least disturbed by outliers."""
    slopes = []
    for i in range(len(times) - 1):
        for j in range(i + 1, min(i + 8, len(times))):
            dt = times[j] - times[i]
            if abs(dt) > 1e-6:
                slopes.append((offsets[j] - offsets[i]) / dt)
    slope = float(np.median(slopes)) if slopes else 0.0
    intercept = float(np.median(offsets - slope * times))
    return slope, intercept


def _line_rms(times, offsets, slope, intercept):
    residual = offsets - (intercept + slope * times)
    return float(np.sqrt(np.mean(residual ** 2)))


def _median(values):
    return float(np.median(values)) if len(values) else 0.0


def _mad(values):
    if not len(values):
        return 0.0
    return float(np.median(np.abs(np.asarray(values) - np.median(values))))


def _find_splits(times, offsets, depth: int = 0):
    """
    Where the offset really steps, searched before anything is thrown away.

    Dropping outliers first is what hid the real jumps on a real pair: the blocks
    that disagreed with the line were the *evidence* for a second segment, and a
    consensus filter deleted them.  So the split is looked for on the raw blocks:
    a boundary qualifies when the medians of the two sides differ by at least
    STEP_MIN_MS and two lines describe the data clearly better than one.  Only
    after the boundaries are fixed are the stragglers inside each segment dropped.
    """
    if depth >= 4 or len(times) < 6:
        return []
    base_scatter = _mad(offsets)
    best = None
    for cut in range(3, len(times) - 2):
        left_o, right_o = offsets[:cut], offsets[cut:]
        step = abs(_median(right_o) - _median(left_o))
        if step < STEP_MIN_MS:
            continue
        scatter = (_mad(left_o) * len(left_o) + _mad(right_o) * len(right_o)) / len(times)
        gain = base_scatter - scatter
        if gain <= 0.35 * max(base_scatter, 1.0):
            continue
        if best is None or gain > best[0]:
            best = (gain, cut)
    if best is None:
        return []
    cut = best[1]
    left = _find_splits(times[:cut], offsets[:cut], depth + 1)
    right = _find_splits(times[cut:], offsets[cut:], depth + 1)
    return left + [float(times[cut])] + right


def _robust_scatter(values):
    """
    How wide a stretch is, in milliseconds.

    Mean absolute deviation, not the median one: a clean step of the size we are
    looking for leaves three points at one level and two at the other, and their
    *median* deviation is exactly zero - a width of zero made every such step
    look like a perfect fit and no split was ever found on the positive case.
    """
    if not len(values):
        return 0.0
    array = np.asarray(values, dtype=np.float64)
    return float(np.mean(np.abs(array - np.median(array))))


def _best_split(times, offsets, min_blocks: int = 2):
    """
    The boundary that explains the data best, or None when there is none.

    Cost is the MAD of each side, which is not moved by a couple of wild blocks;
    a boundary is only worth having when it more than halves that width and the
    two sides really sit at different levels.
    """
    if len(times) < min_blocks * 2:
        return None
    base = _robust_scatter(offsets)
    if base < 5.0:
        return None
    best = None
    for cut in range(min_blocks, len(times) - min_blocks + 1):
        left_o, right_o = offsets[:cut], offsets[cut:]
        left_width = _robust_scatter(left_o)
        right_width = _robust_scatter(right_o)
        cost = (left_width * len(left_o) + right_width * len(right_o)) / len(times)
        gain = 1.0 - cost / base
        step = abs(float(np.median(right_o)) - float(np.median(left_o)))
        if step < STEP_MIN_MS or gain < 0.5:
            continue
        score = gain * min(step, 5000.0)
        if best is None or score > best[0]:
            best = (score, cut)
    return None if best is None else best[1]


def _segment_levels(times, offsets, depth: int = 0):
    """Binary segmentation: find the strongest boundary, then recurse into both."""
    if depth >= 6:
        return []
    cut = _best_split(times, offsets)
    if cut is None:
        return []
    left = _segment_levels(times[:cut], offsets[:cut], depth + 1)
    right = _segment_levels(times[cut:], offsets[cut:], depth + 1)
    return left + [float(times[cut])] + right


def fit_model(points, duration: float):
    """
    A piecewise line found by segmentation, then cleaned inside each stretch.

    The earlier version dropped outliers first and looked for splits afterwards,
    which hid the real changes: the blocks that disagreed with the majority line
    were exactly the evidence for a second segment.  Now the boundaries are found
    on all candidates first (binary segmentation on the robust width), and only
    then is each stretch cleaned and fitted.

    A stretch that ends up with fewer than two usable blocks is merged into its
    neighbour instead of discarding the whole model - that was the second mistake:
    an entire film was left as one line because one stretch was too thin.
    """
    if not points:
        return {"segments": [], "reliable": 0, "points": 0, "duration": duration}
    supports = sorted(p["support"] for p in points)
    threshold = max(SUPPORT_MIN, 0.5 * supports[len(supports) // 2])
    candidates = sorted((p for p in points if p["support"] >= threshold),
                        key=lambda p: p["t"])
    for point in points:
        point["reliable"] = False
    if len(candidates) < 3:
        return {"segments": [], "reliable": len(candidates), "points": len(points),
                "duration": duration, "threshold": round(threshold, 3)}

    times_all = np.array([p["t"] for p in candidates], dtype=np.float64)
    offsets_all = np.array([p["offset"] for p in candidates], dtype=np.float64)
    cuts = _segment_levels(times_all, offsets_all)
    bounds = [0.0] + cuts + [float(duration)]

    def fit_stretch(start, end):
        inside = [p for p in candidates if start - 1e-6 <= p["t"] <= end + 1e-6]
        kept = inside
        for _ in range(3):
            if len(kept) < 2:
                break
            times = np.array([p["t"] for p in kept], dtype=np.float64)
            offsets = np.array([p["offset"] for p in kept], dtype=np.float64)
            slope, intercept = _theil_sen(times, offsets)
            residual = np.abs(offsets - (intercept + slope * times))
            limit = max(80.0, 3.0 * float(np.median(residual)))
            mask = residual <= limit
            if mask.all():
                break
            kept = [p for p, ok in zip(kept, mask) if ok]
        if len(kept) < 2:
            return None
        times = np.array([p["t"] for p in kept], dtype=np.float64)
        offsets = np.array([p["offset"] for p in kept], dtype=np.float64)
        slope, intercept = _theil_sen(times, offsets)
        for point in kept:
            point["reliable"] = True
        return {"start": float(start), "end": float(end), "slope": slope,
                "offset": float(intercept + slope * start), "n": len(kept),
                "rms": _line_rms(times, offsets, slope, intercept)}

    stretches = []
    for start, end in zip(bounds, bounds[1:]):
        fitted = fit_stretch(start, end)
        if fitted is None:
            if stretches:
                stretches[-1]["end"] = float(end)      # merge into the neighbour
                continue
            continue
        stretches.append(fitted)
    if not stretches:
        return {"segments": [], "reliable": sum(1 for p in points if p["reliable"]),
                "points": len(points), "duration": duration,
                "threshold": round(threshold, 3)}

    stretches.sort(key=lambda s: s["start"])
    stretches[0]["start"] = 0.0
    for previous, current in zip(stretches, stretches[1:]):
        current["start"] = previous["end"]
    stretches[-1]["end"] = float(duration)

    out = []
    for stretch in stretches:
        out.append({"t_start": round(stretch["start"], 2),
                    "t_end": round(stretch["end"], 2),
                    "offset": round(float(stretch["offset"]), 2),
                    "slope": round(float(stretch["slope"]), 5),
                    "atempo": round(float(max(0.5, min(2.0,
                                                        1.0 + stretch["slope"] / 1000.0))), 8),
                    "n": int(stretch["n"]),
                    "rms": round(float(stretch["rms"]), 2)})
    # the offset is measured again at each segment's own start, from its blocks
    for segment in out:
        inside = [p for p in points
                  if segment["t_start"] - 1e-6 <= p["t"] <= segment["t_end"] + 1e-6
                  and p.get("reliable")]
        if inside:
            segment["offset"] = round(float(np.median(
                [p["offset"] - segment["slope"] * (p["t"] - segment["t_start"])
                 for p in inside])), 2)
    jumps = []
    for previous, current in zip(out, out[1:]):
        at = previous["offset"] + previous["slope"] * (current["t_start"] - previous["t_start"])
        delta = current["offset"] - at
        jumps.append({"t": current["t_start"], "delta": round(delta, 2),
                      "mode": "pad" if delta < 0 else "skip"})
    return {"segments": out, "jumps": jumps,
            "reliable": sum(1 for p in points if p["reliable"]),
            "points": len(points), "duration": duration,
            "threshold": round(threshold, 3)}


def resample_track(track, rate: float, chunk: int = 1 << 22):
    """
    Stretch or squeeze a track by `rate`, in chunks.

    A 24 fps release of a 25 fps master runs 4 % fast, and inside an 80 s block
    that is three seconds of drift - enough to smear every correlation.  The
    target is brought onto the reference's clock first, and the same factor is
    applied again at export, so the model only has to describe the real edits.
    """
    if abs(rate - 1.0) < 1e-6:
        return track
    length = int(round(len(track) / rate))
    out = np.empty(length, dtype=np.int16)
    positions = np.arange(length, dtype=np.float64) * rate
    source = np.asarray(track, dtype=np.float32)
    index = np.arange(len(source), dtype=np.float64)
    for start in range(0, length, chunk):
        stop = min(length, start + chunk)
        out[start:stop] = np.interp(positions[start:stop], index, source).astype(np.int16)
    return out


def black_gaps(track, min_ms: float = 1500.0, db: float = -60.0):
    """
    Stretches where the track is digitally silent - the black inserts.

    A release often carries a black screen with no sound where another one has a
    logo, a recap or nothing at all.  Those places are structural and they are
    visible without listening to a single note, so they are the most reliable way
    to line two edits up: the sequence of gaps, and where it stops matching, is
    where the two films really part.

    Returns [(start_seconds, length_ms)].
    """
    hop = SCAN_SR // 100
    frames = len(track) // hop
    if frames < 10:
        return []
    block = np.asarray(track[:frames * hop], dtype=np.float32).reshape(frames, hop)
    rms = np.sqrt(np.mean(block * block, axis=1) + 1e-9)
    level = 20.0 * np.log10(np.maximum(rms, 1e-6) / 32768.0)
    gaps = []
    start = None
    for index, value in enumerate(level):
        quiet = value < db
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            length = (index - start) * 10.0
            if length >= min_ms:
                gaps.append((start * 0.01, length))
            start = None
    return gaps


def apply_plan_to_array(track, plan):
    """
    The plan applied to the decoded target, in memory.

    Every segment takes target audio from `t_start + offset` to `t_end + offset`,
    is resampled to the segment's length (that is what atempo does) and dropped
    into the reference timeline.  No ffmpeg, no temporary file: this exists so
    the result can be measured and the model corrected before anything is
    written.
    """
    out = np.zeros(int(len(track)), dtype=np.float32)
    source = np.asarray(track, dtype=np.float32)
    for segment in plan.get("segments") or []:
        t0 = float(segment["t_start"])
        t1 = float(segment["t_end"])
        offset = float(segment["offset"])
        src0 = max(0, int((t0 + offset / 1000.0) * SCAN_SR))
        src1 = min(len(source), int((t1 + offset / 1000.0) * SCAN_SR))
        dst0 = int(t0 * SCAN_SR)
        dst_len = int((t1 - t0) * SCAN_SR)
        if src1 <= src0 or dst_len <= 0 or dst0 >= len(out):
            continue
        piece = source[src0:src1]
        positions = np.linspace(0.0, len(piece) - 1.0, dst_len, dtype=np.float32)
        resampled = np.interp(positions, np.arange(len(piece), dtype=np.float32), piece)
        end = min(len(out), dst0 + dst_len)
        out[dst0:end] = resampled[:end - dst0].astype(np.float32)
    return out


def residual_curve(ref_env, corrected_env, duration: float, block: float = 60.0,
                   window: float = 60.0):
    """
    How far the corrected audio still is from the reference, block by block.

    The model is applied first, so what this measures is the error that is left:
    a flat near-zero curve means the model is right, a stretch that sits at, say,
    +2500 ms means those blocks were never part of the line the model fitted.
    """
    points = []
    t = 0.0
    while t + window <= duration:
        result = measure_block(ref_env, corrected_env, t, 0.0, window, 2.0)
        if result is not None:
            points.append(result)
        t += block
    return points


def refine_with_residual(points, plan, residual_points, duration: float,
                         off_ms: float = 200.0):
    """
    Split the model where the corrected audio is still off.

    The consensus fit cannot see a change that only affects a few late blocks:
    they look like outliers against the majority line.  Measuring the *corrected*
    result does see it, because there the wrong stretch stands out as a run of
    blocks with a large residual.  Each such run becomes its own segment, refitted
    from the original measurements inside it.

    Returns (plan, notes).
    """
    segments = plan.get("segments") or []
    if not segments or not residual_points:
        return plan, []
    bad = [p for p in residual_points
           if abs(float(p["offset"])) >= off_ms and float(p.get("support") or 0) >= 0.25]
    if not bad:
        return plan, []
    # group the bad blocks into runs
    runs = []
    for point in bad:
        if runs and point["t"] - runs[-1][-1]["t"] <= 180.0:
            runs[-1].append(point)
        else:
            runs.append([point])
    runs = [run for run in runs if len(run) >= 1]
    if not runs:
        return plan, []
    notes = []
    bounds = [0.0] + [float(segment["t_start"]) for segment in segments[1:]] + [float(duration)]
    new_bounds = set(bounds)
    for run in runs:
        first = run[0]["t"]
        # the boundary goes to the middle between the last good block and this one
        before = [p["t"] for p in residual_points if p["t"] < first
                  and abs(float(p["offset"])) < off_ms * 0.6]
        boundary = (max(before) + first) / 2.0 if before else max(0.0, first - 30.0)
        if 20.0 < boundary < duration - 20.0:
            new_bounds.add(round(boundary, 2))
    ordered = sorted(new_bounds)
    rebuilt = []
    for start, end in zip(ordered, ordered[1:]):
        inside = [p for p in points if start - 1e-6 <= p["t"] <= end + 1e-6]
        usable = [p for p in inside if p.get("support", 0) >= 0.3]
        if len(usable) < 2:
            rebuilt.append(None)
            continue
        times = np.array([p["t"] for p in usable], dtype=np.float64)
        offsets = np.array([p["offset"] for p in usable], dtype=np.float64)
        slope, intercept = _theil_sen(times, offsets)
        rebuilt.append({"t_start": round(start, 2), "t_end": round(end, 2),
                        "offset": round(float(intercept + slope * start), 2),
                        "slope": round(float(slope), 5),
                        "atempo": round(float(max(0.5, min(2.0, 1.0 + slope / 1000.0))), 8),
                        "n": len(usable),
                        "rms": round(_line_rms(times, offsets, slope, intercept), 2)})
    if any(segment is None for segment in rebuilt):
        return plan, []
    # cover the film and join the boundaries
    rebuilt[0]["t_start"] = 0.0
    for previous, current in zip(rebuilt, rebuilt[1:]):
        current["t_start"] = previous["t_end"]
    rebuilt[-1]["t_end"] = round(float(duration), 2)
    jumps = []
    for previous, current in zip(rebuilt, rebuilt[1:]):
        at = previous["offset"] + previous["slope"] * (current["t_start"] - previous["t_start"])
        delta = current["offset"] - at
        jumps.append({"t": current["t_start"], "delta": round(delta, 2),
                      "mode": "pad" if delta < 0 else "skip"})
        notes.append(f"the corrected audio is off by {delta:+.0f} ms from "
                     f"{current['t_start']:.0f} s")
    out = dict(plan)
    out["segments"] = rebuilt
    out["jumps"] = jumps
    out["residual_refined"] = True
    return out, notes


def refine_jumps(ref_env, tgt_env, points, plan, duration: float,
                 span: float = 300.0, block: float = 20.0, step: float = 10.0):
    """
    Move every segment boundary to where the two lines actually meet.

    An 80 s block cannot place a step better than a couple of minutes, and the
    residual of the corrected film shows it.  Around each boundary the offsets are
    measured again on a 20 s grid, and the cut is moved to the position that
    leaves the least residual on both sides - the two lines are fitted from the
    dense points, so the boundary and the levels come out together.

    Returns (plan, notes).
    """
    segments = plan.get("segments") or []
    if len(segments) < 2:
        return plan, []
    notes = []
    for index in range(1, len(segments)):
        centre = float(segments[index]["t_start"])
        low = max(0.0, centre - span)
        high = min(float(duration), centre + span)
        # the dense pass must stay near the levels the model already has: a wide
        # search here locks onto repetitive music tens of seconds away and the
        # boundary then moves to a place that has nothing to do with the step
        prior = float(segments[index - 1]["offset"]) + float(segments[index - 1]["slope"]) * (
            centre - float(segments[index - 1]["t_start"]))
        dense = []
        t = low
        while t + block <= high:
            result = measure_block(ref_env, tgt_env, t, prior, block, 6.0)
            if (result is not None and result["support"] >= 0.35
                    and abs(result["offset"] - prior) <= 3000.0):
                dense.append(result)
            t += step
        if len(dense) < 6:
            continue
        times = np.array([p["t"] for p in dense], dtype=np.float64)
        offsets = np.array([p["offset"] for p in dense], dtype=np.float64)
        best = None
        for cut in range(3, len(times) - 2):
            left_t, left_o = times[:cut], offsets[:cut]
            right_t, right_o = times[cut:], offsets[cut:]
            left_slope, left_icpt = _theil_sen(left_t, left_o)
            right_slope, right_icpt = _theil_sen(right_t, right_o)
            cost = (_line_rms(left_t, left_o, left_slope, left_icpt) * len(left_t)
                    + _line_rms(right_t, right_o, right_slope, right_icpt) * len(right_t))
            if best is None or cost < best[0]:
                best = (cost, cut, left_slope, left_icpt, right_slope, right_icpt)
        if best is None:
            continue
        cost, cut, left_slope, left_icpt, right_slope, right_icpt = best
        boundary = float(times[cut])
        old = float(segments[index]["t_start"])
        segments[index - 1].update({
            "t_start": segments[index - 1]["t_start"],
            "t_end": round(boundary, 2),
            "slope": round(float(left_slope), 5),
            "offset": round(float(left_icpt + left_slope * segments[index - 1]["t_start"]), 2),
            "atempo": round(float(max(0.5, min(2.0, 1.0 + left_slope / 1000.0))), 8),
            "n": int(cut), "rms": round(float(_line_rms(times[:cut], offsets[:cut],
                                                        left_slope, left_icpt)), 2)})
        segments[index].update({
            "t_start": round(boundary, 2),
            "slope": round(float(right_slope), 5),
            "offset": round(float(right_icpt + right_slope * boundary), 2),
            "atempo": round(float(max(0.5, min(2.0, 1.0 + right_slope / 1000.0))), 8),
            "n": int(len(times) - cut),
            "rms": round(float(_line_rms(times[cut:], offsets[cut:],
                                         right_slope, right_icpt)), 2)})
        notes.append(f"boundary moved from {old:.0f} s to {boundary:.0f} s "
                     f"({boundary - old:+.0f} s), levels "
                     f"{left_icpt + left_slope * boundary:+.0f} / "
                     f"{right_icpt + right_slope * boundary:+.0f} ms")
    jumps = []
    for previous, current in zip(segments, segments[1:]):
        at = previous["offset"] + previous["slope"] * (current["t_start"] - previous["t_start"])
        delta = current["offset"] - at
        jumps.append({"t": current["t_start"], "delta": round(delta, 2),
                      "mode": "pad" if delta < 0 else "skip"})
    out = dict(plan)
    out["segments"] = segments
    out["jumps"] = jumps
    out["refined"] = True
    return out, notes


def mux_text(plan) -> str:
    """
    What to type into MKVToolNix for this model, or why it cannot be done there.

    MKVToolNix takes one delay per track (and optionally a stretch), so it can
    express a single line.  A model with several segments, or with a drift the
    stretch cannot cover, has to be exported instead - and saying so is better
    than handing over a number that is only right for part of the film.
    """
    segments = (plan or {}).get("segments") or []
    if not segments:
        return ""
    if len(segments) > 1:
        return ("MKVToolNix: no single delay fits this model "
                f"({len(segments)} segments) - export the corrected audio")
    segment = segments[0]
    drift = abs(float(segment["slope"]))
    delay = -float(segment["offset"])
    if drift < 0.05:
        return f"MKVToolNix delay: {delay:+.0f} ms"
    total = drift * float((plan or {}).get("duration") or 0.0)
    if total <= 200.0:
        return (f"MKVToolNix delay: {delay:+.0f} ms, plus the stretch "
                f"{1.0 + segment['slope'] / 1000.0:.6f} (about {total:.0f} ms "
                "across the film)")
    return ("MKVToolNix: a single delay cannot cover the "
            f"{total:+.0f} ms of drift - export the corrected audio")


def diagnose_jump(ref, tgt, plan, index: int, span: float = 900.0) -> list:
    """
    Look closely around one jump, over a quarter of an hour either side.

    A jump of several seconds is never the music's fault: one of the two edits has
    a black insert, a logo or a missing scene there.  Black gaps need no
    correlation to find, so they are read first - if the same gap sits a few
    seconds later in the other file, that difference *is* the jump, and the exact
    boundary is the gap itself rather than the middle of an 80 s block.
    """
    notes = []
    segments = plan.get("segments") or []
    if index <= 0 or index >= len(segments):
        return notes
    boundary = float(segments[index]["t_start"])
    delta = float(segments[index]["offset"]) - float(segments[index - 1]["offset"])
    notes.append(f"Jump @ {format_timecode(boundary)}: {delta:+.0f} ms, "
                 f"looking at {format_timecode(max(0.0, boundary - span))} - "
                 f"{format_timecode(boundary + span)}")
    for label, track in (("reference", ref), ("target", tgt)):
        gaps = [g for g in black_gaps(track)
                if boundary - span <= g[0] <= boundary + span]
        if not gaps:
            notes.append(f"  {label}: no black insert within {span:.0f} s")
            continue
        nearest = min(gaps, key=lambda g: abs(g[0] - boundary))
        notes.append(f"  {label}: {len(gaps)} black insert(s), closest at "
                     f"{format_timecode(nearest[0])} lasting {nearest[1] / 1000:.1f} s")
    zones = [z for z in transition_zones(ref, tgt)
             if boundary - span <= z[0] <= boundary + span]
    if zones:
        nearest = min(zones, key=lambda z: abs(z[0] - boundary))
        notes.append(f"  a fade-to-black zone starts at "
                     f"{format_timecode(nearest[0])} and lasts "
                     f"{nearest[1] / 1000:.1f} s - that is what a hard cut in the "
                     f"reference became")
    # the same gap in both files: their difference is the jump
    ref_gaps = [g for g in black_gaps(ref) if boundary - span <= g[0] <= boundary + span]
    tgt_gaps = [g for g in black_gaps(tgt) if boundary - span <= g[0] <= boundary + span]
    for gap in ref_gaps:
        match = min(tgt_gaps, key=lambda g: abs(g[0] - gap[0]), default=None)
        if match is None:
            continue
        shift = match[0] - gap[0]
        if abs(shift) >= 1000.0:
            notes.append(f"  the same black insert sits {shift / 1000:+.1f} s in the "
                         f"target - that difference is the jump")
            break
    return notes


def align_gaps(ref_gaps, tgt_gaps, tolerance: float = 3.0,
               length_tolerance: float = 2.0, window: int = 4,
               min_delta: float = 500.0):
    """
    Line the two black-insert sequences up and report the real structural changes.

    Two things make this harder than it looks, and both are handled here:

    * a gap is only the same gap if it also lasts about as long, so the match
      requires the lengths to agree within `length_tolerance` seconds - matching
      on start time alone paired the wrong gaps and injected fake deltas of
      seconds;
    * one wrong pair must not create an edit, so the shift between the two
      sequences is taken as the *median over a window* of matched pairs either
      side of a boundary (default four), not as a difference between two
      neighbours.  A single mismatch cannot move a median.

    Returns (pairs, events) where an event is {"t": reference time, "delta": ms,
    "support": number of pairs in the smaller window}.
    """
    n, m = len(ref_gaps), len(tgt_gaps)
    if not n or not m:
        return [], []
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ref_start, ref_len = ref_gaps[i - 1]
        for j in range(1, m + 1):
            tgt_start, tgt_len = tgt_gaps[j - 1]
            same = (abs(ref_start - tgt_start) <= tolerance
                    and abs(ref_len - tgt_len) <= max(length_tolerance,
                                                      max(ref_len, tgt_len)))
            if same:
                table[i][j] = table[i - 1][j - 1] + 1
            else:
                table[i][j] = max(table[i - 1][j], table[i][j - 1])
    pairs = []
    i, j = n, m
    while i and j:
        ref_start, ref_len = ref_gaps[i - 1]
        tgt_start, tgt_len = tgt_gaps[j - 1]
        same = (abs(ref_start - tgt_start) <= tolerance
                and abs(ref_len - tgt_len) <= max(length_tolerance,
                                                  max(ref_len, tgt_len)))
        if same and table[i][j] == table[i - 1][j - 1] + 1:
            pairs.append((ref_gaps[i - 1], tgt_gaps[j - 1]))
            i -= 1
            j -= 1
        elif table[i - 1][j] >= table[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    if len(pairs) < window * 2:
        return pairs, []
    shifts = [tgt[0] - ref[0] for ref, tgt in pairs]
    events = []
    for index in range(window, len(pairs) - window + 1):
        before = float(np.median(shifts[max(0, index - window):index]))
        after = float(np.median(shifts[index:index + window]))
        delta = (after - before) * 1000.0
        if abs(delta) < min_delta:
            continue
        if events and abs(pairs[index][0][0] - events[-1]["t"]) < 60.0:
            continue
        events.append({"t": round(pairs[index][0][0], 2),
                       "delta": round(delta, 1), "support": window})
    return pairs, events


def transition_zones(ref, tgt, frame_ms: float = 100.0, drop_db: float = 12.0,
                     min_ms: float = 600.0, pad_ms: float = 300.0):
    """
    Places where the target fades out and back in while the reference cuts.

    A lektor release often replaces a hard cut with a fade to black and a fade
    back: the audio never reaches digital zero, it just drops a dozen decibels and
    comes back.  Looking for digital silence alone therefore finds only the two
    darkest seconds and the cut misses the fades, which is what makes it audible.
    These zones are found against the target's own level, and are widened a little
    at both ends so the fades are removed with the black.

    Returns [(start_seconds, length_ms)].
    """
    hop = int(SCAN_SR * frame_ms / 1000.0)
    frames = min(len(ref), len(tgt)) // hop
    if frames < 10:
        return []
    a = np.asarray(ref[:frames * hop], dtype=np.float32).reshape(frames, hop)
    b = np.asarray(tgt[:frames * hop], dtype=np.float32).reshape(frames, hop)
    level_a = 20.0 * np.log10(np.maximum(np.sqrt(np.mean(a * a, axis=1)), 1e-6) / 32768.0)
    level_b = 20.0 * np.log10(np.maximum(np.sqrt(np.mean(b * b, axis=1)), 1e-6) / 32768.0)
    reference_level = float(np.median(level_b)) if len(level_b) else -20.0
    dip = (level_b < reference_level - drop_db) & (level_a > level_b + 6.0)
    zones = []
    start = None
    for index, flag in enumerate(dip):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            length = (index - start) * frame_ms
            if length >= min_ms:
                zones.append([start * frame_ms / 1000.0, length])
            start = None
    merged = []
    for zone in zones:
        if merged and zone[0] - (merged[-1][0] + merged[-1][1] / 1000.0) <= 1.0:
            end = max(merged[-1][0] + merged[-1][1] / 1000.0, zone[0] + zone[1] / 1000.0)
            merged[-1] = [merged[-1][0], (end - merged[-1][0]) * 1000.0]
        else:
            merged.append(list(zone))
    padded = []
    for start_s, length in merged:
        begin = max(0.0, start_s - pad_ms / 1000.0)
        end = start_s + length / 1000.0 + pad_ms / 1000.0
        padded.append((begin, (end - begin) * 1000.0))
    return padded


def target_holes(ref, tgt, min_ms: float = 200.0, quiet_db: float = -90.0,
                 loud_db: float = -60.0, frame_ms: float = 50.0):
    """
    Places where the target is silent while the reference still has sound.

    That is what a hole looks like from the audio side: not "this is quiet", but
    "this is quiet *here* and the reference is not".  A black insert, a missing
    line or a broken splice all show up as one, and cutting exactly that stretch
    is what puts the two timelines back together.
    """
    hop = int(SCAN_SR * frame_ms / 1000.0)
    frames = min(len(ref), len(tgt)) // hop
    if frames < 10:
        return []
    a = np.asarray(ref[:frames * hop], dtype=np.float32).reshape(frames, hop)
    b = np.asarray(tgt[:frames * hop], dtype=np.float32).reshape(frames, hop)
    level_a = 20.0 * np.log10(np.maximum(np.sqrt(np.mean(a * a, axis=1)), 1e-6) / 32768.0)
    level_b = 20.0 * np.log10(np.maximum(np.sqrt(np.mean(b * b, axis=1)), 1e-6) / 32768.0)
    # Digital zero, not "rather quiet": the inserts that matter measure -180 dBFS
    # in the target while the reference sits at -45, so the test is an absolute
    # one.  An earlier relative threshold of a dozen dB found 83 quiet moments of
    # dialogue and missed every real hole.
    hole = (level_b < quiet_db) & (level_a > loud_db)
    holes = []
    start = None
    for index, flag in enumerate(hole):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            length = (index - start) * frame_ms
            if length >= min_ms:
                holes.append((start * frame_ms / 1000.0, length))
            start = None
    # holes a moment apart are one hole: the target goes quiet, a single sound
    # leaks through, and it goes quiet again - that is still one missing stretch
    merged = []
    for hole in holes:
        if merged and hole[0] - (merged[-1][0] + merged[-1][1] / 1000.0) <= 1.5:
            start = merged[-1][0]
            end = max(merged[-1][0] + merged[-1][1] / 1000.0,
                      hole[0] + hole[1] / 1000.0)
            merged[-1] = (start, (end - start) * 1000.0)
        else:
            merged.append(hole)
    return merged


def model_from_gaps(ref, tgt, points, duration: float, min_delta: float = 500.0):
    """
    Build the model from the black-insert sequence instead of from the music.

    The gaps are language-independent and exact to the frame, so their alignment
    gives the boundaries of the real edits: every place where the shift between
    the two sequences changes is an insert or a cut, and its size is that change.
    The levels between those boundaries are taken from the blocks when there are
    enough of them, and from the cumulative shift otherwise.

    Returns (plan, notes) or (None, []) when the gaps say nothing.
    """
    gaps_ref, gaps_tgt = black_gaps(ref), black_gaps(tgt)
    pairs, events = align_gaps(gaps_ref, gaps_tgt)
    if not pairs:
        return None, []
    cuts = [event for event in events if abs(event["delta"]) >= min_delta]
    if not cuts:
        return None, []
    # two boundaries a few seconds apart are the two ends of one insert, not two
    # edits: a black gap produces an event at its start and another at its end
    spaced = []
    for cut in cuts:
        if not spaced or cut["t"] - spaced[-1]["t"] >= 60.0:
            spaced.append(cut)
        else:
            spaced[-1] = {"t": spaced[-1]["t"],
                          "delta": spaced[-1]["delta"] + cut["delta"]}
    cuts = [cut for cut in spaced if abs(cut["delta"]) >= min_delta]
    if not cuts:
        return None, []
    base = (pairs[0][1][0] - pairs[0][0][0]) * 1000.0
    bounds = [0.0] + [float(cut["t"]) for cut in cuts] + [float(duration)]
    levels = []
    level = base
    for index in range(len(bounds) - 1):
        levels.append(level)
        if index < len(cuts):
            level += float(cuts[index]["delta"])
    segments = []
    for index, (start, end) in enumerate(zip(bounds, bounds[1:])):
        if end - start < 1.0:
            continue
        inside = [p for p in points
                  if start <= p["t"] < end and float(p.get("support") or 0.0) >= 0.35]
        offset = levels[index]
        slope = 0.0
        if len(inside) >= 4:
            times = np.array([p["t"] for p in inside], dtype=np.float64)
            offsets = np.array([p["offset"] for p in inside], dtype=np.float64)
            slope, intercept = _theil_sen(times, offsets)
            fitted = float(np.median(offsets - slope * (times - start)))
            if abs(fitted - offset) <= 4000.0:
                offset = fitted
            else:
                slope = 0.0
        rms = 0.0
        if len(inside) >= 3:
            times = np.array([p["t"] for p in inside], dtype=np.float64)
            offsets = np.array([p["offset"] for p in inside], dtype=np.float64)
            rms = _line_rms(times, offsets, slope, offset - slope * start)
        segments.append({"t_start": round(start, 2), "t_end": round(end, 2),
                         "offset": round(float(offset), 2),
                         "slope": round(float(slope), 5),
                         "atempo": round(float(max(0.5, min(2.0, 1.0 + slope / 1000.0))), 8),
                         "n": len(inside), "rms": round(float(rms), 2)})
    if len(segments) < 2:
        return None, []
    segments[0]["t_start"] = 0.0
    for previous, current in zip(segments, segments[1:]):
        current["t_start"] = previous["t_end"]
    segments[-1]["t_end"] = round(float(duration), 2)
    jumps = []
    notes = []
    for previous, current in zip(segments, segments[1:]):
        at = previous["offset"] + previous["slope"] * (current["t_start"] - previous["t_start"])
        delta = current["offset"] - at
        jumps.append({"t": current["t_start"], "delta": round(delta, 2),
                      "mode": "pad" if delta < 0 else "skip"})
        notes.append(f"edit at {format_timecode(current['t_start'])}: "
                     f"{delta:+.0f} ms ({'pad' if delta < 0 else 'skip'})")
    plan = {"segments": segments, "jumps": jumps,
            "duration": float(duration), "from_gaps": True,
            "reliable": sum(1 for p in points if p.get("reliable")),
            "points": len(points)}
    return plan, notes


def plan_from_holes(points, holes, duration: float, min_hole: float = 1000.0):
    """
    Build the plan around the digital holes, not around the music.

    When the target has a stretch of absolute silence that the reference fills
    with sound, the two timelines part exactly there - no correlation needed.  So
    the holes themselves become the boundaries and the offsets are the median of
    the blocks in each stretch between them.
    """
    cuts = sorted(start for start, length in holes if length >= min_hole)
    if not cuts:
        return None
    bounds = [0.0] + cuts + [float(duration)]
    segments = []
    for start, end in zip(bounds, bounds[1:]):
        if end - start < 5.0:
            continue
        inside = [p for p in points if start <= p["t"] < end]
        if not inside:
            continue
        offsets = sorted(float(p["offset"]) for p in inside)
        offset = offsets[len(offsets) // 2]
        segments.append({"t_start": round(start, 2), "t_end": round(end, 2),
                         "offset": round(offset, 2), "slope": 0.0,
                         "atempo": 1.0, "n": len(inside),
                         "rms": round(float(np.std([float(p["offset"]) - offset
                                                    for p in inside])), 2)})
    if len(segments) < 2:
        return None
    segments[0]["t_start"] = 0.0
    for previous, current in zip(segments, segments[1:]):
        current["t_start"] = previous["t_end"]
    segments[-1]["t_end"] = round(float(duration), 2)
    jumps = []
    for previous, current in zip(segments, segments[1:]):
        delta = float(current["offset"]) - float(previous["offset"])
        jumps.append({"t": current["t_start"], "delta": round(delta, 2),
                      "mode": "pad" if delta < 0 else "skip"})
    return {"segments": segments, "jumps": jumps, "duration": float(duration),
            "reliable": sum(1 for p in points if p.get("reliable")),
            "points": len(points), "from_holes": True}


def single_line_plan(points, duration: float):
    """
    One offset and one drift for the whole film, ignoring every jump.

    This is the safety net for the model: when the break detector finds more
    structure than the material really has, one robust line still describes the
    film well enough to watch it, and the audio can only walk out of sync
    gradually instead of jumping.
    """
    usable = [p for p in points if float(p.get("support") or 0.0) >= 0.3]
    if len(usable) < 2:
        return None
    times = np.array([p["t"] for p in usable], dtype=np.float64)
    offsets = np.array([p["offset"] for p in usable], dtype=np.float64)
    slope, intercept = _theil_sen(times, offsets)
    rms = _line_rms(times, offsets, slope, intercept)
    for point in points:
        point["reliable"] = True
    return {"segments": [{"t_start": 0.0, "t_end": round(float(duration), 2),
                          "offset": round(float(intercept), 2),
                          "slope": round(float(slope), 5),
                          "atempo": round(float(max(0.5, min(2.0,
                                                              1.0 + slope / 1000.0))), 8),
                          "n": len(usable), "rms": round(float(rms), 2)}],
            "jumps": [], "duration": float(duration),
            "reliable": len(usable), "points": len(points)}


def waveform_offset(ref, tgt, centre: float, window: float = 30.0,
                    prior: float = 0.0, search: float = 30.0, decimate: int = 4):
    """
    The offset of one 30 s stretch, measured on the waveform itself.

    The envelope is what the scan uses, because it survives a different language.
    At a cut, though, the question is not "the same music" but "the same samples",
    and thirty seconds of decimated waveform lock onto that far more sharply than
    any envelope, as long as the search is wide enough to reach the other side.
    """
    step = max(1, int(decimate))
    frames = int(window * SCAN_SR)
    start = int((centre - window / 2.0) * SCAN_SR)
    if start < 0 or start + frames > len(ref):
        return None
    a = np.asarray(ref[start:start + frames:step], dtype=np.float64)
    a = a - a.mean()
    norm = float(np.linalg.norm(a))
    if norm < 1e-6:
        return None
    span = int(search * SCAN_SR)
    lo = max(0, start + int(prior / 1000.0 * SCAN_SR) - span)
    hi = min(len(tgt) - frames, start + int(prior / 1000.0 * SCAN_SR) + span)
    if hi <= lo:
        return None
    b = np.asarray(tgt[lo:hi + frames:step], dtype=np.float64)
    b = b - b.mean()
    window_len = len(a)                     # decimated, so the energy uses it too
    corr = np.correlate(b, a / norm, mode="valid")
    squares = np.concatenate(([0.0], np.cumsum(b * b)))
    energy = np.sqrt(np.maximum(squares[window_len:] - squares[:-window_len], 1e-9))
    scores = corr / energy
    best = int(np.argmax(scores))
    value = float(scores[best])
    rival = 0.0
    guard = int(0.5 * SCAN_SR / step)
    for index in range(0, len(scores)):
        if abs(index - best) >= guard and scores[index] > rival:
            rival = float(scores[index])
    lag = (lo + best * step) - start
    return {"offset": round(lag / SCAN_SR * 1000.0, 2),
            "support": round(value, 4), "contrast": round(value - rival, 4)}


def measure_around(ref_env, tgt_env, when: float, window: float = 2.0,
                   prior: float = 0.0, search: float = 3.0):
    """
    The offset measured on a short window - the waveform, not a model.

    A jump placed by 80 s blocks lands somewhere inside a transition; the only way
    to cut exactly is to measure a second or two of real audio on each side of it
    and take those two positions as the cut.
    """
    return measure_block(ref_env, tgt_env, max(0.0, when - window / 2.0),
                         prior, window, search)


def refine_cuts_by_waveform(ref, tgt, plan, zones=None, window: float = 30.0,
                           search: float = 30.0):
    """
    Cut the target to match the reference, measured 30 s either side of the cut.

    The model says the offset changes in this neighbourhood.  Thirty seconds of
    waveform before the cut and thirty after say exactly where the target has to
    be joined: the difference between those two offsets is how much of the target
    is not in the reference, and the boundary is where the transition begins.

    Returns (plan, notes).
    """
    segments = plan.get("segments") or []
    if len(segments) < 2:
        return plan, []
    zones = zones if zones is not None else transition_zones(ref, tgt)
    notes = []
    for index in range(1, len(segments)):
        boundary = float(segments[index]["t_start"])
        zone = min(zones, key=lambda z: abs(z[0] - boundary), default=None)
        centre = zone[0] if zone and abs(zone[0] - boundary) <= 300.0 else boundary
        length = zone[1] if zone else 0.0
        prior_before = float(segments[index - 1]["offset"])
        prior_after = float(segments[index]["offset"])
        ref_env, tgt_env = envelope(ref), envelope(tgt)
        left = waveform_offset(ref, tgt, centre - window / 2.0, window,
                               prior_before, search)
        right = waveform_offset(ref, tgt, centre + length / 1000.0 + window / 2.0,
                                window, prior_after, search)
        # two different masters do not share samples: the raw waveform only locks
        # when both files come from the same mix, so the envelope is the fallback -
        # it is what survives a re-recording of the dialogue
        if (left is None or left["support"] < 0.2) or (right is None or right["support"] < 0.2):
            envelope_left = measure_around(ref_env, tgt_env, centre - window / 2.0,
                                           window, prior_before, search)
            envelope_right = measure_around(ref_env, tgt_env,
                                            centre + length / 1000.0 + window / 2.0,
                                            window, prior_after, search)
            if envelope_left is not None and (left is None or envelope_left["support"] > left["support"]):
                left = envelope_left
            if envelope_right is not None and (right is None or envelope_right["support"] > right["support"]):
                right = envelope_right
        if left is None or right is None:
            continue
        if left["support"] < 0.15 or right["support"] < 0.15:
            notes.append(f"cut at {format_timecode(centre)}: left "
                         f"{left['support']:.2f} / right {right['support']:.2f} "
                         "support - left where it is")
            continue
        offset_before = float(left["offset"])
        offset_after = float(right["offset"])
        removed = offset_after - offset_before
        if removed <= 100.0:
            continue
        segments[index - 1]["t_end"] = round(centre, 2)
        segments[index]["t_start"] = round(centre, 2)
        segments[index - 1]["offset"] = round(offset_before, 2)
        segments[index]["offset"] = round(offset_after, 2)
        notes.append(
            f"cut at {format_timecode(centre)}: {removed / 1000:.2f} s removed, "
            f"offset {offset_before:+.0f} -> {offset_after:+.0f} ms "
            f"(30 s windows, support {left['support']:.2f}/{right['support']:.2f})")
    jumps = []
    for previous, current in zip(segments, segments[1:]):
        at = previous["offset"] + previous["slope"] * (current["t_start"] - previous["t_start"])
        delta = current["offset"] - at
        jumps.append({"t": current["t_start"], "delta": round(delta, 2),
                      "mode": "pad" if delta < 0 else "skip"})
    out = dict(plan)
    out["segments"] = segments
    out["jumps"] = jumps
    out["waveform_cuts"] = True
    return out, notes


def plan_lines(plan):
    """Human-readable summary of the model."""
    segments = plan.get("segments") or []
    if not segments:
        return ["No block of the film was the same audio in both files."]
    lines = [f"{len(segments)} segment(s), built from {plan.get('reliable', 0)} "
             f"of {plan.get('points', 0)} measured blocks."]
    for index, seg in enumerate(segments, start=1):
        lines.append(
            f"Seg {index}: {seg['t_start']:8.1f}-{seg['t_end']:8.1f}s  "
            f"offset {seg['offset']:+8.1f} ms  drift {seg['slope']:+7.3f} ms/s  "
            f"atempo {seg['atempo']:.6f}  [{seg['n']} blocks, RMS {seg['rms']:.0f} ms]")
    for jump in plan.get("jumps") or []:
        lines.append(f"Jump @ {jump['t']:.1f}s: {jump['delta']:+.1f} ms - "
                     f"{'pad silence' if jump['mode'] == 'pad' else 'skip audio'}")
    return lines


def quality_text(plan) -> str:
    segments = plan.get("segments") or []
    if not segments:
        return "No usable model."
    worst = max(float(seg["rms"]) for seg in segments)
    if worst <= 25.0:
        verdict = "good - the music lines up smoothly"
    elif worst <= 60.0:
        verdict = "fair - a normal scatter for material with a voice on top"
    else:
        verdict = "weak - the model is fitting noise, check the blocks by eye"
    return f"Quality: {verdict} (worst RMS {worst:.0f} ms)"


def _segment_chain(segment, next_offset) -> str:
    """
    The filter for one segment, in target time.

    The model says the content of reference time t sits at target time
    t + offset(t), so a segment takes target audio from `t_start + offset` to
    `t_end + offset`, speeds it by the segment's atempo and puts it back at
    t_start.  When the next segment starts earlier in the target than this one
    ended, the target is missing audio there and silence of exactly that length
    is inserted; when it starts later, the surplus is simply skipped.
    """
    offset = float(segment["offset"])
    t_start = float(segment["t_start"])
    t_end = float(segment["t_end"])
    atempo = float(segment["atempo"])
    source_start = t_start + offset / 1000.0
    source_end = t_end + offset / 1000.0
    chain = []
    silence = 0.0
    if source_start < 0.0:
        silence = -source_start
        source_start = 0.0
    chain.append(f"atrim=start={source_start:.3f}:end={max(source_end, source_start + 0.05):.3f}")
    chain.append("asetpts=PTS-STARTPTS")
    if abs(atempo - 1.0) > 1e-6:
        chain.append(f"atempo={atempo:.8f}")
    if silence > 0.004:
        chain.append(f"adelay={silence * 1000.0:.0f}|{silence * 1000.0:.0f}")
    if next_offset is not None:
        gap = float(next_offset) - offset          # negative: target lost audio
        if gap < -4.0:
            chain.append(f"apad=pad_dur={(-gap) / 1000.0:.3f}")
    return ",".join(chain)


def _audio_filter(plan) -> str:
    """
    The whole model as one ffmpeg filter graph.

    One segment is a plain -af chain; several are trimmed, retimed and joined
    with `concat`, with silence padded wherever the target really lacks audio.
    The framerate factor of the plan is multiplied into every segment's atempo,
    because the scan measured the target on the reference's clock and the file
    itself still runs at its own speed.
    """
    segments = plan.get("segments") or []
    if not segments:
        raise ValueError("nothing to export")
    fps_atempo = float(plan.get("fps_atempo") or 1.0)

    def with_fps(segment):
        prepared = dict(segment)
        prepared["atempo"] = float(segment["atempo"]) * fps_atempo
        return prepared

    if len(segments) == 1:
        return _segment_chain(with_fps(segments[0]), None)
    parts = []
    for index, segment in enumerate(segments):
        next_offset = (float(segments[index + 1]["offset"])
                       if index + 1 < len(segments) else None)
        parts.append(f"[0:a]{_segment_chain(with_fps(segment), next_offset)}[s{index}]")
    labels = "".join(f"[s{index}]" for index in range(len(segments)))
    return ";".join(parts) + f";{labels}concat=n={len(segments)}:v=0:a=1[out]"


_ENCODER_FOR = {
    "aac": "aac", "ac3": "ac3", "eac3": "eac3", "dts": "dca", "mp3": "libmp3lame",
    "opus": "libopus", "vorbis": "libvorbis", "flac": "flac", "alac": "alac",
    "pcm_s16le": "pcm_s16le", "pcm_s24le": "pcm_s24le", "truehd": "flac",
    "mlp": "flac", "mp2": "mp2",
}


def audio_stream_info(path: str) -> dict:
    """Codec, sample rate and channel count of the first audio stream."""
    out = _run([_ffmpeg("ffprobe"), "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,sample_rate,channels",
                "-of", "json", path], capture_output=True, text=True)
    info = {"codec": "", "rate": 0, "channels": 0}
    try:
        streams = (json.loads(out.stdout or "{}").get("streams") or [])
    except Exception:
        return info
    if streams:
        info["codec"] = str(streams[0].get("codec_name") or "")
        try:
            info["rate"] = int(streams[0].get("sample_rate") or 0)
        except (TypeError, ValueError):
            info["rate"] = 0
        try:
            info["channels"] = int(streams[0].get("channels") or 0)
        except (TypeError, ValueError):
            info["channels"] = 0
    return info


def export_plan(plan, target_path, out_path, progress=None, should_stop=None):
    """
    Write the corrected audio, reporting how far it has got.

    ffmpeg is asked for its progress on stdout, so the bar moves instead of the
    window sitting frozen: a three hour film takes minutes and there is no other
    way to tell a working export from a hung one.
    """
    segments = plan.get("segments") or []
    jumps = plan.get("jumps") or []
    if len(segments) > 1:
        if any(float(jump["delta"]) < 0 for jump in jumps):
            raise ValueError(
                f"this model has to insert silence in "
                f"{sum(1 for j in jumps if float(j['delta']) < 0)} place(s); that "
                "export is not verified yet")
    graph = _audio_filter(plan)
    multi = len(segments) > 1
    cmd = [_ffmpeg(), "-v", "error", "-nostdin", "-y", "-i", target_path]
    if multi:
        cmd += ["-filter_complex", graph, "-map", "[out]"]
    else:
        cmd += ["-map", "0:a:0", "-af", graph]
    # keep the source's own codec: re-encoding a DTS or AC-3 track to FLAC makes the
    # file bigger and changes what the user has to mux with
    source = audio_stream_info(target_path)
    codec = source["codec"]
    encoder = _ENCODER_FOR.get(codec, "flac")
    if encoder != codec and codec:
        if codec in ("truehd", "mlp"):
            if progress:
                progress(0.0, f"{codec.upper()} cannot be re-encoded - writing FLAC")
        cmd += ["-c:a", encoder]
    elif codec:
        cmd += ["-c:a", encoder]
    else:
        cmd += ["-c:a", "flac"]
    if source["rate"]:
        cmd += ["-ar", str(source["rate"])]
    if source["channels"]:
        cmd += ["-ac", str(source["channels"])]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]

    total = 0.0
    try:
        total = float(probe(target_path).get("duration") or 0.0)
    except Exception:
        total = float(plan.get("duration") or 0.0)
    flags = 0x08000000 if os.name == "nt" else 0
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, creationflags=flags)
    try:
        for line in process.stdout:
            if should_stop and should_stop():
                process.kill()
                raise RuntimeError("stopped")
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    done = float(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                if progress and total > 1.0:
                    progress(min(99.0, 100.0 * done / total),
                             f"Exporting {min(99.0, 100.0 * done / total):.0f} %")
            elif line == "progress=end":
                break
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode != 0:
        error = process.stderr.read().strip()
        raise RuntimeError(error or "ffmpeg failed")
    if progress:
        progress(100.0, "Export finished")
    return out_path


def residual_of(ref_path, out_path, progress=None):
    """How far the corrected file still is from the reference, in ms."""
    ref = decode(ref_path)
    out = decode(out_path)
    env_a = envelope(ref)
    env_b = envelope(out)
    duration = min(len(env_a), len(env_b)) * HOP_MS / 1000.0
    samples = []
    t = duration * 0.1
    while t < duration * 0.9:
        result = measure_block(env_a, env_b, t, 0.0, 40.0, 2.0)
        if result is not None:
            samples.append(result["offset"])
        t += max(60.0, duration * 0.05)
    if not samples:
        return None
    return float(np.median(samples))


# ---- interface ---------------------------------------------------------------

setTheme(Theme.DARK)
setThemeColor("#0f6cbd")


def fps_slope(fps_ref, fps_tgt) -> float:
    """Drift in ms per second implied by two framerates (0 when equal)."""
    try:
        ref = float(fps_ref)
        tgt = float(fps_tgt)
    except (TypeError, ValueError):
        return 0.0
    if ref <= 0 or tgt <= 0 or abs(ref - tgt) < 0.01:
        return 0.0
    return (ref / tgt - 1.0) * 1000.0


def format_timecode(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds))))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


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


class SampleDialog(QDialog):
    """
    The two soundtracks drawn one above the other on the same timeline.

    The reference is drawn as it is; the target is drawn already placed by the
    model, so a hole in the target - a black insert, a missing scene - appears as
    an empty stretch of the lower trace, and the model's own edits are the dashed
    lines.  That is what makes a wrong cut obvious without listening to it.
    """

    SPANS = (3, 10, 30, 60, 300)

    def __init__(self, parent, ref_path: str, tgt_path: str, plan_getter,
                 start_at: float = 0.0):
        super().__init__(parent)
        self.setWindowTitle("Sample")
        self.resize(980, 560)
        self.ref_path = ref_path
        self.tgt_path = tgt_path
        self.plan_getter = plan_getter
        self._ref = None
        self._tgt = None
        self.start = max(0.0, float(start_at))

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("At"))
        self.time_edit = QLineEdit(format_timecode(self.start))
        self.time_edit.setFixedWidth(90)
        self.time_edit.returnPressed.connect(self.refresh)
        row.addWidget(self.time_edit)
        self.combo_span = ComboBox(self)
        self.combo_span.addItems([f"{span} s" for span in self.SPANS])
        self.combo_span.setCurrentIndex(1)
        self.combo_span.currentIndexChanged.connect(self.refresh)
        row.addWidget(QLabel("Length"))
        row.addWidget(self.combo_span)
        show = QPushButton("Show")
        show.clicked.connect(self.refresh)
        row.addWidget(show)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        layout.addLayout(row)

        self.figure = Figure(figsize=(8, 4.6), facecolor="#16181d")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(211)
        self.ax_below = self.figure.add_subplot(212, sharex=self.ax)
        layout.addWidget(self.canvas, 1)
        QTimer.singleShot(200, self.refresh)

    def _tracks(self):
        if self._ref is None:
            self._ref = decode(self.ref_path)
        if self._tgt is None:
            self._tgt = decode(self.tgt_path)
        return self._ref, self._tgt

    def _offset_at(self, t: float) -> float:
        segments = (self.plan_getter() or {}).get("segments") or []
        if not segments:
            return 0.0
        for segment in segments:
            if float(segment["t_start"]) <= t <= float(segment["t_end"]):
                return float(segment["offset"]) + float(segment["slope"]) * (
                    t - float(segment["t_start"]))
        first = segments[0]
        return float(first["offset"]) + float(first["slope"]) * t

    def refresh(self):
        target = parse_timecode(self.time_edit.text())
        if target is None:
            self.status.setText("type 1:47:20")
            return
        self.start = max(0.0, target)
        span = self.SPANS[max(0, self.combo_span.currentIndex())]
        ref, tgt = self._tracks()
        offset = self._offset_at(self.start)
        a_from = int(self.start * SCAN_SR)
        a_to = a_from + int(span * SCAN_SR)
        b_from = int((self.start + offset / 1000.0) * SCAN_SR)
        b_to = b_from + int(span * SCAN_SR)
        if a_to > len(ref) or b_to > len(tgt) or a_from < 0 or b_from < 0:
            self.status.setText("outside the files")
            return
        a = np.asarray(ref[a_from:a_to], dtype=np.float32)
        b = np.asarray(tgt[b_from:b_to], dtype=np.float32)
        limit = min(len(a), len(b))
        a, b = a[:limit], b[:limit]
        times = np.arange(limit) / SCAN_SR
        peak = max(1.0, float(np.abs(a).max()), float(np.abs(b).max()))
        for axis in (self.ax, self.ax_below):
            axis.clear()
            axis.set_facecolor("#16181d")
            axis.tick_params(colors="#9aa4b8", labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#2a2f3a")
            axis.grid(True, color="#232833", linewidth=0.6)
        self.figure.patch.set_facecolor("#16181d")
        self.ax.plot(times, a / peak, color="#5fd38d", linewidth=0.7)
        self.ax.set_title("reference", color="#9aa4b8", fontsize=8, loc="left")
        self.ax_below.plot(times, b / peak, color="#4ea1ff", linewidth=0.7)
        self.ax_below.set_title("target, placed on the reference timeline",
                                color="#9aa4b8", fontsize=8, loc="left")
        self.ax_below.set_xlabel("seconds from the chosen point",
                                 color="#9aa4b8", fontsize=8)
        for segment in (self.plan_getter() or {}).get("segments") or []:
            position = float(segment["t_start"]) - self.start
            if 0.0 <= position <= times[-1]:
                for axis in (self.ax, self.ax_below):
                    axis.axvline(position, color="#ffb454", linewidth=0.8,
                                 linestyle="--")
        correlation = float(np.corrcoef(a, b)[0, 1]) if limit > 100 else 0.0
        # levels in dBFS for the quietest and loudest second of each trace: that
        # is what tells a real digital hole from a passage that only looks flat
        def second_levels(trace):
            size = SCAN_SR
            count = len(trace) // size
            if not count:
                return None, None
            chunks = np.asarray(trace[:count * size], dtype=np.float32).reshape(count, size)
            rms = np.sqrt(np.mean(chunks * chunks, axis=1) + 1e-9)
            level = 20.0 * np.log10(np.maximum(rms, 1e-6) / 32768.0)
            return float(level.min()), float(level.max())

        ref_min, ref_max = second_levels(a)
        tgt_min, tgt_max = second_levels(b)
        self.figure.tight_layout()
        self.canvas.draw_idle()
        self.status.setText(
            f"offset {offset:+.0f} ms · corr {correlation:+.2f} · "
            f"ref {ref_min:.0f}..{ref_max:.0f} dBFS · "
            f"target {tgt_min:.0f}..{tgt_max:.0f} dBFS")


class PreviewDialog(QDialog):
    """
    Both films side by side, with either soundtrack - or both - in the ears.

    The reference is the master clock: the picture on the left plays straight, and
    the target on the right is kept at `reference time + model offset`, with the
    segment's atempo as its playback rate.  The two audio outputs have their own
    switches, so it is possible to hear only the lektor, only the original, or
    both at once - which is the fastest way to judge whether a stretch really is
    in sync.
    """

    def __init__(self, parent, ref_path: str, tgt_path: str, plan_getter,
                 start_at: float = 0.0):
        super().__init__(parent)
        self.setWindowTitle("Preview - both films")
        self.resize(1500, 760)
        self.plan_getter = plan_getter
        self.start = max(0.0, float(start_at))
        self._scrubbing = False
        self._was_playing = False

        layout = QVBoxLayout(self)
        videos = QHBoxLayout()
        self.video_ref = QVideoWidget(self)
        self.video_tgt = QVideoWidget(self)
        for widget, title in ((self.video_ref, "reference"), (self.video_tgt, "target")):
            column = QVBoxLayout()
            label = QLabel(title)
            label.setStyleSheet("color:#9aa4b8; font: 9pt 'Segoe UI'")
            column.addWidget(label)
            column.addWidget(widget, 1)
            videos.addLayout(column, 1)
        layout.addLayout(videos, 1)

        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 1000)
        layout.addWidget(self.slider)

        row = QHBoxLayout()
        self.btn_play = QPushButton("Pause")
        self.btn_play.clicked.connect(self._toggle)
        row.addWidget(self.btn_play)
        self.chk_ref_audio = CheckBox("Original audio")
        self.chk_ref_audio.setChecked(False)
        self.chk_ref_audio.stateChanged.connect(self._apply_audio)
        row.addWidget(self.chk_ref_audio)
        self.chk_tgt_audio = CheckBox("Target audio")
        self.chk_tgt_audio.setChecked(True)
        self.chk_tgt_audio.stateChanged.connect(self._apply_audio)
        row.addWidget(self.chk_tgt_audio)
        row.addWidget(QLabel("Go to"))
        self.time_edit = QLineEdit()
        self.time_edit.setPlaceholderText("1:47:20")
        self.time_edit.setFixedWidth(90)
        self.time_edit.returnPressed.connect(self._jump)
        row.addWidget(self.time_edit)
        go = QPushButton("Go")
        go.clicked.connect(self._jump)
        row.addWidget(go)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        layout.addLayout(row)

        self.ref_player = QMediaPlayer(self)
        self.ref_audio = QAudioOutput(self)
        self.ref_audio.setVolume(1.0)
        self.ref_player.setAudioOutput(self.ref_audio)
        self.ref_player.setVideoOutput(self.video_ref)
        self.tgt_player = QMediaPlayer(self)
        self.tgt_audio = QAudioOutput(self)
        self.tgt_audio.setVolume(1.0)
        self.tgt_player.setAudioOutput(self.tgt_audio)
        self.tgt_player.setVideoOutput(self.video_tgt)

        self.ref_player.positionChanged.connect(self._on_position)
        self.slider.sliderPressed.connect(self._scrub_start)
        self.slider.sliderMoved.connect(self._scrub)
        self.slider.sliderReleased.connect(self._scrub_end)

        self.sync = QTimer(self)
        self.sync.timeout.connect(self._keep_sync)

        self.ref_player.setSource(QUrl.fromLocalFile(ref_path))
        self.tgt_player.setSource(QUrl.fromLocalFile(tgt_path))
        self._apply_audio()
        QTimer.singleShot(600, lambda: self._seek_to(self.start))

    # ---- the model ------------------------------------------------------
    def _plan(self):
        return self.plan_getter() or {}

    def _offset_at(self, t: float) -> float:
        segments = self._plan().get("segments") or []
        if not segments:
            return 0.0
        for segment in segments:
            if float(segment["t_start"]) <= t <= float(segment["t_end"]):
                return float(segment["offset"]) + float(segment["slope"]) * (
                    t - float(segment["t_start"]))
        first = segments[0]
        return float(first["offset"]) + float(first["slope"]) * t

    def _atempo_at(self, t: float) -> float:
        for segment in self._plan().get("segments") or []:
            if float(segment["t_start"]) <= t <= float(segment["t_end"]):
                return float(segment["atempo"])
        return 1.0

    def _duration(self) -> float:
        if self.ref_player.duration() > 0:
            return self.ref_player.duration() / 1000.0
        if self.tgt_player.duration() > 0:
            return self.tgt_player.duration() / 1000.0
        return 1.0

    # ---- audio switches -------------------------------------------------
    def _apply_audio(self):
        self.ref_audio.setMuted(not self.chk_ref_audio.isChecked())
        self.tgt_audio.setMuted(not self.chk_tgt_audio.isChecked())

    # ---- controls -------------------------------------------------------
    def _toggle(self):
        if self.ref_player.playbackState() == QMediaPlayer.PlayingState:
            self.ref_player.pause()
            self.tgt_player.pause()
            self.sync.stop()
            self.btn_play.setText("Play")
        else:
            self.ref_player.play()
            self.tgt_player.play()
            self.sync.start(400)
            self.btn_play.setText("Pause")

    def _seek_to(self, t: float):
        duration = self._duration()
        self.start = max(0.0, min(float(t), duration))
        self.sync.stop()
        self.ref_player.pause()
        self.tgt_player.pause()
        self.ref_player.setPosition(int(self.start * 1000))
        self.tgt_player.setPosition(int((self.start + self._offset_at(self.start) / 1000.0) * 1000))
        try:
            self.tgt_player.setPlaybackRate(self._atempo_at(self.start))
        except Exception:
            pass
        self.ref_player.play()
        self.tgt_player.play()
        self.sync.start(400)
        self._show_status(self.start)

    def _jump(self):
        target = parse_timecode(self.time_edit.text())
        if target is None:
            self.status.setText("type 1:47:20")
            return
        self._seek_to(target)

    def _scrub_start(self):
        self._scrubbing = True
        self._was_playing = self.ref_player.playbackState() == QMediaPlayer.PlayingState
        self.sync.stop()
        self.ref_player.pause()
        self.tgt_player.pause()

    def _scrub(self, value):
        duration = self._duration()
        self.start = duration * value / 1000.0
        self.ref_player.setPosition(int(self.start * 1000))
        self.tgt_player.setPosition(int((self.start + self._offset_at(self.start) / 1000.0) * 1000))
        self._show_status(self.start)

    def _scrub_end(self):
        self._scrubbing = False
        if self._was_playing:
            self.ref_player.play()
            self.tgt_player.play()
            self.sync.start(400)

    def _on_position(self, position):
        if self._scrubbing:
            return
        now = position / 1000.0
        self.slider.setValue(int(max(0.0, min(1.0, now / self._duration())) * 1000))
        self._show_status(now)

    def _keep_sync(self):
        if self.ref_player.playbackState() != QMediaPlayer.PlayingState:
            return
        now = self.ref_player.position() / 1000.0
        wanted = (now + self._offset_at(now) / 1000.0) * 1000
        if abs(self.tgt_player.position() - wanted) > 120:
            self.tgt_player.setPosition(int(wanted))

    def _shutdown(self):
        """Stop both players and detach the sources before the window goes."""
        try:
            self.sync.stop()
        except Exception:
            pass
        for player in (self.ref_player, self.tgt_player):
            try:
                player.stop()
                player.setSource(QUrl())
            except Exception:
                pass

    def closeEvent(self, event):              # noqa: N802 - Qt naming
        self._shutdown()
        super().closeEvent(event)

    def reject(self):
        self._shutdown()
        super().reject()

    def done(self, result):
        self._shutdown()
        super().done(result)

    def _show_status(self, now: float):
        self.status.setText(
            f"{format_timecode(now)} / {format_timecode(self._duration())}  ·  "
            f"offset {self._offset_at(now):+.0f} ms  ·  "
            f"atempo {self._atempo_at(now):.6f}")


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(build_icon(256))
        self.resize(1500, 900)
        self.setMicaEffectEnabled(True)
        self.setAcceptDrops(True)
        self._centred = False

        self.settings = QSettings("AudioDesync", "AudioDesyncFinder2")
        self.ref_path = ""
        self.tgt_path = ""
        self.points = []
        self.plan = None
        self.meta = {}
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()
        self.worker = None

        page = QWidget()
        page.setObjectName("page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        self.addSubInterface(page, None, APP_NAME)
        # a single page: no navigation pane at all
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.hide()
        self.titleBar.setDoubleClickEnabled(True)

        # ---- files
        files = CardWidget()
        fl = QVBoxLayout(files)
        fl.setContentsMargins(14, 10, 14, 10)
        self.edit_ref = LineEdit(page)
        self.edit_tgt = LineEdit(page)
        self.radio_ref = RadioButton("Base")
        self.radio_ref.setChecked(True)
        for label, edit, is_ref in (("File 1:", self.edit_ref, True),
                                    ("File 2:", self.edit_tgt, False)):
            row = QHBoxLayout()
            row.addWidget(StrongBodyLabel(label))
            row.addWidget(edit, 1)
            browse = PushButton("Browse")
            browse.clicked.connect(lambda _=False, e=edit: self._browse(e))
            row.addWidget(browse)
            if is_ref:
                row.addWidget(self.radio_ref)
            else:
                radio = RadioButton("Base")
                radio.toggled.connect(lambda checked: self.radio_ref.setChecked(not checked))
                row.addWidget(radio)
            fl.addLayout(row)
        self.info = BodyLabel("")
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fl.addWidget(self.info)
        layout.addWidget(files)

        # ---- settings
        controls = CardWidget()
        cl = QHBoxLayout(controls)
        cl.setContentsMargins(14, 10, 14, 10)
        self.edit_block = LineEdit(page)
        self.edit_block.setFixedWidth(70)
        self.edit_block.setText(str(int(BLOCK_S)))
        self.edit_block.setValidator(QIntValidator(10, 600, self))
        self.edit_window = LineEdit(page)
        self.edit_window.setFixedWidth(70)
        self.combo_mode = ComboBox(page)
        self.combo_mode.addItems(["Auto", "Envelope", "Waveform"])
        self.combo_mode.setToolTip("Auto uses the envelope unless the two files "
                                   "share a language")
        self.edit_window.setText(str(int(FFT_WINDOW_S)))
        cl.addWidget(StrongBodyLabel("Block (s)"))
        cl.addWidget(self.edit_block)
        cl.addWidget(StrongBodyLabel("Window (s)"))
        cl.addWidget(self.edit_window)
        cl.addWidget(StrongBodyLabel("Signal"))
        cl.addWidget(self.combo_mode)
        self.edit_cores = LineEdit(page)
        self.edit_cores.setText(str(max(1, (os.cpu_count() or 4) - 2)))
        self.edit_cores.setFixedWidth(60)
        self.edit_cores.setValidator(QIntValidator(1, max(1, os.cpu_count() or 1), self))
        cl.addWidget(StrongBodyLabel("Cores"))
        cl.addWidget(self.edit_cores)
        self.chk_single = CheckBox("Single line")
        self.chk_single.setToolTip("One offset and one drift for the whole film - "
                                   "no jumps at all")
        cl.addWidget(self.chk_single)
        cl.addStretch(1)
        self.btn_scan = PrimaryPushButton("Scan")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_stop = PushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_scan)
        self.btn_stop.setEnabled(False)
        self.btn_cache = PushButton("Clear cache")
        self.btn_cache.clicked.connect(self.on_clear_cache)
        cl.addWidget(self.btn_scan)
        cl.addWidget(self.btn_stop)
        cl.addWidget(self.btn_cache)
        layout.addWidget(controls)

        # ---- chart + table
        middle = QHBoxLayout()
        chart_card = CardWidget()
        chl = QVBoxLayout(chart_card)
        chl.setContentsMargins(10, 8, 10, 8)
        self.figure = Figure(figsize=(7, 4), facecolor="#16181d")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        chl.addWidget(self.canvas)
        self._pan_start = None
        self._xlim = None
        self._ylim = None
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        middle.addWidget(chart_card, 3)

        table_card = CardWidget()
        tl = QVBoxLayout(table_card)
        tl.setContentsMargins(10, 8, 10, 8)
        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Time", "Offset (ms)", "Support", "Kind"])
        self.table.setSelectionBehavior(TableWidget.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        tl.addWidget(self.table)
        middle.addWidget(table_card, 2)
        layout.addLayout(middle, 1)

        # ---- summary + log
        bottom = QHBoxLayout()
        model_card = CardWidget()
        ml = QVBoxLayout(model_card)
        ml.setContentsMargins(14, 10, 14, 12)
        ml.addWidget(StrongBodyLabel("Fitted model"))
        self.summary = StrongBodyLabel("")
        ml.addWidget(self.summary)
        self.quality = BodyLabel("")
        ml.addWidget(self.quality)
        self.mux = BodyLabel("")
        self.mux.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ml.addWidget(self.mux)
        self.model_view = TextEdit()
        self.model_view.setReadOnly(True)
        ml.addWidget(self.model_view)
        bottom.addWidget(model_card, 1)

        log_card = CardWidget()
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(14, 10, 14, 12)
        ll.addWidget(StrongBodyLabel("Log"))
        self.log_view = TextEdit()
        self.log_view.setReadOnly(True)
        ll.addWidget(self.log_view)
        bottom.addWidget(log_card, 1)
        layout.addLayout(bottom, 1)

        # the bar gets a row of its own: sharing the buttons' row squeezed it to
        # a four pixel sliver and nobody could see the progress at all
        self.progress = ProgressBar()
        self.progress.setRange(0, 1000)      # 0.1 % steps, so the bar creeps
        self.progress.setValue(0)
        self.progress.setFixedHeight(8)
        self.progress.setMinimumWidth(300)
        layout.addWidget(self.progress)

        footer = QHBoxLayout()
        self.status = BodyLabel("Ready")
        footer.addWidget(self.status)
        footer.addStretch(1)
        self.btn_delete = PushButton("Delete point")
        self.btn_delete.clicked.connect(self.delete_point)
        footer.addWidget(self.btn_delete)
        self.btn_play_here = PushButton("Play from here")
        self.btn_play_here.clicked.connect(self.play_here)
        footer.addWidget(self.btn_play_here)
        self.btn_sample = PushButton("Sample")
        self.btn_sample.clicked.connect(self.sample)
        footer.addWidget(self.btn_sample)
        self.btn_preview = PushButton("Preview")
        self.btn_preview.clicked.connect(self.preview)
        footer.addWidget(self.btn_preview)
        self.btn_export = PrimaryPushButton("Export corrected audio")
        self.btn_export.clicked.connect(self.export_audio)
        self.btn_export.setEnabled(False)
        footer.addWidget(self.btn_export)
        layout.addLayout(footer)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(120)
        self._draw()

    # ------------------------------------------------------------- helpers
    def log(self, text: str) -> None:
        self.log_view.append(text)

    def _pump(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(payload)
            elif kind == "status":
                self.status.setText(payload)
            elif kind == "progress":
                value, text = payload
                self.progress.setValue(int(round(float(value) * 10.0)))
                if text:
                    self.status.setText(text)
            elif kind == "points":
                self.points = payload
                self._fill_table()
                self._draw()
            elif kind == "plan":
                self.plan = payload
                self.model_view.setPlainText("\n".join(plan_lines(payload)))
                self.summary.setText(self._summary_text(payload))
                self.quality.setText(quality_text(payload))
                self.mux.setText(mux_text(payload))
                self.btn_export.setEnabled(bool(payload.get("segments")))
                self._draw()
            elif kind == "done":
                self.btn_scan.setEnabled(True)
                self.btn_stop.setEnabled(False)
                self.progress.setValue(1000)
            elif kind == "export_done":
                self.btn_export.setEnabled(True)
                self.progress.setValue(1000)

    def _centre_on_screen(self) -> None:
        """Put the window in the middle of the available screen, once."""
        if self._centred:
            return
        self._centred = True
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # never open bigger than the screen itself, or the centre is off-screen
        width = min(self.width(), available.width() - 80)
        height = min(self.height(), available.height() - 80)
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def showEvent(self, event):               # noqa: N802 - Qt naming
        super().showEvent(event)
        self._centre_on_screen()

    @staticmethod
    def _num(widget, default: float) -> float:
        """The number in a line edit, or the default when it is empty."""
        try:
            value = float(widget.text())
        except (TypeError, ValueError):
            return float(default)
        return value

    # ---- drag and drop ---------------------------------------------------
    @staticmethod
    def _dropped_paths(event):
        paths = []
        for url in (event.mimeData().urls() or []):
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                paths.append(path)
        return paths

    def _highlight(self, target=None) -> None:
        """Light up the field a dropped file would land in."""
        style = "border: 1px solid #4ea1ff;"
        self.edit_ref.setStyleSheet(style if target is self.edit_ref else "")
        self.edit_tgt.setStyleSheet(style if target is self.edit_tgt else "")

    def _field_at(self, event):
        """The path field under the pointer, if any."""
        try:
            point = event.position().toPoint()
        except Exception:
            return None
        widget = self.childAt(point)
        while widget is not None:
            if widget is self.edit_ref:
                return self.edit_ref
            if widget is self.edit_tgt:
                return self.edit_tgt
            widget = widget.parentWidget()
        return None

    def _default_field(self):
        """Where a drop goes when the pointer is not over one of the fields."""
        if not self.edit_ref.text().strip():
            return self.edit_ref
        if not self.edit_tgt.text().strip():
            return self.edit_tgt
        return self.edit_tgt

    def dragLeaveEvent(self, event):          # noqa: N802 - Qt naming
        self._highlight(False)

    def dragEnterEvent(self, event):          # noqa: N802 - Qt naming
        if self._dropped_paths(event):
            self._highlight(self._field_at(event) or self._default_field())
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):           # noqa: N802 - Qt naming
        if self._dropped_paths(event):
            self._highlight(self._field_at(event) or self._default_field())
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):               # noqa: N802 - Qt naming
        paths = self._dropped_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        target = self._field_at(event)
        self._highlight(None)
        if len(paths) >= 2:
            self.edit_ref.setText(paths[0])
            self.edit_tgt.setText(paths[1])
        else:
            field = target or self._default_field()
            field.setText(paths[0])
        self.log("Dropped: " + ", ".join(os.path.basename(p) for p in paths[:2]))
        self._check_ready()

    def _browse(self, edit: LineEdit) -> None:
        start = str(self.settings.value("last_dir", "") or "")
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file", start,
                                              "Video/audio (*.mkv *.mp4 *.m4a *.mka *.avi *.wav *.flac);;All files (*)")
        if path:
            edit.setText(path)
            self.settings.setValue("last_dir", os.path.dirname(path))
            self._check_ready()

    def _check_ready(self) -> None:
        self.ref_path = self.edit_ref.text().strip()
        self.tgt_path = self.edit_tgt.text().strip()
        if self.ref_path and self.tgt_path and os.path.exists(self.ref_path) \
                and os.path.exists(self.tgt_path):
            self._load_meta()

    def _load_meta(self) -> None:
        key = (self.ref_path, self.tgt_path)
        if key == getattr(self, "_meta_key", None):
            return
        self._meta_key = key
        try:
            first = probe(self.ref_path)
            second = probe(self.tgt_path)
        except Exception as error:              # noqa: BLE001 - shown to the user
            self.log(f"ffprobe failed: {error}")
            return
        self.meta = {"ref_duration": first["duration"],
                     "tgt_duration": second["duration"],
                     "ref_fps": first["fps"], "tgt_fps": second["fps"]}
        self.log(f"Reference: {first['duration']:.0f} s at {first['fps'] or 0:.3f} fps")
        self.log(f"Target:    {second['duration']:.0f} s at {second['fps'] or 0:.3f} fps")
        self._update_info()

    def _update_info(self) -> None:
        """Framerate of each file, and what the pair implies - nothing else."""
        if not self.meta:
            self.info.setText("")
            return
        ref_fps = self.meta.get("ref_fps")
        tgt_fps = self.meta.get("tgt_fps")
        first = (f"File 1: {ref_fps:.3f} fps" if ref_fps
                 else f"File 1: {self.meta.get('ref_duration', 0):.0f} s")
        second = (f"File 2: {tgt_fps:.3f} fps" if tgt_fps
                  else f"File 2: {self.meta.get('tgt_duration', 0):.0f} s")
        slope = fps_slope(ref_fps, tgt_fps)
        tail = (f"drift {slope:+.1f} ms/s (atempo {1.0 + slope / 1000.0:.6f})"
                if slope else "same framerate")
        self.info.setText(f"{first}      {second}      {tail}")

    # -------------------------------------------------------------- tasks
    def start_scan(self) -> None:
        self._check_ready()
        if not (self.ref_path and self.tgt_path):
            MessageBox("Pick both files first.", APP_NAME, self).exec()
            return
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.progress.update()
        self.worker = threading.Thread(target=self._scan_task, daemon=True)
        self.worker.start()

    def stop_scan(self) -> None:
        self.stop_event.set()
        self.ui_queue.put(("status", "Stopping…"))

    def _scan_task(self) -> None:
        try:
            started = time.time()
            self.ui_queue.put(("status", "Decoding…"))
            cores = int(self._num(self.edit_cores, max(1, (os.cpu_count() or 4) - 2)))

            def decoding(base: float, span: float, label: str):
                def report(fraction: float) -> None:
                    value = base + span * max(0.0, min(1.0, fraction))
                    self.ui_queue.put(("progress", (
                        value, f"Decoding {label}… {100.0 * fraction:.0f} %")))
                return report

            # the status says "reference" and "target", never the file name: the
            # names are already in the fields above and only push the useful
            # information out of the bar
            ref = decode(self.ref_path, workers=cores,
                         on_fraction=decoding(0.0, 25.0, "reference"))
            tgt = decode(self.tgt_path, workers=cores,
                         on_fraction=decoding(25.0, 25.0, "target"))
            duration = min(len(ref), len(tgt)) / SCAN_SR
            block = max(10.0, float(self.edit_block.text() or BLOCK_S))
            # the correlation window and the step are the same thing in this
            # design; letting them differ measured 30 s of every 80 s block and
            # the model fell apart
            window = block
            self.edit_window.setText(str(int(block)))
            self.ui_queue.put(("log", f"Decoded: reference {len(ref) / SCAN_SR:.0f} s, "
                                      f"target {len(tgt) / SCAN_SR:.0f} s"))
            self.ui_queue.put(("log", f"Scanning {duration:.0f} s in blocks of "
                                      f"{block:.0f} s, window {window:.0f} s"))
            hint = fps_slope(self.meta.get("ref_fps"), self.meta.get("tgt_fps"))
            fps_atempo = 1.0
            if hint:
                # A framerate difference only means a speed change when the
                # running times agree with it.  A 30.000 fps flag on a 23.976
                # film implies -200 ms/s, which would squeeze the target to 80 %
                # of its length and destroy a pair that is actually in sync.
                ref_dur = float(self.meta.get("ref_duration") or 0.0)
                tgt_dur = float(self.meta.get("tgt_duration") or 0.0)
                if ref_dur > 60.0 and tgt_dur > 60.0:
                    expected = 1.0 + hint / 1000.0
                    actual = ref_dur / tgt_dur
                    if abs(actual - expected) > 0.02:
                        self.ui_queue.put(("log", (
                            f"The framerates disagree ({self.meta.get('ref_fps'):.3f}"
                            f" vs {self.meta.get('tgt_fps'):.3f}) but the running "
                            f"times do not ({actual:.4f} against the "
                            f"{expected:.4f} that would imply) - the fps drift is "
                            "ignored.")))
                        hint = 0.0
            if abs(hint) >= 5.0:
                rate = 1.0 + hint / 1000.0
                self.ui_queue.put(("status", "Bringing the target onto the "
                                             "reference clock…"))
                tgt = resample_track(tgt, rate)
                fps_atempo = rate
                duration = min(len(ref), len(tgt)) / SCAN_SR
                self.ui_queue.put(("log", (
                    f"The framerates differ: the target is resampled by "
                    f"{rate:.6f} first ({hint:+.1f} ms/s); the same factor is "
                    "applied again at export.")))
            env_a = envelope(ref)
            env_b = envelope(tgt)
            mode = {0: "auto", 1: "envelope", 2: "waveform"}.get(
                self.combo_mode.currentIndex(), "auto")
            points = scan(env_a, env_b, block=block, window=window, duration=duration,
                          mode=mode,
                          progress=lambda done, total: self.ui_queue.put(
                              ("progress", (50.0 + 45.0 * done / max(total, 1),
                                            f"Measuring {done}/{total} blocks"))),
                          should_stop=self.stop_event.is_set)
            if self.stop_event.is_set():
                self.ui_queue.put(("status", "Stopped"))
                self.ui_queue.put(("done", None))
                return
            if self.chk_single.isChecked():
                plan = single_line_plan(points, duration)
                if plan is None:
                    plan = fit_model(points, duration)
                else:
                    self.ui_queue.put(("log", "Single line: one offset and one "
                                              "drift, no jumps at all."))
            else:
                plan = fit_model(points, duration)
            plan["fps_atempo"] = fps_atempo
            holes = target_holes(ref, tgt)
            if holes and plan.get("jumps"):
                moved = []
                for jump in plan["jumps"]:
                    nearest = min(holes, key=lambda h: abs(h[0] - float(jump["t"])),
                                  default=None)
                    if nearest and abs(nearest[0] - float(jump["t"])) <= 180.0:
                        moved.append((round(float(jump["t"]), 1), round(nearest[0], 1),
                                      round(nearest[1] / 1000.0, 1)))
                        jump["t"] = round(nearest[0], 1)
                        for index, segment in enumerate(plan["segments"]):
                            if abs(float(segment["t_start"]) - moved[-1][0]) < 0.2:
                                segment["t_start"] = jump["t"]
                                if index:
                                    plan["segments"][index - 1]["t_end"] = jump["t"]
                                break
                for before, after, length in moved:
                    self.ui_queue.put(("log", (
                        f"Hole in the target at {format_timecode(after)} "
                        f"({length:.1f} s) - the cut moved there from "
                        f"{format_timecode(before)}.")))
            plan, cut_notes = refine_cuts_by_waveform(ref, tgt, plan)
            for note in cut_notes:
                self.ui_queue.put(("log", note))
            hole_plan = plan_from_holes(points, holes, duration)
            if hole_plan and not plan.get("jumps"):
                plan = hole_plan
                self.ui_queue.put(("log", (
                    f"The block model found no boundary, but the target has "
                    f"{len(hole_plan['segments']) - 1} digital hole(s): the plan "
                    "is built around them.")))
            # model_from_gaps() exists but is not used for the plan yet: on a real
            # pair the gap sequence alignment matches only half the gaps and the
            # deltas it reports swing by seconds, so the levels it builds are
            # worse than the block model.  It is logged for now, not trusted.
            gap_plan, _gap_notes = model_from_gaps(ref, tgt, points, duration)
            if gap_plan:
                self.ui_queue.put(("log", (
                    f"Black-insert model (diagnostic only, not used): "
                    f"{len(gap_plan['segments'])} segments.")))
            gaps_ref = black_gaps(ref)
            gaps_tgt = black_gaps(tgt)
            if gaps_ref or gaps_tgt:
                pairs, events = align_gaps(gaps_ref, gaps_tgt)
                self.ui_queue.put(("log", (
                    f"Black inserts: reference {len(gaps_ref)}, target "
                    f"{len(gaps_tgt)}, {len(pairs)} of them line up.")))
                for event in events:
                    if abs(event["delta"]) >= 500.0:
                        self.ui_queue.put(("log", (
                            f"  structural difference after "
                            f"{format_timecode(event['t'])}: the target gains "
                            f"{event['delta'] / 1000:+.2f} s there")))
            reliable = sum(1 for p in points if p.get("reliable"))
            self.ui_queue.put(("log", (
                f"Measured {len(points)} blocks, {reliable} of them the same audio "
                f"({100.0 * reliable / max(len(points), 1):.0f} %); the rest is "
                "dialogue and is interpolated.")))
            for line in plan_lines(plan):
                self.ui_queue.put(("log", line))
            for jump_index in range(1, len(plan.get("segments") or [])):
                for note in diagnose_jump(ref, tgt, plan, jump_index):
                    self.ui_queue.put(("log", note))
            self.ui_queue.put(("points", points))
            self.ui_queue.put(("plan", plan))
            self.ui_queue.put(("status", f"Done in {time.time() - started:.0f} s"))
        except Exception as error:              # noqa: BLE001 - shown to the user
            self.ui_queue.put(("log", f"Error: {error}"))
            self.ui_queue.put(("status", "Failed"))
        finally:
            self.ui_queue.put(("done", None))

    def delete_point(self) -> None:
        """Drop the measurement selected in the table and refit."""
        row = self.table.currentRow()
        if not (0 <= row < len(self.points)):
            self.status.setText("pick a row first")
            return
        del self.points[row]
        self._fill_table()
        self.ui_queue.put(("log", f"Removed the measurement at row {row + 1}."))

    def play_here(self) -> None:
        """Open the preview at the measurement selected in the table."""
        row = self.table.currentRow()
        if not (0 <= row < len(self.points)):
            self.status.setText("pick a row first")
            return
        self._open_preview_at(float(self.points[row]["t"]))

    def _open_preview_at(self, centre: float) -> None:
        if not HAVE_PLAYER:
            MessageBox("This build no QtMultimedia player.", APP_NAME, self).exec()
            return
        if not (self.ref_path and self.tgt_path):
            MessageBox("Pick both files first.", APP_NAME, self).exec()
            return
        dialog = PreviewDialog(self, self.ref_path, self.tgt_path,
                               lambda: self.plan, max(0.0, float(centre)))
        dialog.exec()

    def sample(self) -> None:
        if not (self.ref_path and self.tgt_path):
            MessageBox("Pick both files first.", APP_NAME, self).exec()
            return
        x0, x1 = self.ax.get_xlim()
        start = max(0.0, 0.5 * (float(x0) + float(x1)))
        dialog = SampleDialog(self, self.ref_path, self.tgt_path,
                              lambda: self.plan, start)
        dialog.exec()

    def preview(self) -> None:
        if not HAVE_PLAYER:
            MessageBox("This build has no QtMultimedia player.", APP_NAME, self).exec()
            return
        if not (self.ref_path and self.tgt_path):
            MessageBox("Pick both files first.", APP_NAME, self).exec()
            return
        x0, x1 = self.ax.get_xlim()
        start = max(0.0, 0.5 * (float(x0) + float(x1)))
        dialog = PreviewDialog(self, self.ref_path, self.tgt_path,
                               lambda: self.plan, start)
        dialog.exec()

    def on_clear_cache(self) -> None:
        files, freed = clear_cache()
        self.log(f"Cache cleared: {files} file(s), {freed / 1e6:.0f} MB freed.")

    def export_audio(self) -> None:
        if not (self.plan and self.plan.get("segments")):
            return
        start = os.path.join(str(self.settings.value("last_dir", "") or ""),
                             "corrected.mka")
        path, _ = QFileDialog.getSaveFileName(self, "Save corrected audio", start,
                                              "Matroska Audio (*.mka)")
        if not path:
            return
        self.btn_export.setEnabled(False)
        self.progress.setValue(0)
        self.ui_queue.put(("status", "Exporting…"))
        # an export of a three hour film takes minutes, so it runs off the
        # interface thread - doing it inline froze the window completely
        threading.Thread(target=self._export_task, args=(path,), daemon=True).start()

    def _export_task(self, path: str) -> None:
        try:
            export_plan(self.plan, self.tgt_path, path,
                        progress=lambda value, text: self.ui_queue.put(
                            ("progress", (value, text))))
            self.ui_queue.put(("status", "Measuring the result…"))
            residual = residual_of(self.ref_path, path)
            self.ui_queue.put(("log", f"Saved {path}"))
            if residual is not None:
                self.ui_queue.put(("log", (
                    f"Residual against the reference: {residual:+.1f} ms")))
            self.ui_queue.put(("status", "Exported"))
        except Exception as error:              # noqa: BLE001 - shown to the user
            self.ui_queue.put(("log", f"Export failed: {error}"))
            self.ui_queue.put(("status", "Export failed"))
        finally:
            self.ui_queue.put(("export_done", None))

    def _apply_time_axis(self) -> None:
        """
        Tick the time axis in round intervals of time, not in round seconds.

        A film is measured in minutes and hours, so the ticks follow the zoom:
        seconds when the view is a minute wide, hours when it is the whole film.
        """
        x0, x1 = self.ax.get_xlim()
        span = max(1.0, abs(x1 - x0))
        step = 5
        for candidate in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
                          3600, 7200, 18000):
            step = candidate
            if span / candidate <= 10:
                break
        self.ax.xaxis.set_major_locator(MultipleLocator(step))
        self.ax.xaxis.set_major_formatter(
            FuncFormatter(lambda value, _pos: format_timecode(value) if value >= 0 else ""))

    # ---- chart zoom and pan ---------------------------------------------
    def _on_scroll(self, event):
        """
        The wheel zooms around the pointer.

        Ctrl used to be required, but the modifier is not always reported when the
        wheel is over a widget that does not hold focus, so zooming silently did
        nothing.  Plain wheel zooms now; the modifier only makes it faster.
        """
        if event.inaxes is not self.ax or event.xdata is None:
            return
        fast = (event.key or "").lower().startswith("control")
        step = 0.6 if fast else 0.8
        factor = step if event.button == "up" else 1.0 / step
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        self.ax.set_xlim(event.xdata - (event.xdata - x0) * factor,
                         event.xdata + (x1 - event.xdata) * factor)
        if event.ydata is not None:
            self.ax.set_ylim(event.ydata - (event.ydata - y0) * factor,
                             event.ydata + (y1 - event.ydata) * factor)
        self._apply_time_axis()
        self.canvas.draw_idle()

    def _on_press(self, event):
        if event.inaxes is not self.ax or event.button != 1:
            return
        if event.dblclick:
            self._draw()
            return
        self._pan_start = (event.x, event.y)
        self._xlim = self.ax.get_xlim()
        self._ylim = self.ax.get_ylim()

    def _on_motion(self, event):
        if self._pan_start is None or event.x is None or event.y is None:
            return
        dx = event.x - self._pan_start[0]
        dy = event.y - self._pan_start[1]
        (x0, x1), (y0, y1) = self._xlim, self._ylim
        width = max(1.0, self.canvas.width())
        height = max(1.0, self.canvas.height())
        shift_x = dx / width * (x1 - x0)
        shift_y = dy / height * (y1 - y0)
        self.ax.set_xlim(x0 - shift_x, x1 - shift_x)
        self.ax.set_ylim(y0 - shift_y, y1 - shift_y)
        self.canvas.draw_idle()

    def _on_release(self, event):
        start = self._pan_start
        self._pan_start = None
        if start is None or event.x is None:
            return
        moved = abs(event.x - start[0]) + abs(event.y - start[1])
        if moved > 5 or event.inaxes is not self.ax:
            return
        nearest, best = None, 14.0
        for index, point in enumerate(self.points):
            px, py = self.ax.transData.transform((point["t"], point["offset"]))
            distance = ((px - event.x) ** 2 + (py - event.y) ** 2) ** 0.5
            if distance < best:
                nearest, best = index, distance
        if nearest is not None:
            self.table.selectRow(nearest)
            item = self.table.item(nearest, 0)
            if item is not None:
                self.table.scrollToItem(item)
            self.status.setText(
                f"selected {format_timecode(self.points[nearest]['t'])}  ·  "
                f"offset {self.points[nearest]['offset']:+.0f} ms  ·  "
                f"support {self.points[nearest].get('support', 0):.2f}")

    # ---- drawing ---------------------------------------------------------
    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.points))
        for row, point in enumerate(self.points):
            kind = "same audio" if point.get("reliable") else "dialogue"
            values = (self._timecode(point["t"]), f"{point['offset']:+.1f}",
                      f"{point.get('support', 0):.2f}", kind)
            for column, text in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(text))

    @staticmethod
    def _timecode(seconds: float) -> str:
        return format_timecode(seconds)

    def _summary_text(self, plan) -> str:
        segments = plan.get("segments") or []
        if not segments:
            return "no model"
        first = segments[0]
        if len(segments) == 1:
            return (f"offset {first['offset']:+.1f} ms   drift {first['slope']:+.3f} ms/s   "
                    f"atempo {first['atempo']:.6f}   RMS {first['rms']:.0f} ms")
        return (f"{len(segments)} segments   first offset {first['offset']:+.1f} ms   "
                f"RMS {first['rms']:.0f} ms")

    def _draw(self) -> None:
        self.ax.clear()
        self.ax.set_facecolor("#16181d")
        self.figure.patch.set_facecolor("#16181d")
        self.ax.tick_params(colors="#9aa4b8", labelsize=8)
        for spine in self.ax.spines.values():
            spine.set_color("#2a2f3a")
        self.ax.grid(True, color="#232833", linewidth=0.6)

        if self.points:
            good = [p for p in self.points if p.get("reliable")]
            bad = [p for p in self.points if not p.get("reliable")]
            if bad:
                self.ax.scatter([p["t"] for p in bad], [p["offset"] for p in bad],
                                s=12, color="#4a5163", label="dialogue (not evidence)")
            if good:
                self.ax.scatter([p["t"] for p in good], [p["offset"] for p in good],
                                s=18, color="#4ea1ff", label="same audio")
        if self.plan and self.plan.get("segments"):
            for segment in self.plan["segments"]:
                t0, t1 = segment["t_start"], segment["t_end"]
                y0 = segment["offset"] + segment["slope"] * (t0 - segment["t_start"])
                y1 = segment["offset"] + segment["slope"] * (t1 - segment["t_start"])
                self.ax.plot([t0, t1], [y0, y1], color="#5fd38d", linewidth=1.6)
        self.ax.axhline(0, color="#5a6273", linewidth=0.8, linestyle="--")
        self.ax.set_xlabel("film time", color="#9aa4b8", fontsize=8)
        self._apply_time_axis()
        self.ax.set_ylabel("offset (ms)", color="#9aa4b8", fontsize=8)
        if self.points:
            # above the plot, not inside it: a legend over the data hides exactly
            # the measurements it is describing
            legend = self.ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02),
                                    ncol=2, fontsize=7, frameon=False,
                                    labelcolor="#c7cedb")
        self.figure.tight_layout()
        self.canvas.draw_idle()


def main() -> int:
    global _instance_handle
    _instance_handle = acquire_single_instance()
    if _instance_handle is None:
        show_message(APP_NAME, f"{APP_NAME} is already running.\n"
                               "Close the other window first.")
        return 0
    if os.name == "nt":
        # without an explicit AppUserModelID Windows groups the window under
        # python.exe and shows the interpreter's icon in the taskbar
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Jank8.AudioDesyncFinder2")
        except Exception:
            pass
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
