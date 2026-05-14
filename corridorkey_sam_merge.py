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


def merge_ck_with_sam_chroma_gated(ck_alpha, sam_silhouette, source_rgb=None):
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
        return merge_ck_with_sam_chroma_gated(ck_alpha, sam_silhouette, source_rgb=source_rgb)
    except Exception:
        # Safety net: never let a CFM solver hiccup crash the renderer.
        # Fall back to Path B which always returns a usable alpha.
        return merge_ck_with_sam(ck_alpha, sam_silhouette)


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    """v2.2 debug dump — no-op in v1.0."""
    return


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
