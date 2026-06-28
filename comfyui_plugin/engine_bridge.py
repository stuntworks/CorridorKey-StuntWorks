"""Engine bridge for the CorridorKey StuntWorks ComfyUI nodes.

Locates the CorridorKey engine repo, puts it on sys.path, and exposes the
shared engine + merge functions plus tensor <-> numpy helpers. Keeping all
the path/discovery/import ugliness here keeps nodes.py clean.

CorridorKey was created by Niko Pueringer / Corridor Digital (CC BY-NC-SA 4.0).
This package is the StuntWorks Cinema build. See README.md for credits + links.
"""
from __future__ import annotations

import os
import sys
import logging

import numpy as np

logger = logging.getLogger("corridorkey_sw")

# ── Engine discovery (priority order — same contract as the AE/Resolve hosts) ──
#   1. CORRIDORKEY_ROOT env var
#   2. corridorkey_path.txt next to this file
#   3. hardcoded dev fallback  (REMOVE before any public release)
#   4. ~/CorridorKey
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEV_FALLBACK = r"D:\New AI Projects\CorridorKey"  # TODO: strip before public ship


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

    # The node may itself live inside the repo (comfyui_plugin/ subfolder).
    parent = os.path.dirname(_HERE)
    if os.path.isdir(os.path.join(parent, "CorridorKeyModule")):
        return parent

    if os.path.isdir(os.path.join(_DEV_FALLBACK, "CorridorKeyModule")):
        return _DEV_FALLBACK

    home = os.path.join(os.path.expanduser("~"), "CorridorKey")
    if os.path.isdir(home):
        return home

    return None


CORRIDORKEY_ROOT = _discover_root()
if CORRIDORKEY_ROOT and CORRIDORKEY_ROOT not in sys.path:
    # repo root holds corridorkey_sam_merge.py; CorridorKeyModule is a package under it
    sys.path.insert(0, CORRIDORKEY_ROOT)
    os.environ.setdefault("CORRIDORKEY_ROOT", CORRIDORKEY_ROOT)


def engine_available() -> bool:
    return CORRIDORKEY_ROOT is not None


def require_root() -> str:
    if CORRIDORKEY_ROOT is None:
        raise RuntimeError(
            "CorridorKey engine not found. Set CORRIDORKEY_ROOT env var to the "
            "CorridorKey repo, or drop a corridorkey_path.txt next to this node "
            "containing that path. (Needs the folder with CorridorKeyModule/ in it.)"
        )
    return CORRIDORKEY_ROOT


# ── Lazy engine import (torch load is 40-60s; never at module import time) ──
def get_engine_class():
    require_root()
    from CorridorKeyModule import CorridorKeyEngine  # noqa: WPS433 (lazy by design)
    return CorridorKeyEngine


def get_merge_fns():
    """Return the SHARED merge functions — same code the DaVinci/AE hosts call.
    No reimplementation: this is the whole point, identical clean key + green-aware
    garbage matte across every host."""
    require_root()
    from corridorkey_sam_merge import (  # noqa: WPS433
        merge_ck_with_garbage_matte,
        solidify_sam_silhouette,
    )
    return merge_ck_with_garbage_matte, solidify_sam_silhouette


def get_matte_toolkit():
    """The full shared matte toolkit the DaVinci host uses, so the ComfyUI
    Garbage Merge node mirrors DaVinci exactly: green-aware merge dispatcher,
    SAM margin/soften (process_sam_matte), and the LOOSE dilated holdout
    (compute_garbage_matte) that gives the subject room AND zeroes the
    off-green satellite speckles outside the holdout."""
    require_root()
    from corridorkey_sam_merge import (  # noqa: WPS433
        merge_ck_with_sam_active,
        process_sam_matte,
        compute_garbage_matte,
        solidify_sam_silhouette,
    )
    return {
        "merge_active": merge_ck_with_sam_active,
        "process_sam_matte": process_sam_matte,
        "compute_garbage_matte": compute_garbage_matte,
        "solidify": solidify_sam_silhouette,
    }


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


