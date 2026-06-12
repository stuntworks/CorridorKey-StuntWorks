# Last modified: 2026-06-12 | Change: ck-green band -- CK rules all green + interior, solver only junk-backed edge + feet
#
# WHAT IT DOES:
#   Hybrid matting solver.  Inside the unknown band ONLY:
#     alpha = W * nn_alpha + (1 - W) * vitmatte_alpha
#   W is a per-pixel binary map derived from BAND_MODE (see constant below).
#
#   BAND_MODE = 'geometric'  (ACTIVE -- Berto verdict 2026-06-12):
#     W=1 (CK rules)  -- unknown pixel OUTSIDE the SAM binary silhouette.
#                        Outer edge ring: hair, wisps, fine detail. CK owns it.
#     W=0 (ViTMatte)  -- unknown pixel INSIDE the SAM binary silhouette.
#                        Interior holes (eaten-butt class). ViTMatte fills solid.
#     W=0 (ViTMatte)  -- FEET ZONE (bottom 12% bbox), inner AND outer band.
#                        Berto: "SAM feet look okay, just combine it with CK."
#
#   BAND_MODE = 'green-confidence'  (PARKED -- rejected 2026-06-12):
#     k-means LAB chroma W map. Rejected: too much CK detail lost. Code kept
#     (_is_green_centroid, _fit_green_components, _inpaint_bg_chroma,
#     _build_green_confidence_map) for the gauntlet comparative evaluation.
#
#   FG/BG: passthrough exact 1.0/0.0.
#   FALLBACK: 'guided' if 'vitmatte' not registered, with warning.
#   TORCH: never imported at module level.
#
# DEPENDS ON: fusion_v2.solver_interface, numpy, cv2
# AFFECTS: callers of solve_matte(solver='hybrid')
# ISOLATED: swapping solvers requires no change here

import warnings

import numpy as np
import cv2

from fusion_v2.solver_interface import register_solver

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BG      = np.uint8(0)
_UNKNOWN = np.uint8(128)
_FG      = np.uint8(255)
_EPSILON = 1e-7

# Active band mode.  'ck-green' is Berto law v2 since 2026-06-12.
# 'geometric': parked -- interior=ViTMatte was eating the butt (Berto, 2026-06-12).
# 'green-confidence': parked -- too much CK detail lost (Berto, 2026-06-12).
BAND_MODE = 'ck-green'

_KMEANS_K             = 3      # BG colour components (green-confidence mode)
_MAX_BG_SAMPLES       = 5000   # subsample cap for k-means speed
_MIN_COMPONENT_PIXELS = 20     # min pixels per cluster to fit covariance
_FALLOFF_SCALE        = 2.0    # W = exp(-d_sq / scale): ~0.6 at 1sigma
_INPAINT_RADIUS       = 5
_FEATHER_SIGMA_PCT    = 0.005  # 0.5% of bbox height (~10 px at 2160); smooths interior/outer seam


# ---------------------------------------------------------------------------
# Geometric W map (PARKED -- BAND_MODE = 'geometric' -- interior=ViTMatte ate the butt)
# ---------------------------------------------------------------------------

def _build_geometric_band_map(
    trimap: np.ndarray,
    sam_binary,
    feet_zone_pct: float,
) -> np.ndarray:
    """
    Pure-geometry W map -- no color statistics.

    W=1 (CK rules): unknown AND outside SAM silhouette (outer ring -- hair, wisps).
    W=0 (ViTMatte): unknown AND inside SAM silhouette (inner holes -- body bites).
    W=0 (ViTMatte): feet zone (bottom feet_zone_pct of bbox) inner AND outer.

    sam_binary: uint8 0/255, same shape as trimap or resized NEAREST.
                None -> all unknown treated as outer band (W=1 fallback).
    """
    H, W = trimap.shape
    W_map = np.zeros((H, W), dtype=np.float32)
    unknown_mask = (trimap == _UNKNOWN)

    if sam_binary is not None:
        sam = sam_binary
        if sam.ndim == 3:
            sam = sam[..., 0]
        if sam.shape[:2] != (H, W):
            sam = cv2.resize(sam.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
        # Outer ring: unknown AND outside SAM -> CK rules (W=1)
        outer_band = unknown_mask & (sam == 0)
        W_map[outer_band] = 1.0
        # Inner ring: unknown AND inside SAM -> ViTMatte rules (W stays 0)
    else:
        W_map[unknown_mask] = 1.0   # no SAM binary: full CK fallback

    # Feet zone override: W=0 entire band, inner and outer (same bbox def as Phase 1)
    non_bg_rows = np.any(trimap != _BG, axis=1)
    if non_bg_rows.any():
        y_min    = int(np.argmax(non_bg_rows))
        y_max    = int(H - 1 - np.argmax(non_bg_rows[::-1]))
        bh       = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - feet_zone_pct))
        W_map[feet_top:, :] = 0.0

    return W_map



