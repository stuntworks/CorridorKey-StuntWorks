# CorridorKey Review — Save Queue / Preview Paths

Timestamp: 2026-05-14 18:00:21 -04:00
Reviewer: Codex
Mode: Review only
Code changed: None

## Scope Reviewed

This was a static code review only. No fixes were applied.

Files reviewed:
- C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py
- C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\preview_viewer_v2.py

Code paths reviewed:
- PROCESS RANGE worker thread _run()
- _save_queue enqueue/drain path
- on_poll_timer() queue drain path
- single-frame preview path process_current_frame()
- single-frame combined export path
- BRAW and non-BRAW range combined export paths
- live viewer ender_composite() and Matte render path
- SAM apply / reload path in the viewer

## Review Summary

### 1. PROCESS RANGE save bug — confirmed

This bug is real.

Why:
- The worker thread enqueues frame saves and the final import task with _save_queue.put(...).
- The only consumer for that queue is the Fusion PollTimer drain in on_poll_timer().
- Inside the worker thread, the code calls _QApp.processEvents(), which pumps Qt events, not the Fusion UIManager timer loop.
- Static review supports the observed symptom: the loop can process all frames, but disk saves/import can stall because the queue drain is not being driven reliably during the run.

Reviewed locations:
- CorridorKey.py lines around 3721-4080
- CorridorKey.py lines around 4274-4288

### 2. Composite preview broken — confirmed

This bug is real.

Why:
- Single-frame preview intentionally sends CK-only alpha to the viewer.
- Single-frame export in Combined mode can run a SAM merge before saving.
- That means the live Composite preview is not showing the same alpha math as the saved Combined result.

Reviewed locations:
- CorridorKey.py lines around 2589-2664
- preview_viewer_v2.py lines around 299-374
- preview_viewer_v2.py lines around 1347-1384

### 3. Combined output kills CK body detail — confirmed

This bug is real.

Why:
- Single-frame Combined export uses _panel_dispatch_sam2_combine(...), which preserves the per-mask dispatch behavior.
- Range export paths still bypass that and call pply_sam2_gate_subtract(...) directly on a union SAM mask.
- That mismatch is a user-facing behavior difference and fits the reported loss of body detail.

Reviewed locations:
- CorridorKey.py lines around 2619-2649
- CorridorKey.py lines around 3641-3646
- CorridorKey.py lines around 3986-3991

### 4. SAM matte viewer first display is wrong — likely confirmed by code path

The first-display path is definitely divergent.

Why:
- _apply_sam_mask() in the viewer overwrites lpha.png with the raw SAM silhouette and reloads it immediately.
- That means the first picture after applying SAM is not a re-keyed CK result and not the final CK×SAM combined result.
- I did not run Resolve live in this review, so I am not claiming I reproduced the exact “outline-only” image, but the code path is clearly not showing the final intended render.

Reviewed locations:
- preview_viewer_v2.py lines around 1593-1653
- preview_viewer_v2.py lines around 241-269

### 5. Live margin / soften currently affect final alpha globally

This is important to the body-detail complaint.

Why:
- In the viewer, margin and soften are applied to the final lpha, not only to the SAM gate.
- That can fatten the full body silhouette and eat fine CK edge detail on hair, garments, and prop edges.

Reviewed locations:
- preview_viewer_v2.py lines around 311-326
- preview_viewer_v2.py lines around 1365-1379

## Review Call

Priority order from this review:
1. PROCESS RANGE save queue bug
2. Composite preview mismatch
3. SAM first-display path
4. Range combined dispatch mismatch / body-detail loss

## Notes

- This review was based on static code inspection only.
- No code was modified.
- No runtime verification inside DaVinci Resolve was performed in this review pass.

---

## Addendum

Timestamp: 2026-05-14 18:02:27 -04:00
Reviewer: Codex
Mode: Review only
Code changed: None

Reason for addendum:
- Original file was saved successfully.
- A formatting glitch in the first write mangled one line in the scope section.
- This addendum preserves the original file and adds the corrected summary without overwriting anything.

Reviewed code:
- C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py
- C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\preview_viewer_v2.py

Reviewed paths / behavior:
- PROCESS RANGE worker thread _run()
- _save_queue enqueue and on_poll_timer() drain path
- single-frame preview path process_current_frame()
- single-frame Combined export path
- BRAW and non-BRAW range Combined export paths
- live viewer ender_composite() Composite path
- live viewer Matte path
- SAM apply / reload path in the viewer

Review call:
1. PROCESS RANGE save queue bug is confirmed.
2. Live Composite preview does not match the actual Combined export math.
3. Range Combined still bypasses the per-mask dispatch used by single-frame Combined.
4. The first SAM display path in the viewer is divergent from the final intended CK+SAM render path.
5. Live margin / soften are currently being applied to the final alpha, which can eat CK body detail.

