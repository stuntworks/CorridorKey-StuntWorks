# Last modified: 2026-06-12 | Change: Phase 3b — hybrid band combiner (Berto's law)
#
# WHAT IT DOES:
#   Hybrid matting solver.  Inside the unknown band ONLY:
#     alpha = W * nn_alpha + (1 - W) * vitmatte_alpha
#   W is a per-pixel green-backing confidence map [0, 1] built from per-shot
#   screen-color statistics (Mahalanobis distance in LAB chroma space).
#   W=1 means the local background is screen-colored → CK/NN alpha is accurate.
#   W=0 means the local background is NOT screen-colored → trust ViTMatte.
#   Feet zone (bottom 12% of bbox) hard-overrides W=0 unconditionally.
#   Registers as 'hybrid' in the shared solver interface.
#
#   LAB COLORSPACE: a* axis cleanly separates green from non-green.
#   uint8 LAB: a*_uint8 < 128  →  a* < 0 (float)  →  green side.
#
#   BG CHROMA PROPAGATION: cv2.inpaint(INPAINT_TELEA) fills the unknown band
#   from the surrounding definite-BG ring pixels.  Each unknown-band pixel gets
#   an estimate of its nearest background color without scipy dependency.
#
#   FALLBACK: if 'vitmatte' is not registered, falls back to 'guided'.
#   Torch is never imported at module level — only inside the called solver.
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

# Minimum green-BG pixels required to fit screen-color statistics.
# Below this, W=0 everywhere (ViTMatte rules all).
_MIN_GREEN_PIXELS = 100

# Mahalanobis distance falloff: W = exp(-d_sq / _FALLOFF_SCALE).
# Scale=2.0 → W drops to ~0.6 at 1 sigma, ~0.01 at 3 sigma.
_FALLOFF_SCALE = 2.0

# Trimming percentile for robust statistics (5th–95th).
_TRIM_PCT = 5.0

# cv2.inpaint neighbourhood radius (pixels).  Controls estimation quality,
# not fill reach — TELEA fast-marching fills the full mask regardless.
_INPAINT_RADIUS = 5


# ---------------------------------------------------------------------------
# Screen-color statistics fitting
# ---------------------------------------------------------------------------

def _fit_screen_stats(frame_lab_u8: np.ndarray, bg_mask: np.ndarray):
    """
    Fit a 2D Gaussian to the green-side chroma (a*, b*) of definite-BG pixels.

    Returns (mu, cov_inv) as float32 arrays, or None if not enough green pixels.

    Parameters
    ----------
    frame_lab_u8 : uint8 (H, W, 3) LAB image (OpenCV encoding)
    bg_mask      : bool (H, W) — True where trimap == BG
    """
    ab = frame_lab_u8[:, :, 1:3].astype(np.float32)  # a*, b* in [0, 255]
    bg_chroma = ab[bg_mask]  # (N, 2)

    if bg_chroma.shape[0] == 0:
        return None

    # Pre-filter: a*_uint8 < 128  →  green side in float LAB
    green_idx = bg_chroma[:, 0] < 128.0
    green_chroma = bg_chroma[green_idx]

    if green_chroma.shape[0] < _MIN_GREEN_PIXELS:
        return None

    # Robust trim at 5th–95th percentile per axis
    lo_a = float(np.percentile(green_chroma[:, 0], _TRIM_PCT))
    hi_a = float(np.percentile(green_chroma[:, 0], 100.0 - _TRIM_PCT))
    lo_b = float(np.percentile(green_chroma[:, 1], _TRIM_PCT))
    hi_b = float(np.percentile(green_chroma[:, 1], 100.0 - _TRIM_PCT))

    keep = (
        (green_chroma[:, 0] >= lo_a) & (green_chroma[:, 0] <= hi_a) &
        (green_chroma[:, 1] >= lo_b) & (green_chroma[:, 1] <= hi_b)
    )
    trimmed = green_chroma[keep]

    if trimmed.shape[0] < 50:
        trimmed = green_chroma

    mu  = trimmed.mean(axis=0)                                     # (2,)
    cov = np.cov(trimmed.T) + _EPSILON * np.eye(2, dtype=np.float32)  # (2, 2)

    try:
        cov_inv = np.linalg.inv(cov).astype(np.float32)
    except np.linalg.LinAlgError:
        return None

    return mu.astype(np.float32), cov_inv


