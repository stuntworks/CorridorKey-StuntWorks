# Part 03. Components

**Scope: the detailed spec of each major subsystem. Load this when the task is deep inside one component. Seven components, one block each, matching the architecture map in Part 02.**

---

# Component: CK Neural Keyer

## Role
Turns a raw green/blue-screen RGB frame into an alpha matte and an estimated foreground color, one frame at a time. This is the primary matte source for the whole project (Part 01 Rule 1).

## Entry points
`CorridorKeyModule/backend.py:390 create_engine()` — factory; resolves the backend (Torch/MLX) and returns an engine object exposing `process_frame()`.
`CorridorKeyModule/inference_engine.py:450 CorridorKeyEngine.process_frame()` — the actual per-frame keyer call: `image` (RGB) + `mask_linear` (alpha hint) in, `{'alpha', 'fg', 'comp'}` out. This is the function every host (Resolve, AE/Premiere) ultimately calls to run the neural keyer.

## Choice and why
CorridorKeyModule, Niko Pueringer / Corridor Digital's open-source neural keyer (PyTorch). Chosen because it was released open source and, on the crew's actual test footage (motion blur, imperfect lighting, fast stunt action), out-performs manual chroma-picking. This is the entire reason the project exists.

## License / sourcing
CC-BY-NC-SA-4.0. Upstream: `github.com/nikopueringer/CorridorKey`. The NonCommercial clause is why the whole StuntWorks build cannot be sold (Part 01 Rule 6, Part 06).

## Interfaces
Inputs: RGB frame (numpy array; from cv2/PyAV decode or braw-decode.exe).
Outputs: alpha channel (float32, 0..1) and an fg_decoder RGB estimate. The fg_decoder is an independent 3-channel conv head, not tied to input RGB, which is exactly why it can hallucinate (see Known weaknesses).

## Device / runtime paths
GPU (CUDA) via CorridorKey.pth weights. CPU fallback exists but README's own hardware table calls it unusably slow; treat GPU as required in practice.

## Known weaknesses and where they are absorbed
fg_decoder hallucinates skin tone for wardrobe colors it has not seen (documented as the yellow-shirt-to-pink-shift investigation, 2026-04-29) because it is untied to input RGB. Absorbed downstream: substitute the source-plate RGB for the model's FG estimate and keep only the learned alpha.
An earlier HSV-based alpha-hint attempt (now banned, see Part 08) flagged tan/khaki/olive fabric as screen color. Absorbed by keeping the alpha hint an inline RGB chroma test, never HSV.

## Protected elements
CorridorKey.pth model path/location. The "CK IS the matte" law itself (Part 01 Rule 1): this stage's output is never downstream-overridden as the primary matte source.

## What not to do here
Never feed the fg_decoder's RGB output straight into a composite as ground-truth color on unseen wardrobe. Never fold SAM2 or merge logic into this stage; it stays a pure per-frame keyer.

---

# Component: SAM2 Support Stage

## Role
Turns operator click-dots (positive = keep, negative = exclude) into a body-silhouette holdout mask, either for one frame (image predictor) or propagated across a batch range (video predictor).

## Entry points
Image predictor (single frame): `resolve_plugin/preview_viewer_v2.py:3436 _apply_sam_mask()` (Resolve host); `ae_plugin/cep_panel/ae_processor.py:4307 cmd_sam_apply()` (Adobe host, the `sam-apply` job).
Video predictor (batch range): `resolve_plugin/CorridorKey_Pro.py:1585 run_sam2_video_propagation()` (Resolve host, a standalone function). On the Adobe host the same propagation is inlined rather than factored out — `ae_plugin/cep_panel/ae_processor.py:2391 cmd_batch()` (predictor calls at :2871-2954) and its duplicate `:3506 cmd_batch_scrub()` (predictor calls at :3701-3728); named plainly here because no single Adobe-side function covers it the way the Resolve side does.
Model construction (Adobe host helpers): `ae_plugin/cep_panel/ae_processor.py:263 _get_video_predictor()`, `:277 _get_sam_image_model()`.

## Choice and why
Meta's SAM2 (`sam2.1_hiera_small.pt`), chosen for click-to-mask quality and DaVinci-side precedent. Runs as a separate process/subprocess so its long-lived GPU state and non-daemon threads never poison the host panel process.

