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
import sys, os, site, tempfile, math, queue, threading, io, traceback, shutil, signal
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
# v1.0 — enable OpenCV's OpenEXR codec so the EXR 32-bit output option works.
# OpenCV ships EXR disabled by default since v4.5 (CVE-2017-5110/5111 family).
# Plugin runs only the user's local PNG/TIFF/EXR files in this venv — no
# untrusted EXR ingestion path — so the disable-by-default isn't relevant
# here. Must be set BEFORE cv2 imports.
_os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
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
# Shutdown sentinel — set True in on_close so PollTimer ticks that race the close event no-op.
# Prevents mid-tick inference (2-5s on UI thread) from blocking on_close from reaching os._exit.
_shutting_down = False
# WHAT IT DOES: Holds a CPU-only processor pre-inited at LIVE PREVIEW time for SCRUB RANGE use.
#   Avoids creating a new CPU proc inside the background thread (which triggers torch.compile
#   at img_size=2048 on CPU — a 6+ minute hang). Populated once; reused for all scrub runs.
# DEPENDS-ON: CorridorKeyProcessor(device="cpu"), CORRIDORKEY_SKIP_COMPILE=1 env flag
# AFFECTS: _start_scrub_keying worker (reads this instead of creating its own)
cached_scrub_cpu_proc = {"proc": None}  # CPU proc for SCRUB RANGE — pre-inited, never CUDA
sam_points = {"positive": [], "negative": [], "frame": None}
# Multi-object v0.8 — per-mask click data populated by _merge_live_params
# from sam_positive_obj{N}, sam_negative_obj{N}, sam_anchor_frame_obj{N}.
# sam_points (above) is kept as the legacy union for code that hasn't been
# refactored yet. Multi-object SAM2 video propagation reads sam_points_per_obj.
sam_points_per_obj: dict = {
    1: {"positive": [], "negative": [], "frame": None},
    2: {"positive": [], "negative": [], "frame": None},
}
frame_range = {"in_frame": None, "out_frame": None}
_viewer_proc = None      # Tracks Live Preview subprocess — stays alive while scrubber is open
_scrubber_proc = None   # Tracks SCRUB RANGE subprocess — separate from live preview
_scrubber_job = None    # Win32 Job Object holding the scrubber — KILL_ON_JOB_CLOSE auto-kills it when Resolve's Python dies (mirrors _viewer_job). Handle must stay referenced or the job closes early.
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
# Hoisted: in-thread `from corridorkey_sam_merge import ...` can deadlock
# Fusion's import hook on daemon threads, hanging PROCESS RANGE silently.
from corridorkey_sam_merge import (
    apply_chroma_kill_to_matte,
    union_binary_silhouettes,
    process_sam_matte,
    logits_to_soft_mask,
    merge_ck_with_sam_active,
)

# 2026-05-14: chroma_kill bypass flag. apply_chroma_kill_to_matte did NOT exist
# in v1.0.1-stable but runs by default in current code at 3 call sites.
# threshold=0.05 is aggressive and can shave soft hair-edge alpha where green
# spill is present. Diagnostic bypass for A/B comparison vs v1.0.1 matte
# quality. Flip True to re-enable.
_CK_CHROMA_KILL_ENABLED = False
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
        ui.Slider({"ID": "RefinerStrength", "Minimum": 0, "Maximum": 100, "Value": 100, "Weight": 3,
                   "Orientation": "Horizontal", "SingleStep": 1,
                   "StyleSheet": "QSlider::groove:horizontal { height: 6px; background: #222; border-radius: 3px; } QSlider::sub-page:horizontal { background: #0dcaf0; border-radius: 3px; } QSlider::handle:horizontal { background: #fff; border: 2px solid #0dcaf0; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; } QSlider::handle:horizontal:hover { background: #0dcaf0; border-color: #fff; }"}),
        ui.SpinBox({"ID": "RefinerInput", "Minimum": 0, "Maximum": 100, "Value": 100, "Weight": 0,
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
    # v1.0 — output codec / bit depth picker. PNG 8-bit default for editor
    # workflows. PNG 16-bit eliminates banding on subtle gradients. TIFF 16-bit
    # is universal lossless. EXR 32-bit is the VFX float standard.
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Codec:", "Weight": 0}),
        ui.ComboBox({"ID": "OutputCodec", "Weight": 2, "StyleSheet": "QComboBox { background-color: #1a1a1a; border: 1px solid #333; border-radius: 3px; padding: 4px 8px; color: #ccc; } QComboBox:hover { border-color: #0dcaf0; background-color: #222; } QComboBox::drop-down { border-left: 1px solid #333; width: 24px; } QComboBox::down-arrow { border-top: 5px solid #0dcaf0; border-left: 4px solid transparent; border-right: 4px solid transparent; width: 0; height: 0; }"}),
    ]),
    # v1.0 OutputContent — pick what gets written to disk. Combined is the
    # drag-and-drop default (CK x SAM single RGBA clip). Both is the legacy
    # two-mask power-user mode (CK clip + SAM matte sidecar). CK / SAM only
    # are escape hatches when one of the two is unwanted.
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Label({"Text": "Content:", "Weight": 0}),
        ui.ComboBox({"ID": "OutputContent", "Weight": 2, "StyleSheet": "QComboBox { background-color: #1a1a1a; border: 1px solid #333; border-radius: 3px; padding: 4px 8px; color: #ccc; } QComboBox:hover { border-color: #0dcaf0; background-color: #222; } QComboBox::drop-down { border-left: 1px solid #333; width: 24px; } QComboBox::down-arrow { border-top: 5px solid #0dcaf0; border-left: 4px solid transparent; border-right: 4px solid transparent; width: 0; height: 0; }"}),
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
        ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable source clip after processing  (uncheck to leave source visible)", "Checked": False,
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
    # 2026-05-21 fix v2: previous PID + process-name checks both false-positive
    # because Resolve hosts CK in its own fusion/fuscript process family — the
    # name filter matches even when no real CK is running. Use a HEARTBEAT
    # approach instead: CK touches the lock file every 5s while alive. If the
    # lock's mtime is more than 15 seconds old, treat as stale.
    if _INSTANCE_LOCK.exists():
        try:
            import time as _time_lk
            _lock_age = _time_lk.time() - _INSTANCE_LOCK.stat().st_mtime
            _show_dialog = False
            if _lock_age <= 15.0:
                # Recent lock — verify PID alive. Heartbeat keeps mtime fresh
                # while CK runs; stale > 15s means previous CK crashed or exited.
                pid = int(_INSTANCE_LOCK.read_text().strip())
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    _show_dialog = True
            if _show_dialog:
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
# v1.0 codec dropdown — PNG 8-bit default; user upgrades to 16-bit / EXR for
# lossless / VFX workflows. EXR support via OPENCV_IO_ENABLE_OPENEXR (set at
# the top of this module).
items["OutputCodec"].AddItem("PNG 8-bit")
items["OutputCodec"].AddItem("PNG 16-bit (default, lossless)")
items["OutputCodec"].AddItem("TIFF 16-bit (lossless)")
items["OutputCodec"].AddItem("EXR 32-bit (VFX float)")
# 2026-05-14: default flipped from 0 (PNG 8-bit) to 1 (PNG 16-bit). 8-bit
# quantizes alpha to 256 levels which destroys soft hair gradient detail.
# Berto's chunky-hair-edges complaint on yellow-backdrop test 2026-05-14
# was partly this. 16-bit = 65k alpha levels = soft hair edges restored.
try: items["OutputCodec"].CurrentIndex = 1  # default PNG 16-bit
except Exception: pass
# OutputContent: which file(s) to write. 2026-05-21: REVERTED to "Combined (CK x SAM)"
# as default. The 5/20 flip to "CK only" was an overnight fix attempt that turned out
# WORSE — raw CK alpha alone leaves dark greenscreen folds (upper-right corner of
# typical setups) un-killed because chroma threshold can't catch near-black pixels.
# The merge stack (SAM silhouette × CK alpha) bounds the keep-zone and kills those
# folds correctly — that's the user-visible quality CK had at 2026-05-21 00:49.
items["OutputContent"].AddItem("Combined (CK x SAM)")
items["OutputContent"].AddItem("Both (CK + SAM sidecar)")
items["OutputContent"].AddItem("CK only")
items["OutputContent"].AddItem("SAM matte only")
try: items["OutputContent"].CurrentIndex = 0  # default to "Combined (CK x SAM)"
except Exception: pass
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
    # Cross-thread Fusion widget mutation deadlocks on the UI mutex after the
    # first call. Worker threads must route through _ui_queue, drained by
    # on_poll_timer on the main thread. File I/O also skipped — Defender can
    # block thread file opens (see _tlog comment in _run).
    if threading.get_ident() != _main_thread_id:
        try: _ui_queue.put(("log", msg))
        except Exception: pass
        return
    try: print(msg)  # sys.stdout is None in Resolve background threads — must guard
    except Exception: pass
    try: items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"
    except Exception: pass
    try:
        with open(_ck_debug_log, "a", encoding="utf-8") as f: f.write(msg + "\n")
    except Exception: pass

# WHAT IT DOES: Updates the cyan status label at the center of the panel
def status(msg):
    if threading.get_ident() != _main_thread_id:
        try: _ui_queue.put(("status", msg))
        except Exception: pass
        return
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
        # 2026-05-15: despeckle default flipped to False. The engine-internal
        # despeckle at inference_engine.py:278/376 has area_threshold=400 +
        # dilation=25 which deletes fine flyaway hair-strand connected
        # components before the matte leaves the engine. v1.0.1-stable did
        # not have this internal despeckle. Berto can re-enable per-clip
        # via the viewer's DESPECKLE checkbox if a specific shot needs it.
        "despeckle_enabled": False,
        "despeckle_size": 400,      # viewer-owned; overridden by _merge_live_params
        "export_format": items["ExportFormat"].CurrentIndex,
        # 0 = Combined CK x SAM, 1 = Both, 2 = CK only, 3 = SAM only
        "output_content": items["OutputContent"].CurrentIndex,
        # v1.0 codec selector: 0=PNG8 / 1=PNG16 / 2=TIFF16 / 3=EXR32. Default
        # PNG8 keeps current renders byte-identical for users who don't change it.
        "output_codec": items["OutputCodec"].CurrentIndex,
        "output_mode": items["OutputMode"].CurrentIndex,
        # margin/soften sliders moved to viewer; defaults of 0 are overridden by
        # _merge_live_params() which pulls the actual values from live_params.json
        "sam2_margin": 0.0,
        "sam2_soften": 0.0,
        # HALO FEET: SAM2 gate dilation in non-green zones (px). Viewer-owned
        # default 0 = bit-identical to no-halo behavior; overridden by
        # _merge_live_params.
        "halo_px": 0,
        # HALO BODY: SAM2 gate dilation in green-bordered zones (px). May 1
        # TWO HALO design — independent of halo_px so a body buffer can be set
        # without growing feet into the floor. Default 0 = bit-identical.
        "halo_body_px": 0,
        # TRIM SAM2: chroma-aware mask refinement (0-100). Viewer-owned default
        # 0 = bit-identical to no-trim behavior; overridden by _merge_live_params.
        "trim_chroma": 0,
        # FILL HOLES: color-aware interior alpha-zero fill (0-100). Viewer-owned
        # default 0 = bit-identical to no-fill behavior; overridden by
        # _merge_live_params. >0 fills alpha=0 holes inside the SAM2 gate at
        # non-screen-color pixels — rescues NN dropouts on yellow shirts/skin.
        "fill_holes": 0,
        # FG SOURCE: "nn" (default = model FG, original behavior) | "source"
        # (use the original plate inside the matte — Mocha-style; rescues warm
        # wardrobe like yellow shirts that the NN paints pink) | "blend" (50/50).
        # Viewer-owned; overridden by _merge_live_params.
        "fg_source": "source",
        # SAM2 ADDITIVE: when True, switches the NN+SAM2 combine math from
        # multiplicative (alpha = NN x SAM2_gate, default) to additive
        # (alpha = max(NN, SAM2_gate * non_screen)). Preserves NN's correct
        # alpha across visual boundaries SAM2 misses (e.g. a stunt-rig strap
        # crossing the actor). Default False = bit-identical to prior render.
        # Viewer-owned; overridden by _merge_live_params.
        "sam2_additive": False,
        # SAM2 SMART BLEND: when True, per-pixel blends NN and SAM2 by
        # green-presence (chroma-derived weight). NN trusted where green
        # exists (preserves hair / butt-across-strap), SAM2 trusted off-green
        # (kills floor / props NN can't see). Wins over sam2_additive when
        # both are checked. Default False = bit-identical to prior render.
        # Viewer-owned; overridden by _merge_live_params.
        "sam2_weighted": False,
        # SAM2 SUBTRACT: alpha * dilated_SAM2_silhouette. Industry-standard
        # garbage-matte combine. NN owns matte values; SAM2 owns spatial
        # bounds; everything outside dilated silhouette killed.
        "sam2_subtract": False,
        # EDGE GUARD: isotropic dilation pixels around SAM2 silhouette.
        # Higher = recovers hair / butt curve SAM2 cut tight. Default 20.
        # In multi-mask mode, applies to MASK 1 (body / on-green parts).
        "edge_guard_px": 20,
        # FEET GUARD: per-mask margin override for MASK 2 (off-green parts —
        # feet on floor, body against wall). Smaller default so the keep-zone
        # doesn't pillow into non-green junk. Single-mask sessions ignore.
        "feet_guard_px": 5,
        # FEET SOFTEN: MASK 2-only Gaussian sigma. Softens the feet edge
        # without affecting the body. Default 0 = off.
        "feet_soften": 0.0,
        # SAM2 BYPASS: master switch — when True, all SAM2 paths skipped.
        "sam2_bypass": False,
        # MASK 1/2 BYPASS: per-mask isolation, mirrors viewer.
        "mask1_bypass": False,
        "mask2_bypass": False,
        # 2026-05-19 GARBAGE MATTE: third sidecar derived from SAM silhouette.
        # garbage_expand_px (0-200): cv2.dilate radius from raw SAM silhouette.
        # garbage_feather_px (0-30):  cv2.GaussianBlur sigma after dilate.
        # garbage_bypass: when True, skip computation and sidecar export.
        "garbage_expand_px": 0,
        "garbage_feather_px": 0,
        "garbage_bypass": False,
        "garbage_y_top_pct": 0,
        "garbage_y_bot_pct": 100,
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
        # Multi-object v0.8 — rename any legacy single-mask PNGs to MASK 1
        # namespace before any code reads them. Idempotent + silent on error.
        try:
            from sam2_combine import migrate_legacy_sam_pngs as _migrate_pngs
            _migrate_pngs(SESSION_DIR)
        except Exception:
            pass
        lp_path = SESSION_DIR / "live_params.json"
        if not lp_path.exists():
            return settings
        with open(lp_path, "r", encoding="utf-8") as f:
            lp = json.load(f)
        # Translate legacy sam_positive/sam_negative/sam_anchor_frame into MASK 1
        # keys so multi-object readers find values for old sessions.
        try:
            from sam2_combine import migrate_legacy_sam_keys as _migrate_keys
            lp = _migrate_keys(lp)
        except Exception:
            pass
        out = dict(settings)
        if "despill" in lp:
            try: out["despill_strength"] = max(0.0, min(1.0, float(lp["despill"])))
            except (ValueError, TypeError): pass
        if "despeckle" in lp:
            # 2026-05-15: viewer's DESPECKLE checkbox is the user-facing toggle.
            # If the user explicitly checks it on, respect that. If it's off
            # (default or user-unchecked), keep it off — engine despeckle at
            # area_threshold=400 + dilation=25 deletes fine flyaway hair.
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
        if "halo_px" in lp:
            # HALO FEET supports negative values (shrink silhouette from bottom).
            # Clamp to slider range -100..+150.
            try: out["halo_px"] = max(-100, min(150, int(lp["halo_px"])))
            except (ValueError, TypeError): pass
        if "halo_body_px" in lp:
            # HALO BODY positive only (extends silhouette upward). 0..300.
            try: out["halo_body_px"] = max(0, min(300, int(lp["halo_body_px"])))
            except (ValueError, TypeError): pass
        if "trim_chroma" in lp:
            try: out["trim_chroma"] = max(0, min(100, int(lp["trim_chroma"])))
            except (ValueError, TypeError): pass
        if "fill_holes" in lp:
            try: out["fill_holes"] = max(0, min(100, int(lp["fill_holes"])))
            except (ValueError, TypeError): pass
        if "fg_source" in lp:
            _v = str(lp["fg_source"]).lower()
            if _v in ("nn", "source", "blend"):
                out["fg_source"] = _v
        if "sam2_additive" in lp:
            # Accept native bool, "true"/"false" strings (case-insensitive),
            # and 0/1 numerics. Anything else falls through to default False.
            _av = lp["sam2_additive"]
            if isinstance(_av, bool):
                out["sam2_additive"] = _av
            elif isinstance(_av, str):
                out["sam2_additive"] = _av.strip().lower() in ("true", "1", "yes", "on")
            else:
                try:
                    out["sam2_additive"] = bool(int(_av))
                except (ValueError, TypeError):
                    pass
        if "sam2_weighted" in lp:
            # Mirrors sam2_additive coercion — accept native bool, string,
            # or 0/1 numeric. Defaults False if anything else.
            _wv = lp["sam2_weighted"]
            if isinstance(_wv, bool):
                out["sam2_weighted"] = _wv
            elif isinstance(_wv, str):
                out["sam2_weighted"] = _wv.strip().lower() in ("true", "1", "yes", "on")
            else:
                try:
                    out["sam2_weighted"] = bool(int(_wv))
                except (ValueError, TypeError):
                    pass
        if "sam2_subtract" in lp:
            _sv = lp["sam2_subtract"]
            if isinstance(_sv, bool):
                out["sam2_subtract"] = _sv
            elif isinstance(_sv, str):
                out["sam2_subtract"] = _sv.strip().lower() in ("true", "1", "yes", "on")
            else:
                try:
                    out["sam2_subtract"] = bool(int(_sv))
                except (ValueError, TypeError):
                    pass
        if "edge_guard_px" in lp:
            try: out["edge_guard_px"] = max(0, min(60, int(lp["edge_guard_px"])))
            except: pass
        if "feet_guard_px" in lp:
            try: out["feet_guard_px"] = max(0, min(60, int(lp["feet_guard_px"])))
            except (ValueError, TypeError): pass
        if "feet_soften" in lp:
            try: out["feet_soften"] = max(0.0, min(20.0, float(lp["feet_soften"])))
            except (ValueError, TypeError): pass
        if "sam2_bypass" in lp:
            _bv = lp["sam2_bypass"]
            if isinstance(_bv, bool):
                out["sam2_bypass"] = _bv
            elif isinstance(_bv, str):
                out["sam2_bypass"] = _bv.strip().lower() in ("true", "1", "yes", "on")
            else:
                try:
                    out["sam2_bypass"] = bool(int(_bv))
                except (ValueError, TypeError):
                    pass
        for _bk in ("mask1_bypass", "mask2_bypass"):
            if _bk in lp:
                _bv = lp[_bk]
                if isinstance(_bv, bool):
                    out[_bk] = _bv
                elif isinstance(_bv, str):
                    out[_bk] = _bv.strip().lower() in ("true", "1", "yes", "on")
                else:
                    try:
                        out[_bk] = bool(int(_bv))
                    except (ValueError, TypeError):
                        pass
        # 2026-05-19 GARBAGE MATTE — three settings keys.
        if "garbage_expand_px" in lp:
            try: out["garbage_expand_px"] = max(0, min(200, int(lp["garbage_expand_px"])))
            except (ValueError, TypeError): pass
        if "garbage_feather_px" in lp:
            try: out["garbage_feather_px"] = max(0, min(30, int(lp["garbage_feather_px"])))
            except (ValueError, TypeError): pass
        if "garbage_bypass" in lp:
            _gbv = lp["garbage_bypass"]
            if isinstance(_gbv, bool):
                out["garbage_bypass"] = _gbv
            elif isinstance(_gbv, str):
                out["garbage_bypass"] = _gbv.strip().lower() in ("true", "1", "yes", "on")
            else:
                try:
                    out["garbage_bypass"] = bool(int(_gbv))
                except (ValueError, TypeError):
                    pass
        if "garbage_y_top_pct" in lp:
            try: out["garbage_y_top_pct"] = max(0, min(100, int(lp["garbage_y_top_pct"])))
            except (ValueError, TypeError): pass
        if "garbage_y_bot_pct" in lp:
            try: out["garbage_y_bot_pct"] = max(0, min(100, int(lp["garbage_y_bot_pct"])))
            except (ValueError, TypeError): pass
        if "sam_positive" in lp or "sam_negative" in lp:
            sam_points["positive"] = [tuple(p) for p in lp.get("sam_positive", [])]
            sam_points["negative"] = [tuple(p) for p in lp.get("sam_negative", [])]
            sam_points["frame"]    = lp.get("sam_anchor_frame", None)
            # Auto-enable SAM2 mode whenever positive points exist — panel dropdown
            # does not need to be set to SAM2; the viewer places points = intent to use SAM2.
            if lp.get("alpha_method") == 1 or sam_points["positive"]:
                out["alpha_method"] = 1
        # Multi-object v0.8 — also load per-mask click data so video propagation
        # can register obj_id=1 and obj_id=2 separately. Falls back to empty
        # lists when keys aren't present (single-mask sessions still go through
        # the legacy sam_points union above).
        for _oid in (1, 2):
            sam_points_per_obj[_oid]["positive"] = [tuple(p) for p in lp.get(f"sam_positive_obj{_oid}", []) or []]
            sam_points_per_obj[_oid]["negative"] = [tuple(p) for p in lp.get(f"sam_negative_obj{_oid}", []) or []]
            sam_points_per_obj[_oid]["frame"]    = lp.get(f"sam_anchor_frame_obj{_oid}", None)
        if any(sam_points_per_obj[oid]["positive"] for oid in (1, 2)):
            out["alpha_method"] = 1
        # 2026-05-21: RESTORED sam2_mask.png auto-load. If a saved SAM2 mask exists
        # on disk for this session, activate SAM2 mode automatically — this is what
        # made CK output clean at 2026-05-21 00:49 (only 2 dots survived). The 5/20
        # removal of this auto-load was intended to prevent stale-PNG cross-session
        # contamination but the cost (raw CK alpha showing dark greenscreen folds)
        # is worse than the disease. If user switches clips and the PNG is stale,
        # they can clear via the panel.
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


# ===== Multi-object SAM2 helpers (v0.8) =====

# WHAT IT DOES: Runs the SAM2 + NN combine for ONE per-mask gate using whatever
#   mode the user has toggled (subtract / weighted / additive / trimap default).
#   Caller invokes this per-mask and unions the resulting alphas. Per-mask is
#   the correct contract for HALO operations: apply_sam2_gate uses the gate's
#   own bbox internally so each mask's halo zones stay confined to its region.
# DEPENDS-ON: sam2_combine.{apply_sam2_gate,_additive,_weighted,_subtract,
#   trim_gate_by_chroma,fill_holes_color_aware}; cv2; numpy.
# AFFECTS: returns a fresh alpha array; inputs unchanged.
def _panel_combine_one_mask(alpha, gate, src_rgb, settings):
    import cv2, numpy as np
    from sam2_combine import (
        apply_sam2_gate, apply_sam2_gate_additive, apply_sam2_gate_weighted,
        apply_sam2_gate_subtract, trim_gate_by_chroma, fill_holes_color_aware,
    )
    sam2_subtract = bool(settings.get("sam2_subtract", False))
    sam2_weighted = bool(settings.get("sam2_weighted", False))
    sam2_additive = bool(settings.get("sam2_additive", False))
    halo_px = int(settings.get("halo_px", 0))
    halo_body_px = int(settings.get("halo_body_px", 0))
    edge_guard_px = int(settings.get("edge_guard_px", 20))
    trim_chroma = int(settings.get("trim_chroma", 0))
    fill_holes = int(settings.get("fill_holes", 0))
    stype = str(settings.get("screen_type", "green"))
    a2d = alpha[:, :, 0] if alpha.ndim == 3 else alpha
    if sam2_subtract:
        fp = max(int(edge_guard_px) // 2, 1)
        _di = settings.get("_dilate_into", None)
        out = apply_sam2_gate_subtract(a2d, gate, src_rgb,
                                       screen_type=stype,
                                       buffer_px=int(edge_guard_px),
                                       feather_px=fp,
                                       halo_px=int(halo_px),
                                       dilate_into=_di)
        # FILL HOLES post-pass — fills NN dropouts in harness/clothing inside
        # the keep-zone for non-green pixels. Mirrors viewer's _combine_one_mask.
        if fill_holes > 0 and src_rgb is not None:
            _bin = (gate > 0.5).astype(np.uint8)
            _bp = max(int(edge_guard_px), 0)
            if _bp > 0:
                _kk = cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE, (2 * _bp + 1, 2 * _bp + 1))
                _bin = cv2.dilate(_bin, _kk)
            out = fill_holes_color_aware(out, _bin.astype(np.float32),
                                         src_rgb, stype, fill_holes)
        return out
    if sam2_weighted:
        return apply_sam2_gate_weighted(a2d, gate, src_rgb, screen_type=stype)
    if sam2_additive:
        gate_a = gate
        if halo_px and halo_px > 0:
            _bin = (gate_a > 0.5).astype(np.uint8)
            _k = int(halo_px) * 2 + 1
            _kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_k, _k))
            gate_a = cv2.dilate(_bin, _kernel).astype(np.float32)
        return apply_sam2_gate_additive(a2d, gate_a, src_rgb, screen_type=stype)
    # Default trimap mode
    g = gate
    if trim_chroma > 0 and src_rgb is not None:
        g = trim_gate_by_chroma(g, src_rgb, stype, trim_chroma)
    out = apply_sam2_gate(a2d, g, invert=False, halo_px=halo_px, halo_body_px=halo_body_px)
    if fill_holes > 0 and src_rgb is not None:
        out = fill_holes_color_aware(out, g, src_rgb, stype, fill_holes)
    return out


