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

# 2026-05-13: v2.2 chroma-gated merge restored for combined CK+SAM2 export.
# When True (default), merge_ck_with_sam_active routes to the trimap+CFM path
# in merge_ck_with_sam_chroma_gated (CK injected post-solve in unknown band,
# hard clamp outside dilated SAM). When False, falls back to Path B
# (max-style) merge_ck_with_sam. Live-preview (reprocess_with_cached) stays
# CK-only regardless: this flag only controls the export-time merge.
USE_CHROMA_GATED_MERGE = True
DEBUG_ENABLED = False
DEBUG_MODE = False

# 2026-05-16: chroma-weight blend restored from c96deb07 (May 6 perfect-map state).
# Hard binary chroma test: pixel is on-green if (G - max(R, B)) >= threshold.
# On-green pixels => CK rules (soft hair alpha shines through).
# Off-green pixels => SAM_binary rules (hard kill of floor/wire/walls).
# Trimap+CFM architecture from 9093bb8 is archived as _v22_trimap_cfm_archived
# below for hot-revert if this regresses.
CHROMA_GATE_THRESHOLD = 0.20  # UNUSED in confidence-based merge; kept for hot-revert
CHROMA_GATE_DILATE_PX = 0     # UNUSED in confidence-based merge; kept for hot-revert
CHROMA_GATE_SOFT_BAND = 0.0   # UNUSED in confidence-based merge; kept for hot-revert

# CK-confidence-based routing — evolved version from 0ffc24b1 (2026-05-07).
# Soft zone gated by dilated SAM buffer so CK partial alpha far from body
# (wire echoes, structure edges) get killed. CK_SOFT_HI lowered to 0.7
# (from 0.95) so "almost confident" CK pixels also route through SAM gate.
# NO Gaussian blur anywhere — cv2.dilate binary only — see
# [[feedback-ck-no-gaussian-on-sam-mask]].
CK_SOFT_LO = 0.05
CK_SOFT_HI = 0.7
# 2026-05-16: bumped 40 → 80 after Berto's first test on motion-blur clip.
# 40px wasn't reaching motion-blurred fingertips that extend past body
# silhouette + leg-meets-green boundary line. 80px gives CK twice the
# working area for soft alpha while still killing wire echoes 80+ px from
# body. Tune higher if hair/fingers still cut, lower if floor mat near
# body's feet leaks through.
# 2026-05-18: 80 -> 25 -> 40.
#   80: lateral leak right of feet + ankle seam (image #39).
#   25: line killed, but feet had spill, body edge eaten (butt soft).
#       Berto: "needs bigger overlap into green."
#   40: middle. Should reach further into green to kill foot spill without
#       bringing back the lateral seam at the chroma boundary.
SOFT_ZONE_SAM_BUFFER_PX = 40

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


def pad_to_square(arr: np.ndarray, fill: int = 0):
    """Letterbox-pad an HxW or HxWxC array to a square. Fixes SAM 2's anisotropic
    stretch (Phase 0a, 2026-05-09).

    SAM 2's image encoder hard-resizes any input to a 1024x1024 SQUARE — not
    long-edge fit. A 4K landscape (3840x2160) gets x-scale 3.75 and y-scale
    2.11, a 1.78x non-uniform distortion the model never saw at training time.
    The wavy contour Berto called out is partly that geometry warp; padding
    to square first gives SAM uniform downsampling and uniform output.

    Returns:
        (padded, (top, bottom, left, right)) — padding amounts in source pixels.
        padded.shape is (target, target) or (target, target, C).
    """
    h, w = arr.shape[:2]
    target = max(int(h), int(w))
    pad_h = target - h
    pad_w = target - w
    top = pad_h // 2
    bottom = pad_h - top
    left = pad_w // 2
    right = pad_w - left
    if arr.ndim == 3:
        padded = np.full((target, target, arr.shape[2]), fill, dtype=arr.dtype)
        padded[top:top + h, left:left + w, :] = arr
    else:
        padded = np.full((target, target), fill, dtype=arr.dtype)
        padded[top:top + h, left:left + w] = arr
    return padded, (top, bottom, left, right)


def unpad_from_square(arr: np.ndarray, padding) -> np.ndarray:
    """Inverse of pad_to_square — crop a square padded array back to the source
    rectangle. padding = (top, bottom, left, right) returned by pad_to_square.
    """
    top, bottom, left, right = padding
    h = arr.shape[0] - top - bottom
    w = arr.shape[1] - left - right
    if arr.ndim == 3:
        return arr[top:top + h, left:left + w, :]
    return arr[top:top + h, left:left + w]


def shift_points_for_padding(points, padding):
    """Shift (x, y) prompt point coords from source-frame space into padded-square
    space. SAM 2 sees the padded frame, so click coords have to follow.

    points: iterable of (x, y) or [x, y] in source pixel coords.
    padding: (top, bottom, left, right) returned by pad_to_square.
    """
    top, _bottom, left, _right = padding
    return [[int(p[0]) + int(left), int(p[1]) + int(top)] for p in points]


_SAM2_PNG_LOADER_PATCHED = False


