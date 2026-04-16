# Kimi Design Review — CorridorKey Plugin

**Model:** moonshotai/kimi-k2
**Date:** 2026-04-16 00:09

---

Below are the three self-contained style blocks you asked for.  
Drop any one of them straight into index CEP panel (replace everything inside the `<style>` tag) and the Qt viewer will automatically inherit the same tokens via the shared :root block.

 sliders, buttons, status bar).

---

**OPTION A – NEON LAB**  
*"Blade Runner meets grading suite." Electric green tubes pulse against charcoal steel, glass, and CRT scanlines. Every control glows like it's about to hack the mainframe.*

```css
/* ===== NEON-lab.css ===== */
:root{
  --brand:#00ff9d;
  --bg:#0a0a0f;
  --surface:#121218;
  --surface-elev:#1c1c24;
  --border:#2a2a38;
  --text-p:#e6f7ff;
  --text-s:#5a6a8a;
  --accent-error:#ff0055;
  
  --font:"Inter","SF Pro","Segoe UI",sans-serif;
  --mono:"JetBrains Mono","SF Mono","Consolas",monospace;
  --radius:2px;
  --glow:0 0 4px var(--brand),0 0 12px var(--brand);
}

body{
  background:var;
  background-image:
   repeating-linear-gradient(0deg,
   rgba(0,255,157,.03) 0 1px,transparent 1px 3px),
   radial-gradient(circle at 50% 50%,#0a0a0f 0,#08080e 100%);
  padding:10px 8px;
  font:11px/1.3 var(--font);
  color:var(--text-p);
}

button{
  border:none;
  border-radius:var(--radius);
  padding:8px 0;
  margin:4px 0;
  font:600 10px/1 var(--font);
  text-transform:uppercase;
  letter-spacing:.8px;
  background:var(--surface-elev);
  color:var(--text-s);
  box-shadow:inset 0 1px 1px rgba(0,0,0,.5),var(--glow);
  transition:all .18s cubic-bezier(.4,0,.2,1);
}
button:hover:enabled{
  background:var(--brand);
  color:var(--bg);
  box-shadow:0 0 16px var(--brand),inset 0 1px 0 rgba(255,255,255,.25);
}
button:active:enabled{
  transform:translateY(1px);
  box-shadow:inset 0 2px 4px rgba(0,0,0,.6);
}

input[type="range"]{
  -webkit-appearance:none;
  appearance:none;
  width:100%;
  height:20px;
  background:transparent;
  cursor:pointer;
}
input[type="range"]::-webkit-slider-runnable-track{
  height:4px;
  border-radius:2px;
  background:
    linear-gradient(to right,
    var(--brand) 0,var(--brand) var(--fill,50%),
    var(--surface-elev) var(--fill,50%),var(--surface-elev) 100%);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.6);
}
input[type="range"]::-webkit-slider-thumb{
  -webkit-appearance:none;
  appearance:none;
  width:14px;
  height:14px;
  border-radius:50%;
  background:var(--bg);
  border:2px solid var(--brand);
  margin-top:-5px;
  box-shadow:var(--glow),0 2px 8px rgba(0,0,0,.5);
  transition:transform .12s,box-shadow .2s;
}
input[type="range"]:hover::-webkit-slider-thumb{transform:scale(1.25);}
input[type="range"]:active::-webkit-slider-thumb{box-shadow:0 0 0 5px var(--brand);}
```

---

**OPTION B – FILM STUDIO**  
*"Whisper-quiet confidence." A colorist's instrument: velvet blacks, soft gradients, and micro-bezel borders so thin they're almost invisible. Every pixel feels calibrated.

