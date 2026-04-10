#!/usr/bin/env python3
"""Write the CorridorKey plugin file"""

content = r'''
"""CorridorKey - Neural Green Screen for DaVinci Resolve"""
import sys
import os
import site
import tempfile
import threading
from pathlib import Path

# Find CorridorKey installation
def _find_corridorkey_root():
    # 1. Environment variable
    env_root = os.environ.get("CORRIDORKEY_ROOT")
    if env_root and os.path.isdir(env_root):
        return env_root
    # 2. Config file next to this script (Resolve may not define __file__)
    script_dirs = []
    try:
        script_dirs.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    # Also check known Resolve script locations
    if sys.platform == "win32":
        script_dirs.append(os.path.join(os.environ.get("PROGRAMDATA", "C:/ProgramData"),
            "Blackmagic Design", "DaVinci Resolve", "Fusion", "Scripts", "Utility"))
    elif sys.platform == "darwin":
        script_dirs.append("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility")
    else:
        script_dirs.append("/opt/resolve/Fusion/Scripts/Utility")
    for sd in script_dirs:
        config_path = os.path.join(sd, "corridorkey_path.txt")
        if os.path.exists(config_path):
            with open(config_path) as f:
                root = f.read().strip()
                if os.path.isdir(root):
                    return root
    # 3. Check if we're running from within the repo
    for sd in script_dirs:
        for candidate in [sd, os.path.dirname(sd)]:
            if os.path.exists(os.path.join(candidate, "resolve_plugin", "core")):
                return candidate
    raise RuntimeError("CorridorKey not found. Set CORRIDORKEY_ROOT env var or create corridorkey_path.txt")

CORRIDORKEY_ROOT = _find_corridorkey_root()
venv_packages = os.path.join(CORRIDORKEY_ROOT, ".venv", "Lib", "site-packages")
site.addsitedir(venv_packages)
sys.path.insert(0, venv_packages)
# Add Resolve scripting modules
if sys.platform == "win32":
    _resolve_modules = os.path.join(os.environ.get("PROGRAMDATA", "C:/ProgramData"),
        "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules")
elif sys.platform == "darwin":
    _resolve_modules = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
else:
    _resolve_modules = "/opt/resolve/Developer/Scripting/Modules"
sys.path.insert(0, _resolve_modules)
sys.path.insert(0, CORRIDORKEY_ROOT)
sys.path.insert(0, os.path.join(CORRIDORKEY_ROOT, "resolve_plugin"))

import fusionscript

# Use fu provided by Resolve's script runner (dvr.scriptapp hangs in newer Resolve)
resolve = fu.GetResolve()
ui = fu.UIManager
disp = fusionscript.UIDispatcher(ui)

pm = resolve.GetProjectManager()
project = pm.GetCurrentProject()
media_pool = project.GetMediaPool() if project else None
timeline = project.GetCurrentTimeline() if project else None

# Store last processed result for preview
last_preview_data = {"original": None, "keyed": None, "alpha": None}

win = disp.AddWindow({"ID": "CK", "WindowTitle": "CorridorKey", "Geometry": [100, 100, 420, 620]}, [
    ui.VGroup({"Spacing": 8, "Margin": 12}, [
        ui.Label({
            "Text": "CorridorKey Neural Green Screen",
            "Alignment": {"AlignHCenter": True},
            "Font": ui.Font({"PixelSize": 16, "Bold": True}),
            "StyleSheet": "color: #4CAF50;",
        }),
        ui.HGroup({"Spacing": 10}, [
            ui.Label({"Text": "Screen Type:", "MinimumSize": [90, 0]}),
            ui.ComboBox({"ID": "ScreenType"}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.Label({"Text": "Despill:", "MinimumSize": [90, 0]}),
            ui.Slider({"ID": "DespillSlider", "Minimum": 0, "Maximum": 100, "Value": 50, "Orientation": "Horizontal"}),
            ui.Label({"ID": "DespillValue", "Text": "0.50", "MinimumSize": [35, 0]}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.Label({"Text": "Refiner:", "MinimumSize": [90, 0]}),
            ui.Slider({"ID": "RefinerSlider", "Minimum": 0, "Maximum": 100, "Value": 100, "Orientation": "Horizontal"}),
            ui.Label({"ID": "RefinerValue", "Text": "1.00", "MinimumSize": [35, 0]}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.CheckBox({"ID": "DespeckleCheck", "Text": "Auto Despeckle", "Checked": True}),
            ui.SpinBox({"ID": "DespeckleSize", "Minimum": 50, "Maximum": 1000, "Value": 400, "SingleStep": 50}),
            ui.Label({"Text": "px min"}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.Label({"Text": "Input Gamma:", "MinimumSize": [90, 0]}),
            ui.ComboBox({"ID": "InputGamma"}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.Label({"Text": "Output:", "MinimumSize": [90, 0]}),
            ui.ComboBox({"ID": "OutputMode"}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.CheckBox({"ID": "DisableTrack1", "Text": "Disable Track 1 after processing", "Checked": True}),
        ]),
        ui.Label({"ID": "Status", "Text": "Ready - Put clip on Track 1", "Alignment": {"AlignHCenter": True}, "StyleSheet": "color: #0FF; font-size: 16px; font-weight: bold;", "MinimumSize": [0, 30]}),
        ui.Button({"ID": "ShowPreview", "Text": "SHOW PREVIEW", "MinimumSize": [0, 50], "StyleSheet": "background: #9C27B0; color: white; font-size: 14px; font-weight: bold;"}),
        ui.Button({"ID": "ProcessFrame", "Text": "PROCESS FRAME AT PLAYHEAD", "MinimumSize": [0, 40], "StyleSheet": "background: #2196F3; color: white; font-size: 13px; font-weight: bold;"}),
        ui.HGroup({"Spacing": 10}, [
            ui.Button({"ID": "ProcessAll", "Text": "PROCESS ALL", "MinimumSize": [0, 40], "StyleSheet": "background: #4CAF50; color: white; font-weight: bold;"}),
            ui.Button({"ID": "Cancel", "Text": "CANCEL", "MinimumSize": [0, 40], "StyleSheet": "background: #f44336; color: white; font-weight: bold;"}),
        ]),
        ui.HGroup({"Spacing": 10}, [
            ui.Button({"ID": "ToggleTrack1", "Text": "TOGGLE TRACK 1", "MinimumSize": [0, 30], "StyleSheet": "background: #607D8B; color: white;"}),
            ui.Button({"ID": "OpenFusion", "Text": "OPEN FUSION", "MinimumSize": [0, 30], "StyleSheet": "background: #FF9800; color: white;"}),
        ]),
        ui.TextEdit({"ID": "Log", "ReadOnly": True, "MinimumSize": [0, 80], "StyleSheet": "background: #111; color: #0f0; font-family: monospace; font-size: 10px;"}),
    ])
])

items = win.GetItems()
items["ScreenType"].AddItem("Green Screen")
items["ScreenType"].AddItem("Blue Screen")
items["InputGamma"].AddItem("sRGB (Video/PNG)")
items["InputGamma"].AddItem("Linear (EXR)")
items["OutputMode"].AddItem("Track 2 (Above Source)")
items["OutputMode"].AddItem("MediaPool Only")
items["OutputMode"].AddItem("Fusion Comp")

def log(msg):
    print(msg)
    current = items["Log"].PlainText or ""
    items["Log"].PlainText = current + msg + "\n"

def status(msg):
    items["Status"].Text = msg

def get_settings():
    return {
        "screen_type": "green" if items["ScreenType"].CurrentIndex == 0 else "blue",
        "despill_strength": items["DespillSlider"].Value / 100.0,
        "refiner_strength": items["RefinerSlider"].Value / 100.0,
        "despeckle_enabled": items["DespeckleCheck"].Checked,
        "despeckle_size": items["DespeckleSize"].Value,
        "input_is_srgb": items["InputGamma"].CurrentIndex == 0,
        "output_mode": items["OutputMode"].CurrentIndex,
    }

def on_despill_changed(ev):
    value = items["DespillSlider"].Value / 100.0
    items["DespillValue"].Text = f"{value:.2f}"

def on_refiner_changed(ev):
    value = items["RefinerSlider"].Value / 100.0
    items["RefinerValue"].Text = f"{value:.2f}"


def create_checkerboard(height, width, square_size=20):
    import numpy as np
    checker = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        for x in range(width):
            if ((x // square_size) + (y // square_size)) % 2 == 0:
                checker[y, x] = [180, 180, 180]
            else:
                checker[y, x] = [120, 120, 120]
    return checker

def composite_over_checker(fg_rgb, alpha, checker_size=20):
    import numpy as np
    h, w = fg_rgb.shape[:2]
    checker = create_checkerboard(h, w, checker_size)
    if len(alpha.shape) == 3:
        alpha = alpha[:, :, 0]
    alpha_3ch = np.stack([alpha, alpha, alpha], axis=2)
    result = (fg_rgb * alpha_3ch + checker * (1 - alpha_3ch)).astype(np.uint8)
    return result

def show_preview_window(original_bgr, keyed_rgb, alpha):
    import threading
    def run_preview():
        import tkinter as tk
        from PIL import Image, ImageTk
        import numpy as np
        import cv2
        orig_rgb = cv2.cvtColor(original_bgr, cv2.COLOR_BGR2RGB)
        keyed_checker = composite_over_checker(keyed_rgb, alpha)
        h, w = orig_rgb.shape[:2]
        max_w = 700
        if w > max_w:
            scale = max_w / w
            new_w = int(w * scale)
            new_h = int(h * scale)
            orig_rgb = cv2.resize(orig_rgb, (new_w, new_h))
            keyed_checker = cv2.resize(keyed_checker, (new_w, new_h))
        preview_win = tk.Tk()
        preview_win.title("CorridorKey Preview - Original vs Keyed")
        preview_win.configure(bg="#1a1a1a")
        frame = tk.Frame(preview_win, bg="#1a1a1a")
        frame.pack(padx=10, pady=10)
        orig_label_text = tk.Label(frame, text="ORIGINAL", fg="white", bg="#1a1a1a", font=("Arial", 12, "bold"))
        orig_label_text.grid(row=0, column=0, pady=(0, 5))
        keyed_label_text = tk.Label(frame, text="KEYED (transparency)", fg="#4CAF50", bg="#1a1a1a", font=("Arial", 12, "bold"))
        keyed_label_text.grid(row=0, column=1, pady=(0, 5))
        orig_pil = Image.fromarray(orig_rgb)
        keyed_pil = Image.fromarray(keyed_checker)
        orig_tk = ImageTk.PhotoImage(orig_pil)
        keyed_tk = ImageTk.PhotoImage(keyed_pil)
        orig_img_label = tk.Label(frame, image=orig_tk, bg="#1a1a1a")
        orig_img_label.grid(row=1, column=0, padx=5)
        keyed_img_label = tk.Label(frame, image=keyed_tk, bg="#1a1a1a")
        keyed_img_label.grid(row=1, column=1, padx=5)
        h, w = original_bgr.shape[:2]
        info_label = tk.Label(preview_win, text=f"Size: {w}x{h} | Checkerboard = transparency", fg="#888", bg="#1a1a1a", font=("Arial", 10))
        info_label.pack(pady=(0, 5))
        close_btn = tk.Button(preview_win, text="Close Preview", command=preview_win.destroy, bg="#f44336", fg="white", font=("Arial", 11, "bold"), padx=20, pady=5)
        close_btn.pack(pady=10)
        preview_win.update_idletasks()
        preview_win.lift()
        preview_win.attributes("-topmost", True)
        preview_win.after(100, lambda: preview_win.attributes("-topmost", False))
        preview_win.mainloop()
    thread = threading.Thread(target=run_preview, daemon=True)
    thread.start()

def process_current_frame(preview_only=False):
    global last_preview_data

    status("PROCESSING...")
    log("=" * 35)
    try:
        import cv2
        import numpy as np
        if not timeline or not media_pool:
            status("ERROR: No timeline!")
            return
        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips:
            status("ERROR: No clips on Track 1!")
            return
        settings = get_settings()
        log(f"Screen: {settings['screen_type']}, Despill: {settings['despill_strength']:.2f}")
        try:
            fps = float(project.GetSetting("timelineFrameRate") or 24)
            tc = timeline.GetCurrentTimecode()
            parts = tc.replace(";", ":").split(":")
            if len(parts) == 4:
                h, m, s, f = [int(p) for p in parts]
                current_frame = int(h * 3600 * fps + m * 60 * fps + s * fps + f)
            else:
                current_frame = 0
            log(f"Playhead: {tc} (frame {current_frame})")
        except Exception as e:
            log(f"Timecode error: {e}")
            current_frame = 0
        clip = None
        clip_start = 0
        for c in clips:
            start = c.GetStart()
            end = c.GetEnd()
            if start <= current_frame < end:
                clip = c
                clip_start = start
                break
        if not clip:
            clip = clips[0]
            clip_start = clip.GetStart()
            log("Using first clip")
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        file_path = props.get("File Path", "")
        log(f"Source: {os.path.basename(file_path)}")
        source_start = clip.GetLeftOffset()
        frame_offset = current_frame - clip_start
        frame_num = source_start + frame_offset
        if frame_num < 0:
            frame_num = 0
        log(f"Source frame: {frame_num}")
        cap = cv2.VideoCapture(file_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            status("ERROR: Cannot read video")
            return
        log(f"Size: {frame.shape[1]}x{frame.shape[0]}")
        log("Loading AI...")
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        from core.alpha_hint_generator import AlphaHintGenerator
        processor = CorridorKeyProcessor(device="cuda")
        log("Processing...")
        hint_gen = AlphaHintGenerator(screen_type=settings["screen_type"])
        alpha_hint = hint_gen.generate_hint(frame)
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        alpha_hint = alpha_hint.astype(np.float32) / 255.0 if alpha_hint.dtype == np.uint8 else alpha_hint
        proc_settings = ProcessingSettings(screen_type=settings["screen_type"], despill_strength=settings["despill_strength"])
        result = processor.process_frame(frame_rgb, alpha_hint, proc_settings)
        out_dir = Path(tempfile.gettempdir()) / "corridorkey_output"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / f"keyed_{current_frame:06d}.png"
        fg = result.get("fg")
        matte = result.get("alpha")
        if fg is not None and matte is not None:
            if len(matte.shape) == 3:
                matte = matte[:, :, 0]
            fg_bgr = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
            alpha_uint8 = (matte * 255).astype(np.uint8)
            rgba = cv2.merge([fg_bgr[:,:,0], fg_bgr[:,:,1], fg_bgr[:,:,2], alpha_uint8])
            cv2.imwrite(str(out_path), rgba)
            log(f"Saved: {out_path.name}")
        else:
            status("ERROR: No output from AI")
            processor.cleanup()
            return
        # matte already squeezed inside the block above
        last_preview_data["original"] = frame.copy()
        last_preview_data["keyed"] = (fg * 255).astype(np.uint8)
        last_preview_data["alpha"] = matte.copy()
        processor.cleanup()
        if preview_only:
            log("Opening preview window...")
            show_preview_window(frame, (fg * 255).astype(np.uint8), matte)
            status("Preview shown - adjust settings")
            return
        output_mode = settings["output_mode"]
        log("Importing...")
        root = media_pool.GetRootFolder()
        ck_bin = None
        for folder in root.GetSubFolderList():
            if folder.GetName() == "CorridorKey":
                ck_bin = folder
                break
        if not ck_bin:
            ck_bin = media_pool.AddSubFolder(root, "CorridorKey")
        media_pool.SetCurrentFolder(ck_bin)
        imported = media_pool.ImportMedia([str(out_path)])
        if not imported:
            status("ERROR: Import failed")
            return
        log("Added to MediaPool")
        if output_mode == 0 or output_mode == 2:
            log("Adding to Track 2...")

            # Get timeline start timecode offset (important for recordFrame!)
            timeline_start_tc = timeline.GetStartTimecode()
            log(f"Timeline starts at: {timeline_start_tc}")

            # Parse timeline start timecode to frames
            try:
                parts = timeline_start_tc.replace(";", ":").split(":")
                if len(parts) == 4:
                    h, m, s, f = [int(p) for p in parts]
                    timeline_start_frame = int(h * 3600 * fps + m * 60 * fps + s * fps + f)
                else:
                    timeline_start_frame = 0
            except:
                timeline_start_frame = 0

            log(f"Timeline start frame: {timeline_start_frame}")

            # Get source clip info
            clip_end = clip.GetEnd()
            clip_duration = clip_end - clip_start
            log(f"Source clip: start={clip_start}, end={clip_end}, dur={clip_duration}")

            # Calculate recordFrame - this is the ABSOLUTE timeline position
            # recordFrame = where we want it on timeline
            record_frame = clip_start
            log(f"Placing at recordFrame: {record_frame}")

            # AppendToTimeline with full parameters
            clip_info = {
                "mediaPoolItem": imported[0],
                "startFrame": 0,
                "endFrame": 1,
                "trackIndex": 2,
                "recordFrame": record_frame,
                "mediaType": 1,
            }
            log(f"clipInfo: trackIndex=2, recordFrame={record_frame}")

            new_clips = media_pool.AppendToTimeline([clip_info])
            log(f"AppendToTimeline returned: {new_clips}")

            if new_clips:
                log("DONE! Clip added to Track 2")
                # Disable Track 1 if option checked
                if items["DisableTrack1"].Checked:
                    timeline.SetTrackEnable("video", 1, False)
                    log("Track 1 disabled")
                if output_mode == 0:
                    status("DONE! Check Track 2")
                else:
                    status("Added - click OPEN FUSION")
            else:
                # Try without recordFrame as fallback
                log("Failed with recordFrame, trying without...")
                new_clips = media_pool.AppendToTimeline([{
                    "mediaPoolItem": imported[0],
                    "startFrame": 0,
                    "endFrame": 1,
                    "trackIndex": 2,
                    "mediaType": 1,
                }])
                if new_clips:
                    log("Added (at end of timeline)")
                    status("Added - may need to move manually")
                else:
                    log("Failed to add to timeline")
                    status("MediaPool only - drag to timeline")
        else:
            status("Done - check MediaPool")
    except Exception as e:
        status("ERROR!")
        log(f"ERROR: {e}")
        import traceback
        log(traceback.format_exc())


def on_show_preview(ev):
    threading.Thread(target=process_current_frame, args=(True,), daemon=True).start()

def on_process_frame(ev):
    threading.Thread(target=process_current_frame, args=(False,), daemon=True).start()

processing_cancelled = False

def _process_all_worker():
    global processing_cancelled
    processing_cancelled = False

    log("=" * 35)
    log("PROCESS ALL FRAMES")

    try:
        import cv2
        import numpy as np
        import time

        if not timeline or not media_pool:
            status("ERROR: No timeline!")
            return

        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips:
            status("ERROR: No clips on Track 1!")
            return

        # Use first clip on Track 1
        clip = clips[0]
        clip_start = clip.GetStart()
        clip_end = clip.GetEnd()
        clip_duration = clip_end - clip_start

        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        file_path = props.get("File Path", "")
        source_start = clip.GetLeftOffset()

        log(f"Source: {os.path.basename(file_path)}")
        log(f"Frames: {clip_duration} ({clip_start} to {clip_end})")

        settings = get_settings()
        fps = float(project.GetSetting("timelineFrameRate") or 24)

        # Create output directory for this clip
        clip_name = Path(file_path).stem
        out_dir = Path(tempfile.gettempdir()) / "corridorkey_output" / clip_name
        out_dir.mkdir(parents=True, exist_ok=True)
        log(f"Output: {out_dir}")

        # Load AI model once
        log("Loading AI model...")
        status("Loading AI...")
        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        from core.alpha_hint_generator import AlphaHintGenerator

        processor = CorridorKeyProcessor(device="cuda")
        hint_gen = AlphaHintGenerator(screen_type=settings["screen_type"])
        proc_settings = ProcessingSettings(
            screen_type=settings["screen_type"],
            despill_strength=settings["despill_strength"]
        )

        # Open video
        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            status("ERROR: Cannot open video")
            processor.cleanup()
            return

        # Process each frame
        start_time = time.time()
        processed = 0
        output_files = []

        for i in range(clip_duration):
            if processing_cancelled:
                log("Cancelled by user")
                break

            # Calculate source frame
            source_frame = source_start + i
            cap.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
            ret, frame = cap.read()

            if not ret:
                log(f"Skip frame {i} - read failed")
                continue

            # Process frame
            alpha_hint = hint_gen.generate_hint(frame)
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            alpha_hint = alpha_hint.astype(np.float32) / 255.0 if alpha_hint.dtype == np.uint8 else alpha_hint

            result = processor.process_frame(frame_rgb, alpha_hint, proc_settings)

            # Save with alpha
            fg = result.get("fg")
            matte = result.get("alpha")
            if fg is not None and matte is not None:
                if len(matte.shape) == 3:
                    matte = matte[:, :, 0]
                fg_bgr = cv2.cvtColor((fg * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
                alpha_uint8 = (matte * 255).astype(np.uint8)
                rgba = cv2.merge([fg_bgr[:,:,0], fg_bgr[:,:,1], fg_bgr[:,:,2], alpha_uint8])

                out_path = out_dir / f"{clip_name}_{i:06d}.png"
                cv2.imwrite(str(out_path), rgba)
                output_files.append(str(out_path))

            processed += 1

            # Update progress
            elapsed = time.time() - start_time
            fps_rate = processed / elapsed if elapsed > 0 else 0
            remaining = (clip_duration - processed) / fps_rate if fps_rate > 0 else 0

            status(f"Frame {processed}/{clip_duration} ({fps_rate:.1f} fps, {remaining:.0f}s left)")

            # Log every 10 frames
            if processed % 10 == 0:
                log(f"Processed {processed}/{clip_duration}")

        cap.release()
        processor.cleanup()

        if not output_files:
            status("ERROR: No frames processed")
            return

        log(f"Processed {len(output_files)} frames in {time.time()-start_time:.1f}s")

        # Import sequence to MediaPool
        log("Importing sequence...")
        status("Importing to MediaPool...")

        root = media_pool.GetRootFolder()
        ck_bin = None
        for folder in root.GetSubFolderList():
            if folder.GetName() == "CorridorKey":
                ck_bin = folder
                break
        if not ck_bin:
            ck_bin = media_pool.AddSubFolder(root, "CorridorKey")

        media_pool.SetCurrentFolder(ck_bin)

        # Import as image sequence (first file)
        imported = media_pool.ImportMedia([output_files[0]])

        if not imported:
            status("ERROR: Import failed")
            return

        log("Imported to MediaPool")

        # Add to timeline
        output_mode = settings["output_mode"]
        if output_mode == 0 or output_mode == 2:
            log("Adding to Track 2...")

            clip_info = {
                "mediaPoolItem": imported[0],
                "startFrame": 0,
                "endFrame": len(output_files),
                "trackIndex": 2,
                "recordFrame": clip_start,
                "mediaType": 1,
            }

            new_clips = media_pool.AppendToTimeline([clip_info])

            if new_clips:
                log("DONE! Sequence on Track 2")
                # Disable Track 1 if option checked
                if items["DisableTrack1"].Checked:
                    timeline.SetTrackEnable("video", 1, False)
                    log("Track 1 disabled")
                status(f"DONE! {len(output_files)} frames on Track 2")
            else:
                status("MediaPool only - drag to timeline")
        else:
            status(f"Done - {len(output_files)} frames in MediaPool")

    except Exception as e:
        status("ERROR!")
        log(f"ERROR: {e}")
        import traceback
        log(traceback.format_exc())

def on_process_all(ev):
    threading.Thread(target=_process_all_worker, daemon=True).start()

def on_cancel(ev):
    global processing_cancelled
    processing_cancelled = True
    log("Cancelling...")

def on_toggle_track1(ev):
    try:
        if timeline:
            # Get current state and toggle
            current = timeline.GetIsTrackEnabled("video", 1)
            timeline.SetTrackEnable("video", 1, not current)
            state = "enabled" if not current else "disabled"
            log(f"Track 1 {state}")
            status(f"Track 1 {state}")
    except Exception as e:
        log(f"Error: {e}")

def on_open_fusion(ev):
    try:
        resolve.OpenPage("fusion")
        log("Opened Fusion page")
        status("Fusion page opened")
    except Exception as e:
        log(f"Error: {e}")
        status("Error opening Fusion")

def on_close(ev):
    disp.ExitLoop()

win.On.DespillSlider.SliderMoved = on_despill_changed
win.On.RefinerSlider.SliderMoved = on_refiner_changed
win.On.ShowPreview.Clicked = on_show_preview
win.On.ProcessFrame.Clicked = on_process_frame
win.On.ProcessAll.Clicked = on_process_all
win.On.Cancel.Clicked = on_cancel
win.On.ToggleTrack1.Clicked = on_toggle_track1
win.On.OpenFusion.Clicked = on_open_fusion
win.On.CK.Close = on_close

log("CorridorKey Ready")
log("Put green screen clip on Track 1")
log("Move playhead, click SHOW PREVIEW")
win.Show()
disp.RunLoop()
win.Hide()

'''

import sys as _sys

# Auto-detect Resolve scripts path
if _sys.platform == "win32":
    import os as _os
    _scripts_base = _os.path.join(_os.environ.get("PROGRAMDATA", "C:/ProgramData"),
        "Blackmagic Design", "DaVinci Resolve", "Fusion", "Scripts", "Utility")
elif _sys.platform == "darwin":
    _scripts_base = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
else:
    _scripts_base = "/opt/resolve/Fusion/Scripts/Utility"

output_path = _os.path.join(_scripts_base, "CorridorKey.py")

# Write the plugin script
_os.makedirs(_os.path.dirname(output_path), exist_ok=True)
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Written to {output_path}")

# Write config file pointing to CorridorKey root
ck_root = _os.path.dirname(_os.path.abspath(__file__))
config_path = _os.path.join(_scripts_base, "corridorkey_path.txt")
with open(config_path, 'w') as f:
    f.write(ck_root)
print(f"Config written to {config_path}")
