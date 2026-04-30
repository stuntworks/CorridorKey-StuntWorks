# Last modified: 2026-04-27 | Change: Despeckle parity — render path (SINGLE FRAME, BRAW PROCESS RANGE, PROCESS RANGE) now applies the same matte despeckle the viewer applies via render_composite. Previously the despeckle slider in the viewer changed what the user saw but had zero effect on rendered output. Also previously: SAM2 propagation now applies 101px morphological close to bridge inter-dot dips (interior holes), and quality gate is tail-aware so actor-exit frames stay empty instead of being mistaken for mid-range collapse. | Full history: git log
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
import sys, os, site, tempfile, math, queue, threading, io, traceback, shutil
from pathlib import Path

# DANGER ZONE FRAGILE: Resolve's embedded Python sets sys.stdout/stderr to None for
# background threads. Any print() call in a daemon thread crashes silently, killing
# the thread before it runs a single line. Patch them here before any threads start.
# breaks: if removed, all background thread log output silently disappears
if sys.stdout is None: sys.stdout = io.StringIO()
if sys.stderr is None: sys.stderr = io.StringIO()

# WHAT IT DOES: disables ALL tqdm progress bars before SAM2 imports them.
# DANGER ZONE FRAGILE: tqdm writes to Fusion's broken sys.stdout in background threads,
# throwing SystemError. env var fires before any import; monkeypatch covers third-party
# libs that cache the class before the env var takes effect.
# breaks: if removed, SAM2 init_state throws SystemError on first BRAW range run.
import os as _os
_os.environ["TQDM_DISABLE"] = "True"
try:
    from functools import partialmethod
    from tqdm import tqdm as _tqdm_cls
    _tqdm_cls.__init__ = partialmethod(_tqdm_cls.__init__, disable=True)
except Exception:
    pass

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
SESSION_DIR = Path(tempfile.gettempdir()) / "corridorkey_session"

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

# Pre-import cv2 on the main thread so FFMPEG/COM initializes here, not in a daemon thread.
# On Windows, cv2's FFMPEG backend touches COM objects that require a main-thread message pump.
# If cv2 first imports inside a background thread, VideoCapture can hang indefinitely.
try:
    import cv2 as _cv2_preload
except Exception:
    pass

# Thread-safe queues — background thread posts UI updates and import tasks here;
# the main-thread timer drains them so Resolve's UIDispatcher stays safe.
_ui_queue = queue.Queue()
_import_queue = queue.Queue()
_save_queue = queue.Queue()  # thread puts encoded PNG bytes, main thread writes to disk
_main_thread_id = threading.get_ident()

# Global state caches — these persist between button clicks during one session
last_preview_data = {"original": None, "keyed": None, "alpha": None}
cached_source = {"frame": None, "file_path": None, "frame_num": None}
cached_processor = {"proc": None}  # Holds loaded AI model to avoid reloading every frame
# WHAT IT DOES: Holds a CPU-only processor pre-inited at LIVE PREVIEW time for SCRUB RANGE use.
#   Avoids creating a new CPU proc inside the background thread (which triggers torch.compile
#   at img_size=2048 on CPU — a 6+ minute hang). Populated once; reused for all scrub runs.
# DEPENDS-ON: CorridorKeyProcessor(device="cpu"), CORRIDORKEY_SKIP_COMPILE=1 env flag
# AFFECTS: _start_scrub_keying worker (reads this instead of creating its own)
cached_scrub_cpu_proc = {"proc": None}  # CPU proc for SCRUB RANGE — pre-inited, never CUDA
sam_points = {"positive": [], "negative": [], "frame": None}
frame_range = {"in_frame": None, "out_frame": None}
_viewer_proc = None      # Tracks Live Preview subprocess — stays alive while scrubber is open
_scrubber_proc = None   # Tracks SCRUB RANGE subprocess — separate from live preview
_scrubber_frames_dir = None    # TIFF temp dir for scrubber — cleaned up on close/new scrub
_scrub_pending = []          # frames queued for Phase 1 timer-based export (list of (fi, tl_frame))
_scrub_pending_buffers = []  # accumulated BytesIO results from Phase 1
_scrub_pending_ctx = {}      # state dict from on_scrub_range, consumed by on_poll_timer
# Phase 2 keying — main-thread one-frame-per-tick (avoids background thread CUDA deadlock)
_scrub_key_queue   = []      # list of (frame_idx, BytesIO) waiting to be keyed
_scrub_key_ctx     = {}      # keying context: proc, ps, hint_gen, scrub_dir, settings, despill
_scrub_key_done    = 0       # frames successfully keyed so far
_scrub_key_total   = 0       # total frames to key in this run
_proxy_mpi        = None   # MediaPoolItem waiting for Resolve to finish optimized media generation
_proxy_mode_saved = None   # proxy mode value before we enabled it — restored after scrub finishes

# WHAT IT DOES: Guarantees cleanup when Resolve shuts down — kills the preview viewer
#   subprocess and releases the CUDA context held by the cached neural-net model.
#   Without this, Resolve hangs and the user has to kill a stale python.exe in Task
#   Manager before Resolve will restart (the orphaned Python holds GPU/CUDA open).
# DEPENDS-ON: atexit (stdlib) — Python calls this on ANY exit (normal or signal).
# AFFECTS: Terminates _viewer_proc, clears cached_processor, frees CUDA memory.
import atexit
def _cleanup_on_exit():
    # WHAT IT DOES: Kills the viewer subprocess on Resolve exit and WAITS for it to die.
    #   Without wait(), kill() fires the signal but returns immediately — the viewer
    #   python.exe is still alive (holding a CUDA handle) when Resolve tries to restart,
    #   forcing the user to kill it in Task Manager before Resolve will open again.
    #   proc.cleanup() and torch.cuda.empty_cache() were removed here because they caused
    #   Resolve to hang on shutdown (CUDA unload blocked the interpreter). Windows reclaims
    #   GPU memory when the process exits — we just need the process to actually be dead.
    # DEPENDS-ON: atexit (stdlib), subprocess on Windows.
    # AFFECTS: terminates _viewer_proc and waits for full exit before returning.
    global _viewer_proc
    try:
        if _viewer_proc is not None and _viewer_proc.poll() is None:
            pid = _viewer_proc.pid
            _viewer_proc.kill()
            try:
                _viewer_proc.wait(timeout=3)
            except Exception:
                # Still alive after 3 s — force-kill entire process tree
                # (covers any SAM2 child processes the viewer may have spawned)
                try:
                    import subprocess as _sp
                    _sp.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=3
                    )
                except Exception:
                    pass
            _viewer_proc = None
    except Exception:
        pass
    try:
        if _scrubber_proc is not None and _scrubber_proc.poll() is None:
            _scrubber_proc.kill()
            try: _scrubber_proc.wait(timeout=2)
            except Exception: pass
    except Exception:
        pass
    try:
        if _scrubber_frames_dir and Path(_scrubber_frames_dir).exists():
            shutil.rmtree(_scrubber_frames_dir, ignore_errors=True)
    except Exception:
        pass
    # Skip CUDA/torch finalizers — they block 30-60 s on Windows when Resolve terminates
    # the Python session without first firing the window Close event.
    # This handles the path where on_close never ran (crash, force-quit, Resolve killed first).
    os._exit(0)
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

