# Part 13. Open Issues and Decisions

**Scope: everything currently broken, undecided, or half-verified across all three hosts. Deduped from the repo, log, and memory failure ledgers as of 2026-07-19/22. Load this before assuming any of the below is solved.**

---

## Issue: SAM dot-position lottery causes body holes; "multi-frame stamps fixed it" memory REFUTED (2026-07-22, parallel live session via bus)

Symptom: same clip, same engine, same settings render clean or with big body holes depending on ~100px shifts in the 5 anchor dot positions. SAM either keeps or drops the beige vest/waist region on the anchor frame; downstream merge inherits the roll.
Current evidence: disk A/B (this session): CK_ONLY byte-identical across 9 days, GARBAGE_MATTE frame 0 differs ~870k px between a clean 07-13 render and a holed 07-22 render of the same clip and range. Parallel session (bus reply 2026-07-22 18:43) confirms dot-position lottery as root cause. Tag: VERIFIED.
CORRECTION OF RECORD: the 07-18 memory "multi-frame stamps fixed the dropout" is FALSE. Log grep shows the stamps NEVER fired (zero "extra frame(s) registered" on 07-18). The clean 07-13/07-18 renders were single-anchor luck, not a shipped fix. Treat any claim that stamps solved garment dropout as REFUTED. Tag: FAILED APPROACH (as historical claim). Last verified: 2026-07-22.

Tested hypotheses:
- Frame-0-specific engine bug: REFUTED (CK layer identical; lottery reproduces across dot sets). 2026-07-22.
- Post-render rescue sufficient: REFUTED by parallel session, best rescue filled ~2% of gap. 2026-07-22.
- Mask-seed (feed CK whole-person silhouette to SAM via add_new_mask() as anchor prompt instead of dots, Meta-recommended for garment dropout): IN TEST in the parallel session, render in flight. Tag: HYPOTHESIS. 2026-07-22.

Failed approaches: see hypotheses; also the entire June hole-chase family in Part 04/08 graveyards.

Next test: parallel session's mask-seed render; judge by Berto's eye plus multi-clip corpus before default flip. ae_processor.py has uncommitted work from that session; no other session edits it until landed.
Files involved: ae_plugin/cep_panel/ae_processor.py (parallel session, uncommitted); SAM2 anchor path.
Owner: parallel CK session (bus id PC-5A60549E96-5240a3b3); this session = bible keeper only.
Status: OPEN, fix in flight elsewhere

Update 2026-07-24/26 (from parallel sessions via bus, tags per their own confidence):
- Arm carve reproduced OFFLINE with no host involved (126,446px, snap_20260724_100419) and found in all 7 sessions of that frame incl. AE. The carve is engine-level, NOT Premiere-only. Tag: VERIFIED (their measurement). 2026-07-24.
- PANEL VIEW LIE found and fixed 07-24: matte-sam view showed the healed solidify hull while the engine consumed the raw gate. Any pre-07-24 visual judgment of the SAM matte view is suspect. Tag: VERIFIED. 2026-07-24.
- Premiere-worse impression explained by three stacked causes: pre-fix morning renders (ghost-era fill+recolor since removed), SAM roll + hand-dot variance (8-124px deltas), and the 24fps-hardcoded nest preset REJECTING 119.88fps clips so fallback dumped raw output on V1 without the garbage-mask track. fps-bake fix in host.jsx built, UNCOMMITTED, needs full Premiere restart + live verify. Tag: PARTIALLY VERIFIED. 2026-07-24.
- Cleared at preview level: dot coordinate space (random variance only, no systematic offset), frame origin (source byte-identical across hosts), Edge V2 default (forced true both hosts, uncommitted 07-22), CK Authority (OFF both), band constants (shared). Tag: VERIFIED at preview level only. 2026-07-24.
- NOT cleared, their top suspect: Premiere RENDER-path frame math at 119.88fps (four-offset alignment family built/tested at 24fps; cd958a4 already fixed one PPro re-anchor-early bug). Tag: HYPOTHESIS. Recheck when: render-level head-to-head runs.
- Rim creep quantified: band trusts CK ~40px around body so non-green junk rims ship; every creep px within 40px, median 8px, BOTH engines BOTH hosts, Edge V2 27% worse. Tag: VERIFIED (their measurement). 2026-07-24.
- OWED TESTS: (1) post-fix RENDER-level AE vs Premiere head-to-head, never done; (2) spring the built ck_params_* snapshot trap during a live PPro render and diff vs AE.

