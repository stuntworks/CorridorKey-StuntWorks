# Part 04. The Hard Part

**Scope: getting a CUDA-heavy neural keying pipeline to actually run, and run correctly, inside Adobe's hostile host sandbox. Load this when the task touches CUDA-from-CEP, Premiere track creation, or BRAW decode, or when an assistant claims any of this is not possible.**

---

## The problem

Adobe's CEP panels run inside a CEF (Chromium Embedded Framework) child process, which is sandboxed on purpose as a browser security boundary: it inherits sticky OS-level process mitigations (ACG, Arbitrary Code Guard; Win32k syscall disable) and a restricted access token. CorridorKey's real work, CUDA-based neural keying and SAM2 inference, needs exactly what that sandbox forbids: dynamically-generated GPU driver code paths and full Win32k access. Any CUDA call made from inside the panel's own process, or from any child process it spawns (even one launched with every mitigation-stripping `CreateProcess` flag available), crashes with an `0xC0000005` access violation at `cuInit`. The mitigations are inherited deeply enough that a child cannot remove them from itself.

Two more walls stood in the way of the same overall goal, getting the pipeline to actually run and produce correct output inside these hosts, and both turned out to be the same shape of problem: an Adobe-side surface that looks scriptable but silently is not. Premiere's QE (Quality Engineering) DOM API for creating additional timeline tracks is undocumented and unsupported; anything placed on a QE-created track renders completely blank, with no error. And Blackmagic RAW footage cannot be decoded by anything already in the Python stack: `cv2`, `FFmpeg`, and `PyAV` all fail on `.braw` identically.

The obvious approaches for all three fail the same way: they assume the host environment will cooperate if you script it carefully enough. It does not. The sandbox, the QE API, and the missing codec are not bugs to work around with cleverness; they are walls that do not move no matter how the caller behaves.

---

## The solution

1. **CUDA broker: escape the CEF sandbox entirely, do not fight it from inside.** A separate, persistent process (`ck_broker.py`) is started by a Windows per-user logon Scheduled Task, whose parent is Task Scheduler/svchost, never AE. Because it was never inside AE's process tree, it never inherited AE's CEF mitigations, and CUDA initializes normally. The CEP panel talks to it over a loopback TCP socket with a shared secret; the broker allow-lists exactly one script (`ae_processor.py`) and one set of subcommands, so this is a narrow, authenticated bridge, not an arbitrary-code hole.
2. **Warm/cold subprocess split inside the broker.** CNN-only (CK keyer) jobs reuse a warm worker process for speed. Any SAM2 job always gets a brand-new subprocess, because SAM2 leaves non-daemon threads and Hydra atexit state that hang a warm process after a handful of jobs.
3. **Premiere sequence preset: route around the broken QE API by moving the one-time setup to the operator.** Instead of scripting new tracks at runtime, the operator authors a 3-track sequence preset once (`CK_3TRACK.sqpreset`); the panel instantiates new sequences from that preset, so no QE-created track is ever asked to hold a real clip.
4. **Custom BRAW decoder: route around the missing codec support with a dedicated native bridge.** `braw-decode.exe`, linked directly against the Blackmagic RAW SDK, decodes `.braw` frames and streams raw pixel bytes to the calling Python process over stdout, with a byte-exact read contract on the receiving end, plus a Rec.709 color-science override so the flat native decode is actually keyable.

---

## What the naive approach breaks

Calling anything that triggers `cuInit` from inside the CEP panel's own Python child process crashes it every time with `0xC0000005`, even after stripping every mitigation flag `CreateProcess` exposes and using `CREATE_BREAKAWAY_FROM_JOB` (the `ck_launch.py` attempt); the mitigations are inherited at a level a child process cannot undo on itself.

Scripting Premiere's QE `addTracks` path produces tracks that accept clips in the UI but render completely blank, with zero error and effectively zero documentation of why.

Feeding `cv2.imread` or `PyAV` a `.braw` file returns silent failures (`None`, or an identical "Load failed" message) in both host apps, because neither links the proprietary Blackmagic codec. And even once a raw decode exists, feeding its flat, log-space output straight to the keyer starves the chroma key (saturation around 0.23 on real green), so "can we read the file" and "is the color right to key" turn out to be two separate problems.

---

## Governing principle

