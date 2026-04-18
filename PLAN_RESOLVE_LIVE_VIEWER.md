# PLAN — Port AE Live-Preview Viewer to DaVinci Resolve

**Goal:** Resolve's slider changes feel as responsive and visually dramatic as AE's live preview, without re-running the neural net for every drag.

**Current state (2026-04-17):** Resolve re-runs the full keyer on every slider change. Works but each slider move = 1+ second neural-net pass. AE caches NN output once and re-applies post-processing (despill, refiner, despeckle, composite) in ~50 ms per drag.

---

## 1. What AE Does That Resolve Doesn't

- CEP panel launches `preview_viewer.py` (at repo root, ~633 lines) as a persistent subprocess with `-u` stdin
- Panel runs the neural net ONCE via `ae_processor.py` subcommand `cache` → writes raw FG + alpha tensors to a session dir (`%TEMP%/ck_session_XXXX/`)
- Panel streams JSON-per-line slider updates to viewer's stdin:
  - `{"cmd": "update", "despill": 0.5, "refiner": 1.0, "despeckle": true, "despeckleSize": 400, "background": "checker"}`
- Viewer holds the cached FG + alpha in memory, re-applies **post-processing only** on each update, redraws in < 50 ms
- Full re-key only runs when the user switches frames or explicitly reloads

Reference: `D:\New AI Projects\CorridorKey-Plugin\PLAN_LIVE_PREVIEW.md` (Section 4 — Architecture)

---

## 2. Target Architecture for Resolve

```
CorridorKey.py  (Fusion Python, in-process)
  │ on first Preview click:
  │   1. read frame from Track 1 at playhead
  │   2. run neural net ONCE → get fg_raw + alpha
  │   3. write session dir: orig.png + fg_raw.npy + alpha.npy + settings.json
  │   4. spawn preview_viewer.py with session dir path
  │
  │ on every slider change:
  │   - write settings.json only (no NN re-run)
  │   - viewer's file-watcher on settings.json triggers fast post-process
  │
  │ on frame change:
  │   - re-run NN, overwrite session dir, viewer reloads
  │
preview_viewer.py  (PySide6 subprocess)
  │ loads fg_raw.npy + alpha.npy into memory ONCE
  │ watches settings.json for mtime change (300 ms poll)
  │ on change: apply despill_opencv + clean_matte + composite → repaint
```

---

## 3. Phases

### Phase 1 — Extract post-processing into a shared module
- New file: `CorridorKey-Plugin/resolve_plugin/live_postproc.py`
- Function: `apply(fg_raw, alpha_raw, settings) -> (composite, fg, matte)`
- Mirrors `CorridorKeyModule/core/color_utils.py` functions: `despill_opencv`, `clean_matte_opencv`, `srgb_to_linear`, `linear_to_srgb`, `composite_straight`
- **All numpy, no torch** — keeps viewer fast and GPU-free

### Phase 2 — Add session dir + raw tensor caching to `CorridorKey.py`
- After `process_frame` returns, save `fg_raw.npy` + `alpha_raw.npy` to a per-frame session dir
- Session dir: `%TEMP%/ck_session_<frame_num>/`
- Write `settings.json` with current despill/refiner/despeckle values

### Phase 3 — Rewrite `preview_viewer.py` for settings-file watching
- On startup: load fg_raw.npy + alpha_raw.npy into memory
- QTimer watches `settings.json` mtime (300 ms)
- On change: call `live_postproc.apply(...)` with new settings, redraw
- Keep current mode/zoom/pan state

### Phase 4 — Swap slider handlers from full re-key to settings-write
- `on_despill_changed` / `on_refiner_changed` / `on_despeckle_changed` → just write `settings.json`, no full pipeline call
- Full re-key only on playhead move (new frame)

### Phase 5 — Validate against AE
- Same frame, same settings → identical preview output in both AE and Resolve
- Slider drag latency < 100 ms in Resolve

---

## 4. Files Touched

- `resolve_plugin/live_postproc.py` — NEW (~150 lines)
- `CorridorKey-Plugin/resolve_plugin/preview_viewer.py` — major rewrite
- Installed `CorridorKey.py` — slider handlers rewritten, add session-dir caching
- Optional: share `live_postproc.py` with AE so both platforms use the same code

---

## 5. Risks

- Qt QFileSystemWatcher is flaky on Windows — use QTimer mtime polling (already proven)
- Race condition: main plugin writing settings.json while viewer reads it. Fix: write to `settings.json.tmp`, rename atomically.
- Neural-net output tensors can be 50–200 MB uncompressed. Use `np.savez_compressed` or mmap.

---

## 6. Estimated Time

- Phase 1: 45 min
- Phase 2: 30 min
- Phase 3: 60 min
- Phase 4: 30 min
- Phase 5: 30 min
- **Total: ~3 hours** on a focused session

---

## 7. Why It's Worth Doing

- Slider responsiveness matches AE (professional feel)
- No UI thread blocking — even on slow GPUs
- One post-processing implementation shared across AE and Resolve
- Foundation for future features: refiner-on-release threading, background plate preview, multi-frame scrubbing
