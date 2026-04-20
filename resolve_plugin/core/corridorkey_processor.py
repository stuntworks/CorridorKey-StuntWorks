# Last modified: 2026-04-13 | Change: HRCS retrofit (documentation only, no logic changes) | Full history: git log
"""
CorridorKey Processing Engine Wrapper
Wraps the CorridorKey neural network for use in DaVinci Resolve plugin.

WHAT IT DOES:
    Wraps the CorridorKey neural keying engine for single-frame and batch
    sequence processing. Handles image I/O, color space conversion, and
    GPU lifecycle for use inside the DaVinci Resolve OFX plugin.

DEPENDS-ON:
    - CorridorKeyModule.backend.create_engine (neural keyer)
    - numpy, cv2 (image I/O and conversion)

AFFECTS:
    - resolve_plugin — this is the sole processing bridge between the
      Resolve UI layer and the CorridorKey neural network.
"""
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Dict, Any
from dataclasses import dataclass

import numpy as np
import cv2

# Add CorridorKey to path
CORRIDORKEY_ROOT = Path(__file__).parent.parent.parent
if str(CORRIDORKEY_ROOT) not in sys.path:
    sys.path.insert(0, str(CORRIDORKEY_ROOT))


# WHAT IT DOES: Holds all user-facing knobs for keying (screen color, despill, despeckle, etc.)
# ISOLATED: pure data container, no side effects
@dataclass
class ProcessingSettings:
    """Settings for CorridorKey processing."""
    screen_type: str = "green"  # "green" or "blue"
    despill_strength: float = 0.5  # 0.0-1.0
    despeckle_enabled: bool = True
    despeckle_size: int = 400  # Min pixel area
    refiner_strength: float = 1.0
    input_is_srgb: bool = True  # True for video/PNG, False for EXR


