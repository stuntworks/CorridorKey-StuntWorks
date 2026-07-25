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