```css
/* ===== film-studio.css ===== */
:root{
  --brand:#00c853;
  --bg:#141415;
  --surface:#1e1e1f;
  --surface-elev:#262627;
  --border:#333334;
  --text-p:#f2f2f2;
  --text-s:#888;
  --accent-error:#ff3b30;
  
  --font:"Inter","SF Pro","Segoe UI",sans-serif;
  --mono:"JetBrains Mono","SF Mono","Consolas",monospace;
  --radius:4px;
}

body{
  background:var(--bg);
  padding:12px 10px;
  font:11px/1.4 var(--font);
  color:var(--text-p);
  -webkit-font-smoothing:antialiased;
}

button{
  border:none;
  border-radius:var(--radius);
  padding:7px 0;
  margin:3px 0;
  font:600 10px/1 var(--font);
  text-transform:uppercase;
  letter-spacing:.6px;
  background:var(--surface-elev);
  color:var(--text-p);
  box-shadow:
    inset 0 -1px 0 rgba(0,0,0,.25),
    inset 0 1px 0 rgba(255,255,255,.06);
  transition:all .15s ease-out;
}
button:hover:enabled{
  background:linear-gradient(180deg,var(--surface-elev) 0,var(--surface) 100%);
  box-shadow:
    inset 0 -1px 0 rgba(0,0,0,.2),
    inset 0 1px 0 rgba(255,255,255,.08),
    0 4px 12px rgba(0,0,0,.35);
}
button:active:enabled{transform:translateY(.5px);}

input[type="range"]{
  -webkit-appearance:none;
  appearance:none;
  width:100%;
  height:18px;
  background:transparent;
  cursor:ew-res-resize;
}
input[type="range"]::-webkit-slider-runnable-track{
  height:3px;
  border-radius:1.5px;
  background:
    linear-gradient(to right,
    var(--brand) 0,var(--brand) var(--fill,50%),
    var(--surface) var(--fill,50%),var(--surface) 100%);
}
input[type="range"]::-webkit-slider-thumb{
  -webkit-appearance:none;
  appearance:none;
  width:12px;
  height:12px;
  border-radius:50%;
  background:var(--text-p);
  border:1.5px solid var(--border);
  margin-top:-4.5px;
  box-shadow:0 1px 3px rgba(0,0,0,.35);
  transition:transform .1s;
}
input[type="range"]:hover::-webkit-slider-thumb{transform:scale(1.15);}
```

---

**OPTION C – CORRIDOR DIGITAL**  
*"Energy you can feel." Corridor's signature lime slashes through matte black like a light-saber. Chunky buttons, aggressive shadows, and micro-animations scream "premium plug-in" the instant it opens.

```css
/* ===== corridor-digital.css ===== */
:root{
  --brand:#00ff85;
  --bg:#0e0e0e;
  --surface:#1a1a1a;
  --surface-elev:#252525;
  --border:#333;
  --text-p:#ffffff;
  --text-s:#999;
  --accent-error:#ff5252;
  
  --font:"Inter","SF Pro","Segoe UI",sans-serif;
  --mono:"JetBrains Mono","SF Mono","Consolas",monospace;
  --radius:6px;
  --shadow:0 8px 24px rgba(0,0,0,.55);
}

body{
  background:
    radial-gradient(ellipse at top,#1a1a1a 0,#0e0e0e 100%),
    var(--bg);
  padding:10px 8px;
  font:11px/1.3 var(--font);
  color:var(--text-p);
}

button{
  border:none;
  border-radius:var(--radius);
  padding:9px 0;
  margin:4px 0;
  font:700 10px/1 var(--font);
  text-transform:uppercase;
  letter-spacing:1px;
  background:var(--surface-elev);
  color:var(--text-p);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,.05),
    0 4px 12px rgba(0,0,0,.45);
  transition:all .15s cubic-bezier(.4,0,.2,1);
}
button:hover:enabled{
  background:var(--brand);
  color:var(--bg);
  box-shadow:0 6px 20px rgba(0,255,133,.4);
  transform:translateY(-1px);
}
button:active:enabled{
  transform:translateY(0);
  box-shadow:inset 0 2px 4px rgba(0,0,0,.6);
}

input[type="range"]{
  -webkit-appearance:none;
  appearance:none;
  width:100%;
  height:22px;
  background:transparent;
  cursor:pointer;
}
input[type="range"]::-webkit-slider-runnable-track{
  height:6px;
  border-radius:3px;
  background:
    linear-gradient(to right,
    var(--brand) 0,var(--brand) var(--fill,50%),
    var(--surface) var(--fill,50%),var(--surface) 100%);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.7);
}
input[type="range"]::-webkit-slider-thumb{
  -webkit-appearance:none;
  appearance:none;
  width:16px;
  height:16px;
  border-radius:50%;
  background:var(--brand);
  border:3px solid var(--bg);
  margin-top:-5px;
  box-shadow:0 0 0 2px var(--brand),var(--shadow);
  transition:transform .12s;
}
input[type="range"]:hover::-webkit-slider-thumb{transform:scale(1.2);}
```

Pick one, paste, and the whole panel (and matching Qt window) instantly adopts that personality.