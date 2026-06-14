#!/usr/bin/env python
# Last modified: 2026-05-29 | Change: CK_COMBINED merge in batch + surface silent SAM/merge fallbacks (CK_WARN); LIVE-FILE banner. | Full history: git log
# ============================================================================
# LIVE FILE — THIS is the canonical CorridorKey AE/Premiere processor (1180 lines,
# full SAM2 batch). The CEP panel runs THIS copy via a Windows junction:
#   %APPDATA%\Adobe\CEP\extensions\com.corridorkey.panel  ->  ae_plugin\cep_panel
# index.html spawns PANEL_DIR/ae_processor.py, which resolves to this file.
#
# DANGER ZONE FRAGILE: a 729-line look-alike DUMMY lives at ae_plugin/ae_processor.py.
#   Edit THIS file. Edits to the dummy change nothing at runtime. / breaks: wrong-file edits
# ============================================================================
"""CorridorKey After Effects / Premiere Processor.

WHAT IT DOES: Command-line bridge between the CEP panel (which spawns Python via
    child_process.execFileSync with a safe argv array) and the CorridorKey neural
    keying engine. Three subcommands:

    extract <source_video> <output_png> --frame N
        Pulls a single frame from a video using OpenCV. Replaces the old inline
        `python -c "import cv2; ..."` shell one-liner that the panel used to run
        (which had a shell-injection bug for filenames with quotes).

    single <input_png> <output_png> [--params PATH | --screen... --despill...]
        Keys a single frame. Settings can come from a JSON file (preferred) or argv.

    batch <source_video> <output_folder> [--params PATH | --start-frame ... ]
        Keys a range of frames from a video. Emits `PROGRESS n/m` lines on stdout
        every frame so the panel can draw a progress bar.

DEPENDS-ON: The CorridorKey engine resolved via corridorkey_path.txt next to this
    script, or CORRIDORKEY_ROOT env var, or fallback locations.
AFFECTS: Reads video / images from disk, writes PNGs to disk, writes log lines to
    %TEMP%/corridorkey.log.
"""
import sys
import os
import json
import argparse
import logging
import tempfile
import traceback
from pathlib import Path

# Force UTF-8 on stdout/stderr so the CorridorKey engine's Unicode log messages (→, μ, etc.)
# do not crash Python's default cp1252 StreamHandler on Windows. Must run before any other
# logger configures a handler pointed at the old binary streams.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── CorridorKey engine discovery ──────────────────────────────
# Same resolution order as the Resolve plugin and the Node.js panel.
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
        if path and path.exists():
            return path
    raise RuntimeError(
        "CorridorKey engine not found. Tried:\n  " +
        "\n  ".join(str(c) for c in candidates)
    )

CK_ROOT = find_corridorkey_root()
sys.path.insert(0, str(CK_ROOT))
sys.path.insert(0, str(CK_ROOT / "resolve_plugin" / "core"))

# ── Logging ───────────────────────────────────────────────────
LOG_PATH = Path(tempfile.gettempdir()) / "corridorkey.log"
logging.basicConfig(
    level=logging.INFO,
    format="[CK-AE %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("corridorkey")


# ── Settings (JSON file preferred, argv fallback) ─────────────
DEFAULT_SETTINGS = {
    "screenType": "green",
    "despill": 0.5,
    "refiner": 1.0,
    "despeckle": True,
    "despeckleSize": 400,
    "choke": 0,
}


def load_settings(params_path, args):
    """Load settings from --params JSON file if provided, otherwise from argv.

    JSON is the preferred path because the panel generates it with safe Node.js fs
    writes, so there is no shell escaping involved at any step.
    """
    settings = dict(DEFAULT_SETTINGS)
    if params_path:
        with open(params_path, "r", encoding="utf-8") as f:
            settings.update(json.load(f))
    # argv values override JSON only when explicitly passed
    if getattr(args, "screen", None):
        settings["screenType"] = args.screen
    if getattr(args, "despill", None) is not None:
        settings["despill"] = float(args.despill)
    if getattr(args, "despeckle", None) is not None:
        settings["despeckle"] = bool(int(args.despeckle))
    if getattr(args, "despeckle_size", None) is not None:
        settings["despeckleSize"] = int(args.despeckle_size)
    if getattr(args, "refiner", None) is not None:
        settings["refiner"] = float(args.refiner)
    # Normalize + clamp
    settings["screenType"] = "blue" if settings.get("screenType") == "blue" else "green"
    settings["despill"] = max(0.0, min(1.0, float(settings["despill"])))
    settings["refiner"] = max(0.0, min(1.0, float(settings["refiner"])))
    settings["despeckleSize"] = max(50, min(2000, int(settings["despeckleSize"])))
    settings["despeckle"] = bool(settings["despeckle"])
    settings["choke"] = max(0, min(20, int(settings.get("choke", 0))))
    return settings


# ── Chroma hint ───────────────────────────────────────────────
# HSV-based detection matches DaVinci's AlphaHintGenerator — catches dark/shadowed
# green areas that the old RGB ratio test missed (ProRes crash mats, crumpled screen edges).
def generate_chroma_hint(image, screen_type="green"):
    import numpy as np
    import cv2
    if image.dtype != np.uint8:
        img_uint8 = (np.clip(image, 0, 1) * 255).astype(np.uint8)
    else:
        img_uint8 = image
    # image is RGB — convert to BGR for HSV
    img_bgr = cv2.cvtColor(img_uint8, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    if screen_type == "green":
        lower = np.array([35, 50, 50])
        upper = np.array([85, 255, 255])
    else:
        lower = np.array([100, 50, 50])
        upper = np.array([130, 255, 255])
    screen_mask = cv2.inRange(hsv, lower, upper)
    subject_mask = cv2.bitwise_not(screen_mask)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_CLOSE, kernel)
    subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_OPEN, kernel)
    subject_mask = cv2.GaussianBlur(subject_mask, (5, 5), 0)
    return subject_mask.astype(np.float32) / 255.0


# ── Subcommand: extract ───────────────────────────────────────
def _extract_frame_pyav(src_path, time_sec):
    """PyAV decode path for HEVC/H.265. Reads source colorspace from codec_context
    and passes it explicitly to swscale reformat — avoids BT.601 default that causes
    wrong colors on BT.709 HEVC (PyAV Issue #873). Returns BGR uint8 ndarray or None."""
    try:
        import av
        import numpy as _np
        import cv2 as _cv2
    except ImportError as _ie:
        log.warning(f"PyAV not available, falling back to cv2: {_ie}")
        return None
    container = None
    try:
        container = av.open(str(src_path))
        vs = container.streams.video[0]
        vs.thread_type = "AUTO"
        try:
            _cs_int = int(vs.codec_context.colorspace or 0)
            _rng_int = int(vs.codec_context.color_range or 0)
        except Exception:
            _cs_int = 0
            _rng_int = 0
        # FFmpeg AVColorSpace: 5=BT.601-PAL, 6=BT.601-NTSC, 9=BT.2020, else 709
        if _cs_int in (5, 6):
            _src_cs = "itu601"
        elif _cs_int == 9:
            _src_cs = "itu2020_ncl"
        else:
            _src_cs = "itu709"
        _src_rng = "jpeg" if _rng_int == 2 else "mpeg"
        target_pts = int(time_sec / float(vs.time_base))
        try:
            container.seek(target_pts, any_frame=False, backward=True, stream=vs)
        except Exception:
            container.seek(max(0, target_pts), any_frame=False, backward=True)
        decoded = None
        for f in container.decode(vs):
            if f.pts is None:
                continue
            if f.pts < target_pts:
                continue
            decoded = f
            break
        if decoded is None:
            log.warning(f"PyAV: no frame at t={time_sec:.3f}s")
            return None
        try:
            log.info(f"PyAV reformat: src_cs={_src_cs} src_rng={_src_rng}")
            reformatted = decoded.reformat(
                format="rgb48le",
                src_colorspace=_src_cs,
                dst_colorspace="itu709",
                src_color_range=_src_rng,
                dst_color_range="jpeg",
            )
            rgb16 = reformatted.to_ndarray()
            rgb8 = (rgb16 >> 8).astype(_np.uint8)
        except Exception as _ref_e:
            log.warning(f"PyAV reformat fallback (rgb24): {_ref_e}")
            rgb8 = decoded.to_ndarray(format="rgb24")
        return _cv2.cvtColor(rgb8, _cv2.COLOR_RGB2BGR)
    except Exception as _pe:
        log.warning(f"PyAV extract failed: {_pe}")
        return None
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


_HEVC_EXTS = {".hevc", ".heic"}
_HEVC_CODECS = {"hevc", "h265", "x265"}


def cmd_extract(source_video, output_png, frame_idx=None, time_sec=None):
    """Pull one frame from a video. HEVC sources use PyAV with explicit BT.709 colorspace
    to avoid the BT.601 default that causes wrong colors on H.265 footage (PyAV Issue #873).
    All other codecs use cv2. Falls back to cv2 if PyAV is not installed."""
    import cv2
    src = Path(source_video)
    if not src.exists():
        log.error(f"Source video not found: {src}")
        return False

    # Resolve time_sec for both paths
    if time_sec is None:
        cap_probe = cv2.VideoCapture(str(src))
        source_fps = cap_probe.get(cv2.CAP_PROP_FPS) or 24.0
        cap_probe.release()
        time_sec_resolved = float(frame_idx) / source_fps
    else:
        time_sec_resolved = float(time_sec)

    # Detect HEVC by extension or codec name (mp4/mov containers can carry HEVC)
    _use_pyav = src.suffix.lower() in _HEVC_EXTS
    if not _use_pyav:
        try:
            cap_probe2 = cv2.VideoCapture(str(src))
            _fourcc_int = int(cap_probe2.get(cv2.CAP_PROP_FOURCC))
            cap_probe2.release()
            _fourcc_str = "".join(chr((_fourcc_int >> (8 * i)) & 0xFF) for i in range(4)).lower()
            _use_pyav = any(c in _fourcc_str for c in _HEVC_CODECS)
        except Exception:
            pass

    if _use_pyav:
        log.info(f"HEVC detected — using PyAV BT.709 decode path")
        frame = _extract_frame_pyav(src, time_sec_resolved)
        if frame is not None:
            out = Path(output_png)
            out.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(out), frame)
            log.info(f"Extracted (PyAV) -> {out}")
            return True
        log.warning("PyAV path failed, falling back to cv2")

    # cv2 path (all non-HEVC codecs, or PyAV fallback)
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        log.error(f"Could not open video: {src}")
        return False
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, time_sec_resolved * 1000.0)
        if time_sec is not None:
            log.info(f"Seek by time: {time_sec_resolved:.4f}s")
        else:
            log.info(f"Seek by frame {frame_idx} -> {time_sec_resolved*1000:.2f}ms")
        ok, frame = cap.read()
        if not ok or frame is None:
            log.error("Could not read frame at requested position")
            return False
        out = Path(output_png)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), frame)
        log.info(f"Extracted -> {out}")
        return True
    finally:
        cap.release()


# ── SAM2 output gate helper ──────────────────────────────────
# WHAT IT DOES: Loads sam2_mask.png written by preview_viewer_v2.py after the user
#   clicks "Apply Mask". Returns a float32 [0,1] array (1=foreground, 0=background)
#   ready to multiply with the neural keyer's output alpha as a garbage matte gate.
#   Same approach as Resolve's _load_sam2_output_gate — gate the OUTPUT, not the input.
# DEPENDS-ON: sam2_mask_path existing on disk (written by viewer).
# AFFECTS: called from cmd_single and cmd_batch; returns None if mask not available.
def load_sam2_gate(sam2_mask_path, target_h, target_w):
    import numpy as np
    import cv2
    if not sam2_mask_path or not Path(sam2_mask_path).exists():
        return None
    raw = cv2.imread(str(sam2_mask_path), cv2.IMREAD_GRAYSCALE)
    if raw is None:
        log.warning(f"SAM2 gate: could not read {sam2_mask_path}")
        return None
    _, raw = cv2.threshold(raw, 127, 255, cv2.THRESH_BINARY)
    if raw.shape != (target_h, target_w):
        raw = cv2.resize(raw, (target_w, target_h), interpolation=cv2.INTER_NEAREST)
    gate = raw.astype(np.float32) / 255.0
    log.info(f"SAM2 gate loaded — coverage {gate.mean():.3f}")
    return gate


# ── Subcommand: single ────────────────────────────────────────
# WHAT IT DOES: Keys a single PNG. Accepts an optional --sam2-mask path pointing to
#   sam2_mask.png written by the viewer after Apply Mask is clicked. When present,
#   the SAM2 binary mask is applied to the OUTPUT alpha as a garbage matte gate
#   (same technique as traditional garbage mattes — gate the output, not the input).
# DEPENDS-ON: corridorkey_processor, load_sam2_gate.
# AFFECTS: writes output BGRA PNG + _matte.png.
# ── Shared matte post-processing — ONE source of truth ────────────────────────
# cmd_single (KEY CURRENT FRAME), cmd_batch (PROCESS WORK AREA) and cmd_postproc
# (inline PREVIEW) all run the SAME post-proc through these helpers, so
# PREVIEW == RENDER == KEY FRAME can never drift again. Canonical order matches the
# DaVinci live viewer (the artist-facing source of truth):
#     SAM garbage-matte merge  ->  choke  ->  despeckle  ->  despill
# despill only touches the foreground, and the SAM merge only reads the source plate
# (never the despilled fg), so despill being last is order-independent of the matte.
def sam_garbage_merge(alpha, sam_soft, source_rgb, settings, screen_type="green",
                      return_garbage=False):
    """CK x SAM garbage-matte merge via the shared engine (DaVinci-identical).
    sam_soft: soft 0..1 SAM silhouette, or None. Returns the merged 2D alpha.
    return_garbage (Berto 2026-06-14): when True, returns (alpha, garbage_matte_or_None)
    so cmd_batch can write the green-aware keep-gate as a stable garbage-matte sidecar.
    Default False keeps the single-array return — the other caller (cmd_single) is safe."""
    import numpy as np, cv2
    def _ret(a, g=None):
        return (a, g) if return_garbage else a
    if sam_soft is None or bool(settings.get("sam2_bypass", False)):
        return _ret(alpha)
    try:
        from corridorkey_sam_merge import binarize_sam_silhouette, merge_ck_with_sam_active
    except Exception as e:
        log.warning(f"SAM merge unavailable, using CK alpha: {e}")
        return _ret(alpha)
    sg = sam_soft
    if sg.ndim == 3:
        sg = sg[:, :, 0]
    if sg.shape != alpha.shape:
        sg = cv2.resize(sg, (alpha.shape[1], alpha.shape[0]), interpolation=cv2.INTER_LINEAR)
    try:
        # GARBAGE MASK buffer: the panel's samMargin fader drives the merge's actual
        # kill-zone (generous dilation + chroma-escape radius). Before 2026-06-06 the
        # fader only inflated the exported SAM matte layer — the merge silently ran on
        # edge_guard_px (default 7) and the operator's buffer setting did nothing here.
        _buffer_px = settings.get("sam2_margin", settings.get("edge_guard_px", 0))
        # merge_ck_with_sam_active returns (alpha, garbage) when return_garbage=True,
        # else just alpha — so the return is already the right shape for both modes.
        return merge_ck_with_sam_active(
            alpha, binarize_sam_silhouette(sg), source_rgb=source_rgb,
            screen_type=screen_type, proximity_px=int(_buffer_px),
            carve_points=settings.get("sam_negative") or None,
            return_garbage=return_garbage)
    except Exception as e:
        log.warning(f"SAM merge failed, using CK alpha: {e}")
        return _ret(alpha)


