"""SAM2 + NN alpha combine helper — single source of truth for the v1 INVERT plumbing."""
from __future__ import annotations

import numpy as np


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