def patch_sam2_loader_for_png() -> None:
    """One-time monkey patch — make SAM 2's video predictor accept PNG (and PNG-
    stored frames) in its init_state(video_path=...) directory mode.

    SAM 2 vendored `load_video_frames_from_jpg_images` at sam2/utils/misc.py:213
    filters os.listdir to .jpg/.jpeg only. The underlying loader
    (_load_img_as_tensor) opens with PIL, which already auto-detects format from
    magic bytes — so PNG works once it gets past the extension filter. This
    patch re-implements the function with a widened whitelist (.jpg/.jpeg/.png)
    and is idempotent — calling it twice is a no-op.

    Why we need it (Phase 0b, 2026-05-09): the previous JPEG q=95 re-encode in
    the frame export path was a self-inflicted lossy step. Switching to PNG
    keeps the source data intact through SAM's ingest.
    """
    global _SAM2_PNG_LOADER_PATCHED
    if _SAM2_PNG_LOADER_PATCHED:
        return

    import os
    import torch
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(x, **_):
            return x
    import sam2.utils.misc as _sam_misc

    _orig_loader = _sam_misc.load_video_frames_from_jpg_images
    _ALLOWED_EXT = (".jpg", ".jpeg", ".JPG", ".JPEG", ".png", ".PNG")

    def _patched(video_path, image_size, offload_video_to_cpu,
                 img_mean=(0.485, 0.456, 0.406),
                 img_std=(0.229, 0.224, 0.225),
                 async_loading_frames=False,
                 compute_device=torch.device("cuda")):
        if not (isinstance(video_path, str) and os.path.isdir(video_path)):
            return _orig_loader(video_path, image_size, offload_video_to_cpu,
                                img_mean, img_std, async_loading_frames, compute_device)
        names = [p for p in os.listdir(video_path)
                 if os.path.splitext(p)[-1] in _ALLOWED_EXT]
        names.sort(key=lambda p: int(os.path.splitext(p)[0]))
        n = len(names)
        if n == 0:
            raise RuntimeError(f"no images found in {video_path}")
        paths = [os.path.join(video_path, p) for p in names]
        mean_t = torch.tensor(img_mean, dtype=torch.float32)[:, None, None]
        std_t = torch.tensor(img_std, dtype=torch.float32)[:, None, None]

        if async_loading_frames:
            lazy = _sam_misc.AsyncVideoFrameLoader(
                paths, image_size, offload_video_to_cpu, mean_t, std_t, compute_device,
            )
            return lazy, lazy.video_height, lazy.video_width

        images = torch.zeros(n, 3, image_size, image_size, dtype=torch.float32)
        video_height = video_width = 0
        for i, p in enumerate(tqdm(paths, desc="frame loading (PNG/JPEG)")):
            images[i], video_height, video_width = _sam_misc._load_img_as_tensor(p, image_size)
        if not offload_video_to_cpu:
            images = images.to(compute_device)
            mean_t = mean_t.to(compute_device)
            std_t = std_t.to(compute_device)
        images -= mean_t
        images /= std_t
        return images, video_height, video_width

    _sam_misc.load_video_frames_from_jpg_images = _patched
    _SAM2_PNG_LOADER_PATCHED = True


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
    # WHAT IT DOES: Path B fallback merge. final = max(CK, SAM_binary) via
    #               clip-add. Returns CK alone if SAM is None or shape-mismatched
    #               (safe fallback for callers without a usable SAM silhouette).
    # DEPENDS ON:   ck_alpha shape == sam_silhouette shape when both present.
    #               numpy. No CFM, no chroma reasoning.
    # AFFECTS:      Only the fallback branch of merge_ck_with_sam_active when
    #               USE_CHROMA_GATED_MERGE is False or source_rgb is missing.
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if ck.ndim == 3:
        ck = ck[..., 0]
    if sam_silhouette is None:
        return ck.copy()
    sam = np.asarray(sam_silhouette, dtype=np.float32)
    if sam.ndim == 3:
        sam = sam[..., 0]
    if ck.shape != sam.shape:
        return ck.copy()
    difference = np.clip(sam - ck, 0.0, 1.0)
    return np.clip(ck + difference, 0.0, 1.0).astype(np.float32)


