"""
Per-shot hole diagnostic v2: don't just look for alpha==0; look for low-alpha
patches INSIDE the SAM2 region. Berto reports clusters of holes near
neck/shoulder where yellow + skin + red meet.
"""
import json
from pathlib import Path

import cv2
import numpy as np

SESSION = Path(r"C:/Users/ragsn/AppData/Local/Temp/corridorkey_session")
ALPHA = SESSION / "alpha.png"
SAM2  = SESSION / "sam2_mask.png"
SAM2_RAW = SESSION / "sam2_gate_raw.png"
ORIG  = SESSION / "original.png"

def load_gray(p):
    img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img

def load_bgr(p):
    return cv2.imread(str(p), cv2.IMREAD_COLOR)

print("=" * 70)
print("CORRIDORKEY HOLE DIAGNOSTIC v2")
print("=" * 70)

alpha = load_gray(ALPHA)
sam2  = load_gray(SAM2)
sam2_raw = load_gray(SAM2_RAW) if SAM2_RAW.exists() else None
orig  = load_bgr(ORIG)

if sam2.shape != alpha.shape:
    sam2 = cv2.resize(sam2, (alpha.shape[1], alpha.shape[0]),
                      interpolation=cv2.INTER_NEAREST)
if sam2_raw is not None and sam2_raw.shape != alpha.shape:
    sam2_raw = cv2.resize(sam2_raw, (alpha.shape[1], alpha.shape[0]),
                          interpolation=cv2.INTER_LINEAR)
if orig.shape[:2] != alpha.shape:
    orig = cv2.resize(orig, (alpha.shape[1], alpha.shape[0]),
                      interpolation=cv2.INTER_AREA)

H, W = alpha.shape
print(f"Frame: {W}x{H}")

sam2_bin = (sam2 >= 128).astype(np.uint8)
print(f"SAM2 subject area : {int(sam2_bin.sum()):,} px "
      f"({100*sam2_bin.mean():.2f}%)")

# Erode SAM2 gate slightly so we ignore the boundary fuzz where alpha is
# legitimately low.
kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
sam2_inner = cv2.erode(sam2_bin, kernel)

# Show alpha distribution INSIDE the eroded SAM2 region
inside_alpha = alpha[sam2_inner.astype(bool)]
print(f"\nAlpha histogram inside eroded SAM2 ({inside_alpha.size:,} px):")
hist, _ = np.histogram(inside_alpha, bins=[0,1,5,16,32,64,96,128,160,192,224,255,256])
labels = ["==0","1-4","5-15","16-31","32-63","64-95","96-127",
          "128-159","160-191","192-223","224-254","==255"]
for lab, c in zip(labels, hist):
    print(f"  {lab:<10} {c:>10,}  ({100*c/inside_alpha.size:>6.3f}%)")

# Try several "low alpha" thresholds and see how many components emerge.
print("\nConnected-component sweep (low-alpha patches inside eroded SAM2):")
print(f"  {'T_alpha':>8} {'pixels':>10} {'comps':>6} {'>4':>5} {'>16':>5} "
      f"{'>64':>5} {'>256':>5} {'>1024':>6} {'biggest':>8}")
