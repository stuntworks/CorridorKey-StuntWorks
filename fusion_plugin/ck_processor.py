#!/usr/bin/env python
"""CorridorKey Fusion Plugin - Python Processor

Command-line interface for Fusion to call.
Usage: python ck_processor.py <input_path> <output_path> [options]

Options:
    --screen green|blue
    --despill 0.0-1.0
    --model simple|sam2
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

logging.basicConfig(level=logging.INFO, format='[CK] %(message)s')
log = logging.getLogger(__name__)


def generate_chroma_hint(image, screen_type="green"):
    """Generate simple chroma key alpha hint."""
    import numpy as np

    if screen_type == "green":
        # Green screen: high green, low red/blue
        green = image[:, :, 1]
        red = image[:, :, 0]
        blue = image[:, :, 2]
        screen_mask = (green > 0.3) & (green > red * 1.2) & (green > blue * 1.2)
    else:
        # Blue screen
        blue = image[:, :, 2]
        red = image[:, :, 0]
        green = image[:, :, 1]
        screen_mask = (blue > 0.3) & (blue > red * 1.2) & (blue > green * 1.2)

    # Invert: we want foreground=1, screen=0
    alpha_hint = (~screen_mask).astype(np.float32)

    # Slight blur for softer edges
    import cv2
    alpha_hint = cv2.GaussianBlur(alpha_hint, (5, 5), 0)

    return alpha_hint


def process_frame(input_path, output_path, screen_type="green", despill=0.5,
                  despeckle=True, despeckle_size=400, refiner=1.0):
    """Process a single frame through CorridorKey."""
    import numpy as np
    import cv2

    log.info(f"Input: {input_path}")
    log.info(f"Output: {output_path}")
    log.info(f"Screen: {screen_type}, Despill: {despill}")

    # Read input image
    img = cv2.imread(str(input_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        log.error(f"Cannot read image: {input_path}")
        return False

    # Convert to RGB float32
    if len(img.shape) == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    if img.dtype == np.uint8:
        img = img.astype(np.float32) / 255.0
    elif img.dtype == np.uint16:
        img = img.astype(np.float32) / 65535.0

    log.info(f"Image size: {img.shape[1]}x{img.shape[0]}")

    # Generate alpha hint
    log.info("Generating alpha hint...")
    alpha_hint = generate_chroma_hint(img, screen_type)

    # Load CorridorKey processor
    log.info("Loading CorridorKey AI...")
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

        log.info("Processing frame...")
        result = processor.process_frame(img, alpha_hint, settings)

        fg = result.get("fg")
        alpha = result.get("alpha")

        if fg is None or alpha is None:
            log.error("Processing failed - no output")
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
        output = cv2.merge([fg_bgr[:,:,0], fg_bgr[:,:,1], fg_bgr[:,:,2], alpha_uint8])

        # Save output
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), output)

        log.info(f"Saved: {output_path}")

        processor.cleanup()
        return True

    except Exception as e:
        log.error(f"Processing error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description="CorridorKey Fusion Processor")
    parser.add_argument("input", help="Input image path")
    parser.add_argument("output", help="Output image path (PNG with alpha)")
    parser.add_argument("--screen", default="green", choices=["green", "blue"],
                        help="Screen type (default: green)")
    parser.add_argument("--despill", type=float, default=0.5,
                        help="Despill strength 0.0-1.0 (default: 0.5)")
    parser.add_argument("--despeckle", type=int, default=1,
                        help="Enable despeckle 0/1 (default: 1)")
    parser.add_argument("--despeckle-size", type=int, default=400,
                        help="Min despeckle area in pixels (default: 400)")
    parser.add_argument("--refiner", type=float, default=1.0,
                        help="Refiner strength 0.0-1.0 (default: 1.0)")

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
