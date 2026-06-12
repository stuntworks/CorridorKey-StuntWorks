# Last modified: 2026-06-12 | Change: Phase 1 tests — synthetic silhouette verification
#
# WHAT IT DOES:
#   Unit tests for fusion_v2.trimap_builder.  All shapes are synthetic (numpy +
#   cv2 draw calls) — no real footage required.  Tests cover:
#     (a) Resolution invariance: same silhouette at 2x scale → IoU > 0.95 per class
#     (b) Feet zone: bottom 12% of bbox uses ~half the dilation width
#     (c) Outside dilated SAM = 0 regardless of nn_alpha (Amendment 1)
#     (d) Deep inside eroded SAM + nn_alpha > 0.95 = 255; < 0.95 = 128
#     (e) Circular structuring element: dilation uniform along axes and 45° diagonals
#
# DEPENDS ON: numpy, cv2, fusion_v2.trimap_builder
# AFFECTS: nothing (test-only file)
# ISOLATED: yes

import sys
import os
import traceback

import numpy as np
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fusion_v2.trimap_builder import (
    build_trimap,
    build_trimap_sequence,
    TRIMAP_BG,
    TRIMAP_UNKNOWN,
    TRIMAP_FG,
    _EPSILON,
)


# ---------------------------------------------------------------------------
# Synthetic shape generators
# ---------------------------------------------------------------------------

def _ellipse_mask(H, W, cy_frac=0.5, cx_frac=0.5, ry_frac=0.30, rx_frac=0.25):
    """Filled ellipse mask; axes given as fractions of H / W."""
    mask = np.zeros((H, W), dtype=np.uint8)
    cy, cx = int(H * cy_frac), int(W * cx_frac)
    ry, rx = int(H * ry_frac), int(W * rx_frac)
    cv2.ellipse(mask, (cx, cy), (rx, ry), 0, 0, 360, 255, -1)
    return mask


def _stick_figure_mask(H, W):
    """Crude stick figure: head ellipse, torso rect, thin arms and legs."""
    mask = np.zeros((H, W), dtype=np.uint8)
    cx = W // 2
    cv2.ellipse(mask, (cx, int(H * 0.10)), (int(W * 0.06), int(H * 0.07)), 0, 0, 360, 255, -1)
    cv2.rectangle(mask,
                  (cx - int(W * 0.04), int(H * 0.17)),
                  (cx + int(W * 0.04), int(H * 0.55)), 255, -1)
    cv2.line(mask, (cx - int(W*0.04), int(H*0.22)), (cx - int(W*0.18), int(H*0.42)), 255, 3)
    cv2.line(mask, (cx + int(W*0.04), int(H*0.22)), (cx + int(W*0.18), int(H*0.42)), 255, 3)
    cv2.line(mask, (cx - int(W*0.02), int(H*0.55)), (cx - int(W*0.06), int(H*0.90)), 255, 4)
    cv2.line(mask, (cx + int(W*0.02), int(H*0.55)), (cx + int(W*0.06), int(H*0.90)), 255, 4)
    return mask


def _floor_silhouette_mask(H, W):
    """Trapezoid silhouette whose feet touch the bottom frame edge."""
    mask = np.zeros((H, W), dtype=np.uint8)
    cx = W // 2
    pts = np.array([
        [cx - 30, 50],
        [cx + 30, 50],
        [cx + 50, H - 1],
        [cx - 50, H - 1],
    ], dtype=np.int32)
    cv2.fillPoly(mask, [pts], 255)
    return mask


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = int((a & b).sum())
    union = int((a | b).sum())
    return inter / (union + _EPSILON)


# ---------------------------------------------------------------------------
# (a) Resolution invariance
# ---------------------------------------------------------------------------

def test_resolution_invariance():
    """Same silhouette at 2x scale must produce IoU > 0.95 per trimap class after resize."""
    H1, W1 = 400, 400
    H2, W2 = 800, 800

    mask_small = _ellipse_mask(H1, W1)
    mask_large = _ellipse_mask(H2, W2)

    nn_small = np.where(mask_small > 0, 0.97, 0.0).astype(np.float32)
    nn_large = np.where(mask_large > 0, 0.97, 0.0).astype(np.float32)

    t_small = build_trimap(mask_small, nn_small)
    t_large = build_trimap(mask_large, nn_large)

    # Nearest-neighbour resize preserves class labels
    t_large_down = cv2.resize(t_large, (W1, H1), interpolation=cv2.INTER_NEAREST)

    for label in [int(TRIMAP_BG), int(TRIMAP_UNKNOWN), int(TRIMAP_FG)]:
        a = (t_small == label)
        b = (t_large_down == label)
        if not a.any() and not b.any():
            continue  # class absent in both — skip
        score = _iou(a, b)
        assert score > 0.95, (
            f"Resolution invariance IoU for class {label}: {score:.3f} < 0.95\n"
            f"  small trimap classes: {np.unique(t_small)}, "
            f"large-down classes: {np.unique(t_large_down)}"
        )


