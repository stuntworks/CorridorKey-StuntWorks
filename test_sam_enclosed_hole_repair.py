# Last modified: 2026-07-19 | Change: Verify CK-supported enclosed body holes are restored | Full history: git log
"""Regression checks for large enclosed holes in the unified-band keep matte."""

import importlib

import numpy as np


# WHAT IT DOES: Verifies a large enclosed region is restored only with strong CK evidence.
# DEPENDS ON:   corridorkey_sam_merge exposing should_restore_enclosed_body_region.
# AFFECTS:      Prevents SAM from punching holes through solid CK body regions.
def test_large_enclosed_hole_requires_majority_solid_ck_pixels():
    merge = importlib.import_module("corridorkey_sam_merge")
    should_restore = getattr(merge, "should_restore_enclosed_body_region", None)

    assert should_restore is not None
    mostly_solid_ck = np.array([1.0] * 7 + [0.0] * 3, dtype=np.float32)
    mostly_background_ck = np.array([1.0] * 3 + [0.0] * 7, dtype=np.float32)

    assert should_restore(32000, 6000, mostly_solid_ck)
    assert not should_restore(32000, 6000, mostly_background_ck)
    assert should_restore(1000, 6000, mostly_background_ck)