def apply_choke(alpha, settings):
    """Shrink the matte edge inward by N px (cv2.erode, ellipse kernel). 0 = off."""
    import numpy as np, cv2
    choke_px = int(settings.get("choke", 0))
    if choke_px <= 0:
        return alpha
    k = choke_px * 2 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    a8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
    return cv2.erode(a8, kernel).astype(np.float32) / 255.0


def apply_despeckle(alpha, settings):
    """Remove small disconnected alpha islands. Default OFF — matches DaVinci, where
    despeckle was disabled because it ate flyaway hair. Opt-in via the panel checkbox."""
    if not settings.get("despeckle", False):
        return alpha
    from CorridorKeyModule.core import color_utils as cu
    return cu.clean_matte_opencv(
        alpha, area_threshold=int(settings.get("despeckleSize", 400)), dilation=25, blur_size=5)


def apply_despill(fg_rgb, settings):
    """Green-spill removal on the foreground (average mode). 0 = off (warm wardrobe)."""
    despill_strength = float(settings.get("despill", 0.0))
    if despill_strength <= 0.0:
        return fg_rgb
    from CorridorKeyModule.core import color_utils as cu
    return cu.despill_opencv(fg_rgb, green_limit_mode="average", strength=despill_strength)


def apply_shirt_rescue(alpha, sam_soft, src_rgb, settings):
    """PORTED 2026-06-06 from CorridorKey_Pro.py:_apply_shirt_rescue (Resolve, live since
    2026-05-23). Thin/bright fabric over green keys semi-transparent (green bounces
    through the weave). Where a pixel is NOT green (chroma < 0.15) AND deep inside the
    SAM body (>0.85, eroded 5px), force alpha to max(alpha, SAM). Genuine green pixels
    are untouched — fence holes / real see-through showing greenscreen stay transparent
    by construction (Berto's fence rule, 2026-06-06)."""
    if not settings.get("shirtRescue", True):
        return alpha
    if sam_soft is None or src_rgb is None:
        return alpha
    try:
        import numpy as np, cv2
        rgb = np.asarray(src_rgb).astype(np.float32)
        if rgb.max() > 1.5:
            rgb = rgb / 255.0
        green = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])
        not_green = green < 0.15
        sam_arr = np.asarray(sam_soft).astype(np.float32)
        if sam_arr.ndim == 3:
            sam_arr = sam_arr[..., 0]
        if sam_arr.max() > 1.5:
            sam_arr = sam_arr / 255.0
        sam_bin = (sam_arr > 0.85).astype(np.uint8)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))   # erode 5px
        sam_bin = cv2.erode(sam_bin, k)
        if sam_bin.shape != alpha.shape or not_green.shape != alpha.shape:
            return alpha
        rescue = not_green & (sam_bin > 0)
        out = alpha.copy()
        out[rescue] = np.maximum(alpha[rescue], sam_bin[rescue].astype(np.float32))
        return np.clip(out, 0.0, 1.0)
    except Exception as e:
        log.warning(f"shirt rescue skipped: {e}")
        return alpha


def apply_matte_postproc(fg_rgb, alpha_raw, settings, sam_soft=None, source_rgb=None,
                         screen_type="green"):
    """Canonical CK post-proc shared by single / batch / postproc. Returns (fg_rgb, alpha).
    Order: SAM garbage merge -> choke -> despeckle -> despill."""
    alpha = alpha_raw.copy()
    if alpha.ndim == 3:
        alpha = alpha[:, :, 0]
    src = source_rgb if source_rgb is not None else fg_rgb
    alpha = sam_garbage_merge(alpha, sam_soft, src, settings, screen_type)
    _sam_r = sam_soft
    if _sam_r is not None:
        import cv2 as _cv2_r
        import numpy as _np_r
        _sam_r = _np_r.asarray(_sam_r)
        if _sam_r.ndim == 3:
            _sam_r = _sam_r[..., 0]
        if _sam_r.shape[:2] != alpha.shape[:2]:
            _sam_r = _cv2_r.resize(_sam_r, (alpha.shape[1], alpha.shape[0]),
                                   interpolation=_cv2_r.INTER_LINEAR)
    alpha = apply_shirt_rescue(alpha, _sam_r, src, settings)
    alpha = apply_choke(alpha, settings)
    alpha = apply_despeckle(alpha, settings)
    fg_rgb = apply_despill(fg_rgb, settings)
    return fg_rgb, alpha


# ── Recipe composite helpers ───────────────────────────────────
# ONE shared function used by cmd_batch (render) and cmd_postproc (preview) so
# the two paths are mathematically identical: preview == render by construction.

_auto_zone_logged = False  # log throttle: one line per process lifetime


def _measure_sam_offset(ck_alpha, sam_binary, zone_mask_or_bbox, scale):
    """Measure SAM boundary's median distance to nearest CK alpha edge.

    Subsamples every 4th SAM boundary pixel; keeps only samples within 15px
    of a CK edge crossing (ck_alpha crosses 0.5); returns median clamped to
    [0, 15] px at current resolution.  Returns None if < 30 valid samples.
    zone_mask_or_bbox: float32 H×W mask (>0.5 = inside zone) to exclude, or
                       4-tuple (x, y, w, h) bbox, or None (measure everywhere).
    """
    import numpy as np, cv2
    k1 = np.ones((3, 3), np.uint8)
    # SAM boundary ring: binary XOR 1px-erosion
    sam_boundary = sam_binary ^ cv2.erode(sam_binary, k1)
    # CK edge: pixels where ck_alpha crosses 0.5, XOR with 1px-erosion
    ck_thresh = (ck_alpha > 0.5).astype(np.uint8)
    ck_edge = ck_thresh ^ cv2.erode(ck_thresh, k1)
    # Distance transform: 0 at CK edge pixels, grows outward
    dist_from_ck = cv2.distanceTransform((ck_edge == 0).astype(np.uint8),
                                         cv2.DIST_L2, 3)
    # Collect boundary pixel coords, subsample every 4th
    by, bx = np.where(sam_boundary > 0)
    if len(by) == 0:
        return None
    idx = np.arange(0, len(by), 4)
    sy, sx = by[idx], bx[idx]
    # Exclude zone interior
    if zone_mask_or_bbox is not None:
        if isinstance(zone_mask_or_bbox, np.ndarray):
            inside = zone_mask_or_bbox[sy, sx] > 0.5
        else:
            zx, zy, zw, zh = zone_mask_or_bbox
            inside = (sx >= zx) & (sx < zx + zw) & (sy >= zy) & (sy < zy + zh)
        sy, sx = sy[~inside], sx[~inside]
    if len(sy) == 0:
        return None
    dists = dist_from_ck[sy, sx]
    valid = dists[dists <= 15.0]
    if len(valid) < 30:
        return None
    return float(np.clip(np.median(valid), 0.0, 15.0))


def _zone_cut_from_sam(sam_bin, settings, w, h, scale, auto_offset=None):
    """zone_cut mask: 1 everywhere except inside user zone where = tight SAM.

    sam_bin : uint8 binary SAM already thresholded at 0.5.
    scale   : frame_w / 1920 (used for tight SAM feather sigma).
    Returns float32 H×W mask.
    """
    import numpy as np, cv2
    zone = settings.get('zone')
    if zone is None:
        return np.ones((h, w), dtype=np.float32)
    zone_anchor_bbox = settings.get('zone_anchor_bbox')
    # Compute current frame SAM bbox for zone transform
    cols = np.any(sam_bin, axis=0)
    rows = np.any(sam_bin, axis=1)
    if not cols.any() or not rows.any():
        return np.ones((h, w), dtype=np.float32)
    x_min = int(np.where(cols)[0][0]); x_max = int(np.where(cols)[0][-1])
    y_min = int(np.where(rows)[0][0]); y_max = int(np.where(rows)[0][-1])
    cur_w = max(1, x_max - x_min); cur_h = max(1, y_max - y_min)
    cur_cx = x_min + cur_w / 2.0;   cur_cy = y_min + cur_h / 2.0
    # Transform zone by bbox center-delta + scale
    zx = float(zone['x']); zy = float(zone['y'])
    zw = float(zone['w']); zh = float(zone['h'])
    zone_cx = zx + zw / 2.0; zone_cy = zy + zh / 2.0
    if (zone_anchor_bbox is not None
            and float(zone_anchor_bbox[2]) > 0 and float(zone_anchor_bbox[3]) > 0):
        anc_cx = float(zone_anchor_bbox[0]) + float(zone_anchor_bbox[2]) / 2.0
        anc_cy = float(zone_anchor_bbox[1]) + float(zone_anchor_bbox[3]) / 2.0
        sx = cur_w / float(zone_anchor_bbox[2])
        sy = cur_h / float(zone_anchor_bbox[3])
        t_cx = cur_cx + (zone_cx - anc_cx) * sx
        t_cy = cur_cy + (zone_cy - anc_cy) * sy
        t_w = zw * sx; t_h = zh * sy
    else:
        t_cx = zone_cx; t_cy = zone_cy; t_w = zw; t_h = zh
    # Build zone mask
    zone_mask = np.zeros((h, w), dtype=np.uint8)
    feather_px = float(zone.get('feather_px', 10))
    shape = zone.get('shape', 'ellipse')
    if shape == 'ellipse':
        cv2.ellipse(zone_mask,
                    (int(round(t_cx)), int(round(t_cy))),
                    (max(1, int(round(t_w / 2.0))), max(1, int(round(t_h / 2.0)))),
                    0, 0, 360, 255, -1)
    else:
        cv2.rectangle(zone_mask,
                      (int(round(t_cx - t_w / 2.0)), int(round(t_cy - t_h / 2.0))),
                      (int(round(t_cx + t_w / 2.0)), int(round(t_cy + t_h / 2.0))),
                      255, -1)
    if feather_px > 0:
        zone_f = cv2.GaussianBlur(zone_mask.astype(np.float32), (0, 0),
                                  max(0.5, feather_px / 3.0)) / 255.0
    else:
        zone_f = zone_mask.astype(np.float32) / 255.0
    # Tight SAM inside zone: erode (effective_erode>0) or dilate (=0) binary SAM, then feather.
    # zone_erode_px slider default 4 = neutral trim (0 adjustment vs auto measurement).
    zone_erode_px = int(settings.get('zone_erode_px', 4))
    if auto_offset is not None:
        effective_erode = int(np.clip(auto_offset + (zone_erode_px - 4), 0, 20))
    else:
        effective_erode = zone_erode_px
    if effective_erode > 0:
        erode_r = max(1, int(round(effective_erode * w / 1920)))
        k_e = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * erode_r + 1, 2 * erode_r + 1))
        tight = cv2.erode(sam_bin, k_e)
    else:
        k_tight = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        tight = cv2.dilate(sam_bin, k_tight)
    tight_f = cv2.GaussianBlur(tight.astype(np.float32), (0, 0), max(0.5, 1.5 * scale))
    # Blend: inside zone -> tight_sam, outside -> 1
    return np.clip(1.0 - zone_f + zone_f * tight_f, 0.0, 1.0)


def apply_recipe_composite(alpha_raw, sam_soft, frame_w, settings):
    """Deterministic recipe: alpha_final = ck_alpha * keep_mask * zone_cut.

    Unified keep rule (two-stage filter):
      1. Fat kill: morphological opening on (ck_solid AND NOT protected) kills
         structures fatter than thick_px — slabs, cables, fused floors.
      2. Connectivity filter on remainder (body + surviving thin junk):
         keep only connected components that OVERLAP the SAM protected zone.
         Thin straps/wires attached to body survive; disconnected thin outlines
         (junk rim spiderwebs) die even though their width survived the opening.
      3. Feather keep mask with sigma=2*scale.

    Pure-green shots: outside ≈ empty → no-op.

    strap_bridge_px slider drives thick_px as before:
        thick_px = settings.get('strap_bridge_px', 8) * 1.875 * scale
        (default 8 → 15 px at 1920 width)

    keep_mask : float 0..1 (1 = keep). Returned so the caller can invert it
                for the garbage sidecar.
    zone_cut  : 1 everywhere; inside user zone = tight SAM clip (unchanged).

    Returns (alpha_final float32, keep_mask float32|None).
    keep_mask is None when sam_soft is None (no SAM active for this frame).
    """
    import numpy as np, cv2
    if sam_soft is None:
        return alpha_raw, None
    h, w = alpha_raw.shape[:2]
    scale = frame_w / 1920.0
    sam = np.asarray(sam_soft, dtype=np.float32)
    if sam.ndim == 3:
        sam = sam[:, :, 0]
    if sam.shape[:2] != (h, w):
        sam = cv2.resize(sam, (w, h), interpolation=cv2.INTER_LINEAR)
    sam_solid = (sam > 0.5).astype(np.uint8)
    ck_solid  = (alpha_raw > 0.5).astype(np.uint8)

    # Protected zone: SAM body expanded by 3*scale px
    body_buffer_px = max(1, int(round(3 * scale)))
    k_buf = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                      (body_buffer_px * 2 + 1, body_buffer_px * 2 + 1))
    protected = cv2.dilate(sam_solid, k_buf)                    # uint8 0/1

    # Fat kill: opening on (ck_solid AND NOT protected)
    _sbpx = float(settings.get('strap_bridge_px', 8))
    thick_px = max(1, int(round(_sbpx * 1.875 * scale)))       # 8 default → 15 px at 1920
    k_thick = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                        (thick_px * 2 + 1, thick_px * 2 + 1))
    outside  = (ck_solid & (protected == 0)).astype(np.uint8)
    fat_kill = cv2.morphologyEx(outside, cv2.MORPH_OPEN, k_thick)  # uint8 0/1

    # Connectivity filter: thin junk rims die if disconnected from SAM body
    remainder = np.clip(ck_solid.astype(np.int32) - fat_kill.astype(np.int32),
                        0, 1).astype(np.uint8)
    _, labels = cv2.connectedComponents(remainder, connectivity=8)
    overlap_labels = np.unique(labels[protected > 0])
    overlap_labels = overlap_labels[overlap_labels > 0]
    if len(overlap_labels) > 0:
        keep_bin = np.isin(labels, overlap_labels).astype(np.uint8)
    else:
        keep_bin = np.zeros_like(remainder)

    # Feather keep mask
    keep_f = cv2.GaussianBlur(keep_bin.astype(np.float32), (0, 0), max(0.5, 2.0 * scale))
    keep_mask = np.clip(keep_f, 0.0, 1.0)
    # auto zone tightness: measure SAM looseness vs CK edge, drive zone erosion
    global _auto_zone_logged
    auto_offset = _measure_sam_offset(alpha_raw, sam_solid, None, scale)
    if auto_offset is not None and not _auto_zone_logged:
        log.info(f'auto zone tighten: measured {auto_offset:.1f}px')
        _auto_zone_logged = True
    zone_cut = _zone_cut_from_sam(sam_solid, settings, w, h, scale, auto_offset=auto_offset)
    # base = CK alpha only; SAM must never add alpha CK did not produce
    alpha_final = np.clip(alpha_raw * keep_mask * zone_cut, 0.0, 1.0)
    return alpha_final, keep_mask


