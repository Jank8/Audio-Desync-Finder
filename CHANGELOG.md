# Changelog

## 4.1.0

- **the whole waveform is followed, for every pair.** Instead of measuring a block
  every eighty seconds and fitting a model through those blocks, the offset is
  tracked second by second: each step takes six seconds of the reference, looks
  for it within +-1.5 s of the current offset, and holds the last value where it
  cannot find itself (re-recorded dialogue). The result is a curve, and the
  segments are the runs of that curve
  - a median filter over fifteen seconds removes the tracker's twitch without
    moving a real step
  - runs shorter than twenty-five measured seconds are absorbed by the neighbour
    with the closer level, so a blink cannot open a segment
  - neighbours that end on the same level within 200 ms are joined
  - every boundary is then snapped by measurement: the fade zone counts only
    within 45 s (three minutes away it belongs to another cut), otherwise
    `locate_cut()` searches +-90 s for the exact second
- measured on the goat pair: 5974 seconds followed in about 7 s, five segments,
  and the first cut lands at **10:03** where the block model said 11:20
- the block scan is still there as the fallback, and `Blocks only` forces it

## 4.0.15

- **a short window is called out.** With Block 10 and Window 10 the offsets scatter
  over tens of seconds - a ten second window cannot tell a real lag from a
  coincidence when the search spans +-90 s - the model collapses to one line and
  the real cuts disappear into the cloud. The scan now says so instead of quietly
  producing a meaningless result
- the table separates `off line` (a block that correlates well, support >= 0.5, but
  sits away from the fitted line) from `dialogue` (did not correlate at all); a
  row with support 0.99 used to say `dialogue`, which was simply wrong

## 4.0.14

- **the Window field keeps what you type.** The scan used to overwrite it with the
  block size on every run, so changing 80 to 30 snapped straight back to 80. The
  window is the correlation length and is independent of the block; it is only
  clamped (with a line in the log) when it would be longer than the block, because
  a window longer than the step averages the same audio twice
- verified: Block 80 with Window 30 scans `window 30 s` and the field still says 30

## 4.0.13

- **the scan no longer freezes after "Measuring 74/74 blocks".** Two causes:
  * `waveform_offset()` correlated a 30 s window against a 60 s search with a
    direct `np.correlate` - a trillion operations, minutes of work for one jump.
    It uses an FFT correlation now.
  * `black_gaps()`, `transition_zones()` and `target_holes()` each reshaped a
    whole track and averaged it per row - hundreds of megabytes per call, and they
    were called once per jump. They share `frame_levels()`, computed once per
    track, and the envelopes behind `locate_cut()` are cached per scan.
- measured on the goat pair: the same scan went from **191.9 s to 26.7 s**
- the chart, the table and the log now fill in straight after the measuring phase

## 4.0.12

- **a model that has to insert silence exports now.** It was refused because the
  first attempt drifted by 1.5 s; the cause was a duplicated second: each segment
  started at `t_start + offset` even when the target had already been played up to
  that point, so the overlap was played twice instead of being filled with silence
- `_segments_to_sources()` keeps a cursor of the target already used, works out how
  much is missing before each segment, and `_audio_filter()` adds that silence at
  the end of the previous part
- verified on both synthetic cases: a +3 s skip gives 236.6 s with 0 ms on both
  sides of the splice, and a -3 s cut gives a full 240.0 s with 0 ms on both sides
- the export is no longer refused for padding

## 4.0.11

- **a cut without a fade is located by the step itself.** `locate_cut()` measures a
  dozen seconds every six across three minutes either side of the boundary and
  finds the moment the level switches, for the cuts that no transition marks
- measured on the goat pair: the boundary the block grid gave (680 s) is wrong,
  and the step the audio actually shows is at **842 s** - the user sees a cut at
  10:05, but the correlation matches the first level right through to 14:02, so
  that cut is not an audio change at all
- segments built from two blocks (like that pair's 680-840 s stretch at -940 ms,
  RMS 0) are the weak spot: they look exact because a line through two points is
  exact, and they are what moves the boundary to the wrong place

## 4.0.10

- **the measurements around a cut are marked in the table.** For every boundary
  the nearest measurement before it and the nearest after it get a red dot in the
  first column and the kind `cut`, because those are the two that describe the
  step; a fixed +/-30 s window would miss the neighbour on one side, as the block
  grid is 80 s
- the row is repainted whenever a new plan arrives

## 4.0.9

- **the cut is measured 30 s to each side of it, and the target is cut to match
  the reference.** `refine_cuts_by_waveform()` takes thirty seconds of audio
  before the boundary and thirty after it, measures the offset in each, and sets
  the boundary and both levels from those two numbers - the amount removed is
  their difference, measured, not inferred from an 80 s block
- the raw waveform is tried first (it gives sample precision when both files come
  from the same mix), but two different masters do not share samples: on the
  Return of the King pair the waveform support is 0.03-0.04 while the envelope
  locks at 0.92/0.79, so the envelope is the fallback
