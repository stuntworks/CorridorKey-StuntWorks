# Last modified: 2026-06-12 | Change: ck-green band tests (Berto law v2)
#
# WHAT IT DOES:
#   Unit tests for fusion_v2.solver_hybrid.  Tests verify BAND_MODE='ck-green':
#     (a) interior unknown -> W=1 always, even gray BG (fence rule / butt-intact)
#     (b) outer unknown over green BG -> W high -> CK
#     (c) outer unknown over junk/gray BG -> W low -> ViTMatte
#     (d) feet zone -> W=0 inner and outer
#     (e) FG/BG passthrough exact
#     (f) Torch-free fallback: 'guided' used when 'vitmatte' not registered
#
#   Tests (a), (b), (c) call _build_ck_green_band_map directly with feather=0
#   so W map values can be asserted exactly or with tight tolerance.
#   Tests (d), (e), (f) call solve_matte for the full blend path.
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

def _green_scene(H=300, W=300):
    """Ellipse foreground on pure green BG.  Returns (frame, trimap, nn, mask)."""
    from fusion_v2.trimap_builder import build_trimap
    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.ellipse(mask, (W // 2, H // 2), (W // 5, H // 4), 0, 0, 360, 255, -1)
    frame = np.full((H, W, 3), [30, 220, 30], dtype=np.uint8)   # pure green BG (RGB)
    frame[mask > 0] = [200, 120, 80]
    nn = cv2.GaussianBlur(np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0)
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn, mask


def _gray_scene(H=300, W=300):
    """Ellipse foreground on neutral gray BG (no green).  Returns (frame, trimap, nn, mask)."""
    from fusion_v2.trimap_builder import build_trimap
    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.ellipse(mask, (W // 2, H // 2), (W // 5, H // 4), 0, 0, 360, 255, -1)
    frame = np.full((H, W, 3), [128, 128, 128], dtype=np.uint8)  # neutral gray (RGB)
    frame[mask > 0] = [200, 120, 80]
    nn = cv2.GaussianBlur(np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0)
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn, mask


def _feet_scene(H=300, W=300):
    """Rectangle touching the bottom of the frame on green BG.  Returns (frame, trimap, nn, mask)."""
    from fusion_v2.trimap_builder import build_trimap
    mask = np.zeros((H, W), dtype=np.uint8)
    mask[50:, 100:200] = 255
    frame = np.full((H, W, 3), [30, 220, 30], dtype=np.uint8)
    frame[mask > 0] = [200, 120, 80]
    nn = cv2.GaussianBlur(np.where(mask > 0, 0.97, 0.02).astype(np.float32), (15, 15), 4.0)
    nn = np.clip(nn, 0.0, 1.0)
    trimap = build_trimap(mask, nn)
    return frame, trimap, nn, mask


# Kept for reference (green-confidence / geometric parked test reference).
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
# (a) Interior unknown -> W=1 always, even when BG is not green (fence rule)
# ---------------------------------------------------------------------------

def test_ck_green_interior_is_ck_always():
    """
    RULE 1 (ck-green): interior unknown (trimap==128 AND inside SAM) -> W=1 unconditionally.
    Test uses GRAY background (no green) so the outer-band pixel test would give W=0;
    yet interior pixels must still be W=1 (fence rule -- CK already keyed the body).
    Calls _build_ck_green_band_map directly with feather=0 for exact assertion.
    """
    import fusion_v2.solver_hybrid
    from fusion_v2.solver_hybrid import _build_ck_green_band_map

    # _build_ck_green_band_map is parked (BAND_MODE='ck-only' since overseer 2026-06-12)
    # but function logic must stay correct for gauntlet re-evaluation.  Test it directly.

    frame, trimap, nn, mask = _gray_scene()
    mask_bin = (mask > 0).astype(np.uint8) * 255

    W_map = _build_ck_green_band_map(trimap, mask_bin, frame, feet_zone_pct=0.12,
                                      feather_sigma_pct=0.0)

    interior = (trimap == 128) & (mask > 0)
    if not interior.any():
        pytest.skip("No interior unknown pixels in scene")

    # Exclude feet zone -- feet override forces W=0 even on interior (by design / Berto RULE 3).
    # We assert only non-feet interior pixels here to isolate RULE 1.
    H_t, W_t = trimap.shape
    non_bg = np.any(trimap != 0, axis=1)
    if non_bg.any():
        y_min = int(np.argmax(non_bg))
        y_max = int(H_t - 1 - np.argmax(non_bg[::-1]))
        bh = max(y_max - y_min + 1, 1)
        feet_top = int(y_min + bh * (1.0 - 0.12))
        feet_mask = np.zeros((H_t, W_t), dtype=bool)
        feet_mask[feet_top:, :] = True
        interior_no_feet = interior & ~feet_mask
    else:
        interior_no_feet = interior

    if not interior_no_feet.any():
        pytest.skip("All interior unknown pixels are in the feet zone")

    W_int = W_map[interior_no_feet]
    assert np.all(W_int == 1.0), (
        f"Non-feet interior W must be 1.0 everywhere (fence rule). "
        f"min={W_int.min():.6f}  max={W_int.max():.6f}  "
        f"n_bad={(W_int != 1.0).sum()}"
    )


# ---------------------------------------------------------------------------
# (b) Outer unknown over green BG -> W high -> CK
# ---------------------------------------------------------------------------

def test_ck_green_outer_green_backed_is_ck():
    """
    RULE 2 (ck-green): outer unknown pixels (trimap==128 AND outside SAM) over pure green
    BG must have W close to 1.0 so CK alpha carries through.
    Calls _build_ck_green_band_map directly with feather=0.
    """
    import fusion_v2.solver_hybrid
    from fusion_v2.solver_hybrid import _build_ck_green_band_map

    frame, trimap, nn, mask = _green_scene()
    mask_bin = (mask > 0).astype(np.uint8) * 255

    W_map = _build_ck_green_band_map(trimap, mask_bin, frame, feet_zone_pct=0.12,
                                      feather_sigma_pct=0.0)

    outer = (trimap == 128) & (mask == 0)
    if not outer.any():
        pytest.skip("No outer unknown pixels in scene")

    W_outer = W_map[outer]
    mean_W = float(W_outer.mean())
    assert mean_W > 0.7, (
        f"Outer band over green BG: mean W={mean_W:.4f}, expected >0.7 (CK rules). "
        "K-means green detection failed on saturated green BG."
    )


# ---------------------------------------------------------------------------
# (c) Outer unknown over junk/gray BG -> W low -> ViTMatte
# ---------------------------------------------------------------------------

def test_ck_green_outer_junk_backed_is_vitmatte():
    """
    RULE 2 (ck-green): outer unknown pixels over neutral gray BG must have W close to 0.
    Gray pixels will not match any green LAB component -> W_comp ~ 0 everywhere.
    Calls _build_ck_green_band_map directly with feather=0.
    """
    import fusion_v2.solver_hybrid
    from fusion_v2.solver_hybrid import _build_ck_green_band_map

    frame, trimap, nn, mask = _gray_scene()
    mask_bin = (mask > 0).astype(np.uint8) * 255

    W_map = _build_ck_green_band_map(trimap, mask_bin, frame, feet_zone_pct=0.12,
                                      feather_sigma_pct=0.0)

    outer = (trimap == 128) & (mask == 0)
    if not outer.any():
        pytest.skip("No outer unknown pixels in scene")

    W_outer = W_map[outer]
    mean_W = float(W_outer.mean())
    # Gray BG: k-means finds no green component OR Mahalanobis distance is huge -> W~0
    # Allow slightly above 0 if k-means finds a spurious centroid
    assert mean_W < 0.25, (
        f"Outer band over gray BG: mean W={mean_W:.4f}, expected <0.25 (ViTMatte). "
        "Gray BG should not pass green-component test."
    )


# ---------------------------------------------------------------------------
# (d) Feet zone -> W=0 on BOTH interior and outer unknown
# ---------------------------------------------------------------------------

def test_ck_green_feet_vitmatte_both_sides():
    """
    RULE 3 (ck-green): feet zone (bottom 12% bbox) forces W=0 for all unknown pixels,
    both interior (where RULE 1 would give W=1) and outer green (where RULE 2 gives W~1).
    """
    cleanup = _register_mock_vitmatte(0.3)
    try:
        import fusion_v2.solver_guided
        import fusion_v2.solver_hybrid
        from fusion_v2.solver_interface import solve_matte

        frame, trimap, nn, mask = _feet_scene()
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
        assert abs(mean_feet - 0.3) < 0.12, (
            f"Feet zone: mean alpha={mean_feet:.4f}, expected ~0.3 (W=0 -> ViTMatte). "
            "Feet zone W=0 override not working."
        )
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# (e) FG/BG passthrough exact
# ---------------------------------------------------------------------------

def test_hybrid_fg_bg_passthrough():
    """FG must be 1.0, BG must be 0.0, regardless of ck-green blending."""
    cleanup = _register_mock_vitmatte(0.3)
    try:
        import fusion_v2.solver_guided
        import fusion_v2.solver_hybrid
        from fusion_v2.solver_interface import solve_matte
        from fusion_v2.trimap_builder import TRIMAP_FG, TRIMAP_BG

        frame, trimap, nn, mask = _green_scene()
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
# (f) Torch-free fallback: 'guided' when 'vitmatte' not registered
# ---------------------------------------------------------------------------

def test_hybrid_fallback_to_guided_torch_free():
    """
    When 'vitmatte' not in registry, hybrid must:
      - Issue a UserWarning mentioning 'guided'
      - Return valid float32 alpha with FG=1.0, BG=0.0
    """
    from fusion_v2.solver_interface import _REGISTRY

    import fusion_v2.solver_guided
    import fusion_v2.solver_hybrid

    prior = _REGISTRY.pop("vitmatte", None)
    try:
        from fusion_v2.solver_interface import solve_matte
        from fusion_v2.trimap_builder import TRIMAP_FG, TRIMAP_BG

        frame, trimap, nn, mask = _green_scene()
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
        assert 0.0 <= float(alpha.min()) and float(alpha.max()) <= 1.0

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
        test_ck_green_interior_is_ck_always,
        test_ck_green_outer_green_backed_is_ck,
        test_ck_green_outer_junk_backed_is_vitmatte,
        test_ck_green_feet_vitmatte_both_sides,
        test_hybrid_fg_bg_passthrough,
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
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)
