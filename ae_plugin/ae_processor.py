#!/usr/bin/env python
"""CorridorKey After Effects Processor

Command-line interface for AE plugins to call.
Usage: python ae_processor.py <input_path> <output_path> [options]
"""
import sys
import os
import argparse
import logging
from pathlib import Path

# Find CorridorKey root: read corridorkey_path.txt if it exists (installed panel),
# otherwise walk up from script location (running from inside repo)
_config = Path(__file__).parent / "corridorkey_path.txt"
if _config.exists():
    CORRIDORKEY_ROOT = Path(_config.read_text().strip())
else:
    CORRIDORKEY_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(CORRIDORKEY_ROOT))
sys.path.insert(0, str(CORRIDORKEY_ROOT / "resolve_plugin" / "core"))

logging.basicConfig(level=logging.INFO, format='[CK-AE] %(message)s')
log = logging.getLogger(__name__)


def generate_chroma_hint(image, screen_type="green"):
    """Generate simple chroma key alpha hint."""
    import numpy as np

    if screen_type == "green":
        green = image[:, :, 1]
        red = image[:, :, 0]
        blue = image[:, :, 2]
        screen_mask = (green > 0.3) & (green > red * 1.2) & (green > blue * 1.2)
    else:
        blue = image[:, :, 2]
        red = image[:, :, 0]
        green = image[:, :, 1]
        screen_mask = (blue > 0.3) & (blue > red * 1.2) & (blue > green * 1.2)

    alpha_hint = (~screen_mask).astype(np.float32)

    import cv2
    alpha_hint = cv2.GaussianBlur(alpha_hint, (5, 5), 0)

    return alpha_hint


def process_frame(input_path, output_path, screen_type="green", despill=0.5,
                  despeckle=True, despeckle_size=400, refiner=1.0):
    """Process a single frame through CorridorKey."""
    import numpy as np
    import cv2

    log.info(f"Processing: {input_path}")

    # Read input image
    img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        log.error(f"Cannot read: {input_path}")
        return False

    # Handle alpha channel if present
    has_alpha = img.shape[2] == 4 if len(img.shape) == 3 else False

    # Convert to RGB float32
    if has_alpha:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    else:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    if img_rgb.dtype == np.uint8:
        img_rgb = img_rgb.astype(np.float32) / 255.0
    elif img_rgb.dtype == np.uint16:
        img_rgb = img_rgb.astype(np.float32) / 65535.0

    log.info(f"Size: {img_rgb.shape[1]}x{img_rgb.shape[0]}")

    # Generate alpha hint
    alpha_hint = generate_chroma_hint(img_rgb, screen_type)

    # Load and run CorridorKey
    try:
        from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings

        processor = CorridorKeyProcessor(device="cuda")
        settings = ProcessingSettings(
            screen_type=screen_type,
            despill_strength=despill,
            despeckle_enabled=despeckle,
            despeckle_size=despeckle_size,
            refiner_strength=refiner,
        )

        result = processor.process_frame(img_rgb, alpha_hint, settings)

        fg = result.get("fg")
        alpha = result.get("alpha")

        if fg is None or alpha is None:
            log.error("Processing failed")
            processor.cleanup()
            return False

        # Ensure alpha is 2D
        if len(alpha.shape) == 3:
            alpha = alpha[:, :, 0]

        # Create RGBA output
        fg_uint8 = (np.clip(fg, 0, 1) * 255).astype(np.uint8)
        alpha_uint8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)

        # Convert RGB to BGR for OpenCV
        fg_bgr = cv2.cvtColor(fg_uint8, cv2.COLOR_RGB2BGR)

        # Merge to BGRA
        output = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint8])

        # Save output
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), output)

        log.info(f"Saved: {output_path}")
        processor.cleanup()
        return True

    except Exception as e:
        log.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def process_batch(source_video, output_folder, start_frame, end_frame, fps,
                   screen_type="green", despill=0.5, despeckle=True,
                   despeckle_size=400, refiner=1.0):
    """Process a range of frames from a source video file.

    Reads directly from video via OpenCV — no AE render pipeline needed.
    One Python process handles the entire batch.
    """
    import numpy as np
    import cv2

    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    log.info(f"Batch: {source_video}")
    log.info(f"Frames {start_frame}-{end_frame} ({end_frame - start_frame} total)")

    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        log.error(f"Cannot open: {source_video}")
        return 0

    # Load CorridorKey processor once for entire batch
    try:
        from corridorkey_processor import CorridorKeyProcessor, ProcessingSettings

        processor = CorridorKeyProcessor(device="cuda")
        settings = ProcessingSettings(
            screen_type=screen_type,
            despill_strength=despill,
            despeckle_enabled=despeckle,
            despeckle_size=despeckle_size,
            refiner_strength=refiner,
        )
    except Exception as e:
        log.error(f"Failed to load CorridorKey: {e}")
        cap.release()
        return 0

    processed = 0
    failed_frames = []

    for frame_idx in range(start_frame, end_frame):
        seq_num = frame_idx - start_frame
        output_path = output_folder / f"output_{seq_num:05d}.png"

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            log.warning(f"Failed to read frame {frame_idx}")
            failed_frames.append(frame_idx)
            continue

        # Convert BGR → RGB float32
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

        # Generate alpha hint
        alpha_hint = generate_chroma_hint(img_rgb, screen_type)

        try:
            result = processor.process_frame(img_rgb, alpha_hint, settings)
            fg = result.get("fg")
            alpha = result.get("alpha")

            if fg is None or alpha is None:
                log.warning(f"Processing failed for frame {frame_idx}")
                failed_frames.append(frame_idx)
                continue

            if len(alpha.shape) == 3:
                alpha = alpha[:, :, 0]

            fg_uint8 = (np.clip(fg, 0, 1) * 255).astype(np.uint8)
            alpha_uint8 = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)
            fg_bgr = cv2.cvtColor(fg_uint8, cv2.COLOR_RGB2BGR)
            output = cv2.merge([fg_bgr[:, :, 0], fg_bgr[:, :, 1], fg_bgr[:, :, 2], alpha_uint8])

            # Also save matte
            matte_path = output_folder / f"output_{seq_num:05d}_matte.png"
            cv2.imwrite(str(output_path), output)
            cv2.imwrite(str(matte_path), alpha_uint8)

            processed += 1
            if processed % 10 == 0:
                log.info(f"Progress: {processed}/{end_frame - start_frame}")

        except Exception as e:
            log.warning(f"Frame {frame_idx} error: {e}")
            failed_frames.append(frame_idx)
            continue

    cap.release()
    processor.cleanup()

    log.info(f"Done: {processed}/{end_frame - start_frame} frames")
    if failed_frames:
        log.warning(f"Failed frames: {failed_frames}")

    # Write result summary for ExtendScript to read
    summary_path = output_folder / "batch_result.txt"
    summary_path.write_text(f"{processed},{end_frame - start_frame},{len(failed_frames)}")

    return processed