def cmd_single(input_path, output_path, settings, sam2_mask_path=None):
    import numpy as np
    import cv2
    log.info(f"Keying: {input_path}")
    img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        log.error(f"Cannot read: {input_path}")
        return False

    has_alpha = len(img.shape) == 3 and img.shape[2] == 4
    if has_alpha:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    else:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    if img_rgb.dtype == np.uint8:
        img_rgb = img_rgb.astype(np.float32) / 255.0
    elif img_rgb.dtype == np.uint16:
        img_rgb = img_rgb.astype(np.float32) / 65535.0

    alpha_hint = generate_chroma_hint(img_rgb, settings["screenType"])

    from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    from CorridorKeyModule.core import color_utils as _cu
    processor = CorridorKeyProcessor(device="cuda")
    try:
        # Run NN with despill + despeckle DISABLED. ALL post-proc now runs through the
        # shared apply_matte_postproc() below, so KEY CURRENT FRAME == PROCESS WORK AREA
        # == inline PREVIEW. despeckle is a post-NN step now (default OFF, like DaVinci).
        ps = ProcessingSettings(
            screen_type=settings["screenType"],
            despill_strength=0.0,
            despeckle_enabled=False,
            despeckle_size=settings["despeckleSize"],
            refiner_strength=settings["refiner"],
        )
        result = processor.process_frame(img_rgb, alpha_hint, ps)
        fg = result.get("fg")
        alpha = result.get("alpha")
        if fg is None or alpha is None:
            log.error("Keyer returned no output")
            return False
        if len(alpha.shape) == 3:
            alpha = alpha[:, :, 0]
        # Optional SAM gate file -> soft silhouette for the shared garbage-matte merge.
        sam_soft = None
        if sam2_mask_path:
            _g = cv2.imread(str(sam2_mask_path), cv2.IMREAD_UNCHANGED)
            if _g is not None:
                sam_soft = _g.astype(np.float32) / (65535.0 if _g.dtype == np.uint16 else 255.0)
        # Shared post-proc: SAM merge -> choke -> despeckle -> despill. source_rgb is the
        # REAL frame so the merge's hair chroma-escape sees true green (matches batch).
        fg, alpha = apply_matte_postproc(
            fg, alpha, settings, sam_soft=sam_soft, source_rgb=img_rgb,
            screen_type=settings["screenType"])
        fg_uint16 = (np.clip(fg, 0, 1) * 65535).astype(np.uint16)
        alpha_uint16 = (np.clip(alpha, 0, 1) * 65535).astype(np.uint16)
        fg_bgr = cv2.cvtColor(fg_uint16, cv2.COLOR_RGB2BGR)
        out_bgra = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint16])
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), out_bgra)
        matte_path = out_path.with_name(out_path.stem + "_matte.png")
        cv2.imwrite(str(matte_path), alpha_uint16)
        log.info(f"Saved: {out_path}")
        return True
    finally:
        processor.cleanup()


def _atomic_imwrite(path, img, params=None):
    """imwrite via temp-name + os.replace so importers / live-preview copies never
    see a half-written frame, and a False return (disk full, bad path) raises
    instead of silently dropping the frame. cv2 infers format from extension, so
    the temp name keeps .png (``x.png`` -> ``x.tmp.png`` -> rename to ``x.png``)."""
    import cv2
    path = str(path)
    tmp = path[:-4] + ".tmp.png" if path.lower().endswith(".png") else path + ".tmp"
    ok = cv2.imwrite(tmp, img) if params is None else cv2.imwrite(tmp, img, params)
    if not ok:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise IOError(f"imwrite failed (disk full or bad path): {path}")
    os.replace(tmp, path)


# ── Named sidecar passes (Editable Layers / Fusion-comp parity) ───
# WHAT IT DOES: writes the named CK sidecar PNGs for one frame, each into its OWN
#   clean subfolder so the host imports each as an isolated numbered-stills sequence.
#   THIS function writes: CK_ALPHA/ (raw CK alpha) + SAM_JUNK/ (inverted SAM, white=junk).
#   The cmd_batch loop also writes CK_ONLY/ (full-hair CK clip) and, on the clean
#   engine, GARBAGE_MATTE/ (green-aware garbage matte). It does NOT write CK_RGB/
#   CK_COMBINED/SAM_ALPHA (that was the old Resolve naming — corrected 2026-06-14).
# PORTED FROM: CorridorKey_Pro.py:3156-3231 `_write_fusion_sidecars`. The Resolve
#   version writes per-clip-named files into ONE dir (Fusion Loader contract). AE's
#   host (Premiere/AE numbered-stills importer) wants one PNG pattern per folder
#   plus a dummy 00000 frame (the importer silently drops the first frame), so the
#   folder layout follows the SAME convention cmd_batch already uses for
#   output_/mattes/sam_mattes. Folder NAMES, 16-bit depth, the 3-channel-for-matte
#   write path, and the despill-on-FG step all match the Resolve source exactly.
# AFFECTS: writes <out_dir>/{CK_RGB,CK_COMBINED,CK_ALPHA,SAM_ALPHA}/<name>_NNNNN.png
#   (16-bit) plus a per-folder <name>_00000.png dummy on the first written frame.
# GUARDED: any failure is logged as CK_WARN: and swallowed — a sidecar fault must
#   never crash the main key (matches the loud-fallback pattern in cmd_batch).
def _write_fusion_sidecars(ck_alpha, sam_union,
                           settings, out_dir, seq_num, is_first):
    """Write CK_ALPHA + SAM_JUNK sidecars for one frame (the two matte deliverables).

    ck_alpha  : float32 mono 0..1 (RAW CK neural-net alpha)
    sam_union : float32 mono 0..1 (SAM2 soft silhouette) or None
    CK_ALPHA is written 16-bit 3-channel. SAM_JUNK is uint8 (white=junk, black=body).
    """
    import cv2, numpy as np

    def _write(folder_name, file_stem, img):
        sub = out_dir / folder_name
        sub.mkdir(parents=True, exist_ok=True)
        _atomic_imwrite(sub / f"{file_stem}_{seq_num:05d}.png", img)
        if is_first:
            _atomic_imwrite(sub / f"{file_stem}_00000.png", img)

    try:
        # 1. CK_ALPHA — RAW CK neural-net alpha (un-merged, un-choked). 16-bit 3-channel.
        if ck_alpha is not None:
            m = ck_alpha[:, :, 0] if ck_alpha.ndim == 3 else ck_alpha
            m16 = (np.clip(m, 0.0, 1.0) * 65535.0).astype(np.uint16)
            _write("CK_ALPHA", "CK_ALPHA", cv2.merge([m16, m16, m16]))

        # 2. SAM_JUNK — inverted SAM mask (uint8 0/255, white=junk to discard, black=body).
        #    AE layer named 'SAM JUNK MASK' with Simple Choker buffer — see host.jsx.
        if sam_union is not None:
            s_2d = sam_union[:, :, 0] if sam_union.ndim == 3 else sam_union
            junk_u8 = ((1.0 - np.clip(s_2d, 0.0, 1.0)) * 255.0).astype(np.uint8)
            _write("SAM_JUNK", "SAM_JUNK", cv2.merge([junk_u8, junk_u8, junk_u8]))
    except Exception as _sc_err:
        print(f"CK_WARN: named sidecar pass failed on frame {seq_num} "
              f"(main key unaffected): {_sc_err}", flush=True)
        log.warning(f"Sidecar write failed frame {seq_num}: {_sc_err}")


