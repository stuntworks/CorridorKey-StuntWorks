# CorridorKey BRAW Process Range — Full Debug Report
**Date:** 2026-04-23
**Engineer:** Claude (Sonnet 4.6) + Berto Lopez
**Session duration:** ~4 hours across multiple handoffs

---

## THE PROBLEM

CorridorKey's **Process Range** button was completely broken for BRAW clips.

Symptoms:
- BRAW frames exported correctly (6 TIFFs written)
- AI model loaded correctly
- All 6 frames decoded to numpy on main thread
- Thread launched (alive=True)
- **Then: silence. Zero PNGs written. No log output. No errors. Nothing.**
- Resolve did not hang — user could click Process Range again
- Single-frame preview worked fine on the same BRAW clip

---

## WHAT WE THOUGHT THE PROBLEM WAS (AND WASN'T)

### Wrong theory 1: Windows Defender blocking thread file writes
The previous session (2026-04-23 morning) had established that Defender blocks file I/O from background threads. The first fix attempt routed PNG saves through a `_save_queue`: thread encodes PNG bytes in memory, main thread (via PollTimer) writes bytes to disk.

This was architecturally correct for a different problem. It did not fix the actual bug.

### Wrong theory 2: cv2.imwrite() specifically blocked
Previous diagnostics showed `cv2.imwrite()` hanging from background threads. We replaced it with `cv2.imencode()` (in-memory) + queue. Still didn't work.

### Wrong theory 3: Thread not starting
Thread was confirmed alive. But zero probe output appeared. Initially suspected thread startup failure.

### Wrong theory 4: _save_queue code broken
Reviewed all 4 code changes. Syntax was clean. Logic was correct. No bugs found.

---

## THE DIAGNOSTIC PROCESS

### Step 1: Confirm thread completes
Realized the user could run Process Range multiple times without the "Already running" guard triggering. This proved `_range_running` was being reset to False — meaning the thread WAS completing. But producing zero output.

### Step 2: Read probe files
The thread has granular PROBE 3 through PROBE 8 + PROC-A through PROC-F logging. These go through `_ui_queue` → `on_poll_timer` → `ck_thread.txt`.

`ck_thread.txt` did not exist. Zero probes written.

### Step 3: Changed probe handler to call log() directly
Probe handler was writing to `ck_thread.txt` via a separate file open. Changed it to call `log()` directly (which writes to `corridorkey_debug.txt`, a known-working file). Still zero output.

### Step 4: Added MAIN-THREAD-TEST probe
Put a test probe into `_ui_queue` from the MAIN THREAD right after thread launch:
```python
_ui_queue.put(("probe", "MAIN-THREAD-TEST: timer check"))
```
If `on_poll_timer` was draining `_ui_queue`, this would appear in the log regardless of whether the thread was working.

It never appeared. This was the key diagnostic: **the problem was not the thread. The problem was on_poll_timer was not firing.**

### Step 5: Added TIMER-TICK diagnostic
Added `if _range_running: log("TIMER-TICK")` at the top of `on_poll_timer`. If the PollTimer was firing at all after thread launch, this would appear.

It never appeared. **Confirmed: the PollTimer completely stopped firing after thread launch.**

### Step 6: Tried Stop/Start timer restart
Added `items["PollTimer"].Stop(); items["PollTimer"].Start()` right after thread launch. Thought this might revive the timer.

No TIMER-TICK appeared. Stop/Start did not work.

### Step 7: Found the render wait loop
Searched for the BRAW render blocking code:

```python
project.StartRendering(job_id)
deadline = time.time() + 600
while project.IsRenderingInProgress() and time.time() < deadline:
    elapsed = int(time.time() - (deadline - 600))
    status(f"Exporting BRAW frames... {elapsed}s")
    time.sleep(1.0)
```

**This is the culprit.** This `time.sleep(1.0)` loop runs on the main thread inside a Fusion event handler (`on_process_range`). Fusion's event loop is completely blocked for the entire duration of the BRAW render (30-60+ seconds depending on clip length and drive speed).

Fusion apparently has a watchdog or internal state machine that marks the PollTimer as dead when an event handler blocks for too long. Once dead, even Stop/Start cannot revive it.

Non-BRAW clips (MOV, MP4) do not have this render loop. They open quickly via OpenCV. The event handler returns in milliseconds. The PollTimer keeps working normally. That's why all the SGNFLM test clips worked fine.

---

## THE ACTUAL FIX

### Fix 1: Synchronous BRAW processing path

