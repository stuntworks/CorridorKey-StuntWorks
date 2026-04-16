# CorridorKey Viewer — Style Guide

**For any AI or developer working on preview_viewer.py**

---

## Design Language

**Cinematic Studio Dark.** Think DaVinci Resolve color wheels meets Bloomberg terminal.
Dark, dense, precise. No default-looking widgets. This tool lives in a professional
film studio — it should look like it belongs next to Resolve and Nuke.

---

## Color Palette

| Name | Hex | Where |
|------|-----|-------|
| Base background | `#141414` | Window, all panels |
| Surface | `#1e1e1e` | Inactive buttons, input fields, cards |
| Elevated | `#282828` | Hover states, active surfaces |
| Image pane bg | `#000000` | Behind the preview image (pure black — content is king) |
| Border subtle | `#1e1e1e` | Image pane borders, dividers |
| Text primary | `#e8e8e8` | Main text, active labels |
| Text secondary | `#888888` | Inactive button text, labels |
| Text muted | `#555555` | Disabled text, hints, BG label |
| Accent green | `#00C853` | Title, active slider fill, status values, "done" states |
| Accent purple | `#9C27B0` | Composite mode button (active) |
| Accent blue | `#2979FF` | Foreground mode button (active) |
| Accent orange | `#FF9100` | Matte mode button (active) |
| Accent slate | `#607D8B` | Original mode button (active) |
| Accent red | `#FF3D00` | Error text |
| Overlay bg | `rgba(0,0,0,180)` | Re-keying overlay |

**Rules:**
- No pure white (`#FFFFFF`) anywhere — use `#e8e8e8` max
- No color above `#282828` for backgrounds — the image is the star
- Mode buttons are ONLY colored when active; inactive = `#1e1e1e` + `#888` text

---

## Typography

| Element | Font | Size | Weight | Style |
|---------|------|------|--------|-------|
| Window title | Inter / SF Pro / Segoe UI | 14px | 700 | UPPERCASE, letter-spacing 1.5px |
| Mode buttons | Inter / SF Pro / Segoe UI | 11px | 600 | Normal case |
| BG buttons | Inter / SF Pro / Segoe UI | 9px | 400 | UPPERCASE |
| Slider labels | Inter / SF Pro / Segoe UI | 10px | 600 | Normal case, color #888 |
| Slider values | JetBrains Mono / SF Mono / Consolas | 11px | 500 | Monospace, color #00C853 |
| Status bar | JetBrains Mono / SF Mono / Consolas | 10px | 400 | Monospace, color #555 |
| Overlay text | Inter / SF Pro / Segoe UI | 22px | 700 | Color #00C853 |

**Font stack (always in this order):**
```
Sans:  'Inter', 'SF Pro Display', 'Segoe UI', sans-serif
Mono:  'JetBrains Mono', 'SF Mono', 'Consolas', monospace
```

---

## Components

### Mode Buttons (Original / Composite / Foreground / Matte)
- Shape: pill (border-radius 12px)
- Height: ~28px (padding 5px 12px)
- Spacing between buttons: 4px
- **Inactive:** bg `#1e1e1e`, text `#888`
- **Active:** bg = mode color (see palette), text `#fff`
- **Hover (inactive):** bg `#282828`, text `#e8e8e8`
- No border on any state

### Split Toggle
- Same pill shape as mode buttons
- Inactive: bg `#1e1e1e`, text `#555`
- Active (checked): bg `#333`, text `#e8e8e8`

### Background Buttons (Checker / Black / White / V1)
- Smaller pills: border-radius 10px, padding 3px 8px
- Font: 9px uppercase
- Inactive: bg `#1e1e1e`, text `#666`
- Active (checked): visually matches — checkable state

### Sliders (Qt QSlider — when added to viewer)
- Track: 3px height, bg `#1e1e1e`, filled portion `#00C853`
- Thumb: 12px circle, `#00C853`, 1px border `#333`
- Hover: thumb scales 1.1x, subtle green glow
- Label left (10px, `#888`), monospace value right (11px, `#00C853`)

### Image Pane
- Background: pure `#000000`
- Border: 1px solid `#1e1e1e`
- Min size: 320x240 (single pane), 200x150 (split pane left)
- Zoom: scroll wheel 1x-10x, click-drag pan when zoomed

### Processing Overlay
- Covers entire image pane
- Background: `rgba(0,0,0,180)`
- Text: "Re-keying..." in 22px bold `#00C853`, centered
- No timer countdown (feels faster without it)
- Hides immediately when processing completes

### Status Bar
- Position: bottom of window
- Font: monospace 10px, color `#555`
- Shows: mode, despill value, despeckle state, meanRGB, render time
- No border, transparent background

---

## Layout

### Default (single pane)
```
+------------------------------------------+
| CORRIDORKEY LIVE PREVIEW                 |  <- #00C853, 14px bold uppercase
| [Original][Composite][FG][Matte] [Split] |  <- pills, 4px gap
| BG: [Checker][Black][White][V1]          |  <- smaller pills
|------------------------------------------|
|                                          |
|              [ IMAGE ]                   |  <- black bg, fills space
|              (scroll zoom, drag pan)     |
|                                          |
|------------------------------------------|
| Composite | despill=0.50 @400 | (R,G,B)  |  <- monospace status
+------------------------------------------+
```

### Split mode (two panes)
```
+------------------------------------------+
| [Original][Composite][FG][Matte] [Split] |
|------------------------------------------|
|           |                              |
| ORIGINAL  |  PROCESSED                   |
|           |  (mode applies here)         |
|           |                              |
|------------------------------------------|
| status bar                               |
+------------------------------------------+
```

### Window sizing
- Single pane default: `disp_w + 24` wide, `disp_h + 140` tall
- Minimum: 360x300
- Split adds `disp_w` to width
- Resizable — scales image to fill

---

## Spacing
- Layout margins: 10px horizontal, 8px vertical
- Layout spacing: 6px between rows
- Mode button row spacing: 4px
- BG button row spacing: 3px
- Image pane border: 1px

---

## What This Style Is NOT
- Not a web app — no rounded cards, no shadows, no gradients on buttons
- Not macOS native — no vibrancy, no translucency
- Not Resolve's exact theme — we match the FEEL, not the pixel grid
- Not colorful when idle — color appears only on active states and the accent green
