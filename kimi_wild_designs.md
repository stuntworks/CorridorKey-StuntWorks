```html
<!DOCTYPE html>
<html>
<head>
<style>
body {
  margin: 0;
  background: #000;
  font-family: 'Arial', sans-serif;
  overflow: hidden;
}
</style>
</head>
<body>

<!-- DESIGN A: ORBITAL RING interface -->
<div style="position: absolute; top: 50px; left: 50px; width: 250px; height: 400px; background: linear-gradient(135deg, #0a0a0a 0%, #111 100%); border-radius: 20px; padding: 20px; box-shadow: 0 0 50px rgba(0,255,150,0.3);">
  <div style="position: relative; width: 100%; height: 100%;">
    <div style="position: absolute; top: 0; left: 50%; transform: translateX(-50%); color: #0ff; font-size: 14px; text-align: center; letter-spacing: 3px; text-shadow: 0 0 10px #0ff;">CORRIDORKEY</div>
    
    <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 180px; height: 180px;">
      <svg width="180" height="180">
        <circle cx="90" cy="90" r="80" fill="none" stroke="rgba(0,255,150,0.2)" stroke-width="1"/>
        <circle cx="90" cy="90" r="60" fill="none" stroke="rgba(0,255,150,0.2)" stroke-width="1"/>
        <circle cx="90" cy="90" r="40" fill="none" stroke="rgba(0,255,150,0.2)" stroke-width="1"/>
      </svg>
      
      <div style="position: absolute; top: 10px; left: 50%; transform: translateX(-50%); width: 40px; height: 40px; background: linear-gradient(45deg, #0f0, #0a0); border-radius: 50%; cursor: pointer; transition: all 0.3s; box-shadow: 0 0 20px #0f0;" onmouseover="this.style.transform='translateX(-50%) scale(1.2)'" onmouseout="this.style.transform='translateX(-50%) scale(1)'" onclick="this.style.background=(this.style.background.includes('rgb(0, 255, 0)')?'linear-gradient(45deg, rgb(0, 0, 255), rgb(0, 0, 170))':'linear-gradient(45deg, rgb(0, 255, 0), rgb(0, 170, 0))')"></div>
      
      <input type="range" style="position: absolute; top: 50px; left: 50%; transform: translateX(-50%) rotate(45deg); width: 60px; background: rgba(255,255,255,0.1); border-radius: 10px;" max="100">
      <input type="range" style="position: absolute; top: 90px; left: 50%; transform: translateX(-50%) rotate(90deg); width: 60px; background: rgba(255,255,255,0.1); border-radius: 10px;" max="100">
      <input type="range" style="position: absolute; top: 130px; left: 50%; transform: translateX(-50%) rotate(135deg); width: 60px; background: rgba(255,255,255,0.1); border-radius: 10px;" max="100">
      
      <div style="position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%); width: 30px; height: 30px; background: rgba(255,255,255,0.1); border-radius: 50%; cursor: pointer; transition: all 0.3s;" onmouseover="this.style.background='rgba(255,255,255,0.3)'" onmouseout="this.style.background='rgba(255,255,255,0.1)'"></div>
    </div>
    
    <div style="position: absolute; bottom: 80px; left: 50%; transform: translateX(-50%); display: flex; gap: 10px;">
      <div style="width: 30px; height: 30px; background: rgba(0,255,150,0.2); border-radius: 50%; cursor: pointer; transition: all 0.3s;" onmouseover="this.style.background='rgba(0,255,150,0.4)'" onmouseout="this.style.background='rgba(0,255,150,0.2)'"></div>
      <div style="width: 30px; height: 30px; background: rgba(0,255,150,0.2); border-radius: 50%; cursor: pointer; transition: all 0.3s;" onmouseover="this.style.background='rgba(0,255,150,0.4)'" onmouseout="this.style.background='rgba(0,255,150,0.2)'"></div>
      <div style="width: 30px; height: 30px; background: rgba(0,255,150,0.2); border-radius: 50%; cursor: pointer; transition: all 0.3s;" onmouseover="this.style.background='rgba(0,255,150,0.4)'" onmouseout="this.style.background='rgba(0,255,150,0.2)'"></div>
    </div>
    
    <div style="position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%); color: #0ff; font-size: 8px; text-align: center;">STATUS: IDLE</div>
  </div>
</div>

<!-- DESIGN B: FLIPPABLE CARD interface -->
<div style="position: absolute; top: 50px; left: 350px; width: 250px; height: 400px; background: #000; border-radius: 15px; overflow: hidden; box-shadow: 0 0 30px rgba(0,255,255,0.5);">
  <div style="position: relative; width: 100%; height: 100%; perspective: 1000px;">
    <div id="card" style="position: absolute; width: 100%; height: 100%; transform-style: preserve-3d; transition: transform 0.6s;">
      <div style="position: absolute; width: 100%; height: 100%; backface-visibility: hidden; background: linear-gradient(45deg, #001 0%, #003 100%); display: flex; flex-direction: column; align-items: center; justify: center;">
        <div style="color: #0ff; font-size: 18px; margin-bottom: 20px; text-shadow: 0 0 20px #0ff;">CORRIDORKEY</div>
        <div style="width: 100px; height: 100px; background: radial-gradient(circle, #0f0 0%, #000 70%); border-radius: 50%; margin: 10px; cursor: pointer; transition: all 0.3s; animation:-box-shadow: 0 0 30px #0f0;" onmouseover="this.style.transform='scale(1.1)'" onmouseout="this.style.transform='scale(1)'"></div>
        <div style="color: #0ff; font-size: 10px; margin-top: 20px;">CLICK TO FLIP</div>
      </div>
      <div style="position: absolute; width: 100%; height: 100%; backface-visibility: hidden; background: linear-gradient(45deg, #000 0%, #002 100%); transform: rotateY(180deg); padding: 20px; box-sizing: border-box;">
        <div style="width: 100%; height: 100%; background: rgba(0,255,150,0.05); border-radius: 10px; padding: 15px; box-sizing: border-box;">
          <div style="display: flex; justify-content: space-between; margin-bottom: 20px;">
            <div style="width: 50px; height: 50px; background: rgba(0,255,0,0.3); border-radius: 50%; cursor: pointer; transition: all 0.3s;"></div>
            <div style="width: 50px; height: 50px; background: rgba(0,0,255,0.3); border-radius: 50%; cursor: pointer; transition: all 0.3s;"></div>
          </div>
          <div style="margin-bottom: 15px;">
            <div style="color: #0ff; font-size: 8px; margin-bottom: 5px;">DESPILL</div>
            <input type="range" style="width: 100%;" max="100">
          </div>
          <div style="margin-bottom: 15px;">
            <div style="color: #0ff; font-size: 8px; margin-bottom: 5px;">REFINER</div>
            <input type="range" style="width: 100%;" max="100">
          </div>
          <div style="margin-bottom: 15px;">
            <div style="color: #0ff; font-size: 8px; margin-bottom: 5px;">DESPECKLE</div>
<input type="range" style="width: 100%;" max="100">
          </div>
          <div style="display: flex; gap: 10px; margin-top: 20px;">
            <div style="flex: 1; height: 30px; background: rgba(0,255,150,0.2); border-radius: 15px; cursor: pointer; transition: all 0.3s;"></div>
            <div style="flex: 1; height: 30px; background: rgba(0,255,150,0.2); border-radius: 15px; cursor: pointer; transition: all 0.3s;"></div>
            <div style="flex: 1; height: 30px; background: rgba(0,255,150,0.2); border-radius: 15px; cursor: pointer; transition: all 0.3s;"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div style="position: absolute; bottom: 10px; left: 50%; transform: translateX(-50%); color: #0ff; font-size: 8px;">STATUS: PROCESSING</div>
</div>

<!-- DESIGN C: HEXAGONAL HONEYCOMB interface -->
<div style="position: absolute; top: 50px; left: 650px; width: 250px; height: 400px; background: linear-gradient(180deg, #000 0%, #001 100%); border-radius: 20px; padding: 20px; box-sizing: border-box; box-shadow: 0 0 40px rgba(0,255,255,0.4);">
  <div style="position: relative; width: 100%; height: 100%;">
    <div style="position: absolute; top: 0; left: 50%; transform: translateX(-50%); color: #0ff; font-size: 16px; letter-spacing: 2px; text-shadow: 0 0 15px #0ff;">STUNTWÖRKS</div>
    
    <div style="position: absolute; top: 40px; left: 50%; transform: translateX(-50%); width: 60px; height: 60px; background: rgba(0,255,0,0.1); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); cursor: pointer; transition: all 0.3s;" onmouseover="this.style.background='rgba(0,255,0,0.3)'" onmouseout="this.style.background='rgba(0,255,0,0.1)'"></div>
    
    <div style="position: absolute; top: 120px; left: 20px;">
      <div style="width: 40px; height: 40px; background: rgba(0,255,150,0.1); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); margin-bottom: 5px;"></div>
      <input type="range" style="width: 40px; transform: rotate(-90deg); margin-top: 10px;" max="100">
    </div>
    
    <div style="position: absolute; top: 120px; right: 20px;">
      <div style="width: 40px; height: 40px; background: rgba(0,255,150,0.1); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); margin-bottom: 5px;"></div>
      <input type="range" style="width: 40px; transform: rotate(-90deg); margin-top: 10px;" max="100">
    </div>
    
    <div style="position: absolute; top: 200px; left: 50%; transform: translateX(-50%);">
      <div style="width: 40px; height: 40px; background: rgba(0,255,150,0.1); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); margin-bottom: 5px;"></div>
      <input type="range" style="width: 40px; transform: rotate(-90deg); margin-top: 10px;" max="100">
    </div>
    
    <div style="position: absolute; bottom: 80px; left: 50%; transform: translateX(-50%); display: flex; gap: 5px;">
      <div style="width: 30px; height: 30px; background: rgba(0,255,150,0.2); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); cursor: pointer; transition: all 0.3s;"></div>
      <div style="width: 30px; height: 30px; background: rgba(0,255,150,0.2); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); cursor: pointer; transition: all 0.3s;"></div>
      <div style="width: 30px; height: 30px; background: rgba(0,255,150,0.2); clip-path: polygon(30% 0%, 70% 0%, 100% 50%, 70% 100%, 30% 100%, 0% 50%); cursor: pointer; transition: all 0.3s;"></div>
    </div>
    
    <div style="position: absolute; bottom: 30px; left: 50%; transform: translateX(-50%); width: 50px; height: 25px; background: rgba(255,255,255,0.1); clip-path: polygon(20% 0%, 80% 0%, 100% 100%, 20% 100%); cursor: pointer; display: flex; align-items: center; justify-content: center; color: #0ff; font-size: 8px;">ON</div>
    
    <div style="position: absolute; bottom: 5px; left: 50%; transform: translateX(-50%); color: #0ff; font-size: 8px;">READY</div>
  </div>
</div>

<script>
document.getElementteById('card').addEventListener('click', function() {
  this.style.transform = this.style.transform === 'rotateY(180deg)' ? 'rotateY(0deg)' : 'rotateY(180deg)';
});
</script>

</body>
</html>
```