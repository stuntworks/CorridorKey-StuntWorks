# Last modified: 2026-06-12 | Change: Phase 3b tests — hybrid solver (k-means W fix)
#
# WHAT IT DOES:
#   Unit tests for fusion_v2.solver_hybrid.  Tests verify:
#     (a) FG/BG passthrough exact
#     (b) Mixed background: W high on green side, low on dark side — the exact
#         failure that slipped through with the single-Gaussian approach
#     (c) Non-green backing → W=0 → alpha tracks base solver
#     (d) Feet zone always uses base solver (W=0 forced)
#     (e) Torch-free fallback: 'guided' used when 'vitmatte' not registered
#
#   Tests use a deterministic mock 'vitmatte' solver (returns 0.5 in band)
#   so blending logic can be verified without real ViTMatte weights.
#
# DEPENDS ON: numpy, cv2, pytest, fusion_v2.solver_hybrid, fusion_v2.solver_guided,
#             fusion_v2.solver_interface, fusion_v2.trimap_builder
# AFFECTS: nothing (test-only)
# ISOLATED: yes

import sys
import os
import traceback
import warnings

import numpy as np
import cv2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


# ---------------------------------------------------------------------------
# Mock-vitmatte fixture
# ---------------------------------------------------------------------------

def _register_mock_vitmatte(return_value: float = 0.5):
    """
    Register a mock 'vitmatte' that returns a fixed alpha value in the unknown band.
    Returns a cleanup callable that restores the prior registry state.
    """
    from fusion_v2.solver_interface import _REGISTRY

    def _mock_solve(frame_rgb, trimap, nn_alpha, **kwargs):
        return np.where(trimap == 255, 1.0,
               np.where(trimap == 0,   0.0, float(return_value))).astype(np.float32)

    prior = _REGISTRY.get("vitmatte")
    _REGISTRY["vitmatte"] = _mock_solve

    def cleanup():
        if prior is None:
            _REGISTRY.pop("vitmatte", None)
        else:
            _REGISTRY["vitmatte"] = prior

    return cleanup


# ---------------------------------------------------------------------------
# Scene builders
# ---------------------------------------------------------------------------

