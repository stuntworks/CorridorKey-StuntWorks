# Last modified: 2026-05-07 | Change: v2.1 connectivity filter — replace merge math with topology-based component selection. Walls/junk eliminated by definition, hair preserved by connection to body. Vectorized via np.isin. Empty SAM warning logged. Debug gated by DEBUG_MODE | Full history: git log
"""v2.1 connectivity-based CK + SAM merge per Berto 2026-05-07.

REPLACES the chroma-gated merge + outer SAM kill mask + soft-zone +
SAM-buffer architecture. Distance-based heuristics (chroma threshold,
chroma dilation, kill-mask buffers, soft-zone SAM buffer) all hit the
same ceiling: at body edges they could not distinguish "body part on
green that needs CK soft alpha" from "wall outline transition that needs
killing." Topology can.

The new architecture:

  1. Fill SAM notches (close + flood) so it is a stable body anchor.
  2. Threshold CK permissively to catch hair tendrils.
  3. Find connected components of CK foreground.
  4. Keep ONLY components that intersect the filled SAM body.
  5. Output is CK alpha within kept components, zero elsewhere.

Walls / forklift / cubes are disconnected from the body by definition,
so they are eliminated without any distance tuning. Hair tendrils that
touch the body silhouette get carried along with the body component.

The old apply_sam2_junk_kill / apply_sam2_gate_* helpers in
sam2_combine.py remain on disk as dead code (unreferenced from any call
site) for hot-revert.
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

# Active merge mode toggle. True = v2.1 connectivity filter (current).
# False = Path B fallback (chroma-blind max(CK, SAM)). Call sites use the
# single dispatcher merge_ck_with_sam_active so flipping this flag is a
# one-deploy A/B without touching call sites. Path B kept on disk for
# hot-revert; the chroma-gated math is gone.
USE_CHROMA_GATED_MERGE = True

# v2.1 connectivity filter constants
DEBUG_MODE = True   # set False for production renders
CK_FILTER_THRESHOLD = 0.02
CK_FILTER_CLOSE_PX = 75


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


def _save_debug_dump(
    ck: np.ndarray,
    sam: np.ndarray,
    sam_filled: np.ndarray,
    ck_binary: np.ndarray,
    keep_mask: np.ndarray,
    final: np.ndarray,
    source_rgb: Optional[np.ndarray] = None,
    num_components: int = 0,
    num_components_kept: int = 0,
    sam_pixel_count: int = 0,
    empty_sam_warning: bool = False,
) -> None:
    # WHAT IT DOES: Diagnostic dump for the v2.1 connectivity filter. Writes
    #   the per-stage masks + stats to DEBUG_DIR. Overwrites previous dump;
    #   only the LAST merge call survives.
    # DEPENDS ON:   cv2 (lazy), numpy, pathlib. DEBUG_DIR mkdir-on-write.
    #               Handles source_rgb=None gracefully (skips that one image).
    # AFFECTS:      writes 6-7 files to DEBUG_DIR; no return; no logic side effect.
    import cv2 as _cv2
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    ck_vis = np.clip(ck * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_ck_alpha.png"), ck_vis)

    sam_vis = (sam.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_silhouette.png"), sam_vis)

    sam_filled_vis = (sam_filled.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_filled.png"), sam_filled_vis)

    ck_binary_vis = (ck_binary.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_ck_binary.png"), ck_binary_vis)

    keep_vis = (keep_mask.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_keep_mask.png"), keep_vis)

    final_vis = np.clip(final * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_final.png"), final_vis)

    if source_rgb is not None:
        src_vis = np.clip(source_rgb * 255.0, 0.0, 255.0).astype(np.uint8)
        src_bgr = _cv2.cvtColor(src_vis, _cv2.COLOR_RGB2BGR)
        _cv2.imwrite(str(DEBUG_DIR / "debug_source_rgb.png"), src_bgr)

    H, W = ck.shape
    n_pixels = float(ck.size)
    pct_keep = float(keep_mask.sum()) / n_pixels * 100.0

    # Sample pixel locations — picked for Berto's woman-on-greenscreen test
    # clip. Adjust per shot if needed; top-left should always be confident bg.
    sample_topleft = (0, 0)
    sample_body_ctr = (H // 2, W // 2)
    sample_wall = (int(H * 0.10), int(W * 0.15))
    sample_foot = (int(H * 0.90), int(W * 0.50))

    def _fmt(name: str, y: int, x: int) -> str:
        return (
            f"PIXEL_{name}: ck={float(ck[y, x]):.4f} "
            f"sam={int(sam[y, x])} "
            f"final={float(final[y, x]):.4f}  ({y}, {x})"
        )

    stats_lines = [
        "=== v2.1 connectivity filter debug dump ===",
        f"EMPTY_SAM_WARNING: {empty_sam_warning}",
        f"SAM_PIXEL_COUNT: {sam_pixel_count}",
        f"NUM_COMPONENTS_FOUND: {num_components}",
        f"NUM_COMPONENTS_KEPT: {num_components_kept}",
        f"KEEP_MASK_PERCENT_WHITE: {pct_keep:.4f}",
        f"MEAN_FINAL: {float(final.mean()):.6f}",
        f"SHAPE_CK: {ck.shape}",
        f"SHAPE_SAM: {sam.shape}",
        f"CK_FILTER_THRESHOLD={CK_FILTER_THRESHOLD} CK_FILTER_CLOSE_PX={CK_FILTER_CLOSE_PX}",
        _fmt("TOPLEFT     ", *sample_topleft),
        _fmt("BODY_CENTER ", *sample_body_ctr),
        _fmt("WALL        ", *sample_wall),
        _fmt("FOOT        ", *sample_foot),
    ]
    (DEBUG_DIR / "debug_stats.txt").write_text("\n".join(stats_lines) + "\n")


def merge_ck_with_sam_chroma_gated(ck_alpha, sam_silhouette, source_rgb=None):
    """
    v2.1 connectivity-based filter.

    Keeps only CK regions that are topologically connected to the SAM body.
    Walls, forklift, ceiling, and other disconnected junk are removed by
    definition, not by distance tuning.

    source_rgb is unused but kept in signature for backward compatibility.
    """
    import numpy as np
    import cv2
    from scipy import ndimage

    ck = np.clip(ck_alpha.astype(np.float32), 0.0, 1.0)
    sam = (sam_silhouette > 0.5).astype(np.uint8)

    # Step 1: Fill SAM notches so the body anchor is topologically stable.
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (CK_FILTER_CLOSE_PX, CK_FILTER_CLOSE_PX)
    )
    sam_filled = cv2.morphologyEx(sam, cv2.MORPH_CLOSE, kernel)
    sam_filled = ndimage.binary_fill_holes(sam_filled).astype(np.uint8)

    # Empty SAM detection. Output will be all zeros if this triggers.
    sam_pixel_count = int(sam_filled.sum())
    empty_sam_warning = (sam_pixel_count == 0)

    # Step 2: Threshold CK permissively to catch hair tendrils.
    ck_binary = (ck > CK_FILTER_THRESHOLD).astype(np.uint8)

    # Step 3: Find connected components of the CK foreground.
    num_components, labels = cv2.connectedComponents(ck_binary, connectivity=8)

    # Step 4: Keep only components that intersect the filled SAM body.
    # Vectorized via np.isin to avoid O(N) full-resolution boolean allocs
    # in a Python loop on 4K frames.
    component_ids_to_keep = np.unique(labels[sam_filled > 0])
    component_ids_to_keep = component_ids_to_keep[component_ids_to_keep != 0]
    keep_mask = np.isin(labels, component_ids_to_keep).astype(np.uint8)

    # Step 5: Output is CK alpha within the kept components, zero elsewhere.
    final = ck * keep_mask

    if DEBUG_MODE:
        _save_debug_dump(
            ck=ck,
            sam=sam,
            sam_filled=sam_filled,
            ck_binary=ck_binary,
            keep_mask=keep_mask,
            final=final,
            source_rgb=source_rgb,
            num_components=num_components,
            num_components_kept=len(component_ids_to_keep),
            sam_pixel_count=sam_pixel_count,
            empty_sam_warning=empty_sam_warning,
        )

    return final

# Tuning guide:
# If foot halo appears (foot connects to floor through weak CK pickup):
#   RAISE CK_FILTER_THRESHOLD toward 0.03 or 0.05 so floor pixels drop
#   out of ck_binary and the connection breaks. Tradeoff: hair fringe
#   below the new threshold is also lost. If both happen at the same
#   threshold, this is the v2.2 geodesic propagation case.
# If hair clipped: LOWER CK_FILTER_THRESHOLD toward 0.01.
# If butt notch returns: RAISE CK_FILTER_CLOSE_PX toward 100 or 150.


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
) -> np.ndarray:
    # WHAT IT DOES: Public dispatcher. Routes to the v2.1 connectivity merge
    #   when USE_CHROMA_GATED_MERGE is True, otherwise falls back to Path B
    #   (max). Call sites use this single entry point so the A/B switch is a
    #   one-flag flip with no call-site changes. screen_type kwarg accepted
    #   for backward compatibility; v2.1 ignores it.
    # DEPENDS ON:   USE_CHROMA_GATED_MERGE module flag, merge_ck_with_sam (Path B),
    #               merge_ck_with_sam_chroma_gated (v2.1 connectivity).
    # AFFECTS:      every site that imports this dispatcher. Currently:
    #               resolve renderer, resolve viewer, AE viewer (composite + matte).
    if USE_CHROMA_GATED_MERGE:
        print(
            f"[merge] v2.1 connectivity branch  USE_CHROMA_GATED_MERGE=True  "
            f"source_rgb.shape={getattr(source_rgb, 'shape', None)}"
        )
        return merge_ck_with_sam_chroma_gated(
            ck_alpha, sam_silhouette, source_rgb=source_rgb,
        )
    print("[merge] Path B fallback branch  (flag=False)")
    return merge_ck_with_sam(ck_alpha, sam_silhouette)


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    # WHAT IT DOES: Snapshots the v2.1 debug PNGs (debug_*.png) under matte_-
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
        ("debug_ck_binary.png", "matte_debug_ck_binary.png"),
        ("debug_keep_mask.png", "matte_debug_keep_mask.png"),
        ("debug_final.png", "matte_debug_final.png"),
        ("debug_source_rgb.png", "matte_debug_source_rgb.png"),
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
        "  - If identical: the merge inputs (matte_debug_ck_alpha + matte_debug_sam_filled + matte_debug_keep_mask) explain the output.\n"
    )
    (DEBUG_DIR / "matte_debug_stats.txt").write_text(stats)
