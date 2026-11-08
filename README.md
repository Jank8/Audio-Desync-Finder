# Audio Desync Finder 4

Measures how two releases of the same film are offset, and writes the corrected
audio back with the source's own codec.

This is a**rebuild**. The interface is the familiar Fluent/Mica shell; everything
under it was written again around one idea that the previous version kept getting
wrong:

> **A dub is a different recording of the dialogue and the same recording of
> everything else.** Music, effects and ambience survive the dubbing; the voices
> do not.

So the film is measured block by block, every block is asked one question —
*"is this the same audio at all?"* — and only the blocks that answer yes are
allowed to shape the model. Blocks of re-recorded dialogue are not junk and not
cuts: they are simply not evidence, and the offset across them is interpolated
from the music on either side.

## How it measures

1. **Blocks** (`Block`, 80 s by default): the 10 ms energy envelope of the
   reference is correlated against the target over a wide lag range (±90 s). Each
   block yields a lag, a *support* (how well it correlates) and a *contrast*.
2. **Evidence.** Blocks above half the film's median support are candidates; a
   robust line is fitted through them and blocks far off it are dropped and the
   line refitted. The survivors are what the chart draws in blue.
3. **Structure.** Boundaries are found by binary segmentation on those blocks —
   a step needs to be at least 300 ms and to halve the scatter of the stretch.
4. **Digital holes.** A stretch where the target is at −90 dBFS while the
   reference still has sound is a black insert or a missing scene; those are
   language independent, so they are used to place the cut exactly (the real pair
   has one at 1:45:40 lasting 6.2 s, and the cut lands on it).
5. **Framerate.** A difference in fps is only believed when the running times
   agree with it; otherwise it is a container flag and is ignored.
6. **Export** applies the model with ffmpeg, keeps the source's codec, sample rate
   and channel layout (AC-3 5.1 stays AC-3 5.1, AAC 2.0 stays AAC 2.0), and then
   measures the result against the reference.

## What you get

```
File 1: 23.976 fps      File 2: 23.976 fps      same framerate
2 segments, built from 131 of 171 measured blocks
Seg 1:      0.0-  6340.0s  offset    +60.0 ms  drift +0.000  [131 blocks, RMS 32 ms]
Seg 2:   6340.0- 13691.7s  offset  +7457.5 ms  drift +0.031
Quality: fair - a normal scatter for material with a voice on top
MKVToolNix: no single delay fits this model (2 segments) - export the corrected audio
```

and after an export:

```
Residual against the reference: +0.0 ms
```

## Interface

- **Dragging** files over the window drops each one into the field under the
  pointer; two at once fill File 1 and File 2.
- **Chart**: wheel zooms around the pointer, dragging pans, double click restores,
  the axis ticks in minutes and hours, and clicking a point selects its row.
- **Sample** draws the two soundtracks stacked on one timeline, so a hole in the
  target is an empty stretch of the lower trace.
- **Preview** plays both films side by side with a switch for each soundtrack.
- **Cores** drives the parallel decoding (the file is cut into slices and decoded
  in parallel).
- **Single line** replaces a model that invents structure with one offset and one
  drift.
- **Delete point**, **Play from here**, **Clear cache**, and an export that runs in
  the background with a progress bar.

## Running

```
pythonw AudioDesyncFinder.pyw
```

needs `ffmpeg` and `ffprobe` on `PATH`; the app installs the Python packages it
needs itself (PySide6, qfluentwidgets, numpy, scipy, matplotlib).

Decoded tracks are cached in `%LOCALAPPDATA%\AudioDesyncFinder2\cache` and removed
by `Clear cache`.

## Known limits

- a model that has to **insert silence** cannot be exported yet (only skips are
  verified end to end); the app says so rather than writing a wrong file,
- the model built from the black-insert sequence is a diagnostic only: matching
  49 gaps against 70 pairs the wrong ones and the levels do not hold.

See [CHANGELOG.md](CHANGELOG.md).