# ---------------------------------------------------------------------------
# Background chroma inpainting
# ---------------------------------------------------------------------------

def _inpaint_bg_chroma(
    frame_lab_u8: np.ndarray,
    trimap: np.ndarray,
) -> np.ndarray:
    """
    Estimate local background chroma (a*, b*) at each unknown-band pixel by
    inpainting the BG ring colors inward via cv2.inpaint(INPAINT_TELEA).

    Crops to the non-BG bounding box for efficiency.

    Returns float32 (H, W, 2); meaningful only where trimap == _UNKNOWN.
    """
    H, W = trimap.shape
    ab = frame_lab_u8[:, :, 1:3]  # uint8 a*, b*

    inpaint_mask = (trimap == _UNKNOWN).astype(np.uint8)

    if inpaint_mask.sum() == 0:
        return ab.astype(np.float32)

    # Crop to bbox of non-BG region + small margin for efficiency
    rows = np.any(trimap != _BG, axis=1)
    cols = np.any(trimap != _BG, axis=0)
    y0 = max(0, int(np.argmax(rows)) - 8)
    y1 = min(H, int(H - 1 - np.argmax(rows[::-1])) + 9)
    x0 = max(0, int(np.argmax(cols)) - 8)
    x1 = min(W, int(W - 1 - np.argmax(cols[::-1])) + 9)

    ab_crop   = ab[y0:y1, x0:x1]
    mask_crop = inpaint_mask[y0:y1, x0:x1]

    # Inpaint each chroma channel separately (cv2.inpaint needs 1- or 3-ch)
    a_filled = cv2.inpaint(
        ab_crop[:, :, 0], mask_crop, _INPAINT_RADIUS, cv2.INPAINT_TELEA
    )
    b_filled = cv2.inpaint(
        ab_crop[:, :, 1], mask_crop, _INPAINT_RADIUS, cv2.INPAINT_TELEA
    )

    result = ab.astype(np.float32).copy()
    result[y0:y1, x0:x1, 0] = a_filled.astype(np.float32)
    result[y0:y1, x0:x1, 1] = b_filled.astype(np.float32)

    return result


# ---------------------------------------------------------------------------
# Green confidence map
# ---------------------------------------------------------------------------

def _build_green_confidence_map(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    feet_zone_pct: float,
) -> np.ndarray:
    """
    Build per-pixel green-backing confidence W [0, 1].

    W is non-zero only inside the unknown band (trimap == 128).
    Feet zone gets W = 0 unconditionally.

    Parameters
    ----------
    frame_rgb     : uint8 (H, W, 3) RGB
    trimap        : uint8 (H, W)
    feet_zone_pct : fraction of bbox height that is the feet zone (0.12)

    Returns
    -------
    float32 (H, W) W map
    """
    H, W = trimap.shape

    # Convert to LAB (uint8 encoding: L in [0,255], a*/b* in [0,255])
    frame_lab = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2LAB)

    bg_mask = (trimap == _BG)

    # Fit screen-color statistics from definite-BG green pixels
    stats = _fit_screen_stats(frame_lab, bg_mask)
    if stats is None:
        # No green backing detected — ViTMatte rules everywhere
        return np.zeros((H, W), dtype=np.float32)

    mu, cov_inv = stats

    # Estimate local background chroma at each unknown-band pixel
    ab_bg_est = _inpaint_bg_chroma(frame_lab, trimap)  # float32 (H, W, 2)

    # Mahalanobis distance squared at every pixel
    diff = ab_bg_est - mu[np.newaxis, np.newaxis, :]   # (H, W, 2)
    flat = diff.reshape(-1, 2)                          # (H*W, 2)
    d_sq = np.sum((flat @ cov_inv) * flat, axis=1)     # (H*W,)
    d_sq = d_sq.reshape(H, W).astype(np.float32)

    # W = exp(-d_sq / scale); large d → small W → ViTMatte preferred
    W_map = np.exp(-np.clip(d_sq, 0.0, None) / _FALLOFF_SCALE)

    # Only meaningful inside the unknown band
    W_map[trimap != _UNKNOWN] = 0.0

    # Feet zone override: W = 0 (ViTMatte unconditionally)
    # Feet zone = bottom feet_zone_pct of bbox height, same definition as Phase 1
    non_bg_rows = np.any(trimap != _BG, axis=1)
    if non_bg_rows.any():
        y_min = int(np.argmax(non_bg_rows))
        y_max = int(H - 1 - np.argmax(non_bg_rows[::-1]))
        bh    = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - feet_zone_pct))
        W_map[feet_top:, :] = 0.0

    return W_map.astype(np.float32)


