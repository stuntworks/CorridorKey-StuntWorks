# CorridorKey Plugin — Agent Briefing

## What This Project Is
CorridorKey is a neural-net green-screen keying plugin for video editors.
- Physically accurate unmixing — separates foreground color + alpha even on hair, motion blur, semi-transparent pixels
- Three host implementations: After Effects, Premiere Pro, DaVinci Resolve
- Built for StuntWorks / Berto Labs — used heavily in real stunt production work
- Free to share with other filmmakers. Branded with StuntWorks + YouTube channel link.

## Owner
Berto Lopez — StuntWorks / Berto Labs, NYC. 30 years stunt work.
- Dyslexic — types in ALL CAPS with phonetic spelling. Interpret intent, never correct spelling.
- Does not write code himself. Directs and tests.
- One question at a time. No walls of text.

## Code Rules (HRCS — Non-Negotiable)
- Every function gets a comment block: WHAT IT DOES / DEPENDS-ON / AFFECTS
- Fragile sections get DANGER ZONE flags
- Every file gets a one-line header: last modified date + change summary
- Plain English naming — no cryptic abbreviations
- Test before adding features. Never break working features.

## RULE — Never Mark Done Without Testing
- You CANNOT mark any task as done until you have run the relevant test and confirmed it works
- "Looks correct in the code" is NOT a test
- If you cannot run it yourself, set status to "in_review" and tell Berto exactly what to test
- Berto does the live test, confirms it passes, then you mark it done

## Git Rules
- After every fix, commit with a clear message and push to GitHub
- Never mark a task done without committing first
- Branch: `live-preview`

---

## CRITICAL — DO NOT TOUCH THESE

**AE and Premiere plugins work perfectly. Any edit breaks them.**
- `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\ae_processor.py` — DO NOT TOUCH
- `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\cep_panel\` — DO NOT TOUCH
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\` — engine, read-only imports only
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\core\color_utils.py` — import only, never edit

**Before touching ANY Premiere frame math** (`ppro_getFrameInfo`, `ppro_getInOutInfo`, `ppro_importFrame`, `ppro_importSequence`, or batch frame math in `ae_processor.py`) — read `ALIGNMENT.md` first. This alignment has been fixed FOUR SEPARATE TIMES. Every regression came from someone editing without understanding all four offsets.

---

## Current State (as of 2026-04-17)

### What Works
- **After Effects plugin** — production ready, working perfectly
- **Premiere Pro plugin** — production ready, working perfectly
- **Resolve plugin** — working but slow. Sliders apply, despill/refiner/despeckle reach engine. SAM manual corrections work great. Live preview is ON but runs full NN per click (~1 second per change).

### The Problem With Resolve
- AE latency: ~50ms per slider change (NN cached, post-proc only)
- Resolve latency: ~1 second per slider change (full NN re-run)
- Goal: port AE's caching architecture to Resolve

### What Was Fixed Last Session (2026-04-17)
| Fix | File | Status |
|---|---|---|
| `reprocess_with_cached` reuses cached model (was reloading per slider) | CorridorKey.py:589 | Done |
| `on_process_range` save block re-nested in loop (only last frame was saving) | CorridorKey.py:815 | Done |
| `on_close` calls cleanup + CUDA empty_cache | CorridorKey.py:875 | Done |
| Wrapper replaces raw fg with post-processed output | corridorkey_processor.py:88-140 | Done |
| `post_process_on_gpu=False` forced (GPU despill silently no-ops in Fusion) | corridorkey_processor.py:85 | Done |
| LineEdit replaces Fusion SpinBox/Slider (Fusion .Value was unreliable) | CorridorKey.py:~170 | Done |
| `__file__` NameError try/except fallback for Fusion env | CorridorKey.py:31 | Done |
| LivePreview defaulted OFF (blocks Fusion UI thread until Phase 4) | CorridorKey.py | Done |
| `reprocess_with_cached` validates frame identity before reusing cache | CorridorKey.py | Done |

