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
    # 2026-07-28 (matte picker): the two hardcoded aux conforms became one loop over
    # the selected mattes, so the fragments below locate the loop instead of the two
    # named variables. The invariant under test is IDENTICAL — every matte is
    # conformed before the first overwriteClip — and it is now enforced for up to
    # four mattes rather than exactly two.
    branch = _preset_stacked_branch(_host())

    placements = []
    for frag in (
        "nestSeq.videoTracks[0].overwriteClip",
        "videoTracks[_trkIdx].overwriteClip(_matteSlots[_msP].item",
    ):
        assert frag in branch, f"missing placement fragment: {frag!r}"
        placements.append(branch.index(frag))
    first_placement = min(placements)

    conform_frag = "_msItem.setOverrideFrameRate(targetRate)"
    assert conform_frag in branch, "selected mattes are never rate-conformed in this branch"
    assert branch.index(conform_frag) < first_placement, (
        "mattes are conformed AFTER the first overwriteClip; their placed "
        "duration is baked from the pre-conform interpretation"
    )

    # The conform loop must cover EVERY selected matte, not just the first one.
    assert "for (var _msC = 0; _msC < _matteSlots.length; _msC++)" in branch, (
        "the conform step is no longer a loop over all selected mattes"
    )


# WHAT IT DOES: Verifies the branch still places all three layers on the right tracks.
# DEPENDS ON:   V1 main, V2 CK MASTER, V3 GARBAGE MASK track assignment.
# AFFECTS:      Fails when the ordering test could pass on a gutted branch.
def test_all_three_layers_are_still_placed():
    # 2026-07-28 (matte picker): V2/V3 are no longer hardcoded — they come from the
    # selected-matte plan. What must NOT change is the default: with no plan (an older
    # panel) or the shipped defaults, V1 is the keyed output, V2 is CK MASTER and V3 is
    # GARBAGE MASK above it, because Premiere's Track Matte Key only offers HIGHER
    # tracks in its Matte dropdown. This test now pins that default ordering at its
    # source — the legacy fallback — plus the V1 placement that never moved.
    host = _host()
    branch = _preset_stacked_branch(host)

    assert "nestSeq.videoTracks[0].overwriteClip(imported" in branch, (
        "V1 no longer receives the keyed output"
    )
    assert "videoTracks[_trkIdx].overwriteClip(_matteSlots[_msP].item" in branch, (
        "selected mattes are no longer placed on the tracks above V1"
    )

    cko = host.index('_matteSlots.push({ key: "CK MASTER", item: ckOnlyImported })')
    gm = host.index('_matteSlots.push({ key: "GARBAGE MASK", item: samImported })')
    assert cko < gm, (
        "legacy fallback order changed: CK MASTER must take V2 and GARBAGE MASK V3, "
        "so the garbage matte sits ABOVE the clip it cuts"
    )


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
