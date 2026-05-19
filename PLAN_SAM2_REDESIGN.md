# CorridorKey SAM2 Redesign — Plan

**Date:** 2026-04-28
**Status:** Plan complete, awaiting Berto approval before any code changes.
**Driving principle:** CorridorKey's NN matte is the star. SAM2 is an **optional garbage matte** that subtracts junk the user clicks. Detail (hair, motion blur, fabric edges) is preserved everywhere SAM2 isn't clicked.

---

## 1. WHY THIS REDESIGN

Current behavior: SAM2 mask gates the NN matte (multiplies). SAM2's hard body silhouette CHOPS OFF the NN's hair/edge detail. Documented in `corridorkey_two_pass_hair_workflow.md` — this is the hair-killer.

New behavior: same SAM2 clicks, same propagation, same multi-object support — just flip the polarity at the end:

```
Old: final_alpha = NN_alpha × sam2_mask        (SAM2 = subject definer)
New: final_alpha = NN_alpha × (1 - sam2_mask)  (SAM2 = garbage subtractor)
```

A single **INVERT MASK** checkbox toggles between them.

### Berto's 3-second decision rule (goes on the panel)

> **Look at the raw NN matte first. Don't touch SAM2.**
> - Matte clean? Ship. Don't touch SAM2.
> - Matte has JUNK around the actor (floor, crew, c-stand)? Turn INVERT **ON**, click the junk.
> - Matte has HOLES inside the actor? SAM2 can't fix that — fix despill or screen type instead.

---

## 2. WHAT SAM2 ACTUALLY GIVES US (capabilities inventory)

From the SAM2 API research:

**Already wired in CorridorKey today:**
- Positive clicks (left-click)
- Negative clicks (right-click) — Berto was right, these exist
- Forward + backward video propagation (two passes from anchor frame)

**Available in SAM2 but NOT exposed today:**
- **3 mask hypotheses per click** (whole/part/sub-part) — code auto-picks best, UI doesn't expose
- **Mid-clip prompt corrections** — `add_new_points_or_box` works on ANY frame, not just anchor
- **Multi-object tracking** — `obj_id=1` is hardcoded; SAM2 supports many objects in one inference state
- **Box prompt** — drag a rectangle (faster than many clicks for big garbage areas)
- **Mask refinement seed** — feed a prior mask back as input to iteratively improve
- **Mask threshold** — logit cutoff (-4..+4, default 0); lower = looser/larger mask
- **Hole-fill / sprinkle-removal** at the SAM2 stage (tunable)
- **Non-overlap masks** — force multi-object masks to be mutually exclusive

**Cannot be exposed (baked into the model):**
- Memory bank size, attention layers, occlusion scores, online streaming inference

---

## 3. WHAT THE COMPETITION DOES (UX patterns we should copy)

From the AE Roto Brush 3 / Resolve Magic Mask 2 / Runway / Premiere / Topaz teardown:

| Pattern | Source | Why it matters |
|---|---|---|
| **Persistent Add/Subtract buttons** (not modifier keys) | Resolve, Runway | Berto's users drop many clicks per shot — modifier-key model causes mis-clicks |
| **Click dots persist on canvas with +/- glyphs** | Runway, Resolve | Lets user delete one bad click instead of starting over |
| **Hotkeys 1=include, 2=exclude** | Runway | Faster than aiming at a button between clicks |
| **Mid-clip corrective click creates a new keyframe** | AE, Resolve, Runway | Universal drift-fix pattern — fix frame 50 without restarting frame 1 |
| **Sharp vs Smooth output mode** | Premiere | For a GARBAGE matte, Sharp/binary is correct — NN already keeps the soft edge |
| **Color-code by intent** | Topaz trimap | Red overlay where SAM2 is subtracting + green where NN keeps = legible at a glance |
| **No 3-mask UI** | Universal | NO pro tool exposes the 3-mask choice — auto-pick + "click again if wrong" is the universal model |

The last one matters: **don't build the 3-mask picker UI in v1.** Real users prefer "click again if wrong" over "pick mask 1, 2, or 3."

---

## 4. SHIPPING PHASES

### v1 — INVERT (target: this week)

**Scope:**
- Single global **INVERT MASK** checkbox in the SAM2 panel.
- Warning label next to checkbox: *"SAM2 now removes what you click instead of keeping it. Click on garbage (floor, props, crew) — NOT on the actor."*
- Defaults: INVERT off, MARGIN 0, SOFTEN 0.
- **Red-tint preview overlay** of what will be subtracted, BEFORE propagate (this is the safety feature — user sees the damage before committing).
- **Auto-CLEAR on clip switch** and on INVERT toggle (toggling polarity changes meaning of existing dots).
- Fix `CLEAR` button to truly reset state (closes `corridorkey_clear_button_corrupts_state.md`).
- Hard-block anchor-frame-outside-scrub-range with a readable error (closes `corridorkey_scrub_anchor_bug.md`).