- measured on that pair: `cut at 2:07:38: 5.03 s removed, offset +0 -> +5030 ms`
  - the cut now carries its own numbers instead of inheriting the block's +60/+4890

## 4.0.8

- **the cut is measured on the waveform, on both sides of the transition.**
  `refine_cuts_by_waveform()` takes a short window immediately before and after the
  detected fade zone, measures the offset in each, and sets the boundary to the
  start of the zone with the two levels taken from those windows - so the amount
  skipped is their difference, measured, not a block's guess
- it only fires when both windows lock (support >= 0.25); a note says what it did
- measured on the Return of the King pair: the window *before* the fade locks at
  +0 ms with support 0.94, but every window *after* it fails (offsets -2540, -3000,
  +2550 with support 0.30 down to 0.13). The audio after that transition simply
  does not match the reference there, which is the honest answer to "why is the
  jump 4.8 s while the fade is 2.8 s": the rest is a real difference between the
  two cuts, not a transition
## 4.0.7

- **fade-to-black transitions are measured, not guessed.** Looking for digital
  silence missed them: a lektor release replaces a hard cut with a fade out, a
  moment of black and a fade back, and the audio never reaches zero. `transition_zones()`
  finds where the target drops a dozen decibels below its own level while the
  reference keeps playing, widens the zone by 300 ms at both ends and reports it
  next to every jump
- on the Return of the King pair it reports `a fade-to-black zone starts at
  2:07:38 and lasts 2.8 s` while the model's jump there is +4830 ms - so 2.8 s of
  that jump is the dissolved transition and the remaining ~2 s is a genuine edit
  difference between the two cuts. The app now says both numbers instead of
  silently cutting 4.8 s

## 4.0.6

- **the decoding is now 3.1.x verbatim.** PCM on stdout, FFmpeg's progress on
  stderr (`-progress pipe:2 -stats_period 0.05`), a follower thread per slice that
  keeps `done[index]` up to date, and one throttled `report()` that sums the slice
  positions - so the bar moves while a slice is still decoding instead of jumping
  when it ends. Slices overlap by half a second and are trimmed to the exact sample
  count, and Stop kills them at once
- measured on the hour-long file: 22 callbacks in a 1.0 s decode, strictly rising
  from 0.001 to 1.0
- **only one copy can run.** A named Windows mutex is taken at start-up; a second
  window shows 'Audio Desync Finder is already running. Close the other window
  first.' and exits. Two copies used to fight over the same files and the same
  decoded-audio cache

## 4.0.5

- **the decoding finally moves the bar the way 3.1.x did.** The old build read
  FFmpeg's own `out_time_us=` lines inside every slice and reported the overall
  fraction up to thirty times a second; the rebuild only reported a slice when it
  finished, which on a four hour film is ten jumps and looks frozen
- each slice now decodes to a temporary raw file (stdout is taken by the progress
  stream) and reports continuously, throttled to ten updates a second
- measured on the hour-long test file: fourteen callbacks during a 0.9 s decode,
  and a four hour film will get hundreds

## 4.0.4

- **the progress bar works like it did in 3.1.x**: range 0-1000, so it creeps in
  tenths of a percent instead of jumping in whole tens, and it is refreshed the
  moment a scan starts
- the decoding phase says how many parallel slices it is working through
  (`Decoding in 10 parallel slices…`), so it is obvious that the silence is a
  long slice rather than a hung decode
- a cached track reports 100 % at once, because there is nothing to decode

## 4.0.3

- **the status bar says `reference` and `target`, never the file name.** The names
  are already in the fields above, and in the bar they only pushed the useful
  numbers out: `Decoding reference… 38 %` instead of the whole title of the film

## 4.0.2

- **the progress bar is visible.** It shared a row with the status text and the
  buttons and was squeezed to 111 x 4 pixels, so nothing could be seen even
  though the numbers were arriving (0, 25, 52 ... 100). It has its own row now,
  eight pixels tall and at least 300 wide

## 4.0.1

- the progress bar shows the decoding: every finished slice reports its share, so
  a four hour film says `Decoding reference… 38 %` instead of sitting at zero for
  a minute. The bar is split honestly - reference 0-25 %, target 25-50 %,
  measuring 50-95 %, done 100 %
- the window title carries the version, so it is obvious which build is running

## 4.0.0

The programme was rebuilt from scratch. Everything below the interface is new; the
interface itself is the same Fluent/Mica shell, now with the pieces that had been
added to it over time.

**The idea it is built on:** a dub replaces the dialogue and keeps the music,
effects and ambience. So a block that does not match is not a cut and not junk -
it is a block without evidence, and the offset across it is interpolated.

Engine:

- blocks of 80 s measured on a 10 ms envelope, with a **±90 s** lag search; the
  old narrow search followed the previous block, lost the lock on a real pair and
  reported the entire second half as dialogue
- evidence is chosen **by consensus** (blocks above half the film's median
  support, then a robust line with outliers dropped and refitted) instead of by a
  fixed contrast threshold, which threw away nine blocks in ten on real audio
