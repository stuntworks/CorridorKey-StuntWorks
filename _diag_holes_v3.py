"""v3: try several erode sizes; also probe the neck/shoulder zone explicitly."""
from pathlib import Path
import cv2
import numpy as np

SESSION = Path(r"C:/Users/ragsn/AppData/Local/Temp/corridorkey_session")
alpha = cv2.imread(str(SESSION / "alpha.png"), cv2.IMREAD_UNCHANGED)
sam2  = cv2.imread(str(SESSION / "sam2_mask.png"), cv2.IMREAD_UNCHANGED)
orig  = cv2.imread(str(SESSION / "original.png"), cv2.IMREAD_COLOR)
if sam2.shape != alpha.shape:
    sam2 = cv2.resize(sam2, (alpha.shape[1], alpha.shape[0]),
                      interpolation=cv2.INTER_NEAREST)
if orig.shape[:2] != alpha.shape:
    orig = cv2.resize(orig, (alpha.shape[1], alpha.shape[0]),
                      interpolation=cv2.INTER_AREA)

print("Erode-size sweep on SAM2 gate (T_alpha=64 holes):")
print(f"  {'erode':>5} {'inner_px':>10} {'comps':>6} {'>16':>5} {'>64':>5} "
      f"{'>256':>5} {'biggest':>8}")
for e in [0, 3, 5, 8, 12, 15, 25, 40]:
    if e == 0:
        inner = (sam2 >= 128).astype(np.uint8)
    else:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*e+1, 2*e+1))
        inner = cv2.erode((sam2 >= 128).astype(np.uint8), k)
    holes = ((alpha < 64) & inner.astype(bool)).astype(np.uint8)
    num, lbl, stats, _ = cv2.connectedComponentsWithStats(holes, 8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    n_total = len(areas)
    n_16  = int((areas > 16).sum())
    n_64  = int((areas > 64).sum())
    n_256 = int((areas > 256).sum())
    biggest = int(areas.max()) if len(areas) else 0
    print(f"  {e:>5} {int(inner.sum()):>10,} {n_total:>6} "
          f"{n_16:>5} {n_64:>5} {n_256:>5} {biggest:>8,}")

# Now look in NECK / SHOULDER / TORSO bbox specifically (eyeball from
# preview ~ x in [1100..2100], y in [350..1100])
print("\nNeck/shoulder/torso ROI scan (x:1100-2100, y:350-1100):")
roi = np.zeros_like(alpha, dtype=bool)
roi[350:1100, 1100:2100] = True
sam_in = (sam2 >= 128) & roi
print(f"  ROI subject px : {int(sam_in.sum()):,}")
print(f"  ROI alpha histogram (where SAM2 says subject):")
ah = alpha[sam_in]
hist, _ = np.histogram(ah, bins=[0,1,32,64,128,200,255,256])
labs = ["==0","1-31","32-63","64-127","128-199","200-254","==255"]
for lab, c in zip(labs, hist):
    print(f"    {lab:<10} {c:>9,}  ({100*c/max(1,ah.size):>6.3f}%)")

# Components of low-alpha within that ROI (no erode)
holes = (alpha < 64) & sam_in
holes_u = holes.astype(np.uint8)
num, lbl, stats, _ = cv2.connectedComponentsWithStats(holes_u, 8)
areas = stats[1:, cv2.CC_STAT_AREA]
print(f"  ROI low-alpha components: {len(areas)}")
if len(areas):
    bins = [4, 16, 64, 256, 1024]
    labels = ["<=4", "5-16", "17-64", "65-256", "257-1k", ">1k"]
    edges = [0]+bins+[10**9]
    for lab, lo, hi in zip(labels, edges[:-1], edges[1:]):
        c = int(((areas > lo) & (areas <= hi)).sum())
        print(f"    {lab:<8} {c}")
    print(f"  biggest: {areas.max()} px")
