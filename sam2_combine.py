"""SAM2 + NN alpha combine helper — single source of truth for the v1 INVERT plumbing."""
from __future__ import annotations

import numpy as np


def trim_gate_by_chroma(gate: np.ndarray, source_rgb: np.ndarray, screen_type: str = "green", strength: int = 0) -> np.ndarray:
    """Trim screen-colored pixels FROM the SAM2 gate before combine.

    Returns gate unchanged when strength <= 0 (bit-identical to no-trim path).
    For strength > 0, computes a per-pixel chroma score on source_rgb (RGB float
    [0..1]) — green = G - max(R, B), blue = B - max(R, G) — and zeros out gate
    pixels whose chroma exceeds threshold t = 0.05 + (1 - strength/100) * 0.4.
    Higher strength = lower threshold = more aggressive trim. Used to prevent
    SAM2 from claiming green pixels at silhouette edges that NN correctly keyed
    as background.
    """
    if strength is None or int(strength) <= 0:
        return gate
    s = float(int(strength))
    if screen_type == "blue":
        chroma_score = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:  # default to green for any other value
        chroma_score = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma_score = np.clip(chroma_score, 0.0, 1.0)
    t = 0.05 + (1.0 - s / 100.0) * 0.4
    is_screen = (chroma_score > t).astype(np.float32)
    return (gate * (1.0 - is_screen)).astype(gate.dtype, copy=False)


def fill_holes_color_aware(alpha: np.ndarray, gate: np.ndarray, source_rgb: np.ndarray, screen_type: str = "green", strength: int = 0) -> np.ndarray:
    """Fill alpha=0 holes inside SAM2 region for non-screen-color pixels.

    strength: 0..100 integer. 0 = off (returns input unchanged — bit-identical).
    Higher = more aggressive (more lenient on what counts as "non-screen").

    For each pixel where gate > 0.5 AND alpha < 0.5 AND pixel is not screen-color,
    set alpha = 1.0. Returns a NEW array; input alpha is unchanged.

    Why this differs from trim_gate_by_chroma: trim removes screen-colored pixels
    FROM the gate (kills 0×1 to 0×0 — invisible). Fill flips alpha at low-alpha
    pixels INSIDE the gate that are not screen-colored — rescues NN dropouts on
    yellow shirts / skin / red while leaving correctly-killed green pixels alone.
    """
    if strength is None or int(strength) <= 0:
        return alpha
    s = float(int(strength))
    is_inside = gate > 0.5
    is_low_alpha = alpha < 0.5
    candidates = is_inside & is_low_alpha
    if screen_type == "blue":
        chroma = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma = np.clip(chroma, 0.0, 1.0)
    # Higher strength = higher threshold = more pixels qualify as non-screen.
    # At strength=1, t=0.054 (strict — almost anything green-leaning excluded).
    # At strength=100, t=0.45 (lenient — only pure-green excluded).
    t = 0.05 + (s / 100.0) * 0.4
    is_screen = chroma > t
    fill_mask = candidates & ~is_screen
    out = alpha.copy()
    out[fill_mask] = 1.0
    return out.astype(alpha.dtype, copy=False)


def apply_sam2_gate_additive(alpha, gate, source_rgb, screen_type='green'):
    """Additive combine: alpha = max(NN, gate * non_screen).

    SAM2 can ADD confidence where NN missed but never SUBTRACT NN's correct
    alpha. The non_screen test prevents SAM2 from flooding green-pixel
    regions (e.g. green-screen background that NN correctly killed).

    Use as a drop-in alternative to apply_sam2_gate(alpha, gate) when the
    user has SAM2 ADDITIVE mode toggled ON. source_rgb is float [0..1] RGB.
    """
    if gate is None:
        return alpha
    # Non-screen mask: 1.0 where pixel is NOT screen-color, 0.0 where it is.
    if screen_type == "blue":
        chroma = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma = np.clip(chroma, 0.0, 1.0)
    is_screen = (chroma > 0.1).astype(np.float32)  # fixed threshold; tune later if needed
    non_screen = 1.0 - is_screen
    contribution = gate * non_screen
    return np.maximum(alpha, contribution).astype(alpha.dtype, copy=False)


# DANGER ZONE FRAGILE: this is the SINGLE SOURCE OF TRUTH for SAM2 + NN combine. All 6 callsites must use this. Do NOT inline alpha*gate elsewhere.
def apply_sam2_gate(alpha: np.ndarray, gate: np.ndarray | None, invert: bool = False, halo_px: int = 0) -> np.ndarray:
    """Combine NN alpha with SAM2 gate. invert=True = garbage matte mode (subtract clicked region).

    halo_px: trimap-guided halo band width in pixels. When 0 (default), behavior is
    bit-identical to the previous version: alpha * gate (or alpha * (1-gate) when
    invert). When >0, the SAM2 gate is binarized at 0.5 and dilated by halo_px so a
    band of NN-driven alpha values around the SAM2 silhouette survives the gate
    multiply. Recovers hair / motion-blur detail at the SAM2 edge.
    """
    if gate is None:
        return alpha
    gate_use = (1.0 - gate) if invert else gate
    if halo_px and halo_px > 0:
        # Lazy import — keep the no-halo fast path numpy-only and avoid forcing
        # cv2 onto callers that never use halo. Viewers already import cv2.
        import cv2 as _cv2
        binary = (gate_use > 0.5).astype(np.uint8)
        k = int(halo_px) * 2 + 1
        kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (k, k))
        extended = _cv2.dilate(binary, kernel).astype(np.float32)
        return alpha * extended
    return alpha * gate_use
