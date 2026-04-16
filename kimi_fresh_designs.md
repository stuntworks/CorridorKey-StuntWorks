APPROACH 1  
“Glowing glass toggles and liquid meters hovering in a 2026 carbon-fUIber chassis.”
APPROACH 2  
“Brutalist matte-black slab with minimal cyan neon scribbles—data-only, zero chrome.”
APPROACH 3  
“Pressure-ink HUD: white glyphs laser-etched onto obsidian with a vertical ribbon of molten controls.”

––––––––––––––––––––––––––––––––––
APPROACH 1  (glass + glow)
Save as approach1.html  –  250 px wide, open anywhere.
––––––––––––––––––––––––––––––––––
```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CorridorKey – Approach 1</title>
<style>
  :root{
    --bg:#0d0f11;
    --base:#1a1d21;
    --accent:#00ffd2;
    --accent2:#ff307a;
    --text:#e1e7ed;
    --text2:#7b8a9a;
    --glow:0 0 8px var(--accent),0 0 4px var(--accent);
  }
  *{box-sizing:border-box;margin:0;font-family:"Inter",system-ui,Arial;}
  html,body{height:100%;background:var(--bg);color:var(--text);overflow:hidden;}
  body{display:flex;flex-direction:column;width:250px;height:100vh;padding:14px 12px;font-size:13px;}
  h1{margin:0 0 12px;font-weight:700;font-size:17px;letter-spacing:-.25px;text-align:center;color:var(--accent);text-shadow:var(--glow);}
  .row{display:flex;align-items:center;margin:8px 0;}
  .row label{flex:0 0 86px;font-size:12px;color:var(--text2);}
  select,button{width:100%;border-radius:6px;border:none;font-weight:500;background:rgba(0,0,0,.2);color:var(--text);}
  /* glass toggles */
  .radioGroup{display:flex;gap:6px;flex:1;}
  .radioGroup input{position:absolute;opacity:0;}
  .radioGroup label{flex:1;text-align:center;padding:7px 0;border-radius:6px;border:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.03);cursor:pointer;font-size:12px;transition:.15s;}
  .radioGroup input:checked + label{background:var(--accent);color:var(--bg);border-color:var(--accent);box-shadow:var(--glow);}
  /* liquid sliders */
  .sliderWrap{flex:1;position:relative;height:24px;display:flex;items:center;}
  input[type=range]{-webkit-appearance:none;width:100%;height:4px;background:rgba(255,255,255,.08);border-radius:2px;outline:none;}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--accent);border:3px solid var(--base);box-shadow:var(--glow);cursor:ew-resize;}
  .sliderWrap output{position:absolute;right:0;top:-2px;font-size:11px;color:var(--text2);}
  /* buttons */
  button{margin:6px 0 4px;padding:9px 0;border-radius:6px;background:linear-gradient(135deg,var(--accent),#00b8a0);color:var(--bg);font-weight:600;letter-spacing:.3px;cursor:pointer;transition:.15s;}
  button:hover{filter:brightness(1.2);}
  /* status */
  #status{margin-top:10px;font-size:11px;color:var(--accent);}
  #log{flex:1;overflow-y:auto;margin-top:10px;padding:8px;border-radius:6px;background:rgba(0,0,0,.15);font-size:10px;color:var(--text2);}
</style>
</head>
<body>
  <h1>CorridorKey</h1>

  <div class=row>
    <label>Screen</label>
    <div class=radioGroup>
      <input type=radio name=screen id=gr value=green checked><label for=gr>Green</label>
      <input type=radio name=screen id=bl value=blue><label for=bl>Blue</label>
    </div>
  </div>

  <div class=row>
    <label>Despill</label>
    <div class=sliderWrap>
      <input type=range id=des min=0 max=100 value=50><output for=des>50</output></div>
  </div>

  <div class=row>
    <label>Refiner</label>
    <div class=sliderWrap>
      <input type=range id=ref min=0 max=100 value=75><output for=ref>75</output></div>
  </div>

  <div class=row>
    <label>Despeckle</label>
    <label class="switch" style=margin-left:auto><input type=checkbox id=ds><span></span></label>
  </div>

  <div class=row id=dsRow style=opacity:.4>
    <label>Size px</label>
    <div class=sliderWrap>
      <input type=range id=dsSize min=50 max=2000 value=600><output for=dsSize>600</output></div>
  </>

  <button id=prev>PREVIEW FRAME</button>
  <button id=key>KEY CURRENT FRAME</button>
  <button id=wrk>PROCESS WORK AREA</button>

  <div id=status>Idle</div>
  <div id=log>CorridorKey v1.0 – StuntWorks 2026<br>Ready.</div>

<script>
  const upd=(s,o)=>o.value=s.value;
  document.querySelectorAll("input[type=range]").forEach(s=>{
    const o=s.nextElementSibling; upd(s,o); s.oninput=_=>upd(s,o);
  });
  document.getElementById('ds').onchange=e=>{
    document.getElementById('dsRow').style.opacity=e.target.checked?1:.4;
  };
  ["prev","key","wrk"].forEach((b,i)=>{
    document.getElementById(b).onclick=_=>{
      document.getElementById('status').textContent=["Previewing…","Keying current…","Processing work area…"][i];
      document.getElementById('log').prepend(["Preview complete","Keyed frame","Processed work area\n"][i]);
    }
  });
</script>
</body>
</html>
```

