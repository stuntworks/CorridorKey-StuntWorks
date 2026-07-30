# Last modified: 2026-07-29 | Change: TDD tests pinning _reconnect_split_body's bridge to the measured gap between SAM body pieces (was a fixed 6%x18% kernel that filled every concavity, incl. the crotch-to-floor wedge) | Full history: git log
"""Behavioral tests for the waist-reconnect bridge geometry.

WHAT THIS PROVES: `_reconnect_split_body` may only restore pixels that actually sit
between the two split SAM body pieces. Before this fix it closed the whole silhouette
with a fixed 231x389 px kernel at 4K, which is 8x wider than the real 29 px gap on the
reference frame, so it filled every concavity -- including the wedge between the legs
down onto the floor mats. Measured on batch ck_batch_fdd2e28de847 frame 53: 105,890 px
forced to alpha 1.0, 73.7% of it in the bottom third of the frame.

These tests call the REAL function (no reimplementation), so the numbers cannot drift
from shipped code. The real-data tests read the regression corpus on K:; they skip
rather than fail when that drive is not mounted, because the corpus is not in the repo.

DELIBERATELY NOT TESTED HERE: the HSV anti-screen gate and the CK-evidence question.
The corpus proves CK alpha is >=0.5 on 99.8% of the restored pixels (89.6% exactly 1.0),
so a CK gate cannot discriminate vest from floor -- both are non-green. That is a
separate finding, not this fix.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
ndimage = pytest.importorskip("scipy.ndimage")

ROOT = Path(__file__).resolve().parent
CEP_PANEL_DIR = ROOT / "ae_plugin" / "cep_panel"
sys.path.insert(0, str(CEP_PANEL_DIR))

import ae_processor  # noqa: E402  (sys.path insert above must precede this import)

BATCH = Path(r"K:\CK_REGRESSION_CORPUS\CorridorKey_renders\ck_batch_fdd2e28de847")
SOURCE_VIDEO = Path(r"K:\CK_REGRESSION_CORPUS\SBV_0727.MOV")
BATCH_START_FRAME = 694  # render_manifest.json
BATCH_FRAME_COUNT = 107

corpus_required = pytest.mark.skipif(
    not BATCH.is_dir(), reason="regression corpus not mounted (K:)")


# WHAT IT DOES: Loads one exported SAM_JUNK frame and returns SAM body confidence.
# DEPENDS ON: the corpus PNGs. SAM_JUNK is white=junk / black=keep, so body = 1 - it.
# AFFECTS: every real-data test below; an inverted read here would silently invert
#   the whole suite, which is why the polarity is asserted in test_polarity_guard.
def _sam_body_from_junk_pass(frame_index):
    path = BATCH / "SAM_JUNK" / f"SAM_JUNK_{frame_index:05d}.png"
    junk = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert junk is not None, f"missing {path}"
    return 1.0 - junk[..., 0].astype(np.float32) / 255.0


# WHAT IT DOES: Loads CK's own matte for a frame, used as the ck_raw argument.
# DEPENDS ON: the corpus CK_ALPHA PNGs (uint16).
# AFFECTS: nothing about the restore footprint -- ck_raw is currently unused by the
#   function under test. Passed anyway so the call matches the production signature.
def _ck_alpha(frame_index):
    path = BATCH / "CK_ALPHA" / f"CK_ALPHA_{frame_index:05d}.png"
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert img is not None, f"missing {path}"
    return img[..., 0].astype(np.float32) / 65535.0


# WHAT IT DOES: Pulls the original plate frame for a batch index as RGB.
# DEPENDS ON: SOURCE_VIDEO and BATCH_START_FRAME.
# AFFECTS: the HSV anti-screen gate inside the function. Seeks by MSEC because
#   CAP_PROP_POS_FRAMES is off-by-one on H.264/HEVC in some OpenCV builds.
def _source_plate_rgb(frame_index):
    cap = cv2.VideoCapture(str(SOURCE_VIDEO))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.set(cv2.CAP_PROP_POS_MSEC, (BATCH_START_FRAME + frame_index) / fps * 1000.0)
        ok, bgr = cap.read()
    finally:
        cap.release()
    assert ok, f"could not decode source frame {BATCH_START_FRAME + frame_index}"
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


# WHAT IT DOES: Returns the large SAM components the function keys off, biggest first,
#   using the same res-relative minimum area the function uses.
# DEPENDS ON: scipy.ndimage.label. Mirrors ae_processor's _min_area formula.
# AFFECTS: the "unrelated region" definition in the batch sweep.
def _large_components(sam_body):
    height, width = sam_body.shape[:2]
    solid = sam_body > 0.5
    labels, count = ndimage.label(solid)
    if count < 1:
        return []
    minimum_area = (0.02 * height) * (0.02 * width)
    sizes = ndimage.sum(np.ones_like(labels), labels, range(1, count + 1))
    big = [(int(sizes[i]), i + 1) for i in range(count) if sizes[i] >= minimum_area]
    big.sort(reverse=True)
    return [labels == label for _, label in big]


# WHAT IT DOES: Runs the function and returns the boolean mask of pixels it raised.
# DEPENDS ON: ae_processor._reconnect_split_body.
# AFFECTS: every assertion below. Uses a zeroed alpha so any raised pixel is visible;
#   the restore mask does not depend on the incoming alpha values.
def _restored_mask(sam_body, ck_raw, src_rgb):
    base = np.zeros_like(sam_body, dtype=np.float32)
    out = ae_processor._reconnect_split_body(
        base, sam_body, ck_raw, src_rgb=src_rgb, screen_type="green")
    return np.asarray(out, dtype=np.float32) > 1e-6


@corpus_required
def test_polarity_guard_sam_junk_is_white_equals_junk():
    """Guards the whole file: SAM_JUNK must be white=junk, so the body is the dark part
    and occupies a minority of the frame. An inverted corpus would make every other
    assertion here meaningless."""
    sam_body = _sam_body_from_junk_pass(53)
    body_fraction = float((sam_body > 0.5).mean())
    assert 0.05 < body_fraction < 0.60, (
        f"SAM body covers {body_fraction:.1%} of frame -- polarity looks inverted")


@corpus_required
def test_frame_53_does_not_restore_the_crotch_to_floor_wedge():
    """THE REGRESSION. On frame 53 the two SAM pieces are the body and a 41k px hand
    blob at y[944-1208]. Nothing legitimate to reconnect lives in the bottom third of
    the frame, so nothing there may be forced solid. Pre-fix this restored 77,927 px
    of crotch-and-floor wedge in the bottom third alone."""
    sam_body = _sam_body_from_junk_pass(53)
    restored = _restored_mask(sam_body, _ck_alpha(53), _source_plate_rgb(53))
    height = sam_body.shape[0]
    bottom_third = restored[int(height * 2 / 3):]
    assert int(bottom_third.sum()) == 0, (
        f"{int(bottom_third.sum()):,} px restored in the bottom third of frame 53")


@corpus_required
def test_frame_53_still_bridges_the_real_gap():
    """The fix must not degenerate into restoring nothing. The body and the hand blob
    are 29 px apart, so a bridge between them is legitimate and must survive."""
    sam_body = _sam_body_from_junk_pass(53)
    restored = _restored_mask(sam_body, _ck_alpha(53), _source_plate_rgb(53))
    assert int(restored.sum()) > 0, "nothing bridged at all -- the feature is dead"
    components = _large_components(sam_body)
    assert len(components) >= 2
    distance_to_second = cv2.distanceTransform(
        (~components[1]).astype(np.uint8), cv2.DIST_L2, 5)
    assert restored[distance_to_second <= 60].any(), (
        "no restored pixel lies near the detached piece -- bridge is in the wrong place")


@corpus_required
def test_frame_2_is_an_exact_no_op():
    """Frame 2's SAM is a single component (the held-mask artifact welds arm to torso),
    so the function has no split to repair and must return the input untouched."""
    sam_body = _sam_body_from_junk_pass(2)
    assert len(_large_components(sam_body)) == 1
    restored = _restored_mask(sam_body, _ck_alpha(2), _source_plate_rgb(2))
    assert int(restored.sum()) == 0, "no-op frame restored pixels"


def test_synthetic_torso_leg_split_reconnects():
    """GUARD, not a regression: a genuine waist split -- torso above, legs below, an
    8 px dead band between them -- must still be bridged into one connected body. The
    corpus contains no such frame (37 frames fire, all of them side blobs), so the
    feature's actual purpose is only provable synthetically."""
    height, width = 400, 300
    sam_body = np.zeros((height, width), dtype=np.float32)
    sam_body[80:180, 110:190] = 1.0    # torso
    sam_body[188:320, 120:180] = 1.0   # legs, 8 px gap under the torso
    plate = np.full((height, width, 3), 128, dtype=np.uint8)  # grey: not screen-coloured

    assert len(_large_components(sam_body)) == 2, "synthetic setup is not actually split"
    restored = _restored_mask(sam_body, np.ones_like(sam_body), plate)
    assert restored[180:188, 130:170].any(), "the waist gap was not bridged"

    joined = (sam_body > 0.5) | restored
    labels, count = ndimage.label(joined)
    assert labels[100, 150] == labels[300, 150] != 0, (
        f"torso and legs still in different components ({count} components)")


