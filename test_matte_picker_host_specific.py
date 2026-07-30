# Last modified: 2026-07-28 | Change: Pin AE immunity to the Premiere matte picker | Full history: git log
"""Contract: the matte picker is a PREMIERE feature and must never gate AE's passes.

The picker decides which mattes get their own Premiere video track, and two of its four
toggles ship OFF so the Premiere timeline stays exactly as it is today. Those same
settings keys also gate what the ENGINE writes to disk, which is the trap: AE's
ae_createSAMPrecomp consumes CK_ALPHA (its "CK MATTE") and SAM_JUNK (its "GARBAGE MASK")
on every render. If the Premiere-side defaults reached the engine while keying from AE,
two layers would silently vanish from an AE precomp nobody asked us to change.

So ckGetSettings sends the toggle values only when HOST_APP is 'ppro'; under AE all four
are forced true, which is today's behavior. ExtendScript and CEP cannot run headless, so
this is a source contract; the runtime counterpart is an AE precomp that still contains
CK MATTE and GARBAGE MASK after a render.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent
PANEL_PATH = ROOT / "ae_plugin" / "cep_panel" / "index.html"

MATTE_KEYS = ("matte_ck_master", "matte_garbage", "matte_sam_junk", "matte_ck_alpha")


def _panel():
    return PANEL_PATH.read_text(encoding="utf-8")


# WHAT IT DOES: Verifies the settings object sends host-adjusted values, not raw toggles.
# DEPENDS ON:   ckGetSettings computing *_eff from HOST_APP before building settings.
# AFFECTS:      Fails when an AE render could be gated by Premiere's picker defaults.
def test_settings_send_host_adjusted_matte_values():
    panel = _panel()
    for key in MATTE_KEYS:
        raw = "%s: %s," % (key, key)
        eff = "%s: %s_eff" % (key, key)
        assert eff in panel, (
            f"{key} is no longer host-adjusted; AE would inherit Premiere's picker default"
        )
        assert raw not in panel, (
            f"{key} is sent raw from the toggle; under AE that strips a precomp layer"
        )


# WHAT IT DOES: Verifies every matte pass is forced ON when the host is not Premiere.
# DEPENDS ON:   the _isPpro ternaries defaulting to true on the AE branch.
# AFFECTS:      Fails when an AE precomp would lose CK MATTE or GARBAGE MASK.
def test_ae_forces_every_matte_pass_on():
    panel = _panel()
    assert "const _isPpro = (HOST_APP === 'ppro');" in panel, (
        "the host check that protects AE is gone"
    )
    for key in MATTE_KEYS:
        frag = "const %s_eff = _isPpro ? %s : true;" % (key, key)
        assert frag in panel, (
            f"{key} no longer falls back to true under AE; that is an AE regression"
        )


# WHAT IT DOES: Verifies the plan handed to Premiere is built from the toggles themselves.
# DEPENDS ON:   ckBuildMattePlan reading settings.matte_* and pairing each with a path.
# AFFECTS:      Fails when a ticked matte would not reach a Premiere track.
def test_matte_plan_covers_all_four_keys():
    panel = _panel()
    start = panel.index("function ckBuildMattePlan")
    body = panel[start:start + 1200]
    for key, label in zip(
        MATTE_KEYS, ("CK MASTER", "GARBAGE MASK", "SAM JUNK MASK", "CK ALPHA")
    ):
        assert "settings.%s" % key in body, f"{key} is not consulted when building the plan"
        assert "'%s'" % label in body, f"{label} is not an entry the plan can produce"
