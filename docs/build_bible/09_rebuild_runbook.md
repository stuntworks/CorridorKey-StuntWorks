# Part 09. Rebuild Runbook

**Scope: reconstructing CorridorKey (StuntWorks build) from a clean machine. Load this only when rebuilding from zero. Steps marked UNKNOWN have no confirmed evidence in the ledgers or repo; do not invent detail for them, verify against a live machine instead.**

---

## 1. Pre-flight

Confirm before starting, stop and report if any is missing:
- Windows 10/11 or macOS 12+ (Windows is the primary developed-and-tested platform; Mac paths are documented but exercised less).
- Python 3.10-3.12 (3.12 recommended; 3.13 untested).
- Git, and Git LFS if the model weights are pulled via LFS.
- NVIDIA GPU with 4GB+ VRAM and a matching CUDA-capable driver (CUDA 11.8 / 12.1 / 12.4); CPU-only will technically run but is unusably slow per README's own hardware table.
- DaVinci Resolve 18.5+ and/or Adobe After Effects / Premiere Pro 2022+ (22.0+), whichever hosts are targeted.
- Roughly 15GB free disk (PyTorch wheels alone run 3-5GB).
Tag: VERIFIED. Last verified: 2026-07-22 (INSTALL.md section 2).

## 2. Environment

```
git clone <the engine + plugin repo, currently one combined repo> "D:\New AI Projects\CorridorKey"
cd "D:\New AI Projects\CorridorKey"
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip wheel
pip install -r requirements-gpu.txt --index-url https://download.pytorch.org/whl/cu124   # match your CUDA version
pip install -r requirements.txt
python -c "import torch, cv2, numpy, PIL, timm; print(torch.__version__, torch.cuda.is_available())"
```
Confirm `cuda True` before proceeding. If `False` on a machine with an NVIDIA card, the CPU wheel was installed by mistake; redo the `--index-url` step.
Tag: VERIFIED. Last verified: 2026-07-22 (INSTALL.md sections 2, 4; setup.bat/setup.sh).

## 3. Core assets

- `CorridorKey.pth` (the CK neural keyer weights) must exist under the engine's checkpoints path; the plugin fails to start entirely without it.
- SAM2 (optional): `pip install git+https://github.com/facebookresearch/sam2.git`, then `sam2_weights/sam2.1_hiera_small.pt` downloaded from Meta's SAM2 release page (or via `setup.bat`/`setup.sh`'s automatic download).
- `braw-decode.exe`: build or obtain from the sibling `braw-decode-win` repo if BRAW footage will ever be used; place at `braw-decode-win/bin/braw-decode.exe` or the ProgramData Fusion Scripts Utility path. Requires `BlackmagicRawAPI.dll`, which ships with DaVinci Resolve or Blackmagic's own installer; obtaining and legally redistributing this DLL on a non-Resolve machine is UNKNOWN (Part 13), do not assume it is bundleable.
Tag: VERIFIED (CK.pth, SAM2 weights). UNKNOWN (braw-decode.exe redistribution terms). Last verified: 2026-07-22.

## 4. Core flow

Before touching any host integration, confirm the bare engine works: run the CK keyer directly on one test frame (no SAM2, no merge, no host) and confirm an alpha matte comes out. This is the cheapest possible round trip and isolates "the engine itself is broken" from every host-integration problem in the steps below.
Tag: HYPOTHESIS (a reasonable minimal smoke test; no single command for this was found named as such in the ledgers). Last verified: 2026-07-22.

## 5. The hard part, early

Set up and test the CUDA sandbox bridge before wiring the rest of the Adobe integration, because every Adobe-side CUDA job depends on it:
- Register `ck_broker.py` as a Windows per-user logon Scheduled Task via `install_broker_task.ps1` (run from a normal PowerShell, not from inside AE).
- Confirm `ck_broker_config.json` exists with a host/port/secret (auto-generated on first broker run).
- Confirm the broker responds to a ping over the loopback socket, then confirm one real CUDA job (a single-frame key) round-trips successfully through it.
- Only after the broker is proven working, wire up the CEP panel's `ck_send.py` calls and confirm the same job works when triggered from inside AE.
If BRAW footage is in scope, also confirm `braw-decode.exe` decodes a real `.braw` frame and that the Rec.709 color-science override produces a keyable (not flat/desaturated) result, before assuming the BRAW path works end to end.
If Premiere's 3-track workflow is in scope, author `CK_3TRACK.sqpreset` once, in a normal Premiere session, and confirm the panel can instantiate a sequence from it, before assuming multi-track output works.
Tag: VERIFIED (the broker ping/job round trip and the sqpreset workflow are both proven, Part 04). Last verified: 2026-07-22.

