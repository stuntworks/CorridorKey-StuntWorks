# Part 02. Architecture Map

**Scope: how the pieces fit and how signal or data moves through them. Load this for anything about structure, flow, or the process model. For one component in depth, load Part 03 instead.**

---

## Runtime shape

CorridorKey is multi-host and multi-process. There is no single "the app"; there are two separate integration shapes that both call into the same underlying engine (CorridorKeyModule + SAM2 + corridorkey_sam_merge.py).

**DaVinci Resolve path.** `resolve_plugin/CorridorKey_Pro.py` runs directly inside Resolve's own embedded Python interpreter as a Fusion `UIManager` script, single top-level file, no `main()`. GPU inference (CK keyer, SAM2) happens in that same process. A live-slider preview is a separate OS process, `preview_viewer_v2.py` (PySide6), spawned so CUDA inference and the Qt event loop never share a process or fight over the GIL/event loop.
Tag: VERIFIED. Last verified: 2026-07-22 (resolve_plugin/CLAUDE-MAP/INDEX.md, resolve_plugin/CorridorKey_Pro.py).

**Adobe path (After Effects + Premiere Pro).** The CEP panel (`ae_plugin/cep_panel/index.html`, a Chromium/CEF webview, plus `jsx/host.jsx` ExtendScript) is UI and orchestration only; it does no neural-net math itself and cannot, because CEF child processes cannot initialize CUDA (Part 04). It shells a JSON job to `ck_send.py`, which forwards the job over a loopback TCP socket to `ck_broker.py`, a persistent process started outside AE's process tree by a Windows logon Scheduled Task. The broker runs the job through a warm `ck_engine_worker.py` (CNN-only commands) or a fresh one-shot subprocess (any SAM2 command), both of which execute `ae_plugin/cep_panel/ae_processor.py`.
Tag: VERIFIED. Last verified: 2026-07-22 (ae_plugin/CLAUDE-MAP/INDEX.md, ae_plugin/cep_panel/ck_broker.py header).

**BRAW decode sidecar.** `braw-decode.exe`, a separate native executable in a sibling repo (`D:/New AI Projects/braw-decode-win`), linked against the Blackmagic RAW SDK. Invoked as a subprocess by both `CorridorKey_Pro.py` and `ae_processor.py` whenever the source clip is proprietary `.braw` footage that `cv2`/`FFmpeg`/`PyAV` cannot decode.
Tag: VERIFIED. Last verified: 2026-07-22 (resolve_plugin/CorridorKey_Pro.py:2513-2570).

---

## Primary flow

```
 SOURCE CLIP  (green/blue screen, sitting in the host timeline)
     |
     v
 FRAME READ            cv2 / PyAV for MOV-HEVC
                        braw-decode.exe subprocess for .braw (proprietary codec)
     |
     v
 CK NEURAL KEYER        CorridorKeyModule, PyTorch + CUDA, CorridorKey.pth weights
     |                  outputs: alpha (float32) + an independent fg_decoder RGB estimate
     v
 SAM2 SUPPORT MASK      optional. image predictor = single-frame preview / anchor click
 (optional)             video predictor = propagation across a batch range
     |
     v
 MERGE / GARBAGE-MATTE  corridorkey_sam_merge.py: CK alpha x zoned/dilated SAM holdout,
                         plain multiply, chroma escape valve for hair. MERGE_MODE="garbage_matte"
     |
     v
 POST-PROC              apply_matte_postproc: despill (subtractive-only), despeckle, choke,
                         zone-cut, feather
     |
     v
 OUTPUT WRITE           PNG (8/16-bit) / TIFF 16-bit / EXR 32-bit sidecar files
                         Resolve: atomic (tmp + os.replace). AE: still plain cv2.imwrite (open gotcha)
     |
     v
 HOST TIMELINE IMPORT   Resolve API (Track 2) / AE comp (layer above source) /
                         Premiere (V2, or CK_3TRACK.sqpreset nest for the 3-track workflow)
```

---

## Secondary flows

**CUDA sandbox bridge (Adobe hosts only).** This is the mechanism that makes the primary flow's CK/SAM2 stages possible at all from inside AE/Premiere. See Part 04 for why it has to look like this.

```
 CEP PANEL (index.html, inside CEF sandbox, cannot init CUDA)
     |  JSON job over stdio
     v
 ck_send.py (Node-spawned Python, still inside the sandbox)
     |  newline-delimited JSON over loopback TCP (127.0.0.1), shared-secret auth
     v
 ck_broker.py  (Windows logon Scheduled Task, parent = Task Scheduler/svchost, NEVER sandboxed)
     |  subprocess
     v
 ck_engine_worker.py (warm, CNN-only jobs)   OR   fresh subprocess (any SAM2 command)
     |
     v
 ae_processor.py  -->  same CK / SAM2 / merge stages as the primary flow
```

