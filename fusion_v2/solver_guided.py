# Last modified: 2026-06-12 | Change: Phase 2 — guided filter matting solver
#
# WHAT IT DOES:
#   Implements the guided filter matting solver and registers it as 'guided'.
#   The original frame (converted to grayscale luminance) guides a smoothed
#   alpha estimation inside the trimap unknown band.  Only the unknown band
#   is solved; definite-FG and definite-BG pixels pass through untouched.
#
#   Implementation path: cv2.ximgproc.guidedFilter is NOT available in this
#   venv (opencv-python, not opencv-contrib-python).  The guided filter is
#   implemented directly in numpy using cv2.blur (mean box filter) — O(N)
#   cost regardless of radius, consistent with the reference algorithm
#   (He et al., 2013).
#
#   Radius and eps are percentage-based:
#     radius  = radius_pct * bbox_height (consistent with Phase 1)
#     eps     = regularization in float32 [0,1] variance units
#
# DEPENDS ON: numpy, cv2, fusion_v2.solver_interface
# AFFECTS: any caller of solve_matte(solver='guided')
# ISOLATED: replacing this solver requires no changes to solver_interface.py

import numpy as np
import cv2

from fusion_v2.solver_interface import register_solver

_EPSILON = 1e-7

# Trimap class values (mirrors trimap_builder constants without cross-import)
_BG      = 0
_UNKNOWN = 128
_FG      = 255


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bbox_height_from_trimap(trimap: np.ndarray) -> int:
    """Return the height of the non-background (trimap != 0) bounding region."""
    fg_rows = np.any(trimap != _BG, axis=1)
    if not fg_rows.any():
        return 1
    y_min = int(np.argmax(fg_rows))
    y_max = int(len(fg_rows) - 1 - np.argmax(fg_rows[::-1]))
    return max(y_max - y_min + 1, 1)


def _guided_filter_gray(
    guide: np.ndarray,
    p: np.ndarray,
    radius: int,
    eps: float,
) -> np.ndarray:
    """
    Guided filter with a single-channel grayscale guide (He et al., 2013).

    All inputs float32 [0, 1].  Uses cv2.blur (mean box filter) — no scipy,
    no ximgproc.  cv2.blur is O(N) regardless of radius.

    Parameters
    ----------
    guide  : float32 (H, W), guide image (frame luminance)
    p      : float32 (H, W), input signal to filter (seeded alpha)
    radius : filter window half-size in pixels
    eps    : regularization — prevents division by zero and controls
             edge sensitivity.  Typical: 0.001 for sharp edges.
    """
    r = max(1, int(radius))
    ksize = (2 * r + 1, 2 * r + 1)

    mean_I  = cv2.blur(guide,         ksize)
    mean_p  = cv2.blur(p,             ksize)
    mean_II = cv2.blur(guide * guide, ksize)
    mean_Ip = cv2.blur(guide * p,     ksize)

    var_I  = mean_II - mean_I * mean_I
    cov_Ip = mean_Ip - mean_I * mean_p

    a = cov_Ip / (var_I + eps)
    b = mean_p - a * mean_I

    mean_a = cv2.blur(a, ksize)
    mean_b = cv2.blur(b, ksize)

    return mean_a * guide + mean_b


# ---------------------------------------------------------------------------
# Solver implementation
# ---------------------------------------------------------------------------

def _guided_solve(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    nn_alpha: np.ndarray,
    radius_pct: float = 0.02,
    eps: float = 0.001,
) -> np.ndarray:
    """
    Guided filter matting solver.

    Seeds alpha from nn_alpha inside the unknown band, guides via frame
    luminance, then enforces trimap hard constraints.

    Parameters
    ----------
    frame_rgb  : uint8 or float32 (H, W, 3), RGB channel order
    trimap     : uint8 (H, W), 0=BG / 128=unknown / 255=FG
    nn_alpha   : float32 (H, W), NN soft alpha in [0, 1]
    radius_pct : guided filter radius as fraction of trimap bbox height (default 2%)
    eps        : guided filter regularization in [0, 1] variance units (default 0.001)

    Returns
    -------
    float32 (H, W) alpha, clamped [0, 1], FG/BG pixels exact at 1.0/0.0
    """
    H, W = trimap.shape

    # Guide: grayscale luminance, float32 [0, 1]
    if frame_rgb.dtype == np.uint8:
        frame_f = frame_rgb.astype(np.float32) / 255.0
    else:
        frame_f = frame_rgb.astype(np.float32)
        if frame_f.max() > 1.0 + _EPSILON:
            frame_f /= 255.0

    guide = (0.299 * frame_f[:, :, 0]
             + 0.587 * frame_f[:, :, 1]
             + 0.114 * frame_f[:, :, 2]).astype(np.float32)

    # Seed alpha: use nn_alpha in unknown band; hard values elsewhere
    nn_f32  = nn_alpha.astype(np.float32)
    seeded  = np.where(trimap == _FG, 1.0,
              np.where(trimap == _BG, 0.0, nn_f32)).astype(np.float32)

    # Radius from bbox height — same convention as Phase 1 morphology
    bh     = _bbox_height_from_trimap(trimap)
    radius = max(1, int(round(radius_pct * bh)))

    # Apply guided filter
    filtered = _guided_filter_gray(guide, seeded, radius, float(eps))

    # Clamp to valid range
    result = np.clip(filtered, 0.0, 1.0).astype(np.float32)

    # Enforce trimap hard constraints (FG/BG are exact — no filter drift)
    result[trimap == _FG] = 1.0
    result[trimap == _BG] = 0.0

    return result


# ---------------------------------------------------------------------------
# Self-registration — runs at import time
# ---------------------------------------------------------------------------

register_solver("guided", _guided_solve)
