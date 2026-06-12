# Last modified: 2026-06-12 | Change: Phase 3b end-to-end — hybrid solver on real 4K frame
#
# WHAT IT DOES:
#   Runs the full Phase 1 trimap + Phase 3b hybrid solve on the cached session
#   frame and writes visual outputs to D:\CLAUDE_JUNK\ck-gauntlet\phase3b_e2e\.
#
#   Outputs:
#     matte_view.png       — 8-bit grayscale hybrid alpha for eyeball review
#     alpha_solved.png     — uint16 full-precision hybrid alpha
#     w_map.png            — 8-bit W map (white=CK rules, black=ViTMatte rules)
#     side_by_side_4way.png — CK RAW | OLD (FEETFIX) | ViTMatte | HYBRID
#
#   Berto's law (Phase 3 verdict): RAW CK alpha is ALWAYS the first panel.
#
# DEPENDS ON: fusion_v2.trimap_builder, fusion_v2.solver_vitmatte,
#             fusion_v2.solver_guided, fusion_v2.solver_hybrid,
#             fusion_v2.solver_interface, numpy, cv2, torch (via vitmatte solver)
# AFFECTS: D:\CLAUDE_JUNK\ck-gauntlet\phase3b_e2e\ (write-only)
# ISOLATED: yes — no imports from CorridorKeyModule

import sys
import os
import time

import numpy as np
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fusion_v2.solver_vitmatte  # self-registers 'vitmatte'
import fusion_v2.solver_guided    # self-registers 'guided' (fallback)
import fusion_v2.solver_hybrid    # self-registers 'hybrid'

from fusion_v2.trimap_builder import build_trimap
from fusion_v2.solver_interface import solve_matte
from fusion_v2.solver_hybrid import _build_geometric_band_map

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SESSION       = r"D:\CK_SCRATCH\ck_session_122bd74c7815ec990de08475"
OLD_MATTE     = r"D:\CLAUDE_JUNK\ck-revert-ab\matte_FEETFIX.png"
PHASE3_MATTE  = r"D:\CLAUDE_JUNK\ck-gauntlet\phase3_e2e\matte_view.png"
OUT_DIR       = r"D:\CLAUDE_JUNK\ck-gauntlet\phase3b_e2e"

os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_as_gray8(path: str) -> np.ndarray:
    """Load any image format and normalise to 8-bit grayscale."""
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    assert img is not None, f"Not found: {path}"
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.dtype != np.uint8:
        mx = float(img.max()) or 1.0
        img = (img.astype(np.float32) / mx * 255.0).clip(0, 255).astype(np.uint8)
    return img


def _labeled_bgr(gray: np.ndarray, label: str, H: int, W: int) -> np.ndarray:
    """Convert gray to BGR, resize to (H, W), add a text label."""
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if bgr.shape[:2] != (H, W):
        bgr = cv2.resize(bgr, (W, H), interpolation=cv2.INTER_LINEAR)
    cv2.putText(bgr, label, (60, 100),
                cv2.FONT_HERSHEY_SIMPLEX, 3.0, (0, 220, 255), 6, cv2.LINE_AA)
    return bgr


# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------

t0 = time.perf_counter()
print("Loading inputs...")

source_bgr = cv2.imread(os.path.join(SESSION, "source.png"), cv2.IMREAD_COLOR)
assert source_bgr is not None, "source.png not found"
source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)

alpha_u16 = cv2.imread(os.path.join(SESSION, "alpha.png"), cv2.IMREAD_UNCHANGED)
assert alpha_u16 is not None, "alpha.png not found"
nn_alpha = alpha_u16.astype(np.float32) / 65535.0

sam_u16 = cv2.imread(os.path.join(SESSION, "sam2_gate_raw.png"), cv2.IMREAD_UNCHANGED)
assert sam_u16 is not None, "sam2_gate_raw.png not found"
sam_mask = (sam_u16.astype(np.float32) / 65535.0 > 0.5).astype(np.uint8) * 255

print(f"  source: {source_rgb.shape}   nn_alpha range: [{nn_alpha.min():.3f}, {nn_alpha.max():.3f}]")


# ---------------------------------------------------------------------------
# Phase 1 — trimap
# ---------------------------------------------------------------------------