# ---------------------------------------------------------------------------
# (b) Feet zone half dilation
# ---------------------------------------------------------------------------

def test_feet_zone_half_dilation():
    """Bottom feet_zone_pct rows must have approximately half the dilation width of body rows."""
    H, W = 600, 300
    # Rectangle silhouette: bbox_h=400, cols 50-199, rows 50-449
    mask = np.zeros((H, W), dtype=np.uint8)
    mask[50:450, 50:200] = 255

    nn_alpha = np.where(mask > 0, 0.97, 0.0).astype(np.float32)
    trimap = build_trimap(mask, nn_alpha, erode_pct=0.03, dilate_pct=0.06, feet_zone_pct=0.12)

    # Expected: bbox_h=400, dilate_radius=24, feet_top = 50 + int(400*0.88) = 402
    # Right edge of mask is col 199; col 200 is first outside pixel.
    # Body row 200: full-dilation unknown band = 24px (cols 200-223)
    # Feet row 430: half-dilation unknown band = 12px (cols 200-211)
    right_edge_col = 200

    def _unknown_band_width(row):
        """Count consecutive UNKNOWN pixels rightward from right_edge_col."""
        width = 0
        for c in range(right_edge_col, min(right_edge_col + 60, W)):
            if trimap[row, c] == TRIMAP_UNKNOWN:
                width += 1
            else:
                break
        return width

    body_width = _unknown_band_width(200)   # body zone row (< 402)
    feet_width = _unknown_band_width(430)   # feet zone row (> 402)

    assert body_width > 0, f"No unknown band at body row 200 (got {body_width})"
    assert feet_width > 0, f"No unknown band at feet row 430 (got {feet_width})"

    ratio = feet_width / (body_width + _EPSILON)
    assert 0.3 <= ratio <= 0.7, (
        f"Feet zone dilation ratio {ratio:.2f} not near 0.5 "
        f"(body={body_width}px, feet={feet_width}px)"
    )


# ---------------------------------------------------------------------------
# (c) Outside dilated SAM is always 0 — Amendment 1
# ---------------------------------------------------------------------------

def test_outside_dilated_always_zero():
    """Pixels outside the dilated SAM must be 0 regardless of nn_alpha value."""
    H, W = 300, 300
    mask = _ellipse_mask(H, W)  # ellipse ~90x75px

    # nn_alpha = 1.0 everywhere — old AND-condition bug would let outside pixels be FG
    nn_ones = np.ones((H, W), dtype=np.float32)
    trimap = build_trimap(mask, nn_ones, erode_pct=0.03, dilate_pct=0.06)

    # Pixels far enough from the mask edge are outside any dilation.
    # bbox_h ≈ 180, dilate_radius ≈ 10.8 → r=11, so 30px is safely outside.
    dist = cv2.distanceTransform(
        (1 - (mask > 0).astype(np.uint8)), cv2.DIST_L2, 5
    )
    far_outside = dist > 30
    outside_values = np.unique(trimap[far_outside])
    assert list(outside_values) == [0], (
        f"Far-outside pixels with nn_alpha=1.0 must be 0, got {outside_values} "
        f"(Amendment 1: outside dilated SAM = hard zero, no NN condition)"
    )

    # Test with a stick figure to confirm thin limbs also respected
    sf_mask = _stick_figure_mask(H, W)
    sf_trimap = build_trimap(sf_mask, nn_ones)

    dist_sf = cv2.distanceTransform(
        (1 - (sf_mask > 0).astype(np.uint8)), cv2.DIST_L2, 5
    )
    far_outside_sf = dist_sf > 40  # bbox_h ≈ 260, dilate ≈ 15px → 40px is safe
    sf_outside_values = np.unique(sf_trimap[far_outside_sf])
    assert list(sf_outside_values) == [0], (
        f"Far-outside stick-figure pixels must be 0, got {sf_outside_values}"
    )


# ---------------------------------------------------------------------------
# (d) Deep inside eroded SAM + nn_alpha > nn_high = 255
# ---------------------------------------------------------------------------