# ---------------------------------------------------------------------------
# CK-GREEN W map (ACTIVE -- BAND_MODE = 'ck-green', Berto law v2 2026-06-12)
# ---------------------------------------------------------------------------

def _build_ck_green_band_map(
    trimap: np.ndarray,
    sam_binary,
    frame_rgb: np.ndarray,
    feet_zone_pct: float,
    feather_sigma_pct: float = _FEATHER_SIGMA_PCT,
) -> np.ndarray:
    """
    CK-GREEN W map -- CK rules wherever it can see green.  Solver covers blind spots only.

    RULE 1 -- Interior unknown (trimap==128 AND inside SAM silhouette): W=1 always.
      CK is correct here -- the body IS on the green screen and CK already keyed it.
      ViTMatte was eating the butt; this stops it.  Also preserves fence/see-through
      holes (Berto's fence rule: CK owns transparency inside the body).

    RULE 2 -- Outer unknown (trimap==128 AND outside SAM silhouette): per-pixel
      green-backed test on ACTUAL frame colors (no inpainting -- hair over green is
      already green-shifted in LAB and passes naturally).
        Green-backed -> W=1 (CK, full hair detail).
        Junk-backed  -> W=0 (ViTMatte kills / solves the dirty wall/floor edge).
      Uses the PARKED k-means green-component fit from definite-BG pixels.

    RULE 3 -- Feet zone (bottom feet_zone_pct of bbox): W=0, ViTMatte unconditional
      (same bbox definition as Phase 1 trimap).

    Feathering: small Gaussian blur at interior/outer seam (feather_sigma_pct * bbox_height)
      prevents a hard switch line.  Pass feather_sigma_pct=0.0 to get exact binary W
      (useful for tests verifying RULE 1 and RULE 2).

    sam_binary: uint8 0/255, same pixel space as trimap.
                None -> all unknown treated as interior (full CK fallback).
    """
    H, W = trimap.shape
    W_map = np.zeros((H, W), dtype=np.float32)
    unknown_mask = (trimap == _UNKNOWN)

    non_bg_rows = np.any(trimap != _BG, axis=1)
    has_bbox = bool(non_bg_rows.any())
    if has_bbox:
        y_min    = int(np.argmax(non_bg_rows))
        y_max    = int(H - 1 - np.argmax(non_bg_rows[::-1]))
        bh       = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - feet_zone_pct))
    else:
        bh       = H
        feet_top = H   # no-op slice

    if sam_binary is not None:
        sam = sam_binary
        if sam.ndim == 3:
            sam = sam[..., 0]
        if sam.shape[:2] != (H, W):
            sam = cv2.resize(sam.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)

        interior = unknown_mask & (sam > 0)
        outer    = unknown_mask & (sam == 0)

        # RULE 1: Interior -> W=1 (CK verbatim, no solver inside the body)
        W_map[interior] = 1.0

        # RULE 2: Outer -> per-pixel green-backed test on actual frame colors
        if outer.any():
            frame_lab    = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2LAB)
            bg_mask      = (trimap == _BG)
            green_stats  = _fit_green_components(frame_lab, bg_mask)
            if green_stats:
                flat_ab = frame_lab[:, :, 1:3].reshape(-1, 2).astype(np.float32)
                W_outer_flat = np.zeros(H * W, dtype=np.float32)
                for mu, cov_inv in green_stats:
                    diff   = flat_ab - mu[np.newaxis, :]
                    d_sq   = np.sum((diff @ cov_inv) * diff, axis=1)
                    w_comp = np.exp(-np.clip(d_sq, 0.0, None) / _FALLOFF_SCALE)
                    W_outer_flat = np.maximum(W_outer_flat, w_comp)
                W_outer = W_outer_flat.reshape(H, W)
                W_map[outer] = W_outer[outer]
            # else: no green BG components found -> W_map[outer] stays 0 (ViTMatte edge)
    else:
        # No SAM binary: CK everywhere in unknown band (safe fallback)
        W_map[unknown_mask] = 1.0

    # RULE 3: Feet zone -> W=0 (ViTMatte unconditional, same as Phase 1 trimap bbox)
    W_map[feet_top:, :] = 0.0

    # Feather the interior/outer seam to prevent a hard switch line
    if feather_sigma_pct > 0 and has_bbox:
        sigma_px = max(1.0, bh * feather_sigma_pct)
        ksize    = int(sigma_px * 6) | 1   # odd, ~99% Gaussian coverage
        W_map    = cv2.GaussianBlur(W_map, (ksize, ksize), sigma_px)
        W_map    = np.clip(W_map, 0.0, 1.0)

    return W_map