**What NOT to touch in v1:**
- Multi-object stays single-object (hardcoded `obj_id=1`).
- Mid-clip prompts stay disabled.
- 3-mask picker not exposed.
- No mask refinement, no box prompt.
- Engine-side combine math change goes in only ONCE — at all 5 multiply sites identified by the audit (`CorridorKey_Pro.py:496, 682, 2322, 2601, 2778`, plus the AE viewer).

**Engine change is tiny:** at every `final = nn_alpha * gate` site, swap to `final = nn_alpha * (gate if not invert else 1 - gate)`. One helper function, called from 5 places. Single source of truth.

### v1.5 — Multi-object + mid-clip correction (next sprint)

**Scope:**
- Multi-object SAM2 (drop `obj_id=1` hardcode; support up to N tracked regions in one shot — floor, c-stand, crew, etc.)
- Mid-clip prompt support — add/refresh clicks at any frame, propagate forward from there. Drop `clear_old_points=True` and add a "fix from here" mode.
- Visible click dots on canvas with +/- glyphs (Runway/Resolve pattern).
- Hotkeys: `1` = positive click mode, `2` = negative click mode.
- Undo on click placement.
- "Preview before propagate" — single-frame SAM2 result shown before the range render commits.

### v2 — Mixed polarity, FILL mode, edge cases (after real-shot feedback)

