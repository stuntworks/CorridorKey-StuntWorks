# Last modified: 2026-07-26 | Change: Lock the preset frame-rate bake contract | Full history: git log
"""Static contract checks for the Premiere preset frame-rate bake.

Premiere ignores a sequence frame-rate setSettings after the sequence exists, so
the 24fps-authored CK_3TRACK.sqpreset can only serve a non-24fps clip if the rate
is baked into a COPY of the preset BEFORE app.project.newSequence runs. Without
that, the downstream geometry gate correctly refuses the nest and the import
silently degrades to aux-on-main (observed 2026-07-23: "preset geometry
verification failed: 3840x2160 @ 24; expected 3840x2160 @ 119.88011988012").
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOST_PATH = ROOT / "ae_plugin" / "cep_panel" / "jsx" / "host.jsx"
PRESET_PATH = ROOT / "ae_plugin" / "cep_panel" / "presets" / "CK_3TRACK.sqpreset"

# Premiere's fixed internal timebase, in ticks per second.
PREMIERE_TICKS_PER_SECOND = 254016000000


def _host():
    return HOST_PATH.read_text(encoding="utf-8")


def _preset_block(host):
    """The preset-creation region only: candidate resolution through its catch."""
    start = host.index("_presetTried = _presetPath ||")
    end = host.index('} catch (ePre) { nestErr = nestErr || ("preset: "')
    assert start < end, "preset block markers found out of order"
    return host[start:end]


# WHAT IT DOES: Verifies the target rate is baked into the preset copy before use.
# DEPENDS ON:   ppro_importSequence resolving _presetPath before newSequence.
# AFFECTS:      Fails when a non-24fps clip would hit an unpatched 24fps preset.
def test_rate_is_baked_before_new_sequence():
    block = _preset_block(_host())

    assert "_presetPath = _patchF.fsName" in block, (
        "no frame-rate bake in the preset block: a non-24fps clip cannot pass "
        "the geometry gate, so the preset nest is unreachable"
    )
    bake = block.index("_presetPath = _patchF.fsName")
    new_seq = block.index("app.project.newSequence(_nestName")
    assert bake < new_seq, (
        "frame-rate bake happens AFTER newSequence; Premiere bakes the sequence "
        "rate at creation, so the patch would have no effect"
    )


# WHAT IT DOES: Verifies the bake derives ticks from Premiere's timebase.
# DEPENDS ON:   PREMIERE_TICKS_PER_SECOND matching the shipped preset's units.
# AFFECTS:      Fails when the tick formula drifts and the gate silently refuses.
def test_bake_uses_premiere_tick_timebase():
    block = _preset_block(_host())
    assert "Math.round(254016000000 / targetRate)" in block, (
        "bake does not derive ticks as round(254016000000 / targetRate)"
    )


# WHAT IT DOES: Verifies the shipped preset file is read, never written.
# DEPENDS ON:   The bake writing only to a Folder.temp copy.
# AFFECTS:      Fails when a client install could have its seed preset mutated.
def test_shipped_preset_is_never_written():
    block = _preset_block(_host())

    assert "var _seedF = new File(_presetPath);" in block
    assert '_seedF.open("r")' in block, "seed preset is not opened read-only"
    assert '_seedF.open("w")' not in block, "seed preset is opened for WRITING"

    write_targets = re.findall(r'(\w+)\.open\("w"\)', block)
    assert write_targets == ["_patchF"], (
        f"unexpected write target(s) in the bake region: {write_targets}"
    )

    decl = re.search(r"var _patchF = new File\(([^)]*)\)", block)
    assert decl, "could not locate the _patchF declaration"
    assert "Folder.temp" in decl.group(1), (
        f"patched preset is not written to Folder.temp: {decl.group(1)}"
    )


# WHAT IT DOES: Verifies a 24fps bake reproduces the shipped preset's own value.
# DEPENDS ON:   CK_3TRACK.sqpreset carrying exactly one <VideoFrameRate>.
# AFFECTS:      Fails when the bake would alter established 24fps behavior.
def test_twenty_four_fps_bake_is_a_no_op():
    xml = PRESET_PATH.read_text(encoding="utf-8", errors="replace")
    rates = re.findall(r"<VideoFrameRate>(\d+)</VideoFrameRate>", xml)
    assert len(rates) == 1, (
        f"expected exactly one <VideoFrameRate> (the bake replaces the first "
        f"match only); found {rates}"
    )
    shipped_ticks = int(rates[0])
    assert round(PREMIERE_TICKS_PER_SECOND / 24) == shipped_ticks, (
        f"a 24fps bake would change the preset: round(254016000000/24)="
        f"{round(PREMIERE_TICKS_PER_SECOND / 24)} but the preset ships "
        f"{shipped_ticks}"
    )


# WHAT IT DOES: Verifies the NTSC 119.88 case bakes to an exact integer tick count.
# DEPENDS ON:   Premiere reporting 119.88 as 120000/1001.
# AFFECTS:      Fails when rounding loss would push the rate outside the gate's
#               0.01fps tolerance.
def test_ntsc_rate_bakes_to_exact_ticks():
    ntsc_119_88 = 120000 / 1001
    ticks = round(PREMIERE_TICKS_PER_SECOND / ntsc_119_88)
    # Observed in the live runtime log as "[fps-patched:2118916800]".
    assert ticks == 2118916800, ticks
    # Exact, not merely close: the round-trip must land back inside the gate.
    assert abs(PREMIERE_TICKS_PER_SECOND / ticks - ntsc_119_88) < 0.01


# WHAT IT DOES: Verifies a failed bake falls back to the shipped preset.
# DEPENDS ON:   The bake being wrapped so _presetPath survives any throw.
# AFFECTS:      Fails when a bake error could leave _presetPath unusable.
def test_bake_failure_falls_back_to_shipped_preset():
    block = _preset_block(_host())
    assert "_presetPath = _patchF.fsName" in block, "no bake to guard"
    bake = block.index("_presetPath = _patchF.fsName")
    assert "catch (eBake)" in block, "bake is not wrapped in its own catch"
    assert bake < block.index("catch (eBake)"), (
        "the bake mutation sits outside its catch"
    )
