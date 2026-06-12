# Last modified: 2026-06-12 | Change: Phase 3 end-to-end run — ViTMatte on real 4K frame
#
# WHAT IT DOES:
#   Runs the Phase 1 trimap + Phase 3 ViTMatte solve on the cached session
#   frame and writes outputs for human review, including a 3-way side-by-side
#   comparing OLD (FEETFIX), Phase 2 (guided filter), and Phase 3 (ViTMatte).
#
# DEPENDS ON: fusion_v2.trimap_builder, fusion_v2.solver_vitmatte,
#             fusion_v2.solver_interface, numpy, cv2, torch (via solver)
# AFFECTS: D:\CLAUDE_JUNK\ck-gauntlet\phase3_e2e\ (write-only)
# ISOLATED: yes — no imports from CorridorKeyModule

import sys
import os
import time

import numpy as np
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fusion_v2.solver_vitmatte  # self-registers 'vitmatte'

from fusion_v2.trimap_builder import build_trimap
from fusion_v2.solver_interface import solve_matte

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SESSION      = r"D:\CK_SCRATCH\ck_session_122bd74c7815ec990de08475"
PHASE2_MATTE = r"D:\CLAUDE_JUNK\ck-gauntlet\phase2_e2e\matte_view.png"
OLD_MATTE    = r"D:\CLAUDE_JUNK\ck-revert-ab\matte_FEETFIX.png"
OUT_DIR      = r"D:\CLAUDE_JUNK\ck-gauntlet\phase3_e2e"

os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------

t0 = time.perf_counter()
print("Loading inputs...")

source_bgr = cv2.imread(os.path.join(SESSION, "source.png"), cv2.IMREAD_COLOR)
source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)

alpha_u16 = cv2.imread(os.path.join(SESSION, "alpha.png"), cv2.IMREAD_UNCHANGED)
nn_alpha  = alpha_u16.astype(np.float32) / 65535.0

sam_u16   = cv2.imread(os.path.join(SESSION, "sam2_gate_raw.png"), cv2.IMREAD_UNCHANGED)
sam_mask  = (sam_u16.astype(np.float32) / 65535.0 > 0.5).astype(np.uint8) * 255

print(f"  source: {source_rgb.shape}")

# ---------------------------------------------------------------------------
# Phase 1 trimap (same as Phase 2)
# ---------------------------------------------------------------------------

print("Phase 1 — trimap...")
t1 = time.perf_counter()
trimap = build_trimap(sam_mask, nn_alpha)
t2 = time.perf_counter()
print(f"  {(t2-t1)*1000:.0f}ms  BG={(trimap==0).sum()} unk={(trimap==128).sum()} FG={(trimap==255).sum()}")

# ---------------------------------------------------------------------------
# Phase 3 ViTMatte solve (model loads on first call)
# ---------------------------------------------------------------------------

print("Phase 3 — ViTMatte solve (first call loads model)...")
t3 = time.perf_counter()
alpha_solved = solve_matte(source_rgb, trimap, nn_alpha, solver="vitmatte")
t4 = time.perf_counter()
print(f"  {(t4-t3)*1000:.0f}ms total (includes model load on first call)")
print(f"  alpha range: [{alpha_solved.min():.4f}, {alpha_solved.max():.4f}]")

# Warm run to get true per-frame timing
print("Phase 3 — ViTMatte warm run (model already loaded)...")
t5 = time.perf_counter()
alpha_solved = solve_matte(source_rgb, trimap, nn_alpha, solver="vitmatte")
t6 = time.perf_counter()
print(f"  {(t6-t5)*1000:.0f}ms (warm, no model load)")

# ---------------------------------------------------------------------------
# Write outputs
# ---------------------------------------------------------------------------

print(f"\nWriting to {OUT_DIR} ...")

matte_8 = (alpha_solved * 255.0).clip(0, 255).astype(np.uint8)
cv2.imwrite(os.path.join(OUT_DIR, "matte_view.png"), matte_8)

alpha_u16_out = (alpha_solved * 65535.0).clip(0, 65535).astype(np.uint16)
cv2.imwrite(os.path.join(OUT_DIR, "alpha_solved.png"), alpha_u16_out)

print("  matte_view.png + alpha_solved.png written")

# ---------------------------------------------------------------------------
# 3-way side-by-side: OLD | Phase 2 guided | Phase 3 ViTMatte
# ---------------------------------------------------------------------------

print("Building 3-way side-by-side...")

def _load_as_gray8(path):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    assert img is not None, f"Not found: {path}"
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.dtype != np.uint8:
        img = (img.astype(np.float32) / float(img.max() or 1) * 255).astype(np.uint8)
    return img

old_gray   = _load_as_gray8(OLD_MATTE)
phase2_gray = _load_as_gray8(PHASE2_MATTE)
phase3_gray = matte_8

H, W = old_gray.shape[:2]

# Normalise sizes to match frame
def _to_bgr_frame(gray, label):
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if bgr.shape[:2] != (H, W):
        bgr = cv2.resize(bgr, (W, H), interpolation=cv2.INTER_LINEAR)
    cv2.putText(bgr, label, (60, 100),
                cv2.FONT_HERSHEY_SIMPLEX, 3.0, (0, 220, 255), 6, cv2.LINE_AA)
    return bgr

panels = [
    _to_bgr_frame(old_gray,    "OLD (FEETFIX)"),
    _to_bgr_frame(phase2_gray, "Phase 2 (guided)"),
    _to_bgr_frame(phase3_gray, "Phase 3 (ViTMatte)"),
]
sbs = np.hstack(panels)
sbs_path = os.path.join(OUT_DIR, "side_by_side_3way.png")
cv2.imwrite(sbs_path, sbs)
print(f"  side_by_side_3way.png written ({sbs.shape[1]}x{sbs.shape[0]})")

total = time.perf_counter() - t0
print(f"\nDone in {total:.1f}s total")
print(f"Outputs:\n  {OUT_DIR}\\matte_view.png\n  {OUT_DIR}\\alpha_solved.png\n  {sbs_path}")
