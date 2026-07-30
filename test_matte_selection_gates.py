# Last modified: 2026-07-28 | Change: TDD tests for the new matte-deliverable selection gates (matte_ck_master/matte_garbage/matte_sam_junk/matte_ck_alpha) | Full history: git log
"""Behavioral tests for the four matte-deliverable selection gates.

WHAT THIS PROVES: four independent booleans decide which matte deliverables get
WRITTEN to disk. Unchecked = the folder is never created and no PNG is encoded.
This file exercises `_write_fusion_sidecars` directly (the CK_ALPHA + SAM_JUNK
gates) against real tmp-dir filesystem writes with synthetic float32 arrays --
no mocking of cv2 or the write path itself, so a broken gate shows up as a real
missing/present folder on disk, not a source-text match.

The other two gates (matte_ck_master -> CK_ONLY, matte_garbage -> GARBAGE_MATTE)
live inside cmd_batch's per-frame loop, which only runs after a real NN engine +
SAM2 video predictor are built (see test_sam_source_frame_alignment.py for how
heavy that mocking is) -- out of scope for this file; the task brief scopes the
behavioral tests to _write_fusion_sidecars + the DEFAULT_SETTINGS contract.
CK_ONLY/GARBAGE_MATTE gating was verified by direct code read instead
(ae_processor.py cmd_batch, CK_ONLY block ~3306-3322, GARBAGE_MATTE block
~3323-3341).
"""
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parent
CEP_PANEL_DIR = ROOT / "ae_plugin" / "cep_panel"
sys.path.insert(0, str(CEP_PANEL_DIR))

import ae_processor  # noqa: E402  (sys.path insert above must precede this import)


def _synthetic_alpha(seed):
    """8x8 float32 mono 0..1 array, distinct per seed so the CK_ALPHA and SAM_JUNK
    inputs are never accidentally identical (would hide a swapped-argument bug)."""
    rng = np.random.RandomState(seed)
    return rng.rand(8, 8).astype(np.float32)


# WHAT IT DOES: Pins the shipped defaults -- these values were handed down as
#   NOT up for redesign (CK_ONLY/GARBAGE_MATTE stay on by default, matching
#   today's always-on behavior; SAM_JUNK/CK_ALPHA start off).
# DEPENDS ON: ae_processor.DEFAULT_SETTINGS.
# AFFECTS: Every render that doesn't explicitly set these four keys.
def test_default_settings_matte_flags_are_true_true_false_false():
    assert ae_processor.DEFAULT_SETTINGS["matte_ck_master"] is True
    assert ae_processor.DEFAULT_SETTINGS["matte_garbage"] is True
    assert ae_processor.DEFAULT_SETTINGS["matte_sam_junk"] is False
    assert ae_processor.DEFAULT_SETTINGS["matte_ck_alpha"] is False


# WHAT IT DOES: Both matte_ck_alpha and matte_sam_junk ON (the historical
#   always-on behavior for these two, before this feature existed) must still
#   produce the exact same folder + filename layout _write_fusion_sidecars has
#   always produced -- including the per-folder _00000 dummy on the first frame.
# DEPENDS ON: ae_processor._write_fusion_sidecars.
# AFFECTS: Any host-side Fusion-comp / AE-layer wiring that expects these exact paths.
def test_write_fusion_sidecars_all_flags_true_matches_todays_filenames(tmp_path):
    settings = {**ae_processor.DEFAULT_SETTINGS, "matte_ck_alpha": True, "matte_sam_junk": True}
    ck_alpha = _synthetic_alpha(1)
    sam_union = _synthetic_alpha(2)

    ae_processor._write_fusion_sidecars(ck_alpha, sam_union, settings, tmp_path,
                                         seq_num=7, is_first=True)

    assert (tmp_path / "CK_ALPHA" / "CK_ALPHA_00007.png").exists()
    assert (tmp_path / "CK_ALPHA" / "CK_ALPHA_00000.png").exists()
    assert (tmp_path / "SAM_JUNK" / "SAM_JUNK_00007.png").exists()
    assert (tmp_path / "SAM_JUNK" / "SAM_JUNK_00000.png").exists()


# WHAT IT DOES: Both flags OFF must mean the folders are never even created --
#   not just empty of PNGs, per the brief ("the folder is never created and no
#   PNG is encoded").
# DEPENDS ON: ae_processor._write_fusion_sidecars.
# AFFECTS: Disk footprint / host-side folder scans when Berto unchecks a deliverable.
def test_write_fusion_sidecars_all_flags_false_creates_no_folders(tmp_path):
    settings = {**ae_processor.DEFAULT_SETTINGS, "matte_ck_alpha": False, "matte_sam_junk": False}
    ck_alpha = _synthetic_alpha(3)
    sam_union = _synthetic_alpha(4)

    ae_processor._write_fusion_sidecars(ck_alpha, sam_union, settings, tmp_path,
                                         seq_num=7, is_first=True)

    assert not (tmp_path / "CK_ALPHA").exists()
    assert not (tmp_path / "SAM_JUNK").exists()


# WHAT IT DOES: A settings dict that predates this feature (no matte_* keys at
#   all -- e.g. an in-flight render launched with an old params.json) must not
#   crash, and must fall back to the shipped default for each flag (False for
#   both CK_ALPHA and SAM_JUNK -- see the DEFAULT_SETTINGS test above).
# DEPENDS ON: ae_processor._write_fusion_sidecars's settings.get(..., default) reads.
# AFFECTS: Backward compatibility for any caller/params.json written before this change.
def test_write_fusion_sidecars_missing_keys_falls_back_to_default_without_crashing(tmp_path):
    settings = {}  # deliberately bare -- no matte_* keys, no other DEFAULT_SETTINGS keys
    ck_alpha = _synthetic_alpha(5)
    sam_union = _synthetic_alpha(6)

    ae_processor._write_fusion_sidecars(ck_alpha, sam_union, settings, tmp_path,
                                         seq_num=3, is_first=False)

    assert not (tmp_path / "CK_ALPHA").exists()
    assert not (tmp_path / "SAM_JUNK").exists()


# WHAT IT DOES: A non-boolean truthy/falsy value (e.g. 1/0, as a hand-edited or
#   older-format params.json checkbox value might serialize) must be tolerated,
#   not raise -- the defensive style this file already uses for its other
#   settings reads (e.g. despeckle/choke clamping in load_settings).
# DEPENDS ON: ae_processor._write_fusion_sidecars's settings.get(..., default) reads.
# AFFECTS: Robustness against a hand-edited or older-format params.json.
def test_write_fusion_sidecars_tolerates_non_boolean_flag_values(tmp_path):
    settings = {"matte_ck_alpha": 1, "matte_sam_junk": 0}
    ck_alpha = _synthetic_alpha(7)
    sam_union = _synthetic_alpha(8)

    ae_processor._write_fusion_sidecars(ck_alpha, sam_union, settings, tmp_path,
                                         seq_num=2, is_first=False)

    assert (tmp_path / "CK_ALPHA" / "CK_ALPHA_00002.png").exists()
    assert not (tmp_path / "SAM_JUNK").exists()