## 6. Secondary flows

Confirm the Resolve-side live preview (`preview_viewer_v2.py`, a separate PySide6 process) launches and updates on slider drag without freezing the host. Confirm the SAM2 image predictor (single-frame) and video predictor (range propagation) both produce masks, feeding native unpadded frame shape to the video predictor specifically (Part 04, Part 08). Confirm "Match Render" preview mode actually invokes the video predictor, not the image predictor, if this toggle is present in the version being rebuilt.
Tag: VERIFIED. Last verified: 2026-07-22.

## 7. Interfaces

Wire up the control surface from Part 05: screen-type picker, despill/refiner sliders, PROCESS/KEY buttons, SAM2 left/right-click dot placement, Tab for MASK 1/MASK 2, CLEAR. Confirm the operator feedback paths work: the busy strip, and the `%TEMP%\corridorkey.log` / `corridorkey_error.txt` error bridges. Remember the uncertainty-signal gap noted in Part 05: there is no confidence indicator to wire up, because none exists yet.
Tag: VERIFIED. Last verified: 2026-07-22.

## 8. State and licensing

Confirm `corridorkey_path.txt` / `CORRIDORKEY_ROOT` correctly resolves the engine root and that the resolution logic validates real repo markers rather than trusting the path blindly (Part 08 gotcha). Confirm `ck_broker_config.json`'s secret is freshly machine-generated, not copied from another machine or from any document (it must never appear in one). There is no license key or trial mechanism to configure; CC-BY-NC-SA-4.0 governs distribution, not a runtime check (Part 06).
Tag: VERIFIED. Last verified: 2026-07-22.

## 9. Package

There is no freeze/compile step. "Packaging" is `deploy.py` (day-to-day, pushes source to each host's live folder with a timestamped backup) for an existing install, or `install.py` (first-time setup: copies the Resolve plugin, creates the AE/Premiere CEP junction, writes `corridorkey_path.txt`) for a clean machine. Be aware `install.py`'s Resolve installer currently deploys the superseded `resolve_corridorkey.py` / `core` / `ui` legacy plugin, not the live `CorridorKey_Pro.py` (Part 07, Part 12); manually confirm which generation actually needs to end up live before trusting the installer's Resolve output.
Tag: VERIFIED. Last verified: 2026-07-22.

## 10. Audit

Run `deploy.py --map` to print the deploy map without writing, and confirm every path in it matches what each host actually auto-discovers (Fusion `Scripts/Utility`, the AE CEP extensions folder). Confirm `origin/main` was freshly fetched, not a stale local ref, before basing any release-prep work on it (Part 07, Part 08). A formal license audit of `requirements*.txt` against CC-BY-NC-SA-4.0 has not been confirmed run as of this pass; treat that as an outstanding step, not a completed one (Part 13).
Tag: PARTIALLY VERIFIED. Last verified: 2026-07-22.

## 11. Sign / finalize

There is no code-signing certificate in this pipeline. The panel is unsigned; distribution relies on Adobe's `PlayerDebugMode` flag (`install.py --allow-unsigned`, or the registry/`defaults write` steps in INSTALL.md's troubleshooting section) to let an unsigned CEP extension load at all. This is a stated, deliberate tradeoff, not a step to "fix" during a rebuild unless a signed panel is explicitly the goal.
Tag: VERIFIED. Last verified: 2026-07-22.

## 12. Smoke test the shipped artifact

Not the dev tree: restart the actual host application (Resolve / AE / Premiere) after every deploy, since CEP caches `index.html` and AE only reloads `host.jsx` on a full quit-and-relaunch. Then, per README's own first-run smoke test:
- Resolve: `Workspace > Scripts > CorridorKey` opens the panel; key one frame, then process a short range, on real green-screen footage.
- After Effects: `Window > Extensions > CorridorKey`, select a layer, KEY CURRENT FRAME, confirm a keyed PNG imports above the layer.
- Premiere: `Window > Extensions > CorridorKey`, park the playhead on green-screen footage, KEY CURRENT FRAME, confirm the keyed frame lands on V2 exactly one frame wide.
Run the mandatory ALIGNMENT.md pre-commit smoke test (Tests A through D) if any Premiere-side frame code was touched during the rebuild, before considering the Premiere path done.
Tag: VERIFIED. Last verified: 2026-07-22 (README.md section 8, ALIGNMENT.md).
