# Last modified: 2026-05-07 | Change: v2.2 tuning - tighten dilation 161 to 81, gate CK injection on CFM agreement. Reduces foot halo from floor-under-feet inclusion. | Full history: git log
"""v2.2 trimap + Closed-Form Matting (pymatting) + CK hair injection.

REPLACES the v2.1 topology connectivity filter. v2.1 failed when CK had
soft-alpha paths > threshold connecting body to walls / frame corners
through green spill — the filter kept the entire connected blob (50%+
of frame on the test clip).

v2.2 abandons CK-driven topology and uses image-driven alpha matting:

  1. Build a SAM-only trimap (CK NOT used in trimap):
       fg_def      = erode(sam_filled, ~20px)   -> definite foreground
       sam_dilated = dilate(sam_filled, ~80px)  -> outer boundary
       trimap = 0.5 elsewhere; 1.0 in fg_def; 0.0 outside sam_dilated.
  2. Downsample 2x.
  3. Closed-Form Matting via pymatting on the downsampled image.
  4. Upsample alpha 2x.
  5. CK injection in the unknown band only: alpha = max(alpha, ck).
     This recovers hair/fringe detail that CFM smooths over but CK
     keyed correctly.
  6. Hard clamp outside dilated SAM: alpha = 0 there.

The hard clamp is the safety rail. Walls / forklift / drape edges that
sit outside the dilation buffer cannot survive, no matter what CK or
CFM produced. The dilation radius is the spatial trust budget.

source_rgb is REQUIRED in v2.2 (was optional in v2.1). If None, raises.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import numpy as np


# Threshold matches the existing 0.5 convention used by every viewer
# binarisation site (post-sigmoid SAM output). Raise to make SAM more
# conservative; lower to let more of SAM's confidence band contribute.
SAM_BINARIZE_THRESHOLD = 0.5

# Diagnostic toggle for the matte-branch snapshot. The merge-time debug
# dump is gated by DEBUG_MODE below (separate flag so the matte snapshot
# can be turned off independently). Both default True until validated.
DEBUG_ENABLED = True
DEBUG_DIR = Path(__file__).parent / ".chroma_debug"

# Active merge mode toggle. True = v2.2 trimap + CFM (current).
# False = Path B fallback (chroma-blind max(CK, SAM)). Call sites use the
# single dispatcher merge_ck_with_sam_active so flipping this flag is a
# one-deploy A/B without touching call sites. Path B kept on disk for
# hot-revert; v2.1 connectivity logic is gone.
USE_CHROMA_GATED_MERGE = True

# v2.2 debug toggle. Separate from DEBUG_ENABLED so the merge dump can be
# disabled for production renders independently of the matte snapshot.
DEBUG_MODE = True   # set False for production renders


def binarize_sam_silhouette(sam: np.ndarray, threshold: float = SAM_BINARIZE_THRESHOLD) -> np.ndarray:
    # WHAT IT DOES: Threshold a continuous SAM mask to binary {0.0, 1.0} float32.
    # DEPENDS ON:   numpy. Caller pre-applies sigmoid if input is logit-space.
    # AFFECTS:      every merge — controls SAM silhouette edge sharpness.
    return (np.asarray(sam, dtype=np.float32) >= float(threshold)).astype(np.float32)


def union_binary_silhouettes(silhouettes: Iterable[np.ndarray]) -> Optional[np.ndarray]:
    # WHAT IT DOES: OR-combine N already-binarised SAM silhouettes via per-pixel max.
    # DEPENDS ON:   all silhouettes share identical H x W shape. None entries dropped.
    # AFFECTS:      multi-object renders (MASK 1 + MASK 2). Single-object: pass-through.
    valid = [np.asarray(s, dtype=np.float32) for s in silhouettes if s is not None]
    if not valid:
        return None
    out = valid[0]
    for s in valid[1:]:
        out = np.maximum(out, s)
    return out.astype(np.float32, copy=False)


def merge_ck_with_sam(ck_alpha: np.ndarray, sam_silhouette: Optional[np.ndarray]) -> np.ndarray:
    # WHAT IT DOES: Path B fallback merge — final = max(CK, SAM_binary). Kept
    #               on disk for hot-revert via USE_CHROMA_GATED_MERGE=False.
    # DEPENDS ON:   ck_alpha and sam_silhouette must share identical H x W shape.
    #               Both float32 in [0, 1]. sam should be ALREADY binary.
    # AFFECTS:      only the fallback branch of merge_ck_with_sam_active. Not on
    #               the active code path under normal operation.
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if sam_silhouette is None:
        return ck.copy()
    sam = np.asarray(sam_silhouette, dtype=np.float32)
    if ck.shape != sam.shape:
        raise ValueError(f"shape mismatch: ck={ck.shape}, sam={sam.shape}")
    difference = np.clip(sam - ck, 0.0, 1.0)
    final = np.clip(ck + difference, 0.0, 1.0)
    return final


def _save_debug_dump_v22(
    ck: np.ndarray,
    sam: np.ndarray,
    sam_filled: np.ndarray,
    fg_def: np.ndarray,
    sam_dilated: np.ndarray,
    trimap: np.ndarray,
    alpha_solved: np.ndarray,
    final: np.ndarray,
    scale: int,
) -> None:
    # WHAT IT DOES: Diagnostic dump for the v2.2 trimap + CFM merge. Writes the
    #   per-stage masks + CFM output + final to DEBUG_DIR. Overwrites previous
    #   dump; only the LAST merge call survives.
    # DEPENDS ON:   cv2 (lazy), numpy, pathlib. DEBUG_DIR mkdir-on-write.
    # AFFECTS:      writes 8 files to DEBUG_DIR; no return; no logic side effect.
    import cv2 as _cv2
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    ck_vis = np.clip(ck * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_ck_alpha.png"), ck_vis)

    sam_vis = (sam.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_silhouette.png"), sam_vis)

    sam_filled_vis = (sam_filled.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_filled.png"), sam_filled_vis)

    fg_def_vis = (fg_def.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_fg_def.png"), fg_def_vis)

    sam_dilated_vis = (sam_dilated.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_dilated.png"), sam_dilated_vis)

    # trimap visualization: 0.0 -> 0, 0.5 -> 128, 1.0 -> 255.
    trimap_vis = np.clip(trimap * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_trimap.png"), trimap_vis)

    alpha_solved_vis = np.clip(alpha_solved * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_alpha_solved.png"), alpha_solved_vis)

    final_vis = np.clip(final * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_final.png"), final_vis)

    H, W = ck.shape
    n_pixels = float(ck.size)
    unknown_band_pixels = int(((trimap > 0.4) & (trimap < 0.6)).sum())
    fg_pixels = int((trimap >= 1.0).sum())
    bg_pixels = int((trimap <= 0.0).sum())

    sample_topleft = (0, 0)
    sample_body_ctr = (H // 2, W // 2)
    sample_wall = (int(H * 0.10), int(W * 0.15))
    sample_foot = (int(H * 0.90), int(W * 0.50))

    def _fmt(name: str, y: int, x: int) -> str:
        return (
            f"PIXEL_{name}: ck={float(ck[y, x]):.4f} "
            f"sam={int(sam[y, x])} "
            f"trimap={float(trimap[y, x]):.2f} "
            f"final={float(final[y, x]):.4f}  ({y}, {x})"
        )

    stats_lines = [
        "=== v2.2 trimap + CFM debug dump ===",
        f"SHAPE_CK: {ck.shape}",
        f"DOWNSAMPLE_SCALE: {scale}x",
        f"TRIMAP_FG_PIXELS: {fg_pixels}",
        f"TRIMAP_BG_PIXELS: {bg_pixels}",
        f"TRIMAP_UNKNOWN_PIXELS: {unknown_band_pixels}",
        f"FRACTION_UNKNOWN: {unknown_band_pixels / n_pixels * 100.0:.4f}%",
        f"ALPHA_SOLVED_RANGE: min={float(alpha_solved.min()):.4f} "
        f"max={float(alpha_solved.max()):.4f} mean={float(alpha_solved.mean()):.4f}",
        f"FINAL_RANGE: min={float(final.min()):.4f} "
        f"max={float(final.max()):.4f} mean={float(final.mean()):.4f}",
        f"SAM_DILATED_COVERAGE: {float(sam_dilated.sum()) / n_pixels * 100.0:.4f}%",
        _fmt("TOPLEFT     ", *sample_topleft),
        _fmt("BODY_CENTER ", *sample_body_ctr),
        _fmt("WALL        ", *sample_wall),
        _fmt("FOOT        ", *sample_foot),
    ]
    (DEBUG_DIR / "debug_stats.txt").write_text("\n".join(stats_lines) + "\n")


def merge_ck_with_sam_chroma_gated(ck_alpha, sam_silhouette, source_rgb=None):
    """
    v2.2 trimap + Closed-Form Matting with CK hair injection.

    source_rgb is REQUIRED in v2.2. If None, raise ValueError.
    """
    import numpy as np
    import cv2
    from scipy.ndimage import binary_fill_holes
    from pymatting import estimate_alpha_cf

    if source_rgb is None:
        raise ValueError("v2.2 requires source_rgb input")

    H, W = ck_alpha.shape
    ck = np.clip(ck_alpha.astype(np.float32), 0.0, 1.0)
    sam = (sam_silhouette > 0.5).astype(np.uint8)
    rgb = np.clip(source_rgb.astype(np.float32) / 255.0, 0.0, 1.0) \
          if source_rgb.dtype != np.float32 \
          else np.clip(source_rgb, 0.0, 1.0)

    # 1. Preprocess SAM
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (40, 40))
    sam_closed = cv2.morphologyEx(sam, cv2.MORPH_CLOSE, k_close)
    sam_filled = binary_fill_holes(sam_closed).astype(np.uint8)

    # 2. Definite foreground (eroded SAM)
    k_erode = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))
    fg_def = cv2.erode(sam_filled, k_erode, iterations=1)

    # 3. Outer boundary (dilated SAM)
    k_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (81, 81))
    sam_dilated = cv2.dilate(sam_filled, k_dilate, iterations=1)

    # 4. Build trimap (SAM only, CK not used)
    trimap = np.full((H, W), 0.5, dtype=np.float32)
    trimap[fg_def > 0] = 1.0
    trimap[sam_dilated == 0] = 0.0

    # 5. Downsample for CFM
    scale = 2 if H > 3000 else 1
    if scale > 1:
        h, w = H // scale, W // scale
        trimap_small = cv2.resize(trimap, (w, h), cv2.INTER_NEAREST)
        rgb_small = cv2.resize(rgb, (w, h), cv2.INTER_AREA)
    else:
        trimap_small = trimap
        rgb_small = rgb

    # 6. Run CFM. pymatting's Numba kernels expect float64; cast immediately
    # before the call so upstream stays float32 for memory.
    alpha_small = estimate_alpha_cf(
        rgb_small.astype(np.float64),
        trimap_small.astype(np.float64),
    )

    # 7. Upsample
    if scale > 1:
        alpha = cv2.resize(alpha_small.astype(np.float32), (W, H), cv2.INTER_LINEAR)
    else:
        alpha = alpha_small.astype(np.float32)
    alpha = np.clip(alpha, 0.0, 1.0)

    # 8. CK hair injection in unknown band only
    unknown_band = (trimap == 0.5)
    # Only inject CK where CFM also found foreground signal.
    # Prevents CK = 1.0 floor pixels from blowing up alpha where
    # CFM correctly assigned them low alpha.
    inject_zone = unknown_band & (alpha > 0.1)
    alpha[inject_zone] = np.maximum(alpha[inject_zone], ck[inject_zone])

    # 9. Hard clamp outside dilated SAM
    alpha[sam_dilated == 0] = 0.0

    if DEBUG_MODE:
        _save_debug_dump_v22(
            ck=ck, sam=sam, sam_filled=sam_filled,
            fg_def=fg_def, sam_dilated=sam_dilated,
            trimap=trimap, alpha_solved=alpha_small,
            final=alpha,
            scale=scale,
        )

    return alpha.astype(np.float32)

# Tuning guide (v2.2):
# If foot halo within dilated band (CFM extends alpha into floor near foot):
#   This is the dark-on-dark concern. Two levers:
#   - Reduce dilation kernel from 161 toward 121 or 81 (tighter trust budget,
#     hard-clamp kicks in sooner). Watch hair clipping.
#   - The CK injection in the unknown band uses max(); CK can only INCREASE
#     alpha, not decrease. Halos from CFM cannot be killed by CK.
# If hair tendrils clipped beyond dilated SAM:
#   Increase dilation kernel from 161 toward 181 or 201. Trust budget grows.
#   Tradeoff: more room for CFM and CK to bleed into junk.
# If butt notch returns (SAM under-clipping not absorbed by close):
#   Increase close kernel from 40 toward 60 or 80.
# If CFM runtime exceeds budget on a real shot:
#   The downsample threshold is H > 3000. Lower it (e.g. H > 2000) to force
#   downsample on smaller frames.
# DO NOT use CK in the trimap.
# DO NOT skip the hard clamp at the end.


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
) -> np.ndarray:
    # WHAT IT DOES: Public dispatcher. Routes to the v2.2 trimap+CFM merge when
    #   USE_CHROMA_GATED_MERGE is True, otherwise falls back to Path B (max).
    #   Call sites use this single entry point so the A/B switch is a one-flag
    #   flip with no call-site changes. screen_type kwarg accepted for backward
    #   compatibility; v2.2 ignores it.
    # DEPENDS ON:   USE_CHROMA_GATED_MERGE module flag, merge_ck_with_sam (Path B),
    #               merge_ck_with_sam_chroma_gated (v2.2 trimap+CFM).
    # AFFECTS:      every site that imports this dispatcher. Currently:
    #               resolve renderer, resolve viewer, AE viewer (composite + matte).
    if USE_CHROMA_GATED_MERGE:
        print(
            f"[merge] v2.2 trimap+CFM branch  USE_CHROMA_GATED_MERGE=True  "
            f"source_rgb.shape={getattr(source_rgb, 'shape', None)}"
        )
        return merge_ck_with_sam_chroma_gated(
            ck_alpha, sam_silhouette, source_rgb=source_rgb,
        )
    print("[merge] Path B fallback branch  (flag=False)")
    return merge_ck_with_sam(ck_alpha, sam_silhouette)


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    # WHAT IT DOES: Snapshots the v2.2 debug PNGs (debug_*.png) under matte_-
    #   prefixed names so they survive any subsequent composite-mode re-render
    #   that would overwrite them, then saves the final post-processed alpha as
    #   matte_debug_final_displayed.png + writes matte_debug_stats.txt with the
    #   ordered list of ops applied to the merge output before display.
    # DEPENDS ON:   cv2 (lazy), shutil, numpy, pathlib. DEBUG_DIR mkdir-on-write.
    #               Assumes the caller's most recent merge_ck_with_sam_chroma_gated
    #               call dumped to debug_*.png moments earlier.
    # AFFECTS:      writes matte_-prefixed files to DEBUG_DIR; no return; no logic
    #               side effect. No-op when DEBUG_ENABLED is False.
    if not DEBUG_ENABLED:
        return
    import cv2 as _cv2
    import shutil as _shutil
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_pairs = [
        ("debug_ck_alpha.png", "matte_debug_ck_alpha.png"),
        ("debug_sam_silhouette.png", "matte_debug_sam_silhouette.png"),
        ("debug_sam_filled.png", "matte_debug_sam_filled.png"),
        ("debug_fg_def.png", "matte_debug_fg_def.png"),
        ("debug_sam_dilated.png", "matte_debug_sam_dilated.png"),
        ("debug_trimap.png", "matte_debug_trimap.png"),
        ("debug_alpha_solved.png", "matte_debug_alpha_solved.png"),
        ("debug_final.png", "matte_debug_final.png"),
        ("debug_stats.txt", "matte_debug_merge_stats.txt"),
    ]
    for src_name, dst_name in snapshot_pairs:
        src_p = DEBUG_DIR / src_name
        dst_p = DEBUG_DIR / dst_name
        try:
            if src_p.exists():
                _shutil.copy2(str(src_p), str(dst_p))
        except Exception:
            pass
    a = np.asarray(alpha_final, dtype=np.float32)
    final_vis = np.clip(a * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "matte_debug_final_displayed.png"), final_vis)
    stats = (
        "=== matte view final-displayed dump ===\n"
        f"ops applied to merge_output before display: {list(ops_applied)}\n"
        f"final alpha shape={a.shape}  "
        f"mean={float(a.mean()):.4f}  "
        f"min={float(a.min()):.4f}  "
        f"max={float(a.max()):.4f}\n"
        "Compare matte_debug_final.png vs matte_debug_final_displayed.png:\n"
        "  - If different: a downstream op altered the merge output.\n"
        "  - If identical: the merge inputs (matte_debug_ck_alpha + matte_debug_trimap + matte_debug_alpha_solved) explain the output.\n"
    )
    (DEBUG_DIR / "matte_debug_stats.txt").write_text(stats)
