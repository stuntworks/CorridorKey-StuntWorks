# Last modified: 2026-07-25 | Change: regression test — SAM's interactive preview must key the source frame CK actually extracted, not a sequence/comp-fps-mismatched frame number | Full history: git log
"""Regression test for the Premiere/AE interactive-preview frame-alignment bug.

ROOT CAUSE (proven two independent ways, see D:\\CLAUDE_JUNK\\ck_frame_alignment_investigation_20260725\\):
CK and SAM were handed two different frames of the SAME source video for the SAME
preview. CK seeks by TIME (cmd_extract, ae_processor.py ~1078-1084/1115, via
CAP_PROP_POS_MSEC) -- correct, fps-independent. SAM seeks by FRAME INDEX
(cmd_sam_apply, ae_processor.py ~4255-4298, via CAP_PROP_POS_FRAMES) using
settings["sourceFrame"], a number the PANEL computes as
round(sourceTimeSeconds * HOST-TIMELINE-fps) -- Premiere's SEQUENCE fps
(ppro_getFrameInfo, host.jsx:863) or AE's COMP fps (ae_getFrameInfo, host.jsx:188)
-- NOT the source clip's own fps. When the two differ, SAM keys the wrong frame.

Worked example (Berto's regression case): a 24fps Premiere sequence containing a
119.88fps source clip, playhead at t=5.92258333333333s.
    round(5.92258333333333 * 24)     = 142   <- WRONG: what the panel sent (pre-fix)
    round(5.92258333333333 * 119.88) = 710   <- RIGHT: the frame CK actually keys

This file proves, against the REAL cmd_sam_apply / cmd_extract code (no video
mocking -- a real, frame-exact synthetic source clip is decoded by both paths):
  1. SAM's pre-roll branch resolves its target frame to 710, not 142.
  2. The frame SAM's pre-roll branch feeds toward the predictor is pixel-identical
     to the frame CK's own cmd_extract decodes for the same sourceTimeSeconds.

No SAM2 model weights are loaded and no GPU inference runs: _get_video_predictor
is monkeypatched to raise a probe-only sentinel the instant init_state() would be
called, after the pre-roll branch has already seeked+decoded+written the real
frames to disk via genuine cv2 video decode.
"""
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parent
CEP_PANEL_DIR = ROOT / "ae_plugin" / "cep_panel"
sys.path.insert(0, str(CEP_PANEL_DIR))

import ae_processor  # noqa: E402  (sys.path insert above must precede this import)

# ── Regression case constants (Berto's worked example) ─────────────────────
SOURCE_FPS = 119.88
SEQUENCE_FPS = 24.0
SOURCE_TIME_SEC = 5.92258333333333
WRONG_SEQUENCE_FPS_FRAME = 142   # round(SOURCE_TIME_SEC * SEQUENCE_FPS) -- pre-fix panel value
CORRECT_SOURCE_FPS_FRAME = 710   # round(SOURCE_TIME_SEC * SOURCE_FPS)  -- the true source frame
N_FRAMES = 720                    # > CORRECT_SOURCE_FPS_FRAME, gives room for pre-roll seek


def test_worked_example_arithmetic_matches_bug_report():
    """Pins the two numbers this whole test file is built around."""
    assert round(SOURCE_TIME_SEC * SEQUENCE_FPS) == WRONG_SEQUENCE_FPS_FRAME
    assert round(SOURCE_TIME_SEC * SOURCE_FPS) == CORRECT_SOURCE_FPS_FRAME


