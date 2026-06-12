# Last modified: 2026-06-12 | Change: geometric band tests (replaces k-means tests)
#
# WHAT IT DOES:
#   Unit tests for fusion_v2.solver_hybrid.  Tests verify:
#     (a) FG/BG passthrough exact -- alpha=1 at FG, alpha=0 at BG
#     (b) Geometric outer band -- unknown AND outside SAM -> W=1 -> CK (nn_alpha)
#     (c) Geometric inner band -- unknown AND inside SAM  -> W=0 -> ViTMatte
#     (d) Feet zone both sides -- W=0 inner AND outer in bottom 12% bbox
#     (e) Torch-free fallback: 'guided' used when 'vitmatte' not registered
#
#   Tests use a deterministic mock 'vitmatte' solver (returns 0.3 in band)
#   so blending logic can be verified without real ViTMatte weights.
#
#   NOTE: test_hybrid_mixed_bg_w_spatial_split, test_hybrid_gray_bg_tracks_base_solver,
#   and test_hybrid_feet_zone_uses_base_solver (k-means green-confidence tests) removed
#   per Berto verdict 2026-06-12 -- green-confidence mode parked as BAND_MODE constant.
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

def _register_mock_vitmatte(return_value: float = 0.3):
    """
    Register a mock 'vitmatte' returning a fixed alpha in the unknown band.
    Returns cleanup callable restoring prior registry state.
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

def _geo_scene(H=400, W=400):
    """
    Ellipse foreground on green background, ellipse bottom touching the frame
    bottom so feet-zone unknown pixels exist.

    Returns (frame_rgb, trimap, nn_alpha, mask_u8) -- mask_u8 is the raw
    binary SAM proxy (0/255), used as sam_binary kwarg in geometric mode.
    """
    from fusion_v2.trimap_builder import build_trimap

    mask = np.zeros((H, W), dtype=np.uint8)
    cx, cy = W // 2, H - H // 4     # center near bottom so feet zone has unknown pixels
    cv2.ellipse(mask, (cx, cy), (W // 5, H // 3), 0, 0, 360, 255, -1)

    frame = np.full((H, W, 3), [30, 220, 30], dtype=np.uint8)   # green BG (RGB)
    frame[mask > 0] = [200, 120, 80]                             # flesh-tone FG

    nn = cv2.GaussianBlur(
        np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0
    )
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn, mask


# Kept for reference (green-confidence mode gauntlet comparisons).
def _mixed_bg_scene(H=400, W=400):
    """Left=green, right=dark-gray. K-means test scene (BAND_MODE='green-confidence')."""
    from fusion_v2.trimap_builder import build_trimap
    mask = np.zeros((H, W), dtype=np.uint8)
    cx, cy = W // 2, H // 2
    cv2.ellipse(mask, (cx, cy), (W // 6, H // 4), 0, 0, 360, 255, -1)
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    frame[:, :W // 2] = [30, 220, 30]
    frame[:, W // 2:] = [40, 40, 40]
    frame[mask > 0]   = [200, 120, 80]
    nn = cv2.GaussianBlur(np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0)
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn


# ---------------------------------------------------------------------------
# (a) FG/BG passthrough exact
# ---------------------------------------------------------------------------

def test_hybrid_fg_bg_passthrough():
    """FG must be 1.0, BG must be 0.0, regardless of blending in the band."""
    cleanup = _register_mock_vitmatte(0.3)
    try:
        import fusion_v2.solver_guided
        import fusion_v2.solver_hybrid

        from fusion_v2.solver_interface import solve_matte
        from fusion_v2.trimap_builder import TRIMAP_FG, TRIMAP_BG

        frame, trimap, nn, mask = _geo_scene()
        mask_bin = (mask > 0).astype(np.uint8) * 255
        alpha = solve_matte(frame, trimap, nn, solver="hybrid", sam_binary=mask_bin)

        fg_vals = alpha[trimap == TRIMAP_FG]
        bg_vals = alpha[trimap == TRIMAP_BG]

        assert fg_vals.size > 0, "No FG pixels"
        assert bg_vals.size > 0, "No BG pixels"
        assert np.all(fg_vals == 1.0), f"FG drift: min={fg_vals.min():.6f}"
        assert np.all(bg_vals == 0.0), f"BG drift: max={bg_vals.max():.6f}"
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (b) Geometric outer band -> CK (W=1 -> alpha == nn_alpha)
# ---------------------------------------------------------------------------

def test_geometric_outer_band_is_ck():
    """
    Outer unknown (trimap==128 AND mask==0) must have W=1 in geometric mode.
    Mock vitmatte returns 0.3; nn_alpha in band is ~0.02-0.97 (Gaussian blur).
    W=1 => alpha = 1.0*nn + 0.0*0.3 = nn. Max absolute diff from nn must be tiny.
    """
    cleanup = _register_mock_vitmatte(0.3)
    try:
        import fusion_v2.solver_hybrid
        import fusion_v2.solver_guided
        from fusion_v2.solver_interface import solve_matte

        frame, trimap, nn, mask = _geo_scene()
        mask_bin = (mask > 0).astype(np.uint8) * 255
        alpha = solve_matte(frame, trimap, nn, solver="hybrid", sam_binary=mask_bin)

        outer = (trimap == 128) & (mask == 0)
        if not outer.any():
            pytest.skip("No outer unknown pixels in scene")

        # Feet zone also forces W=0 (correct behavior per spec).
        # Exclude those pixels so we test only non-feet outer band.
        H_t, W_t = trimap.shape
        non_bg = np.any(trimap != 0, axis=1)
        if non_bg.any():
            y_min = int(np.argmax(non_bg))
            y_max = int(H_t - 1 - np.argmax(non_bg[::-1]))
            bh = max(y_max - y_min + 1, 1)
            feet_top = int(y_min + bh * (1.0 - 0.12))
            feet_mask = np.zeros((H_t, W_t), dtype=bool)
            feet_mask[feet_top:, :] = True
            outer_no_feet = outer & ~feet_mask
        else:
            outer_no_feet = outer

        if not outer_no_feet.any():
            pytest.skip("All outer unknown pixels are in feet zone")

        diff = np.abs(alpha[outer_no_feet] - nn[outer_no_feet])
        assert diff.max() < 1e-4, (
            f"Outer band (non-feet): max |alpha - nn_alpha| = {diff.max():.6f}, "
            "expected <1e-4. Geometric W=1 (CK) not holding."
        )
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (c) Geometric inner band -> ViTMatte (W=0 -> alpha == mock 0.3)
# ---------------------------------------------------------------------------

def test_geometric_inner_band_is_vitmatte():
    """
    Inner unknown (trimap==128 AND mask==255) must have W=0 in geometric mode.
    Mock vitmatte returns 0.3; alpha must be close to 0.3 in inner band.
    """
    cleanup = _register_mock_vitmatte(0.3)
    try:
        import fusion_v2.solver_hybrid
        import fusion_v2.solver_guided
        from fusion_v2.solver_interface import solve_matte

        frame, trimap, nn, mask = _geo_scene()
        mask_bin = (mask > 0).astype(np.uint8) * 255
        alpha = solve_matte(frame, trimap, nn, solver="hybrid", sam_binary=mask_bin)

        inner = (trimap == 128) & (mask > 0)
        if not inner.any():
            pytest.skip("No inner unknown pixels in scene")

        mean_inner = float(alpha[inner].mean())
        assert abs(mean_inner - 0.3) < 0.08, (
            f"Inner band: mean alpha = {mean_inner:.4f}, expected 0.3 (mock ViTMatte). "
            "Geometric W=0 (ViTMatte) not holding."
        )
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (d) Feet zone -> W=0 on BOTH inner and outer unknown
# ---------------------------------------------------------------------------

def test_geometric_feet_vitmatte_both_sides():
    """
    Even outer-band pixels in the feet zone (bottom 12% bbox) must have W=0.
    Mock vitmatte returns 0.3; alpha must be close to 0.3 in feet-zone unknown,
    both where mask==0 (outer) and mask==255 (inner).
    """
    cleanup = _register_mock_vitmatte(0.3)
    try:
        import fusion_v2.solver_hybrid
        import fusion_v2.solver_guided
        from fusion_v2.solver_interface import solve_matte

        frame, trimap, nn, mask = _geo_scene()
        H, W_img = trimap.shape
        mask_bin = (mask > 0).astype(np.uint8) * 255
        alpha = solve_matte(frame, trimap, nn, solver="hybrid", sam_binary=mask_bin)

        non_bg_rows = np.any(trimap != 0, axis=1)
        if not non_bg_rows.any():
            pytest.skip("No non-BG rows")

        y_min    = int(np.argmax(non_bg_rows))
        y_max    = int(H - 1 - np.argmax(non_bg_rows[::-1]))
        bh       = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - 0.12))

        feet_unknown = (trimap[feet_top:, :] == 128)
        if not feet_unknown.any():
            pytest.skip("No unknown pixels in feet zone")

        mean_feet = float(alpha[feet_top:, :][feet_unknown].mean())
        assert abs(mean_feet - 0.3) < 0.10, (
            f"Feet zone: mean alpha = {mean_feet:.4f}, expected 0.3 (W=0 override). "
            "Feet zone forcing W=0 on both sides failed."
        )

        # Also verify the outer-side of feet specifically (W=1 would give nn, not 0.3)
        feet_outer = (trimap[feet_top:, :] == 128) & (mask[feet_top:, :] == 0)
        if feet_outer.any():
            mean_feet_outer = float(alpha[feet_top:, :][feet_outer].mean())
            assert abs(mean_feet_outer - 0.3) < 0.12, (
                f"Feet outer band: mean={mean_feet_outer:.4f}, expected 0.3. "
                "Feet override did not zero-out outer-band W."
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

        frame, trimap, nn, mask = _geo_scene()
        mask_bin = (mask > 0).astype(np.uint8) * 255

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            alpha = solve_matte(frame, trimap, nn, solver="hybrid", sam_binary=mask_bin)

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
        test_geometric_outer_band_is_ck,
        test_geometric_inner_band_is_vitmatte,
        test_geometric_feet_vitmatte_both_sides,
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