Since the PollTimer is dead after the BRAW render and cannot be revived, the entire `_save_queue` + background thread architecture is useless for BRAW clips.

The observation: by the time the thread would run, all 6 BRAW frames are already decoded as numpy arrays in `_braw_frames_decoded` (done on the main thread before thread launch). There is no reason to use a background thread. We have all the data. We can just process it right now, on the main thread, synchronously.

Added this path directly in `on_process_range`, right after warmup:

```python
if _braw_frames_decoded:
    _range_running = True
    try:
        ofs = []
        pr = 0
        st = time.time()
        for fidx, frame in enumerate(_braw_frames_decoded):
            if processing_cancelled:
                break
            status(f"{pr+1}/{dur}...")
            chroma_float = chroma_hint_gen.generate_hint(frame).astype(np.float32) / 255.0
            fr = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            res = proc.process_frame(fr, chroma_float, ps)
            fg, mt = res.get("fg"), res.get("alpha")
            if _despill_str > 0 and fg is not None:
                fg = _cu.despill_opencv(fg, ...)
            # choke handling...
            if fg is not None and mt is not None:
                op = od / f"CK_{cn}_{pr:06d}.png"
                save_output(fg, mt, op, settings["export_format"])
                ofs.append(str(op))
            pr += 1
            log(f"{pr}/{dur} ({fpsr:.1f}fps)")
        log(f"Done: {len(ofs)} frames in {el:.1f}s")
        if ofs and not processing_cancelled:
            _do_import({"ofs": ofs, ...})
    except Exception as _e:
        log(f"Range error: {_e}")
        status("ERROR!")
    finally:
        _range_running = False
    return  # skip thread launch
```

This is identical to what single-frame mode does, run in a loop. No timer. No queue. No thread. It works because `save_output()` and `_do_import()` run on the main thread.

### Fix 2: Off-by-one in BRAW frame count

After the sync path worked (6 frames, 7.0s), Berto reported the result was one frame short of the expected range.

Root cause: Resolve's timeline end is exclusive. The BRAW export range was computed as:
```python
src_end = ss + (out_f - cs)      # WRONG — misses last frame
```

Fixed to:
```python
src_end = ss + (out_f - cs) + 1  # CORRECT — Resolve timeline end is exclusive
```

---

## RESULT

```
1/6 (0.4fps)
2/6 (0.6fps)
3/6 (0.7fps)
4/6 (0.8fps)
5/6 (0.8fps)
6/6 (0.9fps)
Done: 6 frames in 7.0s
Imported 1 items to MediaPool
Placing on V2 — frames 0-5
AppendToTimeline result: [<BlackmagicFusion.PyRemoteObject ...>]
V1 hidden — press D in timeline to re-enable source clip
```

6 frames. 7 seconds. Imported. Placed on V2. Working.

---

## WHAT THE CODE LOOKS LIKE NOW

| Path | Trigger | Mechanism | Timer needed? |
|---|---|---|---|
| BRAW clips | `_braw_frames_decoded` not empty | Synchronous on main thread | No |
| Non-BRAW clips | `_braw_frames_decoded` empty | Background thread + PollTimer + _save_queue | Yes |

The `_save_queue` architecture remains in the code and works correctly for non-BRAW. The sync path is a branch that runs instead of the thread for BRAW.

---

## LESSONS LEARNED

1. **Fusion's PollTimer can die permanently.** If an event handler blocks for 30+ seconds (e.g., a render queue wait loop), Fusion kills the timer and Stop/Start cannot revive it.

2. **Silence is not the same as hanging.** The thread completed every time. Zero output was not a crash — it was everything going into a queue that nothing was draining.

3. **The simplest path is usually right.** Once we had all frames in memory on the main thread, there was no reason to use a thread at all. The complexity of _save_queue + timer was solving a problem that didn't need to be solved that way.

4. **Diagnostic probes must confirm two things:** (a) the thread is putting data somewhere, AND (b) something is reading it. We were only confirming (a) for a long time.

5. **For BRAW specifically:** Never depend on PollTimer after a BRAW range render. The render loop kills it. Route all post-render work through synchronous calls on the main thread.

---

## FILES

- Installed plugin: `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
- This report: `D:\New AI Projects\CorridorKey-Plugin\BRAW_RANGE_DEBUG_REPORT_2026-04-23.md`
- Handoff (session resume): `D:\New AI Projects\CorridorKey-Plugin\HANDOFF_2026-04-23e.md`
