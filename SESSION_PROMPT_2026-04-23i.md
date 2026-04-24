# CorridorKey Pro — New Session Prompt
# Use this as the opening prompt for the next coding session.

---

## WHO YOU ARE / CONTEXT

You are working on **CorridorKey Pro** — a DaVinci Resolve Fusion panel plugin for AI greenscreen keying.
Developer: Berto Lopez, StuntWorks/Berto Labs NYC.

**Key files:**
- Plugin panel: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
- Live preview viewer: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\preview_viewer_v2.py`
- Session temp dir: `C:\Users\ragsn\AppData\Local\Temp\corridorkey_session\`

---

## WHAT WE FIXED THIS SESSION (confirmed working)

### CorridorKey.py

1. **alpha_method flip-flop** — `_merge_live_params()` now checks if `sam2_mask.png` exists on disk and forces `alpha_method=1` regardless of panel dropdown or live_params.json state. Previously the SAM2 gate was being skipped on every other PROCESS FRAME click.

2. **Path 0 — no-timeline-switch frame read** — `_read_frame_via_resolve_render()` now has a `try_direct=True` parameter. When called from `process_current_frame`, it tries `project.ExportCurrentFrameAsStill()` directly BEFORE creating any temp timeline. This eliminates `SetCurrentTimeline` calls on successful PROCESS FRAME clicks. Confirmed working in log: `OK (Path 0 — direct still, no timeline switch)`.

3. **Single-frame BRAW audio guard** — Path B (`StartRendering`) in `_read_frame_via_resolve_render` is now blocked for `.braw/.cin/.dng/.ari` files. Returns None with clear error instead of killing audio.

4. **Dead render-queue code removed** — `_export_braw_range_to_frames` had 85 lines of unreachable code including a `StartRendering` call for BRAW that could never be reached but was a maintenance trap. Deleted.

5. **`time` import bug** — `_try_braw_decode_exe()` used `time.time()` but `time` was never imported in that function scope. Fixed: added `time as _time2` to local imports, changed call to `_time2.time()`. This was causing a `NameError` every time PROCESS RANGE ran on BRAW, making braw-decode.exe appear to fail even when the exe was fine.

6. **`subprocess` scope bug in `on_close`** — `taskkill` call used `subprocess.CREATE_NO_WINDOW` but `subprocess` was not in scope (local import was aliased as `_sp`). Fixed to `_sp.CREATE_NO_WINDOW`.

7. **Resolve hang on close** — `on_close` now always runs `taskkill /F /T /PID` to kill the entire viewer process tree (viewer + any SAM2 child processes). Previously only killed the direct viewer process, leaving children holding the GPU. This was why Resolve needed End Task to close.

8. **PollTimer adaptive** — PollTimer (Fusion UI timer that updates the log/progress widgets) was firing every 500ms unconditionally = 120 Resolve main-thread interrupts per minute. Now: 500ms while processing is active, 5000ms when idle. Should reduce or eliminate the continuous audio pops while plugin is open doing nothing.

9. **BRAW decode subprocess flags** — Changed from `CREATE_NEW_CONSOLE` to `DETACHED_PROCESS` on both the `-n` info query and the decode Popen. `CREATE_NEW_CONSOLE` was spawning a new conhost.exe every time braw-decode.exe ran, which triggered Windows audio session manager resets = system-wide audio pop on every PROCESS RANGE attempt.

10. **Resolve install dir added to braw-decode.exe PATH** — Added `C:\Program Files\Blackmagic Design\DaVinci Resolve` to the subprocess PATH env var so BlackmagicRawAPI.dll's own dependency DLLs can be found by the Windows loader. `BRAW_SDK_PATH` alone was not sufficient.

11. **CLEAR button confirm dialog** — In viewer, clicking CLEAR now shows a dialog: "CLEAR will delete your SAM2 mask gate. Continue?" Defaults to No. Prevents accidental deletion of the render gate.

12. **SAM2 gate status in on_close** — Minor UX improvement.

### preview_viewer_v2.py

13. **JumpSlider** — Added custom `JumpSlider` class replacing all 3 `QSlider` instances (despill, despeckle size, choke). Snaps immediately to click position and tracks 1:1 with mouse drag. Default Qt slider moved in slow steps toward cursor.

14. **Render throttle** — Replaced debounce pattern with throttle: renders immediately on first slider touch, then at most once per 60ms during continuous drag. Old debounce only fired 60ms after drag STOPPED — sliders felt dead.

15. **Despeckle init** — Removed racing `singleShot(0, _on_despeckle_toggled)` that fired before live_params.json was loaded. Added `singleShot(300, win._render_now)` after `win.show()` so first render fires after poll has loaded correct params. Fixed "must cycle despeckle checkbox before it works."

16. **Despill + despeckle sliders** — Both now use `_schedule_render()` (throttle) instead of `_render_now()` directly. Prevents 4K lockup on drag.

17. **`on_update` param ordering** — `self._params = merged` is now committed BEFORE any `_render_now()` call inside `on_update`. Previously the render fired with stale hardcoded defaults because params were merged AFTER the rekeying render.

---

## WHAT IS STILL BROKEN / NOT CONFIRMED FIXED

### PROBLEM 1 — AUDIO POPS (highest priority)

**Symptom:** System-wide audio pops on the Focusrite interface. Happens:
- Once when Resolve starts (likely Resolve's own audio engine init — possibly unfixable from plugin)
- Continuously while plugin is open (should be fixed by PollTimer adaptive — NOT YET CONFIRMED by user)

**What we tried:**
- `CREATE_NEW_CONSOLE` → `DETACHED_PROCESS` on braw-decode.exe (removed conhost.exe spawning)
- PollTimer 500ms → adaptive 500ms/5000ms (reduces Resolve main-thread interrupts)
- Path 0 for PROCESS FRAME eliminates SetCurrentTimeline (which resets audio engine)
- Dead StartRendering code removed

**What user tried:**
- Unplugging/replugging Focusrite (temporary fix, pops return on Resolve restart)
- 48kHz sample rate confirmed correct everywhere
- Changing Resolve audio device to non-Focusrite was REJECTED — user does all sound work on Focusrite

**Status:** PollTimer fix applied but user has NOT confirmed whether continuous idle pops are gone. User reloaded plugin and immediately reported blank screen (different issue). Audio investigation interrupted.

**Next steps:**
- Ask user: with plugin sitting idle (no clicking, viewer closed), do the pops still happen? If yes at regular ~5s intervals it's still the PollTimer. If gone, the fix worked.
- If still popping randomly: investigate whether Fusion's event loop itself (not our timer) causes audio interrupts. Consider stopping PollTimer entirely when idle and restarting only on button click.
- If popping only during PROCESS FRAME: check if Path 0 is succeeding (log should say "Path 0 — direct still"). If Path A fires (temp timeline), it still causes SetCurrentTimeline = audio pop.

### PROBLEM 2 — BRAW DECODE EXE FAILING (PROCESS RANGE broken)

**Symptom:** `BRAW decode exe: info failed (rc=3221225477)` — that's 0xC0000005 access violation. braw-decode.exe crashes on startup.

**Root cause suspected:** BlackmagicRawAPI.dll cannot find its dependency DLLs because the Resolve install directory was not in the subprocess PATH.

**Fixes applied this session (NOT yet tested for BRAW decode):**
1. Resolve install dir now prepended to braw-decode.exe subprocess PATH
2. `DETACHED_PROCESS` flag (no conhost.exe, clean NULL handles for DLL init)
3. `time` import bug fixed (was causing NameError before exe even ran)

**Next test:** Click PROCESS RANGE on a BRAW clip. Check log for:
- SUCCESS: `BRAW decode exe: 4096x2160` (or whatever resolution)
- STILL FAILING: `info failed (rc=...)` — if still 0xC0000005, the DLL dependency issue is not the root cause. May need to check if BlackmagicRawAPI.dll requires the Resolve process to be running (IPC/COM dependency).

**If exe keeps failing:** Consider routing single-frame BRAW (PROCESS FRAME) fully through braw-decode.exe as well (it accepts `-i <start> -o <start+1>` for single frames), bypassing Resolve's render path entirely.

### PROBLEM 3 — BLANK SCREEN IN LIVE VIEWER

**NOT A CODE BUG.** The SAM2 gate (`sam2_mask.png`) is stale from a previous session. Log shows:
```
SAM2 output gate applied — alpha mean 0.363 -> 0.000
Matte after norm — min:0 max:255 mean:0.0
```
The mask marks a region where the actor was in a previous frame/session. Neural alpha gets multiplied by wrong-location mask → result is zero everywhere → blank composite.

**Fix for user:** In live viewer → CLEAR → confirm dialog → Yes → re-click SAM2 points on current frame → APPLY MASK.

**Do NOT change any code for this.**

---

## ARCHITECTURE NOTES FOR NEXT SESSION

- SESSION_DIR is fixed (no PID): `C:\Users\ragsn\AppData\Local\Temp\corridorkey_session\`
- `sam2_mask.png` in SESSION_DIR persists across viewer restarts (intentional) — but is frame-specific, so if user changes frames or sessions it will be wrong
- `live_params.json` holds despill/despeckle/choke values written by viewer, read by plugin
- braw-decode.exe locations checked: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\braw-decode.exe` and `D:\New AI Projects\braw-decode-win\bin\braw-decode.exe`
- `ExportCurrentFrameAsStill` works on Resolve 18.5+ — confirmed in log
- PollTimer is a Fusion UIManager timer (not Qt) — fires on Resolve's main thread

---

## KNOWN REMAINING BUGS (from earlier agent audit, not yet fixed)

- `_do_import` called from PollTimer can trigger `ImportMedia` + `AppendToTimeline` after processing — these Resolve API calls may cause minor audio pops post-render
- `process_current_frame` crashes with `AttributeError` if NN returns None alpha (guard missing)
- `_do_import` doesn't verify Resolve grouped PNGs as a sequence vs individual stills
- Background plate BRAW (`_read_frame_via_resolve_render` with `try_direct=False`) still causes SetCurrentTimeline audio pop — no clean fix without caching BG frame
