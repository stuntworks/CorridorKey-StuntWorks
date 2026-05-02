# Last modified: 2026-04-27 | Change: Margin and Soften sliders are now SAM2-ONLY everywhere — they no longer apply to NN alpha when SAM2 is off (was a viewer-only behavior that didn't match render output and silently misled users). Sliders also grey out in the UI when no SAM2 mask is active, so the user can see the controls aren't doing anything until they engage SAM2. Net effect: live preview and rendered output now agree completely on Margin/Soften behavior. Prior fixes preserved.
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

# Add CK_ROOT (engine repo root) to sys.path so we can import the shared
# sam2_combine helper. CRITICAL: the helper lives at the REPO ROOT, NOT
# inside CorridorKeyModule — importing CorridorKeyModule.anything triggers
# the package __init__ which loads torch (40-60s) and breaks the viewer's
# subprocess that doesn't have torch in its env.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sam2_combine import apply_sam2_gate, apply_sam2_gate_additive, apply_sam2_gate_weighted, apply_sam2_gate_subtract, trim_gate_by_chroma, fill_holes_color_aware

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


# ===== Lightweight color utils (no torch dependency) =====
# WHAT IT DOES: Pure numpy/cv2 implementations of the 4 functions the viewer needs.
#   Eliminates the 40-60 second torch import that happens when importing the engine's
#   color_utils module. These are exact copies of the numpy code paths.
# DEPENDS-ON: numpy, cv2 (already imported above).
# AFFECTS: nothing — pure functions.
class _ViewerColorUtils:
    """Drop-in replacement for engine color_utils — numpy-only, zero torch."""

    @staticmethod
    def composite_straight(fg, bg, alpha):
        """Composites straight FG over BG. Formula: FG * alpha + BG * (1 - alpha)."""
        return fg * alpha + bg * (1.0 - alpha)

    @staticmethod
    def despill_opencv(image, green_limit_mode="average", strength=1.0):
        """Removes green spill from an RGB float (0-1) image. Subtractive only
        (no R/B boost) + warm-wardrobe guard (R >= G skipped). Mirrors the
        engine's color_utils.despill_opencv. Both fixes are required to keep
        yellow/orange/tan wardrobe from shifting pink. DANGER ZONE FRAGILE —
        if the viewer ever shows yellow shirts as pink again, FIRST check
        whether someone reverted these two changes."""
        if strength <= 0.0:
            return image
        r = image[..., 0]
        g = image[..., 1]
        b = image[..., 2]
        if green_limit_mode == "max":
            limit = np.maximum(r, b)
        else:
            limit = (r + b) / 2.0
        spill_amount = np.maximum(g - limit, 0.0)
        # Warm-wardrobe guard — zero spill on R >= G pixels.
        spill_amount = np.where(r >= g, 0.0, spill_amount)
        # SUBTRACTIVE despill — do NOT reintroduce R/B boost.
        g_new = g - spill_amount
        r_new = r
        b_new = b
        despilled = np.stack([r_new, g_new, b_new], axis=-1)
        if strength < 1.0:
            return image * (1.0 - strength) + despilled * strength
        return despilled

    @staticmethod
    def clean_matte_opencv(alpha_np, area_threshold=300, dilation=15, blur_size=5):
        """Removes small disconnected components from a predicted alpha matte."""
        is_3d = False
        if alpha_np.ndim == 3:
            is_3d = True
            alpha_np = alpha_np[:, :, 0]
        mask_8u = (alpha_np > 0.5).astype(np.uint8) * 255
        num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            mask_8u, connectivity=8
        )
        cleaned_mask = np.zeros_like(mask_8u)
        for i in range(1, num_labels):
            if stats[i, cv2.CC_STAT_AREA] >= area_threshold:
                cleaned_mask[labels == i] = 255
        if dilation > 0:
            kernel_size = int(dilation * 2 + 1)
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
            )
            cleaned_mask = cv2.dilate(cleaned_mask, kernel)
        if blur_size > 0:
            b_size = int(blur_size * 2 + 1)
            cleaned_mask = cv2.GaussianBlur(cleaned_mask, (b_size, b_size), 0)
        safe_zone = cleaned_mask.astype(np.float32) / 255.0
        result_alpha = alpha_np * safe_zone
        if is_3d:
            result_alpha = result_alpha[:, :, np.newaxis]
        return result_alpha

    @staticmethod
    def create_checkerboard(width, height, checker_size=64, color1=0.2, color2=0.4):
        """Creates a linear grayscale checkerboard pattern. Returns [H,W,3] float."""
        x_tiles = np.arange(width) // checker_size
        y_tiles = np.arange(height) // checker_size
        x_grid, y_grid = np.meshgrid(x_tiles, y_tiles)
        checker = (x_grid + y_grid) % 2
        bg_img = np.where(checker == 0, color1, color2).astype(np.float32)
        return np.stack([bg_img, bg_img, bg_img], axis=-1)


def _import_color_utils():
    """Returns lightweight viewer-local color utils. No torch import needed."""
    return _ViewerColorUtils()


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


def _dilate_mask(mask_f32, margin):
    # Expands the mask boundary by margin pixels. Sub-pixel via lerp between adjacent dilations.
    # DEPENDS-ON: numpy, cv2
    # AFFECTS: returns a new float32 mask array, input unchanged
    margin = float(margin)
    if margin <= 0:
        return mask_f32
    int_m = int(margin)
    frac = margin - int_m
    mask_u8 = (np.clip(mask_f32, 0, 1) * 255).astype(np.uint8)
    if int_m > 0:
        k_lo = int_m * 2 + 1
        lo = cv2.dilate(mask_u8, cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (k_lo, k_lo))).astype(np.float32) / 255.0
    else:
        lo = mask_f32.copy()
    if frac > 0:
        k_hi = (int_m + 1) * 2 + 1
        hi = cv2.dilate(mask_u8, cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (k_hi, k_hi))).astype(np.float32) / 255.0
        return lo * (1.0 - frac) + hi * frac
    return lo


def _soften_mask(mask_f32, soften):
    # Blurs mask edges with a Gaussian. Float soften for sub-pixel control.
    # DEPENDS-ON: numpy, cv2
    # AFFECTS: returns a new float32 mask array, input unchanged
    soften = float(soften)
    if soften <= 0:
        return mask_f32
    k = int(soften * 6) | 1
    if k < 3:
        k = 3
    return cv2.GaussianBlur(mask_f32, (k, k), sigmaX=soften, sigmaY=soften)


# Morphological close kernel (px) applied to the SAM2 binary mask before
# multiplying with alpha_raw. With many user dots, SAM2 finds internal
# "edges" along fabric creases / shadows / motion blur and drops the gate to
# 0 between dots — which would poke black holes in the body. Close fills
# those gaps. 101px on 4K is ~2.5% of frame width — large enough to bridge
# wide inter-dot dips, while real arm/torso gaps are protected separately
# by the alpha_raw multiply (alpha_raw is 0 in genuine holes, so anything ×
# 0 = 0 regardless of how aggressive the close is). Push higher if interior
# holes persist.
GATE_BRIDGE_PX = 0
# Edge feather kernel + sigma for the SAM2 boundary. Small Gaussian on the
# binary close result gives a 2-3px anti-aliased silhouette so the matte
# doesn't have a paper-cut edge from the binary threshold.
EDGE_FEATHER_KSIZE = 11
EDGE_FEATHER_SIGMA = 2.5


def _trimap_fuse(alpha_raw, gate, source_rgb=None, screen_type="green", trim_chroma=0, halo_px=0, halo_body_px=0, fill_holes=0):
    # WHAT IT DOES: Uses SAM2 as a pure GARBAGE MATTE — crops the background
    #   without touching alpha_raw values inside the actor. The NN alpha is now
    #   reliably solid inside the body (post tan-vest alpha-hint fix), so this
    #   step should NOT try to force, multiply, or threshold interior values.
    #   Steps: (0) optional chroma-aware trim (TRIM SAM2 slider) on the gate
    #   when trim_chroma > 0 and source_rgb provided — removes screen-colored
    #   pixels from the gate before edge softening; (1) binarize the gate at
    #   0.5; (2) close with a large kernel to bridge inter-dot confidence dips;
    #   (3) soften the edge with a small Gaussian; (4) multiply with alpha_raw
    #   via apply_sam2_gate (which applies the trimap halo dilation when
    #   halo_px > 0). Real holes survive because alpha_raw is 0 in them.
    #   (5) optional FILL HOLES — when fill_holes > 0 and source_rgb provided,
    #   alpha=0 pixels inside the gate that are NOT screen-colored are flipped
    #   to alpha=1. Rescues NN dropouts on yellow shirts / skin / red.
    # DEPENDS-ON: numpy, cv2 (morphology + Gaussian), alpha_raw and gate same
    #   shape, both float32 [0..1]. halo_px forwarded to apply_sam2_gate.
    # AFFECTS: returns a new float32 array; inputs unchanged.
    if trim_chroma and trim_chroma > 0 and source_rgb is not None:
        gate = trim_gate_by_chroma(gate, source_rgb, screen_type, trim_chroma)
    binary = (gate > 0.5).astype(np.uint8) * 255
    if GATE_BRIDGE_PX >= 3:
        kernel = np.ones((GATE_BRIDGE_PX, GATE_BRIDGE_PX), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    soft = cv2.GaussianBlur(
        binary.astype(np.float32) / 255.0,
        (EDGE_FEATHER_KSIZE, EDGE_FEATHER_KSIZE),
        EDGE_FEATHER_SIGMA,
    )
    combined = apply_sam2_gate(alpha_raw, soft, invert=False, halo_px=halo_px, halo_body_px=halo_body_px).astype(np.float32)
    # FILL HOLES: bit-identical when fill_holes <= 0 (helper short-circuits).
    # Use the SAME soft gate that fed the combine so fill respects whatever
    # TRIM SAM2 / edge-softening did. fill is applied on the COMBINED alpha,
    # not the raw NN alpha.
    if fill_holes and fill_holes > 0 and source_rgb is not None:
        combined = fill_holes_color_aware(combined, soft, source_rgb, screen_type, fill_holes)
    return combined


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
        # alpha_raw is the pre-gate NN alpha used by margin/soften.
        # The Resolve engine only writes alpha.png (not alpha_raw.png),
        # so we use alpha.png as the raw source — same as the AE version does.
        self.alpha_raw = self.alpha.copy()
        self.sam2_gate_raw = None
        # original.png is the RAW source frame (with green spill, before NN). The
        # Original view mode displays this. Falls back to fg_rgb if not present
        # (older sessions / AE which doesn't write it).
        orig_path = self.session_dir / "original.png"
        self.original_rgb = None
        if orig_path.exists():
            o_bgr = _read_png_any_depth(orig_path)
            if o_bgr is not None:
                self.original_rgb = cv2.cvtColor(_to_float01(o_bgr), cv2.COLOR_BGR2RGB)
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
        # Reload original.png too so re-keys (different playhead) update the
        # Original view mode. Optional file — falls back to fg_rgb if absent.
        orig_path = self.session_dir / "original.png"
        new_original = None
        if orig_path.exists():
            o_bgr = _read_png_any_depth(orig_path)
            if o_bgr is not None:
                new_original = cv2.cvtColor(_to_float01(o_bgr), cv2.COLOR_BGR2RGB)
        # Only assign after both reads succeed so a partial write doesn't leave
        # the viewer in an inconsistent state.
        self.fg_rgb = fg_rgb
        self.alpha = alpha
        self.original_rgb = new_original
        # alpha_raw is the pre-gate NN alpha used by margin/soften.
        # The Resolve engine only writes alpha.png (not alpha_raw.png),
        # so we derive alpha_raw directly from the freshly loaded alpha —
        # same as the AE version does.
        self.alpha_raw = alpha.copy()
        # Always clear the SAM2 gate when a new frame loads. The leftover
        # sam2_gate_raw.png in the session dir is from the previous frame's
        # pose — auto-applying it to a new frame produces a wrong-shape choke
        # (the "fish" still cutting into the body). User must click Apply Mask
        # fresh on each new frame to regenerate the gate for that frame.
        self.sam2_gate_raw = None


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

    choke_px = float(params.get("choke", 0))
    sam2_margin = float(params.get("sam2_margin", 0))
    sam2_soften = float(params.get("sam2_soften", 0))
    halo_px = int(params.get("halo_px", 0))
    halo_body_px = int(params.get("halo_body_px", 0))
    trim_chroma = int(params.get("trim_chroma", 0))
    fill_holes = int(params.get("fill_holes", 0))
    sam2_additive = bool(params.get("sam2_additive", False))
    sam2_weighted = bool(params.get("sam2_weighted", False))
    sam2_subtract = bool(params.get("sam2_subtract", False))
    sam2_bypass = bool(params.get("sam2_bypass", False))
    edge_guard_px = int(params.get("edge_guard_px", 20))
    if session.alpha_raw is not None and session.sam2_gate_raw is not None and not sam2_bypass:
        _gate = session.sam2_gate_raw.copy()
        if _gate.shape != session.alpha_raw.shape:
            _gate = cv2.resize(_gate, (session.alpha_raw.shape[1], session.alpha_raw.shape[0]),
                               interpolation=cv2.INTER_LINEAR)
        _src_rgb = session.original_rgb if session.original_rgb is not None else session.fg_rgb
        if sam2_subtract:
            # SUBTRACT mode: NN owns the matte everywhere green exists. SAM2 only
            # permitted to kill in non-green zones (floor under feet, off-screen
            # props). Hair / fine detail in green territory protected by the
            # buffer + feather distance ramp. Wins over weighted/additive when
            # toggled. trim/fill_holes/halo intentionally not applied.
            # REVERTED 2026-05-01 to f6fa072 semantics: EDGE GUARD = hard
            # buffer past the green edge before SAM2 may kill, with feather =
            # half buffer for a soft transition tail. No closings on
            # nn_killed or SAM2 silhouette. This is the version that produced
            # Berto's clean-matte-from-feet-dots result.
            _fp = max(int(edge_guard_px) // 2, 1)
            alpha = apply_sam2_gate_subtract(session.alpha_raw, _gate, _src_rgb,
                                             screen_type="green",
                                             buffer_px=int(edge_guard_px),
                                             feather_px=_fp)
        elif sam2_weighted:
            # SMART BLEND: per-pixel weighted combine — NN trusted in green
            # regions (preserves hair / butt-across-strap detail), SAM2 trusted
            # off-green (kills floor / props NN can't see). Skips _trimap_fuse
            # and additive paths; trim/fill_holes/halo intentionally not applied
            # (the chroma-derived weight handles boundary blending).
            alpha = apply_sam2_gate_weighted(session.alpha_raw, _gate, _src_rgb,
                                             screen_type="green")
        elif sam2_additive:
            # ADDITIVE mode: alpha = max(NN, gate * non_screen). SAM2 can ADD
            # confidence where NN missed but never SUBTRACT NN's correct alpha.
            # Trim/fill_holes don't apply here (no multiplicative combine to
            # gate); HALO still dilates the gate but the effect is "extend SAM2
            # contribution outward" rather than "preserve NN edge band".
            _gate_for_add = _gate
            if halo_px and halo_px > 0:
                # Dilate the gate so additive contribution extends outward.
                # Keeps HALO functional in additive mode; semantics differ from
                # multiplicative mode (where it preserves NN edge values).
                _bin = (_gate_for_add > 0.5).astype(np.uint8)
                _k = int(halo_px) * 2 + 1
                _kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_k, _k))
                _gate_for_add = cv2.dilate(_bin, _kernel).astype(np.float32)
            alpha = apply_sam2_gate_additive(session.alpha_raw, _gate_for_add,
                                             _src_rgb, screen_type="green")
        else:
            # Trimap fusion: solid 1.0 inside SAM2 confident core where NN saw
            # something; multiply-as-before everywhere else (including real holes
            # inside actor, which keep alpha=0). halo_px>0 dilates the gate so a
            # band of NN values around the SAM2 silhouette survives — recovers
            # hair / motion-blur detail. trim_chroma>0 removes screen-colored
            # pixels from the gate first. fill_holes>0 fills alpha=0 holes inside
            # the gate at non-screen-color pixels (rescues yellow/skin/red NN
            # dropouts). See _trimap_fuse and apply_sam2_gate.
            alpha = _trimap_fuse(session.alpha_raw, _gate, source_rgb=_src_rgb,
                                 screen_type="green", trim_chroma=trim_chroma, halo_px=halo_px,
                                 halo_body_px=halo_body_px,
                                 fill_holes=fill_holes)
        if sam2_margin > 0:
            alpha = _dilate_mask(alpha, sam2_margin)
        if sam2_soften > 0:
            alpha = _soften_mask(alpha, sam2_soften)
    else:
        # SAM2 inactive — Margin and Soften are SAM2-only controls and do
        # nothing here. The sliders grey out in the viewer UI to make this
        # visible to the user. Render output (PROCESS RANGE / SCRUB RANGE)
        # has always behaved this way; the previous viewer-only branch that
        # applied margin/soften to NN alpha was a parity bug — preview
        # showed the user something the render couldn't deliver.
        alpha = session.alpha.copy()
    if choke_px > 0:
        int_choke = int(choke_px)
        frac = choke_px - int_choke
        a8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
        alpha_lo = cv2.erode(a8, cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (int_choke * 2 + 1, int_choke * 2 + 1)
        )).astype(np.float32) / 255.0 if int_choke > 0 else alpha.copy()
        if frac > 0:
            k2 = (int_choke + 1) * 2 + 1
            alpha_hi = cv2.erode(a8, cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (k2, k2)
            )).astype(np.float32) / 255.0
            alpha = alpha_lo * (1.0 - frac) + alpha_hi * frac
        else:
            alpha = alpha_lo
    if despeckle_on and despeckle_size > 0:
        # clean_matte_opencv expects area threshold in pixels
        alpha = cu.clean_matte_opencv(alpha, area_threshold=despeckle_size)

    # FG SOURCE — substitute the model's FG color with the original source plate
    # (or a 50/50 blend) BEFORE despill. The matte is unchanged. Used to rescue
    # warm wardrobe (yellow shirts) that the NN paints pink. Default "nn" keeps
    # current behavior. Falls through silently when original_rgb wasn't loaded.
    fg_source = str(params.get("fg_source", "nn")).lower()
    if fg_source != "nn" and getattr(session, "original_rgb", None) is not None:
        _orig = session.original_rgb
        if _orig.shape[:2] != session.fg_rgb.shape[:2]:
            _orig = cv2.resize(_orig, (session.fg_rgb.shape[1], session.fg_rgb.shape[0]),
                               interpolation=cv2.INTER_LINEAR)
        if fg_source == "source":
            fg_rgb = _orig.astype(np.float32, copy=True)
        elif fg_source == "blend":
            fg_rgb = (0.5 * session.fg_rgb + 0.5 * _orig).astype(np.float32)
        else:
            fg_rgb = session.fg_rgb.copy()
    else:
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


