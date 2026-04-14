<div align="center">

# CorridorKey StuntWorks

### AI Green Screen Removal for Your Editor

[![DaVinci Resolve](https://img.shields.io/badge/DaVinci_Resolve-18+-233A51?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMTAiIGZpbGw9IiNFMzQyM0QiLz48L3N2Zz4=)](https://www.blackmagicdesign.com/products/davinciresolve)
[![After Effects](https://img.shields.io/badge/After_Effects-2020+-9999FF?style=for-the-badge&logo=adobeaftereffects&logoColor=white)](https://www.adobe.com/products/aftereffects.html)
[![Premiere Pro](https://img.shields.io/badge/Premiere_Pro-2020+-9999FF?style=for-the-badge&logo=adobepremierepro&logoColor=white)](https://www.adobe.com/products/premiere.html)

[![Ko-fi](https://img.shields.io/badge/Buy_Me_A_Coffee-Support_Development-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white)](https://ko-fi.com/stuntworks)

[![License](https://img.shields.io/badge/License-CC--BY--NC--SA--4.0-green?style=flat-square)](LICENSE)
[![YouTube](https://img.shields.io/badge/YouTube-StuntWorks-FF0000?style=flat-square&logo=youtube&logoColor=white)](https://www.youtube.com/@stuntworkscinema)

---

**One-click neural keying powered by [CorridorKey](https://github.com/nikopueringer/CorridorKey)**
**by Niko Pueringer / Corridor Digital**

*Plugin by Roberto Lopez / [StuntWorks](https://www.youtube.com/@stuntworkscinema)*

</div>

---

## Before / After

| Before | Keyed | Composite |
|:---:|:---:|:---:|
| ![Before](docs/ae_before.png) | ![Keyed](docs/ae_keyed.png) | ![Composite](docs/ae_composite.png) |

---

## Screenshots

| DaVinci Resolve | Premiere Pro |
|:---:|:---:|
| ![Resolve](docs/resolve_screenshot.png) | ![Premiere](docs/premiere_screenshot.png) |

---

## What It Does

> Drop green screen footage in your editor. Click one button. Get a clean key.

- AI-powered green/blue screen removal — no manual color picking
- Works inside **Resolve**, **After Effects**, and **Premiere Pro**
- Preview the key before committing to your timeline
- Batch process entire clips or frame ranges
- Adjustable despill, edge refinement, and despeckle
- Output saves to your project folder automatically

---

## Requirements

| Requirement | Details |
|---|---|
| **CorridorKey Engine** | [Install from GitHub](https://github.com/nikopueringer/CorridorKey) with Python venv |
| **GPU** | NVIDIA with CUDA, 8GB+ VRAM recommended |
| **Editor** | Resolve Studio 18+, After Effects 2020+, or Premiere Pro 2020+ |

---

## Install

```bash
git clone https://github.com/stuntworks/CorridorKey-StuntWorks.git
cd CorridorKey-StuntWorks
python install.py
```

| Flag | What it does |
|---|---|
| `--all` | Install to all detected apps |
| `--resolve` | Resolve only |
| `--adobe` | AE + Premiere only |
| `--uninstall` | Remove from all apps |

> Set `CORRIDORKEY_ROOT` environment variable if your CorridorKey install isn't in a sibling directory.

---

<details>
<summary><h2>DaVinci Resolve</h2></summary>

**Open:** `Workspace > Scripts > CorridorKey`

### Setup
1. Preferences > System > General > External scripting: **Local**
2. Restart Resolve

### How to Use

| Step | Action |
|:---:|---|
| 1 | Put green screen footage on **Track 1** |
| 2 | Open the CorridorKey panel |
| 3 | Pick screen type, adjust despill and refiner |
| 4 | **SHOW PREVIEW** — check the key quality |
| 5 | **PROCESS FRAME** — key one frame, adds to Track 2 |
| 6 | **PROCESS ALL** — key entire clip, sequence on Track 2 |

Output saves to a `CorridorKey` folder next to your project.

</details>

---

<details>
<summary><h2>After Effects</h2></summary>

**Open:** `Window > Extensions > CorridorKey`

### How to Use

| Step | Action |
|:---:|---|
| 1 | Select the green screen **layer** in your comp |
| 2 | Pick screen type, adjust despill and refiner |
| 3 | **PREVIEW FRAME** — see result in panel (no comp changes) |
| 4 | **PROCESS FRAME** — key current frame, imports above your layer |
| 5 | **PROCESS WORK AREA** — key all frames in work area (B/N to set range) |

Output saves to a `CorridorKey` folder next to your project.

> **Note:** Batch processing runs in one shot — AE will freeze while processing, then come back with all frames ready.

</details>

---

<details>
<summary><h2>Premiere Pro</h2></summary>

**Open:** `Window > Extensions > CorridorKey`

### How to Use

| Step | Action |
|:---:|---|
| 1 | Put green screen footage on **V1** |
| 2 | Move playhead to the frame you want |
| 3 | Pick screen type, adjust despill and refiner |
| 4 | **PREVIEW FRAME** — see result in panel |
| 5 | **PROCESS FRAME** — key current frame, places on V2 |
| 6 | **PROCESS IN/OUT RANGE** — set I/O points, batch key all frames |

Output saves to a `CorridorKey` folder next to your project.

### Options

| Option | What it does |
|---|---|
| **Add keyed clip to timeline** | Uncheck for complex timelines — files go to bin only |
| **Output Folder** | Defaults to project folder. Click Browse to change. |

> **Note:** Keyed files appear in a "CorridorKey" bin in your project panel. You need V1 + V2 tracks for auto-placement.

</details>

---

## For Developers

Editing this plugin? Read these first, in order:

1. **[ALIGNMENT.md](./ALIGNMENT.md)** — canonical reference for Premiere Pro frame
   alignment. Before touching `ppro_getFrameInfo`, `ppro_importFrame`, or any
   batch frame math, read this. Same alignment was broken and re-fixed four
   times in three days; the doc exists so it stops happening. Includes a
   mandatory pre-commit smoke test.
2. **[CLAUDE.md](./CLAUDE.md)** — entry point for AI coding assistants working
   on this repo.
3. **[INSTALL.md](./INSTALL.md)** — full install walkthrough for end users.
4. **[CODE_REVIEW_2026-04-14.md](./CODE_REVIEW_2026-04-14.md)** — latest
   security + quality audit (all Critical and High items are addressed).

Rebuild the engine venv: `setup.bat` (Windows) or `./setup.sh` (macOS / Linux).

---

<div align="center">

## Support

If this saves you time, consider buying me a coffee: https://ko-fi.com/stuntworks

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/stuntworks)

---

### Credits

**CorridorKey AI Engine** — [Niko Pueringer / Corridor Digital](https://github.com/nikopueringer/CorridorKey)

**Plugin** — Roberto Lopez / [StuntWorks](https://www.youtube.com/@stuntworkscinema)

---

*Free and open source under [CC-BY-NC-SA-4.0](LICENSE)*

</div>
