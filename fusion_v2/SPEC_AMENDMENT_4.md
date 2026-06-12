# SPEC AMENDMENT 4 -- CK-Green Band Mode (2026-06-12)

## Berto Correction (verbatim)

> "The butt is on green screen, there is no reason to mess with it.
>  The CK tab was PERFECT on this shot -- our pipeline (interior=ViTMatte)
>  was the thing eating the butt, not CK.
>  CK RULES WHEREVER IT CAN SEE GREEN. The solver only covers CK's true blind spots."

## Root Cause of AMENDMENT 3 Failure

AMENDMENT 3 (geometric mode) set interior unknown = W=0 = ViTMatte unconditionally.
On this shot: the subject's body IS on the green screen. CK already produced a clean key
there. ViTMatte filling interior holes was eating body mass (butt, torso) that CK had
correctly keyed. The "eaten-butt class" was our own pipeline, not a CK flaw.

## New Rules (BAND_MODE = 'ck-green')

| Zone | W | Owner |
|------|---|-------|
| Interior unknown (trimap==128, inside SAM) | 1.0 | CK verbatim -- always |
| Outer unknown + green-backed pixel | ~1.0 | CK (hair detail, semitransparent wisps) |
| Outer unknown + junk-backed pixel | ~0.0 | ViTMatte (wall, floor, dirty edge) |
| Feet zone (bottom 12% bbox) -- both sides | 0.0 | ViTMatte unconditional |
| FG (trimap==255) | passthrough 1.0 | CK |
| BG (trimap==0) | passthrough 0.0 | -- |

**Green-backed test** (outer band only): k-means (k=3) on definite-BG LAB pixels;
per-pixel Mahalanobis likelihood against green clusters using ACTUAL frame pixel colors
(not inpainted -- hair over green is already green-shifted in LAB and passes naturally).

**Feather**: Gaussian blur at interior/outer seam, sigma = 0.5% of bbox height
(~10px at 2160). Prevents hard switch line at the SAM silhouette boundary.
Pass `feather_sigma_pct=0.0` in unit tests for exact W assertions.

## Parked Modes

- `'geometric'`: interior=ViTMatte, outer=CK. Caused butt-eating on this shot.
- `'green-confidence'`: k-means W map over full band. Too much CK detail lost (AMENDMENT 3).

Both preserved behind BAND_MODE constant for gauntlet comparative evaluation.

## Implementation

```python
BAND_MODE = 'ck-green'
_FEATHER_SIGMA_PCT = 0.005   # 0.5% of bbox height

def _build_ck_green_band_map(trimap, sam_binary, frame_rgb, feet_zone_pct,
                              feather_sigma_pct=_FEATHER_SIGMA_PCT):
    ...
```

## Test Results (2026-06-12)

- Unit: 20/20 pass (6 ck-green tests + 5 trimap + 5 guided + 3 vitmatte)
- e2e 4K frame: W range [0.0000, 1.0000] mean=0.5626
  - Interior (body): W=1 -- CK untouched
  - Outer over green screen: W~1 -- CK hair detail preserved
  - Feet zone: W=0 -- ViTMatte
  - Warm solve: 826ms (includes k-means + Mahalanobis over outer band)

Commit: ae25d847
