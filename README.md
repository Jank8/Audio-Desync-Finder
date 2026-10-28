# Audio Desync Finder

A precise, lightweight GUI utility written in Python to detect, analyze, and calculate the exact audio offset (desynchronization) between two audio or video files.

---

## 🚀 Features

- **Advanced Signal Preprocessing** – Removes DC offset, normalises amplitudes, and applies a **4th-order Butterworth bandpass filter (100 Hz – 8 kHz)** to eliminate background rumble and high-frequency noise, isolating human speech and distinct transients for maximum accuracy.
- **Sub-Sample Precision** – Uses FFT-based cross-correlation combined with **parabolic peak interpolation** to determine the delay down to a fraction of a millisecond (well below the ~0.022 ms sample-rate limit of 44.1 kHz audio).
- **Interactive Waveform View** – Embedded dark-themed Matplotlib chart with zoom in/out, timeline scrolling, and live manual offset preview.
- **Whole-Track Timeline Sync** – Scans the entire runtime in one pass, plots offset vs. time, automatically detects edits (different intro, missing scene) and tempo drift, and exports a single corrected track that fixes all of them.
- **Pre-Offset Support** – Independent per-file time offsets let you account for known timecode differences before running the correlation; the tool automatically adds them back into the reported total adjustment.
- **Graceful Error Handling** – If the automatic installers cannot run (offline, no winget, restricted Python), the exact manual command and the installer log are shown instead of a cryptic traceback.
- **Automatic Dependency Installation** – On first launch the program checks for `numpy`, `scipy` and `matplotlib`; any that are missing are installed with `pip` automatically, with live output in a progress window. No terminal, no manual `pip install` step.
- **Automatic FFmpeg Setup** – If FFmpeg is missing it is installed with `winget` on startup, no button or confirmation needed. `PATH` is re-read from the registry, so a fresh install works immediately, without restarting the app.
- **No Console Window** – Ships as a `.pyw` file; the Win32 console handle is suppressed explicitly so the tool behaves as a pure GUI application.
- **Built-in Console Panel** – A live log column on the right shows FFmpeg commands, extraction progress, correlation status, and results. The same panel streams the automatic dependency and FFmpeg installers.

---

## 🛠️ Requirements

| Requirement | Notes |
|---|---|
| **Python 3.8+** | Tested with 3.10, 3.11 and 3.13 |
| **FFmpeg** | Installed automatically via `winget` when missing (see below) |
| **numpy**, **scipy**, **matplotlib** | Installed automatically on first launch (see below) |

### Automatic installation

You do **not** need to run `pip install` by hand. On startup the program checks
for `numpy`, `scipy` and `matplotlib`; whatever is missing is installed with
`pip` while a small window streams the installer output. The main window opens
as soon as the packages are ready.

The installer tries, in order, until one succeeds:

1. `pip install <packages>` — normal install, respects an active virtualenv
2. `pip install --user <packages>` — used when `site-packages` is not writable
3. `--user --break-system-packages` — only on Linux/macOS (PEP 668 systems)

If `pip` itself is absent, `ensurepip` bootstraps it first. When automatic
installation is impossible (offline machine, frozen executable, restricted
Python), a dialog with the exact manual command is shown instead.

To disable the automatic installer — e.g. for diagnostics or to manage the
packages yourself — set an environment variable before launching:

```
set ADS_SKIP_DEP_INSTALL=1
```

Manual install, if you prefer it:

```
pip install numpy scipy matplotlib
```

Install FFmpeg (if not already present):

```
winget install --id Gyan.FFmpeg -e
```

FFmpeg is installed **automatically**, no button and no confirmation step: if
none is found at startup, the app runs the winget command above and streams its
output into the console panel. Detection is repeated before every analysis, and
`PATH` is re-read from the registry each time, so an FFmpeg installed by a
just-finished winget run — or one you installed yourself outside the app — is
picked up immediately, without restarting the program.