- structure is found by **binary segmentation** on those blocks; a step needs at
  least 300 ms and has to halve the scatter of the stretch
- **digital holes**: stretches where the target sits at -90 dBFS while the
  reference has sound are black inserts or missing scenes. They need no
  correlation, so the cut is placed on them exactly - on the reference pair the
  hole is at 1:45:40 and lasts 6.2 s, and the boundary moved there from 1:47:20
- **framerate** is only believed when the running times agree with it; a 30.000 fps
  flag on a 23.976 film would otherwise squeeze the target to 80 % of its length
- **Single line** fits one robust line through every usable block, for material
  where the model invents structure
- export keeps the **source codec, sample rate and channel layout** (AC-3 5.1
  stays AC-3 5.1) and refuses a stepped model that would need padding

Interface:

- drag and drop into the field under the pointer, two files at once
- wheel zoom around the pointer, drag to pan, double click to restore, and an axis
  that ticks in minutes and hours instead of seconds
- clicking a point selects its row; `Delete point`, `Play from here`
- `Sample` draws both soundtracks stacked on one timeline so a hole is visible
- `Preview` plays both films side by side, with a switch for each soundtrack, and
  stops both players when it closes
- `Cores` for parallel decoding, `Signal` (Auto / Envelope / Waveform)
- export runs in the background with a progress bar and reports the residual
  against the reference
- the neon waveform icon is drawn in code, as before

Measured on the reference pair (extended 4K against a Polish lektor):

```
2 segments, 131 of 171 blocks the same audio
0-6340 s  +60 ms    6340 s onward  +7457 ms
cut placed on the digital hole at 1:45:40
```

Known limits: a model that has to insert silence is refused at export (only skips
are verified), and the model built from the black-insert sequence is a diagnostic
only.

---

# History

The entries below belong to the previous line (3.1.x / 2.1.x), kept for reference.


Every entry is a release of both builds: the Qt one is `3.0.x`, the Tk one
`2.0.x`. The `.x` is the same number in both.

## 3.1.26 / 2.1.26

- **`Delete off-line points` explains itself when a dense model hides the junk.**
  With many segments the model bends to follow its own points, so every row is
  legitimately "on the line" and the button answered "every point already sits on
  the line" - true, but useless. When that happens and the plan has five or more
  segments, it now re-compares everything against **one straight line** fitted
  through the points, says so in the log, and removes what disagrees with the
  film's overall behaviour. With a one-segment model nothing changes.

## 3.1.25 / 2.1.25

- **The dots are painted pixmaps now.** The table theme overwrites an item's
  foreground *and* its background, so a glyph and a coloured cell both came out
  invisible or plain. The status is a 14x14 circle drawn with QPainter and set as
  the item's icon: red for on the fitted line, blue for off it, which the theme
  does not touch at all. The column carries a tooltip saying so.

## 3.1.24 / 2.1.24

- **`Delete off-line points` no longer refuses when the model is empty.** It said
  "scan the files first" whenever `self.plan` had no segments - which also
  happens when a scan found nothing, or when the plan has not arrived yet. It now
  falls back to a line fitted through the points themselves
  (`single_line_plan`) and says so in the log, so the button works on any set of
  measurements. Only an empty table still stops it, with a message that says that.

## 3.1.23 / 2.1.23

- **`Delete off-line points`.** One button drops every measurement whose status
  cell is blue - the ones that do not sit on the fitted line - and refits the
  model from what is left, so the next comparison is made against the new lines
  and not the old ones. It refuses when there is no model yet or when nothing
  would be left, and logs how many were removed. Measured: 14 points with four
  deliberately shifted -> 10 left, one segment.

## 3.1.22 / 2.1.22

