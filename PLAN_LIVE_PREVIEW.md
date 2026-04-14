# PLAN — Live Slider Preview + Pop-out Viewer

**Date:** 2026-04-14
**Status:** DRAFT — pending Berto approval before any code is written.
**Scope:** plugin layer only. Cannot retrain or modify the CorridorKey model.

---

## 1. Goal (one sentence)

When Berto drags a despill / refiner / despeckle slider, the keyed preview
updates in under 200 ms in a pop-out window big enough to judge edges — the
same pop-out window used today in Resolve, now wired for AE and Premiere.

---

## 2. User story

1. Berto parks on a frame and clicks **KEY CURRENT FRAME**.
2. The full pipeline runs once (~1-2 s): extract, neural net, post-proc.
3. A pop-out window appears — two large panels: **original** on the left,
   **keyed composite** on the right. Background toggle: black / white /
   checker / V1-below.
4. Berto drags a slider in the CEP panel.
5. The pop-out refreshes in ~100-200 ms. The neural net does **not** re-run
   — only the post-processing stage runs against the cached foreground +
   alpha from step 2.
6. When the key looks right, Berto clicks **COMMIT TO V2**. The plugin writes
   the final PNG and imports it on V2 using the existing (tested) timeline
   code path.
7. Parking on a new frame and clicking KEY CURRENT FRAME again re-runs step 2
   with the new source frame.
8. Closing the CEP panel closes the pop-out cleanly.

---

## 3. Why this is the right feature

- **Matches council consensus:** Architect + Skeptic + Critic all said
  "speed of decision beats speed of render" — live slider feedback is the
  purest implementation of that.
- **Zero timeline-code risk:** we do not touch `overwriteClip`,
  `importFiles`, frame-alignment math, or any of the 5 Premiere API quirks
  documented in ALIGNMENT.md. The only timeline code runs on COMMIT, which
  is identical to today's path.
- **Reuses proven code:** Resolve already has `preview_viewer.py`. We
  extend it to stay alive and accept updates; same window for AE/Premiere.
- **Testable:** Berto uses it on a real shot. If slider drag does not
  change the preview visibly, the build fails.

---

## 4. Architecture

### 4.1 The two-stage split

CorridorKey pipeline has two stages:

| Stage | What it does | Time | Depends on |
|-------|-------------|------|------------|
| 1. Neural net | raw frame + alpha hint → foreground + alpha matte | ~1-2 s (first call), ~300 ms warm | GPU, model loaded |
| 2. Post-proc | despill + refiner + despeckle on cached FG + alpha | ~50-100 ms | CPU, numpy/cv2 only |

Today the plugin runs both stages on every slider change. The new build caches
stage 1's output and re-runs only stage 2 on slider moves.

### 4.2 Persistent pop-out viewer

A single Python process stays alive between slider moves. It holds:

- the cached FG (float32 array) and alpha (float32 array)
- the current settings (despill / refiner / despeckle)
- a Qt (or Tk — pick one during Phase 2) window showing the composite

The process reads JSON lines from stdin. Each line is a settings update.
On every line it re-applies stage 2 and repaints the window.

### 4.3 Process tree

```
Adobe host (AE / Premiere)
  └── CEP panel (index.html + Node.js)
       ├── ae_processor.py cache ...     ← one-shot, writes FG+alpha PNGs
       └── preview_viewer.py --persistent ← stays alive, listens on stdin
             ├── loads FG+alpha from session dir
             ├── reads { despill, refiner, ... } lines
             └── renders to Qt/Tk window
```

Resolve later points its existing viewer at the same persistent mode.

### 4.4 Communication protocol (stdin → viewer)

Panel writes one JSON object per line to viewer's stdin:

```json
{ "cmd": "update", "despill": 0.5, "refiner": 1.0, "despeckle": true, "despeckleSize": 400, "background": "checker" }
{ "cmd": "reload", "sessionDir": "C:\\Users\\ragsn\\AppData\\Local\\Temp\\ck_session_a1b2c3" }
{ "cmd": "quit" }
```

Viewer replies nothing (fire-and-forget). Panel does not block on viewer.

### 4.5 Debounce

Slider `oninput` fires on every pixel of drag. Debounce to 50 ms — if
another change comes in within 50 ms of the last send, drop the earlier
one. 50 ms is the sweet spot: snappy enough to feel live, slow enough that
the viewer keeps up.

### 4.6 Session directory

