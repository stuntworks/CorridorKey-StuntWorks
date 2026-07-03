"""Engine bridge for the CorridorKey MCP server.

Locates the CorridorKey engine repo, puts it on sys.path, and exposes the
engine class + chroma hint helper. Works with file paths — no torch tensors.

CorridorKey was created by Niko Pueringer / Corridor Digital (CC BY-NC-SA 4.0).
This package is the StuntWorks Cinema build. See README.md for credits + links.
"""
from __future__ import annotations

import os
import sys
import logging

import numpy as np

logger = logging.getLogger("corridorkey_sw")

# ── Engine discovery (priority order — same contract as the AE/ComfyUI hosts) ──
#   1. CORRIDORKEY_ROOT env var
#   2. corridorkey_path.txt next to this file
#   3. parent folder check (mcp_server/ lives inside the repo)
#   4. ~/CorridorKey home fallback
_HERE = os.path.dirname(os.path.abspath(__file__))


def _discover_root() -> str | None:
    env = os.environ.get("CORRIDORKEY_ROOT")
    if env and os.path.isdir(env):
        return env

    txt = os.path.join(_HERE, "corridorkey_path.txt")
    if os.path.isfile(txt):
        try:
            with open(txt, "r", encoding="utf-8") as fh:
                p = fh.read().strip()
            if p and os.path.isdir(p):
                return p
        except OSError:
            pass

    # mcp_server/ is a subfolder of the repo root
    parent = os.path.dirname(_HERE)
    if os.path.isdir(os.path.join(parent, "CorridorKeyModule")):
        return parent

    home = os.path.join(os.path.expanduser("~"), "CorridorKey")
    if os.path.isdir(home):
        return home

    return None


CORRIDORKEY_ROOT = _discover_root()
if CORRIDORKEY_ROOT and CORRIDORKEY_ROOT not in sys.path:
    sys.path.insert(0, CORRIDORKEY_ROOT)
    os.environ.setdefault("CORRIDORKEY_ROOT", CORRIDORKEY_ROOT)


def engine_available() -> bool:
    return CORRIDORKEY_ROOT is not None


def require_root() -> str:
    if CORRIDORKEY_ROOT is None:
        raise RuntimeError(
            "CorridorKey engine not found. Set CORRIDORKEY_ROOT env var to the "
            "CorridorKey repo, or drop a corridorkey_path.txt next to this file "
            "containing that path. (Needs the folder with CorridorKeyModule/ in it.)"
        )
    return CORRIDORKEY_ROOT


def get_engine_class():
    require_root()
    from CorridorKeyModule import CorridorKeyEngine  # noqa: WPS433 (lazy by design)
    return CorridorKeyEngine


def get_processor_path() -> str | None:
    """Return the absolute path to ae_processor.py under the discovered root."""
    root = _discover_root()
    if root is None:
        return None
    candidate = os.path.join(root, "ae_plugin", "cep_panel", "ae_processor.py")
    return candidate if os.path.isfile(candidate) else None


# ── Chroma alpha hint ───────────────────────────────────────────────
# Vendored verbatim from ae_plugin/cep_panel/ae_processor.py generate_chroma_hint
# (HSV detection that matches DaVinci's AlphaHintGenerator). Vendored, not imported,
# because importing ae_processor drags in AE-host-only code. numpy/cv2 only.
def generate_chroma_hint(image: np.ndarray, screen_type: str = "green") -> np.ndarray:
    import cv2

    if image.dtype != np.uint8:
        img_u8 = (np.clip(image, 0.0, 1.0) * 255.0).astype(np.uint8)
    else:
        img_u8 = image
    hsv = cv2.cvtColor(img_u8, cv2.COLOR_RGB2HSV)
    if screen_type == "blue":
        lower = np.array([100, 50, 50])
        upper = np.array([130, 255, 255])
    else:
        lower = np.array([35, 50, 50])
        upper = np.array([85, 255, 255])
    screen_mask = cv2.inRange(hsv, lower, upper)
    subject_mask = cv2.bitwise_not(screen_mask)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_CLOSE, kernel)
    subject_mask = cv2.morphologyEx(subject_mask, cv2.MORPH_OPEN, kernel)
    subject_mask = cv2.GaussianBlur(subject_mask, (5, 5), 0)
    return subject_mask.astype(np.float32) / 255.0
