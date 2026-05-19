# Handoff: SAM2 video propagation tracking the wrong object on SCRUB RANGE

**Date:** 2026-04-27 (~01:00 local)
**Status:** Single-frame SAM2 confirmed working with 3-5 dots. Multi-frame SCRUB RANGE produces masks that DON'T follow the actor.

## The actual bug (verified 2026-04-27)

Looked at the per-frame SAM2 gates from a SCRUB RANGE run on Berto's stunt footage. Gates are at `corridorkey_session/scrub/{NNN}/sam2_gate_raw.png`. **The gate is segmenting the GREEN SCREEN RECTANGLE, not the actor.** The actor appears as a BLACK silhouette inside the white "mask" — the inverse of what we want.

Confirmed by:
- Reading frame 0's gate: shows the green-screen rectangle white, actor black
- Reading frame 5's gate: same — green-screen rectangle, actor as a hole
- Centroid of gate at (1980, 760) at 4K — that's roughly the green-screen area, NOT where the click points were placed in image coordinates

Cross-checked with Berto's verbal description: "It jumped around to different frames... walls came in and went away... selection is not the same as what's coming up." Matches: SAM2 is segmenting whatever happens to be at the click coordinates on each anchor frame, which is the green-screen wall when the actor isn't there.

## Root cause

Click points are stored as **image coordinates** on a specific timeline frame (`sam_anchor_frame`, e.g. 86423 from `live_params.json`). The points are valid ONLY at that frame. When SCRUB RANGE runs:

1. `on_scrub_range` (~line 2200, `CorridorKey_Pro.py`) exports the scrub range frames to a temp dir.
2. It computes `anchor_rel` as the closest frame in the range to `sam_anchor_frame` (`CorridorKey_Pro.py:2237`).
3. If the click frame isn't in the range, it picks the nearest frame — but the actor on that frame is in a different position. The click coordinates now land on green screen.
4. `run_sam2_video_propagation` adds the points at frame `anchor_rel`, then propagates. SAM2 segments whatever happens to be at those coordinates → the green-screen rectangle.

The single-frame APPLY MASK works because it runs SAM2 directly on `fg.png` from the same frame the user clicked — coordinates align with the actor.

## What's working / what's not

Working:
- Single-frame APPLY MASK (with 3-5 dots, the matte is clean)
- NN-alone alpha (the tan-vest HSV bug from earlier today is fixed in `generate_alpha_hint`)
- Trimap fusion / garbage matte logic in the viewer

Not working:
- SCRUB RANGE when the click frame isn't inside the scrub range
- (Likely also broken: PROCESS RANGE in the same scenario — uses the same propagation path)

## Tomorrow's fix plan (in priority order)

**UPDATE 2026-04-27 ~01:30:** Berto verified the diagnosis with a clean test (4 dots, no playhead move → scrub worked perfectly — masks tracked the actor across the range). Then he tried to refine: clicked a red exclude dot to remove leftover floor, the playhead moved, and the mask broke again. This proves the bug has two parts:

1. The global `sam_anchor_frame` model is wrong. Each click point needs to remember which frame it was placed on, not share one global frame.
2. Re-clicking AFTER an initial scrub overrides the original anchor with the new playhead frame — invalidating all previously-placed dots.

Berto's words: *"we have to figure out how to keep it in sync, even if we change it after the fact."*

### Option A (still the right first ship) — block the bad scenario
Same as before. In `on_scrub_range` / `on_process_range`, check if `sam_points["frame"]` is inside the scrub range; refuse with a clear message if not.

### Option B (now the REAL real fix) — per-dot frame tracking
Change the data model so each click point stores its own frame number.

**`live_params.json` schema change:**
```jsonc
// Old (current):
"sam_positive": [[2131, 568], [2045, 1534], [2403, 1330]],
"sam_negative": [],
"sam_anchor_frame": 86423   // one global frame for ALL points

// New:
"sam_clicks": [
  {"x": 2131, "y": 568, "label": 1, "frame": 86423},
  {"x": 2045, "y": 1534, "label": 1, "frame": 86423},
  {"x": 2403, "y": 1330, "label": 1, "frame": 86423},
  {"x": 1820, "y": 1900, "label": 0, "frame": 86440}    // red dot added later on different frame
]
// Keep sam_positive / sam_negative / sam_anchor_frame as fallback for back-compat with reading old live_params.json
```