def test_synthetic_far_piece_is_not_bridged():
    """A detached blob far below the body -- floor junk -- must not be dragged in, even
    though it is large enough to count and the plate is not screen-coloured."""
    height, width = 400, 300
    sam_body = np.zeros((height, width), dtype=np.float32)
    sam_body[60:200, 110:190] = 1.0    # body
    sam_body[330:390, 100:200] = 1.0   # floor junk, 130 px away
    plate = np.full((height, width, 3), 128, dtype=np.uint8)

    assert len(_large_components(sam_body)) == 2
    restored = _restored_mask(sam_body, np.ones_like(sam_body), plate)
    assert int(restored[210:325].sum()) == 0, (
        f"{int(restored[210:325].sum())} px bridged across a 130 px gap to floor junk")


@corpus_required
def test_no_restored_pixels_in_unrelated_bottom_regions_across_batch():
    """Sweeps all 107 frames. For every frame, if no detached piece reaches the bottom
    third, then nothing in the bottom third may be restored. src_rgb is None on purpose:
    that disables the HSV gate, which only ever REMOVES pixels, so this asserts against
    the widest possible restore footprint the geometry can produce."""
    offenders = []
    fired = 0
    for frame_index in range(BATCH_FRAME_COUNT):
        sam_body = _sam_body_from_junk_pass(frame_index)
        components = _large_components(sam_body)
        if len(components) < 2:
            continue
        fired += 1
        height = sam_body.shape[0]
        bottom_start = int(height * 2 / 3)
        detached_reaches_bottom = any(c[bottom_start:].any() for c in components[1:])
        if detached_reaches_bottom:
            continue
        restored = _restored_mask(sam_body, _ck_alpha(frame_index), None)
        leaked = int(restored[bottom_start:].sum())
        if leaked:
            offenders.append((frame_index, leaked))
    assert fired > 0, "no frame triggered the reconnect -- corpus or threshold changed"
    assert not offenders, (
        f"{len(offenders)} of {fired} firing frames restored bottom-region pixels: "
        f"{offenders[:8]}")
