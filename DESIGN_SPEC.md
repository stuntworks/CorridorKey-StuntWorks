# CorridorKey StuntWorks — UI Design Specification

**Date:** 2026-04-16
**Status:** MOCKUP — awaiting Berto approval before code changes.

---

## 1. Design Philosophy

**Reference tools:** DaVinci Resolve color wheels, Nuke's Node Graph panel,
Blackmagic's ATEM control surfaces. Dark, dense, precise. No default-looking
sliders, no generic Bootstrap buttons. This tool lives in a film studio — it
should look like it belongs there.

**One visual language, three implementations:**

| Editor | Panel Tech | Styling Tech | Notes |
|--------|-----------|-------------|-------|
| After Effects | CEP HTML | CSS3 | Full control |
| Premiere Pro | CEP HTML | CSS3 | Same panel as AE |
| DaVinci Resolve | Fusion UIManager | Native Resolve theme | Very limited — uses Resolve's own dark chrome. We match by keeping our text/labels consistent, not by overriding Resolve's look. |
| Viewer Window (all editors) | PySide6 Qt | QSS stylesheets | Full control — shared across all three editors |

---

## 2. Color System

### Token Table

| Token | Hex | Usage |
|-------|-----|-------|
| `bg-base` | `#141414` | Panel + viewer base background |
| `bg-surface` | `#1e1e1e` | Cards, sections, input fields |
| `bg-elevated` | `#282828` | Hover states, active sections |
| `border-subtle` | `#333333` | Input borders, dividers |
| `border-active` | `#4a4a4a` | Focus rings, active borders |
| `text-primary` | `#e8e8e8` | Main text, values |
| `text-secondary` | `#888888` | Labels, descriptions |
| `text-muted` | `#555555` | Disabled text, hints |
| `accent-green` | `#00C853` | Primary accent — key action, status, slider fill |
| `accent-purple` | `#9C27B0` | Preview/Composite mode |
| `accent-blue` | `#2979FF` | Batch/Foreground mode |
| `accent-orange` | `#FF9100` | Matte mode |
| `accent-red` | `#FF3D00` | Error states, processing alerts |
| `glow-green` | `rgba(0,200,83,0.25)` | Slider thumb glow, active hover |
| `glow-purple` | `rgba(156,39,176,0.25)` | Preview button hover glow |

### Dark Theme Rationale
- `#141414` is darker than Resolve's `#1a1a1a` — the panel should RECEDE
  behind the timeline, not compete with it. A keying panel is secondary UI.
- No pure black (`#000000`) anywhere — it looks like a rendering bug on LCD
  screens in studio lighting.
- No pure white text — `#e8e8e8` has enough contrast (13:1 against `#141414`)
  without being harsh.

---

## 3. Typography

### Font Stack
```
Primary:    'Inter', 'SF Pro Display', 'Segoe UI', sans-serif
Monospace:  'JetBrains Mono', 'SF Mono', 'Consolas', monospace
```

**Inter** is the cinematic default — free, modern, excellent at small sizes.
Falls back to platform-appropriate fonts on machines where Inter isn't installed
(SF Pro on Mac, Segoe UI on Windows). No Google Fonts load — Inter ships as a
local `@font-face` embedded in the panel.

### Scale

| Element | Font | Size | Weight | Case | Spacing |
|---------|------|------|--------|------|---------|
| Panel title | Primary | 13px | 700 | UPPERCASE | 1.5px |
| Section label | Primary | 10px | 600 | Normal | 0.3px |
| Slider value | Monospace | 11px | 500 | Normal | 0 |
| Button text | Primary | 10px | 700 | UPPERCASE | 0.8px |
| Status bar | Monospace | 9px | 400 | Normal | 0 |
| Log text | Monospace | 8px | 400 | Normal | 0 |
| Viewer title | Primary | 16px | 700 | UPPERCASE | 1px |
| Viewer mode button | Primary | 11px | 600 | Normal | 0.3px |
| Viewer status | Monospace | 10px | 400 | Normal | 0 |
| Processing overlay | Primary | 22px | 700 | Normal | 0 |

---

## 4. Component Specs

### 4.1 Sliders (CEP Panel)

```
Track:     3px height, rounded corners (1.5px radius)
           bg-surface (#1e1e1e) unfilled, accent-green (#00C853) filled
Thumb:     12px circle, accent-green fill
           1px border: #333
           box-shadow: 0 0 8px glow-green on hover/active
           transition: transform 0.1s (scale 1.15x on hover)
Row:       label left (10px, text-secondary), value right (monospace, accent-green)
           slider below spanning full width
           total row height: ~32px
```

The filled portion of the track should use a CSS gradient that shows how far
the slider has traveled — a visual "fill bar" like Resolve's color wheels.

### 4.2 Buttons (CEP Panel)

```
Shape:     full-width, 6px border-radius
Padding:   8px vertical
Font:      10px, weight 700, uppercase, letter-spacing 0.8px
Shadow:    0 2px 4px rgba(0,0,0,0.3) — subtle depth

PREVIEW FRAME:
  bg: accent-purple (#9C27B0)
  hover: lighten 10%, shadow expands, translateY(-1px)
  text: white

KEY CURRENT FRAME:
  bg: accent-green (#00C853)
  hover: lighten 10%
  text: #141414 (dark on bright green)

PROCESS WORK AREA:
  bg: accent-blue (#2979FF)
  hover: lighten 10%
  text: white

Disabled:
  opacity: 0.35
  cursor: not-allowed
  no hover effect
```

