# CorridorKey Development Notes

## Lessons Learned

### 1. DaVinci Resolve Script Location
Scripts must be placed in the correct folder to appear in the Scripts menu:
```
C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\
```
NOT `Fusion\Utility\` (missing Scripts folder).

### 2. AppendToTimeline Parameters
The `recordFrame` parameter is the key to placing clips at specific positions:
```python
media_pool.AppendToTimeline([{
    "mediaPoolItem": clip,
    "trackIndex": 2,
    "recordFrame": timeline_frame,  # THIS IS CRITICAL
    "startFrame": 0,
    "endFrame": duration,
    "mediaType": 1,
}])
```
Without `recordFrame`, clips append to the END of the timeline.

### 3. Popups Appear Behind Window
Windows message boxes (`ctypes.windll.user32.MessageBoxW`) appear BEHIND DaVinci Resolve.
**Solution:** Use UIManager labels for status updates instead of popups.

### 4. Module Import Issues
Resolve uses its own Python, not your venv. To use venv packages:
```python
import site
site.addsitedir(r"path\to\venv\Lib\site-packages")
sys.path.insert(0, venv_packages)
```
Or install packages globally: `pip install timm torch opencv-python`

### 5. Track Numbering
Track numbers start at 1, not 0:
- Track 1 = first video track
- Track 2 = second video track (above Track 1)

### 6. Timecode Parsing
Timecode format: `HH:MM:SS:FF` or `HH:MM:SS;FF` (drop frame)
```python
def tc_to_frames(tc, fps):
    parts = tc.replace(";", ":").split(":")
    h, m, s, f = [int(p) for p in parts]
    return int(h * 3600 * fps + m * 60 * fps + s * fps + f)
```

### 7. Clip Frame Calculation
To get the correct source frame from timeline position:
```python
source_start = clip.GetLeftOffset()  # Trimmed frames
frame_offset = timeline_frame - clip.GetStart()
source_frame = source_start + frame_offset
```

### 8. Image Sequence Import
Import first file of sequence - Resolve auto-detects the sequence:
```python
imported = media_pool.ImportMedia([first_file_path])
```

### 9. UIManager Event Binding
Event names must match widget IDs exactly:
```python
win.On.MyButtonID.Clicked = handler_function
win.On.MySliderID.SliderMoved = slider_handler
win.On.WindowID.Close = close_handler
```

### 10. Global Variables for Cancel
Use global variable for cancel flag in batch processing:
```python
processing_cancelled = False

def on_process_all(ev):
    global processing_cancelled
    processing_cancelled = False
    # ... processing loop checks processing_cancelled

def on_cancel(ev):
    global processing_cancelled
    processing_cancelled = True
```

## Debugging Tips

1. **Print to console**: `print()` statements appear in Resolve's console
2. **Log to UI**: Add TextEdit widget and append messages
3. **Check script syntax**: Run with Python directly before loading in Resolve
4. **Restart Resolve**: Scripts are cached; restart to reload changes

## GitHub Resources Used

- [FLIP Fluids](https://github.com/rlguy/Blender-FLIP-Fluids) - Addon architecture inspiration
- [DaVinci Resolve API Docs](https://github.com/leoweyr/DaVinci_Resolve_API_Docs)
- [DaVinci Resolve Wiki](https://wiki.dvresolve.com/developer-docs/scripting-api)

## Build Process

The plugin is generated using `write_plugin.py`:
```bash
python "D:/New AI Projects/CorridorKey/write_plugin.py"
```
This writes to the Resolve Scripts folder directly.

## Testing Checklist

- [ ] Script appears in Workspace > Scripts menu
- [ ] Window opens without errors
- [ ] Settings controls work (sliders, combos, checkboxes)
- [ ] Single frame processing works
- [ ] Result placed on correct track/position
- [ ] Batch processing with progress
- [ ] Cancel button stops processing
- [ ] Track 1 disable works
- [ ] Toggle Track 1 works