––––––––––––––––––––––––––––––––––
APPROACH 2  (brutalist matte)
Save as approach2.html
––––––––––––––––––––––––––––––––––
```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CorridorKey – Approach 2</title>
<style>
  :root{--bg:#0a0a0a;--surface:#121212;--beam:#00f4c2;--danger:#ff3d7a;--text:#c4c4c4;--text2:#5c5c5c;}
  *{box-sizing:border-box;margin:0;font-family:"JetBrains Mono",monospace;}
  body{width:250px;height:100vh;background:var(--bg);color:var(--text);display:flex;flex-direction:column;font-size:11px;padding:12px 10px;border-top:4px solid var(--beam);}
  header{margin-bottom:14px;font-weight:700;font-size:15px;letter-spacing:-.4px;text-transform:uppercase;}
  header span{color:var(--beam);}
  hr{border:none;height:1px;background:var(--surface);margin:10px 0;}
  select{width:100%;background:var(--surface);color:var(--text);border:none;padding:6px;font-weight:500;}
  .ctrl{margin:8px 0;display:flex;justify-content:space-between;}
  .ctrl label{color:var(--text2);}
  input[type=range]{-webkit-appearance:none;width:100%;height:2px;background:var(--surface);outline:none;}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:10px;height:10px;background:var(--beam);}
  .inset{background:var(--surface);padding:4px 6px;border-radius:2px;font-size:10px;}
  button{width:100%;padding:8px 0;border:none;background:var(--surface);color:var(--text);font-weight:500;margin:4px 0;cursor:pointer;border-left:3px solid transparent;transition:.1s;}
  button:hover{border-left-color:var(--beam);}
  button:active{background:var(--beam);color:var(--bg);}
  #status{color:var(--beam);}
  #log{background:var(--surface);height:60px;overflow-y:auto;padding:5px;font-size:9px;color:var(--text2);margin-top:6px;}
</style>
</head>
<body>
  <header>Corridor<span>Key</span></header>
  <div class=ctrl>
    <label>Screen</label>
    <select id=screen><option>Green<option>Blue</select></div>
  <hr>
  <div class=ctrl><label>Despill</label><span class=inset id=desv>50</span></div>
  <input type=range id=des min=0 max=100 value=50>
  <div class=ctrl><label>Refiner</label><span class=inset id=refv>75</span></div>
  <input type=range id=ref min=0 max=100 value=75>
  <hr>
  <div class=ctrl>
    <label>Despeckle</label>
    <input type=checkbox id=ds></div>
  <div class=ctrl><label>Size</label><span class=inset id=dsSizeV>600</span></div>
  <input type=range id=dsSize min=50 max=2000 value=600>
  <hr>
  <button id=prev>PREVIEW FRAME</button>
  <button id=key>KEY CURRENT FRAME</button>
  <button id=wrk style="background:var(--danger);">PROCESS WORK AREA</button>
  <div id=status>Idle</div>
  <div id=log>CorridorKey / StuntWorks<br>Ready.</div>
<script>
  const set=(s,v)=>v.textContent=s.value;
  document.querySelectorAll("input[type=range]").forEach(s=>{
    const v=document.getElementById(s.id+"v"); set(s,v); s.oninput=_=>set(s,v);
  });
  const log=document.getElementById('log'),sts=document.getElementById('status');
  ["prev","key","wrk"].forEach((b,i)=>{
    document.getElementById(b).onclick=_=>{
      sts.textContent="Working…"; 
      log.prepend(["Preview done\n","Keyed\n","Work area done\n"][i]);
    }
  });
</script>
</body>
</html>
```

