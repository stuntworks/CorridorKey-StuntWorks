"""CorridorKey Pro - Neural Green Screen for DaVinci Resolve
Enhanced with SAM2 Click-to-Mask, Frame Range, Export Modes
"""
import sys, os, site, tempfile
from pathlib import Path

# ── CorridorKey root: set CORRIDORKEY_ROOT env var, or auto-detect relative to this plugin
CORRIDORKEY_ROOT = Path(os.environ.get("CORRIDORKEY_ROOT", Path(__file__).resolve().parent.parent))
_venv_packages = CORRIDORKEY_ROOT / ".venv" / "Lib" / "site-packages"
if _venv_packages.exists():
    site.addsitedir(str(_venv_packages))
    sys.path.insert(0, str(_venv_packages))

# Resolve scripting modules
if sys.platform == "win32":
    _resolve_modules = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
else:
    _resolve_modules = Path("/opt/resolve/Developer/Scripting/Modules")
sys.path.insert(0, str(_resolve_modules))
sys.path.insert(0, str(CORRIDORKEY_ROOT))
sys.path.insert(0, str(CORRIDORKEY_ROOT / "resolve_plugin"))

import fusionscript

# Use fu/fusion provided by Resolve's script runner (avoid dvr.scriptapp hang)
resolve = fu.GetResolve()
ui = fu.UIManager
disp = fusionscript.UIDispatcher(ui)

pm = resolve.GetProjectManager()
project = pm.GetCurrentProject()
media_pool = project.GetMediaPool() if project else None
timeline = project.GetCurrentTimeline() if project else None

last_preview_data = {"original": None, "keyed": None, "alpha": None}
cached_source = {"frame": None, "file_path": None, "frame_num": None}
cached_processor = {"proc": None}
sam_points = {"positive": [], "negative": [], "frame": None}
frame_range = {"in_frame": None, "out_frame": None}

# Persistent settings
_config_path = Path(tempfile.gettempdir()) / "corridorkey_config.txt"
def _load_output_path():
    try:
        if _config_path.exists():
            return _config_path.read_text().strip()
    except: pass
    return str(Path.home() / "Documents" / "CorridorKey")
def _save_output_path(p):
    try: _config_path.write_text(p)
    except: pass

