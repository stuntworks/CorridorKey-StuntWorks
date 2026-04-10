"""
Generate simple chroma key alpha hints for CorridorKey
No GPU needed - uses basic green screen detection
"""
import os
import cv2
import numpy as np
from pathlib import Path
from tqdm import tqdm

def chroma_key_mask(frame, green_thresh=50, blur_size=5):
    """
    Simple green screen detection
    Returns a rough mask where subject is white, green screen is black
    """
    # Convert to HSV
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    # Define green range (adjust these for your green screen)
    lower_green = np.array([35, 50, 50])
    upper_green = np.array([85, 255, 255])

    # Create mask of green areas
    green_mask = cv2.inRange(hsv, lower_green, upper_green)

    # Invert to get subject mask (subject is white)
    subject_mask = cv2.bitwise_not(green_mask)

    # Clean up with morphology
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_CLOSE, kernel)
    subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_OPEN, kernel)

    # Blur edges slightly
    if blur_size > 0:
        subject_mask = cv2.GaussianBlur(subject_mask, (blur_size, blur_size), 0)

    return subject_mask

def main():
    clip_dir = Path("ClipsForInference/earl_green_fall")
    input_video = clip_dir / "Input.mov"
    alpha_dir = clip_dir / "AlphaHint"

    os.makedirs(alpha_dir, exist_ok=True)

    # Open video
    cap = cv2.VideoCapture(str(input_video))
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"Processing {frame_count} frames from {input_video}")
    print(f"Output to: {alpha_dir}")

    for i in tqdm(range(frame_count)):
        ret, frame = cap.read()
        if not ret:
            break

        # Generate chroma key mask
        mask = chroma_key_mask(frame)

        # Save with matching filename format
        out_name = f"Input_alpha_{i:05d}.png"
        cv2.imwrite(str(alpha_dir / out_name), mask)

    cap.release()
    print(f"Done! Generated {i+1} alpha hints.")
    print(f"\nNow run: uv run python corridorkey_cli.py run-inference")

if __name__ == "__main__":
    main()
