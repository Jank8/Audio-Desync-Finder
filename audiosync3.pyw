import os
import sys
import subprocess
import tempfile
import shutil

# --- HIDE CONSOLE ---
if os.name == 'nt':
    import ctypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd != 0:
        ctypes.windll.user32.ShowWindow(hwnd, 0)
        ctypes.windll.kernel32.CloseHandle(hwnd)

def get_startupinfo():
    if os.name == 'nt':
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        return info
    return None

# --- AUTO-INSTALL FFMPEG ---
if not shutil.which("ffmpeg"):
    try:
        subprocess.check_call(["winget", "install", "Gyan.FFmpeg", 
            "--accept-source-agreements", "--accept-package-agreements"], 
            startupinfo=get_startupinfo())
        os.environ["PATH"] += os.pathsep + r"C:\Program Files\ffmpeg\bin"
    except: pass

# --- AUTO-INSTALL LIBRARIES ---
try:
    import numpy as np
    import scipy.signal as signal
    from scipy.io import wavfile
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", 
        "numpy", "scipy", "matplotlib"], startupinfo=get_startupinfo())
    import numpy as np
    import scipy.signal as signal
    from scipy.io import wavfile
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

import tkinter as tk
from tkinter import filedialog, messagebox

# --- GLOBAL VARIABLES ---
viz_data = {'data1': None, 'data2': None, 'sr': None, 'offset_samples': 0,
            'zoom_start': 0, 'zoom_end': None, 'canvas': None, 'fig': None, 'ax': None, 
            'scrollbar': None, 'scrollbar_frame': None, 'updating_scroll': False}

def to_seconds(t_str):
    if ":" in t_str:
        parts = t_str.split(":")
        if len(parts) == 3: 
            return int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
        elif len(parts) == 2: 
            return int(parts[0])*60 + float(parts[1])
    return float(t_str)

def update_visualization():
    if viz_data['data1'] is None or viz_data['data2'] is None:
        return
    
    ax = viz_data['ax']
    ax.clear()
    
    data1, data2 = viz_data['data1'], viz_data['data2']
    sr, offset_samples = viz_data['sr'], viz_data['offset_samples']
    start = viz_data['zoom_start']
    end = viz_data['zoom_end'] if viz_data['zoom_end'] else len(data1)
    
    time1 = np.arange(start, min(end, len(data1))) / sr
    
    if offset_samples >= 0:
        data2_shifted = np.pad(data2, (offset_samples, 0), 'constant')[start:end]
    else:
        data2_shifted = data2[-offset_samples:][start:end]
    
    min_len = min(len(time1), len(data1[start:end]), len(data2_shifted))
    time1 = time1[:min_len]
    plot_data1 = data1[start:start+min_len]
    plot_data2 = data2_shifted[:min_len]
    
    ax.plot(time1, plot_data1, color='#4a9eff', linewidth=0.8, alpha=0.7, label='File 1 (base)')
    ax.plot(time1, plot_data2, color='#ff6b6b', linewidth=0.8, alpha=0.7, label='File 2 (offset)')
    
    ax.set_xlabel('Time (s)', color='white', fontsize=9)
    ax.set_ylabel('Amplitude', color='white', fontsize=9)
    ax.legend(loc='upper right', fontsize=8, framealpha=0.8)
    ax.grid(True, alpha=0.2, color='white')
    ax.set_facecolor('#1a1a1a')
    ax.tick_params(colors='white', labelsize=8)
    
    viz_data['fig'].patch.set_facecolor('#1a1a1a')
    viz_data['canvas'].draw()
    
    # Update scrollbar visibility and range
    update_scrollbar()

def update_scrollbar():
    """Update scrollbar state based on zoom level"""
    if viz_data['data1'] is None or viz_data['scrollbar'] is None:
        return
    
    total_len = len(viz_data['data1'])
    current_range = viz_data['zoom_end'] - viz_data['zoom_start']
    
    # Always show scrollbar, but update its range
    if current_range < total_len:
        # Zoomed in - enable scrolling
        viz_data['scrollbar'].config(to=total_len - current_range, state=tk.NORMAL)
        viz_data['updating_scroll'] = True
        viz_data['scrollbar'].set(viz_data['zoom_start'])
        viz_data['updating_scroll'] = False
    else:
        # Full view - disable scrolling but keep visible
        viz_data['scrollbar'].config(to=1, state=tk.DISABLED)
        viz_data['scrollbar'].set(0)

