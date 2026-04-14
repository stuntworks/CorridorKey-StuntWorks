# CorridorKey Plugin — Full Code Review
**Date:** 2026-04-14
**Reviewer:** Claude Code (4 parallel agents: security, code quality, install UX, performance)
**Scope:** `D:\New AI Projects\CorridorKey-Plugin` vs. GitHub origin
**Last commit:** `f646d5b Fix UI layout using proper Fusion UIManager pattern + add Ko-fi link`

---

## 0. SYNC STATUS — FIX FIRST

- **Repo name mismatch:** Memory + request reference `github.com/stuntworks/CorridorKey-StuntWorks`. Local `origin` is `github.com/stuntworks/CorridorKey-Plugin.git`. Decide which is canonical.
- **9 uncommitted modified files + 1 untracked** (`docs/screenshot_monitor1.png`). Local and GitHub are out of sync. Violates standing rule.
  - Modified: `ae_processor.py`, `cep_panel/index.html`, `host.jsx`, `CorridorKey_Pro.py`, `core/alpha_hint_generator.py`, `core/corridorkey_processor.py`, `core/resolve_bridge.py`, `install.py`, `ui/uimanager_panel.py`

---

## 1. CRITICAL — Ship blockers

| # | File:Line | Issue |
|---|-----------|-------|
| C1 | `host.jsx:94,187,304,408` | **Shell injection** — paths + numeric args concatenated into `system.callSystem()` unquoted. A clip path with `"` or `;` executes arbitrary shell code. Switch to `cep.process.createProcess` with argv array. |
| C2 | `host.jsx:8-9` | **`eval()` used as JSON.parse** on settings payload from CEP panel. Any DOM value with `);` injects ExtendScript. Use real JSON parser. |
| C3 | `install.py:239-259` | **`PlayerDebugMode=1` shipped to all users**. Opens Chromium debug port 7777 on every Adobe session for life. Remove from prod installer; dev-only. |
| C4 | `manifest.xml:27` | **`--mixed-context` flag** collapses Node/browser security boundary. Combined with C3, any localhost process can RCE inside AE/Premiere. |
| C5 | `CorridorKey_Pro.py:26-32, 252, 262, 400, 439-440, 619` | **Hardcoded `D:\New AI Projects\CorridorKey\...` paths** in shipping code. Error dumps go to `D:\ck_error.txt`. Any user without D:\ gets ImportError on first launch. `install.py` writes a `corridorkey_path.txt` config but `CorridorKey_Pro.py` never reads it — config system is half-wired. |
| C6 | `ae_processor.py:87` | **Model reloaded on EVERY frame** in single-frame mode. 300-frame shot = 300 cold Python starts + 300 model loads from disk. Single worst perf issue. |
| C7 | `ae_processor.py:182` | **Random seek per frame** — `cap.set(CAP_PROP_POS_FRAMES)` inside loop on H.264/ProRes long-GOP is 10-100x slower than sequential read. Frames are already in order; seek once, `read()` the rest. |
| C8 | Repo-wide | **No log file anywhere**. `host.jsx` spawns Python with stderr discarded. User-visible failures report "Error: " with zero forensic trail. Ship blocker for non-developer users. |
| C9 | Repo | **No `requirements.txt`, no Python version pin, no model weights, no download link for SAM2 `sam2.1_hiera_small.pt`**. Fresh user cannot install. README step 3 also references wrong installer path (`cd resolve_plugin && python install.py` — actual installer is at root). |

---

## 2. HIGH

| # | File:Line | Issue |
|---|-----------|-------|
| H1 | `host.jsx:364` | **Premiere batch will freeze Premiere** like AE batch did (commit `f31daa8` disabled that). Untested. Needs async worker + progress pipe before shipping. |
| H2 | `CorridorKey_Pro.py:64,71,579,736,741` + `golobulus/CorridorKey.py:138` | **6x `except: pass`** — silently corrupts settings, swallows KeyboardInterrupt. |
| H3 | `CorridorKey_Pro.py:301,386,524,673` + `CorridorKey_Full.py:101,151` | **`cv2.VideoCapture` leak on exception** — Windows locks media file until Resolve exits. `ae_processor.py:153` has correct pattern; port it everywhere. |
| H4 | `CorridorKey_Pro.py:265` | **SAM2 reloaded on every click** — 300MB model, 2-4GB VRAM spike. Needs cache dict like main keyer has. DANGER ZONE comment on L253 flagged this, never fixed. |
| H5 | `corridorkey_processor.py:72` | **No `torch.no_grad()`/`inference_mode()` wrapper** visible at plugin level. If engine doesn't wrap internally, gradient graph built every frame. |
| H6 | `ae_processor.py:17`, `corridorkey_processor.py:31` | **`sys.path` injection from `corridorkey_path.txt`** with zero validation. User-writable config → arbitrary Python import redirect. |
| H7 | `CorridorKey_Pro.py:440` | Hardcoded `.venv\Scripts\python.exe` for preview subprocess — breaks on any machine. File header flags this as FRAGILE but not fixed. |
| H8 | `host.jsx` FPS fallback | `if (isNaN(fps) \|\| fps <= 0) fps = 24` — a 29.97 project with a malformed rate silently keys at 24fps = frame offset disaster. Should error. |