- **The dot is a coloured cell now.** Emoji glyphs did not render at all in the
  table (the theme's font has no emoji coverage), and a foreground brush is
  overpainted by the theme. The status cell therefore gets a **background**
  colour - red for on the fitted line, blue for off it - with a white dot in it,
  which no stylesheet foreground can take away. Tk keeps the plain dot with its
  row tag colours.

## 3.1.21 / 2.1.21

- **The dots are visible now.** A plain `●` coloured with a brush came out blue
  in every row: the table theme paints its own foreground over the item's. The
  dots are emoji now (🔴 on the fitted line, 🔵 off it), which carry their own
  colour and cannot be overpainted, plus a tooltip that says which is which.
  Verified: 13 red and 1 blue for a set with one deliberately shifted point.

## 3.1.20 / 2.1.20

- **The measurement list says which points are good.** A dot column in front of
  the time, red when the point sits on the fitted line and blue when it does not
  - the same rule and colour the chart uses, from `_point_colour()`. The time is
  tinted to match, so a bad row is visible without reading the offsets. The Tk
  build colours the whole row through two treeview tags.

## 3.1.19 / 2.1.19

- **`Anchors…`: measure only the start and the end.** One long window near each
  end, and the two offsets tell the whole story: the same value means the file
  only shifted (same edit, same speed), a different one is drift, and the
  difference over a known distance is the tempo. A constant 421 ms measures
  `+420.6 / +420.8 ms -> only a shift`; a deliberately drifting file measures
  `delta 8000 ms -> 36.4 ms/s`. The dialog can use the result as the plan - one
  line, no cuts, no inserted silence - and Tk has the same button behind a
  confirmation.
- `anchor_points()` is the engine behind it, shared by both builds.

## 3.1.18 / 2.1.18

- **A pad that a later skip undoes is removed.** Two versions of one film rarely
  need a hole: when the plan inserted 7.5 s at 2:34 and skipped 7.6 s at 2:42,
  the pair cancels out, but the listener heard seven seconds of silence followed
  by a jump. `collapse_cancelling_jumps()` folds the segments between such a
  pair back into one line and drops both jumps; a pair only qualifies when the
  deltas cancel within 250 ms and are at most 30 minutes apart, so a genuine
  missing scene stays.
- **`Single line` checkbox.** One offset and one drift for the whole film, built
  from all measurements with a Theil-Sen slope: no cuts, no silence inserted
  anywhere. Audio can then only walk out of sync gradually instead of jumping,
  which is the right answer both for a pure PAL speed-up and for material where
  the break detector keeps inventing edits.

## 3.1.17 / 2.1.17

- **A 24 -> 25 pair is recognised as a speed change.** `speed_hint()` compared
  the duration ratio with `fps_ref / fps_tgt`, but a target running at fps_tgt is
  `fps_ref / fps_tgt` **times longer**, so the ratio it implies is the inverse.
  The test only worked while both rates were nearly equal (23.976 vs 24 slips
  through either way); a 24 -> 25 pair read as "a different edit", the target was
  never pre-warped, and every 80 s window carried 3.3 s of drift inside it, which
  smeared every correlation into the 40 s cloud. Measured after the fix:
  `24 -> 25 = -40.0 ms/s` (atempo 0.960000), `23.976 -> 24 = -1.0 ms/s`,
  `25 -> 24 = +41.7 ms/s`, same framerate with a longer intro = no hint.

## 3.1.16 / 2.1.16

- **The model grades itself.** Under the headline numbers there is a plain
  sentence about the fit, built from the worst RMS of any segment:
  `good` up to 12 ms, `fair` up to 30 ms, `weak` above that - and `weak` says what
  to change (Window 80 / Step 80, or a higher Min confidence). The Tk build logs
  the same line. Without it, 80/80 (RMS 6 ms) and 60/60 (RMS 54 ms) look equally
  convincing on the chart.

## 3.1.15 / 2.1.15

- **A short step is called out.** `Step` far below `Window` (step < window / 3)
  means every measurement averages the same audio many times over: a sharp cut
  smears across many overlapping points and the break detector starts splitting
  the film - 80 / 10 turned one film into 86 segments with an RMS of 243 ms. The
  scan now says so in the log and names the sane range (80 / 30 measures clean).

## 3.1.14 / 2.1.14

- **`Play from here`.** Select a measurement in the table and the new button
  opens the preview at that exact second, so checking a suspicious point no
  longer means typing the time by hand. The preview button and this one share
  `_open_preview_at()`.

## 3.1.13 / 2.1.13

- **`Go to` no longer stalls the picture.** It seeked while both players were
  running, and a playing stream with a pending rate change is exactly what the
  FFmpeg backend chokes on. The jump now pauses the players, moves the positions,
  sets the rate only when it really changes and resumes - the click on the
  timeline was smooth because it went through the drag path, which already
  paused first. Measured: every jump lands in ~0.35 s and ends in playback.

## 3.1.12 / 2.1.12

- **Clicking the timeline now really seeks.** The first version passed
  `event.position().x()` (a float) straight into `sliderValueFromPosition()`,
  which expects integers: PySide raised inside the handler, Qt swallowed it, and
  the click did nothing at all. The coordinates are cast now, and a click emits
  its own `clickedAt` signal that seeks both players *and* starts playback, so it
  no longer depends on the drag state. Verified: a click at 60 % jumps to 4:00 of
  6:40 and plays.

## 3.1.11 / 2.1.11

- **The preview takes a typed time.** `Go to [1:07:15] [Go]` under the slider
  accepts `2:34`, `1:07:15`, `95` or `95.5` and jumps there; the parser
  (`parse_timecode()`) sits next to `format_timecode()` in both builds.
- **Clicking the slider jumps** instead of paging: a small `JumpSlider` subclass
  computes the value from the click position.
- **The status follows playback.** It used to be written once on seek; it now
  updates from `positionChanged`, so the timecode, the offset and the atempo
  move with the film.

## 3.1.10 / 2.1.10

- **Cuts are measured densely.** The refinement around a jump used to sample
  45 s either side with 6 s windows every 2 s; it now covers 60 s either side
  with 4 s windows every second - about 120 measurements around the splice
  instead of 45 - so the chart is packed exactly where the cut is and the
  breakpoint is visible to the second.
- The refinement runs on the Cores pool and measures against the same (possibly
  coarse-shifted) target as the scan, so its points land in the same frame
  instead of a shifted one.

## 3.1.9 / 2.1.9

- **The preview slider no longer stutters.** Dragging the handle used to seek
  both players *and* start them on every pixel, while the sync timer kept pulling
  the sound back into place - the result was a fight between the drag and the
  playback. The handle now takes the players off the clock (pause, timer
  stopped), moves only the position while dragged, and resumes on release with
  the sound already where the handle is.
- The Offset and FPS tempo export checkboxes are gone: the export always uses
  the fitted model again.
- Point measurement keeps using the Cores pool.

## 3.1.8 / 2.1.8

- **All cores but two, everywhere.** Cores already defaulted to `cpu_count - 2`
  (12 threads -> 10 on this machine) and drove the decoding slices and the new
  measurement pool; the Tk build still fell back to 4 when the field was empty,
  which quietly halved the scan. It falls back to the same number now.
- The scan line says what it uses: `Measuring every 30s (auto) on 10 threads…`,
  and the Cores tooltip names both jobs.

## 3.1.7 / 2.1.7

- **Measuring the points uses the cores too.** Decoding was parallel, the scan
  itself was a plain loop: one window after another. The windows are independent,
  so they now run in a pool of Cores threads (scipy's FFT releases the GIL) and
  the results come back in timeline order. Measured on an hour of audio:
  15.7 s -> 8.2 s, and the progress bar keeps moving because the work is handed
  over in batches of four per thread.
- The Cores setting drives both the decoding slices and the measurement pool.

## 3.1.6 / 2.1.6

- **The preview seeks the whole film.** The 30 s window is gone: the slider runs
  from the start to the end of the film, the buttons jump 10 minutes each way, and
  the status names the position as `5:00 / 1:30:46 · offset +0 ms · atempo
  1.000000`. Seeking stops at the ends instead of running past them.

## 3.1.5 / 2.1.5

- **Preview works before the first scan.** The button no longer waits for a plan:
  with nothing measured it plays the two files with no correction at all
  (`offset +0 ms, atempo 1.000000`), which is the quickest way to decide whether a
  pair is worth scanning at all. It only needs both files picked.

## 3.1.4 / 2.1.4

- **The last 20 scans are remembered, per pair and per settings.** The key holds
  both files (name, size, time) and every setting that shapes the result - Window,
  Step, Min confidence, Signal, Tempo and the framerate hint - so the same pair
  with different settings is a different entry. Asking again with the same pair
  and the same settings replays the stored points and plan in ~0.2 s instead of
  decoding and measuring for minutes: "Loaded the stored result - same pair, same
  settings."
- The store keeps 20 entries (200 MB cap, least recently used go first) in
  `%LOCALAPPDATA%\AudioDesyncFinder\results`, and "Clear cache" empties it
  together with the decoded audio.

## 3.1.3 / 2.1.3

- **The export no longer re-encodes the film for a few milliseconds.** The
  verification pass re-exported the whole file whenever the measured residual
  reached 3 ms, which is inaudible and doubled the export time. The bar is 10 ms
  now.
- A simple export (Offset / FPS tempo ticked) is never re-shifted: correcting the
  correction contradicts the checkbox you just set.

## 3.1.2 / 2.1.2

- **Closing the preview stops the sound.** The two players are children of the
  window, and Qt keeps them running while it hides: leaving the window with
  Close or Esc left the target audio playing behind the main window. Both are now
  stopped and their sources detached on close, on Esc and on accept.

## 3.1.1 / 2.1.1

- **The preview renders nothing.** The render fallback is gone: it encoded 30 s
  with libx264 and could pin the machine, while the live players cover the real
  case. If Qt cannot decode the file the window says so instead of starting an
  encode.
- Tk, which has no video widget, still renders its short clip, but lighter:
  15 s, ultrafast, crf 24, aac 160k.

## 3.1.0 / 2.1.0

- **The preview plays both files at once instead of rendering.** The reference
  gives the picture (muted), a second player gives the target sound, seeked to
  where the plan says its audio is; the plan's atempo becomes that player's
  playback rate and a timer nudges it back into place every 400 ms. Seeking is
  instant, nothing is written to disk first.
- Rendering the window stays as a fallback for files Qt cannot decode, and it is
  still what Tk uses (there is no video widget there).
- Measured on the test pair: both players running, sound 5 ms from where the plan
  puts it, status line showing `offset +421 ms, atempo 1.000000`.

## 3.0.99 / 2.0.99

- **The Preview button lights up.** 3.0.98 enabled it in only one of the two
  places the export button is enabled, so after a scan it stayed grey. It now
  follows the export button everywhere: scan result, deleted point, export run.

## 3.0.98 / 2.0.98

- **Preview: reference picture, target sound.** A "▶ Preview" button renders a
  30 s window around the middle of what the chart shows - video from the
  reference, audio from the target with the plan applied - and plays it.
  Qt uses a player built into the app (QtMultimedia): play/pause, a scrub bar and
  "30 s" jumps, with the next window rendered in the background while the current
  one plays. Tk renders the same clip and opens it in the system player.
- The preview uses the same plan the export would use, including the Offset and
  FPS tempo checkboxes, so what you watch is what you get.
- A reference without a video stream still previews as audio instead of failing.

## 3.0.97 / 2.0.97

- **Two export checkboxes: "Offset" and "FPS tempo".** Ticking one asks for a
  simple correction instead of the fitted model: one delay for the whole film
  and/or one atempo taken from the framerates, with no segments and no edit
  points. Both ticked gives exactly "offset plus fps tempo". Unticked (the
  default) the export uses the fitted model as before.
- The log names what was applied: `Simple export: offset +350.0 ms,
  atempo 0.99899900`. Both builds.

## 3.0.96 / 2.0.96

- **A wide RMS no longer means "wrong release".** The large notice fired when a
  segment fit worse than 80 ms RMS, which is simply what a different dub looks
  like - the envelope of the other language scatters by design. The notice needs
  evidence now: two or more cuts, 1.5 s of drift, or half a second of missing
  audio. The RMS is still named in the sentence when it does fire.

## 3.0.95 / 2.0.95

- **Vertical dragging follows the pointer.** Matplotlib reports `event.y` with
  the origin at the bottom of the display, so the vertical offset was added where
  it had to be subtracted and the chart moved the opposite way. Both builds.

## 3.0.94 / 2.0.94

- **Dragging is smooth.** The pan measured its delta in data units, which are
  derived from the limits it was changing at the same time: every pixel of travel
  altered how far the next pixel went, so the chart stuttered and accelerated.
  The delta is measured in pixels against the view captured at the press and
  converted once, so the movement follows the pointer exactly.

## 3.0.93 / 2.0.93

- **The legend is a row of labels in the card, not part of the chart.** It is
  built from the interface widgets now (coloured dot + caption), sits in the
  strip under the canvas and leaves the plot entirely to the data - the bottom
  margin went from 30 % to 22 %, so the chart is taller too. Both builds.

## 3.0.92 / 2.0.92

- **The legend sits in the free strip at the bottom of the chart card.** It is
  anchored in figure coordinates now, below the "Film time" title, so it uses
  the space the card already had instead of taking height away from the plot.

## 3.0.91 / 2.0.91

- **A 125 ms level change is not a cut.** 3.0.89 split the Smurfs pair at the
  hour mark, but a single delay muxes that film fine, so the bar for the local
  median break moves from 100 ms / 3 spreads to 150 ms / 4 spreads. A real edit
  (200 ms and up) still splits.

## 3.0.90 / 2.0.90

- **Drag to pan.** Holding the left mouse button on the chart moves the view with
  the pointer, in both builds, next to the Ctrl + wheel zoom. In Tk a click that
  did not move still measures at that timestamp; a drag no longer drops a point
  by accident.

## 3.0.89 / 2.0.89

- **Level changes are found against local medians.** The Smurfs pair sits at
  +105 for the first hour and -20 after, yet the model averaged the two into one
  line at +83: the break threshold grows with the scatter of the whole film, and
  both split passes needed a difference of twice the segment RMS, which the step
  itself inflates. A break is now also placed where the median of the ten
  measurements before a candidate differs from the ten after by 3x their own
  spread (and 100 ms).
- **Defaults changed**: Window 80 s, Min confidence 2.
- **The legend moved under the plot**, in one row, so it stops covering the
  points, and the axis title sits above it.
- **Ctrl + mouse wheel zooms** the chart around the pointer, in both builds.

## 3.0.88 / 2.0.88

- **The measurements that sit on the model are red.** A point is red when it is
  within half the segment's RMS of the fitted line (never less than 8 ms, never
  more than 30 ms) - that is the set the offset is actually built from -
  everything else keeps the accent colour. The legend gained "on the model", so
  the chart now answers "is the film really in sync at this moment" at a glance.

## 3.0.87 / 2.0.87

- **The chart says what it draws.** A legend now names the three things on it:
  the blue points are the measured target offsets, the green line is the fitted
  model (what the export applies), the grey dashed line is the reference at
  0 ms. The green line was previously unexplained, which made the whole plot
  read like two unrelated series.

## 3.0.86 / 2.0.86

- **The zero line is visible.** 3.0.85 kept zero inside the range but drew it as
  a dark dotted line on a dark background, which is invisible in practice. It is
  now a light dashed line labelled "reference 0 ms", so the distance from the
  reference to the target is obvious at a glance.

## 3.0.85 / 2.0.85

- **The reference is always on screen.** Zero is the reference track, but the
  chart range came from the 5th..95th percentile of the offsets, so with a
  target a second or ten seconds off the zero line fell outside the view and
  the cloud looked like it had no distance to anything. The range now always
  includes zero; the percentile logic still keeps junk points from stretching
  it.

## 3.0.84 / 2.0.84

- **The unmistakable matches decide the offset.** Every measurement already
  carries a confidence - how sharp and unambiguous its correlation peak was.
  That number is now the weight of the point (squared) in the group fit, both
  for the level and for the slope, so a window of identical music counts roughly
  eighty times more than a vague window of re-recorded dialogue
  (`9^2` against `2.5^2`).
- `_weighted_median()` and `_confidence_weights()` are shared by both builds;
  the plain median is still used when no confidence is available.

## 3.0.83 / 2.0.83

- **A split has to beat the noise.** On the Smurfs pair the model broke the
  timeline at 1:07 for a 62.8 ms difference while that segment's own RMS was
  65 ms - a cut smaller than the measurement scatter, which produced a pointless
  edit point and 62 ms of inserted silence. Both split passes now need a
  difference of at least max(25 ms / 40 ms, 2 x the segment's RMS).
- Measured: the noisy 63 ms step stays one segment with no jumps; a clean
  300 ms step still splits; the clean 34 ms weak tail in Cloudy still splits.

## 3.0.82 / 2.0.82

- **The axis title has room again.** The rotated timecode labels of 3.0.80 were
  drawn into the canvas margin, so "Film time" was clipped away on shorter
  windows. The plot now keeps a 26 % bottom strip (and a slightly wider left
  margin) for them, in both builds.

## 3.0.81 / 2.0.81

- **A jump that needs seconds of silence is a missing piece, not an offset.**
  The model can only keep the video in sync by padding the gap, so the export
  gets a hole - which is exactly what happened on Despicable Me 3: 10.0 s of
  silence at 1:07:15, and the muxed film fell apart there. The plan now says it
  in the report ("Jump at 1:07:15 needs 10.0 s of silence: the target is
  missing that content, not shifted") and in the large notice ("1 cut, 10.0 s
  missing at 1:07:15 - a different release of the target is recommended").
- A gap under half a second is still treated as an ordinary edit.

## 3.0.80 / 2.0.80

- **The time axis reads like a film, not like a number of seconds.** Ticks are
  now placed every 15 s to 30 min depending on the running time (about sixteen
  labels instead of ten), formatted `m:ss` / `h:mm:ss`, and rotated so they do
  not collide. The axis title is "Film time".
- **The points table shows the same timecode** instead of raw seconds, so a
  suspicious point can be found in a player directly. `format_timecode()` and
  `time_tick_step()` live in both builds' engines.

## 3.0.79 / 2.0.79

- **A whole-track offset has to show up in the durations.** A file that starts
  66 s late is also 66 s shorter, so a coarse alignment bigger than the
  difference in running time plus 20 s is ignored instead of being applied:
  "Coarse alignment -66450 ms ignored - the files differ by 15 s, so that is
  not a whole-track offset". 3.0.78 cleaned the score up, but a strong spurious
  peak still passed it on two dubs of the same cut.
- Measured with the bogus peak forced in: the model comes out at the real
  421.2 ms instead of -66450.

## 3.0.78 / 2.0.78

- **The coarse alignment can no longer run away.** The whole-track correlation
  was summed unnormalised and took the absolute value, so a lag with a short
  overlap - or an anti-correlation - could win. On two dubs of the same cut it
  reported -66 s, shifted the target before measuring and every later point
  then agreed with the shift. The score is now a mean product per overlapping
  sample, only positive correlation counts, lags with less than 40 % overlap
  are ignored, and a shift has to score 0.15+ and beat "already in sync" by
  15 %.
- Measured: 421 ms delay -> 420 ms; 24.6 s intro cut -> -24180 ms; identical
  tracks, unrelated noise and a 24.6 s adelay -> 0 ms.

## 3.0.77 / 2.0.77

- **The decode cache is per file, not per pair.** Scanning A against B and then
  A against C used to decode A twice, because the cache key held both paths.
  Each track is now keyed by its own name, size and modification time, so any
  file decoded once is never read again, whatever it is paired with. The log
  says `Reference: 5777 s of audio from the cache`.
- **The manual-mux line is no longer fine print**: 13 px semibold in the accent
  colour in Qt, 11 pt bold in Tk.

## 3.0.76 / 2.0.76

- **The weak level at the end gets a segment even when the strong points still
  reach the end.** 3.0.75 only split a tail when the supporting points stopped
  early; here they run to the end while the measurements below Min confidence
  sit ~35 ms away for the last fifteen minutes. Those weak points now split the
  last segment when they are tight (8+ points, 120 s+, spread <= 50 ms) and at
  least 25 ms away from the line.
- Measured: strong 494 with a weak 460 tail -> `0-2115 / 2115-4900 / 4900-5776
  (weak)`; a film without weak points still gives one segment.

## 3.0.75 / 2.0.75

- **A segment no longer claims time it has no measurements for.** When the
  points that support a segment stop early - the last fifteen minutes of a dub
  that sits at another level, measured below Min confidence - the tail becomes
  a segment of its own, placed from those weaker measurements and marked
  `weak` so it is not merged back into the line above it. The log says so:
  "Split 1 unsupported tail(s) off using the measurements below Min
  confidence."
- If nothing usable exists there, the segment keeps covering the film: a
  truncated model would cut the exported audio.

## 3.0.74 / 2.0.74

- the "different cut" notice uses the system accent colour instead of a fixed
  amber - the same accent the window already highlights with (`#f7630c` here),
  and `FG_ACCENT` in the Tk build.

## 3.0.73 / 2.0.73

- **A loud notice when the target is not the same cut.** Two or more edits, a
  drift of 1.5 s or more across the film, or a fit worse than 80 ms RMS now
  prints in 15 px amber above the model report, not in the 9 pt fine print:
  "The target's audio differs a lot from the reference (2 cuts, +5.5 s of
  drift). A different release of the target is recommended."
- The sentence comes from `mismatch_text(plan)`, so both builds word it the same.

## 3.0.72 / 2.0.72

- **The split is followed by a merge.** Splitting a segment for a rescued run
  can leave a piece on the same line as its neighbour - two points are enough to
  produce a `+0.6 ms -> skip audio` cut at the seam. The merge that used to run
  before the rescue now runs after it too, so the leftover is joined back and
  the pointless edit disappears.

## 3.0.71 / 2.0.71

- **The rescue split refits its pieces from their own measurements.** A piece
  with no points of its own no longer inherits the parent line - that is how the
  model ended up drawing -13778 over the last fifteen minutes while the points
  sat at -16000, which also produced a `+2719.6 ms -> skip audio` cut at the
  boundary. A piece without support is dropped and the rescued segment extends
  over that time instead.

## 3.0.70 / 2.0.70

- **Long runs the fit dropped are rescued.** A dub re-cut late in a film shows
  up as a tight band of points a second or two away from the model; the fit
  rejected them as outliers, the model kept drawing the old line to the end and
  the export drifted for the rest of the film. A run of 12+ unexplained points
  spanning 240 s+ with a spread under 50 ms now becomes a segment of its own and
  the covering segment is split around it.
- **Min confidence defaults to 4** instead of 2. The scans that worked used 4:
  at 2, two noisy measurements of a dub could get into the model.
- **`24` vs `23.976` is a real tempo.** It is 0.1 %, six seconds across a film,
  and it used to be thrown away because the duration difference was below the
  0.5 % gate - no hint, no pre-warp, a drift fitted from five points. The
  framerates now decide, and a segment resting on fewer than 20 points takes the
  known ratio instead of its own slope.
- **Framerates compare with a tolerance** (0.01 fps): two releases both showing
  `23.976` (`23.976000` vs `23.976024`) are the same rate, so the Tempo box is
  respected.
- **The "only N survived" warning** no longer fires on a healthy scan (44 points
  with RMS 2 ms is fine); it warns only when the model rests on fewer than ten
  measurements.
- **The window opens 1500 wide and centred** in both builds.

## 3.0.69 / 2.0.69

- **Tempo only when the framerates differ.** A drift has nowhere to come from
  when both files report the same rate, and fitting one turned 87 ms of
  measurement slop into a tempo that made the mux hint refuse a good delay. The
  new **Tempo** checkbox forces a drift check anyway. A suppressed drift is
  still reported when it is large.
- The refit inside outlier rejection respects the same rule, so "tempo off"
  really means a level even after a point is dropped.

## 3.0.68 / 2.0.68

- **The mux hint names the real reason** it refuses a single delay: `2 segments`,
  `1 edit point`, or `drift -0.016 ms/s, 87 ms over the film` - instead of
  claiming there is no straight line.

## 3.0.67 / 2.0.67

- The status line ends with **"Measuring done"** in both builds.

## 3.0.66 / 2.0.66

- Qt reports the end of a scan (`plan_slope` came back to the Qt build for it).

## 3.0.65 / 2.0.65

- **One drift rule for the model and the mux hint.** A drift below 40 ms across
  the whole file flattens (was 25), and the manual-mux hint uses the same rule,
  so it can no longer promise a delay while the export applies atempo.

## 3.0.64 / 2.0.64

- Outlier warnings show the range (`Dropped 48 outlier(s) in 0-10920s`) instead
  of the first point of the group.

## 3.0.63 / 2.0.63

- **The trend is the model.** The drift comes from the file's own Theil-Sen
  trend instead of the median of consecutive gaps, and a group shorter than ten
  minutes takes the trend and fits only its level. Breakpoints are detected on a
  five-point running median with a threshold that grows with the measured
  scatter, so 140 ms of dialogue scatter no longer splits a film into forty
  segments.

## 3.0.62 / 2.0.62

- **Points are judged against the file's trend.** `build_plan` fits the trend
  first (Theil-Sen on up to 400 sampled points) and measures every point against
  that line; the band is `max(350 ms, 6 x MAD)`. A run of 20 consecutive points
  off the trend is kept, so a re-timed recap or a second master survives.