def on_scroll(value):
    """Handle scrollbar movement"""
    if viz_data['data1'] is None or viz_data['updating_scroll']:
        return
    
    total_len = len(viz_data['data1'])
    current_range = viz_data['zoom_end'] - viz_data['zoom_start']
    
    # Update zoom window position
    viz_data['zoom_start'] = int(float(value))
    viz_data['zoom_end'] = min(total_len, viz_data['zoom_start'] + current_range)
    
    # Ensure we don't go past the end
    if viz_data['zoom_end'] >= total_len:
        viz_data['zoom_end'] = total_len
        viz_data['zoom_start'] = max(0, total_len - current_range)
    
    update_visualization()

def apply_manual_offset():
    try:
        manual_ms = float(entry_manual_offset.get())
        viz_data['offset_samples'] = int((manual_ms / 1000.0) * viz_data['sr'])
        update_visualization()
        
        offset_ms = round(manual_ms, 2)
        ref_file = ref_var.get()
        target_name = "File 2" if ref_file == 1 else "File 1"
        
        if offset_ms == 0:
            res, color = "✓ Files perfectly synchronized!\nOffset: 0.00 ms", "#00ff88"
        elif offset_ms > 0:
            res = f"⏱️ {target_name} IS DELAYED\nOffset: +{offset_ms} ms\n→ Shift forward by {offset_ms} ms"
            color = "#ffa500"
        else:
            res = f"⏱️ {target_name} IS AHEAD\nOffset: {offset_ms} ms\n→ Delay by {abs(offset_ms)} ms"
            color = "#ff6b6b"
        
        label_result.config(text=res, fg=color)
    except ValueError:
        messagebox.showerror("Error", "Invalid offset value!")