---

## 3. MEDIUM

| # | Location | Issue |
|---|----------|-------|
| M1 | `__pycache__/` at repo root + `resolve_plugin/__pycache__/` committed | `.gitignore` lists them but they're tracked. `git rm -r --cached`. |
| M2 | `CorridorKey_Full.py` (205 lines) + whole `golobulus/` tree | **Zero imports anywhere**. Orphan/legacy code. Delete or mark `_archive/`. Confusion risk: which script actually ships? |
| M3 | `host.jsx` temp file names use PID (`ck_ae_in_{pid}.png`) | Predictable → symlink/TOCTOU hijack on shared Windows machines. Use `tempfile.mkstemp` equivalent. |
| M4 | `index.html:269` | Settings JSON-escape via `.replace(/\\/g,...)` — breaks on apostrophes, smart quotes, non-ASCII project paths. |
| M5 | `CorridorKey_Pro.py:349-352` | `create_checkerboard()` nested Python loops over 8.3M pixels at 4K. Replace with numpy slicing. UI-blocking. |
| M6 | `CorridorKey_Pro.py:165` log() | Reads-full-text → appends → writes-back per log call. O(n²) on 2000-frame batches. |
| M7 | `host.jsx:94-100` Python one-liner via `-c` | Fails silently on Windows long-path (>260 char) or non-ASCII clip paths. No `\\?\` prefix guard. |
| M8 | CEP `HostList` `[22.0,99.9]` | README says "AE 2020+" but manifest is 2022+. Users on 2020/2021 load empty panel. |
| M9 | `torch.load` chain via CorridorKey engine | No hash/sig on third-party weights. Pickle = RCE if engine tampered. |
| M10 | `ae_processor.py:215-216` batch PNG writes | Two `cv2.imwrite` per frame (RGBA + matte). Default PNG level. No async write queue. |

---

## 4. LOW

- `host.jsx:40`, `golobulus/CorridorKey.py:14` — Windows-only `.venv` path, no macOS fallback.
- `install.py:54,63` — `Path("C:/Program Files/Adobe")` ignores `%PROGRAMFILES%`.
- 129 bare `print()` calls — hidden subprocess → nowhere.
- `docs/screenshot_monitor1.png` untracked, 2.6 MB — bloats clone when committed.
- No icon at `./icons/icon.png` referenced by manifest.
- No codesign / SmartScreen handling.
- README claims "Batch process entire clips" as feature — AE batch disabled, Premiere batch untested.
- No tile/downscale path for 8K input to 2048×2048 model — will OOM or garbage out.

---

## 5. SUGGESTED FIX ORDER (minimum effort → max safety)

1. **Commit current 9 modified files to GitHub** (sync rule).
2. **Resolve repo-name mismatch** (Plugin vs StuntWorks) — pick one, update memory.
3. **Fix C5 (hardcoded paths)** — wire `corridorkey_path.txt` read in `CorridorKey_Pro.py` + replace `D:\ck_error.txt` with `%TEMP%\corridorkey.log`.
4. **Add global logger** (C8) writing to `%TEMP%\corridorkey.log`, capture subprocess stderr in `host.jsx`.
5. **Fix C1+C2 shell/eval injection** — `cep.process.createProcess` + real JSON parser.
6. **Remove C3 PlayerDebugMode from prod installer** (or gate behind `--dev` flag).
7. **Cache AE model load (C6)** + sequential read (C7) — huge perf win, both in `ae_processor.py`.
8. **Write `requirements.txt` + README install section** with Python version, model weights location.
9. **Purge `__pycache__/` and orphan `CorridorKey_Full.py` + `golobulus/`**.
10. **Gate Premiere batch behind "experimental" + add progress pipe** before wider test.

---

**End of review.**