Each KEY CURRENT FRAME creates a fresh session dir under
`%TEMP%\ck_session_<random-hex>\` containing:

- `fg.png` — raw foreground from stage 1 (BGR or RGBA)
- `alpha.png` — raw alpha matte from stage 1 (grayscale)
- `meta.json` — source clip, frame index, timestamp, session id

On a new KEY, panel sends `{ "cmd": "reload", "sessionDir": "..." }` to the
viewer. Old session dir gets cleaned up by a background sweep or on
viewer exit.

### 4.7 Commit flow

COMMIT TO V2 does NOT touch the viewer. It sends current settings to
ae_processor.py's existing `single` subcommand, which runs full stage 1 +
stage 2 with those settings and writes a final PNG. JSX then imports on V2
exactly as today. Frame-alignment code path unchanged. **This is the rule
that keeps the Critic's warning satisfied.**

---

## 5. File changes

| File | Change | Phase |
|------|--------|-------|
| `CorridorKey/postproc.py` (new, in engine repo — may instead go in plugin repo at `ae_plugin/postproc.py` if engine is off-limits) | Factor despill / refiner / despeckle math out of `corridorkey_processor.py` so both the full keyer and the live viewer call the same functions. No behavior change. | 1 |
| `CorridorKey/resolve_plugin/preview_viewer.py` or plugin-side replacement | Add `--persistent` + `--session <dir>` flags. Load FG + alpha from session dir. Listen on stdin for JSON. Call postproc. Render window. Exit on EOF or `{"cmd":"quit"}`. | 2 |
| `ae_plugin/ae_processor.py` | Add `cache` subcommand: runs stage 1 only, writes `fg.png` + `alpha.png` + `meta.json` to `--out-dir`. | 3 |
| `ae_plugin/cep_panel/index.html` | — spawn `preview_viewer.py --persistent` on first key<br>— debounce slider `oninput` (50 ms)<br>— write JSON lines to viewer stdin<br>— add **COMMIT TO V2** button separate from KEY CURRENT FRAME<br>— clean shutdown on panel close | 4 |
| `resolve_plugin/CorridorKey_Pro.py` | Wire Resolve's sliders to the same persistent viewer behind an opt-in toggle. Existing fire-and-forget viewer stays the default until we confirm no regressions. | 5 |
| `ALIGNMENT.md` | Add a new "NO TIMELINE CODE TOUCHED" check to the pre-commit smoke test so nobody inadvertently regresses frame alignment while fiddling with the preview pipeline. | 6 |

---

## 6. Phases (build order)

Each phase is a separate commit. Each phase is independently reversible.
Each phase ends with its own smoke test.

### Phase 1 — Factor post-proc into a shared module (~2 h)

- Extract despill / refiner / despeckle into `postproc.py`.
- Both `corridorkey_processor.py` and future `preview_viewer.py` call the
  same functions.
- Unit-test: same image in → same image out as before the extraction.
- Commit.

### Phase 2 — Persistent viewer (~3 h)

- Add `--persistent` + `--session` flags to `preview_viewer.py`.
- Load FG + alpha from session dir on startup.
- Listen on stdin for JSON settings.
- Render via Qt or Tk — whichever is already in the venv. No new deps.
- Standalone test: `echo '{"cmd":"update","despill":0.3,...}' | python preview_viewer.py --persistent --session <dir>` updates the window.
- Commit.

### Phase 3 — `cache` subcommand on `ae_processor.py` (~1 h)

- Stage 1 only — extract frame, run NN, write fg.png + alpha.png + meta.json.
- Test at CLI level.
- Commit.

### Phase 4 — CEP panel wires it up (~3 h)

- KEY CURRENT FRAME triggers:
  - Run ae_processor cache → session dir.
  - Launch (or reload) the persistent viewer.
  - Wire sliders to debounced stdin writes.
- Add COMMIT TO V2 button — uses existing `single` path.
- Test in AE: drag slider, watch preview refresh.
- Test in Premiere: drag slider, watch preview refresh.
- Run full ALIGNMENT.md smoke test (A/B/C/D) — COMMIT path must still be
  frame-perfect. If any alignment test fails, revert and debug.
- Commit.

### Phase 5 — Resolve integration (~2 h)

- Wire Resolve's sliders to the same persistent viewer.
- Keep a toggle to fall back to fire-and-forget until stable.
- Test in Resolve.
- Commit.

### Phase 6 — Docs + smoke test update (~1 h)

- Update `ALIGNMENT.md` with the new "no timeline code touched" rule.
- Update `README.md` "For Developers" with the live-preview feature.
- Update `CODE_REVIEW` entries as needed.
- Commit.

**Total estimated effort:** ~12 hours of plugin-layer work, spread across
6 commits. Fits a dedicated weekend.

---

## 7. Test plan

For each NLE (Resolve, AE, Premiere), before COMMIT:

| ID | Test | Pass condition |
|----|------|----------------|
| T1 | Smoke | KEY CURRENT FRAME → viewer appears, shows original left + keyed right |
| T2 | Live slider (despill) | Drag despill from 0 → 1 → 0 over 2 seconds, preview tracks the drag with <300 ms perceived lag |
| T3 | Live slider (refiner) | Same for refiner |
| T4 | Live slider (despeckle) | Toggle checkbox, preview updates. Drag size slider, preview tracks |
| T5 | Background toggle | Switch black ↔ white ↔ checker ↔ V1-below, preview updates |
| T6 | Frame switch | Park new frame, KEY CURRENT FRAME, viewer shows the new frame (not old) |
| T7 | Commit | Click COMMIT TO V2, final PNG matches what the viewer was showing |
| T8 | ALIGNMENT | Re-run ALIGNMENT.md tests A/B/C/D in Premiere — all pass |
| T9 | Lifecycle | Close panel → viewer exits; kill viewer → panel handles gracefully |
| T10 | Perf | Drag slider for 10 seconds, no memory leak, no crash, no zombie process |

All 10 must pass before commit to main.

---

## 8. Risks and mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| Post-proc math diverges between full keyer and live viewer | medium | Phase 1 factor-out: one shared `postproc.py`, called by both paths |
| Viewer race condition (new settings before last render finishes) | medium | Debounce 50 ms in panel + drop-stale in viewer (keep only latest pending) |
| Orphan viewer process if panel crashes | low | Viewer monitors parent PID, exits on parent death |
| Qt / Tk dependency conflict with engine venv | low | Use whichever is already in the engine venv — no new deps |
| Slider drag faster than viewer can render | medium | Debounce + drop-stale (above). Worst case: viewer lags 1-2 slider positions behind |
| Regresses Premiere frame alignment | low-medium | COMMIT path is untouched; ALIGNMENT.md smoke test in Phase 4 |
| Resolve regression | medium | Keep fire-and-forget as default in Resolve until Phase 5 stable (see Phase 5) |
| Session dir bloat in %TEMP% | low | Sweep on viewer exit + weekly cleanup or size cap |

---

## 9. Explicitly out of scope

The following are NOT in this plan and must not be added during the build:

- SAM2 click-to-mask UI surfacing
- ONNX export wiring
- New slider types or new post-proc algorithms
- Render queue / batch viewer
- Cross-machine or network rendering
- Mac platform testing (nice-to-have, not a blocker)
- CEP panel resize / dockable improvements beyond adding the COMMIT button
- Any change to install.py or PlayerDebugMode behavior

If any of these creep in during the build, stop and discuss with Berto.

---

## 10. Rollback plan

- Each phase is a separate commit.
- If phase N breaks anything, `git revert` to phase N-1 commit.
- No database, no file-format changes, no persistent config changes — clean revert is always safe.
- PlayerDebugMode behavior unchanged.
- Existing install.py flow unchanged.

---

## 11. Scoping decisions (ALL ANSWERED — ready to build)

1. **Cache location** — fully user-controllable with a smart default. See §13 below for the complete spec. Rule in one line: **default to the project file's folder; fall back to the user's Documents folder; never to `%TEMP%`; never force a specific drive letter.** A laptop-only user whose only drive is C: gets C: automatically — we don't block that, we just don't *assume* D:/E:/K: exist.
2. **Button layout** — keep three existing buttons, reassign behaviors:
   - `PREVIEW FRAME` (purple) → launches the pop-out viewer with live slider feedback. **Does NOT place anything on the timeline.**
   - `KEY CURRENT FRAME` (green) → commits with current slider state, runs the full pipeline, places PNG on V2. Unchanged commit path → frame alignment untouched.
   - `PROCESS IN/OUT RANGE` (blue) → unchanged batch flow.
3. **Viewer library** — **Qt (PySide6)**. Resolve's existing `preview_viewer.py` is already PySide6. Engine venv already has it. No new dependencies.
4. **Debounce feel** — **50 ms + drop-stale**. If a new slider value arrives while the viewer is still rendering the previous one, the older one is discarded and the newest one renders. Feels real-time, no queue backup.
5. **Backgrounds in v1** — **ALL FOUR**, including V1-below. Resolution via `/autoresearch:reason` (3-0 unanimous judge vote, convergence round 1). Rationale: the same compositor serves any background buffer, so the integration surface is identical — the marginal cost of adding V1-below is only the JSX frame export (~30 lines/host). V1-below is the single most useful background for judging a stunt key in its actual scene, which is the whole point. Safety: cap V1 export at viewer resolution (960x540), cache by timecode, fall back to last cached frame if export exceeds 100 ms. Brightness-adjustable checker is the zero-latency default when only alpha matters.
6. **Build order** — **AE + Premiere first, Resolve last.** Resolution via `/autoresearch:reason` (3-0 unanimous, same round). Rationale: AE/Premiere have only a tiny thumbnail today — that IS the pain point. A greenfield pop-out cannot regress anything. Building there first forces the viewer to accept externally-supplied frames from day one, making the Resolve retrofit a thin adapter instead of a rewrite of production-load code. Bonus architectural leverage: the file-watcher + temp-PNG bridge that carries V1-below (#5) is the SAME bridge that carries slider updates — one mechanism, double payoff.

## 12. Locked build architecture (post-reason-loop)

- **One compositor** in the Qt viewer: takes `keyed_rgba` and blits over any background buffer (solid color / generated checker / decoded V1 frame).
- **One file-watcher bridge** between CEP panel and viewer: JSX (or panel JS) writes a JSON params file and optionally a V1-below reference PNG to a session dir; viewer polls the dir for changes. **No live IPC in v1.** Simple, weekend-sized, unified.
- **Viewer standalone process** launched by the panel on PREVIEW FRAME. Closes cleanly on panel close.
- **Commit path unchanged** — KEY CURRENT FRAME and PROCESS IN/OUT RANGE run the existing ae_processor `single` / `batch` subcommands with no viewer involvement → frame alignment untouched.
- **Resolve retrofit** (Phase 5) is a thin adapter: Resolve's existing `preview_viewer.py` gets the same file-watcher input, behind a settings-flag fallback to the old fire-and-forget viewer if the persistent mode misbehaves.

---

**All questions answered. Phase 1 (factor post-proc into shared module) is cleared to start when Berto gives the go.**

---

## 13. Cache & Output Location — Full Specification

### 13.1 Design goals

1. **Work on any machine**, including a laptop whose only drive is C:.
2. **Don't dump files where the user won't find them** — no `%TEMP%` by default (Windows cleans it, and the user wants to keep raw FG/alpha for a minute in case they need to re-key without re-running the NN).
3. **Don't dump files into system folders** — no `C:\`, `C:\Program Files\`, `C:\Windows\`, `C:\ProgramData\`.
4. **Default should match how the user already works** — Premiere/AE users keep project-adjacent files; Resolve users pick a scratch folder once and leave it.
5. **Fully user-overridable** in the panel. One button, one folder picker, stored per-NLE.
6. **Separate throwaway sessions from committed renders** so the user can back up / clean up with confidence.

### 13.2 One setting, two folders under it

The panel exposes a single **Working Folder** per NLE. Inside it, the plugin creates two subfolders automatically:

```
<Working Folder>/
  CorridorKey/
    ck_batch_<hex>/       ← committed batch renders (keep these)
      output_00000.png
      output_00001.png
      ...
      mattes/
  .corridorkey_sessions/
    ck_session_<hex>/     ← throwaway PREVIEW caches (auto-swept)
      fg.png
      alpha.png
      v1_underlay.png
      meta.json
