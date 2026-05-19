# CorridorKey Release-Blocker Hardening Plan — Codex Review 2026-05-14

> Source: external Codex static review. Pasted verbatim. Findings NOT YET VERIFIED against current source. Verification task is on the TODO list: `todo-ck-codex-review-verify-2026-05-14`.

---

## Summary

- Goal: remove the release-blocker risks from the review without rewriting the product.
- Implementation order: `shared backend core` first, then `Adobe runtime hardening`, then `Resolve integration safety`.
- Success criteria: ranged video jobs are frame-accurate, incomplete/corrupt outputs never mark a clip complete, loose standalone videos never share one output root, installed Adobe panels only run from an explicit trusted engine root, Resolve export/import does not disturb project state, and these flows have regression coverage.

## Implementation Changes

- Add a shared frame-source layer for backend processing so indexed video reads and sequence reads use one contract. `run_inference()` and `reprocess_single_frame()` must use indexed/random-access reads whenever a specific frame number is requested; sequential `cap.read()` remains allowed only for full linear scans from frame 0.
- Tighten completion semantics in the backend. Keep the existing clip states, but never transition to `COMPLETE` unless the full input frame count is satisfied and validated. If input/alpha counts mismatch or frames are skipped, leave the clip in `READY` and attach a warning instead of silently truncating to success.
- Strengthen the manifest contract. Write the run manifest at job start with `run_state`, `expected_frame_count`, `enabled_outputs`, and `formats`. Only flip `run_state` to `complete` after all required outputs for the full clip validate successfully.
- Make per-frame output writes atomic. Write each output to a temp file, validate that it can be read back, then rename into place. A frame only counts as successful after every enabled output for that stem passes validation.
- Tighten resume logic. A stem counts as complete only when every enabled output exists, is readable, and matches the expected extension/type from the manifest. Corrupt or partial files must be ignored and re-rendered.
- Add a `materialize_standalone_clip()` step for loose top-level videos. Discovery stays read-only, but before any write-producing action the app creates a dedicated project/clip folder under `Projects/` with `copy_source=False`, writes clip metadata, updates the in-memory clip root, and sends all future outputs there.
- Replace broad Adobe engine-root fallbacks with an explicit policy. Installed builds may use `corridorkey_path.txt` or an explicit developer env var only. Remove production fallback searching of hardcoded folders like `D:\New AI Projects\CorridorKey` and `~/CorridorKey`.
- Validate the resolved Adobe engine root before launch. Require the expected repo markers and runtime files to exist, including the engine module, venv, processor, and `preview_viewer_v2.py`. If validation fails, stop with an actionable error instead of importing from whatever path was found.
- Fix the Adobe live-preview install contract. The installer must copy the exact `preview_viewer_v2.py` that the panel launches and stop relying on legacy viewer names or engine-side fallbacks for the normal installed path.
- Unify device selection. AE/Premiere processor code and Resolve wrapper code must resolve device through shared detection and fall back to CPU with a clear warning when CUDA is unavailable, instead of assuming `device="cuda"` always works.
- Make Resolve render export side-effect safe. Snapshot current render settings and existing render job IDs, create one isolated job, enforce timeout/cancel behavior, delete only the created job, and restore prior render settings in a `finally` block.
- Make Resolve sequence import format-aware. Import logic must support TIFF alongside PNG/EXR and select the import candidate based on the actual output format being produced.
- Remove background-thread UI mutations in Resolve. Worker threads may only enqueue state updates; all widget text/status/progress resets must happen on the main UI queue.
- Remove the fixed `V1 is source` preview assumption in Resolve. Background-plate lookup must use the actual source track index for the current clip, so preview composites stay correct on non-V1 timelines.
- Guard Windows-only shutdown behavior. Keep the documented hard-exit fallback only for the Windows shutdown-hang path, and add a safe platform-aware terminate/join helper for non-Windows behavior.

## Public / Internal Interface Changes

- Internal processing adds a shared frame-source abstraction for indexed reads across video and sequence assets.
- Internal manifest schema adds `run_state` and `expected_frame_count` and becomes the source of truth for resume/completion validation.
- Processing contract for loose standalone videos changes from "write into the scanned source directory root" to "materialize into a dedicated project clip before first write."
- Installed Adobe runtime contract changes from "best-effort root search" to "explicit configured root only, with validation."

## Test Plan

- Add pytest coverage for backend ranged video inference using a tiny synthetic video fixture: full clip, non-zero start frame, bounded range, and single-frame reprocess must all read the exact expected source frame.
- Add pytest coverage for completion/resume behavior: alpha-count mismatch must not mark a clip `COMPLETE`, corrupt outputs must not count as completed stems, and a full validated run must flip manifest `run_state` to `complete`.
- Add pytest coverage for standalone-video materialization: two loose videos in one folder must produce two distinct clip roots and isolated output trees.
- Add resolver tests for Adobe root selection: valid configured root passes, missing markers fail, and removed fallback roots are not consulted in installed mode.
- Add non-host unit coverage for the render/import helpers where possible, then run manual host-app smoke checks for: TIFF roundtrip import, render-settings restoration after success/failure, preview background on source track not equal to V1, and CPU fallback warning behavior on a non-CUDA machine.
- Release acceptance gate: all automated tests pass, plus one manual smoke pass each in AE/Premiere and Resolve using a non-zero frame range, live preview, and an incomplete-alpha negative test.

