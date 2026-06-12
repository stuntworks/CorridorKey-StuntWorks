# Last modified: 2026-06-12 | Change: Phase 2 end-to-end run on real 4K frame
#
# WHAT IT DOES:
#   Loads the cached 4096x2160 session frame, runs the full Phase 1+2 pipeline
#   (trimap construction then guided filter solve), and writes visual outputs
#   to D:\CLAUDE_JUNK\ck-gauntlet\phase2_e2e\ for human review.
#
# DEPENDS ON: fusion_v2.trimap_builder, fusion_v2.solver_guided,
#             fusion_v2.solver_interface, numpy, cv2
# AFFECTS: D:\CLAUDE_JUNK\ck-gauntlet\phase2_e2e\ (write-only)
# ISOLATED: yes — no imports from CorridorKeyModule

import sys
import os
import time

import numpy as np
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fusion_v2.solver_guided  # self-registers 'guided'

from fusion_v2.trimap_builder import build_trimap
from fusion_v2.solver_interface import solve_matte

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SESSION = r"D:\CK_SCRATCH\ck_session_122bd74c7815ec990de08475"
OLD_MATTE_PATH = r"D:\CLAUDE_JUNK\ck-revert-ab\matte_FEETFIX.png"
OUT_DIR = r"D:\CLAUDE_JUNK\ck-gauntlet\phase2_e2e"

os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------

t0 = time.perf_counter()

print("Loading source frame...")
source_bgr = cv2.imread(os.path.join(SESSION, "source.png"), cv2.IMREAD_COLOR)
assert source_bgr is not None, "source.png not found"
source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)

print("Loading NN alpha (uint16)...")
alpha_u16 = cv2.imread(os.path.join(SESSION, "alpha.png"), cv2.IMREAD_UNCHANGED)
assert alpha_u16 is not None
nn_alpha = alpha_u16.astype(np.float32) / 65535.0

print("Loading SAM2 gate (uint16, binarize at 0.5)...")
sam_u16 = cv2.imread(os.path.join(SESSION, "sam2_gate_raw.png"), cv2.IMREAD_UNCHANGED)
assert sam_u16 is not None
sam_mask = (sam_u16.astype(np.float32) / 65535.0 > 0.5).astype(np.uint8) * 255

print(f"  source: {source_rgb.shape}, sam fg pixels: {(sam_mask > 0).sum()}")

# ---------------------------------------------------------------------------
# Phase 1: Build trimap
# ---------------------------------------------------------------------------

print("\nPhase 1 — Building trimap...")
t1 = time.perf_counter()
trimap = build_trimap(sam_mask, nn_alpha)
t2 = time.perf_counter()
print(f"  Done in {(t2-t1)*1000:.0f}ms")
print(f"  BG={( trimap==0).sum()}, unknown={(trimap==128).sum()}, FG={(trimap==255).sum()}")

# ---------------------------------------------------------------------------
# Phase 2: Guided filter solve
# ---------------------------------------------------------------------------

print("\nPhase 2 — Guided filter solve...")
t3 = time.perf_counter()
alpha_solved = solve_matte(source_rgb, trimap, nn_alpha, solver="guided")
t4 = time.perf_counter()
print(f"  Done in {(t4-t3)*1000:.0f}ms")
print(f"  alpha range: [{alpha_solved.min():.4f}, {alpha_solved.max():.4f}]")

# ---------------------------------------------------------------------------
# Write outputs
# ---------------------------------------------------------------------------

print(f"\nWriting outputs to {OUT_DIR} ...")

# 1. trimap_vis.png — 0/128/255, direct uint8
trimap_vis_path = os.path.join(OUT_DIR, "trimap_vis.png")
cv2.imwrite(trimap_vis_path, trimap)
print(f"  trimap_vis.png written")

# 2. alpha_solved.png — uint16 (full precision)
alpha_u16_out = (alpha_solved * 65535.0).clip(0, 65535).astype(np.uint16)
alpha_path = os.path.join(OUT_DIR, "alpha_solved.png")
cv2.imwrite(alpha_path, alpha_u16_out)
print(f"  alpha_solved.png written (uint16)")

# 3. matte_view.png — 8-bit grayscale for eyeball review
matte_8 = (alpha_solved * 255.0).clip(0, 255).astype(np.uint8)
matte_view_path = os.path.join(OUT_DIR, "matte_view.png")
cv2.imwrite(matte_view_path, matte_8)
print(f"  matte_view.png written")

# 4. side_by_side.png — OLD vs NEW, labeled
print("\nBuilding side-by-side comparison...")
old_raw = cv2.imread(OLD_MATTE_PATH, cv2.IMREAD_UNCHANGED)
assert old_raw is not None, f"OLD matte not found: {OLD_MATTE_PATH}"

# Normalise OLD to 8-bit grayscale
if old_raw.ndim == 3:
    old_gray = cv2.cvtColor(old_raw, cv2.COLOR_BGR2GRAY)
else:
    old_gray = old_raw
if old_gray.dtype != np.uint8:
    old_gray = (old_gray.astype(np.float32) / old_gray.max() * 255).astype(np.uint8)

new_gray = matte_8  # already 8-bit

# Ensure same size (should already match — same session frame)
H, W = old_gray.shape[:2]
if new_gray.shape[:2] != (H, W):
    new_gray = cv2.resize(new_gray, (W, H), interpolation=cv2.INTER_LINEAR)

# Convert both to BGR for colored label text
old_bgr = cv2.cvtColor(old_gray, cv2.COLOR_GRAY2BGR)
new_bgr = cv2.cvtColor(new_gray, cv2.COLOR_GRAY2BGR)

# Labels
label_h = 100
font   = cv2.FONT_HERSHEY_SIMPLEX
scale  = 3.0
thick  = 6
color  = (0, 220, 255)  # yellow-ish in BGR

cv2.putText(old_bgr, "OLD (FEETFIX)", (60, label_h), font, scale, color, thick, cv2.LINE_AA)
cv2.putText(new_bgr, "NEW (Phase 2)", (60, label_h), font, scale, color, thick, cv2.LINE_AA)

side_by_side = np.hstack([old_bgr, new_bgr])
sbs_path = os.path.join(OUT_DIR, "side_by_side.png")
cv2.imwrite(sbs_path, side_by_side)
print(f"  side_by_side.png written ({side_by_side.shape[1]}x{side_by_side.shape[0]})")

total = time.perf_counter() - t0
print(f"\nDone in {total:.1f}s total")
print(f"Outputs:\n  {trimap_vis_path}\n  {alpha_path}\n  {matte_view_path}\n  {sbs_path}")
