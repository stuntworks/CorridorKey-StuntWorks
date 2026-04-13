# CorridorKey StuntWorks

One-click AI green screen keying inside DaVinci Resolve, After Effects, and Premiere Pro.

**Powered by [CorridorKey](https://github.com/nikopueringer/CorridorKey) by Niko Pueringer / Corridor Digital.**
**Plugin by Roberto Lopez / [StuntWorks](https://www.youtube.com/@stuntworkscinema)**

---

## What It Does

- AI-powered green/blue screen removal with one click
- Works inside Resolve, After Effects, and Premiere Pro
- Preview keyed result before committing
- Batch process entire clips or work areas
- Adjustable despill, edge refinement, and despeckle
- Keyed files save to your project folder automatically

## Requirements

- [CorridorKey](https://github.com/nikopueringer/CorridorKey) installed with Python venv
- NVIDIA GPU with CUDA (8GB+ VRAM recommended)
- One or more of:
  - DaVinci Resolve Studio 18+
  - After Effects 2020+
  - Premiere Pro 2020+

---

## Install

```
git clone https://github.com/stuntworks/CorridorKey-StuntWorks.git
cd CorridorKey-StuntWorks
python install.py
```

Options:
- `python install.py --all` — install to all detected apps
- `python install.py --resolve` — Resolve only
- `python install.py --adobe` — AE + Premiere only
- `python install.py --uninstall` — remove from all apps

Set `CORRIDORKEY_ROOT` if your CorridorKey install isn't in a sibling directory.

---

## DaVinci Resolve

**Open:** Workspace > Scripts > CorridorKey

### Setup
1. Preferences > System > General > External scripting: **Local**
2. Restart Resolve

### How to Use
1. Put green screen footage on Track 1
2. Open CorridorKey panel
3. Pick screen type (green/blue), adjust despill and refiner
4. **SHOW PREVIEW** — see the key before committing
5. **PROCESS FRAME** — key one frame, adds to Track 2
6. **PROCESS ALL** — key entire clip, adds sequence to Track 2
7. Output saves to a **CorridorKey** folder next to your project

---

## After Effects

**Open:** Window > Extensions > CorridorKey

### How to Use
1. Select the green screen layer in your comp
2. Pick screen type, adjust despill and refiner
3. **PREVIEW FRAME** — see the keyed result in the panel (no changes to comp)
4. **PROCESS FRAME** — key current frame, imports above selected layer
5. **PROCESS WORK AREA** — key all frames in the work area (B/N keys to set range)
6. Output saves to a **CorridorKey** folder next to your project

### Notes
- Batch processing runs in one shot — AE will freeze until done, then come back
- Results import as a layer above your selected green screen layer

---

## Premiere Pro

**Open:** Window > Extensions > CorridorKey

### How to Use
1. Put green screen footage on V1
2. Move playhead to the frame you want
3. Pick screen type, adjust despill and refiner
4. **PREVIEW FRAME** — see the keyed result in the panel
5. **PROCESS FRAME** — key current frame, places on V2 at playhead
6. **PROCESS IN/OUT RANGE** — set I and O points, then batch key (not yet tested)
7. Output saves to a **CorridorKey** folder next to your project

### Options
- **Add keyed clip to timeline** — uncheck this if you have a complex timeline and want to drag clips from the bin manually
- **Output Folder** — defaults to project folder, click Browse to change

### Notes
- Keyed files also appear in a "CorridorKey" bin in your project panel
- You need at least 2 video tracks (V1 + V2) for auto-placement

---

## Support

If this saves you time:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/stuntworks)

## Credits

- **CorridorKey AI Engine** — [Niko Pueringer / Corridor Digital](https://github.com/nikopueringer/CorridorKey)
- **Plugin** — Roberto Lopez / [StuntWorks](https://www.youtube.com/@stuntworkscinema)

## License

This plugin is free and open source under the same terms as CorridorKey (CC-BY-NC-SA-4.0).
