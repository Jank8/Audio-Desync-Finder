# Changelog

Every entry is a release of both builds: the Qt one is `3.0.x`, the Tk one
`2.0.x`. The `.x` is the same number in both.

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
