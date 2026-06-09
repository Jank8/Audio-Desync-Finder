# Audio Desync Finder

A precise, lightweight GUI utility written in Python to detect, analyze, and calculate the exact audio offset (desynchronization) between two audio or video files.

---

## 🚀 Features

- **Advanced Signal Preprocessing** – Removes DC offset, normalises amplitudes, and applies a **4th-order Butterworth bandpass filter (100 Hz – 8 kHz)** to eliminate background rumble and high-frequency noise, isolating human speech and distinct transients for maximum accuracy.
- **Sub-Sample Precision** – Uses FFT-based cross-correlation combined with **parabolic peak interpolation** to determine the delay down to a fraction of a millisecond (well below the ~0.022 ms sample-rate limit of 44.1 kHz audio).
- **Interactive Waveform View** – Embedded dark-themed Matplotlib chart with zoom in/out, timeline scrolling, and live manual offset preview.
- **Pre-Offset Support** – Independent per-file time offsets let you account for known timecode differences before running the correlation; the tool automatically adds them back into the reported total adjustment.
- **Graceful Error Handling** – Missing FFmpeg or Python packages are detected at startup with clear, actionable dialog messages instead of cryptic tracebacks.
- **No Console Window** – Ships as a `.pyw` file; the Win32 console handle is suppressed explicitly so the tool behaves as a pure GUI application.

---

## 🛠️ Requirements

| Requirement | Notes |
|---|---|
| **Python 3.8+** | Tested with 3.10 and 3.11 |
| **FFmpeg** | Must be available in `PATH` (`ffmpeg.exe` on Windows) |
| **numpy** | Signal data handling |
| **scipy** | Bandpass filtering and FFT cross-correlation |
| **matplotlib** | Embedded waveform chart (TkAgg backend) |

Install the Python packages with:

```
pip install numpy scipy matplotlib
```

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
├── audiosync3.pyw   # Main application (single-file)
└── README.md
```

---

## 📄 License

MIT
