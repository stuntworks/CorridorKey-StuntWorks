# Last modified: 2026-05-05 | Change: NEW — Path B CK+SAM merge per Berto 2026-05-05 spec | Full history: git log
"""Path B SAM2 + CK alpha merge — final = max(CK, threshold(SAM)).

REPLACES the apply_sam2_junk_kill / apply_sam2_gate_* post-hoc combine
architecture in sam2_combine.py. Those helpers stay on disk as dead code
(unreferenced from any call site) until Path B is validated on the test
clip, at which point they get removed in a separate cleanup commit.

CK is the base layer. SAM contributes ONLY where CK is lower than SAM.
CK is never overridden. SAM cannot stomp CK detail. The user clicks 2-3
dots on the WHOLE body and SAM returns a whole-body silhouette; the
plugin does the region isolation math invisibly via this module.

Per Berto 2026-05-05 spec — algebraic form (kept literal in code so the
implementation reads back to the brief):

    difference = clip(SAM_binary - CK, 0, 1)   # pixels SAM thinks are body that CK rated as background
    final      = clip(CK + difference, 0, 1)   # CK preserved, missing regions filled

Mathematically equivalent to per-pixel max(CK, SAM_binary). Algebraic
form preferred for spec traceability — do NOT swap to np.maximum without
updating the docstring and Berto's brief.
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np


# Threshold matches the existing 0.5 convention used by every viewer
# binarisation site (post-sigmoid SAM output). Raise to make SAM more
# conservative; lower to let more of SAM's confidence band contribute.
SAM_BINARIZE_THRESHOLD = 0.5


def binarize_sam_silhouette(sam: np.ndarray, threshold: float = SAM_BINARIZE_THRESHOLD) -> np.ndarray:
    # WHAT IT DOES: Threshold a continuous SAM mask to binary {0.0, 1.0} float32.
    # DEPENDS ON:   numpy. Caller pre-applies sigmoid if input is logit-space.
    # AFFECTS:      every Path B merge — controls SAM silhouette edge sharpness.
    return (np.asarray(sam, dtype=np.float32) >= float(threshold)).astype(np.float32)


def union_binary_silhouettes(silhouettes: Iterable[np.ndarray]) -> Optional[np.ndarray]:
    # WHAT IT DOES: OR-combine N already-binarised SAM silhouettes via per-pixel max.
    # DEPENDS ON:   all silhouettes share identical H x W shape. None entries dropped.
    # AFFECTS:      multi-object renders (MASK 1 + MASK 2). Single-object: pass-through.
    valid = [np.asarray(s, dtype=np.float32) for s in silhouettes if s is not None]
    if not valid:
        return None
    out = valid[0]
    for s in valid[1:]:
        out = np.maximum(out, s)
    return out.astype(np.float32, copy=False)


def merge_ck_with_sam(ck_alpha: np.ndarray, sam_silhouette: Optional[np.ndarray]) -> np.ndarray:
    # WHAT IT DOES: Path B merge — CK preserved, SAM adds pixels CK rated below SAM.
    #               Returns final = clip(CK + clip(SAM - CK, 0, 1), 0, 1).
    # DEPENDS ON:   ck_alpha and sam_silhouette must share identical H x W shape.
    #               Both float32 in [0, 1]. sam_silhouette should be ALREADY binary —
    #               call binarize_sam_silhouette first if input is continuous.
    # AFFECTS:      every per-frame and per-preview matte once Path B is wired in.
    #               When sam_silhouette is None, returns a CK copy (CK alone is the answer).
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if sam_silhouette is None:
        return ck.copy()
    sam = np.asarray(sam_silhouette, dtype=np.float32)
    if ck.shape != sam.shape:
        raise ValueError(f"shape mismatch: ck={ck.shape}, sam={sam.shape}")
    # DANGER ZONE FRAGILE: algebraic form is the spec contract — do NOT collapse
    # to np.maximum(ck, sam) without updating the docstring and Berto's 2026-05-05
    # brief. The two are mathematically equivalent for inputs in [0, 1] but the
    # algebraic form documents intent ("difference is what SAM adds beyond CK").
    # breaks: spec traceability when anyone diffs against the brief.
    # depends on: sam already binary; soft sam will hard-stomp CK soft-edge alpha.
    difference = np.clip(sam - ck, 0.0, 1.0)
    final = np.clip(ck + difference, 0.0, 1.0)
    return final