## License / sourcing
`facebookresearch/sam2`, installed via `pip install git+...`, not bundled inside the CC-BY-NC-SA engine itself. Weights are a separate download; redistribution terms for shipping them pre-bundled are not settled (Part 13).

## Interfaces
Inputs: frame(s) plus click points (x, y, positive/negative, the frame number the click was made on).
Outputs: a binary silhouette mask per frame. Image predictor: one frame. Video predictor: one mask per frame across the propagated range.

## Device / runtime paths
GPU. Always a fresh one-shot subprocess for any SAM2 command, never a warm/resident worker (Known weaknesses). Frames must be fed to the video predictor at native, unpadded shape.

## Known weaknesses and where they are absorbed
Alone, SAM2 is a garbage matte: it loses hair and over-tightens on real body detail. Absorbed by the merge stage, which uses SAM2 only as a zoned/dilated holdout, never as the primary matte (Part 01 Rule 1).
Mistracks fast, motion-blurred aerial spins (open, Part 13).
A resident/warm SAM2 worker leaks non-daemon threads and Hydra atexit state and eventually hangs the broker; absorbed by always spawning SAM2 fresh (Part 04, Part 08).

## Protected elements
Video-predictor input frame shape: native, never letterbox-padded to square (Part 04, Part 08). The "always-fresh-subprocess-for-SAM2" rule inside ck_broker.py.

## What not to do here
Never feed square-padded PNG frames to the video predictor. Never make the SAM2 worker warm or resident. Never let SAM2's mask become the primary matte; two-mask/SAM-primary redesigns are a settled dead end (Part 01 Rule 1).

---

# Component: Merge / Garbage-Matte

## Role
Combines the CK alpha with the SAM2 holdout mask (when SAM2 is active) into the matte that actually ships, plus the surrounding post-processing (despill, despeckle, choke, zone-cut, feather).

## Entry points
`corridorkey_sam_merge.py:1794 merge_ck_with_sam_active()` — single dispatch entry point, routes on `MERGE_MODE`.
`corridorkey_sam_merge.py:1388 merge_ck_with_garbage_matte()` — the live `garbage_matte` implementation (the zoned/dilated-SAM-times-CK-alpha formula named in Protected elements).
`sam2_combine.py:339 apply_sam2_gate()` — the single SAM+NN combine entry point used by the 6-7 call sites named in Protected elements.
`ae_plugin/cep_panel/ae_processor.py:1335 apply_matte_postproc()` — the surrounding post-proc pass (despill/despeckle/choke/zone-cut/feather) shared by single/batch/postproc jobs.
`ae_plugin/cep_panel/ae_processor.py:1811 _reconnect_split_body()` — waist/gap-bound bridge fix (2026-07-29), called from `apply_matte_postproc()`.
`ae_plugin/cep_panel/ae_processor.py:1983 _fill_body_holes()` — enclosed-hole fill under the SCREEN-GAP LAW (2026-07-23), called from `apply_matte_postproc()`.

## Choice and why
The "garbage-matte" architecture (CK alpha times zoned/dilated SAM holdout, plain multiply, a chroma escape valve for hair) replaced roughly four weeks of increasingly complex blending attempts (chroma-weight blend, CK-confidence routing, trimap plus Closed-Form Matting, a seam-suppression pass with 19 thresholds across 353 lines) that each produced visible blend artifacts on some real clip. The replacement is about 80 lines and 3 parameters.

## License / sourcing
Original StuntWorks code. No third-party license constraint beyond consuming CK's (CC-BY-NC-SA) and SAM2's outputs.

## Interfaces
Inputs: CK alpha (float32), SAM2 mask (binary, when present), margin/softness/fill-kernel slider values.
Outputs: final alpha (float32, 0..1), ready for export.

## Device / runtime paths
CPU only (numpy/cv2). No GPU needed at this stage.

## Known weaknesses and where they are absorbed
Cannot automatically separate a support wire from a real body-shadow crease when the two physically touch in frame; this is a physics problem, not a laziness problem, and is absorbed by operator negative dots, not an automatic rule.
The `ck_authority` flag (CK overrides SAM wherever green evidence exists) resurrects 83 percent of real support-wire pixels on the project's wire-regression ground-truth clip; it is deliberately never wired to the panel on the garbage_matte engine.