def zoom_in():
    if viz_data['data1'] is None: return
    current_range = viz_data['zoom_end'] - viz_data['zoom_start']
    new_range = int(current_range * 0.5)
    center = (viz_data['zoom_start'] + viz_data['zoom_end']) // 2
    viz_data['zoom_start'] = max(0, center - new_range // 2)
    viz_data['zoom_end'] = min(len(viz_data['data1']), center + new_range // 2)
    update_visualization()

def zoom_out():
    if viz_data['data1'] is None: return
    current_range = viz_data['zoom_end'] - viz_data['zoom_start']
    new_range = int(current_range * 2)
    center = (viz_data['zoom_start'] + viz_data['zoom_end']) // 2
    viz_data['zoom_start'] = max(0, center - new_range // 2)
    viz_data['zoom_end'] = min(len(viz_data['data1']), center + new_range // 2)
    update_visualization()

def reset_zoom():
    if viz_data['data1'] is None: return
    viz_data['zoom_start'] = 0
    viz_data['zoom_end'] = len(viz_data['data1'])
    update_visualization()

def select_file1():
    f = filedialog.askopenfilename(
        title="Select first audio/video file",
        filetypes=[("Audio/Video files", "*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.webm *.mp3 *.wav *.flac *.aac *.ogg *.m4a *.wma *.opus"),
                   ("All files", "*.*")]
    )
    if f:
        entry_file1.delete(0, tk.END)
        entry_file1.insert(0, f)

def select_file2():
    f = filedialog.askopenfilename(
        title="Select second audio/video file",
        filetypes=[("Audio/Video files", "*.mp4 *.avi *.mkv *.mov *.wmv *.flv *.webm *.mp3 *.wav *.flac *.aac *.ogg *.m4a *.wma *.opus"),
                   ("All files", "*.*")]
    )
    if f:
        entry_file2.delete(0, tk.END)
        entry_file2.insert(0, f)

def analyze_sync():
    file1 = entry_file1.get()
    file2 = entry_file2.get()
    
    if not file1 or not file2:
        messagebox.showerror("Error", "Select both audio/video files!")
        return

    path_wav1 = os.path.join(tempfile.gettempdir(), "sync_temp_1.wav")
    path_wav2 = os.path.join(tempfile.gettempdir(), "sync_temp_2.wav")

    for p in [path_wav1, path_wav2]:
        if os.path.exists(p):
            os.remove(p)

    label_status.config(text="● Extracting audio from files...", fg="#ffa500")
    root.update()

    try:
        start_time = to_seconds(entry_start.get())
        duration = float(entry_duration.get())
        
        # Get pre-offsets for each file
        preoffset1 = float(entry_preoffset1.get())
        preoffset2 = float(entry_preoffset2.get())
        
        # Apply pre-offset to start time for each file independently
        start_time1 = start_time + preoffset1
        start_time2 = start_time + preoffset2

        cmd1 = ["ffmpeg", "-y", "-ss", str(start_time1), "-i", file1, "-t", str(duration), 
                "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", path_wav1]
        cmd2 = ["ffmpeg", "-y", "-ss", str(start_time2), "-i", file2, "-t", str(duration), 
                "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", path_wav2]

        subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, 
                      check=True, startupinfo=get_startupinfo())
        subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, 
                      check=True, startupinfo=get_startupinfo())

        label_status.config(text="● Analyzing synchronization (signal correlation)...", fg="#ffa500")
        root.update()

        sr1, data1 = wavfile.read(path_wav1)
        sr2, data2 = wavfile.read(path_wav2)

        if len(data1.shape) > 1: data1 = np.mean(data1, axis=1)
        if len(data2.shape) > 1: data2 = np.mean(data2, axis=1)

        data1 = data1.astype(np.float64)
        data2 = data2.astype(np.float64)
        
        data1 = data1 - np.mean(data1)
        data2 = data2 - np.mean(data2)
        
        data1 = data1 / (np.sqrt(np.mean(data1**2)) + 1e-10)
        data2 = data2 / (np.sqrt(np.mean(data2**2)) + 1e-10)
        
        nyquist = sr1 / 2
        low_cut = 100 / nyquist
        high_cut = min(8000 / nyquist, 0.99)
        sos = signal.butter(4, [low_cut, high_cut], btype='band', output='sos')
        data1_filtered = signal.sosfilt(sos, data1)
        data2_filtered = signal.sosfilt(sos, data2)
        
        corr = signal.correlate(data1_filtered, data2_filtered, mode="full", method="fft")
        lags = signal.correlation_lags(len(data1_filtered), len(data2_filtered), mode="full")
        
        max_idx = np.argmax(np.abs(corr))
        lag = lags[max_idx]
        
        if 0 < max_idx < len(corr) - 1:
            y1, y2, y3 = np.abs(corr[max_idx-1]), np.abs(corr[max_idx]), np.abs(corr[max_idx+1])
            delta = 0.5 * (y3 - y1) / (2*y2 - y1 - y3 + 1e-10)
            lag = lag + delta

        ref_file = ref_var.get()
        target_name = "File 2" if ref_file == 1 else "File 1"
        lag_target = lag if ref_file == 1 else -lag

        offset_ms = round((lag_target / sr1) * 1000, 2)
        
        # Get pre-offsets
        preoffset1 = float(entry_preoffset1.get())
        preoffset2 = float(entry_preoffset2.get())
        
        # Calculate total offset including pre-offsets
        # If File 1 is base (ref_file == 1), then File 2 is the target
        # Total offset = detected offset + (preoffset_target - preoffset_base)
        if ref_file == 1:
            # File 1 is base, File 2 is target
            preoffset_diff_ms = (preoffset2 - preoffset1) * 1000
        else:
            # File 2 is base, File 1 is target
            preoffset_diff_ms = (preoffset1 - preoffset2) * 1000
        
        total_offset_ms = round(offset_ms - preoffset_diff_ms, 2)
        
        # Build result message
        if offset_ms == 0:
            res = "✓ Files perfectly synchronized!\nOffset: 0.00 ms"
            color = "#00ff88"
        elif offset_ms > 0:
            res = f"⏱️ {target_name} IS DELAYED\nOffset: +{offset_ms} ms\n→ Shift forward by {offset_ms} ms"
            color = "#ffa500"
        else:
            res = f"⏱️ {target_name} IS AHEAD\nOffset: {offset_ms} ms\n→ Delay by {abs(offset_ms)} ms"
            color = "#ff6b6b"
        
        # Add pre-offset info and total offset if any are set
        if preoffset1 != 0 or preoffset2 != 0:
            res += f"\n\n📌 Pre-offsets:"
            if preoffset1 != 0:
                res += f"\n   File 1: +{preoffset1 * 1000:.0f} ms"
            if preoffset2 != 0:
                res += f"\n   File 2: +{preoffset2 * 1000:.0f} ms"
            
            res += f"\n\n🎯 TOTAL OFFSET: "
            if total_offset_ms == 0:
                res += "0.00 ms"
            elif total_offset_ms > 0:
                res += f"+{total_offset_ms} ms"
            else:
                res += f"{total_offset_ms} ms"

        label_result.config(text=res, fg=color)
        label_status.config(text="✓ Analysis completed successfully", fg="#00ff88")

        viz_data['data1'] = data1_filtered
        viz_data['data2'] = data2_filtered
        viz_data['sr'] = sr1
        viz_data['offset_samples'] = int(lag_target)
        viz_data['zoom_start'] = 0
        viz_data['zoom_end'] = len(data1_filtered)
        
        entry_manual_offset.delete(0, tk.END)
        entry_manual_offset.insert(0, str(offset_ms))
        
        update_visualization()

    except Exception as e:
        messagebox.showerror("Error", f"Analysis failed:\n{str(e)}")
        label_status.config(text="✗ Error occurred during analysis", fg="#ff4444")
        label_result.config(text="Could not analyze files.\nCheck if they are valid.", fg="#888888")

# === GUI SETUP ===
root = tk.Tk()
root.title("Audio Sync Finder - Precise Audio Offset Detection")
root.geometry("1600x900")
root.configure(bg="#1a1a1a")
root.resizable(False, False)

bg_main = "#1a1a1a"
bg_panel = "#252525"
bg_input = "#2d2d2d"
fg_main = "#ffffff"
fg_dim = "#aaaaaa"
fg_accent = "#4a9eff"
btn_primary = "#2a7de1"
btn_success = "#28a745"
btn_hover = "#3d8bff"

ref_var = tk.IntVar(value=1)

# Main container
main_container = tk.Frame(root, bg=bg_main)
main_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

# === TOP ROW: Files + Parameters ===
top_row = tk.Frame(main_container, bg=bg_main)
top_row.pack(fill=tk.X, pady=(0, 15))

# FILES SECTION
files_frame = tk.LabelFrame(top_row, text=" Audio/Video Files ", bg=bg_panel, fg=fg_accent,
                            font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2)
files_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

# File 1
file1_container = tk.Frame(files_frame, bg=bg_panel)
file1_container.pack(fill=tk.X, padx=15, pady=(12, 8))

tk.Label(file1_container, text="File 1:", bg=bg_panel, fg=fg_main, 
         font=("Segoe UI", 9, "bold"), width=6).pack(side=tk.LEFT)
entry_file1 = tk.Entry(file1_container, bg=bg_input, fg=fg_main, insertbackground="white",
                       relief=tk.FLAT, font=("Segoe UI", 9))
entry_file1.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(5, 5))
tk.Button(file1_container, text="Browse", command=select_file1, bg=btn_primary, fg=fg_main,
          relief=tk.FLAT, font=("Segoe UI", 9), cursor="hand2", width=8,
          activebackground=btn_hover).pack(side=tk.LEFT, padx=(0, 5))
tk.Label(file1_container, text="Pre-offset:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(5, 2))
entry_preoffset1 = tk.Entry(file1_container, bg=bg_input, fg=fg_main, insertbackground="white",
                            relief=tk.FLAT, font=("Segoe UI", 9), width=6, justify=tk.CENTER)
entry_preoffset1.insert(0, "0")
entry_preoffset1.pack(side=tk.LEFT, ipady=3, padx=(0, 5))
tk.Label(file1_container, text="s", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 5))
tk.Radiobutton(file1_container, text="Base", variable=ref_var, value=1, bg=bg_panel,
               fg=fg_dim, selectcolor=bg_panel, activebackground=bg_panel,
               font=("Segoe UI", 9)).pack(side=tk.LEFT)

# File 2
file2_container = tk.Frame(files_frame, bg=bg_panel)
file2_container.pack(fill=tk.X, padx=15, pady=(0, 12))

tk.Label(file2_container, text="File 2:", bg=bg_panel, fg=fg_main,
         font=("Segoe UI", 9, "bold"), width=6).pack(side=tk.LEFT)
entry_file2 = tk.Entry(file2_container, bg=bg_input, fg=fg_main, insertbackground="white",
                       relief=tk.FLAT, font=("Segoe UI", 9))
entry_file2.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(5, 5))
tk.Button(file2_container, text="Browse", command=select_file2, bg=btn_primary, fg=fg_main,
          relief=tk.FLAT, font=("Segoe UI", 9), cursor="hand2", width=8,
          activebackground=btn_hover).pack(side=tk.LEFT, padx=(0, 5))
