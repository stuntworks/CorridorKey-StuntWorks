# Last modified: 2026-05-07 | Change: SOFT_ZONE_SAM_BUFFER_PX 15->40 to widen buffer past CK natural soft edge extent so hair/fringe survives | Full history: git log
"""CK-confidence-based merge per Berto 2026-05-07.

REPLACES the chroma-gated merge + outer SAM kill mask architecture. The
chroma signal cannot distinguish "body part on green that needs CK soft
alpha" from "body part on floor that needs SAM tight cut" — both are
body-edge pixels near a transition, and any combination of chroma
threshold / dilation / kill-mask buffer keeps trading hair preservation
against shoe halo elimination. The 2026-05-06 evening diagnostic confirmed
this: in the kill-mask edge zone, on-green=100% / off-green=0%, meaning
the small buffer was never selected at body edges.

Routing now uses CK's own alpha values, not chroma:

    soft_zone = (CK_SOFT_LO < ck < CK_SOFT_HI)
    final     = where(soft_zone, ck, ck * sam_binary)

  - CK soft transition (ck typically 0.05..0.95 at green edges): CK wins
    outright. Hair, butt, fingers, fine detail preserved exactly as CK
    keys them. SAM does not vote here.
  - CK confident foreground (ck >= 0.95): SAM gates it. SAM=1 keeps it
    (body, foot below knee). SAM=0 kills it (walls, forklift, shoe halo
    against floor — CK false positives that SAM correctly excludes).
  - CK confident background (ck <= 0.05): output is 0. Green killed.

No chroma signal. No dilation parameters. No kill mask buffers.

The post-hoc apply_sam2_junk_kill / apply_sam2_gate_* helpers in
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

# Diagnostic toggle — when True, every confidence-based merge call dumps
# 4 PNGs + stats.txt to DEBUG_DIR. Flip False once architecture is
# validated. Costs ~10-20 ms per call on a 4K frame.
DEBUG_ENABLED = True
DEBUG_DIR = Path(__file__).parent / ".chroma_debug"

# Active merge mode toggle. True = confidence-based routing (current).
# False = Path B fallback (chroma-blind max(CK, SAM)). Call sites use the
# single dispatcher merge_ck_with_sam_active so flipping this flag is a
# one-deploy A/B without touching call sites. Path B kept on disk for
# hot-revert; the chroma-gated math is gone.
USE_CHROMA_GATED_MERGE = True

# CK confidence band for the soft-zone gate. Per Berto 2026-05-07 brief.
# The two knobs to tune if results are off:
#   CK_SOFT_LO too low  -> noisy near-zero CK pixels enter soft zone, get
#                          preserved instead of killed. Raise toward 0.10.
#   CK_SOFT_HI too high -> CK confident-fg false positives (walls keyed at
#                          0.95+) enter soft zone, escape SAM's gate, walls
#                          re-appear. Drop toward 0.90.
#   CK_SOFT_LO too high -> hair tendrils with low alpha get killed instead
#                          of preserved. Drop toward 0.02.
#   CK_SOFT_HI too low  -> body interior (CK alpha 0.95+) enters soft zone
#                          and bypasses SAM, which is fine functionally
#                          (interior is fg either way) but masks bugs.
CK_SOFT_LO = 0.05
CK_SOFT_HI = 0.7

# Soft-zone SAM gate buffer — per Berto 2026-05-07 morning. Without a SAM gate
# in the soft zone, every CK transition (wall outlines, floor edges, drape
# edges, cube outlines) trusts CK exclusively and survives in the matte. Gate
# the soft zone with a SAM silhouette dilated by this many pixels so hair
# tendrils just past the tight SAM edge survive while wall outlines far from
# SAM die. Tune up if hair clips, down if wall outlines persist.
SOFT_ZONE_SAM_BUFFER_PX = 40


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
    soft_zone: np.ndarray,
    final: np.ndarray,
    sam_buffered: Optional[np.ndarray] = None,
) -> None:
    # WHAT IT DOES: Diagnostic dump for the confidence-based merge. Writes 4-5
    #   PNGs + debug_stats.txt to DEBUG_DIR. Overwrites previous dump; only the
    #   LAST merge call survives.
    # DEPENDS ON:   cv2 (lazy), numpy, pathlib. DEBUG_DIR mkdir-on-write.
    # AFFECTS:      writes 5-6 files to DEBUG_DIR; no return; no logic side effect.
    import cv2 as _cv2
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    ck_vis = np.clip(ck * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_ck_alpha.png"), ck_vis)

    sam_vis = np.clip(sam * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_silhouette.png"), sam_vis)

    soft_vis = (soft_zone.astype(np.uint8)) * 255
    _cv2.imwrite(str(DEBUG_DIR / "debug_soft_zone.png"), soft_vis)

    if sam_buffered is not None:
        buffered_vis = np.clip(sam_buffered * 255.0, 0.0, 255.0).astype(np.uint8)
        _cv2.imwrite(str(DEBUG_DIR / "debug_sam_buffered.png"), buffered_vis)

    final_vis = np.clip(final * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_final.png"), final_vis)

    H, W = ck.shape
    n_pixels = float(ck.size)
    pct_soft = float(soft_zone.sum()) / n_pixels * 100.0
    pct_confident_fg = float((ck >= CK_SOFT_HI).sum()) / n_pixels * 100.0
    pct_confident_bg = float((ck <= CK_SOFT_LO).sum()) / n_pixels * 100.0

    sample_tl_y, sample_tl_x = 0, 0
    sample_ctr_y, sample_ctr_x = H // 2, W // 2
    sample_halo_y, sample_halo_x = int(H * 0.85), int(W * 0.6)

    def _fmt_sample(name: str, y: int, x: int) -> str:
        buf_str = (
            f" sam_buffered={float(sam_buffered[y, x]):.2f}"
            if sam_buffered is not None else ""
        )
        return (
            f"sample {name} ({y}, {x}): "
            f"ck={float(ck[y, x]):.4f} "
            f"sam={float(sam[y, x]):.2f}"
            f"{buf_str} "
            f"soft_zone={bool(soft_zone[y, x])} "
            f"final={float(final[y, x]):.4f}"
        )

    stats_lines = [
        "=== confidence-based merge debug dump ===",
        f"shapes: ck={ck.shape} sam={sam.shape} final={final.shape}",
        f"CK_SOFT_LO={CK_SOFT_LO} CK_SOFT_HI={CK_SOFT_HI} "
        f"SOFT_ZONE_SAM_BUFFER_PX={SOFT_ZONE_SAM_BUFFER_PX}",
        f"% pixels in soft_zone (CK gate via sam_buffered)      = {pct_soft:.2f}%",
        f"% pixels CK >= {CK_SOFT_HI} (confident fg, SAM gates) = {pct_confident_fg:.2f}%",
        f"% pixels CK <= {CK_SOFT_LO} (confident bg, output 0)  = {pct_confident_bg:.2f}%",
        f"mean(final) = {float(final.mean()):.4f}",
    ]
    if sam_buffered is not None:
        soft_pixels = float(soft_zone.sum())
        if soft_pixels > 0:
            soft_in_buffered = (
                float((soft_zone & (sam_buffered > 0.5)).sum()) / soft_pixels * 100.0
            )
        else:
            soft_in_buffered = 0.0
        stats_lines.append(
            f"% soft_zone pixels inside sam_buffered = {soft_in_buffered:.2f}%  "
            f"(high = body edges; low = wall edges far from SAM)"
        )
    stats_lines.extend([
        _fmt_sample("top-left   ", sample_tl_y, sample_tl_x),
        _fmt_sample("frame ctr  ", sample_ctr_y, sample_ctr_x),
        _fmt_sample("halo zone  ", sample_halo_y, sample_halo_x),
    ])
    (DEBUG_DIR / "debug_stats.txt").write_text("\n".join(stats_lines) + "\n")


def merge_ck_with_sam_chroma_gated(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    **_kwargs,
) -> np.ndarray:
    # WHAT IT DOES: Confidence-based merge per Berto 2026-05-07. Routes
    #   per-pixel using CK's own alpha values:
    #     - soft_zone (CK_SOFT_LO < ck < CK_SOFT_HI): final = ck. SAM does
    #       not vote. Preserves hair, butt, fingertips — wherever CK is
    #       transitioning, CK is the truth.
    #     - confident (ck <= LO or ck >= HI): final = ck * sam. SAM gates
    #       confident pixels. Walls / forklift / shoe halos where CK has
    #       false positives get killed because SAM=0; foot below knee
    #       where CK is right gets kept because SAM=1.
    #   Function name kept stable so dispatcher and call sites are untouched.
    #   Function signature accepts source_rgb + **_kwargs for backward
    #   compatibility with the chroma-gated dispatcher's screen_type kwarg;
    #   neither is used in the new logic.
    # DEPENDS ON:   numpy. ck_alpha and sam_silhouette are (H, W) float32 in
    #               [0, 1] with matching shapes when sam is not None. CK_SOFT_LO,
    #               CK_SOFT_HI, DEBUG_ENABLED, _save_debug_dump.
    # AFFECTS:      every per-frame and per-preview matte. Replaces both Path B
    #               (max) and the chroma-gated + kill-mask architecture.
    ck = np.clip(np.asarray(ck_alpha, dtype=np.float32), 0.0, 1.0)
    if sam_silhouette is None:
        return ck.copy()
    sam = (np.asarray(sam_silhouette, dtype=np.float32) > 0.5).astype(np.float32)
    if ck.shape != sam.shape:
        raise ValueError(f"shape mismatch: ck={ck.shape}, sam={sam.shape}")
    # Soft-zone gate via SAM dilated by SOFT_ZONE_SAM_BUFFER_PX. Body edges
    # sit inside this buffer (CK soft alpha preserved); wall outlines and
    # other transitions far from SAM fall outside it (killed).
    import cv2 as _cv2
    _k = max(1, int(SOFT_ZONE_SAM_BUFFER_PX))
    _kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (_k, _k))
    sam_buffered = _cv2.dilate(sam.astype(np.uint8), _kernel).astype(np.float32)
    soft_zone = (ck > CK_SOFT_LO) & (ck < CK_SOFT_HI)
    final = np.where(soft_zone, ck * sam_buffered, ck * sam).astype(np.float32)
    if DEBUG_ENABLED:
        try:
            _save_debug_dump(
                ck=ck, sam=sam, soft_zone=soft_zone, final=final,
                sam_buffered=sam_buffered,
            )
        except Exception as _e:
            print(f"[merge debug] dump failed: {_e}")
    return final


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
) -> np.ndarray:
    # WHAT IT DOES: Public dispatcher. Routes to the active confidence-based
    #   merge when USE_CHROMA_GATED_MERGE is True, otherwise falls back to
    #   Path B (max). Call sites use this single entry point so the A/B
    #   switch is a one-flag flip with no call-site changes.
    # DEPENDS ON:   USE_CHROMA_GATED_MERGE module flag, merge_ck_with_sam (Path B),
    #               merge_ck_with_sam_chroma_gated (confidence-based).
    # AFFECTS:      every site that imports this dispatcher. Currently:
    #               resolve renderer, resolve viewer, AE viewer (composite + matte).
    if USE_CHROMA_GATED_MERGE:
        print(
            f"[merge] confidence-based branch  USE_CHROMA_GATED_MERGE=True  "
            f"source_rgb.shape={getattr(source_rgb, 'shape', None)}"
        )
        return merge_ck_with_sam_chroma_gated(
            ck_alpha, sam_silhouette, source_rgb=source_rgb,
        )
    print("[merge] Path B fallback branch  (flag=False)")
    return merge_ck_with_sam(ck_alpha, sam_silhouette)


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    # WHAT IT DOES: Snapshots the confidence-based debug PNGs (debug_*.png) under
    #   matte_-prefixed names so they survive any subsequent composite-mode
    #   re-render that would overwrite them, then saves the final post-processed
    #   alpha as matte_debug_final_displayed.png + writes matte_debug_stats.txt
    #   with the ordered list of ops applied to the merge output before display.
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
        ("debug_sam_buffered.png", "matte_debug_sam_buffered.png"),
        ("debug_soft_zone.png", "matte_debug_soft_zone.png"),
        ("debug_final.png", "matte_debug_final.png"),
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
        "  - If identical: the merge inputs (matte_debug_ck_alpha + matte_debug_sam_silhouette + matte_debug_soft_zone) explain the output.\n"
    )
    (DEBUG_DIR / "matte_debug_stats.txt").write_text(stats)
