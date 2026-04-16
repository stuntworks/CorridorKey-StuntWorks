# Last modified: 2026-04-14 | Change: Remove hardcoded D:\ paths — dynamic engine resolver | Full history: git log
"""CorridorKey Pro - Neural Green Screen for DaVinci Resolve
Enhanced with SAM2 Click-to-Mask, Frame Range, Export Modes

WHAT IT DOES: One-click AI green screen keyer for DaVinci Resolve. Reads source footage
from the timeline, runs it through Niko Pueringer's CorridorKey neural network, and places
the keyed result on Track 2. Supports single frame, frame range, SAM2 click-to-mask, and
live preview with despill/refiner sliders.

DEPENDS-ON:
  - CorridorKey engine folder — location resolved at startup by find_corridorkey_root()
    which checks CORRIDORKEY_ROOT env var, corridorkey_path.txt config, then fallbacks.
  - DaVinci Resolve running with a project and timeline open
  - Resolve's Fusion scripting environment (fu, fusionscript)
  - core/corridorkey_processor.py (ProcessingSettings, CorridorKeyProcessor)
  - core/alpha_hint_generator.py (AlphaHintGenerator)
  - resolve_plugin/preview_viewer.py (separate process for preview window)

AFFECTS: Timeline Track 2 (writes keyed frames), MediaPool (creates CorridorKey bin),
  source clip on Track 1 (optionally disabled after processing)
"""
import sys, os, site, tempfile
from pathlib import Path