def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@pytest.fixture(scope="module")
def synthetic_source_video(tmp_path_factory):
    """Builds a real, frame-exact 119.88fps source video where frame i's pixel
    content encodes i (low byte in B, high byte in G). Muxed via ffmpeg's image2
    demuxer with a lossless codec (FFV1) and constant frame rate -- cv2's own
    VideoWriter at a fractional fps (119.88) was tried first and produced
    duplicate/dropped frames on read-back (0 -> 576 mismatches out of 720 reads),
    making it useless for a frame-exact test. ffmpeg's CFR PTS generation from
    -r plus a lossless codec gives 0 mismatches on the same read-back check.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        pytest.skip("ffmpeg not found (checked PATH and imageio_ffmpeg) -- "
                     "cannot build a frame-exact synthetic source video")

    work = tmp_path_factory.mktemp("ck_sam_frame_alignment")
    frames_dir = work / "frames"
    frames_dir.mkdir()
    for i in range(N_FRAMES):
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        frame[:, :, 0] = i & 0xFF
        frame[:, :, 1] = (i >> 8) & 0xFF
        frame[:, :, 2] = 200
        cv2.imwrite(str(frames_dir / f"f_{i:06d}.png"), frame)

    out = work / "source_119fps.mkv"
    cmd = [
        ffmpeg, "-y", "-r", str(SOURCE_FPS), "-f", "image2",
        "-i", str(frames_dir / "f_%06d.png"),
        "-c:v", "ffv1", "-pix_fmt", "bgr24", "-vsync", "cfr",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        pytest.skip(f"ffmpeg failed to build synthetic source video: {r.stderr[-1500:]}")

    # Sanity: confirm the muxed file is genuinely frame-exact before trusting it
    # as a test oracle (see docstring above for why this check exists).
    cap = cv2.VideoCapture(str(out))
    reported_fps = cap.get(cv2.CAP_PROP_FPS)
    i = 0
    mismatches = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        idx = int(fr[0, 0, 0]) | (int(fr[0, 0, 1]) << 8)
        if idx != i:
            mismatches += 1
        i += 1
    cap.release()
    if mismatches or i != N_FRAMES or abs(reported_fps - SOURCE_FPS) > 0.01:
        pytest.skip(f"synthetic source video is not frame-exact (fps={reported_fps}, "
                     f"frames_read={i}, mismatches={mismatches}) -- cannot trust it as an oracle")

    return out


def _decode_index(png_bytes):
    """Reads the frame index this test file encoded into a frame's pixels back out."""
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    assert img is not None
    return int(img[0, 0, 0]) | (int(img[0, 0, 1]) << 8)


class _ProbeStop(BaseException):
    """Raised from inside a monkeypatched _get_video_predictor().init_state() the
    instant it would hand frames to the real SAM2 predictor. Deliberately a
    BaseException (not Exception) so it is NOT swallowed by cmd_sam_apply's own
    `except Exception` handlers and instead escapes straight out to the test --
    carrying the exact padded PNG bytes SAM's pre-roll branch decoded, with zero
    SAM2 model weights loaded and zero GPU inference run."""
    def __init__(self, padded_frame_bytes):
        super().__init__("probe stop (test-only, not a real failure)")
        self.padded_frame_bytes = padded_frame_bytes  # {0: preroll bytes, 1: target bytes}


def _run_sam_preroll_probe(monkeypatch, session_dir, source_video, sam_source_frame,
                            with_source_time_seconds):
    """Runs the REAL ae_processor.cmd_sam_apply and returns the padded PNG bytes of
    the TARGET frame (index 1) its pre-roll branch decoded from `source_video`.
    """
    fg_path = session_dir / "fg.png"
    cv2.imwrite(str(fg_path), np.zeros((64, 64, 3), dtype=np.uint16))

    settings = {
        "sam_positive": [[32, 32]],
        "sam_negative": [],
        "sourceVideo": str(source_video),
        "sourceFrame": sam_source_frame,
    }
    if with_source_time_seconds:
        settings["sourceTimeSeconds"] = SOURCE_TIME_SEC

    def _fake_get_video_predictor(cfg, ckpt, device):
        class _FakeVideoPredictor:
            def init_state(self, video_path, **kwargs):
                vp = Path(video_path)
                frames = {i: (vp / f"{i:06d}.png").read_bytes() for i in (0, 1)}
                raise _ProbeStop(frames)
        return _FakeVideoPredictor()

    monkeypatch.setattr(ae_processor, "_get_video_predictor", _fake_get_video_predictor)

    try:
        ae_processor.cmd_sam_apply(str(session_dir), settings)
    except _ProbeStop as stop:
        return stop.padded_frame_bytes[1]

    pytest.fail("cmd_sam_apply never reached the video-predictor pre-roll branch "
                "(returned without calling init_state) -- test setup is broken, "
                "this is not evidence about the fix.")