**Viewer (`preview_viewer_v2.py`) changes:**
- `_sam_display_pts` already stores per-click metadata (normalized x/y + label). Add `frame_num` to each click record at click time, sourced from `meta.json` at the moment of the click.
- `_save_live_params_now` writes the new `sam_clicks` array. Also writes the legacy fields (computed from sam_clicks) so old code paths still work for the immediate APPLY MASK display.

**Panel (`CorridorKey_Pro.py`) changes:**
- `_merge_live_params` reads `sam_clicks` if present, falls back to the legacy fields.
- `run_sam2_video_propagation` takes a list of `(frame, points, labels)` groupings instead of `(pos_pts, neg_pts, anchor_frame_abs)`.
  - For each unique frame in the click list, call `predictor.add_new_points_or_box(inference_state=state, frame_idx=group_rel, obj_id=1, points=group_pts, labels=group_labels, clear_old_points=False)`.
  - Then `propagate_in_video(state)` once forward and once backward from the EARLIEST anchor frame.
- The temp dir for SAM2 must include EVERY frame that has clicks, plus all the scrub range frames. Click-frames outside the scrub range get exported but their masks are dropped from the returned dict.

**SAM2 video predictor confirmation:** `add_new_points_or_box` with `clear_old_points=False` is the documented way to add new clicks without losing prior memory. State persists across calls. This is exactly what's needed.

### Option C (UX nicety, after B works)
After scrub, when the user adds a refining click: detect that propagation already happened. Re-run propagation incrementally instead of from scratch. SAM2 supports this — just call `add_new_points_or_box` on the existing state and re-propagate. Saves the 30-60s per re-scrub.

## Files to change

- `CorridorKey_Pro.py:2200-2256` — `on_scrub_range` SAM2 propagation block
- `CorridorKey_Pro.py:2470-2515` — BRAW PROCESS RANGE SAM2 propagation block
- `CorridorKey_Pro.py:2660-2700` — third propagation call site (likely standard PROCESS RANGE)
- `CorridorKey_Pro.py:711-859` — `run_sam2_video_propagation` itself (Option B requires extending the input contract)

## Test plan

After Option A ships:
1. Click on frame N, scrub range NOT containing N → should get a clear refusal with helpful message.
2. Click on frame N, scrub range containing N → should still work (no regression).

After Option B ships:
1. Click on frame N (at far end of timeline), scrub range NOT containing N → masks should follow the actor through the scrub range.
2. Single-frame APPLY MASK should still work unchanged.
3. The temp JPEG dir should be cleaned up even on error.
4. VRAM should be released after propagation (the existing `predictor.reset_state` + `del state` + `del predictor` + `torch.cuda.empty_cache` block).

## What was done tonight (already shipped, do NOT redo)

1. **HSV alpha-hint bug fixed** (`CorridorKey_Pro.py:610` — `generate_alpha_hint`). HSV [35-85] was flagging tan/khaki vest as screen color. Switched to AE's RGB chroma test. NN-alone matte now solid.
2. **Trimap fusion / garbage matte in the viewer** (`preview_viewer_v2.py` `_trimap_fuse`). Currently does binary close at 101px + Gaussian feather + multiply. Works fine for single-frame SAM2.
3. **Saturation ramp on SAM2 logits** (`preview_viewer_v2.py:1693`). `clip(0.5 + L*0.25, 0, 1)` on the full-res `masks[best_idx]`. Replaces the prior `low_res_masks` sigmoid path that had banding.
4. **`/kimi` skill set up** (`D:\AI\skills\claude_to_kimi.md` + `~/.claude/commands/kimi.md`). Used during this session for cross-validation. K2.6 = `moonshotai/kimi-k2-thinking`.

## Known unrelated issues

