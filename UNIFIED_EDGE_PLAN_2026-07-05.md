# UNIFIED EDGE ENGINE — build plan v2 (2026-07-05)

> Berto directive: no quick fix — a boundary system that passes the test of time.
> v1 design converged by 5-way panel (tracer, architect, EZ deconstruction, GPT-5.5, Grok).
> v2 = v1 amended after TWO adversarial reviews (internal plan-vs-code + Grok critique).
> Both reviewers: core idea sound, v1 unsafe to build. All their MUST-FIXes folded in below.
> LAW for the builder: the current garbage_matte implementation is the SPEC of behaviors to
> re-express or deliberately retire with corpus proof — not just the three famous ones.

## THE DESIGN (amended)

New merge path `unified_band`, selected by a PER-SESSION SETTINGS KEY threaded through
sam_garbage_merge/apply_matte_postproc — mirroring the fusion_v2 settings pattern
(index.html toggle + localStorage + settings payload). NOT the MERGE_MODE module constant:
that constant is shared by AE, DaVinci (6 call sites), and ComfyUI (engine_bridge imports),
and flipping it ships to all hosts at once. Old garbage_matte path untouched = rollback is
a real one-click toggle, AE-only.

1. **Distance field D(p)** — exterior distance from the POST-solidify, POST-carve silhouette
   (carve holes must reduce D locally, or wire exclusions get refilled).
2. **Confidence field C(p)** — continuous chroma score, branching on screen_type (green AND
   blue — blue is in scope, the current code branches everywhere and unified_band must too).
   Smoothing: plain cv2.bilateralFilter (INSTALLED; cv2.ximgproc guided filter is NOT in the
   CK venv — verified — and must not be assumed) A/B'd against the current morph+Gaussian
   G_soft in P2 with halo checks. Optional temporal EMA of C across frames in the batch loop
   (frames are sequential in cmd_batch; state carry is available) — shimmer control.
3. **Width field W(p)** = tight + (wide − tight)·smoothstep(C), then:
   - low-pass W before use (kills local wobble from C gradients + DT quantization),
   - continuous feet taper near bbox bottom — DISABLED by the framing guard
     `_body_exits_bottom` (waist-crop protection, kept verbatim from current code),
   - directional no-down bias re-encoded (masked/asymmetric term) — the current sam_wide
     zeroes downward growth to stop floor expansion; W must too.
   All terms resolution-relative. Widths blended BEFORE any edge is drawn.
4. **Support = smoothstep((W − D)/feather_px)** with explicit edge0/edge1 normalization.
   Monotonic by construction.
5. **Keep/kill/band rule (amended per both reviewers):**
   - inside eroded SAM: keep — alpha = max(CK soft alpha, off-green SAM body fill). The
     off-green body fill is a LOAD-BEARING RESCUE (body parts extending past the green,
     dark fabric where CK has zero signal), not a bypass. It is part of the keep rule.
   - beyond W: kill (this replaces shadow_kill + wing/ridge distance kills — coverage
     matrix required, see P1).
   - band between: alpha = CK soft alpha × support (EZ shell trust; hair/blur/straps).
6. **Loud failures.** unified_band exceptions log.error + panel warning. NEVER the silent
   Path B fallback the current dispatcher uses (buried temp file, crude max-merge).
7. **Rim detector from day one** — high-low-high profile scan along outward normals ships
   as a P1 debug output, used as a P2 gate metric and kept forever as regression tooling.

Explicitly UNTOUCHED (say it so no worker deletes them): apply_shirt_rescue (separate
mechanism — bright/thin fabric inside body, NOT the off-green fill), merge_ck_simple
(Resolve-parity path), experimental_recipe/apply_recipe_composite (dead from UI — 4th path,
unified_band does not reach it), fusion_v2 solver branch, CK engine, SAM2 pipeline,
temporal SAM guard, dot forensics, zone_cut, choke/despeckle/despill chain.

## RETIRED-RULE COVERAGE MATRIX (P1 deliverable, gate for P2 entry)

Builder produces a table: EVERY rule of merge_ck_with_garbage_matte (tight/wide/G_soft,
feet override + erosion, escape valve, off-green fill, shadow_kill, wing filter, ridge kill,
proximity/EDGE GUARD slider, seam suppression, framing guards, no-down kernels, 92% cut)
→ covered-by-new / deliberately-retired-with-proof / kept-downstream. No silent drops.

## PHASES

- **P0 — Corpus reality first.** (a) Audit every candidate session's settings for
  fusion_v2/simple_combine/experimental_recipe — sessions carrying those flags bypass the
  merge path and would fake the gate. (b) Extend the proven bit-exact replay harness
  (D:\CLAUDE_JUNK\ck_edgebite_trace_2026-07-05\) to ALL corpus clips, not just the one.
  (c) Corpus = butt clip, feet-ring clip, hair-whip spin, harness clip, partial-screen clip
  + ADD: waist-crop clip, static-subject take (shimmer measurement), blue-screen clip,
  multi-performer clip — flag to Berto which need fresh renders vs exist on disk.
  (d) Baseline current garbage_matte on all → archived metrics + crops.
- **P1 — Build** unified_band per design above + coverage matrix + rim detector.
  Performance budget: merge stage ≤ 1.5× current garbage_matte wall-time at 4K, measured.
- **P2 — Offline corpus gate.** All P0 clips, new vs baseline: butt intact (±3%), feet rim
  ≤ current, hair-whip band intact, zero high-low-high profiles, junk/wing/ridge kill ≥
  current, blue clip parity, waist-crop lower-torso intact, AND the named forensic crease
  pixel (ck_edgebite_trace session, row y≈1190) must SURVIVE — monotonic shape is not
  enough, the tight endpoint must be wide enough there. PLUS temporal metric: frame-to-frame
  edge-position variance on the static take ≤ current baseline. Fail → iterate P1.
- **P3 — Berto eye gate.** Panel toggle A/B, real clips, JUDGED IN MOTION (stills lie —
  the 06-12 redesign died exactly here). Berto is release authority.
- **P4 — Default flip decision + DaVinci/ComfyUI port plan + docs + STALE-marking.**

## RISKS (v2)

- Prior-art honesty (Grok): NO production keyer ships this exact architecture. Lessons
  imported: temporal stabilization in the edge band, filter only the confidence field
  (never CK's alpha — June scar), explicit re-derivation of directional/framing/off-green
  cases. The corpus gate is the proof, not the pedigree.
- EZ shell fix is partial in their repo (open issue #179) — shell trust rides WITH the
  monotonic width field here, and the rim detector catches what slips.
- The band trust could un-kill real wires CK half-detects — wire/strap clip is a named P2
  gate with its own replay.
- One-clip overfit — corpus gate across ALL clips before any default flips (Berto law).