# WHAT IT DOES: Path B CK+SAM merge per Berto 2026-05-05 — CK preserved;
#   each per-mask SAM gate binarised at 0.5, gates unioned, then OR-blended
#   into CK so missing off-green regions are filled WITHOUT overriding CK
#   detail. Algebraic form: final = clip(CK + clip(SAM_union - CK, 0, 1), 0, 1).
#   Per-mask BYPASS toggles preserved (user-facing UI for disabling a mask).
#   Mode flags (sam2_subtract / _weighted / _additive) and HALO / EDGE GUARD /
#   FEET GUARD / TRIM SAM2 / FILL HOLES sliders are NO-OPS under Path B —
#   _panel_combine_one_mask() above and apply_sam2_gate_* in sam2_combine.py
#   are now dead code, kept on disk for hot-revert per Berto's instruction.
# DEPENDS-ON: corridorkey_sam_merge.{binarize_sam_silhouette,
#   union_binary_silhouettes, merge_ck_with_sam_active}. The active dispatcher
#   routes to chroma-gated merge when USE_CHROMA_GATED_MERGE flag is True and
#   src_rgb is provided; falls back to Path B (chroma-blind max) otherwise.
# AFFECTS: returns a fresh alpha array; inputs unchanged. Returns alpha
#   when gates list is empty or all masks bypassed (NN-only fallback).
def _panel_dispatch_sam2_combine(alpha, gates, src_rgb, settings, obj_ids=None):
    if not gates:
        return alpha
    from corridorkey_sam_merge import (
        binarize_sam_silhouette, union_binary_silhouettes, merge_ck_with_sam_active,
    )
    import numpy as np
    _m1b = bool(settings.get("mask1_bypass", False))
    _m2b = bool(settings.get("mask2_bypass", False))
    _subtract = bool(settings.get("sam2_subtract", False))
    # 2026-05-19: EDGE GUARD slider drives SAM proximity (body-edge rescue).
    # Range 0-30, default 7. Low = tight feet, high = generous edge rescue.
    _prox_px = int(settings.get("edge_guard_px", 7))
    # 2026-05-19 GARBAGE MATTE in-render multiply (applied to merged alpha
    # at the end of dispatch). When bypass is False, the garbage matte
    # (dilated SAM + feather + Y-crop) multiplies onto the final alpha,
    # so the in-app slider controls what ships to CK_*.png directly. No
    # DaVinci roundtrip needed when the user wants a holdout on a region.
    _gm_bypass = bool(settings.get("garbage_bypass", False))
    _gm_expand_px = int(settings.get("garbage_expand_px", 0))
    _gm_feather_px = int(settings.get("garbage_feather_px", 0))
    _gm_y_top_pct = int(settings.get("garbage_y_top_pct", 0))
    _gm_y_bot_pct = int(settings.get("garbage_y_bot_pct", 100))
    _gm_active = (not _gm_bypass) and (
        _gm_expand_px > 0 or _gm_y_top_pct > 0 or _gm_y_bot_pct < 100
    )
    # 2026-05-17 — SUBTRACT wired. When SUBTRACT is enabled, MASK 2 acts as a
    # kill mask (subtractive). MASK 1 goes through the merge as usual; MASK 2
    # is then multiplied as (1 - mask2) onto the result, removing whatever
    # region MASK 2 selected. This matches the "INVERT MASK / garbage matte"
    # feature from past commits (f0294c2d). When SUBTRACT is off, MASK 1 and
    # MASK 2 union as before.
    mask1_silhouettes = []
    mask2_silhouettes = []
    for i, gate in enumerate(gates):
        if gate is None:
            continue
        oid = obj_ids[i] if (obj_ids is not None and i < len(obj_ids)) else None
        if oid == 1 and _m1b:
            continue
        if oid == 2 and _m2b:
            continue
        _sil = binarize_sam_silhouette(gate)
        if oid == 2:
            mask2_silhouettes.append(_sil)
        else:
            mask1_silhouettes.append(_sil)
    if _subtract and mask2_silhouettes:
        # SUBTRACT path. MASK 1 (if present) goes through merge; then MASK 2
        # is applied as a kill multiplier — EXCEPT inside the eroded body core,
        # which is strictly preserved (prevents MASK 2 from accidentally
        # clipping body interior when SAM segmentation overlaps body).
        # 2026-05-18 — TWO architectural fixes from Codex + multi-AI consensus:
        #   1. Chroma-boundary CC filter on MASK 1 SAM mask before merge.
        #      Prevents "SAM grows into floor" when MASK 1 positives sit near
        #      the green/carpet boundary (e.g., on feet). Cuts SAM mask at
        #      strong-green pixels and keeps only components containing the
        #      MASK 1 positive prompts.
        #   2. Body-core override for SUBTRACT. body_core = erode(SAM, 30px).
        #      Inside body_core: SUBTRACT cannot touch (SAM is truth).
        #      Outside body_core (including body_topology's 40px ring):
        #      SUBTRACT kills freely. Solves the "chunky platform between feet
        #      survives because body_topology preserves it" failure mode.
        _sam_union_m1 = None
        _body_core_2d = None
        if mask1_silhouettes:
            _sam_union_m1 = union_binary_silhouettes(mask1_silhouettes)
            # FIX 1 — chroma-boundary CC filter on MASK 1
            try:
                import cv2 as _cv2_cc
                import json as _json_cc
                from pathlib import Path as _P_cc
                _lp_path_cc = _P_cc(tempfile.gettempdir()) / "corridorkey_session" / "live_params.json"
                _m1_pos = []
                if _lp_path_cc.exists():
                    with open(_lp_path_cc, "r", encoding="utf-8") as _f_cc:
                        _lp_cc = _json_cc.load(_f_cc)
                    _m1_pos = _lp_cc.get("sam_positive_obj1", [])
                if _m1_pos and src_rgb is not None:
                    _rgb_cc = src_rgb.astype(np.float32)
                    if _rgb_cc.max() > 1.5:
                        _rgb_cc = _rgb_cc / 255.0
                    # Conservative green threshold — strong green only
                    _chroma_cc = _rgb_cc[..., 1] - np.maximum(_rgb_cc[..., 0], _rgb_cc[..., 2])
                    _green_strong = (_chroma_cc > 0.15).astype(np.uint8)
                    _sam_bin_cc = (_sam_union_m1 > 0.5).astype(np.uint8)
                    _sam_cut = _sam_bin_cc * (1 - _green_strong)
                    _n_lbl, _labels = _cv2_cc.connectedComponents(_sam_cut, connectivity=8)
                    _keep = set()
                    _H_cc, _W_cc = _labels.shape
                    for _pt in _m1_pos:
                        _px, _py = int(_pt[0]), int(_pt[1])
                        if 0 <= _py < _H_cc and 0 <= _px < _W_cc:
                            _lbl_at = int(_labels[_py, _px])
                            if _lbl_at > 0:
                                _keep.add(_lbl_at)
                    if _keep:
                        _constrained = np.zeros_like(_sam_bin_cc, dtype=np.float32)
                        for _lbl in _keep:
                            _constrained[_labels == _lbl] = 1.0
                        # Preserve soft edges from original SAM mask where kept
                        if _sam_union_m1.dtype != np.float32:
                            _sam_f32 = _sam_union_m1.astype(np.float32)
                        else:
                            _sam_f32 = _sam_union_m1
                        _sam_union_m1 = (_sam_f32 * _constrained).astype(_sam_union_m1.dtype)
            except Exception:
                pass
            merged = merge_ck_with_sam_active(alpha, _sam_union_m1, source_rgb=src_rgb, proximity_px=_prox_px)
            # FIX 2 — compute body_core for SUBTRACT override
            try:
                import cv2 as _cv2_bc
                _k_body_core = _cv2_bc.getStructuringElement(_cv2_bc.MORPH_ELLIPSE, (61, 61))
                _k_body_core[:30, :] = 0
                _sam_bin_bc = (_sam_union_m1 > 0.5).astype(np.uint8)
                if _sam_bin_bc.ndim == 3:
                    _sam_bin_bc = _sam_bin_bc[..., 0]
                _body_core_2d = _cv2_bc.erode(_sam_bin_bc, _k_body_core).astype(np.float32)
            except Exception:
                _body_core_2d = None
        else:
            merged = alpha
        mask2_union = union_binary_silhouettes(mask2_silhouettes)
        _kill = mask2_union.astype(np.float32)
        if _kill.ndim == 3 and _kill.shape[2] == 1:
            _kill = _kill[..., 0]
        # Apply body-core override: SUBTRACT cannot kill inside body_core
        if _body_core_2d is not None and _body_core_2d.shape == _kill.shape:
            _kill = _kill * (1.0 - _body_core_2d)
        if merged.shape[:2] == _kill.shape[:2]:
            _inv = (1.0 - _kill).astype(np.float32)
            if merged.ndim == 3:
                merged = (merged.astype(np.float32) * _inv[..., None]).astype(merged.dtype)
            else:
                merged = (merged.astype(np.float32) * _inv).astype(merged.dtype)
        merged = _apply_edge_feather(merged, settings)
        # 2026-05-23 shirt rescue — pull yellow/red shirt back from CK-NN
        # over-keying by deferring to SAM in non-green regions. Runs before
        # garbage matte so the matte still gates the rescued pixels.
        merged = _apply_shirt_rescue(merged, _sam_union_m1 if mask1_silhouettes else None, src_rgb)
        merged = _apply_garbage_matte(merged, _sam_union_m1 if mask1_silhouettes else None,
                                       _gm_active, _gm_expand_px, _gm_feather_px,
                                       _gm_y_top_pct, _gm_y_bot_pct)
        return merged
    # Union path (legacy / default)
    active_silhouettes = mask1_silhouettes + mask2_silhouettes
    if not active_silhouettes:
        return _apply_edge_feather(alpha, settings)
    sam_union = union_binary_silhouettes(active_silhouettes)
    _merged = _apply_edge_feather(
        merge_ck_with_sam_active(alpha, sam_union, source_rgb=src_rgb, proximity_px=_prox_px),
        settings,
    )
    # 2026-05-23 shirt rescue (union path) — same fix as SUBTRACT branch above.
    _merged = _apply_shirt_rescue(_merged, sam_union, src_rgb)
    return _apply_garbage_matte(_merged, sam_union, _gm_active, _gm_expand_px,
                                _gm_feather_px, _gm_y_top_pct, _gm_y_bot_pct)


# WHAT IT DOES: Per-pixel rescue for body content that CK NN over-keyed because
#   the foreground color (yellow/red shirt, dark prop) reads as green to the NN.
#   Where the SOURCE pixel is NOT green (chroma score < threshold) AND SAM says
#   body, force alpha to max(alpha, SAM). On genuine green pixels, no change —
#   CK's keying is preserved. Implements the "use SAM where green is absent"
#   half of the Path B KeyMix architecture as a post-merge override.
# DEPENDS-ON: numpy. Inputs are float32 [0..1] or uint8 [0..255].
# AFFECTS: returns a fresh alpha array of the same shape/dtype as input.
def _apply_shirt_rescue(alpha, sam, src_rgb, threshold=0.15, sam_threshold=0.85, erode_px=5):
    if sam is None or src_rgb is None or alpha is None:
        return alpha
    try:
        import numpy as _np
        import cv2 as _cv2
        _rgb = _np.asarray(src_rgb).astype(_np.float32)
        if _rgb.max() > 1.5:
            _rgb = _rgb / 255.0
        # green chroma score: positive where green dominates
        _green = _rgb[..., 1] - _np.maximum(_rgb[..., 0], _rgb[..., 2])
        _not_green = _green < float(threshold)
        _sam_arr = _np.asarray(sam).astype(_np.float32)
        if _sam_arr.ndim == 3:
            _sam_arr = _sam_arr[..., 0]
        if _sam_arr.max() > 1.5:
            _sam_arr = _sam_arr / 255.0
        # 2026-05-23: SAM2-video propagation outputs looser edges than single-frame
        # SAM. Tighten with high threshold + erode to bring rescue zone closer to
        # actual body edge. Kills the 10px halo seen in range Combined output.
        _sam_bin = (_sam_arr > float(sam_threshold)).astype(_np.uint8)
        if erode_px > 0:
            _k = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (erode_px * 2 + 1, erode_px * 2 + 1))
            _sam_bin = _cv2.erode(_sam_bin, _k)
        # Match shape — if alpha is HxW and SAM is HxW, both 2D
        _a = _np.asarray(alpha).astype(_np.float32)
        if _a.ndim == 3:
            _a2d = _a[..., 0]
        else:
            _a2d = _a
        if _a.max() > 1.5:
            _a2d = _a2d / 255.0
            _scale = 255.0
        else:
            _scale = 1.0
        if _sam_bin.shape != _a2d.shape or _not_green.shape != _a2d.shape:
            return alpha  # shape mismatch — bail safely
        _rescue = _not_green & (_sam_bin > 0)
        _out = _a2d.copy()
        _out[_rescue] = _np.maximum(_a2d[_rescue], _sam_bin[_rescue].astype(_np.float32))
        _out = (_out * _scale).clip(0, _scale)
        if alpha.ndim == 3:
            _out_full = _np.repeat(_out[..., None], alpha.shape[2], axis=2)
            return _out_full.astype(alpha.dtype)
        return _out.astype(alpha.dtype)
    except Exception as _se:
        log(f"shirt rescue failed (non-fatal): {_se}")
        return alpha


def _apply_garbage_matte(alpha, sam, active, expand_px, feather_px, y_top_pct, y_bot_pct):
    """2026-05-19 — multiply garbage matte (dilated SAM + feather + Y crop)
    onto the final alpha. When not active, returns alpha unchanged."""
    if not active or sam is None or alpha is None:
        return alpha
    try:
        from corridorkey_sam_merge import compute_garbage_matte as _cgm
        import numpy as _np
        _gm = _cgm(sam, expand_px=expand_px, feather_px=feather_px,
                   y_top_pct=y_top_pct, y_bot_pct=y_bot_pct)
        if _gm is None or _gm.shape[:2] != alpha.shape[:2]:
            return alpha
        if alpha.ndim == 3:
            return (alpha.astype(_np.float32) * _gm[..., None]).astype(alpha.dtype)
        return (alpha.astype(_np.float32) * _gm).astype(alpha.dtype)
    except Exception:
        return alpha


def _apply_edge_feather(alpha, settings):
    # 2026-05-17 — EDGE FEATHER. Reads sam2_soften from settings (repurpose
    # of existing SOFTEN slider) and applies a Gaussian blur on the FINAL
    # alpha — softens the matte's outer boundary so the FG-to-BG transition
    # has a smooth gradient. Per Berto: "between the mask and the green need
    # a 10 pixel buffer that diffuses into green."
    # 0 = no feather (default). Sigma = feather_px / 3 (3-sigma rule).
    try:
        _fp = float(settings.get("sam2_soften", 0.0))
    except Exception:
        _fp = 0.0
    if _fp <= 0.0 or alpha is None:
        return alpha
    import cv2 as _cv2_ef
    import numpy as _np_ef
    _sigma = max(0.1, _fp / 3.0)
    _ksize = max(3, int(round(_fp * 2 + 1)))
    if _ksize % 2 == 0:
        _ksize += 1
    _a32 = alpha.astype(_np_ef.float32)
    _blurred = _cv2_ef.GaussianBlur(_a32, (_ksize, _ksize), _sigma)
    return _np_ef.clip(_blurred, 0.0, 1.0)


