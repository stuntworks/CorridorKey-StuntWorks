# CorridorKey Plugin

One-click AI green screen keying inside DaVinci Resolve and After Effects.

**Powered by [CorridorKey](https://github.com/nikopueringer/CorridorKey) by Niko Pueringer / Corridor Digital.**

This plugin wraps the CorridorKey neural keyer into native panels for DaVinci Resolve Studio and Adobe After Effects, so editors can use AI-powered green screen removal without touching the command line.

## What It Does

- AI-powered green/blue screen keying with one click
- Preview before committing to timeline
- Batch process entire clips
- Auto-import results to MediaPool and timeline
- Adjustable despill, edge refinement, and despeckle controls

## Requirements

- [CorridorKey](https://github.com/nikopueringer/CorridorKey) installed with Python venv
- NVIDIA GPU with CUDA support (4GB+ VRAM)
- DaVinci Resolve Studio 18+ (free version lacks scripting API) and/or Adobe After Effects 2020+

## DaVinci Resolve Installation

1. Clone this repo
2. Install CorridorKey following their instructions
3. Run the installer:
   ```
   cd resolve_plugin
   python install.py
   ```
4. Enable scripting in Resolve: Preferences > System > General > External scripting using: Local
5. Restart Resolve
6. Access via: Workspace > Scripts > CorridorKey

## Usage

1. Place green screen footage on Track 1
2. Open CorridorKey panel (Workspace > Scripts > CorridorKey)
3. Adjust settings (screen type, despill, refiner)
4. Move playhead to a frame and click **SHOW PREVIEW** to check quality
5. Click **PROCESS FRAME** for single frame or **PROCESS ALL** for the full clip
6. Keyed result appears on Track 2 with transparency

## After Effects (Coming Soon)

Two integration methods are in development:
- **CEP Panel** - HTML extension panel
- **Golobulus Effect** - Native AE effect plugin

## Credits

- **CorridorKey AI Engine** - [Niko Pueringer / Corridor Digital](https://github.com/nikopueringer/CorridorKey)
- **Plugin Integration** - [Berto Labs / StuntWorks](https://github.com/stuntworks)

## License

This plugin is free and open source. The CorridorKey engine is subject to its own license terms.