# ── Matte-quality steps vendored from ae_plugin/cep_panel/ae_processor.py ──
# Hair-safe junk handling the AE/DaVinci hosts have but the ComfyUI node lacked.
# Vendored (not imported) for the same reason as generate_chroma_hint: importing
# ae_processor drags in AE-host-only code. solidify_sam_silhouette is pulled from
# the SHARED merge module (same code the hosts use). numpy/cv2/scipy only.
def fill_body_holes(alpha: np.ndarray, sam_soft: np.ndarray) -> np.ndarray:
    """Fill enclosed interior holes in the CK matte that fall INSIDE the solid SAM
    body. Only fully-enclosed holes inside the body go to 1.0 — the soft outer hair
    edge is never touched, so hair survives. Verbatim from AE _fill_body_holes."""
    try:
        import scipy.ndimage as _snd
        import cv2
        from corridorkey_sam_merge import solidify_sam_silhouette as _solid

        a = np.asarray(alpha, dtype=np.float32)
        if a.ndim == 3:
            a = a[..., 0]
        h, w = a.shape
        solid = _solid(sam_soft).astype(np.uint8)
        if solid.shape[:2] != (h, w):
            solid = cv2.resize(solid, (w, h), interpolation=cv2.INTER_NEAREST)
        a_bin = a > 0.5
        enclosed = _snd.binary_fill_holes(a_bin) & ~a_bin
        fix = enclosed & (solid > 0)
        if fix.any():
            a = a.copy()
            a[fix] = 1.0
        return a
    except Exception as exc:  # noqa: BLE001 — never let post-proc kill the key
        logger.warning("fill_body_holes skipped: %s", exc)
        return alpha


def kill_loose_junk_keepmask(alpha: np.ndarray, sam_soft: np.ndarray,
                             frame_w: int, junk_thick_px: int = 8):
    """Hair-SAFE junk killer — core of AE apply_recipe_composite (zone-cut and
    auto-offset dropped; those serve AE's user-drawn zones). Two stages:
      1. Fat kill: morphological OPEN on (alpha-solid AND outside the SAM body)
         removes structures fatter than thick_px — slabs, cables, fused floor.
      2. Connectivity filter: of what's left, keep ONLY components that touch the
         SAM body. Thin straps/wires attached to the subject survive; disconnected
         junk rims die — WITHOUT eroding the edge, so hair is untouched.
    Returns a feathered keep_mask (float 0..1, 1 = keep) for the caller to multiply,
    or None if there is no SAM body this frame (no-op)."""
    import cv2

    if sam_soft is None:
        return None
    h, w = alpha.shape[:2]
    scale = float(frame_w) / 1920.0
    sam = np.asarray(sam_soft, dtype=np.float32)
    if sam.ndim == 3:
        sam = sam[..., 0]
    if sam.shape[:2] != (h, w):
        sam = cv2.resize(sam, (w, h), interpolation=cv2.INTER_LINEAR)
    sam_solid = (sam > 0.5).astype(np.uint8)
    a_solid = (np.asarray(alpha, dtype=np.float32) > 0.5).astype(np.uint8)
    if sam_solid.sum() == 0:
        return None

    body_buf = max(1, int(round(3 * scale)))
    k_buf = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (body_buf * 2 + 1, body_buf * 2 + 1))
    protected = cv2.dilate(sam_solid, k_buf)

    thick = max(1, int(round(float(junk_thick_px) * 1.875 * scale)))  # 8 -> 15px @1920
    k_thick = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (thick * 2 + 1, thick * 2 + 1))
    outside = (a_solid & (protected == 0)).astype(np.uint8)
    fat_kill = cv2.morphologyEx(outside, cv2.MORPH_OPEN, k_thick)

    remainder = np.clip(a_solid.astype(np.int32) - fat_kill.astype(np.int32), 0, 1).astype(np.uint8)
    _, labels = cv2.connectedComponents(remainder, connectivity=8)
    keep_labels = np.unique(labels[protected > 0])
    keep_labels = keep_labels[keep_labels > 0]
    keep_bin = np.isin(labels, keep_labels).astype(np.uint8) if len(keep_labels) else np.zeros_like(remainder)

    keep_f = cv2.GaussianBlur(keep_bin.astype(np.float32), (0, 0), max(0.5, 2.0 * scale))
    return np.clip(keep_f, 0.0, 1.0)


