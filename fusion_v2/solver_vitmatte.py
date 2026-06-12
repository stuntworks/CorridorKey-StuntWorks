# Last modified: 2026-06-12 | Change: Phase 3 — ViTMatte solver
#
# WHAT IT DOES:
#   Implements a ViTMatte matting solver using HuggingFace transformers
#   VitMatteForImageMatting (Apache 2.0) with weights hustvl/vitmatte-small-
#   composition-1k (Apache 2.0).  Registers as 'vitmatte' in the shared
#   solver interface.  FG/BG pixels pass through exactly; ViTMatte resolves
#   only the unknown band.
#
#   LICENSE: codebase MIT (hustvl/ViTMatte), weights Apache 2.0
#     (hustvl/vitmatte-small-composition-1k), transformers Apache 2.0.
#     All permissive; compatible with CorridorKey CC BY-NC-SA.
#
#   4K STRATEGY — crop-downscale-solve-upscale:
#     ViTMatte was trained at ~1K resolution.  For 4K frames we:
#       1. Find the bounding box of the unknown-band region (+ padding)
#       2. Crop frame + trimap to that box (keeps full context tight)
#       3. Scale the crop so its longest dimension is ≤ max_dim (default 1024)
#       4. Snap to the backbone's size_divisor (32)
#       5. Run a SINGLE ViTMatte forward pass on the scaled crop
#       6. Upscale the resulting alpha back to original crop dimensions
#       7. Paste into the full-frame result at the original box coordinates
#     Rationale: tiling would break ViTMatte's full-attention span across the
#     unknown band and require overlap-blending that introduces seams.  A
#     single scaled pass is deterministic, faster, and lets the transformer
#     see the whole edge context at once.  Sub-pixel detail is restored by
#     the upscale step; the trimap hard constraints (FG=1.0, BG=0.0) are
#     re-enforced after pasting.
#
#   TORCH IMPORT: lazy — torch and transformers are imported only when the
#   solver function is first called.  Module import is torch-free so Phase
#   1/2 pytest runs do not trigger a torch load.  Model weights are loaded
#   ONCE on first call and cached in module-level globals.
#
#   WEIGHTS: cached to fusion_v2_weights/ at repo root (gitignored).
#
# DEPENDS ON: torch (lazy), transformers (lazy), PIL (lazy), numpy, cv2
#             fusion_v2.solver_interface (register_solver)
# AFFECTS: callers of solve_matte(solver='vitmatte')
# ISOLATED: swapping to a different solver requires no change here

import os
import numpy as np
import cv2

from fusion_v2.solver_interface import register_solver

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BG      = 0
_UNKNOWN = 128
_FG      = 255
_EPSILON = 1e-7

_WEIGHTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fusion_v2_weights",
)
_HF_REPO = "hustvl/vitmatte-small-composition-1k"

# Module-level model cache — loaded once per process, never per frame
_MODEL     = None
_PROCESSOR = None
_DEVICE    = "cuda"


# ---------------------------------------------------------------------------
# Model loading (lazy, once per process)
# ---------------------------------------------------------------------------