**Scope:**
- **Per-object polarity** — object 1 = subtract floor, object 2 = subtract crew, object 3 = ADD chest hole-fill, all in one shot.
- **FILL mode** — positive SAM2 click that ADDS to NN alpha (rescues subject holes when despill can't fix them).
- Soft-edge SAM2 mask (feather the subtract boundary so motion-blurred edges survive).
- Box prompt (drag rectangle for big garbage areas).
- Mask refinement brush (use SAM2's mask_input seed).
- Automated two-pass mode — NN-only output + SAM2-gated output, blended at a user-set waist line for the documented hair workflow.

---

## 5. WHAT BREAKS THIS DESIGN (and what doesn't)

### Works perfectly with v1 (no/trivial SAM2):
- Locked-off wide on clean cyc
- Mid-shot against pop-up green panel that crops out floor
- Hair-flying punch closeup with rigging above frame
- Static foreground prop (one INVERT-on click)
- Visible taped seam well behind actor
- Crew member parked in deep corner

### Breaks v1 (needs v1.5 or v2):
| Failure | When it happens | Fix lives in |
|---|---|---|
| Subject HOLE in NN alpha (chest goes gray) | Tan vest, yellow shirt, green shadow | v2 FILL mode (or: fix despill — not a SAM2 problem) |
| Garbage crossing actor (c-stand behind torso) | Anything close to the body | v2 per-object polarity, or soft subtract |
| Actor walks into garbage zone | Floor click + walking actor | v1.5 mid-clip prompt |
| Crew enters mid-clip | Crew member walks into shot | v1.5 mid-clip prompt |
| Reflections on mylar floor | Glossy stage shoots | Two-pass workflow (existing escape hatch) |
| Transparent fabric near garbage | Wedding dress over a c-stand | Two-pass workflow |
| Motion blur edge bleeding into garbage | Fast arm sweep over a stand | v2 soft-edge SAM2 |

---

## 6. WHERE TO TOUCH THE CODE (v1 implementation map)

Based on the audit. NOT a green light to write code yet — this is the inventory for when Berto says go.

**Add INVERT toggle:**
- New checkbox in viewer SAM2 panel — `resolve_plugin/preview_viewer_v2.py` near the SAM/CLEAR/APPLY MASK row at `:920-929`.
- Mirror in AE viewer at `ae_plugin/cep_panel/preview_viewer_v2.py`.
- Persist to `live_params.json` alongside existing `sam_positive`/`sam_negative` (add `sam_invert: bool`).
- Read in panel `_merge_live_params` at `CorridorKey_Pro.py:491-493`.

**Apply polarity at combine sites** (5 places + AE viewer = 6 total):
- `CorridorKey_Pro.py:2105-2111` (scrub keying)
- `CorridorKey_Pro.py:2670-2676` (BRAW range)
- `CorridorKey_Pro.py:2880-2894` (standard range, propagated + static fallback)
- `preview_viewer_v2.py:386-398` (live preview)
- `preview_viewer_v2.py:1546-1568` (matte view)
- `ae_plugin/cep_panel/preview_viewer_v2.py:318-329`

**Wrap in single helper** — define `apply_sam2_gate(alpha, gate, invert: bool) -> alpha` in one shared module, call from all 6 sites. Eliminates the drift problem the audit flagged.

**Auto-CLEAR plumbing:**
- INVERT toggle handler must call `_clear_sam_points` (already exists at `preview_viewer_v2.py:1755`).
- Clip-switch path must call same.
- BUG FIX: also clear the global `sam_points` dict in panel (`CorridorKey_Pro.py:153`) — currently the viewer clears PNGs but the panel can re-merge stale state.

**Red-tint preview overlay:**
- Run SAM2 once on the anchor frame as today.
- New: render a red-tinted alpha-50% overlay of the SAM2 mask on top of the live frame BEFORE propagate fires.
- User sees what will be subtracted; clicks PROPAGATE to commit, or adds/removes clicks first.

**CLEAR button fix** (closes `corridorkey_clear_button_corrupts_state.md`):
- Reset SAM2 in-memory gate (currently leaves "weird black mass").
- Trigger viewer repaint.
- Already deletes both PNGs — keep.

---

## 7. KNOWN STRUCTURAL HAZARDS (from audit, do not ignore)

- **Three propagation call sites with drifting anchor-frame conventions** (range-relative in 2, absolute in 1). v1 doesn't change this but v1.5 mid-clip prompts WILL trip over it. Plan to unify the contract before adding mid-clip support.
- **Two parallel mask files** (`sam2_mask.png` binary + `sam2_gate_raw.png` float) — written together, only one read in panel fallback. v2 should consolidate.
- **`CLOSE_KERNEL_PX = 101`** in propagation (`CorridorKey_Pro.py:846`) — fills gaps between positive clicks defining the body. WRONG semantics for garbage matte (would close gaps between independent junk regions). v1 must skip this kernel when INVERT is on.
- **Margin/Soften default mismatch** between Resolve viewer (float ÷10) and AE viewer (int 0–80). Same key name, different units silently. Fix during INVERT plumbing pass.
- **`alpha_method` auto-flip on point presence** — deprecated UI but the branching still controls 5 code paths. INVERT plumbing must respect this.
- **Viewer post-proc differs from render** (trimap fusion in viewer vs simple multiply in render). Documented preview/render parity hazard. v1 INVERT must be applied in BOTH paths identically — single helper function approach addresses this.

---

## 8. BERTO'S DECISIONS (confirmed 2026-04-28)

1. **3-mask picker UI** — BUILD IT. Replaces today's single `APPLY MASK` button with three buttons (`1 / 2 / 3`), each labeled with its SAM2 IoU confidence %. Workflow:
   1. User places dots on anchor frame (positive / negative — same as today, multi-click additive).
   2. User clicks button `1` — SAM2 runs the anchor-frame inference, returns 3 mask hypotheses, shows mask #1 as red preview overlay.
   3. User can click `2` or `3` to swap the preview to those hypotheses. No commit yet.
   4. User clicks `PROPAGATE` to run SAM2 through the rest of the clip with the chosen mask.
   - All three buttons stay visible until PROPAGATE. The previously-clicked button is highlighted to show which hypothesis is currently previewed.
   - The button with highest IoU is pre-highlighted as a hint when buttons first appear.
   - This pattern preserves Berto's existing mental model (place dots → commit → propagate) while exposing SAM2's native 3-hypothesis output.
2. **Click input** — THREE ways, all available, all do the same thing:
   - Left-click = positive, right-click = negative (default — Berto's preference)
   - On-screen `+` and `−` buttons in the panel (Resolve Magic Mask style — visible for new users)
   - Keyboard shortcut as optional alternative (for users whose mouse buttons are already mapped to other apps)
3. **Red-tint preview overlay** — click-to-commit. Red overlay appears showing what will be subtracted. User clicks PROPAGATE to commit, or adds/removes clicks first.
4. **Two-pass workflow automation** — DO NOT BUILD as an automated pipeline. The two-pass workflow was a WORKAROUND for the old "SAM2 as subject mask" problem (where SAM2 chopped hair). The new INVERT/garbage-matte approach should remove the need for it entirely — because we never touch the NN matte, the hair detail is preserved automatically. **Test the INVERT approach on real footage first.** If it still kills the feet (or whatever specific failure shows up), the fix lives INSIDE the plugin/preview window — not in a "render twice and comp in DaVinci" workflow. Berto is firm on this: fixes belong in the plugin, not the NLE.
5. **NEW — Export mask separately (bonus feature)** — add an option to export the SAM2 mask alone (without combining it with the NN alpha) for users who want to do their own effects work in DaVinci/AE. Slot this into v1.5 alongside multi-object support.

---

## 9. APPENDIX — research artifacts

This plan synthesizes 4 parallel research agents from 2026-04-28:
1. SAM2 API capabilities (Meta's sam2 package + paper)
2. Audit of current CorridorKey SAM2 integration (file:line citations)
3. Competitive UX teardown (AE Roto Brush 3, Resolve Magic Mask 2, Topaz, Runway, Premiere, Mocha)
4. Edge cases / failure modes / shipping phasing

Full agent reports were inlined into this plan; raw outputs not preserved separately.