print("Phase 1 — trimap...")
t1 = time.perf_counter()
trimap = build_trimap(sam_mask, nn_alpha)
t2 = time.perf_counter()
print(f"  {(t2-t1)*1000:.0f}ms  "
      f"BG={(trimap==0).sum()} unk={(trimap==128).sum()} FG={(trimap==255).sum()}")


# ---------------------------------------------------------------------------
# Phase 3b — hybrid solve (includes ViTMatte model load on first call)
# ---------------------------------------------------------------------------

print("Phase 3b — hybrid solve (first call loads ViTMatte model)...")
t3 = time.perf_counter()
alpha_hybrid = solve_matte(source_rgb, trimap, nn_alpha, solver="hybrid", sam_binary=sam_mask)
t4 = time.perf_counter()
print(f"  {(t4-t3)*1000:.0f}ms total (includes model load)")
print(f"  alpha range: [{alpha_hybrid.min():.4f}, {alpha_hybrid.max():.4f}]")

print("Phase 3b — warm run (model already loaded)...")
t5 = time.perf_counter()
alpha_hybrid = solve_matte(source_rgb, trimap, nn_alpha, solver="hybrid", sam_binary=sam_mask)
t6 = time.perf_counter()
print(f"  {(t6-t5)*1000:.0f}ms (warm)")


# ---------------------------------------------------------------------------
# Build W map for visualisation
# ---------------------------------------------------------------------------

print("Building W map visualisation...")
W_map = _build_geometric_band_map(trimap, sam_mask, feet_zone_pct=0.12)
W_vis = (W_map * 255.0).clip(0, 255).astype(np.uint8)
print(f"  W range in unknown band: [{W_map[trimap==128].min():.4f}, "
      f"{W_map[trimap==128].max():.4f}]  "
      f"mean={W_map[trimap==128].mean():.4f}")


# ---------------------------------------------------------------------------
# Write outputs
# ---------------------------------------------------------------------------

print(f"\nWriting to {OUT_DIR} ...")

matte_8 = (alpha_hybrid * 255.0).clip(0, 255).astype(np.uint8)
cv2.imwrite(os.path.join(OUT_DIR, "matte_view.png"), matte_8)

alpha_u16_out = (alpha_hybrid * 65535.0).clip(0, 65535).astype(np.uint16)
cv2.imwrite(os.path.join(OUT_DIR, "alpha_solved.png"), alpha_u16_out)

cv2.imwrite(os.path.join(OUT_DIR, "w_map.png"), W_vis)

print("  matte_view.png, alpha_solved.png, w_map.png written")


# ---------------------------------------------------------------------------
# 4-way side-by-side: CK RAW | OLD (FEETFIX) | ViTMatte | HYBRID
# Berto's law: RAW CK alpha MUST be the first panel.
# ---------------------------------------------------------------------------

print("Building 4-way side-by-side...")

H, W = source_rgb.shape[:2]

ck_raw_gray    = _load_as_gray8(os.path.join(SESSION, "alpha.png"))
old_gray       = _load_as_gray8(OLD_MATTE)
phase3_gray    = _load_as_gray8(PHASE3_MATTE)
hybrid_gray    = matte_8

panels = [
    _labeled_bgr(ck_raw_gray,  "CK RAW",          H, W),
    _labeled_bgr(old_gray,     "OLD (FEETFIX)",    H, W),
    _labeled_bgr(phase3_gray,  "ViTMatte",         H, W),
    _labeled_bgr(hybrid_gray,  "HYBRID (3b)",      H, W),
]

sbs = np.hstack(panels)
sbs_path = os.path.join(OUT_DIR, "side_by_side_4way.png")
cv2.imwrite(sbs_path, sbs)
print(f"  side_by_side_4way.png written ({sbs.shape[1]}x{sbs.shape[0]})")

total = time.perf_counter() - t0
print(f"\nDone in {total:.1f}s total")
print(f"Outputs:\n"
      f"  {OUT_DIR}\\matte_view.png\n"
      f"  {OUT_DIR}\\alpha_solved.png\n"
      f"  {OUT_DIR}\\w_map.png\n"
      f"  {sbs_path}")