def shirt_rescue(alpha: np.ndarray, sam_soft: np.ndarray,
                 src_rgb: np.ndarray) -> np.ndarray:
    """Force alpha UP on non-green pixels deep inside the SAM body.
    Vendored verbatim from ae_plugin/cep_panel/ae_processor.py apply_shirt_rescue.
    Hair-safe: only touches pixels where G - max(R,B) < 0.15 (not green) AND
    sam > 0.85 eroded 5 px (deep body core). Green pixels are never modified."""
    try:
        import cv2

        if sam_soft is None or src_rgb is None:
            return alpha

        a = np.asarray(alpha, dtype=np.float32)
        if a.ndim == 3:
            a = a[..., 0]
        h, w = a.shape

        sam = np.asarray(sam_soft, dtype=np.float32)
        if sam.ndim == 3:
            sam = sam[..., 0]
        if sam.shape[:2] != (h, w):
            sam = cv2.resize(sam, (w, h), interpolation=cv2.INTER_LINEAR)

        src = np.asarray(src_rgb, dtype=np.float32)
        if src.shape[:2] != (h, w):
            src = cv2.resize(src, (w, h), interpolation=cv2.INTER_LINEAR)

        r = src[..., 0]
        g = src[..., 1]
        b = src[..., 2]
        not_green = (g - np.maximum(r, b)) < 0.15

        deep = (sam > 0.85).astype(np.uint8)
        k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))  # erode 5px radius (matches AE)
        deep = cv2.erode(deep, k5)

        rescue_mask = not_green & (deep > 0)
        if rescue_mask.any():
            a = a.copy()
            a[rescue_mask] = np.maximum(a[rescue_mask], 1.0)
        return a
    except Exception as exc:  # noqa: BLE001 — never let post-proc kill the key
        logger.warning("shirt_rescue skipped: %s", exc)
        return alpha


# ── Tensor <-> numpy helpers ────────────────────────────────────────
# ComfyUI IMAGE = torch float32 [B,H,W,C] in 0-1 (sRGB, RGB order).
# ComfyUI MASK  = torch float32 [B,H,W]   in 0-1.
def image_to_numpy_frames(image) -> list[np.ndarray]:
    """[B,H,W,C] torch -> list of [H,W,3] float32 numpy in 0-1."""
    arr = image.detach().cpu().numpy().astype(np.float32)
    return [np.clip(arr[i], 0.0, 1.0) for i in range(arr.shape[0])]


def mask_to_numpy_frames(mask) -> list[np.ndarray]:
    """[B,H,W] (or [B,H,W,1]) torch -> list of [H,W] float32 numpy in 0-1."""
    arr = mask.detach().cpu().numpy().astype(np.float32)
    if arr.ndim == 4:
        arr = arr[..., 0]
    if arr.ndim == 2:  # single mask, no batch
        arr = arr[np.newaxis, ...]
    return [np.clip(arr[i], 0.0, 1.0) for i in range(arr.shape[0])]


def numpy_frames_to_image(frames: list[np.ndarray]):
    """list of [H,W,3] float32 0-1 -> torch IMAGE [B,H,W,3]."""
    import torch

    stack = np.stack([np.clip(f, 0.0, 1.0).astype(np.float32) for f in frames], axis=0)
    return torch.from_numpy(stack)


def numpy_masks_to_mask(masks: list[np.ndarray]):
    """list of [H,W] float32 0-1 -> torch MASK [B,H,W]."""
    import torch

    stack = np.stack([np.clip(m, 0.0, 1.0).astype(np.float32) for m in masks], axis=0)
    return torch.from_numpy(stack)