Notes:
- Static review only.
- No code modified.
- No live Resolve runtime verification performed in this review pass.

## Addendum Correction

Timestamp: 2026-05-14 18:02:50 -04:00
Reviewer: Codex
Mode: Review only
Code changed: None

Correction:
- The previous addendum had one PowerShell markdown-escaping glitch.
- Correct reviewed path list item:
  live viewer render_composite() Composite path

This correction is append-only. Nothing above was overwritten.

---

# REVIEW (Claude) — appended

**Timestamp:** 2026-05-14 17:59 EDT
**Reviewer:** Claude (Opus 4.7, 1M context), invoked via Claude Code
**Mode:** Read-only review at Berto's request ("review dont edit"). No code changed. Codex's review and addenda above were already in this file when I opened it; I did not modify them.

## What I reviewed

### Source under review
- File: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
- Live file mtime at review time: `2026-05-14 17:17 EDT`, 274,103 bytes
- Source repo: `D:\New AI Projects\CorridorKey\resolve_plugin\CorridorKey_Pro.py` (working tree dirty — uncommitted edits present)
- Branch: `test/issue-c-revert`
- Repo HEAD at review time: `e2cdce0a fix(single-frame): route Combined export through _panel_dispatch_sam2_combine (per-mask dilate_into)`
- Last deploy stamp per memory: `2026-05-14_064816` (live mtime is later — a subsequent deploy ran today)

### Claim under review (from prior Claude session output, paraphrased)
> #1 PROCESS RANGE save bug, EVIDENCE LOCKED. Probe shows loop ran all 21 frames, queue.put fired 21 times. Disk shows only frame 086493 saved. The PollTimer drain at line 4274 only fires once during _run, drains exactly one item, then doesn't fire again. Other 20 PNG saves + MediaPool import sit in the queue forever. _QApp.processEvents() is Qt, PollTimer is Fusion UIManager. Different event systems. Fix: drain _save_queue inline from inside _run instead of relying on PollTimer.

### Code locations I read in `CorridorKey.py`

| What | Line |
|---|---|
| `_save_queue = queue.Queue()` definition | 146 |
| PollTimer Timer widget definition | 394 |
| `items["PollTimer"].Start()` | 469 |
| `_save_queue.put(("save", ...))` for CK PNG | 4031 |
| `_save_queue.put(("save", ...))` for SAM matte sidecar | 4047 |
| `_save_queue.put(("import", ...))` import task | 4080 |
| `QApplication.processEvents()` between frames | 4063 |
| Path B comment: `_run()` called directly on main thread | 4107-4116 |
| `on_poll_timer` definition | 4143 |
| `_save_queue` drain (`while not _save_queue.empty():`) | 4274 |
| Drain body (save + import handling) | 4275-4288 |
| Adaptive timer interval logic | 4314-4324 |
| `win.On.PollTimer.Timeout = on_poll_timer` binding | 4518 |

I did NOT read `preview_viewer_v2.py` this pass. Codex's preview/viewer findings are not independently verified by me.

## Findings

### #1 PROCESS RANGE save bug — diagnosis is PARTLY WRONG, fix direction is RIGHT

**What the prior session got right** (and Codex confirmed):
- `_run()` runs synchronously on the main thread (Path B is implemented per comment at line 4107: "Path B (2026-05-13): kill the worker thread, run _run on the main thread"). Confirmed at line 4116: bare `_run()` call.
- `QApplication.processEvents()` (line 4063) and Fusion UIManager `PollTimer` (line 394) ARE different event systems.
- While `_run` blocks the main thread, Fusion UIManager timer callbacks cannot fire normally.

**What the prior session got wrong** (I disagree with both the prior session's "exactly one item" claim AND Codex's "confirmed" verdict on that specific mechanism):

**Claim:** "drains exactly one item, then doesn't fire again."

Code at line 4274 is `while not _save_queue.empty():` with no `break` and no early return. When `on_poll_timer` fires ONCE, it drains EVERY item currently in the queue, not one. The "one item drained" inference does not match the code.

**Implication for the symptom (1 PNG on disk):**

The "1 PNG on disk + 21 queue.puts" symptom is consistent with multiple alternative mechanisms:

