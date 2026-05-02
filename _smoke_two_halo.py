"""Smoke tests for the TWO HALO design (anisotropic kernels).

HALO BODY dilates the SAM2 silhouette UPWARD and sideways (recovers hair,
butt-above-gap, fingertip wisps). HALO FEET dilates DOWNWARD (foot shadow
recovery; default 0 = tight cutoff). By construction HALO BODY cannot bleed
below the silhouette regardless of slider value — the band-below-feet bug
is gone.

Run:  .venv\\Scripts\\python.exe _smoke_two_halo.py
"""
from __future__ import annotations

import sys

import numpy as np

from sam2_combine import apply_sam2_gate


def _build_single(h: int = 800, w: int = 200) -> tuple[np.ndarray, np.ndarray]:
    """Tall scene with a SINGLE silhouette in the middle. Solid alpha so all
    gate growth is directly visible. Wider buffer above/below the silhouette
    than the largest tested halo (300) so direction tests are unambiguous.
    """
    alpha = np.ones((h, w), dtype=np.float32)
    gate = np.zeros((h, w), dtype=np.float32)
    gate[400:500, 60:140] = 1.0  # silhouette rows 400-499
    return alpha, gate


def _build_multi(h: int = 800, w: int = 200) -> tuple[np.ndarray, np.ndarray]:
    """Scene with two silhouettes (one large, one small below). Used to
    verify each silhouette gets its own anisotropic extension.
    """
    alpha = np.ones((h, w), dtype=np.float32)
    gate = np.zeros((h, w), dtype=np.float32)
    gate[200:300, 60:140] = 1.0  # main "body" silhouette
    gate[600:630, 80:120] = 1.0  # smaller "feet" silhouette
    return alpha, gate


def _outside_gate_sum(alpha_out: np.ndarray, gate_in: np.ndarray,
                      rows: slice) -> float:
    region_gate = gate_in[rows, :]
    region_alpha = alpha_out[rows, :]
    return float(region_alpha[region_gate <= 0.5].sum())