---

## Issue: PNG-square SAM2 propagation fix REGRESSED in live AE code (found 2026-07-22 by fresh-agent bible test)

Symptom: the verified 2026-06-29 fix (feed SAM2's video predictor native unpadded JPEG q=95 frames, because square-padded PNG silently breaks temporal propagation and hollows the mask after the anchor frame) is NOT in the live AE/Premiere engine. Live code pads to square and writes PNG again at all three video-predictor call sites.
Current evidence: grep on 2026-07-22: `ae_plugin/cep_panel/ae_processor.py` calls `pad_to_square` at lines ~2392-2438 (batch/range), ~3165-3202 (scrub), ~3866-3929 (preview pre-roll); zero `IMWRITE_JPEG_QUALITY` occurrences in the live file. The correct fixed code exists in `ae_processor.py.bak-preRevert-2026-07-01` (JPEG q=95 writes at the same call sites). Resolve side (`resolve_plugin/CorridorKey_Pro.py:1660`) never regressed, still native JPEG. Tag: VERIFIED. Last verified: 2026-07-22.

Tested hypotheses:
- Regression vector = the 2026-07-01 wholesale stack-revert (which restored a working key but took the pre-fix file state with it); same revert already known to have swallowed the +_actual_preroll off-by-one fix. Tag: HYPOTHESIS. Last verified: 2026-07-22.

Failed approaches: none yet on the re-apply; the original fix itself is VERIFIED (see Part 08 chest/butt holes gotcha and Part 04 graveyard).

Runtime test 2026-07-22: Berto ran a live AE propagation-range render (aerial stunt clip, 5 dots). Body stayed SOLID on post-anchor frames. The hole symptom did NOT reproduce, despite the pad-to-square code being live. Code-level discrepancy (live pads square + PNG, backup has JPEG fix) remains real but unexplained: either propagation tolerates padding on this clip class, or another layer (dot-law fill, hole repair) absorbs it. Tag: PARTIALLY VERIFIED. Last verified: 2026-07-22.
Next test: only if hollow-after-anchor symptom ever reappears in real renders, rerun this comparison on that clip before touching code. No code change until symptom exists.
Files involved: ae_plugin/cep_panel/ae_processor.py (live); ae_processor.py.bak-preRevert-2026-07-01 (reference).
Owner: dormant until symptom appears.
Status: OPEN (downgraded from action item; symptom not reproducing)

---

## Issue: SAM2 exit ghost, frozen arm remnant after subject leaves frame (found 2026-07-22, live AE BRAW test)

Symptom: after the actor's arm exits frame left, the rendered output keeps a small frozen brown remnant at the exit point for the rest of the range. Looks like dark muck at the frame edge.
Current evidence: render ck_batch_00e9f3d5a363 (A001_02091926_C007.braw frames 193-225, unified_band ON): output frames 26-30 carry an identical ~24k px blob, bbox x 0-265, y 1575-1856, frozen across 5 straight frames. Source frame 221 at that bbox measures 100% screen green, 0% dark: nothing is there in camera. Per-layer check: CK_ONLY = 0 px at the bbox on every frame (CK correct); GARBAGE_MATTE = ~34k frozen px frames 26-30 (SAM is the source). Brown color = kept green pixels after despill. Tag: VERIFIED. Last verified: 2026-07-22.

Tested hypotheses:
- Real motion-blur pixels in source: REFUTED, source bbox pure green on frame 221 while blob persists. Tag: FAILED APPROACH (as explanation). 2026-07-22.
- CK matte responsible: REFUTED by CK_ONLY = 0 px. Tag: FAILED APPROACH (as explanation). 2026-07-22.
- SAM2 memory-bank keeps predicting the object at last known position after frame exit: SUPPORTED by frozen identical mask. Sibling of the open fast-spin mistrack issue (07-01). Tag: PARTIALLY VERIFIED. 2026-07-22.

Failed approaches: none yet on a fix; issue fresh.

Next test: candidate guard = screen-color veto on SAM-only regions (where SAM claims foreground but CK says background AND source pixel is confidently screen-colored, drop the SAM claim). Symmetric to the shipped DOT LAW exception, so consistent with existing law. Merge territory = protected; corpus gate + Berto approval required before any default change. Interim operator workaround: negative dot near the exit edge, or trim range at exit.
Files involved: corridorkey_sam_merge.py (merge zones); SAM2 video predictor path in ae_processor.py.
Owner: awaiting Berto decision on whether to build the veto.
Status: OPEN

---

## Issue: HSV on-green floor drift (comment/memory says 20, live code says 50)

Symptom: green spill leaks on dark-shadowed green behind black pants; a 2026-06-22 session recorded lowering the HSV value floor from 50 to 20 to fix exactly this, and this was also independently logged as an open discrepancy on 2026-07-19 ("EdgeV2 floor: comment says 20, code says 50, never reconciled").
Current evidence: live-code grep on 2026-07-22 across `ae_plugin/cep_panel/ae_processor.py` (lines 357, 1426, 1533) and `corridorkey_sam_merge.py` (line 1546) shows the hue/sat/val floor at (35, 50, 50) at every site. No occurrence of the lowered floor (35, 50, 20) exists anywhere in the current tree.

Tested hypotheses:
- The 2026-06-22 fix was recorded but never actually committed, or was committed and later reverted without a note. Tag: HYPOTHESIS. Last verified: 2026-07-22.
- A different, unlogged constant elsewhere carries the effective fix and the floor value itself is a red herring. Tag: HYPOTHESIS. Last verified: 2026-07-22.

Failed approaches: none recorded; this is a fresh reconciliation, not yet investigated further than the grep above.

Next test: grep git log/blame across the four call sites for any commit touching the HSV lower bound around 2026-06-22 through today; if none exists, treat the fix as never shipped and re-apply it deliberately, then re-verify against a real dark-clothing clip before closing this issue.
Files involved: ae_plugin/cep_panel/ae_processor.py; corridorkey_sam_merge.py.
Owner: next session to touch chroma/despill code.
Status: OPEN

---

## Issue: corridorkey_sam_merge.py docstring contradicts its own MERGE_MODE constant

Symptom: the module's header docstring (lines 1-25) describes a "v1.0, CK and SAM independent, the plugin no longer merges them" design where merge responsibility has moved to the user's host compositor. Immediately below it, `MERGE_MODE = "garbage_matte"` and the surrounding code still implement an active CK x SAM merge.
Current evidence: read directly on 2026-07-22, on branch `feat/mcp-server` with an uncommitted working tree (`git status` shows dozens of modified/untracked files). Recent commit history on this branch includes a "TWO HALO helper added, viewer wiring pending" WIP commit.

Tested hypotheses:
- A rewrite toward the "no merge, host does it" design is mid-flight and the docstring was updated ahead of the code. Tag: HYPOTHESIS. Last verified: 2026-07-22.

Failed approaches: none recorded.

Next test: ask whoever is driving `feat/mcp-server` whether the v1.0 no-merge redesign is an active, intended direction or stale copy; until answered, do not trust either the docstring's description or an assumption that garbage_matte is final.
Files involved: corridorkey_sam_merge.py.
Owner: whoever resumes `feat/mcp-server`.
Status: AWAITING USER DECISION

---

## Issue: SAM2 matte horizontal banding in DaVinci's Matte view

Symptom: visible horizontal bands in `alpha_raw x sam2_gate` compositing, specifically in DaVinci's Matte view.
Current evidence: six distinct fix attempts (full-res masks, INTER_CUBIC interpolation, raw frame to SAM2, a Gaussian-blur gate, a contrast-curve view) all failed to fix it; the contrast-curve attempt was additionally rejected by Berto for hiding real holes rather than fixing banding.
Tested hypotheses: none confirmed; root cause remains unknown after six attempts. Tag: HYPOTHESIS (unconfirmed). Last verified: 2026-04-26.

Failed approaches:
- Tried: full-res masks. Root cause of failure: did not fix banding. Tag: FAILED APPROACH. Last verified: 2026-04-26.
- Tried: INTER_CUBIC interpolation. Root cause of failure: did not fix banding. Tag: FAILED APPROACH. Last verified: 2026-04-26.
- Tried: feeding SAM2 the raw (unprocessed) frame. Root cause of failure: did not fix banding. Tag: FAILED APPROACH. Last verified: 2026-04-26.
- Tried: a Gaussian-blur gate. Root cause of failure: did not fix banding, and independently conflicts with the standing no-Gaussian rule (Part 01 Rule 4). Tag: FAILED APPROACH. Last verified: 2026-04-26.
- Tried: a contrast-curve view. Root cause of failure: hid real holes instead of fixing the underlying banding; rejected by Berto on those grounds specifically. Tag: FAILED APPROACH. Last verified: 2026-04-26.

Next test: instrument the exact pixel values at a band boundary across several frames to determine whether the banding is a quantization artifact (float32 to 8-bit path), a Matte-view-specific display transform, or a real alternating pattern in the alpha itself; six tries at fixing it without first isolating which of those three it is suggests the next step should be diagnosis, not another fix attempt.
Files involved: DaVinci Matte view rendering path (resolve_plugin), alpha_raw / sam2_gate composite.
Owner: next session touching Resolve-side matte display.
Status: OPEN

---

## Issue: alpha-frame-count mismatch silently duplicates the last alpha frame

Symptom: `resolve_plugin/core/corridorkey_processor.py:115` duplicates the last available alpha frame when the alpha-frame count does not match the expected frame count, with no visible warning to the operator.
Current evidence: flagged during a repo review pass; not yet fixed.
Tested hypotheses: none recorded.
Failed approaches: none recorded.
Next test: reproduce with a deliberately short alpha sequence and confirm whether the duplication is silent in the current tree; if so, add at minimum a log warning, and evaluate whether duplicating is ever the right behavior versus failing loudly.
Files involved: resolve_plugin/core/corridorkey_processor.py:115.
Owner: unassigned.
Status: OPEN

---

## Issue: SAM2 reloads on every click in the Resolve panel

Symptom: each SAM2 click in the Resolve panel spikes VRAM by 2-4GB and reloads a roughly 300MB model, unlike the main CK keyer, which is cached.
Current evidence: flagged as a DANGER ZONE comment since 2026-04-14; still present as of the last review pass on 2026-07-05.
Tested hypotheses: a cache dict matching the main keyer's caching pattern would fix it. Tag: HYPOTHESIS (a clear, low-risk fix, just not yet implemented). Last verified: 2026-07-05.
Failed approaches: none recorded; this has not been attempted yet, only diagnosed.
Next test: add a SAM2 model cache dict keyed the same way the CK keyer's cache is, confirm VRAM no longer spikes on repeated clicks within one session, and confirm it does not reintroduce the warm-worker thread-leak problem that SAM2 subprocess isolation exists to avoid (Part 04).
Files involved: resolve_plugin/CorridorKey_Pro.py:1541 (comment marks the spot).
Owner: unassigned.
Status: OPEN

---

## Issue: batch range reads are still sequential from frame 0

Symptom: a ranged processing job can silently process frame 0 and save it as, for example, frame 500, corrupting output, because the underlying reader advances sequentially from the start of the file regardless of the requested range's start index.
Current evidence: flagged as a release blocker in a Codex-authored review; fix (a shared indexed/random-access frame-source layer) was recommended but not confirmed shipped as of that review.
Tested hypotheses: none beyond the diagnosis itself. Tag: HYPOTHESIS. Last verified: 2026-05-14.
Failed approaches: none recorded.
Next test: write a small range job that starts well past frame 0 on a long clip, and diff the saved frame contents against a known-good single-frame extraction at the same index; if they differ, the bug is still live and the shared frame-source layer needs to actually be built.
Files involved: backend/service.py:394-721.
Owner: unassigned.
Status: OPEN

---

## Issue: AE session-directory PNG writes are still non-atomic

Symptom: the AE-side viewer can read a partially-written file, or a stale-source-plus-new-matte mismatch, because `ae_processor.py` writes fg/alpha/original PNGs with a plain `cv2.imwrite()` instead of a tmp-file-plus-rename pattern.
Current evidence: Resolve already has an atomic write pattern (`_atomic_imwrite`, tmp file plus `os.replace`); it was flagged in an April review as not yet ported to the AE side.
Tested hypotheses: porting the existing Resolve pattern would fix it. Tag: HYPOTHESIS (straightforward, not yet done). Last verified: 2026-04-30.
Failed approaches: none recorded.
Next test: port `_atomic_imwrite` (or an equivalent tmp+replace helper) into the AE write path, and confirm with a tight read/write race test (read the file the instant after the write call returns) that no partial file is ever observable.
Files involved: ae_plugin/cep_panel/ae_processor.py.
Owner: unassigned.
Status: OPEN

---

## Issue: two alpha-hint code paths coexist, the banned one still importable

Symptom: `AlphaHintGenerator` (the HSV-based alpha hint, explicitly banned by a DANGER ZONE comment for flagging tan/khaki/olive fabric as screen color) still exists in the tree and is still importable; it has already been silently reintroduced once via a bulk-restore.
Current evidence: `generate_alpha_hint` (inline RGB) is the live, correct path; `AlphaHintGenerator` (HSV) sits alongside it, uncalled but present.
Tested hypotheses: none needed; the risk is structural (a future restore or merge could reintroduce a call to it), not a live bug today.
Failed approaches: none; this has not yet been fixed, only flagged.
Next test: either delete `AlphaHintGenerator` outright, or add an explicit guard (a runtime assertion, or a lint rule) that fails loudly if anything ever calls it, so a future bulk-restore cannot silently reintroduce it the way it did once already.
Files involved: core/alpha_hint_generator.py (Resolve side); any AE-side equivalent.
Owner: unassigned.
Status: OPEN

---

## Issue: export_clip_frames mutates global Resolve render settings with no timeout

Symptom: a stalled render, or a pre-existing item already in Resolve's render queue, can hang the whole plugin or disturb the user's actual project, because `export_clip_frames` touches Resolve's global render settings/queue directly with no isolation and no timeout.
Current evidence: flagged in a 2026-05-14 review; the recommended fix (snapshot current settings, run an isolated job with a timeout, restore the snapshot in a `finally` block) was not confirmed shipped as of that review.
Tested hypotheses: the snapshot/timeout/restore pattern would fix it. Tag: HYPOTHESIS. Last verified: 2026-05-14.
Failed approaches: none recorded.
Next test: confirm in the current tree whether the snapshot/timeout/restore pattern exists at `resolve_plugin/core/resolve_bridge.py:163, 196-236`; if not, implement it and test against a deliberately pre-loaded render queue to confirm no disturbance to the user's existing queue items.
Files involved: resolve_plugin/core/resolve_bridge.py:163, 196-236.
Owner: unassigned.
Status: OPEN

---

## Issue: correction-dot off-by-one fix, verified correct, lost in the 2026-07-01 stack revert

Symptom: correction dots placed during `cmd_batch` land one frame early, scrambling SAM2's tracking memory, which is believed to explain why adding dots to "fix" holes made them worse for weeks.
Current evidence: root cause identified as a missing `+_actual_preroll` offset in the correction-dot loop (introduced at commit f8a33b0); the fix for this specific offset was verified correct at the time, but the whole session's stack of six changes (interior-fill, per-frame correction dots, scrub post-pass plus this off-by-one fix, render-review, full-res SAM) was reverted wholesale on 2026-07-01 after breaking a working key, and this one verified-correct fix was never independently re-applied afterward.
Tested hypotheses:
- Missing `+_actual_preroll` causes the one-frame-early placement. Tag: VERIFIED (at time of original diagnosis). Last verified: 2026-06-30.

Failed approaches:
- Tried: shipping the off-by-one fix as part of a six-change overnight stack. Root cause of failure: an unrelated change in the same stack broke the key; the whole stack was reverted together, including this correct fix, because it was not isolated. Tag: FAILED APPROACH (process failure, not a defect in the fix itself). Last verified: 2026-07-01.

Next test: re-apply the `+_actual_preroll` offset to the correction-dot loop in isolation (Part 01 Rule 2, one change at a time), and re-verify against the same evidence that confirmed it originally, before shipping anything else in the same session.
Files involved: cmd_batch correction-dot loop (engine, exact current file:line needs reconfirming post-revert).
Owner: unassigned.
Status: OPEN

---

## Issue: SAM2 mistracks fast, motion-blurred aerial spins

Symptom: SAM2 produces a partially wrong mask on fast, motion-blurred aerial spin shots.
Current evidence: unsolved since 2026-07-01.
Tested hypotheses: none recorded as tested.
Failed approaches: none recorded.
Next test: build a small dedicated test clip of an aerial spin and confirm whether the failure is in the image predictor's anchor-frame quality, the video predictor's temporal propagation, or the merge stage's holdout logic, before attempting a fix; this has not yet been isolated to a specific stage.
Files involved: SAM2 support stage (Part 03).
Owner: unassigned.
Status: OPEN

---

## Issue: off-green hair retention

Symptom: hair detail is lost specifically when it crosses from the green screen onto a non-green background within the same frame. Described in memory as "the real open problem" as of 2026-07-19.
Current evidence: no root cause identified; distinct from the (solved) chest/butt SAM hole bug and from the (standing, physics-level) wire-versus-crease separation problem.
Tested hypotheses: none recorded as tested.
Failed approaches: none recorded.
Next test: isolate a test clip where hair crosses the green/non-green boundary and compare the CK alpha alone (no SAM) against the merged output at that boundary, to determine whether the loss originates in the CK keyer itself or in the merge/holdout stage.
Files involved: CK Neural Keyer and Merge / Garbage-Matte components (Part 03).
Owner: unassigned.
Status: OPEN

---

## Issue: shared settings not unified across AE and Premiere

Symptom: a fix or setting that works correctly in After Effects does not carry over to Premiere on the same machine.
Current evidence: AE and Premiere maintain separate localStorage state; this is identified in memory as the root of an entire recurring bug class ("works in AE not Premiere"), and separately as imperfect preview color parity across hosts in the log ledger.
Tested hypotheses: unifying settings storage across both hosts would close this bug class. Tag: HYPOTHESIS. Last verified: 2026-07-19.
Failed approaches: none recorded.
Next test: pick one already-reported "works in AE, not Premiere" bug, confirm it reproduces, and confirm the specific setting involved is the one that differs between the two hosts' localStorage; if confirmed, that is the case for prioritizing a shared-settings store over continuing to patch instances one at a time.
Files involved: ae_plugin/cep_panel/index.html (settings read/write for both AEFT and PPRO host types).
Owner: unassigned.
Status: OPEN

---

## Issue: unique-per-render filenames not implemented

Symptom: Premiere media-cache collisions have bitten the same project at least three times, because rendered output filenames are not guaranteed unique per render.
Current evidence: flagged in both the log and memory ledgers as of 2026-07-19, not yet built.
Tested hypotheses: none recorded.
Failed approaches: none recorded.
Next test: add a render-unique component (timestamp or incrementing ID) to output filenames and confirm a repeated render of the same range no longer collides with Premiere's media cache.
Files involved: output-write path shared across hosts (Part 02 primary flow, Output Write stage).
Owner: unassigned.
Status: OPEN

---

## Issue: f32-33 SAM drop pocket

Symptom: a SAM mask dropout specifically around frames 32-33 in at least one test range.
Current evidence: an ADD FRAME rescue mechanism exists and is believed to address it; whether PREVIEW-follow-playhead behaves correctly in Premiere for this specific case is unverified.
Tested hypotheses: the ADD FRAME rescue covers this case. Tag: PARTIALLY VERIFIED (Resolve/AE side believed covered; Premiere PREVIEW-follow-playhead specifically unverified). Last verified: 2026-07-19.
Failed approaches: none recorded.
Next test: reproduce the f32-33 drop in Premiere specifically, with PREVIEW-follow-playhead active, and confirm whether ADD FRAME rescues it there the same way it does in the already-verified hosts.
Files involved: ADD FRAME rescue path (engine), Premiere preview-follow-playhead logic (host.jsx / index.html).
Owner: unassigned.
Status: OPEN

---

## Issue: 2-7k pixel speck family

Symptom: a family of small (2,000 to 7,000 pixel) specks appears in output mattes on some clips.
Current evidence: unresolved as of 2026-07-19; no further detail recorded beyond the pixel-count range.
Tested hypotheses: none recorded.
Failed approaches: none recorded.
Next test: gather 2-3 clips that reliably show this speck family, confirm the size range holds across them, and check whether despeckle threshold tuning alone addresses it before assuming a deeper merge-stage cause.
Files involved: post-processing despeckle stage (Part 02).
Owner: unassigned.
Status: OPEN

---

## Issue: garbage-mask display blink in Premiere Pro

Symptom: the garbage-mask display blinks/flickers in Premiere Pro specifically.
Current evidence: flagged as of 2026-07-19, no root cause recorded.
Tested hypotheses: none recorded.
Failed approaches: none recorded.
Next test: reproduce with screen capture at frame-step granularity to determine whether the blink is a render-timing race, a track-matte refresh issue, or a genuine per-frame mask instability, since these would each point to a different owning component.
Files involved: Premiere track-matte / garbage-mask display path.
Owner: unassigned.
Status: OPEN

---

## Issue: green spill edge line on pants

Symptom: a visible green spill edge line on pants in at least one test case; Berto was mid-test on the DESPILL slider against this specific case as of 2026-07-19.
Current evidence: no conclusion recorded yet.
Tested hypotheses: none confirmed.
Failed approaches: none recorded.
Next test: resume the DESPILL slider test Berto had in progress and record the result; this issue is mid-investigation, not yet started from zero.
Files involved: despill stage (Part 02, Part 03 CK Neural Keyer known weaknesses).
Owner: Berto (mid-test as of 2026-07-19).
Status: OPEN

---

## Issue: multi-frame / moving-camera SAM prompting not built

Symptom: SAM2 prompting only supports a single first-frame anchor; pull-back and reveal shots (where the camera moves enough that a first-frame anchor stops matching later frames) are an acknowledged capability gap, not a bug.
Current evidence: acknowledged directly as a gap in the log ledger, 2026-07-19.
Tested hypotheses: not applicable; this is unbuilt functionality, not a misbehaving feature.
Failed approaches: none recorded.
Next test: scope what multi-anchor or moving-camera-aware SAM2 prompting would require (likely: multiple anchor frames feeding the video predictor, or a re-anchoring UI action) as a feature design question, not a bug investigation.
Files involved: SAM2 Support Stage (Part 03).
Owner: unassigned.
Status: OPEN

---

## Issue: DaVinci Resolve not yet at AE/Premiere parity

Symptom: Resolve runs an older UI and an older merge implementation at all six of its combine call sites, compared to the AE/Premiere side.
Current evidence: stated directly in memory as of 2026-07-19; consistent with `resolve_plugin/CLAUDE-MAP/INDEX.md`'s own note that Resolve's live plugin (`CorridorKey_Pro.py`) and its two alpha-hint implementations still disagree, and that `install.py` deploys the superseded legacy plugin, not the live one (Part 12).
Tested hypotheses: none beyond the parity gap itself being real and acknowledged.
Failed approaches: none recorded.
Next test: enumerate the six merge call sites in the Resolve plugin, confirm which ones still run pre-garbage-matte logic, and port them to the shared `apply_sam2_gate` entry point one call site at a time (Part 01 Rule 2, not all six at once).
Files involved: resolve_plugin/CorridorKey_Pro.py (all six combine call sites).
Owner: unassigned.
Status: OPEN

---

## Issue: ComfyUI plugin built but never run by Berto

Symptom: a ComfyUI integration exists in the repo (`comfyui_plugin/`) but is untracked/uncommitted and has never actually been exercised by Berto.
Current evidence: noted as of 2026-07-19.
Tested hypotheses: not applicable; this is an unverified, not a misbehaving, component.
Failed approaches: none recorded.
Next test: get Berto to actually run it once on a real clip before it is trusted as a supported host at all; until then, treat it as experimental and unproven, not a fourth supported host.
Files involved: comfyui_plugin/.
Owner: Berto (needs to actually test it).
Status: BLOCKED

---

## Issue: only one clip is fully pipeline-tested end to end

Symptom: "the bra clip" is the only footage confirmed to work correctly through the full pipeline; non-bra clips, and Premiere specifically on any non-bra clip, are untested.
Current evidence: stated directly in memory as of 2026-07-19.
Tested hypotheses: not applicable.
Failed approaches: none recorded.
Next test: run the full pipeline (both hosts) against at least 2-3 clips beyond the bra clip, ideally drawn from the project's own multi-clip test corpus already required for merge changes (Part 01 Rule 3), and record pass/fail per host.
Files involved: not code, a testing gap.
Owner: unassigned.
Status: OPEN

---

## Issue: right-foot dark ring

Symptom: a dark ring artifact around the right foot on at least one clip.
Current evidence: parked as of 2026-07-19; believed to be a genuine plate shadow (an artifact of the actual footage, not a pipeline bug), but not confirmed either way.
Tested hypotheses: it is a genuine plate shadow, not a pipeline defect. Tag: HYPOTHESIS. Last verified: 2026-07-19.
Failed approaches: none recorded.
Next test: check the raw, unkeyed source plate at the same frame for the same dark ring; if it is present in the source, this closes as "not a bug," if absent, it needs real investigation.
Files involved: not yet identified pending the source-plate check above.
Owner: unassigned.
Status: BLOCKED (needs the source-plate check before it can move)

---

## Issue: BMD SDK/DLL redistribution license unclear

Symptom: it is not established whether `BlackmagicRawAPI.dll` can be legally bundled and shipped to end users who do not already have DaVinci Resolve installed, which blocks shipping the BRAW decode capability to non-Resolve users.
Current evidence: flagged as of 2026-07-19; no legal determination recorded. Consistent with Part 03 and Part 09 marking this UNKNOWN rather than assuming either a permissive or restrictive answer.
Tested hypotheses: none; this is a licensing question, not a technical one.
Failed approaches: not applicable.
Next test: read Blackmagic's actual SDK license/EULA for `BlackmagicRawAPI.dll` redistribution terms before shipping BRAW support to any user who does not already have Resolve installed; do not assume either direction without reading the license text itself.
Files involved: braw-decode.exe distribution (Part 03).
Owner: unassigned (needs a human legal read, not an AI guess).
Status: AWAITING USER DECISION

---

## Issue: no undo for misplaced SAM dots

Symptom: there is no Ctrl+Z-equivalent undo for a single misplaced SAM2 dot; the only recovery is a full CLEAR and re-placing every dot from scratch.
Current evidence: noted as of 2026-07-19, not yet built.
Tested hypotheses: not applicable.
Failed approaches: none recorded.
Next test: scope a per-dot undo stack (last-dot-removed, not a full CLEAR) as a UI feature; likely small compared to most items in this ledger, but not yet built.
Files involved: SAM2 dot-placement UI (CEP panel canvas; Resolve preview).
Owner: unassigned.
Status: OPEN

---

## Issue: tutorial videos still not made

Symptom: README states tutorials are "on the way" but none exist yet.
Current evidence: noted as of 2026-07-19.
Tested hypotheses: not applicable.
Failed approaches: not applicable.
Next test: not a code task; scope and schedule as a content task separate from engineering work.
Files involved: none.
Owner: Berto / StuntWorks Cinema channel.
Status: OPEN

---

## Issue: ck_broker.py has no clear failure signal when the Scheduled Task is not running

Symptom: if the per-user logon Scheduled Task hosting `ck_broker.py` is not registered, or the broker process has stopped, a CUDA job from the CEP panel appears to the operator as a stalled job, not a clear "broker unavailable" error.
Current evidence: identified during this bible pass as a hardening gap in the CUDA bridge component (Part 03); not previously tracked as a distinct ledger item.
Tested hypotheses: none tested.
Failed approaches: none recorded.
Next test: kill the broker process deliberately, trigger a CUDA job from the panel, and observe what the operator actually sees; if it is an indefinite stall rather than a clear error, add a ping-first check with a short timeout before dispatching the real job, and surface a specific "broker not running, re-run install_broker_task.ps1" message.
Files involved: ae_plugin/cep_panel/ck_send.py; ae_plugin/cep_panel/ck_broker.py.
Owner: unassigned.
Status: OPEN