def compute_chroma_weight(
    source_rgb: np.ndarray,
    screen_type: str = "green",
    threshold: float = CHROMA_GATE_THRESHOLD,
    soft_band: float = CHROMA_GATE_SOFT_BAND,
    dilate_px: int = CHROMA_GATE_DILATE_PX,
) -> np.ndarray:
    # WHAT IT DOES: Per-pixel float [0, 1] weight encoding "is this pixel on the
    #   green-screen side?" 1 = on-green (CK rules), 0 = off-green (SAM rules).
    #   chroma_score = G - max(R, B) for green; B - max(R, G) for blue. Binary
    #   threshold by default; optional smoothstep soft band; optional inward
    #   dilation of the on-green region for body parts that sit inside the green
    #   area but have no spill (butt-notch case from 2026-05-05 testing).
    # DEPENDS ON:   source_rgb is (H, W, 3) float32 in [0, 1]. cv2 imported lazily.
    # AFFECTS:      sole authority signal for merge_ck_with_sam_chroma_gated.
    if screen_type == "blue":
        chroma_score = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma_score = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma_score = np.clip(chroma_score, 0.0, 1.0)

    # DANGER ZONE FRAGILE: do NOT add Gaussian blur to weight or chroma_score.
    # SMART BLEND in sam2_combine.apply_sam2_gate_weighted stacked three sources
    # of softness and produced 50% ghost bands at body-green edges where CK soft
    # alpha 0.5 blended with SAM 1.0 gave 0.75 output (Berto 2026-05-01).
    if soft_band <= 0.0:
        weight = (chroma_score >= threshold).astype(np.float32)
    else:
        t = np.clip((chroma_score - threshold) / soft_band, 0.0, 1.0)
        weight = (t * t * (3.0 - 2.0 * t)).astype(np.float32)

    if dilate_px > 0:
        # Extend on-green region INTO body interior by dilate_px pixels. Covers
        # body parts (butt, fingertips) that sit inside the green area but have
        # no spill, so CK can rule there instead of SAM.
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
    soft_band: float = CHROMA_GATE_SOFT_BAND,
    dilate_px: int = CHROMA_GATE_DILATE_PX,
    proximity_px: Optional[int] = None,
) -> np.ndarray:
    # WHAT IT DOES: c96deb07 chroma-weight blend. final = weight * CK + (1 - weight) * SAM_binary.
    #   On-green pixels (weight=1): CK rules — soft hair alpha shines through.
    #   Off-green pixels (weight=0): SAM rules — hard binary kill of floor/wire/walls.
    #   No CFM solver, no internal Gaussian blur, no trimap, no unknown band.
    # DEPENDS ON:   ck_alpha and sam_silhouette are (H, W) float32 in [0, 1] with
    #               matching shapes. source_rgb is (H, W, 3) float32 in [0, 1].
    #               compute_chroma_weight + binarize_sam_silhouette.
    # AFFECTS:      every Combined-mode render once dispatched from merge_ck_with_sam_active.
    if source_rgb is None:
        raise ValueError("chroma-gated merge requires source_rgb")
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if ck.ndim == 3:
        ck = ck[..., 0]
    if sam_silhouette is None:
        return ck.copy()
    sam = (np.asarray(sam_silhouette, dtype=np.float32) > 0.5).astype(np.float32)
    if sam.ndim == 3:
        sam = sam[..., 0]
    if ck.shape != sam.shape:
        return ck.copy()

    # Morphological close to fill body-edge concavities and small notches in
    # the SAM contour (harness gaps, arm pits, finger sub-pixel under-cover).
    # Kernel 41px from the v2.2 architecture — fills concavities up to ~20px
    # wide without expanding the outer silhouette enough to leak past feet on
    # carpet. Binary morphology only — NO Gaussian per
    # [[feedback-ck-no-gaussian-on-sam-mask]].
    try:
        import cv2 as _cv2_close
        _k_close = _cv2_close.getStructuringElement(_cv2_close.MORPH_ELLIPSE, (41, 41))
        sam = _cv2_close.morphologyEx(
            sam.astype(np.uint8), _cv2_close.MORPH_CLOSE, _k_close
        ).astype(np.float32)
    except Exception:
        pass

    # Fill SAM2 segmentation interior holes after the close (any gaps that
    # survived the close pass).
    try:
        from scipy.ndimage import binary_fill_holes
        sam = binary_fill_holes((sam > 0.5).astype(np.uint8)).astype(np.float32)
    except Exception:
        pass

    # 2026-05-18 LARGEST-BLOB FILTER (Nuke / Mocha / Cara industry pattern).
    # SAM2 occasionally produces satellite blobs near the subject (floor
    # patches the model thinks are body, hand silhouettes that broke off
    # from the main body fragment). Keep only the LARGEST connected
    # component and drop the rest. Cleans up "wing fragments" that survive
    # the geometric wing filter downstream. Safe failure: if SAM is empty
    # or single-blob, this is a no-op.
    try:
        import cv2 as _cv2_cc_lb
        _sam_u8 = (sam > 0.5).astype(np.uint8)
        _n_lbl, _labels = _cv2_cc_lb.connectedComponents(_sam_u8, connectivity=8)
        if _n_lbl > 2:  # background label 0 + at least 2 foreground blobs
            # Pick the largest non-background component
            _sizes = np.bincount(_labels.ravel())
            _sizes[0] = 0  # ignore background
            _largest = int(np.argmax(_sizes))
            if _sizes[_largest] > 100:  # sanity guard against degenerate
                sam = (_labels == _largest).astype(np.float32)
    except Exception:
        pass

    import cv2 as _cv2

    # Compute chroma score first — used both for chroma-aware SAM widening and
    # the chroma-aware soft-zone gate.
    rgb_in = np.asarray(source_rgb)
    if rgb_in.dtype != np.float32:
        rgb = np.clip(rgb_in.astype(np.float32) / 255.0, 0.0, 1.0)
    else:
        rgb = np.clip(rgb_in, 0.0, 1.0)
    if rgb.ndim == 2:
        rgb = np.stack([rgb, rgb, rgb], axis=-1)
    if rgb.shape[:2] != ck.shape:
        rgb = _cv2.resize(rgb, (ck.shape[1], ck.shape[0]), interpolation=_cv2.INTER_AREA)
    if screen_type == "blue":
        chroma_score = rgb[..., 2] - np.maximum(rgb[..., 0], rgb[..., 1])
    else:
        chroma_score = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])

    # 2026-05-19 CHROMA-AMBIGUOUS SEAM SUPPRESSION inside body interior.
    # The carpet/wall chroma boundary produces a thin band of pixels with
    # "mix" chroma (0.05-0.30) — neither strong green nor body color. The
    # chroma engine outputs partial alpha at those pixels, producing the
    # 2-3 px horizontal seam line Berto's been seeing across the legs.
    # Fingerprint: inside SAM body + deep from silhouette boundary +
    # chroma in transition range → that's the seam, force CK to 1.
    #
    # Safety vs other features:
    # - Vertical wire: wire color (gray/black), chroma ≈ 0, outside the
    #   0.05-0.30 range → untouched.
    # - Vertical motion blur of body: body chroma (~ 0 or negative),
    #   outside ambiguous range → untouched.
    # - Hair tendrils: at silhouette boundary, excluded by sam_eroded.
    # - Strong-green pixels misclassified as body by SAM: chroma > 0.30,
    #   outside range → untouched (those die via the normal merge math).
    # - Body silhouette edge (skin + spill, chroma 0.05-0.20): excluded by
    #   sam_eroded so SAM body edge softness is preserved.
    try:
        _sam_seam_bin = (sam > 0.5).astype(np.uint8)
        # 2026-05-19 widened: 10px erode -> 5px (narrow legs lose all interior
        # at 10px), chroma 0.05-0.30 -> 0.0-0.45 (catch wider transition band).
        # ck<1.0 gate still filters out solid-body false positives.
        # 2026-05-19 morning: Y-restrict to bottom 60% of SAM bbox. Hair with
        # green spill was getting solidified into a blob (head silhouette had
        # ambiguous-chroma pixels inside sam_eroded). Seam line is at calf
        # level — bottom 60% always covers it; top 40% covers head/hair.
        _k_seam = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (11, 11))
        _deep_body_seam = _cv2.erode(_sam_seam_bin, _k_seam)
        _ys_seam = np.where(_sam_seam_bin > 0)[0]
        _y_zone = np.zeros_like(_sam_seam_bin)
        if _ys_seam.size > 0:
            _y_top_sam = int(_ys_seam.min())
            _y_bot_sam = int(_ys_seam.max())
            _y_split = _y_top_sam + int((_y_bot_sam - _y_top_sam) * 0.40)
            _y_zone[_y_split:, :] = 1
        if int(_deep_body_seam.sum()) > 100:
            _ambig_chroma = (chroma_score > 0.0) & (chroma_score < 0.45)
            _seam_zone = (
                (_deep_body_seam > 0)
                & _ambig_chroma
                & (ck < 1.0)
                & (_y_zone > 0)
            )
            _n_seam = int(_seam_zone.sum())
            if _n_seam > 0:
                ck = np.where(
                    _seam_zone, np.float32(1.0), ck.astype(np.float32),
                ).astype(np.float32)
            # Diagnostic dump so we can verify the mask is firing where we expect.
            try:
                from pathlib import Path as _P_sz
                from PIL import Image as _Img_sz
                import tempfile as _tf_sz
                _d_sz = _P_sz(_tf_sz.gettempdir()) / "ck_merge_diag"
                _d_sz.mkdir(parents=True, exist_ok=True)
                _Img_sz.fromarray((_seam_zone.astype(np.uint8) * 255), mode="L").save(_d_sz / "06_seam_zone.png")
                with open(_d_sz / "06_seam_zone_stats.txt", "w") as _f_sz:
                    _f_sz.write(f"seam_zone pixels: {_n_seam}\n")
                    _f_sz.write(f"deep_body pixels: {int(_deep_body_seam.sum())}\n")
                    _f_sz.write(f"chroma_score range: min={float(chroma_score.min()):.3f} max={float(chroma_score.max()):.3f}\n")
                    _f_sz.write(f"sam mask (after close/fill/CC): nonzero={int(_sam_seam_bin.sum())}\n")
            except Exception:
                pass
    except Exception:
        pass

    # 2026-05-17: TWO separate thresholds + speckle cleanup.
    #   is_on_green_widen (0.05 + MORPH_OPEN 5px) — generous, controls SAM
    #     widening. Catches mildly-spilled body edges (hand, harness, waist,
    #     hair). MORPH_OPEN kills isolated 2-3px chroma speckles on carpet/
    #     shoe edges that were causing speckled SAM widening artifacts.
    #   is_on_green_gate (0.25) — strict, controls the soft-zone CK shine-
    #     through. Only strong-green pixels qualify; kills weak-green
    #     boundary-line transitions.
    _is_widen_raw = (chroma_score > 0.05).astype(np.uint8)
    try:
        _k_clean = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (5, 5))
        _is_widen_cleaned = _cv2.morphologyEx(_is_widen_raw, _cv2.MORPH_OPEN, _k_clean)
        is_on_green_widen = _is_widen_cleaned.astype(bool)
    except Exception:
        is_on_green_widen = _is_widen_raw.astype(bool)
    is_on_green_gate = (chroma_score > 0.25)
    # Kept for diagnostic dump compat:
    is_on_green = is_on_green_gate

    # 2026-05-18: FEET-DOWN SAM extension REVERTED. Tried 11×21 top-half-active
    # kernel (10px down + 5px laterally). Berto: "SAM mask needs to be tighter
    # around the feet. it's not really capturing the feet a little bit off."
    # The 5px lateral spread made the SAM blob loose at feet. Leaving raw SAM
    # alone here — if feet still need extension, do it via a 3×7 narrow kernel
    # (1px lateral, 3px down) instead, not the wider 11×21.

    # 2026-05-17: CHROMA-AWARE SAM widening — SAM widens 10px ONLY where the
    # source pixel is on green (uses LOW threshold so mild-spill body edges
    # get covered). Off-green pixels keep tight SAM so feet on carpet don't
    # show a 10px halo.
    # Binary cv2.dilate — NO Gaussian per [[feedback-ck-no-gaussian-on-sam-mask]].
    try:
        # 2026-05-17: kernel 21 (10px radius) → 31 (15px radius) → 51 (25px
        # radius). Berto's test on deploy 084839 showed body outline still
        # visible at waist, fingers, harness strap, and floor boundary line
        # at 15px. 25px should push SAM far enough past body on green that
        # the CK takes over before any visible SAM line.
        _k_widen = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (51, 51))
        sam_widened = _cv2.dilate(sam.astype(np.uint8), _k_widen).astype(np.float32)
        # Use the GENEROUS (speckle-cleaned) widening threshold so body edges
        # with mild green spill get covered (hand, harness, hair, etc.).
        sam = np.where(is_on_green_widen, sam_widened, sam)
    except Exception:
        pass

    # CK-confidence-based routing (0ffc24b1 + chroma-aware soft gate 2026-05-17).
    #   soft_zone & on-green → final = ck * sam_buffered. CK's soft alpha
    #     (hair, edges, motion blur on green) preserved within
    #     SOFT_ZONE_SAM_BUFFER_PX of body. Off-green partial alpha falls
    #     through to the confident path and gets killed by SAM=0.
    #   else → final = ck * sam. SAM gates the decision: kills CK false
    #     positives on walls / floor where CK=1 but SAM=0; keeps body
    #     where CK and SAM agree.
    _k = max(1, int(SOFT_ZONE_SAM_BUFFER_PX))
    _kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (_k * 2 + 1, _k * 2 + 1))
    _kernel[:_k, :] = 0
    sam_buffered = _cv2.dilate(sam.astype(np.uint8), _kernel).astype(np.float32)

    # 2026-05-18 SAM PROXIMITY OVERRIDE — rescues body edges SAM2 cut tight.
    # SAM2 hiera_small ends precisely at the visible silhouette; fingers,
    # buttock curve, strap edges extend a few px past SAM. Chroma constraint
    # then drops them (skin-with-spill has chroma_score < 0.05 → off-green
    # → sam_buffered gated to 0). Result: hard clip at body edge.
    # FIX: proximity zone around SAM (UP/lateral only, no down — same
    # direction as sam_buffered, so platform stays dead). Inside proximity,
    # chroma gate is overridden; outside, current chroma gate still applies.
    # 2026-05-19: 30 → 10 → 7 → slider-driven via EDGE GUARD (proximity_px).
    # Range 0-30, default 7. User tunes per shot: low = tight feet, high =
    # generous body-edge rescue (butt, fingers).
    _prox_px = int(proximity_px if proximity_px is not None else 7)
    _prox_px = max(0, min(30, _prox_px))
    _prox_diam = max(3, _prox_px * 2 + 1)
    _k_prox = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (_prox_diam, _prox_diam))
    _k_prox[:_prox_px, :] = 0  # zero top half → dilation UP/lateral only
    _body_proximity = _cv2.dilate(
        sam.astype(np.uint8), _k_prox,
    ).astype(np.float32)
    # 2026-05-18 SIMPLE MULTIPLY + CHROMA-CONSTRAINED KEEP-ZONE.
    # keep_zone = max(sam, sam_buffered * is_on_green_widen).
    # final = ck * keep_zone.
    #
    # Body interior:        sam=1               → keep=1, CK preserved.
    # Hair / strap on green: sam=0, buf=1, on_green=1 → keep=1, CK preserved.
    # Floor near feet:       sam=0, buf=1, on_green=0 → keep=0, alpha=0.
    #
    # The is_on_green_widen mask is the ONLY thing left from the chroma-
    # gated stack. body_topology, body_core, near_weight distance transform,
    # ridge_kill: all gone. Berto's render showed lateral floor bands
    # surviving pure simple multiply (sam_buffered extends 40px sideways
    # at foot level, catching dark mat where CK keys partially).
    # is_on_green_widen kills those bands cleanly. Hair / strap on green
    # wall keep their full sam_buffered coverage because they ARE on green.
    # keep_zone gate: pixel preserved if EITHER on green wall (hair / strap
    # context) OR within 30px of SAM body (skin-with-spill body edges).
    _on_green_f = is_on_green_widen.astype(np.float32)
    _gate = np.maximum(_on_green_f, _body_proximity).astype(np.float32)
    _keep_zone = np.maximum(
        sam.astype(np.float32),
        sam_buffered.astype(np.float32) * _gate,
    ).astype(np.float32)
    final = np.clip(
        ck.astype(np.float32) * _keep_zone, 0.0, 1.0,
    ).astype(np.float32)

    # 2026-05-18 RIDGE KILL — orphaned partial alpha → 0.
    # The seam line at the green/carpet chroma boundary survives the
    # multiply because at that band:  sam=1 (inside body), CK=partial
    # (chroma engine can't decide), so final = CK × 1 = partial alpha
    # line visible across the legs.
    #
    # Detection: pixels with partial alpha (0.05 < final < 0.70) that are
    # FAR (>4px) from confident-FG (alpha >= 0.70) AND CLOSE (<3px) to
    # confident-BG (alpha <= 0.02). Hair survives because it's attached to
    # confident FG (d_to_FG ≤ 2px). Motion blur on a limb survives the
    # same way. Only ORPHANED ribbons — the chroma seam line — die.
    try:
        _fg_conf = (final >= 0.70).astype(np.uint8)
        _bg_conf = (final <= 0.02).astype(np.uint8)
        if int(_fg_conf.sum()) > 1000 and int(_bg_conf.sum()) > 1000:
            _d_fg = _cv2.distanceTransform(1 - _fg_conf, _cv2.DIST_L2, 5)
            _d_bg = _cv2.distanceTransform(1 - _bg_conf, _cv2.DIST_L2, 5)
            _ridge = (
                (_d_fg > 4.0)
                & (_d_bg < 3.0)
                & (final > 0.05)
                & (final < 0.70)
            )
            if int(_ridge.sum()) > 0:
                final = np.where(
                    _ridge, np.float32(0.0), final,
                ).astype(np.float32)
    except Exception:
        pass

    # 2026-05-18 GEOMETRIC WING FILTER — replaces 7-negative-dot UX.
    # Kill alpha pixels that satisfy ALL THREE:
    #   (1) OUTSIDE raw SAM silhouette (so body interior is safe)
    #   (2) inside FEET ZONE = bottom 30% of SAM bbox (so hair / hands at
    #       hip / chest level are safe — feet zone is the only place
    #       floor wings live)
    #   (3) > 12px laterally from raw SAM body (so a 1-2px feather at
    #       the foot edge survives, only the wing pixels die)
    # Body interior gated by (1). Hair gated by (2). All thresholds use
    # SAM-relative geometry, not absolute frame coords, so this generalises
    # across framings / shots. If SAM is empty or bbox degenerate, the
    # filter is a no-op.
    try:
        _sam_bin = (sam > 0.5).astype(np.uint8)
        if int(_sam_bin.sum()) > 100:  # need a real silhouette
            _ys, _xs = np.where(_sam_bin > 0)
            _y_min, _y_max = int(_ys.min()), int(_ys.max())
            _bbox_h = _y_max - _y_min
            if _bbox_h > 40:  # need real vertical extent
                # Feet zone = bottom 30% of bbox
                _feet_zone_top = _y_min + int(_bbox_h * 0.70)
                _feet_zone = np.zeros_like(_sam_bin)
                _feet_zone[_feet_zone_top:_y_max + 1, :] = 1
                # Distance from raw SAM body, in px (lateral + vertical mixed —
                # but feet zone restriction makes this effectively horizontal
                # since vertical extent of feet zone is small).
                _dist_from_sam = _cv2.distanceTransform(
                    1 - _sam_bin, _cv2.DIST_L2, 5,
                )
                # Wing kill mask: outside SAM AND in feet zone AND > 12px from SAM
                _WING_DIST_THRESH_PX = 12.0
                _wing_kill = (
                    (_sam_bin == 0)
                    & (_feet_zone > 0)
                    & (_dist_from_sam > _WING_DIST_THRESH_PX)
                    & (final > 0.05)
                )
                if int(_wing_kill.sum()) > 0:
                    final = np.where(
                        _wing_kill, np.float32(0.0), final,
                    ).astype(np.float32)
    except Exception:
        pass

    return final