# ---------------------------------------------------------------------------
# Hybrid solver
# ---------------------------------------------------------------------------

def _hybrid_solve(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    nn_alpha: np.ndarray,
    feet_zone_pct: float = 0.12,
    **kwargs,
) -> np.ndarray:
    """
    Hybrid band combiner.

    Inside the unknown band:
        alpha = W * nn_alpha + (1 - W) * vitmatte_alpha

    W = green-backing confidence (per-shot Mahalanobis, LAB chroma).
    Feet zone: W = 0 unconditionally — ViTMatte only.
    FG / BG pixels pass through as exact 1.0 / 0.0.

    Falls back to 'guided' if 'vitmatte' is not registered.

    Parameters
    ----------
    frame_rgb     : uint8 (H, W, 3) RGB
    trimap        : uint8 (H, W) — 0=BG / 128=unknown / 255=FG
    nn_alpha      : float32 (H, W) — raw CK/NN alpha [0, 1]
    feet_zone_pct : fraction of bbox height for hard ViTMatte override (default 0.12)

    Returns
    -------
    float32 (H, W) hybrid alpha, clamped [0, 1]
    """
    from fusion_v2.solver_interface import available_solvers, solve_matte

    # Resolve which base solver to call for the ViTMatte leg
    solvers = available_solvers()
    if "vitmatte" in solvers:
        base_solver = "vitmatte"
    elif "guided" in solvers:
        warnings.warn(
            "'vitmatte' not registered — hybrid falling back to 'guided'. "
            "Import fusion_v2.solver_vitmatte before calling hybrid for full quality.",
            stacklevel=2,
        )
        base_solver = "guided"
    else:
        warnings.warn(
            "Neither 'vitmatte' nor 'guided' registered — hybrid using nn_alpha only.",
            stacklevel=2,
        )
        base_solver = None

    H, W = trimap.shape

    # Seed result with trimap hard values
    result = np.where(trimap == _FG, 1.0,
             np.where(trimap == _BG, 0.0, nn_alpha)).astype(np.float32)

    unknown_mask = (trimap == _UNKNOWN)
    if not unknown_mask.any():
        result[trimap == _FG] = 1.0
        result[trimap == _BG] = 0.0
        return result

    # Get base solver alpha (ViTMatte or guided)
    if base_solver is not None:
        base_alpha = solve_matte(
            frame_rgb, trimap, nn_alpha, solver=base_solver, **kwargs
        )
    else:
        base_alpha = nn_alpha.copy()

    # Build green confidence map W
    W_map = _build_green_confidence_map(frame_rgb, trimap, feet_zone_pct)

    # Blend inside unknown band only
    # alpha = W * nn_alpha + (1 - W) * base_alpha
    nn_f32   = nn_alpha.astype(np.float32)
    base_f32 = base_alpha.astype(np.float32)

    blended = W_map * nn_f32 + (1.0 - W_map) * base_f32

    result[unknown_mask] = blended[unknown_mask]

    # Re-enforce hard constraints
    result[trimap == _FG] = 1.0
    result[trimap == _BG] = 0.0

    return np.clip(result, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Self-registration (torch-free at import time)
# ---------------------------------------------------------------------------

register_solver("hybrid", _hybrid_solve)
