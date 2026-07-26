# Last modified: 2026-07-26 | Change: Pin aux rate conform ahead of overwriteClip | Full history: git log
"""Static contract checks for auxiliary clip rate conform ordering.

overwriteClip bakes a placed trackItem's duration from the projectItem's footage
interpretation AT CALL TIME. Setting the rate afterward does not retroactively
rescale a clip already sitting on the timeline. When the preset-stacked branch
conformed CK MASTER / GARBAGE MASK after placing them, a 205-frame 119.88fps clip
landed V1 at 1.627s and both aux layers at 8.166s (205/24) — observed
2026-07-25T18:26:19Z, corrected 2026-07-26T05:08:50Z with all three at 1.627s.

All three import paths must conform BEFORE they place. The paths use distinct
interpretation variable names (fiSamP / fiSamN / fiSam), which is what lets each
path be located independently here.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOST_PATH = ROOT / "ae_plugin" / "cep_panel" / "jsx" / "host.jsx"

PRESET_BRANCH_START = 'if (nestSeq && _nestGeomSource === "preset") {'
PRESET_BRANCH_END = "// Place the preset-born sequence itself on the user's timeline."

AUX_ITEMS = ("samImported", "ckOnlyImported")


def _host():
    return HOST_PATH.read_text(encoding="utf-8")


def _preset_stacked_branch(host):
    """The preset-stacked placement branch only."""
    start = host.index(PRESET_BRANCH_START)
    end = host.index(PRESET_BRANCH_END)
    assert start < end, "preset-stacked branch markers found out of order"
    return host[start:end]


# WHAT IT DOES: Verifies both aux project items are conformed before any placement.
# DEPENDS ON:   The preset-stacked branch holding all three overwriteClip calls.
# AFFECTS:      Fails when aux layers would be placed at the nest's default rate,
#               landing at the wrong duration on a non-24fps clip.
def test_both_aux_items_conform_before_placement():
    branch = _preset_stacked_branch(_host())

    placements = []
    for track in (0, 1, 2):
        frag = "nestSeq.videoTracks[%d].overwriteClip" % track
        assert frag in branch, f"missing placement for videoTracks[{track}]"
        placements.append(branch.index(frag))
    first_placement = min(placements)

    for item in AUX_ITEMS:
        frag = "%s.setOverrideFrameRate(targetRate)" % item
        assert frag in branch, f"{item} is never rate-conformed in this branch"
        conform = branch.index(frag)
        assert conform < first_placement, (
            f"{item} is conformed AFTER the first overwriteClip; its placed "
            f"duration is baked from the pre-conform interpretation"
        )


# WHAT IT DOES: Verifies the branch still places all three layers on the right tracks.
# DEPENDS ON:   V1 main, V2 CK MASTER, V3 GARBAGE MASK track assignment.
# AFFECTS:      Fails when the ordering test could pass on a gutted branch.
def test_all_three_layers_are_still_placed():
    branch = _preset_stacked_branch(_host())
    expected = ((0, "imported"), (1, "ckOnlyImported"), (2, "samImported"))
    for track, item in expected:
        frag = "nestSeq.videoTracks[%d].overwriteClip(%s" % (track, item)
        assert frag in branch, f"videoTracks[{track}] no longer receives {item}"


# WHAT IT DOES: Verifies the other two import paths also conform before placing.
# DEPENDS ON:   fiSamN/fiCkoN (factory-nest) and fiSam/fiCko (flat) naming.
# AFFECTS:      Fails when a future edit regresses a path that was already correct.
def test_other_import_paths_also_conform_before_placement():
    host = _host()
    tail = host[host.index(PRESET_BRANCH_END):]

    # Factory-nest fallback: aux route to the main timeline via _auxList.
    aux_list_place = "overwriteClip(_auxList[alI]"
    assert aux_list_place in tail, "factory-nest aux placement not found"
    for conform in ("var fiSamN =", "var fiCkoN ="):
        assert conform in tail, f"factory-nest conform {conform} not found"
        assert tail.index(conform) < tail.index(aux_list_place), (
            f"factory-nest path conforms {conform} after placement"
        )

    # Flat fallback: aux placed directly onto the main sequence.
    flat = (
        ("var fiSam =", "vSam.overwriteClip(samImported"),
        ("var fiCko =", "vCko.overwriteClip(ckOnlyImported"),
    )
    for conform, place in flat:
        assert conform in tail, f"flat-fallback conform {conform} not found"
        assert place in tail, f"flat-fallback placement {place} not found"
        assert tail.index(conform) < tail.index(place), (
            f"flat-fallback path conforms {conform} after {place}"
        )
