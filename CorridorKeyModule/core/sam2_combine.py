"""SAM2 + NN alpha combine helper — single source of truth for the v1 INVERT plumbing."""
from __future__ import annotations

import numpy as np


# DANGER ZONE FRAGILE: this is the SINGLE SOURCE OF TRUTH for SAM2 + NN combine. All 6 callsites must use this. Do NOT inline alpha*gate elsewhere.
def apply_sam2_gate(alpha: np.ndarray, gate: np.ndarray | None, invert: bool = False) -> np.ndarray:
    """Combine NN alpha with SAM2 gate. invert=True = garbage matte mode (subtract clicked region)."""
    if gate is None:
        return alpha
    gate_use = (1.0 - gate) if invert else gate
    return alpha * gate_use