# WHAT IT DOES: QSlider that locks onto the click position immediately and tracks
#   the mouse 1:1 during drag. Default QSlider moves in page steps toward the cursor
#   instead of snapping — this replaces all three sliders in the panel.
# DEPENDS-ON: QtWidgets, QtCore.
# AFFECTS: nothing outside the slider widget itself.
class JumpSlider(QtWidgets.QSlider):
    def _value_from_pos(self, pos):
        ratio = pos.x() / max(self.width() - 1, 1)
        ratio = max(0.0, min(1.0, ratio))
        return int(self.minimum() + ratio * (self.maximum() - self.minimum()) + 0.5)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            self.setValue(self._value_from_pos(ev.pos()))
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if ev.buttons() & QtCore.Qt.LeftButton:
            self.setValue(self._value_from_pos(ev.pos()))
            ev.accept()
        else:
            super().mouseMoveEvent(ev)


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
        # WHAT IT DOES: Default values written to live_params.json on first
        #   open. Must match the panel's get_settings() defaults — otherwise
        #   the viewer leaks its values back into the panel via
        #   _merge_live_params and overrides the panel's intended defaults
        #   without the user ever touching a slider.
        # DEPENDS-ON: panel get_settings() in CorridorKey_Pro.py — keep in sync.
        # AFFECTS: PROCESS RANGE, BRAW PROCESS RANGE, SCRUB RANGE all read
        #   live_params.json. Wrong defaults here = wrong matte everywhere.
        # DANGER ZONE FRAGILE: do NOT bump sam2_margin or sam2_soften here
        #   "for a nicer default" — the panel will inherit your value silently.
        #   breaks: matte gets dilated/softened on first run with no visible UI cause.
        self._params = {
            "despill": 1.0,
            "despeckle": True,
            "despeckleSize": 400,
            "background": "checker",
            "choke": 0,
            "sam2_margin": 0.0,
            "sam2_soften": 0.0,
            # HALO: trimap-guided halo band width in pixels. 0 = current
            # behavior (bit-identical). >0 dilates the SAM2 gate so a band of
            # NN values around the silhouette survives — recovers hair /
            # motion-blur detail at the SAM2 edge. SAM2-only control.
            "halo_px": 0,
            # HALO BODY: SAM2 gate dilation in GREEN-BORDERED zones (body
            # silhouette where it meets the green screen). Pairs with halo_px
            # (= HALO FEET) for the May 1 TWO HALO design — independent control
            # so a 30+ px buffer can recover hair / butt-across-strap detail
            # without bloating feet into the floor. 0 = off (bit-identical to
            # single-halo behavior). SAM2-only control.
            "halo_body_px": 0,
            # TRIM SAM2: chroma-aware mask refinement. 0 = off (bit-identical).
            # >0 removes screen-colored pixels from the SAM2 gate before
            # combine — kills "holes" at silhouette edges where SAM2 claims
            # green but NN keyed transparent. SAM2-only control.
            "trim_chroma": 0,
            # FILL HOLES: color-aware interior alpha-zero fill. 0 = off
            # (bit-identical). >0 fills alpha=0 holes inside the SAM2 gate at
            # non-screen-color pixels — rescues NN dropouts on yellow shirts,
            # skin, red. Higher = more lenient on what counts as "non-screen".
            # SAM2-only control.
            "fill_holes": 0,
            # FG SOURCE: "nn" = model FG (default, original behavior)
            #            "source" = original plate inside the matte (yellow-shirt rescue)
            #            "blend" = 50/50 NN + source (built but not exposed in UI yet)
            "fg_source": "nn",
            # SAM2 ADDITIVE: when True, switches combine math from NN x SAM2
            # (multiplicative, default) to max(NN, SAM2 x non_screen) (additive).
            # Preserves subject regions SAM2 misses across visual boundaries
            # (straps, props). Off = bit-identical to current behavior.
            "sam2_additive": False,
            # SAM2 SMART BLEND: when True, per-pixel blends NN and SAM2 by
            # green-presence (chroma-derived weight). NN trusted where green
            # exists, SAM2 trusted off-green. Wins over sam2_additive when
            # both are checked. Off = bit-identical to current behavior.
            "sam2_weighted": False,
            # SAM2 SUBTRACT: when True, NN matte preserved everywhere green
            # exists; SAM2 only permitted to kill in non-green zones (floor /
            # off-screen junk). Buffer + feather distance ramp protects body
            # parts at the green edge. Wins over sam2_weighted/sam2_additive
            # when toggled. Off = bit-identical to current behavior.
            "sam2_subtract": False,
            # SAM2 BYPASS: master switch — when True, skip ALL SAM2 combine
            # paths and return NN alpha directly. Lets the user A/B compare
            # NN-only vs NN+SAM2 without clearing dots.
            "sam2_bypass": False,
            # SHOW SAM2: viewer-only overlay (cyan outline of SAM2 silhouette
            # over the matte/composite view). Does not affect rendered output.
            "show_sam2": False,
            # EDGE GUARD: distance in pixels past the green edge before SAM2
            # may begin to kill (buffer). Feather is auto-set to half this
            # value. Default 8 px works for typical 1080p stunt footage.
            "edge_guard_px": 20,
        }
        # Drop-stale: if a new update comes in while we're painting, we only keep
        # the latest one. _pending is None when idle, or a dict when a render is
        # queued. _painting is True between compute and paint.
        self._pending = None
        self._painting = False

        # Live-params persistence: when the user drags a slider IN THE VIEWER, we
        # update self._params immediately (local render), then debounce a write of
        # live_params.json so the panel sees the value for batch processing. The
        # _suppress_poll flag prevents our own write from looping back through the
        # file-watcher and re-rendering with the same values.
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self._save_live_params_now)
        # Throttle render: immediate on first drag event, then at most once per 60ms
        # while still dragging. SingleShot restarts each cycle — stops automatically
        # when no more events arrive.
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._on_render_throttle_tick)
        self._render_pending = False
        self._suppress_poll_mtime = 0.0
        # Tracks the last time a LOCAL slider/checkbox fired. While within
        # 500ms of a local change we ignore file-poller updates — otherwise
        # the poller reads the old value from disk (before the 250ms debounced
        # save fires) and overrides the slider move, making sliders appear dead.
        self._local_change_time = 0.0
        # Tracks last time ANY local activity happened (slider drag, checkbox,
        # background switch). Used by the idle-slowdown logic in _poll_live_params
        # to back off the poll interval from 50ms to 500ms after 2s of quiet.
        # This reduces ASIO buffer pressure on Focusrite during inactive periods.
        self._last_activity_time = time.perf_counter()

        # Zoom + pan state. Mouse wheel adjusts _zoom, drag-while-zoomed updates
        # _pan_x/_pan_y (fractional 0..1 center point). _paint_into crops the
        # full-res cached image to this window before scaling into the label.
        self._zoom = 1.0
        self._pan_x = 0.5
        self._pan_y = 0.5
        self._dragging = False
        self._drag_start = None
        self._drag_start_pan = (0.5, 0.5)

        # SAM click-to-mask state
        self._sam_mode = False
        self._sam_display_pts = []  # (nx, ny, is_positive, frame_num) — frame_num is the absolute timeline frame the click was placed on (read from meta.json at click time). None if meta not available.
        self._last_right_geom = None  # cached paint geometry for coord mapping

        h, w = session.shape_hw
        # Default display scale picks a size that fits on a 1366x768 laptop with
        # both panes + UI chrome — the window is resizable after launch so the
        # user can drag it larger on a big monitor.
        self.scale = min(1.0, 480.0 / w, 360.0 / h)
        self.disp_w = max(240, int(w * self.scale))
        self.disp_h = max(180, int(h * self.scale))
        # Prefer the raw source frame (original.png) over fg.png for Original
        # view mode — fg.png is the NN-clean FG (greens are black) which is NOT
        # what users want when they click Original. Falls back to fg_rgb if
        # original.png wasn't written by the host plugin.
        _orig_src = session.original_rgb if session.original_rgb is not None else session.fg_rgb
        self.original_u8 = np.clip(_orig_src * 255.0, 0, 255).astype(np.uint8)
        self.original_scaled = cv2.resize(self.original_u8, (self.disp_w, self.disp_h))

        self._scrub_index_mtime = 0.0
        self._scrub_count = 0
        self._scrub_base = None
        # Retry counter for _on_scrub_slider when frame files aren't written yet.
        # Keying takes ~2-3 sec/frame; frame 0 may not exist when scrub_index.json
        # lands. Each retry fires after 300ms; capped at 30 attempts (~9 sec).
        self._scrub_frame_retry_count = 0

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

        # ADAPTIVE POLL RATE — reduces ASIO buffer pressure on Focusrite during idle.
        # After 2 seconds with no local activity, slow the poll from 50ms to 500ms.
        # Any incoming update from disk resets the rate back to 50ms immediately.
        # At 128-sample / 48kHz (2.7ms headroom), 20 polls/sec adds measurable
        # scheduler jitter; 2 polls/sec is imperceptible.
        idle_secs = time.perf_counter() - self._last_activity_time
        desired_interval = 50 if idle_secs < 2.0 else 500
        if self._live_watcher.interval() != desired_interval:
            self._live_watcher.setInterval(desired_interval)

        # Check for scrub_index.json — runs every tick, independent of live_params changes.
        # Written by panel when SCRUB RANGE finishes keying N frames.
        _scrub_path = self.session.session_dir / "scrub_index.json"
        try:
            _scrub_mt = _scrub_path.stat().st_mtime
        except FileNotFoundError:
            _scrub_mt = 0.0
        if _scrub_mt != self._scrub_index_mtime:
            self._scrub_index_mtime = _scrub_mt
            try:
                import tempfile as _vt
                with open(str(Path(_vt.gettempdir()) / "ck_viewer_scrub.txt"), "a") as _vf:
                    _vf.write(f"scrub_index mtime changed: {_scrub_mt} path={_scrub_path} exists={_scrub_path.exists()}\n")
            except Exception: pass
            if _scrub_mt > 0.0:
                self._enter_scrub_mode(_scrub_path)

        try:
            mt = self._live_params_path.stat().st_mtime
        except FileNotFoundError:
            return
        except OSError:
            return
        if mt == self._live_params_mtime:
            return
        # Skip if this mtime came from our own save — viewer-driven slider moves
        # already updated self._params and re-rendered. Otherwise we'd re-enter
        # on every write. The tolerance handles FS-layer mtime jitter.
        if abs(mt - self._suppress_poll_mtime) < 0.01:
            self._live_params_mtime = mt
            return
        # Skip if a local slider/checkbox fired within the last 500ms. The
        # debounced save takes 250ms, so during that window the file on disk
        # still has the OLD value. Reading it back would override the slider
        # move and make sliders appear dead. 500ms covers the debounce + jitter.
        if (time.perf_counter() - self._local_change_time) < 0.5:
            return
        self._live_params_mtime = mt
        try:
            with open(self._live_params_path, "r", encoding="utf-8") as f:
                params = json.load(f)
        except Exception:
            # Partial write or transient — next tick will retry.
            return
        # Disk update received — reset activity clock so poll snaps back to 50ms.
        self._last_activity_time = time.perf_counter()
        self.on_update(params)

    def _enter_scrub_mode(self, index_path: Path):
        # WHAT IT DOES: Shows the scrubber slider and loads the first cached keyed frame.
        # DEPENDS-ON: SESSION_DIR/scrub_index.json and SESSION_DIR/scrub/NNN/fg.png+alpha.png
        # AFFECTS: self._scrub_count, self._scrub_base, self._scrub_bar visibility
        def _vlog(msg):
            try:
                import tempfile as _vt2
                with open(str(Path(_vt2.gettempdir()) / "ck_viewer_scrub.txt"), "a") as _vf2:
                    _vf2.write(msg + "\n")
            except Exception: pass
        _vlog(f"_enter_scrub_mode called: {index_path}")
        try:
            with open(index_path) as _f:
                _data = json.load(_f)
            self._scrub_count = int(_data["count"])
            self._scrub_base = self.session.session_dir / _data["base_dir"]
            _vlog(f"  count={self._scrub_count} base={self._scrub_base}")
        except Exception as _e:
            _vlog(f"  FAILED: {_e}")
            print(f"Scrub mode enter failed: {_e}", file=sys.stderr)
            return
        # BUG FIX: Block signals on setRange/setValue so the valueChanged signal does NOT
        # fire _on_scrub_slider during setup. Without this, setValue(0) fires the slot early
        # (before _scrub_bar is shown) when the slider's previous value was non-zero, causing
        # a redundant double-render. We call _on_scrub_slider(0) explicitly below instead.
        self._scrub_slider.blockSignals(True)
        self._scrub_slider.setRange(0, max(0, self._scrub_count - 1))
        self._scrub_slider.setValue(0)
        self._scrub_slider.blockSignals(False)
        # Clear SAM2 click dots — they only make sense during live preview, not scrub playback.
        # The repaint triggered by _on_scrub_slider(0) below will paint the first scrub frame
        # without dots since _draw_sam_overlay returns early when _sam_display_pts is empty.
        self._sam_display_pts = []
        self._scrub_bar.show()
        _vlog(f"  scrub_bar.show() called — isVisible={self._scrub_bar.isVisible()}")
        # Resize window taller to accommodate scrub panel, then nudge upward if the
        # bottom would go off-screen. sizeHint() called AFTER show() so layout is realised.
        _extra = self._scrub_bar.sizeHint().height() + 8
        self.resize(self.width(), self.height() + _extra)
        _screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        _win_bottom = self.y() + self.height()
        if _win_bottom > _screen.bottom():
            _new_y = max(_screen.top(), _screen.bottom() - self.height())
            self.move(self.x(), _new_y)
        # BUG FIX: Snap poll back to 50ms when entering scrub mode — detection may have
        # happened while the poller was at its 500ms idle rate, and we want stage-2 slider
        # changes to feel responsive immediately.
        self._last_activity_time = time.perf_counter()
        # Reset retry counter so each new scrub session gets a clean slate.
        self._scrub_frame_retry_count = 0
        self._on_scrub_slider(0)
        self.status.setText(f"SCRUB MODE — {self._scrub_count} keyed frames  •  adjust sliders freely")

    def _exit_scrub_mode(self):
        # WHAT IT DOES: Hides scrubber panel, deletes scrub_index.json, and reloads the
        #   original single-frame preview.
        # DEPENDS-ON: self.session (must have valid fg.png + alpha.png, session_dir)
        # AFFECTS: self._scrub_bar visibility, SESSION_DIR/scrub_index.json,
        #   self._scrub_index_mtime, reloads session fg/alpha
        self._scrub_bar.hide()
        # Reset _scrub_base so _paint_right's guard re-enables SAM dot overlay in live preview.
        # DANGER ZONE: FRAGILE — must be set to None BEFORE _render_now() below, otherwise
        #   _paint_right still sees a non-None _scrub_base and suppresses dots after exit.
        self._scrub_base = None
        # Delete scrub_index.json so a restarted viewer does not re-enter scrub mode
        # from a stale file left over from the previous run.
        # DANGER ZONE: FRAGILE — must reset _scrub_index_mtime too, otherwise the
        # mtime guard below will think the (now-deleted) file hasn't changed.
        try:
            _idx_path = self.session.session_dir / "scrub_index.json"
            if _idx_path.exists():
                _idx_path.unlink()
            self._scrub_index_mtime = 0.0
        except Exception:
            pass
        try:
            self.session.reload_pngs()
            self._render_now()
        except Exception:
            pass
        self.status.setText("Ready")

    def _on_scrub_slider(self, idx: int):
        # WHAT IT DOES: Loads cached fg/alpha for the given scrub frame index and re-renders.
        #   No neural net involved — pure stage-2 (instant).
        # DEPENDS-ON: self._scrub_base / f"{idx:03d}" / fg.png + alpha.png
        # AFFECTS: self.session.fg_rgb, self.session.alpha, triggers re-render
        # DANGER ZONE: FRAGILE — scrub_index.json is written by the panel as soon as
        #   keying STARTS, not when it finishes. Frame 0 (and later frames) may not exist
        #   yet when this fires. See retry logic below.
        if self._scrub_base is None:
            return
        # BUG FIX: Scrub slider drag is user activity — reset the activity clock so the
        # poller stays at 50ms during scrub. Without this, the poller idles at 500ms and
        # a stray live_params.json update (e.g. rekeying:false) could overwrite the scrub
        # frame before the user notices.
        self._last_activity_time = time.perf_counter()
        frame_dir = self._scrub_base / f"{idx:03d}"
        fg_path  = frame_dir / "fg.png"
        alp_path = frame_dir / "alpha.png"
        if not fg_path.exists() or not alp_path.exists():
            # BUG FIX: Frame not written yet — schedule a retry instead of giving up.
            # The panel writes scrub_index.json at the start of keying, so frame 0 can
            # arrive 2-3 seconds later. Cap at 30 retries (~9 sec) to avoid spinning
            # forever if keying fails. Each retry resets _scrub_frame_retry_count to 0
            # so a manual slider drag on the same frame also gets fresh retries.
            _MAX_RETRIES = 30
            if self._scrub_frame_retry_count < _MAX_RETRIES:
                self._scrub_frame_retry_count += 1
                remaining = _MAX_RETRIES - self._scrub_frame_retry_count
                self.status.setText(
                    f"Scrub frame {idx+1} keying... "
                    f"({self._scrub_frame_retry_count}/{_MAX_RETRIES})"
                )
                QtCore.QTimer.singleShot(300, lambda: self._on_scrub_slider(idx))
            else:
                self.status.setText(
                    f"Scrub frame {idx+1} not available after {_MAX_RETRIES} retries — "
                    f"keying may have failed"
                )
            return
        # Frame files are present — reset retry counter so the next "not ready" hit
        # (on a different frame) also gets the full 30 attempts.
        self._scrub_frame_retry_count = 0
        fg_bgr = _read_png_any_depth(fg_path)
        if fg_bgr is None:
            return
        self.session.fg_rgb = cv2.cvtColor(_to_float01(fg_bgr), cv2.COLOR_BGR2RGB)
        alp_img = _read_png_any_depth(alp_path)
        if alp_img is None:
            return
        if alp_img.ndim == 3:
            alp_img = cv2.cvtColor(alp_img, cv2.COLOR_BGR2GRAY)
        alpha_loaded = _to_float01(alp_img)
        # WHAT IT DOES: Load alpha_raw.png + sam2_gate_raw.png if the panel wrote
        #   them for this scrub frame, so render_composite uses the same NN×gate
        #   formula as live preview and MARGIN/SOFTEN sliders work during scrub.
        # WHY: panel's keying loop multiplies NN × SAM2 gate and saves the product
        #   as alpha.png. When SAM2 returns an empty mask for a frame, the product
        #   is all zeros and the viewer shows a blank matte. Falling back to the
        #   pre-gate NN alpha lets the user still scrub through the keyed body
        #   even when SAM2 fails on that frame — the live preview already does this.
        # AFFECTS: self.session.alpha_raw, self.session.sam2_gate_raw, self.session.alpha.
        alpha_raw_path = frame_dir / "alpha_raw.png"
        gate_path      = frame_dir / "sam2_gate_raw.png"
        alpha_raw = None
        if alpha_raw_path.exists():
            _ar_img = _read_png_any_depth(alpha_raw_path)
            if _ar_img is not None:
                if _ar_img.ndim == 3:
                    _ar_img = cv2.cvtColor(_ar_img, cv2.COLOR_BGR2GRAY)
                alpha_raw = _to_float01(_ar_img)
        gate = None
        if gate_path.exists():
            _g_img = _read_png_any_depth(gate_path)
            if _g_img is not None:
                if _g_img.ndim == 3:
                    _g_img = cv2.cvtColor(_g_img, cv2.COLOR_BGR2GRAY)
                _gate_f = _to_float01(_g_img)
                # Only treat the gate as valid when it actually has nonzero
                # pixels — an all-zero gate (SAM2 produced no mask for this
                # frame) would zero out the entire matte if we used it.
                if float(_gate_f.max()) > 0.0:
                    gate = _gate_f
        self.session.alpha_raw      = alpha_raw
        self.session.sam2_gate_raw  = gate
        # If alpha.png is empty (the typical SAM2-empty-mask case) but we have
        # a real alpha_raw, swap it in so the basic display works.
        if alpha_loaded.max() == 0.0 and alpha_raw is not None:
            self.session.alpha = alpha_raw.copy()
        else:
            self.session.alpha = alpha_loaded
        self._scrub_frame_lbl.setText(f"Frame {idx + 1} / {self._scrub_count}")
        self._render_now()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 8, 10, 8)

        # Title
        title = QtWidgets.QLabel("CORRIDORKEY LIVE PREVIEW")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet(
            "color: #6ab; font-size: 16px; font-weight: 700; "
            "letter-spacing: 1.5px; border: none; background: transparent;"
        )
        layout.addWidget(title)

        # View mode — pill buttons with mode colors when active
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addStretch(1)
        # Each mode gets its own color — muted so it doesn't
        # compete with the footage, bright enough to read.
        self._mode_colors = {
            "Original": ("#3a5a6a", "#7ab"),    # slate blue (neutral/source)
            "Composite": ("#1a4a2a", "#5b5"),    # muted green (result)
            "Foreground": ("#1a3a5a", "#5af"),   # steel blue (extracted)
            "Matte": ("#4a3a1a", "#da5"),         # amber (technical)
        }
        self.mode_buttons = {}
        for mode, (bg, fg) in self._mode_colors.items():
            btn = QtWidgets.QPushButton(mode)
            btn.setStyleSheet(
                f"background-color: #111; color: #667; padding: 6px 14px; "
                f"border: 1px solid {fg}; border-radius: 12px; font-size: 13px; font-weight: 600;"
            )
            btn.clicked.connect(lambda _=False, m=mode: self._set_view_mode(m))
            mode_row.addWidget(btn)
            self.mode_buttons[mode] = btn
        # Split toggle
        self._split_btn = QtWidgets.QPushButton("Split")
        self._split_btn.setCheckable(True)
        self._split_btn.setStyleSheet(
            "background-color: #111; color: #667; padding: 6px 12px; "
            "border: 1px solid #2a2a2a; border-radius: 12px; font-size: 12px;"
        )
        self._split_btn.clicked.connect(self._toggle_split)
        mode_row.addWidget(self._split_btn)
        mode_row.addStretch(1)  # closing stretch — sandwiches buttons to center
        layout.addLayout(mode_row)

        # SAM click-to-mask toggle + clear
        sam_row = QtWidgets.QHBoxLayout()
        sam_row.setSpacing(8)
        sam_row.addStretch(1)  # opening stretch
        self._sam_btn = QtWidgets.QPushButton("CLICK TO MASK")
        self._sam_btn.setCheckable(True)
        self._sam_btn.setStyleSheet(
            "background-color: #111; color: #da5; padding: 5px 14px; "
            "border: 1px solid #da5; border-radius: 12px; font-size: 12px; font-weight: 600;"
        )
        self._sam_btn.clicked.connect(self._toggle_sam_mode)
        sam_row.addWidget(self._sam_btn)
        self._sam_clear_btn = QtWidgets.QPushButton("CLEAR")
        self._sam_clear_btn.setStyleSheet(
            "background-color: #111; color: #aaa; padding: 5px 10px; "
            "border: 1px solid #444; border-radius: 12px; font-size: 12px;"
        )
        self._sam_clear_btn.clicked.connect(self._clear_sam_points)
        sam_row.addWidget(self._sam_clear_btn)
        self._sam_apply_btn = QtWidgets.QPushButton("APPLY MASK")
        self._sam_apply_btn.setStyleSheet(
            "background-color: #1a4a2a; color: #5b5; padding: 5px 12px; "
            "border: 1px solid #5b5; border-radius: 12px; font-size: 12px; font-weight: 600;"
        )
        self._sam_apply_btn.clicked.connect(self._apply_sam_mask)
        sam_row.addWidget(self._sam_apply_btn)
        sam_row.addStretch(1)  # closing stretch
        layout.addLayout(sam_row)

        # Background — smaller pills
        bg_row = QtWidgets.QHBoxLayout()
        bg_row.setSpacing(8)
        bg_row.addStretch(1)  # opening stretch
        bg_label = QtWidgets.QLabel("BG:")
        bg_label.setStyleSheet(
            "color: #aaa; border: none; background: transparent; font-size: 12px;"
        )
        bg_row.addWidget(bg_label)
        self.bg_buttons = {}
        bg_colors = {
            "checker": "#596",
            "black":   "#667",
            "white":   "#887",
            "v1":      "#768",
        }
        for bg_name in ("checker", "black", "white", "v1"):
            btn = QtWidgets.QPushButton(bg_name.upper())
            btn.setCheckable(True)
            c = bg_colors[bg_name]
            btn.setStyleSheet(
                f"background-color: #111; color: #aaa; padding: 4px 10px; "
                f"border: 1px solid {c}; border-radius: 10px; font-size: 12px;"
            )
            btn.clicked.connect(lambda _=False, n=bg_name: self._set_background(n))
            bg_row.addWidget(btn)
            self.bg_buttons[bg_name] = btn
        self.bg_buttons["checker"].setChecked(True)
        bg_row.addStretch(1)  # closing stretch
        layout.addLayout(bg_row)

        # Keying controls — live sliders that drive in-viewer post-processing.
        # Added for Resolve because Fusion UIManager's Slider widget has a known
        # .Value unreliability bug (drags don't update the stored value). PySide6
        # sliders in a subprocess work reliably and don't block the host UI thread.
        # Only post-proc-separable parameters live here (despill, despeckle).
        # Refiner is NN-internal (forward hook inside the model graph) so it stays
        # on the panel — changing it requires a full re-key, not just a repaint.
        self._build_slider_panel(layout)

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
        self.left_label.setStyleSheet("background-color: #000; border: 1px solid rgba(0,255,255,0.08);")
        self.left_label.hide()
        self.right_label = QtWidgets.QLabel()
        self.right_label.setAlignment(QtCore.Qt.AlignCenter)
        self.right_label.setSizePolicy(expanding)
        self.right_label.setMinimumSize(320, 240)
        self.right_label.setStyleSheet("background-color: #000; border: 1px solid rgba(0,255,255,0.08);")
        self.right_label.installEventFilter(self)
        self._pane_row.addWidget(self.left_label, 1)
        self._pane_row.addWidget(self.right_label, 1)
        layout.addLayout(self._pane_row, 1)

        # Processing overlay — shown during refiner re-key
        self._overlay = QtWidgets.QLabel("Re-keying...", self.right_label)
        self._overlay.setAlignment(QtCore.Qt.AlignCenter)
        self._overlay.setStyleSheet(
            "background-color: rgba(0,0,0,180); color: #0ff; "
            "font-size: 22px; font-weight: 700; border: none; border-radius: 2px;"
        )
        self._overlay.hide()

        # Full-res arrays for resize re-scaling without stage-2 re-render
        self._last_right_full = None
        self._place_original()

        # Status bar — monospace readout. #cccccc = 80% white, readable on black
        # without being harsh. Kimi design review 2026-04-20.
        self.status = QtWidgets.QLabel("Ready")
        self.status.setStyleSheet(
            "color: #cccccc; border: none; background: transparent; "
            "font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace; "
            "font-size: 12px;"
        )
        layout.addWidget(self.status)

        # Scrubber panel — hidden until panel writes scrub_index.json
        self._scrub_bar = QtWidgets.QWidget()
        self._scrub_bar.setStyleSheet("background: #111; border-top: 1px solid #333; padding: 4px;")
        _sb_layout = QtWidgets.QVBoxLayout(self._scrub_bar)
        _sb_layout.setContentsMargins(4, 4, 4, 4)
        _sb_layout.setSpacing(4)
        _sb_hdr = QtWidgets.QLabel("SCRUB PREVIEW — drag to check key quality across frames")
        _sb_hdr.setAlignment(QtCore.Qt.AlignCenter)
        _sb_hdr.setStyleSheet("color: #a5f; font-size: 11px; font-weight: bold;")
        _sb_layout.addWidget(_sb_hdr)
        self._scrub_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._scrub_slider.setRange(0, 9)
        self._scrub_slider.setValue(0)
        self._scrub_slider.setStyleSheet(
            "QSlider::groove:horizontal { height: 8px; background: #333; border-radius: 4px; } "
            "QSlider::handle:horizontal { background: #a5f; width: 20px; height: 20px; border-radius: 10px; margin: -6px 0; } "
            "QSlider::sub-page:horizontal { background: #a5f; border-radius: 4px; }"
        )
        self._scrub_slider.valueChanged.connect(self._on_scrub_slider)
        _sb_layout.addWidget(self._scrub_slider)
        self._scrub_frame_lbl = QtWidgets.QLabel("Frame 1 / 10")
        self._scrub_frame_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._scrub_frame_lbl.setStyleSheet("color: #0dcaf0; font-size: 11px;")
        _sb_layout.addWidget(self._scrub_frame_lbl)
        self._scrub_hint = QtWidgets.QLabel("Drag slider • Adjust Margin/Soften/Despill sliders above — changes apply instantly")
        self._scrub_hint.setAlignment(QtCore.Qt.AlignCenter)
        self._scrub_hint.setStyleSheet("color: #666; font-size: 10px;")
        _sb_layout.addWidget(self._scrub_hint)
        _sb_exit = QtWidgets.QPushButton("EXIT SCRUB MODE")
        _sb_exit.setStyleSheet(
            "QPushButton { background: transparent; color: #f66; border: 1px solid #f66; "
            "padding: 6px; border-radius: 3px; font-size: 11px; font-weight: bold; } "
            "QPushButton:hover { background: rgba(255,102,102,0.15); }"
        )
        _sb_exit.clicked.connect(self._exit_scrub_mode)
        _sb_layout.addWidget(_sb_exit)
        layout.addWidget(self._scrub_bar)
        self._scrub_bar.hide()

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
            bg, fg = self._mode_colors[mode]
            if mode == self._view_mode:
                btn.setStyleSheet(
                    f"background-color: {bg}; color: {fg}; padding: 6px 14px; "
                    f"border: 1px solid {fg}; border-radius: 12px; font-size: 13px; font-weight: 600;"
                )
            else:
                btn.setStyleSheet(
                    f"background-color: #111; color: #667; padding: 6px 14px; "
                    f"border: 1px solid {fg}; border-radius: 12px; font-size: 13px; font-weight: 600;"
                )

    # WHAT IT DOES: Toggles between single-pane (default) and two-up split view.
    # DEPENDS-ON: self.left_label visibility state.
    # AFFECTS: left_label visibility, window width, split button style.
    def _toggle_split(self, checked):
        if checked:
            self.left_label.show()
            self._split_btn.setStyleSheet(
                "background-color: #1a3a4a; color: #8cf; padding: 6px 12px; "
                "border: 1px solid #3a6a7a; border-radius: 12px; font-size: 12px;"
            )
            self.resize(self.width() + self.disp_w, self.height())
        else:
            self.left_label.hide()
            self._split_btn.setStyleSheet(
                "background-color: #111; color: #667; padding: 6px 12px; "
                "border: 1px solid #2a2a2a; border-radius: 12px; font-size: 12px;"
            )
            # Mirror the widen on entry — without this the window stays wide
            # forever after a single Split toggle. Clamp to a sane minimum
            # so we never resize below the layout's own minimum.
            _new_w = max(self.minimumWidth(), self.width() - self.disp_w)
            self.resize(_new_w, self.height())
        self._repaint_both()

    # WHAT IT DOES: Builds the in-viewer slider panel — despill, despeckle on/off,
    #   and despeckle size. These drive live post-processing against the cached NN
    #   output. Every slider move updates self._params, repaints immediately, and
    #   debounces a live_params.json write so the Fusion panel sees the final
    #   values for batch processing.
    # DEPENDS-ON: PySide6 QSlider / QCheckBox, self._params already initialized.
    # AFFECTS: adds self.despill_slider, self.despeckle_cb, self.despeckle_slider,
    #   self.despill_label, self.despeckle_label as instance attributes.
    def _build_slider_panel(self, parent_layout):
        panel = QtWidgets.QFrame()
        panel.setStyleSheet(
            "QFrame { background-color: rgba(10,20,30,0.4); border: 1px solid "
            "rgba(0,255,255,0.08); border-radius: 6px; }"
        )
        grid = QtWidgets.QGridLayout(panel)
        grid.setSpacing(14)
        grid.setContentsMargins(12, 12, 12, 12)

        def _label(text, color="#8ab"):
            lbl = QtWidgets.QLabel(text)
            lbl.setStyleSheet(
                f"color: {color}; border: none; background: transparent; "
                f"font-size: 12px; font-weight: 600; letter-spacing: 0.5px;"
            )
            return lbl

        # --- Despill slider: 0-100 → 0.0-1.0 ---
        grid.addWidget(_label("DESPILL"), 0, 0)
        self.despill_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.despill_slider.setRange(0, 100)
        self.despill_slider.setValue(int(self._params["despill"] * 100))
        self.despill_slider.valueChanged.connect(self._on_despill_slider_changed)
        grid.addWidget(self.despill_slider, 0, 1)
        self.despill_value_label = _label(f"{self._params['despill']:.2f}", "#0ff")
        self.despill_value_label.setMinimumWidth(42)
        grid.addWidget(self.despill_value_label, 0, 2)

        # --- Despeckle on/off + size slider ---
        self.despeckle_cb = QtWidgets.QCheckBox("DESPECKLE")
        self.despeckle_cb.setChecked(bool(self._params["despeckle"]))
        self.despeckle_cb.setStyleSheet(
            "QCheckBox { color: #8ab; border: none; background: transparent; "
            "font-size: 12px; font-weight: 600; letter-spacing: 0.5px; } "
            "QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid "
            "#2a6a7a; border-radius: 3px; background: #001a28; } "
            "QCheckBox::indicator:checked { background: #0ff; border-color: #0ff; }"
        )
        self.despeckle_cb.toggled.connect(self._on_despeckle_toggled)
        # FIX A-1: singleShot(0) removed — it raced with the 50ms live_params.json poll
        # and caused the first render to use stale default params instead of saved values.
        # The 300ms delayed render in _run_persistent (Fix A-2) handles the first paint
        # after the poll has had time to load the correct params.
        grid.addWidget(self.despeckle_cb, 1, 0)
        self.despeckle_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.despeckle_slider.setRange(50, 2000)
        self.despeckle_slider.setValue(int(self._params["despeckleSize"]))
        self.despeckle_slider.valueChanged.connect(self._on_despeckle_size_changed)
        self.despeckle_slider.setEnabled(self.despeckle_cb.isChecked())
        grid.addWidget(self.despeckle_slider, 1, 1)
        self.despeckle_value_label = _label(f"{self._params['despeckleSize']}", "#0ff")
        self.despeckle_value_label.setMinimumWidth(42)
        grid.addWidget(self.despeckle_value_label, 1, 2)

        # --- Choke slider: 0-20 px erosion to tighten soft/bloated matte edges ---
        grid.addWidget(_label("CHOKE"), 2, 0)
        self.choke_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.choke_slider.setRange(0, 20)
        self.choke_slider.setValue(int(self._params["choke"]))
        self.choke_slider.valueChanged.connect(self._on_choke_changed)
        grid.addWidget(self.choke_slider, 2, 1)
        self.choke_value_label = _label(f"{self._params['choke']}", "#0ff")
        self.choke_value_label.setMinimumWidth(42)
        grid.addWidget(self.choke_value_label, 2, 2)

        # --- Mask Margin: 0.0-80.0 px in 0.1 steps (slider 0-800, ÷10). ---
        # SAM2-only. Tooltip explains the grey-out state when no mask active.
        _SAM2_TOOLTIP = ("SAM2 must be active for this control to work.\n"
                         "Click on the actor and press APPLY MASK first.")
        # Store references to the left-side labels so _update_sam2_slider_state
        # can swap their text between "MARGIN" and "MARGIN — OFF" depending on
        # whether SAM2 is active. Qt's default disabled look is too subtle for
        # the user to notice; explicit text makes the off-state unmissable.
        self.margin_label_widget = _label("MARGIN")
        self.margin_label_widget.setToolTip(_SAM2_TOOLTIP)
        grid.addWidget(self.margin_label_widget, 3, 0)
        self.margin_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.margin_slider.setRange(0, 800)
        self.margin_slider.setValue(int(float(self._params["sam2_margin"]) * 10))
        self.margin_slider.valueChanged.connect(self._on_margin_changed)
        self.margin_slider.setToolTip(_SAM2_TOOLTIP)
        grid.addWidget(self.margin_slider, 3, 1)
        self.margin_value_label = _label(f"{float(self._params['sam2_margin']):.1f}", "#0ff")
        self.margin_value_label.setMinimumWidth(42)
        self.margin_value_label.setToolTip(_SAM2_TOOLTIP)
        grid.addWidget(self.margin_value_label, 3, 2)

        # --- Soften: 0.0-20.0 px in 0.1 steps (slider 0-200, ÷10). ---
        self.soften_label_widget = _label("SOFTEN")
        self.soften_label_widget.setToolTip(_SAM2_TOOLTIP)
        grid.addWidget(self.soften_label_widget, 4, 0)
        self.soften_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.soften_slider.setRange(0, 200)
        self.soften_slider.setValue(int(float(self._params["sam2_soften"]) * 10))
        self.soften_slider.valueChanged.connect(self._on_soften_changed)
        self.soften_slider.setToolTip(_SAM2_TOOLTIP)
        grid.addWidget(self.soften_slider, 4, 1)
        self.soften_value_label = _label(f"{float(self._params['sam2_soften']):.1f}", "#0ff")
        self.soften_value_label.setMinimumWidth(42)
        self.soften_value_label.setToolTip(_SAM2_TOOLTIP)
        grid.addWidget(self.soften_value_label, 4, 2)

        # --- HALO BODY: SAM2 gate dilation in GREEN-BORDERED zones (slider 0-150). ---
        # The May 1 TWO HALO design: independent dilation values for body
        # silhouette (this slider) vs feet/floor cutoff (HALO FEET below).
        # Body buffer recovers hair / butt-across-strap detail where SAM2's
        # silhouette is tighter than the actor's actual edge, without bloating
        # feet into the floor. 0 = off (bit-identical to single-halo behavior).
        _HALO_BODY_TOOLTIP = ("HALO BODY — dilate SAM2 silhouette INTO green pixels only.\n"
                              "Recovers hair, butt-across-strap, fingertip detail. Cannot bleed\n"
                              "into floor / non-green areas (intersection with NN green mask).\n"
                              "0 = off. 30-100 typical. Max 300. Self-clamping at green edges.\n"
                              "SAM2 must be active for this control to work.")
        self.halo_body_label_widget = _label("HALO BODY")
        self.halo_body_label_widget.setToolTip(_HALO_BODY_TOOLTIP)
        grid.addWidget(self.halo_body_label_widget, 5, 0)
        self.halo_body_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.halo_body_slider.setRange(0, 300)
        self.halo_body_slider.setValue(int(self._params["halo_body_px"]))
        self.halo_body_slider.valueChanged.connect(self._on_halo_body_changed)
        self.halo_body_slider.setToolTip(_HALO_BODY_TOOLTIP)
        grid.addWidget(self.halo_body_slider, 5, 1)
        self.halo_body_value_label = _label(f"{int(self._params['halo_body_px'])}", "#0ff")
        self.halo_body_value_label.setMinimumWidth(42)
        self.halo_body_value_label.setToolTip(_HALO_BODY_TOOLTIP)
        grid.addWidget(self.halo_body_value_label, 5, 2)

        # --- HALO FEET: bidirectional silhouette adjustment at feet (-100 to +150). ---
        # Negative values SHRINK the silhouette upward from the bottom edge —
        # useful for removing connected floor patches that the largest-component
        # filter couldn't drop. Positive values extend the silhouette down (foot
        # shadow / contact recovery). Zero = no change.
        _HALO_FEET_TOOLTIP = ("HALO FEET — adjust SAM2 silhouette at the feet.\n"
                              "Negative: lift foot edge UP (removes connected floor patches).\n"
                              "Positive: extend foot DOWN (foot shadow / contact recovery).\n"
                              "Zero: no change. Range -100 to +150.\n"
                              "SAM2 must be active for this control to work.")
        self.halo_label_widget = _label("HALO FEET")
        self.halo_label_widget.setToolTip(_HALO_FEET_TOOLTIP)
        grid.addWidget(self.halo_label_widget, 6, 0)
        self.halo_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.halo_slider.setRange(-100, 150)
        self.halo_slider.setValue(int(self._params["halo_px"]))
        self.halo_slider.valueChanged.connect(self._on_halo_changed)
        self.halo_slider.setToolTip(_HALO_FEET_TOOLTIP)
        grid.addWidget(self.halo_slider, 6, 1)
        self.halo_value_label = _label(f"{int(self._params['halo_px'])}", "#0ff")
        self.halo_value_label.setMinimumWidth(42)
        self.halo_value_label.setToolTip(_HALO_FEET_TOOLTIP)
        grid.addWidget(self.halo_value_label, 6, 2)

        # TRIM SAM2 widget removed from UI 2026-05-01 (Berto declared useless).
        # Underlying flow + helper kept; param defaults to 0 (bit-identical to off).

        # --- FILL HOLES: color-aware interior alpha-zero fill (slider 0-100 integer). ---
        # SAM2-only. Fills alpha=0 holes INSIDE the SAM2 gate at pixels whose
        # source RGB is NOT screen-colored — rescues NN dropouts on yellow
        # shirts / skin / red while leaving correctly-killed green pixels alone.
        # 0 = bit-identical. Higher = more lenient (more pixels qualify as
        # "non-screen"). Differs from TRIM SAM2: trim removes pixels FROM the
        # gate (invisible 0×1→0×0); fill flips alpha at low-alpha pixels (visible).
        _FILL_TOOLTIP = ("FILL HOLES — fills NN alpha=0 dropouts inside SAM2 mask, but only for non-screen-color pixels.\n"
                         "0 = off (bit-identical). 30-60 typical. Higher = more aggressive.")
        self.fill_holes_label_widget = _label("FILL HOLES")
        self.fill_holes_label_widget.setToolTip(_FILL_TOOLTIP)
        grid.addWidget(self.fill_holes_label_widget, 7, 0)
        self.fill_holes_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.fill_holes_slider.setRange(0, 100)
        self.fill_holes_slider.setValue(int(self._params["fill_holes"]))
        self.fill_holes_slider.valueChanged.connect(self._on_fill_holes_changed)
        self.fill_holes_slider.setToolTip(_FILL_TOOLTIP)
        grid.addWidget(self.fill_holes_slider, 7, 1)
        self.fill_holes_value_label = _label(f"{int(self._params['fill_holes'])}", "#0ff")
        self.fill_holes_value_label.setMinimumWidth(42)
        self.fill_holes_value_label.setToolTip(_FILL_TOOLTIP)
        grid.addWidget(self.fill_holes_value_label, 7, 2)

        # --- FG SOURCE: NN vs SOURCE radio buttons ---
        # Path A "warm wardrobe rescue" (yellow shirts going pink). NN = current
        # behavior (model paints the FG); SOURCE = use the original plate inside
        # the matte and let despill clean any green spill (Mocha/Keylight style).
        # BLEND is built into the engine but intentionally not exposed in v1 UI.
        # Default NN — render output stays bit-identical until the user opts in.
        _FGSRC_TOOLTIP = ("FG SOURCE — what color goes inside the matte.\n"
                          "NN: model's predicted FG (default). Can paint warm wardrobe pink.\n"
                          "SOURCE: original plate, despilled. Real color, more spill risk.")
        self.fg_source_label_widget = _label("FG SOURCE")
        self.fg_source_label_widget.setToolTip(_FGSRC_TOOLTIP)
        grid.addWidget(self.fg_source_label_widget, 8, 0)
        _fgsrc_row = QtWidgets.QWidget()
        _fgsrc_row.setStyleSheet("background: transparent; border: none;")
        _fgsrc_layout = QtWidgets.QHBoxLayout(_fgsrc_row)
        _fgsrc_layout.setContentsMargins(0, 0, 0, 0)
        _fgsrc_layout.setSpacing(8)
        self.fg_source_group = QtWidgets.QButtonGroup(self)
        self.fg_source_btn_nn = QtWidgets.QRadioButton("NN")
        self.fg_source_btn_src = QtWidgets.QRadioButton("SOURCE")
        for _b in (self.fg_source_btn_nn, self.fg_source_btn_src):
            _b.setStyleSheet(
                "QRadioButton { color: #8ab; border: none; background: transparent; "
                "font-size: 12px; font-weight: 600; letter-spacing: 0.5px; } "
                "QRadioButton::indicator { width: 12px; height: 12px; border: 1px solid "
                "#2a6a7a; border-radius: 6px; background: #001a28; } "
                "QRadioButton::indicator:checked { background: #0ff; border-color: #0ff; }"
            )
            _b.setToolTip(_FGSRC_TOOLTIP)
        self.fg_source_group.addButton(self.fg_source_btn_nn, 0)
        self.fg_source_group.addButton(self.fg_source_btn_src, 1)
        _cur_fg = str(self._params.get("fg_source", "nn")).lower()
        if _cur_fg == "source":
            self.fg_source_btn_src.setChecked(True)
        else:
            self.fg_source_btn_nn.setChecked(True)
        self.fg_source_group.buttonClicked.connect(self._on_fg_source_changed)
        _fgsrc_layout.addWidget(self.fg_source_btn_nn)
        _fgsrc_layout.addWidget(self.fg_source_btn_src)
        _fgsrc_layout.addStretch(1)
        grid.addWidget(_fgsrc_row, 8, 1, 1, 2)

        # --- SAM2 ADDITIVE: combine math toggle (checkbox) ---
        # Switches the NN+SAM2 combine from multiplicative (alpha = NN x gate,
        # default) to additive (alpha = max(NN, gate * non_screen)). Preserves
        # subject regions SAM2 misses across visual boundaries (e.g. a butt
        # across a stunt-rig strap). OFF = bit-identical to previous behavior.
        # SAM2-only — greyed out when SAM2 is inactive.
        _SAM2_ADDITIVE_TOOLTIP = (
            "SAM2 ADDITIVE — switches combine math from multiply (NN x SAM2, "
            "default) to additive (max of NN, SAM2). Preserves subject regions "
            "SAM2 misses across visual boundaries (straps, props). Off = "
            "bit-identical to before."
        )
        self.sam2_additive_label_widget = _label("SAM2 ADDITIVE")
        self.sam2_additive_label_widget.setToolTip(_SAM2_ADDITIVE_TOOLTIP)
        grid.addWidget(self.sam2_additive_label_widget, 9, 0)
        self.sam2_additive_checkbox = QtWidgets.QCheckBox("")
        self.sam2_additive_checkbox.setStyleSheet(
            "QCheckBox { color: #8ab; border: none; background: transparent; "
            "font-size: 12px; font-weight: 600; letter-spacing: 0.5px; } "
            "QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid "
            "#2a6a7a; border-radius: 2px; background: #001a28; } "
            "QCheckBox::indicator:checked { background: #0ff; border-color: #0ff; }"
        )
        self.sam2_additive_checkbox.setChecked(bool(self._params.get("sam2_additive", False)))
        self.sam2_additive_checkbox.setToolTip(_SAM2_ADDITIVE_TOOLTIP)
        self.sam2_additive_checkbox.toggled.connect(self._on_sam2_additive_changed)
        grid.addWidget(self.sam2_additive_checkbox, 9, 1, 1, 2)

        # SMART BLEND widget removed from UI 2026-05-01 (50/50 ghost confirmed
        # broken). Underlying flow + helper kept; param defaults to False.

        # --- SAM2 SUBTRACT: subtract-only combine toggle (checkbox) ---
        # NN owns the matte everywhere green exists. SAM2 is permitted to
        # subtract junk (floor under feet, off-screen props) only in regions
        # far from the green edge. EDGE GUARD slider controls the protected
        # buffer width. Wins over SMART BLEND + ADDITIVE when toggled.
        # SAM2-only — greyed out when SAM2 is inactive.
        _SAM2_SUBTRACT_TOOLTIP = (
            "SAM2 SUBTRACT — NN owns the matte where green exists. SAM2 can "
            "only kill non-green junk (floor under feet, props off-screen). "
            "Hair / fine detail protected automatically. EDGE GUARD slider "
            "controls how far past the green edge SAM2 may reach. Off = "
            "bit-identical to before."
        )
        self.sam2_subtract_label_widget = _label("SUBTRACT")
        self.sam2_subtract_label_widget.setToolTip(_SAM2_SUBTRACT_TOOLTIP)
        grid.addWidget(self.sam2_subtract_label_widget, 11, 0)
        self.sam2_subtract_checkbox = QtWidgets.QCheckBox("")
        self.sam2_subtract_checkbox.setStyleSheet(
            "QCheckBox { color: #8ab; border: none; background: transparent; "
            "font-size: 12px; font-weight: 600; letter-spacing: 0.5px; } "
            "QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid "
            "#2a6a7a; border-radius: 2px; background: #001a28; } "
            "QCheckBox::indicator:checked { background: #0ff; border-color: #0ff; }"
        )
        self.sam2_subtract_checkbox.setChecked(bool(self._params.get("sam2_subtract", False)))
        self.sam2_subtract_checkbox.setToolTip(_SAM2_SUBTRACT_TOOLTIP)
        self.sam2_subtract_checkbox.toggled.connect(self._on_sam2_subtract_changed)
        grid.addWidget(self.sam2_subtract_checkbox, 11, 1, 1, 2)

        # --- EDGE GUARD: distance buffer past green edge before SAM2 may kill ---
        # Slider 0-50 px integer. Higher = more body protection, less aggressive
        # SAM2 reach. Feather (soft transition) auto-set to half this value.
        # Only meaningful when SUBTRACT mode is on.
        _EDGE_GUARD_TOOLTIP = (
            "EDGE GUARD — distance (px) past the green edge before SUBTRACT "
            "may kill anything. Higher = more protection for body parts "
            "surrounded by green (no SAM2 dots needed there). A small soft "
            "transition tail extends past this distance. Default 20. Only "
            "used when SUBTRACT mode is on."
        )
        self.edge_guard_label_widget = _label("EDGE GUARD")
        self.edge_guard_label_widget.setToolTip(_EDGE_GUARD_TOOLTIP)
        grid.addWidget(self.edge_guard_label_widget, 12, 0)
        self.edge_guard_slider = JumpSlider(QtCore.Qt.Horizontal)
        self.edge_guard_slider.setRange(0, 200)
        self.edge_guard_slider.setValue(int(self._params.get("edge_guard_px", 20)))
        self.edge_guard_slider.valueChanged.connect(self._on_edge_guard_changed)
        self.edge_guard_slider.setToolTip(_EDGE_GUARD_TOOLTIP)
        grid.addWidget(self.edge_guard_slider, 12, 1)
        self.edge_guard_value_label = _label(f"{int(self._params.get('edge_guard_px', 20))}", "#0ff")
        self.edge_guard_value_label.setMinimumWidth(42)
        self.edge_guard_value_label.setToolTip(_EDGE_GUARD_TOOLTIP)
        grid.addWidget(self.edge_guard_value_label, 12, 2)

        # --- SAM2 BYPASS: master switch (checkbox) ---
        # When ON, skip ALL SAM2 combine paths and return NN alpha directly.
        # Lets the user A/B compare NN-only vs NN+SAM2 without clearing dots.
        _SAM2_BYPASS_TOOLTIP = (
            "BYPASS SAM2 — master switch. When ON, ignore SAM2 entirely "
            "and show CorridorKey only. Lets you A/B compare NN vs NN+SAM2 "
            "without clearing the dots."
        )
        self.sam2_bypass_label_widget = _label("BYPASS SAM2")
        self.sam2_bypass_label_widget.setToolTip(_SAM2_BYPASS_TOOLTIP)
        grid.addWidget(self.sam2_bypass_label_widget, 13, 0)
        self.sam2_bypass_checkbox = QtWidgets.QCheckBox("")
        self.sam2_bypass_checkbox.setStyleSheet(
            "QCheckBox { color: #8ab; border: none; background: transparent; "
            "font-size: 12px; font-weight: 600; letter-spacing: 0.5px; } "
            "QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid "
            "#2a6a7a; border-radius: 2px; background: #001a28; } "
            "QCheckBox::indicator:checked { background: #0ff; border-color: #0ff; }"
        )
        self.sam2_bypass_checkbox.setChecked(bool(self._params.get("sam2_bypass", False)))
        self.sam2_bypass_checkbox.setToolTip(_SAM2_BYPASS_TOOLTIP)
        self.sam2_bypass_checkbox.toggled.connect(self._on_sam2_bypass_changed)
        grid.addWidget(self.sam2_bypass_checkbox, 13, 1, 1, 2)

        # --- SHOW SAM2: viewer-only silhouette overlay (checkbox) ---
        # Draws a cyan outline of SAM2's mask on top of the matte/composite
        # view. Does NOT affect rendered output. For debugging dot placement.
        _SHOW_SAM2_TOOLTIP = (
            "SHOW SAM2 — overlays a cyan outline of SAM2's silhouette on "
            "the view. Display only; does not change render output. Use to "
            "check whether SAM2 is covering what you expect."
        )
        self.show_sam2_label_widget = _label("SHOW SAM2")
        self.show_sam2_label_widget.setToolTip(_SHOW_SAM2_TOOLTIP)
        grid.addWidget(self.show_sam2_label_widget, 14, 0)
        self.show_sam2_checkbox = QtWidgets.QCheckBox("")
        self.show_sam2_checkbox.setStyleSheet(
            "QCheckBox { color: #8ab; border: none; background: transparent; "
            "font-size: 12px; font-weight: 600; letter-spacing: 0.5px; } "
            "QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid "
            "#2a6a7a; border-radius: 2px; background: #001a28; } "
            "QCheckBox::indicator:checked { background: #0ff; border-color: #0ff; }"
        )
        self.show_sam2_checkbox.setChecked(bool(self._params.get("show_sam2", False)))
        self.show_sam2_checkbox.setToolTip(_SHOW_SAM2_TOOLTIP)
        self.show_sam2_checkbox.toggled.connect(self._on_show_sam2_changed)
        grid.addWidget(self.show_sam2_checkbox, 14, 1, 1, 2)

        grid.setColumnStretch(1, 1)
        parent_layout.addWidget(panel)

    # WHAT IT DOES: Despill slider handler. Slider is 0-100 integer, we scale to
    #   0.0-1.0 float. Updates local params, repaints, schedules a debounced save.
    # DEPENDS-ON: self._params, _render_now, _schedule_save.
    # AFFECTS: self._params["despill"], self.despill_value_label, repaint, pending save.
    def _on_despill_slider_changed(self, value: int):
        v = value / 100.0
        self._params["despill"] = v
        self.despill_value_label.setText(f"{v:.2f}")
        self._schedule_render()
        self._schedule_save()

    # WHAT IT DOES: Despeckle on/off checkbox. Toggles the boolean flag and enables
    #   or disables the size slider so it's visually clear which state we're in.
    # DEPENDS-ON: self._params, self.despeckle_slider.
    # AFFECTS: self._params["despeckle"], self.despeckle_slider enabled state.
    def _on_despeckle_toggled(self, checked: bool):
        self._params["despeckle"] = bool(checked)
        self.despeckle_slider.setEnabled(checked)
        self._local_change_time = time.perf_counter()  # guard poller for 500ms so it doesn't read stale file
        self._last_activity_time = self._local_change_time  # reset idle clock → snap poll back to 50ms
        self._render_now()
        self._save_live_params_now()  # checkbox is discrete — save immediately so Process Frame sees it

    # WHAT IT DOES: Despeckle size slider (50-2000 px area threshold). Updates
    #   the params dict, shows the integer in the label, repaints.
    # DEPENDS-ON: self._params, _render_now.
    # AFFECTS: self._params["despeckleSize"], self.despeckle_value_label, repaint.
    def _on_despeckle_size_changed(self, value: int):
        self._params["despeckleSize"] = int(value)
        self.despeckle_value_label.setText(f"{value}")
        # FIX B: Was _render_now() — on 4K frames each render takes 50-300ms so
        # dragging fired 20+ renders and locked the UI. _schedule_render() uses a
        # 60ms debounce timer so only one render fires after the drag settles.
        self._schedule_render()
        self._schedule_save()

    def _on_choke_changed(self, value: int):
        self._params["choke"] = int(value)
        self.choke_value_label.setText(f"{value}")
        # Use throttled render — same reason as FIX B above. Each render on a
        # 4K frame is 50-300ms; calling _render_now() per slider tick blocks
        # the Qt event loop so the slider can't track the mouse cursor. The
        # _schedule_render() cooldown caps renders at ~16fps and keeps drag responsive.
        self._schedule_render()
        self._schedule_save()

    def _on_margin_changed(self, value: int):
        # Slider 0-800; divide by 10 → 0.0-80.0 px in 0.1 steps.
        v = value / 10.0
        self._params["sam2_margin"] = v
        self.margin_value_label.setText(f"{v:.1f}")
        self._schedule_render()
        self._schedule_save()

    def _on_soften_changed(self, value: int):
        # Slider 0-200; divide by 10 → 0.0-20.0 px in 0.1 steps.
        v = value / 10.0
        self._params["sam2_soften"] = v
        self.soften_value_label.setText(f"{v:.1f}")
        self._schedule_render()
        self._schedule_save()

    def _on_halo_changed(self, value: int):
        # HALO FEET — SAM2 gate dilation in non-green zones (feet, floor).
        # Slider 0-150 px integer. SAM2-only — visible effect requires an
        # active SAM2 mask (greyed-out otherwise via _update_sam2_slider_state).
        self._params["halo_px"] = int(value)
        self.halo_value_label.setText(f"{int(value)}")
        self._schedule_render()
        self._schedule_save()

    def _on_halo_body_changed(self, value: int):
        # HALO BODY — SAM2 gate dilation in green-bordered zones (body
        # silhouette). Slider 0-150 px integer. SAM2-only — visible effect
        # requires an active SAM2 mask (greyed-out otherwise via
        # _update_sam2_slider_state).
        self._params["halo_body_px"] = int(value)
        self.halo_body_value_label.setText(f"{int(value)}")
        self._schedule_render()
        self._schedule_save()

    def _on_trim_chroma_changed(self, value: int):
        # Slider 0-100 integer. SAM2-only — visible effect requires an active
        # SAM2 mask (greyed-out otherwise via _update_sam2_slider_state).
        # 0 = bit-identical to no-trim path.
        self._params["trim_chroma"] = int(value)
        self.trim_chroma_value_label.setText(f"{int(value)}")
        self._schedule_render()
        self._schedule_save()

    def _on_fill_holes_changed(self, value: int):
        # Slider 0-100 integer. SAM2-only — visible effect requires an active
        # SAM2 mask (greyed-out otherwise via _update_sam2_slider_state).
        # 0 = bit-identical (helper short-circuits before any work).
        self._params["fill_holes"] = int(value)
        self.fill_holes_value_label.setText(f"{int(value)}")
        self._schedule_render()
        self._schedule_save()

    # WHAT IT DOES: FG SOURCE radio handler. Switches between NN (model FG —
    #   default) and SOURCE (original plate inside the matte — Mocha-style
    #   warm-wardrobe rescue). Writes to live_params.json immediately so a
    #   subsequent PROCESS RANGE picks up the choice without re-touching UI.
    # DEPENDS-ON: self.fg_source_group ids — 0=NN, 1=SOURCE.
    # AFFECTS: self._params["fg_source"], repaints, persists to disk.
    def _on_fg_source_changed(self, btn):
        try:
            _id = self.fg_source_group.id(btn)
        except Exception:
            _id = 0
        v = "source" if _id == 1 else "nn"
        self._params["fg_source"] = v
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time
        self._render_now()
        self._save_live_params_now()  # discrete control — save immediately so PROCESS RANGE sees it

    # WHAT IT DOES: SAM2 ADDITIVE checkbox handler. Toggles the combine math
    #   between multiplicative (default, NN x SAM2) and additive
    #   (max(NN, SAM2 x non_screen)). Discrete control — saves immediately so
    #   PROCESS RANGE sees the choice without a slider-debounce delay.
    # DEPENDS-ON: self._params, _render_now, _save_live_params_now.
    # AFFECTS: self._params["sam2_additive"], repaint, persists to disk.
    def _on_sam2_additive_changed(self, checked: bool):
        self._params["sam2_additive"] = bool(checked)
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time
        self._render_now()
        self._save_live_params_now()

    # WHAT IT DOES: SMART BLEND checkbox handler. Toggles per-pixel weighted
    #   NN/SAM2 combine. Wins over sam2_additive when both checked. Discrete
    #   control — saves immediately so PROCESS RANGE sees the choice.
    # DEPENDS-ON: self._params, _render_now, _save_live_params_now.
    # AFFECTS: self._params["sam2_weighted"], repaint, persists to disk.
    def _on_sam2_weighted_changed(self, checked: bool):
        self._params["sam2_weighted"] = bool(checked)
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time
        self._render_now()
        self._save_live_params_now()

    # WHAT IT DOES: SAM2 SUBTRACT checkbox handler. Toggles subtract-only
    #   combine. Wins over sam2_weighted + sam2_additive when checked.
    #   Discrete control — saves immediately.
    # AFFECTS: self._params["sam2_subtract"], repaint, persists to disk.
    def _on_sam2_subtract_changed(self, checked: bool):
        self._params["sam2_subtract"] = bool(checked)
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time
        self._render_now()
        self._save_live_params_now()

    # WHAT IT DOES: EDGE GUARD slider handler. 0-50 px integer. Only meaningful
    #   when SUBTRACT mode is on. Continuous control — debounced save.
    def _on_edge_guard_changed(self, value: int):
        self._params["edge_guard_px"] = int(value)
        self.edge_guard_value_label.setText(f"{int(value)}")
        self._schedule_render()
        self._schedule_save()

    # WHAT IT DOES: BYPASS SAM2 master toggle handler. When ON, all SAM2 paths
    #   are skipped and NN alpha returns directly. Discrete control.
    def _on_sam2_bypass_changed(self, checked: bool):
        self._params["sam2_bypass"] = bool(checked)
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time
        self._render_now()
        self._save_live_params_now()

    # WHAT IT DOES: SHOW SAM2 overlay toggle handler. Viewer-only; does not
    #   affect render output. Discrete control.
    def _on_show_sam2_changed(self, checked: bool):
        self._params["show_sam2"] = bool(checked)
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time
        self._render_now()
        self._save_live_params_now()

    # WHAT IT DOES: Greys out the SAM2-only controls (Margin, Soften) when
    #   no SAM2 mask is active. This makes it visually obvious to the user
    #   that those sliders aren't doing anything until they engage SAM2.
    #   "Active" = self.session.sam2_gate_raw exists (i.e. APPLY MASK has
    #   been pressed and produced a mask, or the panel wrote one for the
    #   current scrub frame).
    # DEPENDS-ON: self.margin_slider, self.margin_value_label,
    #   self.soften_slider, self.soften_value_label, self.session.
    # AFFECTS: setEnabled state on those four widgets only.
    # DANGER ZONE FRAGILE: do NOT also disable the close-kernel slider here
    #   if/when it gets added — it should follow the same pattern in its
    #   own helper, since adding more SAM2 controls means this list grows.
    def _update_sam2_slider_state(self):
        sam2_active = (self.session is not None
                       and self.session.sam2_gate_raw is not None)
        for w in (self.margin_slider, self.margin_value_label,
                  self.soften_slider, self.soften_value_label,
                  self.margin_label_widget, self.soften_label_widget,
                  self.halo_slider, self.halo_value_label,
                  self.halo_label_widget,
                  self.halo_body_slider, self.halo_body_value_label,
                  self.halo_body_label_widget,
                  self.fill_holes_slider, self.fill_holes_value_label,
                  self.fill_holes_label_widget,
                  self.sam2_additive_checkbox,
                  self.sam2_additive_label_widget,
                  self.sam2_subtract_checkbox,
                  self.sam2_subtract_label_widget,
                  self.edge_guard_slider,
                  self.edge_guard_value_label,
                  self.edge_guard_label_widget,
                  self.sam2_bypass_checkbox,
                  self.sam2_bypass_label_widget,
                  self.show_sam2_checkbox,
                  self.show_sam2_label_widget):
            try:
                w.setEnabled(sam2_active)
            except Exception:
                pass
        # Make the OFF state unmissable — Qt's default disabled-look is too
        # subtle to read at a glance. Swap the left-label text so the user
        # sees "MARGIN — OFF (needs Click to Mask)" instead of just a slightly
        # dimmer "MARGIN". When SAM2 engages, restore the plain labels.
        if sam2_active:
            try: self.margin_label_widget.setText("MARGIN")
            except Exception: pass
            try: self.soften_label_widget.setText("SOFTEN")
            except Exception: pass
            try: self.halo_label_widget.setText("HALO FEET")
            except Exception: pass
            try: self.halo_body_label_widget.setText("HALO BODY")
            except Exception: pass
            try: self.fill_holes_label_widget.setText("FILL HOLES")
            except Exception: pass
        else:
            # Short suffix — long phrasing crowded the slider row. Tooltip still
            # carries the full "needs Click to Mask" explanation on hover.
            try: self.margin_label_widget.setText("MARGIN (mask)")
            except Exception: pass
            try: self.soften_label_widget.setText("SOFTEN (mask)")
            except Exception: pass
            try: self.halo_label_widget.setText("HALO FEET (mask)")
            except Exception: pass
            try: self.halo_body_label_widget.setText("HALO BODY (mask)")
            except Exception: pass
            try: self.fill_holes_label_widget.setText("FILL HOLES (mask)")
            except Exception: pass

    # WHAT IT DOES: Debounces live_params.json writes. Every slider move calls this;
    #   the actual disk write happens 250ms after the LAST move. Prevents filesystem
    #   thrash on rapid drags.
    # DEPENDS-ON: self._save_timer QTimer already built in __init__.
    # AFFECTS: restarts the save timer — does not touch disk.
    def _schedule_render(self):
        # Throttle: render immediately if not in cooldown, otherwise flag for next tick.
        self._render_pending = True
        if not self._render_timer.isActive():
            self._on_render_throttle_tick()

    def _on_render_throttle_tick(self):
        if self._render_pending:
            self._render_pending = False
            self._render_now()
            self._render_timer.start()  # start 60ms cooldown for next tick

    def _schedule_save(self):
        self._local_change_time = time.perf_counter()
        self._last_activity_time = self._local_change_time  # reset idle clock → snap poll back to 50ms
        self._save_timer.start()

    # WHAT IT DOES: Atomically writes current self._params to live_params.json.
    #   Write to .tmp, os.replace to final — same-volume atomic. Records the
    #   new mtime in self._suppress_poll_mtime so our own poll doesn't loop back
    #   and re-render with identical values.
    # DEPENDS-ON: self._live_params_path, self._params, self._save_timer (callback).
    # AFFECTS: writes live_params.json in session_dir. Sets self._suppress_poll_mtime.
    def _save_live_params_now(self):
        try:
            tmp = str(self._live_params_path) + ".tmp"
            payload = dict(self._params)
            # Write SAM2 click points so the panel can use them during render
            if self._sam_display_pts:
                ih, iw = self.session.shape_hw
                # Each click stores its OWN frame_num (the frame the user was viewing
                # when they placed it). This lets the panel place each click on its
                # correct frame during SAM2 video propagation, instead of forcing
                # all clicks onto a single global anchor frame. Fixes the bug where
                # adding a refining click after the playhead moved invalidated the
                # original clicks (because they got re-anchored to the new frame).
                payload["sam_clicks"] = [
                    {"x": int(nx * iw), "y": int(ny * ih), "label": 1 if v else 0,
                     "frame": fr}
                    for nx, ny, v, fr in self._sam_display_pts
                ]
                # Legacy fields for back-compat with code paths that haven't been
                # updated yet. sam_anchor_frame falls back to the FIRST click's
                # frame (the original anchor). Most paths will read sam_clicks.
                payload["sam_positive"] = [[int(nx * iw), int(ny * ih)] for nx, ny, v, _ in self._sam_display_pts if v]
                payload["sam_negative"] = [[int(nx * iw), int(ny * ih)] for nx, ny, v, _ in self._sam_display_pts if not v]
                payload["alpha_method"] = 1
                _frames = [fr for _, _, _, fr in self._sam_display_pts if fr is not None]
                if _frames:
                    payload["sam_anchor_frame"] = _frames[0]
                else:
                    # Fall back to current meta.frame_num if no click had a frame
                    # captured (shouldn't happen post-fix, but be safe).
                    try:
                        meta_path = self.session.session_dir / "meta.json"
                        if meta_path.exists():
                            import json as _json
                            meta = _json.loads(meta_path.read_text())
                            payload["sam_anchor_frame"] = meta.get("frame_num")
                    except Exception:
                        pass
            else:
                payload["sam_clicks"] = []
                payload["sam_positive"] = []
                payload["sam_negative"] = []
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._live_params_path)
            try:
                self._suppress_poll_mtime = self._live_params_path.stat().st_mtime
            except OSError:
                pass
        except Exception as e:
            # Best-effort — don't crash the viewer if the session dir is gone.
            print(f"[viewer] live_params save failed: {e}", file=sys.stderr)

    # ===== Commands from stdin =====
    # WHAT IT DOES: Merges incoming params into the live params dict, then schedules
    #   a repaint. If already painting, the new update is queued as _pending and
    #   overwrites any prior pending update (drop-stale policy).
    # DEPENDS-ON: _render_now() for the actual work.
    # AFFECTS: self._params, self._pending, self._painting.
    @QtCore.Slot(dict)
    def on_update(self, params: dict):
        # Show/hide processing overlay for refiner re-key — simple pulse,
        # no timer counting up (watching seconds tick feels slower).
        if params.get("rekeying") is True:
            self._overlay.setGeometry(0, 0, self.right_label.width(), self.right_label.height())
            self._overlay.setText("Re-keying...")
            self._overlay.show()
            self._overlay.raise_()
            return
        # FIX: Build merged params and sync self._params BEFORE any _render_now() call.
        # Previously, merged was built AFTER the rekeying:false block, which calls
        # _set_view_mode() -> _render_now(). That render used stale self._params (the
        # hardcoded defaults, e.g. despeckle=True) instead of the values from the file.
        # The checkbox was then synced correctly, but self._params was not updated until
        # after the render. If the 300ms singleShot fired between those two points, or
        # if the rekeying render was the only render (no second _render_now at the end
        # when _painting was True from within the rekeying block), the display stayed
        # stale. Fix: update self._params from the file values first, then render once
        # with the correct state.
        merged = dict(self._params)
        for k, v in params.items():
            if k in ("despill", "despeckle", "despeckleSize", "background", "fg_source", "trim_chroma", "fill_holes", "sam2_additive", "sam2_weighted", "sam2_subtract", "edge_guard_px", "sam2_bypass", "show_sam2"):
                merged[k] = v
        # Sync checkbox UI and self._params NOW — before any render fires — so that
        # every code path below uses the correct despeckle value. blockSignals prevents
        # _on_despeckle_toggled from firing (and re-saving) during the programmatic sync.
        if "despeckle" in params:
            new_val = bool(merged["despeckle"])
            if self.despeckle_cb.isChecked() != new_val:
                self.despeckle_cb.blockSignals(True)
                self.despeckle_cb.setChecked(new_val)
                self.despeckle_slider.setEnabled(new_val)
                self.despeckle_cb.blockSignals(False)
        # Commit the merged params immediately so any _render_now() called below
        # (including from _set_view_mode) sees the correct despeckle value.
        self._params = merged
        if params.get("rekeying") is False:
            self._overlay.hide()
            # BUG FIX: If we're in scrub mode, do NOT reload the single-frame PNGs —
            # that would overwrite session.fg_rgb/alpha with the original frame and wipe
            # the scrub frame the user is looking at. Just re-render with the current
            # scrub frame using the newly-merged params (margin, soften, despill, etc.).
            if self._scrub_base is not None:
                if not self._painting:
                    self._render_now()
                else:
                    self._pending = merged
                return
            # Panel signals cache is done — PNGs are fully written, safe to read.
            rekey_rendered = False
            try:
                self.session.reload_pngs()
                _orig_src = (self.session.original_rgb
                             if self.session.original_rgb is not None
                             else self.session.fg_rgb)
                self.original_u8 = np.clip(
                    _orig_src * 255.0, 0, 255
                ).astype(np.uint8)
                self._place_original()
                self._set_view_mode("Composite")
                rekey_rendered = True
            except Exception:
                pass  # PNGs unreadable — stale view is better than a crash
            if rekey_rendered:
                # _set_view_mode already called _render_now() — don't double-render.
                return
            # PNG reload failed — fall through to the normal render path below so
            # the viewer at least repaints with the updated params.
        if self._painting:
            self._pending = merged
            return
        self._render_now()

    @QtCore.Slot(str)
    def on_reload(self, session_dir: str):
        try:
            new_session = Session(Path(session_dir))
        except Exception as e:
            self.status.setText(f"Reload failed: {e}")
            return
        self.session = new_session
        _orig_src = (new_session.original_rgb
                     if new_session.original_rgb is not None
                     else new_session.fg_rgb)
        self.original_u8 = np.clip(_orig_src * 255.0, 0, 255).astype(np.uint8)
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
                # Show the same alpha the composite uses: gate ceiling + margin + soften
                params_for_matte = dict(self._params)
                matte_margin = float(params_for_matte.get("sam2_margin", 0))
                matte_soften = float(params_for_matte.get("sam2_soften", 0))
                matte_halo = int(params_for_matte.get("halo_px", 0))
                matte_halo_body = int(params_for_matte.get("halo_body_px", 0))
                matte_trim = int(params_for_matte.get("trim_chroma", 0))
                matte_fill = int(params_for_matte.get("fill_holes", 0))
                matte_additive = bool(params_for_matte.get("sam2_additive", False))
                matte_weighted = bool(params_for_matte.get("sam2_weighted", False))
                matte_subtract = bool(params_for_matte.get("sam2_subtract", False))
                matte_bypass = bool(params_for_matte.get("sam2_bypass", False))
                matte_edge_guard = int(params_for_matte.get("edge_guard_px", 20))
                if (self.session.alpha_raw is not None
                        and self.session.sam2_gate_raw is not None
                        and not matte_bypass):
                    _gate = self.session.sam2_gate_raw.copy()
                    # SAM2 logits return at 256x256 — must resize to alpha shape or
                    # the multiply throws ValueError (silently caught by the outer
                    # try/except, leaving the matte view blank). The Composite
                    # branch in render_composite handles this; the Matte branch was
                    # missing the resize. Fixed 2026-04-26.
                    if _gate.shape != self.session.alpha_raw.shape:
                        _gate = cv2.resize(
                            _gate,
                            (self.session.alpha_raw.shape[1],
                             self.session.alpha_raw.shape[0]),
                            interpolation=cv2.INTER_LINEAR,
                        )
                    _src_rgb_m = (self.session.original_rgb
                                  if self.session.original_rgb is not None
                                  else self.session.fg_rgb)
                    if matte_subtract:
                        # SUBTRACT mirror — see Composite branch comment.
                        _fp_m = max(int(matte_edge_guard) // 2, 1)
                        alpha = apply_sam2_gate_subtract(self.session.alpha_raw,
                                                         _gate, _src_rgb_m,
                                                         screen_type="green",
                                                         buffer_px=int(matte_edge_guard),
                                                         feather_px=_fp_m)
                    elif matte_weighted:
                        # SMART BLEND mirror of Composite branch — per-pixel
                        # weighted NN/SAM2 by chroma. trim/fill_holes/halo
                        # intentionally not applied here either.
                        alpha = apply_sam2_gate_weighted(self.session.alpha_raw,
                                                         _gate, _src_rgb_m,
                                                         screen_type="green")
                    elif matte_additive:
                        # ADDITIVE mode mirrors the Composite branch. HALO still
                        # functional but its semantics shift (extends additive
                        # contribution outward rather than preserving NN edge band).
                        _gate_for_add = _gate
                        if matte_halo and matte_halo > 0:
                            _bin = (_gate_for_add > 0.5).astype(np.uint8)
                            _k = int(matte_halo) * 2 + 1
                            _kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_k, _k))
                            _gate_for_add = cv2.dilate(_bin, _kernel).astype(np.float32)
                        alpha = apply_sam2_gate_additive(self.session.alpha_raw,
                                                         _gate_for_add, _src_rgb_m,
                                                         screen_type="green")
                    else:
                        # Trimap fusion: solid 1.0 inside SAM2 confident core where
                        # NN saw something; multiply elsewhere. Real holes preserved.
                        alpha = _trimap_fuse(self.session.alpha_raw, _gate,
                                             source_rgb=_src_rgb_m, screen_type="green",
                                             trim_chroma=matte_trim, halo_px=matte_halo,
                                             halo_body_px=matte_halo_body,
                                             fill_holes=matte_fill)
                    if matte_margin > 0:
                        alpha = _dilate_mask(alpha, matte_margin)
                    if matte_soften > 0:
                        alpha = _soften_mask(alpha, matte_soften)
                else:
                    # SAM2 inactive — Matte view shows the raw NN alpha
                    # without margin/soften (those are SAM2-only controls).
                    alpha = self.session.alpha.copy()
                if params_for_matte.get("despeckle", True):
                    alpha = self.cu.clean_matte_opencv(
                        alpha, area_threshold=int(params_for_matte.get("despeckleSize", 400))
                    )
                img = alpha_to_rgb_u8(alpha)

            # SHOW SAM2: viewer-only overlay. Draws cyan outline of SAM2's
            # silhouette on top of whatever view is shown. Does not affect
            # rendered output. Skipped if no SAM2 gate exists.
            if bool(self._params.get("show_sam2", False)) and self.session is not None \
                    and self.session.sam2_gate_raw is not None and img is not None:
                try:
                    _g = self.session.sam2_gate_raw
                    if img.ndim == 3 and (_g.shape[0] != img.shape[0] or _g.shape[1] != img.shape[1]):
                        _g = cv2.resize(_g, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
                    _gb = (_g > 0.5).astype(np.uint8) * 255
                    _edges = cv2.Canny(_gb, 50, 150)
                    # Dilate by 1 pixel so the line is visible at any zoom level.
                    _edges = cv2.dilate(_edges, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
                    img = img.copy()
                    img[_edges > 0] = [0, 255, 255]  # cyan (RGB)
                except Exception:
                    pass

            # Cache the full-res result so window resizes can rescale without
            # re-running stage-2. Then paint it at the current label size.
            self._last_right_full = img
            self._paint_right(img)
            # Sync SAM2-only slider enabled state with whether SAM2 actually
            # produced a mask for this frame. Cheap (4 setEnabled calls);
            # safe to do every render.
            self._update_sam2_slider_state()
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
                # 16ms (~one 60fps frame) gives ASIO one full buffer cycle of breathing
                # room before the next render fires. singleShot(0) pumped immediately
                # on the next event loop iteration, adding measurable scheduler pressure
                # on Focusrite ASIO at 128-sample / 48kHz (2.7ms headroom).
                QtCore.QTimer.singleShot(16, self._render_now)

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
            x0, y0, cw, ch = 0, 0, iw, ih
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
        if label is self.right_label:
            self._last_right_geom = dict(
                x0=x0, y0=y0, cw=cw, ch=ch, tw=tw, th=th, iw=iw, ih=ih, lw=lw, lh=lh
            )

    def _paint_right(self, full_img):
        self._paint_into(self.right_label, full_img)
        # Skip SAM dots during scrub mode — dots are anchor-frame-only and would
        # be misleading on other frames. _scrub_base is non-None while scrub is active.
        if self._sam_display_pts and self._scrub_base is None:
            self._draw_sam_overlay()

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

    # WHAT IT DOES: Toggles SAM click-to-mask mode. When ON, crosshair cursor shown
    #   on the composite pane and clicks are captured as include/exclude points.
    #   When OFF, normal pan/zoom behavior resumes and cursor resets.
    # DEPENDS-ON: self._sam_mode, self._sam_btn, self.right_label.
    # AFFECTS: self._sam_mode, button style, right_label cursor.
    def _toggle_sam_mode(self, checked: bool):
        self._sam_mode = checked
        if checked:
            self._sam_btn.setStyleSheet(
                "background-color: #4a3a1a; color: #da5; padding: 5px 14px; "
                "border: 1px solid #da5; border-radius: 12px; font-size: 12px; font-weight: 600;"
            )
            self.right_label.setCursor(QtCore.Qt.CrossCursor)
            self.status.setText("SAM mode ON — left-click: include (+)  right-click: exclude (−)")
        else:
            self._sam_btn.setStyleSheet(
                "background-color: #111; color: #da5; padding: 5px 14px; "
                "border: 1px solid #da5; border-radius: 12px; font-size: 12px; font-weight: 600;"
            )
            self.right_label.setCursor(QtCore.Qt.ArrowCursor)
            self.status.setText("SAM mode OFF")

    # WHAT IT DOES: Event filter on right_label — captures mouse clicks when SAM mode
    #   is active. Left-click = positive (include) point, right-click = negative (exclude).
    #   Stores display coords, redraws overlay. Returns True to consume the event.
    # DEPENDS-ON: self._sam_mode, self._sam_display_pts, self._draw_sam_overlay.
    # AFFECTS: self._sam_display_pts, right_label pixmap overlay.
    def eventFilter(self, obj, event):
        if obj is self.right_label and self._sam_mode:
            if event.type() == QtCore.QEvent.MouseButtonPress:
                is_pos = event.button() == QtCore.Qt.LeftButton
                is_neg = event.button() == QtCore.Qt.RightButton
                if is_pos or is_neg:
                    p = event.pos()
                    g = self._last_right_geom
                    if g and g["iw"] > 0 and g["ih"] > 0:
                        # Convert label coords → normalized image coords (0..1)
                        x_off = (g["lw"] - g["tw"]) // 2
                        y_off = (g["lh"] - g["th"]) // 2
                        px = p.x() - x_off
                        py = p.y() - y_off
                        if 0 <= px < g["tw"] and 0 <= py < g["th"]:
                            nx = (g["x0"] + px * g["cw"] / g["tw"]) / g["iw"]
                            ny = (g["y0"] + py * g["ch"] / g["th"]) / g["ih"]
                            # Read meta.json fresh — session.meta is only loaded at
                            # __init__ and won't reflect a later PREVIEW that updated
                            # the playhead. Each click captures the current frame.
                            _click_frame = None
                            try:
                                _meta_path = self.session.session_dir / "meta.json"
                                if _meta_path.exists():
                                    _meta = json.loads(_meta_path.read_text(encoding="utf-8"))
                                    _click_frame = _meta.get("frame_num")
                            except Exception:
                                pass
                            self._sam_display_pts.append((nx, ny, bool(is_pos), _click_frame))
                    pos_count = sum(1 for t in self._sam_display_pts if t[2])
                    neg_count = len(self._sam_display_pts) - pos_count
                    self.status.setText(f"SAM points: {pos_count}+ {neg_count}−  (right-click=exclude)")
                    self._draw_sam_overlay()
                    return True
        return super().eventFilter(obj, event)

    # WHAT IT DOES: Clears all SAM click points, deletes sam2_mask.png gate, and
    #   restores the NN alpha backup. No confirm dialog — the dialog was invisible
    #   on Windows with WindowStaysOnTopHint (rendered behind the viewer), causing
    #   users to click CLEAR 4+ times with no visible response. Status bar confirms.
    # DEPENDS-ON: self._sam_display_pts, _repaint_both to redraw without dots.
    # AFFECTS: self._sam_display_pts emptied, sam2_mask.png deleted, status updated.
    def _clear_sam_points(self):
        sam2_mask_path = self.session.session_dir / "sam2_mask.png"
        sam2_gate_path = self.session.session_dir / "sam2_gate_raw.png"
        self._sam_display_pts = []
        # Delete BOTH the binary mask AND the raw gate. Previously only
        # sam2_mask.png was deleted; sam2_gate_raw.png lingered on disk and
        # render paths kept reading it, producing a "weird black mass" over
        # the body and corrupted Composite/Matte views — the user had to
        # close + reopen the viewer to fully reset. Deleting both files is
        # the only way to land in a true "no SAM2" state.
        for _p in (sam2_mask_path, sam2_gate_path):
            try:
                if _p.exists():
                    _p.unlink()
            except Exception:
                pass
        # Restore the NN alpha that _apply_sam_mask backed up before overwriting alpha.png.
        # Without this, CLEAR leaves alpha.png as the SAM2 binary → actress disappears.
        nn_backup = self.session.session_dir / "alpha_nn_backup.png"
        alpha_path = self.session.session_dir / "alpha.png"
        try:
            if nn_backup.exists():
                import shutil as _shutil
                _shutil.copy2(str(nn_backup), str(alpha_path))
                nn_backup.unlink()
        except Exception:
            pass
        try:
            self.session.reload_pngs()
        except Exception:
            pass
        # Belt-and-suspenders: reload_pngs() already clears in-memory
        # sam2_gate_raw to None, but if reload_pngs threw above the gate
        # might still point at the previous SAM2 result. Force-clear here
        # so the next render unconditionally takes the no-SAM2 branch.
        try:
            self.session.sam2_gate_raw = None
        except Exception:
            pass
        self._save_live_params_now()
        # Sync the SAM2-only slider grey-out immediately — without this,
        # the user sees MARGIN/SOFTEN stay enabled until the next render
        # tick, which is confusing (CLEAR is supposed to be instantaneous).
        try:
            self._update_sam2_slider_state()
        except Exception:
            pass
        self.status.setText("SAM2 mask cleared — NN alpha restored")
        self._render_now()

    # WHAT IT DOES: Runs SAM2 on the cached source frame using the user's click points.
    #   Converts normalized image coords back to full-res pixel coords, loads SAM2 from
    #   the engine's sam2_weights folder, predicts the best mask, writes alpha.png to the
    #   session dir, reloads the session, and re-renders so the viewer shows the new key.
    # DEPENDS-ON: self._sam_display_pts (normalized coords), self.session.fg_rgb (source
    #   frame as float32 RGB), sam2 package in the engine venv, session dir writable.
    # AFFECTS: session_dir/alpha.png overwritten, session.alpha reloaded, repaint triggered.
    # DANGER ZONE HIGH: Synchronous — Qt event loop freezes during SAM2 inference (~2-5s).
    def _apply_sam_mask(self):
        if not self._sam_display_pts:
            self.status.setText("No points — click on image first")
            return
        self.status.setText("Running SAM2…")
        QtWidgets.QApplication.processEvents()
        try:
            import cv2, numpy as np, torch, os
            ih, iw = self.session.shape_hw
            pos_pts = [(int(nx * iw), int(ny * ih)) for nx, ny, v, _ in self._sam_display_pts if v]
            neg_pts = [(int(nx * iw), int(ny * ih)) for nx, ny, v, _ in self._sam_display_pts if not v]
            all_pts = [[p[0], p[1]] for p in pos_pts] + [[p[0], p[1]] for p in neg_pts]
            labels  = [1] * len(pos_pts) + [0] * len(neg_pts)
            if not all_pts:
                self.status.setText("No valid points in image bounds")
                return
            # Source image for SAM2: uint8 RGB (session stores float32 RGB 0..1)
            # Tried feeding original.png (raw greenscreen) — didn't help, reverted.
            frame_rgb = np.clip(self.session.fg_rgb * 255.0, 0, 255).astype(np.uint8)
            # CK_ROOT is two levels up from this script (resolve_plugin/ → engine root)
            ck_root = Path(__file__).parent.parent
            ckpt = str(ck_root / "sam2_weights" / "sam2.1_hiera_small.pt")
            cfg   = "configs/sam2.1/sam2.1_hiera_s.yaml"
            device = "cuda" if torch.cuda.is_available() else "cpu"
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
            model = build_sam2(cfg, ckpt, device=device)
            pred  = SAM2ImagePredictor(model)
            pred.set_image(frame_rgb)
            # return_logits=True returns full-res raw logits (clamped to [-32, +32]
            # by SAM2). Use masks[best_idx] — the full-res slot — not low_res_masks
            # (256x256), because upscaling soft sigmoid values 15x bakes 256-grid
            # banding into the matte.
            masks, scores, _low_res = pred.predict(
                point_coords=np.array(all_pts),
                point_labels=np.array(labels),
                multimask_output=True,
            )
            del pred, model
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            best_idx = int(np.argmax(scores))
            # Standard SAM2 sigmoid output. The earlier saturation ramp
            # (commit bcb376c) hard-clipped logits below -2 to zero, which
            # erased subject regions separated by visual boundaries (e.g.
            # an actor's butt across a stunt-rig strap). Sigmoid keeps those
            # regions at low-but-nonzero alpha so they survive the multiply
            # and can be recovered with HALO/MARGIN if needed.
            best = masks[best_idx].astype(np.float32)
            # Backup the NN alpha if it exists and hasn't been backed up yet.
            # CLEAR restores this backup so the actress comes back without re-processing.
            alpha_path = self.session.session_dir / "alpha.png"
            nn_backup  = self.session.session_dir / "alpha_nn_backup.png"
            if alpha_path.exists() and not nn_backup.exists():
                import shutil as _shutil
                _shutil.copy2(str(alpha_path), str(nn_backup))
            # Save soft gate as uint16 PNG so the 0..1 precision survives the
            # save/load roundtrip (uint8 would quantize to 256 levels and undo
            # the soft-edge benefit). _to_float01 handles uint16 on read.
            gate_u16  = (best * 65535.0).astype(np.uint16)
            gate_path = self.session.session_dir / "sam2_gate_raw.png"
            gate_tmp  = self.session.session_dir / "sam2_gate_raw.tmp.png"
            cv2.imwrite(str(gate_tmp), gate_u16)
            os.replace(str(gate_tmp), str(gate_path))
            # sam2_mask.png stays binary uint8 — it's consumed by the panel's
            # batch path which expects the legacy hard-mask contract.
            sam2_mask_u8   = (best > 0.5).astype(np.uint8) * 255
            sam2_mask_path = self.session.session_dir / "sam2_mask.png"
            sam2_tmp_path  = self.session.session_dir / "sam2_mask.tmp.png"
            cv2.imwrite(str(sam2_tmp_path), sam2_mask_u8)
            os.replace(str(sam2_tmp_path), str(sam2_mask_path))
            # reload_pngs() must run BEFORE assigning the new gate — it
            # unconditionally clears sam2_gate_raw to None (so a new frame
            # never inherits the previous frame's gate). Calling it after
            # the assignment would wipe the gate we just computed and
            # Apply Mask would silently do nothing.
            self.session.reload_pngs()
            self.session.sam2_gate_raw = best.copy()
            # Keep points visible so the user can add/refine and re-apply —
            # each predict() takes ALL current points, so wiping them here
            # forced users to re-click everything. CLEAR button wipes manually.
            self._render_now()
            self._save_live_params_now()  # write points + anchor to disk NOW, not on timer
            self.status.setText(f"SAM2 mask applied — {len(pos_pts)}+ {len(neg_pts)}-")
        except Exception as e:
            self.status.setText(f"SAM2 error: {e}")

    # WHAT IT DOES: Draws colored dot overlays on the right_label pixmap for all SAM
    #   click points. Green filled circle = include (+), red = exclude (−). Called
    #   after every click and after every repaint so dots persist through re-renders.
    #   IMPORTANT: click coords are in right_label space (full label including black
    #   bars). The pixmap is centered inside the label, so we subtract the letterbox
    #   offsets before painting — otherwise dots appear in the black padding area.
    # DEPENDS-ON: self._sam_display_pts (label coords), right_label having a pixmap.
    # AFFECTS: right_label pixmap (in-place QPainter draw).
    def _draw_sam_overlay(self):
        if not self._sam_display_pts:
            return
        g = self._last_right_geom
        if not g:
            return
        pixmap = self.right_label.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        for nx, ny, is_pos, _ in self._sam_display_pts:
            # Normalized image → source image coords
            src_x = nx * g["iw"] - g["x0"]
            src_y = ny * g["ih"] - g["y0"]
            # Skip if outside the current zoom/pan crop window
            if src_x < 0 or src_y < 0 or src_x >= g["cw"] or src_y >= g["ch"]:
                continue
            # Crop coords → pixmap coords
            px = int(src_x * g["tw"] / g["cw"])
            py = int(src_y * g["th"] / g["ch"])
            fill = QtGui.QColor("#00ee00") if is_pos else QtGui.QColor("#ff3333")
            painter.setPen(QtGui.QPen(QtGui.QColor("#000000"), 2))
            painter.setBrush(QtGui.QBrush(fill))
            painter.drawEllipse(QtCore.QPoint(px, py), 9, 9)
            painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
            font = painter.font()
            font.setBold(True)
            font.setPixelSize(13)
            painter.setFont(font)
            painter.drawText(px - 4, py + 5, "+" if is_pos else "\u2212")
        painter.end()
        self.right_label.setPixmap(pixmap)

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

    # WHAT IT DOES: Start pan-drag on mouse press while zoomed in. Disabled in SAM mode.
    def mousePressEvent(self, event):
        if self._sam_mode:
            super().mousePressEvent(event)
            return
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
        close.setStyleSheet("background-color: rgba(0,255,255,0.2); color: #0ff; border: 1px solid rgba(0,255,255,0.3); border-radius: 6px; padding: 10px 30px;")
        close.clicked.connect(self.close)
        layout.addWidget(close, alignment=QtCore.Qt.AlignRight)
        self.setFixedSize(dw * 2 + 32, dh + 80)


_DARK_STYLE = """
/* ── CorridorKey Honeycomb Theme (Qt viewer) ── */
QWidget {
    background-color: #000;
    color: #e8e8e8;
    font-family: 'Inter', 'SF Pro Display', 'Segoe UI', sans-serif;
    font-size: 15px;
}
QPushButton {
    border: 1px solid #1a6a7a; border-radius: 6px;
    font-weight: 600; font-size: 14px; padding: 7px 18px;
    background-color: #0a2a3a; color: #0ff;
}
QPushButton:hover {
    background-color: #104050; color: #fff;
    border-color: #2af;
}
QPushButton:checked, QPushButton[active="true"] {
    background-color: #0ff; color: #000;
    border-color: #0ff;
}
QLabel {
    background-color: #000; border: 1px solid rgba(0,255,255,0.08);
    border-radius: 2px;
}
QSlider::groove:horizontal {
    height: 5px; background: rgba(0,20,40,0.5); border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 18px; height: 18px; margin: -7px 0;
    background: #000; border: 2px solid #0ff; border-radius: 9px;
}
QSlider::handle:horizontal:hover {
    border-color: #0ff;
}
QSlider::sub-page:horizontal {
    background: #0ff; border-radius: 2px;
}
QSlider::add-page:horizontal {
    background: rgba(0,20,40,0.5); border-radius: 2px;
}
"""


# ===== BRAW Scrubber Window =====
# WHAT IT DOES: Shows exported TIFF frames from a BRAW range with a scrubber slider.
#   Lets the user verify frame content (lighting, subject position, screen cleanliness)
#   across the full IN→OUT range before committing to Process Range.
# DEPENDS-ON: cv2, numpy, PySide6. Frames exported by on_scrub_range() in CorridorKey.py.
# AFFECTS: display only — reads TIFFs, writes nothing.
class BrawScrubberWindow(QtWidgets.QWidget):
    """Frame scrubber for BRAW TIFF sequences. No keying — raw camera frames only."""

    def __init__(self, frames_dir: Path, parent_pid: int = 0):
        super().__init__()
        self._frames_dir = Path(frames_dir)
        self._parent_pid = parent_pid
        self._frame_paths = sorted(self._frames_dir.glob("*.tif*"))
        self._n_frames = len(self._frame_paths)
        self._current_idx = 0
        self._build_ui()
        if self._n_frames > 0:
            self._show_frame(0)
        else:
            self._info_label.setText("No TIFF frames found in export directory")

    def _build_ui(self):
        self.setWindowTitle(f"CorridorKey — SCRUB RANGE  ({self._n_frames} frames)")
        self.setStyleSheet("background: #1a1a1a; color: #ccc;")
        self.setMinimumSize(900, 560)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Header
        hdr = QtWidgets.QLabel("SCRUB RANGE — verify frames before processing")
        hdr.setAlignment(QtCore.Qt.AlignCenter)
        hdr.setStyleSheet("color: #a5f; font-size: 13px; font-weight: bold; padding: 4px;")
        layout.addWidget(hdr)

        # Frame display
        self._image_label = QtWidgets.QLabel()
        self._image_label.setAlignment(QtCore.Qt.AlignCenter)
        self._image_label.setMinimumHeight(400)
        self._image_label.setStyleSheet("border: 1px solid #333; background: #111;")
        layout.addWidget(self._image_label, stretch=1)

        # Frame counter
        self._info_label = QtWidgets.QLabel("Loading…")
        self._info_label.setAlignment(QtCore.Qt.AlignCenter)
        self._info_label.setStyleSheet("color: #0dcaf0; font-size: 12px; font-weight: bold; padding: 2px;")
        layout.addWidget(self._info_label)

        # Scrubber slider
        self._slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._slider.setRange(0, max(0, self._n_frames - 1))
        self._slider.setValue(0)
        self._slider.setStyleSheet("""
            QSlider::groove:horizontal { height: 8px; background: #333; border-radius: 4px; }
            QSlider::handle:horizontal { background: #a5f; width: 20px; height: 20px;
                                         border-radius: 10px; margin: -6px 0; }
            QSlider::sub-page:horizontal { background: #a5f; border-radius: 4px; }
        """)
        self._slider.valueChanged.connect(self._show_frame)
        layout.addWidget(self._slider)

        # Step hint
        hint = QtWidgets.QLabel("Drag slider to scrub through frames  •  Check lighting, screen cleanliness, and subject position")
        hint.setAlignment(QtCore.Qt.AlignCenter)
        hint.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(hint)

        # Close button
        close_btn = QtWidgets.QPushButton("CLOSE SCRUBBER")
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #f66; border: 1px solid #f66; "
            "padding: 8px; border-radius: 3px; font-size: 12px; font-weight: bold; } "
            "QPushButton:hover { background: rgba(255,102,102,0.2); }"
        )
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

    def _show_frame(self, idx: int):
        if not self._frame_paths or idx >= self._n_frames:
            return
        fp = self._frame_paths[idx]
        img = cv2.imread(str(fp), cv2.IMREAD_UNCHANGED)
        if img is None:
            self._info_label.setText(f"Cannot read: {fp.name}")
            return
        # Normalise to 8-bit for display
        if img.dtype == np.uint16:
            img = (img >> 8).astype(np.uint8)
        elif img.dtype != np.uint8:
            img = np.clip(img.astype(np.float32) * 255, 0, 255).astype(np.uint8)
        # Convert colour space to RGB for Qt
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        elif img.shape[2] >= 4:
            img = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        q_img = QtGui.QImage(img.data.tobytes(), w, h, 3 * w, QtGui.QImage.Format_RGB888)
        pix = QtGui.QPixmap.fromImage(q_img)
        label_size = self._image_label.size()
        if label_size.width() > 0 and label_size.height() > 0:
            pix = pix.scaled(label_size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        self._image_label.setPixmap(pix)
        self._info_label.setText(f"Frame {idx + 1} / {self._n_frames}   —   {fp.name}")
        self._current_idx = idx


# ===== main =====
def _run_braw_scrubber(frames_dir: str, parent_pid: int):
    # WHAT IT DOES: Launches the BrawScrubberWindow as the sole Qt window.
    #   Called when preview_viewer_v2.py is invoked with --braw-scrubber <dir>.
    # DEPENDS-ON: BrawScrubberWindow, _parent_alive.
    # AFFECTS: opens a Qt window. Exits when user closes it.
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = BrawScrubberWindow(Path(frames_dir), parent_pid)
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
    if parent_pid > 0:
        watchdog = QtCore.QTimer()
        watchdog.setInterval(1000)
        watchdog.timeout.connect(
            lambda: app.quit() if not _parent_alive(parent_pid) else None
        )
        watchdog.start()
    sys.exit(app.exec())


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
    # NOTE: activateWindow() removed — it stole keyboard focus from Resolve
    # every time the viewer launched, making the panel unresponsive to typing.
    # raise_() is enough to bring the window to the front visually.
    # FIX A-2: 300ms delay gives the 50ms poll at least 6 cycles to load
    # live_params.json before the first render fires, so despeckle state is correct.
    QtCore.QTimer.singleShot(300, win._render_now)

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
    elif args and args[0] == "--braw-scrubber":
        # --braw-scrubber <frames_dir> [--session <dir>] [--parent-pid N]
        # The <frames_dir> is the first positional arg after --braw-scrubber.
        frames_dir = None
        parent_pid = 0
        i = 1
        while i < len(args):
            if args[i] == "--session" and i + 1 < len(args):
                i += 2  # session dir not needed for scrubber — skip
            elif args[i] == "--parent-pid" and i + 1 < len(args):
                try:
                    parent_pid = int(args[i + 1])
                except ValueError:
                    parent_pid = 0
                i += 2
            else:
                frames_dir = args[i]; i += 1
        if not frames_dir:
            print("ERROR: --braw-scrubber requires a frames directory", file=sys.stderr)
            sys.exit(2)
        _run_braw_scrubber(frames_dir, parent_pid)
    elif args:
        _run_oneshot(args[0])
    else:
        print(
            "Usage:\n"
            "  preview_viewer.py <json-paths>                              # one-shot\n"
            "  preview_viewer.py --persistent --session <dir> [--parent-pid N]  # live\n"
            "  preview_viewer.py --braw-scrubber <frames_dir> [--parent-pid N]  # scrubber",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