# ---------------------------------------------------------------------------
# Green-component detection (PARKED -- green-confidence mode only)
# ---------------------------------------------------------------------------

def _is_green_centroid(centroid_lab_u8_float: np.ndarray) -> bool:
    """True if LAB centroid is green-dominant: G > R AND G > B after BGR conversion."""
    lab_px = centroid_lab_u8_float.clip(0, 255).astype(np.uint8).reshape(1, 1, 3)
    bgr = cv2.cvtColor(lab_px, cv2.COLOR_LAB2BGR)
    B, G, R = int(bgr[0, 0, 0]), int(bgr[0, 0, 1]), int(bgr[0, 0, 2])
    return bool(G > R and G > B)


def _fit_green_components(
    frame_lab_u8: np.ndarray,
    bg_mask: np.ndarray,
    k: int = _KMEANS_K,
    max_samples: int = _MAX_BG_SAMPLES,
    seed: int = 42,
):
    """K-means on definite-BG LAB pixels; returns (mu_ab, cov_inv_ab) for green clusters."""
    lab_all = frame_lab_u8[bg_mask].astype(np.float32)
    N = len(lab_all)
    if N < k * _MIN_COMPONENT_PIXELS:
        return []
    if N > max_samples:
        rng = np.random.default_rng(seed)
        idx = rng.choice(N, size=max_samples, replace=False)
        lab_sample = lab_all[idx]
    else:
        lab_sample = lab_all
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1.0)
    _, labels_raw, centroids = cv2.kmeans(
        lab_sample.astype(np.float32), k, None, criteria,
        attempts=5, flags=cv2.KMEANS_PP_CENTERS,
    )
    labels = labels_raw.flatten().astype(np.int32)
    green_stats = []
    for i in range(k):
        if not _is_green_centroid(centroids[i]):
            continue
        component_ab = lab_sample[labels == i, 1:3]
        if len(component_ab) < _MIN_COMPONENT_PIXELS:
            continue
        mu  = component_ab.mean(axis=0)
        cov = np.cov(component_ab.T) + _EPSILON * np.eye(2, dtype=np.float32)
        if np.ndim(cov) != 2 or cov.shape != (2, 2):
            continue
        try:
            cov_inv = np.linalg.inv(cov).astype(np.float32)
        except np.linalg.LinAlgError:
            continue
        green_stats.append((mu.astype(np.float32), cov_inv))
    return green_stats


# ---------------------------------------------------------------------------
# Background chroma inpainting (PARKED -- green-confidence mode only)
# ---------------------------------------------------------------------------

def _inpaint_bg_chroma(frame_lab_u8: np.ndarray, trimap: np.ndarray) -> np.ndarray:
    """Inpaint BG chroma (a*, b*) into unknown band from surrounding BG ring."""
    H, W = trimap.shape
    ab = frame_lab_u8[:, :, 1:3]
    inpaint_mask = (trimap == _UNKNOWN).astype(np.uint8)
    if inpaint_mask.sum() == 0:
        return ab.astype(np.float32)
    rows = np.any(trimap != _BG, axis=1)
    cols = np.any(trimap != _BG, axis=0)
    y0 = max(0, int(np.argmax(rows)) - 8)
    y1 = min(H, int(H - 1 - np.argmax(rows[::-1])) + 9)
    x0 = max(0, int(np.argmax(cols)) - 8)
    x1 = min(W, int(W - 1 - np.argmax(cols[::-1])) + 9)
    ab_crop   = ab[y0:y1, x0:x1]
    mask_crop = inpaint_mask[y0:y1, x0:x1]
    a_filled = cv2.inpaint(ab_crop[:, :, 0], mask_crop, _INPAINT_RADIUS, cv2.INPAINT_TELEA)
    b_filled = cv2.inpaint(ab_crop[:, :, 1], mask_crop, _INPAINT_RADIUS, cv2.INPAINT_TELEA)
    result = ab.astype(np.float32).copy()
    result[y0:y1, x0:x1, 0] = a_filled.astype(np.float32)
    result[y0:y1, x0:x1, 1] = b_filled.astype(np.float32)
    return result


# ---------------------------------------------------------------------------
# Green confidence map (PARKED -- green-confidence mode only)
# ---------------------------------------------------------------------------