# ── Subcommand: batch ─────────────────────────────────────────
def cmd_batch(source_video, output_folder, settings,
              start_frame=None, end_frame=None, fps=None,
              start_seconds=None, end_seconds=None):
    """Batch-key a range. Accepts EITHER a frame range (AE) OR a time-in-seconds range
    (Premiere). Time range wins if both are given. Time mode reads the source's native
    fps via CAP_PROP_FPS and converts — this is the fix for frame drift when sequence
    fps != source fps."""
    import numpy as np
    import cv2
    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the shared merge module's silent-fallback crash files so we can detect
    # (and loudly surface) a CK_COMBINED merge that faulted and fell back to plain CK
    # during THIS run. The dispatcher swallows its own errors, so the crash file is the
    # only external signal. NOTE: those paths are hardcoded to the user home in
    # corridorkey_sam_merge.py today — a follow-up moves both to %TEMP% in that module.
    _merge_crash_mtimes = {}
    for _cf_name in ("ck_garbage_merge_exception.txt", "ck_chroma_merge_exception.txt"):
        _cf = Path.home() / _cf_name
        _merge_crash_mtimes[_cf] = _cf.stat().st_mtime if _cf.exists() else None

    # Resolve time range to frame indices using SOURCE's native fps. This eliminates
    # the drift that happens when JSX converts seconds->frames using the sequence fps
    # rather than the clip's actual fps.
    cap_probe = cv2.VideoCapture(str(source_video))
    if not cap_probe.isOpened():
        log.error(f"Cannot open: {source_video}")
        return 0
    source_fps = cap_probe.get(cv2.CAP_PROP_FPS)
    cap_probe.release()
    if not source_fps or source_fps <= 0:
        log.warning(f"Source fps unknown, defaulting to {fps or 24}")
        source_fps = float(fps or 24)

    if start_seconds is not None and end_seconds is not None:
        start_frame = int(round(float(start_seconds) * source_fps))
        end_frame   = int(round(float(end_seconds)   * source_fps))
        log.info(f"Time range {start_seconds:.4f}..{end_seconds:.4f}s @ source fps {source_fps:.3f} -> frames {start_frame}..{end_frame}")
    else:
        start_frame = int(start_frame); end_frame = int(end_frame)
        log.info(f"Frame range {start_frame}..{end_frame} (source fps {source_fps:.3f})")

    log.info(f"Batch: {source_video}  frames {start_frame}..{end_frame}")
    cap = cv2.VideoCapture(str(source_video), cv2.CAP_FFMPEG)   # FIX C: FFMPEG backend -> POS_FRAMES reliable
    if not cap.isOpened():
        log.error(f"Cannot open: {source_video}")
        return 0

    from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    from CorridorKeyModule.core import color_utils as _cu
    processor = CorridorKeyProcessor(device="cuda")
    # Run NN with despill + despeckle DISABLED. All post-proc runs through the shared
    # apply_* helpers below (same as KEY FRAME + inline PREVIEW) so render == preview.
    ps = ProcessingSettings(
        screen_type=settings["screenType"],
        despill_strength=0.0,
        despeckle_enabled=False,
        despeckle_size=settings["despeckleSize"],
        refiner_strength=settings["refiner"],
    )

    # v1.0 SAM 2 batch propagation. If the user dropped SAM 2 click points in the
    # viewer, run SAM 2's VIDEO predictor over the same frame range and write a
    # per-frame SAM matte sidecar to <out_dir>/sam_mattes/. Mirrors cmd_batch_scrub
    # so render matches the side-by-side preview the user just signed off on.
    sam_pos = settings.get("sam_positive", []) or []
    sam_neg = settings.get("sam_negative", []) or []
    sam_anchor_abs = settings.get("sam_anchor_frame")
    sam_margin   = float(settings.get("sam_sidecar_margin", settings.get("sam2_margin", 0)))
    sam_soften   = float(settings.get("sam2_soften", 0))
    sam_fill     = int(settings.get("fill_holes", 0))

    sam_active = (len(sam_pos) + len(sam_neg)) > 0
    sam_video_masks = {}  # {seq_num: float32 mask 0..1}

    # SAM temp-frame resolution cap — mirrors cmd_batch_scrub so preview and render
    # produce identical SAM masks. CK neural-net keying and all outputs stay full-res;
    # only the frames written to SAM's temp dir are downscaled.
    _src_w_sam = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    _src_h_sam = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    if max(_src_w_sam, _src_h_sam) > 1920:
        _sam_scale = 1920.0 / max(_src_w_sam, _src_h_sam)
        _sam_w = int(round(_src_w_sam * _sam_scale))
        _sam_h = int(round(_src_h_sam * _sam_scale))
        log.info(f"SAM2 batch: downscale {_src_w_sam}x{_src_h_sam} -> {_sam_w}x{_sam_h} (scale={_sam_scale:.4f})")
    else:
        _sam_scale = 1.0
        _sam_w = _src_w_sam
        _sam_h = _src_h_sam

    sam_torch = None
    try:
        _manifest = {
            "source_video": str(Path(source_video).resolve()),
            "start_frame": int(start_frame),
            "end_frame": int(end_frame),
            "screen_type": settings.get("screenType", ""),
            "sam_active": sam_active,
        }
        with open(out_dir / "render_manifest.json", "w") as _mf:
            json.dump(_manifest, _mf, indent=2)
    except Exception as _mf_err:
        log.warning(f"render_manifest.json write failed: {_mf_err}")
    if sam_active:
        try:
            import torch as sam_torch
            import tempfile as _sam_tmp
            import shutil as _sam_shutil
            from sam2.build_sam import build_sam2_video_predictor
            # Use the module-level CK_ROOT (find_corridorkey_root) — the old inline
            # `Path(__file__).parent.parent` fallback resolved to the CEP extensions
            # folder, so SAM2 weights load silently failed. CK_ROOT honours the same
            # env / corridorkey_path.txt / fallback chain the Resolve plugin uses.
            ckpt = str(CK_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
            cfg = "configs/sam2.1/sam2.1_hiera_s.yaml"
            device = "cuda" if sam_torch.cuda.is_available() else "cpu"

            # Map absolute click frame → range-relative anchor. If the click was
            # outside the batch range (or never recorded), anchor at frame 0 and
            # forward-propagate only — same fallback cmd_batch_scrub uses.
            if sam_anchor_abs is not None and start_frame <= int(sam_anchor_abs) < end_frame:
                sam_anchor_rel = int(sam_anchor_abs) - int(start_frame)
            else:
                sam_anchor_rel = 0

            # Export the N range frames to a temp dir — SAM2 video predictor
            # reads frames from disk via init_state(video_path=...). Phase 0
            # 2026-05-09: lossless PNG (was JPEG q=95) and letterbox-padded
            # to a square BEFORE SAM sees them, so the encoder downsample is
            # uniform.
            _count = int(end_frame - start_frame)
            sam_tmp_dir = Path(_sam_tmp.mkdtemp(prefix="ck_sam2_batch_"))
            log.info(f"SAM2 video: exporting {_count} frames to {sam_tmp_dir}")
            from corridorkey_sam_merge import (
                pad_to_square as _pad_to_square,
                unpad_from_square as _unpad_from_square,
                shift_points_for_padding as _shift_pts,
                patch_sam2_loader_for_png as _patch_sam2_png,
                logits_to_soft_mask as _ramp,
            )
            _patch_sam2_png()
            _src_h = _src_w = _pad_box = None
            try:
                _exp_cap = cv2.VideoCapture(str(source_video), cv2.CAP_FFMPEG)
                # FIX C: POS_FRAMES + throwaway prime read (MSEC returns a dirty first read on long-GOP).
                _sf = int(start_frame)
                if _sf > 0:
                    _exp_cap.set(cv2.CAP_PROP_POS_FRAMES, _sf - 1)
                    _exp_cap.read()        # throwaway: warms decoder -> next read = start_frame
                # start_frame == 0: never seek — _exp_cap was JUST opened, so it already
                # sits at frame 0 and reads it clean sequentially. Seeking (even to 0)
                # flushes the decoder and dirties the next read, which fed SAM2 a junk
                # anchor frame (junk-first-frame bug, Berto 2026-06-04).
                _exported = 0
                for _i in range(_count):
                    _ok, _fr = _exp_cap.read()
                    if not _ok or _fr is None:
                        log.warning(f"SAM2 video: skipped unreadable frame {start_frame + _i}")
                        continue
                    _fr_scaled = cv2.resize(_fr, (_sam_w, _sam_h), interpolation=cv2.INTER_AREA) if _sam_scale != 1.0 else _fr
                    if _src_h is None:
                        _src_h, _src_w = _fr.shape[:2]
                        _, _pad_box = _pad_to_square(_fr_scaled)
                        log.info(f"SAM2 video: source {_src_w}x{_src_h} -> "
                                 f"SAM {_sam_w}x{_sam_h} -> "
                                 f"padded square {max(_sam_h, _sam_w)} "
                                 f"(scale={_sam_scale:.4f}, pad_box={_pad_box})")
                    _padded, _ = _pad_to_square(_fr_scaled)
                    cv2.imwrite(str(sam_tmp_dir / f"{_i:06d}.png"), _padded,
                                [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    _exported += 1
                _exp_cap.release()
                log.info(f"SAM2 video: {_exported} frames exported")

                _video_predictor = build_sam2_video_predictor(cfg, ckpt, device=device)
                _all_pts = list(sam_pos) + list(sam_neg)
                _labels  = [1] * len(sam_pos) + [0] * len(sam_neg)
                if _sam_scale != 1.0:
                    _all_pts = [[p[0] * _sam_scale, p[1] * _sam_scale] for p in _all_pts]
                _all_pts_padded = _shift_pts(_all_pts, _pad_box) if _pad_box is not None else _all_pts
                log.info(f"SAM2 video: anchor at range frame {sam_anchor_rel} (absolute {sam_anchor_abs})")
                with sam_torch.inference_mode():
                    _state = _video_predictor.init_state(
                        video_path=str(sam_tmp_dir),
                        offload_video_to_cpu=True,
                        async_loading_frames=False,
                    )
                    _video_predictor.add_new_points_or_box(
                        inference_state=_state,
                        frame_idx=sam_anchor_rel,
                        obj_id=1,
                        points=np.array(_all_pts_padded, dtype=np.float32),
                        labels=np.array(_labels, dtype=np.int32),
                        clear_old_points=True,
                    )
                    # CRISP-SNAP (Berto 2026-06-13 "46px even blur, not motion blur"):
                    # SAM runs downscaled (1920) then upscales 2x to 4K with INTER_LINEAR,
                    # smearing every edge into a wide ramp. The TRUE body boundary is the
                    # 0.5 crossing of the ramp. Snap there + a 0.8px anti-alias so the
                    # later upscale yields a thin ~2px edge instead of a 46px smear. NOT a
                    # sharpen filter — geometric reconstruction, adds no noise. SAM is a
                    # hard silhouette by design; softness only ever existed to dodge jaggies,
                    # which the tiny anti-alias still covers. (Hair stays CK's job, untouched.)
                    def _snap_sam_edge(_m):
                        _b = (_m >= 0.5).astype(np.float32)
                        return cv2.GaussianBlur(_b, (3, 3), 0.8)
                    # Forward: anchor → last frame. Logits are at padded square
                    # shape — apply ramp, then unpad to source frame shape.
                    for _fi, _obj_ids, _mask_logits in _video_predictor.propagate_in_video(_state):
                        _L = _mask_logits[0].squeeze().cpu().numpy()
                        _m_padded = _ramp(_L)
                        _mu = (_unpad_from_square(_m_padded, _pad_box)
                               if _pad_box is not None else _m_padded)
                        sam_video_masks[_fi] = _snap_sam_edge(_mu)
                    # Backward: anchor → frame 0. Forward wins on overlap.
                    if sam_anchor_rel > 0:
                        for _fi, _obj_ids, _mask_logits in _video_predictor.propagate_in_video(_state, reverse=True):
                            if _fi in sam_video_masks:
                                continue
                            _L = _mask_logits[0].squeeze().cpu().numpy()
                            _m_padded = _ramp(_L)
                            _mu = (_unpad_from_square(_m_padded, _pad_box)
                                   if _pad_box is not None else _m_padded)
                            sam_video_masks[_fi] = _snap_sam_edge(_mu)

                # Empty / collapsed-mask post-pass — ported from CorridorKey_Pro.py
                # (~1806-1847). SAM2 can yield a near-empty mask on some frames:
                #   - INTERIOR collapse (mid-range tracking glitch): a frame between
                #     two substantial masks goes empty. Filling it with a ones-mask
                #     means CK alone keys that frame (SAM gates nothing) instead of
                #     writing a black frame (empty SAM × CK = nothing).
                #   - TAIL / HEAD empties (actor not in frame at the ends): hold the
                #     nearest substantial mask so the junk SAM was killing stays
                #     killed when the subject leaves frame.
                # Operates on the single-object sam_video_masks dict (AE keys by
                # range-relative frame index; DaVinci keys per obj_id).
                _sorted_keys = sorted(sam_video_masks.keys())
                if _sorted_keys:
                    # Soft-coverage sum (logits_to_soft_mask yields float [0..1], so
                    # sum() = fractional pixel area), scaled by _sam_scale^2 so
                    # hold/collapse behaviour is identical at full-res and 1080p-cap.
                    _sam_thresh = max(1, int(round(100 * _sam_scale * _sam_scale)))
                    _first_sub = next(
                        (f for f in _sorted_keys if sam_video_masks[f].sum() >= _sam_thresh), None)
                    _last_sub = next(
                        (f for f in reversed(_sorted_keys) if sam_video_masks[f].sum() >= _sam_thresh), None)
                    _collapsed = 0
                    _held = 0
                    if _first_sub is not None and _last_sub is not None:
                        for _f in _sorted_keys:
                            if sam_video_masks[_f].sum() >= _sam_thresh:
                                continue
                            if _first_sub <= _f <= _last_sub:
                                # interior collapse -> ones-mask -> CK-only fallback
                                sam_video_masks[_f] = np.ones_like(sam_video_masks[_f])
                                _collapsed += 1
                            elif _f > _last_sub:
                                sam_video_masks[_f] = sam_video_masks[_last_sub].copy()
                                _held += 1
                            else:  # _f < _first_sub (head empty)
                                sam_video_masks[_f] = sam_video_masks[_first_sub].copy()
                                _held += 1
                    log.info(f"SAM2 post-pass: {_collapsed} interior empties -> NN fallback, "
                             f"{_held} tail/head empties held to nearest substantial mask.")
                    # Anchor-frame fix — ported from CorridorKey_Pro.py (~1835-1847).
                    # Frame 0 uses no_mem_embed (image-SAM mode) producing a weaker
                    # mask with interior holes. If frame 2 has >10% more coverage than
                    # frame 0 or 1, copy frame 2 back over the weak anchor frames.
                    if 2 in sam_video_masks:
                        _ref_cov = sam_video_masks[2].sum()
                        if _ref_cov >= _sam_thresh:
                            _patched = []
                            for _early in (0, 1):
                                if _early in sam_video_masks and sam_video_masks[_early].sum() < _ref_cov * 0.9:
                                    sam_video_masks[_early] = sam_video_masks[2].copy()
                                    _patched.append(_early)
                            if _patched:
                                log.info(f"SAM2 anchor-fix: copied frame 2 -> frames {_patched}")

                # VRAM teardown — ported from CorridorKey_Pro.py (1849-1862).
                # reset_state releases SAM2's internal CUDA buffers BEFORE we drop the
                # predictor (fixes the GPU memory leak on Windows, issue #258). Delete
                # state first (holds CUDA tensors), then predictor (holds weights) —
                # wrong order leaks VRAM because the predictor references state.
                # synchronize() waits for the GPU before empty_cache() actually frees.
                try:
                    _video_predictor.reset_state(_state)
                except Exception:
                    pass
                del _state
                del _video_predictor
                if sam_torch.cuda.is_available():
                    sam_torch.cuda.synchronize()
                    sam_torch.cuda.empty_cache()
                log.info(f"SAM2 video: {len(sam_video_masks)} per-frame masks ready")
            finally:
                try:
                    _sam_shutil.rmtree(sam_tmp_dir, ignore_errors=True)
                except Exception:
                    pass
        except Exception as _e:
            log.warning(f"SAM2 video predictor failed for batch: {_e} — keying without SAM matte sidecar")
            sam_video_masks = {}

    # LOUD fallback surfacing: SAM points were set but propagation yielded nothing.
    # Without this the batch silently keys CK-only and the result looks like "SAM did
    # nothing" with no signal. The panel greps stdout for the "CK_WARN:" prefix.
    if sam_active and not sam_video_masks:
        print("CK_WARN: SAM points were set but propagation produced 0 masks. Keying WITHOUT SAM (no CK_COMBINED).", flush=True)
        log.warning("SAM active but 0 masks produced — keying without SAM.")

    # Derive zone_anchor_bbox from the SAM anchor mask when JS did not send it.
    # _zone_cut_from_sam uses this to track the zone rect as the subject moves.
    # Without it the zone stays at raw drawn coords and drifts off the subject.
    if settings.get('zone') is not None and settings.get('zone_anchor_bbox') is None and sam_video_masks:
        _zaf = settings.get('zone_anchor_frame')
        _zab_rel = None
        if _zaf is not None and start_frame <= int(_zaf) < end_frame:
            _zab_rel = int(_zaf) - int(start_frame)
        _zab_mask = None
        if _zab_rel is not None and _zab_rel in sam_video_masks:
            _zab_mask = sam_video_masks[_zab_rel]
        if _zab_mask is None:
            _zab_thresh = max(1, int(round(100 * _sam_scale * _sam_scale)))
            for _fi in sorted(sam_video_masks.keys()):
                if sam_video_masks[_fi].sum() >= _zab_thresh:
                    _zab_mask = sam_video_masks[_fi]
                    break
        if _zab_mask is not None:
            _zb_bin = (_zab_mask > 0.5).astype(np.uint8)
            _zb_cols = np.any(_zb_bin, axis=0)
            _zb_rows = np.any(_zb_bin, axis=1)
            if _zb_cols.any() and _zb_rows.any():
                _zb_x0 = int(np.where(_zb_cols)[0][0])
                _zb_x1 = int(np.where(_zb_cols)[0][-1])
                _zb_y0 = int(np.where(_zb_rows)[0][0])
                _zb_y1 = int(np.where(_zb_rows)[0][-1])
                settings['zone_anchor_bbox'] = [_zb_x0, _zb_y0, _zb_x1 - _zb_x0 + 1, _zb_y1 - _zb_y0 + 1]
                log.info(f"zone_anchor_bbox derived from SAM anchor: {settings['zone_anchor_bbox']}")

    # CK_COMBINED merge: when a per-frame SAM mask exists, combine the RAW CK alpha
    # with the soft SAM silhouette via the SAME dispatcher DaVinci uses
    # (merge_ck_with_sam_active -> garbage_matte mode). This is what makes the AE key
    # match Resolve's clean key instead of raw, over-keyed CK.
    # DRIFT NOTE: AE v1 omits _apply_shirt_rescue (a Resolve-only refinement that lives
    # in the Resolve plugin, not the shared engine). Flagged for a follow-up port.
    from corridorkey_sam_merge import merge_ck_with_sam_active

    processed = 0
    failed = []
    total = max(1, end_frame - start_frame)

    # Disk preflight — refuse to start a render the volume can't hold. Measured
    # cost on 4K is ~30 MB/frame across output+sidecars; estimate from actual
    # source dims at ~5 bytes/px (PNG-compressed, all outputs) with 1.3x margin
    # so the render fails HERE with a clear message, not at frame 184 of 238.
    try:
        import shutil as _sh
        _w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 3840)
        _h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 2160)
        _need = int(total * _w * _h * 5 * 1.3)
        _free = _sh.disk_usage(str(out_dir)).free
        if _free < _need:
            cap.release()
            try:
                processor.cleanup()   # release CUDA weights — preflight bail is outside the try/finally
            except Exception:
                pass
            print(f"CK_ERROR: not enough disk for this render — need ~"
                  f"{_need / 1e9:.1f} GB free on {Path(out_dir).drive or out_dir}, "
                  f"have {_free / 1e9:.1f} GB. Free space or shorten the range.",
                  flush=True)
            log.error(f"Disk preflight failed: need {_need}, free {_free}")
            return False
    except Exception as _pf_e:          # preflight must never block a render itself
        log.warning(f"Disk preflight skipped: {_pf_e}")

    try:
        # FIX C: POS_FRAMES seek + throwaway prime read. CAP_PROP_POS_MSEC returns a DIRTY
        # first read on long-GOP H.264/HEVC (the Resolve engine forbids MSEC for exactly
        # this and seeks via POS_FRAMES). Seek to start_frame-1 and consume one frame so the
        # decoder is warm and the first REAL read below returns a clean start_frame.
        # start_frame==0: no -1 to seek to, so prime-read frame 0 then rewind to 0.
        _sf = int(start_frame)
        if _sf > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, _sf - 1)
            cap.read()                 # throwaway: consumes (_sf-1) -> next read = start_frame
        else:
            # start_frame == 0: NEVER seek. A POS_FRAMES seek (even to 0) flushes the
            # long-GOP decoder, and the first read AFTER a seek is the dirty one — the
            # old warm-read+rewind landed that dirt on the REAL frame 0 (junk first
            # frame on the timeline, Berto 2026-06-04). A freshly opened demuxer sits
            # at frame 0 and decodes it clean sequentially, so reopen and don't seek.
            cap.release()
            cap = cv2.VideoCapture(str(source_video), cv2.CAP_FFMPEG)
        for frame_idx in range(int(start_frame), int(end_frame)):
            seq_num = frame_idx - int(start_frame)
            ok, frame = cap.read()
            if not ok or frame is None:
                failed.append(frame_idx)
                # stdout line parsed by the panel
                print(f"PROGRESS {processed}/{total}", flush=True)
                continue
            try:
                img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                alpha_hint = generate_chroma_hint(img_rgb, settings["screenType"])
                result = processor.process_frame(img_rgb, alpha_hint, ps)
                fg = result.get("fg"); alpha = result.get("alpha")
                if fg is None or alpha is None:
                    failed.append(frame_idx)
                    print(f"PROGRESS {processed}/{total}", flush=True)
                    continue
                if len(alpha.shape) == 3:
                    alpha = alpha[:, :, 0]
                # RAW CK alpha — snapshot BEFORE choke/merge mutate it. The merge and
                # (later) the CK_ALPHA sidecar both need the un-choked matte.
                alpha_raw = alpha.copy()
                _sam_frame = sam_video_masks.get(seq_num)
                # _garbage_gate is only produced by the experimental_recipe path;
                # the GARBAGE sidecar write below guards on it. Default None or the
                # fusion_v2/default paths crash every frame at the sidecar step
                # (0/27 render bug, 2026-06-12).
                _garbage_gate = None
                # Green-aware garbage matte (Berto 2026-06-14): the clean engine's
                # internal keep-gate (white=keep body, black=kill junk), green-screen
                # informed + stable. Set only by the default branch; stays None on the
                # fusion path so the sidecar write falls back to inverted-SAM.
                _green_garbage = None
                if settings.get('experimental_recipe'):
                    alpha, _garbage_gate = apply_recipe_composite(
                        alpha_raw, _sam_frame, alpha.shape[1], settings)
                else:
                    # FUSION V2 branch — trimap + hybrid solve replaces garbage-matte leg.
                    # choke / despeckle / despill run AFTER this block (lines below) — NOT here.
                    # DEPENDS-ON: fusion_v2 package (lazy import; torch-free at cold start)
                    # ISOLATED: guarded by settings.get('fusion_v2') AND _sam_frame is not None
                    if settings.get('fusion_v2') and _sam_frame is not None:
                        import sys as _sys
                        _fv2_root = str(Path(__file__).resolve().parent.parent.parent)
                        if _fv2_root not in _sys.path:
                            _sys.path.insert(0, _fv2_root)
                        from fusion_v2.trimap_builder import build_trimap as _build_trimap
                        import fusion_v2.solver_guided    # noqa: F401 — self-registers 'guided'
                        import fusion_v2.solver_vitmatte  # noqa: F401 — self-registers 'vitmatte' (torch loaded lazily inside solve)
                        import fusion_v2.solver_hybrid    # noqa: F401 — self-registers 'hybrid'
                        from fusion_v2.solver_interface import solve_matte as _solve_matte
                        _sf_b = (_sam_frame if _sam_frame.shape[:2] == alpha_raw.shape[:2]
                                 else cv2.resize(_sam_frame,
                                                 (alpha_raw.shape[1], alpha_raw.shape[0]),
                                                 interpolation=cv2.INTER_LINEAR))
                        _sf_2d = _sf_b if _sf_b.ndim == 2 else _sf_b[..., 0]
                        _sam_bin_b = (np.clip(_sf_2d, 0, 1) > 0.5).astype(np.uint8) * 255
                        _frame_u8_b = (img_rgb if img_rgb.dtype == np.uint8
                                       else (np.clip(img_rgb, 0, 1) * 255).astype(np.uint8))
                        _expand = int(settings.get("fusion_expand", 6))
                        _trimap_b = _build_trimap(_sam_bin_b, alpha_raw, dilate_pct=_expand / 100.0)
                        alpha = _solve_matte(_frame_u8_b, _trimap_b, alpha_raw, solver='hybrid', sam_binary=_sam_bin_b)
                        # Shirt/harness rescue (Berto 2026-06-14): the fusion engine never
                        # ported this step (DRIFT NOTE ~line 1159) — the OLD default path and
                        # the DaVinci original both run it. Dark webbing/clothing (not green,
                        # inside SAM body) is under-keyed by CK; this forces alpha=max(alpha,
                        # SAM) there so the harness strap survives on the frames SAM keeps the
                        # body. _sf_2d is the soft SAM silhouette already resized to alpha above.
                        alpha = apply_shirt_rescue(alpha, _sf_2d, img_rgb, settings)
                        if settings.get('zone') and _sam_frame is not None:
                            _h_z, _w_z = alpha.shape[:2]
                            _sz = (_sam_frame if _sam_frame.shape[:2] == (_h_z, _w_z)
                                   else cv2.resize(_sam_frame, (_w_z, _h_z),
                                                   interpolation=cv2.INTER_LINEAR))
                            _zone_mask = _zone_cut_from_sam(
                                (_sz > 0.5).astype(np.uint8), settings, _w_z, _h_z, _w_z / 1920.0)
                            alpha = np.clip(alpha * _zone_mask, 0.0, 1.0)
                        log.info(f'fusion_v2 batch frame {frame_idx}: hybrid solve done')
                    else:
                        # Default: 06-10 proven merge chain
                        alpha, _green_garbage = sam_garbage_merge(
                            alpha_raw, _sam_frame, img_rgb, settings,
                            settings["screenType"], return_garbage=True)
                        _sam_for_rescue = _sam_frame
                        if _sam_for_rescue is not None and _sam_for_rescue.shape[:2] != alpha.shape[:2]:
                            _sam_for_rescue = cv2.resize(
                                _sam_for_rescue, (alpha.shape[1], alpha.shape[0]),
                                interpolation=cv2.INTER_LINEAR)
                        alpha = apply_shirt_rescue(alpha, _sam_for_rescue, img_rgb, settings)
                        if settings.get('zone') and _sam_frame is not None:
                            _h_z, _w_z = alpha.shape[:2]
                            _sz = _sam_frame if _sam_frame.shape[:2] == (_h_z, _w_z) else cv2.resize(
                                _sam_frame, (_w_z, _h_z), interpolation=cv2.INTER_LINEAR)
                            _zone_mask = _zone_cut_from_sam(
                                (_sz > 0.5).astype(np.uint8), settings, _w_z, _h_z, _w_z / 1920.0)
                            alpha = np.clip(alpha * _zone_mask, 0.0, 1.0)
                alpha = apply_choke(alpha, settings)
                alpha = apply_despeckle(alpha, settings)
                fg = apply_despill(fg, settings)
                fg_uint16 = (np.clip(fg, 0, 1) * 65535).astype(np.uint16)
                alpha_uint16 = (np.clip(alpha, 0, 1) * 65535).astype(np.uint16)
                fg_bgr = cv2.cvtColor(fg_uint16, cv2.COLOR_RGB2BGR)
                out_bgra = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint16])
                _atomic_imwrite(out_dir / f"output_{seq_num:05d}.png", out_bgra)
                # CK_ONLY keyed clip (Berto 2026-06-14): the CK key with NO SAM clip —
                # full hair + all junk. SAM clips hair on the merged result; the user
                # matte-boxes the head from THIS clip and lays it back over the merged
                # key to restore the eaten hair. Despilled fg + RAW CK alpha (pre-fusion,
                # un-choked), so it carries every wisp the merged output loses.
                _ck_a = alpha_raw[:, :, 0] if alpha_raw.ndim == 3 else alpha_raw
                _ck_a16 = (np.clip(_ck_a, 0, 1) * 65535).astype(np.uint16)
                _ck_only_bgra = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], _ck_a16])
                _ck_only_dir = out_dir / "CK_ONLY"
                _ck_only_dir.mkdir(parents=True, exist_ok=True)
                _atomic_imwrite(_ck_only_dir / f"CK_ONLY_{seq_num:05d}.png", _ck_only_bgra)
                if processed == 0:
                    _atomic_imwrite(_ck_only_dir / "CK_ONLY_00000.png", _ck_only_bgra)
                # GARBAGE_MATTE (Berto 2026-06-14): the clean engine's green-aware keep-gate,
                # surfaced as a STABLE knock-out matte — better than raw inverted-SAM (SAM_JUNK),
                # which wobbles per-frame. Written SAME polarity as SAM_JUNK (white=junk) so it
                # is a drop-in luma-inverted matte in the precomp. None on the fusion path, so
                # the precomp falls back to SAM_JUNK there.
                if _green_garbage is not None:
                    _gg = _green_garbage[:, :, 0] if _green_garbage.ndim == 3 else _green_garbage
                    if _gg.shape[:2] != alpha_raw.shape[:2]:
                        _gg = cv2.resize(_gg.astype(np.float32),
                                         (alpha_raw.shape[1], alpha_raw.shape[0]),
                                         interpolation=cv2.INTER_LINEAR)
                    _gg_junk = ((1.0 - np.clip(_gg, 0.0, 1.0)) * 255.0).astype(np.uint8)
                    _gg_dir = out_dir / "GARBAGE_MATTE"
                    _gg_dir.mkdir(parents=True, exist_ok=True)
                    _gg_img = cv2.merge([_gg_junk, _gg_junk, _gg_junk])
                    _atomic_imwrite(_gg_dir / f"GARBAGE_MATTE_{seq_num:05d}.png", _gg_img)
                    if processed == 0:
                        _atomic_imwrite(_gg_dir / "GARBAGE_MATTE_00000.png", _gg_img)
                # Sidecar deliverables: CK_ALPHA (raw NN matte) + SAM_JUNK (inverted SAM mask).
                _sam_for_sidecar = None
                if seq_num in sam_video_masks:
                    _sam_for_sidecar = sam_video_masks[seq_num]
                    if _sam_for_sidecar.shape[:2] != alpha_raw.shape[:2]:
                        _sam_for_sidecar = cv2.resize(
                            _sam_for_sidecar,
                            (alpha_raw.shape[1], alpha_raw.shape[0]),
                            interpolation=cv2.INTER_LINEAR,
                        )
                _write_fusion_sidecars(
                    alpha_raw, _sam_for_sidecar,
                    settings, out_dir, seq_num, is_first=(processed == 0),
                )
                # Premiere Pro's sequence importer silently drops the first frame. Write
                # a dummy output_00000.png (and matching matte) so the user's actual
                # frame range survives the import intact.
                # NOTE: output_00000 is the REAL first frame (seq_num starts at 0), not a
                # separate leader — this re-write is a harmless no-op duplicate of the
                # seq-0 write above. Kept for the Premiere drop-first-frame comment above;
                # FIX C's clean POS_FRAMES decode means frame 0 itself is no longer dirty.
                if processed == 0:
                    _atomic_imwrite(out_dir / "output_00000.png", out_bgra)
                processed += 1
            except Exception as e:
                failed.append(frame_idx)
                log.warning(f"Frame {frame_idx}: {e}")
            print(f"PROGRESS {processed}/{total}", flush=True)
    finally:
        cap.release()
        processor.cleanup()
        # Flush any leftover SAM2 video predictor allocations.
        if sam_torch is not None:
            try:
                if sam_torch.cuda.is_available():
                    sam_torch.cuda.empty_cache()
            except Exception:
                pass

    (out_dir / "batch_result.txt").write_text(f"{processed},{total},{len(failed)}")
    log.info(f"Done: {processed}/{total} ({len(failed)} failed)")
    # LOUD fallback surfacing: if a merge crash file was (re)written during this run, the
    # CK_COMBINED merge faulted and silently fell back to plain CK on >=1 frame. Tell the
    # user instead of shipping a worse key as if it were the clean one.
    for _cf, _was in _merge_crash_mtimes.items():
        try:
            _now = _cf.stat().st_mtime if _cf.exists() else None
        except Exception:
            _now = None
        if _now is not None and _now != _was:
            print(f"CK_WARN: CK_COMBINED merge faulted and fell back to plain CK on at least one frame. Trace: {_cf}", flush=True)
            log.warning(f"Merge fallback detected this run — see {_cf}")
    return processed