- The viewer's trimap fusion logic is now "over-engineered" given that NN-alone is solid. Could be simplified back to a plain `alpha_raw * gate` multiply with a 5px gate feather. Tomorrow.
- File `inference_engine.py` has a pre-existing uncommitted change (per the older session's note). Not investigated tonight.

---

## 2026-04-27 morning continuation — SCRUB stunt fall tracking issue (MID-DIAGNOSIS)

After last night's per-click frame tracking ship (multi-frame click_groups for PROCESS RANGE, soft saturation ramp on propagation logits), Berto tested SCRUB on a 62-frame stunt fall. Max Frames was 10 → subsampled to 10 frames evenly spaced (gap of ~7 frames between samples).

**Three observed problems on the subsampled scrub:**
1. Frame 1 (start, standing): floor between feet INCLUDED in matte. Possibly trimap close kernel (101px) bridging foot region to nearby floor.
2. Frame 5 (mid-fall): tracking lost — only a small fragment of actor.
3. Frame 10 (end): garbage — SAM2 flipped to wrong region of frame.

**Berto's recall:** "this always worked all the way through" — three weeks of testing. He insists the failure is new today. Trust this per memory file `trust_user_memory_search_harder.md`.

**Hypotheses (priority order):**
1. **Sample gap.** 7 frames between samples + dramatic motion = SAM2 can't track. Fix: Max Frames = 0 (all 62 contiguous). **CURRENTLY BEING TESTED — Berto running it now.**
2. **My morning edits regressed propagation.** Changed `(mask_logits > 0.0)` binary → `clip(0.5 + L*0.25, 0, 1)` saturation ramp at the propagation output (preview_viewer_v2.py mask line, CorridorKey_Pro.py mask extraction in run_sam2_video_propagation around lines 910-930). **Should not affect SAM2's internal tracking** since it only post-processes returned logits, but test by reverting if hyp 1 fails.
3. **Bulk-restore yesterday morning** of `CorridorKey_Pro.py` from a `49a478bb-...740751c7288f2d02@v2` (13:58) file-history snapshot may have introduced subtle scrub bugs. Snapshot might be older than what Berto's "always worked" tests ran against.

**Decision tree for next move:**

If Max Frames = 0 fixes the tracking → done, document rule "set Max Frames to 0 for fast-motion plates."

If Max Frames = 0 still fails on the same frames → revert the saturation ramp lines in `run_sam2_video_propagation` (CorridorKey_Pro.py around lines 910-930) back to `mask = (mask_logits[0] > 0.0).squeeze().cpu().numpy().astype(np.float32)` (both forward and backward pass).

If binary revert still fails → bulk-restore concern (hypothesis 3). Compare current panel against `49a478bb-d7c2-4534-aae7-c65915e0c13f/740751c7288f2d02@v1` and earlier snapshots in `C:\Users\ragsn\.claude\file-history\`.

## Recent deploy timestamps

- `2026-04-27_012601` — last night's per-click frame tracking ship
- `2026-04-27_100556` — removed conservative SCRUB block, restored nearest-frame anchor
- `2026-04-27_101122` — saturation ramp on propagation logits (replaced binary thresholding)
- `2026-04-27_102606` — INTER_AREA + 3px Gaussian smooth in keying loop after resize

Revert: `python deploy.py --revert <timestamp>`

## File paths quick ref

- Source viewer: `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py`
- Source panel: `D:\New AI Projects\CorridorKey\resolve_plugin\CorridorKey_Pro.py`
- Live panel: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
- Deploy: `python deploy.py` (cwd: `D:\New AI Projects\CorridorKey\`)
- DaVinci debug log: `C:\Users\ragsn\AppData\Local\Temp\corridorkey_debug.txt`
- AE log: `C:\Users\ragsn\AppData\Local\Temp\corridorkey.log`
- Session dir: `C:\Users\ragsn\AppData\Local\Temp\corridorkey_session\`

## Tasks still pending in TodoWrite

#6. Deploy panel and test (in_progress) — Berto running Max Frames = 0 test