def _v22_trimap_cfm_archived(ck_alpha, sam_silhouette, source_rgb=None):
    # ARCHIVED 2026-05-16: was merge_ck_with_sam_chroma_gated. Replaced by the
    # chroma-weight blend from c96deb07 (May 6 perfect-map state — pure binary
    # switch between CK on-green and SAM off-green; no CFM, no internal blur).
    # Kept on disk for hot-revert: rename this back to merge_ck_with_sam_chroma_gated
    # and remove the c96deb07 implementation below.
    # ORIGINAL HEADER (9093bb8):
    # 9093bb8 verbatim: v2.2 trimap + Closed-Form Matting with CK hair injection
    # in unknown band, hard clamp outside dilated SAM. No "improvements" added.
    # The artist remembers this exact recipe as the "perfect blend." Validate
    # first, tune later if needed.
    #
    # source_rgb is REQUIRED. ValueError if None.
    # Empty-mask guard: only safety net added beyond verbatim — returns CK if
    # SAM has < 100 active pixels (prevents silent black-frame failure on a
    # single bad SAM result mid-render).
    import cv2
    from scipy.ndimage import binary_fill_holes
    from pymatting import estimate_alpha_cf

    if source_rgb is None:
        raise ValueError("v2.2 chroma-gated merge requires source_rgb")

    ck_arr = np.asarray(ck_alpha, dtype=np.float32)
    if ck_arr.ndim == 3:
        ck_arr = ck_arr[..., 0]
    H, W = ck_arr.shape
    ck = np.clip(ck_arr, 0.0, 1.0)

    sam_arr = np.asarray(sam_silhouette)
    if sam_arr.ndim == 3:
        sam_arr = sam_arr[..., 0]
    sam = (sam_arr > 0.5).astype(np.uint8)

    if int(sam.sum()) < 100:
        return ck.copy()

    rgb_in = np.asarray(source_rgb)
    if rgb_in.dtype != np.float32:
        rgb = np.clip(rgb_in.astype(np.float32) / 255.0, 0.0, 1.0)
    else:
        rgb = np.clip(rgb_in, 0.0, 1.0)
    if rgb.ndim == 2:
        rgb = np.stack([rgb, rgb, rgb], axis=-1)
    if rgb.shape[:2] != (H, W):
        rgb = cv2.resize(rgb, (W, H), interpolation=cv2.INTER_AREA)

    # 1. Preprocess SAM: morphological close + hole fill -> solid body region.
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (40, 40))
    sam_closed = cv2.morphologyEx(sam, cv2.MORPH_CLOSE, k_close)
    sam_filled = binary_fill_holes(sam_closed).astype(np.uint8)

    # 2. Definite foreground (eroded SAM).
    k_erode = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))
    fg_def = cv2.erode(sam_filled, k_erode, iterations=1)

    # 3. Outer boundary (dilated SAM).
    k_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (81, 81))
    sam_dilated = cv2.dilate(sam_filled, k_dilate, iterations=1)

    # 4. Trimap. SAM only, CK not used.
    trimap = np.full((H, W), 0.5, dtype=np.float32)
    trimap[fg_def > 0] = 1.0
    trimap[sam_dilated == 0] = 0.0

    # 5. Downsample for CFM speed at 4K (CFM is O(N^1.5) on pixels).
    scale = 2 if max(H, W) > 2500 else 1
    if scale > 1:
        h, w = H // scale, W // scale
        trimap_small = cv2.resize(trimap, (w, h), interpolation=cv2.INTER_NEAREST)
        rgb_small = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
    else:
        trimap_small = trimap
        rgb_small = rgb

    # 6. Closed-Form Matting. pymatting Numba kernels expect float64.
    alpha_small = estimate_alpha_cf(
        rgb_small.astype(np.float64),
        trimap_small.astype(np.float64),
    )

    # 7. Upsample alpha back to source resolution.
    if scale > 1:
        alpha = cv2.resize(alpha_small.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR)
    else:
        alpha = alpha_small.astype(np.float32)
    alpha = np.clip(alpha, 0.0, 1.0)

    # 8. CK hair injection in unknown band only. Protects hair tendrils CFM smooths
    # over. Gated on CFM > 0.01 so CK = 1.0 floor pixels don't blow up alpha where
    # CFM correctly assigned near-zero alpha.
    unknown_band = (trimap == 0.5)
    inject_zone = unknown_band & (alpha > 0.01)
    alpha[inject_zone] = np.maximum(alpha[inject_zone], ck[inject_zone])

    # 9. Internal smoothing inside dilated SAM. Feathers fg_def -> unknown transitions
    # over ~8 px to kill the 1-2 pixel cliffs that show as visible matte edges.
    INTERNAL_BLUR_KERNEL = 15
    INTERNAL_BLUR_SIGMA = 2.5
    alpha_smooth = cv2.GaussianBlur(
        alpha, (INTERNAL_BLUR_KERNEL, INTERNAL_BLUR_KERNEL), INTERNAL_BLUR_SIGMA
    )
    inside_band = sam_dilated > 0
    alpha = np.where(inside_band, alpha_smooth, alpha)

    # 10. Hard clamp outside dilated SAM. Safety rail.
    alpha[sam_dilated == 0] = 0.0

    return alpha.astype(np.float32)


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
    proximity_px: Optional[int] = None,
) -> np.ndarray:
    # WHAT IT DOES: Dispatcher for the active merge mode. Routes to v2.2
    #   chroma-gated merge when USE_CHROMA_GATED_MERGE is True (default after
    #   2026-05-13 restoration). Falls back to Path B (max-style) when the
    #   flag is False, when sam_silhouette is None, or when source_rgb is
    #   missing. Single entry point so call sites are agnostic to which
    #   merge is in effect.
    # DEPENDS ON: USE_CHROMA_GATED_MERGE flag, merge_ck_with_sam_chroma_gated,
    #             merge_ck_with_sam (fallback).
    # AFFECTS: every exporter call site (process_current_frame Combined branch,
    #          on_process_range Combined branch, AE/Fusion plugin call sites).
    #          screen_type kwarg accepted for backward compatibility; v2.2
    #          chroma-gated ignores it (uses RGB matting, not chroma keying).
    if not USE_CHROMA_GATED_MERGE:
        return merge_ck_with_sam(ck_alpha, sam_silhouette)
    if sam_silhouette is None or source_rgb is None:
        return merge_ck_with_sam(ck_alpha, sam_silhouette)
    try:
        return merge_ck_with_sam_chroma_gated(
            ck_alpha, sam_silhouette, source_rgb,
            screen_type=screen_type,
            proximity_px=proximity_px,
        )
    except Exception:
        # 2026-05-16: dump the exception so we can fix it. The previous silent
        # fallback was hiding a real bug behind a "Combined export OK" log.
        try:
            import traceback as _tb_inner
            from pathlib import Path as _P_inner
            _P_inner(r"C:\Users\ragsn\ck_chroma_merge_exception.txt").write_text(
                _tb_inner.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        # Safety net: never let a chroma test or numpy hiccup crash the renderer.
        # Fall back to Path B which always returns a usable alpha.
        return merge_ck_with_sam(ck_alpha, sam_silhouette)


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    """v2.2 debug dump — no-op in v1.0."""
    return


def compute_garbage_matte(
    sam_silhouette: np.ndarray,
    expand_px: int = 0,
    feather_px: int = 0,
    y_top_pct: int = 0,
    y_bot_pct: int = 100,
    crop_mode: str = "body",
) -> Optional[np.ndarray]:
    """2026-05-19 garbage matte: cv2.dilate(SAM, expand_px) + GaussianBlur(feather_px)
    + optional vertical Y crop (y_top_pct, y_bot_pct).
    Returns float32 [0,1] (H, W) matte for sidecar export. Returns None if SAM is None.

    expand_px: 0-200, isotropic dilation radius from raw SAM silhouette.
    feather_px: 0-30, GaussianBlur sigma. 0 = hard binary edge.
    y_top_pct, y_bot_pct: 0-100, Y crop band.
    crop_mode: "body" (% of SAM bbox — follows subject across moving camera)
               or "frame" (% of frame height — locked to frame coords).
    """
    if sam_silhouette is None:
        return None
    import cv2 as _cv2_g
    sam_bin = (np.asarray(sam_silhouette) > 0.5).astype(np.uint8)
    if sam_bin.ndim == 3:
        sam_bin = sam_bin[..., 0]
    _ep = max(0, min(200, int(expand_px)))
    if _ep > 0:
        _k_g = _cv2_g.getStructuringElement(_cv2_g.MORPH_ELLIPSE, (_ep * 2 + 1, _ep * 2 + 1))
        sam_bin = _cv2_g.dilate(sam_bin, _k_g)
    matte = sam_bin.astype(np.float32)
    _fp = max(0, min(30, int(feather_px)))
    if _fp > 0:
        # GaussianBlur with ksize=(0,0) infers from sigma. Sigma = feather_px.
        matte = _cv2_g.GaussianBlur(matte, (0, 0), float(_fp))
        matte = np.clip(matte, 0.0, 1.0)
    # Y crop — holdout applies ONLY inside the user-selected vertical band.
    # Outside the band, matte = 1.0 (no holdout, CK passes through unchanged).
    # Inside the band, matte = dilated/feathered SAM (acts as holdout).
    # 2026-05-19: Y% is RELATIVE TO SAM BBOX (body-mode). SAM tracks the
    # subject across frames, so the crop band follows the body when the
    # camera moves or the subject re-frames. Berto: "but it does not
    # follow the foot, it stays cut off where it is on the frame."
    # Frame-mode (% of frame height) is the fallback when SAM has no bbox.
    _yt = max(0, min(100, int(y_top_pct)))
    _yb = max(0, min(100, int(y_bot_pct)))
    if _yt > 0 or _yb < 100:
        _h = matte.shape[0]
        # Body-mode: % of SAM bbox height. SAM moves with subject → crop follows.
        _ys_bb = np.where(sam_bin > 0)[0]
        if _ys_bb.size > 0:
            _y_min_b = int(_ys_bb.min())
            _y_max_b = int(_ys_bb.max())
            _bbox_h = _y_max_b - _y_min_b
            if _bbox_h > 0:
                _y0 = _y_min_b + int(_bbox_h * _yt / 100)
                _y1 = _y_min_b + int(_bbox_h * _yb / 100)
            else:
                _y0 = int(_h * _yt / 100)
                _y1 = int(_h * _yb / 100)
        else:
            # No SAM — fall back to frame-relative
            _y0 = int(_h * _yt / 100)
            _y1 = int(_h * _yb / 100)
        _y0 = max(0, min(_h, _y0))
        _y1 = max(0, min(_h, _y1))
        if _y1 <= _y0:
            return np.ones_like(matte)
        _band = np.zeros_like(matte)
        _band[_y0:_y1, :] = 1.0
        matte = matte * _band + (1.0 - _band)
    return matte


# Chroma-aware BG-cleanup pass, restored from the e4ed333 (2026-05-06) known-good
# matte behavior. The post-5/7 architecture rip removed the BG kill mask Berto
# was relying on — CK matte now leaves partial-alpha bleed across dim/poorly-lit
# green areas (forklift, floor mats, shadowed BG tarp), which composites as a
# lifted blue cast on the timeline.
#
# This re-applies the BG cleanup as a pure post-process on the CK matte. SAFE:
# only zeros pixels where ALPHA IS ALREADY WEAK (< alpha_threshold) AND the
# source plate is clearly on-screen-color (chroma >= chroma_threshold). The
# subject body (alpha ~1) is preserved even if green spill is present on hair
# or skin — body never gets cut.
def apply_chroma_kill_to_matte(
    matte: np.ndarray,
    source_rgb: np.ndarray,
    screen_type: str = "green",
    chroma_threshold: float = 0.05,
    alpha_threshold: float = 0.5,
) -> np.ndarray:
    """Zero matte pixels in BG green areas with weak alpha. Body preserved.

    matte: float [0,1], shape (H,W) or (H,W,1) — CK alpha output.
    source_rgb: float [0,1], shape (H,W,3) — source plate fed into the engine.
    screen_type: "green" or "blue" — picks which chroma channel dominates.
    chroma_threshold: minimum (G - max(R,B)) for a pixel to count as on-screen.
                     0.05 catches teal-leaning + dim screens; raise for purer green.
    alpha_threshold: only kill matte below this. Body (~1) protected; bleed (<0.5) killed.
    """
    src = np.asarray(source_rgb, dtype=np.float32)
    mt = np.asarray(matte, dtype=np.float32)
    if screen_type == "blue":
        chroma = src[..., 2] - np.maximum(src[..., 0], src[..., 1])
    else:
        chroma = src[..., 1] - np.maximum(src[..., 0], src[..., 2])
    mt_2d = mt[..., 0] if mt.ndim == 3 else mt
    if mt_2d.shape != chroma.shape:
        return mt
    is_screen = chroma >= float(chroma_threshold)
    weak_alpha = mt_2d < float(alpha_threshold)
    kill_zone = is_screen & weak_alpha
    result = np.where(kill_zone, 0.0, mt_2d).astype(np.float32)
    if mt.ndim == 3:
        return result[..., np.newaxis]
    return result
