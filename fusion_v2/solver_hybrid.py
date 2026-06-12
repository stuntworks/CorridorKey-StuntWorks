# Last modified: 2026-06-12 | Change: fix W map — k-means green components, mixed-bg blind spot
#
# WHAT IT DOES:
#   Hybrid matting solver.  Inside the unknown band ONLY:
#     alpha = W * nn_alpha + (1 - W) * vitmatte_alpha
#   W is a per-pixel green-backing confidence map [0, 1].
#
#   W CONSTRUCTION (k-means fix):
#     1. K-means (k=3, fixed seed, subsampled to 5000 px for speed) on the
#        full 3-channel LAB values of all definite-BG pixels.
#     2. Each centroid is tested: convert to BGR, check G > R AND G > B.
#        Components that pass are 'screen-colored'; others are discarded.
#        This handles mixed backgrounds (green screen + dark set walls, lit + shadow
#        green regions, etc.) — the single-Gaussian approach was blind to this.
#     3. W per band pixel = max over all green components of the Mahalanobis
#        likelihood: exp(-d_mahal² / scale) against that component's (a*, b*) stats.
#     4. Junk components (walls, floors, dark set) contribute nothing to W.
#     5. If no component is green (fully off-green shot), W=0 everywhere;
#        ViTMatte rules and a warning is emitted.
#
#   BG CHROMA PROPAGATION: cv2.inpaint(INPAINT_TELEA) fills the unknown band
#   from the surrounding definite-BG ring pixels.  Each unknown-band pixel gets
#   an estimate of its nearest background color.
#
#   FEET ZONE: bottom 12% of bbox forced W=0 (ViTMatte unconditionally).
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

_KMEANS_K             = 3      # BG colour components
_MAX_BG_SAMPLES       = 5000   # subsample cap for k-means speed
_MIN_COMPONENT_PIXELS = 20     # min pixels per cluster to fit covariance
_FALLOFF_SCALE        = 2.0    # W = exp(-d_sq / scale): ~0.6 at 1σ, ~0.01 at 3σ
_INPAINT_RADIUS       = 5


# ---------------------------------------------------------------------------
# Green-component detection
# ---------------------------------------------------------------------------

def _is_green_centroid(centroid_lab_u8_float: np.ndarray) -> bool:
    """
    True if this LAB centroid (uint8 scale: L∈[0,255], a*∈[0,255], b*∈[0,255])
    represents a green-dominant colour.

    Rule (derived from physical colour): convert centroid to BGR via
    cv2.COLOR_LAB2BGR; accept if G > R AND G > B.  Handles both lit and
    shadowed green screen regions that have different luminance.
    """
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
    """
    K-means (k=3) on definite-BG LAB pixels; returns per-green-component stats.

    Subsamples to max_samples pixels with a fixed seed for determinism.
    For each cluster whose centroid is green (G > R AND G > B), fits a
    2D Gaussian in (a*, b*) space.

    Returns list of (mu_ab, cov_inv_ab) as float32.  Empty list if no green
    components are found (off-green shot).
    """
    lab_all = frame_lab_u8[bg_mask].astype(np.float32)   # (N, 3)
    N = len(lab_all)
    if N < k * _MIN_COMPONENT_PIXELS:
        return []

    # Fixed-seed subsample
    if N > max_samples:
        rng = np.random.default_rng(seed)
        idx = rng.choice(N, size=max_samples, replace=False)
        lab_sample = lab_all[idx]
    else:
        lab_sample = lab_all

    # K-means on 3-channel LAB
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1.0)
    _, labels_raw, centroids = cv2.kmeans(
        lab_sample.astype(np.float32), k, None, criteria,
        attempts=5, flags=cv2.KMEANS_PP_CENTERS,
    )
    labels = labels_raw.flatten().astype(np.int32)   # (n_sample,)
    # centroids: (k, 3) float32, uint8 LAB scale

    green_stats = []
    for i in range(k):
        if not _is_green_centroid(centroids[i]):
            continue  # non-green component (wall, floor, etc.) — skip

        component_ab = lab_sample[labels == i, 1:3]   # a* and b* of cluster i
        if len(component_ab) < _MIN_COMPONENT_PIXELS:
            continue

        mu  = component_ab.mean(axis=0)                                      # (2,)
        cov = np.cov(component_ab.T) + _EPSILON * np.eye(2, dtype=np.float32)

        if np.ndim(cov) != 2 or cov.shape != (2, 2):
            continue   # degenerate (shouldn't happen after min-size guard)

        try:
            cov_inv = np.linalg.inv(cov).astype(np.float32)
        except np.linalg.LinAlgError:
            continue

        green_stats.append((mu.astype(np.float32), cov_inv))

    return green_stats