## Protected elements
The SUBTRACT formula itself: `alpha * dilate(SAM2_binary, margin)`, plain multiply, no Gaussian anywhere in this stage (Part 01 Rule 4). `SAM_BASELINE_SMOOTH_SIGMA = 1.0`. `MERGE_MODE = "garbage_matte"` as the active mode. `sam2_combine.py`'s `apply_sam2_gate` as the single combine entry point used by 6-7 call sites.

## What not to do here
Never add Gaussian blur to the mask, the weight, or the soft transition. Never wire `ck_authority_force_gm` to the panel on the garbage_matte engine. Never let a single clip or single frame decide a merge change (Part 01 Rule 3, corpus gate).

---

# Component: CEP Panel (Adobe: After Effects + Premiere Pro)

## Role
The in-host UI (a rack-style panel) plus the orchestration script that turns button clicks and canvas dots into Python jobs, and imports the results back onto the AE/Premiere timeline.

## Entry points
`ae_plugin/cep_panel/index.html:3462 runPython()` and `:3483 runPythonAsync()` — build/dispatch the JSON job to `ae_processor.py` via `ck_send.py`; `runPythonAsync` is the non-blocking variant used so the panel UI thread never freezes.
`ae_plugin/cep_panel/jsx/host.jsx:586 ae_createSAMPrecomp()`, `:994 ppro_importFrame()`, `:1079 ppro_importSequence()` — the ExtendScript callbacks that place results back on the AE/Premiere timeline.

## Choice and why
Adobe's CEP (Common Extensibility Platform) is the supported way to build a custom panel UI inside AE/Premiere without a compiled native plugin. `index.html` (Chromium/CEF webview) plus `jsx/host.jsx` (ExtendScript) is Adobe's standard CEP shape, shared by both host apps from one manifest.

## License / sourcing
Original StuntWorks code. CEP itself ships as part of the host application, not a bundled dependency.

## Interfaces
Inputs: operator clicks (KEY CURRENT FRAME, PROCESS WORK AREA / IN-OUT RANGE, APPLY MASK, CLEAR, sliders), SAM2 dot placements on the in-panel canvas.
Outputs: a JSON job dispatched to Python (`extract` / `cache` / `sam-apply` / `postproc` / `batch`), and ExtendScript calls back into the host (`ppro_importFrame`, `ae_createSAMPrecomp`, `ppro_importSequence`, etc.) that place results on the timeline.

## Device / runtime paths
Runs inside a CEF child process (Chromium sandbox); cannot itself initialize CUDA (see Component: ck_broker, and Part 04). One manifest and one panel serve both AEFT and PPRO host types.

## Known weaknesses and where they are absorbed
CEF's sandbox blocks CUDA entirely; absorbed by the ck_broker bridge (Part 04). CEF caches `index.html` aggressively; a panel-only reload can serve stale HTML for hours (Part 08).

## Protected elements
`CSXS/manifest.xml` (Adobe reads this to load the panel; a stray `--` inside an XML comment silently kills the entire extension load with no error UI). The `ae_plugin/cep_panel` junction (editing the same-named file at the `ae_plugin/` root does nothing at runtime).

## What not to do here
Never edit `ae_plugin/ae_processor.py` (repo root, an explicitly marked DEAD DUMMY) expecting it to affect the live panel. Never call `window.resizeTo()` in the panel JS (crashes `CEPHtmlEngine.exe`). Never ship panel UI changes blind, without an actual AE/Premiere screenshot first.

---

# Component: ck_broker (CUDA Sandbox Bridge)

## Role
Lets CUDA-heavy Python jobs run at all when triggered from an Adobe CEP panel, by executing them in a process the CEF sandbox never touched. This is the mechanism at the center of Part 04, the hard part.

## Entry points
`ae_plugin/cep_panel/ck_broker.py:629 main()` — process entry: loads config, binds the loopback socket, accepts connections, spawns a handler thread per connection.
`ae_plugin/cep_panel/ck_broker.py:536 _handle_run()` — the allow-list check (`ALLOWED_CMDS`) plus the routing table that sends each subcommand to the warm worker (`_run_via_worker`) or a fresh one-shot subprocess (`_run_via_sam_subprocess`).