# WHAT IT DOES: Wraps the CorridorKey neural keyer — loads model, processes frames, manages GPU memory
# DEPENDS-ON: CorridorKeyModule.backend.create_engine, ProcessingSettings
# AFFECTS: All keying output in the Resolve plugin flows through this class
class CorridorKeyProcessor:
    """Wrapper for CorridorKey neural keying engine."""

    def __init__(self, device: str = "cuda"):
        """Initialize CorridorKey engine.

        Args:
            device: Compute device ("cuda", "mps", "cpu")
        """
        self.device = device
        self.engine = None
        self._load_engine()

    # WHAT IT DOES: Imports and instantiates the CorridorKey backend engine on the target device
    # DEPENDS-ON: CorridorKeyModule.backend.create_engine
    # AFFECTS: self.engine — every process call fails if this fails
    # DANGER ZONE FRAGILE/HIGH/CRITICAL: import path must resolve via sys.path hack above / breaks: all processing / depends on: CORRIDORKEY_ROOT on sys.path
    def _load_engine(self):
        """Load the CorridorKey model."""
        try:
            from CorridorKeyModule.backend import create_engine
            self.engine = create_engine(backend="torch", device=self.device)
        except Exception as e:
            raise RuntimeError(f"Failed to load CorridorKey engine: {e}")

    # WHAT IT DOES: Sends one RGB frame + alpha hint through the neural keyer, returns fg/alpha/composite
    # DEPENDS-ON: self.engine (loaded by _load_engine), ProcessingSettings
    # AFFECTS: process_sequence calls this per-frame; Resolve single-frame preview uses this directly
    def process_frame(
        self,
        image: np.ndarray,
        alpha_hint: np.ndarray,
        settings: ProcessingSettings
    ) -> Dict[str, np.ndarray]:
        """Process single frame through CorridorKey.

        Args:
            image: RGB image [H, W, 3] as float32 (0-1)
            alpha_hint: Alpha hint mask [H, W] (0-1)
            settings: Processing settings

        Returns:
            Dict with keys:
                - "fg": Foreground color [H, W, 3]
                - "alpha": Clean alpha channel [H, W]
                - "processed": RGBA [H, W, 4]
                - "comp": Preview composite [H, W, 3]
        """
        result = self.engine.process_frame(
            image,
            alpha_hint,
            input_is_linear=not settings.input_is_srgb,
            fg_is_straight=True,
            despill_strength=settings.despill_strength,
            auto_despeckle=settings.despeckle_enabled,
            despeckle_size=settings.despeckle_size,
            refiner_scale=settings.refiner_strength,
        )
        return result

    # WHAT IT DOES: Batch-processes an image sequence (PNG/EXR/TIF), writes FG, Matte, and Processed EXR outputs
    # DEPENDS-ON: self.process_frame, cv2 for I/O, filesystem dirs for input/alpha/output
    # AFFECTS: Creates FG/, Matte/, Processed/ subdirs under output_dir; progress_callback drives UI progress bar
    # DANGER ZONE FRAGILE/HIGH/CRITICAL: alpha frame count mismatch silently duplicates last alpha / breaks: matte accuracy if alpha set is incomplete / depends on: alpha_frames list length
    def process_sequence(
        self,
        input_dir: str,
        alpha_dir: str,
        output_dir: str,
        settings: ProcessingSettings,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Dict[str, Any]:
        """Process entire image sequence.

        Args:
            input_dir: Directory containing input frames
            alpha_dir: Directory containing alpha hint frames
            output_dir: Directory for output frames
            settings: Processing settings
            progress_callback: Optional callback(current, total, message)

        Returns:
            Dict with output paths and statistics
        """
        input_path = Path(input_dir)
        alpha_path = Path(alpha_dir)
        output_path = Path(output_dir)

        # Create output subdirectories
        fg_dir = output_path / "FG"
        matte_dir = output_path / "Matte"
        processed_dir = output_path / "Processed"

        for d in [fg_dir, matte_dir, processed_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Find input frames
        input_frames = sorted(
            list(input_path.glob("*.png")) +
            list(input_path.glob("*.exr")) +
            list(input_path.glob("*.tif")) +
            list(input_path.glob("*.tiff"))
        )

        alpha_frames = sorted(
            list(alpha_path.glob("*.png")) +
            list(alpha_path.glob("*.exr"))
        )

        if not input_frames:
            raise RuntimeError(f"No input frames found in {input_dir}")

        if len(alpha_frames) < len(input_frames):
            if not alpha_frames:
                raise RuntimeError(f"No alpha hint frames found in {alpha_dir}")
            import logging
            logging.getLogger(__name__).warning(
                "Alpha frame count %d < input count %d — duplicating last alpha for remaining frames",
                len(alpha_frames), len(input_frames),
            )
            last = alpha_frames[-1]
            alpha_frames.extend([last] * (len(input_frames) - len(alpha_frames)))

        total_frames = len(input_frames)
        processed_count = 0

        for i, (input_frame, alpha_frame) in enumerate(zip(input_frames, alpha_frames)):
            if progress_callback:
                progress_callback(i, total_frames, f"Processing frame {i+1}/{total_frames}")

            # Read input frame
            img = cv2.imread(str(input_frame), cv2.IMREAD_UNCHANGED)
            if img is None:
                if progress_callback:
                    progress_callback(i, total_frames, f"WARNING: could not read {input_frame.name} — skipping")
                continue

            # Convert BGR to RGB, normalize to 0-1
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

            # Read alpha hint
            alpha = cv2.imread(str(alpha_frame), cv2.IMREAD_GRAYSCALE)
            if alpha is None:
                # Generate simple mask if alpha missing
                alpha = np.ones((img.shape[0], img.shape[1]), dtype=np.float32)
            else:
                if alpha.dtype == np.uint8:
                    alpha = alpha.astype(np.float32) / 255.0
                elif alpha.dtype == np.uint16:
                    alpha = alpha.astype(np.float32) / 65535.0

            # Resize alpha to match input if needed
            if alpha.shape[:2] != img.shape[:2]:
                alpha = cv2.resize(alpha, (img.shape[1], img.shape[0]))

            # Process frame
            result = self.process_frame(img, alpha, settings)

            # Save outputs
            frame_name = f"{i:05d}.exr"

            # Save FG (straight RGB) — keep float32 for EXR precision
            fg_rgb = result.get("fg", img)
            if fg_rgb is not None:
                fg_f32 = np.clip(fg_rgb, 0, 1).astype(np.float32)
                fg_bgr = cv2.cvtColor(fg_f32, cv2.COLOR_RGB2BGR)
                cv2.imwrite(str(fg_dir / frame_name), fg_bgr)

            # Save Matte (alpha only)
            matte = result.get("alpha", alpha)
            if matte is not None:
                if len(matte.shape) == 3:
                    matte = matte[:, :, 0]
                cv2.imwrite(str(matte_dir / frame_name), np.clip(matte, 0, 1).astype(np.float32))

            # Save Processed (premultiplied RGBA)
            processed = result.get("processed")
            if processed is not None:
                # Convert RGBA to BGRA for OpenCV — keep float32 for EXR
                proc_f32 = np.clip(processed, 0, 1).astype(np.float32)
                processed_bgra = cv2.cvtColor(proc_f32, cv2.COLOR_RGBA2BGRA)
                cv2.imwrite(str(processed_dir / frame_name), processed_bgra)

            processed_count += 1

        if progress_callback:
            progress_callback(total_frames, total_frames, "Complete")

        return {
            "output_dir": str(output_path),
            "fg_dir": str(fg_dir),
            "matte_dir": str(matte_dir),
            "processed_dir": str(processed_dir),
            "frame_count": processed_count,
        }

    # WHAT IT DOES: Releases the neural engine and frees GPU/CUDA memory
    # DEPENDS-ON: self.engine, torch (optional — gracefully skipped if not installed)
    # AFFECTS: Must be called on plugin unload or GPU memory leaks between sessions
    def cleanup(self):
        """Release GPU memory."""
        if self.engine:
            del self.engine
            self.engine = None

        import gc
        gc.collect()

        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
