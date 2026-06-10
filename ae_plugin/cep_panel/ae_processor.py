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
def sam_garbage_merge(alpha, sam_soft, source_rgb, settings, screen_type="green"):
    """CK x SAM garbage-matte merge via the shared engine (DaVinci-identical).
    sam_soft: soft 0..1 SAM silhouette, or None. Returns the merged 2D alpha."""
    import numpy as np, cv2
    if sam_soft is None or bool(settings.get("sam2_bypass", False)):
        return alpha
    try:
        from corridorkey_sam_merge import binarize_sam_silhouette, merge_ck_with_sam_active
    except Exception as e:
        log.warning(f"SAM merge unavailable, using CK alpha: {e}")
        return alpha
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
        _buffer_px = settings.get("sam2_margin", None)   # panel fader key (ckGetSettings)
        if _buffer_px is None:
            _buffer_px = settings.get("edge_guard_px", 7)
        return merge_ck_with_sam_active(
            alpha, binarize_sam_silhouette(sg), source_rgb=source_rgb,
            screen_type=screen_type, proximity_px=int(_buffer_px),
            carve_points=settings.get("sam_negative") or None)
    except Exception as e:
        log.warning(f"SAM merge failed, using CK alpha: {e}")
        return alpha


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
# WHAT IT DOES: writes the four named CK sidecar PNGs for one frame, each into its
#   OWN clean subfolder so the host imports each as an isolated numbered-stills
#   sequence:  CK_RGB/, CK_COMBINED/, CK_ALPHA/, SAM_ALPHA/.
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
def _write_fusion_sidecars(fg, ck_alpha, ck_combined, sam_union,
                           settings, out_dir, seq_num, is_first,
                           fg_despill_done=False):
    """Write CK_RGB / CK_COMBINED / CK_ALPHA / SAM_ALPHA sidecars for one frame.

    fg              : float32 RGB 0..1 (NN foreground)
    ck_alpha        : float32 mono 0..1 (RAW CK neural-net alpha)
    ck_combined     : float32 mono 0..1 (CK x SAM merged matte) or None
    sam_union       : float32 mono 0..1 (SAM2 soft silhouette) or None
    fg_despill_done : True when the caller already despilled fg (cmd_batch despills
                      inline so CK_RGB matches the main export) — skips re-despill.
    All written as 16-bit PNG. Mattes are written 3-channel (Fusion / numbered-stills
    importers treat single-channel PNGs as masks with no RGB data).
    """
    import cv2, numpy as np

    def _to_3ch_16(mono):
        m16 = (np.clip(mono, 0.0, 1.0) * 65535.0).astype(np.uint16)
        return cv2.merge([m16, m16, m16])

    def _write(folder_name, file_stem, img16):
        sub = out_dir / folder_name
        sub.mkdir(parents=True, exist_ok=True)
        _atomic_imwrite(sub / f"{file_stem}_{seq_num:05d}.png", img16)
        # Dummy 00000 — host's numbered-stills importer silently drops the first
        # frame, so seed each folder with a copy on the first written frame.
        if is_first:
            _atomic_imwrite(sub / f"{file_stem}_00000.png", img16)

    try:
        # 1. CK_RGB — NN foreground with despill (same as the main Track-2 export).
        if fg is not None:
            fg_clean = np.clip(fg, 0.0, 1.0).copy()
            _despill_str = float(settings.get("despill", 0.5))
            if _despill_str > 0 and not fg_despill_done:
                try:
                    from CorridorKeyModule.core import color_utils as _cu_sc
                    fg_clean = _cu_sc.despill_opencv(
                        fg_clean, green_limit_mode="average", strength=_despill_str)
                except Exception:
                    pass
            # Re-clip AFTER despill: despill adds spill*0.5 back to R/B and can push
            # a bright spill pixel past 1.0 — the uint16 cast would wrap it into a
            # bright speck (main export path clips post-despill; this path must too).
            fg_16 = (np.clip(fg_clean, 0.0, 1.0) * 65535.0).astype(np.uint16)
            fg_16 = cv2.cvtColor(fg_16, cv2.COLOR_RGB2BGR)
            _write("CK_RGB", "CK_RGB", fg_16)

        # 2. CK_COMBINED — CK x SAM merged matte (the "clean key" the viewer signed off).
        if ck_combined is not None:
            m = ck_combined[:, :, 0] if ck_combined.ndim == 3 else ck_combined
            _write("CK_COMBINED", "CK_COMBINED", _to_3ch_16(m))

        # 3. CK_ALPHA — RAW CK neural-net alpha (un-merged, un-choked).
        if ck_alpha is not None:
            m = ck_alpha[:, :, 0] if ck_alpha.ndim == 3 else ck_alpha
            _write("CK_ALPHA", "CK_ALPHA", _to_3ch_16(m))

        # 4. SAM_ALPHA — SAM2 body silhouette.
        if sam_union is not None:
            s = sam_union[:, :, 0] if sam_union.ndim == 3 else sam_union
            _write("SAM_ALPHA", "SAM_ALPHA", _to_3ch_16(s))
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
    sam_margin   = float(settings.get("sam2_margin", 0))
    sam_soften   = float(settings.get("sam2_soften", 0))
    sam_fill     = int(settings.get("fill_holes", 0))

    sam_active = (len(sam_pos) + len(sam_neg)) > 0
    sam_video_masks = {}  # {seq_num: float32 mask 0..1}
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
                    if _src_h is None:
                        _src_h, _src_w = _fr.shape[:2]
                        _, _pad_box = _pad_to_square(_fr)
                        log.info(f"SAM2 video: source {_src_w}x{_src_h} -> "
                                 f"padded square {max(_src_h, _src_w)} "
                                 f"(pad_box={_pad_box})")
                    _padded, _ = _pad_to_square(_fr)
                    cv2.imwrite(str(sam_tmp_dir / f"{_i:06d}.png"), _padded,
                                [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    _exported += 1
                _exp_cap.release()
                log.info(f"SAM2 video: {_exported} frames exported")

                _video_predictor = build_sam2_video_predictor(cfg, ckpt, device=device)
                _all_pts = list(sam_pos) + list(sam_neg)
                _labels  = [1] * len(sam_pos) + [0] * len(sam_neg)
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
                    # Forward: anchor → last frame. Logits are at padded square
                    # shape — apply ramp, then unpad to source frame shape.
                    for _fi, _obj_ids, _mask_logits in _video_predictor.propagate_in_video(_state):
                        _L = _mask_logits[0].squeeze().cpu().numpy()
                        _m_padded = _ramp(_L)
                        sam_video_masks[_fi] = (
                            _unpad_from_square(_m_padded, _pad_box)
                            if _pad_box is not None else _m_padded
                        )
                    # Backward: anchor → frame 0. Forward wins on overlap.
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
                    _first_sub = next(
                        (f for f in _sorted_keys if sam_video_masks[f].sum() >= 100), None)
                    _last_sub = next(
                        (f for f in reversed(_sorted_keys) if sam_video_masks[f].sum() >= 100), None)
                    _collapsed = 0
                    _held = 0
                    if _first_sub is not None and _last_sub is not None:
                        for _f in _sorted_keys:
                            if sam_video_masks[_f].sum() >= 100:
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
                        if _ref_cov >= 100:
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

    # Sidecar dir is created up front so the dummy-first-frame write never
    # races a missing parent. Empty dir is cheap; gets removed by the caller
    # if no SAM points were active and no files were ever written.
    sam_dir = out_dir / "sam_mattes" if sam_active else None
    if sam_dir is not None:
        sam_dir.mkdir(parents=True, exist_ok=True)
        # process_sam_matte lives in the engine root; it is on sys.path already.
        # Option C — feed soft mask straight in; no binarise step.
        from corridorkey_sam_merge import process_sam_matte

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
                # Shared post-proc — identical helpers to KEY FRAME + inline PREVIEW so
                # render == preview. SAM garbage-matte merge first (source_rgb=img_rgb
                # drives the hair chroma-escape valve — same call as the Resolve plugin,
                # CorridorKey_Pro.py:5447). Snapshot the merged matte for the CK_COMBINED
                # sidecar BEFORE choke, then choke + despeckle the alpha, despill the fg.
                alpha = sam_garbage_merge(
                    alpha_raw, sam_video_masks.get(seq_num), img_rgb, settings,
                    settings["screenType"])
                # Shirt rescue in the RENDER path too (2026-06-06 review: it was wired
                # into apply_matte_postproc only, which cmd_batch does not use —
                # preview had rescue, renders didn't). Resize the SAM mask to alpha
                # dims first or the rescue's shape guard silently no-ops on 4K.
                _sam_for_rescue = sam_video_masks.get(seq_num)
                if _sam_for_rescue is not None and _sam_for_rescue.shape[:2] != alpha.shape[:2]:
                    _sam_for_rescue = cv2.resize(
                        _sam_for_rescue, (alpha.shape[1], alpha.shape[0]),
                        interpolation=cv2.INTER_LINEAR)
                alpha = apply_shirt_rescue(alpha, _sam_for_rescue, img_rgb, settings)
                alpha_combined = alpha.copy()
                alpha = apply_choke(alpha, settings)
                alpha = apply_despeckle(alpha, settings)
                fg = apply_despill(fg, settings)
                fg_uint16 = (np.clip(fg, 0, 1) * 65535).astype(np.uint16)
                alpha_uint16 = (np.clip(alpha_raw, 0, 1) * 65535).astype(np.uint16)
                alpha_uint8  = (np.clip(alpha_raw, 0, 1) * 255).astype(np.uint8)
                fg_bgr = cv2.cvtColor(fg_uint16, cv2.COLOR_RGB2BGR)
                out_bgra = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint16])
                _atomic_imwrite(out_dir / f"output_{seq_num:05d}.png", out_bgra)
                # Named sidecar passes (Editable Layers / Fusion-comp parity) — write
                # CK_RGB/, CK_COMBINED/, CK_ALPHA/, SAM_ALPHA/ as separate 16-bit
                # numbered-stills sequences. fg is already despilled here so we tell the
                # writer not to re-despill. SAM silhouette is the per-frame soft mask
                # (resized to alpha shape) when present. GUARDED inside _write_fusion_
                # sidecars — a sidecar fault surfaces as CK_WARN: but never crashes the
                # main key, matching the loud-fallback pattern below.
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
                    fg, alpha_raw, alpha_combined, _sam_for_sidecar,
                    settings, out_dir, seq_num, is_first=(processed == 0),
                    fg_despill_done=True,
                )
                # Matte goes into a SUBFOLDER so the main out_dir contains exactly one
                # PNG pattern. Premiere's importAsNumberedStills auto-detects the range
                # reliably only when the folder is clean.
                matte_dir = out_dir / "mattes"
                matte_dir.mkdir(parents=True, exist_ok=True)
                _atomic_imwrite(matte_dir / f"matte_{seq_num:05d}.png", alpha_uint8)
                # v1.0 SAM matte sidecar — one file per frame in sam_mattes/.
                # uint8 PNG matches the CK matte's format so the host imports
                # both as identical numbered-stills sequences.
                if sam_dir is not None and seq_num in sam_video_masks:
                    try:
                        _gate_soft = sam_video_masks[seq_num]
                        if _gate_soft.shape[:2] != alpha.shape[:2]:
                            _gate_soft = cv2.resize(
                                _gate_soft,
                                (alpha.shape[1], alpha.shape[0]),
                                interpolation=cv2.INTER_LINEAR,
                            )
                        # Option C — soft mask flows straight through; no
                        # binarise step before process_sam_matte.
                        _sam_processed = process_sam_matte(
                            _gate_soft,
                            margin_px=sam_margin,
                            softness_sigma=sam_soften,
                            fill_kernel_px=sam_fill,
                        )
                        _sam_erode_px = int(settings.get("sam_erode_px", 0))
                        if _sam_erode_px > 0:
                            _ek = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_sam_erode_px * 2 + 1, _sam_erode_px * 2 + 1))
                            _sam_processed = cv2.erode((np.clip(_sam_processed, 0, 1) * 255).astype(np.uint8), _ek, iterations=1).astype(np.float32) / 255.0
                        _sam_expand_px = int(settings.get("sam_expand_px", 0))
                        if _sam_expand_px > 0:
                            _ek2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_sam_expand_px * 2 + 1, _sam_expand_px * 2 + 1))
                            _sam_processed = cv2.dilate((_sam_processed * 255).astype(np.uint8), _ek2, iterations=1).astype(np.float32) / 255.0
                        _sam_u8 = (np.clip(_sam_processed, 0, 1) * 255).astype(np.uint8)
                        _sam_rgba = np.zeros((_sam_u8.shape[0], _sam_u8.shape[1], 4), dtype=np.uint8)
                        _sam_rgba[:, :, 0] = _sam_u8  # B = mask
                        _sam_rgba[:, :, 1] = _sam_u8  # G
                        _sam_rgba[:, :, 2] = _sam_u8  # R
                        _sam_rgba[:, :, 3] = _sam_u8  # Alpha = mask
                        _atomic_imwrite(sam_dir / f"sam_{seq_num:05d}.png", _sam_rgba)
                        if processed == 0:
                            _atomic_imwrite(sam_dir / "sam_00000.png", _sam_rgba)
                        # ck_masked sidecar — despilled fg RGBA, alpha = alpha_raw * SAM
                        try:
                            ck_masked_dir = out_dir / "ck_masked"
                            ck_masked_dir.mkdir(parents=True, exist_ok=True)
                            _masked_alpha = np.clip(alpha_raw * _sam_processed, 0, 1)
                            _cm_fg16 = (np.clip(fg, 0, 1) * 65535).astype(np.uint16)
                            _cm_bgr = cv2.cvtColor(_cm_fg16, cv2.COLOR_RGB2BGR)
                            _cm_a16 = (np.clip(_masked_alpha, 0, 1) * 65535).astype(np.uint16)
                            _cm_rgba = cv2.merge([_cm_bgr[:, :, 0], _cm_bgr[:, :, 1], _cm_bgr[:, :, 2], _cm_a16])
                            _atomic_imwrite(ck_masked_dir / f"ck_masked_{seq_num:05d}.png", _cm_rgba)
                            if processed == 0:
                                _atomic_imwrite(ck_masked_dir / "ck_masked_00000.png", _cm_rgba)
                        except Exception as _cm_e:
                            log.warning(f"ck_masked frame {frame_idx}: write failed: {_cm_e}")
                    except Exception as _se:
                        log.warning(f"SAM frame {frame_idx}: sidecar save failed: {_se}")
                # Premiere Pro's sequence importer silently drops the first frame. Write
                # a dummy output_00000.png (and matching matte) so the user's actual
                # frame range survives the import intact.
                # NOTE: output_00000 is the REAL first frame (seq_num starts at 0), not a
                # separate leader — this re-write is a harmless no-op duplicate of the
                # seq-0 write above. Kept for the Premiere drop-first-frame comment above;
                # FIX C's clean POS_FRAMES decode means frame 0 itself is no longer dirty.
                if processed == 0:
                    _atomic_imwrite(out_dir / "output_00000.png", out_bgra)
                    _atomic_imwrite(matte_dir / "matte_00000.png", alpha_uint8)
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
                    if _src_h is None:
                        _src_h, _src_w = _fr.shape[:2]
                        _, _pad_box = _pad_to_square(_fr)
                        log.info(f"SAM2 video: source {_src_w}x{_src_h} -> "
                                 f"padded square {max(_src_h, _src_w)} "
                                 f"(pad_box={_pad_box})")
                    _padded, _ = _pad_to_square(_fr)
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
    fg_rgb, alpha = apply_matte_postproc(
        fg_rgb, alpha, settings, sam_soft=sam_soft, source_rgb=_source_rgb,
        screen_type=settings.get("screenType", "green"))

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
