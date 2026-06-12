# SPEC AMENDMENT 3 — Geometric Band Mode (2026-06-12)

## Berto Verdict (verbatim)

> "the green-confidence combine is NO GOOD — too much CK detail lost.
>  SAM feet look okay, so just combine it with CK and all the detail.
>  KEEP the k-means code in the file but unused behind a module-level constant
>  BAND_MODE = 'geometric' (vs 'green-confidence') with a comment that Berto
>  rejected green-confidence on 2026-06-12 — do not delete, it may return for
>  the gauntlet evaluation."

## What Changed

### W Map Construction (solver_hybrid.py)

BEFORE (green-confidence, now PARKED):
- K-means (k=3) on definite-BG LAB pixels
- W = max Mahalanobis likelihood over green-dominant clusters
- Problem: too much CK edge detail suppressed; ViTMatte bleed visible on fine hair

AFTER (geometric, now ACTIVE):
- W=1 — unknown pixel AND outside SAM binary silhouette (outer ring: hair, wisps, fine edge detail — CK owns it)
- W=0 — unknown pixel AND inside SAM binary silhouette (inner ring: interior holes / eaten-butt class — ViTMatte fills)
- W=0 — feet zone (bottom 12% of bbox), inner AND outer unknown pixels — ViTMatte unconditional

### Module-Level Switch

```python
BAND_MODE = 'geometric'  # 'green-confidence' parked — Berto rejected 2026-06-12
```

Flipping to `'green-confidence'` restores full k-means behavior for gauntlet comparison.

### Caller Changes (ae_processor.py)

Both `solve_matte` calls now pass `sam_binary=` kwarg:
- cmd_batch: `sam_binary=_sam_bin_b`
- cmd_postproc: `sam_binary=_sam_bin_pp`

### Tests (test_solver_hybrid.py)

Old k-means tests replaced with geometric tests:
- (a) FG/BG passthrough exact
- (b) Outer band → W=1 → alpha == nn_alpha (CK)
- (c) Inner band → W=0 → alpha == mock ViTMatte (0.3)
- (d) Feet zone both sides → W=0 unconditionally
- (e) Torch-free fallback: vitmatte→guided warning

## E2E Result (2026-06-12)

W map is now binary: [0.0000, 1.0000] mean=0.5674
4K frame (2160×4096): 185ms warm, 5.9s cold (includes ViTMatte model load)

Commit: 880df4cf