## Choice and why
A persistent broker process (`ck_broker.py`) started by a Windows per-user logon Scheduled Task (parent = Task Scheduler/svchost, never sandboxed), listening on a loopback TCP socket. Chosen after every in-sandbox mitigation-stripping attempt (`ck_launch.py`) still crashed, because CEF's process mitigations (ACG, Win32k syscall disable) and its restricted token are inherited by any child spawned from inside AE's process tree and cannot be turned off by that child.

## License / sourcing
Original StuntWorks code.

## Interfaces
Inputs: newline-delimited JSON over `127.0.0.1` (a pre-shared secret from `ck_broker_config.json`, an allow-listed subcommand, engine args, cwd, CK root).
Outputs: newline-delimited JSON lines (stdout passthrough from the engine subprocess), then a final `done`/exit-code line.

## Device / runtime paths
Runs a warm `ck_engine_worker.py` subprocess for CNN-only (CK keyer) jobs; always spawns a fresh one-shot subprocess for any SAM2 command. Installed as a per-user logon Scheduled Task via `install_broker_task.ps1`.

## Known weaknesses and where they are absorbed
Single point of failure for all Adobe-side CUDA work: if the Scheduled Task is not registered, or the broker process is not running, a CUDA job has nowhere to go. At last review the operator-facing symptom is a stalled job rather than a clear "broker not running" error; this is a known hardening gap, not yet a tracked open issue (Part 13 candidate).

## Protected elements
The shared secret in `ck_broker_config.json` (gitignored, name and role only ever belong in a document, never the value). The loopback-only bind (`127.0.0.1`). The SAM2-always-fresh-subprocess rule. The allow-listed-subcommand check (only `ae_processor.py` subcommands, never an arbitrary command).

## What not to do here
Never make the broker reachable on a non-loopback interface. Never let it execute anything other than an allow-listed `ae_processor.py` subcommand. Never resurrect a warm SAM2 worker inside the broker.

---

# Component: braw-decode.exe (BRAW Sidecar)

## Role
Decodes Blackmagic RAW (`.braw`) frames that neither `cv2`, `FFmpeg`, nor `PyAV` can read, streaming raw BGRA pixel bytes to the calling Python process.

## Entry points
`braw-decode.exe` itself is a native executable in the sibling repo `D:\New AI Projects\braw-decode-win`, outside this repo — no file:line applies to the exe. The entry points below are this repo's calling/decode side:
`resolve_plugin/CorridorKey_Pro.py:2524 _try_braw_decode_exe()` — launches the exe subprocess and streams frames (Resolve host).
`resolve_plugin/CorridorKey_Pro.py:2503 _read_exact()` — the byte-exact read contract named in Protected elements.
`ae_plugin/cep_panel/ae_processor.py:487 _braw_read_exact()` — the Adobe-host equivalent, explicitly mirroring `resolve_plugin`'s `_read_exact()` per its own docstring.

## Choice and why
A dedicated native executable built against the Blackmagic RAW SDK (`BlackmagicRawAPI.dll`), because `.braw` is a proprietary codec with no OpenCV/FFmpeg/PyAV support. This was the path found that reads BRAW directly, without round-tripping through Resolve's render queue.

## License / sourcing
Custom StuntWorks-built executable, in a sibling repo (`braw-decode-win`). Depends on the Blackmagic RAW SDK / `BlackmagicRawAPI.dll`, which ships with DaVinci Resolve or Blackmagic's own installer. Redistribution terms for that DLL on a machine without Resolve installed are UNKNOWN (Part 13).

## Interfaces
Inputs: `.braw` file path, frame range, a `-n` info-only flag for dimension probing.
Outputs: raw BGRA bytes on stdout, with no frame separator. The caller must read exactly `width * height * 4` bytes per frame or the stream desyncs and every subsequent frame is corrupt.

## Device / runtime paths
CPU-side decode via the vendor SDK. Invoked as a subprocess from both `CorridorKey_Pro.py` (Resolve) and `ae_processor.py` (Adobe); candidate paths are checked in order (the ProgramData Fusion Scripts Utility copy, then the sibling dev-repo build), falling back to the slower Resolve render-queue path if the exe is not found.

