# Last modified: 2026-04-14 | Change: Phase 2 — persistent viewer with stdin JSON listener, live slider re-key, 4 backgrounds, parent-PID watchdog
"""CorridorKey Preview Viewer — two modes.

MODE 1 (one-shot, back-compat with Resolve's existing flow):
    python preview_viewer.py '<json-paths>'
  Argv[1] is a JSON blob: {"original": "...", "foreground": "...", "matte": "...",
    "background": "..." (optional V1-below plate)}. Loads static images, shows
    Original + Composite/Foreground/Matte side-by-side. Exits when user closes.

MODE 2 (persistent, for live slider preview in AE/Premiere and — later — Resolve):
    python preview_viewer.py --persistent --session <dir> [--parent-pid N]
  Loads fg.png + alpha.png + meta.json from <dir>. Listens on stdin for newline-
  delimited JSON commands. On every "update" command, re-applies stage-2 post-
  processing (despill + despeckle) against the cached NN output and repaints.
  Neural net does NOT re-run — that's the whole point of the session cache.

WHAT IT DOES: Gives Berto a pop-out window that tracks slider drags in near
real-time (<200 ms) by caching the expensive NN output and running only the
cheap post-processing stage per slider tick.

DEPENDS-ON:
  - CorridorKey engine venv — PySide6, OpenCV, numpy, torch
  - Engine module CorridorKeyModule.core.color_utils (despill_opencv,
    clean_matte_opencv, create_checkerboard, composite_straight)
  - Session dir previously written by ae_processor.py's `cache` subcommand
    (fg.png 16-bit BGR, alpha.png 16-bit grayscale, meta.json)

AFFECTS: Opens a Qt window. Writes nothing to disk. Reads stdin in persistent mode.
"""
import sys
import os
import json
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6 import QtWidgets, QtGui, QtCore

# WHAT IT DOES: Installs diagnostic crash / exception loggers as early as possible.
#   faulthandler dumps Python tracebacks on native signals (SIGSEGV, stack overflow,
#   access violations that Windows translates into exit code 0xC0000409 etc). The
#   excepthook catches un-raised Python exceptions and writes them to a file in
#   %TEMP%\corridorkey_viewer_crash.log so a post-mortem is possible without a
#   debugger attached. This is what made the first stack-overrun crash traceable.
# DEPENDS-ON: faulthandler (Python stdlib), write access to %TEMP%.
# AFFECTS: on fatal errors, writes stderr-style traceback to the crash log.
def _install_crash_diagnostics():
    import faulthandler, tempfile, traceback
    log_path = Path(tempfile.gettempdir()) / "corridorkey_viewer_crash.log"
    try:
        _fh = open(log_path, "a", buffering=1, encoding="utf-8")
        _fh.write(f"\n===== preview_viewer start {__import__('datetime').datetime.now().isoformat()} =====\n")
        faulthandler.enable(_fh)
    except Exception:
        faulthandler.enable()  # best-effort: at least dump to stderr
    def _excepthook(exc_type, exc_value, exc_tb):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- uncaught exception {__import__('datetime').datetime.now().isoformat()} ---\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
        traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook


_install_crash_diagnostics()