### 4.3 Dropdown (Screen Type)

```
bg: bg-surface (#1e1e1e)
border: 1px solid border-subtle (#333)
text: text-primary (#e8e8e8)
padding: 6px 8px
border-radius: 4px
on focus: border-color -> accent-green
```

### 4.4 Viewer Window (PySide6 QSS)

```
Base:           bg-base (#141414)
Title:          accent-green, 16px bold uppercase, centered
Mode buttons:   pill-shaped (border-radius: 12px), 24px height, 10px padding
                Active button: filled with mode color + white text
                Inactive button: bg-surface + text-secondary
                On hover: bg-elevated
BG buttons:     smaller (20px height), same pill shape, bg-surface
Image pane:     bg: #000000 (pure black behind the image — content is king)
Status bar:     monospace 10px, text-muted, left-aligned
                meanRGB values in accent-green when actively rendering
```

### 4.5 Processing Overlay (Viewer)

```
Position:       centered on image pane, fills entire pane
Background:     rgba(0,0,0,0.75) — dark enough to signal "paused" without
                hiding the image entirely
Text:           "Re-keying..." in 22px bold white, centered
Indicator:      pulsing green dot (8px circle, CSS/QSS animation)
                alternates opacity 0.3 <-> 1.0 over 1.2s
```

---

## 5. Layout

### 5.1 CEP Panel (AE / Premiere)

Target width: **220-280px** (typical docked panel width).

```
+----------------------------------+
| CORRIDORKEY STUNTWORKS           | <- 13px, uppercase, accent-green
| by Niko Pueringer / StuntWorks   | <- 9px, text-muted
|----------------------------------|
| Screen  [Green Screen      v]   |
|----------------------------------|
| Despill                    0.50  | <- label left, mono value right
| [===========|------]             | <- filled track + thumb
|                                  |
| Refiner                    1.00  |
| [=====================|--]       |
|                                  |
| [x] Despeckle              400  |
| [========|-------------]         |
|----------------------------------|
| Output  [path...] [Browse]      |
|----------------------------------|
| [ PREVIEW FRAME (LIVE) ]        | <- purple
| [ KEY CURRENT FRAME     ]       | <- green
| [ PROCESS WORK AREA     ]       | <- blue
|----------------------------------|
| Status: Ready                    | <- 10px monospace, cyan
| [==========] 45%                 | <- progress bar
|----------------------------------|
| > exec: python cache ...         | <- 8px monospace log
| > py: [CK-AE 20:27:52] ...     |
+----------------------------------+
```

### 5.2 Viewer Window (PySide6)

Default: **single-pane** (saves screen real estate on laptops).
Optional: **Split** toggle brings back two-up view.

```
+---------------------------------------------+
| CORRIDORKEY LIVE PREVIEW                     | <- green title
| [Original] [Composite] [FG] [Matte] [Split] | <- pill buttons
| BG: [Checker] [Black] [White] [V1]          | <- small pill toggles
|---------------------------------------------|
|                                              |
|            [ IMAGE PANE ]                    | <- single image
|            (zoom/pan with mouse)             |
|                                              |
|---------------------------------------------|
| Composite | despill=0.50 @400 | meanRGB(...) | <- monospace status
+---------------------------------------------+
```

With Split ON:
```
+---------------------------------------------+
| [Original] [Composite] [FG] [Matte] [Split] |
|---------------------------------------------|
|          |                                   |
| ORIGINAL | PROCESSED                         |
|          | (view mode applies here)           |
|          |                                   |
|---------------------------------------------|
| status bar                                   |
+---------------------------------------------+
```

---

## 6. Resolve-Specific Notes

Resolve's Fusion UIManager gives us buttons, labels, dropdowns, sliders, and
text inputs — but their LOOK is controlled by Resolve's own theme. We cannot
override Resolve's button color or font through UIManager.

**What we control in Resolve:**
- Label text, button text (keep consistent naming with AE/Premiere)
- Layout order and spacing
- Which widgets appear

**What Resolve controls:**
- All colors, fonts, borders, shadows, hover states

**Strategy:** Don't fight Resolve's theme. The Resolve panel should look
"native Resolve" — which is already dark and professional. Match the TEXT
content (same labels, same button names, same slider names) so a user moving
between editors recognizes the tool. The Viewer window (PySide6) IS fully
customizable and should match the design spec above regardless of which editor
launched it.

---

## 7. Implementation Files

| File | What Changes |
|------|-------------|
| `ae_plugin/cep_panel/index.html` | Full CSS rewrite (style block), HTML label text, button text |
| `ae_plugin/cep_panel/preview_viewer.py` | QSS rewrite (_DARK_STYLE + per-widget setStyleSheet calls), layout restructure (single-pane default) |
| `resolve_plugin/ui/uimanager_panel.py` | Label/button text alignment only — no visual override |

**No new dependencies.** Inter font can be loaded via @font-face from a local
.woff2 file bundled in the panel dir, or we accept Segoe UI / SF Pro as the
fallback and skip the font embed for V1.

---

## 8. What This Does NOT Change

- No changes to ae_processor.py, host.jsx, or any Python engine code
- No changes to the pipeline (cache, extract, single, batch subcommands)
- No changes to the file-watcher IPC or stdin bridge
- No changes to ALIGNMENT.md smoke tests
- No changes to install.py or the install flow
- Resolve's UIManager panel appearance stays native

---

**Awaiting approval. Say "build it" and I start with the CSS rewrite.**
