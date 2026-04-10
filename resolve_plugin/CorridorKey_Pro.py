"""CorridorKey Pro - Neural Green Screen for DaVinci Resolve
Enhanced with SAM2 Click-to-Mask, Frame Range, Export Modes
"""
import sys, os, site, tempfile
from pathlib import Path

venv_packages = r"D:\New AI Projects\CorridorKey\.venv\Lib\site-packages"
site.addsitedir(venv_packages)
sys.path.insert(0, venv_packages)
sys.path.insert(0, r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules")
sys.path.insert(0, r"D:\New AI Projects\CorridorKey")
sys.path.insert(0, r"D:\New AI Projects\CorridorKey\resolve_plugin")

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

win = disp.AddWindow({"ID": "CK", "WindowTitle": "CorridorKey Pro", "Geometry": [100, 100, 450, 750]}, [
    ui.VGroup({"Spacing": 6, "Margin": 10}, [
        ui.Label({"Text": "CorridorKey Pro", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 18, "Bold": True}), "StyleSheet": "color: #4CAF50;"}),
        ui.HGroup({"Spacing": 10}, [ui.Label({"Text": "Alpha Method:", "MinimumSize": [90, 0]}), ui.ComboBox({"ID": "AlphaMethod"})]),
        ui.HGroup({"Spacing": 10}, [ui.Label({"Text": "Screen Type:", "MinimumSize": [90, 0]}), ui.ComboBox({"ID": "ScreenType"})]),
        ui.HGroup({"Spacing": 10}, [ui.Label({"Text": "Despill:", "MinimumSize": [90, 0]}), ui.Slider({"ID": "DespillSlider", "Minimum": 0, "Maximum": 100, "Value": 50, "Orientation": "Horizontal"}), ui.Label({"ID": "DespillValue", "Text": "0.50", "MinimumSize": [35, 0]})]),
        ui.HGroup({"Spacing": 10}, [ui.Label({"Text": "Refiner:", "MinimumSize": [90, 0]}), ui.Slider({"ID": "RefinerSlider", "Minimum": 0, "Maximum": 100, "Value": 100, "Orientation": "Horizontal"}), ui.Label({"ID": "RefinerValue", "Text": "1.00", "MinimumSize": [35, 0]})]),
        ui.HGroup({"Spacing": 10}, [ui.CheckBox({"ID": "DespeckleCheck", "Text": "Auto Despeckle", "Checked": True}), ui.SpinBox({"ID": "DespeckleSize", "Minimum": 50, "Maximum": 1000, "Value": 400, "SingleStep": 50}), ui.Label({"Text": "px"})]),
        ui.HGroup({"Spacing": 10}, [ui.Label({"Text": "Export:", "MinimumSize": [90, 0]}), ui.ComboBox({"ID": "ExportFormat"})]),
        ui.HGroup({"Spacing": 10}, [ui.Label({"Text": "Output:", "MinimumSize": [90, 0]}), ui.ComboBox({"ID": "OutputMode"})]),
        ui.HGroup({"Spacing": 5}, [ui.Label({"Text": "Save To:", "MinimumSize": [90, 0]}), ui.LineEdit({"ID": "OutputPath", "Text": _load_output_path(), "ReadOnly": True}), ui.Button({"ID": "BrowseOutput", "Text": "...", "MinimumSize": [30, 0], "MaximumSize": [30, 26]})]),
        ui.Label({"Text": "Frame Range:", "StyleSheet": "color: #FF9800; font-weight: bold;"}),
        ui.HGroup({"Spacing": 8}, [
            ui.Button({"ID": "SetInPoint", "Text": "IN", "MinimumSize": [0, 26], "StyleSheet": "background: #2196F3; color: white;"}),
            ui.Label({"ID": "InPointLabel", "Text": "---", "MinimumSize": [50, 0], "StyleSheet": "color: #2196F3;"}),
            ui.Button({"ID": "SetOutPoint", "Text": "OUT", "MinimumSize": [0, 26], "StyleSheet": "background: #2196F3; color: white;"}),
            ui.Label({"ID": "OutPointLabel", "Text": "---", "MinimumSize": [50, 0], "StyleSheet": "color: #2196F3;"}),
            ui.Button({"ID": "ClearRange", "Text": "Clear", "MinimumSize": [0, 26], "StyleSheet": "background: #607D8B; color: white;"})]),
        ui.HGroup({"Spacing": 10}, [ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable Track 1 after processing", "Checked": True})]),
        ui.HGroup({"Spacing": 10}, [ui.CheckBox({"ID": "LivePreview", "Text": "Live Preview on slider change", "Checked": False, "StyleSheet": "color: #FF9800;"})]),
        ui.Label({"ID": "Status", "Text": "Ready", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 14px; font-weight: bold;", "MinimumSize": [0, 28]}),
        ui.Button({"ID": "SAMClickMask", "Text": "SAM CLICK-TO-MASK", "MinimumSize": [0, 38], "StyleSheet": "background: #E91E63; color: white; font-size: 13px; font-weight: bold;"}),
        ui.Button({"ID": "ShowPreview", "Text": "SHOW PREVIEW", "MinimumSize": [0, 45], "StyleSheet": "background: #9C27B0; color: white; font-size: 14px; font-weight: bold;"}),
        ui.Button({"ID": "ProcessFrame", "Text": "PROCESS FRAME", "MinimumSize": [0, 35], "StyleSheet": "background: #2196F3; color: white; font-size: 12px; font-weight: bold;"}),
        ui.HGroup({"Spacing": 10}, [
            ui.Button({"ID": "ProcessRange", "Text": "PROCESS RANGE", "MinimumSize": [0, 35], "StyleSheet": "background: #4CAF50; color: white; font-weight: bold;"}),
            ui.Button({"ID": "Cancel", "Text": "CANCEL", "MinimumSize": [0, 35], "StyleSheet": "background: #f44336; color: white; font-weight: bold;"})]),
        ui.HGroup({"Spacing": 10}, [
            ui.Button({"ID": "ToggleTrack1", "Text": "TOGGLE TRACK 1", "MinimumSize": [0, 26], "StyleSheet": "background: #607D8B; color: white;"}),
            ui.Button({"ID": "OpenFusion", "Text": "OPEN FUSION", "MinimumSize": [0, 26], "StyleSheet": "background: #FF9800; color: white;"})]),
        ui.TextEdit({"ID": "Log", "ReadOnly": True, "MinimumSize": [0, 70], "StyleSheet": "background: #111; color: #0f0; font-family: monospace; font-size: 10px;"})])
])

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
        ckpt = r"D:\New AI Projects\CorridorKey\sam2_weights\sam2.1_hiera_small.pt"
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

def show_preview_window(orig_bgr, keyed_rgb, alpha):
    import cv2, numpy as np, base64
    a2d = alpha[:, :, 0] if len(alpha.shape) == 3 else alpha
    orig_rgb = cv2.cvtColor(orig_bgr, cv2.COLOR_BGR2RGB)
    comp = composite_over_checker(keyed_rgb, a2d)
    matte_vis = (a2d * 255).astype(np.uint8) if a2d.max() <= 1 else a2d
    # Save preview images to output folder
    preview_dir = Path(items["OutputPath"].Text) / "_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(preview_dir / "original.png"), orig_bgr)
    cv2.imwrite(str(preview_dir / "keyed.png"), cv2.cvtColor(comp, cv2.COLOR_RGB2BGR))
    matte_out = cv2.cvtColor(matte_vis, cv2.COLOR_GRAY2BGR) if len(matte_vis.shape) == 2 else matte_vis
    cv2.imwrite(str(preview_dir / "matte.png"), matte_out)
    fg_out = cv2.cvtColor(keyed_rgb, cv2.COLOR_RGB2BGR)
    cv2.imwrite(str(preview_dir / "foreground.png"), fg_out)
    # Open folder so user can inspect
    os.startfile(str(preview_dir))
    log(f"Preview saved to: {preview_dir}")
    log("  original.png | keyed.png | matte.png | foreground.png")

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
                if items["DisableTrack1"].Checked: timeline.SetTrackEnable("video", 1, False)
                status("DONE! Track 2")
            else: status("MediaPool only")
        else: status("Done - MediaPool")
    except Exception as e:
        status("ERROR!")
        log(f"ERROR: {e}")
        import traceback; log(traceback.format_exc())
        with open(r"D:\ck_error.txt", "w") as ef: ef.write(traceback.format_exc())

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
win.On.CK.Close = on_close

log("CorridorKey Pro Ready")
log("SAM2 | Frame Range | Export Modes")
win.Show()
disp.RunLoop()
win.Hide()
