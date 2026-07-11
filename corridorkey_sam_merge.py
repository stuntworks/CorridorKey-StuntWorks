# Last modified: 2026-07-11 | Change: silhouette-continuation spare test added to _ub_shape_kill_wire_components' Signal A (UNIFIED_BAND_SHAPE_SPARE_* constants, _ub_silhouette_contour_points/_ub_local_silhouette_tangent/_ub_axis_angle_deg/_ub_shape_spare_silhouette_continuation) — spares short straight body-edge false-positive components (3rd confirmed class: back grooves/hip blotch + wavy pant edges) that CV/elongation alone cannot separate from wire, by testing tangent alignment with the local silhouette contour. THICKNESS_CV_MAX stays 0.25 (not restored to 0.35). Full history: git log
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

# 2026-05-27: MERGE_MODE selects the active merge architecture.
#   "garbage_matte" — CK × zoned_dilated_SAM + chroma escape for hair (NEW)
#   "chroma_gated"  — v2.3 chroma-weight blend (350 lines, 19 thresholds)
#   "path_b"        — max(CK, SAM) fallback
# Toggle this one string to switch. Old functions stay intact for rollback.
MERGE_MODE = "garbage_matte"
_VALID_MERGE_MODES = {"garbage_matte", "chroma_gated", "path_b"}
if MERGE_MODE not in _VALID_MERGE_MODES:
    raise ValueError(f"MERGE_MODE {MERGE_MODE!r} not in {_VALID_MERGE_MODES}")