# WHAT IT DOES: Proves cmd_sam_apply's interactive pre-roll branch seeks the
#   SOURCE-media frame the panel's sourceTimeSeconds actually points at (710),
#   not the sequence/comp-fps-mismatched frame number the panel also sends (142).
# DEPENDS ON: ae_processor.cmd_sam_apply's pre-roll block (~ae_processor.py:4245-4365).
# AFFECTS: Every interactive SAM preview in Premiere and After Effects.
def test_sam_preroll_resolves_true_source_frame_not_sequence_fps_frame(
    synthetic_source_video, tmp_path, monkeypatch,
):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    target_png = _run_sam_preroll_probe(
        monkeypatch, session_dir, synthetic_source_video,
        sam_source_frame=WRONG_SEQUENCE_FPS_FRAME,
        with_source_time_seconds=True,
    )
    resolved_frame = _decode_index(target_png)
    assert resolved_frame == CORRECT_SOURCE_FPS_FRAME, (
        f"SAM keyed source frame {resolved_frame}, expected {CORRECT_SOURCE_FPS_FRAME} "
        f"(the frame at t={SOURCE_TIME_SEC}s using the SOURCE's own {SOURCE_FPS}fps). "
        f"Got the sequence-fps frame number ({WRONG_SEQUENCE_FPS_FRAME}) instead -- "
        f"this is the bug."
    )


