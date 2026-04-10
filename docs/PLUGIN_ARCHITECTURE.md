# CorridorKey DaVinci Resolve Plugin Architecture

## Overview

CorridorKey is a neural network-based green screen keyer integrated directly into DaVinci Resolve Studio via the Fusion UIManager API.

## File Structure

```
D:/New AI Projects/CorridorKey/
├── core/
│   ├── corridorkey_processor.py   # Main AI processing engine
│   └── alpha_hint_generator.py    # HSV-based green/blue screen detection
├── resolve_plugin/
│   ├── ui/
│   │   └── uimanager_panel.py     # Full panel (alternative version)
│   └── install.py                  # Installer script
├── docs/
│   ├── RESOLVE_API_REFERENCE.md   # API documentation
│   └── PLUGIN_ARCHITECTURE.md     # This file
├── write_plugin.py                 # Script to generate plugin file
└── .venv/                          # Python virtual environment

Installed to:
C:/ProgramData/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/CorridorKey.py
```

## Plugin Features

### Settings Panel
- **Screen Type**: Green Screen / Blue Screen
- **Despill Strength**: 0-100% slider
- **Refiner Strength**: 0-100% slider
- **Auto Despeckle**: Checkbox + minimum size (px)
- **Input Gamma**: sRGB (Video/PNG) / Linear (EXR)
- **Output Mode**: Track 2 / MediaPool Only / Fusion Comp
- **Disable Track 1**: Auto-disable source after processing

### Actions
- **SHOW PREVIEW**: Process frame and show original vs keyed side-by-side (Tkinter popup)
- **PROCESS FRAME AT PLAYHEAD**: Single frame processing + import to timeline
- **PROCESS ALL**: Batch process entire clip
- **CANCEL**: Stop batch processing
- **TOGGLE TRACK 1**: Show/hide source for comparison
- **OPEN FUSION**: Switch to Fusion page

### Preview Window
The SHOW PREVIEW button opens a Tkinter popup showing:
- Left: Original frame
- Right: Keyed result with checkerboard background (shows transparency)
- This allows tuning settings before committing to timeline

### Workflow
1. User places green screen clip on Track 1
2. User adjusts settings (despill, screen type, etc.)
3. User moves playhead to desired frame
4. Click SHOW PREVIEW to check result (adjust settings if needed)
5. Click PROCESS FRAME or PROCESS ALL
5. Plugin:
   - Reads frame from source video
   - Generates alpha hint (simple chroma key)
   - Runs CorridorKey neural network
   - Saves RGBA PNG with transparency
   - Imports to MediaPool (CorridorKey bin)
   - Places on Track 2 at correct position
   - Optionally disables Track 1

## Key Implementation Details

### Path Setup
```python
venv_packages = r"D:\New AI Projects\CorridorKey\.venv\Lib\site-packages"
site.addsitedir(venv_packages)
sys.path.insert(0, venv_packages)
sys.path.insert(0, r"D:\New AI Projects\CorridorKey")
sys.path.insert(0, r"D:\New AI Projects\CorridorKey\resolve_plugin")
```

### Frame Extraction
```python
# Calculate source frame from timeline position
source_start = clip.GetLeftOffset()  # Frames trimmed from start
frame_offset = current_frame - clip_start
frame_num = source_start + frame_offset

# Read with OpenCV
cap = cv2.VideoCapture(file_path)
cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
ret, frame = cap.read()
cap.release()
```

### AI Processing
```python
from core.corridorkey_processor import CorridorKeyProcessor, ProcessingSettings
from core.alpha_hint_generator import AlphaHintGenerator

processor = CorridorKeyProcessor(device="cuda")
hint_gen = AlphaHintGenerator(screen_type="green")

# Generate hint and process
alpha_hint = hint_gen.generate_hint(frame)
frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
alpha_hint = alpha_hint.astype(np.float32) / 255.0

settings = ProcessingSettings(screen_type="green", despill_strength=0.5)
result = processor.process_frame(frame_rgb, alpha_hint, settings)

# Get outputs
fg = result.get("fg")      # Foreground RGB
matte = result.get("alpha") # Alpha channel
```

### Timeline Placement
```python
clip_info = {
    "mediaPoolItem": imported[0],
    "startFrame": 0,
    "endFrame": frame_count,
    "trackIndex": 2,
    "recordFrame": clip_start,  # Place at source clip position
    "mediaType": 1,
}
media_pool.AppendToTimeline([clip_info])
```

### Track Control
```python
# Disable Track 1 to show only keyed result
timeline.SetTrackEnable("video", 1, False)

# Toggle for comparison
current = timeline.GetIsTrackEnabled("video", 1)
timeline.SetTrackEnable("video", 1, not current)
```

## Performance Notes

- First run: ~30 seconds (model loading)
- Subsequent frames: ~2-5 seconds each (RTX 5090)
- Batch processing keeps model loaded for efficiency
- Progress shows: frame count, fps, ETA

## Future Improvements

- [x] Preview window before processing (DONE)
- [ ] Presets (save/load settings)
- [ ] Edge refinement controls
- [ ] Proxy generation
- [ ] Background processing (non-blocking UI)
- [ ] Fusion node integration
