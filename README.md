# Audio Desync Finder

A precise, lightweight GUI utility written in Python to detect, analyze, and calculate the exact audio offset (desynchronization) between two audio or video files. 


---

## 🚀 Features

* **Zero-Configuration Setup:** Automatically detects and installs missing system dependencies, including **FFmpeg** (via Winget) and required Python packages (`numpy`, `scipy`, `matplotlib`) on launch.
* **Advanced Signal Preprocessing:** Removes DC offset, normalizes amplitudes, and applies a **4th-order Butterworth bandpass filter (100 Hz – 8 kHz)** to eliminate background rumble and high-frequency noise, isolating human speech and distinct transients for maximum accuracy.
* **Sub-Sample Precision:** Utilizes highly efficient **FFT-based cross-correlation** combined with **parabolic peak interpolation** to determine the absolute delay down to a fraction of a millisecond.
* **Interactive Visualization:** Embedded dark-themed **Matplotlib** waveform plot featuring smooth real-time zooming, timeline scrolling, and immediate manual offset adjustment previews.
* **Pre-Offset & Delay Calculations:** Allows inputting independent base offsets for both media files, automatically calculating the true total adjustment required.

---

## 🛠️ System Requirements

The script is tailored for **Windows** environments (`.pyw` execution hides the console window) and handles its own package management:

* **Python 3.x**
* **FFmpeg** (automatically installed via `winget` if not found in PATH)
* **Dependencies** (auto-installed via `pip` if missing):
  * `numpy`
  * `scipy`
  * `matplotlib`

---

## 📖 Technical Workflow

1. **Extraction:** The tool passes the user-defined start time and duration to FFmpeg to extract a lightweight, unified mono 16-bit PCM WAV stream at 44.1 kHz from both files.
2. **Filtering & Normalization:** Signals are zero-centered, scaled by their root-mean-square (RMS) energy, and bandpass filtered.
3. **Cross-Correlation:** The script computes the mathematical correlation across all potential lag points. The highest absolute peak indicates the point of alignment.
4. **Sub-Sample Refinement:** To surpass the physical limitation of the 44.1 kHz sampling rate (~0.022 ms boundaries), a parabolic fit is applied to the peak and its immediate neighbors to interpolate the true peak position.

---

## 💡 Usage Tips

### Optimal Selection:
* **Target 30–60 seconds** of media for the best balance between processing speed and correlation depth.
* Choose segments featuring **clear dialogue, distinct music beats, or sharp sound effects** (claps, impacts, title cards).
* When syncing secondary audio or foreign dubs to a high-quality video release, aim for scenes with identical background music or sound effects.

### Avoid:
* Regions with long periods of complete silence or non-stop heavy background noise (e.g., explosions, storms).
* Correlating completely different language dubs in dialogue-only scenes, as voice actors have vastly different speech cadences.