––––––––––––––––––––––––––––––––––
APPROACH 3  (laser HUD ribbon)
Save as approach3.html
––––––––––––––––––––––––––––––––––
```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CorridorKey – Approach 3</title>
<style>
  :root{--bg:#010101;--paper:#0a0a0a;--ink:#fff;--accent:#00e1ff;--grid:rgba(255,255,255,.06);}
  *{box-sizing:border-box;margin:0;font-family:"IBM Plex Mono",monospace;}
  body{width:250px;height:100vh;background:var(--bg);color:var(--ink);display:flex;flex-direction:column;font-size:12px;padding:16px 14px;}
  header{margin-bottom:12px;font-weight:500;font-size:14px;letter-spacing:.4px;text-transform:uppercase;position:relative;}
  header::after{content:"";position:absolute;left:0;bottom:-4px;width:40px;height:2px;background:var(--accent);}
  .ribbon{position:absolute;left:0;top:0;width:4px;height:100%;background:var(--accent);}
  .ctrl{display:flex;justify-content:space-between;margin:10px 0;gap:10px;}
  label{flex:0 0 80px;font-size:11px;color:var(--grid);}
  input[type=range]{-webkit-appearance:none;height:2px;background:var(--paper);}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;border-radius:50%;background:var(--accent);}
  button{width:100%;padding:10px 0;border:1px solid var(--grid);background:transparent;color:var(--ink);font-weight:500;margin:5px 0;cursor:pointer;position:relative;transition:.15s;}
  button:hover{background:var(--accent);color:var(--bg);}
  #status{margin-top:10px;font-size:10px;color:var(--accent);}
  #log{flex:1;overflow-y:auto;margin-top:10px;padding:8px;border:1px solid var(--grid);font-size:9px;color:var(--grid);}
</style>
</head>
<body>
  <div class=ribbon></div>
  <header>CorridorKey</header>

  <div class=ctrl>
    <label>Screen</label>
    <select id=screen style="flex:1;background:var(--paper);border:none;color:var(--ink);padding:4px;">
      <option>Green<option>Blue</select></div>

  <div class=ctrl><label>Despill</label><span id=desv>50</span></div>
  <input type=range id=des min=0 max=100 value=50>

  <div class=ctrl><label>Refiner</label><span id=refv>75</span></div>
  <input type=range id=ref min=0 max=100 value=75>

  <div class=ctrl>
    <label>Despeckle</label>
    <input type=checkbox id=ds>
  </div>

  <div class=ctrl><label>Size</label><span id=dsSizeV>600</span></div>
  <input type=range id=dsSize min=50 max=2000 value=600>

  <button id=prev>PREVIEW FRAME</button>
  <button id=key>KEY CURRENT FRAME</button>
  <button id=wrk>PROCESS WORK AREA</button>

  <div id=status>Idle</div>
  <div id=log>CorridorKey / StuntWorks<br>Ready.</div>

<script>
  const upd=(s,o)=>o.textContent=s.value;
  document.querySelectorAll("input[type=range]").forEach(s=>{
    const o=document.getElementById(s.id+"v"); upd(s,o); s.oninput=_=>upd(s,o);
  });
  const log=document.getElementById('log'),sts=document.getElementById('status');
  ["prev","key","wrk"].forEach((b,i)=>{
    document.getElementById(b).onclick=_=>{
      sts.textContent=["Preview","Keyed","Processed"][i]+"…"; 
      log.prepend(["Preview done\n","Keyed frame\n","Work area done\n"][i]);
    }
  });
</script>
</body>
</html>
```