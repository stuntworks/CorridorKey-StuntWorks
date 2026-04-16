# Kimi Design Consultant — Handoff for Implementation

## What Was Built

We created a Python script (`kimi_design.py`) that sends the CorridorKey plugin's UI code to Kimi K2 (via OpenRouter API) and asks for professional design feedback. It reads both files automatically and writes Kimi's response to `kimi_response.md`.

**To run it again anytime:**
```
cd "D:\New AI Projects\CorridorKey-Plugin"
set KIMI_API_KEY=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
python kimi_design.py
```
Add `--prompt "your specific question"` to focus on one area.

---

## The Two Files Being Redesigned

1. **CEP Panel** — `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\cep_panel\index.html`
   - HTML/CSS embedded in Adobe After Effects / Premiere Pro panel
   - ~250px wide, dark theme, has sliders + buttons + status bar
   - Currently uses cinematic dark theme (#141414 base, #00C853 accent green)

2. **Preview Viewer** — `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\cep_panel\preview_viewer.py`
   - Standalone PySide6/Qt window that shows keying results
   - Has image panes, mode buttons, background selector, built-in sliders
   - Uses QSS stylesheet in `_DARK_STYLE` constant and `_build_ui()` method

**Both must look like they belong to the same $200 professional plugin.**

After editing, copy updated files to the live location:
```
C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel\
```

---

## Kimi's Design Recommendations (Full Results)

Kimi reviewed both files and returned 15 changes ordered by visual impact. Here they are with implementation notes:

### CEP Panel (index.html) Changes

**1. Hide the credit link**
```css
.credit { display:none }
```
Looks hobby-tier. Professional plugins don't self-promote in the chrome.

**2. Tighten to 4px grid spacing**
```css
body { padding:12px 10px }
.section { margin-bottom:12px }
label { margin-bottom:4px }
button { margin:4px 0 }
```

**3. Buttons — 40px height, 12px radius**
```css
button { height:40px; border-radius:12px; font-size:11px; letter-spacing:1px }
```

**4. Better micro-shadows on buttons**
```css
button:enabled { box-shadow:0 1px 2px rgba(0,0,0,.45) }
button:hover:enabled { box-shadow:0 2px 6px rgba(0,0,0,.55); transform:translateY(-1px) }
```

**5. Accent glow on hover for all interactive elements**
```css
select:hover, input[type="range"]:hover::-webkit-slider-thumb { box-shadow:0 0 8px #00C85340 }
```

**6. Precision slider overhaul**
```css
input[type="range"] { height:20px }
::-webkit-slider-runnable-track { height:4px; border-radius:2px; background:#1e1e1e }
::-webkit-slider-thumb { width:14px; height:14px; border:2px solid #141414; margin-top:-5px }
```

**7. Slider tooltip on drag (ASPIRATIONAL — pseudo-elements may not work on range inputs in CEP's Chromium)**
```css
input[type="range"]:active::-webkit-slider-thumb::after {
  content:attr(data-value);
  position:absolute; top:-20px; left:50%; transform:translateX(-50%);
  background:#00C853; color:#141414; padding:2px 6px; border-radius:3px;
  font:10px/1 JetBrains Mono;
}
```

**8. Icon buttons instead of text (SKIP for now — needs icon assets)**

### Qt Viewer (preview_viewer.py) Changes

**9. Unify background with CEP panel**
Add to `_DARK_STYLE`:
```css
QWidget { background:#141414 }
QLabel[imagePane="true"] { background:#0a0a0a; border:1px solid #1e1e1e; border-radius:2px }
```

**10. 1px divider between panes**
```css
QLabel#left_label { border-right:1px solid #1e1e1e }
```

**11. Green filled-left slider groove (Adobe style)**
```css
QSlider::sub-page:horizontal { background:#00C853 }
QSlider::add-page:horizontal { background:#1e1e1e }
```

**12. Hover glow on slider thumb matching CEP**
```css
QSlider::handle:horizontal:hover { border:1px solid #00C853; box-shadow:0 0 8px #00C85340 }
```

**13. Pill buttons — 12px radius + inset shadow when checked**
```css
QPushButton { border-radius:12px; background:#1e1e1e }
QPushButton:checked { background:#00C853; color:#141414; border:1px solid #00C853; box-shadow:inset 0 1px 2px rgba(0,0,0,.3) }
```

**14. Monospace status bar**
```css
QLabel#status { font:10px JetBrains Mono; color:#888; qproperty-alignment:AlignCenter }
```

**15. Frameless window (BIGGEST IMPACT but risky — needs custom drag/close)**
```python
self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
self.setAttribute(Qt.WA_TranslucentBackground)
# paint 8px rounded rect in paintEvent
```

---

## Implementation Priority

**Do first (quick wins, low risk):**
- #2, #3, #4, #6 — spacing, buttons, sliders in CEP panel
- #9, #11, #12 — Qt viewer visual unity with panel

**Do second (medium effort):**
- #1, #5 — credit removal, universal hover glow
- #10, #13, #14 — viewer divider, pill buttons, status bar

**Do last or skip (high risk / needs assets):**
- #7 — tooltip on slider (may not work in CEP Chromium)
- #8 — icon buttons (needs icon files)
- #15 — frameless window (needs custom title bar, drag handling, close button)

---

## Rules

- Follow HRCS: comment blocks on every function (WHAT IT DOES / DEPENDS-ON / AFFECTS)
- Brand color is #00C853 — no other accent colors
- Test in Adobe AE/Premiere before calling it done
- Don't change functionality — visual design ONLY
- The panel sliders were removed from index.html (sliders now live in the viewer only)
