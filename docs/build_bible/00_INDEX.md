# Build Bible System: Index

**Read this file first. It is the router. Then load only the one part that matches the task. Do not load every file.**

This is a reusable system for documenting a finished project so it can be rebuilt correctly and so no AI can stall the work by claiming a piece "cannot be done." It applies to every completed build, software or hardware. You instantiate it once per project by filling each part with that project's specifics.

The layering exists for one reason: an agent should answer a question by reading one small part, not the whole book. Reading the licensing part should never require loading the hardware part.

---

## The two layers

- **Layer 0, this index.** Cheap, always read first. It routes, carries the hardest guardrails, and holds the protected list.
- **Layer 1, the parts.** One file per concern. Self-contained. Loaded only when the task touches that concern.

A third layer is optional per project: deep appendices (full configs, wiring tables, long code) that a part points to but does not inline, so the part stays small.

---

## How to use this system

**To document a finished project:** work through the parts in order, filling each with that project's real specifics. Where a part does not apply (for example licensing on an internal hardware rig), write "not applicable" and one line saying why, rather than deleting the part. The structure stays constant across projects so the router always works.

**To answer a question about an already-documented project:** match the task to a row below, load that one part.

| If the task involves... | Load | Notes |
|-------------------------|------|-------|
| What the project is, its non-negotiable rules | `01_identity_and_rules.md` | Short, always safe |
| The data or signal flow, the stack, the process model | `02_architecture_map.md` | The map |
| Detailed spec of one subsystem or component | `03_component_template.md` | Repeatable block, one per component |
| The centerpiece others call impossible | `04_the_hard_part.md` | The proof-of-possible, plus the graveyard of failed paths |
| Inputs, controls, how users or systems drive it | `05_interfaces_and_controls.md` | |
| Config, storage, licensing, keys, calibration, persistence | `06_state_data_licensing.md` | Flexes by project type |
| Building, packaging, signing, shipping | `07_build_and_ship.md` | The build chain |
| A bug that smells familiar, a trap that bit once | `08_gotchas_and_warnings.md` | Scan first, solved traps only |
| Rebuilding the whole thing from a clean machine | `09_rebuild_runbook.md` | Step by step |
| An AI is saying part of this cannot be done | `10_ai_pushback.md` | Rebuttals, load when blocked |
| Code style, comments, file size, danger-zone tags | `11_coding_standard.md` | Constant across all projects |
| Values that drift between releases | `12_drift_and_factcheck.md` | Verify against live |
| Something currently broken, undecided, or half-verified | `13_open_issues_and_decisions.md` | The open ledger, not scar tissue yet |

---

## Routing rule by change type

- **Advice-only question** (explaining, planning, opinion, no edit): load the index plus the one matching part. Nothing else.
- **Any CODE or CONFIG change:** load the index, the task's part, and `08_gotchas_and_warnings.md` regardless of topic, because a fix that ignores known scar tissue re-breaks it. If the change touches anything on the protected list below, stop and confirm before touching it, even if the task looked small when it was assigned.

---

## Protected list (fill per project)

The constants, files, and physical settings that must never change without tracing the full call chain first. This is the actual list, kept here because this is the one file every session reads. Part 01 restates the rule and the reason for each entry; this table is what a fast pass checks before any edit.