# ===== Engine import =====
# WHAT IT DOES: Imports the engine's color_utils module, trying a direct import first
#   (assumes the launcher put the engine on sys.path) and falling back to resolving
#   CORRIDORKEY_ROOT the same way CorridorKey_Pro.py does.
# DEPENDS-ON: CORRIDORKEY_ROOT env var OR corridorkey_path.txt next to this script
#   OR the sibling CorridorKey folder OR ~/CorridorKey.
# AFFECTS: sys.path may gain the engine root.
def _import_color_utils():
    try:
        from CorridorKeyModule.core import color_utils as cu  # type: ignore
        return cu
    except ImportError:
        pass
    script_dir = Path(__file__).parent
    candidates = []
    env_root = os.environ.get("CORRIDORKEY_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    for probe in (script_dir, script_dir.parent):
        cfg = probe / "corridorkey_path.txt"
        if cfg.exists():
            try:
                candidates.append(Path(cfg.read_text().strip()))
            except Exception:
                pass
    candidates.append(script_dir.parent / "CorridorKey")
    candidates.append(Path(r"D:\New AI Projects\CorridorKey"))
    candidates.append(Path.home() / "CorridorKey")
    for p in candidates:
        if p and (p / "CorridorKeyModule" / "core" / "color_utils.py").is_file():
            sys.path.insert(0, str(p))
            from CorridorKeyModule.core import color_utils as cu  # type: ignore
            return cu
    raise ImportError(
        "Could not locate CorridorKey engine. Set CORRIDORKEY_ROOT or place "
        "corridorkey_path.txt next to preview_viewer.py."
    )


# ===== Image helpers =====
# WHAT IT DOES: Converts a uint8 HxWx3 RGB numpy array into a fully Qt-owned QPixmap.
#   The two-stage construction (tobytes -> QImage -> QImage.copy -> QPixmap.fromImage)
#   is deliberate and solves two real bugs observed on Windows + PySide6:
#     1) Passing numpy .data (a memoryview) to QImage() can leave stale bytes inside
#        a .copy() in some PySide6 builds — the copy is marked deep but the source
#        buffer lifetime still matters. Explicit tobytes() eliminates that class of
#        bug entirely because the Python `buf` binding keeps bytes alive across the
#        QImage.copy() call, and the deep copy then lives in Qt-managed memory.
#     2) Non-contiguous arrays silently produce garbage pixmaps. ascontiguousarray
#        guarantees C-order + tight stride that matches our `ch * w` bytesPerLine.
# DEPENDS-ON: numpy, PySide6 QImage/QPixmap, uint8 RGB input.
# AFFECTS: returns a QPixmap that can be safely assigned to any QLabel; source array
#   can be freed immediately after this function returns.
def _np_to_qpixmap(img_rgb_u8):
    arr = np.ascontiguousarray(img_rgb_u8, dtype=np.uint8)
    h, w, ch = arr.shape
    buf = arr.tobytes()
    qimg = QtGui.QImage(buf, w, h, ch * w, QtGui.QImage.Format_RGB888)
    owned = qimg.copy()   # deep copy while `buf` is still alive in this frame
    return QtGui.QPixmap.fromImage(owned)


def _read_png_any_depth(path):
    """Read PNG preserving bit depth (8 or 16)."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return img


def _to_float01(img):
    """Normalize 8/16-bit image to float32 in [0,1]."""
    if img.dtype == np.uint16:
        return img.astype(np.float32) / 65535.0
    if img.dtype == np.uint8:
        return img.astype(np.float32) / 255.0
    return img.astype(np.float32)


# ===== Persistent session state =====
class Session:
    """Holds cached NN output for one keyed frame. Reused across slider updates."""

    # WHAT IT DOES: Loads fg.png + alpha.png + optional V1 underlay + meta.json from the
    #   session dir. The foreground is straight (unpremultiplied) RGB float32 in sRGB.
    # DEPENDS-ON: ae_processor.py `cache` subcommand having written the session dir.
    # AFFECTS: nothing — pure read. Populates self.fg_rgb, self.alpha, self.v1_underlay,
    #   self.meta.
    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        fg_path = self.session_dir / "fg.png"
        alpha_path = self.session_dir / "alpha.png"
        if not fg_path.exists() or not alpha_path.exists():
            raise FileNotFoundError(
                f"Session dir missing fg.png or alpha.png: {self.session_dir}"
            )
        fg_bgr = _read_png_any_depth(fg_path)
        self.fg_rgb = cv2.cvtColor(_to_float01(fg_bgr), cv2.COLOR_BGR2RGB)
        alpha_img = _read_png_any_depth(alpha_path)
        if alpha_img.ndim == 3:
            alpha_img = cv2.cvtColor(alpha_img, cv2.COLOR_BGR2GRAY)
        self.alpha = _to_float01(alpha_img)
        v1_path = self.session_dir / "v1_underlay.png"
        self.v1_underlay = None
        if v1_path.exists():
            v1_bgr = _read_png_any_depth(v1_path)
            self.v1_underlay = cv2.cvtColor(_to_float01(v1_bgr), cv2.COLOR_BGR2RGB)
        meta_path = self.session_dir / "meta.json"
        self.meta = {}
        if meta_path.exists():
            try:
                self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    @property
    def shape_hw(self):
        return self.fg_rgb.shape[:2]

    # WHAT IT DOES: Re-reads fg.png + alpha.png from disk and updates self.fg_rgb
    #   and self.alpha in place. Called when the panel re-runs stage-1 (refiner
    #   change) so the viewer picks up the new neural-net output without a
    #   process restart.
    # DEPENDS-ON: session_dir/fg.png and alpha.png existing and being readable.
    # AFFECTS: self.fg_rgb, self.alpha. Raises if files are missing or unreadable
    #   (caller retries on next poll tick).
    def reload_pngs(self):
        fg_bgr = _read_png_any_depth(self.session_dir / "fg.png")
        if fg_bgr is None:
            raise IOError("fg.png unreadable")
        fg_rgb = cv2.cvtColor(_to_float01(fg_bgr), cv2.COLOR_BGR2RGB)
        alpha_img = _read_png_any_depth(self.session_dir / "alpha.png")
        if alpha_img is None:
            raise IOError("alpha.png unreadable")
        if alpha_img.ndim == 3:
            alpha_img = cv2.cvtColor(alpha_img, cv2.COLOR_BGR2GRAY)
        alpha = _to_float01(alpha_img)
        # Only assign after both reads succeed so a partial write doesn't leave
        # the viewer in an inconsistent state.
        self.fg_rgb = fg_rgb
        self.alpha = alpha


# ===== Post-processing pipeline =====
# WHAT IT DOES: Applies stage-2 post-proc (despill + despeckle) against the cached
#   NN output, then composites over the requested background. This is what runs on
#   every slider tick — no NN involvement.
# DEPENDS-ON: engine's color_utils (despill_opencv, clean_matte_opencv, create_checkerboard,
#   composite_straight), a loaded Session.
# AFFECTS: returns a fresh uint8 RGB image for display. Session state unchanged.
def render_composite(cu, session: Session, params: dict):
    despill_strength = float(params.get("despill", 1.0))
    despeckle_on = bool(params.get("despeckle", True))
    despeckle_size = int(params.get("despeckleSize", 400))
    background = str(params.get("background", "checker")).lower()

    h, w = session.shape_hw

    alpha = session.alpha.copy()
    if despeckle_on and despeckle_size > 0:
        # clean_matte_opencv expects area threshold in pixels
        alpha = cu.clean_matte_opencv(alpha, area_threshold=despeckle_size)

    fg_rgb = session.fg_rgb.copy()
    if despill_strength > 0:
        fg_rgb = cu.despill_opencv(fg_rgb, green_limit_mode="average", strength=despill_strength)

    # Background
    if background == "black":
        bg = np.zeros_like(fg_rgb)
    elif background == "white":
        bg = np.ones_like(fg_rgb)
    elif background == "v1" and session.v1_underlay is not None:
        v1 = session.v1_underlay
        if v1.shape[:2] != (h, w):
            v1 = cv2.resize(v1, (w, h))
        bg = v1
    else:  # checker (default) — or v1 fell through because no underlay
        bg = cu.create_checkerboard(w, h, checker_size=64)

    alpha_3 = np.stack([alpha, alpha, alpha], axis=2).astype(np.float32)
    comp = cu.composite_straight(fg_rgb.astype(np.float32), bg.astype(np.float32), alpha_3)
    comp_u8 = np.clip(comp * 255.0, 0, 255).astype(np.uint8)
    return comp_u8


# WHAT IT DOES: Converts a float32 alpha matte (0..1) to a uint8 3-channel RGB image
#   so it can be displayed in the Matte view mode.
# DEPENDS-ON: numpy, cv2. Input must be a 2-D float array.
# AFFECTS: returns a new uint8 RGB array. Input unchanged.
def alpha_to_rgb_u8(alpha_float):
    a = np.clip(alpha_float * 255.0, 0, 255).astype(np.uint8)
    return cv2.cvtColor(a, cv2.COLOR_GRAY2RGB)


# ===== Persistent viewer window =====
# WHAT IT DOES: Live-preview Qt widget. Two-up layout (original static, composite
#   live). Re-renders the right pane on every stdin update by running stage-2
#   post-proc against the cached NN output in the Session.
# DEPENDS-ON: PySide6, a loaded Session, engine color_utils, running Qt event loop.
# AFFECTS: creates a window. Uses _pending / _painting for drop-stale coalescing so
#   a fast slider drag never backs up the render queue.
class PersistentWindow(QtWidgets.QWidget):
    """Live-preview window. Loads a session, repaints on every stdin update."""

    # WHAT IT DOES: Two-up layout — Original (left, static) + Composite (right, live).
    #   View-mode buttons switch the right pane between Composite / Foreground / Matte.
    #   Background radio group switches the right pane background in Composite mode.
    # DEPENDS-ON: loaded Session, color_utils module.
    # AFFECTS: shown on-screen. Coalesces pending updates via _pending + QTimer.
    def __init__(self, cu, session: Session):
        super().__init__()
        self.cu = cu
        self.session = session
        self.setWindowTitle("CorridorKey Live Preview")
        self.setStyleSheet(_DARK_STYLE)
        # Stay above the NLE so the user never loses the preview behind the host
        # window. Editors bounce between tools; a hidden preview window defeats
        # the whole point of live slider feedback.
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)

        self._view_mode = "Composite"
        self._params = {
            "despill": 1.0,
            "despeckle": True,
            "despeckleSize": 400,
            "background": "checker",
        }
        # Drop-stale: if a new update comes in while we're painting, we only keep
        # the latest one. _pending is None when idle, or a dict when a render is
        # queued. _painting is True between compute and paint.
        self._pending = None
        self._painting = False

        # Zoom + pan state. Mouse wheel adjusts _zoom, drag-while-zoomed updates
        # _pan_x/_pan_y (fractional 0..1 center point). _paint_into crops the
        # full-res cached image to this window before scaling into the label.
        self._zoom = 1.0
        self._pan_x = 0.5
        self._pan_y = 0.5
        self._dragging = False
        self._drag_start = None
        self._drag_start_pan = (0.5, 0.5)

        h, w = session.shape_hw
        # Default display scale picks a size that fits on a 1366x768 laptop with
        # both panes + UI chrome — the window is resizable after launch so the
        # user can drag it larger on a big monitor.
        self.scale = min(1.0, 480.0 / w, 360.0 / h)
        self.disp_w = max(240, int(w * self.scale))
        self.disp_h = max(180, int(h * self.scale))
        self.original_u8 = np.clip(session.fg_rgb * 255.0, 0, 255).astype(np.uint8)
        self.original_scaled = cv2.resize(self.original_u8, (self.disp_w, self.disp_h))

        self._build_ui()
        self._render_now()
        # File-watcher bridge — polls <session_dir>/live_params.json every 50ms
        # and re-renders on mtime change. This replaces stdin IPC, which was
        # unreliable through CEP Node pipes on Windows (slider writes landed
        # in Node's internal buffer and never reached the Python child).
        self._live_params_path = self.session.session_dir / "live_params.json"
        self._live_params_mtime = 0.0
        self._live_watcher = QtCore.QTimer(self)
        self._live_watcher.setInterval(50)
        self._live_watcher.timeout.connect(self._poll_live_params)
        self._live_watcher.start()

    # WHAT IT DOES: Polls live_params.json in the session dir and dispatches to
    #   on_update() when the panel writes a new slider state. Silent on missing
    #   file (panel hasn't written yet) and on malformed JSON (partial write).
    # DEPENDS-ON: panel writing live_params.json atomically (tmp + rename), the
    #   session_dir being the same one passed on --session.
    # AFFECTS: self._live_params_mtime, self._params (via on_update).
    def _poll_live_params(self):
        # Check live_params.json for slider updates + rekeying completion.
        # NOTE: fg.png mtime polling was removed — reading a partially-written
        # PNG caused libpng crashes. Instead, the panel sends rekeying:false
        # AFTER cache finishes, so PNGs are guaranteed complete when we reload.
        try:
            mt = self._live_params_path.stat().st_mtime
        except FileNotFoundError:
            return
        except OSError:
            return
        if mt == self._live_params_mtime:
            return
        self._live_params_mtime = mt
        try:
            with open(self._live_params_path, "r", encoding="utf-8") as f:
                params = json.load(f)
        except Exception:
            # Partial write or transient — next tick will retry.
            return
        self.on_update(params)

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 8, 10, 8)

        # Title
        title = QtWidgets.QLabel("CORRIDORKEY LIVE PREVIEW")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet(
            "color: #00C853; font-size: 14px; font-weight: 700; "
            "letter-spacing: 1.5px; border: none; background: transparent;"
        )
        layout.addWidget(title)

        # View mode — pill buttons with mode colors when active
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(4)
        self._mode_colors = {
            "Original": "#607D8B",
            "Composite": "#9C27B0",
            "Foreground": "#2979FF",
            "Matte": "#FF9100",
        }
        self.mode_buttons = {}
        for mode, color in self._mode_colors.items():
            btn = QtWidgets.QPushButton(mode)
            btn.setStyleSheet(
                "background-color: #1e1e1e; color: #888; padding: 5px 12px; "
                "border-radius: 12px; font-size: 11px; font-weight: 600;"
            )
            btn.clicked.connect(lambda _=False, m=mode: self._set_view_mode(m))
            mode_row.addWidget(btn)
            self.mode_buttons[mode] = btn
        # Split toggle
        self._split_btn = QtWidgets.QPushButton("Split")
        self._split_btn.setCheckable(True)
        self._split_btn.setStyleSheet(
            "background-color: #1e1e1e; color: #555; padding: 5px 10px; "
            "border-radius: 12px; font-size: 10px;"
        )
        self._split_btn.clicked.connect(self._toggle_split)
        mode_row.addWidget(self._split_btn)
        layout.addLayout(mode_row)

        # Background — smaller pills
        bg_row = QtWidgets.QHBoxLayout()
        bg_row.setSpacing(3)
        bg_label = QtWidgets.QLabel("BG:")
        bg_label.setStyleSheet(
            "color: #555; border: none; background: transparent; font-size: 10px;"
        )
        bg_row.addWidget(bg_label)
        self.bg_buttons = {}
        for bg_name in ("checker", "black", "white", "v1"):
            btn = QtWidgets.QPushButton(bg_name.upper())
            btn.setCheckable(True)
            btn.setStyleSheet(
                "background-color: #1e1e1e; color: #666; padding: 3px 8px; "
                "border-radius: 10px; font-size: 9px;"
            )
            btn.clicked.connect(lambda _=False, n=bg_name: self._set_background(n))
            bg_row.addWidget(btn)
            self.bg_buttons[bg_name] = btn
        self.bg_buttons["checker"].setChecked(True)
        bg_row.addStretch()
        layout.addLayout(bg_row)

        # Image panes — single pane default, left_label hidden until Split on
        expanding = QtWidgets.QSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self._pane_row = QtWidgets.QHBoxLayout()
        self._pane_row.setSpacing(4)
        self.left_label = QtWidgets.QLabel()
        self.left_label.setAlignment(QtCore.Qt.AlignCenter)
        self.left_label.setSizePolicy(expanding)
        self.left_label.setMinimumSize(200, 150)
        self.left_label.setStyleSheet("background-color: #000; border: 1px solid #1e1e1e;")
        self.left_label.hide()
        self.right_label = QtWidgets.QLabel()
        self.right_label.setAlignment(QtCore.Qt.AlignCenter)
        self.right_label.setSizePolicy(expanding)
        self.right_label.setMinimumSize(320, 240)
        self.right_label.setStyleSheet("background-color: #000; border: 1px solid #1e1e1e;")
        self._pane_row.addWidget(self.left_label, 1)
        self._pane_row.addWidget(self.right_label, 1)
        layout.addLayout(self._pane_row, 1)

        # Processing overlay — shown during refiner re-key
        self._overlay = QtWidgets.QLabel("Re-keying...", self.right_label)
        self._overlay.setAlignment(QtCore.Qt.AlignCenter)
        self._overlay.setStyleSheet(
            "background-color: rgba(0,0,0,180); color: #00C853; "
            "font-size: 22px; font-weight: 700; border: none; border-radius: 2px;"
        )
        self._overlay.hide()

        # Full-res arrays for resize re-scaling without stage-2 re-render
        self._last_right_full = None
        self._place_original()

        # Status bar — monospace readout
        self.status = QtWidgets.QLabel("Ready")
        self.status.setStyleSheet(
            "color: #555; border: none; background: transparent; "
            "font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace; "
            "font-size: 10px;"
        )
        layout.addWidget(self.status)

        # Default size — single pane, compact
        self.resize(self.disp_w + 24, self.disp_h + 140)
        self.setMinimumSize(360, 300)

        # Highlight the default active mode button
        self._highlight_mode_button()

    # WHAT IT DOES: Updates mode button styles — active button gets its mode
    #   color, inactive buttons revert to default dark surface.
    # DEPENDS-ON: self._mode_colors, self._view_mode.
    # AFFECTS: button stylesheets only.
    def _highlight_mode_button(self):
        for mode, btn in self.mode_buttons.items():
            color = self._mode_colors[mode]
            if mode == self._view_mode:
                btn.setStyleSheet(
                    f"background-color: {color}; color: #fff; padding: 5px 12px; "
                    f"border-radius: 12px; font-size: 11px; font-weight: 600;"
                )
            else:
                btn.setStyleSheet(
                    "background-color: #1e1e1e; color: #888; padding: 5px 12px; "
                    "border-radius: 12px; font-size: 11px; font-weight: 600;"
                )

    # WHAT IT DOES: Toggles between single-pane (default) and two-up split view.
    # DEPENDS-ON: self.left_label visibility state.
    # AFFECTS: left_label visibility, window width, split button style.
    def _toggle_split(self, checked):
        if checked:
            self.left_label.show()
            self._split_btn.setStyleSheet(
                "background-color: #333; color: #e8e8e8; padding: 5px 10px; "
                "border-radius: 12px; font-size: 10px;"
            )
            self.resize(self.width() + self.disp_w, self.height())
        else:
            self.left_label.hide()
            self._split_btn.setStyleSheet(
                "background-color: #1e1e1e; color: #555; padding: 5px 10px; "
                "border-radius: 12px; font-size: 10px;"
            )
        self._repaint_both()

    # ===== Commands from stdin =====
    # WHAT IT DOES: Merges incoming params into the live params dict, then schedules
    #   a repaint. If already painting, the new update is queued as _pending and
    #   overwrites any prior pending update (drop-stale policy).
    # DEPENDS-ON: _render_now() for the actual work.
    # AFFECTS: self._params, self._pending, self._painting.
    @QtCore.Slot(dict)
    def on_update(self, params: dict):
        # Show/hide processing overlay for refiner re-key
        if params.get("rekeying") is True:
            self._overlay.setGeometry(0, 0, self.right_label.width(), self.right_label.height())
            self._overlay.show()
            self._overlay.raise_()
            return
        if params.get("rekeying") is False:
            self._overlay.hide()
            # Panel signals cache is done — PNGs are fully written, safe to read.
            try:
                self.session.reload_pngs()
                self.original_u8 = np.clip(
                    self.session.fg_rgb * 255.0, 0, 255
                ).astype(np.uint8)
                self._place_original()
                self._set_view_mode("Composite")
            except Exception:
                pass  # PNGs unreadable — stale view is better than a crash
        merged = dict(self._params)
        for k, v in params.items():
            if k in ("despill", "despeckle", "despeckleSize", "background"):
                merged[k] = v
        if self._painting:
            self._pending = merged
            return
        self._params = merged
        self._render_now()

    @QtCore.Slot(str)
    def on_reload(self, session_dir: str):
        try:
            new_session = Session(Path(session_dir))
        except Exception as e:
            self.status.setText(f"Reload failed: {e}")
            return
        self.session = new_session
        self.original_u8 = np.clip(new_session.fg_rgb * 255.0, 0, 255).astype(np.uint8)
        self._place_original()
        self._render_now()

    # ===== Render =====
    def _set_view_mode(self, mode):
        self._view_mode = mode
        self._highlight_mode_button()
        self._render_now()

    def _set_background(self, bg_name):
        for n, btn in self.bg_buttons.items():
            btn.setChecked(n == bg_name)
        self._params["background"] = bg_name
        self._render_now()

    # WHAT IT DOES: Runs the stage-2 post-proc pipeline, builds the right-pane image
    #   based on current view mode, and paints it. Resets _painting after drain so
    #   any pending update schedules itself via QTimer.singleShot.
    # DEPENDS-ON: render_composite(), color_utils, PySide6 event loop.
    # AFFECTS: right_label pixmap, status text, drains _pending.
    def _render_now(self):
        self._painting = True
        t0 = time.perf_counter()
        try:
            if self._view_mode == "Original":
                img = self.original_u8.copy()
            elif self._view_mode == "Composite":
                img = render_composite(self.cu, self.session, self._params)
            elif self._view_mode == "Foreground":
                # Pure despilled RGB with NO alpha blend — shows what the colour
                # data looks like independent of the matte. Use this to judge
                # despill quality ("is the skin still green-tinted?") without
                # the matte hiding problem areas.
                fg_rgb = self.session.fg_rgb.copy()
                despill_strength = float(self._params.get("despill", 1.0))
                if despill_strength > 0:
                    fg_rgb = self.cu.despill_opencv(
                        fg_rgb, green_limit_mode="average", strength=despill_strength
                    )
                img = np.clip(fg_rgb * 255.0, 0, 255).astype(np.uint8)
            else:  # Matte
                # Apply despeckle (matte is what it edits) and show as grayscale RGB
                params_for_matte = dict(self._params)
                alpha = self.session.alpha.copy()
                if params_for_matte.get("despeckle", True):
                    alpha = self.cu.clean_matte_opencv(
                        alpha, area_threshold=int(params_for_matte.get("despeckleSize", 400))
                    )
                img = alpha_to_rgb_u8(alpha)

            # Cache the full-res result so window resizes can rescale without
            # re-running stage-2. Then paint it at the current label size.
            self._last_right_full = img
            self._paint_right(img)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            # meanRGB is the cheapest possible proof that slider changes actually
            # move pixels. If these three numbers don't change between slider
            # positions, the render pipeline is broken (or the slider's effect is
            # smaller than rounding error). If they change but the image looks the
            # same, the display layer is broken. This is the first data point any
            # debugger reaches for when "nothing seems to be happening."
            mean_r, mean_g, mean_b = img.reshape(-1, 3).mean(axis=0)
            self.status.setText(
                f"Mode: {self._view_mode}  |  despill={self._params['despill']:.2f}  "
                f"despeckle={'on' if self._params['despeckle'] else 'off'}"
                f"@{self._params['despeckleSize']}  bg={self._params['background']}  "
                f"|  meanRGB=({mean_r:.1f},{mean_g:.1f},{mean_b:.1f})  "
                f"|  render {dt_ms:.0f} ms"
            )
        except Exception as e:
            self.status.setText(f"Render error: {e}")
        finally:
            self._painting = False
            if self._pending is not None:
                next_params = self._pending
                self._pending = None
                self._params = next_params
                # Defer through the event loop so UI stays responsive during drag.
                QtCore.QTimer.singleShot(0, self._render_now)

    # WHAT IT DOES: Scales a full-res uint8 RGB array to the current size of the
    #   given label (respecting aspect ratio via a letterbox bounding box) and
    #   installs it as the label's pixmap.
    # DEPENDS-ON: cv2, _np_to_qpixmap. Label must be sized already (called after
    #   layout has placed it).
    # AFFECTS: the label's displayed pixmap.
    # WHAT IT DOES: Crops full_img to a zoom-and-pan window, then scales into the
    #   given label preserving aspect ratio. Zoom 1.0 shows the whole image;
    #   larger zoom crops to a smaller source region. Pan coords are fractional
    #   (0.5 = centered) and represent the crop's center point.
    # DEPENDS-ON: self._zoom, self._pan_x, self._pan_y (set on wheel/drag).
    # AFFECTS: label's displayed pixmap only.
    def _paint_into(self, label, full_img):
        if full_img is None:
            return
        lw = max(1, label.width())
        lh = max(1, label.height())
        ih, iw = full_img.shape[:2]
        if iw == 0 or ih == 0:
            return
        zoom = getattr(self, "_zoom", 1.0)
        pan_x = getattr(self, "_pan_x", 0.5)
        pan_y = getattr(self, "_pan_y", 0.5)
        if zoom > 1.001:
            cw = max(1, int(iw / zoom))
            ch = max(1, int(ih / zoom))
            cx = int(pan_x * iw)
            cy = int(pan_y * ih)
            x0 = max(0, min(iw - cw, cx - cw // 2))
            y0 = max(0, min(ih - ch, cy - ch // 2))
            src = full_img[y0:y0 + ch, x0:x0 + cw]
        else:
            src = full_img
        sh, sw = src.shape[:2]
        aspect_src = sw / sh
        aspect_dst = lw / lh
        if aspect_src > aspect_dst:
            tw = lw
            th = max(1, int(lw / aspect_src))
        else:
            th = lh
            tw = max(1, int(lh * aspect_src))
        scaled = cv2.resize(src, (tw, th), interpolation=cv2.INTER_AREA)
        label.setPixmap(_np_to_qpixmap(scaled))

    def _paint_right(self, full_img):
        self._paint_into(self.right_label, full_img)

    def _place_original(self):
        if self.left_label.isVisible():
            self._paint_into(self.left_label, self.original_u8)

    # WHAT IT DOES: Re-paints both panes using current zoom/pan. Called after
    #   wheel scrolls or drag pans. No stage-2 re-render — just re-crops the
    #   cached full-res arrays. Guards left_label on visibility (hidden in
    #   single-pane mode).
    def _repaint_both(self):
        if self.left_label.isVisible() and self.left_label.width() > 1:
            self._place_original()
        if self._last_right_full is not None and self.right_label.width() > 1:
            self._paint_right(self._last_right_full)

    # WHAT IT DOES: Mouse wheel zooms in/out on the preview. Wheel up = zoom in.
    #   Clamps 1.0..10.0. Re-paints both panes on change.
    # DEPENDS-ON: Qt delivering wheelEvent to the window.
    # AFFECTS: self._zoom, both label pixmaps.
    def wheelEvent(self, event):
        try:
            delta = event.angleDelta().y()
        except Exception:
            delta = 0
        if delta == 0:
            return
        # One notch (120 units) = 1.25x zoom step. Feels natural.
        step = 1.25 if delta > 0 else 1.0 / 1.25
        new_zoom = max(1.0, min(10.0, self._zoom * step))
        if abs(new_zoom - self._zoom) < 1e-3:
            return
        self._zoom = new_zoom
        if self._zoom <= 1.001:
            # Snap pan back to center when fully zoomed out.
            self._pan_x = 0.5
            self._pan_y = 0.5
        self._repaint_both()
        event.accept()

    # WHAT IT DOES: Start pan-drag on mouse press while zoomed in.
    def mousePressEvent(self, event):
        if self._zoom > 1.001 and event.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self._drag_start = event.pos()
            self._drag_start_pan = (self._pan_x, self._pan_y)
            self.setCursor(QtCore.Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    # WHAT IT DOES: Update pan offset while dragging. Pan moves in image
    #   coordinates proportional to the visible crop size so drag feel is
    #   consistent across zoom levels.
    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_start is not None:
            dx = event.pos().x() - self._drag_start.x()
            dy = event.pos().y() - self._drag_start.y()
            # Inverted so dragging right shows content to the left of current view
            # (like grabbing and pulling a photo).
            w = max(1, self.right_label.width())
            h = max(1, self.right_label.height())
            self._pan_x = max(0.0, min(1.0, self._drag_start_pan[0] - dx / (w * self._zoom)))
            self._pan_y = max(0.0, min(1.0, self._drag_start_pan[1] - dy / (h * self._zoom)))
            self._repaint_both()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == QtCore.Qt.LeftButton:
            self._dragging = False
            self._drag_start = None
            self.setCursor(QtCore.Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    # WHAT IT DOES: On every window resize, rescale the original and the last
    #   rendered right-pane image to the new label sizes. No stage-2 re-render
    #   is needed — both full-res sources are already in memory.
    # DEPENDS-ON: _last_right_full being populated by the first _render_now call.
    # AFFECTS: left_label and right_label pixmaps.
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._repaint_both()
        # Keep overlay sized to image pane during window resize.
        if hasattr(self, '_overlay'):
            self._overlay.setGeometry(
                0, 0, self.right_label.width(), self.right_label.height()
            )


# ===== Stdin reader thread =====
# WHAT IT DOES: Background thread that reads newline-delimited JSON commands from
#   stdin and emits Qt signals on the main thread. Keeps the GUI event loop free
#   to paint while waiting on blocking stdin reads.
# DEPENDS-ON: sys.stdin (text mode) being connected to the launcher's pipe.
# AFFECTS: emits updateRequested / reloadRequested / quitRequested signals. Never
#   touches widgets directly (thread-safety). Exits on EOF or a "quit" command.
class StdinReader(QtCore.QThread):
    """Reads newline-delimited JSON from stdin and dispatches to the main thread."""

    updateRequested = QtCore.Signal(dict)
    reloadRequested = QtCore.Signal(str)
    quitRequested = QtCore.Signal()

    # WHAT IT DOES: Blocks on stdin.readline(). Each line is a JSON object with a
    #   "cmd" field: update | reload | quit. EOF triggers quit.
    # DEPENDS-ON: sys.stdin opened in text mode.
    # AFFECTS: emits signals into the Qt main thread. Never touches widgets directly.
    def run(self):
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    self.quitRequested.emit()
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cmd = msg.get("cmd", "").lower()
                if cmd == "update":
                    self.updateRequested.emit(
                        {k: v for k, v in msg.items() if k != "cmd"}
                    )
                elif cmd == "reload":
                    sd = msg.get("sessionDir")
                    if sd:
                        self.reloadRequested.emit(sd)
                elif cmd == "quit":
                    self.quitRequested.emit()
                    return
        except Exception:
            # A broken pipe or unexpected error is treated as EOF.
            self.quitRequested.emit()


# ===== Parent PID watchdog =====
# WHAT IT DOES: Polls the parent PID every second. If the parent is gone, exits —
#   this prevents orphan viewer processes when Resolve / AE / Premiere crashes
#   without firing a clean panel-close event.
# DEPENDS-ON: os.kill(pid, 0) on POSIX, GetExitCodeProcess on Windows.
# DANGER ZONE HIGH: This is the single invariant that prevents zombie viewers.
# breaks: if parent PID is reused by another process (rare but possible on heavy machines).
def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ===== One-shot mode (back-compat) =====
# WHAT IT DOES: Back-compat legacy viewer — loads pre-rendered original / foreground /
#   matte / optional-background PNGs and shows a fixed two-up composite. No stdin,
#   no sliders. Preserved so Resolve's existing fire-and-forget preview flow keeps
#   working until Phase 5 migrates it to persistent mode.
# DEPENDS-ON: the four paths already being written to disk by the caller (Resolve plugin).
# AFFECTS: opens a window, exits when the user clicks Close. Does not touch stdin.
class OneShotWindow(QtWidgets.QWidget):
    """Legacy static-image viewer. Preserved so Resolve's existing flow keeps working."""

    def __init__(self, paths):
        super().__init__()
        self.setWindowTitle("CorridorKey Preview")
        self.setStyleSheet(_DARK_STYLE)

        original_bgr = cv2.imread(paths["original"])
        fg_bgr = cv2.imread(paths["foreground"])
        matte = cv2.imread(paths["matte"], cv2.IMREAD_GRAYSCALE)
        if matte is None:
            matte = np.full(original_bgr.shape[:2], 255, dtype=np.uint8)

        original_rgb = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2RGB)
        fg_rgb = cv2.cvtColor(fg_bgr, cv2.COLOR_BGR2RGB)

        alpha3 = np.stack([matte] * 3, axis=2).astype(np.float32) / 255.0
        if "background" in paths and os.path.exists(paths["background"]):
            bg_rgb = cv2.cvtColor(cv2.imread(paths["background"]), cv2.COLOR_BGR2RGB)
            if bg_rgb.shape[:2] != original_rgb.shape[:2]:
                bg_rgb = cv2.resize(bg_rgb, (original_rgb.shape[1], original_rgb.shape[0]))
            comp = fg_rgb.astype(np.float32) * alpha3 + bg_rgb.astype(np.float32) * (1 - alpha3)
        else:
            h, w = original_rgb.shape[:2]
            checker = np.zeros((h, w, 3), dtype=np.uint8)
            tile = 16
            for y in range(0, h, tile):
                for x in range(0, w, tile):
                    checker[y:y+tile, x:x+tile] = 180 if ((x // tile) + (y // tile)) % 2 == 0 else 120
            comp = fg_rgb.astype(np.float32) * alpha3 + checker.astype(np.float32) * (1 - alpha3)
        comp_u8 = np.clip(comp, 0, 255).astype(np.uint8)

        h, w = original_rgb.shape[:2]
        scale = min(1.0, 720.0 / w, 540.0 / h)
        dw, dh = int(w * scale), int(h * scale)

        layout = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        for img in (original_rgb, comp_u8):
            lbl = QtWidgets.QLabel()
            lbl.setFixedSize(dw, dh)
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            lbl.setPixmap(_np_to_qpixmap(cv2.resize(img, (dw, dh))))
            row.addWidget(lbl)
        layout.addLayout(row)
        close = QtWidgets.QPushButton("Close")
        close.setStyleSheet("background-color: #f44336; color: white; padding: 10px 30px;")
        close.clicked.connect(self.close)
        layout.addWidget(close, alignment=QtCore.Qt.AlignRight)
        self.setFixedSize(dw * 2 + 32, dh + 80)


_DARK_STYLE = """
QWidget {
    background-color: #141414;
    color: #e8e8e8;
    font-family: 'Inter', 'SF Pro Display', 'Segoe UI', sans-serif;
}
QPushButton {
    border: none; border-radius: 12px; font-weight: 600;
    font-size: 11px; padding: 5px 14px;
    background-color: #1e1e1e; color: #888;
}
QPushButton:hover { background-color: #282828; color: #e8e8e8; }
QPushButton[active="true"] { color: #fff; }
QLabel {
    background-color: #000; border: 1px solid #1e1e1e;
    border-radius: 2px;
}
"""


# ===== main =====
def _run_persistent(session_dir: str, parent_pid: int):
    cu = _import_color_utils()
    session = Session(Path(session_dir))
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = PersistentWindow(cu, session)
    # Center on the primary screen — avoids the "spawned off-screen on a multi-
    # monitor box / behind the NLE" failure mode.
    screen = app.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        fr = win.frameGeometry()
        win.move(
            geo.left() + (geo.width() - fr.width()) // 2,
            geo.top() + (geo.height() - fr.height()) // 2,
        )
    win.show()
    win.raise_()
    win.activateWindow()

    reader = StdinReader()
    reader.updateRequested.connect(win.on_update)
    reader.reloadRequested.connect(win.on_reload)
    reader.quitRequested.connect(app.quit)
    reader.start()

    if parent_pid > 0:
        watchdog = QtCore.QTimer()
        watchdog.setInterval(1000)
        watchdog.timeout.connect(
            lambda: app.quit() if not _parent_alive(parent_pid) else None
        )
        watchdog.start()

    sys.exit(app.exec())


def _run_oneshot(paths_json: str):
    paths = json.loads(paths_json)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = OneShotWindow(paths)
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


# WHAT IT DOES: Entry point. Dispatches to persistent (live slider) mode or one-shot
#   (legacy Resolve) mode based on argv[0]. Prints usage and exits 2 on bad args.
# DEPENDS-ON: sys.argv. _run_persistent / _run_oneshot handle the actual work.
# AFFECTS: starts a Qt event loop and never returns (sys.exit inside).
def main():
    args = sys.argv[1:]
    if args and args[0] == "--persistent":
        # --persistent --session <dir> [--parent-pid N]
        session_dir = None
        parent_pid = 0
        i = 1
        while i < len(args):
            if args[i] == "--session" and i + 1 < len(args):
                session_dir = args[i + 1]; i += 2
            elif args[i] == "--parent-pid" and i + 1 < len(args):
                try:
                    parent_pid = int(args[i + 1])
                except ValueError:
                    parent_pid = 0
                i += 2
            else:
                i += 1
        if not session_dir:
            print("ERROR: --persistent requires --session <dir>", file=sys.stderr)
            sys.exit(2)
        _run_persistent(session_dir, parent_pid)
    elif args:
        _run_oneshot(args[0])
    else:
        print(
            "Usage:\n"
            "  preview_viewer.py <json-paths>                          # one-shot\n"
            "  preview_viewer.py --persistent --session <dir> [--parent-pid N]  # live",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
