# CorridorKey — Session Handoff 2026-04-17

## Current State

- **Branch:** `live-preview` (CorridorKey-Plugin fork, stuntworks)
- **Engine repo:** pulled to `cf587ec` (upstream nikopueringer/main, +9 commits including #228 GPU fallback fix)
- **Resolve plugin:** patched + working — sliders apply, despill/refiner/despeckle all reach the engine
- **AE plugin:** DO NOT TOUCH — works; any rewrite is Resolve-only

## Files Currently Patched

1. `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` (installed Resolve plugin, ~900 lines)
2. `D:\New AI Projects\CorridorKey\resolve_plugin\core\corridorkey_processor.py` (wrapper around the engine)

## Fixes Applied Today (3 ECC+Consensus blockers)

| Fix | File:Line | Status |
|---|---|---|
| `reprocess_with_cached` reuses `cached_processor["proc"]` instead of reloading model per slider drag | CorridorKey.py:589 | ✅ |
| `on_process_range` save block now nested inside frame loop (was dedented, only last frame saved) | CorridorKey.py:815 | ✅ |
| `on_close` calls `cached_processor.cleanup()` + CUDA `empty_cache` in daemon thread | CorridorKey.py:875 | ✅ |
| Wrapper replaces raw `fg` with engine's fully post-processed output (despill + clean matte + sRGB) | corridorkey_processor.py:88-140 | ✅ |
| `post_process_on_gpu=False` forced (GPU despill silently no-ops in Fusion's Python) | corridorkey_processor.py:85 | ✅ |
| Live Preview re-enabled, slider/text changes trigger re-key with "Processing..." status | CorridorKey.py `_live_rekey` | ✅ |
| `__file__` NameError try/except fallback for Fusion env | CorridorKey.py:31 | ✅ |
| Sliders/SpinBox replaced with LineEdit (Fusion widget `.Value` was unreliable) | CorridorKey.py ~170-183 | ✅ |

## Known Limitation (Not a Bug)

**Visual despill is subtle in Resolve vs dramatic in AE.** Root cause: AE uses a 633-line `preview_viewer.py` (repo root, main branch) that caches raw NN output once and re-applies post-processing in the viewer per-slider. Resolve re-runs the full NN per Preview click. Same math, different latency + visual aggression. **Fixed properly by the rewrite plan below, not today's patches.**

## Next Work: The Rewrite

**Plan:** `D:\New AI Projects\CorridorKey-Plugin\PLAN_RESOLVE_LIVE_VIEWER.md`

**ECC + Consensus corrections to apply to the plan before starting:**
1. **Refiner is NOT post-proc separable** — it's a `register_forward_hook` inside the NN (`inference_engine.py:457`). Treat refiner as full re-key trigger, 300ms debounce. Only despill/despeckle/composite run in viewer.
2. Session dir = **UUID**, not `frame_num` (prevents race on re-key same frame)
3. **Pre-bake LUT for sRGB gamma** — biggest perf win (`np.power` → `cv2.LUT`, ~50ms → ~3ms)
4. **mtime poll at 100ms**, not 300ms (perceived-live threshold)
5. **`.npy + mmap_mode='r'`**, NOT `np.savez_compressed`
6. **`clean_matte_opencv` on release only** (45-85ms on 1080p — blows drag budget)
7. **Atomic rename** (`tmp` → final) for cross-process PNG/npy writes
8. Import `color_utils.linear_to_srgb` directly — don't reimplement
9. `TextChanged` debounce ~400ms before Phase 4
10. **Realistic time: 5-6 hours, not 3**

## Still-Open TODOs (from earlier handoffs)

- AE viewer inline QSS overrides break Honeycomb theme (AE-only, don't touch now)
- Threaded refiner re-key (part of rewrite Phase 4)
- Visual polish iteration

## GitHub State

- **Local live-preview branch** has uncommitted patches beyond commit `80f26e5`. Diff: all files listed in "Files Currently Patched" above.
- Engine repo pulled to `cf587ec` but local backend.py CHECKPOINT_DIR fix (one-line comment) is uncommitted.

## AiConsensus Rating — 2026-04-17 session

**3/10** — Useful validation layer but missed the biggest architectural finding (refiner-not-separable), 70% redundant with the local ECC review, Kimi fabricated a fake review. Don't run before ECC; run after if uncertain.