When a host application's security boundary blocks a capability you need, do not fight the boundary from inside it. Move the forbidden operation to a process (or a person, for the Premiere preset) the boundary was never applied to, and bridge the two sides with an explicit, narrow, authenticated protocol.

---

## Proof it works

A Task-Scheduler-launched broker running a "single" CUDA-keying job exits 0 on the same machine, same code, where the identical command run inside AE's CEP child process crashes `0xC0000005`. Proven end-to-end 2026-06-16.
Tag: VERIFIED. Evidence: session-handoff-2026-06-16-ck-cuda-broker-and-warm-engine.md. Last verified: 2026-06-16. Recheck when: Adobe changes CEP's process-creation model, or Windows changes Scheduled Task session semantics.

The 3-track sequence preset workflow ships live keyed output onto all three tracks in Premiere without touching the QE API, confirmed during the 2026-07-18 parity marathon.
Tag: VERIFIED. Evidence: session-handoff-2026-07-18-ck-premiere-parity-marathon.md. Last verified: 2026-07-18. Recheck when: Adobe ships a documented, supported track-creation API for CEP.

`braw-decode.exe` successfully streams decoded frames from real BRAW footage in both AE and Premiere, where `cv2`/`FFmpeg`/`PyAV` all failed identically. Confirmed 2026-07-16, with the Rec.709 color-science override confirmed keyable on 2026-07-18.
Tag: VERIFIED. Evidence: reference-corridorkey-cannot-read-braw.md; session-handoff-2026-07-18-ck-premiere-parity-marathon.md. Last verified: 2026-07-18. Recheck when: the Blackmagic RAW SDK version bundled with Resolve changes, or a new BRAW variant (higher bit depth, new color science) appears.

---

## The graveyard: paths that failed first

### Attempt: PATH-strip / add_dll_directory DLL fix for the 0xC0000005 crash
Tried: assumed the AE-spawned engine crash was a DLL-loading problem and tried PATH-stripping plus `add_dll_directory` fixes first.
Why it failed: not a DLL or VRAM problem at all. Adobe's CEP/CEF sandboxes child processes with Job Object mitigation policies (ACG = PROHIBIT_DYNAMIC_CODE, WIN32K_SYSTEM_CALL_DISABLE); `cuInit` needs dynamic code generation and win32k access, so the GPU driver faults under the sandbox regardless of which DLLs are on PATH.
What it taught: the crash was a process-mitigation problem, not a linking problem, which redirected the investigation toward `CreateProcess` flags.
Evidence: KNOWLEDGE_LOG_ARCHIVE:3833-3837.
Tag: FAILED APPROACH. Last verified: 2026-06-16.

### Attempt: ck_launch.py, a mitigation-policy-stripping launcher
Tried: `ctypes CreateProcessW` with `STARTUPINFOEX`, explicitly turning the mitigation policies off, plus `CREATE_BREAKAWAY_FROM_JOB` and a clean environment, to let a child process initialize CUDA from inside AE.
Why it failed: the mitigations are inherited stickily enough, and the restricted token is fixed enough, that a child spawned from inside AE's own process tree cannot undo them on itself even with every flag set. `faulthandler` in the engine captured the exact native crash frame, confirming this was not fixable from the inside.
What it taught: the fix could not be "spawn more carefully from inside AE"; it had to be "spawn from a parent that was never inside AE's tree at all," which is what the Scheduled-Task broker does. `ck_launch.py` is kept in the repo, explicitly marked dead in `index.html`'s own comments, superseded by `ck_send.py` + `ck_broker.py`.
Evidence: KNOWLEDGE_LOG_ARCHIVE:3833-3837; ae_plugin/CLAUDE-MAP/INDEX.md Known Issues.
Tag: FAILED APPROACH. Last verified: 2026-06-16.

