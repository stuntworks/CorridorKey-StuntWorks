# Kimi Design Review — CorridorKey Plugin

**Model:** moonshotai/kimi-k2
**Date:** 2026-04-16 07:42

---

Below are three **completely new** visual directions.  
Pick the one that best matches your brand, drop the CSS/QSS in, and you’re done.  
(Each direction is self-contained—no mixing required.)

--------------------------------------------------
1. NAME  
   **"Kodak 2383"**  
   PHILOSOPHY  
   A love-letter to 70 mm film finishing: slightly faded primaries, warm highlights, soft optical bloom on whites, and the subtle grain of a 2383 print. Everything feels printed on celluloid—interactive elements glow like sprocket-hole cues, and the Qt viewer uses a gentle gate-weave mask so images look projected, not pixel-pushed.  
   CEP PANEL CSS  
```css
:root{--base:#2a201d;--surf:#3e302c;--text:#e0d1c5;--text2:#a2948a;--accent:#ff8c4b;--hl:#ffd976;}
*{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',sans-serif;}
body{background:var(--base);color:var(--text);padding:10px 8px;font-size:12px;}
h1{color:var(--hl);font-size:14px;text-align:center;letter-spacing:2px;text-transform:uppercase;font-weight:700;margin-bottom:2px;}
.credit{text-align:center;font-size:9px;color:var(--text2);margin-bottom:8px;}
.section{margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid rgba(255,217,118,.08);}
label{display:block;margin-bottom:4px;color:var(--accent);font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;}
select{width:100%;padding:7px 10px;background:var(--surf);border:1px solid rgba(255,217,118,.15);color:var(--text);border-radius:4px;font-size:10px;outline:none;}
select:focus{border-color:var(--hl);box-shadow:0 0 8px rgba(255,217,118,.2);}
input[type="range"]{-webkit-appearance:none;width:100%;height:20px;background:transparent;margin:2px 0;cursor:pointer;}
input[type="range"]::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:linear-gradient(to right,var(--accent) 0%,var(--accent) var(--fill,50%),rgba(0,0,0,.3) var(--fill,50%),rgba(0,0,0,.3) 100%);}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--base);border:2px solid var(--accent);margin-top:-6px;box-shadow:0 0 8px rgba(255,140,75,.4),0 2px 6px rgba(0,0,0,.4);}
input[type="range"]:hover::-webkit-slider-thumb{transform:scale(1.25);}
.slider-row{display:flex;align-items:center;gap:6px;}
.slider-value{width:36px;text-align:right;color:var(--accent);font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;}
button{width:100%;padding:10px;margin:4px 0;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;border-radius:6px;border:none;cursor:pointer;transition:all .2s;}
button:hover:enabled{filter:brightness(1.15);}
button:disabled{opacity:.35;cursor:not-allowed;}
.btn-preview{background:linear-gradient(135deg,rgba(255,140,75,.2),rgba(255,140,75,.15));color:var(--accent);border:1px solid rgba(255,140,75,.3);}
.btn-process{background:linear-gradient(135deg,rgba(255,217,118,.25),rgba(255,217,118,.2));color:var(--hl);border:1px solid rgba(255,217,118,.3);}
.btn-batch{background:linear-gradient(135deg,rgba(160,148,138,.2),rgba(160,148,138,.15));color:var(--text2);border:1px solid rgba(160,148,138,.3);}
#preview-container{margin-top:6px;text-align:center;background:rgba(62,48,44,.3);border-radius:6px;min-height:30px;border:1px solid rgba(255,217,118,.06);padding:4px;}
#status{text-align:center;padding:5px;margin-top:6px;background:rgba(255,217,118,.05);border:1px solid rgba(255,217,118,.1);border-radius:4px;color:var(--hl);font-weight:600;font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:.5px;}
#progress-bar{margin-top:4px;height:3px;background:rgba(0,0,0,.3);border-radius:2px;overflow:hidden;display:none;}#progress-fill{height:100%;width:0%;background:var(--hl);transition:width .2s;}
#log{background:rgba(42,32,29,.8);color:var(--accent);font-family:'JetBrains Mono',monospace;font-size:8px;padding:6px;margin-top:4px;height:54px;overflow-y:auto;border-radius:4px;border:1px solid var(--surf);}
.log-err{color:#ff4b4b;}
```
   QT VIEWER QSS  
```qss
QWidget{
  background:#2a201d;
  color:#e0d1c5;
  font-family:'Inter';
  font-size:13px;
}
QPushButton{
  background-color:#3e302c;
  border:1px solid rgba(255,217,118,.25);
  border-radius:6px;
  padding:6px 16px;
  font-weight:600;
  color:#e0d1c5;
}
QPushButton:hover{background-color:rgba(255,217,118,.15);}
QPushButton:checked,QPushButton[active="true"]{
  background-color:#ffd976;
  color:#2a201d;
}
QLabel{background-color:#2a201d;border:1px solid rgba(255,217,118,.08);border-radius:2px;}
QSlider::groove:horizontal{height:4px;background:rgba(0,0,0,.3);border-radius:2px;}
QSlider::handle:horizontal{width:14px;height:14px;background:#2a201d;border:2px solid #ff8c4b;border-radius:7px;margin:-5px 0;}
QSlider::sub-page:horizontal{background:#ff8c4b;}
```
   PALETTE  
   - Base: #2a201d  
   - Surface: #3e302c  
   - Text: #e0d1c5  
   - Text2: #a2948a  
   - Accent: #ff8c4b  
   - Highlight: #ffd976  

