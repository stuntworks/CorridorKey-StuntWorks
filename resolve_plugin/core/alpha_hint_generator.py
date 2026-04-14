# Last modified: 2026-04-13 | Change: HRCS retrofit (documentation only, no logic changes) | Full history: git log
"""
Alpha Hint Generator for CorridorKey
Generates rough alpha masks using simple chroma key detection.

WHAT IT DOES:
    Takes color images (stills, sequences, or video) with green/blue screen backgrounds
    and produces single-channel alpha hint masks using HSV-based chroma detection.
    These hints feed downstream keying/compositing stages as a rough matte starting point.

DEPENDS-ON:
    - cv2 (OpenCV) for color conversion, morphology, blur, video I/O
    - numpy for array operations and dtype handling
    - pathlib / os for filesystem traversal

AFFECTS:
    - Any pipeline stage that consumes alpha hint PNGs (frame_NNNNN_alpha.png naming)
    - Downstream composite quality depends on tolerance/softness tuning here
"""
import os
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np


# WHAT IT DOES: Wraps all chroma-based alpha hint generation — single frames, image sequences, and video files.
# DEPENDS-ON: cv2, numpy
# AFFECTS: Every downstream consumer of alpha hint masks in the CorridorKey pipeline.
class AlphaHintGenerator:
    """Generate alpha hint masks using simple chroma detection."""

    def __init__(
        self,
        screen_type: str = "green",
        tolerance: float = 0.4,
        softness: float = 0.1
    ):
        """Initialize generator.

        Args:
            screen_type: "green" or "blue"
            tolerance: Detection threshold (0.0-1.0)
            softness: Edge softness (0.0-1.0)
        """
        self.screen_type = screen_type.lower()
        self.tolerance = tolerance
        self.softness = softness

        # HSV ranges for green/blue screen
        if self.screen_type == "green":
            self.lower_hsv = np.array([35, 50, 50])
            self.upper_hsv = np.array([85, 255, 255])
        else:  # blue
            self.lower_hsv = np.array([100, 50, 50])
            self.upper_hsv = np.array([130, 255, 255])

    # WHAT IT DOES: Produces an alpha hint mask from a single image via HSV chroma detection,
    #   morphological cleanup, and optional Gaussian softening.
    # DEPENDS-ON: self.screen_type, self.tolerance, self.softness, self.lower_hsv, self.upper_hsv
    # AFFECTS: All single-frame and batch callers (generate_hints_for_sequence, generate_hint_from_video).
    # DANGER ZONE FRAGILE/HIGH: HSV range constants (lower_hsv/upper_hsv) are hardcoded in __init__.
    #   Changing them silently alters every mask in the pipeline. / breaks: all output masks / depends on: __init__ ranges
    def generate_hint(self, image: np.ndarray, color_format: str = "bgr") -> np.ndarray:
        """Generate alpha hint from image using chroma detection.

        Args:
            image: Color image as uint8 or float32 (0-1)
            color_format: "bgr" (OpenCV default) or "rgb"

        Returns:
            Alpha hint mask [H, W] with 0=background, 255=foreground (uint8)
        """
        # Handle different input formats
        if image.dtype == np.float32 or image.dtype == np.float64:
            img_uint8 = (image * 255).astype(np.uint8)
        else:
            img_uint8 = image

        # Convert to BGR for HSV conversion
        if len(img_uint8.shape) == 2:
            img_bgr = cv2.cvtColor(img_uint8, cv2.COLOR_GRAY2BGR)
        elif img_uint8.shape[2] == 4:
            if color_format == "rgb":
                img_bgr = cv2.cvtColor(img_uint8, cv2.COLOR_RGBA2BGR)
            else:
                img_bgr = cv2.cvtColor(img_uint8, cv2.COLOR_BGRA2BGR)
        elif color_format == "rgb":
            img_bgr = cv2.cvtColor(img_uint8, cv2.COLOR_RGB2BGR)
        else:
            img_bgr = img_uint8

        # Convert to HSV
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

        # Create mask of screen color areas
        screen_mask = cv2.inRange(hsv, self.lower_hsv, self.upper_hsv)

        # Invert to get subject mask (subject is white)
        subject_mask = cv2.bitwise_not(screen_mask)

        # Clean up with morphology
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_CLOSE, kernel)
        subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_OPEN, kernel)

        # Apply slight blur for softer edges
        if self.softness > 0:
            blur_size = max(3, int(self.softness * 15))
            if blur_size % 2 == 0:
                blur_size += 1
            subject_mask = cv2.GaussianBlur(subject_mask, (blur_size, blur_size), 0)

        return subject_mask

    # WHAT IT DOES: Iterates all image files (png/exr/tif/tiff/jpg/jpeg) in a directory,
    #   generates an alpha hint for each, and writes results as <stem>_alpha.png.
    # DEPENDS-ON: generate_hint(), cv2.imread, pathlib glob patterns
    # AFFECTS: Batch output directory contents; downstream compositors expect the _alpha.png naming convention.
    def generate_hints_for_sequence(
        self,
        input_dir: str,
        output_dir: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> int:
        """Generate alpha hints for all frames in a sequence.

        Args:
            input_dir: Directory containing input frames
            output_dir: Directory for output alpha hints
            progress_callback: Optional callback(current, total, message)

        Returns:
            Number of frames processed
        """
        input_path = Path(input_dir)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Find input frames
        frames = sorted(
            list(input_path.glob("*.png")) +
            list(input_path.glob("*.exr")) +
            list(input_path.glob("*.tif")) +
            list(input_path.glob("*.tiff")) +
            list(input_path.glob("*.jpg")) +
            list(input_path.glob("*.jpeg"))
        )

        if not frames:
            raise RuntimeError(f"No frames found in {input_dir}")

        total = len(frames)
        processed = 0

        for i, frame_path in enumerate(frames):
            if progress_callback:
                progress_callback(i, total, f"Generating hints: {i+1}/{total}")

            # Read frame
            img = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
            if img is None:
                continue

            # Generate alpha hint
            alpha_hint = self.generate_hint(img)

            # Save with matching name
            out_name = f"{frame_path.stem}_alpha.png"
            cv2.imwrite(str(output_path / out_name), alpha_hint)

            processed += 1

        if progress_callback:
            progress_callback(total, total, "Hints complete")

        return processed

    # WHAT IT DOES: Opens a video file, reads every frame, generates an alpha hint per frame,
    #   and writes numbered output PNGs (frame_00000_alpha.png).
    # DEPENDS-ON: generate_hint(), cv2.VideoCapture
    # AFFECTS: Batch output directory contents; frame numbering starts at 0 with zero-padded 5-digit names.
    def generate_hint_from_video(
        self,
        video_path: str,
        output_dir: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> int:
        """Generate alpha hints from a video file.

        Args:
            video_path: Path to video file
            output_dir: Directory for output alpha hints
            progress_callback: Optional callback(current, total, message)

        Returns:
            Number of frames processed
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video: {video_path}")

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        processed = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if progress_callback:
                progress_callback(processed, total, f"Generating hints: {processed+1}/{total}")

            # Generate alpha hint
            alpha_hint = self.generate_hint(frame)

            # Save
            out_name = f"frame_{processed:05d}_alpha.png"
            cv2.imwrite(str(output_path / out_name), alpha_hint)

            processed += 1

        cap.release()

        if progress_callback:
            progress_callback(total, total, "Hints complete")

        return processed
