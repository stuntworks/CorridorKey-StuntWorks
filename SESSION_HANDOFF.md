# CorridorKey — Session Handoff 2026-04-26 (Session 8 morning, ~11:35 AM)

**This handoff replaces the earlier 9:30 AM version.** All references to "still uncommitted CEP folder edits" are no longer accurate — that work is now committed and pushed to origin via the new junction layout.

---

## CRITICAL ARCHITECTURE CHANGES TODAY (READ THIS FIRST)

### 1. CEP folder is now a Windows JUNCTION to the engine repo

**The CEP install path:**
`C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel`

**...is a junction pointing to:**
`D:\New AI Projects\CorridorKey\ae_plugin\cep_panel`

**This means:**
- AE loads files **directly from the engine repo** through the junction
- Editing files at EITHER path edits the SAME files (because they're physically the same)
- `git status` in the engine repo immediately sees AE-related edits — no sync step needed
- The dual-edit divergence problem (193/97 incident) is now structurally impossible

**Old CEP folder preserved as fallback:**
`C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel.OLD-pre-junction-2026-04-26`

**To recover if junction breaks something:**
```bash
rm "C:/Users/ragsn/AppData/Roaming/Adobe/CEP/extensions/com.corridorkey.panel"
mv "C:/Users/ragsn/AppData/Roaming/Adobe/CEP/extensions/com.corridorkey.panel.OLD-pre-junction-2026-04-26" \
   "C:/Users/ragsn/AppData/Roaming/Adobe/CEP/extensions/com.corridorkey.panel"
```

**To recreate the junction (e.g., on a fresh machine):**
```cmd
rmdir "C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel"
mklink /J "C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel" "D:\New AI Projects\CorridorKey\ae_plugin\cep_panel"
```

### 2. origin/main has BRANCH PROTECTION enabled

GitHub now blocks:
- `git push --force` to origin/main (server rejects)
- `git push --delete origin main` (server rejects)
- `enforce_admins=true` so even admin can't override locally

To temporarily disable for legitimate destructive ops:
GitHub repo Settings → Branches → Edit protection rule

### 3. Engine code is now on origin

The 193/97 divergence revealed origin/main was missing the entire engine code (`CorridorKeyModule/backend.py`, `inference_engine.py`, `core/*`, `backend/*`). It only existed locally and never made it up. Restored from `local-work-sam2-integration` branch and pushed.

---

## STATUS — END OF MORNING WORK (11:35 AM)

| Feature | Status |
|---------|--------|
| AE viewer SAM2 formula | TESTED AND COMMITTED |
| AE viewer CLEAR button | TESTED AND COMMITTED |
| AE viewer CHOKE sub-pixel | TESTED AND COMMITTED |
| AE viewer MARGIN / SOFTEN | TESTED (works on NN-only OR NN×SAM2) |
| AE viewer DESPILL / DESPECKLE | TESTED |
| AE viewer SAM2 soft-logits | TESTED AND COMMITTED |
| AE Refiner UI explainer | COMMITTED |
| AE scrub mode end-to-end | TESTED AND COMMITTED |
| AE SAM2 video predictor in scrub | TESTED AND COMMITTED |
| AE SAM2 anchor frame chain | TESTED AND COMMITTED |
| AE runPython env (CORRIDORKEY_ROOT) | TESTED AND COMMITTED |
| **CEP junction layout** | **APPLIED + TESTED — junction works, AE loads through it** |
| **Branch protection on origin/main** | **APPLIED — server-side enforced** |
| **Engine code restored on origin** | **PUSHED at commit 48d1e05** |
| DaVinci CHOKE sub-pixel | APPLIED locally — NOT YET TESTED IN DAVINCI APP |

---

## WHAT WAS DONE THIS MORNING

### 1. Git surgery (Option C — branch + preserve)
The local engine repo had **193 commits ahead of origin AND 97 commits behind**. The 97-behind commits included real work that overlapped with last night's session (SAM2 scrub prep, CORRIDORKEY_ROOT fix, etc.). Cleanest path:
- Created safety bundle: `~/Desktop/ck-pre-merge-2026-04-26.bundle` (59 MB, full repo snapshot)
- Created tag: `pre-sam2-merge-2026-04-26`
- Created branch: `local-work-sam2-integration` (preserves all 193 commits + WIP files)
- Hard-reset main to origin/main at `d6ba447`
- All work preserved on the branch; no data loss

### 2. Branch protection applied via gh API
```
PUT /repos/stuntworks/CorridorKey-StuntWorks/branches/main/protection
{ enforce_admins: true, allow_force_pushes: false, allow_deletions: false }
```

### 3. CEP junction migration
- Backed up CEP folder to `~/Desktop/CEP_BACKUP_pre-junction-2026-04-26/` (745 KB, 17 files)
- Copied current CEP files (last night's working versions) into `ae_plugin/cep_panel/`
- Skipped obsolete files: 6 `.bak` files, old `preview_viewer.py` non-v2 (Apr 16), root `host.jsx` (Apr 14, superseded by `jsx/host.jsx`)
- Renamed CEP folder to `com.corridorkey.panel.OLD-pre-junction-2026-04-26`
- Created junction via PowerShell: `New-Item -ItemType Junction`
- Verified junction: `lrwxrwxrwx ... com.corridorkey.panel -> /d/New AI Projects/CorridorKey/ae_plugin/cep_panel`

### 4. Engine restoration
- AE PREVIEW FRAME failed after the reset with `ModuleNotFoundError: No module named 'CorridorKeyModule.backend'`
- Diagnosis via `__pycache__` files revealed which Python files USED TO EXIST
- Origin/main has ZERO files under `CorridorKeyModule/` and `backend/`
- Restored from `local-work-sam2-integration`:
  - `CorridorKeyModule/__init__.py`, `backend.py`, `inference_engine.py`, `core/__init__.py`, `core/color_utils.py`, `core/model_transformer.py`, `IgnoredCheckpoints/.gitkeep`, `README.md`, `checkpoints/.gitkeep`
  - `backend/__init__.py`, `clip_state.py`, `errors.py`, `ffmpeg_tools.py`, `frame_io.py`, `job_queue.py`, `natural_sort.py`, `project.py`, `service.py`, `validators.py`
- Cleared all `__pycache__` to avoid stale bytecode
- Verified: `from CorridorKeyModule.backend import create_engine` works
- Berto confirmed: PREVIEW FRAME, SAM2 mask, scrub mode all work end-to-end

### 5. Two clean commits pushed to origin
- `48d1e05` — `restore: engine code (CorridorKeyModule + backend) missing on origin`
- `17bfafc` — `feat(ae): migrate CEP panel files to engine repo as source-of-truth (junction layout)`
- main = origin/main = 17bfafc (verified)

---

## OPEN TODO (in recommended priority order)

| # | Task | Time | Why this order |
|---|---|---|---|
| 1 | **Document junction + branch protection in CLAUDE.md** (project + maybe global) | 10 min | Future sessions MUST understand the new architecture |
| 2 | **MARGIN + SOFTEN sub-pixel precision** (0.1 px steps like CHOKE) | 30 min | Berto's new ask — small focused fix |
| 3 | **Verify DaVinci CHOKE in app** (we applied yesterday but never tested) | 5 min | Quick smoke test |
| 4 | **DaVinci button colors** (YouTube/Ko-fi red collide with destructive Clear/Cancel/KillViewer) | 30 min | Locks the palette before Premiere copies it |
| 5 | **Premiere Pro scrub port** (~10 LOC + tests) | 3 hrs | Premiere already has 90% wired; only scrub blocked |
| 6 | **Visual + functional parity sweep** across DaVinci, AE, Premiere | 1 hr | After Premiere ships |
| 7 | **Multi-agent code review** (`bug-hunter`, `security-auditor`, `contracts-reviewer`, `test-coverage-reviewer`) | 1 hr | Before recording video |
| 8 | **OBS walkthrough + tutorial cut** | flexible | Last — film when nothing's broken |
| 9 | **README note about test footage** being intentional stress tests | 5 min | Standalone task, do anytime |
| 10 | **CLEAR → UNDO** behavior in viewer (undo last SAM2 dot, not full clear) | 30 min | Berto's other deferred ask |
| 11 | **Pan/zoom while SAM2 active** (middle-click or Ctrl+left for pan) | 45 min | Berto's other deferred ask |

---

## DEFERRED / LOW-PRIORITY

- **Update install.bat** to use junction instead of xcopy (current install.bat would BREAK the junction by overwriting it on a re-run; needs replacing with `mklink /J`)
- **Cleanup obsolete files in ae_plugin/ root** (`ae_processor.py`, `preview_viewer_v2.py` at ae_plugin/ root are now duplicates of the cep_panel/ versions — safe to delete in a future commit)
- **Pre-push hook + Claude SessionStart hook** — automation layer beyond branch protection, nice-to-have
- **Hugging Face dataset for sample green-screen footage** if BRAW added (current 3 H.264 clips already on v0.7.0 release)

---

## KEY FILE PATHS (UPDATED FOR JUNCTION)

| What | Path |
|------|------|
| AE viewer (loaded by AE through junction) | `D:\New AI Projects\CorridorKey\ae_plugin\cep_panel\preview_viewer_v2.py` |
| AE processor (same — junction means CEP path = engine repo path) | `D:\New AI Projects\CorridorKey\ae_plugin\cep_panel\ae_processor.py` |
| AE panel UI | `D:\New AI Projects\CorridorKey\ae_plugin\cep_panel\index.html` |
| AE host JSX | `D:\New AI Projects\CorridorKey\ae_plugin\cep_panel\jsx\host.jsx` |
| DaVinci plugin | `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` |
| DaVinci viewer | `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\preview_viewer_v2.py` |
| Skills file | `D:\AI\skills\corridorkey.md` |
| Engine root | `D:\New AI Projects\CorridorKey` |
| Log | `C:\Users\ragsn\AppData\Local\Temp\corridorkey.log` |

**The CEP install path still works — it's just a junction now:**
`C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel\` → resolves to engine repo cep_panel/

---

## RECOVERY PATHS (multiple safety nets)

| Asset | Location | Purpose |
|---|---|---|
| Bundle | `~/Desktop/ck-pre-merge-2026-04-26.bundle` (59 MB) | Full repo snapshot pre-reset |
| Tag | `pre-sam2-merge-2026-04-26` (in repo) | Points to pre-reset HEAD |
| Branch | `local-work-sam2-integration` (in repo) | All 193 + 1 WIP commits preserved |
| CEP backup | `~/Desktop/CEP_BACKUP_pre-junction-2026-04-26/` (745 KB) | Pre-junction CEP folder snapshot |
| Old CEP folder | `...\com.corridorkey.panel.OLD-pre-junction-2026-04-26` | In-place fallback (just rename back) |
| Per-file bak files | `*.bak-pre-{scrub,soft-sam,sam-video,sam-scrub,anchor-fix}-20260426` | Per-edit-step backups |

---

## START NEXT SESSION WITH

1. Type `/ckey` — loads `D:\AI\skills\corridorkey.md`
2. Read this `SESSION_HANDOFF.md` for current state
3. **READ THE CRITICAL ARCHITECTURE CHANGES section above before editing AE files** — junction layout means edits in `ae_plugin/cep_panel/` go straight to AE
4. Check `git status` and `git log --oneline -3` to confirm repo state
5. Pick the next TODO item (recommended: **#1 — Document junction in CLAUDE.md**)
6. If anything fails, check log first: `C:\Users\ragsn\AppData\Local\Temp\corridorkey.log`