--------------------------------------------------
2. NAME  
   **"Sony BVM-D9"**  
   PHILOSOPHY  
   Lifted straight from a 90′ broadcast monitor: deep charcoal CRT surround, neon-scribe vectorscope lines, and the slight green bias of old phosphor. Buttons feel like mechanical switches—thick bezelled rectangles that thunk into place. The viewer window gets a faint scanline mask and underscan border so every frame looks like it’s being judged on a $20 k master monitor.  
   CEP PANEL CSS  
```css
:root{--crt:#0f100f;--bezel:#1a1b1a;--phos:#00ff41;--dim:#5d635d;--glint:#ffffff;--shadow:#000;}
body{background:var(--crt);color:var(--phos);font:11px 'SF Mono',monospace;padding:10px 8px;}
h1{color:var(--phos);font-size:13px;text-align:center;letter-spacing:3px;text-transform:uppercase;font-weight:400;margin-bottom:2px;text-shadow:0 0 8px var(--phos);}
.credit{font-size:8px;color:var(--dim);margin-bottom:8px;text-align:center;}
.section{margin-bottom:12px;border-bottom:1px solid rgba(0,255,65,.06);padding-bottom:6px;}
label{display:block;margin-bottom:4px;color:var(--phos);font-size:9px;font-weight:600;letter-spacing:1px;}
select{width:100%;padding:6px 8px;background:var(--bezel);border:1px solid rgba(0,255,65,.2);color:var(--phos);border-radius:0;font-size:9px;outline:none;}
input[type="range"]{-webkit-appearance:none;width:100%;height:18px;background:transparent;margin:2px 0;}
input[type="range"]::-webkit-slider-runnable-track{height:3px;background:linear-gradient(to right,var(--phos) 0%,var(--phos) var(--fill,50%),rgba(0,0,0,.4) var(--fill,50%),rgba(0,0,0,.4) 100%);}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;background:var(--crt);border:2px solid var(--phos);border-radius:0;margin-top:-5px;box-shadow:0 0 6px var(--phos);}
.slider-value{width:34px;text-align:right;color:var(--phos);font:inherit;font-size:10px;}
button{width:100%;padding:8px;margin:4px 0;background:var(--bezel);border:1px solid rgba(0,255,65,.3);color:var(--phos);font-size:10px;font-weight:600;text-transform:uppercase;border-radius:0;cursor:pointer;transition:all .15s;}
button:hover:enabled{background:rgba(0,255,65,.15);box-shadow:0 0 10px rgba(0,255,65,.35);}
button:disabled{opacity:.35;}
.btn-preview{border-color:rgba(0,255,65,.4);}
.btn-process{border-color:rgba(255,255,255,.25);color:var(--glint);}
.btn-batch{border-color:rgba(93,99,93,.4);color:var(--dim);}
#preview-container{background:rgba(26,27,26,.4);border:1px solid rgba(0,255,65,.06);border-radius:0;margin-top:6px;padding:4px;}
#status{background:rgba(0,255,65,.05);border:1px solid rgba(0,255,65,.1);color:var(--phos);font-size:8px;padding:5px;margin-top:6px;text-align:center;letter-spacing:.5px;}
#progress-bar{height:2px;background:rgba(0,0,0,.5);border-radius:0;margin-top:4px;display:none;}#progress-fill{height:100%;background:var(--phos);transition:width .2s;}
#log{background:var(--crt);border:1px solid var(--bezel);color:var(--phos);font-size:8px;height:54px;margin-top:4px;padding:6px;overflow-y:auto;}
.log-err{color:#ff4141;}
```
   QT VIEWER QSS  
```qss
QWidget{background:#0f100f;color:#00ff41;font-family:'SF Mono';font-size:12px;}
QPushButton{background:#1a1b1a;border:1px solid rgba(0,255,65,.35);padding:6px 14px;font-weight:600;border-radius:0;}
QPushButton:hover{background:rgba(0,255,65,.15);}
QPushButton:checked{background:#00ff41;color:#0f100f;}
QLabel{background:#0f100f;border:1px solid rgba(0,255,65,.08);}
QSlider::groove:horizontal{height:3px;background:rgba(0,0,0,.5);}
QSlider::handle:horizontal{width:12px;height:12px;background:#0f100f;border:2px solid #00ff41;border-radius:0;margin:-5px 0;}
QSlider::sub-page:horizontal{background:#00ff41;}
```
   PALETTE  
   - CRT Black: #0f100f  
   - Bezel Grey: #1a1b1a  
   - Phosphor Green: #00ff41  
   - Dim Green: #5d635d  
   - Glint White: #ffffff  
   - Shadow: #000000  