# ── Subcommand: batch-scrub (stage 1 only — feeds viewer scrub mode) ───
# WHAT IT DOES: Keys N consecutive frames from a video and writes each as
#   scrub/NNN/fg.png + alpha.png, then writes scrub_index.json so the live
#   preview viewer detects scrub mode and shows the slider bar.
# DEPENDS-ON: corridorkey_processor.CorridorKeyProcessor, CUDA GPU, source
#   video readable by OpenCV.
# AFFECTS: writes scrub/NNN/fg.png + alpha.png per frame, plus scrub_index.json
#   in the parent of scrub_folder. Emits PROGRESS on stdout for the panel.
# NOTE: Like cmd_cache, post-proc is DISABLED — the viewer applies despill/
#   choke/despeckle live from sliders. uint16 PNG matches cache precision.
def cmd_batch_scrub(source_video, scrub_folder, settings,
                    start_frame=None, count=None):
    import numpy as np
    import cv2
    if count is None:
        count = int(settings.get("count", 10))
    if start_frame is None:
        start_frame = int(settings.get("startFrame", 0))

    scrub_dir = Path(scrub_folder)
    scrub_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Batch-scrub: {source_video} frames {start_frame}..{start_frame+count}")

    cap = cv2.VideoCapture(str(source_video), cv2.CAP_FFMPEG)   # FIX C: FFMPEG backend
    if not cap.isOpened():
        log.error(f"Cannot open: {source_video}")
        return 0
    source_fps = cap.get(cv2.CAP_PROP_FPS)
    if not source_fps or source_fps <= 0:
        log.warning("Source fps unknown, defaulting to 24")
        source_fps = 24.0

    # Scrub resolution cap: clamp to 1080p-class to save VRAM and speed up scrub.
    # RENDER (cmd_batch) runs full-res — only scrub is capped here.
    _src_w_probe = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    _src_h_probe = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    if max(_src_w_probe, _src_h_probe) > 1920:
        _scrub_scale = 1920.0 / max(_src_w_probe, _src_h_probe)
        _scrub_w = int(round(_src_w_probe * _scrub_scale))
        _scrub_h = int(round(_src_h_probe * _scrub_scale))
        log.info(f"Scrub downscale: {_src_w_probe}x{_src_h_probe} -> {_scrub_w}x{_scrub_h} (scale={_scrub_scale:.4f})")
    else:
        _scrub_scale = 1.0
        _scrub_w = _src_w_probe
        _scrub_h = _src_h_probe

    from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    processor = CorridorKeyProcessor(device="cuda")
    ps = ProcessingSettings(
        screen_type=settings["screenType"],
        despill_strength=0.0,
        despeckle_enabled=False,
        despeckle_size=settings.get("despeckleSize", 400),
        refiner_strength=float(settings.get("refiner", 1.0)),
    )

    # SAM2 video propagation. Click points came from the viewer's APPLY MASK on
    # the anchor frame. We pre-compute masks for ALL scrub frames using SAM2's
    # VIDEO predictor, which TRACKS the click points as the subject moves —
    # the image predictor with fixed coords loses the subject when it lands or
    # changes pose (DaVinci's CorridorKey.py takes the same approach).
    session_dir = scrub_dir.parent
    sam_pos = []
    sam_neg = []
    sam_anchor_abs = None  # absolute video frame number where user clicked
    try:
        lp_path = session_dir / "live_params.json"
        if lp_path.exists():
            with open(str(lp_path)) as _lp:
                _lp_data = json.load(_lp)
            sam_pos = _lp_data.get("sam_positive", []) or []
            sam_neg = _lp_data.get("sam_negative", []) or []
            sam_anchor_abs = _lp_data.get("sam_anchor_frame")
    except Exception as _e:
        log.warning(f"Could not read SAM2 points from live_params: {_e}")
    if not sam_pos and not sam_neg:
        sam_pos = settings.get('sam_positive', []) or []
        sam_neg = settings.get('sam_negative', []) or []
    if sam_anchor_abs is None:
        sam_anchor_abs = settings.get('sam_anchor_frame')

    # Map absolute click frame → range-relative index. If the click was
    # outside the scrub range (or never recorded), anchor at frame 0 and
    # forward-propagate only — same fallback DaVinci uses.
    if sam_anchor_abs is not None and start_frame <= int(sam_anchor_abs) < (start_frame + int(count)):
        sam_anchor_rel = int(sam_anchor_abs) - int(start_frame)
    else:
        sam_anchor_rel = 0

    sam_active = (len(sam_pos) + len(sam_neg)) > 0
    sam_video_masks = {}  # {frame_offset: float32 soft mask 0..1}
    sam_torch = None
    if sam_active:
        try:
            import torch as sam_torch
            import tempfile as _sam_tmp
            import shutil as _sam_shutil
            from sam2.build_sam import build_sam2_video_predictor
            # Use the module-level CK_ROOT (find_corridorkey_root) — the old inline
            # `Path(__file__).parent.parent` fallback resolved to the CEP extensions
            # folder, so SAM2 weights load silently failed. CK_ROOT honours the same
            # env / corridorkey_path.txt / fallback chain the Resolve plugin uses.
            ckpt = str(CK_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
            cfg = "configs/sam2.1/sam2.1_hiera_s.yaml"
            device = "cuda" if sam_torch.cuda.is_available() else "cpu"

            # Export the N scrub frames to a temp dir — SAM2 video predictor
            # reads frames from disk via init_state(video_path=...). Phase 0
            # 2026-05-09: lossless PNG (was JPEG q=95) and letterbox-padded to
            # square BEFORE SAM sees them, so the encoder downsample is uniform.
            sam_tmp_dir = Path(_sam_tmp.mkdtemp(prefix="ck_sam2_scrub_"))
            log.info(f"SAM2 video: exporting {count} frames to {sam_tmp_dir}")
            from corridorkey_sam_merge import (
                pad_to_square as _pad_to_square,
                unpad_from_square as _unpad_from_square,
                shift_points_for_padding as _shift_pts,
                patch_sam2_loader_for_png as _patch_sam2_png,
                logits_to_soft_mask as _ramp,
            )
            _patch_sam2_png()
            _src_h = _src_w = _pad_box = None
            try:
                _exp_cap = cv2.VideoCapture(str(source_video), cv2.CAP_FFMPEG)
                # FIX C: POS_FRAMES + throwaway prime read (MSEC dirty first read on long-GOP).
                _sf = int(start_frame)
                if _sf > 0:
                    _exp_cap.set(cv2.CAP_PROP_POS_FRAMES, _sf - 1)
                    _exp_cap.read()        # throwaway warm-up -> next read = start_frame
                else:
                    _exp_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    _exp_cap.read()
                    _exp_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                _exported = 0
                for _i in range(int(count)):
                    _ok, _fr = _exp_cap.read()
                    if not _ok or _fr is None:
                        log.warning(f"SAM2 video: skipped unreadable frame {start_frame + _i}")
                        continue
                    _fr_scaled = cv2.resize(_fr, (_scrub_w, _scrub_h), interpolation=cv2.INTER_AREA) if _scrub_scale != 1.0 else _fr
                    if _src_h is None:
                        _src_h, _src_w = _fr.shape[:2]
                        _, _pad_box = _pad_to_square(_fr_scaled)
                        log.info(f"SAM2 video: source {_src_w}x{_src_h} -> "
                                 f"scrub {_scrub_w}x{_scrub_h} -> "
                                 f"padded square {max(_scrub_h, _scrub_w)} "
                                 f"(scale={_scrub_scale:.4f}, pad_box={_pad_box})")
                    _padded, _ = _pad_to_square(_fr_scaled)
                    cv2.imwrite(str(sam_tmp_dir / f"{_i:06d}.png"), _padded,
                                [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    _exported += 1
                _exp_cap.release()
                log.info(f"SAM2 video: {_exported} frames exported")

                # Load video predictor and propagate from the actual click anchor.
                # DaVinci's two-pass approach: forward from anchor → end, then
                # backward from anchor → 0 (only if anchor isn't already 0).
                # Reusing the SAME state across both passes is critical — the
                # tracker memory built during the forward pass carries over and
                # makes backward results coherent. Reinitialising would lose it.
                _video_predictor = build_sam2_video_predictor(cfg, ckpt, device=device)
                _all_pts = list(sam_pos) + list(sam_neg)
                _labels  = [1] * len(sam_pos) + [0] * len(sam_neg)
                if _scrub_scale != 1.0:
                    _all_pts = [[p[0] * _scrub_scale, p[1] * _scrub_scale] for p in _all_pts]
                _all_pts_padded = _shift_pts(_all_pts, _pad_box) if _pad_box is not None else _all_pts
                log.info(f"SAM2 video: anchor at range frame {sam_anchor_rel} (absolute {sam_anchor_abs})")
                with sam_torch.inference_mode():
                    _state = _video_predictor.init_state(
                        video_path=str(sam_tmp_dir),
                        offload_video_to_cpu=True,
                        async_loading_frames=False,
                    )
                    _video_predictor.add_new_points_or_box(
                        inference_state=_state,
                        frame_idx=sam_anchor_rel,
                        obj_id=1,
                        points=np.array(_all_pts_padded, dtype=np.float32),
                        labels=np.array(_labels, dtype=np.int32),
                        clear_old_points=True,
                    )
                    # Forward: anchor → last frame. Logits at padded square
                    # shape — apply ramp then unpad back to source frame shape.
                    for _fi, _obj_ids, _mask_logits in _video_predictor.propagate_in_video(_state):
                        _L = _mask_logits[0].squeeze().cpu().numpy()
                        _m_padded = _ramp(_L)
                        sam_video_masks[_fi] = (
                            _unpad_from_square(_m_padded, _pad_box)
                            if _pad_box is not None else _m_padded
                        )
                    # Backward: anchor → frame 0. Skip frames the forward pass
                    # already filled (forward wins on overlap, same as DaVinci).
                    if sam_anchor_rel > 0:
                        for _fi, _obj_ids, _mask_logits in _video_predictor.propagate_in_video(_state, reverse=True):
                            if _fi in sam_video_masks:
                                continue
                            _L = _mask_logits[0].squeeze().cpu().numpy()
                            _m_padded = _ramp(_L)
                            sam_video_masks[_fi] = (
                                _unpad_from_square(_m_padded, _pad_box)
                                if _pad_box is not None else _m_padded
                            )
                # VRAM teardown — ported from CorridorKey_Pro.py (1849-1862).
                # reset_state releases SAM2's internal CUDA buffers BEFORE we drop the
                # predictor (fixes the GPU memory leak on Windows, issue #258). Delete
                # state first (holds CUDA tensors), then predictor (holds weights) —
                # wrong order leaks VRAM. synchronize() before empty_cache() so the
                # GPU has actually finished before the cache is freed.
                try:
                    _video_predictor.reset_state(_state)
                except Exception:
                    pass
                del _state
                del _video_predictor
                if sam_torch.cuda.is_available():
                    sam_torch.cuda.synchronize()
                    sam_torch.cuda.empty_cache()
                log.info(f"SAM2 video: {len(sam_video_masks)} per-frame masks ready")
            finally:
                try:
                    _sam_shutil.rmtree(sam_tmp_dir, ignore_errors=True)
                except Exception:
                    pass
        except Exception as _e:
            log.warning(f"SAM2 video predictor failed for scrub: {_e} — keying without SAM2")
            sam_video_masks = {}

    keyed = 0
    failed = []
    try:
        # FIX C: POS_FRAMES seek + throwaway prime read (same as cmd_batch) — MSEC returns a
        # dirty first read on long-GOP. Warm the decoder so the first real read is clean.
        _sf = int(start_frame)
        if _sf > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, _sf - 1)
            cap.read()                 # throwaway warm-up -> next read = start_frame
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            cap.read()
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        for frame_offset in range(int(count)):
            frame_idx = start_frame + frame_offset
            ok, frame = cap.read()
            if not ok or frame is None:
                failed.append(frame_idx)
                print(f"PROGRESS {keyed}/{count}", flush=True)
                continue
            try:
                img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
                if _scrub_scale != 1.0:
                    img_rgb = cv2.resize(img_rgb, (_scrub_w, _scrub_h), interpolation=cv2.INTER_AREA)
                alpha_hint = generate_chroma_hint(img_rgb, settings["screenType"])
                result = processor.process_frame(img_rgb, alpha_hint, ps)
                fg = result.get("fg")
                alpha = result.get("alpha")
                if fg is None or alpha is None:
                    failed.append(frame_idx)
                    print(f"PROGRESS {keyed}/{count}", flush=True)
                    continue
                if len(alpha.shape) == 3:
                    alpha = alpha[:, :, 0]

                out_dir = scrub_dir / f"{frame_offset:03d}"
                out_dir.mkdir(parents=True, exist_ok=True)
                # uint16 — match cmd_cache for precision under live sliders.
                fg_u16 = (np.clip(fg, 0, 1) * 65535.0).astype(np.uint16)
                alpha_u16 = (np.clip(alpha, 0, 1) * 65535.0).astype(np.uint16)
                fg_bgr_u16 = cv2.cvtColor(fg_u16, cv2.COLOR_RGB2BGR)
                cv2.imwrite(str(out_dir / "fg.png"), fg_bgr_u16)
                cv2.imwrite(str(out_dir / "alpha.png"), alpha_u16)
                # Raw plate — source_rgb for garbage_matte body fill / chroma escape.
                plate_u8 = (np.clip(img_rgb, 0, 1) * 255).astype(np.uint8)
                cv2.imwrite(str(out_dir / "plate.png"), cv2.cvtColor(plate_u8, cv2.COLOR_RGB2BGR))
                # Pull the pre-propagated video-predictor mask for this frame.
                # Saving alpha_nn.png + sam2_gate_raw.png makes the viewer's
                # render do alpha_nn × gate composite (with live margin/soften
                # sliders) instead of falling back to plain alpha.png.
                if frame_offset in sam_video_masks:
                    try:
                        _gate_soft = sam_video_masks[frame_offset]
                        # Resize gate to match alpha shape — NN may downscale
                        # internally so its output shape can differ from source.
                        if _gate_soft.shape[:2] != alpha.shape[:2]:
                            _gate_soft = cv2.resize(
                                _gate_soft,
                                (alpha.shape[1], alpha.shape[0]),
                                interpolation=cv2.INTER_LINEAR,
                            )
                        _gate_u16 = (np.clip(_gate_soft, 0, 1) * 65535.0).astype(np.uint16)
                        cv2.imwrite(str(out_dir / "sam2_gate_raw.png"), _gate_u16)
                        cv2.imwrite(str(out_dir / "alpha_nn.png"), alpha_u16)
                    except Exception as _se:
                        log.warning(f"SAM2 frame {frame_idx}: gate save failed: {_se}")
                keyed += 1
            except Exception as e:
                failed.append(frame_idx)
                log.warning(f"Frame {frame_idx}: {e}")
            print(f"PROGRESS {keyed}/{count}", flush=True)
    finally:
        cap.release()
        processor.cleanup()
        # SAM2 video predictor was cleaned up inside the setup block;
        # this final cuda cache flush mops up after the keying NN.
        if sam_torch is not None:
            try:
                if sam_torch.cuda.is_available():
                    sam_torch.cuda.empty_cache()
            except Exception:
                pass

    # Write scrub_index.json to the PARENT of scrub_folder so the viewer
    # (watching <session_dir>/scrub_index.json) detects it.
    index_path = scrub_dir.parent / "scrub_index.json"
    try:
        with open(str(index_path), "w") as f:
            json.dump({"count": keyed, "base_dir": scrub_dir.name}, f)
        log.info(f"Wrote {index_path}: count={keyed}")
    except Exception as e:
        log.error(f"Failed to write scrub_index.json: {e}")

    log.info(f"Batch-scrub done: {keyed}/{count} frames ({len(failed)} failed)")
    return keyed


# ── Subcommand: cache (stage 1 only — live-preview support) ───
# WHAT IT DOES: Runs the CorridorKey neural net on a PNG input and writes the raw
#   foreground + alpha to disk with post-proc DISABLED (despill=0, despeckle=off).
#   This is stage 1 of a two-stage split that lets the live preview viewer re-run
#   only the cheap post-proc when a slider moves — no neural-net re-run required.
# DEPENDS-ON: corridorkey_processor.CorridorKeyProcessor, an input PNG that has
#   already been extracted from the source video (see cmd_extract), CUDA GPU.
# AFFECTS: writes fg.png + alpha.png + meta.json into the given session directory.
# NOTE: refiner_scale IS applied here because it's NN-internal — it's not
#   separable the way despill and despeckle are. Refiner slider changes still
#   require a full cache re-run; despill and despeckle sliders do not.
def cmd_cache(input_path, session_dir, settings):
    import numpy as np
    import cv2
    sess = Path(session_dir)
    sess.mkdir(parents=True, exist_ok=True)

    log.info(f"Cache: {input_path} -> {sess}")
    img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        log.error(f"Cannot read: {input_path}")
        return False

    has_alpha = len(img.shape) == 3 and img.shape[2] == 4
    if has_alpha:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    else:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    if img_rgb.dtype == np.uint8:
        img_rgb = img_rgb.astype(np.float32) / 255.0
    elif img_rgb.dtype == np.uint16:
        img_rgb = img_rgb.astype(np.float32) / 65535.0

    alpha_hint = generate_chroma_hint(img_rgb, settings["screenType"])

    from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    processor = CorridorKeyProcessor(device="cuda")
    try:
        ps = ProcessingSettings(
            screen_type=settings["screenType"],
            despill_strength=0.0,
            despeckle_enabled=False,
            despeckle_size=settings.get("despeckleSize", 400),
            refiner_strength=float(settings.get("refiner", 1.0)),
        )
        result = processor.process_frame(img_rgb, alpha_hint, ps)
        fg = result.get("fg")
        alpha = result.get("alpha")
        if fg is None or alpha is None:
            log.error("Cache keyer returned no output")
            return False
        if len(alpha.shape) == 3:
            alpha = alpha[:, :, 0]

        # Write as uint16 PNG to keep extra precision vs uint8. The viewer reads
        # these back, normalizes to float32, and runs post-proc. uint16 PNG is
        # widely supported by OpenCV and keeps file sizes modest.
        fg_u16 = (np.clip(fg, 0, 1) * 65535.0).astype(np.uint16)
        alpha_u16 = (np.clip(alpha, 0, 1) * 65535.0).astype(np.uint16)
        fg_bgr_u16 = cv2.cvtColor(fg_u16, cv2.COLOR_RGB2BGR)

        cv2.imwrite(str(sess / "fg.png"), fg_bgr_u16)
        cv2.imwrite(str(sess / "alpha.png"), alpha_u16)
        # Raw plate — source_rgb for garbage_matte body fill / chroma escape.
        plate_u8 = (np.clip(img_rgb, 0, 1) * 255).astype(np.uint8)
        cv2.imwrite(str(sess / "plate.png"), cv2.cvtColor(plate_u8, cv2.COLOR_RGB2BGR))
        # Zone fallback: write an all-ones uint16 gate so cmd_postproc fires
        # apply_recipe_composite (zone_cut + garbage_gate) on single-frame previews
        # even when no SAM dots are placed. cmd_sam_apply overwrites this with the
        # real SAM mask when dots exist.
        if settings.get('zone') is not None:
            _ones_gate = np.full(alpha.shape[:2], 65535, dtype=np.uint16)
            cv2.imwrite(str(sess / "sam2_gate_raw.png"), _ones_gate)

        # frame_num lets the viewer record where the user clicked SAM2 points
        # (as sam_anchor_frame in live_params.json). cmd_batch_scrub uses it
        # later to anchor SAM2 video propagation at the actual click frame —
        # without it, propagation defaults to scrub range frame 0 and the
        # tracker fails when the click frame is elsewhere in the range.
        _src_frame = settings.get("sourceFrame")
        meta = {
            "source": str(input_path),
            "width": int(img_rgb.shape[1]),
            "height": int(img_rgb.shape[0]),
            "screenType": settings["screenType"],
            "refiner": float(settings.get("refiner", 1.0)),
            "frame_num": int(_src_frame) if _src_frame is not None else None,
            "created": __import__("datetime").datetime.now().isoformat(),
            "dtype": "uint16",
            "note": "Post-proc disabled — apply despill/despeckle at view time via postproc subcommand or color_utils.",
        }
        (sess / "meta.json").write_text(json.dumps(meta, indent=2))
        log.info(f"Cached stage-1 outputs to {sess}")
        return True
    finally:
        processor.cleanup()


# ── Subcommand: postproc (stage 2 — cheap, slider-driven) ─────
# WHAT IT DOES: Reads the cached fg.png + alpha.png from a session directory, applies
#   despill + despeckle with the given settings, optionally composites onto a chosen
#   background, and writes the result as a single RGBA PNG. This is the cheap stage
#   that runs on every slider move in the live preview viewer — no neural net involved.
# DEPENDS-ON: a prior `cache` run having written fg.png + alpha.png to session_dir,
#   CorridorKeyModule/core/color_utils.py (despill_opencv, clean_matte_opencv).
# AFFECTS: writes the given output PNG.
# NOTE: The background argument controls what the keyed foreground is composited over.
#   'none' = raw RGBA with no composite. 'black'/'white'/'checker' = solid or generated.
#   'v1-below' expects the caller to pass --v1-path pointing at a PNG of the timeline
#   V1 frame to use as a background plate.
def _maybe_downscale(img, max_width):
    # Shrink a BGR/BGRA image to max_width for the inline panel preview. A full 4K
    # PNG base64s to ~6.5MB which exceeds CEF's data-URI limit and silently fails
    # to render; a panel-sized preview loads instantly.
    import cv2
    if max_width and img.shape[1] > int(max_width):
        mw = int(max_width)
        h = int(round(img.shape[0] * mw / img.shape[1]))
        return cv2.resize(img, (mw, h), interpolation=cv2.INTER_AREA)
    return img


def cmd_postproc(session_dir, output_path, settings, background="checker", v1_path=None, max_width=None):
    import numpy as np
    import cv2
    sess = Path(session_dir)
    fg_path = sess / "fg.png"
    alpha_path = sess / "alpha.png"
    if not fg_path.exists() or not alpha_path.exists():
        log.error(f"Session missing fg.png or alpha.png: {sess}")
        return False

    fg_raw = cv2.imread(str(fg_path), cv2.IMREAD_UNCHANGED)
    alpha_raw = cv2.imread(str(alpha_path), cv2.IMREAD_UNCHANGED)
    if fg_raw is None or alpha_raw is None:
        log.error("Cannot read cached fg/alpha")
        return False

    # Normalize to float32 0..1 regardless of stored dtype
    fg_bgr = fg_raw.astype(np.float32)
    fg_bgr /= (65535.0 if fg_raw.dtype == np.uint16 else 255.0)
    alpha = alpha_raw.astype(np.float32)
    alpha /= (65535.0 if alpha_raw.dtype == np.uint16 else 255.0)
    if len(alpha.shape) == 3:
        alpha = alpha[:, :, 0]
    fg_rgb = cv2.cvtColor(fg_bgr, cv2.COLOR_BGR2RGB)

    # SAM gate (optional) -> soft silhouette, then the SHARED post-proc so the inline
    # PREVIEW is byte-identical to the RENDER (KEY FRAME / WORK AREA). source_rgb uses
    # plate.png (raw frame before NN) when available — fixes garbage_matte body fill.
    sam_soft = None
    gate_path = sess / "sam2_gate_raw.png"
    if gate_path.exists():
        _g = cv2.imread(str(gate_path), cv2.IMREAD_UNCHANGED)
        if _g is not None:
            sam_soft = _g.astype(np.float32) / (65535.0 if _g.dtype == np.uint16 else 255.0)
            _sam_erode_px = int(settings.get("sam_erode_px", 0))
            if _sam_erode_px > 0:
                _ek = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_sam_erode_px * 2 + 1, _sam_erode_px * 2 + 1))
                sam_soft = cv2.erode((np.clip(sam_soft, 0, 1) * 255).astype(np.uint8), _ek, iterations=1).astype(np.float32) / 255.0
            _sam_expand_px = int(settings.get("sam_expand_px", 0))
            if _sam_expand_px > 0:
                _ek2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_sam_expand_px * 2 + 1, _sam_expand_px * 2 + 1))
                sam_soft = cv2.dilate((np.clip(sam_soft, 0, 1) * 255).astype(np.uint8), _ek2, iterations=1).astype(np.float32) / 255.0
    # PRE-RENDER MATTE INSPECTOR (Berto 2026-06-06: "we need to see the different
    # mattes sam/ck/and combined before we render"). matte-ck = the raw NN matte
    # before any merge; matte-sam = the raw SAM silhouette. Both write-and-return
    # here, BEFORE post-proc. The 'matte' (combined) view falls through to the
    # shared post-proc below so it stays byte-identical to the render.
    if background in ("matte-ck", "matte-sam"):
        if background == "matte-ck":
            view = alpha
        elif sam_soft is not None:
            try:
                from corridorkey_sam_merge import solidify_sam_silhouette
                view = solidify_sam_silhouette(
                    sam_soft, carve_points=settings.get("sam_negative") or None
                ).astype(np.float32)
            except Exception as _sv_e:
                log.warning(f"SAM view solidify failed, showing raw gate: {_sv_e}")
                view = sam_soft
        else:
            view = np.zeros_like(alpha)
        m8 = (np.clip(view, 0, 1) * 255).astype(np.uint8)
        m8 = _maybe_downscale(m8, max_width)
        _mv_out = Path(output_path)
        _mv_out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_imwrite(_mv_out, cv2.merge([m8, m8, m8]))
        log.info(f"Postproc ({background} view) saved: {_mv_out}")
        return True

    # Load raw plate if cached — gives garbage_matte correct on-green detection.
    # Falls back to None (skips body fill entirely) if plate.png absent.
    _plate_path = sess / "plate.png"
    _plate_raw = cv2.imread(str(_plate_path), cv2.IMREAD_UNCHANGED) if _plate_path.exists() else None
    _source_rgb = (
        cv2.cvtColor(_plate_raw, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        if _plate_raw is not None else None
    )

    # cu is also used by the background/composite section below.
    from CorridorKeyModule.core import color_utils as cu
    if settings.get('experimental_recipe'):
        if sam_soft is not None and not bool(settings.get("sam2_bypass", False)):
            alpha, _ = apply_recipe_composite(alpha, sam_soft, fg_rgb.shape[1], settings)
        alpha = apply_choke(alpha, settings)
        alpha = apply_despeckle(alpha, settings)
        fg_rgb = apply_despill(fg_rgb, settings)
    else:
        # FUSION V2 branch — trimap + hybrid solve replaces apply_matte_postproc.
        # choke / despeckle / despill MUST run inside this branch (not handled after).
        # DEPENDS-ON: fusion_v2 package (lazy import; torch-free at cold start)
        # ISOLATED: guarded by settings.get('fusion_v2') AND sam_soft and _source_rgb present
        if settings.get('fusion_v2') and sam_soft is not None and _source_rgb is not None:
            import sys as _sys
            _fv2_root = str(Path(__file__).resolve().parent.parent.parent)
            if _fv2_root not in _sys.path:
                _sys.path.insert(0, _fv2_root)
            from fusion_v2.trimap_builder import build_trimap as _build_trimap
            import fusion_v2.solver_guided    # noqa: F401 — self-registers 'guided'
            import fusion_v2.solver_vitmatte  # noqa: F401 — self-registers 'vitmatte' (torch loaded lazily inside solve)
            import fusion_v2.solver_hybrid    # noqa: F401 — self-registers 'hybrid'
            from fusion_v2.solver_interface import solve_matte as _solve_matte
            _sf_pp = np.asarray(sam_soft, dtype=np.float32)
            if _sf_pp.ndim == 3:
                _sf_pp = _sf_pp[..., 0]
            if _sf_pp.shape[:2] != alpha.shape[:2]:
                _sf_pp = cv2.resize(_sf_pp, (alpha.shape[1], alpha.shape[0]),
                                    interpolation=cv2.INTER_LINEAR)
            _sam_bin_pp = (np.clip(_sf_pp, 0, 1) > 0.5).astype(np.uint8) * 255
            _src_u8_pp = (np.clip(_source_rgb, 0, 1) * 255).astype(np.uint8)
            _expand = int(settings.get("fusion_expand", 6))
            _trimap_pp = _build_trimap(_sam_bin_pp, alpha, dilate_pct=_expand / 100.0)
            alpha = _solve_matte(_src_u8_pp, _trimap_pp, alpha, solver='hybrid', sam_binary=_sam_bin_pp)
            # Shirt/harness rescue (Berto 2026-06-14): preview parity with the batch render
            # fusion path — fill dark webbing/clothing CK under-keys so the harness strap
            # shows in PREVIEW too, not just the final render. _sf_pp is the soft SAM resized.
            alpha = apply_shirt_rescue(alpha, _sf_pp, _source_rgb, settings)
            alpha = apply_choke(alpha, settings)
            alpha = apply_despeckle(alpha, settings)
            fg_rgb = apply_despill(fg_rgb, settings)
            log.info('fusion_v2 postproc: hybrid solve done')
        else:
            fg_rgb, alpha = apply_matte_postproc(
                fg_rgb, alpha, settings, sam_soft=sam_soft, source_rgb=_source_rgb,
                screen_type=settings.get("screenType", "green"))
        # Zone cut applies to both fusion_v2 and default paths (Berto's hand tool).
        if settings.get('zone') and sam_soft is not None:
            _h_z, _w_z = alpha.shape[:2]
            _sz = np.asarray(sam_soft, dtype=np.float32)
            if _sz.ndim == 3:
                _sz = _sz[..., 0]
            if _sz.shape[:2] != (_h_z, _w_z):
                _sz = cv2.resize(_sz, (_w_z, _h_z), interpolation=cv2.INTER_LINEAR)
            _zone_mask = _zone_cut_from_sam(
                (_sz > 0.5).astype(np.uint8), settings, _w_z, _h_z, _w_z / 1920.0)
            alpha = np.clip(alpha * _zone_mask, 0.0, 1.0)

    # Build background buffer
    h, w = fg_rgb.shape[:2]
    if background == "black":
        bg_rgb = np.zeros((h, w, 3), dtype=np.float32)
    elif background == "white":
        bg_rgb = np.ones((h, w, 3), dtype=np.float32)
    elif background == "checker":
        bg_rgb = cu.create_checkerboard(w, h, checker_size=64)
    elif background == "v1-below":
        if not v1_path or not Path(v1_path).exists():
            log.warning("v1-below requested but no --v1-path given or file missing — falling back to checker")
            bg_rgb = cu.create_checkerboard(w, h, checker_size=64)
        else:
            v1_img = cv2.imread(str(v1_path), cv2.IMREAD_UNCHANGED)
            if v1_img is None:
                log.warning(f"Cannot read v1_path {v1_path} — falling back to checker")
                bg_rgb = cu.create_checkerboard(w, h, checker_size=64)
            else:
                v1_rgb = cv2.cvtColor(v1_img, cv2.COLOR_BGR2RGB).astype(np.float32)
                v1_rgb /= (65535.0 if v1_img.dtype == np.uint16 else 255.0)
                if v1_rgb.shape[:2] != (h, w):
                    v1_rgb = cv2.resize(v1_rgb, (w, h))
                bg_rgb = v1_rgb
    elif background == "matte":
        # MATTE view (Berto 2026-06-06): show the final post-proc'd alpha itself as a
        # grayscale image — white subject, black background, gray = semi-transparent.
        # Same matte the render will use (post SAM merge/choke/despeckle), so the
        # operator judges dots/holes BEFORE burning a render.
        m8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
        m8 = _maybe_downscale(m8, max_width)
        _matte_out = Path(output_path)
        _matte_out.parent.mkdir(parents=True, exist_ok=True)
        _atomic_imwrite(_matte_out, cv2.merge([m8, m8, m8]))
        log.info(f"Postproc (matte view) saved: {_matte_out}")
        return True
    elif background == "none":
        bg_rgb = None
    else:
        log.warning(f"Unknown background '{background}' — falling back to checker")
        bg_rgb = cu.create_checkerboard(w, h, checker_size=64)

    # Composite or write raw RGBA
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Expand alpha to (H,W,1) so it broadcasts cleanly against (H,W,3) RGB buffers.
    alpha_3d = alpha[..., np.newaxis] if alpha.ndim == 2 else alpha
    if bg_rgb is None:
        # Write straight RGBA. cv2.imwrite writes channels as stored, so convert the
        # RGB foreground to BGR first, then append alpha -> a correct BGRA buffer.
        fg_bgr_u8 = cv2.cvtColor((np.clip(fg_rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        bgra = np.dstack([fg_bgr_u8, (np.clip(alpha, 0, 1) * 255).astype(np.uint8)])
        bgra = _maybe_downscale(bgra, max_width)
        _atomic_imwrite(out_path, bgra)   # atomic: ckScrubShowFrame checks existsSync before read
    else:
        # composite_straight signature: (fg, bg, alpha) — NOT (fg, alpha, bg). Bug fix.
        comp = cu.composite_straight(fg_rgb, bg_rgb, alpha_3d)
        comp_u8 = (np.clip(comp, 0, 1) * 255).astype(np.uint8)
        comp_bgr = cv2.cvtColor(comp_u8, cv2.COLOR_RGB2BGR)
        comp_bgr = _maybe_downscale(comp_bgr, max_width)
        _atomic_imwrite(out_path, comp_bgr)  # atomic: ckScrubShowFrame checks existsSync before read

    log.info(f"Postproc -> {out_path} (bg={background})")
    return True


def cmd_sam_apply(session_dir, settings):
    """Run the SAM2 IMAGE predictor ONCE on a cached frame, from the panel's click
    points, and write a soft uint16 gate (sam2_gate_raw.png) into the session dir.

    This runs as its OWN short-lived process (invoked via runPython), NOT on any UI
    thread — which is the whole point: the old Qt viewer ran SAM2 synchronously on its
    GUI thread and froze ("hourglass of death"). Here a hang/crash just exits non-zero
    and the panel recovers. Engine is proven ~0.3s standalone.

    Mirrors the proven DaVinci _apply_sam_mask: SAM source = the NN foreground (fg.png),
    letterbox-pad to square, predict(return_logits=True), saturation-ramp the high-res
    logits, unpad, save uint16. cmd_postproc then garbage-merges the gate.

    Points come from settings: sam_positive / sam_negative = [[x,y],...] in FULL-RES
    source pixels (label 1 = include, 0 = exclude)."""
    import numpy as np
    import cv2
    import os
    sess = Path(session_dir)
    fg_path = sess / "fg.png"
    if not fg_path.exists():
        log.error(f"sam-apply: no fg.png in {sess} — run PREVIEW (cache) first")
        return False
    pos = settings.get("sam_positive", []) or []
    neg = settings.get("sam_negative", []) or []
    if not pos and not neg:
        log.error("sam-apply: no SAM points given")
        return False

    fg_raw = cv2.imread(str(fg_path), cv2.IMREAD_UNCHANGED)
    if fg_raw is None:
        log.error("sam-apply: cannot read fg.png")
        return False
    if fg_raw.ndim == 3 and fg_raw.shape[2] == 4:
        fg_raw = cv2.cvtColor(fg_raw, cv2.COLOR_BGRA2BGR)
    if fg_raw.dtype == np.uint16:
        fg8 = np.clip(fg_raw.astype(np.float32) / 257.0, 0, 255).astype(np.uint8)
    else:
        fg8 = fg_raw.astype(np.uint8)
    frame_rgb = cv2.cvtColor(fg8, cv2.COLOR_BGR2RGB)
    ih, iw = frame_rgb.shape[:2]

    try:
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        from corridorkey_sam_merge import (
            pad_to_square, unpad_from_square,
            shift_points_for_padding, logits_to_soft_mask,
        )
    except Exception as e:
        log.error(f"sam-apply: SAM2 import failed: {e}")
        return False

    pos_pts = [[int(p[0]), int(p[1])] for p in pos]
    neg_pts = [[int(p[0]), int(p[1])] for p in neg]
    all_pts = pos_pts + neg_pts
    labels = [1] * len(pos_pts) + [0] * len(neg_pts)
    padded, pad_box = pad_to_square(frame_rgb)
    adj = shift_points_for_padding(all_pts, pad_box)

    ckpt = str(CK_ROOT / "sam2_weights" / "sam2.1_hiera_small.pt")
    cfg = "configs/sam2.1/sam2.1_hiera_s.yaml"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = None
    pred = None
    try:
        model = build_sam2(cfg, ckpt, device=device)
        pred = SAM2ImagePredictor(model)
        pred.set_image(padded)
        # return_logits=True → masks_hi is HIGH-RES float logits (not binarised); the
        # saturation ramp turns it into a soft 0..1 gate with a 2-4px feather. Using the
        # low-res 256² return instead is what caused the wavy/banded edge — don't.
        masks_hi, scores, _low = pred.predict(
            point_coords=np.array(adj),
            point_labels=np.array(labels),
            multimask_output=True,
            return_logits=True,
        )
        best_idx = int(np.argmax(scores))
        soft_padded = logits_to_soft_mask(masks_hi[best_idx])
        soft = unpad_from_square(soft_padded, pad_box)
        if soft.shape[:2] != (ih, iw):
            soft = cv2.resize(soft, (iw, ih), interpolation=cv2.INTER_LINEAR)
        gate_u16 = (np.clip(soft, 0.0, 1.0) * 65535.0).astype(np.uint16)
        gate_path = sess / "sam2_gate_raw.png"
        gate_tmp = sess / "sam2_gate_raw.tmp.png"
        cv2.imwrite(str(gate_tmp), gate_u16)
        os.replace(str(gate_tmp), str(gate_path))
        log.info(f"sam-apply: wrote {gate_path} (score {float(scores[best_idx]):.3f}, "
                 f"{len(pos_pts)}+ / {len(neg_pts)}- pts)")
        return True
    except Exception as e:
        log.error(f"sam-apply: SAM2 inference failed: {e}")
        log.error(traceback.format_exc())
        return False
    finally:
        try:
            del pred, model
        except Exception:
            pass
        try:
            if device == "cuda":
                import torch as _t
                _t.cuda.empty_cache()
        except Exception:
            pass


# ── Arg parsing ───────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(description="CorridorKey AE/Premiere processor")
    sub = p.add_subparsers(dest="mode")

    ex = sub.add_parser("extract", help="Extract one frame from a video to PNG")
    ex.add_argument("source")
    ex.add_argument("output")
    # Either --frame (frame index) OR --time (seconds into source). --time is preferred
    # for Premiere since it seeks via CAP_PROP_POS_MSEC which is robust against source
    # fps mismatches and long-GOP codecs.
    ex.add_argument("--frame", type=int)
    ex.add_argument("--time", dest="time_sec", type=float)

    sg = sub.add_parser("single", help="Key a single PNG")
    sg.add_argument("input")
    sg.add_argument("output")
    sg.add_argument("--params", help="JSON file with settings (preferred)")
    sg.add_argument("--screen", choices=["green", "blue"])
    sg.add_argument("--despill", type=float)
    sg.add_argument("--despeckle", type=int)
    sg.add_argument("--despeckle-size", dest="despeckle_size", type=int)
    sg.add_argument("--refiner", type=float)
    sg.add_argument("--sam2-mask", dest="sam2_mask", default=None,
                    help="Path to sam2_mask.png written by the viewer after Apply Mask")

    bt = sub.add_parser("batch", help="Key a frame range from a video")
    bt.add_argument("source")
    bt.add_argument("output_folder")
    bt.add_argument("--params", help="JSON file with settings + range (preferred)")
    bt.add_argument("--start-frame", dest="start_frame", type=int)
    bt.add_argument("--end-frame", dest="end_frame", type=int)
    # Seconds range — preferred for Premiere. Python converts to frames using the
    # SOURCE video's own fps (read via cv2.CAP_PROP_FPS), avoiding drift when the
    # sequence fps differs from the source clip fps.
    bt.add_argument("--start-seconds", dest="start_seconds", type=float)
    bt.add_argument("--end-seconds", dest="end_seconds", type=float)
    bt.add_argument("--fps", type=float)
    bt.add_argument("--screen", choices=["green", "blue"])
    bt.add_argument("--despill", type=float)
    bt.add_argument("--despeckle", type=int)
    bt.add_argument("--despeckle-size", dest="despeckle_size", type=int)
    bt.add_argument("--refiner", type=float)

    # batch-scrub: feed viewer scrub mode — writes scrub/NNN/ + scrub_index.json
    bs = sub.add_parser("batch-scrub", help="Key N frames for scrub mode (writes scrub/NNN/ layout)")
    bs.add_argument("source")
    bs.add_argument("scrub_folder", help="Path to scrub/ subdirectory (NNN/ subdirs created here)")
    bs.add_argument("--params", help="JSON file with settings + count + startFrame")
    bs.add_argument("--start-frame", dest="start_frame", type=int)
    bs.add_argument("--count", type=int, help="Number of frames to key (default 10)")
    bs.add_argument("--screen", choices=["green", "blue"])
    bs.add_argument("--despill", type=float)
    bs.add_argument("--despeckle", type=int)
    bs.add_argument("--despeckle-size", dest="despeckle_size", type=int)
    bs.add_argument("--refiner", type=float)

    # cache: stage-1 only — feeds the live preview viewer
    cc = sub.add_parser("cache", help="Run NN only, write raw fg.png + alpha.png for live preview")
    cc.add_argument("input", help="Input PNG (already extracted frame)")
    cc.add_argument("session_dir", help="Session directory to write fg.png + alpha.png + meta.json")
    cc.add_argument("--params", help="JSON file with settings (screenType, refiner)")
    cc.add_argument("--screen", choices=["green", "blue"])
    cc.add_argument("--refiner", type=float)

    # postproc: stage-2 only — reads cached fg/alpha, applies despill+despeckle+composite
    pp = sub.add_parser("postproc", help="Apply despill/despeckle + background composite to a cached session")
    pp.add_argument("session_dir", help="Session directory (must contain fg.png + alpha.png)")
    pp.add_argument("output", help="Output PNG path")
    pp.add_argument("--params", help="JSON file with settings (despill, despeckle, despeckleSize)")
    pp.add_argument("--despill", type=float)
    pp.add_argument("--despeckle", type=int)
    pp.add_argument("--despeckle-size", dest="despeckle_size", type=int)
    pp.add_argument("--background", default="checker",
                    choices=["none", "black", "white", "checker", "v1-below", "matte", "matte-ck", "matte-sam"])
    pp.add_argument("--v1-path", dest="v1_path", help="PNG of V1 frame (required for --background v1-below)")
    pp.add_argument("--max-width", dest="max_width", type=int, help="Downscale output to this max width (inline panel preview)")

    # sam-apply: single-frame SAM2 image predictor from click points -> writes
    # sam2_gate_raw.png into the session. Separate process = never freezes the panel.
    sa = sub.add_parser("sam-apply", help="Run SAM2 on a cached frame from click points; write the gate")
    sa.add_argument("session_dir", help="Session dir (must contain fg.png)")
    sa.add_argument("--params", help="JSON with sam_positive / sam_negative (full-res pixel coords)")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.mode == "extract":
            if args.time_sec is None and args.frame is None:
                log.error("extract requires --time SEC or --frame N")
                sys.exit(2)
            ok = cmd_extract(args.source, args.output,
                             frame_idx=args.frame, time_sec=args.time_sec)
            sys.exit(0 if ok else 1)

        if args.mode == "single":
            settings = load_settings(args.params, args)
            ok = cmd_single(args.input, args.output, settings,
                            sam2_mask_path=getattr(args, "sam2_mask", None))
            sys.exit(0 if ok else 1)

        if args.mode == "batch":
            settings = load_settings(args.params, args)
            start_sec = args.start_seconds if args.start_seconds is not None else settings.get("startSeconds")
            end_sec   = args.end_seconds   if args.end_seconds   is not None else settings.get("endSeconds")
            start_frame = args.start_frame if args.start_frame is not None else settings.get("startFrame")
            end_frame   = args.end_frame   if args.end_frame   is not None else settings.get("endFrame")
            fps         = args.fps         if args.fps         is not None else settings.get("fps", 30.0)
            if start_sec is None and (start_frame is None or end_frame is None):
                log.error("batch requires --start-seconds/--end-seconds OR --start-frame/--end-frame (or equivalents in --params JSON)")
                sys.exit(2)
            n = cmd_batch(args.source, args.output_folder, settings,
                          start_frame=start_frame, end_frame=end_frame, fps=fps,
                          start_seconds=start_sec, end_seconds=end_sec)
            sys.exit(0 if n > 0 else 1)

        if args.mode == "batch-scrub":
            settings = load_settings(args.params, args)
            start_frame = args.start_frame if args.start_frame is not None else settings.get("startFrame")
            count = args.count if args.count is not None else settings.get("count", 10)
            if start_frame is None:
                log.error("batch-scrub requires --start-frame N (or startFrame in --params JSON)")
                sys.exit(2)
            n = cmd_batch_scrub(args.source, args.scrub_folder, settings,
                                start_frame=start_frame, count=count)
            sys.exit(0 if n > 0 else 1)

        if args.mode == "cache":
            settings = load_settings(args.params, args)
            ok = cmd_cache(args.input, args.session_dir, settings)
            sys.exit(0 if ok else 1)

        if args.mode == "postproc":
            settings = load_settings(args.params, args)
            ok = cmd_postproc(args.session_dir, args.output, settings,
                              background=args.background, v1_path=args.v1_path,
                              max_width=getattr(args, "max_width", None))
            sys.exit(0 if ok else 1)

        if args.mode == "sam-apply":
            settings = load_settings(args.params, args)
            ok = cmd_sam_apply(args.session_dir, settings)
            sys.exit(0 if ok else 1)

        parser.print_help()
        sys.exit(2)

    except Exception as e:
        log.error(f"Fatal: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