## Known weaknesses and where they are absorbed
Decodes flat (BMD Film log / wide gamut) by default, which desaturates green below the chroma keyer's usable threshold. Absorbed by a Rec.709 color-science override via the BRAW SDK's clip-processing-attributes API, later split into a two-stream decode (key on Rec.709, ship native color) after forcing native color alone starved the chroma key from the other direction.

## Protected elements
The exact byte-read contract (`_read_exact` must consume exactly `width * height * 4` bytes per frame). The `BRAW_SDK_PATH` / `PATH` environment injection that lets the DLL find its own dependencies (missing this crashes the DLL at init with `0xC0000005`). The `CREATE_NO_WINDOW` plus `stdin=DEVNULL` subprocess flags (`DETACHED_PROCESS` gave the subprocess null std handles and crashed the DLL during `DLL_PROCESS_ATTACH`).

## What not to do here
Never open a BRAW TIFF export from a background thread (Windows Defender scans background-thread file opens and silently stalls; pre-read into `BytesIO` on the main thread first). Never assume the exe is present; always check and fall back gracefully.

---

# Component: Deploy / Install System

## Role
Gets source-tree edits into the exact OS folders each host application actually loads from, and performs first-time installation into Resolve/AE/Premiere.

## Entry points
`deploy.py:453 main()` — CLI entry (`--map` / deploy / `--revert` / `--list-backups`); `:244 cmd_deploy()` pushes the `DEPLOYMENTS` table (`:60`, DaVinci-plugin only) with a timestamped backup; `:343 cmd_revert()` restores from one.
`install.py:375 main()` — CLI entry; `:118 install_resolve()` and `:200 install_adobe()` are the first-time per-host installers. Note found while verifying this block: `install_adobe()` (`install.py:225-237`) currently does a plain `shutil.copytree`/`copy2`, not an `mklink /J` junction — the live AE junction referenced elsewhere in this bible (`deploy.py:81-86` comment) predates this script and is not created by either `deploy.py` or `install.py` today. Flagged here as observation only; prose elsewhere in this bible is unchanged per this pass's scope.

## Choice and why
`deploy.py` (push with timestamped backup, `--revert`, 30-day retention) is the only safe day-to-day deploy path. `install.py` handles first-time setup and is junction-aware for the AE side, so the CEP install path and the repo folder end up as the same physical files.

## License / sourcing
Original StuntWorks code.

## Interfaces
Inputs: source files in the repo (`CorridorKey_Pro.py`, `cep_panel/`, `core/`, `ui/`, `resolve_corridorkey.py`).
Outputs: copies (Resolve side) or a junction (AE side, via `mklink /J`) at the paths each host actually reads: Fusion `Scripts/Utility` (Resolve), `%APPDATA%/Adobe/CEP/extensions/com.corridorkey.panel` (AE/Premiere).

## Device / runtime paths
Not applicable; filesystem operations only.

## Known weaknesses and where they are absorbed
`install.py`'s Resolve installer is stale relative to the live plugin: it still deploys the superseded `resolve_corridorkey.py` / `core` / `ui` OOP path, not `CorridorKey_Pro.py` or `preview_viewer_v2.py`, which are what Berto actually runs. Not fixed as of last scan (Part 12, Part 13).
`install.py`'s clean-install copy step once let the root `ae_processor.py` dummy overwrite the canonical `cep_panel` copy; fixed 2026-05-29 (Part 08).

## Protected elements
The AE junction itself (`mklink /J`; running the old `install.bat`'s `xcopy`-based path would overwrite/break it). `write_plugin.py` (deprecated, no-backup footgun, kept but not to be used). `deploy.py`'s `DEPLOYMENTS` path table (must match what each host actually auto-discovers).

## What not to do here
Never run `install.bat` or `write_plugin.py` as the deploy path. Never assume `install.py` deploys the live Resolve plugin; verify which generation is actually live first (Part 12). Always delete stale `__pycache__` `.pyc` files after editing `corridorkey_sam_merge.py` or any merge-adjacent module, and fully restart the host app, or the old compiled module keeps running.
