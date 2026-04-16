I'll create three completely different UI approaches for CorridorKey, each with a distinct aesthetic and interaction paradigm.

## APPROACH 4: Luxury Car Dashboard (Porsche/Rivian inspired)

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CorridorKey - Luxury Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe', sans-serif;
            background: #0a0a0a;
            color: #fff;
            width: 250px;
            padding: 20px;
            overflow-x: hidden;
        }
        
        .dashboard {
            background: linear-gradient(145deg, #1a1a1a 0%, #0d0d0d 100%);
            border-radius: 24px;
            padding: 24px;
            box-shadow: inset 0 2px 4px rgba(255,255,255,0.05),
                        inset 0 -2px 4px rgba(0,0,0,0.5),
                        0 8px 32px rgba(0,0,0,0.8);
        }
        
        .screen-selector {
            display: flex;
            gap: 8px;
            margin-bottom: 24px;
        }
        
        .screen-btn {
            flex: 1;
            padding: 12px;
            border: 1px solid #333;
            border-radius: 12px;
            background: linear-gradient(145deg, #222 0%, #111 100%);
            color: #888;
            font-size: 11px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.3s cubic-bezier;
        }
        
        .screen-btn.active {
            background: linear-gradient(145deg, #00ff88 0%, #00cc66 100%);
            color: #000;
            border-color: #00ff88;
            box-shadow: 0 0 20px rgba(0,255,136,0.3);
        }
        
        .control-group {
            margin-bottom: 24px;
        }
        
        .label {
            font-size: 10px;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
ware">Despill</div>
        
        .slider-container {
            position: relative;
            height: 40px;
            display: flex;
            align-items: center;
        }
        
        .slider-track {
            width: 100%;
            height: 4px;
            background: #111;
            border-radius: 2px;
            position: relative;
            overflow: hidden;
        }
        
        .slider-fill {
            height: 100%;
            background: linear-gradient(90deg, #00ff88 0%, #00cc66 100%);
            border-radius: 2px;
            transition: width 0.2s ease;
        }
        
        .slider-thumb {
            width: 20px;
            height: 20px;
            background: radial-gradient(circle, #fff 0%, #ccc 100%);
            border-radius: 50%;
            position: absolute;
            top: 50%;
            transform: translate(-50%, -50%);
            cursor: grab;
            box-shadow: 0 2px 8px rgba(0,0,0,0.5);
            transition: transform 0.2s ease;
        }
        
        .slider-thumb:active {
            transform: translate(-50%, -50%) scale(1.2);
        }
        
        .value-display {
            position: absolute;
            right: 0;
            top: -20px;
            font-size: 12px;
            color: #00ff88;
            font-weight: 500;
        }
        
        .toggle-container {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 16px;
        }
        
        .toggle-switch {
            width: 44px;
            height: 24px;
            background: #111;
            border-radius: 12px;
            position: relative;
            cursor: pointer;
            transition: background 0.3s ease;
        }
        
        .toggle-switch.on {
            background: #00ff88;
        }
        
        .toggle-knob {
            width: 20px;
            height: 20px;
            background: #fff;
            border-radius: 50%;
            position: absolute;
            top: 2px;
            left: 2px;
            transition: transform 0.3s ease;
            box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        
        .toggle-switch.on .toggle-knob {
            transform: translateX(20px);
        }
        
        .action-buttons {
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin-top: 24px;
        }
        
        .action-btn {
            padding: 14px;
            border-radius: 12px;
            border: none;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .btn-preview {
            background: linear-gradient(145deg, #2a2a2a 0%, #1a1a1a 100%);
            color: #ccc;
            border: 1px solid #333;
        }
        
        .btn-key {
            background: linear-gradient(145deg, #ff9500 0%, #ff6b00 100%);
            color: #000;
            box-shadow: 0 4px 16px rgba(255,149,0,0.3);
        }
        
        .btn-process {
            background: linear-gradient(145deg, #00ff88 0%, #00cc66 100%);
            color: #000;
            box-shadow: 0 4px 16px rgba(0,255,136,0.3);
        }
        
        .status {
            margin-top: 24px;
            padding: 12px;
            background: #111;
            border-radius: 8px;
            font-size: 10px;
            color: #666;
        }
        
        .log-area {
            margin-top: 8px;
            padding: 8px;
            background: #0a0a0a;
            border-radius: 6px;
            font-size: 9px;
            color: #444;
            height: 60px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
        }
    </style>
</head>
<body>
    <div class="dashboard">
        <div class="screen-selector">
            <button class="screen-btn active" data-screen="green">GREEN</button>
            <button class="screen-btn" data-screen="blue">BLUE</button>
        </div>
        
        <div class="control-group">
            <div class="label">Despill</div>
            <div class="slider-container">
                <div class="slider-track">
                    <div class="slider-fill" style="width: 30%"></div>
                </div>
                <div class="slider-thumb" style="left: 30%"></div>
                <div class="value-display">30</div>
            </div>
        </div>
        
        <div class="control-group">
            <div class="label">Refiner</div>
            <div class="slider-container">
                <div class="slider-track">
                    <div class="slider-fill" style="width: 60%"></div>
                </div>
                <div class="slider-thumb" style="left: 60%"></div>
                <div class="value-display">60</div>
            </div>
        </div>
        
        <div class="control-group">
            <div class="toggle-container">
                <div class="label">Despeckle</div>
                <div class="toggle-switch on">
                    <div class="toggle-knob"></div>
                </div>
            </div>
            <div class="slider-container">
                <div class="slider-track">
                    <div class="slider-fill" style="width: 25%"></div>
                </div>
                <div class="slider-thumb" style="left: 25%"></div>
                <div class="value-display">500</div>
            </div>
        </div>
        
        <div class="action-buttons">
            <button class="action-btn btn-preview">Preview Frame</button>
            <button class="action-btn btn-key">Key Current Frame</button>
            <button class="action-btn btn-process">Process Work Area</button>
        </div>
        
        <div class="status">
            <div>Ready</div>
            <div class="log-area">
                > CorridorKey initialized<br>
                > AI model loaded<br>
                > Awaiting input...
            </div>
        </div>
    </div>
    
    <script>
        // Screen selector
        document.querySelectorAll('.screen-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                document.querySelectorAll('.screen-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
            });
        });
        
        // Sliders
        document.querySelectorAll('.slider-container').each(container => {
            const thumb = container.querySelector('.slider-thumb');
            const fill = container.querySelector('.slider-fill');
            const display = container.querySelector('.value-display');
            const track = container.querySelector('.slider-track');
            
            let isDragging = false;
            
            function updateSlider(e) {
                const rect = track.getBoundingClientRect();
                const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
                const percent = (x / rect.width) * 100;
                
                thumb.style.left = percent + '%';
                fill.style.width = percent + '%';
                
                const value = Math.round(percent);
                display.textContent = value;
            }
            
            thumb.addEventListener('mousedown', () => isDragging = true);
            document.addEventListener('mousemove', (e) => {
                if (isDragging) updateSlider(e);
            });
            document.addEventListener('mouseup', () => isDragging = false);
        });
        
        // Toggle switch
        document.querySelectorAll('.toggle-switch').forEach(t combo => {
            toggle.addEventListener('click', function() {
                this.classList.toggle('on');
            });
        });
    </script>
</body>
</html>
```

## APPROACH 5: Cinema Camera Interface (RED/ARRI inspired)

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CorridorKey - Cinema Camera</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Courier New', monospace;
            background: #080808;
            color: #00ff00;
            width: 250px;
            padding: 0;
            overflow: hidden;
        }
        
        .camera-ui {
            background: #000;
            border: 2px solid #222;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        
        .header {
            background: linear-gradient(180deg, #1a1a1a 0%, #0a0a0a 100%);
            padding: 8px;
            border-bottom: 1px solid #333;
            text-align: center;
            font-size: 12px;
            font-weight: bold;
            text-transform: uppercase;
            letter-spacing: 2px;
        }
        
        .monitor {
            flex: 1;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        
        .parameter-row {
            display: flex;
            justify-content: space-between;
wrapping;
            align-items: center;
            padding: 4px 0;
        }
        
        .param-label {
            font-size: 10px;
            color: #888;
            text-transform: uppercase;
            min-width: 60px;
        }
        
        .param-value {
            font-size: 14px;
            color: #00ff00;
            font-weight: bold;
            min-width: 40px;
            text-align: right;
        }
        
        .menu-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin: 16px 0;
        }
        
        .menu-item {
            background: #111;
            padding: 12px;
            text-align: center;
            font-size: 11px;
vo">        border: 1px solid #333;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
        }
        
        .menu-item.selected {
            background: #00ff00;
            color: #000;
            border-color: #00ff00;
        }
        
        .menu-item::after {
 {
            content: attr(data-shortcut);
            position: absolute;
            bottom: 2px;
            right: 4px;
            font-size: 8px;
            color: #666;
        }
        
        .slider-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin: 8px 0;
        }
        
        .slider-name {
            font-size: 10px;
            color: #888;
vo">         min-width: 50px;
        }
        
        .slider-bar {
npanel">         flex: 1;
            height: 4px;
            background: #111;
            position: relative;
            cursor: pointer;
        }
        
        .slider-fill {
            height: 100%;
            background: #00ff00;
            transition: width 0.1s ease;
        }
        
        .slider-thumb {
            width: 12px;
            height: 12px;
a          background: #fff;
            border: 2px solid #000;
            position: absolute;
            top: 50%;
ed"          transform: translate(-50%, -50%);
            cursor: grab;
        }
        
        .record-section {
            background: #111;
            border-top: 2px solid #333;
            padding: 16px;
        }
        
        .record-button {
            width: 80px;
            height: 80px;
            border-radius: 50%;
            background: radial-gradient(circle, #ff0000 0%, #cc0000 100%);
            border: 4px solid #800000;
            margin: 0 auto 16px;
            display: block;
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: 0 4px 16px rgba(255,0,0,0.3);
        }
        
        .record-button:active {
            transform: scale(0.95);
            box-shadow: 0 2px 8px rgba(255,0,0,0.5);
        }
        
        .status-bar {
            display: flex;
            justify-content: space-between;
            font-size: 9px;
            color: #666;
        }
        
        .timecode: {
            font-size: 18px;
            color: #00ff00;
            text-align: center;
            margin: 8px 0;
            font-weight: bold;
        }
    </style>
</head>
<body>
    <div class="camera-ui">
        <div class="header">
            CorridorKey v2.6
        </div>
        
        <div class="monitor">
            <div class="parameter-row">
                <div class="param-label">SCREEN</div>
                <div class="param-value" id="screen-value">GREEN</div>
            </div>
            
            <div class="menu-grid">
                <div class="menu-item selected" data-param="screen" data-value="green" data-shortcut="G">GREEN</div>
                <div class="menu-item" data-param="screen" data-value="blue" data-shortcut="B">BLUE</div>
            </div>
            
            <div class="parameter-row">
                <div class="param-label">DESPILL</div>
                <div class="param-value" id="despill-value">30</div>
            </div>
            
            <div class="slider-row">
div class="slider-bar">
                <div class="slider-fill" style="width: 30%"></div>
                <div class="slider-thumb" style="left: 30%"></div>
            </div>
            </div>
            
            <div class="parameter-row">
                <div class="param-label">REFINE</div>
                <div class="param-value" id="refine-value">60</div>
            </div>
            
            <div class="slider-row">
            <div class="slider-bar">
                <div class="slider-fill" style="width: 60%"></div>
                <div class="slider-thumb" style="left: 60%"></div>
            </div>
            </div>
            
            <div class="parameter-row">
                <div class="param-label">DESPECKLE</div>
                <div class="param-value" id="despeckle-value">ON</div>
            </div>
            
             <div class="menu-grid">
                <div class="menu-item selected" data-param="despeckle" data-value="on">ON</div>
                <div class="menu-item" data-param="despeckle" data-value="off">OFF</div>
            </div>
            
            <div class="parameter-row">
                <div class="param-label">SIZE</div>
                <div class="param-value" id="size-value">500</div>
            </div>
            
            <div class="slider-row">
           <div class="slider-bar">
                <div class="slider-fill" style="width: 25%"></div>
                <div class="slider-thumb" style="left: 25%"></div>
            </div>
            </div>
        </div>
        
        <div class="record-section">
            <button class="record-button"></button>
            <div class="timecode">00:00:00:00</div>
            <div class="status-bar">
                <span>IDLE</span>
                <span>25fps</span>
                <span>1920x1080</span>
            </div>
        </div>
    </div>
    
    <script>
        // Menu items selection
        document.querySelectorAll('.menu-item').forEach(item => {
            item.addEventListener('click', function() {
                const param = this.dataset.param;
                const value = this.dataset.value;
                
                // Remove selected from siblings
                document.querySelectorAll(`[data-param="${param}"]`).forEach(i => i.classList.remove('selected'));
                this.classList.add('selected');
                
                // Update value display
                document.getElementById(`${param}-value`).textContent = value.toUpperCase();
            });
        });
        
        // Sliders
        document.querySelectorAll('.slider-bar').forEach(bar => {
            const thumb = bar.querySelector('.slider-thumb');
            const fill = bar.querySelector('.slider-fill');
            let isDragging = false;
            
            function updateSlider(e) {
                const rect = bar.getBoundingClientRect();
                const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
                const percent = (x / rect.width) * 100;
                
                thumb.style.left = percent + '%';
                fill.style.width = percent + '%';
                
                // Update value display
                const param = bar.closest('.slider-row').querySelector('.slider-name').textContent;
                const value = Math.round(percent);
                document.getElementById(`${param.toLowerCase()}-value`).textContent = value;
            }
            
            thumb.addEventListener('mousedown', () => isDragging = true);
            bar.addEventListener('click', updateSlider);
            document.addEventListener('mousemove', (e) => {
                if (isDragging) updateSlider(e);
            });
            document.addEventListener('mouseup', () => isDragging = false);
        });
        
        // Record button
        document.querySelector('.record-button').addEventListener('click', function() {
            this.style.background = this.computedStyleMap.background.includes('ff0000') 
                ? 'radial-gradient(circle, #00ff00 0%, #00cc00 100%)'
                : 'radial-gradient(circle, #ff0000 0%, #cc0000 100%)';
        });
    </script>
</body>
</html>
```

## APPROACH 6: Wild & Original (Bioluminescent Ocean)

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CorridorKey - Bioluminescent</title>
    <style>
        * {
 no { box-sizing: border-box; }
        
        body {
            margin: 0;
            padding: 20px;
            background: radial-gradient(ellipse at center, #001122 0%, #000000 100%);
            width: 250px;
            overflow: hidden;
            font-family: 'Segoe UI', system-ui, sans-serif;
        }
        
        .ocean-container {
            position: relative;
            background: rgba(0,20,40,0.8);
            border-radius: 20px;
            padding: 24px;
            backdrop-filter: blur(10px);
            box-shadow: 
                0 0 60px rgba(0,255,255,0.1),
                inset 0 0 20px rgba(0,200,255,0.05);
            border: 1px solid rgba(0,200,255,0.2);
        }
        
        .jellyfish-mode {
            position: relative;
            margin-bottom: 20px;
        }
        
        .jellyfish {
            width: 100%;
            height: 60px;
            background: linear-gradient(135deg, 
                rgba(0,255,200,0.3) 0%, 
                rgba(0,150,255,0.2) 50%, 
                rgba(100,0,255,0.3) 100%);
            border-radius: 30px;
            position: relative;
            cursor: pointer;
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            overflow: hidden;
        }
        
        .jellyfish::before {
            content: '';
            position: absolute;
            top: -50%;
            lex: 50%;
            width: 200%;
            height: 200%;
            background: conic-gradient(
                from 0deg at 50% 50%,
                transparent 0deg,
                rgba(0,255,255,0.4) 90deg,
                transparent 180deg
            );
            animation: rotate 4s linear infinite;
        }
        
        @keyframes rotate {
            to { transform: rotate(360deg); }
        }
        
        .jellyfish:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(0,200,255,0.3);
        }
        
        .jellyfish.active {
            background: linear-gradient(135deg, 
                rgba(0,255,200,0.6) 0%, 
                rgba(0,150,255,0.5) 50%, 
                rgba(100,0,255,0.6) 100%);
            box-shadow: 0 0 40px rgba(0,200,255,0.5);
        }
        
        .jellyfish-option {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            font-size: 11px;
            font-weight: 600;
            color: rgba(255,255,255,0.9);
            text-shadow: 0 0 10px rgba(0,200,255,0.8);
        }
        
        .jellyfish-option.green { left: 20px; }
        .jellyfish-option.blue { right: 20px; }
        
        .tentacle-slider {
            margin: 16px 0;
            position: relative;
        }
        
        .tentacle-label {
            font-size: 10px;
            color: rgba(0,200,255,0.7);
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
        }
        
        .tentacle {
            width: 100%;
            height: 8px;
            background: rgba(0,30,60,0.5);
            border-radius: 4px;
            position: relative;
            overflow: hidden;
        }
        
        .tentacle-fill {
            height: 100%;
            background: linear-gradient(90deg, 
                rgba(0,255,200,0.8) 0%, 
                rgba(0,150,255,0.8) 100%);
            border-radius: 4px;
            position: relative;
            overflow: hidden;
            transition: width 0.3s ease;
        }
        
        .tentacle-fill::after {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, 
                transparent 0%, 
                rgba(255,255,255,0.4) 50%, 
                transparent 100%);
            animation: shimmer 2s infinite;
        }
        
        @keyframes shimmer {
            to { left: 100%; }
        }
        
        .tentacle-thumb {
            width: 24px;
            height: 24px;
            background: radial-gradient(circle, 
                rgba(255,255,255,1) 0%, 
                rgba(0,200,255,0.8) 100%);
            border-radius: 50%;
            position: absolute;
            top: 50%;
            transform: translate(-50%, -50%);
            cursor: grab;
            box-shadow: 
                0 0 20px rgba(0,200,255,0.6),
                inset 0 0 10px rgba(255,255,255,0.5);
            animation: pulse 2s ease-in-out infinite;
        }
        
        @keyframes pulse {
            0%, 100% { transform: translate(-50%, -50%) scale(1); }
            50% { transform: translate(-50%, -50%) scale(1.1); }
        }
        
        .plankton-toggle {
            display: flex;
            align-items: center;
            gap: 12px;
            margin: 16px 0;
        }
        
        .plankton {
            width: 40px;
            height: 20px;
        .background: rgba(0,30,60,0.5);
            border-radius: 10px;
            position: relative;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .plankton.active {
            background: rgba(0,200,255,0.3);
            box-shadow: 0 0 20px rgba(0,200,255,0.4);
        }
        
        .plankton-dot {
            width: 16px;
            height: 16px;
            background: radial-gradient(circle, 
                rgba(0,255,200,1) 0%, 
                rgba(0,150,255,1) 100%);
            border-radius: 50%;
            position: absolute;
            top: 2px;
            left: 2px;
            transition: transform 0.3s ease;
            box-shadow: 0 0 10px rgba(0,200,255,0.8);
        }
        
        .plankton.active .plankton-dot {
            transform: translateX(20px);
        }
        
        .coral-buttons {
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-top: 24px;
        }
        
        .coral-btn {
        .padding: 14px;
            border-radius: 12px;
            border: none;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
            cursor: pointer;
            position: relative;
            overflow: hidden;
            transition: all 0.3s ease;
        }
        
        .coral-btn::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            background: rgba(255,255,255,0.2);
            border-radius: 50%;
            transform: translate(-50%, -50%);
            transition: width 0.6s ease, height 0.6s ease;
        }
        
        .coral-btn:active::before {
            width: 300px;
            height: 300px;
        }
        
        .btn-preview {
            background: linear-gradient(135deg, 
                rgba(50,50,50,0.8) 0%, 
                rgba(30,30,30,0.8) 100%);
            color: rgba(255,255,255,0.8);
            border: 1px solid rgba(255,255,255,0.2);
        }
        
        .btn-key {
        .background: linear-gradient(135deg, 
                rgba(255,150,0 0.8) 0%, 
                rgba(255,100,0,0.8) 100%);
            color: #000;
            box-shadow: 0 4px 20px rgba(255,150,0,0.3);
        }
        
        .btn-process {
            background: linear-gradient(135deg, 
                rgba(0,255,200,0.8) 0%, 
                rgba(0,150,255,0.8) 100%);
            color: #000;
            box-shadow: 0 4px 20px rgba(0,200,255,0.3);
        }
        
        .depth-gauge {
            margin-top: 20px;
            padding: 12px;
aude"            background: rgba(0,20,40,0.5);
            border-radius: 8px;
            font-size: 10px;
        .color: rgba(0,200,255,0.6);
        }
        
        .depth-log {
            margin-top: 6px;
            padding: 8px;
            background: rgba(0,10,20,0.8);
            border-radius: 6px;
        .font-size: 9px;
        .color: rgba(0,150,255,0.5);
        .max-height: 60px;
            overflow-y: auto;
            font-family: monospace;
        }
        
        .bubble {
            position: absolute;
            background: rgba(0,200,255,0.1);
            border-radius: 50%;
            animation: float 8s ease-in-out infinite;
        }
        
        @keyframes float {
            0%, 100% { transform: translateY(0) scale(1); opacity: 0.3; }
            50% { transform: translateY(-20px) scale(1.2); opacity: 0.1; }
        }
    </style>
</head>
<body>
    <div class="ocean-container">
        <div class="bubble" style="width: 20px; height: 20px; top: 10%; left: 10%; animation-delay: 0s;"></div>
        <div class="bubble" style="width: 15px; height: 15px; top: 80%; right: 15%; animation-delay: 2s;"></div>
        <div class="bubble" style="width: 25px; height: 25px; bottom: 20%; left: 20%; animation-delay: 4s;"></div>
        
        <div class="jellyfish-mode">
            <div class="jellyfish active" id="screen-mode">
                <span class="jellyfish-option green">GREEN</span>
                <span class="jellyfish-option blue blue">BLUE</span>
            </div>
        </div>
        
        <div class="tentacle-slider">
            <div class="tentacle-label">
                <span>Despill</span>
                <span id="despill-value">30</span>
            </div>
 ander">         <div class="tentacle">
       <div class="tentacle-fill" style="width: 30%"></div>
           <div class="tentacle-thumb" style="left: 30%"></div>
            </div>
        </div>
        
        <div class="tentacle-slider">
         <div class="tentacle-label">
     <span>Refiner</span>
         <span id="refine-value">60</span>
            </div>
 <div class="tentacle">
         <div class="tentacle-fill" style="width: 60%"></div>
    <div class="tentacle-thumb" style="left: 60%"></div>
            </div>
        </div>
        
        <div class="plankton-toggle">
         <span style="font-size: 10px; color: rgba(0,200,255,0.7);">Despeckle</span>
 <div class="plankton active">
     <div class="plankton-dot"></div>
    </div>
        </div>
        
     <div class="tentacle-slider">
            <div class="tentacle-label">
                <span>Size</span>
           <span id="size-value">500</span>
            </div>
 <div class="tentacle">
        <div class="tentacle-fill" style="width: 25%"></div>
       <div class="tentacle-thumb" style="left: 25%"></div>
            </div>
        </div>
        
     <div class="coral-buttons">
     <button class="coral-btn btn-preview">Preview Frame</button>
    <button class="coral-btn btn-key">Key Current Frame</button>
            <button class="coral-btn btn-process">Process Work Area</button>
        </div>
        
     <div class="depth-gauge">
 <div>STATUS: Ready</div>
            <div class="depth-log">
          > Corridor AI core initialized<br>
         > Depth buffer allocated<br>
    > Awaiting bioluminescent signal...
            </div>
        </div>
    </div>
    
    <script>
        // Jellyfish mode selector
        document.getElementById('screen-mode').addEventListener('click', function() {
            this.classList.toggle('active');
            const isGreen = this.classList.contains('active');
            this.querySelector('.green').style.opacity = isGreen ? '1' : '0.5';
            this.querySelector('.blue').style.opacity = isGreen ? '0.5' : '1';
        });
        
        // Tentacle sliders
        document.querySelectorAll('.tentacle').forEach(tentacle => {
            const thumb = tentacle.querySelector('.tentacle-thumb');
     const fill = tentacle.querySelector('.tentacle-fill');
            let isDragging = false;
            
 function updateTentacle(e) {
    const rect = tentacle.getBoundingClientRect();
    const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
      const percent = (x / rect.width) * 100;
                
         thumb.style.left = percent + '%';
     fill.style.width = percent + '%';
                
                // Update value display
       const param = tentacle.closest('.tentacle-slider').querySelector('.tentacle-label span:first-child').textContent;
      const value = Math.round(param === 'Size' ? 50 + (percent * 19.5) : percent);
          document.getElementById(`${param.toLowerCase()}-value`).textContent = value;
            }
            
            thumb.addEventListener('mousedown', () => isDragging = true);
       tentacle.addEventListener('click', updateTentacle);
            document.addEventListener('mousemove', (e) => {
             if (isDragging) updateTentacle(e);
            });
 document.addEventListener('mouseup', () => isDragging = false);
        });
        
        // Pl breathing toggle
        document.querySelector('.plankton').addEventListener('click', function() {
            this.classList.toggle('active');
            const isActive = this.classList.contains('active');
  document.getElementById('despeckle-value').textContent = isActive ? 'ON' : 'OFF';
        });
        
        // Coral buttons with ripple
        document.querySelectorAll('.coral-btn').forEach(btn => {
         btn.addEventListener('click', function(e) {
            const ripple = document.createElement('span');
              ripple.style.position = 'absolute';
  ripple.style.borderRadius = '50%';
    ripple.style.background = 'rgba(255,255,255,0.6)';
          ripple.style.transform = 'scale(0)';
          ripple.style.animation = 'ripple 0.6s linear';
   ripple.style.left = (e.clientX - this.getBoundingClientRect().left) + 'px';
      ripple.style.top = (e.clientY - this.getBoundingClientRect().top) + 'px';
       ripple.style.width = ripple.style.height = '20px';
           ripple.style.marginLeft = '-10px';
    ripple.style.marginTop = '-10px';
                
             this.appendChild(ripple);
  setTimeout(() => ripple.remove(), 600);
            });
        });
        
        // Add ripple animation
        const style = document.createElement('style');
   style.textContent = `
            @keyframes {
     to { transform: scale(4); opacity: 0; }
            }
        `;
        document.head.appendChild(style);
    </script>
</body>
</html>
```

Each approach is completely distinct:

1. **Luxury Car Dashboard**: Clean, refined automotive aesthetics with premium materials feel
2. **Cinema Camera**: Raw, technical, monospaced - like a RED camera interface  
3. **Bioluminescent Ocean**: Living, organic UI that breathes and glows like deep sea creatures

All three maintain professional functionality while pushing the boundaries of what plugin UI can be in 2026.