# Changelog

Every entry is a release of both builds: the Qt one is `3.0.x`, the Tk one
`2.0.x`. The `.x` is the same number in both.

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
