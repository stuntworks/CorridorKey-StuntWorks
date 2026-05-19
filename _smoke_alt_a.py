"""Smoke test for ALT A v3 apply_sam2_gate. Temp file — re-runnable."""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sam2_combine import apply_sam2_gate


def make_scene(h=60, w=60):
    """Synthetic scene with three alpha zones (proportions match real shots
    so the local-avg box filter has enough context):
      Rows  0..15: alpha=1.0  (non-green floor / set pieces — top of frame)
      Rows 16..19: alpha=0.4  (HAIR FRINGE — soft NN keying transition)
      Rows 20..59: alpha=0.0  (green-killed background — large)
    SAM2 gate: 17x21 box at rows 12..28, cols 20..40. Tight on body —
    straddles fringe + part of green.
    """
    alpha = np.zeros((h, w), dtype=np.float32)
    alpha[0:16, :] = 1.0      # solid foreground (floor / studio)
    alpha[16:20, :] = 0.4     # soft fringe row (hair / butt-strap edge)
    # Rows 20..59 stay 0 (green)
    gate = np.zeros((h, w), dtype=np.float32)
    gate[12:29, 20:41] = 1.0
    return alpha, gate


def make_actor_on_concrete():
    """Synthetic scene: actor partially on concrete (no green nearby).
      Entire frame: alpha=1.0 (NN keyed nothing as background — non-green plate)
    SAM2 gate: covers the actor.
    Tests that v3 keeps SAM2 boundary tight when no green to anchor."""
    h, w = 40, 40
    alpha = np.ones((h, w), dtype=np.float32)
    gate = np.zeros((h, w), dtype=np.float32)
    gate[10:30, 10:30] = 1.0
    return alpha, gate


def test_halo_zero_bit_identical():
    a, g = make_scene()
    out = apply_sam2_gate(a, g, halo_px=0)
    expected = a * g
    assert np.array_equal(out, expected), "halo=0 must be bit-identical to alpha*gate"
    print("PASS: halo=0 bit-identical")


def test_v3_extends_in_green_neighborhood():
    """Hair fringe (alpha=0.4) just outside SAM2, neighborhood is mostly green.
    v3 must EXTEND SAM2 here so CorridorKey's natural fringe alpha shows."""
    a, g = make_scene()
    out = apply_sam2_gate(a, g, halo_px=5)
    # Row 18 col 17 — fringe row, 3px left of gate, neighborhood mostly green.
    # alpha_local_avg: rows 16-20 cols 15-19 = mostly 0.4 + some 0 → ~0.32 < 0.5
    # → green_neighborhood TRUE → extension allowed.
    assert out[18, 17] > 0, f"v3 must recover hair fringe in green neighborhood — got {out[18, 17]}"
    assert abs(out[18, 17] - 0.4) < 1e-6, f"recovered fringe should equal NN alpha 0.4 — got {out[18, 17]}"
    print("PASS: v3 extends SAM2 in green-side neighborhood (hair recovered)")


def test_v3_blocks_in_non_green_neighborhood():
    """Floor pixel (alpha=1) just outside SAM2, neighborhood is mostly non-green.
    v3 must NOT extend SAM2 — kills the foot-line bloat orientation-agnostically."""
    a, g = make_scene()
    out = apply_sam2_gate(a, g, halo_px=5)
    # Row 10 col 17 — non-green floor row, above gate, neighborhood all alpha=1.
    # alpha_local_avg: rows 8-12 cols 15-19 = all 1.0 → 1.0 > 0.5
    # → green_neighborhood FALSE → no extension.
    assert out[10, 17] == 0, f"v3 must NOT extend into non-green neighborhood (row 10 col 17) — got {out[10, 17]}"
    print("PASS: v3 blocks extension in non-green neighborhood (no foot bloat)")


def test_v3_tight_cut_on_concrete():
    """Actor on non-green plate (no green anywhere in scene).
    v3 should fall back to UNIFORM dilation (since no NN-killed pixels) — HALO
    still has visible effect for users on no-green plates."""
    a, g = make_actor_on_concrete()
    out = apply_sam2_gate(a, g, halo_px=5)
    # No green in scene → fallback to uniform dilation. Pixel just outside gate
    # within 5px should be in dilated zone, alpha=1 → result=1.
    assert out[8, 20] > 0, f"no-green fallback should give visible HALO (row 8 col 20) — got {out[8, 20]}"
    print("PASS: v3 no-green fallback degrades to uniform dilation")


def test_old_gate_env_var_fallback():
    """OLD path = uniform dilation everywhere. For A/B comparison."""
    a, g = make_scene()
    os.environ["CORRIDORKEY_OLD_GATE"] = "1"
    try:
        out_old = apply_sam2_gate(a, g, halo_px=5)
    finally:
        del os.environ["CORRIDORKEY_OLD_GATE"]
    out_v3 = apply_sam2_gate(a, g, halo_px=5)
    # Row 10 col 17: OLD bloats (alpha=1 included), v3 blocks (non-green nbhd)
    assert out_old[10, 17] > 0, f"OLD: must dilate uniformly into non-green — got {out_old[10, 17]}"
    assert out_v3[10, 17] == 0, f"v3: must block non-green neighborhood — got {out_v3[10, 17]}"
    print("PASS: env-var fallback A/B divergence verified")


