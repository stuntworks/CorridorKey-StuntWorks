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

# Add CorridorKey to path
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


def main():
    parser = argparse.ArgumentParser(description="CorridorKey AE Processor")
    parser.add_argument("input", help="Input image path")
    parser.add_argument("output", help="Output image path (PNG with alpha)")
    parser.add_argument("--screen", default="green", choices=["green", "blue"])
    parser.add_argument("--despill", type=float, default=0.5)
    parser.add_argument("--despeckle", type=int, default=1)
    parser.add_argument("--despeckle-size", type=int, default=400)
    parser.add_argument("--refiner", type=float, default=1.0)

    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