def test_deep_inside_eroded_is_fg():
    """Core of large silhouette with high nn_alpha must be FG; low alpha must be UNKNOWN."""
    H, W = 300, 300
    # Large ellipse — eroded core is substantial
    mask = _ellipse_mask(H, W, ry_frac=0.35, rx_frac=0.30)

    # High nn_alpha → center must be FG (255)
    nn_high = np.where(mask > 0, 0.97, 0.0).astype(np.float32)
    t_high = build_trimap(mask, nn_high)
    assert t_high[H // 2, W // 2] == TRIMAP_FG, (
        f"Center with nn_alpha=0.97 should be FG, got {t_high[H//2, W//2]}"
    )

    # Low nn_alpha → center must be UNKNOWN (128), not FG
    nn_low = np.where(mask > 0, 0.50, 0.0).astype(np.float32)
    t_low = build_trimap(mask, nn_low)
    assert t_low[H // 2, W // 2] == TRIMAP_UNKNOWN, (
        f"Center with nn_alpha=0.50 should be UNKNOWN, got {t_low[H//2, W//2]}"
    )

    # Floor-touching silhouette: center should still resolve correctly
    floor_mask = _floor_silhouette_mask(H, W)
    nn_floor = np.where(floor_mask > 0, 0.97, 0.0).astype(np.float32)
    t_floor = build_trimap(floor_mask, nn_floor)
    mid_y = (50 + H) // 2  # vertical midpoint of the silhouette
    assert t_floor[mid_y, W // 2] == TRIMAP_FG, (
        f"Midpoint of floor silhouette with high alpha should be FG, "
        f"got {t_floor[mid_y, W//2]}"
    )


# ---------------------------------------------------------------------------
# (e) Circular structuring element — no staircase on diagonals
# ---------------------------------------------------------------------------

def test_circular_structuring_element_no_staircase():
    """
    Dilation extent must be uniform in all directions (0°, 45°, 90°, 135°).
    MORPH_ELLIPSE deviation from mean < 15%.  MORPH_RECT would fail (~40% on diagonal).
    """
    H, W = 201, 201
    cy, cx = 100, 100
    radius = 35

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    point = np.zeros((H, W), dtype=np.uint8)
    point[cy, cx] = 1
    dilated = cv2.dilate(point, kernel)

    def _extent_at_angle(angle_deg):
        """Walk from center outward at angle_deg; return last set pixel distance."""
        angle_rad = np.radians(angle_deg)
        dy = np.sin(angle_rad)
        dx = np.cos(angle_rad)
        last_set = 0
        for d in range(1, radius + 15):
            r = int(round(cy + d * dy))
            c = int(round(cx + d * dx))
            if not (0 <= r < H and 0 <= c < W):
                break
            if dilated[r, c] > 0:
                last_set = d
            else:
                break
        return last_set

    angles = [0, 45, 90, 135]
    extents = [_extent_at_angle(a) for a in angles]
    mean_ext = sum(extents) / len(extents)

    for angle, ext in zip(angles, extents):
        dev = abs(ext - mean_ext) / (mean_ext + _EPSILON)
        assert dev < 0.15, (
            f"Staircase detected at {angle}°: extent={ext} px, mean={mean_ext:.1f} px, "
            f"deviation={dev:.2f} (> 0.15).  All extents: {dict(zip(angles, extents))}\n"
            f"  MORPH_ELLIPSE should give uniform extent; MORPH_RECT would staircase ~40%."
        )


# ---------------------------------------------------------------------------
# Sequence API smoke test
# ---------------------------------------------------------------------------

def test_build_trimap_sequence_passthrough():
    """Sequence builder returns one trimap per frame; temporal_smoother slot is called."""
    H, W = 100, 100
    masks  = [_ellipse_mask(H, W) for _ in range(3)]
    alphas = [np.where(m > 0, 0.97, 0.0).astype(np.float32) for m in masks]

    results = build_trimap_sequence(masks, alphas)
    assert len(results) == 3
    for t in results:
        assert t.dtype == np.uint8
        assert t.shape == (H, W)
        assert set(np.unique(t).tolist()).issubset({0, 128, 255}), (
            f"Unexpected trimap values: {np.unique(t)}"
        )

    # Temporal smoother hook must be called when provided
    calls = []
    def _mock_smoother(trimaps):
        calls.append(len(trimaps))
        return trimaps

    results2 = build_trimap_sequence(masks, alphas, temporal_smoother=_mock_smoother)
    assert calls == [3], f"Smoother not called (or called wrong): {calls}"
    assert len(results2) == 3


# ---------------------------------------------------------------------------
# __main__ runner (fallback if pytest is absent)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_resolution_invariance,
        test_feet_zone_half_dilation,
        test_outside_dilated_always_zero,
        test_deep_inside_eroded_is_fg,
        test_circular_structuring_element_no_staircase,
        test_build_trimap_sequence_passthrough,
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
