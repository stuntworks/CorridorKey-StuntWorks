#!/usr/bin/env python
# Last modified: 2026-04-14 | Change: Add --params JSON mode + extract subcommand,
#   structured PROGRESS output, stderr logging to %TEMP%\corridorkey.log
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
    return settings


# ── Chroma hint ───────────────────────────────────────────────
def generate_chroma_hint(image, screen_type="green"):
    import numpy as np
    import cv2
    if screen_type == "green":
        green = image[:, :, 1]; red = image[:, :, 0]; blue = image[:, :, 2]
        screen_mask = (green > 0.3) & (green > red * 1.2) & (green > blue * 1.2)
    else:
        blue = image[:, :, 2]; red = image[:, :, 0]; green = image[:, :, 1]
        screen_mask = (blue > 0.3) & (blue > red * 1.2) & (blue > green * 1.2)
    alpha_hint = (~screen_mask).astype(np.float32)
    alpha_hint = cv2.GaussianBlur(alpha_hint, (5, 5), 0)
    return alpha_hint


# ── Subcommand: extract ───────────────────────────────────────
def cmd_extract(source_video, output_png, frame_idx=None, time_sec=None):
    """Pull one frame from a video. Prefers time-based seek (CAP_PROP_POS_MSEC) when a
    time is given — avoids drift on variable-fps or long-GOP sources where
    CAP_PROP_POS_FRAMES is unreliable. Falls back to frame-index seek for AE."""
    import cv2
    src = Path(source_video)
    if not src.exists():
        log.error(f"Source video not found: {src}")
        return False
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        log.error(f"Could not open video: {src}")
        return False
    try:
        if time_sec is not None:
            cap.set(cv2.CAP_PROP_POS_MSEC, float(time_sec) * 1000.0)
            log.info(f"Seek by time: {time_sec:.4f}s")
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
            log.info(f"Seek by frame: {frame_idx}")
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
    processor = CorridorKeyProcessor(device="cuda")
    try:
        ps = ProcessingSettings(
            screen_type=settings["screenType"],
            despill_strength=settings["despill"],
            despeckle_enabled=settings["despeckle"],
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
        # Apply SAM2 garbage matte gate to output alpha (post-keyer)
        h, w = alpha.shape[:2]
        gate = load_sam2_gate(sam2_mask_path, h, w)
        if gate is not None:
            before_mean = float(alpha.mean())
            alpha = alpha * gate
            log.info(f"SAM2 gate applied — alpha mean {before_mean:.3f} -> {float(alpha.mean()):.3f}")
        fg_uint8 = (np.clip(fg, 0, 1) * 255).astype(np.uint8)
        alpha_uint8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
        fg_bgr = cv2.cvtColor(fg_uint8, cv2.COLOR_RGB2BGR)
        out_bgra = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint8])
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), out_bgra)
        matte_path = out_path.with_name(out_path.stem + "_matte.png")
        cv2.imwrite(str(matte_path), alpha_uint8)
        log.info(f"Saved: {out_path}")
        return True
    finally:
        processor.cleanup()


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
    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        log.error(f"Cannot open: {source_video}")
        return 0

    from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
    processor = CorridorKeyProcessor(device="cuda")
    ps = ProcessingSettings(
        screen_type=settings["screenType"],
        despill_strength=settings["despill"],
        despeckle_enabled=settings["despeckle"],
        despeckle_size=settings["despeckleSize"],
        refiner_strength=settings["refiner"],
    )

    processed = 0
    failed = []
    total = max(1, end_frame - start_frame)

    try:
        # Seek once, then sequential read — 10-100x faster than re-seeking per frame
        # on long-GOP codecs like H.264 / HEVC / ProRes.
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(start_frame))
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
                fg_uint8 = (np.clip(fg, 0, 1) * 255).astype(np.uint8)
                alpha_uint8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
                fg_bgr = cv2.cvtColor(fg_uint8, cv2.COLOR_RGB2BGR)
                out_bgra = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint8])
                cv2.imwrite(str(out_dir / f"output_{seq_num:05d}.png"), out_bgra)
                # Matte goes into a SUBFOLDER so the main out_dir contains exactly one
                # PNG pattern. Premiere's importAsNumberedStills auto-detects the range
                # reliably only when the folder is clean.
                matte_dir = out_dir / "mattes"
                matte_dir.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(matte_dir / f"matte_{seq_num:05d}.png"), alpha_uint8)
                # Premiere Pro's sequence importer silently drops the first frame. Write
                # a dummy output_00000.png (and matching matte) so the user's actual
                # frame range survives the import intact.
                if processed == 0:
                    cv2.imwrite(str(out_dir / "output_00000.png"), out_bgra)
                    cv2.imwrite(str(matte_dir / "matte_00000.png"), alpha_uint8)
                processed += 1
            except Exception as e:
                failed.append(frame_idx)
                log.warning(f"Frame {frame_idx}: {e}")
            print(f"PROGRESS {processed}/{total}", flush=True)
    finally:
        cap.release()
        processor.cleanup()

    (out_dir / "batch_result.txt").write_text(f"{processed},{total},{len(failed)}")
    log.info(f"Done: {processed}/{total} ({len(failed)} failed)")
    return processed


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
        # Post-proc disabled: we capture the pre-post-proc NN output so the live
        # viewer can re-apply despill/despeckle with variable settings. Refiner
        # stays at the user's value because it's NN-internal.
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

        meta = {
            "source": str(input_path),
            "width": int(img_rgb.shape[1]),
            "height": int(img_rgb.shape[0]),
            "screenType": settings["screenType"],
            "refiner": float(settings.get("refiner", 1.0)),
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
def cmd_postproc(session_dir, output_path, settings, background="checker", v1_path=None):
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

    # Reuse engine's post-proc math so our output matches what the full `single`
    # pipeline produces with the same settings — no drift between preview and commit.
    from CorridorKeyModule.core import color_utils as cu

    # Despeckle (operates on alpha)
    if settings.get("despeckle", True):
        alpha = cu.clean_matte_opencv(
            alpha,
            area_threshold=int(settings.get("despeckleSize", 400)),
            dilation=25,
            blur_size=5,
        )

    # Despill (operates on RGB foreground)
    despill_strength = float(settings.get("despill", 0.5))
    if despill_strength > 0.0:
        fg_rgb = cu.despill_opencv(fg_rgb, green_limit_mode="average", strength=despill_strength)

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
        # Write raw RGBA with alpha channel
        rgba_u8 = np.dstack([
            (np.clip(fg_rgb, 0, 1) * 255).astype(np.uint8),
            (np.clip(alpha, 0, 1) * 255).astype(np.uint8),
        ])
        bgra = cv2.cvtColor(rgba_u8, cv2.COLOR_RGBA2BGRA)
        cv2.imwrite(str(out_path), bgra)
    else:
        # composite_straight signature: (fg, bg, alpha) — NOT (fg, alpha, bg). Bug fix.
        comp = cu.composite_straight(fg_rgb, bg_rgb, alpha_3d)
        comp_u8 = (np.clip(comp, 0, 1) * 255).astype(np.uint8)
        comp_bgr = cv2.cvtColor(comp_u8, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(out_path), comp_bgr)

    log.info(f"Postproc -> {out_path} (bg={background})")
    return True


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
                    choices=["none", "black", "white", "checker", "v1-below"])
    pp.add_argument("--v1-path", dest="v1_path", help="PNG of V1 frame (required for --background v1-below)")
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

        if args.mode == "cache":
            settings = load_settings(args.params, args)
            ok = cmd_cache(args.input, args.session_dir, settings)
            sys.exit(0 if ok else 1)

        if args.mode == "postproc":
            settings = load_settings(args.params, args)
            ok = cmd_postproc(args.session_dir, args.output, settings,
                              background=args.background, v1_path=args.v1_path)
            sys.exit(0 if ok else 1)

        parser.print_help()
        sys.exit(2)

    except Exception as e:
        log.error(f"Fatal: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
