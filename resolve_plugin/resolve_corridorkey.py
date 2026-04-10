"""
CorridorKey for DaVinci Resolve
Neural green screen keying integration.

Launch from: Workspace > Scripts > CorridorKey
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# Add plugin modules to path
PLUGIN_DIR = Path(__file__).parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# Add CorridorKey root to path
CORRIDORKEY_ROOT = PLUGIN_DIR.parent
if str(CORRIDORKEY_ROOT) not in sys.path:
    sys.path.insert(0, str(CORRIDORKEY_ROOT))

from core.resolve_bridge import ResolveBridge, ResolveConnectionError
from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
from core.alpha_hint_generator import AlphaHintGenerator
from ui.uimanager_panel import create_corridorkey_panel


class CorridorKeyResolvePlugin:
    """Main plugin class orchestrating CorridorKey processing in Resolve."""

    def __init__(self):
        self.bridge = None
        self.processor = None
        self.hint_generator = None
        self.temp_dir = None

    def initialize(self):
        """Initialize connection to Resolve and load CorridorKey."""
        # Connect to Resolve
        self.bridge = ResolveBridge()

        # Create temp directory
        self.temp_dir = Path(tempfile.mkdtemp(prefix="corridorkey_"))

    def cleanup(self):
        """Clean up resources."""
        if self.processor:
            self.processor.cleanup()

        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def preview_frame(
        self,
        settings: dict,
        progress_callback: Callable[[int, int, str], None],
        log_callback: Callable[[str], None],
        cancel_check: Callable[[], bool]
    ):
        """Preview single frame at current playhead position.

        Processes one frame and opens the result for review.
        """
        import cv2
        import subprocess

        # Initialize processor on first use
        if self.processor is None:
            log_callback("Loading CorridorKey model (first time)...")
            progress_callback(10, 100, "Loading model...")
            self.processor = CorridorKeyProcessor(device="cuda")
            log_callback("Model loaded successfully")

        # Initialize hint generator
        self.hint_generator = AlphaHintGenerator(
            screen_type=settings.get("screen_type", "green"),
            tolerance=0.4,
            softness=0.1
        )

        progress_callback(20, 100, "Grabbing current frame...")
        log_callback("Grabbing frame at playhead...")

        # Create preview directory
        preview_dir = self.temp_dir / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)

        # Get current frame from timeline
        try:
            timeline = self.bridge.timeline
            if not timeline:
                log_callback("Error: No timeline active")
                return

            # Get current playhead position
            current_tc = timeline.GetCurrentTimecode()
            log_callback(f"  Timecode: {current_tc}")

            # Get clip at playhead on track 1
            clips = self.bridge.get_timeline_video_items(track_index=1)
            if not clips:
                log_callback("Error: No clips on track 1")
                return

            # Find clip under playhead
            playhead_frame = timeline.GetCurrentVideoItem()
            current_clip = None

            if playhead_frame:
                current_clip = playhead_frame
            else:
                # Fallback: use first clip
                current_clip = clips[0]
                log_callback("  Using first clip on track 1")

            clip_info = self.bridge.get_clip_info(current_clip)
            source_path = clip_info.get("file_path", "")

            if not source_path or not os.path.exists(source_path):
                log_callback(f"Error: Source file not found: {source_path}")
                return

            log_callback(f"  Source: {os.path.basename(source_path)}")

            # Calculate frame number within clip
            clip_start = current_clip.GetStart()
            clip_left_offset = current_clip.GetLeftOffset()

            # Parse timecode to frame (simplified)
            fps = float(clip_info.get("fps", 24))
            tc_parts = current_tc.replace(";", ":").split(":")
            if len(tc_parts) == 4:
                h, m, s, f = map(int, tc_parts)
                timeline_frame = int((h * 3600 + m * 60 + s) * fps + f)
            else:
                timeline_frame = clip_start

            # Frame within source media
            source_frame = clip_left_offset + (timeline_frame - clip_start)
            log_callback(f"  Frame: {source_frame}")

            # Extract frame using OpenCV
            cap = cv2.VideoCapture(source_path)
            cap.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                log_callback("Error: Could not read frame from source")
                return

            # Save frame for processing
            frame_path = preview_dir / "preview_frame.png"
            cv2.imwrite(str(frame_path), frame)
            log_callback("  Frame extracted")

        except Exception as e:
            log_callback(f"Error getting frame: {e}")
            import traceback
            traceback.print_exc()
            return

        progress_callback(40, 100, "Generating alpha hint...")

        # Read frame and generate alpha hint
        frame = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
        if frame is None:
            log_callback("Error: Could not read exported frame")
            return

        alpha_hint = self.hint_generator.generate_hint(frame)

        # Save alpha hint for debug
        alpha_path = preview_dir / "preview_alpha.png"
        cv2.imwrite(str(alpha_path), alpha_hint)
        log_callback("  Alpha hint generated")

        progress_callback(60, 100, "Running neural keying...")

        # Convert frame for processing
        if len(frame.shape) == 2:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
        elif frame.shape[2] == 4:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        else:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        if frame_rgb.dtype == np.uint8:
            frame_rgb = frame_rgb.astype(np.float32) / 255.0

        if alpha_hint.dtype == np.uint8:
            alpha_hint = alpha_hint.astype(np.float32) / 255.0

        # Process frame
        proc_settings = ProcessingSettings(
            screen_type=settings.get("screen_type", "green"),
            despill_strength=settings.get("despill_strength", 0.5),
            despeckle_enabled=settings.get("despeckle_enabled", True),
            despeckle_size=settings.get("despeckle_size", 400),
            refiner_strength=settings.get("refiner_strength", 1.0),
            input_is_srgb=settings.get("input_is_srgb", True),
        )

        result = self.processor.process_frame(frame_rgb, alpha_hint, proc_settings)
        log_callback("  Neural keying complete")

        progress_callback(80, 100, "Saving preview...")

        # Save results
        output_fg = preview_dir / "preview_keyed.png"
        output_matte = preview_dir / "preview_matte.png"
        output_comp = preview_dir / "preview_comp.png"

        # Save foreground (convert back to BGR for OpenCV)
        fg = result.get("fg")
        if fg is not None:
            fg_bgr = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(output_fg), fg_bgr)

        # Save matte
        matte = result.get("alpha")
        if matte is not None:
            if len(matte.shape) == 3:
                matte = matte[:, :, 0]
            cv2.imwrite(str(output_matte), (matte * 255).astype(np.uint8))

        # Save comp (preview composite)
        comp = result.get("comp")
        if comp is not None:
            comp_bgr = cv2.cvtColor((comp * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(output_comp), comp_bgr)

        log_callback(f"  Saved to: {preview_dir}")

        progress_callback(100, 100, "Preview complete!")

        # Open the preview folder
        try:
            if sys.platform == "win32":
                os.startfile(str(preview_dir))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(preview_dir)])
            else:
                subprocess.run(["xdg-open", str(preview_dir)])
            log_callback("  Opened preview folder")
        except Exception as e:
            log_callback(f"  Preview saved to: {preview_dir}")

        log_callback("\nPreview complete! Check the opened folder.")
        log_callback("  preview_keyed.png - Foreground with transparency")
        log_callback("  preview_matte.png - Alpha channel")
        log_callback("  preview_comp.png - Composite preview")

    def process_clips(
        self,
        settings: dict,
        mode: str,  # "selected", "all", or "preview"
        progress_callback: Callable[[int, int, str], None],
        log_callback: Callable[[str], None],
        cancel_check: Callable[[], bool]
    ):
        """Process clips through CorridorKey.

        Args:
            settings: Processing settings from UI
            mode: "selected", "all", or "preview"
            progress_callback: Progress update callback
            log_callback: Log message callback
            cancel_check: Check if cancelled
        """
        # Handle preview mode separately
        if mode == "preview":
            return self.preview_frame(settings, progress_callback, log_callback, cancel_check)

        # Initialize processor on first use
        if self.processor is None:
            log_callback("Loading CorridorKey model (first time)...")
            self.processor = CorridorKeyProcessor(device="cuda")
            log_callback("Model loaded successfully")

        # Initialize hint generator
        self.hint_generator = AlphaHintGenerator(
            screen_type=settings.get("screen_type", "green"),
            tolerance=0.4,
            softness=0.1
        )

        # Get clips to process
        if mode == "all":
            clips = self.bridge.get_timeline_video_items(track_index=1)
        else:
            # For "selected", process all clips on track 1 for now
            # (Resolve API doesn't have direct selection access)
            clips = self.bridge.get_timeline_video_items(track_index=1)

        if not clips:
            log_callback("No clips found on timeline")
            return

        log_callback(f"Found {len(clips)} clips to process")

        total_clips = len(clips)
        output_mode = settings.get("output_mode", "above")

        for clip_idx, clip in enumerate(clips):
            if cancel_check():
                log_callback("Cancelled by user")
                break

            clip_info = self.bridge.get_clip_info(clip)
            clip_name = clip_info["name"]
            log_callback(f"\nProcessing: {clip_name}")

            # Create working directories for this clip
            clip_work_dir = self.temp_dir / f"clip_{clip_idx}"
            input_dir = clip_work_dir / "input"
            alpha_dir = clip_work_dir / "alpha"
            output_dir = clip_work_dir / "output"

            for d in [input_dir, alpha_dir, output_dir]:
                d.mkdir(parents=True, exist_ok=True)

            try:
                # Step 1: Export frames from Resolve
                progress_callback(0, 100, f"Exporting: {clip_name}")
                log_callback(f"  Exporting {clip_info['duration']} frames...")

                export_result = self.bridge.export_clip_frames(
                    clip,
                    str(input_dir),
                    format="PNG",
                    progress_callback=lambda c, t, m: progress_callback(
                        int((clip_idx / total_clips + c / t / total_clips) * 33),
                        100,
                        f"Exporting: {m}"
                    )
                )

                if cancel_check():
                    break

                # Step 2: Generate alpha hints
                progress_callback(33, 100, f"Generating hints: {clip_name}")
                log_callback("  Generating alpha hints...")

                hint_count = self.hint_generator.generate_hints_for_sequence(
                    str(input_dir),
                    str(alpha_dir),
                    progress_callback=lambda c, t, m: progress_callback(
                        33 + int((clip_idx / total_clips + c / t / total_clips) * 17),
                        100,
                        m
                    )
                )

                log_callback(f"  Generated {hint_count} alpha hints")

                if cancel_check():
                    break

                # Step 3: Run CorridorKey inference
                progress_callback(50, 100, f"Processing: {clip_name}")
                log_callback("  Running neural keying...")

                proc_settings = ProcessingSettings(
                    screen_type=settings.get("screen_type", "green"),
                    despill_strength=settings.get("despill_strength", 0.5),
                    despeckle_enabled=settings.get("despeckle_enabled", True),
                    despeckle_size=settings.get("despeckle_size", 400),
                    refiner_strength=settings.get("refiner_strength", 1.0),
                    input_is_srgb=settings.get("input_is_srgb", True),
                )

                result = self.processor.process_sequence(
                    str(input_dir),
                    str(alpha_dir),
                    str(output_dir),
                    proc_settings,
                    progress_callback=lambda c, t, m: progress_callback(
                        50 + int((clip_idx / total_clips + c / t / total_clips) * 40),
                        100,
                        m
                    )
                )

                log_callback(f"  Processed {result['frame_count']} frames")

                if cancel_check():
                    break

                # Step 4: Import results to MediaPool
                progress_callback(90, 100, f"Importing: {clip_name}")
                log_callback("  Importing to MediaPool...")

                # Import the processed (premultiplied RGBA) sequence
                keyed_item = self.bridge.import_sequence_to_mediapool(
                    result["processed_dir"],
                    bin_name="CorridorKey Output",
                    generate_proxy=settings.get("generate_proxy", True)
                )

                if keyed_item:
                    log_callback(f"  Imported to MediaPool")

                    # Step 5: Place on timeline based on output mode
                    if output_mode == "above":
                        # Add to track above original
                        new_item = self.bridge.add_clip_to_timeline(
                            keyed_item,
                            track_index=2,  # Track above
                            start_frame=clip_info["start"]
                        )
                        if new_item:
                            log_callback(f"  Added to track 2")

                    elif output_mode == "replace":
                        # TODO: Replace original (more complex)
                        log_callback("  Replace mode not yet implemented")

                    # MediaPool only - nothing more to do

                log_callback(f"  Complete!")

            except Exception as e:
                log_callback(f"  Error: {e}")
                import traceback
                traceback.print_exc()

            finally:
                # Clean up clip temp files
                if clip_work_dir.exists():
                    shutil.rmtree(clip_work_dir, ignore_errors=True)

        progress_callback(100, 100, "All clips processed")
        log_callback(f"\nDone! Processed {total_clips} clips")


def main():
    """Main entry point for script menu invocation."""
    plugin = CorridorKeyResolvePlugin()

    try:
        plugin.initialize()

        # Check for Fusion UIManager
        fusion = plugin.bridge.fusion
        if fusion and fusion.UIManager:
            # Studio version - use native panel
            def on_process(settings, mode, progress_cb, log_cb, cancel_check):
                plugin.process_clips(settings, mode, progress_cb, log_cb, cancel_check)

            win, disp = create_corridorkey_panel(fusion, on_process)
            win.Show()
            disp.RunLoop()
            win.Hide()
        else:
            # Free version - console mode
            print("CorridorKey - Console Mode")
            print("Fusion UIManager not available (requires DaVinci Resolve Studio)")
            print("")
            print("Processing all clips on track 1...")

            def progress(c, t, m):
                print(f"\r  {m} ({c}/{t})", end="")

            def log(m):
                print(m)

            plugin.process_clips(
                {
                    "screen_type": "green",
                    "despill_strength": 0.5,
                    "despeckle_enabled": True,
                    "despeckle_size": 400,
                    "refiner_strength": 1.0,
                    "input_is_srgb": True,
                    "output_mode": "above",
                },
                "all",
                progress,
                log,
                lambda: False
            )

    except ResolveConnectionError as e:
        print(f"Error: {e}")
        print("")
        print("Make sure DaVinci Resolve is running with scripting enabled:")
        print("  Preferences > System > General > External scripting using: Local")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        plugin.cleanup()


if __name__ == "__main__":
    main()
