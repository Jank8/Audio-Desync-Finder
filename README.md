# Audio Desync Finder 3.0

Finds and fixes the audio offset between two releases - different framerates,
different dubs, different edits - and exports a corrected Matroska Audio track.

<img width="1860" height="1290" alt="obraz" src="https://github.com/user-attachments/assets/1f6b1696-263f-4e03-a2e3-3904a197fff8" />

<img width="1860" height="1290" alt="obraz" src="https://github.com/user-attachments/assets/d7c59edd-0c0f-40d1-a51c-cd7de95b3a10" />

## What it does

- scans the whole runtime and fits a piecewise-linear offset model,
- detects tempo drift (PAL 25 vs 23.976, 24 vs 23.976) and locks `atempo` to
  the exact fps ratio,
- detects edits (different intro, missing scene) and skips or pads audio at the
  boundaries,
- compares two language versions with energy-envelope correlation when the
  waveform does not match,
- exports `.mka` that needs no delay at mux time.

## Requirements

**Python 3.9+ only.** PySide6, PySide6-Fluent-Widgets, numpy, scipy and
matplotlib install themselves on first launch; FFmpeg is offered via winget.

## Usage

1. Run `AudioDesyncFinder.pyw`.
2. Pick both files and set **Base** on the one whose timeline you keep.
3. **Auto-scan whole file**, check the model, then **Export corrected audio**.
4. Mux the `.mka` with mkvmerge - no `--sync`, no delay.

## Controls

| Control | What it does |
|---|---|
| **File 1 / File 2** | the two media files; choosing one loads it immediately |
| **Base** | which file's timeline is the reference (the one you keep). The other becomes the target and is corrected. Framerates and the drift hint follow this choice |
| **Window (s)** | length of one analysis window. 40 s is a good default: longer is steadier on music-heavy material, shorter follows rapid edits |
| **Step (s)** | how often a window is measured. 30 s samples the offset twice a minute; smaller values give a denser curve but a slower scan |
| **Min confidence** | how sharp the correlation peak must be for a measurement to count. 2 is permissive, 5+ strict. Raise it when the chart looks scattered - weak points are dropped from the model, not from the chart |
| **Signal** | `Auto` tries the waveform first and falls back to the envelope; `Waveform` for same-language sources and music; `Envelope` for two different dubs |
| **Auto-scan whole file** | runs the scan; progress goes to the bar, messages to the log |
| **Stop** | stops a running scan or export |
| **Chart** | offset over time: dots are measurements, the green line is the fitted model |
| **Table** | every measurement with time, offset, confidence and source (`wave` / `env`) |
| **Delete selected point** | drops a bad measurement and re-fits the model at once |
| **Fitted model** | one line per segment: time range, offset at its start, drift in ms/s and the resulting `atempo` |
| **Export corrected audio** | writes the `.mka`, then measures it again and prints the residual offset in the log |
| **Line under the files** | detected framerates and the `atempo` they imply |

## Files

- `AudioDesyncFinder.pyw` - the application (single file, Python 3.9+)
- `legacy/audiosync3_tk-2.0.18.pyw` - the 2.x Tk version (numpy, scipy, matplotlib, FFmpeg)

Every release carries both files: the current app and the Tk fallback.

## License

Copyright (C) 2026 Janusz Woźniak - GNU GPL v3 or later - [LICENSE](LICENSE),
[full text](https://www.gnu.org/licenses/gpl-3.0.txt). Modified versions must
stay under the same license and ship their source.