for T in [1, 5, 16, 32, 48, 64, 80, 96, 128]:
    holes = ((alpha < T) & sam2_inner.astype(bool)).astype(np.uint8)
    n_pix = int(holes.sum())
    num, lbl, stats, _ = cv2.connectedComponentsWithStats(holes, 8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    n_total = len(areas)
    n_4   = int((areas > 4).sum())
    n_16  = int((areas > 16).sum())
    n_64  = int((areas > 64).sum())
    n_256 = int((areas > 256).sum())
    n_1k  = int((areas > 1024).sum())
    biggest = int(areas.max()) if len(areas) else 0
    print(f"  {T:>8} {n_pix:>10,} {n_total:>6} {n_4:>5} {n_16:>5} "
          f"{n_64:>5} {n_256:>5} {n_1k:>6} {biggest:>8,}")

# Pick a working threshold (T=64 catches "soft" holes too) and analyse.
T_alpha = 64
holes = ((alpha < T_alpha) & sam2_inner.astype(bool)).astype(np.uint8)
num, lbl, stats, cents = cv2.connectedComponentsWithStats(holes, 8)
areas = stats[1:, cv2.CC_STAT_AREA]

# Histogram by size bin
bins = [4, 16, 64, 256, 1024, 4096, 16384, 65536]
labels = ["<=4", "5-16", "17-64", "65-256", "257-1k",
          "1k-4k", "4k-16k", "16k-64k", ">64k"]
print(f"\nT_alpha = {T_alpha}: hole size histogram (interior, eroded SAM2):")
for lab, lo, hi in zip(labels,
                       [0]+bins,
                       bins+[10**9]):
    c = int(((areas > lo) & (areas <= hi)).sum())
    px = int(areas[(areas > lo) & (areas <= hi)].sum())
    print(f"  {lab:<10} count={c:>5}  total_px={px:>10,}")

# Top 20 holes
print(f"\nTop 20 largest interior holes at T_alpha={T_alpha}:")
print(f"  {'#':>3} {'area':>7} {'cx,cy':>15} {'meanRGB':>16} "
      f"{'V':>4} {'S':>4} {'H':>4} {'sam_raw':>8} {'alpha_med':>9}")

orig_rgb = cv2.cvtColor(orig, cv2.COLOR_BGR2RGB)
hsv = cv2.cvtColor(orig, cv2.COLOR_BGR2HSV)

order = np.argsort(-areas)        # largest first
top = order[:20]
shadow_n = 0
yellow_n = 0
red_n = 0
mid_n = 0
for k, idx in enumerate(top):
    label = idx + 1
    m = (lbl == label)
    a = int(areas[idx])
    cy, cx = cents[label][1], cents[label][0]
    rgb = orig_rgb[m].mean(axis=0)
    h_  = float(hsv[m, 0].mean())
    s_  = float(hsv[m, 1].mean())
    v_  = float(hsv[m, 2].mean())
    sr  = float(sam2_raw[m].mean()) if sam2_raw is not None else -1
    am  = float(np.median(alpha[m]))
    print(f"  {k+1:>3} {a:>7} ({int(cx):>4},{int(cy):>4}) "
          f"({int(rgb[0]):>3},{int(rgb[1]):>3},{int(rgb[2]):>3})"
          f"  {int(v_):>4} {int(s_):>4} {int(h_):>4} {sr:>8.1f} {am:>9.1f}")
    if v_ < 90:                    shadow_n += 1
    if 18 <= h_ <= 38 and s_ > 40: yellow_n += 1
    if (h_ <= 12 or h_ >= 168) and s_ > 60: red_n += 1
    if 60 < v_ <= 130:             mid_n += 1

print(f"\n  out of top-20:  shadow(V<90)={shadow_n}  "
      f"midtone(60<V<=130)={mid_n}  near-yellow={yellow_n}  near-red={red_n}")

# Aggregate stats for ALL interior holes
all_int = (lbl > 0)
if all_int.any():
    rgb_med = np.median(orig_rgb[all_int].reshape(-1,3), axis=0)
    rgb_mean = orig_rgb[all_int].mean(axis=0)
    v_med = float(np.median(hsv[all_int, 2]))
    v_mean = float(hsv[all_int, 2].mean())
    s_med = float(np.median(hsv[all_int, 1]))
    h_med = float(np.median(hsv[all_int, 0]))
    print("\nAggregate color of ALL interior-hole pixels:")
    print(f"  median RGB = ({int(rgb_med[0])},{int(rgb_med[1])},{int(rgb_med[2])})")
    print(f"  mean   RGB = ({int(rgb_mean[0])},{int(rgb_mean[1])},{int(rgb_mean[2])})")
    print(f"  median V/S/H = {int(v_med)} / {int(s_med)} / {int(h_med)}")
    print(f"  mean V       = {int(v_mean)}")
    if sam2_raw is not None:
        sr_int = sam2_raw[all_int].astype(np.float32)
        print(f"  SAM2-raw at hole pixels: mean={sr_int.mean():.1f} "
              f"median={float(np.median(sr_int)):.1f}  "
              f"pct<128={100*(sr_int<128).mean():.1f}%")

# ----------------------------------------- threshold-recommendation table
print("\nFill-effect by area threshold (T_alpha=64 holes):")
print(f"  {'T_area':>7}  {'filled#':>7}  {'filled_px':>10}  "
      f"{'kept#':>5}  {'kept_px':>10}")
for T in [4, 8, 16, 32, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 2048, 4096]:
    fill_mask = areas <= T
    n_fill = int(fill_mask.sum())
    px_fill = int(areas[fill_mask].sum())
    n_keep = int((~fill_mask).sum())
    px_keep = int(areas[~fill_mask].sum())
    print(f"  {T:>7}  {n_fill:>7}  {px_fill:>10,}  "
          f"{n_keep:>5}  {px_keep:>10,}")

# Saving overlay so we can eyeball the decision visually
overlay = orig.copy()
for idx in top:
    label = idx + 1
    m = (lbl == label)
    color = (0, 0, 255) if areas[idx] <= 256 else (0, 255, 255)
    overlay[m] = color
mix = cv2.addWeighted(orig, 0.55, overlay, 0.45, 0)
out = SESSION / "_diag_holes_overlay_v2.jpg"
cv2.imwrite(str(out), mix, [cv2.IMWRITE_JPEG_QUALITY, 85])
print(f"\nOverlay -> {out}")