# ---------------------------------------------------------------------------
# Background chroma inpainting
# ---------------------------------------------------------------------------

def _inpaint_bg_chroma(
    frame_lab_u8: np.ndarray,
    trimap: np.ndarray,
) -> np.ndarray:
    """
    Estimate local background chroma (a*, b*) at each unknown-band pixel by
    inpainting the surrounding definite-BG ring colors inward via INPAINT_TELEA.

    Crops to non-BG bounding box for efficiency.
    Returns float32 (H, W, 2); meaningful only where trimap == _UNKNOWN.
    """
    H, W = trimap.shape
    ab = frame_lab_u8[:, :, 1:3]   # uint8 a*, b*
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
# Green confidence map
# ---------------------------------------------------------------------------

def _build_green_confidence_map(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    feet_zone_pct: float,
) -> np.ndarray:
    """
    Per-pixel green-backing confidence W [0, 1], non-zero only inside unknown band.

    K-means separates BG into k=3 components; green components are those whose
    centroid satisfies G > R AND G > B in BGR space.
    W = max Mahalanobis likelihood over all green components.
    Feet zone (bottom feet_zone_pct of bbox) forced to W=0.
    """
    H, W = trimap.shape
    frame_lab = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2LAB)
    bg_mask = (trimap == _BG)

    green_stats = _fit_green_components(frame_lab, bg_mask)
    if not green_stats:
        warnings.warn(
            "solver_hybrid: no green components found in definite-BG. "
            "W=0 everywhere — off-green shot or trimap has no BG pixels.",
            stacklevel=3,
        )
        return np.zeros((H, W), dtype=np.float32)

    ab_bg_est = _inpaint_bg_chroma(frame_lab, trimap)   # float32 (H, W, 2)
    flat_ab   = ab_bg_est.reshape(-1, 2)                # (H*W, 2)

    # W = max over green components of Mahalanobis likelihood
    W_map = np.zeros(H * W, dtype=np.float32)
    for mu, cov_inv in green_stats:
        diff  = flat_ab - mu[np.newaxis, :]             # (H*W, 2)
        d_sq  = np.sum((diff @ cov_inv) * diff, axis=1) # (H*W,)
        w_comp = np.exp(-np.clip(d_sq, 0.0, None) / _FALLOFF_SCALE)
        W_map  = np.maximum(W_map, w_comp)

    W_map = W_map.reshape(H, W).astype(np.float32)
    W_map[trimap != _UNKNOWN] = 0.0

    # Feet zone: W = 0, ViTMatte unconditionally (same bbox definition as Phase 1)
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
    feet_zone_pct: float = 0.12,
    **kwargs,
) -> np.ndarray:
    """
    Hybrid band combiner.

    Inside the unknown band:
        alpha = W * nn_alpha + (1 - W) * vitmatte_alpha

    W = green-backing confidence (k-means, per-shot, LAB chroma).
    Feet zone: W=0 unconditionally (ViTMatte only).
    FG/BG: passthrough exact 1.0/0.0.
    Falls back to 'guided' with a warning if 'vitmatte' is not registered.
    """
    from fusion_v2.solver_interface import available_solvers, solve_matte

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

    W_map    = _build_green_confidence_map(frame_rgb, trimap, feet_zone_pct)
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
