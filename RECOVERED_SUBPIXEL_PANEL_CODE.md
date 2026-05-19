# CorridorKey — Sub-Pixel Margin/Soften Recovery File
# Written: 2026-04-26 | Session handoff document

---

## SITUATION SUMMARY

The sub-pixel (0.1 px step) Mask Margin and Soften controls were NEVER written
to the live file. What exists in the live file is INTEGER controls (0-80 / 0-20).

**write_plugin.py embeds an EVEN OLDER version with NO margin/soften at all.**
Running `python write_plugin.py` will NUKE the current live file.

THIS SESSION will fix all three issues:
1. Implement sub-pixel controls
2. Sync resolve_plugin/CorridorKey_Pro.py with live file
3. Fix write_plugin.py to read from source file instead of embedding

---

## FILE: C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py

### CURRENT STATE (INTEGER — what is actually in the file right now)

**Lines ~271-289 (UI definition):**
```python
ui.HGroup({"Weight": 0, "Spacing": 6}, [
    ui.Label({"Text": "Mask Margin:", "Weight": 0, "StyleSheet": "color: #aaa; font-size: 11px;"}),
    ui.Slider({"ID": "Sam2Margin", "Minimum": 0, "Maximum": 80, "Value": 1, "Weight": 3,
               "Orientation": "Horizontal", "SingleStep": 1,
               "StyleSheet": "..."}),
    ui.SpinBox({"ID": "Sam2MarginInput", "Minimum": 0, "Maximum": 80, "Value": 1, "Weight": 0,
                "StyleSheet": "..."}),
    ui.Label({"Text": "px", "Weight": 0, "StyleSheet": "color: #888; font-size: 11px;"}),
]),

ui.HGroup({"Weight": 0, "Spacing": 6}, [
    ui.Label({"Text": "Soften:", "Weight": 0, "StyleSheet": "color: #aaa; font-size: 11px;"}),
    ui.Slider({"ID": "Sam2Soften", "Minimum": 0, "Maximum": 20, "Value": 1, "Weight": 3,
               "Orientation": "Horizontal", "SingleStep": 1,
               "StyleSheet": "..."}),
    ui.SpinBox({"ID": "Sam2SoftenInput", "Minimum": 0, "Maximum": 20, "Value": 1, "Weight": 0,
                "StyleSheet": "..."}),
    ui.Label({"Text": "px", "Weight": 0, "StyleSheet": "color: #888; font-size: 11px;"}),
]),
```

**Line ~470 (get_settings):**
```python
"sam2_margin": int(items["Sam2Margin"].Value),
"sam2_soften": int(items["Sam2Soften"].Value),
```

**Lines ~3227-3270 (handler functions):**
```python
def on_sam2_margin_changed(ev):
    global _syncing_margin
    if _syncing_margin: return
    _syncing_margin = True
    try: items["Sam2MarginInput"].Value = items["Sam2Margin"].Value
    except Exception: pass
    _syncing_margin = False
    _write_live_params_slider({"sam2_margin": int(items["Sam2Margin"].Value)})

def on_sam2_margin_input(ev):
    global _syncing_margin
    if _syncing_margin: return
    _syncing_margin = True
    try: items["Sam2Margin"].Value = items["Sam2MarginInput"].Value
    except Exception: pass
    _syncing_margin = False
    _write_live_params_slider({"sam2_margin": int(items["Sam2Margin"].Value)})

win.On.Sam2Margin.ValueChanged       = on_sam2_margin_changed
win.On.Sam2MarginInput.ValueChanged  = on_sam2_margin_input

_syncing_soften = False
def on_soften_changed(ev):
    global _syncing_soften
    if _syncing_soften: return
    _syncing_soften = True
    try: items["Sam2SoftenInput"].Value = items["Sam2Soften"].Value
    except Exception: pass
    _syncing_soften = False
    _write_live_params_slider({"sam2_soften": int(items["Sam2Soften"].Value)})

def on_soften_input(ev):
    global _syncing_soften
    if _syncing_soften: return
    _syncing_soften = True
    try: items["Sam2Soften"].Value = items["Sam2SoftenInput"].Value
    except Exception: pass
    _syncing_soften = False
    _write_live_params_slider({"sam2_soften": int(items["Sam2Soften"].Value)})

win.On.Sam2Soften.ValueChanged      = on_soften_changed
win.On.Sam2SoftenInput.ValueChanged = on_soften_input
```

