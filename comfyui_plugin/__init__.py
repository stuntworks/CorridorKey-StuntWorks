"""CorridorKey StuntWorks — ComfyUI custom node package.

AI green screen keyer with the green-aware garbage matte (clean key + clean junk),
the same shared pipeline as the StuntWorks DaVinci/AE tools.

CorridorKey engine (c) Niko Pueringer / Corridor Digital — CC BY-NC-SA 4.0.
ComfyUI plugin by Roberto & Elvis Lopez / StuntWorks Cinema.
YouTube: https://www.youtube.com/@StuntWorksCinema
"""
from .nodes import (
    CorridorKeySWLoader,
    CorridorKeySWKeyer,
    CorridorKeySWGarbageMerge,
    CorridorKeySWAbout,
)

NODE_CLASS_MAPPINGS = {
    "CorridorKeySWLoader": CorridorKeySWLoader,
    "CorridorKeySWKeyer": CorridorKeySWKeyer,
    "CorridorKeySWGarbageMerge": CorridorKeySWGarbageMerge,
    "CorridorKeySWAbout": CorridorKeySWAbout,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CorridorKeySWLoader": "CorridorKey Loader (StuntWorks)",
    "CorridorKeySWKeyer": "CorridorKey Keyer (StuntWorks)",
    "CorridorKeySWGarbageMerge": "CorridorKey Garbage Merge (StuntWorks)",
    "CorridorKeySWAbout": "CorridorKey About (StuntWorks)",
}

# Serve the web/ folder so the "About CorridorKey" menu button + panel load.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