```

- `CorridorKey/` holds user output — the batch sequences you keep.
- `.corridorkey_sessions/` holds PREVIEW session caches — throwaway, auto-swept.

Dot-prefixing the sessions folder keeps it out of the user's casual view on macOS/Linux and de-clutters the Windows folder listing.

### 13.3 Default cascade (per NLE)

On panel startup, if the user has no saved Working Folder for this NLE, resolve in this order:

| Priority | Source | Example |
|---|---|---|
| 1 | User-saved setting for this NLE (localStorage) | `K:\C_key test\C_key premiere test v1` |
| 2 | **Folder containing the current project file** | `C:\Users\ragsn\Documents\MyProject\` (next to `MyProject.prproj` or `MyProject.aep`) |
| 3 | `<user Documents>/CorridorKey/` | `C:\Users\ragsn\Documents\CorridorKey\` |
| 4 | `<user Desktop>/CorridorKey/` (last resort, with warning) | `C:\Users\ragsn\Desktop\CorridorKey\` |

**Never** `%TEMP%`. **Never** any of these system paths: `C:\`, `C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`, `C:\ProgramData`, `/System`, `/Library`.

If the resolved folder is not writable (permission, read-only media, missing drive), fall through to the next priority. If all four fail, show an inline error in the panel: *"No writable location found — please click **Change Working Folder** and pick a folder."*

### 13.4 Resolve specifics — use the project library / render settings

Resolve projects live in a database (no `.drp` file path), **but** they expose:
- **Project name** via `project.GetName()` — always present.
- **Render target directory** via `project.GetSetting("TargetDir")` — set by the user on the Deliver page, the place they already tell Resolve to render out to.
- **Scratch disk / Media storage** via user preferences — where Resolve itself caches.

Cascade for Resolve:

| Priority | Source | Resolution |
|---|---|---|
| 1 | Saved plugin setting for this machine | read from `corridorkey_path.txt` / localStorage equivalent |
| 2 | **Resolve render target directory** for the current project (`project.GetSetting("TargetDir")`) → use its **parent folder** + a subfolder named after the project | e.g. if user renders to `K:\Deliveries\MyStunt\`, Working Folder becomes `K:\Deliveries\MyStunt\` |
| 3 | `<user Documents>/CorridorKey/<project name>/` | e.g. `C:\Users\ragsn\Documents\CorridorKey\MyStunt\` |
| 4 | `<user Desktop>/CorridorKey/<project name>/` (last resort, with warning) | `C:\Users\ragsn\Desktop\CorridorKey\MyStunt\` |

Why this works: the Deliver page target is where the user *already* tells Resolve to write output. Matching it means CorridorKey output lands next to the user's final renders — same folder the user opens to grab stuff. No "where did my keys go?" moment.

If `TargetDir` is empty (user hasn't configured Deliver yet), the cascade skips to priority 3 automatically. `<project name>` is sanitized (strip `/ \ : * ? " < > |`) before being used as a folder segment.

