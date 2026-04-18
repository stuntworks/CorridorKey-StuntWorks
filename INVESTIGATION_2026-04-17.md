# CorridorKey Resolve Rewrite — Consolidated Investigation

**Date:** 2026-04-17
**Status:** READY FOR AiConsensus REVIEW
**Reviewers completed:** 4 parallel ECC agents (contracts, historical, bug-hunt, architecture)

---

## 1. Project Context

**What it is:** CorridorKey is a neural-net green-screen keying plugin with three host implementations:
- **After Effects** (CEP panel) — production, working
- **Premiere Pro** (CEP panel, shares AE's `ae_processor.py`) — production, working
- **DaVinci Resolve** (Fusion script) — being rewritten for live preview parity with AE

All three hosts call the same shared Python engine (`D:\New AI Projects\CorridorKey\CorridorKeyModule\`).

**Current Resolve latency:** ~1 second per slider change (full NN re-run).
**AE latency:** ~50 ms per slider change (NN cached, post-proc only).
**Goal:** Port AE's architecture to Resolve without breaking AE or Premiere.

---

## 2. What Was Patched Today (Before Rewrite)

Applied to `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
and `D:\New AI Projects\CorridorKey\resolve_plugin\core\corridorkey_processor.py`:

| Fix | Where | Rationale |
|---|---|---|
| `reprocess_with_cached` reuses cached model | CorridorKey.py:589 | Was reloading NN per slider drag (VRAM churn, seconds of reload) |
| `on_process_range` save block re-nested in loop | CorridorKey.py:815 | Was dedented; only the last frame of a range was saved |
| `on_close` calls `cleanup()` + daemon `empty_cache` | CorridorKey.py:875 | Fix orphan python.exe holding CUDA after Resolve close |
| Wrapper replaces raw `fg` with engine's post-processed output | corridorkey_processor.py:88-140 | Despill slider was invisible (raw fg showing) |
| `post_process_on_gpu=False` forced | corridorkey_processor.py:85 | Fusion Python's CUDA doesn't fully init, GPU despill silently no-ops |
| LineEdit replaces Fusion SpinBox/Slider | CorridorKey.py:~170-183 | Fusion widget `.Value` was unreliable |
| `__file__` NameError try/except fallback | CorridorKey.py:31 | Fusion env doesn't always set `__file__` |
| LivePreview re-enabled with "Processing..." status | _live_rekey | Users want live visual feedback |

**Just now (2026-04-17 ECC round 2):**
- ✅ LivePreview defaulted to **OFF** (C1 safety — blocks Fusion UI thread)
- ✅ `reprocess_with_cached` now compares cached frame_num vs playhead; falls through to full re-key on mismatch (C2 safety — prevents preview showing wrong matte for wrong frame)

---

## 3. Four-Agent ECC Review — Consolidated Findings

### 3a. Upstream Engine (9 commits `80f26e5..cf587ec`) — AE/Premiere Impact

8 of 9 commits are LOW risk. Zero public-API signature changes.

**Only MED-risk item:** commit `3f398d3` added `safetensors` as a required dep. Engine venv needs `pip install safetensors` before next AE/Premiere launch, or `create_engine` crashes on first model load.

**Per-commit table:**

| SHA | Files | Public API delta | AE | Premiere | Resolve |
|---|---|---|---|---|---|
| `cf587ec` | inference_engine, service | internal only (error narrowing, empty_cache removed) | LOW | LOW | LOW |
| `75dddb3` | device_utils | internal guard | LOW | LOW | LOW |
| `3f398d3` | backend, pyproject | **+safetensors dep**, .pth fallback preserved | **MED** | **MED** | MED |
| `882e9df` | ci.yml | CI only | LOW | LOW | LOW |
| `374de08` | README | docs | LOW | LOW | LOW |
| `5d823c9` | gvm | LoRA loader hardening (not imported by AE/PP) | LOW | LOW | LOW |
| `9610c6c` | birefnet | `trust_remote_code=False` (not imported by AE/PP) | LOW | LOW | LOW |
| `11b2c14` | clip_manager, cli | CLI flags (not used by AE/PP — they use ae_processor.py) | LOW | LOW | LOW |
| `d836296` | device_utils | +`enumerate_gpus` (pure addition) | LOW | LOW | LOW |

**Smoke test AE + Premiere before rewrite:**
1. Run `ae_processor.py single` on one PNG → exit 0
2. Run `ae_processor.py batch` on 10 frames → PROGRESS streams
3. Launch `preview_viewer.py` against cached session → sliders re-apply
4. Ensure `D:\New AI Projects\CorridorKey\.venv` has `safetensors` installed

### 3b. Historical Review — Plan Repeats 5 Past Mistakes

| Issue | Source lesson | Status in current plan |
|---|---|---|
| No orphan-viewer / parent-PID watcher | commit `d6cf87e` | **missing** |
| No single-flight lock on refiner re-key | commit `99a2aee` libpng crash | **missing** |
| No kill-before-respawn | commit `785a84f` | **missing** |
| No ALIGNMENT.md smoke test | `ALIGNMENT.md` + PLAN_LIVE_PREVIEW §8 | **missing** |
| Refiner-as-post-proc in §2 diagram | PLAN_LIVE_PREVIEW §6.1 documented refiner is NN-internal | footnoted only, needs to be in architecture |

**Phasing violation:** AE plan required test-gate per phase. Resolve plan pushes validation to Phase 5. Historical lesson: "test before feature."

### 3c. Bug Hunt — 3 Critical, 4 High

**CRITICAL:**
- **C1** — `_live_rekey` runs NN on UI thread (freezes Fusion per keystroke) → **mitigated today** (LivePreview default OFF until Phase 4 worker thread)
- **C2** — `reprocess_with_cached` didn't validate frame identity (preview lied on stale frames) → **FIXED today** (frame_num check added)
- **C3** — Non-atomic PNG writes in `show_preview_window` (viewer reads torn files) → **must fix in Phase 2**

**HIGH:**
- **H1** — `on_close` daemon-thread `empty_cache` races with synchronous `cleanup()` → zombie python.exe
- **H2** — `on_process_range` leaks cap handle on save error (video file locks)
- **H3** — Wrapper mutates engine's shared result dict in place → double-despill on subsequent frames with `format=2`
- **H4** — LineEdit accepts NaN and locale-comma decimals → corrupted saved PNGs

### 3d. Architecture — APPROVE WITH CHANGES

**5 gaps beyond the 10 prior corrections:**

1. **mtime polling** on Windows NTFS has ~16ms resolution; can miss fast writes. Recommend hybrid: named pipe (or localhost socket) for settings, mmap npy for tensors.
2. **No tier-2 refiner-only re-run.** Refiner is a forward hook; could re-run only the hook layers with cached upstream features. Without it, refiner drags feel identical to frame reloads.
3. **Single-Resolve-session assumption silent.** Need lockfile + PID check; viewer refuses to start if sibling for same Fusion PID exists.
4. **Colorspace assumption undocumented.** Resolve timelines are often linear float EXR or ACEScct, not sRGB 8-bit. Pre-baked `cv2.LUT` optimization (correction #3) silently breaks on float data. Need colorspace tag in `meta.json` + branch in `live_postproc`.
5. **Sharing `live_postproc.py` with AE is a coupling trap.** Different Python versions, numpy versions, release cadences. Recommend copy-by-convention (two files, CI diff-tested), not shared import.

**Production risks:**
- Fusion 3.10 embedded Python may lack PySide6 → unverified assumption, blocks entire plan
- 4K EXR frames: 256MB+ per tensor × 2 → RAM pressure on 16GB laptops
- Resolve 19+ Fusion page reload on project switch → orphan subprocess + mmap bloat
- Float EXR silently routed through 8-bit LUT path → visual regression vs AE
- Windows Defender can hold `.tmp` file open → atomic rename fails

---

## 4. Master Corrections List (15 items)

The 10 original + 5 new architecture items + 5 historical = deduped to 15:

1. Refiner is NN-internal; treat as stage-1 full re-key, 300ms debounce. Not in `live_postproc.py`.
2. Session dir = **UUID**, not frame_num (prevents race on re-key same frame)
3. Pre-bake sRGB gamma LUT (`cv2.LUT`) — **but gate on 8-bit input; separate float path**
4. mtime poll at 100ms — **but back it with named pipe for settings; polling is fallback only**
5. `.npy + mmap_mode='r'`, NOT `np.savez_compressed`
6. `clean_matte_opencv` on release only (45-85ms blows drag budget)
7. Atomic rename (`tmp` → final) for cross-process PNG/npy writes
8. Import `color_utils.linear_to_srgb` directly — don't reimplement
9. `TextChanged` debounce ~400ms before Phase 4
10. Realistic time: 6-7 hours, not 3 or 5
11. **NEW:** `CORRIDORKEY_PARENT_PID` env var + Windows `OpenProcess` watcher in viewer (orphan kill)
12. **NEW:** Single-flight lock in panel for refiner re-key (prevents libpng race)
13. **NEW:** Lockfile in session dir, PID-scoped (reject 2nd viewer for same Fusion PID)
14. **NEW:** Colorspace tag in `meta.json`; `live_postproc.apply()` branches 8-bit LUT vs float math
15. **NEW:** Copy-by-convention for `live_postproc.py` (two files, CI diff-test), not shared import

---

## 5. Revised Rewrite Plan (Phase 0 → Phase 6)

### Phase 0 — Environment probe (30 min) **NEW**
- Verify PySide6 imports in Fusion's embedded Python 3.10
- Verify `safetensors` in engine venv (`.venv`)
- Probe one Resolve EXR timeline + one Rec.709 8-bit timeline; confirm numpy dtype/range arrives at plugin
- **Gate:** if any of the above fails, STOP and re-plan

### Phase 1 — Parity test harness (45 min) **NEW**
- Create `resolve_plugin/tests/test_parity.py`
- Feed one known frame + known settings through:
  - AE's full engine + post-proc path (`ae_processor.py single`)
  - Planned Resolve cache + live_postproc path (stubbed)
- Fail if pixel-diff > 2/255 per channel
- **Gate:** harness runs green before any Phase 2 code

### Phase 2 — `core/live_postproc.py` (60 min)
- Numpy-only, imports `color_utils.linear_to_srgb` directly
- Function: `apply(fg_raw, alpha_raw, settings, is_drag, colorspace) -> composite`
- Module-load: pre-bake sRGB LUT
- Branches: 8-bit LUT path vs float linear-math path
- `clean_matte_opencv` gated on `is_drag=False`
- Location: `resolve_plugin/core/live_postproc.py` (NOT repo root — matches existing `core/` layout)

### Phase 3 — Session dir + tensor caching (45 min)
- In Fusion plugin: after NN run, write to `%TEMP%/ck_session_<uuid>/` — `fg_raw.npy`, `alpha_raw.npy`, `meta.json` (colorspace, frame_num, fps), `settings.json`, `.lock` (Fusion PID)
- All writes via `tmp` → `os.replace()` atomic rename
- Single-flight lock on refiner re-key (threading.Lock with try-acquire)

### Phase 4 — Rewrite viewer (90 min)
- Load `fg_raw.npy` + `alpha_raw.npy` via `mmap_mode='r'`
- Named pipe (`\\.\pipe\ck_<uuid>`) for settings channel; mtime-poll as fallback
- On settings change: call `live_postproc.apply(...)`, repaint
- `CORRIDORKEY_PARENT_PID` watcher kills viewer if Fusion dies
- Check `.lock` file; refuse to start if sibling viewer owns it

### Phase 5 — Slider handler swap (45 min)
- `on_despill_changed` / `on_despeckle_changed` → write settings only, no NN call
- `on_refiner_changed` → 300ms debounce → full re-key in worker thread (cached processor)
- LineEdit: 400ms TextChanged debounce, NaN + comma-decimal guards

### Phase 6 — Validation (60 min)
- Run parity harness against 5 known frames
- Manual smoke: scrub + drag on EXR timeline and Rec.709 timeline
- Run ALIGNMENT.md smoke test (from PLAN_LIVE_PREVIEW §8)
- AE + Premiere regression: confirm still working byte-identical to pre-rewrite

**Total: ~6.25 hours focused work**

---

## 6. Open Questions for AiConsensus

1. Is **named pipe** the right primary IPC for slider updates on Windows + Fusion in-process Python, or is **localhost TCP** simpler and equally fast?
2. Should Phase 2 `live_postproc.py` be **one file with colorspace branch**, or **two sibling files** (`live_postproc_8bit.py` + `live_postproc_float.py`) to avoid branching in the hot path?
3. For 4K EXR frames, is **mmap-backed npy** still the right call, or should we **tile the cached tensors** (1080p chunks) so RAM never holds full 4K × float32 × 2?
4. The **refiner tier-2** (re-run hook layers with cached upstream features) — is this worth the implementation complexity, or should we accept "refiner drags feel like frame-changes" as a documented limitation?
5. **Copy-by-convention vs shared module** for `live_postproc.py` between AE and Resolve — which does AiConsensus recommend for a 2-host plugin suite on different Python versions?
6. Should **Phase 0 environment probe** block, or can we start Phase 1 parity harness in parallel?

---

## 7. What Doesn't Change

- AE plugin files: untouched this session, untouched by rewrite
- Premiere CEP panel: untouched
- Engine repo (`D:\New AI Projects\CorridorKey`): only venv `pip install safetensors`, no code edits
- `color_utils.py`: read-only import

---

## 8. Appendix — File Reference

**Engine (shared):**
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\inference_engine.py`
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\core\color_utils.py`
- `D:\New AI Projects\CorridorKey\CorridorKeyModule\backend.py`

**AE plugin (do not touch):**
- `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\ae_processor.py`
- `D:\New AI Projects\CorridorKey-Plugin\ae_plugin\cep_panel\preview_viewer.py`

**Resolve plugin (rewrite target):**
- `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` (installed, ~900 lines)
- `D:\New AI Projects\CorridorKey\resolve_plugin\core\corridorkey_processor.py` (wrapper)
- `D:\New AI Projects\CorridorKey-Plugin\resolve_plugin\` (dev copy)

**Plans:**
- `D:\New AI Projects\CorridorKey-Plugin\PLAN_RESOLVE_LIVE_VIEWER.md` (original, pre-corrections)
- `D:\New AI Projects\CorridorKey-Plugin\PLAN_LIVE_PREVIEW.md` (AE plan — reference)
- `D:\New AI Projects\CorridorKey-Plugin\ALIGNMENT.md` (mandatory smoke test)
- `D:\New AI Projects\CorridorKey-Plugin\SESSION_2026-04-17_HANDOFF.md` (today's state)
