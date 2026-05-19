"""Crop and save the top-5 hole regions so we can SEE what's there."""
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

kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
sam2_inner = cv2.erode((sam2 >= 128).astype(np.uint8), kernel)

T_alpha = 64
holes = ((alpha < T_alpha) & sam2_inner.astype(bool)).astype(np.uint8)
num, lbl, stats, cents = cv2.connectedComponentsWithStats(holes, 8)
areas = stats[1:, cv2.CC_STAT_AREA]
order = np.argsort(-areas)

# Crop a 200x200 patch around each top-5 hole, side-by-side: original + alpha
W = 240
panels = []
for k, idx in enumerate(order[:6]):
    label = idx + 1
    cx, cy = cents[label]
    cx, cy = int(cx), int(cy)
    x0 = max(0, cx - W//2); y0 = max(0, cy - W//2)
    x1 = min(orig.shape[1], x0 + W); y1 = min(orig.shape[0], y0 + W)
    crop_orig = orig[y0:y1, x0:x1].copy()
    crop_alpha = alpha[y0:y1, x0:x1]
    crop_sam2 = sam2[y0:y1, x0:x1]
    a3 = cv2.merge([crop_alpha, crop_alpha, crop_alpha])
    s3 = cv2.merge([crop_sam2, crop_sam2, crop_sam2])
    # mark hole in red on the original
    m = (lbl[y0:y1, x0:x1] == label)
    crop_orig[m] = (0, 0, 255)
    panel = np.hstack([crop_orig, a3, s3])
    text = f"#{k+1} area={int(areas[idx])} px @ ({cx},{cy})"
    cv2.putText(panel, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255,255,255), 2)
    panels.append(panel)

img = np.vstack(panels)
out = SESSION / "_diag_top_holes.jpg"
cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
print(f"Wrote {out}")

# Also dump just the top-2 at full resolution
for k, idx in enumerate(order[:2]):
    label = idx + 1
    cx, cy = cents[label]
    cx, cy = int(cx), int(cy)
    x0 = max(0, cx - 100); y0 = max(0, cy - 100)
    x1 = min(orig.shape[1], x0 + 200); y1 = min(orig.shape[0], y0 + 200)
    a = alpha[y0:y1, x0:x1]
    o = orig[y0:y1, x0:x1].copy()
    m = (lbl[y0:y1, x0:x1] == label)
    o[m] = (0, 0, 255)
    cv2.imwrite(str(SESSION / f"_diag_hole_{k+1}_orig.png"), o)
    cv2.imwrite(str(SESSION / f"_diag_hole_{k+1}_alpha.png"), a)
    print(f"hole #{k+1}: center=({cx},{cy})  area={int(areas[idx])}")