winLayout = ui.VGroup({"Spacing": 14}, [
    ui.Label({"Text": "CorridorKey Pro", "Weight": 0, "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 16, "Bold": True}), "StyleSheet": "color: #4CAF50;"}),
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
        ui.CheckBox({"ID": "DespeckleCheck", "Text": "Auto Despeckle", "Checked": True, "Weight": 0}),
        ui.SpinBox({"ID": "DespeckleSize", "Minimum": 50, "Maximum": 1000, "Value": 400, "SingleStep": 50, "Weight": 0}),
        ui.Label({"Text": "px", "Weight": 0}),
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
    ui.Label({"Text": "Frame Range:", "Weight": 0, "StyleSheet": "color: #FF9800; font-weight: bold;"}),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "SetInPoint", "Text": "IN", "Weight": 1, "StyleSheet": "QPushButton { background-color: #2196F3; color: white; border-radius: 4px; padding: 4px; }"}),
        ui.Label({"ID": "InPointLabel", "Text": "---", "Weight": 0, "StyleSheet": "color: #2196F3;"}),
        ui.Button({"ID": "SetOutPoint", "Text": "OUT", "Weight": 1, "StyleSheet": "QPushButton { background-color: #2196F3; color: white; border-radius: 4px; padding: 4px; }"}),
        ui.Label({"ID": "OutPointLabel", "Text": "---", "Weight": 0, "StyleSheet": "color: #2196F3;"}),
        ui.Button({"ID": "ClearRange", "Text": "Clear", "Weight": 1, "StyleSheet": "QPushButton { background-color: #607D8B; color: white; border-radius: 4px; padding: 4px; }"}),
    ]),
    ui.HGroup({"Weight": 0}, [
        ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable source clip after processing", "Checked": True}),
    ]),
    ui.HGroup({"Weight": 0}, [
        ui.CheckBox({"ID": "LivePreview", "Text": "Live Preview on slider change", "Checked": False, "StyleSheet": "color: #FF9800;"}),
    ]),
    ui.VGap(2),
    ui.Label({"ID": "Status", "Text": "Ready", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 14px; font-weight: bold;"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "SAMClickMask", "Text": "SAM MASK", "Weight": 1, "StyleSheet": "QPushButton { background-color: #E91E63; color: white; font-weight: bold; border-radius: 5px; padding: 6px; }"}),
        ui.Button({"ID": "ShowPreview", "Text": "PREVIEW", "Weight": 1, "StyleSheet": "QPushButton { background-color: #9C27B0; color: white; font-weight: bold; border-radius: 5px; padding: 6px; }"}),
        ui.Button({"ID": "ProcessFrame", "Text": "SINGLE FRAME", "Weight": 1, "StyleSheet": "QPushButton { background-color: #2196F3; color: white; font-weight: bold; border-radius: 5px; padding: 6px; }"}),
    ]),
    ui.VGap(2),
    ui.Button({"ID": "ProcessRange", "Text": "PROCESS RANGE", "Weight": 0, "StyleSheet": "QPushButton { background-color: #4CAF50; color: white; font-size: 15px; font-weight: bold; border-radius: 6px; padding: 10px; }"}),
    ui.Button({"ID": "Cancel", "Text": "CANCEL", "Weight": 0, "StyleSheet": "QPushButton { background-color: #f44336; color: white; font-weight: bold; border-radius: 5px; padding: 4px; }"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "ToggleTrack1", "Text": "TOGGLE TRACK 1", "Weight": 1, "StyleSheet": "QPushButton { background-color: #607D8B; color: white; border-radius: 4px; padding: 3px; }"}),
        ui.Button({"ID": "OpenFusion", "Text": "OPEN FUSION", "Weight": 1, "StyleSheet": "QPushButton { background-color: #FF9800; color: white; border-radius: 4px; padding: 3px; }"}),
    ]),
    ui.VGap(2),
    ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 3, "StyleSheet": "background: #111; color: #0f0; font-family: monospace; font-size: 10px; border-radius: 4px;"}),
    ui.Button({"ID": "AboutBtn", "Text": "About", "Weight": 0, "StyleSheet": "QPushButton { background-color: #333; color: #999; font-size: 11px; border-radius: 4px; padding: 2px; }"}),
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

def log(msg):
    print(msg)
    items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"

def status(msg): items["Status"].Text = msg

def get_settings():
    return {"alpha_method": items["AlphaMethod"].CurrentIndex, "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
            "despill_strength": items["DespillSlider"].Value / 100.0, "refiner_strength": items["RefinerSlider"].Value / 100.0,
            "despeckle_enabled": items["DespeckleCheck"].Checked, "despeckle_size": items["DespeckleSize"].Value,
            "export_format": items["ExportFormat"].CurrentIndex, "output_mode": items["OutputMode"].CurrentIndex}

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

def on_set_in_point(ev):
    cf, _ = get_current_frame_info()
    frame_range["in_frame"] = cf
    items["InPointLabel"].Text = str(cf)
    log(f"IN: {cf}")

def on_set_out_point(ev):
    cf, _ = get_current_frame_info()
    frame_range["out_frame"] = cf
    items["OutPointLabel"].Text = str(cf)
    log(f"OUT: {cf}")

def on_clear_range(ev):
    frame_range["in_frame"] = frame_range["out_frame"] = None
    items["InPointLabel"].Text = items["OutPointLabel"].Text = "---"
    log("Range cleared")

def on_browse_output(ev):
    folder = fu.RequestDir(items["OutputPath"].Text)
    if folder:
        items["OutputPath"].Text = str(folder)
        _save_output_path(str(folder))
        log(f"Output: {folder}")

def on_despill_changed(ev):
    items["DespillValue"].Text = f"{items['DespillSlider'].Value / 100.0:.2f}"
    if items["LivePreview"].Checked and cached_source["frame"] is not None: reprocess_with_cached()

