# CorridorKey Plugin — Full Installation Guide

**Last modified:** 2026-04-14
**Applies to:** CorridorKey Plugin for DaVinci Resolve, After Effects, and Premiere Pro.

This is the complete install reference. If you are installing for the first time, follow every section in order. If something breaks, check the **Troubleshooting** section at the bottom.

---

## 1. What You Are Installing

Three things cooperate to make this plugin run:

| Component | What it is | Where it lives | How to get it |
|-----------|------------|----------------|---------------|
| **CorridorKey engine** | Niko Pueringer's neural network (the actual keyer) | Any folder you choose, e.g. `D:\CorridorKey` | Separate download — see section 3 |
| **This plugin** | The bridge between your NLE and the engine | Installed into Resolve / Adobe Scripts folders | Clone this repo, run the installer |
| **The engine's Python venv** | The Python environment with PyTorch and friends | `<engine folder>\.venv` | Built in section 4 |

The plugin finds the engine at runtime by (in order):
1. The `CORRIDORKEY_ROOT` environment variable
2. A `corridorkey_path.txt` file next to the launcher
3. A sibling `CorridorKey/` folder
4. `D:\New AI Projects\CorridorKey` (legacy fallback)
5. `~/CorridorKey`

---

## 2. Prerequisites

| Thing | Required | Notes |
|-------|----------|-------|
| **Windows 10 / 11** or **macOS 12+** | Yes | Plugin is developed primarily on Windows. Mac paths documented but test less frequently. |
| **Python 3.10, 3.11, or 3.12** | Yes | 3.12 recommended. 3.13 may work but is untested. [python.org/downloads](https://www.python.org/downloads/) |
| **Git** | Yes | For cloning this repo + SAM2. [git-scm.com](https://git-scm.com/) |
| **NVIDIA GPU, 4 GB+ VRAM** | Strongly recommended | The model runs on CPU but is unusably slow. |
| **NVIDIA driver + CUDA 11.8 / 12.1 / 12.4** | Required for GPU | Check with `nvidia-smi` in a terminal. |
| **DaVinci Resolve 18.5+** | If using Resolve | Free edition works. "External scripting = Local" must be on. |
| **After Effects 2022+ (22.0+)** | If using AE | Check `Help > About`. |
| **Premiere Pro 2022+ (22.0+)** | If using Premiere | Check `Help > About`. |
| **~15 GB disk** | Yes | PyTorch wheels alone are 3-5 GB. |

---

## 3. Download the CorridorKey Engine

The plugin does not contain the neural network. Get it separately:

```
git clone https://github.com/cnikiforov/CorridorKey.git D:\CorridorKey
```

*(or any folder path you prefer — just remember it for step 4)*

You also need the model weights. Depending on the CorridorKey engine version, the weight file(s) live in:

- `<engine>\CorridorKeyModule\checkpoints\CorridorKey.pth` — main keyer model.
- `<engine>\sam2_weights\sam2.1_hiera_small.pt` — optional, for SAM2 click-to-mask.

If the engine repo does not include weights, follow the engine's own README to download them. **The plugin will fail to start if `CorridorKey.pth` is missing** — you will see "CorridorKey engine not found" or a PyTorch load error.

---

## 4. Build the Engine's Python Environment

Open a terminal inside the engine folder.

### Windows

```bat
cd D:\CorridorKey
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip wheel

REM Step 1 — install PyTorch with CUDA (replace cu124 with your CUDA version):
pip install -r "D:\New AI Projects\CorridorKey-Plugin\requirements-gpu.txt" --index-url https://download.pytorch.org/whl/cu124

REM Step 2 — install the rest:
pip install -r "D:\New AI Projects\CorridorKey-Plugin\requirements.txt"
```

### macOS / Linux

```bash
cd ~/CorridorKey
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel

# macOS has no CUDA — just:
pip install -r ~/CorridorKey-Plugin/requirements-gpu.txt
pip install -r ~/CorridorKey-Plugin/requirements.txt
```

### Verify the Install

```
python -c "import torch, cv2, numpy, PIL, timm; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
```

You should see something like `torch 2.4.1+cu124 cuda True`. If `cuda` is `False` on a machine with an NVIDIA card, you installed the CPU wheel — go back and redo step 1 with the correct `--index-url`.

---

## 5. (Optional) Enable SAM2 Click-to-Mask

SAM2 is only needed if you want to click-to-define the subject. The plugin keys without it using the automatic alpha hint generator.

```bat
cd D:\CorridorKey
.venv\Scripts\activate
pip install git+https://github.com/facebookresearch/sam2.git
```

Then download `sam2.1_hiera_small.pt` from Meta's SAM2 release page into `<engine>\sam2_weights\`.

---

## 6. (Optional) Enable BiRefNet Alpha Pre-Pass

Only needed if you want to run the offline script `generate_birefnet_alphas.py`. Skip otherwise.

```bat
cd D:\CorridorKey
.venv\Scripts\activate
pip install git+https://github.com/ZhengPeng7/BiRefNet.git
```

---

## 7. Install the Plugin into Resolve / AE / Premiere

```
git clone https://github.com/stuntworks/CorridorKey-StuntWorks.git "D:\New AI Projects\CorridorKey-Plugin"
cd "D:\New AI Projects\CorridorKey-Plugin"
python install.py
```

The installer:

- Copies the Resolve plugin into `<Resolve Scripts>\Utility\CorridorKey\`.
- Installs the CEP panel for AE and Premiere into `%APPDATA%\Adobe\CEP\extensions\com.corridorkey.panel\`.
- Writes `corridorkey_path.txt` next to the installed files pointing at your engine folder.
- Enables Adobe's `PlayerDebugMode` so unsigned CEP extensions can load (see the "Security" notes section for what this implies — we plan to replace this with a signed panel).

To uninstall: `python install.py --uninstall`.

---

## 8. First Run Smoke Test

### Resolve

1. Restart DaVinci Resolve.
2. Open any project with green-screen footage.
3. `Workspace > Scripts > CorridorKey`.
4. You should see the CorridorKey panel. If nothing appears, check `%TEMP%\corridorkey_error.txt` for a traceback.

### After Effects

1. Restart AE.
2. `Window > Extensions > CorridorKey`.
3. Select a layer, click **Key Current Frame**.
4. A keyed PNG should appear above the selected layer within a few seconds.

### Premiere Pro

1. Restart Premiere.
2. `Window > Extensions > CorridorKey`.
3. Park the playhead on green-screen footage and click **Key Current Frame**.
4. The keyed frame is placed on V2 at the playhead, one frame long.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `CorridorKey engine not found. Tried: ...` | Plugin can't find the engine folder | Set the `CORRIDORKEY_ROOT` environment variable to the engine path, OR edit `corridorkey_path.txt` next to the installed plugin. |
| `ModuleNotFoundError: No module named 'torch'` | Plugin found the engine but its `.venv` is empty | Go back to section 4. The venv exists but is not populated. |
| GPU not detected / processing takes minutes per frame | Installed the CPU wheel of PyTorch | Reinstall with the correct `--index-url` matching your CUDA version (see `requirements-gpu.txt`). |
| AE / Premiere panel is blank white | CEP debug mode not enabled | Re-run `python install.py` — it toggles the registry / defaults for you. |
| `No clip at playhead` in Premiere | Timeline frame rate quirk | Park playhead directly on the clip, not a gap. Known issue on variable-frame-rate source. |
| CEP panel shows "extension is unsigned" and won't load | You are on an Adobe version newer than what the manifest declares | Edit `<plugin>\ae_plugin\cep_panel\CSXS\manifest.xml` and widen the `HostList` range. |
| Error trace — where does it go? | The plugin writes tracebacks to `%TEMP%\corridorkey_error.txt` (Windows) or `$TMPDIR/corridorkey_error.txt` (macOS) | Open that file. The error stack tells you what import or call failed. |

---

## 10. Uninstall

```
cd "D:\New AI Projects\CorridorKey-Plugin"
python install.py --uninstall
```

This removes:

- The Resolve Utility/CorridorKey folder and launcher
- The CEP panel from `%APPDATA%\Adobe\CEP\extensions\`
- The `corridorkey_path.txt` config file

It does **not** delete the engine, the venv, the model weights, or any rendered output. Those are yours to keep or remove manually.

---

## 11. Keeping the Install Up to Date

```
cd "D:\New AI Projects\CorridorKey-Plugin"
git pull
python install.py        # re-runs copy + config, safe to run repeatedly
```

If `requirements.txt` changed, re-activate the engine venv and run:

```
pip install -r requirements.txt --upgrade
```