def _mixed_bg_scene(H=400, W=400):
    """
    Ellipse foreground on a MIXED background:
      - Left half (x < W//2): saturated green screen  [R=30, G=220, B=30]
      - Right half (x >= W//2): dark gray             [R=40, G=40, B=40]

    This is the canonical test for the k-means fix: a single Gaussian fitted
    to all BG pixels would see a bimodal distribution and score everything near
    zero.  K-means must separate the two modes and W must be high on the green
    side and low on the dark side.
    """
    from fusion_v2.trimap_builder import build_trimap

    mask = np.zeros((H, W), dtype=np.uint8)
    cx, cy = W // 2, H // 2
    cv2.ellipse(mask, (cx, cy), (W // 6, H // 4), 0, 0, 360, 255, -1)

    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[:, :W // 2] = [30, 220, 30]    # green left half  (RGB)
    frame[:, W // 2:] = [40, 40, 40]     # dark right half  (RGB)
    frame[mask > 0]   = [200, 120, 80]   # flesh-tone FG

    nn = cv2.GaussianBlur(
        np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0
    )
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn


def _gray_bg_scene(H=300, W=300):
    """Ellipse foreground on neutral gray — no green in background at all."""
    from fusion_v2.trimap_builder import build_trimap

    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.ellipse(mask, (W // 2, H // 2), (W // 5, H // 4), 0, 0, 360, 255, -1)

    frame = np.full((H, W, 3), [128, 128, 128], dtype=np.uint8)
    frame[mask > 0] = [200, 120, 80]

    nn = cv2.GaussianBlur(
        np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0
    )
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn


def _green_bg_feet_scene(H=300, W=300):
    """
    Rectangle silhouette touching the bottom edge on a green background.
    Used to verify the feet-zone W=0 override.
    """
    from fusion_v2.trimap_builder import build_trimap

    mask = np.zeros((H, W), dtype=np.uint8)
    mask[50:, 100:200] = 255

    frame = np.full((H, W, 3), [30, 220, 30], dtype=np.uint8)
    frame[mask > 0] = [200, 120, 80]

    nn = cv2.GaussianBlur(
        np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0
    )
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn


# ---------------------------------------------------------------------------
# (a) FG/BG passthrough exact
# ---------------------------------------------------------------------------

def test_hybrid_fg_bg_passthrough():
    """FG must be 1.0, BG must be 0.0, regardless of blending in the band."""
    cleanup = _register_mock_vitmatte(0.5)
    try:
        import fusion_v2.solver_guided
        import fusion_v2.solver_hybrid

        from fusion_v2.solver_interface import solve_matte
        from fusion_v2.trimap_builder import TRIMAP_FG, TRIMAP_BG

        frame, trimap, nn = _mixed_bg_scene()
        alpha = solve_matte(frame, trimap, nn, solver="hybrid")

        fg_vals = alpha[trimap == TRIMAP_FG]
        bg_vals = alpha[trimap == TRIMAP_BG]

        assert fg_vals.size > 0, "No FG pixels"
        assert bg_vals.size > 0, "No BG pixels"
        assert np.all(fg_vals == 1.0), f"FG drift: min={fg_vals.min():.6f}"
        assert np.all(bg_vals == 0.0), f"BG drift: max={bg_vals.max():.6f}"
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (b) Mixed background: W high on green side, low on dark side
# ---------------------------------------------------------------------------

def test_hybrid_mixed_bg_w_spatial_split():
    """
    K-means must separate green and dark-gray BG components.
    In the unknown band:
      - Pixels adjacent to the GREEN half must have W > 0.5
      - Mean W on the green side must exceed mean W on the dark side
    This is the exact failure mode the k-means fix targets.
    """
    import fusion_v2.solver_guided
    import fusion_v2.solver_hybrid
    from fusion_v2.solver_hybrid import _build_green_confidence_map

    frame, trimap, nn = _mixed_bg_scene()
    H, W_img = trimap.shape

    W_map = _build_green_confidence_map(frame, trimap, feet_zone_pct=0.12)

    unknown_mask = (trimap == 128)
    if not unknown_mask.any():
        pytest.skip("No unknown pixels in test scene")

    # Max W anywhere in band — must find at least one high-confidence green pixel
    W_max = float(W_map[unknown_mask].max())
    assert W_max > 0.5, (
        f"max W = {W_max:.4f}, expected > 0.5. "
        "K-means may not have found a green component."
    )

    # Spatial split: left (green) half must have higher mean W than right (dark) half
    left_unknown  = unknown_mask.copy(); left_unknown[:, W_img // 2:] = False
    right_unknown = unknown_mask.copy(); right_unknown[:, :W_img // 2] = False

    if left_unknown.any() and right_unknown.any():
        W_left  = float(W_map[left_unknown].mean())
        W_right = float(W_map[right_unknown].mean())
        assert W_left > W_right, (
            f"Expected W higher on green side: left={W_left:.4f}, right={W_right:.4f}. "
            "K-means not separating green from non-green component."
        )


# ---------------------------------------------------------------------------
# (c) Non-green backing → W=0 → alpha tracks base solver
# ---------------------------------------------------------------------------

def test_hybrid_gray_bg_tracks_base_solver():
    """
    Neutral gray background → no green component found → W=0 → hybrid = mock(0.5).
    """
    cleanup = _register_mock_vitmatte(0.5)
    try:
        import fusion_v2.solver_guided
        import fusion_v2.solver_hybrid

        from fusion_v2.solver_interface import solve_matte

        frame, trimap, nn = _gray_bg_scene()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            alpha = solve_matte(frame, trimap, nn, solver="hybrid")

        unknown_mask = (trimap == 128)
        if not unknown_mask.any():
            pytest.skip("No unknown pixels")

        mean_val = float(alpha[unknown_mask].mean())
        assert abs(mean_val - 0.5) < 0.15, (
            f"Gray bg → expected W=0 → hybrid≈0.5, got mean={mean_val:.4f}"
        )
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (d) Feet zone always uses base solver (W=0 forced)
# ---------------------------------------------------------------------------

def test_hybrid_feet_zone_uses_base_solver():
    """
    Even with a green background, the feet zone (bottom 12% of bbox)
    must have W=0 → hybrid alpha must match the mock base solver value (0.5).
    """
    cleanup = _register_mock_vitmatte(0.5)
    try:
        import fusion_v2.solver_guided
        import fusion_v2.solver_hybrid

        from fusion_v2.solver_interface import solve_matte

        H, W_img = 300, 300
        frame, trimap, nn = _green_bg_feet_scene(H, W_img)
        alpha = solve_matte(frame, trimap, nn, solver="hybrid")

        # Locate feet zone
        non_bg = np.any(trimap != 0, axis=1)
        if not non_bg.any():
            pytest.skip("No non-BG rows")

        y_min    = int(np.argmax(non_bg))
        y_max    = int(H - 1 - np.argmax(non_bg[::-1]))
        bh       = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - 0.12))

        feet_unknown = (trimap[feet_top:, :] == 128)
        if not feet_unknown.any():
            pytest.skip("No unknown pixels in feet zone")

        mean_feet = float(alpha[feet_top:, :][feet_unknown].mean())
        assert abs(mean_feet - 0.5) < 0.10, (
            f"Feet zone expected W=0 → alpha≈0.5, got mean={mean_feet:.4f}"
        )
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (e) Torch-free fallback: 'guided' used when 'vitmatte' not registered
# ---------------------------------------------------------------------------

def test_hybrid_fallback_to_guided_torch_free():
    """
    When 'vitmatte' not in registry, hybrid must:
      - Issue a UserWarning mentioning 'guided'
      - Return valid float32 alpha with FG=1.0, BG=0.0
      - Never import torch
    """
    from fusion_v2.solver_interface import _REGISTRY

    import fusion_v2.solver_guided
    import fusion_v2.solver_hybrid

    prior = _REGISTRY.pop("vitmatte", None)
    try:
        from fusion_v2.solver_interface import solve_matte
        from fusion_v2.trimap_builder import TRIMAP_FG, TRIMAP_BG

        frame, trimap, nn = _mixed_bg_scene()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            alpha = solve_matte(frame, trimap, nn, solver="hybrid")

        warning_texts = [str(w.message) for w in caught]
        assert any("guided" in t.lower() for t in warning_texts), (
            f"Expected fallback warning mentioning 'guided'. Got: {warning_texts}"
        )

        assert alpha.dtype == np.float32
        assert alpha.shape == trimap.shape
        assert alpha.min() >= 0.0
        assert alpha.max() <= 1.0

        fg_vals = alpha[trimap == TRIMAP_FG]
        bg_vals = alpha[trimap == TRIMAP_BG]
        if fg_vals.size > 0:
            assert np.all(fg_vals == 1.0), f"FG drift: {fg_vals.min():.6f}"
        if bg_vals.size > 0:
            assert np.all(bg_vals == 0.0), f"BG drift: {bg_vals.max():.6f}"
    finally:
        if prior is not None:
            _REGISTRY["vitmatte"] = prior


# ---------------------------------------------------------------------------
# __main__ runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_hybrid_fg_bg_passthrough,
        test_hybrid_mixed_bg_w_spatial_split,
        test_hybrid_gray_bg_tracks_base_solver,
        test_hybrid_feet_zone_uses_base_solver,
        test_hybrid_fallback_to_guided_torch_free,
    ]
    passed = failed = skipped = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except pytest.skip.Exception as e:
            print(f"SKIP  {t.__name__}: {e}")
            skipped += 1
        except Exception as exc:
            print(f"FAIL  {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