**Render path — _dilate_sam2_mask / _soften_sam2_mask (int inputs, lines ~651-652 and ~1978-1979 and ~2542-2543 and ~2750-2751):**
```python
mask = _dilate_sam2_mask(mask, margin=settings.get("sam2_margin", SAM2_MATTE_MARGIN))
mask = _soften_sam2_mask(mask, soften=settings.get("sam2_soften", 0))
```
These already accept float — no render-path change needed for sub-pixel.

---

## SUB-PIXEL TARGET (what needs to be implemented)

**Approach: Slider 0-800 internal / ÷10 = 0.0-80.0 px display for margin**
**Approach: Slider 0-200 internal / ÷10 = 0.0-20.0 px display for soften**

Fusion `ui.Slider` is integer-only. Sub-pixel is achieved by multiplying range by 10
and dividing by 10 at read time.

### UI changes:
```python
# Mask Margin: 0-800 (÷10 = 0.0-80.0 px)
ui.Slider({"ID": "Sam2Margin", "Minimum": 0, "Maximum": 800, "Value": 10, ...})
ui.SpinBox({"ID": "Sam2MarginInput", "Minimum": 0, "Maximum": 800, "Value": 10, ...})
ui.Label({"Text": "px ÷10", ...})

# Soften: 0-200 (÷10 = 0.0-20.0 px)
ui.Slider({"ID": "Sam2Soften", "Minimum": 0, "Maximum": 200, "Value": 10, ...})
ui.SpinBox({"ID": "Sam2SoftenInput", "Minimum": 0, "Maximum": 200, "Value": 10, ...})
ui.Label({"Text": "px ÷10", ...})
```

### get_settings() change:
```python
"sam2_margin": float(items["Sam2Margin"].Value) / 10.0,
"sam2_soften": float(items["Sam2Soften"].Value) / 10.0,
```

### Handler change (÷10 in _write_live_params_slider calls):
```python
_write_live_params_slider({"sam2_margin": float(items["Sam2Margin"].Value) / 10.0})
_write_live_params_slider({"sam2_soften": float(items["Sam2Soften"].Value) / 10.0})
```

### Render path — NO CHANGE NEEDED:
`_dilate_sam2_mask` and `_soften_sam2_mask` already accept float margin/soften.
The cv2 kernel size calc (`sz = int(margin) * 2 + 1`) handles sub-pixel by truncating
to nearest pixel for the kernel, which is correct behavior.

---

## CRITICAL: write_plugin.py OVERWRITE BUG

`D:\New AI Projects\CorridorKey\write_plugin.py` embeds CorridorKey.py as a hardcoded
string starting at line 4: `content = r'''...'''`

The embedded version has NO Sam2Margin or Sam2Soften at all.
Running write_plugin.py DESTROYS all margin/soften changes.

### Fix: Change write_plugin.py to read from source file
Replace the embedded `content = r'''...'''` block with:
```python
import os as _os
_source = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                        "resolve_plugin", "CorridorKey_Pro.py")
with open(_source, "r", encoding="utf-8") as _f:
    content = _f.read()
```
Then keep `resolve_plugin/CorridorKey_Pro.py` as the canonical source.

---

## WORKFLOW AFTER THIS SESSION

1. Always edit `resolve_plugin/CorridorKey_Pro.py` — it is the source of truth
2. Run `python write_plugin.py` to install to Resolve
3. NEVER edit `C:\ProgramData\...Utility\CorridorKey.py` directly
