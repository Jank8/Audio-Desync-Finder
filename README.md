# Audio Desync Finder 3.0

Finds and fixes the audio offset between two releases - different framerates,
different dubs, different edits - and exports a corrected Matroska Audio track.

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

Framerates are detected automatically; different fps are handled for you
(`atempo` is shown under the file names before the scan).

## Files

- `AudioDesyncFinder.pyw` - the application (single file)
- `legacy/audiosync3_tk.pyw` - the 2.x Tk version

## License

MIT