1. Drain fired once early in `_run` (when only item 1 was queued), then never fired again because PollTimer doesn't fire while main thread is blocked. After `_run` completes, drain fires and processes items 2-21, but each save silently fails (`except Exception: pass` at line 4287 swallows errors after the inner `log(...)` at 4283).
2. Drain never fired during `_run` at all. After `_run` returns, drain fires and writes 1 PNG, then crashes on item 2 (e.g., disk full, write to a path that's gone stale), and the outer `except Exception: pass` swallows it.
3. Drain fired correctly and wrote all 21, but 20 of them landed somewhere else (path mutation between iterations, race with the import task).

The probe data referenced ("queue.put fired 21 times") proves the **producer** side of the queue. Without a probe inside the drain loop at line 4276, we don't know which mechanism is actual.

**Fix direction — agree with prior session and Codex.** The proposed inline-save fix is the right shape. Cleanest minimal change: replace `_save_queue.put(("save", ...))` calls at lines 4031 and 4047 with direct disk writes inside the loop. The import task at line 4080 also runs on the main thread (it must, for MediaPool API), and since `_run` IS the main thread under Path B, it can be inlined too.

The `_save_queue` mechanism predates Path B — it was designed for the worker-thread architecture, where thread-side code couldn't write to disk safely under Defender. Now that `_run` runs on the main thread, the queue indirection adds complexity without benefit. Inlining is a simplification, not just a workaround.

### #2 SAM matte outline-only

I did not read viewer code this pass. Plausible based on prior pattern (same class as 2026-05-06 bug per `corridorkey_two_view_paths_duplicate_logic`). Codex's review above gives a more concrete site (`preview_viewer_v2.py` ~1593-1653 — `_apply_sam_mask` overwriting `alpha.png` with raw SAM silhouette and reloading immediately). Codex's read is more specific than mine and is consistent with the symptom; I trust it pending live verification.

### #3 Combined output kills CK body detail

Codex found a concrete dispatch-mismatch site: range export paths call `apply_sam2_gate_subtract` directly on a union SAM mask (lines around 3641-3646 and 3986-3991), while single-frame Combined goes through `_panel_dispatch_sam2_combine` (per-mask dispatch). I did not independently verify those line numbers but the architectural mismatch is the same one called out in the latest handoff (`session-handoff-2026-05-14-ck-single-frame-dispatch-working` item c). Diagnosis is sound.

Codex also flagged a related issue (margin/soften applied to final alpha globally rather than only to the SAM gate). If true, that's a separate amplifier of the body-detail loss — I would treat it as a sub-finding of #3, not a separate bug.

### #4 Composite preview broken — disagreement with Codex

The prior Claude session called this "intentional UI text" because the screenshot label literally says "Reference look only — output is still two separate mattes." I agreed with that reading.

Codex disagrees and calls it a real bug because the live composite preview is showing CK-only alpha math while the saved Combined output runs SAM merge. That mismatch IS real — Codex is right that the math diverges. But that does not necessarily make the LABEL wrong; the label may have been added precisely because the developer knew the preview alpha differs from the saved alpha and wanted to warn the user.

So both readings have merit:
- Codex: math mismatch is a bug (preview should match output)
- Prior session / Claude: the label communicates the known limitation; making preview = output is a feature request, not a bug fix

This is a Berto product call: do you want the live preview to RUN the SAM merge (slower, slows scrub) so it matches the saved Combined output? Or keep the cheap CK-only preview with a label noting the limitation? Either is defensible.

### #5 BRAW + non-BRAW dispatch wiring

Already documented in handoff `session-handoff-2026-05-14-ck-single-frame-dispatch-working` as architectural cleanup. No user-facing impact for single-frame. Same conclusion as Codex's #3 finding (which subsumes this).

## Recommendation on prioritization

Agree with prior session and Codex: attack #1 first. Without saves working, you cannot validate #2/#3/#4 across a range — each fix would have to be eyeballed on a single frame, which cannot catch motion-related regressions.

**But before patching #1, add ONE probe inside the drain loop at line 4276** to capture (a) when on_poll_timer fires during `_run`, (b) how many items it sees per fire, (c) whether each save actually wrote bytes. One probe, one PROCESS RANGE run, then pick the fix based on what the probe shows. Otherwise the inline-save patch could ship without solving the actual bug if root cause is elsewhere (e.g., file write failing for path/permission reasons unrelated to the queue).

## Where Claude's review differs from Codex's

| Topic | Codex | Claude |
|---|---|---|
| #1 "drains exactly one item" | Confirmed | Wrong - line 4274 is a `while` loop that drains all items |
| #1 fix direction | Inline drain | Inline producer (drop the queue for saves entirely) - same intent, slightly different angle |
| #4 Composite preview | Real bug (alpha math mismatch) | Possibly intentional (label warns user) - Berto product call |
| Viewer findings (#2, #4, #5 in Codex's list) | Read directly | Did not read this pass - defer to Codex on viewer code |
| Add probe before patching #1 | Not mentioned | Recommended - current probe data is producer-side only |

## What this review does NOT cover

- I did not run any code or test cases.
- I did not read `_panel_dispatch_sam2_combine` or the per-mask `dilate_into` extension.
- I did not read `preview_viewer_v2.py`.
- I did not read the source-tree (working-copy) version of `CorridorKey_Pro.py`. The live file `CorridorKey.py` is what I reviewed because that is what Resolve runs.