def main():
    parser = argparse.ArgumentParser(description="CorridorKey AE Processor")
    subparsers = parser.add_subparsers(dest="mode", help="Processing mode")

    # Single frame mode (default, backwards compatible)
    single = subparsers.add_parser("single", help="Process one frame")
    single.add_argument("input", help="Input image path")
    single.add_argument("output", help="Output image path (PNG with alpha)")
    single.add_argument("--screen", default="green", choices=["green", "blue"])
    single.add_argument("--despill", type=float, default=0.5)
    single.add_argument("--despeckle", type=int, default=1)
    single.add_argument("--despeckle-size", type=int, default=400)
    single.add_argument("--refiner", type=float, default=1.0)

    # Batch mode
    batch = subparsers.add_parser("batch", help="Process frame range from video")
    batch.add_argument("source", help="Source video file path")
    batch.add_argument("output_folder", help="Output folder for PNG sequence")
    batch.add_argument("--start-frame", type=int, required=True)
    batch.add_argument("--end-frame", type=int, required=True)
    batch.add_argument("--fps", type=float, required=True)
    batch.add_argument("--screen", default="green", choices=["green", "blue"])
    batch.add_argument("--despill", type=float, default=0.5)
    batch.add_argument("--despeckle", type=int, default=1)
    batch.add_argument("--despeckle-size", type=int, default=400)
    batch.add_argument("--refiner", type=float, default=1.0)

    args = parser.parse_args()

    # Backwards compatibility: if no subcommand, treat positional args as single mode
    if args.mode is None:
        # Legacy call: ae_processor.py <input> <output> [options]
        if len(sys.argv) >= 3 and not sys.argv[1].startswith('-'):
            legacy = argparse.ArgumentParser()
            legacy.add_argument("input")
            legacy.add_argument("output")
            legacy.add_argument("--screen", default="green", choices=["green", "blue"])
            legacy.add_argument("--despill", type=float, default=0.5)
            legacy.add_argument("--despeckle", type=int, default=1)
            legacy.add_argument("--despeckle-size", type=int, default=400)
            legacy.add_argument("--refiner", type=float, default=1.0)
            args = legacy.parse_args()
            args.mode = "single"
        else:
            parser.print_help()
            sys.exit(1)

    if args.mode == "single":
        success = process_frame(
            args.input,
            args.output,
            screen_type=args.screen,
            despill=args.despill,
            despeckle=bool(args.despeckle),
            despeckle_size=args.despeckle_size,
            refiner=args.refiner,
        )
        sys.exit(0 if success else 1)

    elif args.mode == "batch":
        count = process_batch(
            args.source,
            args.output_folder,
            start_frame=args.start_frame,
            end_frame=args.end_frame,
            fps=args.fps,
            screen_type=args.screen,
            despill=args.despill,
            despeckle=bool(args.despeckle),
            despeckle_size=args.despeckle_size,
            refiner=args.refiner,
        )
        sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