First-run panel in Resolve shows the resolved Working Folder with a green "Auto-detected" badge if priority 2 hit, or a yellow "Using fallback — set a render target on Deliver to organize automatically" hint if it fell to priority 3.

### 13.5 UI

Panel adds one row above the existing output path:

```
Working Folder:  [K:\C_key test\C_key premiere test v1   ]  [Change...]
                 Next-to-project (auto)   ▼
```

- Dropdown: **Auto (next to project)** | **Fixed: last-chosen folder** | **Change...**
- `Change...` opens a native folder picker.
- Persisted per-NLE in `localStorage` under keys `ck_working_folder_ae`, `ck_working_folder_ppro`, `ck_working_folder_resolve`.

### 13.6 Sweep policy (session caches)

- On **panel open:** scan `.corridorkey_sessions/` in the current Working Folder, delete any `ck_session_*` directory older than **24 hours**. Log one line: `Swept N stale sessions`.
- On **viewer exit:** the session's own dir is kept for 1 minute, then swept — so the user can inspect it if they just exited accidentally.
- On **uninstall:** do not touch the user's Working Folder. Document in the uninstall output: *"Your keyed output at `<Working Folder>/CorridorKey/` was left untouched. Delete manually if not needed."*

### 13.7 Laptop-only user example

User has a single C: drive, AE project at `C:\Users\jane\Documents\MyStunt\MyStunt.aep`:

