# Last modified: 2026-07-18 | Change: Verify universal three-track Premiere preset behavior | Full history: git log
"""Static contract checks for the CEP three-track preset path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOST_PATH = ROOT / "ae_plugin" / "cep_panel" / "jsx" / "host.jsx"
PANEL_PATH = ROOT / "ae_plugin" / "cep_panel" / "index.html"


# WHAT IT DOES: Verifies the preset sequence is conformed to each rendered clip.
# DEPENDS ON:   host.jsx and index.html retaining the CEP import contract.
# AFFECTS:      Fails when a Premiere nest can silently keep seed-preset geometry.
def test_premiere_preset_uses_render_dimensions_and_rate():
    host = HOST_PATH.read_text(encoding="utf-8")
    panel = PANEL_PATH.read_text(encoding="utf-8")

    assert "sourceWidth, sourceHeight" in host
    assert "presetSettings.videoFrameWidth = targetWidth" in host
    assert "presetSettings.videoFrameHeight = targetHeight" in host
    assert "presetSettings.videoFrameRate.seconds = 1.0 / targetRate" in host
    assert "_presetSeq.setSettings(presetSettings)" in host
    assert "preset geometry verification failed" in host
    assert "function ckReadPngDimensions" in panel
    assert "renderDimensions.width, renderDimensions.height" in panel


# WHAT IT DOES: Verifies initialization cannot disable the native preset branch.
# DEPENDS ON:   The preset-state variables inside ppro_importSequence.
# AFFECTS:      Fails when native preset tracks are created but receive no clips.
def test_premiere_preset_branch_state_is_initialized_once():
    host = HOST_PATH.read_text(encoding="utf-8")
    function_body = host.split("function ppro_importSequence", 1)[1]

    initialization = function_body.index('var _nestGeomSource = "";')
    preset_assignment = function_body.index('_nestGeomSource = "preset";')

    assert initialization < preset_assignment
    assert "_nestClips" not in function_body
