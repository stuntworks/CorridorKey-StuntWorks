"""
CorridorKey - Simple Test Version
"""
import sys
import os
import tempfile
from pathlib import Path

if sys.platform == "win32":
    RESOLVE_MODULES = os.path.join(os.environ.get("PROGRAMDATA", "C:/ProgramData"),
        "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules")
elif sys.platform == "darwin":
    RESOLVE_MODULES = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
else:
    RESOLVE_MODULES = "/opt/resolve/Developer/Scripting/Modules"
if RESOLVE_MODULES not in sys.path:
    sys.path.insert(0, RESOLVE_MODULES)

import DaVinciResolveScript as dvr_script
import fusionscript

resolve = dvr_script.scriptapp("Resolve")
fusion = resolve.Fusion()
ui = fusion.UIManager
disp = fusionscript.UIDispatcher(ui)

pm = resolve.GetProjectManager()
project = pm.GetCurrentProject()
timeline = project.GetCurrentTimeline() if project else None

win = disp.AddWindow({
    "ID": "CKPanel",
    "WindowTitle": "CorridorKey Test",
    "Geometry": [100, 100, 400, 350],
}, [
    ui.VGroup({"Spacing": 10, "Margin": 15}, [
        ui.Label({
            "Text": "CorridorKey Test",
            "Alignment": {"AlignHCenter": True},
            "Font": ui.Font({"PixelSize": 18, "Bold": True}),
            "StyleSheet": "color: #4CAF50;",
        }),
        ui.Label({
            "ID": "Status",
            "Text": "Ready",
            "Alignment": {"AlignHCenter": True},
            "StyleSheet": "color: #FF0; font-size: 20px; font-weight: bold;",
        }),
        ui.Button({
            "ID": "TestBtn",
            "Text": "TEST: Just Read Frame (No AI)",
            "MinimumSize": [0, 50],
            "StyleSheet": "background-color: #FF9800; color: white; font-weight: bold; font-size: 14px;",
        }),
        ui.Button({
            "ID": "FullBtn",
            "Text": "FULL: Process with AI",
            "MinimumSize": [0, 50],
            "StyleSheet": "background-color: #2196F3; color: white; font-weight: bold; font-size: 14px;",
        }),
        ui.TextEdit({
            "ID": "Log",
            "ReadOnly": True,
            "MinimumSize": [0, 120],
            "StyleSheet": "background-color: #000; color: #0f0; font-family: monospace;",
        }),
    ]),
])

items = win.GetItems()

def log(msg):
    print(msg)
    items["Log"].PlainText = (items["Log"].PlainText or "") + msg + "\n"

def on_test(ev):
    """Just test reading a frame - no AI."""
    items["Status"].Text = "TESTING..."

    try:
        import cv2

        if not timeline:
            items["Status"].Text = "ERROR: No timeline"
            return

        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips:
            items["Status"].Text = "ERROR: No clips on Track 1"
            return

        clip = clips[0]
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        file_path = props.get("File Path", "")

        if not file_path:
            items["Status"].Text = "ERROR: No file path"
            return

        cap = cv2.VideoCapture(file_path)
        try:
            ret, frame = cap.read()
        finally:
            cap.release()

        if not ret:
            items["Status"].Text = "ERROR: Can't read video"
            return

        out_dir = Path(tempfile.gettempdir()) / "corridorkey_test"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "test_frame.png"
        cv2.imwrite(str(out_path), frame)

        items["Status"].Text = "SUCCESS!"
        os.startfile(str(out_dir))

    except Exception as e:
        items["Status"].Text = f"ERROR: {str(e)[:30]}"
        log(f"Error: {e}")

def on_full(ev):
    """Full AI processing."""
    items["Status"].Text = "AI PROCESSING..."
    log("Loading AI model (first time may take ~30s)...")

    try:
        import cv2
        import numpy as np

        # Add paths relative to this script
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _ck_root = os.path.dirname(_script_dir)
        for p in [_ck_root, _script_dir]:
            if p not in sys.path:
                sys.path.insert(0, p)

        if not timeline:
            items["Status"].Text = "ERROR: No timeline"
            return

        clips = timeline.GetItemListInTrack("video", 1) or []
        if not clips:
            items["Status"].Text = "ERROR: No clips"
            return

        clip = clips[0]
        mpi = clip.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else {}
        file_path = props.get("File Path", "")

        cap = cv2.VideoCapture(file_path)
        try:
            ret, frame = cap.read()
        finally:
            cap.release()

        if not ret:
            items["Status"].Text = "ERROR: Can't read"
            return

        from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
        from core.alpha_hint_generator import AlphaHintGenerator

        processor = CorridorKeyProcessor(device="cuda")
        try:
            hint_gen = AlphaHintGenerator(screen_type="green")
            alpha_hint = hint_gen.generate_hint(frame)

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            alpha_hint = alpha_hint.astype(np.float32) / 255.0 if alpha_hint.dtype == np.uint8 else alpha_hint

            settings = ProcessingSettings(screen_type="green", despill_strength=0.5)
            result = processor.process_frame(frame_rgb, alpha_hint, settings)

            out_dir = Path(tempfile.gettempdir()) / "corridorkey_preview"
            out_dir.mkdir(exist_ok=True)

            matte = result.get("alpha")
            if matte is not None:
                if len(matte.shape) == 3:
                    matte = matte[:, :, 0]
                cv2.imwrite(str(out_dir / "matte.png"), (matte * 255).astype(np.uint8))

            comp = result.get("comp")
            if comp is not None:
                cv2.imwrite(str(out_dir / "composite.png"),
                           cv2.cvtColor((comp * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
        finally:
            processor.cleanup()
        items["Status"].Text = "DONE!"
        os.startfile(str(out_dir))

    except Exception as e:
        items["Status"].Text = "ERROR!"
        log(f"Error: {e}")

def on_close(ev):
    disp.ExitLoop()

win.On.TestBtn.Clicked = on_test
win.On.FullBtn.Clicked = on_full
win.On.CKPanel.Close = on_close

log("Ready!")
log("Click TEST first to verify setup.")
win.Show()
disp.RunLoop()
win.Hide()
