# Handoff: 2026-04-27 — Fresh start decision point

**Status as of 5pm 2026-04-27:** keying alpha matches AE quality on a static frame after a full day of fixing regressions. Berto is at the point of considering starting the resolve_plugin codebase over rather than continuing to patch.

This document is the context for that decision.

## Where we ended today

**What works right now:**
- Keying matte on a static frame matches AE's hair-strand detail (verified: screenshots 172455 DaVinci vs 172623 AE)
- Body silhouette solid, no holes on static frame
- `original.png` write fix shipped — viewer's "Original" view no longer shows stale clip
- Inline RGB alpha hint matching AE shipped (replaces HSV+morph AlphaHintGenerator)
- Fast inference startup — `torch.rand` warmup with double-replay restored
- Margin/soften slider defaults at 0/0 (gating to SAM2-only still pending)

**What's still broken (verified or reported):**
- SAM2 propagation makes the matte WORSE on Berto's stunt clip — undiagnosed
- Live preview "scanning/scaling" issue — Berto reported, not characterized
- CLEAR button in viewer corrupts state — task #7
- AE live preview "Original" button shows black mask — task #9

**Current deploy state:**
- Live panel: backup `2026-04-27_143826` (latest after alpha-hint re-fix)
- Source `CorridorKey_Pro.py` has the inline RGB hint AND the original.png write
- `inference_engine.py` has torch.rand+double warmup
- `preview_viewer_v2.py` has GATE_BRIDGE_PX=0, slider defaults 5.0/1.0 (NEEDS update to 0/0 to gate)

## What today taught us about the codebase

The day was spent fighting these structural issues, not adding capability:

### 1. Bulk-restore from backups silently undoes prior fixes
The 2026-04-26_205112 bulk-restore reintroduced the HSV+morph AlphaHintGenerator (the documented hair-killer per `corridorkey_alpha_hint_hsv_trap.md`). The fix had to be re-applied. There is no mechanism to track which fixes are present in which backup.

### 2. Parameters live in three places that disagree
- Panel UI controls (`get_settings()`)
- `live_params.json` written by viewer
- `_merge_live_params()` overrides — but only some fields

`despeckle_size: 400` default in panel, `1989` left in live_params, viewer slider showed 50. None of these was the engine's actual input. Hard to reason about.

### 3. Three propagation call sites with drifting contracts
`run_sam2_video_propagation` is called from `on_scrub_range`, BRAW PROCESS RANGE, and standard PROCESS RANGE. They were patched at different times and have slightly different argument contracts. Per-click frame tracking shipped to all three this morning but each path has its own quirks.

### 4. Engine returns "alpha" (raw) and "processed" (despeckled+despilled). Viewer reads "alpha" then despeckles AGAIN
Both AE and DaVinci viewers do this. AE's KEY CURRENT FRAME path skips the viewer-side despeckle (writes the raw NN alpha to PNG and AE shows it). DaVinci's viewer always re-despeckles. The engine's hardcoded `dilation=25, blur_size=5` makes the engine-side despeckle aggressive. The viewer's `dilation=15, blur=5` is destructive even at low area thresholds.

### 5. Viewer post-processing fires on every redraw
`render_composite` runs trimap fuse + dilate + soften + choke + despeckle on every Qt repaint. Slider defaults of margin=5, soften=1 caused the soft-body-halo bug. Setting them to 0 reveals the matte's true detail. The pipeline should be lazy/cached or have a "raw" view that bypasses post.

### 6. cv2.VideoCapture vs AE's frame extraction
Both end up at uint8 BGR via cv2 at 4K — but the path is different (DaVinci direct VideoCapture, AE via cmd_extract → PNG → cmd_single read). Subtle decode differences are possible but not confirmed.

## The fixes-of-fixes pattern

Today's fix sequence:
1. Margin/soften causing halo → set defaults to 0
2. Despeckle 1989 suspected → lowered to 50, no effect
3. Despeckle dilation=25/blur=5 in engine suspected → false alarm (AE has same)
4. AlphaHintGenerator HSV+morph identified as hair killer → inline RGB restored
5. original.png stale-clip bug found → write fixed in show_preview_window
6. Pre-viewer: discovered NN input is squashed to 2048×2048 square (red herring — both AE and DaVinci do this)