### Uncommitted Changes
- `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
- `D:\New AI Projects\CorridorKey\resolve_plugin\core\corridorkey_processor.py`
- Local `live-preview` branch is ahead of commit `80f26e5` — push before rewrite

---

## The Rewrite — Resolve Live Viewer

This is the current task. Full plan: `PLAN_RESOLVE_LIVE_VIEWER.md`
Detailed investigation + ECC review: `INVESTIGATION_2026-04-17.md`

**READ BOTH FILES BEFORE WRITING ANY CODE.**

### 15 Master Corrections (apply to everything you build)
1. Refiner is NN-internal (register_forward_hook). Treat as full re-key trigger, 300ms debounce. NOT in live_postproc.py.
2. Session dir = UUID, not frame_num (prevents race on re-key same frame)
3. Pre-bake sRGB gamma LUT (cv2.LUT) — BUT gate on 8-bit input; separate float path for EXR
4. mtime poll at 100ms — BUT back with named pipe for settings; polling is fallback only
5. `.npy + mmap_mode='r'`, NOT np.savez_compressed
6. `clean_matte_opencv` on release only (45-85ms blows drag budget)
7. Atomic rename (tmp → final) for all cross-process PNG/npy writes
8. Import `color_utils.linear_to_srgb` directly — never reimplement
9. TextChanged debounce ~400ms before Phase 4
10. Realistic time: 6-7 hours, not 3 or 5
11. `CORRIDORKEY_PARENT_PID` env var + Windows OpenProcess watcher in viewer (kills viewer if Fusion dies)
12. Single-flight lock in panel for refiner re-key (prevents libpng race — caused crash before)
13. Lockfile in session dir, PID-scoped (reject 2nd viewer for same Fusion PID)
14. Colorspace tag in meta.json; live_postproc.apply() branches 8-bit LUT vs float math
15. live_postproc.py is copy-by-convention (two files, CI diff-test), NOT shared import between AE and Resolve

### 5 Past Mistakes This Rewrite Must Not Repeat
These are real bugs that happened before — from git history:
1. No orphan-viewer / parent-PID watcher → viewer stays open after Resolve closes (fix: correction #11)
2. No single-flight lock on refiner re-key → libpng crash (commit 99a2aee) (fix: correction #12)
3. No kill-before-respawn → duplicate viewers (commit 785a84f) (fix: correction #13)
4. No ALIGNMENT.md smoke test run after changes → frame drift regressions
5. Refiner treated as post-proc (separable) → wrong architecture, re-keys didn't work

### The 6 Phases
**Phase 0 — Environment probe (30 min)**
- Verify PySide6 imports in Fusion's embedded Python 3.10
- Verify `safetensors` in engine venv (D:\New AI Projects\CorridorKey\.venv)
- Probe one Resolve EXR timeline + one Rec.709 8-bit timeline; confirm dtype/range arrives
- GATE: if anything fails, STOP and report to Berto before continuing

**Phase 1 — Parity test harness (45 min)**
- Create `resolve_plugin/tests/test_parity.py`
- Feed known frame + known settings through AE path AND planned Resolve path
- Fail if pixel-diff > 2/255 per channel
- GATE: harness runs green before any Phase 2 code

**Phase 2 — `core/live_postproc.py` (60 min)**
- Numpy-only, imports color_utils.linear_to_srgb directly
- Function: `apply(fg_raw, alpha_raw, settings, is_drag, colorspace) -> composite`
- Pre-bake sRGB LUT at module load
- Branches: 8-bit LUT path vs float linear-math path
- clean_matte_opencv gated on is_drag=False
- Location: `resolve_plugin/core/live_postproc.py`

**Phase 3 — Session dir + tensor caching (45 min)**
- After NN run, write to `%TEMP%/ck_session_<uuid>/`
- Files: fg_raw.npy, alpha_raw.npy, meta.json (colorspace, frame_num, fps), settings.json, .lock (Fusion PID)
- All writes via tmp → os.replace() atomic rename
- Single-flight lock on refiner re-key

**Phase 4 — Rewrite viewer (90 min)**
- Load fg_raw.npy + alpha_raw.npy via mmap_mode='r'
- Named pipe for settings channel; mtime-poll as fallback
- On settings change: call live_postproc.apply(), repaint
- CORRIDORKEY_PARENT_PID watcher kills viewer if Fusion dies
- Check .lock file; refuse to start if sibling viewer owns same Fusion PID

**Phase 5 — Slider handler swap (45 min)**
- on_despill_changed / on_despeckle_changed → write settings only, no NN call
- on_refiner_changed → 300ms debounce → full re-key in worker thread
- LineEdit: 400ms TextChanged debounce, NaN + comma-decimal guards

**Phase 6 — Validation (60 min)**
- Run parity harness against 5 known frames
- Manual smoke: scrub + drag on EXR timeline AND Rec.709 timeline
- Run ALIGNMENT.md smoke test
- AE + Premiere regression: confirm byte-identical to pre-rewrite

---

## File Locations

**Engine (shared — read only):**
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\inference_engine.py`
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\core\color_utils.py`
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\backend.py`

**AE plugin (DO NOT TOUCH):**
- `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\ae_processor.py`
- `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\cep_panel\preview_viewer.py`

**Resolve plugin (rewrite target):**
- `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` (installed, ~900 lines)
- `D:\New AI Projects\CorridorKey\resolve_plugin\core\corridorkey_processor.py` (wrapper)
- `D:\New AI Projects\CorridorKey-Plugin\resolve_plugin\` (dev copy)

**Plans and history (READ BEFORE CODING):**
- `PLAN_RESOLVE_LIVE_VIEWER.md` — rewrite plan (pre-corrections, update with 15 corrections above)
- `INVESTIGATION_2026-04-17.md` — full ECC review, all findings, all corrections
- `ALIGNMENT.md` — mandatory smoke test for any Premiere-side changes
- `SESSION_2026-04-17_HANDOFF.md` — last session state
- `SESSION_2026-04-16_HANDOFF.md` — previous session state
- `CLAUDE.md` — additional rules and danger zones

## GitHub
- `D:\New AI Projects\CorridorKey-Plugin\` — git repo, branch: live-preview
- Commits ahead of `80f26e5` are uncommitted — push before starting rewrite
