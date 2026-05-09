# Last modified: 2026-05-09 | Change: v1.0 strip — remove v2.2 trimap+CFM merge. Add process_sam_matte for two-mask output mode. CK and SAM are independent now; the plugin no longer merges them.
"""v1.0 two-mask SAM matte processing.

CK matte and SAM matte are independent in v1.0. The plugin no longer
merges them — that responsibility belongs to the user inside the host
(DaVinci Fusion / AE / Premiere) where mature compositor tools already
exist.

This module now exposes the simple post-processing the user controls
via panel sliders before the SAM matte is exported:

  process_sam_matte(sam, margin_px, softness_sigma, fill_kernel_px)
      Order: fill holes → shrink/grow margin → soften.
      Returns float32 [0..1] mask, ready for export or split-view display.

The legacy dispatcher merge_ck_with_sam_active is kept as a passthrough
returning the CK matte unchanged. Existing call sites still import it
and get the CK-master-only behavior the v1.0 design wants. They'll be
rewired to call process_sam_matte directly during Phase B/C.

The v2.2 trimap + Closed-Form Matting + CK injection chain is gone.
For the prior architecture (chromacity-aware merge, internal Gaussian
smoothing, hard clamp outside dilated SAM, debug dumps), see git tag
v2.2-experimental-2026-05-08.
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np


SAM_BINARIZE_THRESHOLD = 0.5

# v1.0 two-mask mode: the merge dispatcher is a passthrough, the v2.2
# chain is removed. These flags are kept for any downstream code that
# still reads them; both are False so v2.2 paths never run.
USE_CHROMA_GATED_MERGE = False
DEBUG_ENABLED = False
DEBUG_MODE = False

# Saturation ramp endpoints (logit space) — see logits_to_soft_mask below.
# A 4-logit-wide soft band gives a 2-4 px feather at typical SAM 2 grad
# magnitudes around the contour. Berto verified on 4K Kitchen Fight.
SAM_SOFT_LOGIT_LO = -2.0
SAM_SOFT_LOGIT_HI = 2.0

# v1.0 always-on baseline smoothing of the soft SAM matte. Operates on
# CONTINUOUS values now (the previous MORPH_OPEN k=3 was carving 3 px
# polygonal facets out of the binary SAM silhouette — visible bumpiness
# Berto called out 2026-05-09). Sigma 1.0 dissolves SAM 2's per-pixel
# decoder jitter without touching the contour shape.
SAM_BASELINE_SMOOTH_SIGMA = 1.0


def binarize_sam_silhouette(sam: np.ndarray, threshold: float = SAM_BINARIZE_THRESHOLD) -> np.ndarray:
    # WHAT IT DOES: Threshold a continuous SAM mask to binary {0.0, 1.0} float32.
    # DEPENDS ON:   numpy. Caller pre-applies sigmoid if input is logit-space.
    # AFFECTS:      Kept for legacy contracts (sam2_mask_obj{N}.png hard-mask
    #               files the panel still expects). The v1.0 two-mask render
    #               path no longer binarises before process_sam_matte —
    #               continuous SAM matte flows end-to-end via the saturation
    #               ramp. See logits_to_soft_mask for the entry point.
    return (np.asarray(sam, dtype=np.float32) >= float(threshold)).astype(np.float32)


def logits_to_soft_mask(
    logits: np.ndarray,
    lo: float = SAM_SOFT_LOGIT_LO,
    hi: float = SAM_SOFT_LOGIT_HI,
) -> np.ndarray:
    """Convert SAM 2 mask-decoder logits to a soft 0..1 mask via SATURATION RAMP.

    Mapping:
        logit >= hi   -> 1.0  (solid interior, kills decoder texture)
        logit <= lo   -> 0.0  (solid background)
        lo < L < hi   -> linear ramp (soft edge band, ~2-4 px feather)

    Why a ramp instead of sigmoid: SAM 2's mask-decoder logits in confident
    interior pixels sit in the +2..+6 range, not +20..+30. Sigmoid maps that
    to 0.88..0.998 with subtle per-pixel variation that tracks image texture.
    Multiplied through the alpha downstream, that variation prints as
    horizontal banding and checker artifacts inside the body silhouette —
    invisible at 1080p, very visible at 4K. The ramp pins interior to a
    flat 1.0 and only soft-feathers within [lo, hi] across the contour.

    Berto verified the checker artifact on 4K Kitchen Fight footage
    2026-04-28; this is the same fix originally shipped in the AE viewer
    and now the canonical soft-mask path for every SAM 2 call site.

    Args:
        logits: float array (any shape) of raw SAM 2 mask-decoder logits.
        lo, hi: ramp endpoints in logit space (defaults -2..+2).

    Returns:
        float32 array of same shape, clipped to [0, 1].
    """
    L = np.asarray(logits, dtype=np.float32)
    span = float(hi - lo)
    if span <= 0:
        return (L >= float(hi)).astype(np.float32)
    return np.clip((L - float(lo)) / span, 0.0, 1.0)


def union_binary_silhouettes(silhouettes: Iterable[np.ndarray]) -> Optional[np.ndarray]:
    # WHAT IT DOES: OR-combine N already-binarised SAM silhouettes via per-pixel max.
    # DEPENDS ON:   all silhouettes share identical H x W shape. None entries dropped.
    # AFFECTS:      Multi-object renders (MASK 1 + MASK 2). Single-object: pass-through.
    valid = [np.asarray(s, dtype=np.float32) for s in silhouettes if s is not None]
    if not valid:
        return None
    out = valid[0]
    for s in valid[1:]:
        out = np.maximum(out, s)
    return out.astype(np.float32, copy=False)


def process_sam_matte(
    sam: np.ndarray,
    margin_px: float = 0.0,
    softness_sigma: float = 0.0,
    fill_kernel_px: int = 0,
) -> np.ndarray:
    """Apply the v1.0 SAM matte post-processing chain on a CONTINUOUS soft mask.

    Pre-Option C this function operated on a binary silhouette and applied a
    MORPH_OPEN k=3 baseline, which carved 3 px polygonal facets out of the
    contour. Bumpiness Berto called out 2026-05-09 was that artifact.

    Option C: callers feed a soft post-ramp mask in [0, 1] (see
    logits_to_soft_mask). The pipeline preserves softness — only the user-
    controlled FILL HOLES and MARGIN stages binarise internally, and only
    when the user opts in (defaults are no-op). The baseline is now a small
    Gaussian on continuous values, which dissolves SAM 2 decoder jitter
    without rebuilding the contour out of polygon stamps.

    Order of operations:
      1. BASELINE smoothing (always on) — Gaussian sigma SAM_BASELINE_SMOOTH_SIGMA
         on continuous values. Smooths sub-pixel decoder jitter.
      2. Fill holes — user-controlled MORPH_CLOSE on a 0.5-thresholded copy.
         Pixels in [0, 1] flow through; only run when user sets fill_kernel_px>0.
      3. Margin — user-controlled erode/dilate. Same opt-in semantics.
      4. Softness — user-controlled Gaussian for additional edge feather.

    Args:
        sam: (H, W) float32 soft mask in [0, 1]. Already post-ramp (or post-
             sigmoid). Binary input still works but loses the soft-edge benefit.
        margin_px: -50..+50 typical. Negative erodes, positive dilates.
        softness_sigma: 0..30 typical. Gaussian sigma in pixels.
        fill_kernel_px: 0..50 typical. MORPH_CLOSE kernel; 0 = skip.

    Returns:
        (H, W) float32 mask in [0, 1].
    """
    import cv2

    out = np.asarray(sam, dtype=np.float32)

    # ── 1. BASELINE smoothing on continuous values (always on) ──
    if SAM_BASELINE_SMOOTH_SIGMA and SAM_BASELINE_SMOOTH_SIGMA > 0:
        kbs = max(3, int(SAM_BASELINE_SMOOTH_SIGMA * 6) | 1)
        out = cv2.GaussianBlur(
            out, (kbs, kbs),
            sigmaX=float(SAM_BASELINE_SMOOTH_SIGMA),
            sigmaY=float(SAM_BASELINE_SMOOTH_SIGMA),
        )

    # ── 2. Fill holes (user-controlled MORPH_CLOSE on a binarised copy) ──
    # Only engages when the user dials FILL HOLES > 0. Default is 0, so the
    # soft edge survives undisturbed. When engaged, the result becomes
    # binary at the close stage and propagates that way through the rest
    # of the pipeline — that's the user's choice.
    if fill_kernel_px and fill_kernel_px > 0:
        k = max(3, int(fill_kernel_px) | 1)  # round up to odd
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        binary_u8 = (out > 0.5).astype(np.uint8) * 255
        closed = cv2.morphologyEx(binary_u8, cv2.MORPH_CLOSE, kernel)
        out = closed.astype(np.float32) / 255.0

    # ── 3. Margin (bidirectional: negative=erode, positive=dilate) ──
    # Sub-pixel via lerp. Same opt-in semantics as fill — binarises internally
    # only when engaged.
    margin_f = float(margin_px or 0.0)
    if margin_f != 0.0:
        m_abs = abs(margin_f)
        int_m = int(m_abs)
        frac = m_abs - int_m
        binary_u8 = (np.clip(out, 0, 1) * 255).astype(np.uint8)
        op = cv2.dilate if margin_f > 0 else cv2.erode

        if int_m > 0:
            k_lo = int_m * 2 + 1
            lo = op(binary_u8, cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (k_lo, k_lo)
            )).astype(np.float32) / 255.0
        else:
            lo = out.copy()

        if frac > 0:
            k_hi = (int_m + 1) * 2 + 1
            hi = op(binary_u8, cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (k_hi, k_hi)
            )).astype(np.float32) / 255.0
            out = lo * (1.0 - frac) + hi * frac
        else:
            out = lo

    # ── 4. User softness (Gaussian) ──
    sigma = float(softness_sigma or 0.0)
    if sigma > 0:
        k = max(3, int(sigma * 6) | 1)
        out = cv2.GaussianBlur(out, (k, k), sigmaX=sigma, sigmaY=sigma)

    return np.clip(out, 0.0, 1.0)


# ──────────────────────────────────────────────────────────────────────
# Legacy passthrough shims
# Kept so existing call sites still work during the Phase A→B→C migration.
# In v1.0 two-mask mode no merge happens in the plugin — the dispatcher
# returns the CK matte unchanged. Callers that want SAM post-processing
# call process_sam_matte directly. After Phase B/C wires every call site,
# these shims can be deleted.
# ──────────────────────────────────────────────────────────────────────


def merge_ck_with_sam(ck_alpha: np.ndarray, sam_silhouette: Optional[np.ndarray]) -> np.ndarray:
    """v1.0 passthrough: returns CK unchanged. SAM is exported separately."""
    return np.asarray(ck_alpha, dtype=np.float32).copy()


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
) -> np.ndarray:
    """v1.0 passthrough dispatcher: returns CK unchanged.

    Pre-v1.0 this was the v2.2 chroma-gated merge. v1.0 decouples CK and SAM —
    plugin no longer merges them, the user composites in their host.
    Existing imports keep working; the displayed/rendered alpha is just CK.
    """
    return np.asarray(ck_alpha, dtype=np.float32).copy()


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    """v2.2 debug dump — no-op in v1.0."""
    return