- First run: panel resolves Working Folder to `C:\Users\jane\Documents\MyStunt\` (priority 2).
- PREVIEW session cache lands in `C:\Users\jane\Documents\MyStunt\.corridorkey_sessions\ck_session_a1b2c3\`.
- Committed batch lands in `C:\Users\jane\Documents\MyStunt\CorridorKey\ck_batch_d4e5f6\`.
- User sees their own project folder, plus two plugin-owned subfolders. No surprises on C:\root, no surprises in `%TEMP%`.

### 13.8 Multi-drive user example (Berto)

User has K: drive, Premiere project at `K:\C_key test\C_key premiere test v1\MyStunt.prproj`:

- First run: Working Folder = `K:\C_key test\C_key premiere test v1\` (priority 2).
- Session cache: `K:\...\C_key premiere test v1\.corridorkey_sessions\ck_session_<hex>\`.
- Committed batch: `K:\...\C_key premiere test v1\CorridorKey\ck_batch_<hex>\`.
- C:\ completely untouched.

### 13.9 What this replaces in the current code

- The existing `savedOutput` localStorage key is kept for backward compatibility and remapped to `ck_working_folder_<nle>` on first run.
- The current batch path `<savedOutput>/CorridorKey/ck_batch_<hex>/` is unchanged — same layout, same behavior.
- Session cache is new (this feature adds it).
- Nothing else migrates. No data is moved automatically.
