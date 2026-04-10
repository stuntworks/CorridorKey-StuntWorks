# DaVinci Resolve Scripting API Reference

## Key Learnings from CorridorKey Development

### AppendToTimeline - Placing Clips at Specific Positions

The `AppendToTimeline` function supports precise clip placement with these parameters:

```python
clip_info = {
    "mediaPoolItem": imported_clip,  # MediaPoolItem object
    "startFrame": 0,                  # Source start frame
    "endFrame": 100,                  # Source end frame
    "trackIndex": 2,                  # Target track (1, 2, 3...)
    "recordFrame": timeline_position, # WHERE on timeline to place clip
    "mediaType": 1,                   # 1=Video, 2=Audio
}
media_pool.AppendToTimeline([clip_info])
```

**Critical Notes:**
- `recordFrame` is the ABSOLUTE timeline position in frames
- The function won't add to a track occupied by another clip at that position
- Returns list of TimelineItem objects if successful, empty list if failed

### Track Control

```python
# Enable/disable a track
timeline.SetTrackEnable("video", 1, False)  # Disable video track 1
timeline.SetTrackEnable("video", 1, True)   # Enable video track 1

# Check if track is enabled
is_enabled = timeline.GetIsTrackEnabled("video", 1)

# Get track count
count = timeline.GetTrackCount("video")  # or "audio", "subtitle"
```

### Timeline Timecode

```python
# Get current playhead timecode (string like "01:00:05:12")
tc = timeline.GetCurrentTimecode()

# Get timeline start timecode
start_tc = timeline.GetStartTimecode()

# Parse timecode to frames
def tc_to_frames(tc_string, fps):
    parts = tc_string.replace(";", ":").split(":")
    if len(parts) == 4:
        h, m, s, f = [int(p) for p in parts]
        return int(h * 3600 * fps + m * 60 * fps + s * fps + f)
    return 0
```

### Timeline Items (Clips)

```python
# Get clips on a specific track
clips = timeline.GetItemListInTrack("video", 1)  # Track 1

# Clip properties
clip_start = clip.GetStart()       # Timeline start frame
clip_end = clip.GetEnd()           # Timeline end frame
left_offset = clip.GetLeftOffset() # Frames trimmed from source start

# Get source media info
mpi = clip.GetMediaPoolItem()
props = mpi.GetClipProperty()
file_path = props.get("File Path", "")
```

### MediaPool Operations

```python
# Get root folder
root = media_pool.GetRootFolder()

# Create subfolder
new_bin = media_pool.AddSubFolder(root, "FolderName")

# Set current folder
media_pool.SetCurrentFolder(new_bin)

# Import media
imported = media_pool.ImportMedia(["/path/to/file.png"])
# Returns list of MediaPoolItem objects

# Find existing folder
for folder in root.GetSubFolderList():
    if folder.GetName() == "TargetFolder":
        target = folder
        break
```

### Project Settings

```python
# Get frame rate
fps = float(project.GetSetting("timelineFrameRate") or 24)
```

### Fusion UIManager (Studio Only)

```python
import DaVinciResolveScript as dvr
import fusionscript

resolve = dvr.scriptapp("Resolve")
ui = resolve.Fusion().UIManager
disp = fusionscript.UIDispatcher(ui)

# Create window
win = disp.AddWindow({
    "ID": "MyPanel",
    "WindowTitle": "My Tool",
    "Geometry": [100, 100, 400, 300],  # x, y, width, height
}, [
    ui.VGroup({"Spacing": 10, "Margin": 15}, [
        ui.Label({"Text": "Hello", "ID": "MyLabel"}),
        ui.Button({"Text": "Click Me", "ID": "MyBtn"}),
        ui.Slider({"ID": "MySlider", "Minimum": 0, "Maximum": 100}),
        ui.ComboBox({"ID": "MyCombo"}),
        ui.CheckBox({"ID": "MyCheck", "Text": "Enable", "Checked": True}),
        ui.SpinBox({"ID": "MySpin", "Minimum": 0, "Maximum": 1000}),
        ui.TextEdit({"ID": "MyLog", "ReadOnly": True}),
    ])
])

# Get UI items
items = win.GetItems()
items["MyCombo"].AddItem("Option 1")
items["MyCombo"].AddItem("Option 2")

# Event handlers
def on_button_click(ev):
    items["MyLabel"].Text = "Clicked!"

def on_slider_changed(ev):
    value = items["MySlider"].Value

win.On.MyBtn.Clicked = on_button_click
win.On.MySlider.SliderMoved = on_slider_changed
win.On.MyPanel.Close = lambda ev: disp.ExitLoop()

# Run
win.Show()
disp.RunLoop()
win.Hide()
```

### Page Navigation

```python
resolve.OpenPage("fusion")  # Switch to Fusion page
resolve.OpenPage("edit")    # Switch to Edit page
resolve.OpenPage("color")   # Switch to Color page
```

## Common Issues & Solutions

### Issue: Clip not placed at correct position
**Solution:** Use `recordFrame` parameter with absolute timeline frame number

### Issue: Popups appear behind Resolve window
**Solution:** Use UIManager labels/status instead of message boxes

### Issue: Module not found (timm, torch, etc.)
**Solution:** Install packages globally or add venv to sys.path:
```python
import site
site.addsitedir(r"path\to\venv\Lib\site-packages")
```

### Issue: Script not appearing in Scripts menu
**Solution:** Place in correct folder:
- `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\`

## Resources

- [DaVinci Resolve Wiki - Scripting API](https://wiki.dvresolve.com/developer-docs/scripting-api)
- [ResolveDevDoc](https://resolvedevdoc.readthedocs.io/)
- [Blackmagic Forum](https://forum.blackmagicdesign.com/)
- [We Suck Less Forum](https://www.steakunderwater.com/wesuckless/)
