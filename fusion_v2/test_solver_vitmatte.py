# Last modified: 2026-06-12 | Change: Phase 3 tests — ViTMatte solver (skip if absent)
#
# WHAT IT DOES:
#   Unit tests for fusion_v2.solver_vitmatte.  All tests skip cleanly if
#   torch, transformers, or the ViTMatte weights are unavailable — so the
#   Phase 1/2 pytest run stays green in torch-free environments.
#
# DEPENDS ON: numpy, cv2, fusion_v2.solver_vitmatte (optional),
#             fusion_v2.trimap_builder, fusion_v2.solver_interface
# AFFECTS: nothing (test-only)
# ISOLATED: yes

import sys
import os
import traceback

import numpy as np
import cv2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ---------------------------------------------------------------------------
# Skip guard — everything below requires torch + weights
# ---------------------------------------------------------------------------

def _torch_and_weights_available() -> bool:
    try:
        import torch
        import transformers
        from fusion_v2.solver_vitmatte import _WEIGHTS_DIR, _HF_REPO
        # Check if weights are actually cached
        from huggingface_hub import try_to_load_from_cache
        p = try_to_load_from_cache(_HF_REPO, "config.json", cache_dir=_WEIGHTS_DIR)
        return p is not None
    except Exception:
        return False


_SKIP_REASON = "torch or ViTMatte weights not available"
_skip_if_no_vitmatte = pytest.mark.skipif(
    not _torch_and_weights_available(), reason=_SKIP_REASON
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _small_ellipse_scene(H=200, W=200):
    """Return (mask, frame_rgb, trimap, nn_alpha) for a small ellipse scene."""
    from fusion_v2.trimap_builder import build_trimap
    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.ellipse(mask, (W // 2, H // 2), (W // 4, H // 3), 0, 0, 360, 255, -1)
    nn = np.where(mask > 0, 0.97, 0.0).astype(np.float32)
    frame = np.full((H, W, 3), 200, dtype=np.uint8)
    frame[mask > 0] = 60
    trimap = build_trimap(mask, nn)
    return mask, frame, trimap, nn


# ---------------------------------------------------------------------------
# (a) FG/BG passthrough exact
# ---------------------------------------------------------------------------

@_skip_if_no_vitmatte
def test_vitmatte_fg_bg_passthrough():
    """ViTMatte must return exact 1.0 for FG and 0.0 for BG pixels."""
    import fusion_v2.solver_vitmatte  # trigger registration

    from fusion_v2.solver_interface import solve_matte
    from fusion_v2.trimap_builder import TRIMAP_FG, TRIMAP_BG

    _, frame, trimap, nn = _small_ellipse_scene()
    alpha = solve_matte(frame, trimap, nn, solver="vitmatte")

    fg_vals = alpha[trimap == TRIMAP_FG]
    bg_vals = alpha[trimap == TRIMAP_BG]

    assert fg_vals.size > 0, "No FG pixels"
    assert bg_vals.size > 0, "No BG pixels"
    assert np.all(fg_vals == 1.0), f"FG drift: min={fg_vals.min():.6f}"
    assert np.all(bg_vals == 0.0), f"BG drift: max={bg_vals.max():.6f}"


# ---------------------------------------------------------------------------
# (b) Output range and dtype
# ---------------------------------------------------------------------------

@_skip_if_no_vitmatte
def test_vitmatte_output_range_and_dtype():
    """Output must be float32, shape (H, W), all values in [0, 1]."""
    import fusion_v2.solver_vitmatte

    from fusion_v2.solver_interface import solve_matte

    H, W = 200, 200
    _, frame, trimap, nn = _small_ellipse_scene(H, W)
    alpha = solve_matte(frame, trimap, nn, solver="vitmatte")

    assert alpha.dtype == np.float32, f"Expected float32, got {alpha.dtype}"
    assert alpha.shape == (H, W),     f"Shape mismatch: {alpha.shape}"
    assert alpha.min() >= 0.0,        f"alpha < 0: {alpha.min():.6f}"
    assert alpha.max() <= 1.0,        f"alpha > 1: {alpha.max():.6f}"


# ---------------------------------------------------------------------------
# (c) Registry resolves 'vitmatte'
# ---------------------------------------------------------------------------

def test_vitmatte_registry():
    """Importing solver_vitmatte must register 'vitmatte' in the interface."""
    import fusion_v2.solver_vitmatte  # triggers register_solver('vitmatte', ...)

    from fusion_v2.solver_interface import available_solvers
    assert "vitmatte" in available_solvers(), (
        f"'vitmatte' not in registry after import. Available: {available_solvers()}"
    )


# ---------------------------------------------------------------------------
# __main__ runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_vitmatte_registry,
        test_vitmatte_fg_bg_passthrough,
        test_vitmatte_output_range_and_dtype,
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