```
| Item | Why protected | Full detail in |
|------|---------------|-----------------|
| "CK IS the matte" law | Every other stage (SAM2, merge, zone tools) supports the CK matte, never replaces it as the primary source. Two SAM-primary redesigns already dead-ended twice. | Part 01, Part 03 |
| SUBTRACT / merge formula: alpha * dilate(SAM2_binary, margin), plain multiply | 5+ "smarter" rewrites (chroma-weight, trimap+CFM, geodesic, connected-component) each broke a different real clip over 3+ weeks. | Part 03, Part 04, Part 08 |
| No Gaussian blur anywhere in the SAM merge pipeline (mask, weight, soft transition) | Produces ghost/banding artifacts at body-green edges every time it has been tried. Relearned 3+ times. | Part 01, Part 03, Part 08 |
| Two-mask / two-object SAM2 tracking | Dead end twice, a month apart (2026-05-03 branch, re-litigated 2026-06-22). Never re-port from DaVinci. | Part 01, Part 08 |
| sam2_combine.py apply_sam2_gate (single combine entry point) | 6-7 call sites depend on this one function; a second combine path caused range-vs-single-frame drift once already. | Part 03, Part 08 |
| corridorkey_sam_merge.py MERGE_MODE = "garbage_matte" | The only mode meant to ship; chroma_gated / path_b are hot-revert fallbacks only. | Part 03, Part 12 |
| SAM2 video-predictor frame shape (native, never square-padded) | A silent format bug (PNG-square padding) broke temporal propagation for a month; DaVinci had already found and reverted the same bug once before. | Part 04, Part 08 |
| SAM2 always a fresh subprocess for SAM2 commands, warm worker only for CNN-only jobs | A warm SAM2 worker leaks non-daemon threads and Hydra atexit state and hangs the broker after finishing SAM2 jobs. | Part 03, Part 04, Part 08 |
| ck_broker.py loopback bind + shared secret (name and role only, never the value) | The bridge that lets CUDA run outside the CEP sandbox; must never be reachable off 127.0.0.1 or accept unlisted subcommands. | Part 03, Part 06 |
| ck_launch.py | Superseded, dead code, kept only as inert fallback. Do not resurrect as the CUDA-sandbox fix. | Part 03, Part 04 |
| ae_plugin/cep_panel/ Windows junction | AE's real CEP extension folder; editing ae_plugin/ae_processor.py (repo root) does nothing at runtime. Never restore copy-based install without removing the junction first. | Part 03, Part 07, Part 08 |
| ae_plugin/ae_processor.py (repo root) | Explicitly marked DEAD DUMMY. install.py once let this overwrite the live cep_panel copy on clean install; fixed 2026-05-29, must not regress. | Part 03, Part 07, Part 08 |
| deploy.py | The only safe day-to-day deploy path. write_plugin.py is deprecated (no-backup footgun, not deleted). | Part 03, Part 07 |
| install.py Resolve installer target | Currently deploys the superseded resolve_corridorkey.py / core / ui legacy plugin, not the live CorridorKey_Pro.py. Verify which generation is live before trusting an install. | Part 03, Part 07, Part 12 |
| CorridorKey_Pro.py get_current_frame_info (GetStartFrame subtraction trap, CAP_PROP_POS_FRAMES vs POS_MSEC) | clip.GetStart() is absolute; subtracting it a second time sends every seek negative and clamps to frame 0. | Part 03, Part 08 |
| resolve_plugin/CorridorKey_Pro.py:2399 StartRendering() (Path B) | Banned for BRAW/camera-raw stills; resets the Windows audio engine and kills ASIO/WDM devices. | Part 03, Part 08 |
| ALIGNMENT.md / host.jsx frame functions (ppro_getFrameInfo, ppro_getInOutInfo, ppro_importFrame, ppro_importSequence) + ae_processor.py batch frame math | Four Premiere API quirks whose compensations interact; fixing one in isolation reintroduces the others. Broken and re-fixed four times in three days. | Part 02, Part 08 |
| CSXS/manifest.xml | A bare "--" inside an XML comment silently kills the entire extension load with no error UI. | Part 03, Part 07 |
| braw-decode.exe byte-read contract (_read_exact must consume exactly width*height*4 bytes/frame) | The exe streams raw BGRA with no frame separator; one short read desyncs every subsequent frame. | Part 03, Part 08 |
| generate_alpha_hint (inline RGB chroma test) vs AlphaHintGenerator (HSV) | The HSV class is explicitly banned in a DANGER ZONE comment (flags tan/khaki/olive as screen color) but still exists uncalled in the tree; never wire it back in. | Part 03, Part 08 |
| CorridorKeyModule/__init__.py import boundary | Viewer-subprocess helpers must live outside this package or torch gets imported eagerly into a process that should not carry it. | Part 03 |
| preview_viewer_v2.py (Qt popup, both copies) | Retired; the in-panel canvas preview replaced it. Its removal once silently orphaned ~22 tuning controls that only it wrote to live_params.json. | Part 03, Part 06, Part 08 |
| CC-BY-NC-SA-4.0 license inheritance | Upstream engine (Niko Pueringer / Corridor Digital) is NonCommercial; CorridorKey-StuntWorks cannot be sold under any packaging. | Part 01, Part 06 |
```

> If a task asks to edit anything on this list, stop and confirm before touching it.

---

## Evidence tags

Every claim in Parts 04, 08, 10, 12, and 13 carries one of these tags, so a reader knows how much to trust it without re-deriving it.

- **VERIFIED**: confirmed against the live, running, or shipped artifact. The strongest tag.
- **PARTIALLY VERIFIED**: confirmed in some conditions or environments, not all. State which.
- **HYPOTHESIS**: a reasoned guess, not yet tested. Treat as a question, not a fact.
- **FAILED APPROACH**: tried, did not work, root cause known. Lives in a graveyard (Part 04) or a failed-approaches list (Part 13).
- **STALE-REVERIFY**: was verified once, but conditions have since changed enough that it needs a fresh check before anyone relies on it.
- **USER DECISION**: not a technical fact. The human chose this on purpose; it is not up for AI debate.

Convention: every tagged claim carries `Last verified: <date>` and `Recheck when: <trigger>`. The trigger is the specific event that should force a recheck (a dependency bump, an OS update, a new hardware rev), not a vague "periodically."

---

## Full rebuild load order

Only when reconstructing an entire project: 01, 02, 04, then 03 per component, then 05, 06, 07, verify against 08, 09, and 13. Otherwise never chain-load.

---

## Filling checklist (per project)

A bible is "done" when:

- [ ] Part 01 names the project, its rules, and its protected list.
- [ ] Part 02 has a real signal or data flow diagram, not prose.
- [ ] Part 04 states the hard part and the working solution to it.
- [ ] Part 04's graveyard is filled with every approach tried before the working one.
- [ ] Part 09 rebuilds from zero without referencing memory that only lives in the builder's head.
- [ ] Part 10 lists the actual "cannot be done" claims this project already disproved.
- [ ] Part 12 marks every drift-prone value as check-against-live.
- [ ] Part 13 exists even if empty, and is not skipped just because nothing is open today.
- [ ] Every technical claim in Parts 04, 08, 10, 12, and 13 carries an evidence tag.
