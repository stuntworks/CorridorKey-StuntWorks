# Last modified: 2026-06-12 | Change: Initial spec — Matte Fusion v2 (trimap architecture), authored by Berto via claude.com consult, amended per 3-model consensus | Full history: git log

# PROJECT: CorridorKey Matte Fusion v2 (Trimap Architecture)

> AMENDMENT LOG (deviations from Berto's original paste — flagged per spec rules):
> 1. Stage 1 definite-background rule: original said "outside dilated SAM AND NN alpha < low".
>    AMENDED to "outside dilated SAM, period" — the AND left bright junk outside the body in
>    the unknown band (feet-ring bug reborn). All three consult models (claude.com, GPT-5.5,
>    Kimi K2.6, 2026-06-12) independently said: outside support = hard zero, no conditions.
>    Berto notified 2026-06-12, no objection.
> 2. Hybrid band combiner law (Berto's verdict, 2026-06-12): ViTMatte alone
>    loses hair wisps preserved in the raw CK/NN alpha. AMENDED to a per-shot
>    green-confidence blend: alpha = W * nn_alpha + (1-W) * vitmatte_alpha,
>    where W is a Mahalanobis-distance likelihood that the local background
>    is screen-colored (LAB chroma space, robust statistics from definite-BG pixels).
>    Feet zone hard override: W=0 (ViTMatte unconditionally — feet are off-green).
>    Every side-by-side must include RAW CK alpha as the first panel (Berto's law).

## ROLE
You are implementing a redesign of the matte combination stage in CorridorKey.
Follow this spec exactly. No scope creep. No features not listed here. If
something in this spec conflicts with existing code, flag it and ask before
changing anything outside the matte fusion module.

## CONTEXT
Current pipeline: Hiera NN keyer produces soft alpha (excellent on green,
meaningless off green). SAM2 produces binary body silhouette (temporally
tracked). Current fusion blends these mattes with HSV green detection and
19 tunable thresholds plus rescue passes. This approach is being REMOVED.
Blending is the wrong formulation. We are replacing it with trimap
construction plus a matting solve.

## ARCHITECTURE

### Stage 1: Trimap construction (geometric, from SAM2 silhouette + NN alpha)
- Definite foreground = eroded SAM silhouette AND NN alpha > high threshold
- Definite background = outside dilated SAM silhouette (hard zero — no NN condition; see AMENDMENT 1)
- Unknown = everything else
- Erode amount N and dilate amount M are defined as PERCENTAGE of the SAM
  silhouette bounding box height, never absolute pixels. This is mandatory.
  Resolution independence is a hard requirement.
- Feet zone rule: bottom 12% of silhouette bounding box uses a tighter
  unknown band (reduce dilation M by half in this zone). One zone rule only.
  Do not add more zones.
- Derive trimap geometry from SAM2's tracked video mask, not per-frame
  independent logic.

### Stage 2: Matting solve (resolves the unknown band only)
- Primary implementation: ViTMatte. Input = original frame + trimap.
  Output = solved alpha.
- Fallback implementation: guided filter matting using the original frame
  as guide. Build both behind a common interface so they are swappable.
- The solver only operates on the unknown band. Definite FG and BG pass
  through untouched.

### Stage 3: Temporal smoothing
- Smooth TRIMAP BOUNDARIES across a 3 to 5 frame window. Do not smooth the
  final alpha. Smoothing the output causes ghosting; smoothing the input
  geometry does not.

## REMOVED FROM PIPELINE
- HSV green detection as a decision signal (may remain only as an optional
  hint to tighten the unknown band, disabled by default)
- All 19 chroma blend thresholds
- Rescue passes: interior solidify, chroma escape valve, shadow kill
- Connectivity and thickness filters
Do not silently keep any of these alive. Delete or clearly quarantine them.
(Quarantine = legacy path stays runnable behind the existing settings flag
until the gauntlet proves v2 — CORPUS GATE law. Removal happens in Phase 6
only after a corpus win.)

## PARAMETERS (the only ones allowed)
- erode_pct (default 3% of silhouette bbox height)
- dilate_pct (default 6%)
- feet_zone_pct (default 12%, dilation halved inside)
- nn_high / nn_low trimap hints (defaults 0.95 / 0.05)
- temporal_window (default 5 frames)
If you find yourself wanting to add a parameter beyond these, stop and ask.

## VALIDATION: THE GAUNTLET
- Build a test harness that runs the full pipeline on a fixed set of real
  clips (Berto provides 6 to 8 worst-case clips).
- Metrics, scored ONLY inside the unknown band:
  1. Per-pixel alpha error against reference mattes where available
  2. Gradient-domain error (Sobel on alpha, compare structure) to catch
     chewed and wavy edges
  3. Temporal flicker metric: frame-to-frame alpha delta in stable regions
- Output a per-clip scorecard. A change PASSES only if no clip regresses.
- Also render side-by-side comparison frames (old pipeline vs new) at the
  known failure points: feet at floor seam, hair over junk, limb crossing
  a wall edge. Human review is final, the metrics are a gate not a verdict.

## ACCEPTANCE CRITERIA
1. NN soft edges preserved wherever actor is over green (hair, motion blur)
2. Zero junk surviving outside the SAM silhouette plus unknown band
3. No bites out of face, hair, or body where actor crosses non-green junk
4. Identical behavior at 1080p and 4K with identical parameter values
5. No per-shot tuning needed across the gauntlet clips
6. Gauntlet scorecard shows no regressions vs current pipeline on any clip

## BUILD ORDER
- Phase 1: Trimap construction module + unit tests on synthetic silhouettes
- Phase 2: Guided filter solver behind the common interface, end to end run
- Phase 3: ViTMatte solver integration, same interface
- Phase 4: Temporal trimap smoothing
- Phase 5: Gauntlet harness + scorecard + side-by-side renders
- Phase 6: Remove/quarantine legacy blend code paths

## HRCS REQUIREMENTS
- Plain English headers on every file explaining what it does and why
- DEPENDS-ON / AFFECTS / ISOLATED tags on every module
- Target ~500 lines per file, split if larger
- The solver interface module is ISOLATED: swapping solvers must touch
  nothing else

## REVIEW LOOP
After each phase, grade your work against this spec section by section.
List every deviation, every assumption you made, and every acceptance
criterion not yet met. Fix all deviations before moving to the next phase.
Do not declare a phase complete until the spec check passes. After Phase 5,
run the gauntlet and loop fixes until the scorecard shows no regressions
or you hit a failure you cannot resolve, in which case stop and report
exactly what fails and why instead of adding parameters.

## HISTORY GUARDRAILS (from CK memory — do not relearn these)
- v2.2 "trimap+CFM merge" was tried and REJECTED (destroyed hair when SAM took
  over). That version blended masks inside the band. THIS design solves the band
  from image content. First eyeball test after Phase 2: HAIR over junk.
- Square dilation = staircase on diagonals. MORPH_ELLIPSE / circular only.
- Channel math in float32 with epsilon-guarded divisions. Never uint8 G-max(R,B).
- SAM2 soft logits saved as uint16 PNG (uint8 quantizes the soft edge away).
- cv2.setNumThreads(1) considerations and no per-frame torch.compile/synchronize.