USE_CHROMA_GATED_MERGE = MERGE_MODE == "chroma_gated"
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
# RESTORED to 06-12 values (±2.0) — ±1.0 narrowed the feather too aggressively.
SAM_SOFT_LOGIT_LO = -2.0
SAM_SOFT_LOGIT_HI = 2.0
# Forward-compat aliases used by newer call sites.
SOFT_RAMP_LO = SAM_SOFT_LOGIT_LO
SOFT_RAMP_HI = SAM_SOFT_LOGIT_HI

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
    lo: float = SOFT_RAMP_LO,
    hi: float = SOFT_RAMP_HI,
) -> np.ndarray:
    """Convert SAM 2 mask-decoder logits to a soft 0..1 mask via SATURATION RAMP.

    Mapping:
        logit >= hi   -> 1.0  (solid interior, kills decoder texture)
        logit <= lo   -> 0.0  (solid background)
        lo < L < hi   -> linear ramp (soft edge band, ~2px at 1920, ~4px at 4K)

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
        lo, hi: ramp endpoints in logit space (defaults SOFT_RAMP_LO/HI = ±1.0).
            Widen toward ±2.0 for softer edge; tighten toward ±0.5 for harder.
            The 0.5 crossing is always at logit 0 — position never moves.

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


def adaptive_green_kill(a_nn, sam_fg, src_rgb):
    """Per-frame adaptive screen-color green removal. Kills leftover green BACKGROUND
    (incl. dark/poorly-lit green) while protecting the SAM subject. All thresholds learned
    per frame (no clip-specific constants). a_nn: float HxW alpha [0,1]; sam_fg: float/bool
    HxW subject silhouette (solid); src_rgb: HxW3 RGB [0,1] or uint8. Returns cleaned alpha."""
    import numpy as np, cv2
    if sam_fg is None or src_rgb is None: return a_nn
    h, w = a_nn.shape[:2]
    src = src_rgb.astype(np.float32)
    if src.max() > 1.5: src = src / 255.0
    R, G, B = src[:,:,0], src[:,:,1], src[:,:,2]
    luma = 0.299*R + 0.587*G + 0.114*B
    noise = float(np.percentile(luma, 1)) + 1e-3
    eps = max(noise, 1e-3)
    u = np.log((G+eps)/(R+eps)); v = np.log((G+eps)/(B+eps))
    F = (np.asarray(sam_fg) > 0.5).astype(np.uint8)
    if F.shape[:2] != (h, w): F = cv2.resize(F, (w, h), interpolation=cv2.INTER_NEAREST)
    ew = max(6, int(min(h, w)/120))
    k = np.ones((ew*2+1, ew*2+1), np.uint8)
    fg_protect = cv2.erode(F, k); sam_dil = cv2.dilate(F, k)
    sure_bg = (sam_dil == 0); unknown = (sam_dil > 0) & (fg_protect == 0)
    bgseed = sure_bg & (src.max(2) < 0.98) & (luma > noise*2)
    greenside = bgseed & (u > 0) & (v > 0)
    if greenside.sum() < 200: return a_nn
    uv = np.stack([u[greenside], v[greenside]], 1)
    mu = np.median(uv, 0); cov = np.cov(uv.T) + np.eye(2)*1e-3
    inv = np.linalg.inv(cov)
    duv = np.stack([u-mu[0], v-mu[1]], -1)
    d2 = np.einsum('ijc,cd,ijd->ij', duv, inv, duv)
    P = np.exp(-0.5*d2); P[(u <= 0) | (v <= 0)] = 0.0
    leak = a_nn[bgseed & (P > 0.5)]
    T_leak = float(np.percentile(leak, 95)) if leak.size > 50 else 0.5
    A = a_nn.copy()
    A[sure_bg] = a_nn[sure_bg] * (1 - P[sure_bg])
    clean = unknown & (a_nn <= T_leak) & (P > 0.5)
    A[clean] = a_nn[clean] * (1 - P[clean])
    return np.clip(A, 0, 1)


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

    # 2026-05-27 CORE MATTE DISTANCE MAP — saved here (after morph_close +
    # fill_holes + largest-blob) for the core override applied after final
    # alpha computation. The "Nuke Way": force solid alpha deep inside SAM
    # body, let CK detail rule only near the SAM edge.
    import cv2 as _cv2
    _sam_core_bin = (sam > 0.5).astype(np.uint8)
    _core_dist = _cv2.distanceTransform(_sam_core_bin, _cv2.DIST_L2, 5)

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
        _k_seam = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (5, 5))
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

    # 2026-05-27 CORE MATTE OVERRIDE — REMOVED.
    # Caused hair blob: seam suppression poisoned ck to 1.0 on green-spill
    # pixels, then the core override trusted the poisoned value. Also couldn't
    # reach the feet seam (feet are 1-3px from SAM edge, threshold was 3-5px).
    # The seam suppression at line ~583 (now with 5x5 kernel) handles the
    # feet seam directly. _core_dist is still computed above for potential
    # future use but the hard override is gone.

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


def _selective_carve_reassert(sam_cleaned, sam_raw, carve_points=None):
    """Re-punch ONLY deliberate operator carves into the cleaned SAM silhouette.

    Cleanup (close/fill/largest-blob) fills both accidental SAM2 interior holes
    (shirt-crease fragmenting - must STAY filled) and the operator exclusions
    (wire carve, shackle - must come BACK). Two keep-tests per punched component:
      shape  - long + thin = wire-like (diag > 150px, mean width < 50px)
      intent - within 150px of an operator exclude dot (catches blobby rigging
               hardware like a shackle that shape alone would refill; Berto 2026-06-06)
    Fence rule: real see-through detail lives in CK alpha - filling SAM
    accidental holes cannot hide it.
    carve_points: list of (x, y) full-image pixel coords (sam_negative dots) or None.
    """
    import cv2 as _cv2_sc
    import numpy as _np_sc
    punched = (sam_cleaned.astype(bool) & ~sam_raw.astype(bool)).astype(_np_sc.uint8)
    if not punched.any():
        return sam_cleaned
    try:
        n, lbl, stats, cent = _cv2_sc.connectedComponentsWithStats(punched, connectivity=8)
        out = sam_cleaned.copy()
        for ci in range(1, n):
            area = stats[ci, _cv2_sc.CC_STAT_AREA]
            w = stats[ci, _cv2_sc.CC_STAT_WIDTH]
            h = stats[ci, _cv2_sc.CC_STAT_HEIGHT]
            diag = (w * w + h * h) ** 0.5
            mean_width = area / max(diag, 1.0)
            keep_punched = (diag > 150.0 and mean_width < 50.0)
            if not keep_punched and carve_points:
                cx, cy = cent[ci]
                for pt in carve_points:
                    try:
                        dx = float(pt[0]) - cx
                        dy = float(pt[1]) - cy
                    except (TypeError, IndexError):
                        continue
                    if (dx * dx + dy * dy) ** 0.5 < 150.0:
                        keep_punched = True
                        break
            if keep_punched:
                out[lbl == ci] = 0
        return out
    except Exception:
        # Classifier failure -> return the CLEANED mask unchanged (holes stay filled,
        # carve re-assert skipped). The old fallback intersected with raw, silently
        # regressing the whole silhouette to pre-cleanup quality (review bug 1).
        return sam_cleaned


def solidify_sam_silhouette(sam_soft, carve_points=None):
    """The EXACT SAM silhouette the garbage merge uses: binarize -> morph close 3px ->
    fill interior holes (not boundary concavities) -> largest blob -> selective
    dot-aware carve re-assert. Shared by merge and panel SAM view so preview == render."""
    import cv2 as _cv2_ss
    import numpy as _np_ss
    sam = _np_ss.asarray(sam_soft, dtype=_np_ss.float32)
    if sam.ndim == 3:
        sam = sam[..., 0]          # drop channels BEFORE binarize (BGRA-safe, review bug 3)
    sam = (sam > 0.5).astype(_np_ss.uint8)
    sam_raw = sam.copy()
    try:
        k_close = _cv2_ss.getStructuringElement(_cv2_ss.MORPH_ELLIPSE, (3, 3))
        sam = _cv2_ss.morphologyEx(sam, _cv2_ss.MORPH_CLOSE, k_close)
    except Exception:
        pass
    try:
        import scipy.ndimage as _snd
        sam = _snd.binary_fill_holes(sam > 0).astype(_np_ss.uint8)
    except Exception:
        try:
            h_ss, w_ss = sam.shape[:2]
            _inv = (1 - sam).astype(_np_ss.uint8)
            _ff_mask = _np_ss.zeros((h_ss + 2, w_ss + 2), _np_ss.uint8)
            _flooded = _inv.copy()
            for _corner in [(0, 0), (w_ss - 1, 0), (0, h_ss - 1), (w_ss - 1, h_ss - 1)]:
                _cv2_ss.floodFill(_flooded, _ff_mask, _corner, 0)
            sam = _np_ss.maximum(sam, _flooded).astype(_np_ss.uint8)
        except Exception:
            pass
    try:
        n_lbl, labels = _cv2_ss.connectedComponents(sam, connectivity=8)
        if n_lbl > 2:
            sizes = _np_ss.bincount(labels.ravel())
            sizes[0] = 0
            largest = int(_np_ss.argmax(sizes))
            if sizes[largest] > 100:
                sam = (labels == largest).astype(_np_ss.uint8)
    except Exception:
        pass
    return _selective_carve_reassert(sam, sam_raw, carve_points)


# ============================================================================
# CK AUTHORITY (Berto 2026-07-06) — "SAM may not cut a pixel where CK had
# green-screen evidence." Fixes the harness-guy back-bite (mid-back, wire/
# harness attach point): both merge engines cut CK unconditionally wherever
# SAM's shape + on-plate green test say "background," even when CK's own raw
# alpha is confidently solid there. Root cause: dark harness/wire-attach
# fabric in shadow never tests green in HSV, so neither engine's chroma
# escape valve ever fires for it — no code anywhere checked "only let SAM
# cut where CK confidence is low" (see the 2026-07-06 diagnosis handoff).
#
# Scoped narrowly on purpose — this is NOT "CK always wins." SAM's whole job
# is killing non-green junk (rigging, walls, off-frame edges) where CK is
# ALSO solid for lack of green; a blanket CK-wins rule would swallow that
# too. The rule only fires where CK is solid AND the plate had green
# evidence nearby AND the pixel isn't in the feet zone (feet stay tight,
# standing Berto rule, non-negotiable).
#
# Why this can't repeat the fill_body_holes trap (07-05 handoff, rejected for
# refilling real arm gaps): PHYSICS rules it out. A real daylight gap shows
# the green screen straight through it, so CK is already near-transparent
# there — ck_solid (>= CK_AUTHORITY_SOLID_T, a HIGH floor) and a real gap
# can never coexist at the same pixel. This rule can only ever protect
# pixels CK already considers part of the subject.
#
# Gated end-to-end by settings.get("ck_authority") (falsy default, same
# opt-in pattern as "unified_band" — settings is a dict of per-session panel
# state, threaded down from ae_processor.sam_garbage_merge). OFF is the
# byte-identical pre-authority path: the callers below short-circuit on the
# settings check before touching a single new array, so the hot-path wall-
# time budget is unaffected when the toggle is off.
#
# v1 SCOPE (lead review, 2026-07-06 follow-up): ships unified_band-only.
# merge_ck_with_garbage_matte requires the SEPARATE, hidden
# settings.get("ck_authority_force_gm") key — plain "ck_authority" is a
# no-op in that engine. Reason: measured 521/625px (83%) real-wire
# resurrection on the ck_session_db1e94240509907cf0f87d1a ground-truth clip
# when this ran on that engine (see that function's own DANGER ZONE HIGH
# comment for the full account) — unified_band is safe because its
# post-band shape-kill/dot-kill passes (_ub_shape_kill_wire_components /
# _ub_dot_kill_band_components) run AFTER this rule's support boost and
# still catch real wire; garbage_matte has no equivalent discriminator, so
# nothing there would stop a resurrected wire pixel from shipping. The
# force_gm key exists only so this engine's path stays reachable for future
# testing/debugging — it is NOT wired to any panel control.
# ============================================================================
CK_AUTHORITY_SOLID_T = 0.90  # ck_solid floor — CK's OWN raw alpha (pre-SAM-
                              # gate, pre-garbage-matte) must be this confident
                              # before it can override SAM. High on purpose:
                              # this is an override of SAM's cut, not a normal
                              # keep threshold, and a real gap's CK alpha sits
                              # far below this (see the physics note above).
CK_AUTHORITY_NEAR_BODY_PX_BASE = 12.0  # px @1920. Protection only reaches
                                        # this far beyond the SAM silhouette —
                                        # keeps the rule scoped to the body
                                        # edge, not far-field junk that also
                                        # happens to read CK-solid (e.g. a
                                        # green-tinted wall column CK never
                                        # touched).
CK_AUTHORITY_GREEN_EVIDENCE_PX_BASE = 40.0  # px @1920. on_green_hsv dilation
                                              # radius — the "had green
                                              # evidence nearby" test. Wide on
                                              # purpose: a harness pocket can
                                              # sit well inside the green
                                              # backing's own footprint
                                              # without a single HSV-green
                                              # pixel directly under it
                                              # (local shadow eats the hue
                                              # signal; the screen behind it
                                              # did not stop being there).


def _ck_authority_protect_mask(ck, dist_from_sam, on_green_hsv, scale,
                                feet_start=None, edge_sigma=1.0):
    # WHAT IT DOES: Builds the shared CK-authority protection field — the soft
    #   [0,1] mask of pixels where SAM is NOT allowed to cut, because CK's own
    #   raw alpha is solid AND there was green-screen evidence nearby AND the
    #   pixel isn't in the feet zone. Both merge engines
    #   (merge_ck_with_garbage_matte, merge_ck_unified_band) call this ONE
    #   function and apply its output the same way: raise the SAM-side gate
    #   to 1.0 where protected, so the final composite reduces to CK's own
    #   alpha at those pixels.
    # DEPENDS ON: CK_AUTHORITY_SOLID_T, CK_AUTHORITY_NEAR_BODY_PX_BASE,
    #   CK_AUTHORITY_GREEN_EVIDENCE_PX_BASE. Caller supplies dist_from_sam
    #   (exterior distance-transform from the binary SAM silhouette — 0
    #   INSIDE the SAM silhouette by construction, since distanceTransform
    #   measures distance to the nearest zero pixel and the silhouette's
    #   interior IS the zero region of the (1 - sam) input both engines feed
    #   it) and on_green_hsv (the raw binary-ish on-screen-color test each
    #   engine already computes from source_rgb; None if no plate was
    #   available). No raw SAM mask param needed here — dist_from_sam already
    #   encodes everything this function needs from it.
    # AFFECTS: only settings.get("ck_authority") (unified_band) / settings.get(
    #   "ck_authority_force_gm") (garbage_matte, hidden/testing-only) sessions
    #   — callers gate the call itself on those keys, this function has no
    #   opinion on which one fired. When on_green_hsv is None this returns an
    #   all-zero mask: no plate, no green-evidence signal, no authority
    #   (never protects blind). When feet_start is given, rows >= feet_start
    #   are always zeroed on the FINAL soft mask (see the feet-zone note
    #   below — must happen AFTER the blur, not before).
    import cv2
    if on_green_hsv is None:
        return np.zeros(ck.shape, dtype=np.float32)
    ck_arr = np.asarray(ck)
    h, w = ck_arr.shape[:2]
    ck_solid = ck_arr >= CK_AUTHORITY_SOLID_T
    near_body = np.asarray(dist_from_sam) < (CK_AUTHORITY_NEAR_BODY_PX_BASE * scale)

    # PERF (lead follow-up, 2026-07-06): the exact full-res cv2.dilate here
    # measured ~950ms alone at 4K (171x171 ellipse kernel, 40px@1920 radius —
    # see D:\CLAUDE_JUNK\ck_authority\check6_walltime.py) and blew the 1.8s
    # merge budget on its own. green_evidence is a COARSE "was there green
    # nearby" neighborhood test, not a matte edge — a few px of drift at ITS
    # OWN boundary (not the final matte edge, which is still set by the
    # GaussianBlur feather below + the caller's own composite) is acceptable.
    # pyrDown twice (~4x linear reduction, ~16x fewer pixels) before dilating
    # at 1/4 the kernel radius on the shrunk image, then pyrUp twice and
    # re-threshold. Approximates the full-res isotropic growth within ~1-4px
    # at 4K (pyramid halving is the standard cheap approximation for a large
    # morphological radius — see check6_walltime.py's before/after numbers
    # for the measured wall-time win, and check2/bite_heal numbers for proof
    # this doesn't change which pixels get protected in practice).
    _green_soft = (np.asarray(on_green_hsv) >= 0.5).astype(np.float32)
    _small = cv2.pyrDown(cv2.pyrDown(_green_soft))
    _green_r_small = max(1, int(round(CK_AUTHORITY_GREEN_EVIDENCE_PX_BASE * scale / 4.0)))
    _se_green_small = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (_green_r_small * 2 + 1, _green_r_small * 2 + 1))
    # Low rebinarize threshold (0.25, not 0.5): pyrDown's gaussian pre-blur
    # softens a hard binary blob's edges before the second halving — a 0.5
    # cut there would erode small/thin green regions away before they ever
    # reach the dilate. 0.25 keeps them, matching the "acceptable drift, not
    # acceptable loss" instruction (a wider evidence radius is safe per the
    # physics note above; a NARROWER one risks missing real evidence).
    _small_bin = (_small > 0.25).astype(np.uint8)
    _dilated_small = cv2.dilate(_small_bin, _se_green_small)
    _up = cv2.pyrUp(cv2.pyrUp(_dilated_small.astype(np.float32)))
    if _up.shape[:2] != (h, w):
        _up = cv2.resize(_up, (w, h), interpolation=cv2.INTER_LINEAR)
    green_evidence = _up >= 0.5

    protect = ck_solid & near_body & green_evidence
    protect_soft = cv2.GaussianBlur(protect.astype(np.float32), (0, 0), max(1.0, float(edge_sigma)))
    protect_soft = np.clip(protect_soft, 0.0, 1.0).astype(np.float32)

    # FEET (lead follow-up, 2026-07-06): zero the feet zone AFTER the blur,
    # not before. Blurring a mask that was already hard-zeroed below
    # feet_start still lets the Gaussian kernel smear a few residual px of
    # soft protection PAST feet_start into the tight feet-hug zone (found on
    # ck_session_5a4037a4: +307px / +0.38% on the P2 feet gate with the old
    # before-blur ordering). Zeroing the already-blurred output is a hard cut
    # with no further smear — feet-zone contribution is now exactly 0.0, not
    # "mostly 0".
    # feet_start <= 0 is degenerate — a near-empty/failed SAM bbox (SAM found
    # no real subject), not a legit "feet start at the top row" case. Zeroing
    # from row 0 there would silently disable authority frame-wide and mask
    # the real SAM failure. Treat it as no-feet-info instead (same convention
    # _body_exits_bottom already uses for the "skip the feet-only rules" case).
    if feet_start is not None and feet_start > 0:
        protect_soft[feet_start:, :] = 0.0
    return protect_soft


def merge_ck_with_garbage_matte(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
    proximity_px: Optional[int] = None,
    carve_points=None,
    return_garbage: bool = False,
    settings: Optional[dict] = None,
):
    """CK × zoned_dilated_SAM + chroma escape valve for hair.

    SAM is used purely as a garbage matte — kill outside, preserve CK inside.
    No CK/SAM blending, no chroma-gated mixing, no seam suppression needed.
    Three zones: generous dilation at head/body (20px, block downward),
    tight lateral-only at feet (2px, no downward). Chroma escape valve
    lets CK hair detail survive beyond the dilation boundary on green pixels.

    return_garbage (Berto 2026-06-14): when True, also returns the green-aware
    keep-gate `garbage_matte` (white=keep body, black=kill junk) as the 2nd item
    of a tuple, so callers can surface it as a stable garbage-matte sidecar.
    Default False keeps the single-array return — every existing caller (the
    Resolve plugin's 6 call sites included) is untouched.

    settings (Berto 2026-07-06, scope-narrowed by lead review same day):
    optional per-session dict. Only settings.get("ck_authority_force_gm") is
    read here — NOT plain "ck_authority" (that key is a no-op in THIS
    engine; unified_band is the only ck_authority-enabled path in v1). See
    the DANGER ZONE HIGH comment at this function's application block, and
    the CK_AUTHORITY_* constants + _ck_authority_protect_mask above
    solidify_sam_silhouette, for the full why. None (every existing caller —
    Resolve x6, ComfyUI — never passes either key) keeps the exact
    pre-authority behavior.
    """
    import cv2

    settings = settings or {}

    # proximity_px accepted for API compat (Resolve caller signature unchanged).
    # No longer drives dilation width or escape radius — fixed+scaled constants below.

    ck = np.asarray(ck_alpha, dtype=np.float32)
    if ck.ndim == 3:
        ck = ck[..., 0]
    if sam_silhouette is None:
        return (ck.copy(), None) if return_garbage else ck.copy()
    sam = (np.asarray(sam_silhouette, dtype=np.float32) > 0.5).astype(np.uint8)
    if sam.ndim == 3:
        sam = sam[..., 0]
    if ck.shape != sam.shape:
        return (ck.copy(), None) if return_garbage else ck.copy()
    h, w = ck.shape

    # FIX A v3 (2026-06-06, review-hardened): cleanup + dot-aware carve re-assert via
    # the ONE shared solidify helper — same function the panel's SAM view calls, so
    # preview == render by construction (the previous inline copy was drift waiting
    # to happen; 2026-06-06 Sonnet review, bug 2).
    sam = solidify_sam_silhouette(sam, carve_points)

    # --- SAM bounding box ---
    ys = np.where(sam > 0)[0]
    if ys.size == 0:
        return ck.copy()
    bbox_y0, bbox_y1 = int(ys.min()), int(ys.max())
    bbox_h = max(bbox_y1 - bbox_y0, 1)
    feet_start = bbox_y0 + int(bbox_h * 0.70)
    transition_px = 30

    # Framing guard: if the body runs off the BOTTOM of the frame (waist/hip crop,
    # no feet/floor visible), the feet-zone hug + the 92% "shadow below feet" cut
    # below are WRONG — they were designed for full-body shots with feet on the
    # floor. On a waist crop they land on the lower torso and mangle the bottom
    # edge (ragged/broken bottom in BOTH viewer and render). Detect and skip them.
    _body_exits_bottom = bbox_y1 >= int(h * 0.97)

    # Resolution-aware kernel scaling — constants calibrated at 1920px wide.
    _scale = float(w) / 1920.0
    _pg = max(1, int(round(20.0 * _scale)))  # sam_wide radius: green-zone CK protection
    _esc = max(1.0, 12.0 * _scale)           # chroma escape radius: hair/blur near body

    # --- sam_wide: generous dilation (_pg px, block downward expansion) ---
    k_gen = _pg
    se_gen = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_gen * 2 + 1, k_gen * 2 + 1))
    se_gen[:k_gen, :] = 0
    sam_wide = cv2.dilate(sam, se_gen).astype(np.float32)

    # --- sam_tight: lateral-only dilation (no vertical), scaled ---
    # 5 -> 5.5 (Berto 2026-07-04): floor-50 classes shadowed butt-green as
    # off-green -> tight hug rolled the butt vs raw CK (CK MASTER A/B).
    # 5.5 -> 6.5 (Berto 2026-07-11): "SAM and CK mask is 1 or 2 pixels too
    # tight, except the feet, at least on this shot" — +1@1920 = ~+2px @4K
    # on the body hug. Feet zone unaffected (hugs raw/eroded SAM separately
    # below). Corpus law applies: tuned on the A001 wide shot, gates + eye
    # on other clips decide if it holds. Keep in sync with
    # UNIFIED_BAND_TIGHT_PX_BASE.
    _tight_r = max(1, int(round(6.5 * _scale)))
    se_tight = np.zeros((1, _tight_r * 2 + 1), dtype=np.uint8)
    se_tight[0, :] = 1
    sam_tight = cv2.dilate(sam, se_tight).astype(np.float32)

    # --- Green-aware blend weight (G_soft) built from source_rgb ------------
    # Reuses the same HSV thresholds as the chroma escape valve below — one
    # detection threshold, two uses. on_green_hsv is the raw binary map;
    # G_soft is the morphologically expanded + blurred version used as the
    # per-pixel blend weight: 1 = full sam_wide protection, 0 = tight hug.
    on_green_hsv = None
    G_soft = None
    if source_rgb is not None:
        try:
            rgb_in = np.asarray(source_rgb)
            if rgb_in.dtype != np.float32:
                _img_u8 = np.clip(rgb_in, 0, 255).astype(np.uint8)
            else:
                _img_u8 = (np.clip(rgb_in, 0.0, 1.0) * 255).astype(np.uint8)
            if _img_u8.ndim == 2:
                _img_u8 = np.stack([_img_u8, _img_u8, _img_u8], axis=-1)
            if _img_u8.shape[:2] != (h, w):
                _img_u8 = cv2.resize(_img_u8, (w, h), interpolation=cv2.INTER_AREA)
            img_bgr = cv2.cvtColor(_img_u8, cv2.COLOR_RGB2BGR)
            hsv_map = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            if screen_type == "blue":
                _lower, _upper = np.array([100, 50, 50]), np.array([130, 255, 255])
            else:
                # Value floor 20 RESTORED 2026-07-02 (was 50 since the 06-12 parity
                # test). Floor 50 misses SHADOWED green (subject's own shadow on the
                # screen) -> those zones read as off-green -> garbage matte hugs SAM
                # tight -> CK pixels beyond it amputated (the "lost butt"). Floor 20
                # classes dark green as green: wide protection + chroma escape apply.
                # Verified 2026-07-02 on 4K session: body-zone loss 32k->28k px,
                # zero far-junk regression. Hue stays 35-85 (green only).
                _lower, _upper = np.array([35, 50, 50]), np.array([85, 255, 255])  # A/B 2026-07-03: floor 50 under test vs 20 (hair-bite suspect)
            _green_bin = cv2.inRange(hsv_map, _lower, _upper)
            on_green_hsv = _green_bin.astype(np.float32) / 255.0
            _rc = max(3, int(round(9 * _scale)) | 1)
            _rd = max(3, int(round(15 * _scale)) | 1)
            _sg = max(1.0, 8.0 * _scale)
            _G_closed = cv2.morphologyEx(
                _green_bin, cv2.MORPH_CLOSE,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_rc, _rc)),
            )
            _G_dilated = cv2.dilate(
                _G_closed,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_rd, _rd)),
            )
            _bk = max(3, int(_sg * 6) | 1)
            G_soft = cv2.GaussianBlur(
                _G_dilated.astype(np.float32) / 255.0, (_bk, _bk), _sg,
            )
            G_soft = np.clip(G_soft, 0.0, 1.0)
        except Exception:
            on_green_hsv = None
            G_soft = None

    # sam_gate: full wide protection where green is behind, tight hug elsewhere.
    # Falls back to Y-position blend when source_rgb absent (original behavior).
    if G_soft is not None:
        garbage_matte = sam_tight * (1.0 - G_soft) + sam_wide * G_soft
    else:
        blend = np.ones((h, w), dtype=np.float32)
        ramp_end = min(feet_start + transition_px, h)
        for y in range(feet_start, ramp_end):
            blend[y, :] = 1.0 - (y - feet_start) / float(transition_px)
        blend[ramp_end:, :] = 0.0
        garbage_matte = sam_wide * blend + sam_tight * (1.0 - blend)
    garbage_matte = np.maximum(garbage_matte, sam.astype(np.float32))
    garbage_matte = np.clip(garbage_matte, 0.0, 1.0)

    # FEET RING KILL (Berto 2026-06-12): "there was always an extra 10px mask
    # around the feet — remove it." Inside the feet zone (bottom 30% of SAM bbox)
    # the garbage matte hugs RAW SAM: no lateral dilation, no wide protection.
    # The 5px@1920 lateral dilation scales to ~11px at 4K — that was the ring.
    _feet_zone = np.zeros((h, w), dtype=np.float32)
    if not _body_exits_bottom:
        # Only hug raw SAM at the feet when feet are actually in frame. On a waist
        # crop this zone lands on the lower torso and squares off the bottom edge.
        _feet_zone[feet_start:, :] = 1.0
        # Feet hug erosion (Berto 2026-07-04): raw-SAM hug still left a 1-2px fat
        # rim on shoes — but ONLY over NON-green ground (Berto's observation: over
        # green CK rules and is already tight; over floor/off-green the SAM binary
        # edge rules, and THAT edge is the fat). Erode the hug 1px@1920 (≈2px at
        # 4K) on off-green pixels only; green-side keeps raw SAM so CK's soft
        # motion blur is never clipped. Feet zone only — body/hair untouched.
        # TWO-TIER (Berto 2026-07-10): "remove 1 pixel from sam feet... but
        # only around and below the calf, don't affect the rest." The feet
        # zone (bottom 30% of bbox) keeps the original 1px@1920 hug; from the
        # calf line down (bottom 15% of bbox) one extra pixel comes off
        # (1.5 base -> 3px@4K vs 2px). Light checker made the residual shoe
        # rim readable. Keep in sync with the UNIFIED_BAND_CALF_* constants.
        _feet_erode_r = max(1, int(round(1.0 * _scale)))
        _se_feet = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (_feet_erode_r * 2 + 1, _feet_erode_r * 2 + 1))
        _sam_feet_eroded = cv2.erode(sam.astype(np.float32), _se_feet)
        _calf_start = bbox_y0 + int(bbox_h * 0.85)
        # 1.5 -> 2.0 (Berto 2026-07-11): "feet might need one less" — one more
        # pixel off the shoes (calf-and-below tier), ~4px @4K total. Same-shot
        # tuning caveat as sam_tight above. Sync: UNIFIED_BAND_CALF_ERODE_PX_BASE.
        _calf_erode_r = max(1, int(round(2.0 * _scale)))
        _se_calf = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (_calf_erode_r * 2 + 1, _calf_erode_r * 2 + 1))
        _sam_calf_eroded = cv2.erode(sam.astype(np.float32), _se_calf)
        _calf_rows = np.arange(h, dtype=np.float32)[:, None] >= _calf_start
        _sam_feet_eroded = np.where(_calf_rows, _sam_calf_eroded, _sam_feet_eroded)
        if on_green_hsv is not None:
            _sam_feet_hug = (sam.astype(np.float32) * on_green_hsv
                             + _sam_feet_eroded * (1.0 - on_green_hsv))
        else:
            _sam_feet_hug = _sam_feet_eroded
        garbage_matte = (garbage_matte * (1.0 - _feet_zone)
                         + np.minimum(garbage_matte, _sam_feet_hug) * _feet_zone)

    # Feather gate edges — converts hard sam_tight wall into gradient so CK
    # soft alpha is never clipped by a binary cliff where green detection missed.
    # RESTORED to 06-12: scale-dependent sigma (2.5*_scale). Fixed 2.5 was post-06-12.
    _gate_sigma = max(1.0, 2.5 * _scale)
    garbage_matte = cv2.GaussianBlur(garbage_matte, (0, 0), _gate_sigma)
    garbage_matte = np.clip(garbage_matte, 0.0, 1.0)

    # Proximity-limited chroma escape valve — reuses on_green_hsv from above.
    # CK hair beyond the dilation boundary passes on green pixels near the body.
    # Distant walls with green tint are still killed by the near_sam distance gate.
    # dist_from_sam initialized None here (CK AUTHORITY, Berto 2026-07-06): this
    # try-block is the first place it gets computed from the identical `sam`; the
    # authority block further down reuses it instead of recomputing, but this
    # block can fail/skip, so the authority block still falls back to its own
    # compute when it's still None.
    dist_from_sam = None
    if on_green_hsv is not None:
        try:
            sam_inv = (1 - sam).astype(np.uint8)
            dist_from_sam = cv2.distanceTransform(sam_inv, cv2.DIST_L2, 5)
            near_sam = (dist_from_sam < _esc).astype(np.float32)
            # Feet ring kill: escape valve is for hair — at the feet it re-added
            # green floor pixels, rebuilding the ring. No valve in the feet zone.
            near_sam *= (1.0 - _feet_zone)
            garbage_matte = np.maximum(garbage_matte, on_green_hsv * near_sam)
        except Exception:
            pass

    # CK AUTHORITY (Berto 2026-07-06) — HIDDEN/TESTING-ONLY key in this
    # engine: settings.get("ck_authority_force_gm"), NOT plain "ck_authority"
    # (lead review, same day, v1 SCOPE decision — see the module-level CK
    # AUTHORITY banner above solidify_sam_silhouette). Own try/except,
    # separate from the chroma-escape-valve's above: this rule must not
    # depend on (or be able to break) that valve. Guarded on the settings key
    # FIRST — sessions without ck_authority_force_gm never touch a new array
    # here, keeping the hot-path wall-time budget unaffected (and plain
    # "ck_authority" alone, without the _force_gm key, is ALSO a no-op here —
    # this is intentional, not a bug). Raises the SAM-side gate to 1.0 where
    # protected, so `final = ck * garbage_matte` below reduces to CK's own
    # alpha at those pixels — SAM's cut is overridden, not blended.
    # _protect_soft stays None when off (or on exception) — the shadow_kill
    # pass further down checks this and only applies its own guard when it
    # is a real array, so an off/failed session's shadow_kill is bit-identical
    # to before this feature existed.
    #
    # DANGER ZONE HIGH (measured during 2026-07-06 verification — THIS IS WHY
    # THE GATE ABOVE IS HIDDEN, not a leftover warning): on
    # ck_session_db1e94240509907cf0f87d1a (the wire-regression ground-truth
    # clip), turning this rule on in THIS engine resurrects 521 of 625
    # ground-truth real-wire pixels — 83% — measured via
    # D:\CLAUDE_JUNK\ck_authority\check3_wire_regression.py, wire_regression_
    # results.json. Cause: real support wire runs close beside the body AND
    # crosses in front of the green screen, so it can satisfy ck_solid +
    # near_body + green_evidence exactly like a real harness bite does — this
    # function has no shape/wire discriminator to tell them apart. The
    # PREREQUISITE that makes plain "ck_authority" safe on unified_band is
    # its post-band shape-kill/dot-kill pass
    # (_ub_shape_kill_wire_components / _ub_dot_kill_band_components, run
    # AFTER that engine's own support boost — see merge_ck_unified_band's own
    # ck_authority block below in this file): that pass independently proved
    # 0% new wire leak on the SAME ground-truth clip specifically because it
    # re-evaluates every band pixel for wire-shape AFTER the authority boost
    # and still kills real wire regardless. This engine has NO equivalent
    # pass, so nothing here would stop a resurrected wire pixel from
    # shipping — do not wire ck_authority_force_gm to any panel control, and
    # do not change the gate above to plain "ck_authority" until either (a)
    # this engine gains an equivalent shape/wire discriminator, or (b) Berto
    # explicitly accepts the wire trade-off for this engine.
    # Strict `is True` on purpose: settings is merged unfiltered JSON
    # (ae_processor.load_settings), so a hand-edited string like "false" is
    # still truthy in Python — only an actual JSON `true` may arm this.
    _protect_soft = None
    if settings.get("ck_authority_force_gm") is True and on_green_hsv is not None:
        try:
            print("CK_LOG: ck_authority ACTIVE via hidden ck_authority_force_gm "
                  "(garbage_matte engine, wire-leak DANGER ZONE)", flush=True)
            # Reuse dist_from_sam computed in the escape-valve block above (same
            # `sam`) when it's available; that block's own try may still have
            # failed even though on_green_hsv is not None here too, so fall
            # back to computing our own rather than assume it exists.
            if dist_from_sam is None:
                _sam_inv_auth = (1 - sam).astype(np.uint8)
                dist_from_sam = cv2.distanceTransform(_sam_inv_auth, cv2.DIST_L2, 5)
            _protect_soft = _ck_authority_protect_mask(
                ck, dist_from_sam, on_green_hsv, _scale,
                feet_start=(None if _body_exits_bottom else feet_start),
                edge_sigma=_gate_sigma,
            )
            garbage_matte = np.maximum(garbage_matte, _protect_soft)
        except Exception as _auth_exc:
            print(f"CK_LOG: ck_authority FAILED, continuing without protection: {_auth_exc}",
                  flush=True)
            _protect_soft = None

    final = np.clip(ck * garbage_matte, 0.0, 1.0).astype(np.float32)

    # Off-green body fill: SAM adds body parts outside the green screen zone.
    # Garbage matte returns 0 off-green (CK has no signal there). HSV detects
    # where the green screen ends; SAM fills the body beyond that boundary.
    if on_green_hsv is not None:
        try:
            _no_shadow = np.ones((h, w), dtype=np.float32)
            if not _body_exits_bottom:
                # Cut "shadow below feet" only when feet are in frame. On a waist
                # crop there is no floor/shadow — cutting at 92% just chops the
                # lower body into a hard horizontal line. Keep full height instead.
                _y_cut = int(ys.min()) + int((ys.max() - ys.min()) * 0.92)
                _no_shadow[_y_cut:, :] = 0.0
            off_green_body = sam.astype(np.float32) * (1.0 - on_green_hsv) * _no_shadow
            # Feather the off-green fill: raw SAM is binary/jagged and was added
            # AFTER the gate feather, so its edges printed ragged. Soften to match.
            off_green_body = cv2.GaussianBlur(off_green_body, (0, 0), _gate_sigma)
            final = np.clip(final + off_green_body * (1.0 - final), 0.0, 1.0).astype(np.float32)
        except Exception:
            pass

    # Edge choke moved to Fusion (CK_EDGE_CHOKE ErodeDilate node).
    # Adjustable per shot, non-destructive, no re-render needed.

    if source_rgb is not None and on_green_hsv is not None:
        try:
            _src_arr = np.asarray(source_rgb)
            if _src_arr.dtype in (np.float32, np.float64):
                _fix_u8 = (np.clip(_src_arr, 0.0, 1.0) * 255).astype(np.uint8)
            else:
                _fix_u8 = np.clip(_src_arr, 0, 255).astype(np.uint8)
            if _fix_u8.ndim == 2:
                _fix_u8 = np.stack([_fix_u8, _fix_u8, _fix_u8], axis=-1)
            if _fix_u8.shape[:2] != (h, w):
                _fix_u8 = cv2.resize(_fix_u8, (w, h), interpolation=cv2.INTER_AREA)
            _hsv_kill = cv2.cvtColor(cv2.cvtColor(_fix_u8, cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2HSV)
            shadow_kill = ((_hsv_kill[..., 2] < 52).astype(np.float32) * (1.0 - on_green_hsv) * (1.0 - sam.astype(np.float32)))
            # CK AUTHORITY guard (Berto 2026-07-06): shadow_kill is unconditional —
            # dark + off-green + outside-raw-SAM, no distance/confidence gate of its
            # own (unlike unified_band's equivalent pass, which is naturally gated by
            # (1-support) and so needs no separate guard). Without this, shadow_kill
            # would zero the EXACT harness-bite pixels ck_authority just protected
            # (dark, off-green, outside SAM's own bite in the silhouette there),
            # silently undoing the fix a few lines above. No-op when _protect_soft
            # is None (toggle off or the block above failed) — bit-identical then.
            if _protect_soft is not None:
                shadow_kill = shadow_kill * (1.0 - _protect_soft)
            final = np.clip(final * (1.0 - shadow_kill), 0.0, 1.0).astype(np.float32)
        except Exception:
            pass

    if return_garbage:
        # garbage_matte = green-aware keep-gate (white=keep body, black=kill junk),
        # stable + feathered. Callers invert it to white=junk for an AE luma matte.
        return final, np.clip(garbage_matte, 0.0, 1.0).astype(np.float32)
    return final


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
    proximity_px: Optional[int] = None,
    carve_points=None,
    return_garbage: bool = False,
    settings: Optional[dict] = None,
) -> np.ndarray:
    # WHAT IT DOES: Dispatcher for the active merge mode. Routes based on
    #   MERGE_MODE flag: "garbage_matte" (new), "chroma_gated" (v2.3),
    #   or "path_b" (max-style fallback). Single entry point so call sites
    #   are agnostic to which merge is in effect.
    # return_garbage (Berto 2026-06-14): opt-in. When True, returns
    #   (alpha, garbage_matte_or_None). Only the garbage_matte mode produces a
    #   real gate; every other mode returns None for it. Default False keeps the
    #   single-array return so all existing callers (Resolve x6, previews) are safe.
    # settings (Berto 2026-07-06): optional per-session dict, forwarded to the
    #   garbage_matte engine only (settings.get("ck_authority_force_gm") —
    #   see merge_ck_with_garbage_matte's docstring; plain "ck_authority" is
    #   a no-op there by design, v1 scope is unified_band-only). None (every
    #   caller that doesn't know about this key — Resolve x6, ComfyUI,
    #   chroma_gated/path_b fallbacks) keeps the exact pre-authority behavior.
    def _ret(x):
        return (x, None) if return_garbage else x
    if sam_silhouette is None:
        return _ret(merge_ck_with_sam(ck_alpha, sam_silhouette))
    if MERGE_MODE == "garbage_matte":
        try:
            return merge_ck_with_garbage_matte(
                ck_alpha, sam_silhouette, source_rgb,
                screen_type=screen_type,
                proximity_px=proximity_px,
                carve_points=carve_points,
                return_garbage=return_garbage,
                settings=settings,
            )
        except Exception:
            try:
                import traceback as _tb_gm, tempfile as _tmp_gm
                from pathlib import Path as _P_gm
                _P_gm(_tmp_gm.gettempdir(), "ck_garbage_merge_exception.txt").write_text(
                    _tb_gm.format_exc(), encoding="utf-8"
                )
            except Exception:
                pass
            return _ret(merge_ck_with_sam(ck_alpha, sam_silhouette))
    if MERGE_MODE == "chroma_gated" and source_rgb is not None:
        try:
            return _ret(merge_ck_with_sam_chroma_gated(
                ck_alpha, sam_silhouette, source_rgb,
                screen_type=screen_type,
                proximity_px=proximity_px,
            ))
        except Exception:
            try:
                import traceback as _tb_inner, tempfile as _tmp_inner
                from pathlib import Path as _P_inner
                _P_inner(_tmp_inner.gettempdir(), "ck_chroma_merge_exception.txt").write_text(
                    _tb_inner.format_exc(), encoding="utf-8"
                )
            except Exception:
                pass
            return _ret(merge_ck_with_sam(ck_alpha, sam_silhouette))
    return _ret(merge_ck_with_sam(ck_alpha, sam_silhouette))


def merge_ck_simple(ck_alpha: np.ndarray, sam_silhouette: Optional[np.ndarray]) -> np.ndarray:
    """Resolve-identical simple combine: binarize SAM at 0.5 → GaussianBlur(11×11, σ=2.5) → ck_alpha × gate → clip 0..1.

    Exact match to resolve_plugin/preview_viewer_v2.py _trimap_fuse + apply_sam2_gate no-halo path
    (EDGE_FEATHER_KSIZE=11, EDGE_FEATHER_SIGMA=2.5). No hole-punching, no chroma gates,
    no zone logic, no shirt_rescue. Used as the simple_combine=True A/B path in the AE engine.
    """
    import cv2 as _cv2
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
    gate_bin = (sam > 0.5).astype(np.float32)
    gate_soft = _cv2.GaussianBlur(gate_bin, (11, 11), 2.5)
    return np.clip(ck * gate_soft, 0.0, 1.0).astype(np.float32)


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


# ============================================================================
# UNIFIED BAND EDGE ENGINE — P1 build (2026-07-05), pure ADDITION as far as THIS
# builder's own diff goes — this build itself touches nothing above this line.
# (F10 correction, 2026-07-05: this banner previously claimed flatly that
# nothing above this line is touched — not literally true of the working
# tree: the sam_tight 5->5.5 change at line ~1141, dated 2026-07-04, is a
# real, separate, Berto-approved edit riding in the same uncommitted tree,
# one day ahead of this P1 build. It is not part of this builder's changes.)
# Per UNIFIED_EDGE_PLAN_2026-07-05.md (twice adversarially
# reviewed). Selected by a PER-SESSION SETTINGS KEY (settings["unified_band"]),
# checked in ae_processor.sam_garbage_merge BEFORE the MERGE_MODE dispatch above —
# NOT the MERGE_MODE module constant (shared by AE, DaVinci [6 call sites] and
# ComfyUI [engine_bridge imports]; flipping it ships to every host at once).
# Default OFF = the old garbage_matte path is byte-identical; this is a real
# one-click AE-only rollback.
#
# LAW for this builder (from the plan): merge_ck_with_garbage_matte (above,
# lines ~1063-1308) is the SPEC of behaviors to re-express or deliberately
# retire with proof — see the COVERAGE MATRIX comment block directly above
# merge_ck_unified_band below.
#
# Design (v2):
#   D(p) — exterior distance transform from the POST-solidify/POST-carve
#          silhouette (solidify_sam_silhouette, the SAME shared helper the old
#          merge and the panel's SAM view use — carve holes are real background
#          here, so D(p) collapses to 0 across an operator exclusion and that
#          zone can never be re-widened).
#   C(p) — continuous HSV chroma confidence, screen_type branched (green AND
#          blue). Smoothed with cv2.bilateralFilter (INSTALLED, verified).
#          cv2.ximgproc guided filter is CONFIRMED NOT INSTALLED in the CK venv —
#          do not import it, ever, in this function.
#   W(p) — width field: tight + (wide-tight)*smoothstep(C), no-down directional
#          bias (masked term — any exterior pixel directly below the body's own
#          lowest SAM pixel in its column gets W forced to ZERO, not tight,
#          regardless of confidence — see downward_zone below; F10 correction,
#          2026-07-05, this previously said "forced to tight"), continuous feet
#          taper (disabled by the framing guard, kept verbatim), low-passed.
#          All resolution-relative (_scale pattern).
#   support(p) = smoothstep((W-D)/feather_px) — ONE monotonic transition per
#          outward direction, by construction. This is what removes the
#          high-low-high double-boundary rim defect the old dual-mask
#          (sam_tight/sam_wide) blend produced.
#   keep/kill/band: inside eroded SAM -> keep (alpha = max(CK, off-green body
#          fill) — the fill is a LOAD-BEARING RESCUE, not a bypass); beyond W ->
#          kill (support -> 0 handles this); band between -> alpha = CK * support.
# ============================================================================

UNIFIED_BAND_TIGHT_PX_BASE = 6.5   # matches merge_ck_with_garbage_matte's sam_tight
                                    # radius (feet/lateral hug) at 1920px wide — kept
                                    # identical so on-green baseline pixel counts don't
                                    # drift for free. 5.5 -> 6.5 (Berto 2026-07-11):
                                    # body outline 1-2px too tight except feet, "at
                                    # least on this shot" — feet exempt via the feet
                                    # taper. Corpus law: revisit if other clips object.
                                    # TRIED 3.0 during P1 verification
                                    # to fight wire-bite resurrection (see the coverage
                                    # matrix / build report's honest-miss section) — it
                                    # measurably DEGRADED the named forensic crease fix
                                    # (introduced a partial dip, non-monotonic, peak
                                    # alpha dropped from 0.9998 to 0.85) for only a small
                                    # reduction in wire resurrection. Reverted: the
                                    # crease fix is the headline requirement and is not
                                    # traded away to partially help a metric that a
                                    # width/confidence-only field cannot fully solve
                                    # anyway (see honest miss below).
UNIFIED_BAND_WIDE_PX_BASE = 21.0   # sam_wide's generous head/body radius +1px
    # @1920 (~+2px @4K), Berto request 2026-07-06: give CK more room where the
    # backing is solid green ("CK will do the rest"). Feet are exempt by
    # construction — the feet-zone taper overrides W toward
    # UNIFIED_BAND_FEET_TIGHT_PX_BASE regardless of this value.
UNIFIED_BAND_FEATHER_PX_BASE = 6.0  # support transition half-width at 1920px wide.
                                     # Tuned against the P0 corpus gate (V2-V7,
                                     # UNIFIED_EDGE_PLAN_2026-07-05.md P1 section) —
                                     # see the P1 build report for the exact numbers
                                     # this value was chosen against.
UNIFIED_BAND_ERODE_PX_BASE = 2.0   # eroded-SAM "always fully keep" interior margin —
                                    # the safety rail that guarantees deep-body alpha
                                    # is never attenuated by the support field.
UNIFIED_BAND_W_LOWPASS_SIGMA_BASE = 1.0   # kills local W wobble from C gradients +
                                           # the distance-transform's own quantization.
                                           # 3.0 (tried first) bled ~2x the tight width
                                           # from a nearby genuinely-on-green patch all
                                           # the way out to real wire/strap pixels
                                           # 15-19px away (found via direct diagnostic
                                           # during P1 verification — W went from ~11.7
                                           # pre-blur to ~19-23 post-blur at the y700-1400
                                           # wire-bite zone, resurrecting 508/625 wire
                                           # pixels). 1.0 keeps the intended local-wobble
                                           # smoothing without bleeding across a
                                           # green-vs-not-green spatial discontinuity.
UNIFIED_BAND_BILATERAL_SIGMA_SPACE_BASE = 8.0
UNIFIED_BAND_BILATERAL_SIGMA_COLOR = 0.20  # chroma-score units [0,1] — NOT pixels,
                                            # not resolution-scaled.
UNIFIED_BAND_HUE_GREEN_CENTER = 60.0
UNIFIED_BAND_HUE_GREEN_HALF_WIDTH = 25.0    # matches on_green_hsv's hard hue cutoff [35,85]
UNIFIED_BAND_HUE_BLUE_CENTER = 115.0
UNIFIED_BAND_HUE_BLUE_HALF_WIDTH = 15.0     # matches the blue hard hue cutoff [100,130]
UNIFIED_BAND_SAT_FLOOR = 0.20                # soft ramp, centered near value_floor=50/255
UNIFIED_BAND_SAT_CEIL = 0.35
UNIFIED_BAND_VAL_FLOOR = 0.20
UNIFIED_BAND_VAL_CEIL = 0.35
UNIFIED_BAND_FEET_ZONE_START_PCT = 0.70   # matches merge_ck_with_garbage_matte's feet_start
UNIFIED_BAND_SHADOW_KILL_VAL = 52.0 / 255.0  # matches the old shadow_kill value threshold
UNIFIED_BAND_SHADOW_CUT_PCT = 0.92          # matches the old 92% "shadow below feet" cut
UNIFIED_BAND_FEET_ERODE_PX_BASE = 1.0  # matches merge_ck_with_garbage_matte's feet-hug
                                        # erosion radius — see the feet-erosion note
                                        # above D(p)'s computation in merge_ck_unified_band.
UNIFIED_BAND_CALF_START_PCT = 0.85     # calf line — bottom 15% of the SAM bbox.
UNIFIED_BAND_CALF_ERODE_PX_BASE = 2.0  # Berto 2026-07-10: MINUS one more pixel off
                                        # (1.5 -> 2.0, Berto 2026-07-11: "feet might
                                        # need one less" — shoes one more px tighter,
                                        # same-shot tuning caveat as TIGHT_PX_BASE)
                                        # the SAM silhouette, calf and below ONLY
                                        # ("don't affect the rest") — 3px@4K there
                                        # vs the feet zone's 2px. Off-green side
                                        # only, same as the feet hug. Keep in sync
                                        # with merge_ck_with_garbage_matte's
                                        # two-tier block.
UNIFIED_BAND_FEET_TIGHT_PX_BASE = 1.0  # feet-zone width TAPER TARGET — deliberately
                                        # NOT the general tight_px (5.5). The old
                                        # feet-ring-kill hugs a near-eroded silhouette
                                        # there (~1px margin), not a 5.5px lateral
                                        # dilation; taper-to-5.5 measurably leaked
                                        # diagonal floor pixels beside the feet during
                                        # P1 verification (isotropic D(p) treats a
                                        # diagonal 8px gap as "close", where the old
                                        # code's SINGLE-ROW tight kernel — zero
                                        # diagonal reach by construction — did not).
UNIFIED_BAND_FEATHER_TIGHT_FLOOR_PX_BASE = 1.5  # px @1920 (F8, 2026-07-05: hoisted
    # from a bare 1.5 literal). Feather half-width floor in the no-down zone and
    # the feet taper — restores near-binary precision there without touching the
    # body/hair transition (see feather_field's own comment below).
UNIFIED_BAND_FEET_TRANSITION_PX_BASE = 30.0  # px @1920 (F8, 2026-07-05: hoisted
    # from a bare 30.0 literal). SHORT feet-taper ramp width — matches the old
    # merge_ck_with_garbage_matte's own transition_px=30 constant (see the feet
    # taper's own comment below).
UNIFIED_BAND_OFF_GREEN_FEATHER_SIGMA_PX_BASE = 2.5  # px @1920 (F8, 2026-07-05:
    # hoisted from a bare 2.5 literal). GaussianBlur sigma feathering the
    # off-green body-fill rescue before it's maxed into keep_alpha.

# FEET-ZONE OFF-GREEN ALPHA-RING HARDENING (P4, 2026-07-11) — matte-forensics
# attribution (D:\CLAUDE_JUNK\ck_feet_ring\): a 2-3px dark ring around shoes in
# every off-green (floor) composite. ROOT CAUSE, confirmed on native 4K ROI
# crops of ck_batch_a2cf5b549230 frame 13: CK's raw alpha keeps the dark unlit
# floor as foreground (see CK_ALPHA sidecar — near-1.0 across the floor with no
# SAM gate applied), so at the shoe/floor boundary `final`'s own soft ramp
# (support's smoothstep transition, tuned for hair/body-edge quality) passes
# through 2-3px of SEMI-TRANSPARENT pixels that still carry the dark FLOOR's
# own color — any composite over any background reads a visible dark rim there,
# regardless of what replaces green. This is an edge-COLOR problem, not an
# edge-WIDTH problem: further SAM erosion (UNIFIED_BAND_FEET_ERODE_PX_BASE /
# CALF_ERODE_PX_BASE above) only moves WHERE the ramp sits, it can't stop the
# ramp's own semi-transparent pixels from carrying bad color — already proven
# by the 2026-07-10 calf-shave commits netting zero change at the shoe/foot
# silhouette itself (see ck_parity_feet/ in ck_p3_artifacts INDEX.md).
# FIX: collapse the ramp toward near-binary in the feet zone, OFF-GREEN SIDE
# ONLY (on_green_hsv < 0.5) — this is the SAME "feet stay tight, near-binary
# hug" philosophy every other feet-zone constant above already encodes (the
# old engine's feet-ring-kill was itself a near-binary hug, not a soft ramp).
# A narrow smoothstep straddling 0.5 pushes real-coverage pixels toward 1 and
# mostly-floor pixels toward 0, so at most a sub-pixel sliver of dark-floor-
# colored translucency survives. This intentionally amputates a few px of
# motion-blur softness at the shoe-over-floor boundary — an accepted trade,
# not a regression, because that softness is exactly what was reading as a
# visible dark ring. GREEN side is untouched (CK rules over green, hard law —
# see module docstring) so soft edges / motion blur over actual green screen
# keep their full soft ramp (verified: D:\CLAUDE_JUNK\ck_feet_ring\).
UNIFIED_BAND_FEET_RING_HARD_LO = 0.35   # smoothstep low edge — tuned against
    # ck_batch_a2cf5b549230 frame 13 (ring 2.3px mean -> <=1px target).
UNIFIED_BAND_FEET_RING_HARD_HI = 0.65   # smoothstep high edge — symmetric
    # around 0.5 so a genuinely ~50/50 mixed edge pixel still lands near 0.5,
    # not snapped fully to 0 or 1 (avoids a new hard-edge aliasing artifact).

# ----------------------------------------------------------------------------
# SHAPE-DISCRIMINATION PASS constants (P1b, 2026-07-05/06). Forensic finding
# (D:\CLAUDE_JUNK\ck_p1b_wire\explore_*.py, sessA
# ck_session_db1e94240509907cf0f87d1a): the wire's resurrected pixels and the
# named forensic crease (row y=1190) are NOT separable by distance from the
# body or by chroma/darkness — task brief confirmed this and forbade further
# width/confidence knob-tuning (already tried, traded the crease fix away for
# a partial wire win — see UNIFIED_BAND_TIGHT_PX_BASE's honest-miss comment).
# 84% of the 465 resurrected wire px sit within ~2px@1920 of the raw
# silhouette (same immediate zone real hair/crease occupies), so this pass
# cannot rely on a distance cutoff either. What DOES separate them in this
# frame: the crease's own local component (once the continuous body-hugging
# band is fragmented at a small attach margin so it can be scored piece by
# piece) measures thickness coefficient-of-variation ~0.47-0.54 (a diffuse
# photometric gradient, width varies) while genuine wire fragments elsewhere
# in the same shot measure CV comfortably below 0.35 (near-constant caliber —
# a manufactured object's diameter doesn't wander). Elongation + a minimum
# tangential length keep this from firing on small blobby noise.
UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE = 2.0  # px @1920. Fragments the
    # continuous body-hugging band into locally scoreable pieces before CC —
    # matches UNIFIED_BAND_ERODE_PX_BASE's order (the keep-zone margin), not
    # a re-tuned width knob: this margin never grants trust, it only decides
    # what counts as one "piece" for shape scoring.
UNIFIED_BAND_SHAPE_MIN_AREA_PX = 4          # ignore fragments too small for a
                                              # reliable minAreaRect/thickness read.
UNIFIED_BAND_SHAPE_MIN_TANGENT_LEN_PX_BASE = 8.0  # px @1920. Long-side floor —
    # single-pixel chroma noise must not trigger a kill.
UNIFIED_BAND_SHAPE_ASPECT_MIN = 4.0          # minAreaRect long/short >= this to
                                               # read as "linear," not blobby.
UNIFIED_BAND_SHAPE_THICKNESS_CV_MAX = 0.25   # near-constant-caliber threshold —
    # see the forensic finding above (crease-adjacent component measured
    # ~0.47-0.54, comfortably above this). 0.35 -> 0.25 on 2026-07-05: at 0.35
    # Signal A false-killed short straight segments of the performer's OWN
    # silhouette edge (back grooves + below-butt blotch, P3 render
    # ck_batch_db203eb3be9b; attribution in D:\CLAUDE_JUNK\ck_p3_artifacts\).
    # Known, accepted cost: wire components with cv_th in (0.25, 0.32] now
    # survive — measured +15px genuine-wire leak on the P1b wire session
    # (wire_regression_check.md), remedied by an operator negative dot.
    # Berto-approved trade 2026-07-05: "wire is easier to remove by hand than
    # getting the butt back." ASPECT_MIN / distance-from-silhouette / min-length
    # alternatives all tested and rejected (wire and body-edge overlap on every
    # one) — do not re-tune those knobs to chase this.
UNIFIED_BAND_SHAPE_RECOVER_MARGIN_PX_BASE = 3.0  # px @1920. After a fragment
    # is classified wire, its kill is grown back out by this margin (>
    # attach margin) so the immediate near-body stub that attach-stripping
    # removed for scoring purposes is killed too — the wire doesn't stop
    # existing just because it grazes close to the silhouette for a few px.
    # Never grows past the band_mask itself (see kill_mask &= band_mask).

# ----------------------------------------------------------------------------
# SILHOUETTE-CONTINUATION SPARE TEST (P4, 2026-07-11) — 3rd confirmed
# false-positive class from Signal A above: short STRAIGHT segments of the
# performer's OWN silhouette edge, not wire. Confirmed twice more since the
# CV_MAX 0.35->0.25 fix (which only partially covers this failure mode):
# back grooves + below-butt blotch (2026-07-05, ck_p3_artifacts) and wavy
# pant-leg edges (2026-07-10, D:\\CLAUDE_JUNK\\ck_wavy_pants\\ — right-knee
# component cv_th=0.177, left-calf component cv_th=0.0, the latter
# MATHEMATICALLY unexcludable by ANY CV_MAX ceiling since a body-edge segment
# can measure zero thickness variance same as a real wire can).
#
# CV/elongation alone cannot separate these two populations (this is not a
# new finding — UNIFIED_BAND_SHAPE_THICKNESS_CV_MAX's own comment already
# documents it). What DOES separate them, measured directly on the wavy-pant
# exemplar (D:\\CLAUDE_JUNK\\ck_shape_spare\\, step-by-step harness): a
# false-positive component is a piece of the performer's OWN silhouette, so
# its long axis runs TANGENT to (parallel with) the local silhouette contour
# at the point where it attaches; a real wire crosses the band at an angle or
# runs offset from the contour, so it does not. This is a SPARE test, not a
# new kill signal — it only ever turns an already-would-be-killed Signal A
# candidate back off; it cannot cause anything Signal A wouldn't otherwise
# have killed to be killed, and it never touches Signal B (ridge) at all.
#
# TWO conditions, BOTH required (AND, not OR) before a Signal-A "is_wire"
# candidate is spared:
#   (a) hugs the main silhouette (median exterior-distance D of the
#       component's own pixels <= SPARE_MAX_D_PX). NECESSARY but NOT
#       sufficient — wire_vs_fp_distance_check.py (2026-07-05) already
#       measured this does NOT separate wire from body-edge false positives
#       by itself (the one confirmed wire sample and all three confirmed
#       false-positive samples sit at nearly identical D, ~4-6px native).
#       This gate exists so the tangent test below is never asked to judge a
#       component that isn't even near the body in the first place.
#   (b) its minAreaRect long axis is tangent-aligned (undirected angle <=
#       SPARE_TANGENT_ANGLE_MAX_DEG) with the LOCAL silhouette contour
#       direction at its own closest-approach point on the eroded-SAM
#       boundary, via a small-neighborhood PCA (_ub_local_silhouette_tangent).
#       THIS is the actual discriminator.
# A component whose tangent reading is unavailable (degenerate/too-short
# local contour patch — see _ub_local_silhouette_tangent) is NEVER spared —
# absence of a reliable reading must fail closed (kill), not be treated as
# "aligned," or a noisy silhouette patch could spare real wire by accident.
#
# SAFETY: this test can only REMOVE pixels from kill_seed_a (spare), the same
# one-directional guarantee _ub_shape_kill_wire_components' own docstring
# already states for the whole pass relative to band_alpha — it cannot grant
# alpha the band didn't already have, and it never touches Signal B.
UNIFIED_BAND_SHAPE_SPARE_MAX_D_PX_BASE = 3.0  # px @1920. Condition (a) — see
    # above; chosen from the wavy-pant exemplar's own measured component D
    # (median ~2.6-2.8px@1920 for both spared components) with headroom, and
    # matches the wire-session distance-check's finding that both wire and
    # body-edge false positives sit in this same near-body band, so this gate
    # passes through everything Signal A would plausibly classify as
    # near-silhouette without narrowing the population before the tangent
    # test (the real, condition-(b)) gets to look at it.
UNIFIED_BAND_SHAPE_SPARE_TANGENT_ANGLE_MAX_DEG = 22.5  # Condition (b)
    # threshold — inside the 20-25 degree window the forensic task specified.
    # A body-edge false positive's own local contour is not perfectly
    # straight (real anatomy curves), so this must not be near-zero; a
    # manufactured wire crossing the band has no reason to land inside a
    # generous 22.5 degrees of the silhouette's own local tangent unless it
    # is genuinely running along the body edge.
    #
    # ACCEPTED AS-IS for v1 (P4 gate review, 2026-07-11, both reviewers):
    # the comparison is WHOLE-FRAGMENT minAreaRect axis (one direction for
    # the entire Signal-A component) vs POINT-LOCAL contour tangent (PCA at
    # just the component's own attach point) — different granularity. On a
    # curved wire (one that bends along its own length) the whole-fragment
    # axis is an average direction that may not match the silhouette's local
    # tangent at the exact attach point even for a genuinely non-tangent
    # wire, OR could coincidentally land within tolerance. This generous
    # 22.5-degree threshold is deliberately wide enough to absorb ordinary
    # granularity mismatch without over-firing on false positives (validated
    # empirically, see D:\CLAUDE_JUNK\ck_shape_spare\ V1-V3), so no per-point
    # (as opposed to per-fragment) axis refinement is being built for v1.
UNIFIED_BAND_SHAPE_SPARE_TANGENT_WINDOW_PX_BASE = 15.0  # px @1920. ARC
    # radius (contour-INDEX distance, not Euclidean image-space distance —
    # see _ub_local_silhouette_tangent's own docstring for the P4 gate-review
    # fix: Euclidean windowing mixes points from two different boundary
    # branches near a self-close silhouette, e.g. the inner leg gap) around
    # the nearest contour point over which the local tangent PCA is fit.
    # Sized to comfortably span a typical false-positive component's own
    # long_side (confirmed FP samples ranged ~15-22px@1920) so the PCA sees
    # enough of the REAL local silhouette shape to be meaningful, not just a
    # couple of quantized boundary pixels.

# ----------------------------------------------------------------------------
# SECOND SIGNAL: Hessian ridge/line strength on the SOURCE PLATE (P1b round 3,
# 2026-07-06). The elongation+CV signal above is scored on tiny alpha-mask CC
# fragments and, per direct diagnostic (D:\CLAUDE_JUNK\ck_p1b_wire\
# explore_closing.py, explore_ridge*.py), only recovers a small slice of the
# resurrection because 84% of resurrected px sit within ~2px@1920 of the raw
# silhouette where fragment-level shape stats are dominated by quantization
# noise, not real geometry. A genuine wire is a thin manufactured LINE in the
# plate photograph — two close parallel edges, i.e. high local curvature
# (large Hessian eigenvalue) — while a step edge (the real body silhouette,
# or a soft shadow/crease gradient) has ONE transition and much lower
# curvature away from the silhouette's own 1-2px. Measured directly
# (explore_ridge_full.py, explore_ridge_grid.py, sessA): sigma=3.0px
# (unscaled — this is a plate-texture-scale measurement, not a body-relative
# one, so it is NOT multiplied by the resolution _scale factor the way
# geometry constants are), tiny 1px@1920 near-silhouette exclusion (much
# smaller than UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE — this signal does
# not need to give up the near-body zone).
#
# THRESHOLD — chosen against G3 (rim<20), not just against wire recall.
# threshold=7.0 was the most permissive value with ZERO hits on the crease's
# 16px test window and ZERO hits on a real hair-whip band (grid search), and
# recovered 105/465 resurrected px on sessA -- but it also KILLS scattered
# fragments in the *middle* of an otherwise-continuous band-alpha ring,
# which punches new high-low-high transitions into the ring itself (rim_
# detector_scan's own defect signature) — rim_profile_count jumped from the
# pre-shape-pass baseline of 7 to 64 (sessA) / 53 (sessB), a NEW regression
# that did not exist before this pass and directly fails G3. Swept 7/9/11/14/
# 18/24/32 (D:\CLAUDE_JUNK\ck_p1b_wire\sweep_ridge_thresh.py): rim_count only
# drops back under the G3 gate (<20) at threshold>=11 (rim=8, both sessions).
# SHIPPING 11.0 — the highest-recall value that keeps G3 green. This caps
# recall at 404/465 resurrected remaining on sessA (test-suite honest number;
# see the P1b build report) — most of the reachable win from this signal
# specifically requires threshold<11, which is unshippable. Do not lower this
# without re-running the rim sweep.
UNIFIED_BAND_SHAPE_RIDGE_SIGMA_PX = 3.0       # plate-texture scale, NOT
                                                # resolution-scaled (see above).
UNIFIED_BAND_SHAPE_RIDGE_THRESH = 11.0         # Hessian |lambda1| cutoff.
UNIFIED_BAND_SHAPE_RIDGE_TINY_EXCLUDE_PX_BASE = 1.0  # px @1920 — a much
    # smaller near-silhouette exclusion than the CC pass's attach margin;
    # this signal is precise enough close to the body to not need the wider
    # margin (grid-searched safe at this value).
UNIFIED_BAND_SHAPE_RIDGE_MIN_AREA_PX = 3       # drop single/double-pixel
                                                 # ridge noise before CC.

# ----------------------------------------------------------------------------
# NEGATIVE-DOT BAND KILL constants (P1c, 2026-07-05). Operator escape hatch —
# the shape-discrimination pass above still leaves an honest miss (404/465
# resurrected wire px on the forensic session, sessA ck_session_db1e94240509
# 907cf0f87d1a — see UNIFIED_BAND_SHAPE_RIDGE_THRESH's comment) because the
# wire and the real body crease are not separable by shape/CV at automation's
# reach. Berto's law: operator dots are the universal escape hatch when
# automation hits a physics-proven limit — a negative dot placed directly on
# the surviving wire fragment kills it with zero ambiguity, no knob-tuning.
#
# CONFIRMED (D:\CLAUDE_JUNK\ck_p1c_dotkill\00_locate_wire_remnant.py, sessA):
# the UNSTRIPPED band (band_alpha>0.3 & ~eroded_sam) is not a set of separate
# blobs — it is ONE continuous ring hugging the whole silhouette (a single
# connected component, 15194px, spans the crease AND the wire remnant). A
# naive "kill the whole connected component under the dot" would erase the
# entire band edge (hair, crease, everything) on one click. This pass MUST
# fragment the band the same way the shape pass's Signal A does before
# connected-components — reusing UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE and
# UNIFIED_BAND_SHAPE_RECOVER_MARGIN_PX_BASE verbatim (not new tuned numbers):
# the attach margin fragments the ring into locally scoreable pieces, the dot
# picks ONE piece, and the recover margin grows that piece's kill back toward
# the body — clamped to band_mask (which already excludes eroded_sam), so a
# kill can never cross into the keep zone regardless of how far it grows.
UNIFIED_BAND_MASK_ALPHA_THRESH = 0.3  # shared band-alpha component threshold —
    # the SAME cut both the shape pass's band_mask AND the dot-kill pass's
    # band_mask use, so a dot-killed component boundary always matches what
    # the shape pass already treats as "band" (F8, 2026-07-05: this was two
    # independently hardcoded 0.3 literals — _ub_shape_kill_wire_components
    # had its own bare `> 0.3` — now one named constant for both).
UNIFIED_BAND_DOTKILL_ALPHA_THRESH = UNIFIED_BAND_MASK_ALPHA_THRESH  # back-compat alias
UNIFIED_BAND_DOTKILL_SNAP_RADIUS_PX_BASE = 12.0  # px @1920. If a dot lands on
    # a pixel the attach-margin strip excluded (near-silhouette rim) or just
    # off a component's edge (~2px user aim error is typical), snap to the
    # NEAREST stripped component within this radius rather than doing nothing
    # on a near-miss click. F2 (2026-07-05): inside the feet zone a snap is
    # REJECTED (treated as a miss) when the found distance exceeds half this
    # radius — the feet zone's own band fragments are thin/small, so a full-
    # radius snap there is far more likely to grab the wrong sliver than a
    # genuine near-miss on the intended one.

# ----------------------------------------------------------------------------
# BBOX-CROP PERFORMANCE PATH (P2 perf, 2026-07-05/06). merge_ck_unified_band's
# per-pixel passes (HSV+bilateral C(p), distanceTransform, W-field construction,
# solidify_sam_silhouette, shape/ridge kill, dot-kill) all run full-frame today
# even though the profiled bodies occupy ~11% of a 4K frame. Gated behind
# settings['unified_band_crop'] (checked by merge_ck_unified_band itself, same
# per-session-key pattern 'unified_band' uses) — default OFF until this file's
# own V1 bit-exactness proof (crop ON vs OFF, max_abs_diff over final AND
# band_gate) has been run and shows 0.0 on real sessions.
UNIFIED_BAND_CROP_DEFAULT = False  # flip True only after a clean V1 bit-exact run.
UNIFIED_BAND_CROP_MARGIN_SAFETY_FACTOR = 2.0  # generous headroom over the
    # analytically-derived minimum margin below (task brief: "generously e.g. 2x").


def _ub_crop_margin_px(scale):
    """Safe crop-margin arithmetic, in px, for the GIVEN resolution scale
    (scale = frame_width / 1920.0, same convention every other UNIFIED_BAND_*
    constant uses). Returns the number of px the crop rectangle must extend
    beyond the RAW (pre-solidify) SAM bounding box in every direction so that
    merge_ck_unified_band, run entirely on that crop, produces BIT-IDENTICAL
    output to running it on the full frame. Two components, summed then
    doubled (UNIFIED_BAND_CROP_MARGIN_SAFETY_FACTOR):

    R1 — max distance outside the (pre-solidify) silhouette where band_alpha /
    band_gate can be NONZERO at all. support = smoothstep(-feather, feather,
    W - D) is EXACTLY 0 once D >= W + feather (smoothstep's own floor, not an
    approximation). W is built as tight_px + (wide_px-tight_px)*smoothstep(C)
    then Gaussian-low-passed and only ever TIGHTENED by the feet taper / no-
    down zero-clamp — a Gaussian blur is a convex combination of neighboring
    samples, so low-passing can only pull the ceiling DOWN, never past its
    pre-blur max. So W <= UNIFIED_BAND_WIDE_PX_BASE * scale everywhere, and
    feather_field <= UNIFIED_BAND_FEATHER_PX_BASE * scale everywhere (same
    taper-only-tightens argument). R1 = (WIDE + FEATHER) * scale. Every
    downstream pass (shape-kill, ridge-kill, dot-kill) can only REMOVE alpha
    the band already has (each is clamped `& band_mask` / `& ~eroded_sam` in
    its own code) — none of them push nonzero output further out than R1.

    R2 — extra buffer so every operator with its OWN spatial kernel sees, at
    every pixel out to R1, the identical neighborhood it would see on the
    full frame (distanceTransform itself needs NO buffer at all: cropping
    only discards background=1 pixels the algorithm never treats as a
    candidate nearest-zero, and the crop is guaranteed to contain every real
    SAM foreground/zero pixel by construction of the raw bbox, so D(p) inside
    the crop is identical to D(p) on the full frame for every retained pixel):
      W low-pass Gaussian:            ~3 * UNIFIED_BAND_W_LOWPASS_SIGMA_BASE * scale
      C(p) bilateral filter:          ~   UNIFIED_BAND_BILATERAL_SIGMA_SPACE_BASE * scale
                                            (diameter ~= sigma_space -> radius ~= half that;
                                             using the full sigma_space keeps this a generous over-count)
      shape attach + recover margins: (UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE
                                        + UNIFIED_BAND_SHAPE_RECOVER_MARGIN_PX_BASE) * scale
      dot-kill snap radius:           UNIFIED_BAND_DOTKILL_SNAP_RADIUS_PX_BASE * scale
      ridge Hessian pre-blur:         3 * UNIFIED_BAND_SHAPE_RIDGE_SIGMA_PX px, FLAT —
                                            this sigma is explicitly NOT scale-multiplied
                                            (see its own constant comment: plate-texture
                                            scale, not a body-relative one).
    """
    r1 = (UNIFIED_BAND_WIDE_PX_BASE + UNIFIED_BAND_FEATHER_PX_BASE) * scale
    r2 = (
        3.0 * UNIFIED_BAND_W_LOWPASS_SIGMA_BASE * scale
        + UNIFIED_BAND_BILATERAL_SIGMA_SPACE_BASE * scale
        + (UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE + UNIFIED_BAND_SHAPE_RECOVER_MARGIN_PX_BASE) * scale
        + UNIFIED_BAND_DOTKILL_SNAP_RADIUS_PX_BASE * scale
        + 3.0 * UNIFIED_BAND_SHAPE_RIDGE_SIGMA_PX  # flat px, not scale-multiplied
    )
    return int(np.ceil(UNIFIED_BAND_CROP_MARGIN_SAFETY_FACTOR * (r1 + r2)))


def _nearest_component_label(labels, stats, ix, iy, radius, n_lbl):
    """Nearest connectedComponentsWithStats label to (ix, iy) within radius
    px, or (None, None) if nothing qualifies. Bbox pre-filter (cheap reject) before an
    exact per-pixel distance check only on surviving candidates — stays cheap
    even with dozens of band components (P1c dot-kill snap-fallback, 2026-07-05).

    Returns (label, distance_px) — F2 (2026-07-05) added the distance return
    so the feet-zone snap-clamp can reject a match that is technically within
    `radius` but further than the feet zone's tighter half-radius tolerance.
    """
    best_lbl, best_d2 = None, radius * radius
    r = int(np.ceil(radius))
    h, w = labels.shape
    for lbl in range(1, n_lbl):
        x0, y0, ww, hh, _area = stats[lbl]
        if ix < x0 - r or ix > x0 + ww + r or iy < y0 - r or iy > y0 + hh + r:
            continue
        y_lo, y_hi = max(0, y0 - r), min(h, y0 + hh + r)
        x_lo, x_hi = max(0, x0 - r), min(w, x0 + ww + r)
        sub = labels[y_lo:y_hi, x_lo:x_hi]
        comp_ys, comp_xs = np.where(sub == lbl)
        if comp_ys.size == 0:
            continue
        dy = (comp_ys + y_lo) - iy
        dx = (comp_xs + x_lo) - ix
        d2 = dx * dx + dy * dy
        m = int(np.argmin(d2))
        if float(d2[m]) < best_d2:
            best_d2 = float(d2[m])
            best_lbl = lbl
    if best_lbl is None:
        return None, None
    return best_lbl, float(np.sqrt(best_d2))


def _ub_recover_kernel(margin_px_base, scale, plus_one=True):
    """Shared MORPH_ELLIPSE structuring-element builder for the recover-margin
    regrowth step used by BOTH the shape-kill pass (Signal A/B) and the
    dot-kill pass (F9, 2026-07-05 fix batch — was three separately inlined
    copies of the same `getStructuringElement(ELLIPSE, (r*2+1, r*2+1))` math).

    plus_one=True (Signal A / dot-kill's own recover margin): r = round(margin_px_base
    * scale) + 1 — the "+1" the original inline code always added.
    plus_one=False (Signal B's tiny 1px anti-aliasing pad): r = int(margin_px_base)
    taken as an already-final pixel radius, no scale, no +1 — matches Signal B's
    original `recover_margin_b_px = 1` (unscaled, un-padded) exactly.
    """
    import cv2
    r = (int(round(margin_px_base * scale)) + 1) if plus_one else int(margin_px_base)
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r * 2 + 1, r * 2 + 1))


def _ub_fragment_band(band_mask, D, attach_margin_px):
    """Shared band-fragmentation step used by BOTH the shape-kill pass
    (Signal A) and the dot-kill pass (F9, 2026-07-05 fix batch — was two
    independently inlined copies of the same strip+connected-components
    sequence). Strips the near-silhouette attach margin off band_mask (so a
    continuous body-hugging ring becomes locally scoreable pieces) then runs
    cv2.connectedComponentsWithStats on what survives.

    attach_margin_px may be a scalar OR a per-pixel array (F2, 2026-07-05:
    the feet-zone-tightened margin field) — `D > attach_margin_px` broadcasts
    either way.

    Returns (stripped, n_lbl, labels, stats). When nothing survives stripping,
    returns (stripped, 0, None, None) without calling cv2 — callers already
    treat `not stripped.any()` / `n_lbl <= 1` as the same "nothing to do" case,
    so this short-circuit is behavior-identical to always calling cv2 and
    getting n_lbl==1 (background only) back."""
    import cv2
    stripped = band_mask & (D > attach_margin_px)
    if not stripped.any():
        return stripped, 0, None, None
    n_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(
        stripped.astype(np.uint8), connectivity=8)
    return stripped, n_lbl, labels, stats


def _ub_dbg_name(stem, frame_idx):
    """Suffix a debug-dump filename stem with the frame/seq identifier when
    known (F11, 2026-07-05). frame_idx=None (the default — cmd_single and
    cmd_postproc have no per-frame identifier) keeps the OLD unsuffixed name;
    a batch/scrub caller passing its seq_num/frame_idx gets a distinct file
    per frame instead of every frame overwriting the last one's dump."""
    return stem if frame_idx is None else f"{stem}_{int(frame_idx):05d}"


def _ub_dump_dotkill_debug(debug_dir, band_mask, kill_mask, log_lines, frame_idx=None):
    """Debug overlay for the negative-dot band-kill pass — DISTINCT color from
    the shape-kill overlay (_ub_dump_shape_debug uses red for automation
    kills; this uses magenta so an operator can tell 'automation killed this'
    from 'my dot killed this' at a glance). Diagnostics only — caller wraps
    this in try/except (never break a real render).

    frame_idx (F11, 2026-07-05): optional frame/seq identifier, suffixed onto
    every dumped filename via _ub_dbg_name so a batch run doesn't have every
    frame's dump overwrite the last one. None (default) keeps the old names."""
    import cv2
    import json as _json_dk
    h, w = band_mask.shape
    overlay = np.zeros((h, w, 3), dtype=np.uint8)
    overlay[band_mask] = (0, 160, 0)       # green = band, kept
    overlay[kill_mask] = (220, 0, 220)     # magenta = operator dot-kill
    cv2.imwrite(str(debug_dir / f"{_ub_dbg_name('unified_band_dotkill_overlay', frame_idx)}.png"), overlay)
    (debug_dir / f"{_ub_dbg_name('unified_band_dotkill_log', frame_idx)}.txt").write_text(
        "\n".join(log_lines) + ("\n" if log_lines else ""), encoding="utf-8")
    (debug_dir / f"{_ub_dbg_name('unified_band_dotkill_log', frame_idx)}.json").write_text(
        _json_dk.dumps(log_lines, indent=2), encoding="utf-8")


def _ub_dot_kill_band_components(band_alpha, eroded_sam, D, scale, carve_points,
                                  feet_start=None, feet_floor_px=None,
                                  debug_dir=None, frame_idx=None):
    """OPERATOR ESCAPE HATCH (P1c, 2026-07-05) — negative-dot band-alpha
    component kill. See the UNIFIED_BAND_DOTKILL_* constants above for the
    forensic finding this exists for and why the band must be fragmented by
    the shape pass's attach margin before connected-components.

    Runs AFTER the shape-discrimination pass, on whatever band_alpha
    automation left behind (the honest-miss wire remnant the shape pass's own
    comments document as unshippable to chase further with knobs alone).

    Only band-zone dots are handled here — a dot landing inside eroded_sam
    (the keep zone) is left alone; solidify_sam_silhouette's own
    carve-reassert already owns dots there (interior SAM holes from the raw
    SAM detection), a different mechanism for a different zone entirely.

    This pass can only REMOVE alpha the band already has; it never adds any,
    and every kill is clamped to band_mask (== outside eroded_sam by
    definition) so a dot can never erase anything inside the keep zone.

    feet_start / feet_floor_px (F2, 2026-07-05): same feet_start row + feet
    taper-floor width merge_ck_unified_band already computes. Inside the feet
    zone (row >= feet_start) the attach margin used to fragment the band is
    capped to min(attach_margin_px, max(1, feet_floor_px)) instead of the
    general attach margin — the feet-zone band is already narrower than the
    general attach margin there, so stripping at the general margin left
    nothing for a feet-zone dot to hit (band starved). A feet-zone snap is
    also rejected past half the standard snap radius (tighter geometry there
    makes a full-radius snap likelier to grab the wrong sliver).

    frame_idx (F11, 2026-07-05): optional frame/seq identifier threaded to the
    debug dump so a batch run's per-frame dumps don't overwrite each other.

    Returns (band_alpha_out, kill_mask, log_lines) — log_lines has exactly one
    outcome line per carve_points entry (killed / snapped+killed / inside
    keep zone / band already clear / out of bounds / miss — F7, 2026-07-05),
    for the caller to surface via logging/print.
    """
    import cv2
    h, w = band_alpha.shape
    empty_kill = np.zeros((h, w), dtype=bool)
    log_lines = []
    if not carve_points:
        return band_alpha, empty_kill, log_lines

    band_mask = (band_alpha > UNIFIED_BAND_MASK_ALPHA_THRESH) & (~eroded_sam)

    # Fragment the (often continuous, body-hugging) band into locally
    # scoreable pieces BEFORE connected-components — the SAME attach-margin
    # strip the shape pass's Signal A uses (shared via _ub_fragment_band,
    # F9). Without this, a dot on one wire fragment would identify the WHOLE
    # band ring as "the component" (see the constants-block comment above —
    # confirmed directly on sessA).
    attach_margin_px = UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE * scale
    if feet_start is not None and feet_floor_px is not None:
        _feet_attach_margin = min(attach_margin_px, max(1.0, float(feet_floor_px)))
        attach_margin_field = np.where(
            np.arange(h, dtype=np.float32)[:, None] >= feet_start,
            np.float32(_feet_attach_margin), np.float32(attach_margin_px),
        ).astype(np.float32)
    else:
        attach_margin_field = attach_margin_px
    _stripped, n_lbl, labels, stats = _ub_fragment_band(band_mask, D, attach_margin_field)

    snap_radius = UNIFIED_BAND_DOTKILL_SNAP_RADIUS_PX_BASE * scale
    # SAME recover margin the shape pass's Signal A regrows a classified kill
    # by (reused verbatim via the shared _ub_recover_kernel, F9 — not a new
    # tuned number) — clamped to band_mask below, which already excludes
    # eroded_sam, so growth can approach but never cross the keep-zone
    # boundary (the "sever at attach margin" rule).
    se_recover = _ub_recover_kernel(UNIFIED_BAND_SHAPE_RECOVER_MARGIN_PX_BASE, scale)

    kill_mask = np.zeros((h, w), dtype=bool)
    hit_labels = set()

    for pt in carve_points:
        try:
            px, py = float(pt[0]), float(pt[1])
        except (TypeError, IndexError, ValueError):
            log_lines.append(f"band-kill: dot (unparseable {pt!r}) skipped — malformed point")
            continue
        ix, iy = int(round(px)), int(round(py))
        if not (0 <= ix < w and 0 <= iy < h):
            log_lines.append(f"band-kill: dot ({ix},{iy}) out of bounds — ignored")
            continue
        if eroded_sam[iy, ix]:
            log_lines.append(
                f"band-kill: dot ({ix},{iy}) inside keep zone — ignored (SAM carve owns interior)")
            continue  # keep-zone dot — owned by the SAM carve, not this pass
        if not band_mask[iy, ix]:
            log_lines.append(f"band-kill: dot ({ix},{iy}) band already clear at dot — no-op")
            continue  # not live band alpha here — nothing for this pass to kill

        _in_feet = feet_start is not None and iy >= feet_start
        _snap_note = ""
        lbl = int(labels[iy, ix]) if labels is not None else 0
        if lbl == 0:
            if labels is None:
                log_lines.append(
                    f"band-kill: dot ({ix},{iy}) missed all components (attach margin "
                    f"stripped the entire band — none survived to score; feet-zone "
                    f"geometry may still starve this dot)")
                continue
            snap_lbl, snap_dist = _nearest_component_label(labels, stats, ix, iy, snap_radius, n_lbl)
            if snap_lbl is None:
                log_lines.append(
                    f"band-kill: dot ({ix},{iy}) missed all components "
                    f"(no match within {snap_radius:.1f}px)")
                continue
            if _in_feet and snap_dist > (snap_radius / 2.0):
                log_lines.append(
                    f"band-kill: dot ({ix},{iy}) snap rejected in feet zone "
                    f"({snap_dist:.1f}px > {snap_radius / 2.0:.1f}px half-radius limit) — ignored")
                continue
            lbl = snap_lbl
            _snap_note = f"snapped {snap_dist:.1f}px to component {lbl} — "
        if lbl in hit_labels:
            log_lines.append(
                f"band-kill: dot ({ix},{iy}) {_snap_note}component {lbl} "
                f"already killed by an earlier dot — no-op")
            continue
        hit_labels.add(lbl)

        comp = (labels == lbl)
        comp_size_stripped = int(stats[lbl, cv2.CC_STAT_AREA])
        grown = cv2.dilate(comp.astype(np.uint8), se_recover) > 0
        grown = grown & band_mask
        comp_size_grown = int(grown.sum())
        kill_mask |= grown
        log_lines.append(
            f"band-kill: dot ({ix},{iy}) {_snap_note}killed {comp_size_grown}px component "
            f"(label {lbl}, {comp_size_stripped}px before recover-margin regrowth)")

    if not kill_mask.any():
        if debug_dir is not None:
            try:
                _ub_dump_dotkill_debug(debug_dir, band_mask, empty_kill, log_lines, frame_idx=frame_idx)
            except Exception:
                pass
        return band_alpha, empty_kill, log_lines

    band_alpha_out = band_alpha.copy()
    band_alpha_out[kill_mask] = 0.0

    if debug_dir is not None:
        try:
            _ub_dump_dotkill_debug(debug_dir, band_mask, kill_mask, log_lines, frame_idx=frame_idx)
        except Exception:
            pass

    return band_alpha_out, kill_mask, log_lines


def _ub_smoothstep(edge0, edge1, x):
    """Classic Hermite smoothstep: monotonic non-decreasing in x by construction.
    Shared by every continuous field below (C, W-blend, support) so 'monotonic by
    construction' (design point 4) is one audited implementation, not four.
    edge0/edge1 may be scalars OR per-pixel arrays (the feet/no-down feather taper
    needs array edges) — np.maximum keeps both forms working."""
    denom = np.maximum(np.asarray(edge1, dtype=np.float32) - np.asarray(edge0, dtype=np.float32), 1e-6)
    t = np.clip((x - edge0) / denom, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def rim_detector_scan(alpha_map, sam_bin, window=40, hi=0.5, lo=0.1):
    """High-low-high outward-normal profile scan — row-wise (left/right body edges
    per scanline) AND column-wise (top/bottom, e.g. head/feet rims). Ships as a P1
    debug output (see merge_ck_unified_band's unified_band_debug dump) and is kept
    forever as regression tooling — a P2 gate metric and reusable by the corpus
    harness (design point 7). Returns (count, hits); hits is a list of
    (y, x, direction) tuples for overlay plotting.
    """
    h, w = sam_bin.shape
    hits = []

    def _is_hlh(profile):
        if profile.size < 3:
            return False
        seen_high1 = seen_low = seen_high2 = False
        for v in profile:
            if not seen_high1:
                if v > hi:
                    seen_high1 = True
            elif not seen_low:
                if v < lo:
                    seen_low = True
            elif not seen_high2:
                if v > hi:
                    seen_high2 = True
        return seen_high1 and seen_low and seen_high2

    for y in range(h):
        xs = np.where(sam_bin[y] > 0)[0]
        if xs.size == 0:
            continue
        xmin, xmax = int(xs.min()), int(xs.max())
        lo_x = max(0, xmin - window)
        if _is_hlh(alpha_map[y, lo_x:xmin][::-1]):
            hits.append((y, xmin, "row_left"))
        hi_x = min(w, xmax + window)
        if _is_hlh(alpha_map[y, xmax:hi_x]):
            hits.append((y, xmax, "row_right"))

    for x in range(w):
        ys = np.where(sam_bin[:, x] > 0)[0]
        if ys.size == 0:
            continue
        ymin, ymax = int(ys.min()), int(ys.max())
        lo_y = max(0, ymin - window)
        if _is_hlh(alpha_map[lo_y:ymin, x][::-1]):
            hits.append((ymin, x, "col_top"))
        hi_y = min(h, ymax + window)
        if _is_hlh(alpha_map[ymax:hi_y, x]):
            hits.append((ymax, x, "col_bottom"))

    return len(hits), hits


def _ub_ridge_kill_seed(band_mask, D, scale, source_rgb, h, w):
    """Second shape signal (P1b round 3): Hessian ridge/line strength on the
    SOURCE PLATE. See the UNIFIED_BAND_SHAPE_RIDGE_* constants for the
    forensic finding — a manufactured wire is a thin LINE (two close parallel
    edges = high local curvature); a step edge (real silhouette) or a soft
    shadow/crease gradient has one transition and much lower curvature away
    from the silhouette's own 1-2px. Returns a boolean seed mask (pre-CC,
    pre-recover-growth — caller unions this with the elongation/CV seed
    before the shared recover+clip step). Returns None if source_rgb is
    unavailable (no plate = no ridge signal; caller falls back to signal A
    alone, same degraded posture as the rest of merge_ck_unified_band without
    a plate)."""
    import cv2
    if source_rgb is None:
        return None
    try:
        rgb_in = np.asarray(source_rgb)
        if rgb_in.dtype != np.float32:
            img_u8 = np.clip(rgb_in, 0, 255).astype(np.uint8)
        else:
            img_u8 = (np.clip(rgb_in, 0.0, 1.0) * 255).astype(np.uint8)
        if img_u8.ndim == 2:
            img_u8 = np.stack([img_u8, img_u8, img_u8], axis=-1)
        if img_u8.shape[:2] != (h, w):
            img_u8 = cv2.resize(img_u8, (w, h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(img_u8, cv2.COLOR_RGB2GRAY).astype(np.float32)
        blurred = cv2.GaussianBlur(gray, (0, 0), UNIFIED_BAND_SHAPE_RIDGE_SIGMA_PX)
        Ixx = cv2.Sobel(blurred, cv2.CV_32F, 2, 0, ksize=3)
        Iyy = cv2.Sobel(blurred, cv2.CV_32F, 0, 2, ksize=3)
        Ixy = cv2.Sobel(blurred, cv2.CV_32F, 1, 1, ksize=3)
        tr = Ixx + Iyy
        det = Ixx * Iyy - Ixy * Ixy
        disc = np.sqrt(np.clip((tr * tr) / 4.0 - det, 0.0, None))
        ridge = np.abs(tr / 2.0 + disc)
        tiny_exclude = UNIFIED_BAND_SHAPE_RIDGE_TINY_EXCLUDE_PX_BASE * scale
        return band_mask & (D > tiny_exclude) & (ridge >= UNIFIED_BAND_SHAPE_RIDGE_THRESH)
    except Exception:
        return None


# Sentinel for the spare test's LAZY contour computation (P4 gate-review fix
# 3, 2026-07-11): cv2.findContours costs ~4.5ms/frame even when zero Signal-A
# candidates ever reach is_wire==True, and that cost was previously paid on
# EVERY frame the band fragments at all, across hundreds of frames per batch.
# A plain `None` cannot serve as the "not yet computed" flag because None is
# ALSO the function's genuine "no eroded_sam foreground" result — this
# distinct object identity lets the per-call loop tell "haven't tried yet"
# apart from "tried, and there's honestly no contour" (which must stay
# cached as such, not be recomputed on every subsequent is_wire candidate).
_UB_CONTOUR_NOT_COMPUTED = object()


def _ub_silhouette_contour_points(eroded_sam):
    """WHAT IT DOES: returns the largest exterior contour of eroded_sam as an
    (N,2) float32 array of (x,y) points — the reference boundary the
    silhouette-continuation spare test measures local tangent direction
    against. Returns None if eroded_sam has no foreground (nothing to trace).
    DEPENDS ON: cv2.findContours (RETR_EXTERNAL — only the outer boundary
    matters for a tangent reading; interior holes are irrelevant here).
    AFFECTS: _ub_shape_spare_silhouette_continuation's condition (b) — a bug
    here changes which Signal-A wire candidates get spared.
    """
    import cv2
    if not eroded_sam.any():
        return None
    contours, _ = cv2.findContours(
        eroded_sam.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    pts = largest.reshape(-1, 2).astype(np.float32)
    return pts if len(pts) >= 3 else None


def _ub_local_silhouette_tangent(contour_pts, point_xy, window_px):
    """WHAT IT DOES: local tangent (unit direction) of the silhouette contour
    nearest point_xy, via PCA over contour points within window_px ARC
    distance (contour-index proximity) of the single nearest contour point.
    Returns None when the neighborhood has too few points for a reliable
    reading (degenerate/short local patch) — the caller MUST treat None as
    "cannot confirm," never as "aligned" (P4 safety rule, see
    UNIFIED_BAND_SHAPE_SPARE_* constants).
    DEPENDS ON: contour_pts from _ub_silhouette_contour_points (must be
    CHAIN_APPROX_NONE, i.e. ~1px spacing between consecutive indices — this
    function's index-count-as-arc-length conversion assumes that spacing).
    AFFECTS: _ub_shape_spare_silhouette_continuation condition (b) only.

    P4 GATE-REVIEW FIX (2026-07-11, both reviewers converged on this as the
    blocker): the neighborhood is chosen by ARC distance — contour points
    within +/-K indices of nearest_idx, K derived from window_px at the
    contour's own ~1px-per-index spacing (CHAIN_APPROX_NONE), with modular
    wraparound so it also handles the start/end seam trivially. This was
    PREVIOUSLY Euclidean (image-space) distance, which is backwards: the
    LOCAL geometry at a boundary point is defined by the contiguous arc that
    passes through it, not by "whatever contour points happen to be nearby in
    image space." Near a self-close silhouette (the inner-leg gap, a
    self-occluding limb crossing close to another part of the body), TWO
    physically separate boundary branches can sit within a few px of each
    other in image space while being hundreds of contour-indices apart along
    the boundary's own path. A Euclidean window pulls points from BOTH
    branches into one PCA fit, which returns a confident but WRONG blended
    tangent — exactly the failure mode that could false-spare a real wire
    crossing through such a zone (the wire's own axis could accidentally land
    within SPARE_TANGENT_ANGLE_MAX_DEG of the blended-nonsense direction even
    though it matches NEITHER branch's true local tangent). Arc-indexing
    cannot mix branches: by construction, only points reachable by walking
    along the SAME boundary path from nearest_idx are ever included, so a
    self-close second branch — however close in image space — is invisible
    to this function unless it is ALSO within K indices along the path,
    i.e. actually the same local arc. See
    D:\\CLAUDE_JUNK\\ck_shape_spare\\test_arc_window_regression.py for the
    synthetic self-close-silhouette regression test that pins this down.
    """
    if contour_pts is None:
        return None
    n = len(contour_pts)
    # ACCEPTED AS-IS for v1 (P4 gate review, 2026-07-11): this is an O(N)
    # nearest-point scan over the WHOLE contour per candidate, not a spatial
    # index (k-d tree). Measured cost is only material on pathological
    # SAM-failure frames with very large/noisy contours (25-83ms), which is
    # inside the per-frame wall-time budget (see the P2 wall-time gate) and
    # rare in the corpus. Not worth a k-d tree's added complexity for v1.
    d2 = np.sum((contour_pts - point_xy) ** 2, axis=1)
    nearest_idx = int(np.argmin(d2))
    # K in INDEX units, not px — CHAIN_APPROX_NONE gives ~1px spacing between
    # consecutive contour points, so K index-steps in either direction spans
    # ~K px of arc length, matching window_px's own px meaning. Capped at
    # n // 2 so a tiny/degenerate contour can't wrap around and double-count
    # the same points from both directions.
    k = max(1, min(int(round(window_px)), n // 2))
    idx = np.arange(nearest_idx - k, nearest_idx + k + 1) % n
    pts = contour_pts[idx]
    if len(pts) < 3:
        return None
    mean = pts.mean(axis=0)
    centered = pts - mean
    # PCA via SVD: the first right-singular vector is the direction of
    # maximum spread, i.e. the local tangent — a locally straight-or-curved
    # contour segment's points spread mostly ALONG the boundary, not across
    # it, so this direction is the boundary's own local orientation.
    try:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    tangent = vt[0]
    norm = float(np.linalg.norm(tangent))
    if norm < 1e-9:
        return None
    return tangent / norm


def _ub_axis_angle_deg(vec_a, vec_b):
    """WHAT IT DOES: undirected angle in degrees (0-90) between two
    orientation vectors. ISOLATED — a minAreaRect long axis and a PCA
    tangent both carry a 180-degree sign ambiguity (a line segment has no
    intrinsic "forward"), so the raw angle is folded into [0, 90] before any
    caller compares it to a threshold; without this fold, two genuinely
    parallel-but-oppositely-signed vectors would score ~180 degrees apart
    instead of 0.
    """
    na, nb = float(np.linalg.norm(vec_a)), float(np.linalg.norm(vec_b))
    if na < 1e-9 or nb < 1e-9:
        return 90.0
    cos_a = float(np.dot(vec_a, vec_b)) / (na * nb)
    cos_a = min(1.0, max(-1.0, cos_a))
    deg = float(np.degrees(np.arccos(abs(cos_a))))
    return deg


def _ub_shape_spare_silhouette_continuation(dvals, attach_xy, axis_vec, contour_pts, scale):
    """WHAT IT DOES: the silhouette-continuation spare test (P4, 2026-07-11).
    Returns (spared: bool, median_d: float, tangent_deg: float or None) for
    ONE Signal-A "is_wire" candidate component. See the
    UNIFIED_BAND_SHAPE_SPARE_* constants above for the full forensic finding
    and the two-condition (AND) rule this implements.
    DEPENDS ON: dvals (the component's own D-array values), attach_xy (its
    closest-to-silhouette pixel, in full-frame coords), axis_vec (its
    minAreaRect long-axis direction), contour_pts
    (_ub_silhouette_contour_points' output), scale (resolution factor, same
    convention as every other UNIFIED_BAND_* constant).
    AFFECTS: only whether the CALLER's is_wire candidate is spared — never
    reaches outside this one component's own kill decision.
    """
    median_d = float(np.median(dvals))
    max_d = UNIFIED_BAND_SHAPE_SPARE_MAX_D_PX_BASE * scale
    if median_d > max_d:
        return False, median_d, None  # condition (a) fails — not even near the body
    window_px = UNIFIED_BAND_SHAPE_SPARE_TANGENT_WINDOW_PX_BASE * scale
    tangent = _ub_local_silhouette_tangent(
        contour_pts, np.asarray(attach_xy, dtype=np.float32), window_px)
    if tangent is None:
        return False, median_d, None  # fail closed — cannot confirm alignment
    tangent_deg = _ub_axis_angle_deg(axis_vec, tangent)
    spared = tangent_deg <= UNIFIED_BAND_SHAPE_SPARE_TANGENT_ANGLE_MAX_DEG
    return spared, median_d, tangent_deg


def _ub_shape_kill_wire_components(band_alpha, eroded_sam, D, scale, source_rgb=None,
                                    debug_dir=None, frame_idx=None):
    """SHAPE-DISCRIMINATION PASS (P1b, 2026-07-05/06) — kills wire-shaped
    regions inside merge_ck_unified_band's band zone (outside the eroded-SAM
    keep interior, wherever the width/support field already granted trust).
    See the UNIFIED_BAND_SHAPE_* / UNIFIED_BAND_SHAPE_RIDGE_* constants above
    for the forensic finding this is built from and why width/confidence
    knobs cannot do this job.

    This pass can only REMOVE alpha the band already has; it never adds any.
    Operates on band_alpha directly (pre keep-merge) so the kill is in effect
    before `final = np.where(eroded_sam, keep_alpha, band_alpha)` composites it.

    TWO independent signals feed one shared kill mask (OR'd, per the P1b
    build report's round-by-round findings — neither alone clears enough of
    the resurrection, and each is safe on its own so combining is safe):
      A. Elongation + thickness-CV on attach-margin-stripped CC fragments
         (round 1). Catches genuinely elongated, near-constant-caliber
         fragments once the continuous body-hugging band is fragmented at a
         small attach margin so it can be scored piece by piece. AS OF P4
         (2026-07-11), every "is_wire" candidate this signal finds is run
         through the silhouette-continuation SPARE test
         (_ub_shape_spare_silhouette_continuation) before it is actually
         added to the kill seed — see the UNIFIED_BAND_SHAPE_SPARE_*
         constants above for the 3rd-confirmed-false-positive-class finding
         this closes (short straight segments of the performer's OWN edge,
         not wire; CV/elongation alone cannot tell them apart).
      B. Hessian ridge/line strength on the source plate (round 3, see
         _ub_ridge_kill_seed) — reaches closer to the silhouette than (A)
         because it does not need the attach-margin strip, catching thin
         manufactured lines (A) is structurally blind to (tiny fragments,
         noisy CV at that scale). The spare test does NOT apply to Signal B
         — Signal B's own near-silhouette exclusion already keeps it out of
         the P4 false-positive zone (see UNIFIED_BAND_SHAPE_RIDGE_TINY_EXCLUDE_PX_BASE).
    Both seeds are grown back out by RECOVER_MARGIN (clamped to the band
    zone) so a kill isn't truncated at whatever stripping/exclusion each
    signal used for scoring — the wire doesn't stop being wire where it
    grazes the body.

    HONEST MISS (P1b build report): even combined, these two signals do not
    catch the full resurrection on the named forensic session — most of the
    remainder sits within ~1-2px of the raw silhouette where ridge response
    and fragment shape stats are not reliably distinguishable from the real
    crease/hair's own near-silhouette response. Verified safe (zero hits on
    the crease's 16px test window, zero hits on a real hair-whip band) at the
    thresholds shipped; NOT tunable tighter without risking G1/G8 — see
    D:\\CLAUDE_JUNK\\ck_p1b_wire\\explore_ridge_grid.py.

    Returns (band_alpha_out, kill_mask). debug_dir (Path or None): when given,
    dumps a keep/kill component overlay + per-component feature table (now
    including each candidate's spare-test numbers, P4) — failures here must
    never break a real render (caller wraps in try/except same as the
    existing unified_band_debug block).
    """
    import cv2
    h, w = band_alpha.shape
    band_mask = (band_alpha > UNIFIED_BAND_MASK_ALPHA_THRESH) & (~eroded_sam)
    empty_kill = np.zeros((h, w), dtype=bool)
    if not band_mask.any():
        return band_alpha, empty_kill

    kill_seed_a = np.zeros((h, w), dtype=bool)
    kill_seed_b = np.zeros((h, w), dtype=bool)
    # Exact pixels of every component the spare test protects (P4 gate-review
    # fix 2, 2026-07-11) — see the RECOVER_MARGIN subtraction below for why
    # this has to be tracked separately from kill_seed_a rather than just
    # trusting "is_wire stayed False so it was never added to kill_seed_a."
    spare_mask = np.zeros((h, w), dtype=bool)
    _debug_rows = [] if debug_dir is not None else None

    # --- Signal A: elongation + thickness-CV on attach-stripped CC fragments.
    # Fragmentation shared with the dot-kill pass via _ub_fragment_band (F9).
    attach_margin = UNIFIED_BAND_SHAPE_ATTACH_MARGIN_PX_BASE * scale
    stripped, n_lbl, labels, stats = _ub_fragment_band(band_mask, D, attach_margin)
    if stripped.any():
        min_len_px = UNIFIED_BAND_SHAPE_MIN_TANGENT_LEN_PX_BASE * scale
        # Silhouette contour for the spare test's condition (b) — LAZY (P4
        # gate-review fix 3, 2026-07-11): cv2.findContours costs ~4.5ms/frame,
        # and the vast majority of frames/candidates never reach is_wire==True
        # at all, so paying this cost unconditionally taxed every fragmented-
        # band frame across a whole batch for nothing. Computed on the FIRST
        # is_wire hit below instead, then cached in this local for the rest of
        # the loop. _UB_CONTOUR_NOT_COMPUTED (not None) is the "haven't tried
        # yet" sentinel — None is _ub_silhouette_contour_points' own genuine
        # "no eroded_sam foreground" answer and must stay cached as such
        # without re-invoking findContours on every subsequent candidate.
        _spare_contour_pts = _UB_CONTOUR_NOT_COMPUTED

        for lbl in range(1, n_lbl):
            x0, y0, ww, hh, area = stats[lbl]
            if area < UNIFIED_BAND_SHAPE_MIN_AREA_PX:
                continue
            # Crop to the component's own bbox — keeps minAreaRect/
            # distanceTransform cheap regardless of frame size (G6 budget).
            sub = (labels[y0:y0 + hh, x0:x0 + ww] == lbl)
            ys_sub, xs_sub = np.where(sub)
            pts = np.column_stack([xs_sub, ys_sub]).astype(np.float32)
            rect = cv2.minAreaRect(pts)
            (rw, rh) = rect[1]
            long_side, short_side = max(rw, rh), max(min(rw, rh), 1e-6)
            aspect = long_side / short_side
            # 1px zero-border pad before the distance transform — a component
            # whose bbox it fills COMPLETELY (a straight 1px-wide line, e.g. a
            # single column) has no background pixel inside the crop for
            # distanceTransform to measure against, which returns the float32
            # sentinel max (~3.4e38) instead of a real distance. Found via
            # direct diagnostic during P1b build (component bbox 1x12,
            # dt.max()==3.4028235e+38, silent overflow on the *2.0 thickness
            # multiply). The 1px pad guarantees a real zero boundary exists.
            sub_padded = np.pad(sub, 1, mode="constant", constant_values=False)
            dt_padded = cv2.distanceTransform(sub_padded.astype(np.uint8), cv2.DIST_L2, 5)
            dt = dt_padded[1:-1, 1:-1]
            thickness = dt[sub] * 2.0
            mean_th = float(thickness.mean())
            cv_th = float(thickness.std() / max(mean_th, 1e-6))
            is_wire = (
                long_side >= min_len_px
                and aspect >= UNIFIED_BAND_SHAPE_ASPECT_MIN
                and cv_th <= UNIFIED_BAND_SHAPE_THICKNESS_CV_MAX
            )
            # --- SILHOUETTE-CONTINUATION SPARE TEST (P4, 2026-07-11) — only
            # evaluated for candidates Signal A would otherwise kill; can only
            # turn is_wire back to False, never the reverse. See the
            # UNIFIED_BAND_SHAPE_SPARE_* constants for the full finding.
            spared, spare_median_d, spare_tangent_deg = False, None, None
            if is_wire:
                if _spare_contour_pts is _UB_CONTOUR_NOT_COMPUTED:
                    _spare_contour_pts = _ub_silhouette_contour_points(eroded_sam)
                d_component = D[y0:y0 + hh, x0:x0 + ww][sub]
                _attach_i = int(np.argmin(d_component))
                attach_xy = (float(x0 + xs_sub[_attach_i]), float(y0 + ys_sub[_attach_i]))
                box = cv2.boxPoints(rect)
                edge0, edge1 = box[1] - box[0], box[2] - box[1]
                axis_vec = edge0 if np.linalg.norm(edge0) >= np.linalg.norm(edge1) else edge1
                spared, spare_median_d, spare_tangent_deg = _ub_shape_spare_silhouette_continuation(
                    d_component, attach_xy, axis_vec, _spare_contour_pts, scale)
                if spared:
                    is_wire = False
                    spare_mask[y0:y0 + hh, x0:x0 + ww] |= sub
            if is_wire:
                kill_seed_a[y0:y0 + hh, x0:x0 + ww] |= sub
            if _debug_rows is not None:
                _debug_rows.append(dict(
                    signal="A_elongation_cv", lbl=int(lbl), area=int(area),
                    aspect=round(float(aspect), 2), mean_thickness=round(mean_th, 2),
                    thickness_cv=round(cv_th, 3), long_side=round(float(long_side), 1),
                    killed=bool(is_wire), spared=bool(spared),
                    spare_median_d=(round(spare_median_d, 2) if spare_median_d is not None else None),
                    spare_tangent_deg=(round(spare_tangent_deg, 2) if spare_tangent_deg is not None else None),
                    bbox=[int(x0), int(y0), int(x0 + ww), int(y0 + hh)],
                ))

    # --- Signal B: Hessian ridge/line strength on the source plate, then a
    # light CC + min-area filter so single/double-pixel ridge noise can't fire.
    ridge_raw_seed = _ub_ridge_kill_seed(band_mask, D, scale, source_rgb, h, w)
    if ridge_raw_seed is not None and ridge_raw_seed.any():
        n_r, labels_r, stats_r, _ = cv2.connectedComponentsWithStats(
            ridge_raw_seed.astype(np.uint8), connectivity=8)
        for lbl in range(1, n_r):
            x0, y0, ww, hh, area = stats_r[lbl]
            if area < UNIFIED_BAND_SHAPE_RIDGE_MIN_AREA_PX:
                continue
            sub = (labels_r[y0:y0 + hh, x0:x0 + ww] == lbl)
            kill_seed_b[y0:y0 + hh, x0:x0 + ww] |= sub
            if _debug_rows is not None:
                _debug_rows.append(dict(
                    signal="B_ridge", lbl=int(lbl), area=int(area), killed=True,
                    bbox=[int(x0), int(y0), int(x0 + ww), int(y0 + hh)],
                ))

    if not kill_seed_a.any() and not kill_seed_b.any():
        if debug_dir is not None:
            _ub_dump_shape_debug(debug_dir, band_mask, empty_kill, empty_kill, _debug_rows, frame_idx=frame_idx)
        return band_alpha, empty_kill

    # Signal A needs the RECOVER_MARGIN growth to reclaim the near-body stub
    # the attach-strip removed for scoring purposes (round 1 design).
    #
    # Signal B does NOT get that same growth — round-3 diagnostic (P1b build
    # report) showed the wide recover margin, applied to ridge-seed pixels
    # found just outside the crease's own 16px test window, dilated FAR
    # enough (attach margin + 1 =~7px@4K) to reach back INTO the window and
    # zero real crease alpha there (broke G1). Signal B's own tiny near-
    # silhouette exclusion (UNIFIED_BAND_SHAPE_RIDGE_TINY_EXCLUDE_PX_BASE,
    # much smaller than signal A's attach margin) means it barely needs any
    # regrowth in the first place; it gets a MUCH smaller pad instead, just
    # enough to close 1px anti-aliasing gaps at a kill/keep boundary.
    # Recover-kernel building shared with the dot-kill pass via _ub_recover_kernel (F9).
    se_recover_a = _ub_recover_kernel(UNIFIED_BAND_SHAPE_RECOVER_MARGIN_PX_BASE, scale)
    kill_mask_a = cv2.dilate(kill_seed_a.astype(np.uint8), se_recover_a) > 0 if kill_seed_a.any() else empty_kill
    # P4 gate-review fix 2 (2026-07-11): the dilate above is COMPONENT-BLIND —
    # it grows every killed fragment's footprint by RECOVER_MARGIN regardless
    # of what else sits nearby, so a killed wire fragment within ~RECOVER_
    # MARGIN px of a component the spare test just protected would silently
    # re-zero that spared component's own pixels, defeating the spare test
    # without ever touching is_wire or kill_seed_a for that component. Strip
    # the spared components' EXACT pixels back out after the dilation — the
    # halo must still apply everywhere else (genuine background/wire pixels
    # around a spared component are NOT protected, only the spared component
    # itself), so this is a targeted subtraction, not a blanket exemption.
    if spare_mask.any():
        kill_mask_a = kill_mask_a & (~spare_mask)

    se_recover_b = _ub_recover_kernel(1, 1.0, plus_one=False)
    kill_mask_b = cv2.dilate(kill_seed_b.astype(np.uint8), se_recover_b) > 0 if kill_seed_b.any() else empty_kill

    kill_mask = (kill_mask_a | kill_mask_b) & band_mask  # never reach beyond the band zone

    out = band_alpha.copy()
    out[kill_mask] = 0.0

    if debug_dir is not None:
        _ub_dump_shape_debug(debug_dir, band_mask, kill_seed_a | kill_seed_b, kill_mask, _debug_rows,
                              frame_idx=frame_idx)

    return out, kill_mask


def _ub_dump_shape_debug(debug_dir, band_mask, stripped, kill_mask, debug_rows, frame_idx=None):
    """Debug-mode overlay: green = band pixels kept, red = shape-pass killed.
    Diagnostics only — caller wraps this in try/except (never break a render).
    frame_idx (F11, 2026-07-05): see _ub_dbg_name — None keeps the old names."""
    import cv2
    import json as _json_shp
    h, w = band_mask.shape
    overlay = np.zeros((h, w, 3), dtype=np.uint8)
    overlay[band_mask] = (0, 160, 0)      # green = band, kept
    overlay[kill_mask] = (0, 0, 220)      # red = shape-pass kill
    cv2.imwrite(str(debug_dir / f"{_ub_dbg_name('unified_band_shape_kill_overlay', frame_idx)}.png"), overlay)
    (debug_dir / f"{_ub_dbg_name('unified_band_shape_features', frame_idx)}.json").write_text(
        _json_shp.dumps(debug_rows or [], indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# RETIRED-RULE COVERAGE MATRIX (P1 deliverable, gate for P2 entry per the plan).
# Every rule the plan's own text attributes to merge_ck_with_garbage_matte,
# audited against the ACTUAL source at lines ~1063-1308 (the SPEC, per the
# builder's law) rather than assumed from the plan prose alone:
#
#   tight/wide (sam_tight/sam_wide)      -> COVERED: W(p) tight/wide blend (this fn)
#   G_soft (confidence blend weight)      -> COVERED: C(p) bilateral-smoothed HSV score
#   feet override (feet ring kill)        -> COVERED: continuous feet taper, same
#                                            feet_start=70% bbox_h boundary
#   feet erosion                          -> COVERED: subsumed by the feet taper driving
#                                            W down to tight (no separate raw-SAM erode
#                                            needed — the taper already removes the ring
#                                            continuously instead of via a binary hug)
#   escape valve (chroma near-SAM pass)   -> COVERED: C(p)'s on-green confidence feeding
#                                            W(p) IS the escape valve, generalized from a
#                                            fixed-radius proximity test to the same
#                                            continuous field driving the whole edge
#   off-green body fill                   -> COVERED verbatim: same HSV threshold, same
#                                            92% shadow cut, gated to the eroded-SAM
#                                            keep zone per design point 5 (LOAD-BEARING
#                                            RESCUE, not a bypass)
#   shadow_kill                           -> COVERED verbatim: same val<52/255 threshold,
#                                            same off-green/outside-SAM gating
#   framing guards (_body_exits_bottom)   -> COVERED verbatim: identical bbox_y1>=97%*h
#                                            test, disables the feet taper AND the 92%
#                                            shadow cut exactly like the old function
#   no-down kernels (sam_wide top-zeroed) -> COVERED, re-derived: masked term forcing
#                                            W to ZERO (not tight — F10 correction,
#                                            2026-07-05) for any exterior pixel directly
#                                            below the lowest SAM pixel in its own column
#   92% cut (shadow-below-feet)           -> COVERED verbatim (see off-green body fill)
#   largest-blob / carve                  -> COVERED verbatim: solidify_sam_silhouette
#                                            is reused unchanged (same shared helper)
#   blue branch (screen_type)             -> COVERED: C(p) and on_green_hsv both branch
#                                            on screen_type=="blue" with the same hue
#                                            ranges as the old function
#
#   The following rules the PLAN TEXT lists as "every rule of
#   merge_ck_with_garbage_matte" do NOT actually exist in that function's source
#   (lines 1063-1308, read in full for this build). They live ONLY in the DORMANT
#   merge_ck_with_sam_chroma_gated function (~line 505), which MERGE_MODE has not
#   selected since 2026-05-27 (MERGE_MODE = "garbage_matte" at module top) — i.e.
#   they are not part of the ACTUAL spec per the builder's law ("the current
#   garbage_matte implementation is the SPEC"). Flagging the plan-vs-code
#   discrepancy explicitly rather than silently re-implementing dead code:
#     wing filter              -> DELIBERATELY NOT RE-EXPRESSED. Not in the active
#                                  spec (merge_ck_with_garbage_matte). Lives only in
#                                  the dormant chroma_gated function. No action.
#     ridge kill                -> same as above — dormant-function-only, not in the
#                                  active spec.
#     proximity / EDGE GUARD slider -> same — dormant-function-only. Note:
#                                  ae_processor.sam_garbage_merge still threads
#                                  sam2_margin/edge_guard_px into proximity_px for the
#                                  OLD dispatch path; merge_ck_unified_band does not
#                                  accept a proximity_px parameter because the active
#                                  spec it re-expresses never used it either.
#     seam suppression          -> same — dormant-function-only, not in the active spec.
#   No silent drops of anything that IS in the active spec above.
# ----------------------------------------------------------------------------
def merge_ck_unified_band(
    ck_alpha: np.ndarray,
    sam_soft: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    settings: Optional[dict] = None,
    screen_type: str = "green",
    carve_points=None,
    return_garbage: bool = False,
    frame_idx=None,
):
    """UNIFIED BAND edge engine (P1, 2026-07-05) — distance-field + confidence-field
    merge. Continuous D(p)/C(p)/W(p)/support fields replace the old dual-mask
    (sam_tight/sam_wide) blend so the edge has exactly ONE monotonic transition per
    outward direction. See UNIFIED_EDGE_PLAN_2026-07-05.md for the full design and
    the coverage matrix comment block directly above this function.

    settings: per-session dict. Only unified_band_debug (bool) is read here — the
    "unified_band" selector key itself is checked by the CALLER
    (ae_processor.sam_garbage_merge), not by this function.

    No internal try/except around the core math (design point 6, "loud failures").
    A real failure must surface to the caller so it can log + re-raise a labeled
    error — this function must never silently degrade to a different merge.
    The optional debug PNG dump IS wrapped in try/except: diagnostics failing must
    never break a real render.

    return_garbage: when True, returns (alpha, band_gate) — band_gate uses the SAME
    multiplicative-gate semantics as merge_ck_with_garbage_matte's garbage_matte
    return (white=keep body, black=kill junk): 1.0 inside the eroded-SAM keep zone,
    `support` in the band, tending to 0 beyond W. The off-green body fill (like the
    old function's) is layered on top of this gate, not represented inside it.

    frame_idx: optional frame/seq identifier (F11, 2026-07-05) — threaded down to
    the debug-dump filenames only, so a batch run's per-frame dumps don't overwrite
    each other. None (default, single-frame callers) keeps the old unsuffixed names.
    """
    import cv2

    settings = settings or {}
    # F6 input guard (2026-07-05): capture the ORIGINAL dtype before the float32
    # cast below erases it — needed to pick the right rescale divisor if ck_alpha
    # turns out to still be in 0..255 / 0..65535 range (mirrors apply_shirt_rescue's
    # own dtype-normalize house pattern, ae_processor.py:630-632, which always
    # divides by 255 — this guard is the same idea, generalized to uint16 sources).
    _ck_alpha_orig_dtype = np.asarray(ck_alpha).dtype
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if ck.ndim == 3:
        ck = ck[..., 0]
    _ck_was_finite = bool(np.isfinite(ck).all())
    ck = np.nan_to_num(ck, nan=0.0, posinf=1.0, neginf=0.0)
    if not _ck_was_finite:
        print("CK_LOG: unified_band: input guard sanitized non-finite ck alpha "
              "(nan/posinf/neginf present)", flush=True)
    if ck.size and float(ck.max()) > 1.5:
        _ck_divisor = 65535.0 if _ck_alpha_orig_dtype == np.uint16 else 255.0
        ck = ck / _ck_divisor
        print(f"CK_LOG: unified_band: input guard rescaled ck alpha "
              f"(max>1.5, divided by {_ck_divisor:.0f})", flush=True)
    if sam_soft is None:
        print("CK_LOG: unified_band: SAM absent (sam_soft is None) — "
              "frame rendered CK-solo, no garbage protection", flush=True)
        return (ck.copy(), None) if return_garbage else ck.copy()
    sam_raw_bin = (np.asarray(sam_soft, dtype=np.float32) > 0.5).astype(np.uint8)
    if sam_raw_bin.ndim == 3:
        sam_raw_bin = sam_raw_bin[..., 0]
    if ck.shape != sam_raw_bin.shape:
        print(f"CK_LOG: unified_band: SAM shape mismatch (ck {ck.shape} vs "
              f"sam {sam_raw_bin.shape}) — frame rendered CK-solo, no garbage "
              f"protection", flush=True)
        return (ck.copy(), None) if return_garbage else ck.copy()
    h, w = ck.shape

    # --- BBOX-CROP (P2 perf, 2026-07-05/06) — see _ub_crop_margin_px's docstring
    # for the full padding proof. Crops ck/sam_raw_bin/source_rgb (and offsets
    # carve_points) to the RAW SAM bbox + a proven-safe margin BEFORE solidify,
    # so solidify_sam_silhouette's own cost (morph close/fill/connectedComponents)
    # scales with crop area too, not just the per-pixel field math below it.
    # Gated by settings['unified_band_crop'] — same per-session-key pattern as
    # 'unified_band' itself (checked by the caller, ae_processor.sam_garbage_merge).
    _ck_full = ck            # kept for the (practically unreachable, but shape-
    _full_h, _full_w = h, w  # -safe) "SAM empty after solidify" early-return below.
    _ub_crop_oy, _ub_crop_ox = 0, 0
    _ub_cropped = False
    if bool(settings.get("unified_band_crop", UNIFIED_BAND_CROP_DEFAULT)):
        _raw_ys, _raw_xs = np.where(sam_raw_bin > 0)
        if _raw_ys.size:
            _rb_y0, _rb_y1 = int(_raw_ys.min()), int(_raw_ys.max())
            _rb_x0, _rb_x1 = int(_raw_xs.min()), int(_raw_xs.max())
            _ub_margin_px = _ub_crop_margin_px(float(w) / 1920.0)
            _cy0 = max(0, _rb_y0 - _ub_margin_px)
            _cy1 = min(h, _rb_y1 + _ub_margin_px + 1)
            _cx0 = max(0, _rb_x0 - _ub_margin_px)
            _cx1 = min(w, _rb_x1 + _ub_margin_px + 1)
            if (_cy1 - _cy0) < h or (_cx1 - _cx0) < w:
                _ub_cropped = True
                _ub_crop_oy, _ub_crop_ox = _cy0, _cx0
                ck = ck[_cy0:_cy1, _cx0:_cx1]
                sam_raw_bin = sam_raw_bin[_cy0:_cy1, _cx0:_cx1]
                if source_rgb is not None:
                    source_rgb = np.asarray(source_rgb)[_cy0:_cy1, _cx0:_cx1]
                if carve_points:
                    _carve_local = []
                    for _pt in carve_points:
                        try:
                            _carve_local.append((float(_pt[0]) - _cx0, float(_pt[1]) - _cy0))
                        except (TypeError, IndexError, ValueError):
                            _carve_local.append(_pt)  # unparseable — let the
                            # existing per-point try/except in solidify's carve
                            # reassert / dot-kill log + skip it, same as today.
                    carve_points = _carve_local
                h, w = ck.shape
                print(f"CK_LOG: unified_band: crop ON — bbox=({_rb_x0},{_rb_y0})-"
                      f"({_rb_x1},{_rb_y1}) margin={_ub_margin_px}px crop={w}x{h} "
                      f"(full {_full_w}x{_full_h})", flush=True)

    # --- Post-solidify, post-carve silhouette. SAME shared helper the old merge and
    # the panel's SAM view call — preview == render by construction, and carve holes
    # are real background here so D(p) correctly collapses to 0 across an operator
    # exclusion (design point 1).
    sam = solidify_sam_silhouette(sam_raw_bin, carve_points)
    sam_bool = sam > 0

    ys = np.where(sam_bool)[0]
    if ys.size == 0:
        print("CK_LOG: unified_band: SAM empty after solidify (ys.size==0) — "
              "frame rendered CK-solo, no garbage protection", flush=True)
        return (_ck_full.copy(), None) if return_garbage else _ck_full.copy()
    bbox_y0, bbox_y1 = int(ys.min()), int(ys.max())
    bbox_h = max(bbox_y1 - bbox_y0, 1)
    feet_start = bbox_y0 + int(bbox_h * UNIFIED_BAND_FEET_ZONE_START_PCT)

    # Framing guard — KEPT VERBATIM from merge_ck_with_garbage_matte (Berto's
    # waist-crop protection law). Disables the feet taper and the 92% shadow cut
    # below exactly like it disables the feet-ring hug in the old function.
    # bbox_y1 is CROP-LOCAL when cropping is on — the "does the body run off
    # the bottom of the FRAME" test must compare against the TRUE frame height
    # and the body's ABSOLUTE row, not the crop's (task brief: "pass absolute
    # frame h, don't recompute from crop"). feet_start/the taper/no-down zone
    # below stay crop-local on purpose — those are pure offsets of bbox_y0/h,
    # which commute with translation, so computing them locally already gives
    # the same rows a full-frame run would (shifted by the same crop origin).
    _body_exits_bottom = (bbox_y1 + _ub_crop_oy) >= int(_full_h * 0.97)

    # _scale MUST reflect the TRUE FRAME resolution, not the crop's own (smaller)
    # width — every UNIFIED_BAND_*_PX_BASE constant is calibrated "at 1920px
    # wide" against the SOURCE frame, so scaling them against the crop width
    # would silently shrink every width/feather/bilateral/snap-radius constant
    # whenever cropping shrank w below the full frame (caught by V1: it turned
    # a ~0.66 vs ~0.69 confidence difference into a 33px vs 10px W difference —
    # _full_w is the fix, _ub_cropped or not).
    _scale = float(_full_w) / 1920.0
    # Unconditional feet-floor width (F2, 2026-07-05): same UNIFIED_BAND_FEET_TIGHT_PX_BASE
    # the feet taper below uses, but computed regardless of _body_exits_bottom so the
    # dot-kill pass always has a feet-zone attach-margin cap available — the framing
    # guard disables the feet TAPER (waist-crop protection), not the geometric fact
    # that rows >= feet_start are the feet zone for dot-kill purposes.
    _feet_floor_px_const = UNIFIED_BAND_FEET_TIGHT_PX_BASE * _scale

    # --- C(p): continuous HSV chroma confidence, screen_type branched. Also builds
    # on_green_hsv (binary-ish on-screen-color test), reused unchanged below by the
    # off-green body fill and shadow_kill, exactly like the old function.
    C = None
    on_green_hsv = None
    val = None
    if source_rgb is not None:
        try:
            rgb_in = np.asarray(source_rgb)
            if rgb_in.dtype != np.float32:
                _img_u8 = np.clip(rgb_in, 0, 255).astype(np.uint8)
            else:
                _img_u8 = (np.clip(rgb_in, 0.0, 1.0) * 255).astype(np.uint8)
            if _img_u8.ndim == 2:
                _img_u8 = np.stack([_img_u8, _img_u8, _img_u8], axis=-1)
            if _img_u8.shape[:2] != (h, w):
                _img_u8 = cv2.resize(_img_u8, (w, h), interpolation=cv2.INTER_AREA)
            img_bgr = cv2.cvtColor(_img_u8, cv2.COLOR_RGB2BGR)
            hsv_map = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            hue = hsv_map[..., 0].astype(np.float32)
            sat = hsv_map[..., 1].astype(np.float32) / 255.0
            val = hsv_map[..., 2].astype(np.float32) / 255.0

            # F8 (2026-07-05): hue bounds DERIVED from the named UNIFIED_BAND_HUE_*
            # constants instead of duplicated literals — verified analytically equal
            # to the literals they replace before shipping: green 60.0-25.0=35.0,
            # 60.0+25.0=85.0 (was [35,85]); blue 115.0-15.0=100.0, 115.0+15.0=130.0
            # (was [100,130]). int(round()) matches the original int literals exactly
            # (no fractional component in any of these four values).
            if screen_type == "blue":
                _hue_center = UNIFIED_BAND_HUE_BLUE_CENTER
                _hue_half = UNIFIED_BAND_HUE_BLUE_HALF_WIDTH
                _lower, _upper = (np.array([int(round(_hue_center - _hue_half)), 50, 50]),
                                  np.array([int(round(_hue_center + _hue_half)), 255, 255]))
            else:
                _hue_center = UNIFIED_BAND_HUE_GREEN_CENTER
                _hue_half = UNIFIED_BAND_HUE_GREEN_HALF_WIDTH
                # Value floor 20 (~50/255) — matches merge_ck_with_garbage_matte's
                # 2026-07-02 restore (floor 50 amputated the subject's own shadow-on-
                # screen zones). Same reasoning applies to this fresh HSV pass.
                _lower, _upper = (np.array([int(round(_hue_center - _hue_half)), 50, 50]),
                                  np.array([int(round(_hue_center + _hue_half)), 255, 255]))
            _green_bin = cv2.inRange(hsv_map, _lower, _upper)
            on_green_hsv = _green_bin.astype(np.float32) / 255.0

            hue_dist = np.abs(hue - _hue_center)
            hue_dist = np.minimum(hue_dist, 180.0 - hue_dist)  # OpenCV hue wraps at 180
            hue_score = 1.0 - np.clip(hue_dist / _hue_half, 0.0, 1.0)
            sat_score = _ub_smoothstep(UNIFIED_BAND_SAT_FLOOR, UNIFIED_BAND_SAT_CEIL, sat)
            val_score = _ub_smoothstep(UNIFIED_BAND_VAL_FLOOR, UNIFIED_BAND_VAL_CEIL, val)
            raw_C = (hue_score * sat_score * val_score).astype(np.float32)

            # Smoothing: bilateralFilter (INSTALLED, verified 2026-07-05). cv2.ximgproc
            # guided filter is CONFIRMED NOT INSTALLED in the CK venv — never import it.
            _sigma_space = max(1.0, UNIFIED_BAND_BILATERAL_SIGMA_SPACE_BASE * _scale)
            _bilateral_d = max(3, int(round(_sigma_space)) | 1)
            C = cv2.bilateralFilter(raw_C, _bilateral_d,
                                     UNIFIED_BAND_BILATERAL_SIGMA_COLOR, _sigma_space)
            C = np.clip(C, 0.0, 1.0).astype(np.float32)
        except Exception:
            C = None
            on_green_hsv = None
            val = None
    if C is None:
        # No source plate — no confidence signal available. Falls back to tight
        # width everywhere: the SAME degraded posture merge_ck_with_garbage_matte
        # takes when source_rgb is absent (there: Y-position blend; here: flat zero
        # confidence collapses W(p) to tight_px uniformly).
        C = np.zeros((h, w), dtype=np.float32)

    # --- feet erosion (design point covered in the retirement matrix): the old
    # feet-ring-kill didn't just narrow the WIDTH budget near the feet, it eroded
    # the silhouette itself ~1px@1920 on the OFF-GREEN side only (green side keeps
    # raw SAM — "over green CK rules and is already tight", same reasoning as the
    # old function). Re-expressed here as sam_for_dist: the silhouette D(p) and the
    # eroded-SAM keep-test are measured against, tightened in the feet zone on
    # off-green pixels only. Discovered necessary during P1 verification — tapering
    # W/feather alone left the feet-zone pixel budget ~19% over the ±5% tolerance;
    # the silhouette itself has to shrink there too, not just the trust radius.
    sam_for_dist = sam
    if not _body_exits_bottom and on_green_hsv is not None:
        _feet_erode_r = max(1, int(round(UNIFIED_BAND_FEET_ERODE_PX_BASE * _scale)))
        _se_feet_ub = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (_feet_erode_r * 2 + 1, _feet_erode_r * 2 + 1))
        _sam_feet_eroded_ub = cv2.erode(sam, _se_feet_ub)
        # TWO-TIER (Berto 2026-07-10): minus one MORE pixel calf-and-below only
        # (bottom 15% of bbox); the rest of the feet zone keeps the 1px hug.
        # Mirrors merge_ck_with_garbage_matte's two-tier block exactly.
        _calf_start_ub = bbox_y0 + int(bbox_h * UNIFIED_BAND_CALF_START_PCT)
        _calf_erode_r = max(1, int(round(UNIFIED_BAND_CALF_ERODE_PX_BASE * _scale)))
        _se_calf_ub = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (_calf_erode_r * 2 + 1, _calf_erode_r * 2 + 1))
        _sam_calf_eroded_ub = cv2.erode(sam, _se_calf_ub)
        _rows_col_ub = np.arange(h, dtype=np.float32)[:, None]
        _feet_rows_pre = np.broadcast_to(_rows_col_ub >= feet_start, (h, w))
        _calf_rows_ub = np.broadcast_to(_rows_col_ub >= _calf_start_ub, (h, w))
        _eroded_sel_ub = np.where(_calf_rows_ub, _sam_calf_eroded_ub, _sam_feet_eroded_ub)
        _off_green_feet_ub = _feet_rows_pre & (on_green_hsv < 0.5)
        sam_for_dist = np.where(_off_green_feet_ub, _eroded_sel_ub, sam).astype(np.uint8)

    # --- D(p): exterior distance transform from the (feet-corrected) silhouette.
    D = cv2.distanceTransform((1 - sam_for_dist).astype(np.uint8), cv2.DIST_L2, 5).astype(np.float32)

    # --- W(p): width field. tight + (wide-tight)*smoothstep(C), no-down bias, feet
    # taper (guarded), low-passed. All resolution-relative (design point 3).
    _tight_px = UNIFIED_BAND_TIGHT_PX_BASE * _scale
    _wide_px = UNIFIED_BAND_WIDE_PX_BASE * _scale
    W = _tight_px + (_wide_px - _tight_px) * _ub_smoothstep(0.0, 1.0, C)

    # No-down directional bias: the old sam_tight/sam_wide dilation kernels used a
    # SINGLE-ROW structuring element (height=1) for sam_tight and a top-half-zeroed
    # ellipse for sam_wide — both give EXACTLY ZERO vertical extent downward, not
    # just a smaller one. An isotropic width budget (even "tight") still lets the
    # distance-field trust a few px straight down, which doesn't match that — it
    # measurably leaked floor pixels below the feet during P1 verification (found
    # via direct diff against the old baseline: extra alpha concentrated in the
    # rows well below feet_start, i.e. below the body, not near its lateral edges).
    # So: any exterior pixel directly below the LOWEST SAM pixel in its own column
    # gets W FORCED TO ZERO (not tight) — matching the old kernels' zero downward
    # budget exactly, regardless of confidence.
    col_has_sam = sam_bool.any(axis=0)
    _rows = np.arange(h, dtype=np.int64)[:, None]
    _bottom_of_col = np.where(
        col_has_sam,
        h - 1 - np.argmax(sam_bool[::-1, :], axis=0),
        -1,
    )[None, :]
    downward_zone = (_bottom_of_col >= 0) & (_rows > _bottom_of_col)
    W = np.where(downward_zone, 0.0, W)

    # Feather field — starts flat at _feather_px, then gets TIGHTENED (not just W)
    # in the no-down zone and the feet taper below. A flat wide feather in those
    # zones let a wide, wobbly ±feather transition band survive right at the feet/
    # floor even after W collapsed to tight — that's what blew the feet-zone pixel
    # budget ~19% over the ±5% Berto-tuned tolerance during P1 verification (the old
    # feet-ring-kill was a near-binary hug, not a wide continuous ramp). Tapering
    # feather alongside W restores that same near-binary precision at the feet/floor
    # while leaving the body/hair transition (where the crease-fix lives) untouched.
    _feather_px = max(1.0, UNIFIED_BAND_FEATHER_PX_BASE * _scale)
    _feather_tight_floor = max(1.0, UNIFIED_BAND_FEATHER_TIGHT_FLOOR_PX_BASE * _scale)
    feather_field = np.full((h, w), _feather_px, dtype=np.float32)
    feather_field = np.where(downward_zone, _feather_tight_floor, feather_field)

    # Continuous feet taper near bbox bottom — DISABLED by the framing guard exactly
    # like the old feet-ring-kill (waist-crop protection kept verbatim). Transition
    # width is a SHORT 30px@1920 ramp (matches the old function's own
    # transition_px=30 constant, its Y-blend fallback's ramp width), NOT a taper
    # across the full feet-zone span — the old feet-ring-kill is a near-immediate
    # lockdown to the tight hug for the WHOLE feet zone, not a gradual full-span
    # taper. A full-span linear taper (tried first during P1 verification) left
    # ~16px of generous width for most of the feet zone on confidently-green floor
    # pixels beside the legs/feet, which is exactly the old code's feet-zone
    # override existed to prevent — found via direct diff against the old baseline
    # (extra alpha traced to green-floor-beside-the-leg pixels at high confidence).
    if not _body_exits_bottom:
        _feet_floor_px = UNIFIED_BAND_FEET_TIGHT_PX_BASE * _scale
        _feet_transition_px = max(1.0, UNIFIED_BAND_FEET_TRANSITION_PX_BASE * _scale)
        _row_idx = np.arange(h, dtype=np.float32)
        _feet_t = np.clip((_row_idx - feet_start) / _feet_transition_px, 0.0, 1.0)
        _feet_taper = np.broadcast_to((1.0 - _feet_t)[:, None], (h, w))
        _feet_rows = np.broadcast_to(_row_idx[:, None] >= feet_start, (h, w))
        W = np.where(_feet_rows, _feet_floor_px + (W - _feet_floor_px) * _feet_taper, W)
        feather_field = np.where(
            _feet_rows,
            _feather_tight_floor + (feather_field - _feather_tight_floor) * _feet_taper,
            feather_field,
        )

    # Low-pass W — kills local wobble from C's gradients + the distance transform's
    # own quantization (design point 3).
    _w_lowpass_sigma = max(1.0, UNIFIED_BAND_W_LOWPASS_SIGMA_BASE * _scale)
    W = cv2.GaussianBlur(W, (0, 0), _w_lowpass_sigma)

    # --- support = smoothstep((W-D)/feather_px). Monotonic by construction — exactly
    # ONE transition per outward direction (design point 4). This is what removes
    # the high-low-high double-boundary rim defect the old dual-mask blend produced.
    support = _ub_smoothstep(-feather_field, feather_field, W - D)

    # --- Keep/kill/band rule (design point 5, both reviewers' amendment). Eroded
    # from sam_for_dist (not raw sam) so the feet-zone off-green shrink above also
    # narrows the "always fully keep" interior there, not just the band's D(p).
    _erode_px = max(1, int(round(UNIFIED_BAND_ERODE_PX_BASE * _scale)))
    _se_keep = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_erode_px * 2 + 1, _erode_px * 2 + 1))
    eroded_sam = cv2.erode(sam_for_dist, _se_keep) > 0

    # CK AUTHORITY (Berto 2026-07-06) — settings.get("ck_authority"), OFF by
    # default. See CK_AUTHORITY_* constants + _ck_authority_protect_mask
    # (shared with merge_ck_with_garbage_matte, defined above
    # solidify_sam_silhouette) for the full why. D(p) computed above is
    # ALREADY the exterior distance from the (feet-corrected) SAM silhouette
    # — zero inside SAM by construction — so this is the near_body measure
    # the rule needs at no extra distance-transform cost.
    #
    # Placement is deliberate: AFTER support/eroded_sam, BEFORE band_alpha, so
    # the shape-kill and dot-kill passes immediately below (which operate on
    # band_alpha) run on TOP of this boosted support and keep final say over
    # any protected pixel — an operator's negative dot or a real wire-shape
    # verdict must still win. Do not reorder those passes relative to this
    # block; only touch `support` here.
    # Strict `is True` on purpose: settings is merged unfiltered JSON
    # (ae_processor.load_settings), so a hand-edited string like "false" is
    # still truthy in Python — only an actual JSON `true` may arm this.
    if settings.get("ck_authority") is True and on_green_hsv is not None:
        try:
            print("CK_LOG: ck_authority ACTIVE (unified_band)", flush=True)
            _protect_soft = _ck_authority_protect_mask(
                ck, D, on_green_hsv, _scale,
                feet_start=(None if _body_exits_bottom else feet_start),
                edge_sigma=max(1.0, UNIFIED_BAND_OFF_GREEN_FEATHER_SIGMA_PX_BASE * _scale),
            )
            support = np.maximum(support, _protect_soft)
        except Exception as _auth_exc:
            print(f"CK_LOG: ck_authority FAILED, continuing without protection: {_auth_exc}",
                  flush=True)

    band_alpha = np.clip(ck * support, 0.0, 1.0).astype(np.float32)

    # --- SHAPE-DISCRIMINATION PASS (P1b, 2026-07-05/06) — kills wire-shaped
    # components in the band zone. Runs BEFORE the keep/band composite below so
    # a shape-killed pixel never survives via the eroded-SAM keep path either
    # (it can't — the kill only ever touches band_alpha, outside eroded_sam,
    # per _ub_shape_kill_wire_components's own band_mask scoping). See the
    # UNIFIED_BAND_SHAPE_* constants for the forensic finding this replaces
    # width/confidence knob-tuning with (that path was tried and reverted —
    # see UNIFIED_BAND_TIGHT_PX_BASE's honest-miss comment).
    _dbg_dir = None
    if settings.get("unified_band_debug"):
        try:
            import tempfile as _tf_ub
            from pathlib import Path as _P_ub
            _dbg_dir = _P_ub(_tf_ub.gettempdir()) / "ck_unified_band_debug"
            _dbg_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            _dbg_dir = None
    band_alpha, _shape_kill_mask = _ub_shape_kill_wire_components(
        band_alpha, eroded_sam, D, _scale, source_rgb=source_rgb, debug_dir=_dbg_dir,
        frame_idx=frame_idx)

    # --- NEGATIVE-DOT BAND KILL (P1c, 2026-07-05) — operator escape hatch.
    # Runs on whatever band_alpha the shape pass left behind (the honest-miss
    # wire remnant). carve_points is the SAME sam_negative list solidify_sam_
    # silhouette already consumed above for interior-hole carving — dots inside
    # eroded_sam are ignored here (that mechanism already owns them); only
    # band-zone dots reach this pass. See UNIFIED_BAND_DOTKILL_* constants.
    # feet_start / _feet_floor_px_const (F2, 2026-07-05): let the dot-kill pass
    # tighten its attach margin in the feet zone so a feet-zone dot isn't starved.
    band_alpha, _dot_kill_mask, _dot_kill_log = _ub_dot_kill_band_components(
        band_alpha, eroded_sam, D, _scale, carve_points,
        feet_start=feet_start, feet_floor_px=_feet_floor_px_const,
        debug_dir=_dbg_dir, frame_idx=frame_idx)
    for _dk_line in _dot_kill_log:
        try:
            print(f"CK_LOG: {_dk_line}", flush=True)
        except Exception:
            pass

    off_green_body = None
    if on_green_hsv is not None:
        # Off-green body fill — LOAD-BEARING RESCUE (body parts past the green edge,
        # dark fabric CK has zero signal on), NOT a bypass. Part of the keep rule,
        # gated to the eroded-SAM interior only (design point 5) — never blended
        # into the band. 92% shadow cut kept verbatim, disabled on waist-crop.
        _no_shadow = np.ones((h, w), dtype=np.float32)
        if not _body_exits_bottom:
            _y_cut = bbox_y0 + int(bbox_h * UNIFIED_BAND_SHADOW_CUT_PCT)
            _no_shadow[_y_cut:, :] = 0.0
        off_green_body = sam.astype(np.float32) * (1.0 - on_green_hsv) * _no_shadow
        _feather_sigma = max(1.0, UNIFIED_BAND_OFF_GREEN_FEATHER_SIGMA_PX_BASE * _scale)
        off_green_body = cv2.GaussianBlur(off_green_body, (0, 0), _feather_sigma)

    keep_alpha = np.maximum(ck, off_green_body) if off_green_body is not None else ck.copy()

    final = np.where(eroded_sam, keep_alpha, band_alpha).astype(np.float32)
    final = np.clip(final, 0.0, 1.0)

    # shadow_kill — NOT re-expressed verbatim. The old function's formula (same
    # val<52/255 * off-green * outside-SAM test, still used for the threshold) is
    # gated by (1-support) here, and that gate is load-bearing, not cosmetic:
    # verbatim (ungated) shadow_kill zeroed ANY dark, off-green, outside-raw-SAM
    # pixel regardless of how close it sat to the body — which stomped on the
    # continuous support field exactly at dark hair/crease pixels a few px outside
    # SAM's hard boundary, RECREATING the named forensic crease defect (row y=1190)
    # this whole engine exists to fix (confirmed by direct diagnostic during P1
    # build: verbatim shadow_kill reproduced the identical 4px hard-zero gap).
    # Gating by (1-support) preserves the ORIGINAL intent (kill dark background
    # shadow far from the body, e.g. subject shadow on the screen/floor, where
    # support is already ~0) while never overriding a pixel the width/support
    # field has already decided to trust.
    if on_green_hsv is not None and val is not None:
        shadow_kill = (
            (val < UNIFIED_BAND_SHADOW_KILL_VAL).astype(np.float32)
            * (1.0 - on_green_hsv)
            * (1.0 - sam.astype(np.float32))
            * (1.0 - support)
        )
        final = np.clip(final * (1.0 - shadow_kill), 0.0, 1.0).astype(np.float32)

    # --- FEET-ZONE OFF-GREEN ALPHA-RING HARDENING (P4, 2026-07-11) — see the
    # UNIFIED_BAND_FEET_RING_HARD_* constants above for the full forensic why.
    # Gated identically to the feet taper above: same feet_start row test (crop-
    # local, consistent with `final`/`on_green_hsv` at this point — the crop
    # paste-back happens LATER, below), same _body_exits_bottom waist-crop
    # framing guard (Berto's protection law), on_green_hsv required (no source
    # plate => no color signal => nothing to harden against). Applied to
    # `final` AFTER shadow_kill so it sees the same alpha the shipped render
    # would, and BEFORE the debug dump so unified_band_debug reflects reality.
    # Row-SLICED (not full-frame) on purpose — perf (P2 wall-time budget,
    # measured 55ms full-frame vs ~25-30ms sliced to just rows >= feet_start,
    # D:\CLAUDE_JUNK\ck_feet_ring\): feet_start is always the bottom ~30% of
    # the bbox, so slicing before the smoothstep/where pass roughly halves
    # this block's cost for free, no behavior change (rows above feet_start
    # are provably untouched either way).
    # 0 < feet_start (gate review 2026-07-11): a near-empty/failed SAM bbox can
    # compute feet_start == 0 — without the lower bound this block would
    # near-binary-collapse the ENTIRE frame's off-green alpha, not just the
    # feet. Same degenerate case _ck_authority_protect_mask already guards;
    # treat feet_start <= 0 as no-feet-info and skip.
    if (not _body_exits_bottom and on_green_hsv is not None
            and 0 < feet_start < h):
        _fz_final = final[feet_start:, :]
        _fz_on_green = on_green_hsv[feet_start:, :]
        _fz_hardened = _ub_smoothstep(
            UNIFIED_BAND_FEET_RING_HARD_LO, UNIFIED_BAND_FEET_RING_HARD_HI, _fz_final)
        _fz_final[:] = np.where(_fz_on_green < 0.5, _fz_hardened, _fz_final)

    if _dbg_dir is not None:
        try:
            import json as _json_ub
            _rim_count, _rim_hits = rim_detector_scan(final, sam.astype(np.uint8), window=40)
            _dbg_bgr = cv2.cvtColor((np.clip(final, 0, 1) * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
            for (_ry, _rx, _kind) in _rim_hits[:20000]:
                cv2.circle(_dbg_bgr, (int(_rx), int(_ry)), 2, (0, 255, 255), -1)
            cv2.imwrite(str(_dbg_dir / f"{_ub_dbg_name('unified_band_rim_overlay', frame_idx)}.png"), _dbg_bgr)
            cv2.imwrite(str(_dbg_dir / f"{_ub_dbg_name('unified_band_confidence_C', frame_idx)}.png"),
                        (np.clip(C, 0, 1) * 255).astype(np.uint8))
            cv2.imwrite(str(_dbg_dir / f"{_ub_dbg_name('unified_band_support', frame_idx)}.png"),
                        (np.clip(support, 0, 1) * 255).astype(np.uint8))
            (_dbg_dir / f"{_ub_dbg_name('unified_band_rim_count', frame_idx)}.json").write_text(
                _json_ub.dumps({"rim_profile_count": _rim_count}), encoding="utf-8")
        except Exception:
            pass  # debug dump only — never let a diagnostics failure break a real render

    if return_garbage:
        band_gate = np.where(eroded_sam, 1.0, support).astype(np.float32)
        # Reflect the shape-pass kill in the returned gate too, so any downstream
        # consumer of band_gate (debug tooling, future gates) stays consistent
        # with what `final` actually contains.
        band_gate = np.where(_shape_kill_mask, 0.0, band_gate)
        # Reflect the operator dot-kill too — same reasoning, keep band_gate
        # consistent with what `final` actually contains.
        band_gate = np.where(_dot_kill_mask, 0.0, band_gate)
        band_gate = np.clip(band_gate, 0.0, 1.0).astype(np.float32)
    else:
        band_gate = None

    # --- BBOX-CROP paste-back. Everywhere OUTSIDE the crop: D(p) there is >=
    # the crop margin, which is >= R1 = (WIDE_PX_BASE + FEATHER_PX_BASE) * scale
    # by _ub_crop_margin_px's own construction, so support == 0 EXACTLY
    # (smoothstep's hard floor) and eroded_sam is False (the crop fully
    # contains raw SAM, so nothing outside it is ever inside the eroded keep
    # zone) — band_alpha == 0 there and keep_alpha is never selected there.
    # A full-frame run of this exact function produces final == 0.0 / band_gate
    # == 0.0 at every such pixel too, so zero-initializing the full-size
    # canvas and pasting the crop's own result into the crop rectangle is
    # bit-exact, not an approximation.
    if _ub_cropped:
        _final_full = np.zeros((_full_h, _full_w), dtype=np.float32)
        _final_full[_ub_crop_oy:_ub_crop_oy + h, _ub_crop_ox:_ub_crop_ox + w] = final
        final = _final_full
        if band_gate is not None:
            _gate_full = np.zeros((_full_h, _full_w), dtype=np.float32)
            _gate_full[_ub_crop_oy:_ub_crop_oy + h, _ub_crop_ox:_ub_crop_ox + w] = band_gate
            band_gate = _gate_full

    return (final, band_gate) if return_garbage else final


def dispatch_unified_band(
    ck_alpha: np.ndarray,
    sam_soft: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    settings: Optional[dict] = None,
    screen_type: str = "green",
    carve_points=None,
    return_garbage: bool = False,
    frame_idx=None,
):
    """Single entry point for the P1 unified_band merge — this is what
    ae_processor.sam_garbage_merge calls when settings['unified_band'] is truthy,
    BEFORE the MERGE_MODE dispatch (per-session settings key, not the module
    constant — see UNIFIED_EDGE_PLAN_2026-07-05.md). Deliberately has NO internal
    try/except: a failure here must surface loudly to the caller, never fall
    through to a different merge silently (design point 6).

    frame_idx (F11, 2026-07-05): optional frame/seq identifier, passed straight
    through to merge_ck_unified_band's debug-dump filenames."""
    return merge_ck_unified_band(
        ck_alpha, sam_soft, source_rgb=source_rgb, settings=settings,
        screen_type=screen_type, carve_points=carve_points,
        return_garbage=return_garbage, frame_idx=frame_idx,
    )