def _build_green_confidence_map(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    feet_zone_pct: float,
) -> np.ndarray:
    """W = max Mahalanobis likelihood over k-means green components. PARKED 2026-06-12."""
    H, W = trimap.shape
    frame_lab = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2LAB)
    bg_mask = (trimap == _BG)
    green_stats = _fit_green_components(frame_lab, bg_mask)
    if not green_stats:
        warnings.warn(
            "solver_hybrid: no green components found in definite-BG. "
            "W=0 everywhere -- off-green shot or trimap has no BG pixels.",
            stacklevel=3,
        )
        return np.zeros((H, W), dtype=np.float32)
    ab_bg_est = _inpaint_bg_chroma(frame_lab, trimap)
    flat_ab   = ab_bg_est.reshape(-1, 2)
    W_map = np.zeros(H * W, dtype=np.float32)
    for mu, cov_inv in green_stats:
        diff  = flat_ab - mu[np.newaxis, :]
        d_sq  = np.sum((diff @ cov_inv) * diff, axis=1)
        w_comp = np.exp(-np.clip(d_sq, 0.0, None) / _FALLOFF_SCALE)
        W_map  = np.maximum(W_map, w_comp)
    W_map = W_map.reshape(H, W).astype(np.float32)
    W_map[trimap != _UNKNOWN] = 0.0
    non_bg_rows = np.any(trimap != _BG, axis=1)
    if non_bg_rows.any():
        y_min    = int(np.argmax(non_bg_rows))
        y_max    = int(H - 1 - np.argmax(non_bg_rows[::-1]))
        bh       = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - feet_zone_pct))
        W_map[feet_top:, :] = 0.0
    return W_map


# ---------------------------------------------------------------------------
# Hybrid solver
# ---------------------------------------------------------------------------

def _hybrid_solve(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    nn_alpha: np.ndarray,
    sam_binary=None,
    feet_zone_pct: float = 0.12,
    **kwargs,
) -> np.ndarray:
    """
    Hybrid band combiner.  alpha = W * nn_alpha + (1 - W) * vitmatte_alpha.

    BAND_MODE='ck-green' (active -- Berto law v2 2026-06-12):
      Interior unknown (inside SAM)      -> W=1 -> CK verbatim (butt intact, fence rule).
      Outer unknown + green-backed pixel -> W=1 -> CK (hair/wisps/detail).
      Outer unknown + junk-backed pixel  -> W=0 -> ViTMatte (dirty wall/floor edge).
      Feet zone                          -> W=0 -> ViTMatte unconditionally.

    BAND_MODE='geometric' (parked -- interior=ViTMatte was eating the butt):
      Outer ring -> CK.  Inner ring -> ViTMatte.  Feet -> ViTMatte.

    BAND_MODE='green-confidence' (parked -- too much CK detail lost):
      k-means LAB chroma W map.

    sam_binary: uint8 0/255 mask (same pixel space as trimap).
                Required for ck-green / geometric modes. None -> W=1 fallback (all CK).
    """
    from fusion_v2.solver_interface import available_solvers, solve_matte

    solvers = available_solvers()
    if "vitmatte" in solvers:
        base_solver = "vitmatte"
    elif "guided" in solvers:
        warnings.warn(
            "'vitmatte' not registered -- hybrid falling back to 'guided'. "
            "Import fusion_v2.solver_vitmatte before calling hybrid for full quality.",
            stacklevel=2,
        )
        base_solver = "guided"
    else:
        warnings.warn(
            "Neither 'vitmatte' nor 'guided' registered -- hybrid using nn_alpha only.",
            stacklevel=2,
        )
        base_solver = None

    H, W = trimap.shape
    result = np.where(trimap == _FG,  1.0,
             np.where(trimap == _BG,  0.0, nn_alpha)).astype(np.float32)

    unknown_mask = (trimap == _UNKNOWN)
    if not unknown_mask.any():
        result[trimap == _FG] = 1.0
        result[trimap == _BG] = 0.0
        return result

    if base_solver is not None:
        base_alpha = solve_matte(frame_rgb, trimap, nn_alpha, solver=base_solver, **kwargs)
    else:
        base_alpha = nn_alpha.copy()

    if BAND_MODE == 'ck-green':
        W_map = _build_ck_green_band_map(trimap, sam_binary, frame_rgb, feet_zone_pct)
    elif BAND_MODE == 'geometric':
        W_map = _build_geometric_band_map(trimap, sam_binary, feet_zone_pct)
    else:
        W_map = _build_green_confidence_map(frame_rgb, trimap, feet_zone_pct)

    nn_f32   = nn_alpha.astype(np.float32)
    base_f32 = base_alpha.astype(np.float32)
    blended  = W_map * nn_f32 + (1.0 - W_map) * base_f32

    result[unknown_mask] = blended[unknown_mask]
    result[trimap == _FG] = 1.0
    result[trimap == _BG] = 0.0
    return np.clip(result, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Self-registration (torch-free at import time)
# ---------------------------------------------------------------------------

register_solver("hybrid", _hybrid_solve)