def main() -> int:
    alpha, gate = _build_single()

    # 1. halos=0 — bit-identical to alpha * gate.
    out0 = apply_sam2_gate(alpha, gate, halo_px=0, halo_body_px=0)
    if not np.allclose(out0, alpha * gate):
        print("FAIL: halos=0 not bit-identical to alpha*gate")
        return 1
    print("PASS: halos=0 bit-identical to alpha*gate")

    # 2. HALO BODY > 0 grows ABOVE the silhouette (single silhouette scene).
    out_body = apply_sam2_gate(alpha, gate, halo_px=0, halo_body_px=20)
    above = _outside_gate_sum(out_body, gate, slice(0, 400))
    if not (above > 0):
        print(f"FAIL: HALO BODY=20 should grow above silhouette (sum={above:.2f})")
        return 1
    print(f"PASS: HALO BODY=20 grows above silhouette ({above:.0f})")

    # 3. HALO BODY > 0 does NOT extend below the silhouette.
    below = _outside_gate_sum(out_body, gate, slice(500, 800))
    if below > 0:
        print(f"FAIL: HALO BODY=20 leaked below silhouette (sum={below:.2f})")
        return 1
    print("PASS: HALO BODY=20 does NOT bleed below silhouette")

    # 4. HALO BODY=300 (slider max) — band-below-feet bug should not regress.
    out_body_max = apply_sam2_gate(alpha, gate, halo_px=0, halo_body_px=300)
    below_max = _outside_gate_sum(out_body_max, gate, slice(500, 800))
    if below_max > 0:
        print(f"FAIL: HALO BODY=300 leaked below silhouette (sum={below_max:.2f})")
        return 1
    print("PASS: HALO BODY=300 (max) does NOT bleed below silhouette (band-bug fixed)")

    # 5. HALO FEET > 0 grows BELOW the silhouette.
    out_feet = apply_sam2_gate(alpha, gate, halo_px=20, halo_body_px=0)
    below_feet = _outside_gate_sum(out_feet, gate, slice(500, 800))
    if not (below_feet > 0):
        print(f"FAIL: HALO FEET=20 should grow below silhouette (sum={below_feet:.2f})")
        return 1
    print(f"PASS: HALO FEET=20 grows below silhouette ({below_feet:.0f})")

    # 6. HALO FEET > 0 does NOT extend above the silhouette.
    above_feet = _outside_gate_sum(out_feet, gate, slice(0, 400))
    if above_feet > 0:
        print(f"FAIL: HALO FEET=20 leaked above silhouette (sum={above_feet:.2f})")
        return 1
    print("PASS: HALO FEET=20 does NOT bleed above silhouette")

    # 7. Combined halos: body extends above, feet extends below, max-combined.
    out_both = apply_sam2_gate(alpha, gate, halo_px=15, halo_body_px=30)
    above_both = _outside_gate_sum(out_both, gate, slice(0, 400))
    below_both = _outside_gate_sum(out_both, gate, slice(500, 800))
    if not (above_both > 0 and below_both > 0):
        print(f"FAIL: combined halos missing growth (above={above_both:.2f}, below={below_both:.2f})")
        return 1
    print(f"PASS: combined halos: above={above_both:.0f}, below={below_both:.0f}")

    # 8. Multi-silhouette: each silhouette gets its own anisotropic extension.
    alpha_m, gate_m = _build_multi()
    out_m = apply_sam2_gate(alpha_m, gate_m, halo_px=0, halo_body_px=15)
    above_main = _outside_gate_sum(out_m, gate_m, slice(0, 200))  # above main
    above_lower = _outside_gate_sum(out_m, gate_m, slice(580, 600))  # 20 rows above lower silhouette
    below_lower = _outside_gate_sum(out_m, gate_m, slice(630, 800))  # below lower
    if not (above_main > 0 and above_lower > 0):
        print(f"FAIL: multi — both silhouettes should extend up "
              f"(above_main={above_main:.2f}, above_lower={above_lower:.2f})")
        return 1
    if below_lower > 0:
        print(f"FAIL: multi — HALO BODY bled below lower silhouette (sum={below_lower:.2f})")
        return 1
    print(f"PASS: multi — each silhouette extends up independently, no down-bleed")

    # 9. Component filter: small spurious blob is dropped before halo applies.
    #    Main body 8000 px + 50-px floor patch. Threshold = max(500, 400) = 500.
    #    Patch (50 px) < 500 → dropped. Anisotropic UP shouldn't reach above patch.
    alpha_p = np.ones((800, 200), dtype=np.float32)
    gate_p = np.zeros((800, 200), dtype=np.float32)
    gate_p[200:300, 60:140] = 1.0  # main body, 8000 px
    gate_p[400:405, 100:110] = 1.0  # tiny floor patch, 50 px
    out_filter = apply_sam2_gate(alpha_p, gate_p, halo_px=0, halo_body_px=50)
    # Above the floor patch (rows 350-399) — if patch were kept, anisotropic UP
    # would extend it up to row 350. With filter, patch dropped → no growth here.
    above_patch = _outside_gate_sum(out_filter, gate_p, slice(350, 400))
    if above_patch > 0:
        print(f"FAIL: component filter — small patch leaked upward (sum={above_patch:.2f})")
        return 1
    # Main body should still grow up unaffected.
    above_main = _outside_gate_sum(out_filter, gate_p, slice(0, 200))
    if not (above_main > 0):
        print(f"FAIL: component filter — main body lost growth (sum={above_main:.2f})")
        return 1
    print(f"PASS: component filter — small patch dropped, main body kept (above_main={above_main:.0f})")

    # 10. Component filter: lone small component is KEPT (it's the largest).
    #    A single 300-px silhouette should not be dropped just for being small.
    alpha_s = np.ones((400, 200), dtype=np.float32)
    gate_s = np.zeros((400, 200), dtype=np.float32)
    gate_s[100:130, 90:100] = 1.0  # 300-px silhouette, only component
    out_lone = apply_sam2_gate(alpha_s, gate_s, halo_px=0, halo_body_px=20)
    above_lone = _outside_gate_sum(out_lone, gate_s, slice(0, 100))
    if not (above_lone > 0):
        print(f"FAIL: lone small component dropped (above={above_lone:.2f})")
        return 1
    print(f"PASS: lone small component preserved (300-px silhouette kept, grew above by {above_lone:.0f})")

    # 11a. NEGATIVE HALO FEET shrinks silhouette from bottom.
    #     Single 100-row silhouette, HALO FEET=-30 → bottom 30 rows removed,
    #     top intact.
    alpha_n = np.ones((400, 200), dtype=np.float32)
    gate_n = np.zeros((400, 200), dtype=np.float32)
    gate_n[100:200, 80:120] = 1.0  # 100 rows tall, rows 100-199
    out_neg = apply_sam2_gate(alpha_n, gate_n, halo_px=-30, halo_body_px=0)
    out_neg_bin = out_neg > 0.5
    # Top of silhouette (row 100) should be preserved
    top_kept = int(out_neg_bin[100].sum())
    # Bottom 30 rows (170-199) should be eroded out
    bottom_eroded = int(out_neg_bin[170:200].sum())
    # Just-above-silhouette (rows 90-99) should still be 0 (erosion doesn't add)
    above_silh = int(out_neg_bin[90:100].sum())
    if top_kept == 0:
        print(f"FAIL: HALO FEET=-30 erased silhouette top row (top_kept={top_kept})")
        return 1
    if bottom_eroded > 0:
        print(f"FAIL: HALO FEET=-30 didn't erode bottom rows (sum={bottom_eroded})")
        return 1
    if above_silh > 0:
        print(f"FAIL: HALO FEET=-30 added pixels above silhouette (sum={above_silh})")
        return 1
    print(f"PASS: HALO FEET=-30 shrunk silhouette from bottom (top kept, 30 rows eroded)")

    # 11b. NEGATIVE HALO FEET removes a connected floor patch at the bottom.
    #     Body 100 rows + connected floor patch 20 rows below. With HALO FEET=-25,
    #     the patch + 5 rows of body bottom should be removed.
    alpha_p = np.ones((400, 200), dtype=np.float32)
    gate_p = np.zeros((400, 200), dtype=np.float32)
    gate_p[100:200, 80:120] = 1.0  # body
    gate_p[200:220, 90:110] = 1.0  # connected floor patch (20 rows below body)
    out_neg_p = apply_sam2_gate(alpha_p, gate_p, halo_px=-25, halo_body_px=0)
    out_neg_p_bin = out_neg_p > 0.5
    # All of patch (rows 200-219) and bottom 5 rows of body (195-199) should be eroded
    patch_eroded = int(out_neg_p_bin[195:220].sum())
    if patch_eroded > 0:
        print(f"FAIL: HALO FEET=-25 didn't remove patch + bottom 5 of body (sum={patch_eroded})")
        return 1
    print("PASS: HALO FEET=-25 removed connected floor patch")

    # 11c. Combined HALO BODY > 0 + HALO FEET < 0 — body grows above, silhouette
    #      shrinks at bottom. Both effects independent and visible.
    out_combo = apply_sam2_gate(alpha_n, gate_n, halo_px=-20, halo_body_px=30)
    out_combo_bin = out_combo > 0.5
    # Body extends above silhouette (rows 70-99)
    body_above = int(out_combo_bin[70:100].sum())
    # Silhouette bottom shrunk (rows 180-199 should be 0)
    feet_eroded = int(out_combo_bin[180:200].sum())
    if body_above == 0:
        print(f"FAIL: combined BODY=30 + FEET=-20 — body didn't grow above (sum={body_above})")
        return 1
    if feet_eroded > 0:
        print(f"FAIL: combined BODY=30 + FEET=-20 — bottom not eroded (sum={feet_eroded})")
        return 1
    print(f"PASS: combined BODY=30 + FEET=-20 (above={body_above}, bottom_eroded=0)")

    # 12. gate=None passthrough.
    out_none = apply_sam2_gate(alpha, None, halo_px=20, halo_body_px=20)
    if not np.array_equal(out_none, alpha):
        print("FAIL: gate=None should pass alpha through unchanged")
        return 1
    print("PASS: gate=None passthrough")

    # 12. dtype preserved.
    out32 = apply_sam2_gate(alpha.astype(np.float32), gate, halo_px=10, halo_body_px=20)
    if out32.dtype != np.float32:
        print(f"FAIL: dtype not preserved (got {out32.dtype})")
        return 1
    print("PASS: dtype preserved")

    print("\nAll TWO HALO anisotropic smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
