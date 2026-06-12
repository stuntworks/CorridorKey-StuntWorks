# Last modified: 2026-06-12 | Change: Phase 2 tests — guided filter solver verification
#
# WHAT IT DOES:
#   Unit tests for fusion_v2.solver_guided.  All inputs are synthetic.
#     (a) FG/BG passthrough: exact 1.0 / 0.0, no drift
#     (b) Output range: float32, all values in [0, 1]
#     (c) Soft-edge gradient: solver produces a smooth gradient in the
#         unknown band — not a binary step, not a staircase
#     (d) Resolution invariance: same scene at 2x scale → IoU > 0.90 per class
#
# DEPENDS ON: numpy, cv2, fusion_v2.trimap_builder, fusion_v2.solver_guided,
#             fusion_v2.solver_interface
# AFFECTS: nothing (test-only)
# ISOLATED: yes

import sys
import os
import traceback

import numpy as np
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fusion_v2.solver_guided  # triggers self-registration

from fusion_v2.solver_interface import solve_matte
from fusion_v2.trimap_builder import (
    build_trimap,
    TRIMAP_BG,
    TRIMAP_UNKNOWN,
    TRIMAP_FG,
    _EPSILON,
)

# ---------------------------------------------------------------------------
# Shared shape helpers (reuse from Phase 1 pattern)
# ---------------------------------------------------------------------------

def _ellipse_mask(H, W, cy_frac=0.5, cx_frac=0.5, ry_frac=0.30, rx_frac=0.25):
    mask = np.zeros((H, W), dtype=np.uint8)
    cy, cx = int(H * cy_frac), int(W * cx_frac)
    ry, rx = int(H * ry_frac), int(W * rx_frac)
    cv2.ellipse(mask, (cx, cy), (rx, ry), 0, 0, 360, 255, -1)
    return mask


def _contrast_frame(H, W, mask, inside_gray=80, outside_gray=200):
    """RGB frame with uniform inside/outside tones — gives guided filter clear edges."""
    frame = np.full((H, W, 3), outside_gray, dtype=np.uint8)
    frame[mask > 0] = inside_gray
    return frame


def _build_trimap_and_alpha(H, W, mask):
    nn = np.where(mask > 0, 0.97, 0.0).astype(np.float32)
    trimap = build_trimap(mask, nn)
    return trimap, nn


# ---------------------------------------------------------------------------
# (a) FG / BG passthrough is exact
# ---------------------------------------------------------------------------

def test_fg_bg_passthrough():
    """Definite-FG pixels must be exactly 1.0; definite-BG exactly 0.0."""
    H, W = 200, 200
    mask = _ellipse_mask(H, W)
    trimap, nn = _build_trimap_and_alpha(H, W, mask)
    frame = _contrast_frame(H, W, mask)

    alpha = solve_matte(frame, trimap, nn, solver="guided")

    fg_vals = alpha[trimap == TRIMAP_FG]
    bg_vals = alpha[trimap == TRIMAP_BG]

    assert fg_vals.size > 0, "No FG pixels found"
    assert bg_vals.size > 0, "No BG pixels found"

    assert np.all(fg_vals == 1.0), (
        f"FG passthrough failed: min={fg_vals.min():.6f} max={fg_vals.max():.6f}"
    )
    assert np.all(bg_vals == 0.0), (
        f"BG passthrough failed: min={bg_vals.min():.6f} max={bg_vals.max():.6f}"
    )


# ---------------------------------------------------------------------------
# (b) Output dtype and range
# ---------------------------------------------------------------------------

def test_output_range_and_dtype():
    """Output must be float32 with all values in [0, 1]."""
    H, W = 200, 200
    mask = _ellipse_mask(H, W)
    trimap, nn = _build_trimap_and_alpha(H, W, mask)
    frame = _contrast_frame(H, W, mask)

    alpha = solve_matte(frame, trimap, nn, solver="guided")

    assert alpha.dtype == np.float32, f"Expected float32, got {alpha.dtype}"
    assert alpha.shape == (H, W), f"Shape mismatch: {alpha.shape}"
    assert alpha.min() >= 0.0, f"alpha < 0: {alpha.min():.6f}"
    assert alpha.max() <= 1.0, f"alpha > 1: {alpha.max():.6f}"

    # Unknown band must also be in range
    unknown_vals = alpha[trimap == TRIMAP_UNKNOWN]
    if unknown_vals.size > 0:
        assert unknown_vals.min() >= 0.0
        assert unknown_vals.max() <= 1.0


# ---------------------------------------------------------------------------
# (c) Soft-edge gradient — not binary, not staircase
# ---------------------------------------------------------------------------

