# Part 06. State, Data, and Licensing

**Scope: everything CorridorKey persists, validates, or must keep secret. Load this for config, output paths, the license constraint, secrets, and tuning constants.**

---

## Configuration

`ck_broker_config.json` (host, port, secret): machine-local, auto-generated on first run, gitignored, never committed (Part 06.D). `corridorkey_path.txt`: points the plugin at the engine root; validated against repo markers rather than trusted blindly, after a user-writable pointer being prepended unconditionally to `sys.path` was flagged as an arbitrary-code-execution risk and fixed (Part 08). `CORRIDORKEY_ROOT` environment variable: the highest-priority override for engine location, ahead of `corridorkey_path.txt` and the sibling-folder guess.
Tag: VERIFIED. Last verified: 2026-07-22 (INSTALL.md section 1; ae_plugin/cep_panel/ae_processor.py `find_corridorkey_root`).

## Persistent data

Rendered output writes to a `CorridorKey` folder next to the host project. Default codec PNG 8-bit; Resolve v1.0 also offers PNG 16-bit, TIFF 16-bit, and EXR 32-bit; AE and Premiere stay PNG 8-bit until v1.1.
`live_params.json` used to be the only place roughly 22 tuning-slider values lived, written exclusively by the (now-retired) Qt preview window. When that window was removed without porting the write path, the file stopped being written and every slider silently fell back to a hardcoded `despill=1.0` maximum in both preview and render (peach/salmon shirts). Fixed by making the panel itself own the settings, with preview and render sharing one `getSettings()` source of truth (Part 08).
`render_ledger.json` at the repo root tracks renders; its exact schema and consumers were not deeply audited this pass, flagged in Part 12 as check-against-live rather than assumed current.
Tag: VERIFIED (paths and the live_params.json history), PARTIALLY VERIFIED (render_ledger.json's current role). Last verified: 2026-07-22.

## Licensing / access

No license key, no trial mechanism, no server-backed validation exists, and none is needed: CorridorKey is distributed free under CC-BY-NC-SA-4.0 (Part 01 Rule 6). The constraint that actually matters is legal, not technical: the upstream engine's NonCommercial clause forecloses a paid path for the whole project, not just the engine file, so there is no license scheme to design in the first place. A 2026-05-21 release plan built around an LLC, an EV code-signing certificate, and Stripe pricing was abandoned specifically because of this. Revenue, if any, is reputation-based (a Ko-fi tip link riding the Corridor Crew / StuntWorks Cinema audience), never a purchase gate.
Tag: USER DECISION. Last verified: 2026-05-21. Recheck when: the upstream engine's own license changes, or a repackaging/rebrand is proposed.

## Secrets

`ck_broker_config.json`'s `secret` field: a shared authentication token between the CEP panel's `ck_send.py` and the always-running `ck_broker.py`, preventing an arbitrary local process from submitting jobs to the broker. Name and role only, recorded here; the value is gitignored, machine-generated on first run, never committed, and does not appear anywhere in this bible.
Tag: VERIFIED (existence and role). Last verified: 2026-07-22 (ae_plugin/cep_panel/ck_broker.py header, .gitignore). Recheck when: the broker's auth scheme changes.

## Calibration / tuning

The on-green HSV lower bound appears in multiple call sites as the hue/sat/val triple (35, 50, 50) as of this pass (2026-07-22, verified in `ae_plugin/cep_panel/ae_processor.py` lines 357, 1426, and 1533, and `corridorkey_sam_merge.py` line 1546), even though a 2026-06-22 fix is recorded in memory as having lowered the value floor to (35, 50, 20) specifically to stop green spill leaking on dark-shadowed green behind black pants. The lowered value does not appear anywhere in the current tree. This is an unreconciled drift between what a past session recorded as fixed and what the live code actually contains; it is not assumed fixed here, and is carried forward into Part 12 and Part 13 rather than silently trusted.
Tag: STALE-REVERIFY. Last verified: 2026-07-22. Recheck when: before trusting any green-spill-on-dark-clothing behavior, or before closing the matching Part 13 issue.

`SAM_BASELINE_SMOOTH_SIGMA = 1.0` (`corridorkey_sam_merge.py`) and a separately-tracked SAM smoothing sigma of 2.5 in the v1 code path are both protected constants (Part 01, Part 03). Do not change either without a 4K checker-pattern test and a hair-heavy clip test.
Tag: VERIFIED. Last verified: 2026-07-22 (corridorkey_sam_merge.py:95).

---

## What not applicable here

There is no hardware calibration (no rig, no physical device) and no per-machine license activation to document; both sub-sections of the template that would normally cover those are not applicable to this project.