If winget is unavailable or the install fails, the console panel shows the
exact manual command; the app stays usable and will use FFmpeg as soon as it
appears on `PATH`.

---

## ▶️ Usage

1. Run `audiosync3.pyw` (double-click or `python audiosync3.pyw`).
2. Click **Browse** to pick your two audio or video files.
3. *(Optional)* Set a **Pre-offset** (in seconds) for either file if you already know a rough time difference between the recordings.
4. Set the **Start time** and **Duration** of the clip to analyse (30–60 s recommended).
5. Choose which file is the **Base** (reference) using the radio buttons.
6. Click **⚡ ANALYZE**.
7. Read the offset result in the right-hand panel. The waveform chart shows both signals aligned according to the detected lag.
8. Use the **Manual offset** field to fine-tune the visual alignment and confirm it looks right before applying the value to your video editor.

---

## 🕒 Timeline Sync – whole-track analysis

The single-point workflow answers *"what is the offset here?"*. Real releases
often need more: different intros, removed scenes, re-timed recaps, or a target
that slowly drifts against the reference. **Timeline Sync** handles all of that.

Open it with **🔬 Timeline Sync (multi-point)** in the controls panel. The
window takes the two files already selected in the main window (*Base* = the
reference, the other = the target that gets corrected).

### The workflow

1. **⚡ Auto-scan whole file** – both soundtracks are decoded once to 16 kHz
   and the offset is measured at every *Step* seconds using a *Window*-second
   clip. A 2-hour film takes a few seconds; a 3-minute clip under one.
2. Look at the **offset-over-time chart**. Each blue point is one measurement,
   the green line is the fitted model, dashed orange lines mark edits.
3. **Click anywhere on the chart** to measure the offset at that moment, or
   type a time and a known offset and press **Add manual point** (useful when
   you already know e.g. "0 ms at 10 s, −1200 ms after the recap").
4. Check the **Fitted model** table – one row per segment, with its start
   offset, drift and resulting `atempo`.
5. **⬇ Export corrected audio…** writes a new file with all corrections
   applied. The original is never modified.

**Nothing freezes while it works.** Scanning and exporting run in background
threads, and the progress bar is fed by FFmpeg's own `-progress` output, so it
moves continuously through both export passes with the elapsed time next to it
(`47% – 61s`). The **Stop** button aborts a scan *or* an export in progress, and
the log stays live the whole time (worker threads never touch Tk directly –
messages are queued and flushed on the UI thread).

### How the model is read

| Measured | Meaning | Correction |
|---|---|---|
| offset > 0 | target is late at that moment | audio is advanced |
| drift > 0 | target gets later and later (plays slower) | `atempo = 1 + drift/1000` |
| jump > 0 | target has extra content (longer intro, inserted scene) | that stretch is skipped |
| jump < 0 | target is missing content | silence is inserted |

Points whose correlation peak is not clearly unique (silence, different
dubbing, heavy noise) get a low **Conf** value and are ignored by the fit;
adjust *Min confidence* to be stricter or more forgiving. The window is fully
interactive – delete points, disable them with **Enable/Disable**, or add your
own; the model and the chart update immediately.

**Save plan…** / **Load plan…** store the measurements and the fitted model as
JSON, so a long analysis does not have to be repeated.

### Two language versions (dubs)

Comparing two dubs is the normal case for this tool, and it is the hardest one:
the dialogue is re-recorded, so the waveform is *unrelated* even though the
music, the effects and the timing of the lines are identical.

The **Signal** selector controls how the two tracks are compared:

| Mode | Compares | Best for |
|---|---|---|
| **Waveform** | the actual samples | same source, music, effects, trailers |
| **Envelope (dubs)** | loudness over time (10 ms steps) | different languages, different voice actors |
| **Auto** *(default)* | waveform first, envelope as fallback | everything |

Auto uses the waveform result when it is convincing (a sharp, unique peak) and
switches to the envelope when it is not – typically in dialogue scenes with no
shared music, where waveform correlation produces noise but the rhythm of the
lines still matches. The **Source** column of the measurement table shows
`wave` or `env` for every point, so you always know which method produced it.

