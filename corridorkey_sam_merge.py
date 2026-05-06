# Last modified: 2026-05-06 | Change: ADD chroma-gated merge + dispatcher (USE_CHROMA_GATED_MERGE flag) | Full history: git log
"""Path B SAM2 + CK alpha merge — final = max(CK, threshold(SAM)).

REPLACES the apply_sam2_junk_kill / apply_sam2_gate_* post-hoc combine
architecture in sam2_combine.py. Those helpers stay on disk as dead code
(unreferenced from any call site) until Path B is validated on the test
clip, at which point they get removed in a separate cleanup commit.

CK is the base layer. SAM contributes ONLY where CK is lower than SAM.
CK is never overridden. SAM cannot stomp CK detail. The user clicks 2-3
dots on the WHOLE body and SAM returns a whole-body silhouette; the
plugin does the region isolation math invisibly via this module.

Per Berto 2026-05-05 spec — algebraic form (kept literal in code so the
implementation reads back to the brief):

    difference = clip(SAM_binary - CK, 0, 1)   # pixels SAM thinks are body that CK rated as background
    final      = clip(CK + difference, 0, 1)   # CK preserved, missing regions filled

Mathematically equivalent to per-pixel max(CK, SAM_binary). Algebraic
form preferred for spec traceability — do NOT swap to np.maximum without
updating the docstring and Berto's brief.
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np


# Threshold matches the existing 0.5 convention used by every viewer
# binarisation site (post-sigmoid SAM output). Raise to make SAM more
# conservative; lower to let more of SAM's confidence band contribute.
SAM_BINARIZE_THRESHOLD = 0.5

# Active merge mode toggle. Flip and re-deploy to A/B between Path B and chroma-gated.
# Per Berto 2026-05-06: True for chroma-gated test; flip to False to fall back to Path B.
# Test clip with False already validated 2026-05-05; junk-in-non-green is the failure
# case that chroma-gating addresses.
USE_CHROMA_GATED_MERGE = True

# Chroma test threshold — per-pixel green excess (G - max(R, B)) above which the pixel
# counts as "on-green" and CK rules. 0.05 matches sam2_combine.apply_sam2_gate_additive's
# is_screen threshold (line 185) for cross-module consistency. Lower catches more
# spilled-edge pixels as on-green; higher restricts CK rule to clearly-green pixels.
CHROMA_GATE_THRESHOLD = 0.05


def binarize_sam_silhouette(sam: np.ndarray, threshold: float = SAM_BINARIZE_THRESHOLD) -> np.ndarray:
    # WHAT IT DOES: Threshold a continuous SAM mask to binary {0.0, 1.0} float32.
    # DEPENDS ON:   numpy. Caller pre-applies sigmoid if input is logit-space.
    # AFFECTS:      every Path B merge — controls SAM silhouette edge sharpness.
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
    # WHAT IT DOES: Path B merge — CK preserved, SAM adds pixels CK rated below SAM.
    #               Returns final = clip(CK + clip(SAM - CK, 0, 1), 0, 1).
    # DEPENDS ON:   ck_alpha and sam_silhouette must share identical H x W shape.
    #               Both float32 in [0, 1]. sam_silhouette should be ALREADY binary —
    #               call binarize_sam_silhouette first if input is continuous.
    # AFFECTS:      every per-frame and per-preview matte once Path B is wired in.
    #               When sam_silhouette is None, returns a CK copy (CK alone is the answer).
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if sam_silhouette is None:
        return ck.copy()
    sam = np.asarray(sam_silhouette, dtype=np.float32)
    if ck.shape != sam.shape:
        raise ValueError(f"shape mismatch: ck={ck.shape}, sam={sam.shape}")
    # DANGER ZONE FRAGILE: algebraic form is the spec contract — do NOT collapse
    # to np.maximum(ck, sam) without updating the docstring and Berto's 2026-05-05
    # brief. The two are mathematically equivalent for inputs in [0, 1] but the
    # algebraic form documents intent ("difference is what SAM adds beyond CK").
    # breaks: spec traceability when anyone diffs against the brief.
    # depends on: sam already binary; soft sam will hard-stomp CK soft-edge alpha.
    difference = np.clip(sam - ck, 0.0, 1.0)
    final = np.clip(ck + difference, 0.0, 1.0)
    return final


def compute_chroma_weight(
    source_rgb: np.ndarray,
    screen_type: str = "green",
    threshold: float = CHROMA_GATE_THRESHOLD,
    soft_band: float = 0.0,
    dilate_px: int = 0,
) -> np.ndarray:
    # WHAT IT DOES: Per-pixel float [0, 1] weight encoding "is this pixel on the
    #   green-screen side?" 1 = on-green (CK rules), 0 = off-green (SAM rules).
    #   Reuses the G - max(R, B) chroma score the sam2_combine.py family uses.
    # DEPENDS ON:   source_rgb is float32 (H, W, 3) in [0, 1]. Caller handles dtype.
    #               cv2 imported lazily ONLY when dilate_px > 0.
    # AFFECTS:      determines per-pixel CK vs SAM authority in chroma-gated merge.
    if screen_type == "blue":
        chroma_score = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma_score = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma_score = np.clip(chroma_score, 0.0, 1.0)

    # DANGER ZONE FRAGILE: do NOT add Gaussian blur to weight or chroma_score.
    # SMART BLEND in sam2_combine.apply_sam2_gate_weighted (line 191) stacked
    # three sources of softness — chroma * 5.0 ramp + Gaussian on weight +
    # Gaussian on softened SAM gate — and produced 50% ghost bands at body-green
    # edges where CK soft alpha 0.5 blended with SAM 1.0 gave 0.75 output.
    # breaks: ghost bands at body-green silhouette edges (Berto 2026-05-01).
    # depends on: SAM stays binary downstream and chroma weight stays sharp.
    if soft_band <= 0.0:
        # Hard binary — no soft transition. Berto-approved 2026-05-06 default.
        weight = (chroma_score >= threshold).astype(np.float32)
    else:
        # Smoothstep ramp across [threshold, threshold + soft_band]. OFF by default
        # because of the SMART BLEND ghost history. Only enable for explicit tuning.
        t = np.clip((chroma_score - threshold) / soft_band, 0.0, 1.0)
        weight = (t * t * (3.0 - 2.0 * t)).astype(np.float32)

    if dilate_px > 0:
        # Optional: extend on-green region into body interior by dilate_px pixels.
        # Solves the case where body-skin pixels have low chroma (no green spill)
        # but spatially sit inside the green-screen area (e.g., the butt-notch
        # case from 2026-05-05 testing). OFF by default; turn on if the test
        # clip shows that failure mode.
        import cv2 as _cv2
        binary = (weight > 0.5).astype(np.uint8)
        _k = int(dilate_px) * 2 + 1
        kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (_k, _k))
        dilated = _cv2.dilate(binary, kernel).astype(np.float32)
        weight = np.maximum(weight, dilated)

    return weight


def merge_ck_with_sam_chroma_gated(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: np.ndarray,
    screen_type: str = "green",
    threshold: float = CHROMA_GATE_THRESHOLD,
    soft_band: float = 0.0,
    dilate_px: int = 0,
) -> np.ndarray:
    # WHAT IT DOES: Chroma-gated CK + SAM merge per Berto 2026-05-06.
    #     final = weight * CK + (1 - weight) * SAM_binary
    #   where weight is 1 (use CK) on green-screen pixels, 0 (use SAM) off-green.
    #   On-green: CK is authoritative — NN keys body cleanly on green. SAM has
    #     no authority here, so SAM body-shape errors (the butt notch from
    #     2026-05-05 testing) cannot damage CK.
    #   Off-green: SAM is authoritative — kills false-positive CK pixels in
    #     non-green areas (concrete walls, props) and ADDS body parts CK
    #     missed (foot stepping off the green floor).
    # DEPENDS ON:   ck_alpha and sam_silhouette are (H, W) float32 in [0, 1] with
    #               matching shapes. source_rgb is (H, W, 3) float32 in [0, 1].
    #               compute_chroma_weight, binarize_sam_silhouette.
    # AFFECTS:      every per-frame and per-preview matte when USE_CHROMA_GATED_MERGE
    #               is True. Replaces Path B's chroma-blind max(CK, SAM).
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if sam_silhouette is None:
        return ck.copy()
    sam = binarize_sam_silhouette(sam_silhouette)
    if ck.shape != sam.shape:
        raise ValueError(f"shape mismatch: ck={ck.shape}, sam={sam.shape}")
    weight = compute_chroma_weight(
        source_rgb, screen_type=screen_type,
        threshold=threshold, soft_band=soft_band, dilate_px=dilate_px,
    )
    if weight.shape != ck.shape:
        raise ValueError(f"chroma weight shape mismatch: weight={weight.shape}, ck={ck.shape}")
    final = weight * ck + (1.0 - weight) * sam
    return np.clip(final, 0.0, 1.0).astype(np.float32)


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
) -> np.ndarray:
    # WHAT IT DOES: Public dispatcher. Routes to chroma-gated merge when the
    #   module flag USE_CHROMA_GATED_MERGE is True AND source_rgb is provided;
    #   otherwise falls back to Path B (chroma-blind max). Call sites use this
    #   single entry point so the A/B switch is a one-flag flip with no
    #   call-site changes.
    # DEPENDS ON:   USE_CHROMA_GATED_MERGE module flag, merge_ck_with_sam (Path B),
    #               merge_ck_with_sam_chroma_gated.
    # AFFECTS:      every site that imports this dispatcher. Currently:
    #               resolve renderer, resolve viewer, AE viewer (composite + matte).
    if USE_CHROMA_GATED_MERGE and source_rgb is not None:
        return merge_ck_with_sam_chroma_gated(
            ck_alpha, sam_silhouette, source_rgb, screen_type=screen_type,
        )
    return merge_ck_with_sam(ck_alpha, sam_silhouette)