tk.Label(file2_container, text="Pre-offset:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(5, 2))
entry_preoffset2 = tk.Entry(file2_container, bg=bg_input, fg=fg_main, insertbackground="white",
                            relief=tk.FLAT, font=("Segoe UI", 9), width=6, justify=tk.CENTER)
entry_preoffset2.insert(0, "0")
entry_preoffset2.pack(side=tk.LEFT, ipady=3, padx=(0, 5))
tk.Label(file2_container, text="s", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 5))
tk.Radiobutton(file2_container, text="Base", variable=ref_var, value=2, bg=bg_panel,
               fg=fg_dim, selectcolor=bg_panel, activebackground=bg_panel,
               font=("Segoe UI", 9)).pack(side=tk.LEFT)

# PARAMETERS SECTION
params_frame = tk.LabelFrame(top_row, text=" Analysis Parameters ", bg=bg_panel, fg=fg_accent,
                             font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2)
params_frame.pack(side=tk.LEFT, fill=tk.Y)

params_inner = tk.Frame(params_frame, bg=bg_panel)
params_inner.pack(padx=20, pady=15)

# Start time
start_container = tk.Frame(params_inner, bg=bg_panel)
start_container.pack(side=tk.LEFT, padx=(0, 15))
tk.Label(start_container, text="Start time:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9)).pack()
entry_start = tk.Entry(start_container, bg=bg_input, fg=fg_main, insertbackground="white",
                       relief=tk.FLAT, font=("Segoe UI", 10), justify=tk.CENTER, width=12)
entry_start.insert(0, "00:00:00")
entry_start.pack(ipady=5, pady=3)
tk.Label(start_container, text="(HH:MM:SS)", bg=bg_panel, fg="#666666",
         font=("Segoe UI", 7)).pack()

# Duration
duration_container = tk.Frame(params_inner, bg=bg_panel)
duration_container.pack(side=tk.LEFT)
tk.Label(duration_container, text="Duration (sec):", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9)).pack()
entry_duration = tk.Entry(duration_container, bg=bg_input, fg=fg_main, insertbackground="white",
                          relief=tk.FLAT, font=("Segoe UI", 10), justify=tk.CENTER, width=12)