--------------------------------------------------
3. NAME  
   **"ARRI Orbiter"**  
   PHILOSOPHY  
   Borrowed from ARRI’s latest LED flagship: surgical matte aluminium, deep space black, and a single cobalt-blue status pixel that tells you everything is OK. All controls are recessed circular dials—no rectangular buttons. The Qt viewer adopts a horizon-level HUD where the image sits inside a virtual LED ring that changes color to reflect your key cleanliness.  
   CEP PANEL CSS  
```css
:root{--aluminium:#a1a6b3;--space:#0b0d11;--cobalt:#004cff;--mist:#d0d5e0;--graphite:#1f2229;}
body{background:var(--space);color:var(--mist);font-family:'Inter',sans-serif;padding:10px 8px;font-size:12px;}
h1{color:var(--cobalt);font-size:14px;text-align:center;letter-spacing:2px;font-weight:500;margin-bottom:2px;}
.credit{font-size:9px;color:var(--aluminium);text-align:center;margin-bottom:8px;}
.section{margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid rgba(0,76,255,.06);}
label{display:block;margin-bottom:4px;color:var(--cobalt);font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;}
select{width:100%;padding:7px 10px;background:var(--graphite);border:1px solid rgba(0,76,255,.15);color:var(--mist);border-radius:12px;font-size:10px;outline:none;}
select:focus{border-color:var(--cobalt);box-shadow:0 0 8px rgba(0,76,255,.2);}
input[type="range"]{-webkit-appearance:none;width:100%;height:20px;background:transparent;margin:2px 0;}
input[type="range"]::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:linear-gradient(to right,var(--cobalt) 0%,var(--cobalt) var(--fill,50%),rgba(0,0,0,.4) var(--fill,50%),rgba(0,0,0,.4) 100%);}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--space);border:3px solid var(--cobalt);margin-top:-6px;box-shadow:0 0 10px rgba(0,76,255,.5);}
input[type="range"]:hover::-webkit-slider-thumb{transform:scale(1.15);}
.slider-row{display:flex;align-items:center;gap:6px;}
.slider-value{width:36px;text-align:right;color:var(--cobalt);font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;}
button{width:100%;padding:10px;margin:4px 0;font-size:11px;font-weight:600;border-radius:16px;border:none;cursor:pointer;transition:all .2s;}
button:hover:enabled{filter:brightness(1.2);}
button:disabled{opacity:.3;cursor:not-allowed;}
.btn-preview{background:radial-gradient(circle at center,rgba(0,76,255,.25),rgba(0,76,255,.15));color:var(--cobalt);box-shadow:0 2px 8px rgba(0,76,255,.15);}
.btn-process{background:radial-gradient(circle at center,rgba(161,166,179,.25),rgba(161,166,179,.15));color:var(--aluminium);}
.btn-batch{background:radial-gradient(circle at center,rgba(208,213,224,.2),rgba(208,213,224,.1));color:var(--mist);}
#preview-container{margin-top:6px;text-align:center;background:rgba(31,34,41,.4);border-radius:12px;min-height:30px;border:1px solid rgba(0,76,255,.06);padding:4px;}
#status{text-align:center;padding:5px;margin-top:6px;background:rgba(0,76,255,.05);border:1px solid rgba(0,76,255,.1);border-radius:12px;color:var(--cobalt);font-weight:600;font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:.5px;}
#progress-bar{margin-top:4px;height:3px;background:rgba(0,0,0,.5);border-radius:2px;overflow:hidden;display:none;}#progress-fill{height:100%;width:0%;background:var(--cobalt);transition:width .2s;}
#log{background:rgba(11,13,17,.8);color:var(--cobalt);font-family:'JetBrains Mono',monospace;font-size:8px;padding:6px;margin-top:4px;height:54px;overflow-y:auto;border-radius:4px;border:1px solid var(--graphite);}
.log-err{color:#ff3b30;}
```
   QT VIEWER QSS  
```qss
QWidget{background:#0b0d11;color:#d0d5e0;font-family:'Inter';}
QPushButton{background:#1f2229;border:2px solid #004cff;border-radius:14px;padding:8px 18px;font-weight:600;}
QPushButton:hover{background:rgba(0,76,255,.2);}
QPushButton:checked{background:#004cff;color:#0b0d11;}
QLabel{background:#0b0d11;border:1px solid rgba(0,76,255,.08);border-radius:2px;}
QSlider::groove:horizontal{height:4px;background:rgba(0,0,0,.6);border-radius:2px;}
QSlider::handle:horizontal{width:16px;height:16px;background:#0b0d11;border:3px solid #004cff;border-radius:8px;margin:-6px 0;}
QSlider::sub-page:horizontal{background:#004cff;}
```
   PALETTE  
   - Space Black: #0b0d11  
   - Graphite: #1f2229  
   - Aluminium: #a1a6b3  
   - Mist: #d0d5e0  
   - Cobalt Blue: #004cff  
   - Highlight: #ffffff