## Assumptions

- Scope is `release blockers only`, not full cleanup or long-tail hardening.
- First implementation wave is `backend core`, then `Adobe runtime`, then `Resolve safety`.
- `Moderate refactor` is allowed for shared abstractions and manifest logic, but no full viewer or host-integration rewrite is planned.
- No new user-facing clip state will be added in this pass; incomplete runs remain `READY` with warnings.

---

## Findings (deeper scout, static review only, no host smoke tests)

- **Critical** — `backend/service.py:394-399` and `635-721`. `frame_range` changes the loop index, but video inputs still read sequentially from frame 0 with `cap.read()`. A request for frame 500 can process frame 0 and save it as frame 500. Likelihood: high whenever ranged processing is used on video clips.
- **Critical** — `ae_plugin/cep_panel/index.html:291-317`, `485-503`; `ae_plugin/cep_panel/ae_processor.py:47-73`. The AE/Premiere path trusts `CORRIDORKEY_ROOT` and `corridorkey_path.txt`, prepends that path to `sys.path`, and imports code from it. A malicious local user who can alter that pointer can get arbitrary Python executed inside the host app. Likelihood: medium single-user, high shared/support.
- **High** — `backend/clip_state.py:476-483`. Top-level standalone videos all get `root_path=clips_dir`, so multiple loose videos share one `Output/` and metadata root. Processing one can overwrite or confuse another. Likelihood: medium.
- **High** — `backend/service.py:608-612`, `788-795`; `backend/validators.py:23-51`. Input/alpha mismatches are truncated to the shorter count and can still transition the clip to `COMPLETE`. Missing tail frames can be silently treated as done. Likelihood: medium-high.
- **High** — `backend/service.py:431-452`; `backend/clip_state.py:475-483`, `197-233`. Outputs are written straight to final filenames; resume only checks whether stems exist, and manifest-write failure is non-fatal. Crashed or half-written outputs can be treated as complete on the next run. Likelihood: medium.
- **High** — `resolve_plugin/ui/uimanager_panel.py:401-405`, `434-436`, `466-467`. Background worker threads touch Fusion widgets in `finally` instead of queueing back to the main thread. This is the kind of race that often passes short tests and then crashes or hangs in real Resolve sessions. Likelihood: medium-high.
- **High** — `resolve_plugin/core/resolve_bridge.py:196-236`. Export mutates global render settings, adds/deletes render jobs, and polls `IsRenderingInProgress()` with no timeout or restore path. A stalled render or preexisting queue can hang the plugin or disturb the user's project render state. Likelihood: medium-high.
- **High** — `resolve_plugin/core/resolve_bridge.py:279-287`. Sequence import only searches `*.exr` and `*.png`, even though the product exposes TIFF output. TIFF sequences can export successfully but fail to reimport through this bridge. Likelihood: medium if TIFF is used.
- **High** — `resolve_plugin/CorridorKey_Pro.py:1993-2001`. Background-plate lookup assumes the keyed source lives on `V1` and only searches `V2+`. Any timeline using a different track layout can preview the wrong background or no background at all. Likelihood: medium.
- **High** — `ae_plugin/cep_panel/ae_processor.py:236-239`; `resolve_plugin/core/corridorkey_processor.py:54-74`; `ae_plugin/cep_panel/index.html:315-317`, `323-325`. The processing path assumes a local `.venv` layout and a working CUDA device instead of feature-detecting and degrading cleanly. Likelihood: medium-high outside the dev machine.
- **Medium-High** — `backend/service.py:620-628`, `688-703`. Failed `VideoCapture` opens/reads do not become hard errors; the job just accumulates skipped frames and returns partial results. Hard to diagnose in production. Likelihood: medium.
- **Medium** — `resolve_plugin/CorridorKey_Pro.py:4285-4291`, `4301-4305`. Cleanup relies on Windows `taskkill` and then `os._exit(0)`. On macOS/Linux the process-tree kill path is effectively swallowed. Likelihood: medium on non-Windows installs.
- **Medium** — `CorridorKeyModule/inference_engine.py:167-171`. Model loading uses `strict=False` and only `print()`s missing or unexpected keys. A partially incompatible checkpoint can keep running with subtly wrong output instead of failing fast. Likelihood: low-medium, very hard to debug when it happens.
- **Medium** — `CorridorKeyModule/backend.py:82-104`, `188-209`; `requirements.txt:14-15`, `33-34`. First-run behavior depends on external downloads, loosely pinned Torch/TorchVision, and separate SAM2 source installs. "Works here, not there" risk across drivers, CUDA wheels, and network conditions. Likelihood: medium.
- **Medium** — `resolve_plugin/test_panel.py:1-66`; `requirements-dev.txt:15-17`. Only one tracked "test" file, manual import smoke script. No tracked automated coverage for the batch pipeline, frame-range math, resume/state logic, or live viewers. Likelihood: high for regressions.
- **Medium** — `backend/clip_state.py:446-465`. Project scanning deduplicates by `clip.name`, not by path or stable ID, so two different clips with the same display name can cause one to disappear from the scanned result set. Likelihood: low-medium.

## Reviewer's overall

Biggest risks: frame-accurate video handling, resume/state correctness, host-app side effects in Resolve, machine-specific install/runtime assumptions with almost no automated protection.

## Reviewer's offer

When you're back, I can turn this into a fix order: `must fix before release`, `safe later`, and `test first`.
