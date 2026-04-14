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
def cmd_extract(source_video, output_png, frame_idx):
    """Pull one frame from a video. Written as a real command so the panel no longer
    has to build an inline `python -c "import cv2; ..."` shell string."""
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
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
        ok, frame = cap.read()
        if not ok or frame is None:
            log.error(f"Could not read frame {frame_idx}")
            return False
        out = Path(output_png)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), frame)
        log.info(f"Extracted frame {frame_idx} -> {out}")
        return True
    finally:
        cap.release()


# ── Subcommand: single ────────────────────────────────────────
def cmd_single(input_path, output_path, settings):
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
def cmd_batch(source_video, output_folder, settings, start_frame, end_frame, fps):
    import numpy as np
    import cv2
    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

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
                cv2.imwrite(str(out_dir / f"output_{seq_num:05d}_matte.png"), alpha_uint8)
                # Premiere Pro's sequence importer silently drops the first frame. Write
                # a dummy output_00000.png (a copy of the first real keyed frame) so the
                # user's actual frame range survives the import.
                if processed == 0:
                    cv2.imwrite(str(out_dir / "output_00000.png"), out_bgra)
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


# ── Arg parsing ───────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(description="CorridorKey AE/Premiere processor")
    sub = p.add_subparsers(dest="mode")

    ex = sub.add_parser("extract", help="Extract one frame from a video to PNG")
    ex.add_argument("source")
    ex.add_argument("output")
    ex.add_argument("--frame", type=int, required=True)

    sg = sub.add_parser("single", help="Key a single PNG")
    sg.add_argument("input")
    sg.add_argument("output")
    sg.add_argument("--params", help="JSON file with settings (preferred)")
    sg.add_argument("--screen", choices=["green", "blue"])
    sg.add_argument("--despill", type=float)
    sg.add_argument("--despeckle", type=int)
    sg.add_argument("--despeckle-size", dest="despeckle_size", type=int)
    sg.add_argument("--refiner", type=float)

    bt = sub.add_parser("batch", help="Key a frame range from a video")
    bt.add_argument("source")
    bt.add_argument("output_folder")
    bt.add_argument("--params", help="JSON file with settings + range (preferred)")
    bt.add_argument("--start-frame", dest="start_frame", type=int)
    bt.add_argument("--end-frame", dest="end_frame", type=int)
    bt.add_argument("--fps", type=float)
    bt.add_argument("--screen", choices=["green", "blue"])
    bt.add_argument("--despill", type=float)
    bt.add_argument("--despeckle", type=int)
    bt.add_argument("--despeckle-size", dest="despeckle_size", type=int)
    bt.add_argument("--refiner", type=float)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.mode == "extract":
            ok = cmd_extract(args.source, args.output, args.frame)
            sys.exit(0 if ok else 1)

        if args.mode == "single":
            settings = load_settings(args.params, args)
            ok = cmd_single(args.input, args.output, settings)
            sys.exit(0 if ok else 1)

        if args.mode == "batch":
            settings = load_settings(args.params, args)
            # Range can come from params too — argv wins if both present.
            start_frame = args.start_frame if args.start_frame is not None else settings.get("startFrame")
            end_frame   = args.end_frame   if args.end_frame   is not None else settings.get("endFrame")
            fps         = args.fps         if args.fps         is not None else settings.get("fps", 30.0)
            if start_frame is None or end_frame is None:
                log.error("batch requires --start-frame + --end-frame (or start/endFrame in --params JSON)")
                sys.exit(2)
            n = cmd_batch(args.source, args.output_folder, settings, start_frame, end_frame, fps)
            sys.exit(0 if n > 0 else 1)

        parser.print_help()
        sys.exit(2)

    except Exception as e:
        log.error(f"Fatal: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
