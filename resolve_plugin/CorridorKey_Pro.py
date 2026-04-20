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
import sys, os, site, tempfile, math, queue, threading
from pathlib import Path

# WHAT IT DOES: Finds the CorridorKey engine folder (neural-net code + .venv + model weights)
#   by checking in order: 1) CORRIDORKEY_ROOT env var, 2) corridorkey_path.txt in the script
#   dir or its parent, 3) sibling "CorridorKey" folder two levels up, 4) legacy dev location
#   D:\New AI Projects\CorridorKey, 5) ~/CorridorKey. Raises a clear error if none work.
# DEPENDS-ON: nothing — pure filesystem probe.
# AFFECTS: returns a pathlib.Path. Does not modify sys.path itself.
def find_corridorkey_root():
    # Fusion's script runner doesn't always define __file__ — fall back to the known
    # install location so the plugin doesn't silent-fail at startup.
    try:
        script_dir = Path(__file__).parent
    except NameError:
        script_dir = Path(r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility")
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

# Session dir for v2 viewer IPC — one per plugin process, lives in %TEMP%.
# Holds fg.png, alpha.png, meta.json, optional v1_underlay.png, and live_params.json.
# The v2 viewer polls live_params.json for slider state (viewer writes it too) and
# reloads fg/alpha PNGs when the panel signals "rekeying:false" in that same JSON.
# A single atomic .tmp→os.replace pattern is used for every write.
SESSION_DIR = Path(tempfile.gettempdir()) / f"corridorkey_session_{os.getpid()}"

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

# Thread-safe queues — background thread posts UI updates and import tasks here;
# the main-thread timer drains them so Resolve's UIDispatcher stays safe.
_ui_queue = queue.Queue()
_import_queue = queue.Queue()
_main_thread_id = threading.get_ident()

# Global state caches — these persist between button clicks during one session
last_preview_data = {"original": None, "keyed": None, "alpha": None}
cached_source = {"frame": None, "file_path": None, "frame_num": None}
cached_processor = {"proc": None}  # Holds loaded AI model to avoid reloading every frame
sam_points = {"positive": [], "negative": [], "frame": None}
frame_range = {"in_frame": None, "out_frame": None}
_viewer_proc = None  # Tracks preview viewer subprocess — killed on plugin close

# WHAT IT DOES: Guarantees cleanup when Resolve shuts down — kills the preview viewer
#   subprocess and releases the CUDA context held by the cached neural-net model.
#   Without this, Resolve hangs and the user has to kill a stale python.exe in Task
#   Manager before Resolve will restart (the orphaned Python holds GPU/CUDA open).
# DEPENDS-ON: atexit (stdlib) — Python calls this on ANY exit (normal or signal).
# AFFECTS: Terminates _viewer_proc, clears cached_processor, frees CUDA memory.
import atexit
def _cleanup_on_exit():
    # WHAT IT DOES: Kills the viewer subprocess on Resolve exit. That's all.
    #   proc.cleanup() and torch.cuda.empty_cache() were here but caused Resolve
    #   to hang on shutdown — CUDA unload blocked the Python interpreter.
    #   Windows reclaims GPU memory automatically when the process exits.
    # DEPENDS-ON: atexit (stdlib).
    # AFFECTS: terminates _viewer_proc only.
    global _viewer_proc
    try:
        if _viewer_proc is not None and _viewer_proc.poll() is None:
            _viewer_proc.kill()
            _viewer_proc = None
    except Exception:
        pass
atexit.register(_cleanup_on_exit)

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
    ui.HGroup({"Weight": 0, "Spacing": 0}, [
        ui.Button({"ID": "HeaderCK", "Text": "CorridorKey Pro", "Weight": 1, "StyleSheet": "QPushButton { background: transparent; color: #0ff; font-size: 14px; font-weight: bold; border: none; padding: 2px; } QPushButton:hover { color: #5ff; }"}),
        ui.Label({"Text": "—", "Weight": 0, "StyleSheet": "color: #0ff; font-size: 14px; font-weight: bold;"}),
        ui.Button({"ID": "HeaderSW", "Text": "StuntWorks Action Cinema", "Weight": 1, "StyleSheet": "QPushButton { background: transparent; color: #0ff; font-size: 14px; font-weight: bold; border: none; padding: 2px; } QPushButton:hover { color: #5ff; }"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 8}, [
        ui.Label({"Text": "Alpha:", "Weight": 0}),
        ui.ComboBox({"ID": "AlphaMethod", "Weight": 2}),
        ui.Label({"Text": "Screen:", "Weight": 0}),
        ui.ComboBox({"ID": "ScreenType", "Weight": 2}),
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

    ui.VGap(2),
    ui.Label({"ID": "Status", "Text": "Ready", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 14px; font-weight: bold;"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
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
    ui.HGroup({"Weight": 0, "Spacing": 6}, [
        ui.Button({"ID": "YouTubeBtn", "Text": "▶ YouTube", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a1a1a; color: #cc3300; font-size: 11px; font-weight: bold; border-radius: 4px; padding: 3px; border: 1px solid #cc3300; }"}),
        ui.Button({"ID": "KofiBtn", "Text": "☕ Ko-fi", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a1a1a; color: #FF5E5B; font-size: 11px; font-weight: bold; border-radius: 4px; padding: 3px; border: 1px solid #FF5E5B; }"}),
        ui.Button({"ID": "AboutBtn", "Text": "About", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a1a1a; color: #556; font-size: 11px; border-radius: 4px; padding: 3px; }"}),
    ]),
    ui.Label({"Text": "AI: Niko Pueringer / Corridor Digital  •  Plugin: Roberto & Elvis Lopez / StuntWorks", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #334; font-size: 10px;"}),
    ui.VGap(4),
    ui.Timer({"ID": "PollTimer", "Interval": 500}),
    ui.HGroup({"Weight": 0, "Spacing": 8}, [
        ui.Button({"ID": "KillViewer", "Text": "KILL VIEWER", "Weight": 1,
                   "StyleSheet": "background-color: #3a1a1a; color: #f55; padding: 5px 14px; border: 1px solid #f55; border-radius: 12px; font-size: 12px; font-weight: 600;"}),
        ui.Button({"ID": "ClosePanel", "Text": "CLOSE PANEL", "Weight": 1,
                   "StyleSheet": "background-color: #2a2a2a; color: #aaa; padding: 5px 14px; border: 1px solid #555; border-radius: 12px; font-size: 12px; font-weight: 600;"}),
    ]),
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
try:
    items["PollTimer"].Start()
except Exception:
    pass

# WHAT IT DOES: Writes a message to the log — thread-safe.
#   Main thread writes directly; background threads post to _ui_queue (drained by timer).
# AFFECTS: Log TextEdit widget in the UI panel
def log(msg):
    print(msg)
    if threading.get_ident() == _main_thread_id:
        items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"
    else:
        _ui_queue.put(("log", msg))

# WHAT IT DOES: Updates the cyan status label — thread-safe (same queue pattern as log)
def status(msg):
    if threading.get_ident() == _main_thread_id:
        items["Status"].Text = msg
    else:
        _ui_queue.put(("status", msg))

# WHAT IT DOES: Reads all UI controls and returns a dict of current processing settings.
#   Despill/refiner/despeckle defaults are used here; _merge_live_params() overrides them
#   with whatever the user has dialed in the viewer's live sliders.
# DEPENDS-ON: Combo boxes and checkboxes in the panel
def get_settings():
    return {
        "alpha_method": items["AlphaMethod"].CurrentIndex,
        "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
        "despill_strength": 0.5,    # viewer-owned; overridden by _merge_live_params
        "refiner_strength": 1.0,    # viewer-owned; overridden by _merge_live_params
        "despeckle_enabled": False, # viewer-owned; overridden by _merge_live_params
        "despeckle_size": 400,      # viewer-owned; overridden by _merge_live_params
        "export_format": items["ExportFormat"].CurrentIndex,
        "output_mode": items["OutputMode"].CurrentIndex,
    }

# WHAT IT DOES: Overrides panel's despill / despeckle settings with the v2 viewer's
#   slider state (if the viewer has been opened and written live_params.json). The
#   viewer is the source of truth for visual params once opened — so PROCESS RANGE
#   uses the values the user dialed in live, not the stale LineEdit values.
#   Refiner is NOT merged — it's a full-re-key parameter owned by the panel.
# DEPENDS-ON: SESSION_DIR, live_params.json format written by preview_viewer_v2.py.
# AFFECTS: returns a new settings dict with viewer overrides applied (or original
#   settings if the viewer hasn't written yet or JSON is unreadable).
def _merge_live_params(settings):
    try:
        import json
        lp_path = SESSION_DIR / "live_params.json"
        if not lp_path.exists():
            return settings
        with open(lp_path, "r", encoding="utf-8") as f:
            lp = json.load(f)
        out = dict(settings)
        if "despill" in lp:
            try: out["despill_strength"] = max(0.0, min(1.0, float(lp["despill"])))
            except (ValueError, TypeError): pass
        if "despeckle" in lp:
            out["despeckle_enabled"] = bool(lp["despeckle"])
        if "despeckleSize" in lp:
            try: out["despeckle_size"] = max(50, min(2000, int(lp["despeckleSize"])))
            except (ValueError, TypeError): pass
        return out
    except Exception:
        return settings

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


# WHAT IT DOES: Runs SAM2 video predictor across the entire frame range in one pass.
#   Exports every frame in [in_f, out_f) as a JPEG to a temp directory, loads the
#   SAM2 video predictor, places the user's click points on the anchor frame
#   (defaults to range frame 0), then calls propagate_in_video() which yields one
#   mask per frame. Returns a dict {range_relative_index: float32_mask}.
# DEPENDS-ON: SAM2 video predictor weights at CK_ROOT/sam2_weights/sam2.1_hiera_small.pt,
#   cv2 VideoCapture on fp, ~50 MB disk space per 100 frames (95% JPEG), CUDA VRAM.
# AFFECTS: writes then deletes a temp JPEG dir. Returns mask dict (no disk writes kept).
# DANGER ZONE HIGH: Can fill disk on very long ranges. Each frame is a JPEG on disk.
#   breaks: if disk space < ~0.5 MB * frame_count, or SAM2 weights missing.
def run_sam2_video_propagation(fp, ss, cs, in_f, out_f, pos_pts, neg_pts, anchor_frame_abs):
    import cv2, numpy as np, torch, shutil, tempfile
    ckpt = str(CK_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
    cfg  = "configs/sam2.1/sam2.1_hiera_s.yaml"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dur = out_f - in_f

    # Map the absolute clicked frame to a range-relative index for the predictor.
    # If the click was outside the range (or never recorded), anchor to frame 0.
    if anchor_frame_abs is not None and in_f <= anchor_frame_abs < out_f:
        anchor_rel = anchor_frame_abs - in_f
    else:
        anchor_rel = 0

    tmp_dir = Path(tempfile.mkdtemp(prefix="ck_sam2_frames_"))
    try:
        # --- Export frames ---
        log(f"SAM2 video: exporting {dur} frames to {tmp_dir} ...")
        status(f"SAM2: exporting {dur} frames...")
        cap = cv2.VideoCapture(fp)
        if not cap.isOpened():
            log("SAM2 video: cannot open video"); return {}
        for i in range(dur):
            sf = ss + (in_f + i - cs)
            cap.set(cv2.CAP_PROP_POS_FRAMES, sf)
            ret, frame = cap.read()
            if not ret:
                log(f"SAM2 video: skipped unreadable frame {in_f + i}")
                continue
            cv2.imwrite(str(tmp_dir / f"{i:06d}.jpg"), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
        cap.release()

        # --- Load video predictor and propagate ---
        log(f"SAM2 video: loading predictor, anchor=range-frame {anchor_rel} ...")
        status("SAM2: loading video model...")
        from sam2.build_sam import build_sam2_video_predictor
        predictor = build_sam2_video_predictor(cfg, ckpt, device=device)

        all_pts = ([[p[0], p[1]] for p in pos_pts] +
                   [[p[0], p[1]] for p in neg_pts])
        labs    = ([1] * len(pos_pts) + [0] * len(neg_pts))

        masks = {}
        with torch.inference_mode():
            # offload_video_to_cpu keeps JPEG frames in RAM not VRAM — critical
            # because Resolve already uses 2-4 GB of VRAM on a working timeline.
            # async_loading_frames pipelines disk reads with GPU compute.
            state = predictor.init_state(
                video_path=str(tmp_dir),
                offload_video_to_cpu=True,
                async_loading_frames=True,
            )
            predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=anchor_rel,
                obj_id=1,
                points=np.array(all_pts, dtype=np.float32),
                labels=np.array(labs,    dtype=np.int32),
                clear_old_points=True,
            )
            status("SAM2: propagating...")
            for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(state):
                # mask_logits shape: [num_objects, 1, H, W] — squeeze to [H, W] float32
                mask = (mask_logits[0] > 0.0).squeeze().cpu().numpy().astype(np.float32)
                masks[frame_idx] = mask
                if frame_idx % 20 == 0:
                    log(f"SAM2: propagated {frame_idx}/{dur}")
                    status(f"SAM2: {frame_idx}/{dur} frames")

            # reset_state releases SAM2's internal CUDA buffers before we drop
            # the predictor — prevents the GPU memory leak on Windows (issue #258).
            try:
                predictor.reset_state(state)
            except Exception:
                pass

        # Delete state first (holds CUDA tensors), then predictor (holds weights).
        # Wrong order leaks VRAM because the predictor holds references into state.
        del state
        del predictor
        if torch.cuda.is_available():
            torch.cuda.synchronize()  # wait for GPU to finish before clearing cache
            torch.cuda.empty_cache()
        log(f"SAM2 video propagation done — {len(masks)} masks")
        return masks

    except Exception as e:
        log(f"SAM2 video propagation error: {e}")
        import traceback; log(traceback.format_exc())
        return {}
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


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
    # v2 flow: writes fg.png + alpha.png + v1_underlay.png + meta.json + live_params.json
    # to SESSION_DIR atomically, then launches preview_viewer_v2.py in --persistent mode.
    # The v2 viewer has its OWN despill / despeckle sliders — that's where the user drags
    # (Fusion UIManager sliders can't be trusted in the current Resolve build). Panel sets
    # rekeying:false at the end so an already-running viewer reloads the new PNGs.
    import cv2, numpy as np, subprocess, json
    a2d = alpha[:, :, 0] if len(alpha.shape) == 3 else alpha
    log(f"Matte debug — dtype:{a2d.dtype} min:{a2d.min():.4f} max:{a2d.max():.4f} mean:{a2d.mean():.4f}")
    if a2d.dtype in (np.float32, np.float64):
        matte_vis = (np.clip(a2d / max(a2d.max(), 1e-6), 0, 1) * 255).astype(np.uint8)
    else:
        if a2d.max() > 0 and a2d.max() < 255:
            matte_vis = (a2d.astype(np.float32) / a2d.max() * 255).astype(np.uint8)
        else:
            matte_vis = a2d.astype(np.uint8)
    log(f"Matte after norm — min:{matte_vis.min()} max:{matte_vis.max()} mean:{matte_vis.mean():.1f}")
    bg_frame = grab_background_frame()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    def _atomic_imwrite(final_path, img):
        base, ext = os.path.splitext(str(final_path))
        tmp = base + ".tmp" + ext
        if cv2.imwrite(tmp, img):
            os.replace(tmp, str(final_path))

    def _atomic_json(path, data):
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))

    # fg.png — write the ORIGINAL SOURCE frame (full green spill intact) so the
    # viewer's despill slider has real work to do. The NN's predicted fg is too
    # clean: despill_opencv against it produces a sub-1/255 change per pixel
    # (invisible). Compositing math: comp = fg * alpha + bg * (1-alpha). Since
    # alpha is the NN's smart matte (hair, edges), the composite quality still
    # comes from the NN — we're just using raw source as the color source for
    # the despill-able part. This matches how dedicated keyers show live despill.
    _atomic_imwrite(SESSION_DIR / "fg.png", orig_bgr)
    _atomic_imwrite(SESSION_DIR / "alpha.png", matte_vis)

    # Optional V1 underlay — viewer's BG:V1 button composites over it
    if bg_frame is not None:
        h, w = orig_bgr.shape[:2]
        if bg_frame.shape[:2] != (h, w):
            bg_frame = cv2.resize(bg_frame, (w, h))
        _atomic_imwrite(SESSION_DIR / "v1_underlay.png", bg_frame)
        log("Background plate saved for V1 underlay")

    # meta.json — frame/timebase info for debugging and future per-frame state
    try:
        _cf, _fps = get_current_frame_info()
    except Exception:
        _cf, _fps = 0, 24.0
    _atomic_json(SESSION_DIR / "meta.json", {
        "frame_num": int(_cf),
        "fps": float(_fps),
        "width": int(orig_bgr.shape[1]),
        "height": int(orig_bgr.shape[0]),
    })

    # live_params.json — viewer owns slider state between launches. Preserve it if
    # already present (user has dialed in); otherwise seed from Fusion panel values.
    # Always set rekeying:false so a running viewer reloads the new PNGs.
    lp_path = SESSION_DIR / "live_params.json"
    if lp_path.exists():
        try:
            with open(lp_path, "r", encoding="utf-8") as f:
                lp = json.load(f)
        except Exception:
            lp = {}
    else:
        _s = get_settings()
        lp = {
            "despill": float(_s.get("despill_strength", 1.0)),
            "despeckle": bool(_s.get("despeckle_enabled", False)),
            "despeckleSize": int(_s.get("despeckle_size", 400)),
            "background": "checker",
        }
    lp["rekeying"] = False
    _atomic_json(lp_path, lp)

    # Launch v2 viewer — reuse existing subprocess if still alive. The mtime bump
    # on live_params.json above signals a live viewer to reload.
    global _viewer_proc
    viewer_script = str(CK_ROOT / "resolve_plugin" / "preview_viewer_v2.py")
    if not os.path.exists(viewer_script):
        # Dev fallback — Plugin repo is the canonical source, engine repo mirrors it
        viewer_script = r"D:\New AI Projects\CorridorKey-Plugin\resolve_plugin\preview_viewer_v2.py"
    python_exe = str(CK_PYTHON)
    if _viewer_proc is not None and _viewer_proc.poll() is None:
        log("Preview updated (existing v2 window)")
        return
    # DANGER ZONE FRAGILE: Kill any zombie/orphan viewer before spawning a fresh one.
    # Without this, clicking Preview repeatedly leaves stale python.exe processes holding
    # VRAM/GPU. poll() returns None if still running — terminate first, hard-kill if needed.
    if _viewer_proc is not None:
        if _viewer_proc.poll() is None:
            try:
                _viewer_proc.terminate()
                try:
                    _viewer_proc.wait(timeout=2)
                except Exception:
                    _viewer_proc.kill()
            except Exception:
                pass
        _viewer_proc = None
    env = os.environ.copy()
    env["CORRIDORKEY_PARENT_PID"] = str(os.getpid())
    _viewer_proc = subprocess.Popen(
        [python_exe, viewer_script, "--persistent", "--session", str(SESSION_DIR),
         "--parent-pid", str(os.getpid())],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
        env=env,
    )
    log("v2 preview launched (persistent mode)")

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
        # SAFETY: if the playhead has moved since the frame was cached, the cached frame
        # is stale and re-keying it would silently show the wrong image. Compare timeline
        # frame (cf, absolute playhead) — NOT "frame_num" which is source-video offset.
        try:
            cur_tf, _ = get_current_frame_info()
            cached_tf = cached_source.get("timeline_frame")
            if cached_tf is not None and cur_tf != cached_tf:
                log(f"Cached timeline {cached_tf} != playhead {cur_tf} — falling through to full re-key")
                process_current_frame(preview_only=True)
                return
        except Exception as _fn_err:
            log(f"Frame identity check failed, aborting live re-key: {_fn_err}")
            return
        settings = _merge_live_params(get_settings())
        status("Updating...")
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        # BLOCKER FIX: reuse the cached model instead of reloading weights per slider drag.
        # Without this, every slider change spawned a fresh CUDA processor (seconds of reload + VRAM churn).
        proc = cached_processor.get("proc")
        if proc is None:
            proc = CorridorKeyProcessor(device="cuda")
            cached_processor["proc"] = proc
        ah = generate_alpha_hint(frame, settings)
        fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"], refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"], despeckle_size=settings["despeckle_size"])
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength}")
        res = proc.process_frame(fr, ah, ps)
        fg, mt = res.get("fg"), res.get("alpha")
        if fg is not None:
            try: log(f"FG stats — dtype:{fg.dtype} min:{float(fg.min()):.4f} max:{float(fg.max()):.4f} mean R:{float(fg[..., 0].mean()):.4f} G:{float(fg[..., 1].mean()):.4f} B:{float(fg[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG stat error: {_e}")
        if fg is not None and mt is not None:
            if len(mt.shape) == 3: mt = mt[:, :, 0]
            last_preview_data["original"] = frame.copy()
            last_preview_data["keyed"] = (fg * 255).astype(np.uint8)
            last_preview_data["alpha"] = mt.copy()
            # Send RAW fg (pre-despill) to viewer — viewer applies despill_opencv live
            # per slider. Falls back to despilled fg if wrapper didn't preserve raw.
            _fg_viewer = res.get("fg_raw", fg)
            _is_raw = _fg_viewer is not fg and "fg_raw" in res
            try: log(f"FG->viewer — raw:{_is_raw} mean R:{float(_fg_viewer[..., 0].mean()):.4f} G:{float(_fg_viewer[..., 1].mean()):.4f} B:{float(_fg_viewer[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG->viewer log err: {_e}")
            show_preview_window(frame, (_fg_viewer * 255).astype(np.uint8), mt)
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
    global last_preview_data, cached_source, timeline, media_pool
    # Refresh in case timeline was opened after script loaded
    if project:
        timeline = project.GetCurrentTimeline()
        media_pool = project.GetMediaPool()
    import cv2, numpy as np
    status("PROCESSING...")
    log("=" * 35)
    try:
        if not timeline or not media_pool: status("ERROR: No timeline!"); return
        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips: status("ERROR: No clips!"); return
        settings = _merge_live_params(get_settings())
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
        # Store timeline frame separately — "frame_num" above is source-video offset
        # (clip.GetLeftOffset() + cf - cs), not the timeline position. reprocess_with_cached
        # needs timeline position to know if the playhead moved.
        cached_source["timeline_frame"] = cf
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
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"], refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"], despeckle_size=settings["despeckle_size"])
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength}")
        res = proc.process_frame(fr, ah, ps)
        cn = Path(fp).stem
        od = Path(items["OutputPath"].Text) / f"CK_{cn}"
        od.mkdir(parents=True, exist_ok=True)
        op = od / f"CK_{cn}_{cf:06d}.png"
        fg, mt = res.get("fg"), res.get("alpha")
        if fg is not None:
            try: log(f"FG stats — dtype:{fg.dtype} min:{float(fg.min()):.4f} max:{float(fg.max()):.4f} mean R:{float(fg[..., 0].mean()):.4f} G:{float(fg[..., 1].mean()):.4f} B:{float(fg[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG stat error: {_e}")
        if fg is not None and mt is not None:
            save_output(fg, mt, op, settings["export_format"])
            log(f"Saved: {op.name}")
        if len(mt.shape) == 3: mt = mt[:, :, 0]
        last_preview_data["original"], last_preview_data["keyed"], last_preview_data["alpha"] = frame.copy(), (fg * 255).astype(np.uint8), mt.copy()
        if preview_only:
            # Raw NN fg to viewer — viewer applies despill live per slider drag.
            _fg_viewer = res.get("fg_raw", fg)
            _is_raw = _fg_viewer is not fg and "fg_raw" in res
            try: log(f"FG->viewer — raw:{_is_raw} mean R:{float(_fg_viewer[..., 0].mean()):.4f} G:{float(_fg_viewer[..., 1].mean()):.4f} B:{float(_fg_viewer[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG->viewer log err: {_e}")
            show_preview_window(frame, (_fg_viewer * 255).astype(np.uint8), mt)
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
_range_running = False  # Guard — prevents double-click while processing

def on_process_range(ev):
    # WHAT IT DOES: Starts range processing in a background thread so the UI stays
    #   live and the Cancel button works between frames.
    # DEPENDS-ON: timeline, media_pool, cached_processor, frame_range globals.
    # AFFECTS: Disk (PNGs), MediaPool (sequence), Timeline (places on V above source).
    global processing_cancelled, timeline, media_pool, _range_running
    if _range_running:
        status("Already running — hit CANCEL first"); return
    processing_cancelled = False
    # Refresh in case timeline was opened after script loaded
    if project:
        timeline = project.GetCurrentTimeline()
        media_pool = project.GetMediaPool()
    import cv2, numpy as np, time, threading
    log("=" * 35)
    log("PROCESS RANGE")
    if not timeline or not media_pool: status("ERROR: No timeline!"); return
    # Find the clip at the current playhead — same logic as process_current_frame
    cf, fps = get_current_frame_info()
    source_track = 1
    clip = None
    track_count = timeline.GetTrackCount("video")
    for ti in range(1, track_count + 1):
        clips_on_track = timeline.GetItemListInTrack("video", ti) or []
        for c in clips_on_track:
            if c.GetStart() <= cf < c.GetEnd():
                source_track = ti
                clip = c
                break
        if clip:
            break
    if not clip: status("ERROR: No clip at playhead!"); return
    output_track = source_track + 1
    log(f"Source on V{source_track} → output to V{output_track}")
    cs, ce = clip.GetStart(), clip.GetEnd()
    in_f = frame_range["in_frame"] if frame_range["in_frame"] is not None else cs
    out_f = frame_range["out_frame"] if frame_range["out_frame"] is not None else ce
    in_f, out_f = max(in_f, cs), min(out_f, ce)
    if out_f <= in_f: status("Invalid range!"); return
    dur = out_f - in_f
    log(f"Range: {in_f}-{out_f} ({dur} frames)")
    mpi = clip.GetMediaPoolItem()
    props = mpi.GetClipProperty() if mpi else {}
    fp = props.get("File Path", "")
    ss = clip.GetLeftOffset()
    settings = _merge_live_params(get_settings())
    cn = Path(fp).stem
    od = Path(items["OutputPath"].Text) / f"CK_{cn}"
    od.mkdir(parents=True, exist_ok=True)
    log(f"Saving to: {od}")
    # Kill viewer on main thread before background thread opens VideoCapture.
    # Reuses on_kill_viewer to avoid global scoping issues with nested _run() closure.
    on_kill_viewer(None)
    from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    if cached_processor["proc"] is None:
        log("Loading AI (first time)...")
        status("Loading AI...")
        cached_processor["proc"] = CorridorKeyProcessor(device="cuda")
        log("Model loaded!")
    else:
        log("AI ready (cached)")
    proc = cached_processor["proc"]
    ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"],
                            refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"],
                            despeckle_size=settings["despeckle_size"])
    log(f"Settings: despill={ps.despill_strength} refiner={ps.refiner_strength} despeckle={ps.despeckle_enabled}")

    def _run():
        global _range_running
        _range_running = True
        ofs = []
        pr = 0

        # --- SAM2 video propagation (runs ONCE before the per-frame loop) ---
        # If the user has placed SAM2 click points and selected SAM2 mode, we run
        # the video predictor across the entire range up front. This gives us one
        # mask per frame instead of re-running SAM2 per frame (which was the bug).
        # Falls back to per-frame generate_alpha_hint if propagation fails or if
        # the user is in Simple Chroma mode (alpha_method != 1).
        sam2_video_masks = {}
        if (settings.get("alpha_method") == 1 and
                (sam_points.get("positive") or sam_points.get("negative"))):
            log("SAM2 mode — running video propagation for full range...")
            sam2_video_masks = run_sam2_video_propagation(
                fp, ss, cs, in_f, out_f,
                sam_points.get("positive", []),
                sam_points.get("negative", []),
                sam_points.get("frame"),
            )
            if sam2_video_masks:
                log(f"SAM2 video: {len(sam2_video_masks)} masks ready — using for all frames")
            else:
                log("SAM2 video propagation returned no masks — falling back to per-frame hint")

        cap = cv2.VideoCapture(fp)
        try:
            if not cap.isOpened(): status("Cannot open video"); return
            st = time.time()
            for tf in range(in_f, out_f):
                if processing_cancelled:
                    log(f"Cancelled at frame {pr}/{dur}")
                    status(f"CANCELLED — {pr} frames saved")
                    break
                sf = ss + (tf - cs)
                cap.set(cv2.CAP_PROP_POS_FRAMES, sf)
                ret, frame = cap.read()
                if not ret: continue
                # Use the pre-computed SAM2 video mask if we have one for this frame,
                # otherwise fall back to the per-frame alpha hint (chroma or single-frame SAM2).
                range_idx = tf - in_f
                if sam2_video_masks and range_idx in sam2_video_masks:
                    ah = sam2_video_masks[range_idx]
                else:
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
                    status(f"{pr}/{dur} ({fpsr:.1f}fps, {rem:.0f}s left)")
                    if pr % 10 == 0: log(f"{pr}/{dur}")
        finally:
            try: cap.release()
            except Exception: pass
            _range_running = False

        if not ofs or processing_cancelled: return
        log(f"Done: {len(ofs)} frames in {time.time()-st:.1f}s")
        status("Importing to MediaPool...")
        # Hand off to main thread via queue — MediaPool/Timeline API must run on main thread
        _import_queue.put({
            "ofs": ofs,
            "output_track": output_track,
            "source_track": source_track,
            "in_f": in_f,
            "settings": settings,
        })

    threading.Thread(target=_run, daemon=True).start()

# WHAT IT DOES: Sets the cancel flag so the range processing loop stops on next iteration
def on_cancel(ev):
    global processing_cancelled, _range_running
    processing_cancelled = True
    _range_running = False  # Unlock immediately so user can restart right away
    status("Cancelling — wait for current frame to finish...")
    log("Cancelling...")

# WHAT IT DOES: Toggles Track 1 visibility on/off — lets user quickly show/hide source footage
def on_toggle_track1(ev):
    try:
        if timeline:
            cur = timeline.GetIsTrackEnabled("video", 1)
            timeline.SetTrackEnable("video", 1, not cur)
            status(f"Track 1 {'enabled' if not cur else 'disabled'}")
    except: pass

# WHAT IT DOES: Runs on the main thread (called by PollTimer) — drains _ui_queue to
#   update log/status widgets safely, then processes any pending import tasks.
# DEPENDS-ON: _ui_queue, _import_queue, _do_import()
# AFFECTS: Log widget, Status label, MediaPool, Timeline
def on_poll_timer(ev):
    while not _ui_queue.empty():
        try:
            kind, msg = _ui_queue.get_nowait()
            if kind == "log":
                items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"
            elif kind == "status":
                items["Status"].Text = msg
        except Exception:
            pass
    while not _import_queue.empty():
        try:
            task = _import_queue.get_nowait()
            _do_import(task)
        except Exception as e:
            log(f"Import queue error: {e}")

# WHAT IT DOES: Imports processed PNGs to MediaPool and places them on the output track.
#   Must run on the main thread — Resolve's MediaPool/Timeline API is not thread-safe.
# DEPENDS-ON: media_pool, timeline globals; task dict from _import_queue
# AFFECTS: MediaPool (CorridorKey bin), Timeline (output track), source track enable state
def _do_import(task):
    ofs = task["ofs"]
    output_track = task["output_track"]
    source_track = task["source_track"]
    in_f = task["in_f"]
    settings = task["settings"]
    try:
        root = media_pool.GetRootFolder()
        ckb = None
        for f in root.GetSubFolderList():
            if f.GetName() == "CorridorKey": ckb = f; break
        if not ckb: ckb = media_pool.AddSubFolder(root, "CorridorKey")
        media_pool.SetCurrentFolder(ckb)
        imp = media_pool.ImportMedia(ofs)
        if not imp: status("Import failed — check MediaPool bin"); return
        log(f"Imported {len(imp)} items to MediaPool")
        if settings["output_mode"] in [0, 2]:
            current_tracks = timeline.GetTrackCount("video")
            while current_tracks < output_track:
                timeline.AddTrack("video")
                current_tracks += 1
                log(f"Added video track V{current_tracks}")
            seq_item = imp[0]
            log(f"Placing on V{output_track} — frames 0-{len(ofs)-1}")
            ci_list = [{"mediaPoolItem": seq_item, "startFrame": 0, "endFrame": len(ofs) - 1,
                        "trackIndex": output_track, "recordFrame": int(in_f), "mediaType": 1}]
            result = media_pool.AppendToTimeline(ci_list)
            log(f"AppendToTimeline result: {result}")
            if result:
                if items["DisableTrack1"].Checked:
                    timeline.SetTrackEnable("video", source_track, False)
                status(f"DONE! {len(ofs)} frames on V{output_track}")
            else:
                status("Timeline place failed — clips are in MediaPool")
        else:
            status(f"{len(ofs)} frames in MediaPool")
    except Exception as e:
        import traceback
        status("Import ERROR!")
        log(f"Import error: {e}")
        log(traceback.format_exc())

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
    # Run cleanup in a daemon thread so it can't block the close button.
    # disp.ExitLoop() fires immediately — Resolve window closes right away.
    import threading
    def _cleanup():
        try:
            proc = cached_processor.get("proc")
            if proc is not None:
                try: proc.cleanup()
                except Exception: pass
                cached_processor["proc"] = None
        except Exception: pass
    threading.Thread(target=_cleanup, daemon=True).start()
    disp.ExitLoop()

win.On.SetInPoint.Clicked = on_set_in_point
win.On.SetOutPoint.Clicked = on_set_out_point
win.On.ClearRange.Clicked = on_clear_range
win.On.BrowseOutput.Clicked = on_browse_output
win.On.ShowPreview.Clicked = on_show_preview
win.On.ProcessFrame.Clicked = on_process_frame
win.On.ProcessRange.Clicked = on_process_range
win.On.Cancel.Clicked = on_cancel
win.On.ToggleTrack1.Clicked = on_toggle_track1
win.On.OpenFusion.Clicked = on_open_fusion

# WHAT IT DOES: Terminates the running preview viewer subprocess immediately.
#   Waits up to 2 seconds for a clean exit, then hard-kills if still running.
#   Resets _viewer_proc to None so Preview can spawn a fresh one next click.
# DEPENDS-ON: _viewer_proc global, subprocess module (already imported in show_preview_window)
# AFFECTS: _viewer_proc global, status label
def on_kill_viewer(ev):
    global _viewer_proc
    if _viewer_proc is not None:
        try:
            _viewer_proc.terminate()
            try:
                _viewer_proc.wait(timeout=2)
            except Exception:
                _viewer_proc.kill()
        except Exception:
            pass
        _viewer_proc = None
    status("Viewer killed — click Preview to reopen")

win.On.KillViewer.Clicked = on_kill_viewer
win.On.ClosePanel.Clicked = lambda ev: on_close(ev)
win.On.PollTimer.Timeout = on_poll_timer

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

# WHAT IT DOES: Header link — CorridorKey Pro → Corridor Digital website
def on_header_ck(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://corridordigital.com"], creationflags=subprocess.CREATE_NO_WINDOW)

# WHAT IT DOES: Header link — StuntWorks Action Cinema → YouTube channel
def on_header_sw(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://www.youtube.com/@StuntworksActionCinema"], creationflags=subprocess.CREATE_NO_WINDOW)

win.On.HeaderCK.Clicked = on_header_ck
win.On.HeaderSW.Clicked = on_header_sw

# WHAT IT DOES: Opens StuntWorks YouTube channel in the system browser
def on_youtube(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://www.youtube.com/@StuntworksActionCinema"], creationflags=subprocess.CREATE_NO_WINDOW)

# WHAT IT DOES: Opens the StuntWorks Ko-fi tip jar in the system browser
def on_kofi(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://ko-fi.com/stuntworks"], creationflags=subprocess.CREATE_NO_WINDOW)

win.On.YouTubeBtn.Clicked = on_youtube
win.On.KofiBtn.Clicked = on_kofi
win.On.AboutBtn.Clicked = on_about
win.On.CK.Close = on_close

log("CorridorKey Pro Ready")
log("SAM2 | Frame Range | Export Modes")
win.Show()
disp.RunLoop()
win.Hide()