# WHAT IT DOES: Byte-level proof CK and SAM decode the identical source frame for
#   the same preview -- not just "some frame close to it."
# DEPENDS ON: ae_processor.cmd_extract (CK's path) and cmd_sam_apply (SAM's path).
# AFFECTS: Confirms the fix removes the two-different-frames bug at the pixel level.
def test_sam_preroll_frame_is_pixel_identical_to_ck_extract_frame(
    synthetic_source_video, tmp_path, monkeypatch,
):
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    # CK's real path: cmd_extract, unmodified, seeking by sourceTimeSeconds.
    ck_out = tmp_path / "ck_extract.png"
    ok = ae_processor.cmd_extract(str(synthetic_source_video), str(ck_out), time_sec=SOURCE_TIME_SEC)
    assert ok, "cmd_extract failed against the synthetic source video"
    ck_frame = cv2.imread(str(ck_out), cv2.IMREAD_UNCHANGED)
    assert ck_frame is not None

    # SAM's real path: cmd_sam_apply's pre-roll branch.
    target_png = _run_sam_preroll_probe(
        monkeypatch, session_dir, synthetic_source_video,
        sam_source_frame=WRONG_SEQUENCE_FPS_FRAME,
        with_source_time_seconds=True,
    )
    sam_frame = cv2.imdecode(np.frombuffer(target_png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert sam_frame is not None

    # Compare DECODED PIXEL ARRAYS, not raw PNG file bytes: cmd_extract's
    # cv2.imwrite uses default PNG compression while cmd_sam_apply's pre-roll
    # write uses IMWRITE_PNG_COMPRESSION=1 -- different compression settings
    # produce different compressed file bytes for identical pixel content (PNG
    # compression is lossless, so decoded pixels are unaffected). No other
    # tolerance is used: the two arrays must match exactly, pixel for pixel.
    assert ck_frame.shape == sam_frame.shape
    assert np.array_equal(ck_frame, sam_frame), (
        "CK's cmd_extract frame and SAM's cmd_sam_apply pre-roll target frame "
        "are NOT pixel-identical for the same sourceTimeSeconds -- CK and SAM "
        "are keying two different frames of the source video."
    )


# WHAT IT DOES: Guards the fallback path -- callers that don't send
#   sourceTimeSeconds (older callers / any other unmigrated call site) must keep
#   using the raw panel-supplied sourceFrame exactly as before this fix.
# DEPENDS ON: The same pre-roll block as the two tests above.
# AFFECTS: Backward compatibility of cmd_sam_apply's settings contract.
def test_sam_preroll_falls_back_to_panel_frame_when_source_time_seconds_absent(
    synthetic_source_video, tmp_path, monkeypatch,
):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    target_png = _run_sam_preroll_probe(
        monkeypatch, session_dir, synthetic_source_video,
        sam_source_frame=WRONG_SEQUENCE_FPS_FRAME,
        with_source_time_seconds=False,
    )
    resolved_frame = _decode_index(target_png)
    assert resolved_frame == WRONG_SEQUENCE_FPS_FRAME


# ═══════════════════════════════════════════════════════════════════════════
# Part 2 (2026-07-25): the SAME bug, THIRD location — ae_processor.cmd_batch's
# render-range SAM anchor, reported live by Berto:
#
#   samCommit: SAM -> batch (9 incl, anchor 142)
#   DOT FORENSICS anchor=142 dots=[(142,1354,44,+1) ... 8 more, all frame 142]
#   Batch: K:\CK_REGRESSION_CORPUS\SBV_0727.MOV  frames 710..914
#   SAM2 video: anchor at range frame 1 (absolute 142)
#
# cmd_batch's render range (710..914) is computed correctly from the SOURCE's own
# fps (this was already fixed). The anchor (142) is still the sequence-fps label,
# so it falls outside that range and cmd_batch's own safety-net range guard
# (correctly) rejects it and re-anchors SAM at range-start instead of the dotted
# frame -- SAM then propagates outward from the WRONG frame, and the render loses
# the subject a few frames past the seed (Berto: arm drops where the wire crosses
# it, ~10 frames in). AE's comp fps is close enough to source fps that its label
# usually lands inside the range and passes the guard by luck, not by design --
# which is why AE looked clean and Premiere didn't.
#
# Fix under test: ae_processor._sam_dot_source_frame + its two cmd_batch call
# sites (the single sam_anchor_frame and every sam_frames[] multi-stamp entry).
# ═══════════════════════════════════════════════════════════════════════════

# A second, independent stamped point (the ADD FRAME multi-stamp trap) -- source
# frame 703, inside the same [700, 719) render range as the primary anchor (710)
# but far enough from it to prove each dot resolves on ITS OWN time, not the
# anchor's. Its sequence-fps label is derived the same way SOURCE_TIME_SEC's is
# above, so if the fix ever regresses to raw-label trust, this stamp's label
# (~141, computed below) would land outside [700, 719) and the entry would be
# silently DROPPED -- the multi-stamp desync this test exists to catch.
SECOND_CORRECT_SOURCE_FPS_FRAME = 703
SECOND_SOURCE_TIME_SEC = SECOND_CORRECT_SOURCE_FPS_FRAME / SOURCE_FPS
SECOND_WRONG_SEQUENCE_FPS_FRAME = round(SECOND_SOURCE_TIME_SEC * SEQUENCE_FPS)

BATCH_START_FRAME = 700   # > 0 so SAM's 1-frame pre-roll (source frame 699) is exercised
BATCH_END_FRAME = 719     # < N_FRAMES(720); holds both stamped source frames (703, 710)
BATCH_ACTUAL_PREROLL = 1  # mirrors cmd_batch's `min(1, start_frame)` for start_frame=700


class _BatchProbeStop(BaseException):
    """Raised from a monkeypatched fake SAM2 video predictor's propagate_in_video()
    the instant cmd_batch's SAM block would start real inference -- BaseException
    (not Exception) so it escapes cmd_batch's own `except Exception as _e` guard
    around the whole SAM block (ae_processor.py, wraps the SAM-active branch) instead
    of being silently swallowed into a 0-mask CK-only fallback, which would hide the
    very thing under test: whether the anchor/multi-stamp resolution and the range
    guard did what this test expects. Carries the fake predictor instance so the test
    can inspect every add_new_points_or_box() call cmd_batch made before propagation
    -- the anchor call AND every multi-stamp call -- with zero SAM2 model weights
    loaded and zero GPU inference run."""
    def __init__(self, predictor):
        super().__init__("batch probe stop (test-only, not a real failure)")
        self.predictor = predictor


class _FakeBatchVideoPredictor:
    """Stands in for the real SAM2VideoPredictor inside cmd_batch. init_state() and
    add_new_points_or_box() are real no-ops that just record what cmd_batch called
    them with (video_path, and every add_new_points_or_box call in order) -- this is
    what lets the multi-stamp test observe the SECOND (non-anchor) stamp's resolved
    frame_idx, which cmd_batch only computes AFTER init_state() returns. propagate_in_
    video() raises _BatchProbeStop the instant it's called (before any inference),
    which is the earliest point after BOTH the anchor and every sam_frames[] entry
    have been resolved and registered.
    """
    def __init__(self):
        self.video_path = None
        self.calls = []  # each: dict(frame_idx, obj_id, clear_old_points)

    def init_state(self, video_path, **kwargs):
        self.video_path = video_path
        return {"fake_state": True}

    def add_new_points_or_box(self, inference_state, frame_idx, obj_id, points, labels,
                               clear_old_points):
        # Read the exported PNG's bytes NOW, not after propagate_in_video() raises --
        # cmd_batch's `finally: rmtree(sam_tmp_dir)` deletes video_path the instant the
        # exception unwinds past it, before test code outside cmd_batch could ever see it.
        png_bytes = (Path(self.video_path) / f"{frame_idx:06d}.png").read_bytes()
        self.calls.append({
            "frame_idx": frame_idx, "obj_id": obj_id, "clear_old_points": clear_old_points,
            "png_bytes": png_bytes,
        })

    def propagate_in_video(self, inference_state, reverse=False):
        raise _BatchProbeStop(self)

    def reset_state(self, inference_state):  # pragma: no cover - never reached
        pass


def _fake_get_processor(device="cuda"):
    """Stands in for ae_processor._get_processor. cmd_batch builds a REAL
    CorridorKeyProcessor (~5-8s NN weight load) unconditionally before it even looks
    at SAM settings, but never calls anything on it before our SAM2 probe fires below
    -- so a bare placeholder reaches the probe with zero GPU/NN weights touched."""
    return object()


def _run_cmd_batch_sam_probe(monkeypatch, caplog, source_video, out_dir, settings_extra):
    """Runs the REAL ae_processor.cmd_batch far enough to resolve + register every SAM
    dot (anchor and every sam_frames[] multi-stamp entry) against the REAL range-guard
    code, then stops it via _BatchProbeStop the instant it would start real SAM2
    propagation. Returns the fake predictor (video_path + recorded calls) so the
    caller can inspect exactly what cmd_batch resolved, plus caplog for the anchor's
    own log line and the range-guard fallback warning.
    """
    monkeypatch.setattr(ae_processor, "_get_processor", _fake_get_processor)
    fake_predictor = _FakeBatchVideoPredictor()
    monkeypatch.setattr(ae_processor, "_get_video_predictor",
                         lambda cfg, ckpt, device: fake_predictor)

    settings = {"screenType": "green", "sam_negative": []}
    settings.update(settings_extra)

    caplog.set_level(logging.INFO, logger="corridorkey")
    try:
        ae_processor.cmd_batch(str(source_video), str(out_dir), settings,
                                start_frame=BATCH_START_FRAME, end_frame=BATCH_END_FRAME)
    except _BatchProbeStop:
        return fake_predictor

    pytest.fail("cmd_batch never reached SAM2 propagate_in_video() (returned without "
                "raising) -- test setup is broken, this is not evidence about the fix.")


def _batch_anchor_log_values(caplog):
    """Pulls (range-relative, absolute) straight out of cmd_batch's own
    'SAM2 video: anchor at range frame N (absolute M)' log line -- the exact line
    Berto's real bug report quoted -- so this test reads cmd_batch's decision the
    same way the live log does."""
    for rec in caplog.records:
        m = re.search(r"SAM2 video: anchor at range frame (-?\d+) \(absolute (\S+)\)", rec.message)
        if m:
            return int(m.group(1)), m.group(2)
    pytest.fail("no 'SAM2 video: anchor at range frame' log line found -- "
                "test setup is broken, this is not evidence about the fix.")


# WHAT IT DOES: Reproduces Berto's exact live-log regression against the REAL
#   cmd_batch render-range path (cmd_sam_apply's PREVIEW path above was already
#   fixed in bd6bd27 -- this is the separate, still-broken RENDER path). Proves the
#   SAM anchor resolves to the true source frame (710) from sam_anchor_time_seconds
#   -- not the sequence-fps label (142) -- lands inside the render range, and the
#   range-guard fallback re-anchor does NOT fire.
# DEPENDS ON: ae_processor.cmd_batch's "Map absolute click frame -> range-relative
#   anchor" block and ae_processor._sam_dot_source_frame.
# AFFECTS: Every KEY CLIP / RENDER commit in Premiere and AE that carries SAM dots.
def test_cmd_batch_sam_anchor_resolves_true_source_frame_not_sequence_fps_frame(
    synthetic_source_video, tmp_path, monkeypatch, caplog,
):
    out_dir = tmp_path / "batch_out"
    predictor = _run_cmd_batch_sam_probe(
        monkeypatch, caplog, synthetic_source_video, out_dir,
        {
            "sam_positive": [[32, 32]],
            "sam_anchor_frame": WRONG_SEQUENCE_FPS_FRAME,       # 142 -- what Premiere sent
            "sam_anchor_time_seconds": SOURCE_TIME_SEC,          # new: fps-independent truth
        },
    )
    anchor_rel, anchor_abs = _batch_anchor_log_values(caplog)

    assert anchor_abs == str(CORRECT_SOURCE_FPS_FRAME), (
        f"cmd_batch resolved SAM anchor to absolute frame {anchor_abs}, expected "
        f"{CORRECT_SOURCE_FPS_FRAME} (t={SOURCE_TIME_SEC}s at the source's own "
        f"{SOURCE_FPS}fps). Got the sequence-fps label ({WRONG_SEQUENCE_FPS_FRAME}) "
        f"instead -- this is the bug."
    )

    # Requirement 3: the range-guard fallback (re-anchor at range start + warning)
    # must NOT have fired -- the guard stays a safety net, not the normal path, now
    # that it's being asked a question with the right units.
    fallback_msgs = [r.message for r in caplog.records if "outside decoded range" in r.message]
    assert not fallback_msgs, (
        f"range-guard fallback fired even though the true source frame "
        f"({CORRECT_SOURCE_FPS_FRAME}) is inside [{BATCH_START_FRAME}, {BATCH_END_FRAME}): "
        f"{fallback_msgs}"
    )

    expected_rel = CORRECT_SOURCE_FPS_FRAME - BATCH_START_FRAME + BATCH_ACTUAL_PREROLL
    assert anchor_rel == expected_rel, (
        f"range-relative anchor {anchor_rel} != expected {expected_rel} -- the "
        f"fallback squashed the anchor to the range start instead of resolving it."
    )
    assert predictor.calls, "cmd_batch never called add_new_points_or_box for the anchor"
    assert predictor.calls[0]["frame_idx"] == expected_rel
    assert predictor.calls[0]["clear_old_points"] is True

    # Pixel proof: the frame actually exported to SAM at the resolved anchor position
    # really is source frame 710 (not some off-by-N neighbour).
    resolved_frame = _decode_index(predictor.calls[0]["png_bytes"])
    assert resolved_frame == CORRECT_SOURCE_FPS_FRAME, (
        f"SAM's exported anchor frame encodes source index {resolved_frame}, "
        f"expected {CORRECT_SOURCE_FPS_FRAME}."
    )


# WHAT IT DOES: THE MULTI-FRAME DOT-STAMP TRAP. A second, independently-stamped
#   frame (ADD FRAME) must resolve on ITS OWN sourceTimeSeconds, not the anchor's --
#   proves the fix is a single centralized conversion applied to every dot label,
#   not a patch of the anchor value alone. Without per-entry timeSeconds, this
#   stamp's raw sequence-fps label (~141) falls outside [700, 719) and cmd_batch
#   silently drops the whole stamped frame (desync bug the task brief warns about).
# DEPENDS ON: The same cmd_batch block's "Multi-frame SAM prompting" loop.
# AFFECTS: The ADD FRAME feature (fast-motion clips needing more than one SAM seed).
def test_cmd_batch_sam_multi_stamp_each_resolves_its_own_source_frame(
    synthetic_source_video, tmp_path, monkeypatch, caplog,
):
    out_dir = tmp_path / "batch_out_multi"
    predictor = _run_cmd_batch_sam_probe(
        monkeypatch, caplog, synthetic_source_video, out_dir,
        {
            "sam_positive": [[32, 32]],
            "sam_anchor_frame": WRONG_SEQUENCE_FPS_FRAME,
            "sam_anchor_time_seconds": SOURCE_TIME_SEC,
            "sam_frames": [
                {"frame": WRONG_SEQUENCE_FPS_FRAME, "timeSeconds": SOURCE_TIME_SEC,
                 "positive": [[32, 32]], "negative": []},
                {"frame": SECOND_WRONG_SEQUENCE_FPS_FRAME, "timeSeconds": SECOND_SOURCE_TIME_SEC,
                 "positive": [[20, 20]], "negative": []},
            ],
        },
    )

    fallback_msgs = [r.message for r in caplog.records if "outside decoded range" in r.message]
    assert not fallback_msgs, f"anchor range-guard fallback fired unexpectedly: {fallback_msgs}"
    dropped_msgs = [r.message for r in caplog.records
                    if "SAM2 multi-frame: skipping entry" in r.message]
    assert not dropped_msgs, (
        f"the second stamp was dropped/skipped instead of being registered: {dropped_msgs}"
    )

    expected_anchor_rel = CORRECT_SOURCE_FPS_FRAME - BATCH_START_FRAME + BATCH_ACTUAL_PREROLL
    expected_second_rel = (SECOND_CORRECT_SOURCE_FPS_FRAME - BATCH_START_FRAME
                            + BATCH_ACTUAL_PREROLL)
    assert expected_second_rel != expected_anchor_rel  # sanity: test actually exercises 2 frames

    call_summary = [(c["frame_idx"], c["clear_old_points"]) for c in predictor.calls]
    call_frame_idxs = [c["frame_idx"] for c in predictor.calls]
    assert expected_anchor_rel in call_frame_idxs, (
        f"anchor (source frame {CORRECT_SOURCE_FPS_FRAME}, rel {expected_anchor_rel}) "
        f"was never registered: calls(frame_idx, clear_old_points)={call_summary}"
    )
    assert expected_second_rel in call_frame_idxs, (
        f"second stamp (source frame {SECOND_CORRECT_SOURCE_FPS_FRAME}, rel "
        f"{expected_second_rel}) was never registered -- it resolved from the wrong "
        f"label and either landed outside the range or collided with another frame: "
        f"calls(frame_idx, clear_old_points)={call_summary}"
    )
    second_call = next(c for c in predictor.calls if c["frame_idx"] == expected_second_rel)
    assert second_call["clear_old_points"] is False, (
        "second stamp must NOT clear the anchor's points (clear_old_points=False)"
    )

    # Pixel proof for BOTH stamped frames: each resolved slot in SAM's exported frame
    # folder really does hold ITS OWN dotted source frame, not the anchor's or a
    # neighbour's -- the coordinate-vs-frame relationship (requirement 4) is intact.
    anchor_call = next(c for c in predictor.calls if c["frame_idx"] == expected_anchor_rel)
    assert _decode_index(anchor_call["png_bytes"]) == CORRECT_SOURCE_FPS_FRAME
    assert _decode_index(second_call["png_bytes"]) == SECOND_CORRECT_SOURCE_FPS_FRAME