winLayout = ui.VGroup({"Spacing": 4}, [
    ui.HGroup({"Weight": 0, "Spacing": 0}, [
        ui.Button({"ID": "HeaderCK", "Text": "CorridorKey Pro ↗", "Weight": 1, "ToolTip": "Visit CorridorKey Pro website", "StyleSheet": "QPushButton { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(13, 202, 240, 0.20), stop:1 transparent); border: none; border-left: 3px solid #0dcaf0; color: #0dcaf0; font-size: 20px; font-weight: bold; padding: 10px 16px; text-align: left; } QPushButton:hover { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(13, 202, 240, 0.40), stop:1 transparent); color: #fff; border-left: 3px solid #fff; text-decoration: underline; }"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 0}, [
        ui.Button({"ID": "HeaderSW", "Text": "by Stuntworks Cinema ↗", "Weight": 1, "ToolTip": "Visit StuntWorks Cinema on YouTube", "StyleSheet": "QPushButton { background-color: transparent; border: none; border-left: 2px solid #5af; color: #5af; font-size: 13px; padding: 6px 16px; text-align: left; } QPushButton:hover { background-color: rgba(85, 170, 255, 0.15); color: #8cf; text-decoration: underline; }"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 4}, [
        ui.Button({"ID": "YouTubeBtn", "Text": "▶ YouTube", "Weight": 1, "StyleSheet": "QPushButton { background-color: transparent; color: #cc3300; font-size: 11px; font-weight: bold; border-radius: 2px; padding: 4px 12px; border: 1px solid #cc3300; } QPushButton:hover { background-color: #cc3300; color: #fff; }"}),
        ui.Button({"ID": "KofiBtn", "Text": "☕ Ko-fi", "Weight": 1, "StyleSheet": "QPushButton { background-color: transparent; color: #FF5E5B; font-size: 11px; font-weight: bold; border-radius: 2px; padding: 4px 12px; border: 1px solid #FF5E5B; } QPushButton:hover { background-color: #FF5E5B; color: #fff; }"}),
        ui.Button({"ID": "AboutBtn", "Text": "About", "Weight": 1, "StyleSheet": "QPushButton { background-color: transparent; color: #888; font-size: 11px; border-radius: 2px; padding: 4px 12px; border: 1px solid #333; } QPushButton:hover { background-color: #1a1a1a; color: #ccc; border-color: #555; }"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 8}, [
        ui.Label({"Text": "Screen:", "Weight": 0}),
        ui.ComboBox({"ID": "ScreenType", "Weight": 2, "StyleSheet": "QComboBox { background-color: #1a1a1a; border: 1px solid #333; border-radius: 3px; padding: 4px 8px; color: #ccc; } QComboBox:hover { border-color: #0dcaf0; background-color: #222; } QComboBox::drop-down { border-left: 1px solid #333; width: 24px; } QComboBox::down-arrow { border-top: 5px solid #0dcaf0; border-left: 4px solid transparent; border-right: 4px solid transparent; width: 0; height: 0; }"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 6}, [
        ui.Label({"Text": "Refiner:", "Weight": 0}),
        ui.Slider({"ID": "RefinerStrength", "Minimum": 0, "Maximum": 100, "Value": 75, "Weight": 3,
                   "Orientation": "Horizontal", "SingleStep": 1,
                   "StyleSheet": "QSlider::groove:horizontal { height: 6px; background: #222; border-radius: 3px; } QSlider::sub-page:horizontal { background: #0dcaf0; border-radius: 3px; } QSlider::handle:horizontal { background: #fff; border: 2px solid #0dcaf0; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; } QSlider::handle:horizontal:hover { background: #0dcaf0; border-color: #fff; }"}),
        ui.SpinBox({"ID": "RefinerInput", "Minimum": 0, "Maximum": 100, "Value": 75, "Weight": 0,
                    "StyleSheet": "QSpinBox { background-color: #1a1a1a; color: #ccc; border: 1px solid #333; padding: 4px; border-radius: 3px; min-width: 50px; } QSpinBox::up-button, QSpinBox::down-button { background-color: #2a2a2a; border: none; width: 16px; } QSpinBox::up-button:hover, QSpinBox::down-button:hover { background-color: #0dcaf0; }"}),
        ui.Label({"Text": "%", "Weight": 0, "StyleSheet": "color: #888; font-size: 11px;"}),
    ]),
    ui.Label({"Text": "Edge detail. Re-run Process Range after changing.", "Weight": 0,
              "StyleSheet": "color: #888; font-size: 10px; padding-left: 2px;"}),
    # Mask Margin and Soften sliders moved to the live preview viewer
    # (preview_viewer_v2.py) where they belong — that's where the user dials
    # them in real time against a visible matte. Panel-side duplicates were
    # removed 2026-04-26 per Berto. The viewer writes the values to
    # live_params.json which _merge_live_params reads at render time.
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Export:", "Weight": 0}),
        ui.ComboBox({"ID": "ExportFormat", "Weight": 2, "StyleSheet": "QComboBox { background-color: #1a1a1a; border: 1px solid #333; border-radius: 3px; padding: 4px 8px; color: #ccc; } QComboBox:hover { border-color: #0dcaf0; background-color: #222; } QComboBox::drop-down { border-left: 1px solid #333; width: 24px; } QComboBox::down-arrow { border-top: 5px solid #0dcaf0; border-left: 4px solid transparent; border-right: 4px solid transparent; width: 0; height: 0; }"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Output:", "Weight": 0}),
        ui.ComboBox({"ID": "OutputMode", "Weight": 2, "StyleSheet": "QComboBox { background-color: #1a1a1a; border: 1px solid #333; border-radius: 3px; padding: 4px 8px; color: #ccc; } QComboBox:hover { border-color: #0dcaf0; background-color: #222; } QComboBox::drop-down { border-left: 1px solid #333; width: 24px; } QComboBox::down-arrow { border-top: 5px solid #0dcaf0; border-left: 4px solid transparent; border-right: 4px solid transparent; width: 0; height: 0; }"}),
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
        ui.Label({"ID": "InPointLabel", "Text": "FULL", "Weight": 0, "StyleSheet": "color: #7ab;"}),
        ui.Button({"ID": "SetOutPoint", "Text": "OUT", "Weight": 1, "StyleSheet": "QPushButton { background-color: #3a5a6a; color: #7ab; border-radius: 4px; padding: 4px; font-weight: bold; }"}),
        ui.Label({"ID": "OutPointLabel", "Text": "FULL", "Weight": 0, "StyleSheet": "color: #7ab;"}),
        ui.Button({"ID": "ClearRange", "Text": "Clear", "Weight": 1, "StyleSheet": "QPushButton { background-color: transparent; color: #f66; font-size: 11px; border-radius: 3px; padding: 4px; border: 1px solid #f66; } QPushButton:hover { background-color: rgba(255, 102, 102, 0.2); }"}),
    ]),
    ui.HGroup({"Weight": 0}, [
        ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable source clip after processing  (uncheck to leave source visible)", "Checked": True,
                    "StyleSheet": "color: #aaa; font-size: 11px;"}),
    ]),

    ui.VGap(2),
    ui.Label({"ID": "Status", "Text": "Ready", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 14px; font-weight: bold;"}),
    ui.VGap(4),
    ui.Label({"ID": "Progress", "Text": "", "Weight": 0,
        "StyleSheet": "background: #111; border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #fff; font-size: 10px;"}),
    ui.VGap(2),
    # ── STEP 1 — LIVE PREVIEW ──────────────────────────────────────────────
    ui.Label({"Text": "STEP 1 — LIVE PREVIEW", "Weight": 0,
              "StyleSheet": "color: #0dcaf0; font-size: 11px; font-weight: bold; padding: 2px 0 0 2px;"}),
    ui.Button({"ID": "ShowPreview", "Text": "LIVE PREVIEW", "Weight": 0,
        "StyleSheet": "QPushButton { background-color: transparent; color: #0dcaf0; font-size: 13px; font-weight: bold; padding: 10px 14px; border: 2px solid #0dcaf0; border-radius: 3px; } QPushButton:hover { background-color: rgba(13, 202, 240, 0.15); color: #5ff; border-color: #5ff; }"}),
    # ── STEP 2 — PAINT MASK (optional) ────────────────────────────────────
    ui.VGap(2),
    ui.Label({"Text": "STEP 2 — PAINT MASK  (optional)", "Weight": 0,
              "StyleSheet": "color: #a5f; font-size: 11px; font-weight: bold; padding: 2px 0 0 2px;"}),
    ui.Label({"Text": "Open Live Preview → click the person to isolate them from the green screen", "Weight": 0,
              "StyleSheet": "color: #888; font-size: 10px; padding-left: 2px;"}),
    # ── STEP 3 — SCRUB RANGE (optional) ───────────────────────────────────
    ui.VGap(2),
    ui.Label({"Text": "STEP 3 — SCRUB RANGE  (optional)", "Weight": 0,
              "StyleSheet": "color: #a5f; font-size: 11px; font-weight: bold; padding: 2px 0 0 2px;"}),
    ui.Button({"ID": "ScrubRange", "Text": "SCRUB RANGE", "Weight": 0,
        "ToolTip": "Keys every frame in your IN/OUT range so you can drag through the result.\nTIP: Much faster when Resolve Optimized Media is generated for this clip.",
        "StyleSheet": "QPushButton { background-color: transparent; color: #a5f; font-size: 14px; font-weight: bold; padding: 12px; border: 2px solid #a5f; border-radius: 3px; } QPushButton:hover { background-color: rgba(170, 85, 255, 0.2); color: #c9f; } QPushButton:pressed { background-color: rgba(170, 85, 255, 0.4); }"}),
    ui.HGroup({"Weight": 0, "Spacing": 6}, [
        ui.Label({"Text": "Max frames:", "Weight": 0,
            "StyleSheet": "color: #888; font-size: 10px; padding-left: 4px;"}),
        ui.SpinBox({"ID": "ScrubMaxFrames", "Minimum": 0, "Maximum": 9999, "Value": 0, "Weight": 1,
            "ToolTip": "0 = all frames in range (frame-accurate).\nSet a number to sample evenly — useful for long clips.",
            "StyleSheet": "color: #ccc; font-size: 10px;"}),
        ui.Label({"Text": "  (0 = all frames)", "Weight": 0,
            "StyleSheet": "color: #555; font-size: 10px;"}),
    ]),
    ui.Label({"Text": "Tip: Generate Optimized Media in Resolve first for faster scrubbing", "Weight": 0,
        "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccb84a; font-size: 10px; font-style: italic;"}),
    ui.Label({"Text": "Preview every frame in your range before committing to a full render", "Weight": 0,
        "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #aaa; font-size: 10px;"}),
    # ── STEP 4 — PROCESS RANGE ────────────────────────────────────────────
    ui.VGap(2),
    ui.Label({"Text": "STEP 4 — PROCESS RANGE", "Weight": 0,
              "StyleSheet": "color: #5b5; font-size: 11px; font-weight: bold; padding: 2px 0 0 2px;"}),
    ui.Button({"ID": "ProcessRange", "Text": "PROCESS RANGE", "Weight": 0, "StyleSheet": "QPushButton { background-color: #5b5; color: #000; font-size: 15px; font-weight: bold; padding: 16px; border: none; border-radius: 3px; } QPushButton:hover { background-color: #6c6; } QPushButton:pressed { background-color: #4a4; }"}),
    ui.Button({"ID": "Cancel", "Text": "CANCEL", "Weight": 0, "StyleSheet": "QPushButton { background-color: transparent; color: #f66; font-size: 12px; padding: 10px; border: 1px solid #f66; border-radius: 3px; } QPushButton:hover { background-color: rgba(255, 102, 102, 0.2); }"}),
    # ── Utility ───────────────────────────────────────────────────────────
    ui.Button({"ID": "ProcessFrame", "Text": "SINGLE FRAME", "Weight": 0,
        "StyleSheet": "QPushButton { background-color: transparent; color: #5af; font-size: 11px; font-weight: bold; padding: 6px 14px; border: 1px solid #5af; border-radius: 3px; } QPushButton:hover { background-color: rgba(85, 170, 255, 0.15); color: #8cf; border-color: #8cf; }"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "ToggleTrack1", "Text": "TOGGLE TRACK 1", "Weight": 1, "StyleSheet": "QPushButton { background-color: transparent; color: #7ab; font-size: 11px; font-weight: bold; border-radius: 3px; padding: 6px; border: 1px solid #7ab; } QPushButton:hover { background-color: rgba(119, 170, 187, 0.2); color: #9cf; border-color: #9cf; }"}),
        ui.Button({"ID": "OpenFusion", "Text": "OPEN FUSION", "Weight": 1, "StyleSheet": "QPushButton { background-color: transparent; color: #a85; font-size: 11px; font-weight: bold; border-radius: 3px; padding: 6px; border: 1px solid #a85; } QPushButton:hover { background-color: rgba(170, 136, 85, 0.2); color: #cb9; border-color: #cb9; }"}),
    ]),
    ui.VGap(2),
    ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1, "StyleSheet": "background: #111; color: #0ff; font-family: monospace; font-size: 10px; border-radius: 4px; border: 1px solid #222; min-height: 60px; max-height: 120px;"}),
    ui.Label({"Text": "AI: Niko Pueringer / Corridor Digital  •  Plugin: Roberto & Elvis Lopez / StuntWorks", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #666; font-size: 10px;"}),
    ui.VGap(4),
    ui.Timer({"ID": "PollTimer", "Interval": 500}),
    ui.HGroup({"Weight": 0, "Spacing": 8}, [
        ui.Button({"ID": "KillViewer", "Text": "KILL VIEWER", "Weight": 1,
                   "StyleSheet": "background-color: #3a1a1a; color: #f55; padding: 5px 14px; border: 1px solid #f55; border-radius: 12px; font-size: 12px; font-weight: 600;"}),
        ui.Button({"ID": "ClosePanel", "Text": "CLOSE PANEL", "Weight": 1,
                   "StyleSheet": "QPushButton { background-color: transparent; color: #aaa; padding: 5px 14px; border: 1px solid #aaa; border-radius: 12px; font-size: 12px; font-weight: 600; } QPushButton:hover { background-color: rgba(170, 170, 170, 0.15); color: #fff; border-color: #fff; }"}),
    ]),
])

# WHAT IT DOES: Prevents two copies of the panel from opening at the same time.
#   Writes a lock file with the current PID on launch, deletes it on close.
#   If the lock file exists and the PID is still alive, shows an error and exits.
# DEPENDS-ON: tempfile, os, ctypes (Windows)
# AFFECTS: script startup — exits early if another instance is running
_INSTANCE_LOCK = Path(tempfile.gettempdir()) / "corridorkey_instance.lock"

def _check_single_instance():
    if _INSTANCE_LOCK.exists():
        try:
            pid = int(_INSTANCE_LOCK.read_text().strip())
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                err_win = disp.AddWindow(
                    {"ID": "CKErr", "WindowTitle": "CorridorKey Already Open", "Geometry": [300, 200, 400, 110]},
                    [ui.VGroup({"Spacing": 10, "Margin": 16}, [
                        ui.Label({"Text": "CorridorKey Pro is already open. Close the existing panel first.",
                                  "Alignment": {"AlignHCenter": True}}),
                        ui.Button({"ID": "CKErrOK", "Text": "OK"}),
                    ])]
                )
                def _close_err(ev): disp.ExitLoop()
                err_win.On.CKErrOK.Clicked = _close_err
                err_win.On.CKErr.Close = _close_err
                err_win.Show()
                disp.RunLoop()
                err_win.Hide()
                sys.exit(0)
        except (ValueError, OSError):
            pass
    _INSTANCE_LOCK.write_text(str(os.getpid()))

_check_single_instance()

win = disp.AddWindow({"ID": "CK", "WindowTitle": "CorridorKey Pro", "Geometry": [100, 50, 500, 950]}, winLayout)

items = win.GetItems()

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
try: items["Progress"].Visible = False
except Exception: pass

# WHAT IT DOES: Writes a message to both the console and the in-panel log window.
#   Also writes to a debug log file so background thread output is always recoverable.
# AFFECTS: Log TextEdit widget, _ck_debug_log file
_ck_debug_log = Path(tempfile.gettempdir()) / "corridorkey_debug.txt"
def log(msg):
    try: print(msg)  # sys.stdout is None in Resolve background threads — must guard
    except Exception: pass
    try: items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"
    except Exception: pass
    try:
        with open(_ck_debug_log, "a", encoding="utf-8") as f: f.write(msg + "\n")
    except Exception: pass

# WHAT IT DOES: Updates the cyan status label at the center of the panel
def status(msg):
    try: items["Status"].Text = msg
    except Exception: pass

# WHAT IT DOES: Reads all UI controls and returns a dict of current processing settings.
#   Despill/refiner/despeckle defaults are used here; _merge_live_params() overrides them
#   with whatever the user has dialed in the viewer's live sliders.
# DEPENDS-ON: Combo boxes and checkboxes in the panel
def get_settings():
    return {
        "alpha_method": 0,
        "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
        "despill_strength": 0.5,    # viewer-owned; overridden by _merge_live_params
        "refiner_strength": max(0.0, min(1.0, int(items["RefinerStrength"].Value) / 100.0)),
        "despeckle_enabled": True,  # viewer-owned; overridden by _merge_live_params — default ON matches viewer checkbox default
        "despeckle_size": 400,      # viewer-owned; overridden by _merge_live_params
        "export_format": items["ExportFormat"].CurrentIndex,
        "output_mode": items["OutputMode"].CurrentIndex,
        # margin/soften sliders moved to viewer; defaults of 0 are overridden by
        # _merge_live_params() which pulls the actual values from live_params.json
        "sam2_margin": 0.0,
        "sam2_soften": 0.0,
        # FG SOURCE: "nn" (default = model FG, original behavior) | "source"
        # (use the original plate inside the matte — Mocha-style; rescues warm
        # wardrobe like yellow shirts that the NN paints pink) | "blend" (50/50).
        # Viewer-owned; overridden by _merge_live_params.
        "fg_source": "nn",
        # SAM2 INVERT: False (default) = SAM2 KEEPS what you click (subject mask).
        # True = SAM2 REMOVES what you click (garbage matte for imperfect screens —
        # click on floor / crew / props / taped seams). Viewer-owned; overridden
        # by _merge_live_params.
        "sam_invert": False,
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
        if "sam2_margin" in lp:
            try: out["sam2_margin"] = max(0.0, float(lp["sam2_margin"]))
            except (ValueError, TypeError): pass
        if "sam2_soften" in lp:
            try: out["sam2_soften"] = max(0.0, float(lp["sam2_soften"]))
            except (ValueError, TypeError): pass
        if "fg_source" in lp:
            _v = str(lp["fg_source"]).lower()
            if _v in ("nn", "source", "blend"):
                out["fg_source"] = _v
        if "sam_invert" in lp:
            try: out["sam_invert"] = bool(lp["sam_invert"])
            except (ValueError, TypeError): pass
        if "sam_positive" in lp or "sam_negative" in lp:
            sam_points["positive"] = [tuple(p) for p in lp.get("sam_positive", [])]
            sam_points["negative"] = [tuple(p) for p in lp.get("sam_negative", [])]
            sam_points["frame"]    = lp.get("sam_anchor_frame", None)
            # Auto-enable SAM2 mode whenever positive points exist — panel dropdown
            # does not need to be set to SAM2; the viewer places points = intent to use SAM2.
            if lp.get("alpha_method") == 1 or sam_points["positive"]:
                out["alpha_method"] = 1
        # If a SAM2 gate file exists on disk, always activate SAM2 mode regardless of
        # the panel dropdown or whether live_params.json still has the points. The gate
        # file persists across viewer restarts; the points in live_params.json do not.
        if (SESSION_DIR / "sam2_mask.png").exists():
            out["alpha_method"] = 1
        return out
    except Exception:
        return settings

# WHAT IT DOES: Gets the current playhead position as a frame number and the timeline fps.
#   Returns cf in the SAME absolute timecode-frame coordinate system that clip.GetStart()
#   uses. Both must stay in the same system or fn = GetLeftOffset() + (cf - cs) goes wrong.
# DEPENDS-ON: Resolve project settings for frame rate, timeline for timecode
# DANGER ZONE CRITICAL: DO NOT subtract timeline.GetStartFrame() from cf here.
#   clip.GetStart() returns ABSOLUTE frame numbers matching the timecode conversion below.
#   Subtracting GetStartFrame() makes cf relative while cs stays absolute → cf-cs goes
#   deeply negative → fn clamps to 0 → every seek lands on frame 0 of the source video.
#   Broke April 2026, fixed by reverting. Do not "fix" this without checking both sides.
# DANGER ZONE CRITICAL: DO NOT switch cap.set() calls to CAP_PROP_POS_MSEC.
#   POS_MSEC at non-integer fps (24fps = 41.666ms/frame) has floating-point off-by-one:
#   frame N seeks to N/fps*1000 ms which rounds just below the frame boundary → reads N-1.
#   Resolve footage is all-intra (ProRes, BRAW, DNxHD) so POS_FRAMES is exact. Keep it.
# breaks: if Resolve returns non-standard timecode format or drop-frame semicolons
def get_current_frame_info():
    try:
        fps = float(project.GetSetting("timelineFrameRate") or 24)
        tc = timeline.GetCurrentTimecode()
        log(f"Timecode raw: '{tc}' fps={fps}")
        parts = tc.replace(";", ":").split(":")
        if len(parts) == 4:
            h, m, s, f = [int(p) for p in parts]
            cf = int(h * 3600 * fps + m * 60 * fps + s * fps + f)
            return max(0, cf), fps
        log(f"Timecode parse failed: '{tc}'")
        return 0, fps
    except Exception as e:
        log(f"get_current_frame_info error: {e}")
        return 0, 24.0

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
    items["InPointLabel"].Text = items["OutPointLabel"].Text = "FULL"
    log("Range cleared")

# WHAT IT DOES: Opens a folder picker for the user to choose where keyed frames are saved
# AFFECTS: OutputPath text field, persistent config file in temp
def on_browse_output(ev):
    folder = fu.RequestDir(items["OutputPath"].Text)
    if folder:
        items["OutputPath"].Text = str(folder)
        _save_output_path(str(folder))
        log(f"Output: {folder}")


# WHAT IT DOES: Expands a binary SAM2 mask outward by SAM2_MATTE_MARGIN pixels.
#   This safety buffer ensures the hard garbage-matte boundary sits outside the
#   actual silhouette, leaving the soft chroma-key edges untouched so the neural
#   keyer can refine them properly. Without dilation SAM2 clips the edges too tight.
# DEPENDS-ON: cv2, numpy
# AFFECTS: Every garbage-matte multiply in generate_alpha_hint() and the range loop.
# DANGER ZONE FRAGILE/MEDIUM: Increase margin on 4K+ footage (pixel count scales up).
#   Too small = edge clipping. Too large = garbage matte stops blocking junk BG.
SAM2_MATTE_MARGIN = 5  # default; overridden at runtime by Sam2Margin slider

def _dilate_sam2_mask(mask_float32, margin=SAM2_MATTE_MARGIN):
    import cv2, numpy as np
    if margin <= 0:
        return mask_float32
    sz = int(margin) * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (sz, sz))
    mask_u8 = (mask_float32 * 255).astype(np.uint8)
    dilated = cv2.dilate(mask_u8, kernel, iterations=1)
    return dilated.astype(np.float32) / 255.0

# WHAT IT DOES: Applies Gaussian blur to the mask boundary to create a soft feathered
#   edge instead of a hard pixel cut. Runs AFTER dilation so the safety buffer is
#   already in place before softening. soften=0 skips entirely (no-op).
# DEPENDS-ON: cv2
# AFFECTS: mask boundary softness — higher values create wider, softer transitions
def _soften_sam2_mask(mask_float32, soften=0):
    import cv2, numpy as np
    if soften <= 0:
        return mask_float32
    # Kernel must be odd — multiply by 2+1 so soften=1 → 3px, soften=5 → 11px, etc.
    sz = int(soften) * 2 + 1
    blurred = cv2.GaussianBlur(mask_float32, (sz, sz), sigmaX=soften * 0.5)
    return blurred


# WHAT IT DOES: Applies the same matte despeckle the viewer uses, so what the
#   user dialed in via the Despeckle slider in the live preview is what the
#   client actually receives in the rendered output. Without this, the viewer's
#   live preview cleans the matte but the rendered alpha skipped the cleanup,
#   silently lying to the user about what their settings produced. Mirrors
#   render_composite() in preview_viewer_v2.py:422-424 exactly.
# DEPENDS-ON: CorridorKeyModule.core.color_utils.clean_matte_opencv
#   settings dict carrying "despeckle_enabled" (bool) and "despeckle_size" (int).
# AFFECTS: returns possibly-cleaned matte; original mt unchanged on failure or
#   when despeckle is off / size is 0 (passes mt through unchanged).
# DANGER ZONE FRAGILE: do NOT bypass the settings check — the viewer can have
#   despeckle off, in which case the render must also leave the matte alone.
def _apply_despeckle_to_alpha(mt, settings):
    if mt is None:
        return mt
    if not settings.get("despeckle_enabled", True):
        return mt
    ds_size = int(settings.get("despeckle_size", 400))
    if ds_size <= 0:
        return mt
    try:
        from CorridorKeyModule.core import color_utils as _cu
        mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
        return _cu.clean_matte_opencv(mt2d, area_threshold=ds_size)
    except Exception as _e:
        try:
            log(f"Despeckle skipped: {_e}")
        except Exception:
            pass
        return mt


# WHAT IT DOES: Generates a chroma-key alpha hint for the neural keyer.
#   SAM2 is no longer applied here — it is applied as a POST-PROCESS gate on the
#   neural keyer's OUTPUT alpha (see _apply_sam2_output_gate). Applying SAM2 to
#   the input hint caused the neural network to interpret the hint incorrectly and
#   produce a dark/empty alpha. Traditional garbage mattes gate the OUTPUT, not the input.
# DEPENDS-ON: AlphaHintGenerator
# AFFECTS: Neural keyer input quality — this is the primary alpha signal into process_frame()
def generate_alpha_hint(frame, settings):
    # WHAT IT DOES: Generates the alpha-hint mask fed to the NN. Mirrors AE's
    #   generate_chroma_hint EXACTLY — inline RGB chroma test + 5x5 Gaussian.
    #   Float32 in [0,1] so the NN sees smooth partial-alpha at hair edges.
    # DANGER ZONE FRAGILE/HIGH/CRITICAL: Do NOT swap to AlphaHintGenerator (HSV).
    #   HSV path flags tan/khaki/olive fabric as screen color (memory:
    #   corridorkey_alpha_hint_hsv_trap.md) AND its morph CLOSE+OPEN at 5x5
    #   collapses hair strands into a binary blob — the NN can't recover detail
    #   the hint already destroyed. AE uses RGB inline; matching that is what
    #   gives DaVinci the same hair-strand sharpness.
    # AFFECTS: NN input quality → matte sharpness, hair detail.
    import numpy as np, cv2
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    if frame_rgb.dtype == np.uint8:
        img = frame_rgb.astype(np.float32) / 255.0
    elif frame_rgb.dtype == np.uint16:
        img = frame_rgb.astype(np.float32) / 65535.0
    else:
        img = frame_rgb.astype(np.float32)
    if settings.get("screen_type", "green") == "green":
        red, green, blue = img[:, :, 0], img[:, :, 1], img[:, :, 2]
        screen_mask = (green > 0.3) & (green > red * 1.2) & (green > blue * 1.2)
    else:
        red, green, blue = img[:, :, 0], img[:, :, 1], img[:, :, 2]
        screen_mask = (blue > 0.3) & (blue > red * 1.2) & (blue > green * 1.2)
    alpha_hint = (~screen_mask).astype(np.float32)
    alpha_hint = cv2.GaussianBlur(alpha_hint, (5, 5), 0)
    return alpha_hint


# WHAT IT DOES: Loads the SAM2 binary mask from sam2_mask.png and returns it dilated,
#   ready to multiply with the neural keyer's OUTPUT alpha as a garbage matte gate.
#   This is the correct place to apply a garbage matte — after keying, not before.
#   Applying it before (on the input hint) caused the neural keyer to produce dark results.
# DEPENDS-ON: SESSION_DIR/sam2_mask.png (written ONLY by the viewer after Apply SAM2), _dilate_sam2_mask
# AFFECTS: Called after proc.process_frame() in single-frame and cached render paths.
# NOTE: sam2_mask.png is separate from alpha.png so Preview Frame cannot overwrite it.
#   alpha.png is the neural keyer output (display); sam2_mask.png is the binary SAM2 gate (render).
def _load_sam2_output_gate(frame_shape, settings):
    import numpy as np, cv2
    if settings.get("alpha_method") != 1:
        log(f"SAM2 output gate: alpha_method={settings.get('alpha_method')} — gate skipped")
        return None
    sam2_png = SESSION_DIR / "sam2_mask.png"
    if not sam2_png.exists():
        log(f"SAM2 output gate: sam2_mask.png not found at {sam2_png} — no garbage matte applied")
        return None
    raw = cv2.imread(str(sam2_png), cv2.IMREAD_GRAYSCALE)
    if raw is None:
        log("SAM2 output gate: could not read sam2_mask.png")
        return None
    h, w = frame_shape[:2]
    _, raw = cv2.threshold(raw, 127, 255, cv2.THRESH_BINARY)
    if raw.shape != (h, w):
        raw = cv2.resize(raw, (w, h), interpolation=cv2.INTER_NEAREST)
    mask = raw.astype(np.float32) / 255.0
    mask = _dilate_sam2_mask(mask, margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
    mask = _soften_sam2_mask(mask, soften=settings.get("sam2_soften", 0))
    log(f"SAM2 output gate loaded — coverage {mask.mean():.3f} ({int(mask.sum())} px foreground)")
    return mask

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
        return None  # caller (generate_alpha_hint) handles None — falls back to plain chroma hint


# WHAT IT DOES: Runs SAM2 video predictor across the entire frame range in two passes —
#   forward (anchor → last frame) then backward (anchor → first frame) — so every frame
#   in [in_f, out_f) receives a mask regardless of where the user clicked.
#   Exports every frame as a JPEG to a temp directory, loads the SAM2 video predictor,
#   places the user's click points on the anchor frame (defaults to range frame 0), runs
#   propagate_in_video() forward then reverse=True backward, merges both results.
#   Returns a dict {range_relative_index: float32_mask}.
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

        # BRAW path: caller passes a directory of TIFF files (4:4:4, no seek needed).
        # Normal path: caller passes a video file path for VideoCapture.
        _tif_files = []
        _cap = None
        if Path(fp).is_dir():
            _tif_files = sorted(Path(fp).glob("*.tif*"))
            log(f"SAM2 video: {len(_tif_files)} TIFF frames from {Path(fp).name}")
        else:
            log(f"SAM2 video: opening {os.path.basename(fp)}")
            _cap = cv2.VideoCapture(fp)
            if not _cap.isOpened():
                log("SAM2 video: cannot open video"); return {}
        for i in range(dur):
            if _tif_files:
                fidx = in_f + i  # in_f=0 for BRAW path
                frame = cv2.imread(str(_tif_files[fidx])) if fidx < len(_tif_files) else None
                if frame is None:
                    log(f"SAM2 video: skipped unreadable frame {in_f + i}")
                    continue
            else:
                sf = ss + (in_f + i - cs)
                _cap.set(cv2.CAP_PROP_POS_FRAMES, sf)
                ret, frame = _cap.read()
                if not ret:
                    log(f"SAM2 video: skipped unreadable frame {in_f + i}")
                    continue
            cv2.imwrite(str(tmp_dir / f"{i:06d}.jpg"), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
        if _cap:
            _cap.release()

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
            # async_loading_frames=False — Resolve's embedded Python deadlocks on
            # background threads (same issue that killed threaded PROCESS RANGE).
            # tqdm in SAM2's frame loader writes to sys.stdout — Fusion's patched stdout
            # throws SystemError. Redirect to stderr (a safe stream) during init_state only.
            _ck_save_out = sys.stdout
            sys.stdout = sys.stderr
            try:
                state = predictor.init_state(
                    video_path=str(tmp_dir),
                    offload_video_to_cpu=True,
                    async_loading_frames=False,
                )
            finally:
                sys.stdout = _ck_save_out
            predictor.add_new_points_or_box(
                inference_state=state,
                frame_idx=anchor_rel,
                obj_id=1,
                points=np.array(all_pts, dtype=np.float32),
                labels=np.array(labs,    dtype=np.int32),
                clear_old_points=True,
            )

            # --- Forward pass: anchor → last frame ---
            # DANGER ZONE FRAGILE: SAM2 propagate_in_video() is stateful — the backward
            # pass must reuse the same state object so the tracker memory from the forward
            # pass carries over. Reinitialising state between passes loses anchor context.
            # breaks: if state is reset between passes, backward masks drift from forward.
            # WHAT IT DOES: Bridges inter-dot confidence dips inside the
            #   actor silhouette. With many positives placed on joints, SAM2
            #   sometimes drops confidence between adjacent dots along fabric
            #   creases, shadows, or motion blur — producing black holes in
            #   the matte. Closing with a kernel ~size of the longest plausible
            #   inter-dot gap (~100 px on a 4K subject) fills those holes
            #   without bloating the silhouette outward into hair / background.
            # AFFECTS: every non-empty propagated mask (forward and backward).
            CLOSE_KERNEL_PX = 101
            _close_kernel = np.ones((CLOSE_KERNEL_PX, CLOSE_KERNEL_PX), np.uint8)

            def _bridge_holes(mask):
                # Convert float [0..1] → uint8 [0..255] for cv2 morphology, close,
                # then return as float [0..1]. Inputs unchanged.
                binary = (mask * 255.0).astype(np.uint8)
                closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, _close_kernel)
                return (closed.astype(np.float32) / 255.0)

            status("SAM2: forward pass...")
            log(f"SAM2 video: forward pass (anchor={anchor_rel} → frame {dur-1})")
            forward_count = 0
            forward_empty_frames = []  # frame indices where SAM2 returned empty
            for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(state):
                mask = (mask_logits[0] > 0.0).squeeze().cpu().numpy().astype(np.float32)
                if mask.sum() < 100:
                    # Empty frame — defer "collapse vs actor-exit" decision to
                    # the post-pass step which has visibility across all frames.
                    masks[frame_idx] = mask  # store empty; resolved below
                    forward_empty_frames.append(frame_idx)
                else:
                    masks[frame_idx] = _bridge_holes(mask)
                forward_count += 1
                if frame_idx % 20 == 0:
                    log(f"SAM2 forward: frame {frame_idx}/{dur}")
                    status(f"SAM2 forward: {frame_idx}/{dur} frames")
            log(f"SAM2 forward pass done — {forward_count} masks "
                f"({len(forward_empty_frames)} empty, resolved post-pass)")

            # --- Backward pass: anchor → first frame ---
            # Only needed when the anchor is not the first frame. Frames already
            # written by the forward pass are not overwritten (forward wins on overlap).
            backward_empty_frames = []
            if anchor_rel > 0:
                status("SAM2: backward pass...")
                log(f"SAM2 video: backward pass (anchor={anchor_rel} → frame 0)")
                backward_count = 0
                for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(
                        state, reverse=True):
                    if frame_idx not in masks:
                        mask = (mask_logits[0] > 0.0).squeeze().cpu().numpy().astype(np.float32)
                        if mask.sum() < 100:
                            masks[frame_idx] = mask  # store empty; resolved post-pass
                            backward_empty_frames.append(frame_idx)
                        else:
                            masks[frame_idx] = _bridge_holes(mask)
                    backward_count += 1
                    if frame_idx % 20 == 0:
                        log(f"SAM2 backward: frame {frame_idx}")
                        status(f"SAM2 backward: {frame_idx} frames")
                log(f"SAM2 backward pass done — {backward_count} frames visited, "
                    f"{sum(1 for k in masks if k < anchor_rel)} new masks added "
                    f"({len(backward_empty_frames)} empty, resolved post-pass)")
            else:
                log("SAM2 video: anchor is frame 0 — backward pass skipped")

            # WHAT IT DOES: Resolves which empty frames are mid-range collapses
            #   (fall back to NN-only via ones-mask) vs tail empties at the start
            #   or end of the range (actor entering / leaving the frame — leave
            #   empty so the call site's `mt = mt * gate` correctly zeros out
            #   non-green set elements that would otherwise show through).
            # DEPENDS-ON: masks dict populated by both forward and backward passes
            # AFFECTS: masks dict — substitutes ones-mask for interior empty
            #   frames; leaves tail empties unchanged.
            # DANGER ZONE FRAGILE: do NOT substitute ones for ALL empty frames.
            #   That re-introduces the "non-green set comes back at end of range"
            #   regression seen on 2026-04-27 stunt clip after first quality-gate
            #   fix.
            sorted_frame_keys = sorted(masks.keys())
            collapsed_count = 0
            tail_empty_count = 0
            if sorted_frame_keys:
                first_substantial = next(
                    (f for f in sorted_frame_keys if masks[f].sum() >= 100), None)
                last_substantial = next(
                    (f for f in reversed(sorted_frame_keys) if masks[f].sum() >= 100), None)
                if first_substantial is not None and last_substantial is not None:
                    for f in sorted_frame_keys:
                        if masks[f].sum() >= 100:
                            continue  # mask is real, leave alone
                        if first_substantial <= f <= last_substantial:
                            # Interior empty = mid-range tracking collapse → ones-mask
                            masks[f] = np.ones_like(masks[f])
                            collapsed_count += 1
                        else:
                            # Tail empty = actor not in frame → leave empty
                            tail_empty_count += 1
            log(f"SAM2 propagation post-pass: {collapsed_count} interior "
                f"empties → NN-only fallback, {tail_empty_count} tail empties "
                f"left empty (actor entering/leaving frame).")

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


# WHAT IT DOES: Probes a video file's FourCC codec via cv2.VideoCapture and returns True for HEVC/H.265.
#   Result is cached per file path so repeated calls (per-frame loops) don't re-probe.
# WHY THIS EXISTS: cv2's HEVC decoder mishandles color-space metadata on Nikon Z and other cameras
#   that ship HEVC in BT.709 / Display P3 / BT.2020 — the FFmpeg backend defaults to a generic
#   conversion that drops the color matrix, producing a yellow→pink shift on warm wardrobe.
#   Confirmed 2026-04-29: same H.265 file is YELLOW in DaVinci's normal viewer but PINK when
#   read via cv2.VideoCapture in CorridorKey's "Original" view (raw plate, before any model).
#   Caller routes HEVC through Resolve's own decoder via _read_frame_via_resolve_render or
#   _export_braw_range_to_frames(skip_braw_exe=True) instead.
# DEPENDS-ON: cv2.VideoCapture.get(cv2.CAP_PROP_FOURCC). Module-level cache _hevc_codec_cache.
# AFFECTS: Opens and immediately closes a VideoCapture once per unique file path.
# DANGER ZONE: If FOURCC returns 0 (unknown — e.g. some MOV containers), we report False and
#   let cv2 try. Better to ship a known-good codec via cv2 than misroute everything to slow
#   Resolve render. The downside is HEVC files whose FOURCC tag is missing won't be caught;
#   in that case the user still sees the pink shift and we can extend detection later.
_hevc_codec_cache = {}
def _is_hevc_file(fp):
    if not fp: return False
    fp_key = str(fp).lower()
    if fp_key in _hevc_codec_cache:
        return _hevc_codec_cache[fp_key]
    import cv2
    is_hevc = False
    try:
        _probe = cv2.VideoCapture(fp)
        if _probe.isOpened():
            fourcc_int = int(_probe.get(cv2.CAP_PROP_FOURCC))
            # Decode 32-bit int to 4-char ASCII tag (little-endian byte order in cv2).
            tag = "".join(chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4))
            tag_norm = tag.strip().lower()
            # Known HEVC/H.265 FourCC variants.
            if tag_norm in ("hev1", "hvc1", "hevc", "h265"):
                is_hevc = True
            log(f"HEVC probe: {os.path.basename(fp)} fourcc={tag!r} hevc={is_hevc}")
        _probe.release()
    except Exception as _hp:
        log(f"HEVC probe failed for {os.path.basename(fp)}: {_hp} — assuming non-HEVC")
        is_hevc = False
    _hevc_codec_cache[fp_key] = is_hevc
    return is_hevc


# WHAT IT DOES: Last-resort frame reader for formats OpenCV cannot decode (BRAW, CinemaDNG, etc).
#   Three-path strategy, fastest first:
#   A) ExportCurrentFrameAsStill() — no render queue, TIFF output, Resolve 18.5+ (AiConsensus find)
#   B) Render queue with SetCurrentRenderFormatAndCodec("tif","RGB16LZW") — lossless, full chroma
#   C) Video fallback (.mov H.264) — last resort only; 4:2:0 chroma hurts edge quality for SAM2
#   Creates a 2-frame temp timeline to isolate source frame fn without touching the user's timeline.
# DEPENDS-ON: project, media_pool (Resolve globals), cv2, Path, time, tempfile
# AFFECTS: Creates then deletes a timestamped "CK_TempRender_N_T" timeline in the project.
#   Temporarily switches the active timeline to the temp one, then restores the original.
# DANGER ZONE HIGH: project.SetCurrentTimeline() switches the active timeline.
#   The finally block MUST restore the original timeline — do not add code that can raise before it.
def _read_frame_via_resolve_render(mpi, fn, try_direct=False):
    import time
    import traceback as _tb
    import cv2
    if mpi is None:
        log("Resolve render fallback: mpi is None — skipping")
        return None
    temp_dir = Path(tempfile.gettempdir()) / "corridorkey_renders"
    temp_dir.mkdir(exist_ok=True)
    # Timestamp in name prevents collision if a prior crash left a timeline behind
    temp_name = f"CK_TempRender_{fn}_{int(time.time())}"
    # Clean up stale files from prior runs
    try:
        for stale in temp_dir.glob("CK_TempRender_*"):
            stale.unlink()
    except Exception:
        pass

    # PATH 0: Export the current frame directly — NO timeline switch, NO audio pop.
    # WHY Path 0 runs FIRST: any CreateTimelineFromClips + SetCurrentTimeline resets the
    # Windows audio engine, causing an audible pop through ASIO/WDM devices (Focusrite, etc).
    # By attempting ExportCurrentFrameAsStill on the CURRENT timeline before touching anything,
    # we avoid that reset entirely when the caller's playhead is already at the target frame.
    # Only safe when called from process_current_frame (playhead == fn).
    # NOT used for background plate extraction (wrong composite would be captured).
    if try_direct:
        direct_path = str(temp_dir / f"{temp_name}_direct.tif")
        try:
            ok0 = project.ExportCurrentFrameAsStill(direct_path)
            # DANGER ZONE HIGH: Check file existence independently of ok0 — some Resolve
            # builds return None/False even on success. If the file is readable, trust it.
            # This prevents a spurious fallthrough to CreateTimelineFromClips (= audio pop).
            direct_exists = Path(direct_path).exists()
            if direct_exists:
                frame0 = cv2.imread(direct_path, cv2.IMREAD_COLOR)
                if frame0 is not None:
                    log(f"Resolve render fallback: OK (Path 0 — direct still, no timeline switch) ok0={ok0} shape={frame0.shape}")
                    try: Path(direct_path).unlink()
                    except: pass
                    return frame0
                else:
                    log(f"Resolve render fallback: Path 0 file written but unreadable by cv2 (ok0={ok0}) — falling back to temp timeline")
            else:
                log(f"Resolve render fallback: Path 0 no file (ok0={ok0}) — falling back to temp timeline")
        except Exception as _e0:
            log(f"Resolve render fallback: Path 0 exception ({_e0}) — falling back to temp timeline")

    original_tl = project.GetCurrentTimeline()
    temp_tl = None
    frame = None
    job_id = None
    log(f"Resolve render fallback: mpi={type(mpi).__name__} fn={fn}")
    try:
        # Create 2-frame temp timeline: source frame fn → fn+1
        # endFrame = fn+1 because some Resolve versions reject startFrame == endFrame
        clip_info = {"mediaPoolItem": mpi, "startFrame": fn, "endFrame": fn + 1}
        temp_tl = media_pool.CreateTimelineFromClips(temp_name, [clip_info])
        if not temp_tl:
            log("Resolve render fallback: ranged CreateTimelineFromClips failed, trying without range")
            temp_tl = media_pool.CreateTimelineFromClips(temp_name + "_f", [{"mediaPoolItem": mpi}])
        if not temp_tl:
            log("Resolve render fallback: CreateTimelineFromClips failed (both attempts)")
            return None
        project.SetCurrentTimeline(temp_tl)
        tl_start = temp_tl.GetStartFrame()
        tl_end = temp_tl.GetEndFrame()
        log(f"Resolve render fallback: temp timeline created start={tl_start} end={tl_end}")

        # --- PATH A: ExportCurrentFrameAsStill --- fastest, no render queue, no preset needed.
        # After SetCurrentTimeline the playhead is at the first frame = source frame fn.
        # AiConsensus: confirmed works in Resolve 18.5+. PNG is broken; .tif works.
        still_path = str(temp_dir / f"{temp_name}.tif")
        try:
            ok = project.ExportCurrentFrameAsStill(still_path)
            still_exists = Path(still_path).exists()
            log(f"Resolve render fallback: ExportCurrentFrameAsStill ok={ok} file_exists={still_exists}")
            if still_exists:
                frame = cv2.imread(still_path, cv2.IMREAD_COLOR)
                if frame is not None:
                    log(f"Resolve render fallback: OK (Path A — still) shape={frame.shape}")
                    try: Path(still_path).unlink()
                    except: pass
                    return frame   # finally block still runs and cleans up
                else:
                    log("Resolve render fallback: Path A — still file unreadable by cv2")
                    try: Path(still_path).unlink()
                    except: pass
        except Exception as ae:
            log(f"Resolve render fallback: Path A exception: {ae}")

        # DANGER ZONE HIGH: StartRendering() (Path B) resets the Windows audio engine,
        # killing Focusrite and any ASIO/WDM device — same bug that affected BRAW range export.
        # For BRAW and other camera-raw formats, bail here instead of triggering the audio pop.
        # Path A (ExportCurrentFrameAsStill) is the correct path for these formats on Resolve 18.5+.
        # If Path A failed, the user needs to fix the root cause (Resolve version, permissions),
        # not silently accept an audio device reset.
        try:
            _props = mpi.GetClipProperty() or {}
        except Exception:
            _props = {}
        _clip_fp = (_props.get("File Path") or _props.get("Clip Path") or "").lower()
        if _clip_fp.endswith(('.braw', '.cin', '.dng', '.ari')):
            status("ERROR: Cannot read camera-raw frame — ExportCurrentFrameAsStill failed. Audio protected (render queue skipped). Check Resolve 18.5+.")
            log("Resolve render fallback: camera-raw detected — skipping Path B to protect audio. Fix: verify Resolve 18.5+ and that ExportCurrentFrameAsStill works for this clip.")
            return None

        # --- PATH B: Render queue with TIFF --- lossless, full chroma (non-camera-raw only).
        # SetCurrentRenderFormatAndCodec overrides the active preset's format.
        # PNG is silently broken in Resolve 18.5+ via API (AiConsensus confirmed bug).
        # "tif" + "RGB16LZW" is confirmed working.
        try:
            project.SetCurrentRenderFormatAndCodec("tif", "RGB16LZW")
            log("Resolve render fallback: render format set to tif/RGB16LZW")
        except Exception as fe:
            log(f"Resolve render fallback: SetCurrentRenderFormatAndCodec warning: {fe}")

        project.SetRenderSettings({
            "SelectAllFrames": 1,
            "TargetDir": str(temp_dir),
            "CustomName": temp_name,
            "ExportVideo": 1,
            "ExportAudio": 0,
        })
        job_id = project.AddRenderJob()
        log(f"Resolve render fallback: Path B job added")
        project.StartRendering(job_id)
        deadline = time.time() + 30
        while project.IsRenderingInProgress() and time.time() < deadline:
            time.sleep(0.1)
        if project.IsRenderingInProgress():
            project.StopRendering()
            log("Resolve render fallback: timed out after 30s")
        else:
            all_out = sorted(temp_dir.iterdir()) if temp_dir.exists() else []
            log(f"Resolve render fallback: temp_dir contains {[f.name for f in all_out]}")

            # Try lossless image formats first (TIFF preferred for SAM2 edge quality)
            img_file = None
            for ext in ("*.tif", "*.tiff", "*.exr", "*.dpx", "*.png", "*.jpg"):
                hits = sorted(temp_dir.glob(f"{temp_name}{ext}"))
                if hits:
                    img_file = hits[0]
                    log(f"Resolve render fallback: Path B image — {img_file.name}")
                    break

            if img_file:
                frame = cv2.imread(str(img_file), cv2.IMREAD_COLOR)
                log(f"Resolve render fallback: OK (Path B — image) shape={frame.shape if frame is not None else 'None'}")
                for m in sorted(temp_dir.glob(f"{temp_name}*")):
                    try: m.unlink()
                    except: pass
            else:
                # --- PATH C: Video fallback --- H.264 .mov, 4:2:0 chroma, degraded edges.
                # Only runs if TIFF render also failed. Quality is lower than Path A/B.
                vid_file = None
                for ext in ("*.mov", "*.mp4", "*.mxf", "*.avi"):
                    hits = sorted(temp_dir.glob(f"{temp_name}{ext}"))
                    if hits:
                        vid_file = hits[0]
                        break
                if vid_file:
                    log(f"Resolve render fallback: Path C (degraded H.264) — {vid_file.name}")
                    cap = cv2.VideoCapture(str(vid_file))
                    ret, frame = cap.read()
                    cap.release()
                    if ret and frame is not None:
                        log(f"Resolve render fallback: OK (Path C — video/degraded) shape={frame.shape}")
                    else:
                        log(f"Resolve render fallback: cv2 could not read from {vid_file.name}")
                    for m in sorted(temp_dir.glob(f"{temp_name}*")):
                        try: m.unlink()
                        except: pass
                else:
                    log(f"Resolve render fallback: all paths failed — no output in {temp_dir}")
    except Exception as ex:
        log(f"Resolve render fallback exception: {ex}")
        log(_tb.format_exc())
    finally:
        if original_tl:
            try: project.SetCurrentTimeline(original_tl)
            except: pass
        if job_id:
            try: project.DeleteRenderJob(job_id)
            except: pass
        if temp_tl:
            try: media_pool.DeleteTimelines([temp_tl])
            except: pass
    return frame


# WHAT IT DOES: Reads exactly n bytes from a binary stream (subprocess stdout pipe).
#   Returns bytes on success, None if the stream ends before n bytes are available.
# ISOLATED: pure utility, no side effects
def _read_exact(stream, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


# WHAT IT DOES: Fast BRAW frame extraction via braw-decode.exe + BlackmagicRawAPI.dll.
#   Decodes clip frames src_start..src_end-1 directly from the BRAW file (no Resolve UI,
#   no render queue, no timeline changes). Streams BGRA pixels from the exe's stdout and
#   writes each frame to a TIFF file in a temp directory.
# DEPENDS-ON: braw-decode.exe (alongside this script or dev path), BlackmagicRawAPI.dll
#   (found automatically via Resolve / Desktop Video install — same DLL Resolve uses).
# AFFECTS: Creates CK_BrawDec_* subdirectory in corridorkey_renders temp folder.
# RETURNS: str path to temp dir, or None if exe missing / decode fails (caller falls back).
# DANGER ZONE FRAGILE: braw-decode.exe streams raw bytes with NO frame separator.
#   _read_exact() MUST consume exactly width*height*4 bytes per frame or the stream
#   goes out of sync and all subsequent frames are corrupt.
def _try_braw_decode_exe(fp, src_start, src_end):
    import subprocess, tempfile as _tf2, numpy as _np, cv2 as _cv2, time as _time2
    # __file__ is not defined in Resolve's embedded Python — use known absolute paths.
    exe_candidates = [
        Path("C:/ProgramData/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/braw-decode.exe"),
        Path("D:/New AI Projects/braw-decode-win/bin/braw-decode.exe"),
    ]
    exe = next((str(p) for p in exe_candidates if p.exists()), None)
    if exe is None:
        log("BRAW decode exe: braw-decode.exe not found — will use render queue"); return None

    # Build subprocess env: inherit parent env and ensure BRAW_SDK_PATH points to
    # the Resolve install directory where BlackmagicRawAPI.dll lives. Resolve's
    # Python subprocess does not inherit the standard system PATH entries that
    # braw-decode.exe uses for its DLL fallback lookup.
    import os as _os
    braw_env = _os.environ.copy()
    resolve_dll_dir = r"C:\Program Files\Blackmagic Design\DaVinci Resolve"
    if Path(resolve_dll_dir + "/BlackmagicRawAPI.dll").exists():
        # BRAW_SDK_PATH tells braw-decode.exe where to load the DLL from.
        # PATH must also include this dir so the Windows DLL loader can find
        # BlackmagicRawAPI.dll's own dependencies — without this the DLL
        # crashes at init time with 0xC0000005 (access violation during DLL init).
        if not braw_env.get("BRAW_SDK_PATH"):
            braw_env["BRAW_SDK_PATH"] = resolve_dll_dir
        if resolve_dll_dir.lower() not in braw_env.get("PATH", "").lower():
            braw_env["PATH"] = resolve_dll_dir + ";" + braw_env.get("PATH", "")

    # Query clip dimensions with -n (info-only, no decode).
    # Log env vars before launching so DLL/path errors are diagnosable without a debugger.
    log(f"BRAW decode exe: using {exe!r}  clip={fp!r}")
    # DIAGNOSTIC: show the PATH prefix and BRAW_SDK_PATH actually injected into the subprocess
    log(f"BRAW decode exe env: BRAW_SDK_PATH={braw_env.get('BRAW_SDK_PATH')!r}  PATH[0:120]={braw_env.get('PATH', '')[:120]!r}")
    try:
        # CREATE_NO_WINDOW: suppresses the console window without creating a detached session.
        # DETACHED_PROCESS was giving the subprocess NULL std handles which caused
        # BlackmagicRawAPI.dll to fault (0xC0000005) during DLL initialisation before main()
        # ran — stderr was empty because the crash happened in DLL_PROCESS_ATTACH.
        # CREATE_NO_WINDOW keeps std handles valid (redirected to PIPE/DEVNULL) so the DLL
        # initialises cleanly, while still avoiding any console window or audio-session reset.
        # stdin=DEVNULL: prevents the subprocess from inheriting or blocking on the parent's
        # stdin handle, which can hang when Resolve's stdin is a pipe.
        r = subprocess.run([exe, "-n", fp], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL,
                           env=braw_env,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0 or not r.stdout:
            log(f"BRAW decode exe: info failed (rc={r.returncode}) stderr={r.stderr!r}"); return None
        w, h = None, None
        for line in r.stdout.splitlines():
            if "Resolution:" in line:
                parts = line.split(":")[1].strip().split("x")
                w, h = int(parts[0].strip()), int(parts[1].strip())
                break
        if w is None:
            log(f"BRAW decode exe: cannot parse resolution — stdout={r.stdout!r} stderr={r.stderr!r}"); return None
        log(f"BRAW decode exe: {w}x{h}, decoding clip frames {src_start}–{src_end - 1}")
    except Exception as _e:
        log(f"BRAW decode exe: info query failed: {_e}"); return None

    dur = src_end - src_start
    bytes_per_frame = w * h * 4  # BGRA U8
    temp_dir = (Path(_tf2.gettempdir()) / "corridorkey_renders"
                / f"CK_BrawDec_{src_start}_{src_end}_{int(_time2.time())}")
    temp_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        # CREATE_NO_WINDOW + stdin=DEVNULL: same reasoning as the -n info call above.
        # stdout=PIPE carries the raw BGRA pixel stream; stderr=PIPE captures error text.
        proc = subprocess.Popen(
            [exe, "-c", "bgra", "-s", "1", "-i", str(src_start), "-o", str(src_end), fp],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=braw_env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for fidx in range(dur):
            raw = _read_exact(proc.stdout, bytes_per_frame)
            if raw is None:
                log(f"BRAW decode exe: stream ended at frame {fidx}/{dur}"); break
            arr = _np.frombuffer(raw, dtype=_np.uint8).reshape(h, w, 4)
            bgr = _cv2.cvtColor(arr, _cv2.COLOR_BGRA2BGR)
            _cv2.imwrite(str(temp_dir / f"frame_{fidx:06d}.tif"), bgr)
            written += 1
        proc.stdout.close()
        stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
        proc.wait(timeout=30)
        if proc.returncode not in (0, None):
            log(f"BRAW decode exe: exit {proc.returncode} — {stderr_out}")
        log(f"BRAW decode exe: {written}/{dur} frames written to {temp_dir.name}")
        if written == dur:
            return str(temp_dir)
        import shutil as _sh4
        _sh4.rmtree(temp_dir, ignore_errors=True); return None
    except Exception as _ex:
        import traceback as _tb3, shutil as _sh5
        log(f"BRAW decode exe: failed: {_ex}"); log(_tb3.format_exc())
        _sh5.rmtree(temp_dir, ignore_errors=True); return None


# WHAT IT DOES: Exports a BRAW (or other camera-raw) range to a TIFF image sequence.
#   Fast path: braw-decode.exe (direct SDK, no Resolve UI, no render queue overhead).
#   Fallback: For each frame in range, seek the CURRENT timeline playhead via
#     timeline.SetCurrentTimecode() then call project.ExportCurrentFrameAsStill().
#     NO temp timeline creation, NO SetCurrentTimeline, NO CreateTimelineFromClips.
#     SLOW (~1-3 s/frame) but audio-safe and no mouse-stealing UI blink per frame.
#   Both paths return str(temp_dir) containing frame_XXXXXX.tif files — same shape.
#   Caller must rmtree the returned directory when done.
# DEPENDS-ON: project, timeline (Resolve globals), tempfile, Path, time, shutil
# AFFECTS: Creates CK_Braw* temp subdirectory. Fallback seeks the active timeline
#   playhead via SetCurrentTimecode — no timeline switching at all.
# DANGER ZONE HIGH: SetCurrentTimecode() moves the playhead on the live timeline.
#   in_f is the absolute timeline frame where the BRAW range begins (same coordinate
#   system as clip.GetStart()). fps must match the project frame rate.
#   Resolve does NOT steal the mouse because SetCurrentTimeline is never called.
# WHAT IT DOES: Fires Resolve's built-in optimized media generator for a BRAW clip.
#   Non-blocking — Resolve transcodes in background using its own BRAW engine.
#   on_poll_timer detects completion and switches proxy playback mode on automatically.
#   No ffmpeg, no OpenCV file path needed, no audio driver touch.
# DEPENDS-ON: media_pool (Resolve API), mpi (MediaPoolItem), Resolve 18+.
# AFFECTS: _proxy_mpi global only. Resolve manages all storage.
def _trigger_resolve_proxy(mpi, media_pool_obj):
    global _proxy_mpi
    try:
        _has_fn = getattr(media_pool_obj, 'HasOptimizedMedia', None)
        _gen_fn = getattr(media_pool_obj, 'GenerateOptimizedMedia', None)
        if not callable(_has_fn) or not callable(_gen_fn):
            log("GenerateOptimizedMedia not in this Resolve version — proxy skipped")
            return
        already_done = _has_fn([mpi])
        if already_done:
            log("Optimized media exists — poll_timer will enable proxy mode")
        else:
            ok = _gen_fn([mpi])
            if not ok:
                log("GenerateOptimizedMedia returned False — proxy skipped"); return
            log("GenerateOptimizedMedia queued — poll_timer will detect completion")
            status("Resolve generating proxy in background...")
        _proxy_mpi = mpi
    except Exception as _pe:
        log(f"Proxy trigger error: {_pe}")


def _export_braw_range_to_frames(mpi, src_start, src_end, timeline, in_f, fps, skip_braw_exe=False):
    import time as _t2, tempfile as _tf, shutil as _sh
    import traceback as _tb_fb
    if mpi is None:
        log("BRAW range export: mpi is None"); return None

    # Fast path: direct SDK decode via braw-decode.exe — no Resolve render queue needed.
    # DANGER ZONE: if braw_fp is empty (GetMediaPoolItem returned None, or property key
    # mismatch), we must NOT return None here — the Resolve seek+still fallback below does
    # NOT need the file path at all.  Only skip the exe sub-path; always fall through.
    # skip_braw_exe=True is set by HEVC callers — braw-decode.exe only handles BRAW and
    # would burn a 30-second timeout per range probing a non-BRAW file.
    exe_result = None
    braw_fp = ""
    if not skip_braw_exe:
        try:
            _mpi_media = mpi.GetMediaPoolItem()
            props = _mpi_media.GetClipProperty() if _mpi_media else {}
            braw_fp = (props.get("File Path") or props.get("Clip Path") or "") if props else ""
            if not braw_fp:
                log("BRAW range export: cannot get file path from mpi — skipping exe path, using Resolve fallback")
                # Do NOT return None here — fall through to the Resolve seek+still fallback below.
            else:
                exe_result = _try_braw_decode_exe(braw_fp, src_start, src_end)
        except Exception as _ep:
            log(f"BRAW range export: exe path exception: {_ep}")
            # Fall through to the Resolve still-export fallback below.

    if exe_result is not None:
        return exe_result

    # -----------------------------------------------------------------------
    # FALLBACK: Resolve ExportCurrentFrameAsStill — seek current timeline, no blink.
    # NOTE: when proxy mode is active (_proxy_mode_saved is set), Resolve exports proxy
    # frames here instead of full BRAW — drops from ~6 sec/frame to <1 sec automatically.
    # WHY THIS AND NOT THE RENDER QUEUE: Resolve's StartRendering() resets the
    # Windows audio engine, killing Focusrite and any other ASIO/WDM device.
    # ExportCurrentFrameAsStill does NOT trigger that reset, and — critically —
    # seeking via SetCurrentTimecode() does NOT switch the active timeline, so
    # Resolve never steals the mouse or flickers the viewer per frame.
    # Returns: str(temp_dir) containing frame_000000.tif … frame_NNNNNN.tif — same
    # shape as _try_braw_decode_exe so downstream code needs no changes.
    # DANGER ZONE HIGH: SetCurrentTimecode() format must be HH:MM:SS:FF (colon-separated,
    # no drop-frame semicolons) and fps must match the project setting exactly.
    # If fps is wrong the seek lands on the wrong frame. fps is passed from the caller.
    # -----------------------------------------------------------------------
    dur = src_end - src_start
    log(f"BRAW decode exe failed — using Resolve seek+still fallback ({dur} frames). No mouse blink.")
    status(f"BRAW decode fallback: exporting {dur} frames via Resolve still export (slow, audio-safe)...")

    temp_dir = (Path(_tf.gettempdir()) / "corridorkey_renders"
                / f"CK_BrawFB_{src_start}_{src_end}_{int(_t2.time())}")
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Clamp fps to a sensible minimum to avoid divide-by-zero on broken project settings.
    safe_fps = max(float(fps), 1.0)

    import time as _t3
    _diag_path = str(Path(_tf.gettempdir()) / "ck_scrub_diag.txt")
    def _diag(msg):
        try:
            with open(_diag_path, "a", encoding="utf-8") as _df:
                _df.write(f"[{_t3.time():.2f}] {msg}\n")
        except Exception: pass

    _diag(f"START dur={dur} in_f={in_f} fps={fps}")
    written = 0
    for fidx in range(dur):
        # tl_frame is the absolute timeline frame to seek to — same coordinate as in_f.
        tl_frame = in_f + fidx
        out_path = str(temp_dir / f"frame_{fidx:06d}.tif")
        try:
            # Convert absolute timeline frame number to HH:MM:SS:FF timecode string.
            total_frames = int(tl_frame)
            ff = total_frames % int(safe_fps)
            total_secs = total_frames // int(safe_fps)
            ss_tc = total_secs % 60
            mm_tc = (total_secs // 60) % 60
            hh_tc = total_secs // 3600
            tc_str = f"{hh_tc:02d}:{mm_tc:02d}:{ss_tc:02d}:{ff:02d}"
            # Seek the current timeline playhead — NO timeline switch, NO mouse steal.
            # DANGER ZONE HIGH: some Resolve builds return None/False even on success.
            # Proceed regardless and trust file existence, not the return value.
            _diag(f"BEFORE SetCurrentTimecode tc={tc_str}")
            seek_ok = timeline.SetCurrentTimecode(tc_str)
            _diag(f"AFTER SetCurrentTimecode seek_ok={seek_ok}")
            _t3.sleep(0.4)  # let Resolve finish the seek before capturing the still
            ok = project.ExportCurrentFrameAsStill(out_path)
            _diag(f"AFTER ExportCurrentFrameAsStill ok={ok} exists={Path(out_path).exists()}")
            if Path(out_path).exists():
                written += 1
                log(f"BRAW fallback frame {written}/{dur} (tl={tl_frame} tc={tc_str}) ok={ok}")
            else:
                log(f"BRAW fallback frame {fidx + 1}/{dur}: ExportCurrentFrameAsStill no file "
                    f"(tc={tc_str} ok={ok})")
        except Exception as _fe:
            log(f"BRAW fallback frame {fidx + 1}/{dur} exception: {_fe}")
            log(_tb_fb.format_exc())

    if written == dur:
        log(f"BRAW fallback: all {dur} frames written to {temp_dir.name}")
        return str(temp_dir)
    elif written > 0:
        # Partial success — return the dir anyway. Downstream globs *.tif* so a short
        # sequence produces a short keyed range rather than a silent total failure.
        log(f"BRAW fallback: partial — {written}/{dur} frames written. Returning partial dir.")
        status(f"WARNING: BRAW fallback partial ({written}/{dur} frames) — keyed range will be short.")
        return str(temp_dir)
    else:
        log("BRAW fallback: zero frames written — giving up.")
        status("ERROR: BRAW fallback failed — no frames exported. Check Resolve 18.5+ and media online.")
        _sh.rmtree(temp_dir, ignore_errors=True)
        return None


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
                    # BRAW (and other camera-raw formats) cannot be decoded by OpenCV.
                    # HEVC: cv2 decodes but mishandles color metadata (yellow→pink shift) —
                    # route through Resolve's decoder for correct color.
                    if fp.lower().endswith(('.braw', '.cin', '.dng', '.ari')) or _is_hevc_file(fp):
                        bg_frame = _read_frame_via_resolve_render(mpi, fn)
                        if bg_frame is not None:
                            log(f"BG plate from V{track_idx} via Resolve render: {os.path.basename(fp)}")
                            return bg_frame
                        continue
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

    # fg.png — write the NN's CLEAN PREDICTED FG (matches AE viewer exactly).
    # Earlier this wrote orig_bgr (raw greenscreen) so the despill slider would
    # have visible work to do, BUT: at the soft-alpha falloff (MARGIN/SOFTEN),
    # the raw green pixels showed through the matte edge. Despill on raw green
    # leaves a magenta/purple residue under cyan-cast stage lighting → soft
    # edges came out PURPLE instead of black. AE writes the NN's clean fg and
    # never has this artifact. The despill slider in the viewer still functions
    # for fine-tuning; the NN already despills internally.
    # keyed_rgb is uint8 RGB (caller does (fg * 255).astype(uint8)) — convert to BGR.
    keyed_bgr = cv2.cvtColor(keyed_rgb, cv2.COLOR_RGB2BGR)
    _atomic_imwrite(SESSION_DIR / "fg.png", keyed_bgr)
    _atomic_imwrite(SESSION_DIR / "alpha.png", matte_vis)
    # original.png — RAW source frame (greenscreen, pre-key) for the viewer's
    # "Original" view tab. Written EVERY panel run so a new clip overwrites
    # any stale original.png left over from a previous session.
    # DANGER ZONE: if this write is removed, switching clips leaves the viewer
    #   showing the previous clip's source in the Original/Split tabs while
    #   Composite shows the new clip — looks like cache/memory bug to user.
    _atomic_imwrite(SESSION_DIR / "original.png", orig_bgr)

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
            "despeckle": bool(_s.get("despeckle_enabled", True)),
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
    # Clear stale scrub data so the viewer starts clean, not in scrub mode from last session.
    try:
        _stale_idx = SESSION_DIR / "scrub_index.json"
        if _stale_idx.exists():
            _stale_idx.unlink()
            log("Cleared stale scrub_index.json from previous session")
    except Exception:
        pass
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
        # Signal viewer to show Re-keying overlay before CUDA inference blocks the panel
        _write_live_params_slider({"rekeying": True})
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
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0, refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"], despeckle_size=settings["despeckle_size"], fg_source=settings.get("fg_source", "nn"))
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength} fg_source={ps.fg_source}")
        res = proc.process_frame(fr, ah, ps)
        fg, mt = res.get("fg"), res.get("alpha")
        if fg is not None:
            try: log(f"FG stats — dtype:{fg.dtype} min:{float(fg.min()):.4f} max:{float(fg.max()):.4f} mean R:{float(fg[..., 0].mean()):.4f} G:{float(fg[..., 1].mean()):.4f} B:{float(fg[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG stat error: {_e}")
        # SAM2 gate intentionally NOT applied here — single frame preview shows
        # clean chroma key only. SAM2 gate is applied only in PROCESS RANGE.
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
        sam2_gate_file = SESSION_DIR / "sam2_mask.png"
        log(f"SAM2 gate: alpha_method={settings.get('alpha_method')} sam2_mask.png={'EXISTS' if sam2_gate_file.exists() else 'MISSING'}")
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
        # HEVC decode-routing: cv2's HEVC decoder mishandles BT.709/BT.2020 metadata and
        # produces a yellow→pink color shift on Nikon Z (and similar) clips. Route H.265
        # files through Resolve's own decoder upfront — same path BRAW uses as a fallback.
        # Confirmed root cause 2026-04-29 — see _is_hevc_file() docstring.
        frame = None
        if _is_hevc_file(fp):
            log(f"HEVC detected — routing single-frame preview through Resolve render decoder")
            status("Reading HEVC via Resolve decoder...")
            frame = _read_frame_via_resolve_render(mpi, fn, try_direct=True)
            if frame is None:
                log(f"ERROR: HEVC Resolve render returned no frame for {os.path.basename(fp)}")
                status("ERROR: Cannot read HEVC frame via Resolve"); return
        else:
            cap = cv2.VideoCapture(fp)
            opened = cap.isOpened()
            if not opened:
                log(f"ERROR: OpenCV could not open the file. BRAW requires Blackmagic Desktop Video codecs.")
                status("ERROR: Cannot open source file"); return
            _src_fps = cap.get(cv2.CAP_PROP_FPS) or fps
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            log(f"SEEK: cf={cf} cs={cs} leftoff={clip.GetLeftOffset()} fn={fn} total={total_frames}")
            seek_ok = cap.set(cv2.CAP_PROP_POS_FRAMES, fn)
            if not seek_ok:
                # BRAW does not support CAP_PROP_POS_FRAMES random-access — try millisecond seek.
                ms = (fn / _src_fps) * 1000.0 + 0.01
                seek_ok2 = cap.set(cv2.CAP_PROP_POS_MSEC, ms)
                log(f"SEEK POS_MSEC fallback: ms={ms:.3f} ok={seek_ok2}")
            ret, frame = cap.read()
            if not ret:
                # Last resort: sequential read. Reads every frame from 0 to fn in order.
                # Slow (seconds for deep frames) but works on BRAW which can't random-seek.
                log(f"SEEK sequential: reading {fn+1} frames in order (random seek not supported)")
                cap.release()
                cap = cv2.VideoCapture(fp)
                frame = None
                for _i in range(fn + 1):
                    ret, frame = cap.read()
                    if not ret:
                        log(f"Sequential read failed at frame {_i}")
                        frame = None; break
            cap.release()
        if frame is None:
            # All OpenCV methods failed (BRAW has no OpenCV decoder). Use Resolve's render queue
            # to export the frame as a PNG and read that instead.
            log(f"All OpenCV seeks failed — trying Resolve render export for BRAW...")
            status("Reading via Resolve render (BRAW)...")
            frame = _read_frame_via_resolve_render(mpi, fn, try_direct=True)
            if frame is None:
                log(f"ERROR: All frame reading methods failed for {os.path.basename(fp)}")
                status("ERROR: Cannot read frame"); return
        cached_source["frame"], cached_source["file_path"], cached_source["frame_num"] = frame.copy(), fp, fn
        # Store timeline frame separately — "frame_num" above is source-video offset
        # (clip.GetLeftOffset() + cf - cs), not the timeline position. reprocess_with_cached
        # needs timeline position to know if the playhead moved.
        cached_source["timeline_frame"] = cf
        log(f"Size: {frame.shape[1]}x{frame.shape[0]}")
        # Trigger Resolve's built-in optimized media generation for BRAW clips.
        # Non-blocking — Resolve transcodes in background using its own BRAW engine.
        if fp.lower().endswith('.braw') and mpi is not None:
            _trigger_resolve_proxy(mpi, media_pool)
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        if cached_processor["proc"] is None:
            log("Loading AI (first time)...")
            cached_processor["proc"] = CorridorKeyProcessor(device="cuda")
            log("Model loaded!")
            # Pre-init the CPU proc for SCRUB RANGE in a background thread while
            # the model file is warm in OS cache. Runs async so LIVE PREVIEW doesn't
            # freeze. CORRIDORKEY_SKIP_COMPILE=1 is required — without it, torch.compile
            # fires max-autotune at img_size=2048 on CPU and hangs 6+ minutes.
            if cached_scrub_cpu_proc["proc"] is None:
                def _init_cpu_proc():
                    try:
                        os.environ["CORRIDORKEY_SKIP_COMPILE"] = "1"
                        cached_scrub_cpu_proc["proc"] = CorridorKeyProcessor(device="cpu")
                        log("CPU scrub proc ready — SCRUB RANGE will use CPU inference.")
                    except Exception as _cpu_e:
                        log(f"CPU scrub proc failed (scrub will use CUDA fallback): {_cpu_e}")
                import threading as _cpu_thr
                _cpu_thr.Thread(target=_init_cpu_proc, daemon=True).start()
                log("CPU scrub proc loading in background...")
        else:
            log("AI ready (cached)")
        proc = cached_processor["proc"]
        log("Alpha hint...")
        settings["_render_frame"] = cf
        ah = generate_alpha_hint(frame, settings)
        log("Processing...")
        fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0, refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"], despeckle_size=settings["despeckle_size"], fg_source=settings.get("fg_source", "nn"))
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength} fg_source={ps.fg_source}")
        if ps.despeckle_enabled:
            log(f"Despeckle: ON (size {ps.despeckle_size})")
        res = proc.process_frame(fr, ah, ps)
        cn = Path(fp).stem
        od = Path(items["OutputPath"].Text) / f"CK_{cn}"
        od.mkdir(parents=True, exist_ok=True)
        op = od / f"CK_{cn}_{cf:06d}.png"
        fg, mt = res.get("fg"), res.get("alpha")
        # Apply despill manually — NN ran with despill_strength=0 so result["fg"] is raw.
        # Viewer applies its own despill live; render path must match it here.
        from CorridorKeyModule.core import color_utils as _cu
        _despill_str = float(settings.get("despill_strength", 0.5))
        if _despill_str > 0 and fg is not None:
            fg = _cu.despill_opencv(fg, green_limit_mode="average", strength=_despill_str)
        if fg is not None:
            try: log(f"FG stats — dtype:{fg.dtype} min:{float(fg.min()):.4f} max:{float(fg.max()):.4f} mean R:{float(fg[..., 0].mean()):.4f} G:{float(fg[..., 1].mean()):.4f} B:{float(fg[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG stat error: {_e}")
        # SAM2 gate intentionally NOT applied here — single frame preview shows
        # clean chroma key only. SAM2 gate is applied only in PROCESS RANGE.
        choke_px = int(settings.get("choke", 0))
        if choke_px > 0 and mt is not None:
            k = choke_px * 2 + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
            mt = cv2.erode((_mt_c * 255).astype(np.uint8), kernel).astype(np.float32) / 255.0
            log(f"Choke: {choke_px}px")
        if fg is not None and mt is not None:
            # Despeckle for the saved file (parity with viewer's render_composite).
            # Use a local copy so the unchanged mt below reaches show_preview_window
            # untouched — the viewer applies despeckle live on its own slider.
            mt_for_save = _apply_despeckle_to_alpha(mt, settings)
            save_output(fg, mt_for_save, op, settings["export_format"])
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
                if items["DisableTrack1"].Checked:
                    timeline.SetTrackEnable("video", 1, False)
                    log("Track 1 disabled — uncheck 'Disable source clip' or press D in timeline to re-enable")
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


# WHAT IT DOES: Returns the current project's frame rate as a float.
# DEPENDS-ON: Resolve API — project must be open.
# AFFECTS: nothing — pure read.
def fps_of_timeline():
    try:
        resolve = app.GetResolve() if hasattr(app, 'GetResolve') else bmd.scriptapp("Resolve")
        project = resolve.GetProjectManager().GetCurrentProject()
        fps_str = project.GetSetting("timelineFrameRate")
        return float(fps_str)
    except Exception:
        return 24.0


# WHAT IT DOES: Launches preview_viewer_v2.py in --braw-scrubber mode pointing at
#   the given TIFF frames directory. Kills any existing scrubber process first.
#   Session dir is SESSION_DIR — same as regular viewer, so sam2_mask.png is shared.
# DEPENDS-ON: CK_PYTHON, CK_ROOT, SESSION_DIR, subprocess.
# AFFECTS: global _scrubber_proc. Live Preview (_viewer_proc) is NOT touched — both windows
#   stay open so the user can tweak sliders in Live Preview while scrubbing frames.
def launch_braw_scrubber(frames_dir):
    global _scrubber_proc
    # Kill previous scrubber if still alive — one scrubber at a time, live preview untouched.
    if _scrubber_proc is not None and _scrubber_proc.poll() is None:
        try:
            _scrubber_proc.terminate()
            _scrubber_proc.wait(timeout=2)
        except Exception:
            try: _scrubber_proc.kill()
            except Exception: pass
    _scrubber_proc = None
    viewer_script = str(CK_ROOT / "resolve_plugin" / "preview_viewer_v2.py")
    if not os.path.exists(viewer_script):
        viewer_script = r"D:\New AI Projects\CorridorKey-Plugin\resolve_plugin\preview_viewer_v2.py"
    _scrubber_proc = subprocess.Popen(
        [str(CK_PYTHON), viewer_script,
         "--braw-scrubber", str(frames_dir),
         "--parent-pid", str(os.getpid())],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    log(f"BRAW scrubber launched: {Path(frames_dir).name}")


# WHAT IT DOES: Queues SCRUB RANGE Phase 2 keying work for the main-thread timer.
#   Background threads in Fusion's Python deadlock on _ui_queue.put and CUDA calls.
#   Instead we load frames into _scrub_key_queue; on_poll_timer keys one frame per tick.
# DEPENDS-ON: tif_buffers (list of BytesIO/None), ctx dict with proc/settings/paths.
# AFFECTS: _scrub_key_queue, _scrub_key_ctx, _scrub_key_done, _scrub_key_total, _range_running.
def _start_scrub_keying(tif_buffers, ctx):
    global _range_running, _scrub_key_queue, _scrub_key_ctx, _scrub_key_done, _scrub_key_total
    try:
        from CorridorKeyModule.core import color_utils as _cu_sk
    except ImportError:
        _cu_sk = None
    _scrub_key_queue  = [(i, b) for i, b in enumerate(tif_buffers) if b is not None]
    _scrub_key_done   = 0
    _scrub_key_total  = len(_scrub_key_queue)
    _scrub_key_ctx    = {
        "proc":             ctx["proc"],
        "ps":               ctx["ps"],
        "hint_gen":         ctx["chroma_hint_gen"],
        "despill":          ctx["_despill_str"],
        "settings":         ctx["settings"],
        "scrub_dir":        ctx["scrub_dir"],
        "N":                ctx["N"],
        "cu":               _cu_sk,
        "sam2_video_masks": ctx.get("sam2_video_masks", {}),
    }
    _range_running = True
    log(f"SCRUB: {_scrub_key_total} frames queued for main-thread keying")
    status(f"Scrub: keying frame 1 / {_scrub_key_total}...")


# WHAT IT DOES: Keys ONE frame from _scrub_key_queue on the main thread (called by on_poll_timer).
#   Runs the neural net for one frame, writes fg.png+alpha.png, updates progress.
#   When queue empties: writes scrub_index.json and resets _range_running.
# DEPENDS-ON: _scrub_key_queue, _scrub_key_ctx, cached_processor["proc"], cv2, numpy.
# AFFECTS: SESSION_DIR/scrub/NNN/, SESSION_DIR/scrub_index.json, _scrub_key_done, _range_running.
# DANGER ZONE HIGH: runs on main thread — each call blocks the UI for ~2-5 sec during inference.
def _key_one_scrub_frame():
    global _scrub_key_queue, _scrub_key_done, _scrub_key_total, _range_running
    import cv2 as _cv2, numpy as _np, json as _json, tempfile as _tmp, os as _os
    if not _scrub_key_queue:
        return
    if processing_cancelled:
        _scrub_key_queue.clear()
        _range_running = False
        status("Scrub cancelled.")
        return
    frame_idx, buf = _scrub_key_queue.pop(0)
    ctx = _scrub_key_ctx
    status(f"Scrub: keying frame {frame_idx + 1} / {ctx['N']}...")
    try:
        _tmp_tif = _os.path.join(_tmp.gettempdir(), f"ck_scrub_{frame_idx}.tif")
        with open(_tmp_tif, "wb") as _f: _f.write(buf.getvalue())
        _arr = _cv2.imread(_tmp_tif, _cv2.IMREAD_UNCHANGED)
        try: _os.unlink(_tmp_tif)
        except Exception: pass
        if _arr is None:
            log(f"Scrub frame {frame_idx}: cv2 read None — skipping"); return
        if _arr.dtype == _np.uint16:
            _arr = (_arr >> 8).astype(_np.uint8)
        elif _arr.dtype != _np.uint8:
            _arr = (_arr.astype(_np.float64) / float(_np.iinfo(_arr.dtype).max) * 255).clip(0, 255).astype(_np.uint8)
        if len(_arr.shape) == 2:
            _arr = _cv2.cvtColor(_arr, _cv2.COLOR_GRAY2BGR)
        if _arr.shape[0] > 720:
            _sc = 720.0 / _arr.shape[0]
            _arr = _cv2.resize(_arr, (int(_arr.shape[1] * _sc), 720), interpolation=_cv2.INTER_AREA)
        frame_bgr = _np.ascontiguousarray(_arr); del _arr
    except Exception as _e:
        log(f"Scrub frame {frame_idx}: decode failed: {_e}"); return
    try:
        chroma = ctx["hint_gen"].generate_hint(frame_bgr).astype(_np.float32) / 255.0
        fr     = _cv2.cvtColor(frame_bgr, _cv2.COLOR_BGR2RGB).astype(_np.float32) / 255.0
        res    = ctx["proc"].process_frame(fr, chroma, ctx["ps"])
        fg, mt = res.get("fg"), res.get("alpha")
        if ctx["despill"] > 0 and fg is not None and ctx["cu"] is not None:
            fg = ctx["cu"].despill_opencv(fg, green_limit_mode="average", strength=ctx["despill"])
        if mt is not None and len(mt.shape) == 3:
            mt = mt[:, :, 0]
        # WHAT IT DOES: Apply per-frame SAM2 propagation mask so the person is keyed on
        #   every scrub frame, not just the anchor. Gate is 2D float32 same shape as mt.
        #   Resize needed because scrub frames are downscaled to 720p but masks are full-res.
        # DEPENDS-ON: ctx["sam2_video_masks"] built by on_scrub_range before keying starts.
        # AFFECTS: mt — multiplied by gate, zeroing pixels outside tracked region.
        _s2_masks = ctx.get("sam2_video_masks", {})
        if mt is not None and _s2_masks and frame_idx in _s2_masks:
            # Save raw alpha (before gate) and raw SAM2 mask (before dilate/soften)
            out_dir = ctx["scrub_dir"] / f"{frame_idx:03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            _mt_raw2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
            _al_raw16 = (_mt_raw2d * 65535).clip(0, 65535).astype(_np.uint16)
            _cv2.imwrite(str(out_dir / "alpha_raw.png"), _al_raw16)
            _s2_raw = _s2_masks[frame_idx]
            _s2_raw8 = (_s2_raw * 255).clip(0, 255).astype(_np.uint8)
            _cv2.imwrite(str(out_dir / "sam2_gate_raw.png"), _s2_raw8)
            _gate = _dilate_sam2_mask(_s2_masks[frame_idx],
                                      margin=ctx["settings"].get("sam2_margin", SAM2_MATTE_MARGIN))
            _gate = _soften_sam2_mask(_gate, soften=ctx["settings"].get("sam2_soften", 0))
            if _gate.shape != mt.shape:
                _gate = _cv2.resize(_gate, (mt.shape[1], mt.shape[0]),
                                    interpolation=_cv2.INTER_LINEAR)
            from sam2_combine import apply_sam2_gate
            mt = apply_sam2_gate(mt, _gate, invert=bool(settings.get("sam_invert", False)))
        if fg is not None and mt is not None:
            out_dir = ctx["scrub_dir"] / f"{frame_idx:03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            fg_16 = (fg * 65535).clip(0, 65535).astype(_np.uint16)
            _cv2.imwrite(str(out_dir / "fg.png"),    _cv2.cvtColor(fg_16, _cv2.COLOR_RGB2BGR))
            mt2d  = mt[:, :, 0] if len(mt.shape) == 3 else mt
            al_16 = (mt2d * 65535).clip(0, 65535).astype(_np.uint16)
            _cv2.imwrite(str(out_dir / "alpha.png"), al_16)
            _scrub_key_done += 1
    except Exception as _ke:
        log(f"Scrub frame {frame_idx}: keying failed: {_ke}")
    # When the queue is empty write the index and signal done.
    if not _scrub_key_queue:
        import json as _json2
        try:
            with open(str(SESSION_DIR / "scrub_index.json"), "w") as _jf:
                _json2.dump({"count": _scrub_key_done, "base_dir": "scrub/"}, _jf)
            log(f"SCRUB: wrote scrub_index.json count={_scrub_key_done}")
        except Exception as _we:
            log(f"Scrub index write error: {_we}")
        if _scrub_key_done > 0:
            status(f"Scrub ready — {_scrub_key_done}/{ctx['N']} frames keyed. Drag slider in Live Preview.")
        else:
            status("Scrub: no frames keyed — check clip and green screen settings.")
        _range_running = False


# WHAT IT DOES: Samples N evenly-spaced frames from the IN→OUT range.
#   Phase 1 (main thread): exports each frame as a single TIFF, pre-reads into BytesIO.
#     Takes ~5-10 sec — panel freezes briefly but recovers.
#   Phase 2 (background thread): keys each cached TIFF through the neural net, writes
#     fg.png + alpha.png to SESSION_DIR/scrub/NNN/, writes scrub_index.json when done.
#     Panel stays FULLY RESPONSIVE during keying — close button works at any time.
#   The persistent Live Preview viewer detects scrub_index.json and adds a purple scrub
#   slider — dragging it swaps the cached fg/alpha instantly (no re-keying needed).
# DEPENDS-ON: frame_range, _export_braw_range_to_frames, cached_processor, SESSION_DIR,
#   AlphaHintGenerator, ProcessingSettings, _load_sam2_output_gate, _ui_queue, _viewer_proc.
# AFFECTS: SESSION_DIR/scrub/ directory, SESSION_DIR/scrub_index.json, _range_running.
# DANGER ZONE HIGH: Phase 1 BRAW exports run on the main thread — brief freeze per frame.
def on_scrub_range(ev):
    global _scrubber_frames_dir, _range_running, processing_cancelled
    processing_cancelled = False
    log("SCRUB RANGE: button pressed")
    import cv2, numpy as np, json as _json, threading as _thr
    try:
        from PIL import Image as _PILImage
    except ImportError:
        _PILImage = None
    # --- Guards ---
    if cached_processor["proc"] is None:
        status("Click LIVE PREVIEW first to load the AI model"); return
    log("SCRUB: model loaded OK")
    if _range_running:
        status("Process Range is running — wait or cancel"); return
    log("SCRUB: not already running")
    # Flush any stale messages from a previous run so ghost "Scrub ready" messages
    # don't appear and confuse the user before the new run completes.
    while not _ui_queue.empty():
        try: _ui_queue.get_nowait()
        except Exception: break
    if _viewer_proc is None or _viewer_proc.poll() is not None:
        log(f"SCRUB: viewer guard triggered — _viewer_proc={_viewer_proc} poll={_viewer_proc.poll() if _viewer_proc else 'N/A'}")
        status("Open LIVE PREVIEW first — scrub results display there"); return
    log("SCRUB: viewer alive")
    # Delete stale scrub_index.json so viewer exits any previous scrub mode cleanly.
    try:
        _stale = SESSION_DIR / "scrub_index.json"
        if _stale.exists(): _stale.unlink()
    except Exception: pass
    log("SCRUB: cleared stale index")
    # --- Validate in/out range ---
    in_f  = frame_range.get("in_frame")
    out_f = frame_range.get("out_frame")
    if in_f is not None and out_f is not None and out_f <= in_f:
        status("OUT must be after IN"); return
    # --- Get clip info ---
    try:
        resolve = app.GetResolve() if hasattr(app, 'GetResolve') else bmd.scriptapp("Resolve")
        project = resolve.GetProjectManager().GetCurrentProject()
        timeline = project.GetCurrentTimeline()
        mpi = timeline.GetCurrentVideoItem()
        if mpi is None:
            status("No clip selected — click on a clip in the timeline first"); return
    except Exception as e:
        status(f"Cannot read timeline: {e}"); return
    # --- Check clip type ---
    try:
        mi  = mpi.GetMediaPoolItem()
        props = mi.GetClipProperty() if mi else {}
        fp  = (props.get("File Path") or props.get("Clip Path") or "").lower()
    except Exception:
        fp = ""
    # All formats supported — BRAW/camera-raw uses SDK decoder, everything else uses
    # Resolve's native ExportCurrentFrameAsStill (handles any format Resolve can open).
    _is_camera_raw = fp.endswith(('.braw', '.cin', '.dng', '.ari'))
    # HEVC routing: cv2's decoder produces a yellow→pink color shift on Nikon Z and other
    # cameras that ship HEVC with BT.709/BT.2020 metadata. Route HEVC clips through the
    # Resolve seek+still path (skip_braw_exe=True so we don't waste a 30s timeout per frame
    # probing a non-BRAW file with braw-decode.exe).
    _is_hevc = _is_hevc_file(fp) if fp else False
    # --- Resolve full-clip defaults if no IN/OUT set ---
    cs = mpi.GetStart()
    ce = mpi.GetEnd()
    if in_f is None: in_f = cs
    if out_f is None: out_f = ce
    in_f, out_f = max(in_f, cs), min(out_f, ce)
    if out_f <= in_f:
        status("No frames in range — check IN/OUT points"); return
    ss  = mpi.GetLeftOffset()
    fps = fps_of_timeline()
    log(f"SCRUB: clip {fp} cs={cs} ce={ce} ss={ss}")
    # --- Guard: warn if Live Preview is not open (BrawScrubberWindow launches separately,
    #     but we use the viewer process check as a proxy for whether the session is live) ---
    # NOTE: scrub window opens automatically at the end — this is just an early warning.
    if _viewer_proc is None or _viewer_proc.poll() is not None:
        log(f"SCRUB: second viewer guard triggered — _viewer_proc={_viewer_proc} poll={_viewer_proc.poll() if _viewer_proc else 'N/A'}")
        status("TIP: Open Live Preview first so the Scrub window has a session to anchor to.")
        # Non-fatal — continue anyway; the BrawScrubberWindow opens standalone.
    # --- Load processor + settings ---
    settings = _merge_live_params(get_settings())
    from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    proc = cached_processor["proc"]
    ps = ProcessingSettings(
        screen_type=settings["screen_type"],
        despill_strength=0.0,
        refiner_strength=settings["refiner_strength"],
        despeckle_enabled=settings["despeckle_enabled"],
        despeckle_size=settings["despeckle_size"],
        fg_source=settings.get("fg_source", "nn"),
    )
    from CorridorKeyModule.core import color_utils as _cu
    _despill_str = float(settings.get("despill_strength", 0.5))
    from core.alpha_hint_generator import AlphaHintGenerator
    chroma_hint_gen = AlphaHintGenerator(screen_type=settings["screen_type"])
    # --- Build frame list — all frames by default, capped if user set Max Frames ---
    dur = out_f - in_f
    try:
        max_frames = int(items["ScrubMaxFrames"].Value)
    except Exception:
        max_frames = 0
    if max_frames <= 0 or max_frames >= dur:
        N = dur
        sampled_tl_frames = list(range(in_f, out_f))
    else:
        N = max_frames
        sampled_tl_frames = [int(in_f + round(i * (dur - 1) / (N - 1))) for i in range(N)]
    log(f"SCRUB: sampling {N} frames: {sampled_tl_frames}")
    # --- PHASE 1: blocking export loop (Resolve API must run on main thread) ---
    # UI freezes during export (~2 sec per frame). Panel unfreezes after all frames done.
    _scrubber_frames_dir = None
    scrub_dir = SESSION_DIR / "scrub"
    if scrub_dir.exists():
        try: shutil.rmtree(str(scrub_dir))
        except Exception: pass
    scrub_dir.mkdir(parents=True, exist_ok=True)
    _range_running = True
    tif_buffers = []
    for _si, _stl in enumerate(sampled_tl_frames):
        _ssrc = ss + (_stl - cs)
        status(f"Scrub: exporting frame {_si+1}/{N}...")
        log(f"SCRUB export: frame {_si+1}/{N} tl={_stl} src={_ssrc} camera_raw={_is_camera_raw} hevc={_is_hevc}")
        _sbuf = None
        try:
            if _is_camera_raw or _is_hevc:
                # BRAW / CinemaDNG / ARRI — existing SDK decoder path.
                # HEVC — same Resolve seek+still path but skip braw-decode.exe (30s timeout otherwise).
                _sfdir = _export_braw_range_to_frames(
                    mpi, _ssrc, _ssrc + 1, timeline, _stl, fps,
                    skip_braw_exe=_is_hevc,
                )
                if _sfdir is not None:
                    _stifs = sorted(Path(_sfdir).glob("*.tif*"))
                    if _stifs:
                        with open(str(_stifs[0]), "rb") as _sf:
                            _sbuf = io.BytesIO(_sf.read())
                    shutil.rmtree(_sfdir, ignore_errors=True)
            else:
                # All other formats (H.264, H.265, ProRes, MP4, MOV, PNG seq, etc.)
                # cv2.VideoCapture handles H.264/H.265 on Windows reliably.
                # Note: CreateTimelineFromClips fails for non-raw clips via Resolve scripting API,
                # so we decode directly from the source file instead.
                import cv2 as _cv2
                _frame_bgr = None
                if fp:
                    _cap = _cv2.VideoCapture(fp)
                    if _cap.isOpened():
                        _cap.set(_cv2.CAP_PROP_POS_FRAMES, _ssrc)
                        _ret, _frame_bgr = _cap.read()
                        _cap.release()
                        if not _ret:
                            _frame_bgr = None
                            log(f"SCRUB export: frame {_si+1} cv2 read failed at frame {_ssrc}")
                        else:
                            log(f"SCRUB export: frame {_si+1} decoded via cv2 shape={_frame_bgr.shape}")
                    else:
                        log(f"SCRUB export: frame {_si+1} cv2 could not open: {fp}")
                if _frame_bgr is not None:
                    _, _tif_enc = _cv2.imencode(".tif", _frame_bgr)
                    _sbuf = io.BytesIO(_tif_enc.tobytes())
                else:
                    log(f"SCRUB export: frame {_si+1} all decoders failed")
        except Exception as _sex:
            log(f"SCRUB export error frame {_si+1}: {_sex}")
        tif_buffers.append(_sbuf)
        log(f"SCRUB export: frame {_si+1} buf={'OK' if _sbuf else 'NONE'}")
    _range_running = False
    good = sum(1 for b in tif_buffers if b is not None)
    log(f"SCRUB: export done — {good}/{N} frames captured. Starting keying thread...")
    if good == 0:
        status("Scrub export failed — no frames captured. Check log.")
        return

    # WHAT IT DOES: Run SAM2 video propagation across the N sampled scrub frames so the
    #   tracking mask follows the person on each frame instead of using a static anchor gate.
    #   Writes BytesIO frames to a temp dir, calls run_sam2_video_propagation, maps result
    #   indices back to original buffer positions (handles None/failed exports cleanly).
    # DEPENDS-ON: sam_points (global), run_sam2_video_propagation, tif_buffers, sampled_tl_frames
    # AFFECTS: scrub_sam2_masks — consumed by _start_scrub_keying → _key_one_scrub_frame
    # DANGER ZONE HIGH: runs synchronously on main thread — adds ~20-60s for N frames on GPU.
    scrub_sam2_masks = {}
    if settings.get("alpha_method") == 1:
        _pos = sam_points.get("positive", [])
        _neg = sam_points.get("negative", [])
        if _pos or _neg:
            import tempfile as _stmp2, shutil as _ssh2
            _sam_tmp = Path(_stmp2.mkdtemp(prefix="ck_sam2_scrub_"))
            try:
                _good_orig_idxs = []
                for _si2, _sbuf2 in enumerate(tif_buffers):
                    if _sbuf2 is None: continue
                    _sbuf2.seek(0)
                    with open(str(_sam_tmp / f"{len(_good_orig_idxs):06d}.tif"), "wb") as _sf2:
                        _sf2.write(_sbuf2.read())
                    _sbuf2.seek(0)
                    _good_orig_idxs.append(_si2)
                _n_good = len(_good_orig_idxs)
                if _n_good > 0:
                    _anch_abs = sam_points.get("frame")
                    if _anch_abs is not None and sampled_tl_frames:
                        _good_tl = [sampled_tl_frames[i] for i in _good_orig_idxs]
                        _dists   = [abs(f - _anch_abs) for f in _good_tl]
                        _anchor_rel = _dists.index(min(_dists))
                    else:
                        _anchor_rel = 0
                    status("SAM2: propagating mask across scrub frames...")
                    log(f"SAM2 scrub: {_n_good} frames, anchor_rel={_anchor_rel}")
                    _raw_masks = run_sam2_video_propagation(
                        str(_sam_tmp), 0, 0, 0, _n_good, _pos, _neg, _anchor_rel,
                    )
                    scrub_sam2_masks = {_good_orig_idxs[k]: v
                                        for k, v in _raw_masks.items()
                                        if k < len(_good_orig_idxs)}
                    log(f"SAM2 scrub: {len(scrub_sam2_masks)} per-frame masks ready")
                else:
                    log("SAM2 scrub: no good frames exported — skipping propagation")
            except Exception as _spe:
                log(f"SAM2 scrub propagation failed: {_spe}")
                import traceback as _stb2; log(_stb2.format_exc())
                scrub_sam2_masks = {}
            finally:
                _ssh2.rmtree(str(_sam_tmp), ignore_errors=True)

    # Wait up to 30 sec for the background CPU proc init to finish (it started when
    # LIVE PREVIEW loaded the CUDA model). Export took 60+ sec so it's usually ready.
    if cached_scrub_cpu_proc["proc"] is None:
        status("Waiting for CPU scrub proc to finish loading...")
        import time as _tw
        for _ in range(60):
            if cached_scrub_cpu_proc["proc"] is not None: break
            _tw.sleep(0.5)
        if cached_scrub_cpu_proc["proc"] is None:
            log("SCRUB: CPU proc still not ready after 30s — using CUDA fallback")
        else:
            log("SCRUB: CPU proc ready")
    status(f"Scrub: keying {good}/{N} frames...")
    _ctx = {"N": N, "mpi": mpi, "cs": cs, "ss": ss, "timeline": timeline, "fps": fps,
            "proc": proc, "ps": ps, "chroma_hint_gen": chroma_hint_gen,
            "_despill_str": _despill_str, "settings": settings, "scrub_dir": scrub_dir,
            "sam2_video_masks": scrub_sam2_masks}
    _start_scrub_keying(tif_buffers, _ctx)
    # DANGER ZONE HIGH: Key all frames synchronously here on the main thread.
    # After 90+ seconds of blocking export, Fusion pauses timer dispatch so on_poll_timer
    # never fires — timer-based keying never ran. Synchronous keying works the same way
    # Live Preview runs inference: main-thread CUDA, UI frozen but recovers when done.
    log("SCRUB: keying frames on main thread (UI will freeze ~2-5 sec per frame)...")
    _keyed_count = 0
    while _scrub_key_queue:
        status(f"Scrub: keying frame {_keyed_count + 1} / {_scrub_key_total}...")
        _key_one_scrub_frame()
        _keyed_count += 1
    try:
        items["PollTimer"].Interval = 500
    except Exception:
        pass
    log("SCRUB: synchronous keying done.")


def on_process_range(ev):
    # WHAT IT DOES: Starts range processing in a background thread so the UI stays
    #   live and the Cancel button works between frames.
    # DEPENDS-ON: timeline, media_pool, cached_processor, frame_range globals.
    # AFFECTS: Disk (PNGs), MediaPool (sequence), Timeline (places on V above source).
    global processing_cancelled, timeline, media_pool, _range_running
    if _range_running:
        status("Already running — hit CANCEL first"); return
    # Kill viewer immediately — must happen before any early return so it always closes.
    on_kill_viewer(None)
    processing_cancelled = False
    # Refresh in case timeline was opened after script loaded
    if project:
        timeline = project.GetCurrentTimeline()
        media_pool = project.GetMediaPool()
    import cv2, numpy as np, time, threading
    # tifffile handles 16-bit LZW TIFFs correctly — PIL has silent truncation bugs on 16-bit RGB.
    # tifffile 2026.3.3 is confirmed in the CorridorKey venv.
    try:
        import tifffile as _tifffile
        _has_tifffile = True
    except ImportError:
        _has_tifffile = False
    # PIL fallback if tifffile missing (less reliable for 16-bit).
    try:
        from PIL import Image as _PILImage
        _has_pil = True
    except ImportError:
        _has_pil = False
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
    # BRAW range export — must happen on main thread before background thread starts.
    # For BRAW/camera-raw, OpenCV cannot decode the file. Export the full range as a
    # single H.264 .mov first (one render job for all frames = fast), then the background
    # thread reads TIFF files from that directory instead of the source BRAW.
    # HEVC: cv2 mishandles BT.709/BT.2020 metadata (yellow→pink shift). Same TIFF pre-export
    # path — skip_braw_exe=True so we don't waste a 30s timeout per range probing a non-BRAW
    # file with braw-decode.exe.
    braw_frames_dir = None
    _is_hevc_clip = _is_hevc_file(fp)
    if fp.lower().endswith(('.braw', '.cin', '.dng', '.ari')) or _is_hevc_clip:
        src_start = ss + (in_f - cs)
        src_end   = ss + (out_f - cs) + 1  # +1: Resolve timeline end is exclusive
        _kind = "HEVC" if _is_hevc_clip else "BRAW"
        log(f"{_kind} detected — exporting source frames {src_start}-{src_end} to TIFF sequence...")
        status(f"Exporting {_kind} range ({dur} frames) — please wait...")
        braw_frames_dir = _export_braw_range_to_frames(
            mpi, src_start, src_end, timeline, in_f, fps,
            skip_braw_exe=_is_hevc_clip,
        )
        if braw_frames_dir is None:
            status(f"ERROR: {_kind} range export failed — see log"); return
        n_tifs = len(sorted(Path(braw_frames_dir).glob("*.tif*")))
        log(f"{_kind} range export done: {n_tifs} TIFF frames in {Path(braw_frames_dir).name}")
    # Kill viewer on main thread before background thread opens VideoCapture.
    # Reuses on_kill_viewer to avoid global scoping issues with nested _run() closure.
    on_kill_viewer(None)
    from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    if cached_processor["proc"] is None:
        log("Loading AI (first time)...")
        status("Loading AI...")
        cached_processor["proc"] = CorridorKeyProcessor(device="cuda")
        log("Model loaded!")
        # Pre-init CPU scrub proc in background while model file is warm in OS cache.
        # CORRIDORKEY_SKIP_COMPILE=1 is required — torch.compile on CPU with max-autotune
        # runs a 2048x2048 dummy forward that hangs 6+ minutes without this flag.
        if cached_scrub_cpu_proc["proc"] is None:
            def _init_cpu_proc_b():
                try:
                    os.environ["CORRIDORKEY_SKIP_COMPILE"] = "1"
                    cached_scrub_cpu_proc["proc"] = CorridorKeyProcessor(device="cpu")
                    log("CPU scrub proc ready — SCRUB RANGE will use CPU inference.")
                except Exception as _cpu_e2:
                    log(f"CPU scrub proc failed (scrub will use CUDA fallback): {_cpu_e2}")
            import threading as _cpu_thr2
            _cpu_thr2.Thread(target=_init_cpu_proc_b, daemon=True).start()
            log("CPU scrub proc loading in background...")
    else:
        log("AI ready (cached)")
    proc = cached_processor["proc"]
    ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0,
                            refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"],
                            despeckle_size=settings["despeckle_size"],
                            fg_source=settings.get("fg_source", "nn"))
    log(f"Settings: despill={ps.despill_strength} refiner={ps.refiner_strength} despeckle={ps.despeckle_enabled} fg_source={ps.fg_source}")
    if ps.despeckle_enabled:
        log(f"Despeckle: ON (size {ps.despeckle_size})")
    from CorridorKeyModule.core import color_utils as _cu
    _despill_str = float(settings.get("despill_strength", 0.5))
    from core.alpha_hint_generator import AlphaHintGenerator
    chroma_hint_gen = AlphaHintGenerator(screen_type=settings["screen_type"])

    # Pre-thread diagnostic — runs on main thread, writes to debug log directly (no Defender risk).
    # Also pre-computes the TIFF file list and pre-reads bytes into BytesIO so the thread never
    # needs to open() any file (Defender scans every file open from a background thread).
    _braw_tif_files_precomputed = []
    _braw_tif_buffers = []
    _braw_frames_decoded = []
    if braw_frames_dir:
        _pre_tifs = sorted(Path(braw_frames_dir).glob("*.tif*"))
        _braw_tif_files_precomputed = _pre_tifs
        log(f"Pre-thread TIFF check: {len(_pre_tifs)} files in {Path(braw_frames_dir).name}")
        if _pre_tifs:
            log(f"  First TIFF: {_pre_tifs[0].name}, size={_pre_tifs[0].stat().st_size}")
            # DANGER ZONE: FRAGILE — Defender scans ANY file opened from a background thread, even reads.
            # Pre-read all TIFF bytes on the main thread into BytesIO buffers. Thread uses BytesIO — no
            # open() call from thread = no Defender scan per-file. Same pattern as pre-opened probe file.
            for _ptf in _pre_tifs:
                try:
                    with open(str(_ptf), "rb") as _ptf_fp:
                        _braw_tif_buffers.append(io.BytesIO(_ptf_fp.read()))
                except Exception as _re:
                    log(f"  Pre-read TIFF failed: {_ptf.name}: {_re}")
                    _braw_tif_buffers = []
                    break
            if _braw_tif_buffers:
                # MEMORY FIX: Do NOT pre-decode all frames to numpy — that would consume ~11GB
                # for a 145-frame 4K sequence (53MB per frame as uint8 BGR). Instead, BytesIO
                # buffers stay in memory (~7.7GB compressed) and each frame is decoded on-demand
                # in the processing loop, one at a time. Peak numpy RAM = 1 frame (~53MB).
                log(f"  Pre-read {len(_braw_tif_buffers)} TIFFs into BytesIO — will decode on-demand (1 frame at a time)")
            else:
                log("  WARNING: pre-read failed — thread will open TIFFs directly (may be slow)")
        else:
            log("  WARNING: no TIFF files found — thread will skip all frames")

    # Warmup chroma_hint_gen on main thread — triggers any lazy DLL/model loads before
    # the thread starts. Defender blocks file opens from untrusted threads; if generate_hint
    # loads a model file on first call inside the thread, it would hang indefinitely.
    # Decode only the first frame for warmup — discard immediately after. No persistent array.
    if _braw_tif_buffers:
        try:
            _wb = _braw_tif_buffers[0]
            _wb.seek(0)
            _wf_pil = _PILImage.open(_wb).convert("RGB")
            _wf_arr = np.array(_wf_pil)
            if _wf_arr.dtype == np.uint16:
                _wf_arr = (_wf_arr >> 8).astype(np.uint8)
            elif _wf_arr.dtype != np.uint8:
                _wf_arr = (_wf_arr.astype(np.float64) / float(np.iinfo(_wf_arr.dtype).max) * 255.0).clip(0, 255).astype(np.uint8)
            _wf = np.ascontiguousarray(cv2.cvtColor(_wf_arr, cv2.COLOR_RGB2BGR))
            chroma_hint_gen.generate_hint(_wf)
            cv2.cvtColor(_wf, cv2.COLOR_BGR2RGB)
            del _wf, _wf_arr, _wf_pil  # Free warmup frame immediately — not needed after this
            log("  chroma_hint_gen warmup done on main thread")
        except Exception as _we:
            log(f"  chroma_hint_gen warmup (non-fatal): {_we}")

    # SYNCHRONOUS BRAW PATH: process frames on the main thread, decoding one at a time.
    # The BRAW render queue blocks Fusion's event loop long enough to kill the PollTimer,
    # so the background-thread + queue architecture cannot communicate back. Frames are
    # decoded on-demand from BytesIO buffers — peak numpy RAM is 1 frame (~53MB), not
    # all frames at once (~11GB for a 145-frame 4K sequence).
    if _braw_tif_buffers:
        try: items["PollTimer"].Interval = 500  # Wake up fast — processing starting
        except Exception: pass
        _range_running = True
        try:
            ofs = []
            pr = 0
            st = time.time()
            try:
                items["Progress"].StyleSheet = "background: #111; border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #888; font-size: 10px;"
                items["Progress"].Text = f"  0 / {dur} frames"
                items["Progress"].Visible = True
            except Exception: pass
            # WHAT IT DOES: Run SAM2 video propagation for BRAW — one tracking mask per frame.
            #   Falls back to static gate if propagation returns nothing (e.g. no dots placed,
            #   or Resolve restart wiped sam_points but sam2_mask.png still exists on disk).
            #   BRAW never reaches _run() so propagation must happen here on the sync path.
            # DEPENDS-ON: run_sam2_video_propagation(), braw_frames_dir, sam_points
            #   _load_sam2_output_gate(), _braw_tif_buffers[0] for static gate shape detection
            # AFFECTS: mt (alpha) for every frame in this BRAW range
            # DANGER ZONE HIGH: runs on main thread — blocks Fusion event loop during propagation.
            #   Acceptable because BRAW sync path already blocks during frame export and NN processing.
            _braw_sam2_video_masks = {}
            _braw_sam2_gate = None
            if settings.get("alpha_method") == 1:
                pos = sam_points.get("positive", [])
                neg = sam_points.get("negative", [])
                if (pos or neg) and braw_frames_dir:
                    try:
                        _anchor_abs = sam_points.get("frame")
                        _anchor_in_tif = (_anchor_abs - in_f) if _anchor_abs is not None else None
                        status("SAM2: running video propagation for BRAW range...")
                        _braw_sam2_video_masks = run_sam2_video_propagation(
                            braw_frames_dir, 0, 0, 0, dur,
                            pos, neg, _anchor_in_tif,
                        )
                        if _braw_sam2_video_masks:
                            log(f"SAM2 video propagation: {len(_braw_sam2_video_masks)} per-frame masks ready")
                        else:
                            log("SAM2 video propagation returned no masks — trying static gate fallback")
                    except Exception as _se:
                        log(f"SAM2 video propagation failed: {_se}")
                        log(traceback.format_exc())
                if not _braw_sam2_video_masks:
                    try:
                        _braw_tif_buffers[0].seek(0)
                        _shape_pil = _PILImage.open(_braw_tif_buffers[0]).convert("RGB")
                        _shape_arr = np.array(_shape_pil)
                        _braw_tif_buffers[0].seek(0)
                        _braw_sam2_gate = _load_sam2_output_gate(_shape_arr.shape, settings)
                        del _shape_arr, _shape_pil
                        if _braw_sam2_gate is not None:
                            log(f"SAM2 static gate loaded for BRAW — applying to all {dur} frames")
                        else:
                            log("SAM2 gate: not loaded (file missing or alpha_method mismatch)")
                    except Exception as _ge:
                        log(f"SAM2 gate load failed: {_ge}")

            for fidx, _buf in enumerate(_braw_tif_buffers):
                if processing_cancelled:
                    log(f"Cancelled at frame {pr}/{dur}")
                    status(f"CANCELLED — {pr} frames saved")
                    break
                # ON-DEMAND DECODE: read one frame from its BytesIO buffer, convert to uint8 BGR,
                # process it, then let it fall out of scope. This keeps peak numpy RAM at 1 frame.
                try:
                    _buf.seek(0)
                    _fd_pil = _PILImage.open(_buf).convert("RGB")
                    _fd_arr = np.array(_fd_pil)
                    if _fd_arr.dtype == np.uint16:
                        _fd_arr = (_fd_arr >> 8).astype(np.uint8)
                    elif _fd_arr.dtype != np.uint8:
                        _fd_arr = (_fd_arr.astype(np.float64) / float(np.iinfo(_fd_arr.dtype).max) * 255.0).clip(0, 255).astype(np.uint8)
                    frame = np.ascontiguousarray(cv2.cvtColor(_fd_arr, cv2.COLOR_RGB2BGR))
                    del _fd_arr, _fd_pil  # Free intermediates immediately
                except Exception as _fde:
                    log(f"  Decode frame {fidx} failed: {_fde} — skipping")
                    pr += 1
                    continue
                status(f"Keying frame {pr+1} of {dur}...")
                chroma_float = chroma_hint_gen.generate_hint(frame).astype(np.float32) / 255.0
                fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                res = proc.process_frame(fr, chroma_float, ps)
                fg, mt = res.get("fg"), res.get("alpha")
                if _despill_str > 0 and fg is not None:
                    fg = _cu.despill_opencv(fg, green_limit_mode="average", strength=_despill_str)
                # Apply SAM2 matte to BRAW frame — per-frame tracking mask (propagation)
                # preferred over static gate; static gate used only as fallback.
                # WHAT IT DOES: Multiplies NN alpha by SAM2 mask for this frame.
                # DEPENDS-ON: _braw_sam2_video_masks (propagation), _braw_sam2_gate (fallback)
                # AFFECTS: mt for this frame only
                if mt is not None:
                    from sam2_combine import apply_sam2_gate
                    if _braw_sam2_video_masks and fidx in _braw_sam2_video_masks:
                        _gate = _dilate_sam2_mask(_braw_sam2_video_masks[fidx], margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
                        _gate = _soften_sam2_mask(_gate, soften=settings.get("sam2_soften", 0))
                        _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                        mt = apply_sam2_gate(_mt2d, _gate, invert=bool(settings.get("sam_invert", False)))
                    elif _braw_sam2_gate is not None:
                        _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                        mt = apply_sam2_gate(_mt2d, _braw_sam2_gate, invert=bool(settings.get("sam_invert", False)))
                choke_px = int(settings.get("choke", 0))
                if choke_px > 0 and mt is not None:
                    _k = choke_px * 2 + 1
                    _kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_k, _k))
                    _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    mt = cv2.erode((_mt_c * 255).astype(np.uint8), _kernel).astype(np.float32) / 255.0
                    log(f"Choke: {choke_px}px")
                # Despeckle for the rendered output (parity with viewer's render_composite).
                mt = _apply_despeckle_to_alpha(mt, settings)
                if fg is not None and mt is not None:
                    op = od / f"CK_{cn}_{pr:06d}.png"
                    save_output(fg, mt, op, settings["export_format"])
                    ofs.append(str(op))
                del frame  # Release this frame's numpy array before the next decode
                pr += 1
                el = time.time() - st
                fpsr = pr / el if el > 0 else 0
                log(f"{pr}/{dur} ({fpsr:.1f}fps)")
                # Update progress bar on the main thread — BRAW sync path never goes through _ui_queue.
                try:
                    _bp = max(0.0, min(1.0, pr / dur)) if dur else 0.0
                    if _bp >= 1.0:
                        _bss = "background: #00ffff; border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #111; font-size: 10px;"
                    else:
                        _bss = (f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                                f"stop:0 #00cccc, stop:{_bp:.3f} #00cccc, "
                                f"stop:{_bp:.3f} #1a1a1a, stop:1 #1a1a1a); "
                                f"border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #fff; font-size: 10px;")
                    items["Progress"].StyleSheet = _bss
                    items["Progress"].Text = f"  {pr} / {dur} frames"
                    # Force Qt to repaint now — sync path blocks the event loop so without
                    # this the bar only shows its final state after all frames are done.
                    try:
                        from PyQt5.QtWidgets import QApplication as _QApp
                        _QApp.processEvents()
                    except Exception:
                        try:
                            from PySide2.QtWidgets import QApplication as _QApp
                            _QApp.processEvents()
                        except Exception:
                            pass
                except Exception:
                    pass
            el = time.time() - st
            log(f"Done: {len(ofs)} frames in {el:.1f}s")
            try: items["Progress"].Visible = False
            except Exception: pass
            if ofs and not processing_cancelled:
                status("Importing to MediaPool...")
                _do_import({
                    "ofs": ofs, "output_track": output_track,
                    "source_track": source_track, "in_f": in_f, "settings": settings,
                })
        except Exception as _e:
            log(f"Range error: {_e}")
            log(traceback.format_exc())
            status("ERROR!")
            try: items["Progress"].Visible = False
            except Exception: pass
        finally:
            _range_running = False
        return  # BRAW sync path done — skip thread launch below

    # Run heavy processing in a background thread so the Fusion event loop stays alive.
    # The fix for the old deadlock: patch sys.stdout/stderr at thread start (Resolve sets
    # them None for non-main threads), then route all UI updates through _ui_queue so only
    # the main-thread PollTimer touches Fusion widgets. Resolve MediaPool/timeline calls
    # stay on the main thread via _import_queue → _do_import().
    try: items["PollTimer"].Interval = 500  # Wake up fast — processing starting
    except Exception: pass
    _range_running = True

    def _run():
        global _range_running
        # sys and io are module globals (line 22) — no import needed here.
        # sys.stdout/stderr already patched at module level (lines 29-30).
        # A redundant 'import sys as _sys' inside a daemon thread can block on
        # Fusion's custom import hooks, freezing the thread silently.
        if sys.stdout is None: sys.stdout = io.StringIO()
        if sys.stderr is None: sys.stderr = io.StringIO()
        try:
            def _tlog(msg):
                # Queue-only — no file I/O in thread (Defender blocks file opens, even with try/except).
                _ui_queue.put(("log", msg))
            def _tstatus(msg):
                _ui_queue.put(("status", msg))
            def _tprogress(done, total):
                val = int(done / total * 100) if total > 0 else 0
                _ui_queue.put(("progress", val))
        except BaseException as _be:
            _range_running = False
            return

        ofs = []
        pr = 0
        st = time.time()
        try:
            # SAM2 video propagation — runs once up front, produces one mask per frame.
            # For BRAW, braw_frames_dir is the TIFF sequence directory — pass it so SAM2
            # gets full-chroma frames. Anchor frame shifts to TIFF index space (0-based).
            sam2_video_masks = {}
            if (settings.get("alpha_method") == 1 and
                    (sam_points.get("positive") or sam_points.get("negative"))):
                _tlog(f"SAM2 mode — running video propagation for full range... braw_frames_dir={braw_frames_dir!r}")
                if braw_frames_dir:
                    _anchor_abs = sam_points.get("frame")
                    _anchor_in_tif = (_anchor_abs - in_f) if _anchor_abs is not None else None
                    sam2_video_masks = run_sam2_video_propagation(
                        braw_frames_dir, 0, 0, 0, dur,
                        sam_points.get("positive", []),
                        sam_points.get("negative", []),
                        _anchor_in_tif,
                    )
                else:
                    sam2_video_masks = run_sam2_video_propagation(
                        fp, ss, cs, in_f, out_f,
                        sam_points.get("positive", []),
                        sam_points.get("negative", []),
                        sam_points.get("frame"),
                    )
                if sam2_video_masks:
                    _tlog(f"SAM2 video: {len(sam2_video_masks)} masks ready")
                else:
                    _tlog("SAM2 propagation returned no masks — falling back to chroma hint")

            # WHAT IT DOES: Static SAM2 gate fallback — if video propagation was skipped because
            #   sam_points lost on Resolve restart, but sam2_mask.png still exists on disk,
            #   load one mask on the first rendered frame and stamp it on every frame in the range.
            # DEPENDS-ON: _load_sam2_output_gate, sam2_video_masks, settings["alpha_method"]
            # AFFECTS: _static_sam2_gate applied per-frame in the render loop below.
            # DANGER ZONE: HIGH — gate is loaded lazily (first frame) to get real frame_shape.
            #   breaks: if sam2_mask.png resolution differs wildly from source frames AND cv2.resize
            #   produces a bad result (e.g. rotated media); depends on: SESSION_DIR/sam2_mask.png
            _static_sam2_gate = None          # populated on first frame if needed
            _static_sam2_gate_loaded = False  # guard so we only attempt once

            # For BRAW: read TIFF files from braw_frames_dir (4:4:4, no seeking needed).
            # For normal files: seek with VideoCapture as before.
            cap = None
            braw_tif_files = []
            if braw_frames_dir:
                # Use list pre-computed on main thread — avoids thread glob which triggers Defender directory scan.
                braw_tif_files = _braw_tif_files_precomputed
                _tlog(f"BRAW frames: {len(braw_tif_files)} TIFF files")
                _src_fps = fps
            else:
                cap = cv2.VideoCapture(fp)
                if not cap.isOpened():
                    _tstatus("Cannot open video"); return
                _src_fps = cap.get(cv2.CAP_PROP_FPS) or fps
            for tf in range(in_f, out_f):
                if processing_cancelled:
                    _tlog(f"Cancelled at frame {pr}/{dur}")
                    _tstatus(f"CANCELLED — {pr} frames saved")
                    break
                if braw_frames_dir:
                    fidx = tf - in_f
                    frame = None
                    if fidx < len(braw_tif_files):
                        try:
                            # Use numpy array pre-decoded on main thread — zero file I/O or PIL in thread.
                            if fidx < len(_braw_frames_decoded):
                                frame = _braw_frames_decoded[fidx]
                            else:
                                # Fallback: read from disk if pre-decode didn't cover this frame.
                                tif_path = str(braw_tif_files[fidx])
                                frame = cv2.imread(tif_path, cv2.IMREAD_UNCHANGED)
                        except Exception as _fe:
                            _tlog(f"Read error at frame {tf}: {_fe}")
                            frame = None
                    if frame is None:
                        _tlog(f"Read failed at frame {tf} (TIFF index {fidx}) — skipping")
                        continue
                else:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, ss + (tf - cs))
                    ret, frame = cap.read()
                    if not ret:
                        _tlog(f"Read failed at frame {tf} — skipping")
                        continue
                range_idx = tf - in_f
                chroma_float = chroma_hint_gen.generate_hint(frame).astype(np.float32) / 255.0
                ah = chroma_float
                fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                _torch_m = sys.modules.get("torch")
                if _torch_m is not None:
                    try: _torch_m.cuda.empty_cache()
                    except Exception: pass
                res = proc.process_frame(fr, ah, ps)
                if _torch_m is not None:
                    try: _torch_m.cuda.synchronize()
                    except Exception: pass
                fg, mt = res.get("fg"), res.get("alpha")
                if _despill_str > 0 and fg is not None:
                    fg = _cu.despill_opencv(fg, green_limit_mode="average", strength=_despill_str)
                # WHAT IT DOES: Apply SAM2 garbage matte to the keyed alpha for this frame.
                #   Primary path: per-frame propagated mask from sam2_video_masks.
                #   Fallback path: static sam2_mask.png loaded once and reused every frame —
                #     handles Resolve-restart case where sam_points were lost but PNG still exists.
                # DEPENDS-ON: sam2_video_masks, _static_sam2_gate, _load_sam2_output_gate
                # AFFECTS: mt (alpha) — multiplied by gate, zeroing pixels outside the matte.
                if mt is not None:
                    from sam2_combine import apply_sam2_gate
                    if sam2_video_masks and range_idx in sam2_video_masks:
                        # Normal path — per-frame mask from video propagation.
                        _gate = _dilate_sam2_mask(sam2_video_masks[range_idx], margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
                        _gate = _soften_sam2_mask(_gate, soften=settings.get("sam2_soften", 0))
                        _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                        mt = apply_sam2_gate(_mt2d, _gate, invert=bool(settings.get("sam_invert", False)))
                    else:
                        # Fallback path — static gate loaded lazily on first frame so we have
                        # real frame.shape for the resize check inside _load_sam2_output_gate.
                        if not _static_sam2_gate_loaded:
                            _static_sam2_gate = _load_sam2_output_gate(frame.shape, settings)
                            _static_sam2_gate_loaded = True
                            if _static_sam2_gate is not None:
                                _tlog("SAM2 static gate loaded — applying same mask to all range frames (no propagation)")
                        if _static_sam2_gate is not None:
                            _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                            mt = apply_sam2_gate(_mt2d, _static_sam2_gate, invert=bool(settings.get("sam_invert", False)))
                choke_px = int(settings.get("choke", 0))
                if choke_px > 0 and mt is not None:
                    k = choke_px * 2 + 1
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                    _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    mt = cv2.erode((_mt_c * 255).astype(np.uint8), kernel).astype(np.float32) / 255.0
                    log(f"Choke: {choke_px}px")
                # Despeckle for the rendered output (parity with viewer's render_composite).
                mt = _apply_despeckle_to_alpha(mt, settings)
                if fg is not None and mt is not None:
                    op = od / f"CK_{cn}_{pr:06d}.png"
                    # Encode PNG to bytes IN MEMORY — no file I/O, no Defender block.
                    _fmt = settings["export_format"]
                    _m = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    if _fmt == 0:
                        _fb = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
                        _au = (_m * 255).astype(np.uint8)
                        _img = cv2.merge([_fb[:,:,0], _fb[:,:,1], _fb[:,:,2], _au])
                    elif _fmt == 1:
                        _img = (_m * 255).astype(np.uint8)
                    else:
                        _img = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
                    _ret, _buf = cv2.imencode('.png', _img)
                    if _ret:
                        _save_queue.put(("save", str(op), _buf.tobytes()))
                        ofs.append(str(op))
                    pr += 1
                    el = time.time() - st
                    fpsr = pr / el if el > 0 else 0
                    rem = (dur - pr) / fpsr if fpsr > 0 else 0
                    _tstatus(f"{pr}/{dur} ({fpsr:.1f}fps, {rem:.0f}s left)")
                    _tprogress(pr, dur)
                    if pr % 10 == 0: _tlog(f"{pr}/{dur}")
            if cap:
                cap.release()

            if ofs and not processing_cancelled:
                _tlog(f"Done: {len(ofs)} frames in {time.time()-st:.1f}s")
                _tstatus("Importing to MediaPool...")
                _tprogress(dur, dur)  # fill to 100% before hiding
                _ui_queue.put(("progress", -1))
                # Put import task AFTER all save tasks — poll timer processes in order.
                _save_queue.put(("import", {
                    "ofs": ofs, "output_track": output_track,
                    "source_track": source_track, "in_f": in_f, "settings": settings,
                }))
            else:
                _ui_queue.put(("progress", -1))
        except BaseException as _e:
            # BaseException catches SystemExit/KeyboardInterrupt that bypass except Exception.
            # traceback is a module-level import — never import inside a thread (Fusion hooks block).
            try:
                _tstatus("ERROR!")
                _tlog(f"Range error: {_e}")
                _tlog(traceback.format_exc())
            except Exception: pass
        finally:
            _range_running = False
            if braw_frames_dir:
                # shutil is a module-level import — never import inside a thread.
                try: shutil.rmtree(braw_frames_dir, ignore_errors=True)
                except: pass
            try:
                # Lookup torch from sys.modules — never import inside a thread (Fusion hooks block).
                _torch_mod = sys.modules.get("torch")
                if _torch_mod is not None:
                    _torch_mod.cuda.empty_cache()
            except Exception: pass
    _t = threading.Thread(target=_run, daemon=True)
    _t.start()
    log(f"Range thread launched (alive={_t.is_alive()})")

# WHAT IT DOES: Sets the cancel flag so the range processing loop stops on next iteration
def on_cancel(ev):
    global processing_cancelled, _range_running
    processing_cancelled = True
    _range_running = False
    _scrub_pending.clear()
    _scrub_pending_buffers.clear()
    _scrub_pending_ctx.clear()
    _scrub_key_queue.clear()
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
    # DANGER ZONE HIGH: The adaptive interval logic MUST live in finally: — if any
    # queue drain raises unexpectedly before reaching it, the timer stays at 500ms
    # forever (never backs off to 5000ms idle), sustaining ASIO interrupt pressure.
    global _range_running, _proxy_mpi, _proxy_mode_saved
    try:
        import time as _pt, tempfile as _pt_tf
        try:
            with open(str(Path(_pt_tf.gettempdir()) / "ck_timer_diag.txt"), "a", encoding="utf-8") as _ptf:
                _ptf.write(f"[{_pt.time():.2f}] tick pending={len(_scrub_pending)} running={_range_running} cancelled={processing_cancelled}\n")
        except Exception: pass
        # Refiner debounce: if slider moved 800ms ago and viewer is open, re-key
        global _refiner_rekey_pending
        import time as _rpt
        if _refiner_rekey_pending > 0 and (_rpt.time() - _refiner_rekey_pending) > 0.8:
            if _viewer_proc is not None and _viewer_proc.poll() is None:
                _refiner_rekey_pending = 0.0
                try:
                    reprocess_with_cached()
                except Exception as _rpe:
                    log(f"Refiner reprocess error: {_rpe}")
            else:
                _refiner_rekey_pending = 0.0
        # --- SCRUB Phase 1: export one frame per tick so close/cancel stay responsive ---
        # Each export blocks this tick for ~2 sec (Resolve API, unavoidable).
        # Between exports the event loop runs — CLOSE PANEL / CANCEL process normally.
        if _scrub_pending:
            if processing_cancelled:
                _scrub_pending.clear()
                _scrub_pending_buffers.clear()
                _scrub_pending_ctx.clear()
                _scrub_key_queue.clear()
                _range_running = False
                items["Status"].Text = "Scrub cancelled"
            else:
                try:
                    _sp_fi, _sp_tl = _scrub_pending.pop(0)
                    _sp_ctx  = _scrub_pending_ctx
                    _sp_N    = _sp_ctx["N"]
                    _sp_src  = _sp_ctx["ss"] + (_sp_tl - _sp_ctx["cs"])
                    log(f"SCRUB timer: exporting frame {_sp_fi+1}/{_sp_N} tl={_sp_tl} src={_sp_src}")
                    items["Status"].Text = f"Scrub: exporting frame {_sp_fi+1}/{_sp_N}..."
                    _sp_fdir = _export_braw_range_to_frames(
                        _sp_ctx["mpi"], _sp_src, _sp_src + 1, _sp_ctx["timeline"], _sp_tl, _sp_ctx["fps"])
                    log(f"SCRUB timer: frame {_sp_fi+1} export done — fdir={_sp_fdir is not None}")
                    _sp_buf = None
                    if _sp_fdir is not None:
                        _sp_tifs = sorted(Path(_sp_fdir).glob("*.tif*"))
                        log(f"SCRUB timer: found {len(_sp_tifs)} tifs in {_sp_fdir}")
                        if _sp_tifs:
                            try:
                                with open(str(_sp_tifs[0]), "rb") as _sp_f:
                                    _sp_buf = io.BytesIO(_sp_f.read())
                            except Exception: pass
                        shutil.rmtree(_sp_fdir, ignore_errors=True)
                    _scrub_pending_buffers.append(_sp_buf)
                    if not _scrub_pending:
                        # All frames exported — hand off to keying thread.
                        _bufs = list(_scrub_pending_buffers)
                        _ctx  = dict(_scrub_pending_ctx)
                        _scrub_pending_buffers.clear()
                        _scrub_pending_ctx.clear()
                        _scrub_key_queue.clear()
                        items["Status"].Text = f"Scrub: keying {_sp_N} frames (panel stays responsive)..."
                        _start_scrub_keying(_bufs, _ctx)
                except Exception as _scrub_ex:
                    import traceback as _scrub_tb
                    log(f"SCRUB timer ERROR: {_scrub_ex}")
                    log(_scrub_tb.format_exc())
                    _scrub_pending.clear()
                    _scrub_pending_buffers.clear()
                    _scrub_pending_ctx.clear()
                    _scrub_key_queue.clear()
                    _range_running = False
                    items["Status"].Text = f"Scrub error: {_scrub_ex}"
        # Poll for Resolve optimized media completion — when ready, enable proxy playback.
        # Save whatever proxy mode was set before so we can restore it after scrub finishes.
        if _proxy_mpi is not None and _proxy_mode_saved is None:
            try:
                if media_pool and media_pool.HasOptimizedMedia([_proxy_mpi]):
                    _proxy_mpi = None
                    _proxy_mode_saved = project.GetSetting("proxyMediaMode") or "0"
                    project.SetSetting("proxyMediaMode", "1")
                    log(f"Proxy mode ON (was: {_proxy_mode_saved}) — ExportCurrentFrameAsStill now uses proxy frames")
                    items["Status"].Text = "Proxy ready — SCRUB RANGE will be faster"
            except Exception:
                pass
        while not _ui_queue.empty():
            try:
                kind, msg = _ui_queue.get_nowait()
                if kind == "log":
                    items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"
                elif kind == "status":
                    items["Status"].Text = msg
                elif kind == "restore_proxy":
                    # Scrub keying finished — restore proxy mode to whatever it was before
                    if _proxy_mode_saved is not None:
                        try:
                            project.SetSetting("proxyMediaMode", _proxy_mode_saved)
                            log(f"Proxy mode restored: {_proxy_mode_saved}")
                        except Exception as _rpe:
                            log(f"Proxy mode restore error: {_rpe}")
                        _proxy_mode_saved = None
                elif kind == "progress":
                    if msg < 0:
                        try: items["Progress"].Visible = False
                        except Exception: pass
                    else:
                        try:
                            pct = max(0.0, min(1.0, msg / 100.0))
                            txt = f"  {int(pct * 100)}%"
                            if pct >= 1.0:
                                ss = "background: #00ffff; border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #111; font-size: 10px;"
                            elif pct <= 0.0:
                                ss = "background: #111; border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #888; font-size: 10px;"
                            else:
                                ss = (f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                                      f"stop:0 #00cccc, stop:{pct:.3f} #00cccc, "
                                      f"stop:{pct:.3f} #1a1a1a, stop:1 #1a1a1a); "
                                      f"border: 1px solid #333; border-radius: 4px; min-height: 20px; max-height: 20px; color: #fff; font-size: 10px;")
                            items["Progress"].StyleSheet = ss
                            items["Progress"].Text = txt
                            items["Progress"].Visible = True
                        except Exception: pass
                elif kind == "probe":
                    try:
                        with open(Path(tempfile.gettempdir()) / "ck_thread.txt", "a", encoding="utf-8") as _f:
                            _f.write(msg + "\n")
                    except Exception: pass
            except Exception:
                pass
        while not _save_queue.empty():
            try:
                item = _save_queue.get_nowait()
                if item[0] == "save":
                    _, path_str, png_bytes = item
                    try:
                        with open(path_str, 'wb') as _sf:
                            _sf.write(png_bytes)
                    except Exception as _se:
                        log(f"Save error {path_str}: {_se}")
                elif item[0] == "import":
                    _, import_task = item
                    _do_import(import_task)
            except Exception:
                pass
        while not _import_queue.empty():
            try:
                task = _import_queue.get_nowait()
                _do_import(task)
            except Exception as e:
                log(f"Import queue error: {e}")
    finally:
        # ADAPTIVE TIMER — reduce scripting-thread interrupts when completely idle.
        # Every PollTimer tick re-enters Resolve's main thread, which shares audio scheduling
        # with Windows WASAPI. At 500ms (2x/sec) this causes audible pops on Focusrite interfaces.
        # When all queues are drained and no range is running, slow to 5000ms (0.2x/sec).
        # --- SCRUB Phase 2: key one frame per timer tick on the main thread ---
        # Background threads deadlock in Fusion's Python runtime. Main-thread keying
        # works fine — the CUDA proc was initialized here and inference runs ~2-5 sec/frame.
        if _scrub_key_queue:
            try:
                _key_one_scrub_frame()
            except Exception as _kex:
                import traceback as _ktb
                log(f"SCRUB key error: {_kex}\n{_ktb.format_exc()}")
                _scrub_key_queue.clear()
                _range_running = False
        # Processing start sites reset it back to 500ms so UI stays responsive during work.
        # DEPENDS-ON: _range_running, _ui_queue, _save_queue, _import_queue, items["PollTimer"]
        # AFFECTS: PollTimer.Interval (Fusion UIManager timer property)
        all_idle = (
            not _range_running
            and _ui_queue.empty()
            and _save_queue.empty()
            and _import_queue.empty()
            and not _scrub_key_queue
        )
        try:
            items["PollTimer"].Interval = 5000 if all_idle else 500
        except Exception:
            pass

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
                    log(f"V{source_track} hidden — press D in timeline to re-enable source clip")
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
    global _viewer_proc, _scrubber_proc, processing_cancelled, _range_running
    # Signal any running scrub/range worker thread to stop on its next iteration check.
    # Must happen BEFORE killing subprocesses so the thread doesn't try to spawn new ones.
    # DANGER ZONE — CRITICAL: set this FIRST; the worker checks it every frame decode cycle.
    processing_cancelled = True
    _range_running = False
    _scrub_pending.clear()
    _scrub_pending_buffers.clear()
    _scrub_pending_ctx.clear()
    _scrub_key_queue.clear()
    # Stop the PollTimer first — prevents it firing against a half-dead UI during teardown.
    try: items["PollTimer"].Stop()
    except Exception: pass
    # Kill viewer process tree — taskkill /F /T kills the viewer AND any SAM2 subprocesses
    # it spawned. Without /T, child processes survive and hold the GPU open, which keeps
    # Resolve's process manager waiting indefinitely (the "End Task" bug).
    import subprocess as _sp
    for _proc in [_viewer_proc, _scrubber_proc]:
        if _proc is not None:
            try: _sp.run(["taskkill", "/F", "/T", "/PID", str(_proc.pid)],
                         stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=5,
                         creationflags=_sp.CREATE_NO_WINDOW)
            except Exception: pass
    _viewer_proc = None
    _scrubber_proc = None
    # Drop CUDA model reference immediately — do NOT call proc.cleanup() here because
    # CUDA teardown in a closing Resolve session blocks indefinitely on Windows.
    # Windows reclaims GPU memory when the process dies; we just need to die fast.
    try: cached_processor["proc"] = None
    except Exception: pass
    try: _INSTANCE_LOCK.unlink(missing_ok=True)
    except: pass
    # Exit immediately — do NOT call disp.ExitLoop() and wait for RunLoop to unwind.
    # When Resolve is mid-shutdown, win.Hide() (called after RunLoop returns) blocks
    # indefinitely on a window with no valid Fusion context, causing the Task Manager hang.
    # os._exit skips all Python/CUDA finalizers — Windows reclaims GPU memory on process death.
    os._exit(0)

win.On.SetInPoint.Clicked = on_set_in_point
win.On.SetOutPoint.Clicked = on_set_out_point
win.On.ClearRange.Clicked = on_clear_range
win.On.BrowseOutput.Clicked = on_browse_output
win.On.ShowPreview.Clicked = on_show_preview
win.On.ProcessFrame.Clicked = on_process_frame
win.On.ScrubRange.Clicked = on_scrub_range
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
win.On.CK.Close = on_close  # X button on the window title bar
win.On.PollTimer.Timeout = on_poll_timer

# WHAT IT DOES: Keeps the Refiner slider and spinbox in sync.
#   Guards prevent infinite loops when one updates the other.
#   Margin/Soften sync was removed 2026-04-26 — those sliders now live only in
#   the live preview viewer (preview_viewer_v2.py), not the panel.
# DEPENDS-ON: items["RefinerStrength"], items["RefinerInput"]
# AFFECTS: display and the value actually read at process time (slider.Value)

def _write_live_params_slider(updates):
    # WHAT IT DOES: Merges 'updates' dict into SESSION_DIR/live_params.json atomically.
    #   Used by the live re-key path to signal "rekeying:true" / "rekeying:false"
    #   to the viewer so it can show/hide its overlay during CUDA inference.
    # DEPENDS-ON: SESSION_DIR
    # AFFECTS: live_params.json on disk
    import json as _lpj
    lp_path = SESSION_DIR / "live_params.json"
    try:
        lp = _lpj.loads(lp_path.read_text(encoding="utf-8")) if lp_path.exists() else {}
    except Exception:
        lp = {}
    lp.update(updates)
    tmp = str(lp_path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as _f:
            _lpj.dump(lp, _f, indent=2)
        import os as _os2; _os2.replace(tmp, str(lp_path))
    except Exception:
        pass

_refiner_rekey_pending = 0.0   # nonzero = timestamp of last refiner slider move
_syncing_refiner = False

def on_refiner_changed(ev):
    global _syncing_refiner
    if _syncing_refiner: return
    _syncing_refiner = True
    try: items["RefinerInput"].Value = items["RefinerStrength"].Value
    except Exception: pass
    _syncing_refiner = False
    global _refiner_rekey_pending
    import time as _rt; _refiner_rekey_pending = _rt.time()
    try: items["PollTimer"].Interval = 500  # wake timer fast so debounce fires in ~1.3s
    except Exception: pass

def on_refiner_input(ev):
    global _syncing_refiner
    if _syncing_refiner: return
    _syncing_refiner = True
    try: items["RefinerStrength"].Value = items["RefinerInput"].Value
    except Exception: pass
    _syncing_refiner = False
    global _refiner_rekey_pending
    import time as _rt; _refiner_rekey_pending = _rt.time()
    try: items["PollTimer"].Interval = 500  # wake timer fast so debounce fires in ~1.3s
    except Exception: pass

win.On.RefinerStrength.ValueChanged  = on_refiner_changed
win.On.RefinerInput.ValueChanged     = on_refiner_input

# WHAT IT DOES: Shows the About dialog with credits, how-to-use guide, and Ko-fi link.
#   Credits Niko Pueringer/Corridor Digital (engine) and Roberto+Elvis Lopez/StuntWorks (plugin).
# ISOLATED: self-contained dialog, no side effects
def on_about(ev):
    about_win = disp.AddWindow({"ID": "About", "WindowTitle": "About CorridorKey Pro", "Geometry": [200, 100, 480, 860]}, [
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
            ui.Label({"Text": "Stuntworks Cinema", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 13, "Bold": True}), "StyleSheet": "color: #E91E63;"}),
            ui.Label({"Text": "github.com/stuntworks/CorridorKey-Plugin", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #2196F3; font-size: 11px;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "What Makes This Unique", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "This plugin combines two independent AI systems\n"
                              "into a single one-click workflow:\n\n"
                              "CorridorKey — a neural keyer trained on real VFX\n"
                              "footage that produces clean chroma mattes.\n\n"
                              "Subject Mask — Meta AI object tracking that locks\n"
                              "a precise mask to your subject across any range,\n"
                              "even through motion blur and partial occlusion.\n\n"
                              "Together they solve what neither can alone:\n"
                              "a clean chroma key that stays locked to the\n"
                              "subject on every frame.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px;", "WordWrap": True}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "Open Source Credits", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 13, "Bold": True}), "StyleSheet": "color: #9E9E9E;"}),
            ui.Label({"Text": "Subject Mask is powered by SAM2\n"
                              "(Segment Anything Model 2)  ©  Meta AI\n"
                              "Used under the Apache 2.0 open source license.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #777; font-size: 10px;", "WordWrap": True}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "StuntWorks is a professional stunt rigging company.\nIn our spare time we build the tools we wish existed —\nfree plugins, automation, and workflow helpers.\nIf you find this useful, a coffee helps us keep building.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px; font-style: italic;", "WordWrap": True}),
            ui.Label({"Text": "☕  ko-fi.com/stuntworks", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #FF5E5B; font-size: 13px; font-weight: bold;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "How To Use", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "1. Place green screen footage on your timeline\n"
                              "2. Set Alpha Method — Simple or Subject Mask\n"
                              "3. Choose Green or Blue screen type\n"
                              "4. Click SHOW PREVIEW to check the key\n"
                              "5. Adjust Mask Margin and Soften as needed\n"
                              "6. Set IN/OUT points for your range\n"
                              "7. Click PROCESS RANGE to render\n"
                              "8. Keyed output goes to Track 2 automatically\n\n"
                              "Tip: Place a background plate on the track below\n"
                              "your green screen clip to see the real composite\n"
                              "in the preview window.\n\n"
                              "Refiner note: The Refiner improves fine edge\n"
                              "detail such as hair and soft edges. It has no\n"
                              "effect when Subject Mask is active — the mask\n"
                              "already clips away the fine edges the Refiner\n"
                              "works on. Use Refiner on simple chroma keys\n"
                              "without Subject Mask for best results.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px;", "WordWrap": True}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "Watch the Tutorials", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "Step-by-step video tutorials — coming soon!\n"
                              "Subscribe so you don't miss them.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px;", "WordWrap": True}),
            ui.Label({"Text": "youtube.com/@StuntWorksCinema", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #cc3300; font-size: 12px; font-weight: bold;"}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
            ui.Label({"Text": "Free Test Footage", "Alignment": {"AlignHCenter": True}, "Font": ui.Font({"PixelSize": 14, "Bold": True}), "StyleSheet": "color: #FF9800;"}),
            ui.Label({"Text": "Download free green screen clips to test\n"
                              "CorridorKey Pro — includes BRAW, MOV, and\n"
                              "H.264 samples. Link on the YouTube channel.",
                      "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #ccc; font-size: 11px;", "WordWrap": True}),
            ui.Label({"Text": "─────────────────────────────", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #333;"}),
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

# WHAT IT DOES: Header link — Stuntworks Cinema → YouTube channel
def on_header_sw(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://www.youtube.com/@StuntWorksCinema"], creationflags=subprocess.CREATE_NO_WINDOW)

win.On.HeaderCK.Clicked = on_header_ck
win.On.HeaderSW.Clicked = on_header_sw

# WHAT IT DOES: Opens StuntWorks YouTube channel in the system browser
def on_youtube(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://www.youtube.com/@StuntWorksCinema"], creationflags=subprocess.CREATE_NO_WINDOW)

# WHAT IT DOES: Opens the StuntWorks Ko-fi tip jar in the system browser
def on_kofi(ev):
    import subprocess
    subprocess.Popen(["cmd", "/c", "start", "https://ko-fi.com/stuntworks"], creationflags=subprocess.CREATE_NO_WINDOW)

win.On.YouTubeBtn.Clicked = on_youtube
win.On.KofiBtn.Clicked = on_kofi
win.On.AboutBtn.Clicked = on_about
win.On.CK.Close = on_close

try: items["Log"].PlainText = ""
except Exception: pass
try:
    with open(_ck_debug_log, "w", encoding="utf-8") as _clf: pass
except Exception: pass
log("CorridorKey Pro Ready")
log("SAM2 | Frame Range | Export Modes")
win.Show()
disp.RunLoop()
win.Hide()
# Force-exit immediately after the event loop ends.
# Python's normal shutdown runs CUDA/torch finalizers which block for 30-60 seconds
# on Windows with a GPU model loaded — that's why Resolve hangs and needs End Task.
# Windows reclaims all GPU memory when the process dies; skipping finalizers is safe.
os._exit(0)
