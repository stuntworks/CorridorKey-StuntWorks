# Last modified: 2026-04-23 | Change: BRAW fallback rewired — SetCurrentTimecode seek replaces per-frame temp timeline creation (no mouse steal/blink); _export_braw_range_to_frames gains timeline/in_f/fps params; OOM fix (on-demand TIFF decode) | Full history: git log
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
                        capture_output=True, timeout=3
                    )
                except Exception:
                    pass
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
        ui.Label({"Text": "Mask Mode:", "Weight": 0}),
        ui.ComboBox({"ID": "AlphaMethod", "Weight": 2}),
        ui.Label({"Text": "Screen:", "Weight": 0}),
        ui.ComboBox({"ID": "ScreenType", "Weight": 2}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 8}, [
        ui.Label({"Text": "Refiner:", "Weight": 0}),
        ui.SpinBox({"ID": "RefinerStrength", "Minimum": 0, "Maximum": 100, "Value": 100, "Weight": 1,
                    "StyleSheet": "background: #222; color: #ccc; border: 1px solid #444; border-radius: 3px;"}),
        ui.Label({"Text": "%  (edge detail — re-key required when changed)", "Weight": 2,
                  "StyleSheet": "color: #556; font-size: 10px;"}),
    ]),
    ui.HGroup({"Weight": 0, "Spacing": 8}, [
        ui.Label({"Text": "SAM2 Margin:", "Weight": 0, "StyleSheet": "color: #aaa; font-size: 11px;"}),
        ui.SpinBox({"ID": "Sam2Margin", "Minimum": 0, "Maximum": 80, "Value": 20, "Weight": 1,
                    "StyleSheet": "background: #222; color: #ccc; border: 1px solid #444; border-radius: 3px;"}),
        ui.Label({"Text": "px  (expand SAM2 mask so keyer handles edges)", "Weight": 2,
                  "StyleSheet": "color: #556; font-size: 10px;"}),
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
        ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable source clip after processing  (uncheck to leave source visible)", "Checked": True,
                    "StyleSheet": "color: #aaa; font-size: 11px;"}),
    ]),

    ui.VGap(2),
    ui.Label({"ID": "Status", "Text": "Ready", "Weight": 0, "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 14px; font-weight: bold;"}),
    ui.VGap(4),
    ui.Label({"ID": "Progress", "Text": "", "Weight": 0,
        "StyleSheet": "background: #111; border: 1px solid #333; border-radius: 4px; min-height: 12px; max-height: 12px;"}),
    ui.VGap(2),
    ui.HGroup({"Weight": 0, "Spacing": 5}, [
        ui.Button({"ID": "ShowPreview", "Text": "PREVIEW", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a3a4a; color: #5df; font-weight: bold; border-radius: 5px; padding: 6px; border: 1px solid #5df; }"}),
        ui.Button({"ID": "ProcessFrame", "Text": "SINGLE FRAME", "Weight": 1, "StyleSheet": "QPushButton { background-color: #1a3a5a; color: #5af; font-weight: bold; border-radius: 5px; padding: 6px; border: 1px solid #5af; }"}),
    ]),
    ui.Label({"Text": "SAM2 garbage matte: click PREVIEW → CLICK TO MASK in viewer", "Weight": 0,
              "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #556; font-size: 10px;"}),
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
        "alpha_method": items["AlphaMethod"].CurrentIndex,
        "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
        "despill_strength": 0.5,    # viewer-owned; overridden by _merge_live_params
        "refiner_strength": max(0.0, min(1.0, int(items["RefinerStrength"].Value) / 100.0)),
        "despeckle_enabled": True,  # viewer-owned; overridden by _merge_live_params — default ON matches viewer checkbox default
        "despeckle_size": 400,      # viewer-owned; overridden by _merge_live_params
        "export_format": items["ExportFormat"].CurrentIndex,
        "output_mode": items["OutputMode"].CurrentIndex,
        "sam2_margin": int(items["Sam2Margin"].Value),
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


# WHAT IT DOES: Expands a binary SAM2 mask outward by SAM2_MATTE_MARGIN pixels.
#   This safety buffer ensures the hard garbage-matte boundary sits outside the
#   actual silhouette, leaving the soft chroma-key edges untouched so the neural
#   keyer can refine them properly. Without dilation SAM2 clips the edges too tight.
# DEPENDS-ON: cv2, numpy
# AFFECTS: Every garbage-matte multiply in generate_alpha_hint() and the range loop.
# DANGER ZONE FRAGILE/MEDIUM: Increase margin on 4K+ footage (pixel count scales up).
#   Too small = edge clipping. Too large = garbage matte stops blocking junk BG.
SAM2_MATTE_MARGIN = 20  # default; overridden at runtime by Sam2Margin spinner

def _dilate_sam2_mask(mask_float32, margin=SAM2_MATTE_MARGIN):
    import cv2, numpy as np
    if margin <= 0:
        return mask_float32
    sz = margin * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (sz, sz))
    mask_u8 = (mask_float32 * 255).astype(np.uint8)
    dilated = cv2.dilate(mask_u8, kernel, iterations=1)
    return dilated.astype(np.float32) / 255.0


# WHAT IT DOES: Generates a chroma-key alpha hint for the neural keyer.
#   SAM2 is no longer applied here — it is applied as a POST-PROCESS gate on the
#   neural keyer's OUTPUT alpha (see _apply_sam2_output_gate). Applying SAM2 to
#   the input hint caused the neural network to interpret the hint incorrectly and
#   produce a dark/empty alpha. Traditional garbage mattes gate the OUTPUT, not the input.
# DEPENDS-ON: AlphaHintGenerator
# AFFECTS: Neural keyer input quality — this is the primary alpha signal into process_frame()
def generate_alpha_hint(frame, settings):
    import numpy as np, cv2
    from core.alpha_hint_generator import AlphaHintGenerator
    return AlphaHintGenerator(screen_type=settings["screen_type"]).generate_hint(frame)


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
            status("SAM2: forward pass...")
            log(f"SAM2 video: forward pass (anchor={anchor_rel} → frame {dur-1})")
            forward_count = 0
            for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(state):
                mask = (mask_logits[0] > 0.0).squeeze().cpu().numpy().astype(np.float32)
                masks[frame_idx] = mask
                forward_count += 1
                if frame_idx % 20 == 0:
                    log(f"SAM2 forward: frame {frame_idx}/{dur}")
                    status(f"SAM2 forward: {frame_idx}/{dur} frames")
            log(f"SAM2 forward pass done — {forward_count} masks")

            # --- Backward pass: anchor → first frame ---
            # Only needed when the anchor is not the first frame. Frames already
            # written by the forward pass are not overwritten (forward wins on overlap).
            if anchor_rel > 0:
                status("SAM2: backward pass...")
                log(f"SAM2 video: backward pass (anchor={anchor_rel} → frame 0)")
                backward_count = 0
                for frame_idx, _obj_ids, mask_logits in predictor.propagate_in_video(
                        state, reverse=True):
                    if frame_idx not in masks:
                        mask = (mask_logits[0] > 0.0).squeeze().cpu().numpy().astype(np.float32)
                        masks[frame_idx] = mask
                    backward_count += 1
                    if frame_idx % 20 == 0:
                        log(f"SAM2 backward: frame {frame_idx}")
                        status(f"SAM2 backward: {frame_idx} frames")
                log(f"SAM2 backward pass done — {backward_count} frames visited, "
                    f"{sum(1 for k in masks if k < anchor_rel)} new masks added")
            else:
                log("SAM2 video: anchor is frame 0 — backward pass skipped")

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
def _export_braw_range_to_frames(mpi, src_start, src_end, timeline, in_f, fps):
    import time as _t2, tempfile as _tf, shutil as _sh
    import traceback as _tb_fb
    if mpi is None:
        log("BRAW range export: mpi is None"); return None

    # Fast path: direct SDK decode via braw-decode.exe — no Resolve render queue needed.
    exe_result = None
    braw_fp = ""
    try:
        props = mpi.GetClipProperty()
        braw_fp = (props.get("File Path") or props.get("Clip Path") or "") if props else ""
        if not braw_fp:
            status("ERROR: Cannot read BRAW file path from Resolve. Check media is online.")
            log("BRAW range export: cannot get file path from mpi")
            return None
        exe_result = _try_braw_decode_exe(braw_fp, src_start, src_end)
    except Exception as _ep:
        log(f"BRAW range export: exe path exception: {_ep}")
        # Fall through to the Resolve still-export fallback below.

    if exe_result is not None:
        return exe_result

    # -----------------------------------------------------------------------
    # FALLBACK: Resolve ExportCurrentFrameAsStill — seek current timeline, no blink.
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
            timeline.SetCurrentTimecode(tc_str)
            ok = project.ExportCurrentFrameAsStill(out_path)
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
                    # BRAW (and other camera-raw formats) cannot be decoded by OpenCV
                    if fp.lower().endswith(('.braw', '.cin', '.dng', '.ari')):
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
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0, refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"], despeckle_size=settings["despeckle_size"])
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength}")
        res = proc.process_frame(fr, ah, ps)
        fg, mt = res.get("fg"), res.get("alpha")
        if fg is not None:
            try: log(f"FG stats — dtype:{fg.dtype} min:{float(fg.min()):.4f} max:{float(fg.max()):.4f} mean R:{float(fg[..., 0].mean()):.4f} G:{float(fg[..., 1].mean()):.4f} B:{float(fg[..., 2].mean()):.4f}")
            except Exception as _e: log(f"FG stat error: {_e}")
        # Apply SAM2 garbage matte to output alpha (post-keyer)
        if mt is not None:
            _gate = _load_sam2_output_gate(frame.shape, settings)
            if _gate is not None:
                _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                _gated = _mt2d * _gate
                mt = np.stack([_gated] * mt.shape[2], axis=2) if len(mt.shape) == 3 else _gated
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
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        if cached_processor["proc"] is None:
            log("Loading AI (first time)...")
            cached_processor["proc"] = CorridorKeyProcessor(device="cuda")
            log("Model loaded!")
        else:
            log("AI ready (cached)")
        proc = cached_processor["proc"]
        log("Alpha hint...")
        settings["_render_frame"] = cf
        ah = generate_alpha_hint(frame, settings)
        log("Processing...")
        fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ah = ah.astype(np.float32) / 255.0 if ah.dtype == np.uint8 else ah
        ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0, refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"], despeckle_size=settings["despeckle_size"])
        log(f"Settings: despeckle_enabled={ps.despeckle_enabled} despeckle_size={ps.despeckle_size} despill={ps.despill_strength} refiner={ps.refiner_strength}")
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
        # Apply SAM2 garbage matte to the neural keyer's OUTPUT alpha.
        # Doing it here (post-keyer) instead of on the input hint matches how the viewer works
        # and how traditional garbage mattes work — gate the output, not the input.
        if mt is not None:
            _gate = _load_sam2_output_gate(frame.shape, settings)
            if _gate is not None:
                import numpy as _np
                _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                _gated = _mt2d * _gate
                mt = np.stack([_gated] * mt.shape[2], axis=2) if len(mt.shape) == 3 else _gated
                log(f"SAM2 output gate applied — alpha mean {_mt2d.mean():.3f} -> {_gated.mean():.3f}")
        choke_px = int(settings.get("choke", 0))
        if choke_px > 0 and mt is not None:
            k = choke_px * 2 + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
            mt = cv2.erode((_mt_c * 255).astype(np.uint8), kernel).astype(np.float32) / 255.0
            log(f"Choke: {choke_px}px")
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
    braw_frames_dir = None
    if fp.lower().endswith(('.braw', '.cin', '.dng', '.ari')):
        src_start = ss + (in_f - cs)
        src_end   = ss + (out_f - cs) + 1  # +1: Resolve timeline end is exclusive
        log(f"BRAW detected — exporting source frames {src_start}-{src_end} to TIFF sequence...")
        status(f"Exporting BRAW range ({dur} frames) — please wait...")
        braw_frames_dir = _export_braw_range_to_frames(mpi, src_start, src_end, timeline, in_f, fps)
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
    else:
        log("AI ready (cached)")
    proc = cached_processor["proc"]
    ps = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=0.0,
                            refiner_strength=settings["refiner_strength"], despeckle_enabled=settings["despeckle_enabled"],
                            despeckle_size=settings["despeckle_size"])
    log(f"Settings: despill={ps.despill_strength} refiner={ps.refiner_strength} despeckle={ps.despeckle_enabled}")
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
                items["Progress"].StyleSheet = "background: #111; border: 1px solid #333; border-radius: 4px; min-height: 12px; max-height: 12px;"
                items["Progress"].Visible = True
            except Exception: pass
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
                status(f"{pr+1}/{dur}...")
                chroma_float = chroma_hint_gen.generate_hint(frame).astype(np.float32) / 255.0
                fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                res = proc.process_frame(fr, chroma_float, ps)
                fg, mt = res.get("fg"), res.get("alpha")
                if _despill_str > 0 and fg is not None:
                    fg = _cu.despill_opencv(fg, green_limit_mode="average", strength=_despill_str)
                choke_px = int(settings.get("choke", 0))
                if choke_px > 0 and mt is not None:
                    _k = choke_px * 2 + 1
                    _kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_k, _k))
                    _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    mt = cv2.erode((_mt_c * 255).astype(np.uint8), _kernel).astype(np.float32) / 255.0
                    log(f"Choke: {choke_px}px")
                if fg is not None and mt is not None:
                    op = od / f"CK_{cn}_{pr:06d}.png"
                    save_output(fg, mt, op, settings["export_format"])
                    ofs.append(str(op))
                del frame  # Release this frame's numpy array before the next decode
                pr += 1
                el = time.time() - st
                fpsr = pr / el if el > 0 else 0
                log(f"{pr}/{dur} ({fpsr:.1f}fps)")
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
                if mt is not None and sam2_video_masks and range_idx in sam2_video_masks:
                    _gate = _dilate_sam2_mask(sam2_video_masks[range_idx], margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
                    _mt2d = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    mt = _mt2d * _gate
                choke_px = int(settings.get("choke", 0))
                if choke_px > 0 and mt is not None:
                    k = choke_px * 2 + 1
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                    _mt_c = mt[:, :, 0] if len(mt.shape) == 3 else mt
                    mt = cv2.erode((_mt_c * 255).astype(np.uint8), kernel).astype(np.float32) / 255.0
                    log(f"Choke: {choke_px}px")
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
    # DANGER ZONE HIGH: The adaptive interval logic MUST live in finally: — if any
    # queue drain raises unexpectedly before reaching it, the timer stays at 500ms
    # forever (never backs off to 5000ms idle), sustaining ASIO interrupt pressure.
    try:
        while not _ui_queue.empty():
            try:
                kind, msg = _ui_queue.get_nowait()
                if kind == "log":
                    items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"
                elif kind == "status":
                    items["Status"].Text = msg
                elif kind == "progress":
                    if msg < 0:
                        try: items["Progress"].Visible = False
                        except Exception: pass
                    else:
                        try:
                            pct = max(0.0, min(1.0, msg / 100.0))
                            if pct >= 1.0:
                                ss = "background: #00ffff; border: 1px solid #333; border-radius: 4px; min-height: 12px; max-height: 12px;"
                            elif pct <= 0.0:
                                ss = "background: #111; border: 1px solid #333; border-radius: 4px; min-height: 12px; max-height: 12px;"
                            else:
                                ss = (f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                                      f"stop:0 #00cccc, stop:{pct:.3f} #00cccc, "
                                      f"stop:{pct:.3f} #1a1a1a, stop:1 #1a1a1a); "
                                      f"border: 1px solid #333; border-radius: 4px; min-height: 12px; max-height: 12px;")
                            items["Progress"].StyleSheet = ss
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
        # Processing start sites reset it back to 500ms so UI stays responsive during work.
        # DEPENDS-ON: _range_running, _ui_queue, _save_queue, _import_queue, items["PollTimer"]
        # AFFECTS: PollTimer.Interval (Fusion UIManager timer property)
        all_idle = (
            not _range_running
            and _ui_queue.empty()
            and _save_queue.empty()
            and _import_queue.empty()
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
    global _viewer_proc
    # Stop the PollTimer first — prevents it firing against a half-dead UI during teardown.
    try: items["PollTimer"].Stop()
    except Exception: pass
    # Kill viewer process tree — taskkill /F /T kills the viewer AND any SAM2 subprocesses
    # it spawned. Without /T, child processes survive and hold the GPU open, which keeps
    # Resolve's process manager waiting indefinitely (the "End Task" bug).
    if _viewer_proc is not None:
        import subprocess as _sp
        _pid = _viewer_proc.pid
        _viewer_proc = None
        try: _sp.run(["taskkill", "/F", "/T", "/PID", str(_pid)],
                     capture_output=True, timeout=5,
                     creationflags=_sp.CREATE_NO_WINDOW)
        except Exception: pass
    # Drop CUDA model reference immediately — do NOT call proc.cleanup() here because
    # CUDA teardown in a closing Resolve session blocks indefinitely on Windows.
    # Windows reclaims GPU memory when the process dies; we just need to die fast.
    try: cached_processor["proc"] = None
    except Exception: pass
    try: _INSTANCE_LOCK.unlink(missing_ok=True)
    except: pass
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
# Force-exit immediately after the event loop ends.
# Python's normal shutdown runs CUDA/torch finalizers which block for 30-60 seconds
# on Windows with a GPU model loaded — that's why Resolve hangs and needs End Task.
# Windows reclaims all GPU memory when the process dies; skipping finalizers is safe.
os._exit(0)