def on_refiner_changed(ev):
    items["RefinerValue"].Text = f"{items['RefinerSlider'].Value / 100.0:.2f}"
    if items["LivePreview"].Checked and cached_source["frame"] is not None: reprocess_with_cached()

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

def generate_sam2_mask(frame, pos_pts, neg_pts):
    import cv2, numpy as np, torch
    try:
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        log("Loading SAM2...")
        status("Loading SAM2...")
        ckpt = str(CORRIDORKEY_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
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

def on_sam_click_mask(ev):
    log("Opening SAM2...")
    status("Click on subject")
    items["AlphaMethod"].CurrentIndex = 1
    open_sam_click_window()

def create_checkerboard(h, w, sz=20):
    import numpy as np
    c = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            c[y, x] = [180, 180, 180] if ((x // sz) + (y // sz)) % 2 == 0 else [120, 120, 120]
    return c

def composite_over_checker(fg, alpha, sz=20):
    import numpy as np
    h, w = fg.shape[:2]
    chk = create_checkerboard(h, w, sz)
    a = alpha[:, :, 0] if len(alpha.shape) == 3 else alpha
    a3 = np.stack([a, a, a], axis=2)
    return (fg * a3 + chk * (1 - a3)).astype(np.uint8)

def grab_background_frame():
    """Try to grab a frame from the track below the green screen for composite background."""
    import cv2
    try:
        cf, fps = get_current_frame_info()
        # Check tracks below the source (V1 has green screen, check V2+ for bg plates
        # OR if green screen is on V2+, check V1)
        track_count = timeline.GetTrackCount("video")
        for track_idx in range(1, track_count + 1):
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
    viewer_script = str(CORRIDORKEY_ROOT / "resolve_plugin" / "preview_viewer.py")
    python_exe = str(CORRIDORKEY_ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / ("python.exe" if sys.platform == "win32" else "python"))
    subprocess.Popen(
        [python_exe, viewer_script, json.dumps(paths)],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    log("Preview launched")

def save_output(fg, matte, path, fmt):
    import cv2, numpy as np
    m = matte[:, :, 0] if len(matte.shape) == 3 else matte
    if fmt == 0:
        fb = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        au = (m * 255).astype(np.uint8)
        cv2.imwrite(str(path), cv2.merge([fb[:,:,0], fb[:,:,1], fb[:,:,2], au]))
    elif fmt == 1: cv2.imwrite(str(path), (m * 255).astype(np.uint8))
    elif fmt == 2: cv2.imwrite(str(path), cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))

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
            ci = {"mediaPoolItem": imp[0], "startFrame": 0, "endFrame": 1, "trackIndex": 2, "recordFrame": cs, "mediaType": 1}
            if media_pool.AppendToTimeline([ci]):
                if items["DisableTrack1"].Checked and clip:
                    clip.SetClipEnabled(False)
                    log(f"Disabled source clip: {os.path.basename(fp)}")
                status("DONE! Track 2")
            else: status("MediaPool only")
        else: status("Done - MediaPool")
    except Exception as e:
        status("ERROR!")
        log(f"ERROR: {e}")
        import traceback; log(traceback.format_exc())
        with open(str(Path(tempfile.gettempdir()) / "ck_error.txt"), "w") as ef: ef.write(traceback.format_exc())

def on_show_preview(ev): process_current_frame(preview_only=True)
def on_process_frame(ev): process_current_frame(preview_only=False)

processing_cancelled = False

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

def on_cancel(ev):
    global processing_cancelled
    processing_cancelled = True
    log("Cancelling...")

def on_toggle_track1(ev):
    try:
        if timeline:
            cur = timeline.GetIsTrackEnabled("video", 1)
            timeline.SetTrackEnable("video", 1, not cur)
            status(f"Track 1 {'enabled' if not cur else 'disabled'}")
    except: pass

def on_open_fusion(ev):
    try: resolve.OpenPage("fusion"); status("Fusion opened")
    except: pass

def on_close(ev): disp.ExitLoop()

win.On.DespillSlider.SliderMoved = on_despill_changed
win.On.RefinerSlider.SliderMoved = on_refiner_changed
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