# WHAT IT DOES: Reads per-object SAM2 silhouette PNGs (sam2_mask_obj1.png and
#   sam2_mask_obj2.png) from SESSION_DIR and returns them as a dict[obj_id,
#   float32 mask] at the requested shape, dilated and softened per the user's
#   MARGIN / SOFTEN sliders. Falls back to legacy single sam2_mask.png mapped
#   to obj_id=1 when no per-object files exist.
# DEPENDS-ON: SESSION_DIR/sam2_mask_obj{N}.png written by viewer's _apply_sam_mask.
# AFFECTS: pure read; returns a NEW dict of arrays. obj_id is the dict key
#   so callers (Option C halo binder) know which mask is which.
def _load_per_object_sam2_gates(frame_shape, settings):
    import numpy as np, cv2
    if settings.get("alpha_method") != 1:
        return {}
    h, w = frame_shape[:2]
    pairs = [
        (1, SESSION_DIR / "sam2_mask_obj1.png"),
        (2, SESSION_DIR / "sam2_mask_obj2.png"),
    ]
    legacy = SESSION_DIR / "sam2_mask.png"
    if not any(p.exists() for _, p in pairs) and legacy.exists():
        pairs = [(1, legacy)]
    out = {}
    margin = settings.get("sam2_margin", SAM2_MATTE_MARGIN)
    soften = settings.get("sam2_soften", 0)
    for oid, p in pairs:
        if not p.exists():
            continue
        raw = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if raw is None:
            log(f"per-object gate: could not read {p.name}")
            continue
        _, raw = cv2.threshold(raw, 127, 255, cv2.THRESH_BINARY)
        if raw.shape != (h, w):
            raw = cv2.resize(raw, (w, h), interpolation=cv2.INTER_NEAREST)
        m = raw.astype(np.float32) / 255.0
        m = _dilate_sam2_mask(m, margin=margin)
        m = _soften_sam2_mask(m, soften=soften)
        out[oid] = m
    if out:
        log(f"SAM2 gates loaded — obj_ids={sorted(out.keys())}")
    return out


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
    # 2026-05-21 RESTORED 1-line bypass from deploy 003825 (the "golden" 00:49
    # quality state per Berto). The "smart despeckle" rewrite (CC + keep-zone
    # proximity) introduced 2026-05-21_022438 and tuned at 143325 was the named
    # cause of the "torn-flag artifact in upper corners" — Agent 1 bisect
    # confirmed. Bypass keeps the 2-month-stable behavior. If real despeckle
    # is needed later, re-enable as opt-in with a much smaller default size.
    return mt
    # Dead code below preserved for future re-enable / reference. Unreachable.
    if mt is None:
        return mt
    if not settings.get("despeckle_enabled", True):
        return mt
    area_threshold = int(settings.get("despeckle_size", 300))
    if area_threshold <= 0:
        return mt
    try:
        import cv2 as _cv2_sd
        import numpy as _np_sd
        mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
        # Normalize to float [0,1] for thresholding regardless of input dtype.
        if mt2d.dtype == _np_sd.uint8:
            a = mt2d.astype(_np_sd.float32) / 255.0
        elif mt2d.dtype == _np_sd.uint16:
            a = mt2d.astype(_np_sd.float32) / 65535.0
        else:
            a = mt2d.astype(_np_sd.float32)
        body_threshold = float(settings.get("despeckle_body_threshold", 0.5))
        proximity_px = int(settings.get("despeckle_proximity_px", 30))
        # 2026-05-21 REVERT: use HIGH threshold (0.5) for CC analysis — matches
        # the 2-month-stable clean_matte_opencv behavior. The "smart" 0.05
        # threshold pulled partial-alpha edges into the same component as
        # large junk, making the component pass area_threshold and persist
        # (torn-flag artifact in upper corners). 0.5 binarize drops those.
        low_threshold = 0.5
        # Body = largest CC of high-confidence alpha
        body_bin = (a > body_threshold).astype(_np_sd.uint8)
        n_body, labels_body, stats_body, _c = _cv2_sd.connectedComponentsWithStats(body_bin, connectivity=8)
        if n_body <= 1:
            return mt
        # CC_STAT_AREA index is 4 in OpenCV; argmax over labels 1..N-1
        body_areas = stats_body[1:, _cv2_sd.CC_STAT_AREA]
        largest_label = int(_np_sd.argmax(body_areas)) + 1
        body_mask = (labels_body == largest_label).astype(_np_sd.uint8) * 255
        # Keep-zone = body dilated by proximity_px (preserves hair tips, flyaways)
        if proximity_px > 0:
            k = 2 * proximity_px + 1
            kernel = _cv2_sd.getStructuringElement(_cv2_sd.MORPH_ELLIPSE, (k, k))
            keep_zone = _cv2_sd.dilate(body_mask, kernel)
        else:
            keep_zone = body_mask
        # CC on any-alpha (low threshold) — partial alpha hair gets its own component
        any_bin = (a > low_threshold).astype(_np_sd.uint8)
        n_any, labels_any, stats_any, _c2 = _cv2_sd.connectedComponentsWithStats(any_bin, connectivity=8)
        if n_any <= 1:
            return mt
        # Build keep mask
        keep_mask = _np_sd.zeros_like(a, dtype=_np_sd.float32)
        for i in range(1, n_any):
            comp_pixels = (labels_any == i)
            # Component touching keep-zone? Always keep (preserves hair near body).
            if keep_zone[comp_pixels].any():
                keep_mask[comp_pixels] = 1.0
                continue
            # Outside keep-zone: only keep if large enough (real objects)
            if stats_any[i, _cv2_sd.CC_STAT_AREA] >= area_threshold:
                keep_mask[comp_pixels] = 1.0
            # else: drop (isolated speck — pink reg mark, dust, etc)
        # Apply keep mask to original alpha
        result = (a * keep_mask)
        # Cast back to input dtype
        if mt2d.dtype == _np_sd.uint8:
            result_out = (result * 255.0).clip(0, 255).astype(_np_sd.uint8)
        elif mt2d.dtype == _np_sd.uint16:
            result_out = (result * 65535.0).clip(0, 65535).astype(_np_sd.uint16)
        else:
            result_out = result.astype(mt2d.dtype, copy=False)
        # Preserve original shape (re-add channel dim if input was 3D)
        if len(mt.shape) == 3:
            return result_out[:, :, _np_sd.newaxis]
        return result_out
    except Exception as _e:
        try:
            log(f"Smart despeckle skipped: {_e}")
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
    """Multi-object v0.8 — read per-object SAM2 PNGs and return their union.

    Falls back to legacy sam2_mask.png when no per-object files exist (the
    migration shim normally renames legacy files to obj1, so this path only
    fires for code that bypasses the shim). Per-mask render dispatch is the
    correctness path; this single-gate union is a transitional shim until
    the panel render sites are refactored per-mask too (commit 4).
    """
    import numpy as np, cv2
    if settings.get("alpha_method") != 1:
        log(f"SAM2 output gate: alpha_method={settings.get('alpha_method')} — gate skipped")
        return None
    h, w = frame_shape[:2]
    candidates = [
        SESSION_DIR / "sam2_mask_obj1.png",
        SESSION_DIR / "sam2_mask_obj2.png",
    ]
    legacy = SESSION_DIR / "sam2_mask.png"
    if not any(p.exists() for p in candidates) and legacy.exists():
        candidates = [legacy]
    found = []
    for p in candidates:
        if not p.exists():
            continue
        raw = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if raw is None:
            log(f"SAM2 output gate: could not read {p.name}")
            continue
        _, raw = cv2.threshold(raw, 127, 255, cv2.THRESH_BINARY)
        if raw.shape != (h, w):
            raw = cv2.resize(raw, (w, h), interpolation=cv2.INTER_NEAREST)
        found.append(raw.astype(np.float32) / 255.0)
    if not found:
        log("SAM2 output gate: no per-object PNG present — no garbage matte applied")
        return None
    # Per-pixel union — single-mask sessions take this path with one entry.
    mask = found[0]
    for m in found[1:]:
        mask = np.maximum(mask, m)
    mask = _dilate_sam2_mask(mask, margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
    mask = _soften_sam2_mask(mask, soften=settings.get("sam2_soften", 0))
    log(f"SAM2 output gate loaded — {len(found)} per-object PNG(s), "
        f"coverage {mask.mean():.3f} ({int(mask.sum())} px foreground)")
    return mask


def _load_raw_sam_silhouette(frame_shape, settings):
    """Load the RAW binary SAM silhouette (union of per-object PNGs) without
    dilation or soften. Returns float32 mask in {0.0, 1.0} or None when SAM
    is inactive / PNGs missing.

    Distinct from _load_sam2_output_gate above which applies dilate+soften
    before returning. The v2.2 chroma-gated merge in corridorkey_sam_merge.py
    needs the raw silhouette because it builds its own trimap and runs its
    own dilation (81-pixel ellipse) internally.
    """
    import numpy as np, cv2
    if settings.get("alpha_method") != 1:
        return None
    h, w = frame_shape[:2]
    candidates = [
        SESSION_DIR / "sam2_mask_obj1.png",
        SESSION_DIR / "sam2_mask_obj2.png",
    ]
    legacy = SESSION_DIR / "sam2_mask.png"
    if not any(p.exists() for p in candidates) and legacy.exists():
        candidates = [legacy]
    found = []
    for p in candidates:
        if not p.exists():
            continue
        raw = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if raw is None:
            continue
        _, raw = cv2.threshold(raw, 127, 255, cv2.THRESH_BINARY)
        if raw.shape != (h, w):
            raw = cv2.resize(raw, (w, h), interpolation=cv2.INTER_NEAREST)
        found.append(raw.astype(np.float32) / 255.0)
    if not found:
        return None
    mask = found[0]
    for m in found[1:]:
        mask = np.maximum(mask, m)
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
        # 2026-05-19: hiera_small -> hiera_base_plus. Higher capacity for
        # multi-texture stunt subjects (harness gear + multi-fabric clothing
        # + motion blur). Reduces Swiss-cheese internal holes, calf-level
        # SAM slits, knee-bend hole, finger/butt under-coverage.
        # +25% latency (~2.4s -> ~3s/frame), 323 MB checkpoint vs 176 MB.
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
def run_sam2_video_propagation(fp, ss, cs, in_f, out_f, pos_pts, neg_pts, anchor_frame_abs,
                               pos_pts_obj2=None, neg_pts_obj2=None, anchor_frame_obj2_abs=None):
    """Multi-object v0.8 — track up to two SAM2 objects through the range.

    obj1 = MASK 1 (always required). obj2 = MASK 2 (optional). When obj2 has
    points, both objects are registered on a single SAM2VideoPredictor via the
    native obj_id API and propagated together — one forward pass + one backward
    pass cover both. Cheaper than running the predictor twice.

    Returns dict[frame_idx, list[mask]]: per-frame list of float32 masks, one
    per active object. Consumers iterate over the list and union (or apply
    per-mask combine + union via _panel_dispatch_sam2_combine).
    """
    import cv2, numpy as np, torch, shutil, tempfile
    ckpt = str(CK_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
    cfg  = "configs/sam2.1/sam2.1_hiera_s.yaml"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dur = out_f - in_f

    # Map each object's absolute clicked frame to a range-relative index. If
    # the click was outside the range (or never recorded), anchor to frame 0.
    def _anchor_rel(frame_abs):
        if frame_abs is not None and in_f <= frame_abs < out_f:
            return frame_abs - in_f
        return 0
    anchor_rel = _anchor_rel(anchor_frame_abs)
    anchor_rel_obj2 = _anchor_rel(anchor_frame_obj2_abs)

    has_obj2 = bool(pos_pts_obj2) or bool(neg_pts_obj2)
    active_obj_ids = [1] + ([2] if has_obj2 else [])

    tmp_dir = Path(tempfile.mkdtemp(prefix="ck_sam2_frames_"))
    # 2026-05-09: Phase 0 (letterbox-pad to square + lossless PNG) was here
    # but broke the SCRUB path silently — only frame 0 was producing valid
    # masks. Reverted to JPEG q=95 + native shape for now. The image-
    # predictor (live-preview click-to-mask) keeps Phase 0a padding because
    # that's a separate code path in preview_viewer_v2.py and works fine.
    # The saturation ramp is still applied via logits_to_soft_mask below.
    _ramp = logits_to_soft_mask
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
            # CAP_FFMPEG: Windows MSMF (default) backend deadlocks when opened
            # from a daemon thread — IMFSourceReader needs an STA COM apartment
            # that Resolve's embedded Python doesn't init for worker threads.
            # FFMPEG backend has no COM dependency.
            _cap = cv2.VideoCapture(fp, cv2.CAP_FFMPEG)
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
        log(f"SAM2 video: loading predictor, anchor=range-frame {anchor_rel} (obj_ids={active_obj_ids})...")
        status("SAM2: loading video model...")
        from sam2.build_sam import build_sam2_video_predictor
        # vos_optimized=True needs a C compiler (Triton JIT) which most
        # Windows users don't have. Without it, propagation crashes and the
        # panel falls back to stamping the anchor mask on every frame. Run
        # uncompiled — slower but correct on a stock Windows install.
        predictor = build_sam2_video_predictor(cfg, ckpt, device=device)
        log("SAM2 video: vos_optimized=False (no Triton C compiler dep)")

        # Per-object click sets — labels (1=positive, 0=negative).
        # Source-space coords go straight in (no padding on the video path —
        # see Phase 0 revert note above).
        obj_pts = {
            1: ([[p[0], p[1]] for p in pos_pts] + [[p[0], p[1]] for p in neg_pts],
                [1] * len(pos_pts) + [0] * len(neg_pts),
                anchor_rel),
        }
        if has_obj2:
            obj_pts[2] = (
                [[p[0], p[1]] for p in (pos_pts_obj2 or [])] + [[p[0], p[1]] for p in (neg_pts_obj2 or [])],
                [1] * len(pos_pts_obj2 or []) + [0] * len(neg_pts_obj2 or []),
                anchor_rel_obj2,
            )

        # masks_per_obj[obj_id][frame_idx] = float32 mask. Combined per-frame
        # at the end of propagation.
        masks_per_obj = {oid: {} for oid in active_obj_ids}
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
            # Register each object's prompts. Native multi-object via obj_id
            # — one predictor, multiple trackers, single propagation pass.
            for oid in active_obj_ids:
                _pts, _labs, _anchor = obj_pts[oid]
                if not _pts:
                    continue
                predictor.add_new_points_or_box(
                    inference_state=state,
                    frame_idx=_anchor,
                    obj_id=oid,
                    points=np.array(_pts, dtype=np.float32),
                    labels=np.array(_labs, dtype=np.int32),
                    clear_old_points=True,
                )

            # --- Forward pass: anchor → last frame ---
            # DANGER ZONE FRAGILE: SAM2 propagate_in_video() is stateful — the backward
            # pass must reuse the same state object so the tracker memory from the forward
            # pass carries over. Reinitialising state between passes loses anchor context.
            # breaks: if state is reset between passes, backward masks drift from forward.
            #
            # Option C 2026-05-09: this used to hard-threshold mask_logits > 0 and
            # then run MORPH_CLOSE k=101 to bridge inter-dot confidence dips inside
            # the silhouette. Replaced with the saturation ramp the viewer uses —
            # interior pins to 1.0, edges get a 2-4 px feather.
            #
            # Phase 0a 2026-05-09: SAM 2 is now seeing letterbox-padded square
            # frames, so the logits come back at the padded square shape. Crop
            # the soft mask back to source frame shape before storing.

            def _store_propagated(frame_idx, _obj_ids_returned, mask_logits, direction):
                """Convert per-object logits to soft float masks and stash in masks_per_obj."""
                # mask_logits is shape [n_obj, 1, H, W]. _obj_ids_returned tells us
                # which obj_id each row corresponds to (in active_obj_ids order).
                # Frames are at native source shape on the video predictor path
                # (Phase 0 padding reverted 2026-05-09 due to SCRUB regression).
                for slot, oid in enumerate(_obj_ids_returned):
                    if oid not in masks_per_obj:
                        continue
                    if direction == "backward" and frame_idx in masks_per_obj[oid]:
                        # Forward pass already filled this frame for this obj — keep it.
                        continue
                    L = mask_logits[slot].squeeze().cpu().numpy()
                    m = _ramp(L)
                    if (m > 0.5).sum() < 100:
                        masks_per_obj[oid][frame_idx] = np.zeros_like(m, dtype=np.float32)
                    else:
                        masks_per_obj[oid][frame_idx] = m.astype(np.float32)

            status("SAM2: forward pass...")
            log(f"SAM2 video: forward pass (anchor={anchor_rel}, obj_ids={active_obj_ids} -> frame {dur-1})")
            forward_count = 0
            for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(state):
                _store_propagated(frame_idx, list(_obj_ids), mask_logits, "forward")
                forward_count += 1
                if frame_idx % 20 == 0:
                    log(f"SAM2 forward: frame {frame_idx}/{dur}")
                    status(f"SAM2 forward: {frame_idx}/{dur} frames")
            log(f"SAM2 forward pass done — {forward_count} frames covered")

            # --- Backward pass ---
            # Run if ANY object's anchor isn't frame 0 — single backward pass covers
            # frames before any object's anchor. Forward results are preserved per-obj.
            min_anchor = min(obj_pts[oid][2] for oid in active_obj_ids if obj_pts[oid][0])
            if min_anchor > 0:
                status("SAM2: backward pass...")
                log(f"SAM2 video: backward pass (min_anchor={min_anchor} -> frame 0)")
                backward_count = 0
                for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(
                        state, reverse=True):
                    _store_propagated(frame_idx, list(_obj_ids), mask_logits, "backward")
                    backward_count += 1
                    if frame_idx % 20 == 0:
                        log(f"SAM2 backward: frame {frame_idx}")
                        status(f"SAM2 backward: {frame_idx} frames")
                log(f"SAM2 backward pass done — {backward_count} frames visited")
            else:
                log("SAM2 video: every active object anchored at frame 0 — backward pass skipped")

            # Per-object post-pass: resolve interior empties (mid-range
            # tracking collapse → ones-mask, NN-only fallback) vs tail
            # empties (actor not in frame → hold the nearest substantial
            # mask so junk SAM was killing stays killed when the subject
            # leaves frame). Run the same logic per-object so MASK 1's
            # collapse doesn't get filled by MASK 2's healthy frames and
            # vice-versa.
            #
            # Tail-empty handling — Berto 2026-05-11: previously the tail
            # was left empty, so empty SAM × CK = CK alone on the last
            # frames, which let all the junk SAM was killing (foot mat,
            # crate edges, markers) come back. Holding the last substantial
            # mask forward keeps the junk killed since junk positions are
            # fixed in the frame. Note: in Combined (CK × SAM) output mode
            # everything outside the held mask is transparent. Shots that
            # need to preserve non-actor pixels on tail frames (mirror /
            # partial-green-screen cases) should use the CK-only output
            # mode instead.
            for oid, m_dict in masks_per_obj.items():
                sorted_keys = sorted(m_dict.keys())
                collapsed_count = 0
                tail_held_count = 0
                if sorted_keys:
                    first_substantial = next(
                        (f for f in sorted_keys if m_dict[f].sum() >= 100), None)
                    last_substantial = next(
                        (f for f in reversed(sorted_keys) if m_dict[f].sum() >= 100), None)
                    if first_substantial is not None and last_substantial is not None:
                        for f in sorted_keys:
                            if m_dict[f].sum() >= 100:
                                continue
                            if first_substantial <= f <= last_substantial:
                                m_dict[f] = np.ones_like(m_dict[f])
                                collapsed_count += 1
                            elif f > last_substantial:
                                m_dict[f] = m_dict[last_substantial].copy()
                                tail_held_count += 1
                            else:  # f < first_substantial (head empty)
                                m_dict[f] = m_dict[first_substantial].copy()
                                tail_held_count += 1
                log(f"SAM2 obj{oid} post-pass: {collapsed_count} interior empties -> NN fallback, "
                    f"{tail_held_count} tail empties held to nearest substantial mask.")

            # SAM2 anchor-frame fix: frame 0 uses no_mem_embed (image-SAM mode,
            # no temporal memory) producing weaker masks with interior holes.
            # If frame 2 exists and has >10% more coverage than frames 0 or 1,
            # copy frame 2 backward to replace the weak anchor frames.
            for oid, m_dict in masks_per_obj.items():
                if 2 not in m_dict:
                    continue
                ref_cov = m_dict[2].sum()
                if ref_cov < 100:
                    continue
                patched = []
                for early_f in (0, 1):
                    if early_f in m_dict and m_dict[early_f].sum() < ref_cov * 0.9:
                        m_dict[early_f] = m_dict[2].copy()
                        patched.append(early_f)
                if patched:
                    log(f"SAM2 obj{oid}: anchor-fix copied frame 2 -> frames {patched}")

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
        # Combine per-object dicts into per-frame dict[obj_id, mask]. The
        # obj_id key is preserved so downstream halo binding (Option C) can
        # tell MASK 1 from MASK 2 even when only one of them is present in
        # a given frame.
        masks_out = {}
        all_frame_keys = set()
        for m_dict in masks_per_obj.values():
            all_frame_keys.update(m_dict.keys())
        for f in sorted(all_frame_keys):
            per_frame = {}
            for oid in active_obj_ids:
                m = masks_per_obj[oid].get(f)
                if m is not None:
                    per_frame[oid] = m
            if per_frame:
                masks_out[f] = per_frame
        log(f"SAM2 video propagation done — {len(masks_out)} frames, "
            f"{sum(len(v) for v in masks_out.values())} per-object masks")
        return masks_out

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
def _is_hevc_file(fp, mpi=None):
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
    # v1.0 fix 2026-05-20: cv2 FourCC misses 10-bit HEVC (Main 10 L6.1) in
    # QuickTime containers — returns 0 or an unrecognized tag. Fall back to
    # the MediaPoolItem's Video Codec property string when the cv2 probe
    # didn't detect HEVC. Resolve reports something like "H.265 Main 10 L6.1"
    # for HEVC clips; substring match catches all 10-bit variants.
    if not is_hevc and mpi is not None:
        try:
            _props = mpi.GetClipProperty() if mpi else {}
            _codec_str = str(_props.get("Video Codec", "") or "").lower()
            if "265" in _codec_str or "hevc" in _codec_str or "hev" in _codec_str:
                is_hevc = True
                log(f"HEVC fallback detection via clip property: '{_codec_str}'")
        except Exception as _cpe:
            log(f"HEVC clip-property fallback failed: {_cpe}")
    _hevc_codec_cache[fp_key] = is_hevc
    return is_hevc


# WHAT IT DOES: Detects retime / speed ramp / fps conform on a timeline clip.
# Returns (is_retimed, reason). v1.0 doesn't support retimed input — the
# Path 0/A/B render fallback for HEVC produces wrong frames on retimed clips
# (slow-mo seek math is off), which falls through to Path B = Deliver page popup.
# Workflow guard fires BEFORE any decode/render so the user is told to undo
# the retime, key on the unaltered source, then re-apply retime to the CK output
# (standard pro VFX workflow — Mocha/Silhouette/AE all key first, retime after).
def _is_clip_retimed(timeline_item, timeline_fps=None):
    if timeline_item is None:
        return False, ""
    try:
        ti_duration = timeline_item.GetDuration()
        if not ti_duration or ti_duration <= 0:
            return False, ""
        mpi = timeline_item.GetMediaPoolItem()
        if mpi is None:
            return False, ""
        props = mpi.GetClipProperty() or {}
        try:
            src_fps_str = str(props.get("FPS", "") or props.get("Frame Rate", "")).strip()
            src_fps = float(src_fps_str) if src_fps_str else None
        except Exception:
            src_fps = None
        if timeline_fps is None:
            try:
                timeline_fps = fps_of_timeline()
            except Exception:
                timeline_fps = None
        src_duration_frames = None
        try:
            src_start = timeline_item.GetSourceStartFrame()
            src_end = timeline_item.GetSourceEndFrame()
            if src_start is not None and src_end is not None:
                src_duration_frames = src_end - src_start
        except Exception:
            pass
        if src_duration_frames is None:
            try:
                left = timeline_item.GetLeftOffset() or 0
                right = timeline_item.GetRightOffset() or 0
                total = int(props.get("Frames", "0") or 0)
                if total > 0:
                    src_duration_frames = total - left - right
            except Exception:
                pass
        if src_fps is None or timeline_fps is None or not src_duration_frames or src_duration_frames <= 0:
            return False, ""
        # 2026-05-23: bail if src_duration_frames is unreasonably small.
        # Resolve sometimes returns 1 frame from GetSourceStartFrame/EndFrame on
        # HEVC clips with Clip Attributes fps conform — false-positive retime
        # warning. If we can't trust the source duration, skip the check.
        if src_duration_frames < 10:
            return False, ""
        expected_ti = src_duration_frames * (float(timeline_fps) / float(src_fps))
        if expected_ti < 10:
            return False, ""
        if src_fps and timeline_fps and abs(float(src_fps) - float(timeline_fps)) < 0.5:
            return False, ""
        ratio = ti_duration / expected_ti
        if abs(ratio - 1.0) > 0.05:
            reason = (f"clip occupies {ti_duration} timeline frames, "
                      f"expected {expected_ti:.0f} at 100%% speed "
                      f"(source {src_duration_frames}fr @ {src_fps}fps -> timeline @ {timeline_fps}fps)")
            return True, reason
        return False, ""
    except Exception as _re:
        try:
            log(f"Retime detect error (proceeding without guard): {_re}")
        except Exception:
            pass
        return False, ""


def _warn_speed_ramp(reason=""):
    msg_short = "SPEED RAMP / RETIME DETECTED -- see Log below"
    try:
        status(msg_short)
    except Exception:
        pass
    instructions = (
        "================================================================\n"
        "CORRIDORKEY: SPEED RAMP / RETIME DETECTED ON THIS CLIP\n"
        "================================================================\n"
        "CorridorKey v1 does not key retimed clips. The retime breaks\n"
        "source-frame seek math and Resolve switches to the Deliver page.\n"
        "\n"
        "TO FIX (standard VFX workflow):\n"
        "  1. Right-click the clip on the timeline\n"
        "  2. Change Clip Speed -> reset to 100%% (remove any speed ramp)\n"
        "  3. Run CorridorKey on the unaltered clip\n"
        "  4. After CK finishes, re-apply your speed ramp to the keyed result\n"
        "     (Mocha/Silhouette/AE all use this 'key first, retime after' flow)\n"
        "\n"
        "Native retime support is on the v1.1 polish queue (PyAV reader).\n"
        "================================================================"
    )
    try:
        log(instructions)
    except Exception:
        pass
    if reason:
        try:
            log(f"Retime detection: {reason}")
        except Exception:
            pass


# WHAT IT DOES: Decodes a single source frame from an HEVC file via PyAV (FFmpeg).
#   - Handles 10-bit HEVC Main 10 (cv2 returns frame=None on these).
#   - libswscale applies the file's BT.709/BT.2020 metadata correctly → no yellow→pink shift.
#   - PTS-based seek handles FPS conform automatically (source_t in seconds from source start).
#   - ~0.05-0.1s per frame vs ~2s for Resolve still-export path → 20-40x faster.
# DEPENDS-ON: PyAV >= 12 in CK venv, cv2 for RGB→BGR conversion.
# DANGER ZONE LOW: failure returns None — caller must fall back to Resolve render path.
def _read_frame_via_pyav(fp, source_t):
    """source_t = seconds from the START of the SOURCE file (not timeline).
    Returns BGR uint8 ndarray (cv2 convention) or None on error."""
    try:
        import av
        import numpy as _np
        import cv2 as _cv2
    except ImportError as _ie:
        log(f"PyAV import failed: {_ie}")
        return None
    container = None
    try:
        container = av.open(fp)
        vs = container.streams.video[0]
        vs.thread_type = "AUTO"
        # 2026-05-21 fix v5: read source colorspace + color_range from the
        # codec_context (NOT decoded frame's `.colorspace` attribute — that
        # returns an int enum that doesn't stringify usefully). PyAV/swscale
        # defaults to BT.601 when src_colorspace is omitted, causing the
        # canonical hot-pink shift on BT.709 HEVC. PyAV Issue #873.
        try:
            _cs_int = int(vs.codec_context.colorspace or 0)
            _rng_int = int(vs.codec_context.color_range or 0)
        except Exception:
            _cs_int = 0
            _rng_int = 0
        # FFmpeg AVColorSpace enum: 1=BT.709, 0=RGB, 5=BT.470BG (BT.601 PAL),
        # 6=SMPTE170M (BT.601 NTSC), 9=BT.2020 NCL. Default 709 for HD/UHD.
        if _cs_int in (5, 6):
            _src_cs_name = "itu601"
        elif _cs_int == 9:
            _src_cs_name = "itu2020_ncl"
        else:
            _src_cs_name = "itu709"
        # FFmpeg AVColorRange enum: 1=MPEG/TV (16-235), 2=JPEG/PC (0-255).
        _src_rng_name = "jpeg" if _rng_int == 2 else "mpeg"
        # Seek to source_t. PTS is in vs.time_base units. Backward to nearest keyframe.
        target_pts = int(source_t / float(vs.time_base))
        try:
            container.seek(target_pts, any_frame=False, backward=True, stream=vs)
        except Exception:
            container.seek(max(0, target_pts), any_frame=False, backward=True)
        decoded = None
        for f in container.decode(vs):
            if f.pts is None:
                continue
            # Walk forward until we land at or past the requested time.
            if f.pts < target_pts:
                continue
            decoded = f
            break
        if decoded is None:
            log(f"PyAV: no frame at t={source_t:.3f}s in {os.path.basename(fp)}")
            return None
        # 2026-05-20 fix: reformat to 16-bit RGB (rgb48le) with EXPLICIT colorspace
        # + range, then >>8 to uint8. Two reasons:
        # 1) libswscale uses a HIGHER-QUALITY chroma resampler when the output bit
        #    depth is wider than input (10-bit YUV -> 16-bit RGB picks a multi-tap
        #    filter vs the default bilinear at 8-bit). This kills the 1-2px green
        #    halo at sharp luma edges that was fooling the NN into tighter mattes.
        # 2) dst_range="pc" forces full-range output regardless of whether the source
        #    bitstream has color_range="tv" or "unspecified" — removes the swscale
        #    guess that caused crushed blacks / clipped whites on Nikon HEVC.
        # The >>8 collapse matches the BRAW path exactly (engine line 4198/4104 also
        # does uint16 >> 8 from Resolve's 16-bit TIFF). Net: NN input bit-identical
        # to yesterday's good-matte Resolve-still-export path.
        # 2026-05-21 fix v5: pass src_colorspace + src_color_range + dst variants
        # explicitly via reformat() — PyAV Issue #873 (swscale defaults to BT.601
        # if you omit src_colorspace, producing the hot-pink shift on BT.709
        # source). Range strings must be "mpeg"/"jpeg" — NOT "tv"/"pc". rgb48le
        # for 16-bit precision, then >>8 to 8-bit, then cvtColor RGB→BGR.
        try:
            log(f"PyAV reformat: src_cs={_src_cs_name} src_rng={_src_rng_name}")
            reformatted = decoded.reformat(
                format="rgb48le",
                src_colorspace=_src_cs_name,
                dst_colorspace="itu709",
                src_color_range=_src_rng_name,
                dst_color_range="jpeg",
            )
            rgb16 = reformatted.to_ndarray()
            rgb8 = (rgb16 >> 8).astype(_np.uint8)
        except Exception as _ref_e:
            log(f"PyAV reformat fallback (rgb24, 8-bit): {_ref_e}")
            rgb8 = decoded.to_ndarray(format="rgb24")
        return _cv2.cvtColor(rgb8, _cv2.COLOR_RGB2BGR)
    except Exception as _pe:
        try:
            log(f"PyAV single-frame read failed for {os.path.basename(fp)}: {_pe}")
        except Exception:
            pass
        return None
    finally:
        if container is not None:
            try: container.close()
            except Exception: pass


# WHAT IT DOES: Exports a range of timeline-aligned frames from an HEVC file via PyAV,
#   sampling at TIMELINE fps so the output frame count matches what the timeline shows.
#   - Sequential decode (single seek to start, then walk frames) — fast on RTX hardware.
#   - Handles FPS conform implicitly because we sample by TIME, not source-frame index.
#   - Writes frame_NNNNNN.tif files starting at frame_000000 — same shape as
#     _export_braw_range_to_frames so downstream code needs no changes.
# DEPENDS-ON: PyAV, cv2 for BGR write, tempfile, Path.
# DANGER ZONE LOW: failure returns None — caller must fall back to BRAW/Resolve path.
def _export_hevc_range_via_pyav(fp, source_t_start, n_output_frames, output_fps, status_cb=None):
    """source_t_start = seconds from source-file start; n_output_frames at output_fps cadence."""
    try:
        import av
        import numpy as _np
        import cv2 as _cv2
        import tempfile as _tf
        import time as _t
    except ImportError as _ie:
        log(f"PyAV range export — import failed: {_ie}")
        return None
    if n_output_frames <= 0 or output_fps <= 0:
        log(f"PyAV range export — bad inputs n={n_output_frames} fps={output_fps}")
        return None
    container = None
    try:
        # 2026-05-23 fix: place temp TIFFs OFF %LOCALAPPDATA%\Temp.
        # Defender real-time-scans that path heavily — the post-PyAV
        # glob() over 210 fresh TIFFs blocks the main thread on the scan
        # queue, producing the PROCESS RANGE silent hang. D: drive scans
        # via a different (faster) queue and clears the bottleneck.
        # Falls back to %LOCALAPPDATA%\Temp only if D: is unavailable.
        try:
            _ck_temp_root = Path(r"D:\ck_pyav_temp")
            _ck_temp_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            _ck_temp_root = Path(_tf.gettempdir()) / "corridorkey_renders"
            _ck_temp_root.mkdir(parents=True, exist_ok=True)
        temp_dir = (_ck_temp_root
                    / f"CK_PyAvHEVC_{int(source_t_start*1000)}_{n_output_frames}_{int(_t.time())}")
        temp_dir.mkdir(parents=True, exist_ok=True)
        container = av.open(fp)
        vs = container.streams.video[0]
        vs.thread_type = "AUTO"
        time_base = float(vs.time_base)
        target_times = [source_t_start + i / float(output_fps) for i in range(n_output_frames)]
        start_pts = int(target_times[0] / time_base)
        try:
            container.seek(max(0, start_pts), any_frame=False, backward=True, stream=vs)
        except Exception:
            container.seek(max(0, start_pts), any_frame=False, backward=True)
        out_idx = 0
        for f in container.decode(vs):
            if out_idx >= n_output_frames:
                break
            if f.pts is None:
                continue
            t_frame = f.pts * time_base
            # Emit the current frame for every target that this frame covers.
            # (For high source-fps to low timeline-fps, one decoded frame covers
            # one target; for low source-fps to high timeline-fps, one decoded
            # frame may cover multiple targets — duplicate it.)
            while out_idx < n_output_frames and t_frame >= target_times[out_idx]:
                # 2026-05-20 fix: 16-bit RGB output via reformat (rgb48le) with
                # explicit colorspace+range forces libswscale's high-quality
                # chroma resampler. 16-bit TIFF written here, engine's
                # uint16 >> 8 path (line ~4198) collapses to 8-bit at read time —
                # matches BRAW/Resolve-still bit-depth contract exactly. See
                # _read_frame_via_pyav for full rationale.
                rgb16 = f.to_ndarray(format="rgb24")
                bgr16 = _cv2.cvtColor(rgb16, _cv2.COLOR_RGB2BGR)
                out_path = temp_dir / f"frame_{out_idx:06d}.tif"
                _cv2.imwrite(str(out_path), bgr16)
                out_idx += 1
                if status_cb is not None and (out_idx % 30 == 0 or out_idx == n_output_frames):
                    try:
                        status_cb(f"PyAV HEVC decode: {out_idx}/{n_output_frames}")
                    except Exception:
                        pass
        if out_idx == 0:
            log(f"PyAV range export: no frames decoded for {os.path.basename(fp)}")
            return None
        if out_idx < n_output_frames:
            # Pad missing tail frames by duplicating the last decoded one — keeps
            # downstream frame-count contract intact. IMREAD_UNCHANGED preserves
            # the uint16 bit-depth we just wrote.
            try:
                _last = sorted(temp_dir.glob("frame_*.tif"))[-1]
                last_bgr = _cv2.imread(str(_last), _cv2.IMREAD_UNCHANGED)
                while out_idx < n_output_frames:
                    _cv2.imwrite(str(temp_dir / f"frame_{out_idx:06d}.tif"), last_bgr)
                    out_idx += 1
            except Exception:
                pass
        log(f"PyAV range export done: {out_idx} frames -> {temp_dir.name}")
        return str(temp_dir)
    except Exception as _pe:
        try:
            log(f"PyAV range export failed for {os.path.basename(fp)}: {_pe}")
            import traceback as _tb
            log(_tb.format_exc())
        except Exception:
            pass
        return None
    finally:
        # 2026-05-23: container.close() on 4K HEVC after 210-frame extract hangs
        # the main thread (libav cleanup blocks indefinitely on this codec/file
        # combo). The container is a temp object — letting Python's GC reclaim
        # it on next allocation is harmless. Skip close() entirely.
        # PROBE: confirm we reach this point.
        try:
            log(f"PyAV: finally entered, container is None? {container is None}")
        except Exception:
            pass
        # Container intentionally NOT closed — was the hang point.


def _source_fps_from_props(props, fallback_fps):
    """Best-effort source FPS extraction from MediaPoolItem GetClipProperty dict."""
    try:
        for key in ("FPS", "Frame Rate", "VFR", "Video FPS"):
            val = props.get(key) if props else None
            if val is None or val == "":
                continue
            try:
                f = float(str(val).strip())
                if f > 0:
                    return f
            except Exception:
                continue
    except Exception:
        pass
    try:
        return float(fallback_fps)
    except Exception:
        return 30.0


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
                # 2026-05-15: IMREAD_UNCHANGED preserves 16-bit TIF depth from Resolve's RGB16LZW export.
                # IMREAD_COLOR truncates to uint8 BGR, losing 8 bits per channel = hair edge alpha precision.
                frame = cv2.imread(still_path, cv2.IMREAD_UNCHANGED)
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
                # 2026-05-15: IMREAD_UNCHANGED preserves 16-bit TIF depth from Resolve's RGB16LZW export.
                frame = cv2.imread(str(img_file), cv2.IMREAD_UNCHANGED)
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
                    _is_hevc_bg = _is_hevc_file(fp, mpi=mpi)
                    if fp.lower().endswith(('.braw', '.cin', '.dng', '.ari')) or _is_hevc_bg:
                        bg_frame = None
                        if _is_hevc_bg:
                            # Fast HEVC path via PyAV (same fix as LIVE PREVIEW).
                            _src_fps_bg = _source_fps_from_props(props, fps)
                            _source_t_bg = (c.GetLeftOffset() / _src_fps_bg) + (max(0, cf - c.GetStart()) / float(fps))
                            bg_frame = None  # A/B 2026-05-21: bypass PyAV — test Resolve-render hypothesis
                            if bg_frame is not None:
                                log(f"BG plate from V{track_idx} via PyAV: {os.path.basename(fp)}")
                                return bg_frame
                        # Fallback (BRAW always, HEVC if PyAV failed).
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
    # 2026-05-21: Smart despeckle BEFORE normalization. Preserves hair (proximity-to-body
    # keep-zone protects any partial-alpha component attached to or near the largest blob)
    # while removing isolated specks (pink registration marks on the cyc, dust, lighting
    # variation). Settings come from the panel — defaults via _merge_live_params.
    try:
        _despeckle_settings = _merge_live_params(get_settings())
        a2d = _apply_despeckle_to_alpha(a2d, _despeckle_settings)
    except Exception as _de:
        log(f"Smart despeckle in preview skipped: {_de}")
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
    # Kill tracked viewer subprocess if it exited (stale handle cleanup)
    if _viewer_proc is not None:
        _viewer_proc = None
    # Clear stale scrub data so the viewer starts clean, not in scrub mode from last session.
    try:
        _stale_idx = SESSION_DIR / "scrub_index.json"
        if _stale_idx.exists():
            _stale_idx.unlink()
            log("Cleared stale scrub_index.json from previous session")
    except Exception:
        pass
    try:
        (SESSION_DIR / "plugin_heartbeat").touch(exist_ok=True)
    except Exception:
        pass
    parent_pid = str(os.getpid())
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _pf = SESSION_DIR / "viewer.pid"
        if _pf.exists():
            _old_pid = int(_pf.read_text().strip())
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(_old_pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            import time as _tkill; _tkill.sleep(0.5)
            log(f"Killed previous viewer PID {_old_pid}")
    except Exception:
        pass
    _env = os.environ.copy()
    _env["CORRIDORKEY_PARENT_PID"] = parent_pid
    _viewer_proc = subprocess.Popen(
        [python_exe, viewer_script, "--persistent", "--session", str(SESSION_DIR), "--parent-pid", parent_pid],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
        env=_env,
    )
    try:
        import ctypes as _ct_job
        from ctypes import wintypes as _wt_job
        _k32 = _ct_job.windll.kernel32
        class _JBLI(_ct_job.Structure):
            _fields_ = [("PerProcessUserTimeLimit", _ct_job.c_int64), ("PerJobUserTimeLimit", _ct_job.c_int64),
                         ("LimitFlags", _wt_job.DWORD), ("MinimumWorkingSetSize", _ct_job.c_size_t),
                         ("MaximumWorkingSetSize", _ct_job.c_size_t), ("ActiveProcessLimit", _wt_job.DWORD),
                         ("Affinity", _ct_job.POINTER(_ct_job.c_ulong)), ("PriorityClass", _wt_job.DWORD),
                         ("SchedulingClass", _wt_job.DWORD)]
        class _IOC(_ct_job.Structure):
            _fields_ = [("R", _ct_job.c_uint64), ("W", _ct_job.c_uint64), ("O", _ct_job.c_uint64),
                         ("RT", _ct_job.c_uint64), ("WT", _ct_job.c_uint64), ("OT", _ct_job.c_uint64)]
        class _JELI(_ct_job.Structure):
            _fields_ = [("Basic", _JBLI), ("Io", _IOC), ("ProcMem", _ct_job.c_size_t),
                         ("JobMem", _ct_job.c_size_t), ("PeakProc", _ct_job.c_size_t), ("PeakJob", _ct_job.c_size_t)]
        _job = _k32.CreateJobObjectW(None, None)
        _info = _JELI()
        _info.Basic.LimitFlags = 0x2000
        _k32.SetInformationJobObject(_job, 9, _ct_job.byref(_info), _ct_job.sizeof(_info))
        _k32.AssignProcessToJobObject(_job, int(_viewer_proc._handle))
        global _viewer_job
        _viewer_job = _job
        log(f"v2 preview launched direct (PID {_viewer_proc.pid}) + Job Object")
    except Exception as _je:
        log(f"v2 preview launched direct (PID {_viewer_proc.pid}, Job Object failed: {_je})")

# WHAT IT DOES: Writes the keyed result to disk as PNG. Three export formats:
#   0 = RGBA (foreground + alpha), 1 = Alpha only (grayscale matte), 2 = Foreground only (no alpha)
# DEPENDS-ON: OpenCV, numpy
# ISOLATED: pure file write, no side effects beyond disk
# v1.0 codec map: 0=PNG8 1=PNG16 2=TIFF16 3=EXR32. Returns the file extension
# the codec writes. Used by the renderer to decide both the imwrite call and
# the destination filename so MediaPool finds the right sequence on import.
def _codec_extension(codec):
    return {0: ".png", 1: ".png", 2: ".tiff", 3: ".exr"}.get(int(codec or 0), ".png")


def _srgb_png_info():
    """Build a PngInfo with sRGB + gAMA + cHRM chunks so color-managed apps
    (DaVinci Resolve in ACES, YRGB CM, or any other mode) auto-apply the
    correct inverse transform when reading the PNG.

    Untagged PNGs default to "scene-referred ACEScc" interpretation under
    ACES projects, which lifts midtones across the entire frame (the
    "whole frame brighter" bug Berto reported 2026-05-11). Tagging the
    PNG as sRGB is the universal fix — works for ACES, YRGB, YRGB Color
    Managed, and every other color-managed mode without requiring the
    user to right-click anything.

    Chunks written:
      sRGB — 1 byte rendering intent (0=Perceptual, the standard default).
      gAMA — gamma 1/2.2 (45455 * 100000) — fallback for apps that don't
             honor sRGB chunk.
      cHRM — sRGB chromaticity primaries — completes the sRGB standard
             triple; some color-managed apps require all three to trust
             the tag.
    """
    from PIL.PngImagePlugin import PngInfo
    pnginfo = PngInfo()
    pnginfo.add(b'sRGB', bytes([0]))  # 0 = Perceptual rendering intent
    pnginfo.add(b'gAMA', (45455).to_bytes(4, 'big'))
    def _u32(v): return v.to_bytes(4, 'big')
    chrm = b''.join(_u32(v) for v in (
        31270, 32900,  # white point (D65)
        64000, 33000,  # red primary
        30000, 60000,  # green primary
        15000,  6000,  # blue primary
    ))
    pnginfo.add(b'cHRM', chrm)
    return pnginfo


def save_output(fg, matte, path, fmt, codec=0):
    """Write the keyed output for ONE frame.

    fmt    — structure: 0 = RGBA keyed (default), 1 = alpha-only, 2 = composite (no alpha).
    codec  — bit depth + container: 0 PNG 8-bit, 1 PNG 16-bit, 2 TIFF 16-bit, 3 EXR 32-bit.
    path   — destination path. The caller picks the extension via _codec_extension.
    """
    import cv2, numpy as np
    m = matte[:, :, 0] if len(matte.shape) == 3 else matte
    m = np.clip(m, 0.0, 1.0).astype(np.float32)
    fg_clip = np.clip(fg, 0.0, 1.0).astype(np.float32) if fg is not None else None
    codec = int(codec or 0)

    if codec == 3:
        # EXR: float32 throughout. RGBA mode merges 4 channels; matte-only writes
        # single channel; composite writes RGB. OpenCV's EXR writer expects BGR
        # (or BGRA for 4-channel) float32. EXR is linear scene-referred — no
        # sRGB tag (would be wrong). Color-managed apps already know EXR is
        # linear by convention.
        if fmt == 0 and fg_clip is not None:
            fb = cv2.cvtColor(fg_clip, cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(path), cv2.merge([fb[:, :, 0], fb[:, :, 1], fb[:, :, 2], m]))
        elif fmt == 1:
            cv2.imwrite(str(path), m)
        else:
            if fg_clip is not None:
                cv2.imwrite(str(path), cv2.cvtColor(fg_clip, cv2.COLOR_RGB2BGR))
        return

    # PNG / TIFF — uint8 (codec 0) or uint16 (codec 1, 2). Same channel logic.
    if codec == 0:
        au = (m * 255.0).astype(np.uint8)
        fg_int = (fg_clip * 255.0).astype(np.uint8) if fg_clip is not None else None
    else:
        au = (m * 65535.0).astype(np.uint16)
        fg_int = (fg_clip * 65535.0).astype(np.uint16) if fg_clip is not None else None

    # Untagged write — bytes go to disk verbatim, no color metadata chunks.
    # Resolve interprets the PNG using its timeline color science (Rec.709
    # Gamma 2.4 for davinciYRGB legacy, ACEScct for ACES, etc.). The PIL
    # sRGB-tagged path (7c641cf) caused a 2.2→2.4 re-encode midtone lift on
    # legacy Rec.709 Gamma 2.4 timelines. Proper color-science-aware tagging
    # is a future enhancement.
    if fmt == 0 and fg_int is not None:
        fb = cv2.cvtColor(fg_int, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(path), cv2.merge([fb[:, :, 0], fb[:, :, 1], fb[:, :, 2], au]))
    elif fmt == 1:
        cv2.imwrite(str(path), au)
    elif fmt == 2 and fg_int is not None:
        cv2.imwrite(str(path), cv2.cvtColor(fg_int, cv2.COLOR_RGB2BGR))


def save_alpha_only(matte, path, codec=0):
    """v1.0 SAM matte sidecar — writes alpha-only mask in the user-selected codec.

    Matches save_output's codec semantics so the SAM sidecar always matches the
    keyed clip's bit depth / container.
    """
    import cv2, numpy as np
    m = matte[:, :, 0] if len(matte.shape) == 3 else matte
    m = np.clip(m, 0.0, 1.0).astype(np.float32)
    codec = int(codec or 0)
    if codec == 3:
        cv2.imwrite(str(path), m)
    elif codec == 0:
        cv2.imwrite(str(path), (m * 255.0).astype(np.uint8))
    else:
        cv2.imwrite(str(path), (m * 65535.0).astype(np.uint16))


def _write_fusion_sidecars(
    fg, ck_alpha, sam_union, source_rgb, settings, output_dir, clip_name, frame_num,
):
    """Write 5 sidecar PNGs for Editable Layers (Fusion Comp) mode.

    Returns dict of first-frame paths keyed by sidecar type, or empty dict on
    failure. Uses 16-bit PNG (Fusion reads them reliably, unlike OpenCV TIFFs).
    """
    import cv2, numpy as np

    from corridorkey_sam_merge import compute_garbage_matte
    try:
        from corridorkey_sam_merge import compute_region_matte
    except ImportError:
        compute_region_matte = None

    pad = f"{frame_num:06d}"
    paths = {}

    # 1. CK_RGB — NN foreground with despill (same as Track 2 export).
    if fg is not None:
        fg_clean = np.clip(fg, 0.0, 1.0).copy()
        _despill_str = float(settings.get("despill_strength", 0.5))
        if _despill_str > 0:
            try:
                from CorridorKeyModule.core import color_utils as _cu_sc
                fg_clean = _cu_sc.despill_opencv(fg_clean, green_limit_mode="average", strength=_despill_str)
            except Exception:
                pass
        fg_16 = (cv2.cvtColor(fg_clean, cv2.COLOR_RGB2BGR) * 65535.0).astype(np.uint16)
        p = output_dir / f"CK_RGB_{clip_name}.{pad}.png"
        cv2.imwrite(str(p), fg_16)
        paths["ck_rgb"] = str(p)

    def _to_3ch_16(mono):
        """Convert single-channel matte to 3-channel 16-bit BGR for Fusion compatibility.
        Fusion treats single-channel PNGs as mask images with no RGB data."""
        m16 = (np.clip(mono, 0.0, 1.0) * 65535.0).astype(np.uint16)
        return cv2.merge([m16, m16, m16])

    # 2. CK_ALPHA — CK neural net alpha (3-channel for Fusion Custom tool)
    if ck_alpha is not None:
        m = ck_alpha[:, :, 0] if ck_alpha.ndim == 3 else ck_alpha
        p = output_dir / f"CK_ALPHA_{clip_name}.{pad}.png"
        cv2.imwrite(str(p), _to_3ch_16(m))
        paths["ck_alpha"] = str(p)

    # 3. SAM_ALPHA — SAM2 body silhouette (3-channel for Fusion)
    if sam_union is not None:
        s = sam_union[:, :, 0] if sam_union.ndim == 3 else sam_union
        p = output_dir / f"SAM_ALPHA_{clip_name}.{pad}.png"
        cv2.imwrite(str(p), _to_3ch_16(s))
        paths["sam_alpha"] = str(p)

    # 4. GARBAGE_ALPHA — garbage matte from SAM bbox + user settings
    if sam_union is not None and not bool(settings.get("garbage_bypass", False)):
        _ge = int(settings.get("garbage_expand_px", 0))
        _gf = int(settings.get("garbage_feather_px", 0))
        _gyt = int(settings.get("garbage_y_top_pct", 0))
        _gyb = int(settings.get("garbage_y_bot_pct", 100))
        if _ge > 0 or _gyt > 0 or _gyb < 100:
            gm = compute_garbage_matte(sam_union, expand_px=_ge, feather_px=_gf,
                                       y_top_pct=_gyt, y_bot_pct=_gyb)
            if gm is not None:
                p = output_dir / f"GARBAGE_ALPHA_{clip_name}.{pad}.png"
                cv2.imwrite(str(p), _to_3ch_16(gm))
                paths["garbage_alpha"] = str(p)

    # 5. REGION_ALPHA — green/blue screen detection zone (3-channel for Fusion)
    if source_rgb is not None and compute_region_matte is not None:
        region = compute_region_matte(source_rgb, screen_type=settings.get("screen_type", "green"))
        if region.dtype == np.uint8:
            region = region.astype(np.float32) / 255.0
        p = output_dir / f"REGION_ALPHA_{clip_name}.{pad}.png"
        cv2.imwrite(str(p), _to_3ch_16(region))
        paths["region_alpha"] = str(p)

    return paths


# Tell Resolve not to apply any IDT to imported CK renders. Without this, on
# ACES projects Resolve defaults to applying sRGB→working-space conversion to
# untagged PNGs, which doesn't match the source clip's IDT and produces a
# ~8% midtone lift on the timeline. "Bypass" tells Resolve to leave the PNG
# pixels alone — CK pixels in, CK pixels out, no transform.
def _bypass_idt_on_imports(items):
    if not items:
        return
    # Try a matrix of (key, value) candidates. SetClipProperty returns True on
    # success / False on bad key+value combo. We stop at the first pair that
    # sticks for each clip. Order matters — Rec.709 IDT on a Rec.709-encoded
    # PNG matches what Resolve auto-applies to a ProRes/H.264 source, so the
    # imported clip lands in ACES the same way the source clip would. The
    # Bypass / No Input Transform options are fallbacks for legacy modes.
    candidates = [
        ("ACES Input Transform", "Rec.709"),
        ("ACES Input Transform", "Rec709"),
        ("Input Color Space", "Rec.709"),
        ("Input Color Space", "Rec.709 (Scene)"),
        ("ACES Input Transform", "Bypass"),
        ("ACES Input Transform", "No Input Transform"),
        ("Input Color Space", "Bypass"),
        ("Input Color Space", "No Input Transform"),
    ]
    for _mpi in items:
        _last_log = None
        for _key, _val in candidates:
            try:
                if _mpi.SetClipProperty(_key, _val):
                    _last_log = f"{_key}={_val}"
                    break
            except Exception:
                pass
        if _last_log:
            try: log(f"  CK import: SetClipProperty {_last_log}")
            except Exception: pass


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
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0, refiner_strength=settings["refiner_strength"], despeckle_enabled=False, despeckle_size=settings["despeckle_size"], fg_source=settings.get("fg_source", "nn"))
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength} fg_source={ps.fg_source}")
        res = proc.process_frame(fr, ah, ps)
        fg, mt = res.get("fg"), res.get("alpha")
        if mt is not None:
            if _CK_CHROMA_KILL_ENABLED:
                try:
                    from corridorkey_sam_merge import apply_chroma_kill_to_matte
                    mt = apply_chroma_kill_to_matte(mt, fr, settings.get("screen_type", "green"))
                except Exception as _ckm_e: log(f"chroma kill failed (non-fatal): {_ckm_e}")
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
        settings = _merge_live_params(get_settings())
        sam2_gate_file = SESSION_DIR / "sam2_mask.png"
        log(f"SAM2 gate: alpha_method={settings.get('alpha_method')} sam2_mask.png={'EXISTS' if sam2_gate_file.exists() else 'MISSING'}")
        cf, fps = get_current_frame_info()
        log(f"Frame {cf}")
        # v1.0 fix 2026-05-20: use the SELECTED clip the user clicked on, not a
        # hardcoded track-1 walk. Hardcode would pick the wrong clip when the
        # green-screen is on V2+ or a still photo lives on V1.
        clip = None
        try:
            clip = timeline.GetCurrentVideoItem()
        except Exception:
            clip = None
        if clip is None:
            # Fallback: walk all video tracks for the clip at playhead.
            try:
                track_count = timeline.GetTrackCount("video")
            except Exception:
                track_count = 1
            for _ti in range(1, max(1, int(track_count)) + 1):
                _clips_on_track = timeline.GetItemListInTrack("video", _ti) or []
                for _c in _clips_on_track:
                    if _c.GetStart() <= cf < _c.GetEnd():
                        clip = _c
                        break
                if clip is not None:
                    break
        if clip is None:
            status("ERROR: No clip selected — click a clip in the timeline first"); return
        _is_ramped, _ramp_reason = _is_clip_retimed(clip, timeline_fps=fps)
        if _is_ramped:
            _warn_speed_ramp(_ramp_reason)
            return
        cs = clip.GetStart()
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        fp = props.get("File Path", "")
        log(f"Source: {os.path.basename(fp)}")
        fn = clip.GetLeftOffset() + (cf - cs)
        if fn < 0: fn = 0
        # HEVC decode-routing: cv2's HEVC decoder mishandles BT.709/BT.2020 metadata and
        # produces a yellow→pink color shift on Nikon Z (and similar) clips. Also returns
        # frame=None on 10-bit Main 10 in QT containers. v1.0 fix 2026-05-20: route HEVC
        # through PyAV (FFmpeg) for both color-correct decoding AND speed (~0.05s/frame vs
        # ~2s for Resolve still-export). Resolve render stays as last-resort fallback.
        frame = None
        if _is_hevc_file(fp, mpi=mpi):
            # PyAV samples by TIME, so handle FPS conform correctly:
            _src_fps_hevc = _source_fps_from_props(props, fps)
            _source_t = (clip.GetLeftOffset() / _src_fps_hevc) + (max(0, cf - cs) / float(fps))
            log(f"HEVC detected — PyAV reader, source_t={_source_t:.3f}s (src_fps={_src_fps_hevc} tl_fps={fps})")
            status("Reading HEVC via PyAV...")
            frame = None  # A/B 2026-05-21: bypass PyAV — test Resolve-render hypothesis
            if frame is None:
                log(f"PyAV failed — falling back to Resolve render decoder")
                status("PyAV failed — Resolve fallback...")
                frame = _read_frame_via_resolve_render(mpi, fn, try_direct=True)
            if frame is None:
                log(f"ERROR: All HEVC decode paths failed for {os.path.basename(fp)}")
                status("ERROR: Cannot read HEVC frame"); return
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
            # DISABLED: CPU proc pre-init — suspected VRAM pressure breaking SAM2 preview.
            # Re-enable after SAM2 debug confirms root cause.
            # if cached_scrub_cpu_proc["proc"] is None:
            #     def _init_cpu_proc():
            #         try:
            #             os.environ["CORRIDORKEY_SKIP_COMPILE"] = "1"
            #             cached_scrub_cpu_proc["proc"] = CorridorKeyProcessor(device="cpu")
            #             log("CPU scrub proc ready — SCRUB RANGE will use CPU inference.")
            #         except Exception as _cpu_e:
            #             log(f"CPU scrub proc failed (scrub will use CUDA fallback): {_cpu_e}")
            #     import threading as _cpu_thr
            #     _cpu_thr.Thread(target=_init_cpu_proc, daemon=True).start()
            #     log("CPU scrub proc loading in background...")
        else:
            log("AI ready (cached)")
        proc = cached_processor["proc"]
        log("Alpha hint...")
        settings["_render_frame"] = cf
        ah = generate_alpha_hint(frame, settings)
        log("Processing...")
        fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0, refiner_strength=settings["refiner_strength"], despeckle_enabled=False, despeckle_size=settings["despeckle_size"], fg_source=settings.get("fg_source", "nn"))
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength} fg_source={ps.fg_source}")
        if ps.despeckle_enabled:
            log(f"Despeckle: ON (size {ps.despeckle_size})")
        res = proc.process_frame(fr, ah, ps)
        import re as _re_cn
        cn = _re_cn.sub(r'[^\w.-]', '_', Path(fp).stem)
        od = Path(items["OutputPath"].Text) / f"CK_{cn}"
        od.mkdir(parents=True, exist_ok=True)
        op = od / f"CK_{cn}_{cf:06d}{_codec_extension(settings.get('output_codec', 0))}"
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
        # 2026-05-14 fix: SAM merge is the user's choice via OutputContent dropdown.
        # mt stays CK-alone through choke, despeckle, and the preview push so the
        # artist sees the raw CK matte. The v2.2 chroma-gated merge runs only at
        # save time, only when output_content == 0 (Combined mode). All other
        # modes (Both, CK only, SAM only) export CK alone from this code path.
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
            # OutputContent gate: SAM combine runs ONLY when the user selected
            # Combined mode. Berto's rule (2026-05-14): SAM combination is opt-in
            # via dropdown, never automatic. The artist owns that decision.
            #
            # 2026-05-14 architecture: simple multiply CK * process_sam_matte(SAM)
            # at viewer-parity (preview_viewer_v2.py:2751). Generously-dilated SAM
            # via margin slider lets CK's hair tendrils and soft edges survive
            # INSIDE the dilation; CK pixels outside the dilated SAM go to zero
            # (kills floor/walls). Replaces the v2.2 trimap+CFM merge which
            # REPLACED CK with a smoothed CFM solution and destroyed hair detail.
            _content_single = int(settings.get("output_content", 0))
            if _content_single == 0:
                try:
                    # 1d648e5 PROPER recipe: route through _panel_dispatch_sam2_combine
                    # so per-mask dilate_into kicks in. MASK 1 (body) extends ONLY into
                    # low-alpha pixels (hair, green-screen), MASK 2 (feet) extends ONLY
                    # into high-alpha pixels (floor). Chroma-aware extension is THE
                    # mechanism that preserves hair without pulling in floor halos.
                    # Direct apply_sam2_gate_subtract call misses this — that's why
                    # my earlier 1d648e5-recipe attempt still lost hair detail.
                    _ck_2d_single = mt_for_save[:, :, 0] if len(mt_for_save.shape) == 3 else mt_for_save
                    _per_obj_gates = _load_per_object_sam2_gates(_ck_2d_single.shape, settings)
                    if _per_obj_gates:
                        if isinstance(_per_obj_gates, dict):
                            _frame_obj_ids = list(_per_obj_gates.keys())
                            _gates_list = list(_per_obj_gates.values())
                        else:
                            _frame_obj_ids = list(range(1, len(_per_obj_gates) + 1))
                            _gates_list = list(_per_obj_gates)
                        # Pre-process each gate (margin/soften from sliders) and
                        # resize to ck shape — mirrors the scrub path at line 2914.
                        _processed = []
                        for _gm in _gates_list:
                            _gx = _dilate_sam2_mask(_gm, margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
                            _gx = _soften_sam2_mask(_gx, soften=settings.get("sam2_soften", 0))
                            if _gx.shape != _ck_2d_single.shape:
                                _gx = cv2.resize(_gx, (_ck_2d_single.shape[1], _ck_2d_single.shape[0]),
                                                 interpolation=cv2.INTER_LINEAR)
                            _processed.append(_gx)
                        if _processed:
                            mt_for_save = _panel_dispatch_sam2_combine(
                                _ck_2d_single, _processed, fr, settings, obj_ids=_frame_obj_ids,
                            )
                            log(f"Combined export: 1d648e5 per-mask dispatch ({len(_processed)} mask(s), obj_ids={_frame_obj_ids})")
                    else:
                        log("OutputContent=Combined but SAM PNGs missing — exporting CK alone")
                except Exception as _merge_e:
                    log(f"Combined merge failed (non-fatal, exporting CK alone): {_merge_e}")
                    # 2026-05-16 diagnostic: write full traceback to disk so we can
                    # inspect the merge failure root cause. Remove once chroma-weight
                    # merge is validated.
                    try:
                        import traceback as _tb_diag
                        _tb_text = _tb_diag.format_exc()
                        log(f"Combined merge TRACEBACK:\n{_tb_text}")
                        from pathlib import Path as _P_diag
                        _dbg_path = _P_diag(tempfile.gettempdir()) / "ck_merge_exception.txt"
                        _dbg_path.write_text(_tb_text, encoding="utf-8")
                        log(f"Combined merge traceback written to {_dbg_path}")
                    except Exception as _diag_e:
                        log(f"Combined merge diagnostic write failed: {_diag_e}")
            save_output(fg, mt_for_save, op, settings["export_format"], codec=settings.get("output_codec", 0))
            log(f"Saved: {op.name}")
            # 2026-05-19 GARBAGE MATTE sidecar — third PNG alongside CK/SAM,
            # computed from the SAM silhouette via dilate(expand) + Gaussian(feather).
            try:
                if not bool(settings.get("garbage_bypass", False)):
                    from corridorkey_sam_merge import compute_garbage_matte as _cgm
                    _ge_px = int(settings.get("garbage_expand_px", 0))
                    _gf_px = int(settings.get("garbage_feather_px", 0))
                    _gyt_pct = int(settings.get("garbage_y_top_pct", 0))
                    _gyb_pct = int(settings.get("garbage_y_bot_pct", 100))
                    _sam_for_gm = None
                    try:
                        if _per_obj_gates:
                            _g_list = list(_per_obj_gates.values()) if isinstance(_per_obj_gates, dict) else list(_per_obj_gates)
                            from corridorkey_sam_merge import binarize_sam_silhouette, union_binary_silhouettes
                            _bins = [binarize_sam_silhouette(_g) for _g in _g_list if _g is not None]
                            if _bins:
                                _sam_for_gm = union_binary_silhouettes(_bins)
                    except Exception:
                        _sam_for_gm = None
                    if _sam_for_gm is not None:
                        _gm = _cgm(_sam_for_gm, expand_px=_ge_px, feather_px=_gf_px,
                                   y_top_pct=_gyt_pct, y_bot_pct=_gyb_pct)
                        if _gm is not None:
                            _g_op = op.parent / f"GARBAGE_{op.stem.replace('CK_', '', 1)}{op.suffix}"
                            save_alpha_only(_gm, _g_op, codec=settings.get("output_codec", 0))
                            log(f"GARBAGE matte saved: {_g_op.name} (expand={_ge_px}, feather={_gf_px}, y={_gyt_pct}-{_gyb_pct})")
            except Exception as _gm_e:
                log(f"Garbage matte export failed (non-fatal): {_gm_e}")
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
        _bypass_idt_on_imports(imp)
        if settings["output_mode"] in [0, 2]:
            # v1.0 two-mask placement — find highest-used video track so we
            # never overwrite a previous-run output. Source clip's track itself
            # counts as "used" so highest_used >= source_track always. CK
            # lands on highest_used + 1.
            _src_track_now = 1
            try:
                _tc_for_scan = timeline.GetTrackCount("video")
                for _ti_scan in range(1, int(_tc_for_scan) + 1):
                    _scan_clips = timeline.GetItemListInTrack("video", _ti_scan) or []
                    for _sc in _scan_clips:
                        if _sc.GetStart() <= cf < _sc.GetEnd():
                            _src_track_now = _ti_scan
                            break
            except Exception:
                pass
            _highest_used_now = _highest_used_video_track(timeline)
            _ck_track = max(int(_src_track_now), int(_highest_used_now)) + 1
            tc = timeline.GetTrackCount("video")
            log(f"[v1.0] Video tracks={tc}, source on V{_src_track_now}, highest-used V{_highest_used_now} → CK on V{_ck_track}")
            while tc < _ck_track:
                timeline.AddTrack("video")
                tc = timeline.GetTrackCount("video")
                log(f"Added video track (now V{tc})")
            # Try multiple append methods
            # Set clip In/Out to 1 frame before append (helps recordFrame work)
            try:
                imp[0].SetClipProperty("In", "00:00:00:00")
                imp[0].SetClipProperty("Out", "00:00:00:00")
            except: pass
            # Try recordFrame with constrained clip first
            target_frame = cf + 1
            log(f"[v1.0] Placing on V{_ck_track}, recordFrame={target_frame}")
            result = media_pool.AppendToTimeline([{"mediaPoolItem": imp[0], "trackIndex": _ck_track, "recordFrame": target_frame}])
            if not result:
                log("recordFrame failed, append without it")
                result = media_pool.AppendToTimeline([{"mediaPoolItem": imp[0], "trackIndex": _ck_track}])
            if result:
                try:
                    track2_items = timeline.GetItemListInTrack("video", _ck_track)
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
                    timeline.SetTrackEnable("video", _src_track_now, False)
                    log(f"V{_src_track_now} disabled — uncheck 'Disable source clip' or press D in timeline to re-enable")
                status(f"DONE! V{_ck_track}")
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
    global _scrubber_proc, _scrubber_job
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
    # Kill-on-close Job Object — identical to the live-preview viewer guard (~line 3013).
    # If on_close's fire-and-forget taskkill loses the race, or Resolve hard-kills our
    # Python with no signal, Windows auto-terminates the scrubber when this process dies
    # (and its job handle closes). _scrubber_job MUST stay referenced for the job to live.
    try:
        import ctypes as _ct_sj
        from ctypes import wintypes as _wt_sj
        _k32s = _ct_sj.windll.kernel32
        class _JBLI_s(_ct_sj.Structure):
            _fields_ = [("PerProcessUserTimeLimit", _ct_sj.c_int64), ("PerJobUserTimeLimit", _ct_sj.c_int64),
                         ("LimitFlags", _wt_sj.DWORD), ("MinimumWorkingSetSize", _ct_sj.c_size_t),
                         ("MaximumWorkingSetSize", _ct_sj.c_size_t), ("ActiveProcessLimit", _wt_sj.DWORD),
                         ("Affinity", _ct_sj.POINTER(_ct_sj.c_ulong)), ("PriorityClass", _wt_sj.DWORD),
                         ("SchedulingClass", _wt_sj.DWORD)]
        class _IOC_s(_ct_sj.Structure):
            _fields_ = [("R", _ct_sj.c_uint64), ("W", _ct_sj.c_uint64), ("O", _ct_sj.c_uint64),
                         ("RT", _ct_sj.c_uint64), ("WT", _ct_sj.c_uint64), ("OT", _ct_sj.c_uint64)]
        class _JELI_s(_ct_sj.Structure):
            _fields_ = [("Basic", _JBLI_s), ("Io", _IOC_s), ("ProcMem", _ct_sj.c_size_t),
                         ("JobMem", _ct_sj.c_size_t), ("PeakProc", _ct_sj.c_size_t), ("PeakJob", _ct_sj.c_size_t)]
        _job_s = _k32s.CreateJobObjectW(None, None)
        _info_s = _JELI_s()
        _info_s.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _k32s.SetInformationJobObject(_job_s, 9, _ct_sj.byref(_info_s), _ct_sj.sizeof(_info_s))
        _k32s.AssignProcessToJobObject(_job_s, int(_scrubber_proc._handle))
        _scrubber_job = _job_s
        log(f"BRAW scrubber launched: {Path(frames_dir).name} + Job Object")
    except Exception as _sje:
        log(f"BRAW scrubber launched: {Path(frames_dir).name} (Job Object failed: {_sje})")


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
            # Multi-object v0.8: _s2_masks[frame_idx] can be dict[obj_id, mask],
            # list/tuple of masks, or a bare ndarray. The diagnostic dump below
            # collapses to a single soft mask via per-pixel max (union) — same
            # shape the legacy viewer reads back from sam2_gate_raw.png.
            _s2_raw = _s2_masks[frame_idx]
            if isinstance(_s2_raw, dict):
                _s2_arrs = [_np.asarray(m, dtype=_np.float32) for m in _s2_raw.values()]
            elif isinstance(_s2_raw, (list, tuple)):
                _s2_arrs = [_np.asarray(m, dtype=_np.float32) for m in _s2_raw]
            else:
                _s2_arrs = [_np.asarray(_s2_raw, dtype=_np.float32)]
            _s2_union = _s2_arrs[0]
            for _a in _s2_arrs[1:]:
                _s2_union = _np.maximum(_s2_union, _a)
            _s2_raw8 = (_s2_union * 255).clip(0, 255).astype(_np.uint8)
            _cv2.imwrite(str(out_dir / "sam2_gate_raw.png"), _s2_raw8)
            # Multi-object v0.8 — _s2_masks[frame_idx] is dict[obj_id, mask]
            # (or list-of-masks from older code, or a bare mask in legacy).
            # Track obj_ids so Option C halo binding can route HALO BODY to
            # MASK 1 and HALO FEET to MASK 2.
            _per_frame = _s2_masks[frame_idx]
            if isinstance(_per_frame, dict):
                _frame_obj_ids = list(_per_frame.keys())
                _frame_gates = list(_per_frame.values())
            elif isinstance(_per_frame, (list, tuple)):
                _frame_obj_ids = list(range(1, len(_per_frame) + 1))
                _frame_gates = list(_per_frame)
            else:
                _frame_obj_ids = [1]
                _frame_gates = [_per_frame]
            _gates_list = []
            for _gm in _frame_gates:
                _gx = _dilate_sam2_mask(_gm, margin=ctx["settings"].get("sam2_margin", SAM2_MATTE_MARGIN))
                _gx = _soften_sam2_mask(_gx, soften=ctx["settings"].get("sam2_soften", 0))
                if _gx.shape != mt.shape[:2]:
                    _gx = _cv2.resize(_gx, (mt.shape[1], mt.shape[0]),
                                      interpolation=_cv2.INTER_LINEAR)
                _gates_list.append(_gx)
            if not bool(ctx["settings"].get("sam2_bypass", False)):
                mt = _panel_dispatch_sam2_combine(mt, _gates_list, fr, ctx["settings"], obj_ids=_frame_obj_ids)
        if fg is not None and mt is not None:
            out_dir = ctx["scrub_dir"] / f"{frame_idx:03d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            fg_16 = (fg * 65535).clip(0, 65535).astype(_np.uint16)
            mt2d  = mt[:, :, 0] if len(mt.shape) == 3 else mt
            al_16 = (mt2d * 65535).clip(0, 65535).astype(_np.uint16)
            # Verify each imwrite actually landed a file on disk — Berto bug
            # 2026-05-09: imwrite was silently returning False for frames 1-9
            # while the success counter still incremented, so the viewer read
            # scrub_index.json count=N and asked for files that didn't exist.
            _fg_path = out_dir / "fg.png"
            _al_path = out_dir / "alpha.png"
            _fg_ok = _cv2.imwrite(str(_fg_path), _cv2.cvtColor(fg_16, _cv2.COLOR_RGB2BGR))
            _al_ok = _cv2.imwrite(str(_al_path), al_16)
            if (_fg_ok and _al_ok and _fg_path.exists() and _al_path.exists()):
                _scrub_key_done += 1
            else:
                log(f"Scrub frame {frame_idx}: imwrite returned ok={_fg_ok}/{_al_ok}, "
                    f"on-disk fg={_fg_path.exists()} alpha={_al_path.exists()} "
                    f"out_dir={out_dir} fg_shape={fg_16.shape} alpha_shape={al_16.shape}")
        else:
            log(f"Scrub frame {frame_idx}: engine returned fg={fg is not None} mt={mt is not None} — skipping write")
    except Exception as _ke:
        import traceback as _tb
        log(f"Scrub frame {frame_idx}: keying failed: {_ke}")
        log(_tb.format_exc())
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
    _is_hevc = _is_hevc_file(fp, mpi=mi) if fp else False
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
        despeckle_enabled=False,
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
        # Multi-object v0.8 — pass per-mask points so SAM2 tracks MASK 1 + MASK 2
        # as native obj_id=1 / obj_id=2 in a single propagation pass.
        _p1 = sam_points_per_obj[1]; _p2 = sam_points_per_obj[2]
        _pos = _p1["positive"]; _neg = _p1["negative"]
        _pos2 = _p2["positive"]; _neg2 = _p2["negative"]
        if _pos or _neg or _pos2 or _neg2:
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
                    def _anchor_rel_for(anch_abs):
                        if anch_abs is not None and sampled_tl_frames:
                            _good_tl = [sampled_tl_frames[i] for i in _good_orig_idxs]
                            _dists = [abs(f - anch_abs) for f in _good_tl]
                            return _dists.index(min(_dists))
                        return 0
                    _anchor_rel = _anchor_rel_for(_p1["frame"])
                    _anchor_rel_obj2 = _anchor_rel_for(_p2["frame"])
                    status("SAM2: propagating mask across scrub frames...")
                    log(f"SAM2 scrub: {_n_good} frames, anchor_rel={_anchor_rel}, obj2={'yes' if (_pos2 or _neg2) else 'no'}")
                    _raw_masks = run_sam2_video_propagation(
                        str(_sam_tmp), 0, 0, 0, _n_good, _pos, _neg, _anchor_rel,
                        pos_pts_obj2=_pos2, neg_pts_obj2=_neg2,
                        anchor_frame_obj2_abs=_anchor_rel_obj2,
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


# WHAT IT DOES: Builds a labeled Fusion comp on a timeline clip pointing at the
#   sidecar PNG sequences from a CK render. Loads CK_RGB, optional CK_alpha, and
#   optional SAM_alpha as Loaders; wires a ChannelBooleans MAX as a Stage-1 alpha
#   union; routes through MediaOut. Stage 2 adds KeyMix + REGION + GARBAGE matte.
# DEPENDS-ON: Fusion scripting API (AddTool / ConnectInput / SetAttrs / Undo).
# AFFECTS: Adds nodes to timeline_item.GetFusionCompByIndex(1) inside a single Undo
#   block so one Resolve undo rolls back the whole graph.
# DANGER ZONE LOW: nondestructive; if comp #1 already has nodes they stay intact.
def _build_ck_fusion_comp(timeline_item, ck_rgb_first_path,
                          ck_alpha_first_path=None, sam_alpha_first_path=None):
    """Build a CK auto-comp on the given timeline_item. Returns True on success."""
    if not timeline_item:
        log("Fusion comp: timeline_item is None")
        return False
    if not ck_rgb_first_path:
        log("Fusion comp: no CK RGB sidecar path provided")
        return False

    try:
        comp = timeline_item.GetFusionCompByIndex(1)
        if comp is None:
            try:
                comp = timeline_item.AddFusionComp()
                log("Fusion comp: AddFusionComp created comp #1")
            except Exception as _ae:
                log(f"Fusion comp: AddFusionComp failed: {_ae}")
                return False
    except Exception as e:
        log(f"Fusion comp: GetFusionCompByIndex failed: {e}")
        return False
    if not comp:
        log("Fusion comp: comp handle is None")
        return False

    try:
        try:
            resolve.OpenPage("fusion")
            import time as _ft
            for _pw in range(20):
                if resolve.GetCurrentPage() == "fusion":
                    break
                _ft.sleep(0.1)
        except Exception:
            pass
        comp.Lock()
        comp.StartUndo("CK Auto Comp v1")

        nodes_made = []
        # CK_RGB Loader (foreground color from CK NN)
        ck_rgb = comp.AddTool("Loader", 0, 0)
        if not ck_rgb:
            log("Fusion comp: AddTool('Loader') returned None — Resolve Studio required, or API restricted")
            try: comp.EndUndo(True)
            except Exception: pass
            try: comp.Unlock()
            except Exception: pass
            return False
        try:
            ck_rgb.Clip = ck_rgb_first_path
            ck_rgb.SetAttrs({"TOOLS_Name": "CK_RGB"})
        except Exception as _se:
            log(f"Fusion comp: CK_RGB Loader Clip set failed: {_se}")
        nodes_made.append("CK_RGB")

        # CK_alpha Loader (optional)
        ck_alpha = None
        if ck_alpha_first_path:
            ck_alpha = comp.AddTool("Loader", 1, 0)
            if ck_alpha:
                try:
                    ck_alpha.Clip = ck_alpha_first_path
                    ck_alpha.SetAttrs({"TOOLS_Name": "CK_ALPHA"})
                except Exception:
                    pass
                nodes_made.append("CK_ALPHA")

        # SAM_alpha Loader (optional)
        sam_alpha = None
        if sam_alpha_first_path:
            sam_alpha = comp.AddTool("Loader", 2, 0)
            if sam_alpha:
                try:
                    sam_alpha.Clip = sam_alpha_first_path
                    sam_alpha.SetAttrs({"TOOLS_Name": "SAM_ALPHA"})
                except Exception:
                    pass
                nodes_made.append("SAM_ALPHA")

        # ChannelBooleans MAX of CK_alpha + SAM_alpha — Stage 1 simple alpha union.
        # Stage 2 will replace this with MatteControl + KeyMix using REGION.
        alpha_combine = None
        if ck_alpha and sam_alpha:
            alpha_combine = comp.AddTool("ChannelBoolean", 3, 0)
            if alpha_combine:
                try:
                    alpha_combine.SetAttrs({"TOOLS_Name": "ALPHA_MAX_CK_OR_SAM"})
                    alpha_combine.SetInput("Operation", 12)  # 12 = Max
                    alpha_combine.ConnectInput("Background", ck_alpha.Output)
                    alpha_combine.ConnectInput("Foreground", sam_alpha.Output)
                except Exception:
                    pass
                nodes_made.append("ALPHA_MAX")

        # MediaOut — Stage 1 wires CK_RGB straight through so the timeline clip
        # plays the CK foreground. Operator can rewire to alpha_combine in Fusion.
        mo = comp.AddTool("MediaOut", 4, 0)
        if mo:
            try:
                mo.SetAttrs({"TOOLS_Name": "CK_OUTPUT"})
                mo.ConnectInput("Input", ck_rgb.Output)
            except Exception:
                pass
            nodes_made.append("MediaOut")

        try: comp.EndUndo(True)
        except Exception: pass
        try: comp.Unlock()
        except Exception: pass
        try: resolve.OpenPage("edit")
        except Exception: pass
        log(f"Fusion comp built: {len(nodes_made)} nodes — {', '.join(nodes_made)}")
        return True
    except Exception as e:
        log(f"Fusion comp build error: {type(e).__name__}: {e}")
        try: comp.EndUndo(False)
        except Exception: pass
        try: comp.Unlock()
        except Exception: pass
        try: resolve.OpenPage("edit")
        except Exception: pass
        return False


def _build_ck_fusion_comp_v2(timeline_item, sidecar_paths, render_in=None, render_dur=None, loader_clip_offset=0):
    """Build organized Fusion comp with labeled nodes and override merge points.

    LAYOUT (no crossing lines, left-to-right flow):

    Row 0 (Color):  CK_RGB -> CK_COLOR_FIX -> CK_COMBINE -> MediaOut
    Row 1 (Matte):  CK_COMBINED -> CK_EDGE_CHOKE -> SAM_OVERRIDE -> CK_OVERRIDE -> (up to CK_COMBINE)
    Row 2 (Manual):  SAM_ALPHA (connected to SAM_OVERRIDE fg, inactive)
                     CK_ALPHA (connected to CK_OVERRIDE fg, inactive)
    Row 3 (Notes):  Instructions note
    """
    if not timeline_item:
        log("Fusion comp: timeline_item is None")
        return False
    if not sidecar_paths.get("ck_rgb"):
        log("Fusion comp: no CK RGB sidecar path")
        return False

    try:
        comp = timeline_item.GetFusionCompByIndex(1)
        if comp is None:
            try:
                comp = timeline_item.AddFusionComp()
                log("Fusion comp: created new comp")
            except Exception as _ae:
                log(f"Fusion comp: AddFusionComp failed: {_ae}")
                return False
    except Exception as e:
        log(f"Fusion comp: GetFusionCompByIndex failed: {e}")
        return False
    if not comp:
        log("Fusion comp: comp handle is None")
        return False

    try:
        try:
            resolve.OpenPage("fusion")
            import time as _ft
            for _pw in range(20):
                if resolve.GetCurrentPage() == "fusion":
                    break
                _ft.sleep(0.1)
        except Exception:
            pass
        comp.Lock()
        comp.StartUndo("CK Editable Layers v3")

        _ck_node_names = [
            "CK_RGB", "CK_COLOR_FIX", "CK_COMBINED", "CK_ALPHA", "SAM_ALPHA",
            "REGION_ALPHA", "GARBAGE_ALPHA", "CK_MATTE_MATH", "CK_GARBAGE_GATE",
            "CK_OVERRIDE_SAM", "CK_OVERRIDE_CK", "CK_EDGE_CHOKE", "CK_LEG_CHOKE",
            "CK_LEG_MASK", "CK_LEG_CHOKE", "CK_CHOKE_ZONE", "CK_CHOKE_AREA",
            "SAM_FEET_FIX", "SAM_FEET_MASK",
            "SAM_OVERRIDE", "CK_OVERRIDE", "CK_NOTE", "ERASE_WIRE",
            "FOUNDATION_NOTE", "MASK_1_NOTE", "MASK_2_NOTE", "ERASE_WIRE_NOTE",
            "MASK_1", "MASK_1_AREA", "MASK_2", "MASK_2_AREA", "MASK_3", "MASK_3_AREA",
            "CK_COMBINE", "CK_OUTPUT", "ALPHA_MAX_CK_OR_SAM",
            "MediaOut1",
        ]
        for _name in _ck_node_names:
            try:
                _existing = comp.FindTool(_name)
                if _existing:
                    _existing.Delete()
            except Exception:
                pass

        _clip_tl_start = timeline_item.GetStart()
        comp_attrs = comp.GetAttrs() or {}
        _comp_start = comp_attrs.get("COMPN_GlobalStart", 0)
        _comp_end = comp_attrs.get("COMPN_GlobalEnd", 1000)
        _comp_dur = _comp_end - _comp_start
        _source_lo = timeline_item.GetLeftOffset() or 0
        if render_in is not None and render_dur is not None:
            _render_offset = (render_in - _clip_tl_start) if render_in >= _clip_tl_start else 0
            _loader_start = _comp_start + _source_lo + _render_offset
            _loader_end = _loader_start + render_dur - 1
        else:
            _loader_start = _comp_start
            _loader_end = _comp_end
            render_dur = _comp_dur
        log(f"Fusion comp: comp={_comp_start}-{_comp_end}, loader={_loader_start}-{_loader_end} (dur={render_dur})")

        nodes_made = []

        # Color codes:
        #   GREEN = touchable (adjustables, masks, areas, paint)
        #   RED   = do not touch (loaders, plumbing, output)
        _COLOR_TOUCH = {"R": 0.30, "G": 0.70, "B": 0.30}     # green
        _COLOR_HANDS_OFF = {"R": 0.80, "G": 0.25, "B": 0.25} # red
        # Legacy aliases (kept so existing _tint calls below still work):
        _COLOR_SAM = _COLOR_HANDS_OFF
        _COLOR_CK = _COLOR_HANDS_OFF
        _COLOR_ADJ = _COLOR_TOUCH
        _COLOR_ERASE = _COLOR_TOUCH
        _COLOR_OUT = _COLOR_HANDS_OFF

        def _tint(node, color):
            # Try multiple Fusion API forms for tile color (varies by version)
            try: node.TileColor = color
            except Exception: pass
            try: node.SetAttrs({"TileColor": color})
            except Exception: pass
            try: node.SetAttrs({"TOOLB_TileColor": color})
            except Exception: pass

        def _add_loader(name, path, x, y, color=None):
            node = comp.AddTool("Loader", x, y)
            if not node:
                log(f"Fusion comp: AddTool('Loader') failed for {name}")
                return None
            _p = str(path).replace("\\", "/")
            try:
                node.Clip = _p
                node.SetAttrs({"TOOLS_Name": name})
                if color: _tint(node, color)
                node.SetInput("GlobalIn", _loader_start)
                node.SetInput("GlobalOut", _loader_end)
                node.SetInput("ClipTimeStart", 0)
                node.SetInput("ClipTimeEnd", render_dur - 1)
                node.SetInput("HoldFirstFrame", 0)
                node.SetInput("HoldLastFrame", 0)
                node.SetInput("Loop", 0)
            except Exception as _le:
                log(f"Fusion comp: Loader config for {name} failed: {_le}")
            nodes_made.append(name)
            return node

        # === ROW 0: COLOR PATH (y=0) ===
        ck_rgb = _add_loader("CK_RGB", sidecar_paths["ck_rgb"], 0, 0, _COLOR_CK)
        if not ck_rgb:
            try: comp.EndUndo(True)
            except Exception: pass
            try: comp.Unlock()
            except Exception: pass
            return False

        _ck_rgb_ext = str(sidecar_paths.get("ck_rgb", "")).rsplit(".", 1)[-1].lower()
        _needs_gamut = _ck_rgb_ext in ("png", "tif", "tiff")
        _color_out = ck_rgb
        if _needs_gamut:
            _gamut = comp.AddTool("GamutConvert", 1, 0)
            if not _gamut:
                _gamut = comp.AddTool("Gamut", 1, 0)
            if _gamut:
                try:
                    _gamut.SetAttrs({"TOOLS_Name": "CK_COLOR_FIX"})
                    _tint(_gamut, _COLOR_CK)
                    _gamut.SetInput("SourceSpace", "sRGB")
                    _gamut.SetInput("RemoveGamma", 1)
                    _gamut.ConnectInput("Input", ck_rgb.Output)
                    _color_out = _gamut
                    nodes_made.append("CK_COLOR_FIX")
                except Exception as _ge:
                    log(f"Fusion comp: GamutConvert failed ({_ge})")

        # === FOUNDATION ROW (column 0): all three matte sources stacked ===
        # User picks which one feeds the chain. Default = CK_COMBINED.
        # To switch: delete wire from CK_COMBINED to CK_EDGE_CHOKE,
        # drag from SAM_ALPHA or CK_ALPHA to CK_EDGE_CHOKE instead.
        ck_combined = _add_loader("CK_COMBINED", sidecar_paths.get("ck_combined", ""), 0, 2, _COLOR_TOUCH) if sidecar_paths.get("ck_combined") else None
        # SAM_ALPHA and CK_ALPHA moved up to be visible as foundation options.
        # Each loader also feeds its respective MASK via foreground (wires set later).
        sam_alpha_foundation_pos = (0, 3)
        ck_alpha_foundation_pos = (0, 4)
        _matte_source = ck_combined

        # CK_EDGE_CHOKE: gentle universal edge shrink (whole body)
        edge_choke = None
        if _matte_source:
            edge_choke = comp.AddTool("ErodeDilate", 1, 2)
            if edge_choke:
                try:
                    edge_choke.SetAttrs({"TOOLS_Name": "CK_EDGE_CHOKE", "TOOLB_NameSet": True})
                    _tint(edge_choke, _COLOR_ADJ)
                    edge_choke.SetInput("XAmount", 0.0)
                    edge_choke.ConnectInput("Input", _matte_source.Output)
                    nodes_made.append("CK_EDGE_CHOKE")
                    _matte_source = edge_choke
                except Exception as _ece:
                    log(f"Fusion comp: CK_EDGE_CHOKE failed: {_ece}")

        # CK_CHOKE_ZONE: heavier choke limited to a region (CK_CHOKE_AREA Rectangle).
        # Generic — user moves the rectangle to wherever their shot needs it.
        leg_choke = None
        leg_rect = None
        if _matte_source:
            leg_choke = comp.AddTool("ErodeDilate", 2, 2)
            if leg_choke:
                try:
                    leg_choke.SetAttrs({"TOOLS_Name": "CK_CHOKE_ZONE", "TOOLB_NameSet": True})
                    _tint(leg_choke, _COLOR_ADJ)
                    leg_choke.SetInput("XAmount", 0.0)
                    leg_choke.ConnectInput("Input", _matte_source.Output)
                    nodes_made.append("CK_CHOKE_ZONE")
                    # Rectangle defining where the zone-choke applies. Default to
                    # bottom 50% as a starting point; user moves it per shot.
                    leg_rect = comp.AddTool("RectangleMask", 2, 1)
                    if leg_rect:
                        try:
                            leg_rect.SetAttrs({"TOOLS_Name": "CK_CHOKE_AREA", "TOOLB_NameSet": True})
                            _tint(leg_rect, _COLOR_ADJ)
                            leg_rect.SetInput("Center", {1: 0.5, 2: 0.25, 3: 0.0})
                            leg_rect.SetInput("Width", 1.5)
                            leg_rect.SetInput("Height", 0.5)
                            leg_rect.SetInput("SoftEdge", 0.02)
                            leg_choke.ConnectInput("EffectMask", leg_rect.Output)
                            nodes_made.append("CK_CHOKE_AREA")
                        except Exception as _lme:
                            log(f"Fusion comp: CK_CHOKE_AREA failed: {_lme}")
                    _matte_source = leg_choke
                except Exception as _lce:
                    log(f"Fusion comp: CK_CHOKE_ZONE failed: {_lce}")

        # MASK_1: pre-wired SAM-source region override. Pass Through ON by default.
        # Default Rectangle sits in the lower portion as a starting point;
        # user moves it per shot. Layout: AREA above, MASK in middle, SAM_ALPHA below.
        sam_feet_fix = None
        feet_rect = None
        if _matte_source:
            sam_feet_fix = comp.AddTool("Merge", 3, 2)
            if sam_feet_fix:
                try:
                    sam_feet_fix.SetAttrs({"TOOLS_Name": "MASK_1", "TOOLB_NameSet": True, "TOOLB_PassThrough": True})
                    _tint(sam_feet_fix, _COLOR_TOUCH)
                    sam_feet_fix.ConnectInput("Background", _matte_source.Output)
                    nodes_made.append("MASK_1")
                    feet_rect = comp.AddTool("RectangleMask", 3, 1)
                    if feet_rect:
                        try:
                            feet_rect.SetAttrs({"TOOLS_Name": "MASK_1_AREA", "TOOLB_NameSet": True})
                            _tint(feet_rect, _COLOR_TOUCH)
                            feet_rect.SetInput("Center", {1: 0.5, 2: 0.125, 3: 0.0})
                            feet_rect.SetInput("Width", 1.5)
                            feet_rect.SetInput("Height", 0.25)
                            feet_rect.SetInput("SoftEdge", 0.02)
                            sam_feet_fix.ConnectInput("EffectMask", feet_rect.Output)
                            nodes_made.append("MASK_1_AREA")
                        except Exception as _fme:
                            log(f"Fusion comp: MASK_1_AREA failed: {_fme}")
                    _matte_source = sam_feet_fix
                except Exception as _sfe:
                    log(f"Fusion comp: MASK_1 failed: {_sfe}")

        # MASK_2: pre-wired CK detail recovery slot. Pass Through ON by default.
        # Layout: AREA above, MASK in middle, CK_ALPHA below.
        ck_override = None
        mask2_rect = None
        if _matte_source:
            ck_override = comp.AddTool("Merge", 4, 2)
            if ck_override:
                try:
                    ck_override.SetAttrs({"TOOLS_Name": "MASK_2", "TOOLB_NameSet": True, "TOOLB_PassThrough": True})
                    _tint(ck_override, _COLOR_TOUCH)
                    ck_override.ConnectInput("Background", _matte_source.Output)
                    nodes_made.append("MASK_2")
                    mask2_rect = comp.AddTool("RectangleMask", 4, 1)
                    if mask2_rect:
                        try:
                            mask2_rect.SetAttrs({"TOOLS_Name": "MASK_2_AREA", "TOOLB_NameSet": True})
                            _tint(mask2_rect, _COLOR_TOUCH)
                            mask2_rect.SetInput("Center", {1: 0.5, 2: 0.75, 3: 0.0})
                            mask2_rect.SetInput("Width", 0.3)
                            mask2_rect.SetInput("Height", 0.3)
                            mask2_rect.SetInput("SoftEdge", 0.02)
                            ck_override.ConnectInput("EffectMask", mask2_rect.Output)
                            nodes_made.append("MASK_2_AREA")
                        except Exception as _m2e:
                            log(f"Fusion comp: MASK_2_AREA failed: {_m2e}")
                    _matte_source = ck_override
                except Exception as _coe:
                    log(f"Fusion comp: MASK_2 failed: {_coe}")

        # ERASE_WIRE: Paint node on matte path, pre-configured for wire removal.
        # Default Pass Through ON. Brush black, small size, moderate softness,
        # circular shape. User toggles off, paints along wire to kill alpha.
        wire_paint = None
        if _matte_source:
            try:
                wire_paint = comp.AddTool("Paint", 5, 2)
                if wire_paint:
                    wire_paint.SetAttrs({"TOOLS_Name": "ERASE_WIRE", "TOOLB_NameSet": True, "TOOLB_PassThrough": True})
                    _tint(wire_paint, _COLOR_ERASE)
                    wire_paint.ConnectInput("Input", _matte_source.Output)
                    # Pre-configure for wire removal:
                    # - black color (erase alpha)
                    # - small brush size (~0.005, wire-width at 1080p)
                    # - moderate softness for clean edges
                    # - circular brush shape (default 0)
                    try:
                        # Color: pure black (Apply Mode below does the erasing)
                        wire_paint.SetInput("ColorRed", 0.0)
                        wire_paint.SetInput("ColorGreen", 0.0)
                        wire_paint.SetInput("ColorBlue", 0.0)
                        wire_paint.SetInput("ColorAlpha", 1.0)
                        # Brush: thin (wire-width at 1080p), soft for blended edges, circular
                        wire_paint.SetInput("BrushSize", 0.005)
                        wire_paint.SetInput("BrushSoftness", 0.5)
                        wire_paint.SetInput("BrushShape", 0)
                        # Apply Mode: Color (paint solid color = paint black on matte)
                        wire_paint.SetInput("ApplyMode", "Color")
                        # All frames so the stroke persists across the clip
                        wire_paint.SetInput("StrokeAnimation", "AllFrames")
                        # All channels (matte is grayscale, paint affects R G B A together)
                        wire_paint.SetInput("PaintChannels", 1)
                    except Exception as _wpc:
                        log(f"Fusion comp: ERASE_WIRE config skipped: {_wpc}")
                    nodes_made.append("ERASE_WIRE")
                    _matte_source = wire_paint
            except Exception as _wpe:
                log(f"Fusion comp: ERASE_WIRE failed: {_wpe}")
        sam_override = None  # legacy variable, no longer used

        # === ROW 0 continued: CK_COMBINE + MediaOut ===
        ck_combine = None
        if _color_out and _matte_source:
            ck_combine = comp.AddTool("Custom", 6, 0)
            if ck_combine:
                try:
                    ck_combine.SetAttrs({"TOOLS_Name": "CK_COMBINE", "TOOLB_NameSet": True})
                    _tint(ck_combine, _COLOR_OUT)
                    ck_combine.SetInput("RedExpression", "r1")
                    ck_combine.SetInput("GreenExpression", "g1")
                    ck_combine.SetInput("BlueExpression", "b1")
                    ck_combine.SetInput("AlphaExpression", "r2")
                    ck_combine.ConnectInput("Image1", _color_out.Output)
                    ck_combine.ConnectInput("Image2", _matte_source.Output)
                except Exception as _cbe:
                    log(f"Fusion comp: CK_COMBINE setup failed: {_cbe}")
                nodes_made.append("CK_COMBINE")

        mo = comp.AddTool("MediaOut", 7, 0)
        if mo:
            try:
                _tint(mo, _COLOR_OUT)
                _final = ck_combine if ck_combine else _color_out
                mo.ConnectInput("Input", _final.Output)
            except Exception as _moe:
                log(f"Fusion comp: MediaOut setup failed: {_moe}")
            nodes_made.append("MediaOut")

        # === FOUNDATION ROW continued: SAM_ALPHA and CK_ALPHA at front ===
        # They are foundation options AND they feed MASK_1/MASK_2 foregrounds.
        sam_alpha = _add_loader("SAM_ALPHA", sidecar_paths.get("sam_alpha", ""), *sam_alpha_foundation_pos, _COLOR_TOUCH) if sidecar_paths.get("sam_alpha") else None
        ck_alpha = _add_loader("CK_ALPHA", sidecar_paths.get("ck_alpha", ""), *ck_alpha_foundation_pos, _COLOR_TOUCH) if sidecar_paths.get("ck_alpha") else None

        # SAM_ALPHA feeds MASK_1 foreground (junk killer, pre-positioned at feet)
        if sam_alpha and sam_feet_fix:
            try:
                sam_feet_fix.ConnectInput("Foreground", sam_alpha.Output)
            except Exception:
                pass

        # CK_ALPHA feeds MASK_2 foreground (detail recovery)
        if ck_alpha and ck_override:
            try:
                ck_override.ConnectInput("Foreground", ck_alpha.Output)
            except Exception:
                pass

        # FOUNDATION_NOTE: next to the stacked foundation loaders, explains
        # the three choices and how to switch base matte.
        try:
            fn = comp.AddTool("Note", 1, 3)
            if fn:
                fn.SetAttrs({"TOOLS_Name": "FOUNDATION_NOTE", "TOOLB_NameSet": True})
                _tint(fn, _COLOR_TOUCH)
                fn.SetInput("Comments",
                    "PICK YOUR FOUNDATION MATTE\n"
                    "==========================\n\n"
                    "Three alpha mattes stacked on the left.\n"
                    "All three are real images you can SEE.\n\n"
                    "PREVIEW EACH ONE:\n"
                    "  Click the small dot below each loader name\n"
                    "  (the LEFT dot loads it into Viewer 1, RIGHT\n"
                    "  dot into Viewer 2). Compare them side by side.\n"
                    "  Pick the one that best fits your shot.\n\n"
                    "THE THREE OPTIONS:\n\n"
                    "  CK_COMBINED (default, already wired)\n"
                    "    Smart blend - CK detail merged with SAM's\n"
                    "    garbage gate. Background junk is killed,\n"
                    "    hair and soft edges where SAM agrees.\n"
                    "    Works for ~90% of shots out of the box.\n\n"
                    "  SAM_ALPHA\n"
                    "    Pure body shape from SAM2. Hard clean edge,\n"
                    "    no junk anywhere, no hair detail. Best when\n"
                    "    CK is keying things it shouldn't (rope, mat,\n"
                    "    gear, attached cables).\n\n"
                    "  CK_ALPHA\n"
                    "    Pure neural net alpha. Soft hair wisps and\n"
                    "    fine edges SAM cuts off, but ALSO keeps any\n"
                    "    non-green junk in the scene. Best for clean\n"
                    "    plates with no extra equipment in shot.\n\n"
                    "TO SWITCH FOUNDATION:\n"
                    "  1. Click the wire going from CK_COMBINED into\n"
                    "     the chain (the line ending at CK_EDGE_CHOKE\n"
                    "     input). Press DELETE.\n"
                    "  2. Drag from SAM_ALPHA (or CK_ALPHA) output\n"
                    "     square to CK_EDGE_CHOKE's input triangle.\n"
                    "  3. Done. New foundation drives the rest of\n"
                    "     the chain (chokes, masks, paint).\n\n"
                    "WHY THIS IS BETTER THAN ROTOSCOPING:\n"
                    "  All edits happen on the ALPHA MATTE, not on\n"
                    "  the source video. The original plate is never\n"
                    "  re-rendered, never degraded, never resampled.\n"
                    "  You get the highest possible image quality\n"
                    "  while still cleaning up the matte to taste."
                )
                nodes_made.append("FOUNDATION_NOTE")
        except Exception as _fne:
            log(f"Fusion comp: FOUNDATION_NOTE failed: {_fne}")

        # === PER-NODE MINI-NOTES (positioned next to their target node) ===
        def _add_mini_note(name, x, y, text):
            try:
                n = comp.AddTool("Note", x, y)
                if n:
                    n.SetAttrs({"TOOLS_Name": name, "TOOLB_NameSet": True})
                    _tint(n, _COLOR_TOUCH)
                    n.SetInput("Comments", text)
                    nodes_made.append(name)
            except Exception as _mne:
                log(f"Fusion comp: {name} failed: {_mne}")

        if sam_feet_fix:
            _add_mini_note("MASK_1_NOTE", 3, 5,
                "MASK_1 (SAM = subtract junk)\n"
                "----------------------------\n"
                "Replaces matte with pure SAM in a region you draw.\n"
                "SAM excludes wires, tape, mats, attached gear -\n"
                "so anything SAM doesn't see goes black there.\n\n"
                "1. Click MASK_1. Inspector: click bypass dot to\n"
                "   activate (Pass Through OFF).\n"
                "2. Click MASK_1_AREA. Move/resize Rectangle to\n"
                "   the area you want to clean.\n"
                "3. Keyframe Rectangle Center if subject moves."
            )
        if ck_override:
            _add_mini_note("MASK_2_NOTE", 4, 5,
                "MASK_2 (CK = add back detail)\n"
                "-----------------------------\n"
                "Replaces matte with pure CK in a region you draw.\n"
                "CK has hair wisps and soft edges SAM cuts off.\n"
                "Use where SAM cut too tight.\n\n"
                "1. Click MASK_2. Inspector: click bypass dot to\n"
                "   activate (Pass Through OFF).\n"
                "2. Click MASK_2_AREA. Move/resize Rectangle to\n"
                "   the area you want to recover.\n"
                "3. Keyframe Rectangle Center if subject moves."
            )
        if wire_paint:
            _add_mini_note("ERASE_WIRE_NOTE", 5, 5,
                "ERASE_WIRE (wire / pole removal on the matte)\n"
                "---------------------------------------------\n"
                "Brush pre-set: BLACK, thin, soft, circular,\n"
                "Apply Mode = Color, All Frames.\n\n"
                "STEPS:\n"
                "1. Click ERASE_WIRE. Inspector: toggle\n"
                "   Pass Through OFF (the red dot at top).\n"
                "2. In viewer's left-edge toolbar, click the\n"
                "   PAINT icon (looks like a brush).\n"
                "3. In viewer's top toolbar, pick stroke type:\n"
                "   POLYLINE STROKE = click points along wire.\n"
                "   MULTI STROKE    = freehand drag.\n"
                "4. Click points along the wire on the matte.\n"
                "   Each click extends the stroke.\n"
                "5. Wire pixels go black = transparent in output.\n\n"
                "ANIMATE (for moving wires):\n"
                "  In Inspector, right-click 'Stroke Animation'\n"
                "  -> Animate. Then move time, redraw stroke at\n"
                "  new wire position. Keyframes are auto-set."
            )

        # === ROW 3: MASTER INSTRUCTIONS NOTE (y=5) ===
        try:
            note = comp.AddTool("Note", 1, 5)
            if note:
                note.SetAttrs({"TOOLS_Name": "CK_NOTE", "TOOLB_NameSet": True})
                note.SetInput("Comments",
                    "CORRIDORKEY - Node Guide\n"
                    "========================\n\n"
                    "COLOR KEY:\n"
                    "  GREEN = You can touch this. Tune it, toggle it,\n"
                    "          move it, keyframe it.\n"
                    "  RED   = Do not touch. Plumbing and output - if\n"
                    "          you change it the key breaks.\n\n"
                    "PHILOSOPHY:\n"
                    "  The base matte (CK_COMBINED) already mixes CK detail with\n"
                    "  SAM's garbage gate. Background junk is killed. You only\n"
                    "  need to fix issues on the SUBJECT - everything else handled.\n\n"
                    "  All adjustable nodes ship OFF. Activate only what you need.\n\n"
                    "THE THREE MATTE SOURCES (what's what):\n"
                    "  CK_COMBINED (the default base, in the chain)\n"
                    "    The smart blend. CK x SAM with garbage gate baked in.\n"
                    "    Junk outside the body is killed. Hair/edge detail kept\n"
                    "    where SAM agrees. Good for ~90% of frames as-is.\n\n"
                    "  SAM_ALPHA (loader, fed into MASK_1)\n"
                    "    Pure body shape from SAM2. Hard binary edge, no hair\n"
                    "    detail, no junk. Excludes wires, tape, floor, gear\n"
                    "    that's not part of the body. Use when CK_COMBINED\n"
                    "    keeps stuff it shouldn't (floor tape, wire attached\n"
                    "    to body, mat seam at feet).\n\n"
                    "  CK_ALPHA (loader, fed into MASK_2)\n"
                    "    Pure neural net alpha. Has hair wisps and soft edges\n"
                    "    SAM cuts off, but also keeps non-green junk (cables,\n"
                    "    dark mat). Use when SAM cut too tight (hair strands,\n"
                    "    harness strap, anything thin attached to body).\n\n"
                    "  Rule of thumb in a mask region:\n"
                    "    MASK_1 (SAM) = SUBTRACT  - removes junk SAM excludes\n"
                    "    MASK_2 (CK)  = ADD BACK  - restores detail SAM cut\n\n"
                    "EDGE TUNING (off by default):\n"
                    "  CK_EDGE_CHOKE - Light shrink, whole frame. Default 0.\n"
                    "    Push to -0.001 / -0.002 for dark edge fringe everywhere.\n"
                    "  CK_CHOKE_ZONE - Heavier shrink, only inside CK_CHOKE_AREA.\n"
                    "    Push to -0.002 / -0.005 for heavier fringe in one area.\n"
                    "    Move CK_CHOKE_AREA Rectangle to wherever your shot\n"
                    "    needs the heavier choke (default: bottom 50%).\n\n"
                    "REGION MASKS (Pass Through ON by default = bypassed):\n"
                    "  Each mask has an AREA (Rectangle) above it.\n"
                    "  To use any mask:\n"
                    "    1. Click MASK_N - in Inspector, toggle Pass Through OFF.\n"
                    "    2. Click MASK_N_AREA - move/resize the Rectangle to\n"
                    "       cover the area you want to fix.\n"
                    "    3. Keyframe Rectangle position for moving subjects.\n\n"
                    "  MASK_1 (BLUE) - replaces matte with SAM in the area.\n"
                    "    Pre-positioned at feet. Kills floor tape, mat seam,\n"
                    "    wires touching the body, anything SAM excludes.\n\n"
                    "  MASK_2 (ORANGE) - replaces matte with CK in the area.\n"
                    "    Pre-positioned upper frame. Recovers hair detail,\n"
                    "    harness straps, anything SAM cut too tight.\n\n"
                    "ERASE_WIRE (RED, Pass Through ON by default):\n"
                    "  Paint node on the MATTE (not the video).\n"
                    "  Use when masks aren't precise enough for detail work.\n"
                    "    1. Toggle Pass Through OFF in the Inspector.\n"
                    "    2. In the viewer toolbar, pick Paint mode.\n"
                    "    3. BLACK brush = erase (kills alpha = transparent).\n"
                    "       WHITE brush = restore (brings alpha back).\n"
                    "    4. Each stroke can be keyframed for moving objects.\n"
                    "  Good for: thin wires running across the body.\n\n"
                    "READ-ONLY (don't touch):\n"
                    "  CK_RGB - Foreground color from neural net\n"
                    "  CK_COLOR_FIX - sRGB gamma correction\n"
                    "  CK_COMBINED - Base matte (CK + SAM garbage gate)\n"
                    "  CK_COMBINE - Joins color + matte into final output"
                )
                nodes_made.append("CK_NOTE")
        except Exception:
            pass

        try: comp.EndUndo(True)
        except Exception: pass
        try: comp.Unlock()
        except Exception: pass
        try: resolve.OpenPage("edit")
        except Exception: pass
        if mo:
            try: comp.SetActiveTool(mo)
            except Exception: pass
        log(f"Fusion comp built: {len(nodes_made)} nodes -- {', '.join(nodes_made)}")
        return True
    except Exception as e:
        log(f"Fusion comp error: {type(e).__name__}: {e}")
        try: comp.EndUndo(False)
        except Exception: pass
        try: comp.Unlock()
        except Exception: pass
        try: resolve.OpenPage("edit")
        except Exception: pass
        return False


# WHAT IT DOES: High-level wrapper called after PROCESS RANGE completes when the
#   user picked OutputMode=Fusion Comp. Resolves the source timeline item, picks
#   first-frame sidecar paths, and calls the appropriate Fusion comp builder.
# DEPENDS-ON: timeline global, _build_ck_fusion_comp / _build_ck_fusion_comp_v2.
# AFFECTS: Adds Fusion comp to the source clip (the clip the operator pointed at).
def _build_ck_fusion_comp_after_render(_timeline, source_track_idx, ofs, sam_ofs,
                                       sidecar_first_paths=None,
                                       render_in=None, render_dur=None):
    """Resolve the source clip and build the Fusion comp on it."""
    if not _timeline:
        log("Fusion comp after-render: no timeline global")
        return False
    tl_item = None
    try:
        if hasattr(_timeline, "GetCurrentVideoItem"):
            tl_item = _timeline.GetCurrentVideoItem()
    except Exception:
        tl_item = None
    if tl_item is None:
        try:
            items_on_track = _timeline.GetItemListInTrack("video", int(source_track_idx)) or []
            if items_on_track:
                tl_item = items_on_track[0]
        except Exception as _le:
            log(f"Fusion comp after-render: source-track lookup failed: {_le}")
    if tl_item is None:
        log("Fusion comp after-render: could not resolve source timeline item")
        return False
    if sidecar_first_paths and sidecar_first_paths.get("ck_rgb"):
        return _build_ck_fusion_comp_v2(tl_item, sidecar_first_paths,
                                         render_in=render_in, render_dur=render_dur)
    ck_first = (ofs[0] if ofs else None)
    sam_first = (sam_ofs[0] if sam_ofs else None)
    return _build_ck_fusion_comp(tl_item, ck_first,
                                 ck_alpha_first_path=None,
                                 sam_alpha_first_path=sam_first)


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
    cf, fps = get_current_frame_info()
    source_track = 1
    clip = None
    track_count = timeline.GetTrackCount("video")
    # v1.0 fix 2026-05-20: honor the user's selected clip first. Resolve's
    # GetCurrentVideoItem() returns the timeline item highlighted on the
    # timeline — that's what CK should key. Old bottom-up track walk would
    # pick a still photo on V1 when the green screen was on V2+.
    try:
        sel = timeline.GetCurrentVideoItem()
    except Exception:
        sel = None
    if sel is not None:
        # Locate which track the selected clip lives on (needed for source_track).
        for ti in range(1, track_count + 1):
            clips_on_track = timeline.GetItemListInTrack("video", ti) or []
            for c in clips_on_track:
                try:
                    same = (c.GetUniqueId() == sel.GetUniqueId())
                except Exception:
                    same = (c is sel)
                if same:
                    source_track = ti
                    clip = c
                    break
            if clip is not None:
                break
        if clip is None:
            # Selected item not found in track list (shouldn't happen) — use it anyway.
            clip = sel
    if clip is None:
        # Fallback: walk tracks bottom-up for clip at playhead.
        for ti in range(1, track_count + 1):
            clips_on_track = timeline.GetItemListInTrack("video", ti) or []
            for c in clips_on_track:
                if c.GetStart() <= cf < c.GetEnd():
                    source_track = ti
                    clip = c
                    break
            if clip:
                break
    if not clip: status("ERROR: No clip selected — click a clip in the timeline first"); return
    _is_ramped, _ramp_reason = _is_clip_retimed(clip, timeline_fps=fps)
    if _is_ramped:
        _warn_speed_ramp(_ramp_reason)
        return
    # v1.0 two-mask track stacking: never overwrite an existing higher track.
    # CK matte lands on max(source, highest_used) + 1, SAM matte on +2 (decided
    # in _do_import). source_track + 1 used to be the hardcode, which would
    # silently overwrite previous-run output sitting on V2 / V3.
    _highest_used = _highest_used_video_track(timeline)
    output_track = max(int(source_track), int(_highest_used)) + 1
    log(f"Source on V{source_track}, highest-used V{_highest_used} → CK output to V{output_track} (SAM sidecar on V{output_track + 1} when active)")
    cs, ce = clip.GetStart(), clip.GetEnd()
    in_f = frame_range["in_frame"] if frame_range["in_frame"] is not None else cs
    out_f = frame_range["out_frame"] if frame_range["out_frame"] is not None else ce
    in_f, out_f = max(in_f, cs), min(out_f, ce)
    if out_f <= in_f: status("Invalid range!"); return
    dur = out_f - in_f
    _fr_in_raw = frame_range.get("in_frame")
    _fr_out_raw = frame_range.get("out_frame")
    log(f"Range: in_f={in_f} out_f={out_f} dur={dur} cs={cs} ce={ce} frame_range_in={_fr_in_raw} frame_range_out={_fr_out_raw}")
    mpi = clip.GetMediaPoolItem()
    props = mpi.GetClipProperty() if mpi else {}
    fp = props.get("File Path", "")
    ss = clip.GetLeftOffset()
    log(f"Range: ss(LeftOffset)={ss} fp={fp[:80]}")
    try:
        _tl_start = timeline.GetStartFrame() if timeline else "N/A"
    except Exception:
        _tl_start = "err"
    _diag_early = f"cs={cs} ce={ce} ss={ss} in_f={in_f} tl_start={_tl_start} fp={fp}\n"
    try:
        Path(r"K:\C_key test\ck_diag.txt").write_text(_diag_early, encoding="utf-8")
    except Exception:
        pass
    settings = _merge_live_params(get_settings())
    _output_mode_pre = int(settings.get("output_mode", 0))
    if _output_mode_pre == 2:
        _est_mb_per_frame = 103
        _est_total_gb = (dur * _est_mb_per_frame) / 1024.0
        try:
            _out_drive = Path(items["OutputPath"].Text).resolve().drive or "C:"
            _free_bytes = shutil.disk_usage(_out_drive).free
            _free_gb = _free_bytes / (1024**3)
            if _est_total_gb > _free_gb * 0.9:
                status(f"ABORT: ~{_est_total_gb:.0f}GB needed, only {_free_gb:.0f}GB free on {_out_drive}")
                log(f"Disk space abort: {_est_total_gb:.1f}GB needed, {_free_gb:.1f}GB free")
                return
            log(f"Disk estimate: {_est_total_gb:.1f}GB for {dur} frames ({_free_gb:.1f}GB free)")
        except Exception as _dse:
            log(f"Disk space check skipped: {_dse}")
    import re as _re_cn2
    cn = _re_cn2.sub(r'[^\w.-]', '_', Path(fp).stem)
    # 2026-05-14: each PROCESS RANGE writes to its own timestamped subdir so the
    # new render never collides with PNG files locked by MediaPool from a prior
    # render. Resolve's MediaPool holds file handles on imported sequence clips;
    # overwriting in-place silently fails with PermissionError on Windows. Subdir
    # per render also preserves history for A/B comparison.
    import time as _t_od
    od = Path(items["OutputPath"].Text) / f"CK_{cn}" / _t_od.strftime("%Y%m%d_%H%M%S")
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
    _is_hevc_clip = _is_hevc_file(fp, mpi=mpi)
    if _is_hevc_clip:
        # v1.0 fix 2026-05-20: HEVC range export via PyAV. ~20-40x faster than the
        # Resolve still-export path (~0.05s/frame vs ~2s/frame). PyAV samples by
        # TIME so FPS conform is handled automatically (no broken src-frame math).
        _src_fps_range = _source_fps_from_props(props, fps)
        _source_t_start = (ss / _src_fps_range) + (max(0, in_f - cs) / float(fps))
        n_output = int(out_f - in_f)
        log(f"HEVC range detected — PyAV decode, source_t_start={_source_t_start:.3f}s, n={n_output} @ {fps}fps (src_fps={_src_fps_range})")
        status(f"Decoding HEVC range ({n_output} frames) via PyAV...")
        braw_frames_dir = _export_hevc_range_via_pyav(
            fp, _source_t_start, n_output, fps, status_cb=status,
        )
        log(f"PROBE A: PyAV returned braw_frames_dir={braw_frames_dir}")
        if braw_frames_dir is None:
            # PyAV failed — fall back to the original Resolve still-export path.
            log("PyAV HEVC decode failed — falling back to Resolve still export")
            status("PyAV failed — Resolve still-export fallback (slow)...")
            src_start = ss + (in_f - cs)
            src_end   = ss + (out_f - cs) + 1
            braw_frames_dir = _export_braw_range_to_frames(
                clip, src_start, src_end, timeline, in_f, fps,
                skip_braw_exe=True,
            )
            if braw_frames_dir is None:
                status("ERROR: HEVC range export failed — see log"); return
        log("PROBE B: about to glob TIFFs")
        n_tifs = len(sorted(Path(braw_frames_dir).glob("*.tif*")))
        log(f"PROBE C: glob done, n_tifs={n_tifs}")
        log(f"HEVC range export done: {n_tifs} TIFF frames in {Path(braw_frames_dir).name}")
    elif fp.lower().endswith(('.braw', '.cin', '.dng', '.ari')):
        src_start = ss + (in_f - cs)
        src_end   = ss + (out_f - cs) + 1  # +1: Resolve timeline end is exclusive
        log(f"BRAW detected — exporting source frames {src_start}-{src_end} to TIFF sequence...")
        status(f"Exporting BRAW range ({dur} frames) — please wait...")
        # Pass the CLIP (timeline item), not the MediaPoolItem.
        braw_frames_dir = _export_braw_range_to_frames(
            clip, src_start, src_end, timeline, in_f, fps,
            skip_braw_exe=False,
        )
        if braw_frames_dir is None:
            status("ERROR: BRAW range export failed — see log"); return
        n_tifs = len(sorted(Path(braw_frames_dir).glob("*.tif*")))
        log(f"BRAW range export done: {n_tifs} TIFF frames in {Path(braw_frames_dir).name}")
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
                            refiner_strength=settings["refiner_strength"], despeckle_enabled=False,
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
            sam_ofs = []  # v1.0 two-mask sidecar — populated when SAM is active
            _combined_ofs = []
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
                # Multi-object v0.8 — pass MASK 1 + MASK 2 click sets so SAM2
                # video propagation tracks both natively in one pass.
                _p1b = sam_points_per_obj[1]; _p2b = sam_points_per_obj[2]
                pos = _p1b["positive"]; neg = _p1b["negative"]
                pos2 = _p2b["positive"]; neg2 = _p2b["negative"]
                if (pos or neg or pos2 or neg2) and braw_frames_dir:
                    try:
                        _anchor_abs = _p1b["frame"]
                        _anchor_in_tif = (_anchor_abs - in_f) if _anchor_abs is not None else None
                        _anchor_abs2 = _p2b["frame"]
                        _anchor_in_tif2 = (_anchor_abs2 - in_f) if _anchor_abs2 is not None else None
                        status("SAM2: running video propagation for BRAW range...")
                        _braw_sam2_video_masks = run_sam2_video_propagation(
                            braw_frames_dir, 0, 0, 0, dur,
                            pos, neg, _anchor_in_tif,
                            pos_pts_obj2=pos2, neg_pts_obj2=neg2,
                            anchor_frame_obj2_abs=_anchor_in_tif2,
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
                        # Multi-object v0.8 — list of per-mask static gates.
                        _braw_sam2_gate = _load_per_object_sam2_gates(_shape_arr.shape, settings)
                        del _shape_arr, _shape_pil
                        if _braw_sam2_gate:
                            log(f"SAM2 static {len(_braw_sam2_gate)} per-object gate(s) loaded for BRAW — applying to all {dur} frames")
                        else:
                            log("SAM2 gate: not loaded (file missing or alpha_method mismatch)")
                    except Exception as _ge:
                        log(f"SAM2 gate load failed: {_ge}")

            _sidecar_first_paths = {}
            for fidx, _buf in enumerate(_braw_tif_buffers):
                if processing_cancelled:
                    log(f"Cancelled at frame {pr}/{dur}")
                    status(f"CANCELLED — {pr} frames saved")
                    break
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
                if mt is not None:
                    try:
                        # apply_chroma_kill_to_matte is imported at module level (line 193).
                        # Inline 'from X import Y' inside on_process_range used to bind Y as
                        # a LOCAL of on_process_range; the nested _run() then saw Y as a
                        # closure variable that was unbound when BRAW sync didn't execute
                        # the import (non-BRAW renders produced 21x "cannot access free
                        # variable" errors per range). Use the module-level binding only.
                        if _CK_CHROMA_KILL_ENABLED:
                            mt = apply_chroma_kill_to_matte(mt, fr, settings.get("screen_type", "green"))
                    except Exception as _ckm_e: log(f"chroma kill failed (non-fatal): {_ckm_e}")
                # v1.0 TWO-MASK MODE — CK matte unchanged, SAM matte saved as
                # a separate alpha-only sidecar PNG. The user composites the
                # two in their host (Fusion / AE) using the matte they need.
                # CK matte = mt unchanged below. SAM matte built here when a
                # video-prop or static gate exists for this frame.
                _sam_matte_v1 = None
                _sam_union = None  # raw binary union, fed to 9093bb8 merge below
                if mt is not None and not bool(settings.get("sam2_bypass", False)):
                    # Option C — feed CONTINUOUS soft gates into process_sam_matte.
                    # binarize_sam_silhouette would collapse the 2-4 px soft band
                    # the saturation ramp produces; the contour bumpiness from
                    # 2026-05-09 came from binarising before the merge / matte.
                    # union_binary_silhouettes works on soft input (np.maximum).
                    # 2026-05-14: inline import REMOVED — was binding both names as
                    # locals of on_process_range. Inner _run() captured them via
                    # closure but the import only ran on BRAW path, so non-BRAW
                    # PROCESS RANGE raised UnboundLocalError inside _run's SAM
                    # matte block. Use the module-level imports from line 192-197.
                    _gates_for_sam = []
                    _obj_ids = []
                    if _braw_sam2_video_masks and fidx in _braw_sam2_video_masks:
                        _per = _braw_sam2_video_masks[fidx]
                        if isinstance(_per, dict):
                            _items = list(_per.items())
                        elif isinstance(_per, (list, tuple)):
                            _items = list(enumerate(_per, start=1))
                        else:
                            _items = [(1, _per)]
                        for _oid, _gm in _items:
                            if _oid == 1 and bool(settings.get("mask1_bypass", False)):
                                continue
                            if _oid == 2 and bool(settings.get("mask2_bypass", False)):
                                continue
                            _gates_for_sam.append(np.asarray(_gm, dtype=np.float32))
                            _obj_ids.append(_oid)
                    elif _braw_sam2_gate:
                        if isinstance(_braw_sam2_gate, dict):
                            _items = list(_braw_sam2_gate.items())
                        elif isinstance(_braw_sam2_gate, (list, tuple)):
                            _items = list(enumerate(_braw_sam2_gate, start=1))
                        else:
                            _items = [(1, _braw_sam2_gate)]
                        for _oid, _g in _items:
                            if _oid == 1 and bool(settings.get("mask1_bypass", False)):
                                continue
                            if _oid == 2 and bool(settings.get("mask2_bypass", False)):
                                continue
                            _gates_for_sam.append(np.asarray(_g, dtype=np.float32))
                            _obj_ids.append(_oid)
                    if _gates_for_sam:
                        _sam_union = union_binary_silhouettes(_gates_for_sam)
                        _sam_matte_v1 = process_sam_matte(
                            _sam_union,
                            margin_px=float(settings.get("sam2_margin", 0)),
                            softness_sigma=float(settings.get("sam2_soften", 0)),
                            fill_kernel_px=int(settings.get("fill_holes", 0)),
                        )
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
                    _output_mode = int(settings.get("output_mode", 0))
                    # Editable Layers (Fusion Comp): write sidecars + clean matte.
                    if _output_mode == 2:
                        _sc_paths = _write_fusion_sidecars(
                            fg, mt, _sam_union, fr, settings, od, cn, pr)
                        if not _sidecar_first_paths and _sc_paths:
                            _sidecar_first_paths = dict(_sc_paths)
                        if _sc_paths.get("ck_rgb"):
                            ofs.append(_sc_paths["ck_rgb"])
                        if _sc_paths.get("sam_alpha"):
                            sam_ofs.append(_sc_paths["sam_alpha"])
                        _mt_clean = mt[:, :, 0] if len(mt.shape) == 3 else mt
                        if _sam_union is not None:
                            try:
                                _mt_clean = merge_ck_with_sam_active(
                                    _mt_clean, _sam_union, source_rgb=fr,
                                    proximity_px=int(settings.get("edge_guard_px", 7)))
                            except Exception: pass
                            try: _mt_clean = _apply_shirt_rescue(_mt_clean, _sam_union, fr)
                            except Exception: pass
                            try:
                                if not bool(settings.get("garbage_bypass", False)):
                                    _ge_c = int(settings.get("garbage_expand_px", 0))
                                    _gf_c = int(settings.get("garbage_feather_px", 0))
                                    _gyt_c = int(settings.get("garbage_y_top_pct", 0))
                                    _gyb_c = int(settings.get("garbage_y_bot_pct", 100))
                                    if _ge_c > 0 or _gyt_c > 0 or _gyb_c < 100:
                                        from corridorkey_sam_merge import compute_garbage_matte as _cgm_c
                                        _gm_c = _cgm_c(_sam_union, expand_px=_ge_c, feather_px=_gf_c,
                                                        y_top_pct=_gyt_c, y_bot_pct=_gyb_c)
                                        if _gm_c is not None and _gm_c.shape[:2] == _mt_clean.shape[:2]:
                                            _mt_clean = (_mt_clean.astype(np.float32) * _gm_c).astype(_mt_clean.dtype)
                            except Exception: pass
                        _clean_p = od / f"CK_COMBINED_{cn}.{pr:06d}.png"
                        _m16 = (np.clip(_mt_clean, 0.0, 1.0) * 65535.0).astype(np.uint16)
                        cv2.imwrite(str(_clean_p), cv2.merge([_m16, _m16, _m16]))
                        if not _sidecar_first_paths.get("ck_combined"):
                            _sidecar_first_paths["ck_combined"] = str(_clean_p)
                    else:
                        _ext = _codec_extension(settings.get("output_codec", 0))
                        _content = int(settings.get("output_content", 0))
                        _write_ck = _content in (1, 2)
                        _write_sam = _content in (1, 3) and _sam_matte_v1 is not None
                        _write_combined = _content == 0
                        if _write_combined:
                            _mt_2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                            _mt_for_save = _mt_2d
                            if _sam_union is not None:
                                try:
                                    _mt_for_save = merge_ck_with_sam_active(
                                        _mt_2d, _sam_union, source_rgb=fr,
                                        proximity_px=int(settings.get("edge_guard_px", 7)),
                                    )
                                except Exception as _mge:
                                    log(f"BRAW Combined merge failed (CK alone): {_mge}")
                                    _mt_for_save = _mt_2d
                                try:
                                    _mt_for_save = _apply_shirt_rescue(_mt_for_save, _sam_union, fr)
                                except Exception as _sre:
                                    log(f"Shirt rescue failed (non-fatal): {_sre}")
                                try:
                                    if not bool(settings.get("garbage_bypass", False)):
                                        _ge_r = int(settings.get("garbage_expand_px", 0))
                                        _gf_r = int(settings.get("garbage_feather_px", 0))
                                        _gyt_r = int(settings.get("garbage_y_top_pct", 0))
                                        _gyb_r = int(settings.get("garbage_y_bot_pct", 100))
                                        if _ge_r > 0 or _gyt_r > 0 or _gyb_r < 100:
                                            from corridorkey_sam_merge import compute_garbage_matte as _cgm_r
                                            _gm_r = _cgm_r(_sam_union, expand_px=_ge_r, feather_px=_gf_r,
                                                           y_top_pct=_gyt_r, y_bot_pct=_gyb_r)
                                            if _gm_r is not None and _gm_r.shape[:2] == _mt_for_save.shape[:2]:
                                                if _mt_for_save.ndim == 3:
                                                    _mt_for_save = (_mt_for_save.astype(np.float32) * _gm_r[..., None]).astype(_mt_for_save.dtype)
                                                else:
                                                    _mt_for_save = (_mt_for_save.astype(np.float32) * _gm_r).astype(_mt_for_save.dtype)
                                                log(f"BRAW range: garbage matte applied (expand={_ge_r}, y={_gyt_r}-{_gyb_r})")
                                except Exception as _gm_e:
                                    log(f"BRAW range garbage matte failed (non-fatal): {_gm_e}")
                            op = od / f"CK_{cn}_{pr:06d}{_ext}"
                            save_output(fg, _mt_for_save, op, settings["export_format"], codec=settings.get("output_codec", 0))
                            ofs.append(str(op))
                        elif _write_ck:
                            op = od / f"CK_{cn}_{pr:06d}{_ext}"
                            save_output(fg, mt, op, settings["export_format"], codec=settings.get("output_codec", 0))
                            ofs.append(str(op))
                        if _write_sam:
                            sam_op = od / f"SAM_{cn}_{pr:06d}{_ext}"
                            save_alpha_only(_sam_matte_v1, sam_op, codec=settings.get("output_codec", 0))
                            sam_ofs.append(str(sam_op))
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
                _om_braw = int(settings.get("output_mode", 0))
                if _om_braw != 2:
                    status("Importing to MediaPool...")
                    _do_import({
                        "ofs": ofs, "output_track": output_track,
                        "source_track": source_track, "in_f": in_f, "settings": settings,
                        "sam_ofs": sam_ofs,
                    })
                if _om_braw == 2:
                    try:
                        status("Building Editable Layers comp on source clip...")
                        _ok = _build_ck_fusion_comp_after_render(
                            timeline, source_track, ofs, sam_ofs,
                            sidecar_first_paths=_sidecar_first_paths,
                            render_in=in_f, render_dur=dur)
                        status("DONE — CK layers on your clip. Play timeline." if _ok else "Fusion comp build failed — see log")
                    except Exception as _fce:
                        log(f"Fusion comp after-render error: {_fce}")
                        status("Fusion comp build error — see log")
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

        # Worker thread inherits no CUDA context — first CUDA op silently
        # deadlocks on Windows + Blackwell (RTX 5090) without explicit init.
        _torch_init = sys.modules.get("torch")
        if _torch_init is not None:
            try:
                _torch_init.cuda.set_device(0)
                _torch_init.cuda.init()
                _tlog("CUDA context init: device 0")
            except Exception as _ce:
                _tlog(f"CUDA init failed (non-fatal): {_ce}")

        ofs = []
        sam_ofs = []  # v1.0 two-mask: SAM matte sidecar PNGs
        _combined_ofs = []
        pr = 0
        st = time.time()
        try:
            # SAM2 video propagation — runs once up front, produces one mask per frame.
            # For BRAW, braw_frames_dir is the TIFF sequence directory — pass it so SAM2
            # gets full-chroma frames. Anchor frame shifts to TIFF index space (0-based).
            sam2_video_masks = {}
            # Multi-object v0.8 — pass per-mask click sets so SAM2 video tracks
            # MASK 1 + MASK 2 independently as obj_id=1 / obj_id=2.
            _p1r = sam_points_per_obj[1]; _p2r = sam_points_per_obj[2]
            _has_pts = bool(_p1r["positive"] or _p1r["negative"] or _p2r["positive"] or _p2r["negative"])
            if settings.get("alpha_method") == 1 and _has_pts:
                _tlog(f"SAM2 mode — running video propagation for full range... braw_frames_dir={braw_frames_dir!r}")
                if braw_frames_dir:
                    _anchor_abs = _p1r["frame"]
                    _anchor_in_tif = (_anchor_abs - in_f) if _anchor_abs is not None else None
                    _anchor_abs2 = _p2r["frame"]
                    _anchor_in_tif2 = (_anchor_abs2 - in_f) if _anchor_abs2 is not None else None
                    sam2_video_masks = run_sam2_video_propagation(
                        braw_frames_dir, 0, 0, 0, dur,
                        _p1r["positive"], _p1r["negative"], _anchor_in_tif,
                        pos_pts_obj2=_p2r["positive"], neg_pts_obj2=_p2r["negative"],
                        anchor_frame_obj2_abs=_anchor_in_tif2,
                    )
                else:
                    sam2_video_masks = run_sam2_video_propagation(
                        fp, ss, cs, in_f, out_f,
                        _p1r["positive"], _p1r["negative"], _p1r["frame"],
                        pos_pts_obj2=_p2r["positive"], neg_pts_obj2=_p2r["negative"],
                        anchor_frame_obj2_abs=_p2r["frame"],
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
            _sidecar_first_paths = {}

            # For BRAW: read TIFF files from braw_frames_dir (4:4:4, no seeking needed).
            # For normal files: seek with VideoCapture as before.
            cap = None
            braw_tif_files = []
            # 2026-05-14 hang diagnostic — write tag to a temp file BEFORE _ui_queue,
            # so we can see exactly where _run stops even if PollTimer never drains.
            _probes_enabled = os.environ.get("CK_DEBUG_PROBES", "").lower() in ("1", "true", "yes")
            def _probe(tag):
                if not _probes_enabled:
                    return
                try:
                    import tempfile as _tf, os as _os, time as _tm
                    _pp = _os.path.join(_tf.gettempdir(), "ck_run_probe.txt")
                    with open(_pp, "a", encoding="utf-8") as _pf:
                        _pf.write(f"{_tm.time():.3f}  {tag}\n")
                except Exception: pass
            _probe("AFTER_SAM2_PROP")
            if braw_frames_dir:
                # Use list pre-computed on main thread — avoids thread glob which triggers Defender directory scan.
                braw_tif_files = _braw_tif_files_precomputed
                _tlog(f"BRAW frames: {len(braw_tif_files)} TIFF files")
                _src_fps = fps
            else:
                # CAP_FFMPEG: MSMF deadlocks from daemon thread (see line 1209).
                _probe("BEFORE_VIDEOCAPTURE")
                cap = cv2.VideoCapture(fp, cv2.CAP_FFMPEG)
                _probe("AFTER_VIDEOCAPTURE")
                if not cap.isOpened():
                    _probe("CAP_NOT_OPENED")
                    _tstatus("Cannot open video"); return
                _probe("BEFORE_FPS")
                _src_fps = cap.get(cv2.CAP_PROP_FPS) or fps
                _probe("AFTER_FPS")
            _probe("BEFORE_FRAME_LOOP")
            for tf in range(in_f, out_f):
                if processing_cancelled:
                    _tlog(f"Cancelled at frame {pr}/{dur}")
                    _tstatus(f"CANCELLED — {pr} frames saved")
                    break
                _probe(f"ITER_TOP_{tf}")
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
                    _probe(f"BEFORE_CAP_SET_{tf}")
                    cap.set(cv2.CAP_PROP_POS_FRAMES, ss + (tf - cs))
                    _probe(f"AFTER_CAP_SET_{tf}")
                    ret, frame = cap.read()
                    _probe(f"AFTER_CAP_READ_{tf}")
                    if not ret:
                        _tlog(f"Read failed at frame {tf} — skipping")
                        continue
                range_idx = tf - in_f
                if frame.dtype == np.uint16:
                    frame = (frame >> 8).astype(np.uint8)
                elif frame.dtype != np.uint8:
                    frame = (frame.astype(np.float64) / float(np.iinfo(frame.dtype).max) * 255.0).clip(0, 255).astype(np.uint8)
                _probe(f"BEFORE_CHROMA_{tf}")
                chroma_float = chroma_hint_gen.generate_hint(frame).astype(np.float32) / 255.0
                _probe(f"AFTER_CHROMA_{tf}")
                ah = chroma_float
                fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                _probe(f"BEFORE_PROCESS_FRAME_{tf}")
                res = proc.process_frame(fr, ah, ps)
                fg, mt = res.get("fg"), res.get("alpha")
                if _despill_str > 0 and fg is not None:
                    fg = _cu.despill_opencv(fg, green_limit_mode="average", strength=_despill_str)
                if mt is not None and _CK_CHROMA_KILL_ENABLED:
                    try:
                        mt = apply_chroma_kill_to_matte(mt, fr, settings.get("screen_type", "green"))
                    except Exception as _ckm_e: log(f"chroma kill failed (non-fatal): {_ckm_e}")
                # v1.0 TWO-MASK MODE — CK matte (mt) is unchanged; SAM matte
                # is computed separately for sidecar export. Same per-mask
                # gather as before, but with bidirectional MARGIN + simple
                # FILL HOLES via process_sam_matte. CK keying output is the
                # master clip; SAM matte ships as a separate alpha sidecar.
                _sam_matte_v1 = None
                _sam_union = None  # raw binary union for 9093bb8 chroma-gated merge
                if mt is not None and not bool(settings.get("sam2_bypass", False)):
                    # 2026-05-14b: reverted gates-split. The two-list version
                    # caused PROCESS RANGE to hang silently after frame 1. Back
                    # to single-list (bypass applied to both combined and sidecar).
                    # Bug #6 (Both-mode loses SAM when BYPASS MASK 1 checked) will
                    # be fixed at the UI level instead (guard against bypass during
                    # render or warn user).
                    _gates_for_sam = []
                    _obj_ids = []
                    _probe(f"BEFORE_GATES_BUILD_{tf}")
                    if sam2_video_masks and range_idx in sam2_video_masks:
                        _per = sam2_video_masks[range_idx]
                        if isinstance(_per, dict):
                            _items = list(_per.items())
                        elif isinstance(_per, (list, tuple)):
                            _items = list(enumerate(_per, start=1))
                        else:
                            _items = [(1, _per)]
                        for _oid, _gm in _items:
                            if _oid == 1 and bool(settings.get("mask1_bypass", False)):
                                continue
                            if _oid == 2 and bool(settings.get("mask2_bypass", False)):
                                continue
                            _gates_for_sam.append(np.asarray(_gm, dtype=np.float32))
                            _obj_ids.append(_oid)
                    else:
                        # Fallback path — static per-object gates loaded lazily on
                        # first frame so we have real frame.shape for the resize.
                        if not _static_sam2_gate_loaded:
                            _static_sam2_gate = _load_per_object_sam2_gates(frame.shape, settings)
                            _static_sam2_gate_loaded = True
                            if _static_sam2_gate:
                                _tlog(f"SAM2 static {len(_static_sam2_gate)} per-object gate(s) — applying same mask to all range frames (no propagation)")
                        if _static_sam2_gate:
                            if isinstance(_static_sam2_gate, dict):
                                _items = list(_static_sam2_gate.items())
                            elif isinstance(_static_sam2_gate, (list, tuple)):
                                _items = list(enumerate(_static_sam2_gate, start=1))
                            else:
                                _items = [(1, _static_sam2_gate)]
                            for _oid, _g in _items:
                                if _oid == 1 and bool(settings.get("mask1_bypass", False)):
                                    continue
                                if _oid == 2 and bool(settings.get("mask2_bypass", False)):
                                    continue
                                _gates_for_sam.append(np.asarray(_g, dtype=np.float32))
                                _obj_ids.append(_oid)
                    _probe(f"AFTER_GATES_BUILD_{tf}")
                    if _gates_for_sam:
                        _probe(f"BEFORE_PROCESS_SAM_MATTE_{tf}")
                        try:
                            _sam_union = union_binary_silhouettes(_gates_for_sam)
                            _sam_matte_v1 = process_sam_matte(
                                _sam_union,
                                margin_px=float(settings.get("sam2_margin", 0)),
                                softness_sigma=float(settings.get("sam2_soften", 0)),
                                fill_kernel_px=int(settings.get("fill_holes", 0)),
                            )
                        except Exception as _psm_e:
                            _probe(f"PROCESS_SAM_MATTE_ERR_{tf}_{type(_psm_e).__name__}")
                            _tlog(f"process_sam_matte failed: {_psm_e}")
                _probe(f"AFTER_SAM_MATTE_{tf}")
                choke_px = int(settings.get("choke", 0))
                if choke_px > 0 and mt is not None:
                    k = choke_px * 2 + 1
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                    _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    mt = cv2.erode((_mt_c * 255).astype(np.uint8), kernel).astype(np.float32) / 255.0
                    log(f"Choke: {choke_px}px")
                # Despeckle for the rendered output (parity with viewer's render_composite).
                mt = _apply_despeckle_to_alpha(mt, settings)
                _probe(f"DESPECKLE_DONE_{tf}_fg={fg is not None}_mt={mt is not None}")
                if fg is not None and mt is not None:
                    _output_mode = int(settings.get("output_mode", 0))
                    _probe(f"OUTPUT_MODE_{tf}={_output_mode}")
                    # Editable Layers (Fusion Comp): write sidecars + clean matte.
                    if _output_mode == 2:
                        try:
                            _sc_paths = _write_fusion_sidecars(
                                fg, mt, _sam_union, fr, settings, od, cn, pr)
                            _probe(f"SIDECAR_OK_{tf}={list(_sc_paths.keys()) if _sc_paths else 'NONE'}")
                        except Exception as _swe:
                            _probe(f"SIDECAR_FAIL_{tf}={type(_swe).__name__}:{_swe}")
                            _tlog(f"Sidecar write FAILED frame {tf}: {_swe}")
                            _sc_paths = {}
                        if not _sidecar_first_paths and _sc_paths:
                            _sidecar_first_paths = dict(_sc_paths)
                        if _sc_paths.get("ck_rgb"):
                            ofs.append(_sc_paths["ck_rgb"])
                        if _sc_paths.get("sam_alpha"):
                            sam_ofs.append(_sc_paths["sam_alpha"])
                        _mt_clean = mt[:, :, 0] if len(mt.shape) == 3 else mt
                        if _sam_union is not None:
                            try:
                                _mt_clean = merge_ck_with_sam_active(
                                    _mt_clean, _sam_union, source_rgb=fr,
                                    proximity_px=int(settings.get("edge_guard_px", 7)))
                            except Exception: pass
                            try: _mt_clean = _apply_shirt_rescue(_mt_clean, _sam_union, fr)
                            except Exception: pass
                            try:
                                if not bool(settings.get("garbage_bypass", False)):
                                    _ge_c = int(settings.get("garbage_expand_px", 0))
                                    _gf_c = int(settings.get("garbage_feather_px", 0))
                                    _gyt_c = int(settings.get("garbage_y_top_pct", 0))
                                    _gyb_c = int(settings.get("garbage_y_bot_pct", 100))
                                    if _ge_c > 0 or _gyt_c > 0 or _gyb_c < 100:
                                        from corridorkey_sam_merge import compute_garbage_matte as _cgm_c
                                        _gm_c = _cgm_c(_sam_union, expand_px=_ge_c, feather_px=_gf_c,
                                                        y_top_pct=_gyt_c, y_bot_pct=_gyb_c)
                                        if _gm_c is not None and _gm_c.shape[:2] == _mt_clean.shape[:2]:
                                            _mt_clean = (_mt_clean.astype(np.float32) * _gm_c).astype(_mt_clean.dtype)
                            except Exception: pass
                        _clean_p = od / f"CK_COMBINED_{cn}.{pr:06d}.png"
                        _m16 = (np.clip(_mt_clean, 0.0, 1.0) * 65535.0).astype(np.uint16)
                        cv2.imwrite(str(_clean_p), cv2.merge([_m16, _m16, _m16]))
                        if not _sidecar_first_paths.get("ck_combined"):
                            _sidecar_first_paths["ck_combined"] = str(_clean_p)
                    else:
                        _codec = int(settings.get("output_codec", 0))
                        _ext = _codec_extension(_codec)
                        _content = int(settings.get("output_content", 0))
                        _write_ck = _content in (1, 2)
                        _write_sam = _content in (1, 3) and _sam_matte_v1 is not None
                        _write_combined = _content == 0
                        _fmt = settings["export_format"]
                        _m = mt[:, :, 0] if len(mt.shape) == 3 else mt
                        _m = np.clip(_m, 0.0, 1.0).astype(np.float32)
                        _fg_clip = np.clip(fg, 0.0, 1.0).astype(np.float32)
                        if _write_combined:
                            _probe(f"BEFORE_COMBINED_MERGE_{tf}")
                            if _sam_union is not None:
                                try:
                                    _m_combined = merge_ck_with_sam_active(
                                        _m, _sam_union, source_rgb=fr,
                                        proximity_px=int(settings.get("edge_guard_px", 7)),
                                    )
                                except Exception as _mge:
                                    _tlog(f"Combined merge failed at frame {pr} (CK alone): {_mge}")
                                    _probe(f"COMBINED_MERGE_ERR_{tf}_{type(_mge).__name__}")
                                    _m_combined = _m
                                try:
                                    _m_combined = _apply_shirt_rescue(_m_combined, _sam_union, fr)
                                except Exception as _sre:
                                    _tlog(f"Shirt rescue failed at frame {pr} (non-fatal): {_sre}")
                                try:
                                    if not bool(settings.get("garbage_bypass", False)):
                                        _ge_n = int(settings.get("garbage_expand_px", 0))
                                        _gf_n = int(settings.get("garbage_feather_px", 0))
                                        _gyt_n = int(settings.get("garbage_y_top_pct", 0))
                                        _gyb_n = int(settings.get("garbage_y_bot_pct", 100))
                                        if _ge_n > 0 or _gyt_n > 0 or _gyb_n < 100:
                                            from corridorkey_sam_merge import compute_garbage_matte as _cgm_n
                                            _gm_n = _cgm_n(_sam_union, expand_px=_ge_n, feather_px=_gf_n,
                                                           y_top_pct=_gyt_n, y_bot_pct=_gyb_n)
                                            if _gm_n is not None and _gm_n.shape[:2] == _m_combined.shape[:2]:
                                                if _m_combined.ndim == 3:
                                                    _m_combined = (_m_combined.astype(np.float32) * _gm_n[..., None]).astype(_m_combined.dtype)
                                                else:
                                                    _m_combined = (_m_combined.astype(np.float32) * _gm_n).astype(_m_combined.dtype)
                                                _tlog(f"Range garbage matte applied (expand={_ge_n}, y={_gyt_n}-{_gyb_n})")
                                except Exception as _gm_ne:
                                    _tlog(f"Range garbage matte failed (non-fatal): {_gm_ne}")
                            else:
                                _m_combined = _m
                            _m_active = _m_combined
                            _probe(f"AFTER_COMBINED_MERGE_{tf}")
                        else:
                            _m_active = _m
                        if _write_ck or _write_combined:
                            if _codec == 3:
                                if _fmt == 0:
                                    _fb = cv2.cvtColor(_fg_clip, cv2.COLOR_RGB2BGR)
                                    _img = cv2.merge([_fb[:,:,0], _fb[:,:,1], _fb[:,:,2], _m_active])
                                elif _fmt == 1:
                                    _img = _m_active
                                else:
                                    _img = cv2.cvtColor(_fg_clip, cv2.COLOR_RGB2BGR)
                            else:
                                if _codec == 0:
                                    _au = (_m_active * 255.0).astype(np.uint8)
                                    _fg_int = (_fg_clip * 255.0).astype(np.uint8)
                                else:
                                    _au = (_m_active * 65535.0).astype(np.uint16)
                                    _fg_int = (_fg_clip * 65535.0).astype(np.uint16)
                                if _fmt == 0:
                                    _fb = cv2.cvtColor(_fg_int, cv2.COLOR_RGB2BGR)
                                    _img = cv2.merge([_fb[:,:,0], _fb[:,:,1], _fb[:,:,2], _au])
                                elif _fmt == 1:
                                    _img = _au
                                else:
                                    _img = cv2.cvtColor(_fg_int, cv2.COLOR_RGB2BGR)
                            op = od / f"CK_{cn}_{pr:06d}{_ext}"
                            _probe(f"BEFORE_ENCODE_{tf}")
                            _ret, _buf = cv2.imencode(_ext, _img)
                            _probe(f"AFTER_ENCODE_{tf}")
                            if _ret:
                                _save_ok = False
                                try:
                                    try:
                                        if op.exists(): op.unlink()
                                    except Exception: pass
                                    with open(str(op), 'wb') as _sf:
                                        _sf.write(_buf.tobytes())
                                    _save_ok = True
                                except Exception as _se:
                                    _tlog(f"Save error {op}: {_se}")
                                    _probe(f"SAVE_ERROR_CK_{tf}_{type(_se).__name__}")
                                _probe(f"AFTER_QUEUE_PUT_{tf}")
                                if _save_ok:
                                    ofs.append(str(op))
                        if _write_sam:
                            sam_op = od / f"SAM_{cn}_{pr:06d}{_ext}"
                            _sam = np.clip(_sam_matte_v1, 0.0, 1.0).astype(np.float32)
                            if _codec == 3:
                                _sam_img = _sam
                            elif _codec == 0:
                                _sam_img = (_sam * 255.0).astype(np.uint8)
                            else:
                                _sam_img = (_sam * 65535.0).astype(np.uint16)
                            _ret_s, _buf_s = cv2.imencode(_ext, _sam_img)
                            if _ret_s:
                                _sam_ok = False
                                try:
                                    try:
                                        if sam_op.exists(): sam_op.unlink()
                                    except Exception: pass
                                    with open(str(sam_op), 'wb') as _sf:
                                        _sf.write(_buf_s.tobytes())
                                    _sam_ok = True
                                except Exception as _se:
                                    _tlog(f"Save error {sam_op}: {_se}")
                                    _probe(f"SAVE_ERROR_SAM_{tf}_{type(_se).__name__}")
                                if _sam_ok:
                                    sam_ofs.append(str(sam_op))
                    pr += 1
                    el = time.time() - st
                    fpsr = pr / el if el > 0 else 0
                    rem = (dur - pr) / fpsr if fpsr > 0 else 0
                    _tstatus(f"{pr}/{dur} ({fpsr:.1f}fps, {rem:.0f}s left)")
                    _tprogress(pr, dur)
                    if pr % 10 == 0: _tlog(f"{pr}/{dur}")
                    _probe(f"BEFORE_PROCESS_EVENTS_{tf}")
                    # Path B sync mode: drain _ui_queue + _save_queue via the
                    # main-thread PollTimer, and let Qt process the Cancel
                    # button click. Without this the UI freezes for the whole
                    # range render and CANCEL never fires.
                    try:
                        from PyQt5.QtWidgets import QApplication as _QApp
                        _QApp.processEvents()
                    except Exception:
                        try:
                            from PySide2.QtWidgets import QApplication as _QApp
                            _QApp.processEvents()
                        except Exception:
                            pass
                    _probe(f"AFTER_PROCESS_EVENTS_{tf}")
            if cap:
                cap.release()

            _probe(f"LOOP_DONE_ck={len(ofs)}_sam={len(sam_ofs)}")
            if ofs and not processing_cancelled:
                _tlog(f"Done: {len(ofs)} frames in {time.time()-st:.1f}s")
                _content_final = int(settings.get("output_content", 0))
                if _content_final in (1, 3) and not sam_ofs:
                    _tlog("WARNING: Both/SAM-only mode but 0 SAM frames produced. Check SAM2 anchor points are placed and SAM2 propagation ran.")
                _tstatus("Importing to MediaPool...")
                _tprogress(dur, dur)  # fill to 100% before hiding
                _ui_queue.put(("progress", -1))
                # 2026-05-14: call _do_import inline. PollTimer drain stops firing
                # once Fusion pauses timer dispatch on a long-blocking main-thread run.
                # We are already on the main thread; MediaPool API is main-thread-only
                # and that requirement is satisfied.
                _om_run = int(settings.get("output_mode", 0))
                if _om_run != 2:
                    try:
                        _do_import({
                            "ofs": ofs, "output_track": output_track,
                            "source_track": source_track, "in_f": in_f, "settings": settings,
                            "sam_ofs": sam_ofs,
                        })
                    except Exception as _ie:
                        _tlog(f"Import error: {_ie}")
                if _om_run == 2:
                    try:
                        _tstatus("Building Editable Layers comp on source clip...")
                        _ok = _build_ck_fusion_comp_after_render(
                            timeline, source_track, ofs, sam_ofs,
                            sidecar_first_paths=_sidecar_first_paths,
                            render_in=in_f, render_dur=dur)
                        _tstatus("DONE — CK layers on your clip. Play timeline." if _ok else "Fusion comp build failed — see log")
                    except Exception as _fce:
                        _tlog(f"Fusion comp after-render error: {_fce}")
                        _tstatus("Fusion comp build error — see log")
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
    # Path B (2026-05-13): kill the worker thread, run _run on the main thread.
    # Worker thread died silently before line 3633 across 16 hours of debug
    # (5 deployed thread-safety fixes did not crack it). Cause unknown; likely
    # a Fusion embedded-Python quirk with daemon-thread bootstrap. The BRAW
    # sync path right above this function has been proven to work synchronously.
    # UI freezes during the run; cancel button still works between frames via
    # QApplication.processEvents() inside the loop. Apr 20 handoff doc:
    # "PROCESS RANGE runs synchronously on the main thread. UI freezes
    #  during processing. That is expected and correct."
    _run()

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
    # Shutdown guard — once on_close fires, no more inference/IO from PollTimer ticks.
    # Without this, a tick mid-flight in _key_one_scrub_frame() holds the UI thread for
    # 2-5s and on_close's events queue behind it, deferring os._exit and hanging Resolve.
    if _shutting_down:
        return
    # 2026-05-21 heartbeat: refresh the instance lock's mtime so the next CK
    # launch's stale-lock check (15s) knows this CK is alive. Cheap — just an
    # os.utime touch.
    try:
        if _INSTANCE_LOCK.exists():
            os.utime(str(_INSTANCE_LOCK), None)
    except Exception:
        pass
    try:
        _hb_path = SESSION_DIR / "plugin_heartbeat"
        _hb_path.touch(exist_ok=True)
    except Exception:
        pass
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
# WHAT IT DOES: Returns the highest video track index in the active timeline that
#   currently contains at least one clip. Returns 0 if every track is empty.
# WHY: v1.0 two-mask placement must never overwrite an existing clip on a higher
#   track. Source on V1 + leftover output on V3 → next CK render lands on V4
#   (not V2). Same logic applies whether SAM matte sidecar comes along or not.
# DEPENDS-ON: timeline object exposes GetTrackCount("video") + GetItemListInTrack.
# AFFECTS: pure read; returns int.
def _highest_used_video_track(tl):
    try:
        track_count = tl.GetTrackCount("video")
    except Exception:
        return 0
    highest = 0
    for ti in range(1, int(track_count) + 1):
        try:
            clips = tl.GetItemListInTrack("video", ti) or []
        except Exception:
            clips = []
        if clips:
            highest = ti
    return highest


def _do_import(task):
    ofs = task.get("ofs", []) or []
    output_track = task["output_track"]
    source_track = task["source_track"]
    in_f = task["in_f"]
    settings = task["settings"]
    sam_ofs = task.get("sam_ofs", []) or []  # v1.0 two-mask: SAM matte sidecar PNGs
    # OutputContent-aware import: ofs / sam_ofs may be empty depending on the
    # user's Content choice. SAM-only mode lands the SAM matte on output_track
    # (not output_track+1) since there's no CK clip to sit above.
    if not ofs and not sam_ofs:
        status("Nothing to import — no frames written")
        return
    try:
        root = media_pool.GetRootFolder()
        ckb = None
        for f in root.GetSubFolderList():
            if f.GetName() == "CorridorKey": ckb = f; break
        if not ckb: ckb = media_pool.AddSubFolder(root, "CorridorKey")
        media_pool.SetCurrentFolder(ckb)
        imp = media_pool.ImportMedia(ofs) if ofs else None
        if ofs and not imp:
            status("CK import failed — check MediaPool bin"); return
        if imp:
            log(f"Imported {len(imp)} items to MediaPool")
            _bypass_idt_on_imports(imp)
        # v1.0: also import the SAM matte sidecar sequence when present.
        sam_imp = None
        if sam_ofs:
            sam_imp = media_pool.ImportMedia(sam_ofs)
            if sam_imp:
                log(f"Imported {len(sam_imp)} SAM matte items to MediaPool")
                _bypass_idt_on_imports(sam_imp)
            else:
                log("SAM matte import returned nothing")
        if settings["output_mode"] in [0, 2]:
            # When no CK clip exists (SAM-only), place SAM directly on
            # output_track. When both, CK on output_track + SAM on +1.
            ck_track = output_track if imp else None
            sam_track = (output_track + 1) if (sam_imp and imp) else (output_track if sam_imp else None)
            tracks_needed = max(t for t in (ck_track, sam_track) if t is not None)
            current_tracks = timeline.GetTrackCount("video")
            while current_tracks < tracks_needed:
                timeline.AddTrack("video")
                current_tracks += 1
                log(f"Added video track V{current_tracks}")
            result = None
            if imp and ck_track is not None:
                seq_item = imp[0]
                _label = "Combined (CK x SAM)" if int(settings.get("output_content", 0)) == 0 else "CK matte"
                log(f"Placing {_label} on V{ck_track} — frames 0-{len(ofs)-1}")
                ci_list = [{"mediaPoolItem": seq_item, "startFrame": 0, "endFrame": len(ofs) - 1,
                            "trackIndex": ck_track, "recordFrame": int(in_f), "mediaType": 1}]
                result = media_pool.AppendToTimeline(ci_list)
                log(f"AppendToTimeline (CK/Combined) result: {result}")
            sam_result = None
            if sam_imp and sam_track is not None:
                sam_seq = sam_imp[0]
                log(f"Placing SAM matte on V{sam_track} — frames 0-{len(sam_ofs)-1}")
                sam_ci = [{"mediaPoolItem": sam_seq, "startFrame": 0, "endFrame": len(sam_ofs) - 1,
                           "trackIndex": sam_track, "recordFrame": int(in_f), "mediaType": 1}]
                sam_result = media_pool.AppendToTimeline(sam_ci)
                log(f"AppendToTimeline (SAM) result: {sam_result}")
            if result or sam_result:
                if items["DisableTrack1"].Checked:
                    timeline.SetTrackEnable("video", source_track, False)
                    log(f"V{source_track} hidden — press D in timeline to re-enable source clip")
                _parts = []
                if result and ofs: _parts.append(f"{len(ofs)} CK frames on V{ck_track}")
                if sam_result and sam_ofs: _parts.append(f"SAM matte on V{sam_track}")
                status("DONE! " + " + ".join(_parts))
            else:
                status("Timeline place failed — clips are in MediaPool")
        else:
            _parts = []
            if ofs: _parts.append(f"{len(ofs)} CK frames")
            if sam_ofs: _parts.append(f"{len(sam_ofs)} SAM matte frames")
            status(" + ".join(_parts) + " in MediaPool")
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
    global _viewer_proc, _scrubber_proc, processing_cancelled, _range_running, _shutting_down
    # Shutdown sentinel FIRST — guards PollTimer (and any other re-entry point) from running
    # inference/IO while on_close tears down. Must precede every other line in this function.
    _shutting_down = True
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
    # Kill viewer process tree FIRE-AND-FORGET — synchronous taskkill with timeout=5
    # blocks the UI thread up to 10s (5s per subprocess). Spawn daemon threads instead so
    # the kill happens but the close handler returns immediately to reach os._exit below.
    import subprocess as _sp
    def _kill_proc_async(_pid):
        try: _sp.run(["taskkill", "/F", "/T", "/PID", str(_pid)],
                     stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=5,
                     creationflags=_sp.CREATE_NO_WINDOW)
        except Exception: pass
    for _proc in [_viewer_proc, _scrubber_proc]:
        if _proc is not None:
            try:
                _t = threading.Thread(target=_kill_proc_async, args=(_proc.pid,), daemon=True)
                _t.start()
            except Exception: pass
    _viewer_proc = None
    _scrubber_proc = None
    # NOTE: cached_processor["proc"] is intentionally NOT cleared here. Setting it to None
    # triggers PyTorch's Tensor.__del__ → CUDA buffer release on the MAIN thread, which can
    # pin for 5-30s before os._exit fires. Windows reclaims GPU memory on process death
    # regardless; skip the early dereference and let os._exit kill the process clean.
    try: _INSTANCE_LOCK.unlink(missing_ok=True)
    except: pass
    # Exit immediately — do NOT call disp.ExitLoop() and wait for RunLoop to unwind.
    # When Resolve is mid-shutdown, win.Hide() (called after RunLoop returns) blocks
    # indefinitely on a window with no valid Fusion context, causing the Task Manager hang.
    # os._exit skips all Python/CUDA finalizers — Windows reclaims GPU memory on process death.
    os._exit(0)

# WHAT IT DOES: OS signal handler. Resolve sends SIGTERM/SIGBREAK on quit BEFORE the
# UIDispatcher fires the window Close event. Without this handler, CK only sees the close
# via win.On.CK.Close — which Resolve may never fire on a hard quit. Catching the signal
# gives CK a chance to release subprocesses and die fast before Resolve TerminateProcesses us.
def _on_shutdown_signal(_signum, _frame):
    global _shutting_down
    _shutting_down = True
    try: _cleanup_on_exit()
    except Exception: pass
    os._exit(0)
for _sig_name in ("SIGTERM", "SIGBREAK", "SIGINT"):
    try:
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            signal.signal(_sig, _on_shutdown_signal)
    except Exception: pass

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
    try:
        import subprocess as _sp_kv
        _pf = SESSION_DIR / "viewer.pid"
        if _pf.exists():
            _old = int(_pf.read_text().strip())
            _sp_kv.run(["taskkill", "/F", "/T", "/PID", str(_old)],
                       stdout=_sp_kv.DEVNULL, stderr=_sp_kv.DEVNULL, timeout=3,
                       creationflags=_sp_kv.CREATE_NO_WINDOW)
    except Exception:
        pass
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
# win.Hide() removed — when RunLoop returns due to Resolve shutdown (not user click), the
# Fusion context is already torn down and win.Hide() deadlocks against an invalid handle,
# preventing os._exit below from running. RunLoop returning is sufficient signal to die.
# Force-exit immediately after the event loop ends.
# Python's normal shutdown runs CUDA/torch finalizers which block for 30-60 seconds
# on Windows with a GPU model loaded — that's why Resolve hangs and needs End Task.
# Windows reclaims all GPU memory when the process dies; skipping finalizers is safe.
os._exit(0)