Each "fix" exposed another regression because the regressions were reintroduced by yesterday's bulk-restore. Most of today wasn't building forward — it was unwinding yesterday's revert that itself unwound the day-before's fixes.

## The case for fresh code

If we keep going on the current `CorridorKey_Pro.py` (~3,200 lines) + `preview_viewer_v2.py` (~2,000+ lines), the next session will likely repeat:
- A bug appears
- We trace through 3 code paths to find which one runs
- We patch a symptom
- The patch breaks something else through a shared cache or override

Specific architecture problems that would benefit from a rewrite:
- Single source of truth for params (one JSON file, one schema, one read site)
- Single keying entry point (compose live preview / scrub / process from one function)
- Engine returns ONE alpha (post-processed or not, but consistently)
- Viewer is a pure DISPLAY layer — never re-runs engine post-proc
- Drop the resolve_plugin/ae_plugin shared abstractions where they keep diverging — let them be separate clean implementations
- Atomic deploy with manifest of what's in each backup

What to preserve from existing code:
- `inference_engine.py` — works, tested, only minor params to expose
- `corridorkey_processor.py` — thin wrapper, fine as-is
- `deploy.py` — load-bearing, keep
- AE plugin (ae_plugin/) — simpler, more stable, less touched today
- The user's memory files — `corridorkey_alpha_hint_hsv_trap.md`, `corridorkey_sam2_dot_count.md`, `corridorkey_scrub_anchor_bug.md` are valuable anchor points

What to rewrite:
- `resolve_plugin/CorridorKey_Pro.py` — split into panel UI / state model / keying orchestrator / IPC writer
- `resolve_plugin/preview_viewer_v2.py` — strip render_composite to display-only, move all post-proc to a single non-cached function

## The case against fresh code

- Three weeks of working code goes into the bin
- New code introduces NEW bugs that haven't been caught by Berto's testing yet
- The matte is actually GOOD right now — momentum lost
- Some of the complexity in the current code IS load-bearing (e.g., the BRAW seek fallback, the cached_processor, the deploy backup chain)

## Recommendation

Don't decide tonight. Sleep on it. If you do choose to rewrite, do it during a stretch where Berto has time to NOT use the tool for keying jobs (since the rewrite will go through its own bug cycle). Don't start the rewrite reactively after another bad debug session.

If you stay on the current code, the immediate next priorities are:
1. Diagnose why SAM2 made the matte worse on this clip today
2. Gate margin/soften so they only fire when SAM2 gate is present
3. Update slider defaults to 0/0 in viewer source (currently 5/1 — works around with manual zero but defaults wrong)

## Recent deploy timestamps (today)

- `2026-04-27_012601` — last night's per-click frame tracking ship
- `2026-04-27_100556` through `2026-04-27_122305` — multiple revert/refix cycles
- `2026-04-27_133906` — original.png write fix (stale-clip bug)
- `2026-04-27_143826` — alpha-hint re-fix (HSV → inline RGB, hair detail back)

Revert any: `python deploy.py --revert <timestamp>`

## File paths

- Source viewer: `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py`
- Source panel: `D:\New AI Projects\CorridorKey\resolve_plugin\CorridorKey_Pro.py`
- Live panel: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
- Deploy: `python deploy.py` (cwd: `D:\New AI Projects\CorridorKey\`)
- DaVinci debug log: `C:\Users\ragsn\AppData\Local\Temp\corridorkey_debug.txt`
- Session dir: `C:\Users\ragsn\AppData\Local\Temp\corridorkey_session\`
- Memory: `C:\Users\ragsn\.claude\projects\C--Users-ragsn\memory\`

## Open tasks

- #7 Fix CLEAR-button corrupting viewer state
- #8 Fix viewer showing stale clip after switch (in_progress — original.png fix shipped, needs testing)
- #9 AE live preview Original button shows black mask