**Premiere 3-track nest (workaround for the broken QE track-creation API).**

```
 Operator authors CK_3TRACK.sqpreset ONCE (a normal Premiere sequence preset)
     |
     v
 Panel instantiates new sequences FROM the preset  (never scripts QE addTracks at runtime)
     |
     v
 CK keyed clip / SAM matte / original clip land on their fixed preset tracks
```

**Preview vs render (must be the same path, per Part 01 Rule 5).** Historically these diverged (image predictor for preview, video predictor for render); the "Match Render" toggle forces the real video predictor into the preview path so what the operator judges is what will actually ship.

---

## Stack

| Layer | Choice | Role | Note |
|---|---|---|---|
| Neural keyer | CorridorKeyModule (PyTorch, CorridorKey.pth) | Chroma-independent neural keying, the primary matte source | Upstream: Niko Pueringer / Corridor Digital, CC-BY-NC-SA-4.0 |
| Support mask | SAM2 (facebookresearch/sam2, sam2.1_hiera_small.pt) | Click-to-mask holdout silhouette, image predictor (single frame) + video predictor (range propagation) | Optional; CK keys without it |
| Merge | corridorkey_sam_merge.py | Combines CK alpha with SAM holdout | MERGE_MODE constant: garbage_matte (live) / chroma_gated / path_b (hot-revert fallbacks) |
| Combine entry point | sam2_combine.py apply_sam2_gate | Single source of truth for SAM+NN combine across 6-7 call sites | Do not fork a second combine path |
| BRAW decode | braw-decode.exe (sibling repo) + BlackmagicRawAPI.dll | Reads proprietary .braw frames | Subprocess, raw BGRA stream, no frame separator |
| Resolve host | CorridorKey_Pro.py (Fusion UIManager, embedded Python) | Live panel: SAM2 clicks, BRAW/HEVC read, full-range render, Fusion comp build | Single top-level script, no main() |
| Resolve preview | preview_viewer_v2.py (PySide6) | Separate-process live-slider preview | Keeps CUDA and the Qt event loop apart |
| Adobe host | CEP panel: index.html + jsx/host.jsx | UI + ExtendScript timeline glue for AE and Premiere | Deployed via a Windows junction, not a copy |
| Adobe CUDA bridge | ck_broker.py + ck_send.py + ck_engine_worker.py | Escapes the CEF sandbox so CUDA can run | Scheduled Task, loopback socket, shared secret (Part 04, Part 06) |
| Deploy | deploy.py | Pushes source to live host folders, with backup/revert | Only safe deploy path (Part 07) |
| Install | install.py | First-time install into Resolve/AE/Premiere | Junction-aware for AE; stale for Resolve (Part 12) |

---

## Where the work happens

CK inference and SAM2 inference both run on GPU via PyTorch/CUDA; this is where essentially all processing time goes. The merge and post-proc stages run on CPU (numpy/cv2) and are comparatively instant. Both host UIs (the Fusion `UIManager` panel and the CEP `index.html` panel) are pure glue and orchestration; neither runs the model itself. Any "the panel is slow" complaint traces to the GPU stage or to the CUDA-bridge round trip, never to the JS/ExtendScript/Fusion-UI glue layer.
Tag: VERIFIED. Last verified: 2026-07-22.

---

## Repo and paths (check-against-live, see Part 12 for full drift table)

- Engine root and both plugin folders live in one repo: `D:\New AI Projects\CorridorKey` (contains `resolve_plugin/`, `ae_plugin/`, the shared engine modules, and `docs/`).
- AE/Premiere CEP install path is a junction: `%APPDATA%\Adobe\CEP\extensions\com.corridorkey.panel` -> `ae_plugin\cep_panel` (same physical files, not a copy).
- `braw-decode.exe` resolves from either `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\braw-decode.exe` or the sibling dev repo `D:\New AI Projects\braw-decode-win\bin\braw-decode.exe`.
- Current branch at last bible pass: `feat/mcp-server` (not `main`), commit `caff84f` ("nest polish"), with an uncommitted working tree carrying substantial further changes. Verify branch and commit before trusting any line-number reference in this bible.
Tag: STALE-REVERIFY. Last verified: 2026-07-22. Recheck when: before relying on any file:line reference in this bible for an edit.