### Attempt: warm GPU worker (broker keeps the engine loaded between jobs, CNN and SAM2 alike)
Tried: a persistent broker-side worker process that imports the engine once and reuses it across every job, for speed.
Why it failed: after finishing SAM2 jobs specifically, the process hung. SAM2 leaves non-daemon threads (`AsyncVideoFrameLoader`) alive and Hydra's atexit state never emits a clean DONE; once poisoned, even unrelated postproc/cache commands routed through the same warm process jammed for five minutes at a time.
What it taught: warmth is safe for the CNN-only path but actively unsafe for anything that touches SAM2, which is what produced the current split: a warm worker for CK-only jobs, an always-fresh subprocess for any SAM2 command.
Evidence: session-handoff-2026-06-24-ck-ae-render-clean-broker-rebuild.md; session-handoff-2026-06-25-ck-ae-render-clean-broker-rebuild.md; KNOWLEDGE_LOG_ARCHIVE:3928-3938.
Tag: FAILED APPROACH. Last verified: 2026-06-25.

### Attempt: Premiere's QE (Quality Engineering) DOM API for creating the extra tracks the 3-track workflow needs
Tried: scripted Adobe's undocumented QE DOM (`qe.project.getActiveSequence().addTracks()` and related calls) to create additional video tracks at runtime for the CK/SAM matte stack.
Why it failed: clips placed on a QE-created track render completely blank in Premiere's output, with no error, no refresh call that fixes it, and effectively no documentation of why. Four separate internal theories were tried and ruled out before external research confirmed the QE API is simply unsupported and broken for this use.
What it taught: some Adobe scripting surfaces are unsupported strongly enough that no amount of internal debugging fixes them; the answer is to stop using the broken API and move the one-time setup work (a sequence preset) to the operator instead of automating it.
Evidence: session-handoff-2026-07-18-ck-premiere-parity-marathon.md.
Tag: FAILED APPROACH. Last verified: 2026-07-18.

### Attempt: raw BRAW decode fed straight to the chroma keyer
Tried: once `braw-decode.exe` existed and could produce pixels at all, fed its raw output directly into the keying pipeline.
Why it failed: the SDK decodes BRAW flat by default (Blackmagic Film wide-gamut log color space), which desaturates green far enough (saturation around 0.23) that the chroma keyer cannot reliably grab it. A follow-up attempt to force native color for the whole pipeline (`-k native`) then starved the chroma key from the other direction.
What it taught: "can we read the file" and "is the color right for keying" are two separate problems; a working decode is not automatically a usable decode. This is what forced the eventual two-stream decode: key on Rec.709, ship native color.
Evidence: reference-corridorkey-cannot-read-braw.md; session-handoff-2026-07-18-ck-premiere-parity-marathon.md.
Tag: FAILED APPROACH. Last verified: 2026-07-18.

### Attempt: SAM2 video predictor fed letterbox-padded-to-square PNG frames
Tried: roughly twelve separate tuning attempts across a month (pre-roll changes, cold-start changes, two-pass propagation, margin tuning, edge-feather tuning) to fix chest/butt holes appearing partway through SAM2-propagated ranges.
Why it failed: none of the tuning attempts touched the actual variable. The video predictor was being fed PNG frames letterbox-padded to a square aspect ratio; that padding silently breaks SAM2's temporal propagation, so only the anchor frame gets a valid mask and every frame after it degrades. DaVinci's own code had already found and reverted this exact bug once before, and it was reintroduced.
What it taught: SAM2's video predictor is sensitive to frame shape and format in a way that produces plausible-looking-but-wrong output rather than an obvious crash, which is why a month of matte-quality tuning missed it. The fix is a one-line format change (JPEG at native, unpadded shape, in all three video-predictor call sites), not a tuning change.
Evidence: reference-ck-sam2-png-square-breaks-video-propagation.md.
Tag: FAILED APPROACH. Last verified: 2026-06-29.

### Attempt: build the Adobe panel's SAM/scrub UI entirely blind, with no visibility into the running panel from the coding session
Tried: shipped several UI passes for the CEP panel's canvas/scrub/SAM controls without ever seeing the panel actually running in AE.
Why it failed: many passes shipped broken. A new UI layer was stacked on top of an old one that was never removed (a dead dot-scrubber left in place, which the operator then clicked expecting the new control).
What it taught: a CEP panel is a real, visible surface, not just a script; it needs the same look-before-you-ship discipline as any other UI, via a log-file bridge (`window.onerror` to `%TEMP%\corridorkey.log`) and a screenshot before shipping, not blind code review.
Evidence: KNOWLEDGE_LOG_ARCHIVE:3948-3953; feedback-dont-build-cep-panel-ui-blind.md.
Tag: FAILED APPROACH. Last verified: 2026-06-26.
