"""
Generate AlphaHint masks using BiRefNet for CorridorKey
Works with ~4GB VRAM
"""
import os
import sys
from pathlib import Path

import torch

# Add BiRefNet module
sys.path.insert(0, str(Path(__file__).parent))
from BiRefNetModule.wrapper import BiRefNetHandler

def main():
    clip_dir = Path("ClipsForInference/earl_green_fall")
    input_video = clip_dir / "Input.mov"
    alpha_dir = clip_dir / "AlphaHint"

    os.makedirs(alpha_dir, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading BiRefNet on {device}...")

    # Use Matting model for best green screen results
    handler = BiRefNetHandler(device=device, usage="Matting")

    print(f"Processing video: {input_video}")
    print(f"Output to: {alpha_dir}")

    frame_count = [0]
    def on_frame(idx, _):
        frame_count[0] = idx + 1
        if (idx + 1) % 10 == 0:
            print(f"  Processed {idx + 1} frames...")

    # Process video directly - BiRefNet handles frame extraction
    handler.process(
        input_path=str(input_video),
        alpha_output_dir=str(alpha_dir),
        dilate_radius=0,
        on_frame_complete=on_frame
    )

    handler.cleanup()
    print(f"Done! Generated {frame_count[0]} alpha hints.")

if __name__ == "__main__":
    main()