Practical notes for dubs:

- Use 40–60 s windows. Longer windows collect more shared music and effects.
- Music and action scenes give the most reliable points; pure dialogue is
  usually 5–10 ms less precise but still far below one video frame (40 ms).
- If a whole section ends up below the confidence threshold, raise the window
  size before lowering *Min confidence*.

### Drift compensation (automatic)

A tempo difference smears the correlation peak across the whole window, which
is exactly what makes "the speed is slightly off" cases unreliable. After the
first pass the app therefore resamples the target to the reference speed and
scans again; the peaks become sharp and the fitted drift is accurate to a few
hundredths of a ms/s. The measurements are converted back to the original
timeline, so the export still works on the untouched target.

Typical results on a synthetic 5-minute target with a 10 ms/s drift:
first pass ±100 ms (one point completely wrong), after compensation **±2 ms**
and a drift estimate of 10.097 vs. 10.101 ms/s true.

> Very large speed-ups (PAL 25 → 23.976 fps is ≈ 43 ms/s) smear a window so
> badly that the first pass may not lock at all. In that case add two manual
> points (e.g. 0 ms at 60 s and the known offset at the end), or compare a
> shorter, music-heavy section first.

### Framerate hint (optional, but recommended)

If you know both framerates, type them into the **Framerate** row of the scan
bar. Two things happen:

1. **The scan starts speed-compensated.** A PAL/NTSC difference
   (25 vs 23.976 fps ≈ 43 ms/s) smears a 40 s window beyond recognition, so
   without the hint the first pass simply cannot lock. With it, the target is
   resampled up front and only the small residual has to be measured.
2. **The drift is locked to the exact ratio.** When the fitted slope is close
   to `fps_ref / fps_target`, the model snaps to that rational value, which is
   far more precise than a regression on noisy measurements. Over a two-hour
   film a slope error of 0.05 ms/s is 360 ms of drift at the end.

Example: reference 23.976 fps, target 25 fps →

```
offset  +399.90 ms      drift  -40.9600 ms/s      atempo  0.95904000
```

The correction factor is always exactly `fps_ref / fps_target`, so the export
does not depend on the quality of the fit for the tempo part.

Leave both fields empty when the framerates are equal. If the hint does not fit
the material (e.g. a re-timed master), the app notices that the pre-warped scan
found almost nothing, re-runs it without the hint and keeps the measured drift.

### Practical tips

- 40 s windows / 30 s steps are a good default. Use shorter windows when the
  target has many cuts, longer ones when the audio is noisy.
- Around every detected jump the app automatically re-measures with 6 s
  windows, so the export cuts in the right place.
- If a segment looks wrong, drop the offending points and add two manual ones
  on either side of the problem area.

---

## 📖 How It Works

### 1. Extraction
FFmpeg extracts a short, mono, 16-bit PCM WAV stream at 44.1 kHz from each file, starting at `start_time + pre_offset` for the respective file.  The clips are written to the system temp directory.

### 2. Normalisation
Both signals are zero-centred (DC offset removed) and scaled by their RMS energy so that the cross-correlation measures waveform shape similarity rather than volume difference.

### 3. Bandpass Filtering
A 4th-order Butterworth bandpass filter (100 Hz – 8 kHz) is applied with `scipy.signal.sosfilt`.  This keeps the speech and music content while suppressing low-frequency rumble and ultrasonic artefacts.

### 4. FFT Cross-Correlation
`scipy.signal.correlate(..., method="fft")` computes the full cross-correlation sequence.  The index of the absolute maximum gives the integer-sample lag between the two clips.

### 5. Parabolic Interpolation
To go below the 1-sample resolution floor, a parabola is fit through the peak and its two immediate neighbours.  The vertex of the parabola is the refined sub-sample lag.

### 6. Offset Calculation
The refined lag is converted to milliseconds.  If pre-offsets were set, their difference is added back so the displayed result reflects the true timing gap in the original files.

