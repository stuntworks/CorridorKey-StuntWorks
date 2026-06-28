"""Shared About / credits / links for the CorridorKey StuntWorks ComfyUI plugin.

Single source of truth — the About node and the web "About" panel both read
these so credits + links never drift. Wording mirrors the DaVinci/AE live-view
About dialog.
"""
from __future__ import annotations

PLUGIN_NAME = "CorridorKey StuntWorks"
PLUGIN_VERSION = "0.1.0"

# Links (kept here so the node, the web panel, and pyproject all agree)
LINK_ENGINE_GITHUB = "https://github.com/nikopueringer/CorridorKey"
LINK_CORRIDOR = "https://corridordigital.com"
LINK_CORRIDOR_YT = "https://www.youtube.com/@CorridorDigital"  # Niko / Corridor's channel
LINK_PLUGIN_GITHUB = "https://github.com/stuntworks/CorridorKey-Plugin"
LINK_YOUTUBE = "https://www.youtube.com/@StuntWorksCinema"
LINK_KOFI = "https://ko-fi.com/stuntworks"

# Engine license — upstream is CC BY-NC-SA 4.0 (NonCommercial). This build is free.
ENGINE_LICENSE = "CC BY-NC-SA 4.0 (NonCommercial) — free, cannot be sold"

ABOUT_TEXT = f"""\
CorridorKey — {PLUGIN_NAME} (v{PLUGIN_VERSION})
AI green screen keyer for ComfyUI

──────────────────────────────
CorridorKey Engine
Created by Niko Pueringer / Corridor Digital
{LINK_ENGINE_GITHUB}
{LINK_CORRIDOR}
Niko / Corridor on YouTube: {LINK_CORRIDOR_YT}
License: {ENGINE_LICENSE}

──────────────────────────────
ComfyUI Plugin
by Roberto Lopez & Elvis Lopez — StuntWorks Cinema
{LINK_PLUGIN_GITHUB}

──────────────────────────────
What makes this build unique
The green-aware garbage matte: CorridorKey's neural chroma key combined
with a SAM2 subject mask AND the green-screen geography. It cuts the set
cleaner than a raw subject mask and keeps the key locked to the subject on
every frame — the same clean-matte pipeline shipped in the DaVinci/AE tools.

──────────────────────────────
Open source credits
Subject mask is powered by SAM2 (Segment Anything Model 2) (c) Meta AI,
used under the Apache 2.0 license. Bring your own SAM2 mask via any
ComfyUI segmentation node.

──────────────────────────────
StuntWorks is a professional stunt rigging company. In our spare time we
build the tools we wish existed — free plugins and workflow helpers.
If you find this useful, a coffee helps us keep building.
Ko-fi: {LINK_KOFI}

──────────────────────────────
Watch the tutorials
Step-by-step video tutorials on StuntWorks Cinema:
{LINK_YOUTUBE}
"""
