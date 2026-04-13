# CorridorKey StuntWorks

One-click AI green screen keying inside DaVinci Resolve, After Effects, and Premiere Pro.

**Powered by [CorridorKey](https://github.com/nikopueringer/CorridorKey) by Niko Pueringer / Corridor Digital.**
**Plugin by [Berto Labs / StuntWorks](https://github.com/stuntworks)**

Multi-editor plugin that wraps the CorridorKey neural keyer into native panels for DaVinci Resolve, After Effects, and Premiere Pro. One installer, all your editors.

## What It Does

- AI-powered green/blue screen keying with one click
- Preview before committing to timeline
- Batch process entire clips or work areas
- Auto-import results to MediaPool / project
- Adjustable despill, edge refinement, and despeckle controls

## Requirements

- [CorridorKey](https://github.com/nikopueringer/CorridorKey) installed with Python venv (use the latest version for float16 and GPU-batched processing — runs on 8GB+ VRAM now)
- NVIDIA GPU with CUDA support (8GB+ VRAM recommended)
- One or more of:
  - DaVinci Resolve Studio 18+ (free version lacks scripting API)
  - Adobe After Effects 2020+
  - Adobe Premiere Pro 2020+

## Installation

The unified installer detects your apps and installs to all of them:

```
git clone https://github.com/stuntworks/CorridorKey-StuntWorks.git
cd CorridorKey-StuntWorks
python install.py          # Interactive — detects and asks
python install.py --all    # Install to all detected apps
python install.py --resolve  # Resolve only
python install.py --adobe    # AE + Premiere only
python install.py --uninstall
```

The installer will:
- Copy plugin files to the correct locations
- Enable unsigned CEP extensions for Adobe apps (registry/defaults)
- Point the plugin to your CorridorKey engine

Set `CORRIDORKEY_ROOT` environment variable if your CorridorKey install isn't in a sibling directory.

## DaVinci Resolve

1. Enable scripting: Preferences > System > General > External scripting using: **Local**
2. Restart Resolve
3. Access via: **Workspace > Scripts > CorridorKey**

### Usage
1. Place green screen footage on Track 1
2. Open CorridorKey panel
3. Adjust settings (screen type, despill, refiner)
4. Click **SHOW PREVIEW** to check quality
5. Click **PROCESS FRAME** or **PROCESS ALL** for the full clip
6. Keyed result appears on Track 2 with transparency

## After Effects / Premiere Pro (Experimental)

1. Restart AE or Premiere after running the installer
2. Access via: **Window > Extensions > CorridorKey**

### Usage
1. Select a layer (AE) or have footage on Track 1 (Premiere)
2. Choose screen type, adjust despill and refiner
3. Click **PREVIEW FRAME**, **PROCESS FRAME**, or **PROCESS WORK AREA**
4. Keyed result imports to your project automatically

> **Note:** AE/Premiere support is experimental. The panel and processor are built but still being tested. Issues and PRs welcome.

## Support

If this plugin saves you time, consider buying me a coffee:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/stuntworks)

## Credits

- **CorridorKey AI Engine** - [Niko Pueringer / Corridor Digital](https://github.com/nikopueringer/CorridorKey)
- **Plugin Integration** - [Berto Labs / StuntWorks](https://github.com/stuntworks)

## License

This plugin is free and open source. The CorridorKey engine is subject to its own license terms.