---

## 💡 Usage Tips

### Best segments to choose:
- 30–60 seconds of content for a good balance of speed and accuracy.
- Scenes with **clear dialogue**, **distinct music beats**, or **sharp transients** (claps, door slams, title-card tones).
- Sections where both files contain identical background audio or sound effects.

### Avoid:
- Long periods of silence or continuous heavy noise (explosions, storms).
- Dialogue-only scenes from different-language dubs — voice cadences differ too much for reliable correlation.
- Very short clips (under ~5 s) that do not give the correlator enough data to find a unique peak.

---

## 📂 Project Structure

```
Audio Desync Finder/
├── AudioDesyncFinder.pyw   # 3.0 UI (PySide6 + Fluent, Mica)
├── sync_engine.py          # shared analysis engine
├── legacy/                 # the 2.x Tk single-file version
└── README.md
```

---

## 📜 Changelog

### 3.0

- **New UI: PySide6 + Fluent Widgets** with a real Windows 11 **Mica** backdrop,
  rounded cards and Fluent controls. No navigation pane, no hero header - a
  single screen that fills the window.
- **Engine extracted** to `sync_engine.py` (decoding, multi-point scan, model,
  export, residual measurement). Shared, GUI-free, importable.
- **Two files now**: `AudioDesyncFinder.pyw` (UI) + `sync_engine.py` (engine).
  The Tk version moved to `legacy/audiosync3_tk.pyw` and still works.
- Dependencies: `PySide6`, `PySide6-Fluent-Widgets` (plus numpy/scipy/matplotlib).

### 2.0.1

- "Load files" button removed - picking either file reloads the timeline.
- The theme follows the Windows setting: dark or light palette, title bar and
  sv-ttk theme switch together at startup.
- Windows 11 Mica backdrop requested through DWM (silently ignored on older
  builds); the title bar colour follows the system theme.

### 2.0

- **One screen** - the Quick tab and its single-point analyzer are gone. The
  file bar sits at the top and the Timeline panel takes the whole window.
- **Sun Valley (sv-ttk) dark theme** - native Windows 11 look for the ttk
  widgets, with the palette retuned to match.
- Picking both files loads the timeline automatically; framerates, drift hint
  and the self-aligning export work exactly as before.

### 1.0.3

- Framerate shown next to each file name in the Timeline header, so it is
  obvious which side is PAL and which is original (`[23.976 fps]` vs `[25 fps]`),
  with the resulting `atempo` at the end of the line.

### 1.0.2

- Help window rewritten for the current workflow (Quick sync, Timeline,
  auto-detected framerate, self-aligning MKA export).
- Timeline header shows both framerates and the resulting speed ratio.
- Version numbers now follow 1.0.x with x incrementing on every change.

### 1.0.1

- **Self-aligning export** – the produced file is measured and, if more than
  3 ms is left, the plan is shifted and the export repeated. No delay ever has
  to be typed into the muxer.
- **Automatic framerate detection** – both files are probed on load and the
  drift hint fills itself, so PAL pairs (25 vs 23.976) work with one click.

### 1.0 – first stable release

- **Automatic dependency installation** – missing Python packages are installed
  with pip on first launch, FFmpeg with winget; nothing has to be set up by hand.
- **Timeline Sync** – whole-track scan, offset curve, automatic detection of
  edits (jumps) and drift, piecewise-linear model, one-click export.
- **Dub-aware comparison** – energy-envelope mode for two different language
  versions, chosen automatically when waveform correlation is not convincing.
- **Framerate hint** – a known fps pair (e.g. 23.976 / 25) pre-compensates the
  scan and locks `atempo` to the exact rational ratio.
- **Matroska Audio output** – exports are always `.mka`, with a measured
  residual offset reported after each export.
- **Responsive tabbed UI** – Quick sync and Timeline tabs, shared console and
  progress bar, live FFmpeg progress and a working Stop button.

## 📄 License

MIT
