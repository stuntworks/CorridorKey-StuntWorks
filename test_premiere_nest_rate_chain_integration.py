# Last modified: 2026-07-26 | Change: Pin the full 119.88fps nest rate chain | Full history: git log
"""Integration contract for the Premiere nest frame-rate chain.

Landing three layers at ONE duration on a non-24fps clip needs the whole chain in
order inside ppro_importSequence:

    bake rate into preset copy
      -> newSequence
      -> geometry verification
      -> conform BOTH aux project items
      -> place all three layers

Break any link and the failure is silent-ish: without the bake the geometry gate
refuses the nest and aux degrade to the main timeline; with the bake but without
the conform-first ordering, V1 lands at the source rate while V2/V3 land at the
nest default (205 frames -> 1.627s vs 8.166s at 119.88fps).

ExtendScript cannot run headless, so this is a source-order contract. The runtime
counterpart is the "ppro import diag" log line, where all three placed entries
must report the same durSec.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOST_PATH = ROOT / "ae_plugin" / "cep_panel" / "jsx" / "host.jsx"

# (label, distinctive source fragment) in required execution order.
CHAIN = (
    ("bake rate into preset copy", "_presetPath = _patchF.fsName"),
    ("newSequence from preset", "app.project.newSequence(_nestName"),
    ("geometry verification", "preset geometry verification failed"),
    ("conform GARBAGE MASK", "samImported.setOverrideFrameRate(targetRate)"),
    ("conform CK MASTER", "ckOnlyImported.setOverrideFrameRate(targetRate)"),
    ("place V1", "nestSeq.videoTracks[0].overwriteClip"),
    ("place V2", "nestSeq.videoTracks[1].overwriteClip"),
    ("place V3", "nestSeq.videoTracks[2].overwriteClip"),
)


def _import_sequence_source():
    host = HOST_PATH.read_text(encoding="utf-8")
    return host[host.index("function ppro_importSequence("):]


# WHAT IT DOES: Verifies every link of the nest rate chain exists, in order.
# DEPENDS ON:   ppro_importSequence containing bake, gate, conform and placement.
# AFFECTS:      Fails when a 119.88fps clip would land its three layers at two
#               different durations.
def test_nest_rate_chain_is_present_and_ordered():
    src = _import_sequence_source()

    located = []
    for label, frag in CHAIN:
        assert frag in src, f"missing chain step: {label} ({frag!r})"
        located.append((label, src.index(frag)))

    for (prev_label, prev_idx), (next_label, next_idx) in zip(located, located[1:]):
        assert prev_idx < next_idx, (
            f"chain out of order: {prev_label!r} must precede {next_label!r}"
        )


# WHAT IT DOES: Verifies the geometry gate still validates the rate it was given.
# DEPENDS ON:   The verified-rate comparison retaining a tolerance.
# AFFECTS:      Fails when the bake could mask a genuinely wrong preset instead of
#               being checked by the gate.
def test_geometry_gate_still_validates_rate_after_bake():
    src = _import_sequence_source()
    assert "Math.abs(verifiedRate - targetRate) > 0.01" in src, (
        "the geometry gate no longer verifies the sequence rate against "
        "targetRate; the bake would go unchecked"
    )
    assert "preset geometry verification failed" in src
