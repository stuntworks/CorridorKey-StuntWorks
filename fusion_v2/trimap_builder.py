# Last modified: 2026-06-12 | Change: Phase 1 — trimap construction module
#
# WHAT IT DOES:
#   Builds a uint8 trimap (0=definite-BG, 128=unknown, 255=definite-FG) from
#   a SAM2 binary silhouette and a soft NN alpha map.  All morphology amounts
#   are percentages of the silhouette bounding-box height — resolution-
#   independent by design.  Structuring elements are MORPH_ELLIPSE throughout;
#   square kernels staircase on diagonals (CK bug 2026-04-25).
#
#   Definite-BG rule (Amendment 1, 2026-06-12, 3-model consensus):
#     Outside the dilated SAM mask = 0, always, no NN alpha condition.
#     The old "outside dilated AND nn_alpha < low" formulation left bright
#     junk in the outer unknown ring (feet-ring bug reborn).
#
# DEPENDS ON: numpy, cv2 (OpenCV 4.x) — no torch, no CorridorKeyModule
# AFFECTS: nothing yet — standalone module, not wired into pipeline (Phase 2)
# ISOLATED: yes — safe to import in torch-free subprocesses

import numpy as np
import cv2

# Trimap output convention (uint8)
TRIMAP_BG      = np.uint8(0)    # definite background
TRIMAP_UNKNOWN = np.uint8(128)  # unknown band — solver resolves this
TRIMAP_FG      = np.uint8(255)  # definite foreground

_EPSILON = 1e-7


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_ellipse_kernel(radius: float) -> np.ndarray:
    """Return MORPH_ELLIPSE structuring element for the given float radius.

    Rounds to nearest integer, minimum r=1 (ksize=3).  Never returns a square
    kernel — MORPH_ELLIPSE is mandatory per CK history guardrails.
    """
    r = max(1, int(round(radius)))
    ksize = 2 * r + 1
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))


def _get_sam_bbox(sam_bin: np.ndarray):
    """Return (x, y, w, h) bounding rect of the nonzero region, or None if empty.

    sam_bin must be a 2-D uint8 array (0/nonzero).
    Returns OpenCV convention: x = left col, y = top row, w = width, h = height.
    """
    rows_any = np.any(sam_bin, axis=1)
    cols_any = np.any(sam_bin, axis=0)
    if not rows_any.any():
        return None
    y_min = int(np.argmax(rows_any))
    y_max = int(len(rows_any) - 1 - np.argmax(rows_any[::-1]))
    x_min = int(np.argmax(cols_any))
    x_max = int(len(cols_any) - 1 - np.argmax(cols_any[::-1]))
    return (x_min, y_min, x_max - x_min + 1, y_max - y_min + 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_trimap(
    sam_mask: np.ndarray,
    nn_alpha: np.ndarray,
    erode_pct: float = 0.03,
    dilate_pct: float = 0.06,
    feet_zone_pct: float = 0.12,
    nn_high: float = 0.95,
    nn_low: float = 0.05,
) -> np.ndarray:
    """Build a uint8 trimap from a SAM2 binary silhouette and a soft NN alpha map.

    Returns a 2-D uint8 array, same spatial shape as sam_mask:
        0   — definite background  (outside dilated SAM — always, no NN condition)
        128 — unknown band         (matting solver resolves this in Stage 2)
        255 — definite foreground  (inside eroded SAM AND nn_alpha > nn_high)

    Parameters
    ----------
    sam_mask      : 2-D array (H, W), any dtype — nonzero pixels = foreground
    nn_alpha      : 2-D float32 (H, W), values in [0, 1]
    erode_pct     : erode radius as fraction of bbox height (default 3%)
    dilate_pct    : dilate radius as fraction of bbox height (default 6%)
    feet_zone_pct : bottom fraction of bbox that receives half dilation (default 12%)
    nn_high       : NN alpha threshold for definite-FG classification (default 0.95)
    nn_low        : reserved for future Stage 2 matting hint; unused here
    """
    H, W = sam_mask.shape[:2]
    sam_bin = (sam_mask > 0).astype(np.uint8)
    nn_f32  = nn_alpha.astype(np.float32)

    bbox = _get_sam_bbox(sam_bin)
    if bbox is None:
        # No foreground at all — return all-BG trimap
        return np.zeros((H, W), dtype=np.uint8)

    _bx, by, _bw, bh = bbox
    bh = max(bh, 1)  # guard against degenerate 1-row silhouette

    erode_radius  = erode_pct  * bh
    dilate_radius = dilate_pct * bh

    eroded       = cv2.erode(sam_bin,  _make_ellipse_kernel(erode_radius))
    dilated_full = cv2.dilate(sam_bin, _make_ellipse_kernel(dilate_radius))
    dilated_half = cv2.dilate(sam_bin, _make_ellipse_kernel(dilate_radius * 0.5))

    # Feet zone: bottom feet_zone_pct of bbox rows get half-radius dilation,
    # producing a tighter unknown band near the floor (one zone only — spec rule).
    feet_top = int(by + bh * (1.0 - feet_zone_pct))
    dilated = dilated_full.copy()
    dilated[max(0, feet_top):, :] = dilated_half[max(0, feet_top):, :]

    # Build trimap — start as all-unknown, then carve BG and FG
    trimap = np.full((H, W), TRIMAP_UNKNOWN, dtype=np.uint8)

    # Amendment 1: outside dilated SAM is always definite BG, no NN condition
    trimap[dilated == 0] = TRIMAP_BG

    # Definite FG: inside eroded SAM AND nn_alpha above high threshold
    trimap[(eroded > 0) & (nn_f32 > nn_high)] = TRIMAP_FG

    return trimap


def build_trimap_sequence(
    sam_masks: list,
    nn_alphas: list,
    erode_pct: float = 0.03,
    dilate_pct: float = 0.06,
    feet_zone_pct: float = 0.12,
    nn_high: float = 0.95,
    nn_low: float = 0.05,
    temporal_smoother=None,
) -> list:
    """Build trimaps for a tracked-video sequence, one per frame.

    Each frame is processed independently.  Temporal smoothing of trimap
    boundaries (Phase 4) is injected via temporal_smoother.

    Parameters
    ----------
    sam_masks        : list of 2-D masks, one per frame (tracked SAM2 output)
    nn_alphas        : list of 2-D float32 alpha maps, one per frame
    temporal_smoother: callable(List[np.ndarray]) -> List[np.ndarray], or None.
                       Slot reserved for Phase 4 temporal smoothing.  Pass None
                       (default) for no smoothing.  Signature must be stable —
                       Phase 4 only fills this parameter, nothing else changes.

    Returns
    -------
    List of uint8 trimap arrays, same length and shapes as inputs.
    """
    trimaps = [
        build_trimap(mask, alpha, erode_pct, dilate_pct, feet_zone_pct, nn_high, nn_low)
        for mask, alpha in zip(sam_masks, nn_alphas)
    ]
    if temporal_smoother is not None:
        trimaps = temporal_smoother(trimaps)
    return trimaps