def test_no_green_fallback():
    """If alpha has no NN-killed pixels, v3 degrades to uniform dilation."""
    a = np.ones((20, 20), dtype=np.float32)
    g = np.zeros((20, 20), dtype=np.float32)
    g[8:12, 8:12] = 1.0
    out = apply_sam2_gate(a, g, halo_px=3)
    expected = a * g
    assert out.sum() > expected.sum(), "no-green fallback should still dilate (uniform)"
    print("PASS: no-green plate falls back to uniform dilation")


def test_gate_none():
    a = np.ones((10, 10), dtype=np.float32)
    out = apply_sam2_gate(a, None, halo_px=10)
    assert np.array_equal(out, a)
    print("PASS: gate=None returns alpha unchanged")


def test_dtype_preserved():
    a, g = make_scene()
    out = apply_sam2_gate(a.astype(np.float32), g.astype(np.float32), halo_px=5)
    assert out.dtype == np.float32, f"dtype must be float32, got {out.dtype}"
    print("PASS: dtype preserved")


def test_invert_path():
    a, g = make_scene()
    out_invert_no_halo = apply_sam2_gate(a, g, invert=True, halo_px=0)
    expected = a * (1.0 - g)
    assert np.array_equal(out_invert_no_halo, expected), "invert=True halo=0 must be alpha*(1-gate)"
    print("PASS: invert path with halo=0 unchanged")


def make_notch_scene(h=40, w=40):
    """Body silhouette with a SAM2 notch — cut into the body where SAM2 wrongly
    excludes a body region. NN says body (alpha=1) inside the notch.
    """
    alpha = np.zeros((h, w), dtype=np.float32)
    alpha[10:30, 10:30] = 1.0  # body keyed by NN
    gate = np.zeros((h, w), dtype=np.float32)
    gate[10:30, 10:30] = 1.0   # SAM2 covers body
    gate[18:22, 25:30] = 0.0   # NOTCH: 4x5 inward dip on right side of SAM2
    return alpha, gate


def make_wire_scene(h=40, w=40):
    """Body silhouette with a thin wire passing through. NN drops wire to
    alpha=0 (didn't key it as body). SAM2 also drops the wire region.
    v4 must NOT fill the wire (NN guard prevents it).
    """
    alpha = np.zeros((h, w), dtype=np.float32)
    alpha[10:30, 10:30] = 1.0
    alpha[19:21, 5:35] = 0.0   # 2px wire across body — NN says NOT body here
    gate = np.zeros((h, w), dtype=np.float32)
    gate[10:30, 10:30] = 1.0
    gate[19:21, 5:35] = 0.0    # SAM2 also drops the wire region
    return alpha, gate


def test_v5_fills_butt_notch():
    """SAM2 notch where NN confirms body → v5 NN-OR fills it (any size)."""
    a, g = make_notch_scene()
    out = apply_sam2_gate(a, g, halo_px=5)  # halo>0 triggers v5
    # Notch pixel (20, 27): alpha=1 (NN says body), original gate=0 (SAM2 wrong)
    # nn_body=1 at notch, sam2_zone=1 (within halo of binary)
    # → binary becomes 1 at notch, output = alpha*1 = 1
    assert out[20, 27] > 0.5, f"v5 must fill notch where NN confirms body — got {out[20, 27]}"
    print("PASS: v5 fills NN-confirmed butt notch")


def test_v5_doesnt_seal_wire():
    """Thin wire passing through body. NN says wire is NOT body (alpha=0).
    v5 NN-OR must NOT fill (NN guard prevents it)."""
    a, g = make_wire_scene()
    out = apply_sam2_gate(a, g, halo_px=5)
    # Wire pixel (20, 20): alpha=0 (NN says NOT body), gate=0
    # nn_body=0 at wire → no fill from NN-OR, binary stays 0, output = 0
    assert out[20, 20] == 0, f"v5 must NOT seal wire (NN says not body) — got {out[20, 20]}"
    print("PASS: v5 does NOT seal wire (NN guards)")


def test_v5_doesnt_fill_far_from_sam2():
    """NN-body pixels FAR from SAM2 silhouette must NOT be added (would cause
    studio junk inclusion). NN-OR is gated by sam2_zone (halo dilation)."""
    h, w = 60, 60
    alpha = np.zeros((h, w), dtype=np.float32)
    alpha[10:30, 10:30] = 1.0  # body region — NN says body
    alpha[40:50, 40:50] = 1.0  # studio junk far from body — NN can't tell
    gate = np.zeros((h, w), dtype=np.float32)
    gate[10:30, 10:30] = 1.0   # SAM2 covers body only
    out = apply_sam2_gate(alpha, gate, halo_px=5)
    # Junk pixel (45, 45): NN=1 but FAR from SAM2 (>5px halo away)
    # sam2_zone at (45, 45) = 0 → no NN-OR fill, gate stays 0, result = 0
    assert out[45, 45] == 0, f"v5 must NOT fill NN-body far from SAM2 — got {out[45, 45]}"
    print("PASS: v5 does NOT fill NN-confirmed body far from SAM2 (studio junk safe)")


if __name__ == "__main__":
    test_halo_zero_bit_identical()
    test_v3_extends_in_green_neighborhood()
    test_v3_blocks_in_non_green_neighborhood()
    test_v3_tight_cut_on_concrete()
    test_old_gate_env_var_fallback()
    test_no_green_fallback()
    test_gate_none()
    test_dtype_preserved()
    test_invert_path()
    test_v5_fills_butt_notch()
    test_v5_doesnt_seal_wire()
    test_v5_doesnt_fill_far_from_sam2()
    print("\nAll smoke tests passed.")