# WHAT IT DOES: Finds the CorridorKey engine folder (neural-net code + .venv + model weights)
#   by checking in order: 1) CORRIDORKEY_ROOT env var, 2) corridorkey_path.txt in the script
#   dir or its parent, 3) sibling "CorridorKey" folder two levels up, 4) legacy dev location
#   D:\New AI Projects\CorridorKey, 5) ~/CorridorKey. Raises a clear error if none work.
# DEPENDS-ON: nothing — pure filesystem probe.
# AFFECTS: returns a pathlib.Path. Does not modify sys.path itself.
def find_corridorkey_root():
    script_dir = Path(__file__).parent
    candidates = []
    env_root = os.environ.get("CORRIDORKEY_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    for probe_dir in (script_dir, script_dir.parent):
        cfg = probe_dir / "corridorkey_path.txt"
        if cfg.exists():
            try:
                candidates.append(Path(cfg.read_text().strip()))
            except Exception:
                pass
    candidates.append(script_dir.parent.parent / "CorridorKey")
    candidates.append(Path(r"D:\New AI Projects\CorridorKey"))
    candidates.append(Path.home() / "CorridorKey")
    for path in candidates:
        if path and path.exists() and (path / ".venv").exists():
            return path
    probed = "\n  ".join(str(c) for c in candidates)
    raise RuntimeError(
        "CorridorKey engine not found. Tried:\n  " + probed + "\n\n"
        "Fix: set the CORRIDORKEY_ROOT environment variable to the CorridorKey engine folder, "
        "or place a corridorkey_path.txt file next to this script containing that path."
    )

# WHAT IT DOES: Returns the venv's site-packages directory, Windows or Unix layout.
# DEPENDS-ON: CorridorKey's .venv built with standard python -m venv layout.
# AFFECTS: returns a pathlib.Path.
def find_venv_site_packages(venv_dir):
    win_sp = venv_dir / "Lib" / "site-packages"
    if win_sp.exists():
        return win_sp
    for p in (venv_dir / "lib").glob("python*/site-packages"):
        return p
    return win_sp  # leave as Windows path so downstream error points at the expected location

# DANGER ZONE FRAGILE: If find_corridorkey_root() raises, nothing below this point runs.
# breaks: user has not installed the CorridorKey engine, or the config points at a stale path.
CK_ROOT = find_corridorkey_root()
CK_VENV = CK_ROOT / ".venv"
CK_PYTHON = CK_VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

venv_packages = str(find_venv_site_packages(CK_VENV))
site.addsitedir(venv_packages)
sys.path.insert(0, venv_packages)
sys.path.insert(0, r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules")
sys.path.insert(0, str(CK_ROOT))
sys.path.insert(0, str(CK_ROOT / "resolve_plugin"))

import fusionscript

# DANGER ZONE FRAGILE: Resolve API init — fu is injected by Resolve's script runner.
# breaks: if script is run outside Resolve (standalone Python will crash here)
# depends on: Resolve running, project open, timeline loaded
resolve = fu.GetResolve()
ui = fu.UIManager
disp = fusionscript.UIDispatcher(ui)

pm = resolve.GetProjectManager()
project = pm.GetCurrentProject()
media_pool = project.GetMediaPool() if project else None
timeline = project.GetCurrentTimeline() if project else None

# Global state caches — these persist between button clicks during one session
last_preview_data = {"original": None, "keyed": None, "alpha": None}
cached_source = {"frame": None, "file_path": None, "frame_num": None}
cached_processor = {"proc": None}  # Holds loaded AI model to avoid reloading every frame
sam_points = {"positive": [], "negative": [], "frame": None}
frame_range = {"in_frame": None, "out_frame": None}
_viewer_proc = None  # Tracks preview viewer subprocess — killed on plugin close

# Persistent settings — saved to temp folder so output path survives between sessions
_config_path = Path(tempfile.gettempdir()) / "corridorkey_config.txt"

# WHAT IT DOES: Reads the user's last-used output folder from a config file in temp
# ISOLATED: no dependencies, returns a safe default if file missing or unreadable
def _load_output_path():
    try:
        if _config_path.exists():
            return _config_path.read_text().strip()
    except: pass
    return str(Path.home() / "Documents" / "CorridorKey")

# WHAT IT DOES: Saves the user's chosen output folder to a config file in temp
# ISOLATED: no dependencies, silently fails if temp folder is locked
def _save_output_path(p):
    try: _config_path.write_text(p)
    except: pass

winLayout = ui.VGroup({"Spacing": 14}, [
    ui.Label({"Text": "CorridorKey Pro", "Weight": 0, "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 16, "Bold": True}), "StyleSheet": "color: #6ab;"}),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Alpha Method:", "Weight": 0}),
        ui.ComboBox({"ID": "AlphaMethod", "Weight": 2}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Screen Type:", "Weight": 0}),
        ui.ComboBox({"ID": "ScreenType", "Weight": 2}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Despill:", "Weight": 0}),
        ui.Slider({"ID": "DespillSlider", "Minimum": 0, "Maximum": 100, "Value": 50, "Orientation": "Horizontal", "Weight": 2}),
        ui.Label({"ID": "DespillValue", "Text": "0.50", "Weight": 0}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Refiner:", "Weight": 0}),
        ui.Slider({"ID": "RefinerSlider", "Minimum": 0, "Maximum": 100, "Value": 100, "Orientation": "Horizontal", "Weight": 2}),
        ui.Label({"ID": "RefinerValue", "Text": "1.00", "Weight": 0}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.CheckBox({"ID": "DespeckleCheck", "Text": "Despeckle:", "Checked": True, "Weight": 0}),
        ui.Slider({"ID": "DespeckleSize", "Minimum": 50, "Maximum": 2000, "Value": 400, "Orientation": "Horizontal", "Weight": 2}),
        ui.Label({"ID": "DespeckleValue", "Text": "400", "Weight": 0}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Export:", "Weight": 0}),
        ui.ComboBox({"ID": "ExportFormat", "Weight": 2}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Output:", "Weight": 0}),
        ui.ComboBox({"ID": "OutputMode", "Weight": 2}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Save To:", "Weight": 0}),
        ui.LineEdit({"ID": "OutputPath", "Text": _load_output_path(), "ReadOnly": True, "Weight": 2}),
        ui.Button({"ID": "BrowseOutput", "Text": "...", "Weight": 0}),
    ]),
    ui.VGap(2),
    ui.Label({"Text": "Frame Range:", "Weight": 0, "StyleSheet": "color: #0ff; font-weight: bold;"}),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "SetInPoint", "Text": "IN", "Weight": 1, "StyleSheet": "QPushButton { background-color: #3a5a6a; color: #7ab; border-radius: 4px; padding: 4px; font-weight: bold; }"}),
        ui.Label({"ID": "InPointLabel", "Text": "---", "Weight": 0, "StyleSheet": "color: #7ab;"}),
        ui.Button({"ID": "SetOutPoint", "Text": "OUT", "Weight": 1, "StyleSheet": "QPushButton { background-color: #3a5a6a; color: #7ab; border-radius: 4px; padding: 4px; font-weight: bold; }"}),
        ui.Label({"ID": "OutPointLabel", "Text": "---", "Weight": 0, "StyleSheet": "color: #7ab;"}),
        ui.Button({"ID": "ClearRange", "Text": "Clear", "Weight": 1, "StyleSheet": "QPushButton { background-color: #222; color: #667; border-radius: 4px; padding: 4px; }"}),
    ]),
    ui.HGroup({"Weight": 0}, [
        ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable source clip after processing", "Checked": True}),
    ]),
    ui.HGroup({"Weight": 0}, [
        ui.CheckBox({"ID": "LivePreview", "Text": "Live Preview on slider change", "Checked": False, "StyleSheet": "color: #0ff;"}),
    ]),
    ui.VGap(2),
    ui.Label({"ID": "Status", "Text": "Ready", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 14px; font-weight: bold;"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "SAMClickMask", "Text": "SAM MASK", "Weight": 1, "StyleSheet": "QPushButton { background-color: #4a3a1a; color: #da5; font-weight: bold; border-radius: 5px; padding: 6px; border: 1px solid #da5; }"}),
        ui.Button({"ID": "ShowPreview", "Text": "PREVIEW", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a3a4a; color: #5df; font-weight: bold; border-radius: 5px; padding: 6px; border: 1px solid #5df; }"}),
        ui.Button({"ID": "ProcessFrame", "Text": "SINGLE FRAME", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a3a5a; color: #5af; font-weight: bold; border-radius: 5px; padding: 6px; border: 1px solid #5af; }"}),
    ]),
    ui.VGap(2),
    ui.Button({"ID": "ProcessRange", "Text": "PROCESS RANGE", "Weight": 0, "StyleSheet": "QPushButton { background-color: #1a4a2a; color: #5b5; font-size: 15px; font-weight: bold; border-radius: 6px; padding: 10px; border: 1px solid #5b5; }"}),
    ui.Button({"ID": "Cancel", "Text": "CANCEL", "Weight": 0, "StyleSheet": "QPushButton { background-color: #4a1a1a; color: #f66; font-weight: bold; border-radius: 5px; padding: 4px; border: 1px solid #f66; }"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "ToggleTrack1", "Text": "TOGGLE TRACK 1", "Weight": 1, "StyleSheet": "QPushButton { background-color: #222; color: #667; border-radius: 4px; padding: 3px; }"}),
        ui.Button({"ID": "OpenFusion", "Text": "OPEN FUSION", "Weight": 1, "StyleSheet": "QPushButton { background-color: #3a3a1a; color: #a85; border-radius: 4px; padding: 3px; border: 1px solid #a85; }"}),
    ]),
    ui.VGap(2),
    ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 3, "StyleSheet": "background: #111; color: #0ff; font-family: monospace; font-size: 10px; border-radius: 4px; border: 1px solid #222;"}),
    ui.Button({"ID": "AboutBtn", "Text": "About", "Weight": 0, "StyleSheet": "QPushButton { background-color: #222; color: #556; font-size: 11px; border-radius: 4px; padding: 2px; }"}),
])

win = disp.AddWindow({"ID": "CK", "WindowTitle": "CorridorKey Pro", "Geometry": [100, 50, 500, 750]}, winLayout)

items = win.GetItems()
items["AlphaMethod"].AddItem("Simple Chroma Key")
items["AlphaMethod"].AddItem("SAM2 Click-to-Mask")
items["ScreenType"].AddItem("Green Screen")
items["ScreenType"].AddItem("Blue Screen")
items["ExportFormat"].AddItem("RGBA (Full)")
items["ExportFormat"].AddItem("Alpha Only")
items["ExportFormat"].AddItem("Foreground Only")
items["OutputMode"].AddItem("Track 2 (Above Source)")
items["OutputMode"].AddItem("MediaPool Only")
items["OutputMode"].AddItem("Fusion Comp")

# WHAT IT DOES: Writes a message to both the console and the in-panel log window
# AFFECTS: Log TextEdit widget in the UI panel
def log(msg):
    print(msg)
    items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"

# WHAT IT DOES: Updates the cyan status label at the center of the panel
def status(msg): items["Status"].Text = msg

# WHAT IT DOES: Reads all UI controls and returns a dict of current processing settings
# DEPENDS-ON: All combo boxes, sliders, and checkboxes in the panel
def get_settings():
    return {"alpha_method": items["AlphaMethod"].CurrentIndex, "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
            "despill_strength": items["DespillSlider"].Value / 100.0, "refiner_strength": items["RefinerSlider"].Value / 100.0,
            "despeckle_enabled": items["DespeckleCheck"].Checked, "despeckle_size": items["DespeckleSize"].Value,
            "export_format": items["ExportFormat"].CurrentIndex, "output_mode": items["OutputMode"].CurrentIndex}

# WHAT IT DOES: Gets the current playhead position as a frame number and the timeline fps
# DEPENDS-ON: Resolve project settings for frame rate, timeline for timecode
# DANGER ZONE FRAGILE: Timecode parsing assumes HH:MM:SS:FF format
# breaks: if Resolve returns non-standard timecode format or drop-frame semicolons
def get_current_frame_info():
    try:
        fps = float(project.GetSetting("timelineFrameRate") or 24)
        tc = timeline.GetCurrentTimecode()
        parts = tc.replace(";", ":").split(":")
        if len(parts) == 4:
            h, m, s, f = [int(p) for p in parts]
            return int(h * 3600 * fps + m * 60 * fps + s * fps + f), fps
        return 0, fps
    except: return 0, 24.0

# --- Frame Range UI Callbacks ---
# WHAT IT DOES: Sets IN point to current playhead frame for range processing
def on_set_in_point(ev):
    cf, _ = get_current_frame_info()
    frame_range["in_frame"] = cf
    items["InPointLabel"].Text = str(cf)
    log(f"IN: {cf}")

# WHAT IT DOES: Sets OUT point to current playhead frame for range processing
def on_set_out_point(ev):
    cf, _ = get_current_frame_info()
    frame_range["out_frame"] = cf
    items["OutPointLabel"].Text = str(cf)
    log(f"OUT: {cf}")

# WHAT IT DOES: Clears both IN and OUT points, resets labels to "---"
def on_clear_range(ev):
    frame_range["in_frame"] = frame_range["out_frame"] = None
    items["InPointLabel"].Text = items["OutPointLabel"].Text = "---"
    log("Range cleared")

# WHAT IT DOES: Opens a folder picker for the user to choose where keyed frames are saved
# AFFECTS: OutputPath text field, persistent config file in temp
def on_browse_output(ev):
    folder = fu.RequestDir(items["OutputPath"].Text)
    if folder:
        items["OutputPath"].Text = str(folder)
        _save_output_path(str(folder))
        log(f"Output: {folder}")

# WHAT IT DOES: Updates despill value label; if Live Preview is on, re-keys with new setting
# DEPENDS-ON: cached_source (must have a frame loaded), reprocess_with_cached()
def on_despill_changed(ev):
    items["DespillValue"].Text = f"{items['DespillSlider'].Value / 100.0:.2f}"
    if items["LivePreview"].Checked and cached_source["frame"] is not None: reprocess_with_cached()

# WHAT IT DOES: Updates refiner value label; if Live Preview is on, re-keys with new setting
# DEPENDS-ON: cached_source (must have a frame loaded), reprocess_with_cached()
def on_refiner_changed(ev):
    items["RefinerValue"].Text = f"{items['RefinerSlider'].Value / 100.0:.2f}"
    if items["LivePreview"].Checked and cached_source["frame"] is not None: reprocess_with_cached()

# WHAT IT DOES: Updates despeckle value label when slider moves
def on_despeckle_changed(ev):
    items["DespeckleValue"].Text = str(items["DespeckleSize"].Value)

# WHAT IT DOES: Generates an alpha hint (rough matte) for the neural keyer to refine.
#   Simple mode uses chroma difference; SAM2 mode uses click points from the SAM window.
# DEPENDS-ON: core/alpha_hint_generator.py, SAM2 weights (if SAM2 mode selected)
# AFFECTS: Quality of the final key — bad hint = bad matte
def generate_alpha_hint(frame, settings):
    import numpy as np
    if settings["alpha_method"] == 0:
        from core.alpha_hint_generator import AlphaHintGenerator
        return AlphaHintGenerator(screen_type=settings["screen_type"]).generate_hint(frame)
    elif settings["alpha_method"] == 1 and (sam_points["positive"] or sam_points["negative"]):
        return generate_sam2_mask(frame, sam_points["positive"], sam_points["negative"])
    else:
        from core.alpha_hint_generator import AlphaHintGenerator
        return AlphaHintGenerator(screen_type=settings["screen_type"]).generate_hint(frame)

# WHAT IT DOES: Runs SAM2 (Segment Anything Model 2) to generate a mask from user click points.
#   Loads the SAM2 model, feeds it the frame + positive/negative points, returns the best mask.
# DEPENDS-ON: SAM2 weights at <CK_ROOT>/sam2_weights/sam2.1_hiera_small.pt, CUDA GPU
# DANGER ZONE HIGH: Loads a ~300MB model into VRAM every call. No caching.
# breaks: if VRAM is full (Resolve already uses 2-4GB), or SAM2 weights are missing
def generate_sam2_mask(frame, pos_pts, neg_pts):
    import cv2, numpy as np, torch
    try:
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        log("Loading SAM2...")
        status("Loading SAM2...")
        ckpt = str(CK_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
        cfg = "configs/sam2.1/sam2.1_hiera_s.yaml"
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = build_sam2(cfg, ckpt, device=device)
        pred = SAM2ImagePredictor(model)
        pred.set_image(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        pts = [[p[0], p[1]] for p in pos_pts] + [[p[0], p[1]] for p in neg_pts]
        labs = [1]*len(pos_pts) + [0]*len(neg_pts)
        if not pts: return np.ones((frame.shape[0], frame.shape[1]), dtype=np.float32)
        masks, scores, _ = pred.predict(point_coords=np.array(pts), point_labels=np.array(labs), multimask_output=True)
        del pred, model; torch.cuda.empty_cache()
        log("SAM2 done")
        return masks[np.argmax(scores)].astype(np.float32)
    except Exception as e:
        log(f"SAM2 error: {e}")
        from core.alpha_hint_generator import AlphaHintGenerator
        return AlphaHintGenerator(screen_type="green").generate_hint(frame)

# WHAT IT DOES: Opens a tkinter window showing the current frame. User left-clicks to mark
#   subject (include), right-clicks to mark background (exclude). Points feed into SAM2.
# DEPENDS-ON: tkinter, PIL, cv2, timeline clip on Track 1
# AFFECTS: sam_points global dict (stores click coordinates for generate_sam2_mask)
# DANGER ZONE FRAGILE: Runs tkinter in a daemon thread — Resolve's event loop can conflict.
# breaks: if tkinter mainloop deadlocks with Fusion UIDispatcher
def open_sam_click_window():
    import threading, cv2
    def run():
        import tkinter as tk
        from PIL import Image, ImageTk
        import numpy as np
        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips: log("No clips"); return
        cf, _ = get_current_frame_info()
        clip = clips[0]
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        fp = props.get("File Path", "")
        fn = clip.GetLeftOffset() + (cf - clip.GetStart())
        if fn < 0: fn = 0
        cap = cv2.VideoCapture(fp)
        cap.set(cv2.CAP_PROP_POS_FRAMES, fn)
        ret, frame = cap.read()
        cap.release()
        if not ret: log("Read failed"); return
        sam_points["frame"], sam_points["positive"], sam_points["negative"] = frame.copy(), [], []
        sw = tk.Tk()
        sw.title("SAM2: Left=Include, Right=Exclude")
        sw.configure(bg="#1a1a1a")
        h, w = frame.shape[:2]
        sc = min(1.0, 900 / max(h, w))
        dw, dh = int(w * sc), int(h * sc)
        canvas = tk.Canvas(sw, width=dw, height=dh, bg="black", highlightthickness=0)
        canvas.pack(padx=10, pady=10)
        img = ImageTk.PhotoImage(Image.fromarray(cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), (dw, dh))))
        canvas.create_image(0, 0, anchor=tk.NW, image=img)
        canvas.image = img
        def click(e, pos):
            ox, oy = int(e.x / sc), int(e.y / sc)
            (sam_points["positive"] if pos else sam_points["negative"]).append((ox, oy))
            c = "#00FF00" if pos else "#FF0000"
            canvas.create_oval(e.x-8, e.y-8, e.x+8, e.y+8, outline=c, width=3)
            canvas.create_text(e.x, e.y, text="+" if pos else "-", fill=c, font=("Arial", 12, "bold"))
            sl.config(text=f"{len(sam_points['positive'])}+ {len(sam_points['negative'])}-")
        canvas.bind("<Button-1>", lambda e: click(e, True))
        canvas.bind("<Button-3>", lambda e: click(e, False))
        sl = tk.Label(sw, text="Left=Include, Right=Exclude", fg="white", bg="#1a1a1a", font=("Arial", 12))
        sl.pack(pady=5)
        bf = tk.Frame(sw, bg="#1a1a1a")
        bf.pack(pady=10)
        def done(): log(f"SAM: {len(sam_points['positive'])}+ {len(sam_points['negative'])}-"); sw.destroy()
        tk.Button(bf, text="Done", command=done, bg="#4CAF50", fg="white", font=("Arial", 12, "bold"), padx=25, pady=5).pack(side=tk.LEFT, padx=10)
        tk.Button(bf, text="Cancel", command=sw.destroy, bg="#f44336", fg="white", font=("Arial", 12, "bold"), padx=20, pady=5).pack(side=tk.LEFT, padx=10)
        sw.lift(); sw.attributes("-topmost", True); sw.after(100, lambda: sw.attributes("-topmost", False)); sw.mainloop()
    threading.Thread(target=run, daemon=True).start()

# WHAT IT DOES: Button handler — switches to SAM2 mode and opens the click window
def on_sam_click_mask(ev):
    log("Opening SAM2...")
    status("Click on subject")
    items["AlphaMethod"].CurrentIndex = 1
    open_sam_click_window()

# WHAT IT DOES: Generates a gray checkerboard pattern for transparency preview
# ISOLATED: pure function, no dependencies
def create_checkerboard(h, w, sz=20):
    import numpy as np
    c = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            c[y, x] = [180, 180, 180] if ((x // sz) + (y // sz)) % 2 == 0 else [120, 120, 120]
    return c

# WHAT IT DOES: Composites foreground over a checkerboard using the alpha matte
# DEPENDS-ON: create_checkerboard()
def composite_over_checker(fg, alpha, sz=20):
    import numpy as np
    h, w = fg.shape[:2]
    chk = create_checkerboard(h, w, sz)
    a = alpha[:, :, 0] if len(alpha.shape) == 3 else alpha
    a3 = np.stack([a, a, a], axis=2)
    return (fg * a3 + chk * (1 - a3)).astype(np.uint8)

# WHAT IT DOES: Searches all video tracks for a clip at the current playhead to use as
#   composite background in the preview window. Checks every track, grabs the frame via OpenCV.
# DEPENDS-ON: timeline, get_current_frame_info(), OpenCV
# AFFECTS: nothing — read-only, returns a frame or None
def grab_background_frame():
    """Try to grab a frame from tracks BELOW the green screen for composite background.
    DEPENDS-ON: timeline, get_current_frame_info()
    AFFECTS: nothing — read-only, returns a frame or None
    DANGER ZONE: Skips V1 (assumed green screen source). If user has green screen
      on V2+, this won't find the right background. Future: pass source track index."""
    import cv2
    try:
        cf, fps = get_current_frame_info()
        # Start from V2 — V1 is the green screen source. Grabbing V1 as background
        # creates a double image in the composite (keyed fg over original = ghost).
        track_count = timeline.GetTrackCount("video")
        for track_idx in range(2, track_count + 1):
            clips = timeline.GetItemListInTrack("video", track_idx) or []
            for c in clips:
                if c.GetStart() <= cf < c.GetEnd():
                    mpi = c.GetMediaPoolItem()
                    if not mpi: continue
                    props = mpi.GetClipProperty() if mpi else {}
                    fp = props.get("File Path", "")
                    if not fp: continue
                    fn = c.GetLeftOffset() + (cf - c.GetStart())
                    cap = cv2.VideoCapture(fp)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, fn))
                    ret, bg_frame = cap.read()
                    cap.release()
                    if ret:
                        log(f"BG plate from V{track_idx}: {os.path.basename(fp)}")
                        return bg_frame
        log("No background plate found on other tracks")
    except Exception as e:
        log(f"BG grab failed: {e}")
    return None

# WHAT IT DOES: Saves original, foreground, matte, and optional background plate to temp PNGs,
#   then launches preview_viewer.py as a separate process to display them side by side.
# DEPENDS-ON: preview_viewer.py at <CK_ROOT>/resolve_plugin/,
#   CorridorKey venv Python, grab_background_frame()
# DANGER ZONE FRAGILE: Hardcoded paths to viewer script and Python exe
# breaks: if CorridorKey folder moves or venv is rebuilt
def show_preview_window(orig_bgr, keyed_rgb, alpha):
    import cv2, numpy as np, subprocess, json
    a2d = alpha[:, :, 0] if len(alpha.shape) == 3 else alpha
    log(f"Matte debug — dtype:{a2d.dtype} min:{a2d.min():.4f} max:{a2d.max():.4f} mean:{a2d.mean():.4f}")
    # Normalize matte to full 0-255 range (white=subject, black=background)
    if a2d.dtype in (np.float32, np.float64):
        matte_vis = (np.clip(a2d / max(a2d.max(), 1e-6), 0, 1) * 255).astype(np.uint8)
    else:
        if a2d.max() > 0 and a2d.max() < 255:
            matte_vis = (a2d.astype(np.float32) / a2d.max() * 255).astype(np.uint8)
        else:
            matte_vis = a2d.astype(np.uint8)
    log(f"Matte after norm — min:{matte_vis.min()} max:{matte_vis.max()} mean:{matte_vis.mean():.1f}")
    # Try to grab background plate from another track
    bg_frame = grab_background_frame()
    # Save preview images to temp folder
    preview_dir = Path(items["OutputPath"].Text) / "_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "original": str(preview_dir / "original.png"),
        "foreground": str(preview_dir / "foreground.png"),
        "matte": str(preview_dir / "matte.png"),
    }
    cv2.imwrite(paths["original"], orig_bgr)
    cv2.imwrite(paths["foreground"], cv2.cvtColor(keyed_rgb, cv2.COLOR_RGB2BGR))
    cv2.imwrite(paths["matte"], matte_vis)
    # Save background plate if found
    if bg_frame is not None:
        h, w = orig_bgr.shape[:2]
        if bg_frame.shape[:2] != (h, w):
            bg_frame = cv2.resize(bg_frame, (w, h))
        paths["background"] = str(preview_dir / "background.png")
        cv2.imwrite(paths["background"], bg_frame)
        log("Background plate saved for composite")
    # Launch preview as separate process — no event loop conflicts
    # DANGER ZONE: Must track this process and kill on plugin close, or Resolve
    # can't restart (orphaned Python holds GPU/CUDA context open).
    global _viewer_proc
    if _viewer_proc is not None:
        try: _viewer_proc.kill()
        except: pass
        _viewer_proc = None
    viewer_script = str(CK_ROOT / "resolve_plugin" / "preview_viewer.py")
    python_exe = str(CK_PYTHON)
    _viewer_proc = subprocess.Popen(
        [python_exe, viewer_script, json.dumps(paths)],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    log("Preview launched")

# WHAT IT DOES: Writes the keyed result to disk as PNG. Three export formats:
#   0 = RGBA (foreground + alpha), 1 = Alpha only (grayscale matte), 2 = Foreground only (no alpha)
# DEPENDS-ON: OpenCV, numpy
# ISOLATED: pure file write, no side effects beyond disk
def save_output(fg, matte, path, fmt):
    import cv2, numpy as np
    m = matte[:, :, 0] if len(matte.shape) == 3 else matte
    if fmt == 0:
        fb = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        au = (m * 255).astype(np.uint8)
        cv2.imwrite(str(path), cv2.merge([fb[:,:,0], fb[:,:,1], fb[:,:,2], au]))
    elif fmt == 1: cv2.imwrite(str(path), (m * 255).astype(np.uint8))
    elif fmt == 2: cv2.imwrite(str(path), cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))

# WHAT IT DOES: Re-runs the neural keyer on the already-loaded frame using current slider values.
#   Used by Live Preview mode — avoids re-reading video from disk on every slider change.
# DEPENDS-ON: cached_source (must have a frame), CorridorKeyProcessor, show_preview_window()
# AFFECTS: last_preview_data global, launches preview viewer
def reprocess_with_cached():
    global last_preview_data
    import cv2, numpy as np
    try:
        frame = cached_source["frame"]
        if frame is None: return
        settings = get_settings()
        status("Updating...")
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        proc = CorridorKeyProcessor(device="cuda")
        ah = generate_alpha_hint(frame, settings)
        fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"])
        res = proc.process_frame(fr, ah, ps)
        fg, mt = res.get("fg"), res.get("alpha")
        if fg is not None and mt is not None:
            if len(mt.shape) == 3: mt = mt[:, :, 0]
            last_preview_data["original"] = frame.copy()
            last_preview_data["keyed"] = (fg * 255).astype(np.uint8)
            last_preview_data["alpha"] = mt.copy()
            show_preview_window(frame, (fg * 255).astype(np.uint8), mt)
            status("Updated")
    except Exception as e: log(f"Error: {e}")

# WHAT IT DOES: The main single-frame workflow. Reads the frame at the playhead from Track 1,
#   runs it through the CorridorKey neural keyer, saves the result to disk, imports it into
#   the MediaPool "CorridorKey" bin, and places it on Track 2 at the playhead position.
#   If preview_only=True, just shows the preview window without importing to timeline.
# DEPENDS-ON: timeline, media_pool, get_current_frame_info(), generate_alpha_hint(),
#   CorridorKeyProcessor, save_output(), show_preview_window()
# AFFECTS: MediaPool (creates CorridorKey bin, imports keyed PNG), Timeline Track 2 (places clip),
#   Track 1 source clip (optionally disabled), cached_source and last_preview_data globals
# DANGER ZONE HIGH: Timeline manipulation (lines 470-517) uses multiple Resolve API methods
#   that can fail silently or behave differently across Resolve versions.
# breaks: if Resolve API changes AppendToTimeline behavior, or if clip trimming fails
def process_current_frame(preview_only=False):
    global last_preview_data, cached_source
    import cv2, numpy as np
    status("PROCESSING...")
    log("=" * 35)
    try:
        if not timeline or not media_pool: status("ERROR: No timeline!"); return
        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips: status("ERROR: No clips!"); return
        settings = get_settings()
        cf, fps = get_current_frame_info()
        log(f"Frame {cf}")
        clip = None
        for c in clips:
            if c.GetStart() <= cf < c.GetEnd(): clip = c; break
        if not clip: clip = clips[0]
        cs = clip.GetStart()
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        fp = props.get("File Path", "")
        log(f"Source: {os.path.basename(fp)}")
        fn = clip.GetLeftOffset() + (cf - cs)
        if fn < 0: fn = 0
        cap = cv2.VideoCapture(fp)
        cap.set(cv2.CAP_PROP_POS_FRAMES, fn)
        ret, frame = cap.read()
        cap.release()
        if not ret: status("ERROR: Cannot read"); return
        cached_source["frame"], cached_source["file_path"], cached_source["frame_num"] = frame.copy(), fp, fn
        log(f"Size: {frame.shape[1]}x{frame.shape[0]}")
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        if cached_processor["proc"] is None:
            log("Loading AI (first time)...")
            cached_processor["proc"] = CorridorKeyProcessor(device="cuda")
            log("Model loaded!")
        else:
            log("AI ready (cached)")
        proc = cached_processor["proc"]
        log("Alpha hint...")
        ah = generate_alpha_hint(frame, settings)
        log("Processing...")
        fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"])
        res = proc.process_frame(fr, ah, ps)
        cn = Path(fp).stem
        od = Path(items["OutputPath"].Text) / f"CK_{cn}"
        od.mkdir(parents=True, exist_ok=True)
        op = od / f"CK_{cn}_{cf:06d}.png"
        fg, mt = res.get("fg"), res.get("alpha")
        if fg is not None and mt is not None:
            save_output(fg, mt, op, settings["export_format"])
            log(f"Saved: {op.name}")
        if len(mt.shape) == 3: mt = mt[:, :, 0]
        last_preview_data["original"], last_preview_data["keyed"], last_preview_data["alpha"] = frame.copy(), (fg * 255).astype(np.uint8), mt.copy()
        if preview_only:
            show_preview_window(frame, (fg * 255).astype(np.uint8), mt)
            status("Preview"); return
        root = media_pool.GetRootFolder()
        ckb = None
        for f in root.GetSubFolderList():
            if f.GetName() == "CorridorKey": ckb = f; break
        if not ckb: ckb = media_pool.AddSubFolder(root, "CorridorKey")
        media_pool.SetCurrentFolder(ckb)
        imp = media_pool.ImportMedia([str(op)])
        if not imp: status("Import failed"); return
        if settings["output_mode"] in [0, 2]:
            tc = timeline.GetTrackCount("video")
            log(f"[v0.7] Video tracks: {tc}")
            if tc < 2:
                timeline.AddTrack("video")
                tc = timeline.GetTrackCount("video")
                log(f"Tracks after add: {tc}")
            # Try multiple append methods
            # Set clip In/Out to 1 frame before append (helps recordFrame work)
            try:
                imp[0].SetClipProperty("In", "00:00:00:00")
                imp[0].SetClipProperty("Out", "00:00:00:00")
            except: pass
            # Try recordFrame with constrained clip first
            target_frame = cf + 1
            log(f"[v1.2] Track 2, recordFrame={target_frame}")
            result = media_pool.AppendToTimeline([{"mediaPoolItem": imp[0], "trackIndex": 2, "recordFrame": target_frame}])
            if not result:
                log("recordFrame failed, append without it")
                result = media_pool.AppendToTimeline([{"mediaPoolItem": imp[0], "trackIndex": 2}])
            if result:
                try:
                    track2_items = timeline.GetItemListInTrack("video", 2)
                    if track2_items:
                        placed = track2_items[-1]
                        # Trim to 1 frame
                        dur = placed.GetDuration()
                        if dur > 1:
                            excess = dur - 1
                            ro = placed.GetRightOffset()
                            placed.SetRightOffset(ro + excess)
                            log(f"Trimmed {dur} → {placed.GetDuration()} frame(s)")
                        # Move to playhead position
                        current_start = placed.GetStart()
                        if current_start != cf:
                            offset = cf - current_start
                            moved = placed.SetProperty("Start", cf)
                            log(f"Move: {current_start} → {cf} (result: {moved})")
                except Exception as trim_err:
                    log(f"Trim/Move: {trim_err}")
                if items["DisableTrack1"].Checked and clip:
                    clip.SetClipEnabled(False)
                    log(f"Disabled source clip: {os.path.basename(fp)}")
                status("DONE! Track 2")
            else:
                log(f"AppendToTimeline FAILED")
                status("MediaPool only — drag from CorridorKey bin")
        else: status("Done - MediaPool")
    except Exception as e:
        status("ERROR!")
        log(f"ERROR: {e}")
        import traceback; log(traceback.format_exc())
        err_log = Path(tempfile.gettempdir()) / "corridorkey_error.txt"
        with open(err_log, "w") as ef: ef.write(traceback.format_exc())
        log(f"Error trace written to {err_log}")

# WHAT IT DOES: Button handlers — preview shows key without importing, process imports to timeline
def on_show_preview(ev): process_current_frame(preview_only=True)
def on_process_frame(ev): process_current_frame(preview_only=False)

processing_cancelled = False

# WHAT IT DOES: Processes every frame in the IN-OUT range (or full clip if no range set).
#   Reads each frame from disk via OpenCV, keys it through the neural network, saves PNGs,
#   then imports the full sequence into MediaPool and places it on Track 2.
# DEPENDS-ON: timeline, media_pool, CorridorKeyProcessor, generate_alpha_hint(), save_output()
# AFFECTS: Disk (writes all keyed PNGs), MediaPool (imports sequence), Timeline Track 2,
#   Track 1 (optionally disabled after processing)
# DANGER ZONE HIGH: Long-running loop with no progress callback to Resolve.
#   Resolve may appear frozen during processing. Cannot be interrupted by Resolve UI.
# breaks: if user closes Resolve during processing, or if disk fills up mid-range
def on_process_range(ev):
    global processing_cancelled
    processing_cancelled = False
    import cv2, numpy as np, time
    log("=" * 35)
    log("PROCESS RANGE")
    try:
        if not timeline or not media_pool: status("ERROR: No timeline!"); return
        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips: status("ERROR: No clips!"); return
        clip = clips[0]
        cs, ce = clip.GetStart(), clip.GetEnd()
        inf = frame_range["in_frame"] if frame_range["in_frame"] is not None else cs
        outf = frame_range["out_frame"] if frame_range["out_frame"] is not None else ce
        inf, outf = max(inf, cs), min(outf, ce)
        if outf <= inf: status("Invalid range!"); return
        dur = outf - inf
        log(f"Range: {inf}-{outf} ({dur} frames)")
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        fp = props.get("File Path", "")
        ss = clip.GetLeftOffset()
        settings = get_settings()
        cn = Path(fp).stem
        od = Path(items["OutputPath"].Text) / f"CK_{cn}"
        od.mkdir(parents=True, exist_ok=True)
        log(f"Saving to: {od}")
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        if cached_processor["proc"] is None:
            log("Loading AI (first time)...")
            status("Loading AI...")
            cached_processor["proc"] = CorridorKeyProcessor(device="cuda")
            log("Model loaded!")
        else:
            log("AI ready (cached)")
        proc = cached_processor["proc"]
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"])
        cap = cv2.VideoCapture(fp)
        if not cap.isOpened(): status("Cannot open video"); return
        st = time.time()
        ofs = []
        pr = 0
        for tf in range(inf, outf):
            if processing_cancelled: log("Cancelled"); break
            sf = ss + (tf - cs)
            cap.set(cv2.CAP_PROP_POS_FRAMES, sf)
            ret, frame = cap.read()
            if not ret: continue
            ah = generate_alpha_hint(frame, settings)
            fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
            res = proc.process_frame(fr, ah, ps)
            fg, mt = res.get("fg"), res.get("alpha")
            if fg is not None and mt is not None:
                op = od / f"CK_{cn}_{pr:06d}.png"
                save_output(fg, mt, op, settings["export_format"])
                ofs.append(str(op))
            pr += 1
            el = time.time() - st
            fpsr = pr / el if el > 0 else 0
            rem = (dur - pr) / fpsr if fpsr > 0 else 0
            status(f"{pr}/{dur} ({fpsr:.1f}fps, {rem:.0f}s)")
            if pr % 10 == 0: log(f"{pr}/{dur}")
        cap.release()
        if not ofs: status("No frames"); return
        log(f"Done: {len(ofs)} frames in {time.time()-st:.1f}s")
        status("Importing...")
        root = media_pool.GetRootFolder()
        ckb = None
        for f in root.GetSubFolderList():
            if f.GetName() == "CorridorKey": ckb = f; break
        if not ckb: ckb = media_pool.AddSubFolder(root, "CorridorKey")
        media_pool.SetCurrentFolder(ckb)
        imp = media_pool.ImportMedia(ofs)
        if not imp: status("Import failed"); return
        log(f"Imported {len(imp)} items to MediaPool")
        if settings["output_mode"] in [0, 2]:
            ci = {"mediaPoolItem": imp[0], "startFrame": 0, "endFrame": len(ofs), "trackIndex": 2, "recordFrame": inf, "mediaType": 1}
            if media_pool.AppendToTimeline([ci]):
                if items["DisableTrack1"].Checked: timeline.SetTrackEnable("video", 1, False)
                status(f"DONE! {len(ofs)} frames")
            else: status("MediaPool only")
        else: status(f"{len(ofs)} frames in MediaPool")
    except Exception as e:
        status("ERROR!"); log(f"ERROR: {e}")
        import traceback; log(traceback.format_exc())

# WHAT IT DOES: Sets the cancel flag so the range processing loop stops on next iteration
def on_cancel(ev):
    global processing_cancelled
    processing_cancelled = True
    log("Cancelling...")

# WHAT IT DOES: Toggles Track 1 visibility on/off — lets user quickly show/hide source footage
def on_toggle_track1(ev):
    try:
        if timeline:
            cur = timeline.GetIsTrackEnabled("video", 1)
            timeline.SetTrackEnable("video", 1, not cur)
            status(f"Track 1 {'enabled' if not cur else 'disabled'}")
    except: pass

# WHAT IT DOES: Switches Resolve to the Fusion page for manual compositing
def on_open_fusion(ev):
    try: resolve.OpenPage("fusion"); status("Fusion opened")
    except: pass

# WHAT IT DOES: Exits the Fusion UIDispatcher event loop, closing the plugin window
# WHAT IT DOES: Kills any running preview viewer, then exits the Fusion event loop.
#   Without this, the orphaned Python viewer holds GPU/CUDA open and Resolve can't restart.
def on_close(ev):
    global _viewer_proc
    if _viewer_proc is not None:
        try: _viewer_proc.kill()
        except: pass
        _viewer_proc = None
    disp.ExitLoop()

win.On.DespillSlider.SliderMoved = on_despill_changed
win.On.RefinerSlider.SliderMoved = on_refiner_changed
win.On.DespeckleSize.SliderMoved = on_despeckle_changed
win.On.SetInPoint.Clicked = on_set_in_point
win.On.SetOutPoint.Clicked = on_set_out_point
win.On.ClearRange.Clicked = on_clear_range
win.On.BrowseOutput.Clicked = on_browse_output
win.On.SAMClickMask.Clicked = on_sam_click_mask
win.On.ShowPreview.Clicked = on_show_preview
win.On.ProcessFrame.Clicked = on_process_frame
win.On.ProcessRange.Clicked = on_process_range
win.On.Cancel.Clicked = on_cancel
win.On.ToggleTrack1.Clicked = on_toggle_track1
win.On.OpenFusion.Clicked = on_open_fusion

# WHAT IT DOES: Shows the About dialog with credits, how-to-use guide, and Ko-fi link.
#   Credits Niko Pueringer/Corridor Digital (engine) and Roberto+Elvis Lopez/StuntWorks (plugin).
# ISOLATED: self-contained dialog, no side effects
def on_about(ev):
    about_win = disp.AddWindow({"ID": "About", "WindowTitle": "About CorridorKey Pro", "Geometry": [200, 150, 460, 620]}, [
        ui.VGroup({"Spacing": 8, "Margin": 16}, [
            ui.Label({"Text": "CorridorKey Pro", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 22, "Bold": True}), "StyleSheet": "color: #4CAF50;"}),
            ui.Label({"Text": "AI-Powered Green Screen Keyer for DaVinci Resolve", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #aaa; font-size: 12px;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "CorridorKey Engine", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "Created by Niko Pueringer / Corridor Digital", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ddd;"}),
            ui.Label({"Text": "github.com/nikopueringer/CorridorKey", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #2196F3; font-size: 11px;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "DaVinci Resolve Plugin", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "by Roberto Lopez & Elvis Lopez", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ddd;"}),
            ui.Label({"Text": "StuntWorks Action Cinema", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 13, "Bold": True}), "StyleSheet": "color: #E91E63;"}),
            ui.Label({"Text": "github.com/stuntworks/CorridorKey-Plugin", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #2196F3; font-size: 11px;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "StuntWorks is a professional stunt rigging company.\nIn our spare time we build the tools we wish existed —\nfree plugins, automation, and workflow helpers.\nIf you find this useful, a coffee helps us keep building.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px; font-style: italic;", "WordWrap": True}),
            ui.Label({"Text": "☕  ko-fi.com/stuntworks", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #FF5E5B; font-size: 13px; font-weight: bold;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "How To Use", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "1. Place green screen footage on your timeline\n"
                              "2. Set Alpha Method (Simple or SAM2 Click-to-Mask)\n"
                              "3. Choose Green or Blue screen type\n"
                              "4. Click SHOW PREVIEW to check the key\n"
                              "5. Adjust Despill and Refiner sliders as needed\n"
                              "6. Set IN/OUT points for your range\n"
                              "7. Click PROCESS RANGE to render\n"
                              "8. Keyed output goes to Track 2 automatically\n\n"
                              "Tip: Place a background plate on the track below\n"
                              "your green screen clip to see the real composite\n"
                              "in the preview window.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px;", "WordWrap": True}),
            ui.Button({"ID": "CloseAbout", "Text": "Close", "MinimumSize": [0, 30], "StyleSheet": "background: #607D8B; color: white;"})])
    ])
    def close_about(ev): disp.ExitLoop()
    about_win.On.CloseAbout.Clicked = close_about
    about_win.On.About.Close = close_about
    about_win.Show()
    disp.RunLoop()
    about_win.Hide()

win.On.AboutBtn.Clicked = on_about
win.On.CK.Close = on_close

log("CorridorKey Pro Ready")
log("SAM2 | Frame Range | Export Modes")
win.Show()
disp.RunLoop()
win.Hide()