entry_duration.insert(0, "30")
entry_duration.pack(ipady=5, pady=3)
tk.Label(duration_container, text="(30-60 rec.)", bg=bg_panel, fg="#666666",
         font=("Segoe UI", 7)).pack()

# === BOTTOM ROW: Chart + Control Panel ===
bottom_row = tk.Frame(main_container, bg=bg_main)
bottom_row.pack(fill=tk.BOTH, expand=True)

# CHART (LEFT)
chart_frame = tk.LabelFrame(bottom_row, text=" 📊 Waveform Visualization ", bg=bg_panel, fg=fg_accent,
                            font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2)
chart_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

canvas_container = tk.Frame(chart_frame, bg=bg_panel)
canvas_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

fig = Figure(figsize=(10, 6), dpi=90, facecolor='#1a1a1a')
ax = fig.add_subplot(111)
ax.set_facecolor('#1a1a1a')
canvas = FigureCanvasTkAgg(fig, master=canvas_container)
canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

# Horizontal scrollbar for zoomed view
scrollbar_frame = tk.Frame(chart_frame, bg=bg_panel, height=25)
scrollbar_frame.pack(fill=tk.X, padx=10, pady=(5, 10))
scrollbar_frame.pack_propagate(False)

scrollbar = tk.Scale(scrollbar_frame, from_=0, to=100, orient=tk.HORIZONTAL, 
                     command=on_scroll, showvalue=False, bg=bg_panel, fg=fg_accent,
                     troughcolor=bg_input, highlightthickness=0, relief=tk.FLAT,
                     activebackground=fg_accent, sliderrelief=tk.FLAT, bd=0)
scrollbar.pack(fill=tk.BOTH, expand=True)

viz_data['fig'] = fig
viz_data['ax'] = ax
viz_data['canvas'] = canvas
viz_data['scrollbar'] = scrollbar
viz_data['scrollbar_frame'] = scrollbar_frame

# CONTROL PANEL (RIGHT)
control_panel = tk.LabelFrame(bottom_row, text=" Controls ", bg=bg_panel, fg=fg_accent,
                              font=("Segoe UI", 10, "bold"), relief=tk.GROOVE, bd=2, width=300)
control_panel.pack(side=tk.LEFT, fill=tk.Y)
control_panel.pack_propagate(False)