def _load_model_once(weights_dir: str, device: str) -> None:
    global _MODEL, _PROCESSOR
    if _MODEL is not None:
        return

    import torch  # lazy — only here
    from transformers import VitMatteForImageMatting, VitMatteImageProcessor

    os.makedirs(weights_dir, exist_ok=True)

    _PROCESSOR = VitMatteImageProcessor.from_pretrained(
        _HF_REPO, cache_dir=weights_dir
    )
    _MODEL = VitMatteForImageMatting.from_pretrained(
        _HF_REPO, cache_dir=weights_dir
    )

    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"

    _MODEL.to(device).eval()
    _DEVICE = device


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unknown_band_bbox(trimap: np.ndarray, pad_px: int):
    """Return (x0, y0, w, h) bbox of the unknown band with padding, or None."""
    H, W = trimap.shape
    rows = np.any(trimap == _UNKNOWN, axis=1)
    cols = np.any(trimap == _UNKNOWN, axis=0)
    if not rows.any():
        return None
    y0 = max(0, int(np.argmax(rows)) - pad_px)
    y1 = min(H - 1, int(H - 1 - np.argmax(rows[::-1])) + pad_px)
    x0 = max(0, int(np.argmax(cols)) - pad_px)
    x1 = min(W - 1, int(W - 1 - np.argmax(cols[::-1])) + pad_px)
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def _scale_to_max_dim(h: int, w: int, max_dim: int, size_div: int = 32):
    """Return (new_h, new_w) scaled so max(h,w) <= max_dim, snapped to size_div."""
    if max(h, w) <= max_dim:
        nh, nw = h, w
    else:
        scale = max_dim / max(h, w)
        nh = max(size_div, int(round(h * scale)))
        nw = max(size_div, int(round(w * scale)))

    nh = max(size_div, (nh // size_div) * size_div)
    nw = max(size_div, (nw // size_div) * size_div)
    return nh, nw


# ---------------------------------------------------------------------------
# Solver implementation
# ---------------------------------------------------------------------------

def _vitmatte_solve(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    nn_alpha: np.ndarray,
    weights_dir: str = _WEIGHTS_DIR,
    device: str = "cuda",
    max_dim: int = 1024,
    band_pad_px: int = 64,
) -> np.ndarray:
    """
    ViTMatte matting solver.  Resolves the unknown band using a ViT-based
    model.  FG (255) and BG (0) pixels pass through as exact 1.0 / 0.0.

    Parameters
    ----------
    frame_rgb   : uint8 (H, W, 3) RGB frame
    trimap      : uint8 (H, W) — 0=BG / 128=unknown / 255=FG
    nn_alpha    : float32 (H, W) — unused by this solver (kept for interface compat)
    weights_dir : path to cache downloaded model weights
    device      : 'cuda' or 'cpu'
    max_dim     : longest edge of the crop sent to ViTMatte (default 1024)
    band_pad_px : padding around the unknown-band bbox before solve (default 64)

    Returns
    -------
    float32 (H, W) alpha, clamped [0, 1]
    """
    import torch
    from PIL import Image as PILImage

    _load_model_once(weights_dir, device)

    H, W = trimap.shape
    dev = next(_MODEL.parameters()).device

    # Seed result with trimap hard values; ViTMatte fills only the unknown band
    result = np.where(trimap == _FG, 1.0,
             np.where(trimap == _BG, 0.0, 0.5)).astype(np.float32)

    # Locate the unknown band
    bbox = _unknown_band_bbox(trimap, band_pad_px)
    if bbox is None:
        result[trimap == _FG] = 1.0
        result[trimap == _BG] = 0.0
        return result

    x0, y0, bw, bh = bbox
    x1, y1 = x0 + bw, y0 + bh

    # Crop frame and trimap to the band bbox
    crop_frame  = frame_rgb[y0:y1, x0:x1]
    crop_trimap = trimap[y0:y1, x0:x1]

    # Scale to max_dim, snapped to size_div=32
    nh, nw = _scale_to_max_dim(bh, bw, max_dim, size_div=32)

    small_frame  = cv2.resize(crop_frame,  (nw, nh), interpolation=cv2.INTER_LINEAR)
    small_trimap = cv2.resize(crop_trimap, (nw, nh), interpolation=cv2.INTER_NEAREST)

    # Processor: RGB uint8 image + grayscale trimap (0/128/255)
    pil_frame  = PILImage.fromarray(small_frame.astype(np.uint8))
    pil_trimap = PILImage.fromarray(small_trimap)

    inputs = _PROCESSOR(images=pil_frame, trimaps=pil_trimap, return_tensors="pt")
    inputs = {k: v.to(dev) for k, v in inputs.items()}

    with torch.inference_mode():
        out = _MODEL(**inputs)
        # alphas shape: (1, 1, padded_H, padded_W) — crop back to (nh, nw)
        alpha_small = out.alphas[0, 0, :nh, :nw].float().cpu().numpy()

    # Upscale back to original crop size
    alpha_crop = cv2.resize(alpha_small, (bw, bh), interpolation=cv2.INTER_LINEAR)
    alpha_crop = np.clip(alpha_crop, 0.0, 1.0).astype(np.float32)

    # Paste into full-frame result; apply only where trimap == unknown
    unknown_in_crop = crop_trimap == _UNKNOWN
    result_view = result[y0:y1, x0:x1]
    result_view[unknown_in_crop] = alpha_crop[unknown_in_crop]

    # Re-enforce hard constraints (upscale + paste can't drift these)
    result[trimap == _FG] = 1.0
    result[trimap == _BG] = 0.0

    return result


# ---------------------------------------------------------------------------
# Self-registration (torch-free at import time — only register_solver called)
# ---------------------------------------------------------------------------

register_solver("vitmatte", _vitmatte_solve)