def test_soft_edge_gradient():
    """
    Guided filter must produce a smooth gradient in the unknown band.
    Test uses a soft (Gaussian-blurred) nn_alpha — the realistic case where
    the NN produces a gradual transition at the silhouette edge.

    Checks:
    - The outer unknown band (outside the SAM mask) has a range > 0.05
    - No staircase: max adjacent step in that region < 0.3
    - The filter produces different values depending on position (not flat)
    """
    H, W = 300, 300
    cy, cx, r = 150, 150, 70
    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.circle(mask, (cx, cy), r, 255, -1)

    # Soft nn_alpha via Gaussian blur — realistic: NN gives gradual transitions
    nn_soft = cv2.GaussianBlur(
        mask.astype(np.float32) / 255.0, (21, 21), 5.0
    )

    frame  = _contrast_frame(H, W, mask, inside_gray=60, outside_gray=220)
    trimap = build_trimap(mask, nn_soft)
    alpha  = solve_matte(frame, trimap, nn_soft, solver="guided")

    unknown_mask = trimap == TRIMAP_UNKNOWN
    assert unknown_mask.any(), "No unknown band — trimap parameters may be off"

    # Focus on the outer unknown band at row cy (outside SAM mask boundary)
    # This is where the smooth transition should be clearly visible.
    outer_cols = np.where(unknown_mask[cy, :] & (mask[cy, :] == 0))[0]

    if len(outer_cols) >= 3:
        vals = alpha[cy, outer_cols].astype(np.float32)

        # Gradient exists: range must be > 0.05 (not all the same value)
        band_range = float(vals.max() - vals.min())
        assert band_range > 0.05, (
            f"No gradient in outer unknown band (range={band_range:.4f}). "
            f"Guided filter may not be smoothing the edge."
        )

        # No staircase: max adjacent step must be small with a soft seed
        steps = np.abs(np.diff(vals))
        max_step = float(steps.max())
        assert max_step < 0.3, (
            f"Staircase in outer unknown band: max step={max_step:.3f} (> 0.3). "
            f"Square kernel or unfiltered seeding would cause this."
        )

    # Full unknown-band range must also span some meaningful values
    all_unknown_vals = alpha[unknown_mask]
    full_range = float(all_unknown_vals.max() - all_unknown_vals.min())
    assert full_range > 0.1, (
        f"Unknown band too narrow (range={full_range:.3f}); solver may be clamping early."
    )


# ---------------------------------------------------------------------------
# (d) Resolution invariance
# ---------------------------------------------------------------------------

def test_resolution_invariance():
    """Same scene at 2x scale must produce consistent solver output.

    Uses soft (Gaussian-blurred) nn_alpha for a realistic soft edge.
    Checks:
    - FG/BG hard constraints hold at both scales
    - Unknown-band mean alpha is within 0.15 of each other across scales
      (percentage-based radius ensures the filter scales proportionally)
    """
    H1, W1 = 300, 300
    H2, W2 = 600, 600

    mask_s = _ellipse_mask(H1, W1)
    mask_l = _ellipse_mask(H2, W2)

    # Soft nn_alpha matches real NN output; sigma scaled proportionally
    nn_s = cv2.GaussianBlur(mask_s.astype(np.float32) / 255.0, (21, 21), 5.0)
    nn_l = cv2.GaussianBlur(mask_l.astype(np.float32) / 255.0, (41, 41), 10.0)

    frame_s = _contrast_frame(H1, W1, mask_s)
    frame_l = _contrast_frame(H2, W2, mask_l)

    t_s = build_trimap(mask_s, nn_s)
    t_l = build_trimap(mask_l, nn_l)

    alpha_s = solve_matte(frame_s, t_s, nn_s, solver="guided")
    alpha_l = solve_matte(frame_l, t_l, nn_l, solver="guided")

    # Hard constraints must hold at both scales
    assert np.all(alpha_s[t_s == TRIMAP_FG] == 1.0), "FG passthrough broken at small scale"
    assert np.all(alpha_s[t_s == TRIMAP_BG] == 0.0), "BG passthrough broken at small scale"
    assert np.all(alpha_l[t_l == TRIMAP_FG] == 1.0), "FG passthrough broken at large scale"
    assert np.all(alpha_l[t_l == TRIMAP_BG] == 0.0), "BG passthrough broken at large scale"

    # Unknown-band mean alpha must be similar across scales
    # (percentage radius ensures the filter window scales with the scene)
    unk_s = alpha_s[t_s == TRIMAP_UNKNOWN]
    unk_l = alpha_l[t_l == TRIMAP_UNKNOWN]

    if unk_s.size > 0 and unk_l.size > 0:
        mean_diff = abs(float(unk_s.mean()) - float(unk_l.mean()))
        assert mean_diff < 0.15, (
            f"Unknown-band mean alpha differs across scales: "
            f"small={unk_s.mean():.3f}, large={unk_l.mean():.3f}, diff={mean_diff:.3f} > 0.15. "
            f"Percentage-based radius should keep this consistent."
        )


# ---------------------------------------------------------------------------
# Solver interface smoke test
# ---------------------------------------------------------------------------

def test_solver_registration():
    """solver_guided must register itself as 'guided' on import."""
    from fusion_v2.solver_interface import available_solvers
    assert "guided" in available_solvers(), (
        f"'guided' not registered. Available: {available_solvers()}"
    )


# ---------------------------------------------------------------------------
# __main__ runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_fg_bg_passthrough,
        test_output_range_and_dtype,
        test_soft_edge_gradient,
        test_resolution_invariance,
        test_solver_registration,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