controls_inner = tk.Frame(control_panel, bg=bg_panel)
controls_inner.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

# ANALYZE BUTTON
btn_analyze = tk.Button(controls_inner, text="⚡ ANALYZE", command=analyze_sync,
                        bg=btn_success, fg=fg_main, relief=tk.FLAT, cursor="hand2",
                        font=("Segoe UI", 12, "bold"), activebackground="#23923d")
btn_analyze.pack(fill=tk.X, ipady=15, pady=(0, 15))

# RESULTS SECTION
result_section = tk.Frame(controls_inner, bg=bg_panel)
result_section.pack(fill=tk.X, pady=(0, 15))

label_status = tk.Label(result_section, text="● Ready to analyze", bg=bg_panel, fg="#888888",
                        font=("Segoe UI", 9), anchor=tk.W)
label_status.pack(fill=tk.X, pady=(0, 8))

label_result = tk.Label(result_section, text="Waiting for analysis...", bg=bg_panel, fg=fg_dim,
                        font=("Segoe UI", 9, "bold"), anchor=tk.W, wraplength=260, justify=tk.LEFT)
label_result.pack(fill=tk.X)

# SEPARATOR
tk.Frame(controls_inner, bg="#444444", height=1).pack(fill=tk.X, pady=15)

# ZOOM CONTROLS
zoom_section = tk.Frame(controls_inner, bg=bg_panel)
zoom_section.pack(fill=tk.X, pady=(0, 15))

tk.Label(zoom_section, text="🔍 Zoom:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 8))

zoom_buttons = tk.Frame(zoom_section, bg=bg_panel)
zoom_buttons.pack(fill=tk.X)

tk.Button(zoom_buttons, text="Zoom +", command=zoom_in, bg="#333333", fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
          activebackground="#444444").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3), ipady=5)
tk.Button(zoom_buttons, text="Zoom -", command=zoom_out, bg="#333333", fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
          activebackground="#444444").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 3), ipady=5)
tk.Button(zoom_buttons, text="Reset", command=reset_zoom, bg="#333333", fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9),
          activebackground="#444444").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0), ipady=5)

# SEPARATOR
tk.Frame(controls_inner, bg="#444444", height=1).pack(fill=tk.X, pady=15)

# MANUAL OFFSET
manual_section = tk.Frame(controls_inner, bg=bg_panel)
manual_section.pack(fill=tk.X, pady=(0, 15))

tk.Label(manual_section, text="✏️ Manual offset:", bg=bg_panel, fg=fg_dim,
         font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 8))

manual_controls = tk.Frame(manual_section, bg=bg_panel)
manual_controls.pack(fill=tk.X)

entry_manual_offset = tk.Entry(manual_controls, bg=bg_input, fg=fg_main, insertbackground="white",
                                relief=tk.FLAT, font=("Segoe UI", 10), justify=tk.CENTER)
entry_manual_offset.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(0, 5))

tk.Button(manual_controls, text="Apply", command=apply_manual_offset, bg=btn_primary, fg=fg_main,
          relief=tk.FLAT, cursor="hand2", font=("Segoe UI", 9, "bold"),
          activebackground=btn_hover, width=8).pack(side=tk.LEFT, ipady=6)

tk.Label(manual_section, text="(in milliseconds)", bg=bg_panel, fg="#666666",
         font=("Segoe UI", 7)).pack(anchor=tk.W, pady=(3, 0))

# SEPARATOR
tk.Frame(controls_inner, bg="#444444", height=1).pack(fill=tk.X, pady=15)

# TIPS SECTION (expandable)
tips_frame = tk.LabelFrame(controls_inner, text=" 💡 Tips ", bg=bg_panel, fg=fg_accent,
                           font=("Segoe UI", 9, "bold"), relief=tk.GROOVE, bd=1)
tips_frame.pack(fill=tk.BOTH, expand=True)

tips_text = tk.Text(tips_frame, bg=bg_panel, fg="#999999",
                    font=("Segoe UI", 7), relief=tk.FLAT, wrap=tk.WORD,
                    borderwidth=0, highlightthickness=0, cursor="arrow")
tips_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
tips_text.insert(1.0, "SELECT clips with:\n• Clear speech/music\n• Single language\n• Sharp sounds (claps/hits)\n\nAVOID:\n• Multilingual sections\n• Background noise\n• Long pauses/silence")
tips_text.config(state=tk.DISABLED)

root.mainloop()
