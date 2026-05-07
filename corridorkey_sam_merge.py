# Last modified: 2026-05-06 | Change: diagnostic tracing for chroma-aware kill mask - 3-panel split image, branch selection stats, sample pixel checks | Full history: git log
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

from pathlib import Path
from typing import Iterable, Optional

import numpy as np


# Threshold matches the existing 0.5 convention used by every viewer
# binarisation site (post-sigmoid SAM output). Raise to make SAM more
# conservative; lower to let more of SAM's confidence band contribute.
SAM_BINARIZE_THRESHOLD = 0.5

# Diagnostic toggle — per Berto 2026-05-06 to confirm what chroma-gated merge
# actually computes on the dim-greenscreen test clip (output observed identical
# to SAM-alone, possible causes: chroma_score zero everywhere, or a stale code
# path overriding our merge). When True, every chroma-gated call dumps 6 PNGs
# + stats.txt to DEBUG_DIR. Flip to False once diagnosis is done — debug write
# overwrites previous run, so only the LAST merge survives. Costs ~30-50 ms per
# call on a 4K frame.
DEBUG_ENABLED = True
DEBUG_DIR = Path(__file__).parent / ".chroma_debug"

# Active merge mode toggle. Flip and re-deploy to A/B between Path B and chroma-gated.
# Per Berto 2026-05-06: True for chroma-gated test; flip to False to fall back to Path B.
# Test clip with False already validated 2026-05-05; junk-in-non-green is the failure
# case that chroma-gating addresses.
USE_CHROMA_GATED_MERGE = True

# Chroma test threshold — per-pixel green excess (G - max(R, B)) above which the pixel
# counts as "on-green" and CK rules. Dropped 0.05 -> 0.01 on 2026-05-06 after dim
# green screen test clip showed chroma_score < 0.05 across the whole frame — zero
# pixels registered as on-green, CK contributed nothing, output collapsed to SAM-alone.
# 0.01 sits below sam2_combine.apply_sam2_gate_additive's 0.1 threshold (line 185)
# precisely to handle dim/under-lit green-screen sets. Lower catches more spilled-edge
# pixels as on-green; higher restricts CK rule to clearly-green pixels.
CHROMA_GATE_THRESHOLD = 0.01

# On-green region dilation in pixels. Extends the binary chroma mask inward so body
# pixels with low chroma (no green spill on skin / dark fabric) but spatially inside
# the green-screen area still register as on-green and let CK rule. Solves the
# butt-notch + fingertip-cut cases observed 2026-05-06 in DaVinci testing — body
# skin is chroma~0, SAM under-clipped at butt + fingertips, without dilation those
# pixels were ruled by SAM's wrong silhouette. 50 px works on the woman-with-foot-
# off-green clip (notch depth ~30-50 px). Tune lower if walls re-appear (dilation
# reaching junk pixels close to green); tune higher if more body parts get cut.
CHROMA_GATE_DILATE_PX = 50

# Outer SAM-derived kill-mask kernel sizes. Per Berto 2026-05-06 — chroma-gated
# weight=1 covers walls/forklift when those pixels have green spill or are within
# CHROMA_GATE_DILATE_PX of a green tarp; CK rules there with all its false
# positives. The kill-mask wraps the merge output: 1 = inside (SAM closed +
# outward buffer) so merge votes survive, 0 = far from any plausible body so
# merge output is forced to 0 regardless of what CK said.
#
# CLOSE_KERNEL_PX fills internal holes/notches in the SAM silhouette before it
# becomes a kill mask — prevents the butt-notch from punching through the kill
# mask and over-killing valid CK body pixels.
#
# DILATE_ON_GREEN_PX vs DILATE_OFF_GREEN_PX: per Berto 2026-05-06 evening, a
# single uniform dilation can't satisfy both ends of the trade-off — hair / butt
# soft edges need a LARGE buffer (they live on-green where CK supplies soft
# alpha past SAM), while leg / shoe halos need a SMALL buffer (off-green where
# CK leftover alpha leaks through). Buffer is chroma-aware: large in on-green
# zones, small in off-green zones.
SAM_KILL_CLOSE_KERNEL_PX = 75
SAM_KILL_DILATE_ON_GREEN_PX = 100
SAM_KILL_DILATE_OFF_GREEN_PX = 15


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


def compute_chroma_weight(
    source_rgb: np.ndarray,
    screen_type: str = "green",
    threshold: float = CHROMA_GATE_THRESHOLD,
    soft_band: float = 0.0,
    dilate_px: int = CHROMA_GATE_DILATE_PX,
) -> np.ndarray:
    # WHAT IT DOES: Per-pixel float [0, 1] weight encoding "is this pixel on the
    #   green-screen side?" 1 = on-green (CK rules), 0 = off-green (SAM rules).
    #   Reuses the G - max(R, B) chroma score the sam2_combine.py family uses.
    # DEPENDS ON:   source_rgb is float32 (H, W, 3) in [0, 1]. Caller handles dtype.
    #               cv2 imported lazily ONLY when dilate_px > 0.
    # AFFECTS:      determines per-pixel CK vs SAM authority in chroma-gated merge.
    if screen_type == "blue":
        chroma_score = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma_score = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma_score = np.clip(chroma_score, 0.0, 1.0)

    # DANGER ZONE FRAGILE: do NOT add Gaussian blur to weight or chroma_score.
    # SMART BLEND in sam2_combine.apply_sam2_gate_weighted (line 191) stacked
    # three sources of softness — chroma * 5.0 ramp + Gaussian on weight +
    # Gaussian on softened SAM gate — and produced 50% ghost bands at body-green
    # edges where CK soft alpha 0.5 blended with SAM 1.0 gave 0.75 output.
    # breaks: ghost bands at body-green silhouette edges (Berto 2026-05-01).
    # depends on: SAM stays binary downstream and chroma weight stays sharp.
    if soft_band <= 0.0:
        # Hard binary — no soft transition. Berto-approved 2026-05-06 default.
        weight = (chroma_score >= threshold).astype(np.float32)
    else:
        # Smoothstep ramp across [threshold, threshold + soft_band]. OFF by default
        # because of the SMART BLEND ghost history. Only enable for explicit tuning.
        t = np.clip((chroma_score - threshold) / soft_band, 0.0, 1.0)
        weight = (t * t * (3.0 - 2.0 * t)).astype(np.float32)

    if dilate_px > 0:
        # Extend on-green region into body interior by dilate_px pixels. Solves the
        # case where body-skin pixels have low chroma (no green spill) but spatially
        # sit inside the green-screen area — the butt-notch + fingertip-cut cases
        # observed 2026-05-06 in DaVinci. ON by default at CHROMA_GATE_DILATE_PX=50.
        # Set dilate_px=0 to disable.
        import cv2 as _cv2
        binary = (weight > 0.5).astype(np.uint8)
        _k = int(dilate_px) * 2 + 1
        kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (_k, _k))
        dilated = _cv2.dilate(binary, kernel).astype(np.float32)
        weight = np.maximum(weight, dilated)

    return weight


def compute_sam_kill_mask(
    sam_silhouette: np.ndarray,
    chroma_weight: np.ndarray,
    close_kernel_px: int = SAM_KILL_CLOSE_KERNEL_PX,
    on_green_dilate_px: int = SAM_KILL_DILATE_ON_GREEN_PX,
    off_green_dilate_px: int = SAM_KILL_DILATE_OFF_GREEN_PX,
    _components_out: Optional[dict] = None,
) -> np.ndarray:
    # WHAT IT DOES: Build a chroma-aware binary kill mask from the SAM
    #   silhouette. 1 = merge votes survive. 0 = merge forced to 0.
    #   Buffer size depends on chroma_weight per pixel:
    #     on-green  (weight > 0.5): LARGE outward buffer — preserves CK soft
    #                                edges (hair, butt fringe).
    #     off-green (weight <= 0.5): SMALL outward buffer — kills shoe/leg
    #                                halos and CK leftover alpha bleed.
    #   Per Berto 2026-05-06 evening — uniform dilation hit a hard trade-off
    #   between hair preservation and shoe halo elimination; chroma-aware
    #   buffer breaks the trade-off because the two failure modes live in
    #   different chroma zones.
    # DEPENDS ON:   cv2 (lazy), numpy. sam_silhouette is (H, W) float32 in [0,1],
    #               typically already binarized. chroma_weight is (H, W) float32
    #               in [0,1] from compute_chroma_weight (binary at the merge
    #               site, soft band off by default).
    # AFFECTS:      multiplies the chroma-gated merge result inside
    #               merge_ck_with_sam_chroma_gated. Caller MUST pass the same
    #               weight it used in the per-pixel merge — using a recomputed
    #               weight risks zone mismatch at edges.
    import cv2 as _cv2
    sam_uint8 = sam_silhouette.astype(np.uint8)
    # Morphological close fills internal notches (butt notch, finger gaps) so
    # they don't punch through the kill mask after dilation.
    close_size = max(1, int(close_kernel_px))
    close_kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (close_size, close_size))
    sam_closed = _cv2.morphologyEx(sam_uint8, _cv2.MORPH_CLOSE, close_kernel)
    # Two dilations — big (on-green) and small (off-green) — then per-pixel
    # selection by chroma zone.
    big_size = max(1, int(on_green_dilate_px))
    small_size = max(1, int(off_green_dilate_px))
    big_kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (big_size, big_size))
    small_kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (small_size, small_size))
    sam_kill_big = _cv2.dilate(sam_closed, big_kernel)
    sam_kill_small = _cv2.dilate(sam_closed, small_kernel)
    sam_kill = np.where(chroma_weight > 0.5, sam_kill_big, sam_kill_small)
    if _components_out is not None:
        _components_out["sam_closed"] = sam_closed.astype(np.float32)
        _components_out["sam_kill_big"] = sam_kill_big.astype(np.float32)
        _components_out["sam_kill_small"] = sam_kill_small.astype(np.float32)
        _components_out["received_chroma_weight"] = bool(
            isinstance(chroma_weight, np.ndarray)
            and chroma_weight.shape == sam_silhouette.shape
        )
        _components_out["close_kernel_px"] = int(close_kernel_px)
        _components_out["on_green_dilate_px"] = int(on_green_dilate_px)
        _components_out["off_green_dilate_px"] = int(off_green_dilate_px)
    return sam_kill.astype(np.float32)


def _write_debug_dump(ck, sam, source_rgb, weight, final, screen_type, threshold, dilate_px, sam_kill=None, merge_pre_kill=None, kill_components=None):
    # WHAT IT DOES: Diagnostic dump — writes 6 PNGs + debug_stats.txt to DEBUG_DIR
    #   so Berto can see what chroma-gated merge actually saw + produced. Called
    #   only when DEBUG_ENABLED is True. Recomputes raw chroma_score (cheap) for
    #   the pre-clip / pre-threshold view; everything else is what merge already
    #   computed. Overwrites previous dump; only the LAST merge call survives.
    # DEPENDS ON:   cv2 (lazy), numpy, pathlib. Inputs are the merge function's
    #               local state at the moment of return.
    # AFFECTS:      writes 7 files to DEBUG_DIR; no return; no logic side effect.
    import cv2 as _cv2
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    # Recompute raw (pre-clip) chroma to expose negative scores too.
    if screen_type == "blue":
        raw_chroma = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        raw_chroma = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])

    # raw_chroma in [-1, 1] (typically [-0.5, +0.5]) → mid-grey at zero.
    raw_vis = np.clip((raw_chroma + 0.5) * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_chroma_score_raw.png"), raw_vis)

    weight_vis = np.clip(weight * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_chroma_weight_final.png"), weight_vis)

    # source_rgb is RGB float [0,1]; OpenCV writes BGR.
    source_vis = np.clip(source_rgb * 255.0, 0.0, 255.0).astype(np.uint8)
    source_bgr = _cv2.cvtColor(source_vis, _cv2.COLOR_RGB2BGR)
    _cv2.imwrite(str(DEBUG_DIR / "debug_source_rgb.png"), source_bgr)

    ck_vis = np.clip(ck * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_ck_alpha.png"), ck_vis)

    sam_vis = np.clip(sam * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_sam_silhouette.png"), sam_vis)

    final_vis = np.clip(final * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "debug_merge_output.png"), final_vis)

    if sam_kill is not None:
        kill_vis = np.clip(sam_kill * 255.0, 0.0, 255.0).astype(np.uint8)
        _cv2.imwrite(str(DEBUG_DIR / "debug_sam_kill_mask.png"), kill_vis)

    # Side-by-side split: sam_kill_big | sam_kill_small | np.where result.
    # Proves both branches compute correctly and that np.where picks the
    # expected branch per pixel.
    if (
        sam_kill is not None
        and kill_components is not None
        and "sam_kill_big" in kill_components
        and "sam_kill_small" in kill_components
    ):
        big = kill_components["sam_kill_big"]
        small = kill_components["sam_kill_small"]
        H_split, W_split = sam_kill.shape
        sep_w = max(8, W_split // 200)
        sep = np.full((H_split, sep_w), 0.5, dtype=np.float32)
        composite = np.concatenate([big, sep, small, sep, sam_kill], axis=1)
        composite_vis = np.clip(composite * 255.0, 0.0, 255.0).astype(np.uint8)
        _cv2.imwrite(str(DEBUG_DIR / "debug_kill_mask_split.png"), composite_vis)

    n_pixels = float(raw_chroma.size)
    pct_above_threshold = float(np.sum(raw_chroma >= threshold)) / n_pixels * 100.0
    pct_weight_high = float(np.sum(weight > 0.5)) / n_pixels * 100.0
    ck_times_weight = ck * weight

    stats_lines = [
        "=== chroma-gated merge debug dump ===",
        f"raw_chroma  min={float(raw_chroma.min()):.4f}  "
        f"max={float(raw_chroma.max()):.4f}  "
        f"mean={float(raw_chroma.mean()):.4f}",
        f"threshold (CHROMA_GATE_THRESHOLD)  = {threshold}",
        f"dilate_px (CHROMA_GATE_DILATE_PX)  = {dilate_px}",
        f"% pixels chroma>=threshold  (pre-dilate)  = {pct_above_threshold:.2f}%",
        f"% pixels weight>0.5         (post-dilate) = {pct_weight_high:.2f}%",
        f"sum(chroma_weight) = {float(weight.sum()):.2f}",
        f"sum(merge_output)  = {float(final.sum()):.2f}",
        f"mean(CK * weight)  = {float(ck_times_weight.mean()):.6f}  "
        f"# nonzero means CK is contributing somewhere",
        f"shapes: ck={ck.shape} sam={sam.shape} source_rgb={source_rgb.shape} "
        f"weight={weight.shape} final={final.shape}",
    ]
    if sam_kill is not None:
        on_green = weight > 0.5
        off_green = ~on_green
        kill_one = sam_kill > 0.5
        pct_kill_one = float(kill_one.sum()) / n_pixels * 100.0
        on_green_pixels = float(on_green.sum())
        off_green_pixels = float(off_green.sum())
        pct_kill_in_ongreen = (
            float(np.sum(kill_one & on_green)) / on_green_pixels * 100.0
            if on_green_pixels > 0 else 0.0
        )
        pct_kill_in_offgreen = (
            float(np.sum(kill_one & off_green)) / off_green_pixels * 100.0
            if off_green_pixels > 0 else 0.0
        )
        stats_lines.append(
            f"SAM_KILL_CLOSE_KERNEL_PX={SAM_KILL_CLOSE_KERNEL_PX} "
            f"SAM_KILL_DILATE_ON_GREEN_PX={SAM_KILL_DILATE_ON_GREEN_PX} "
            f"SAM_KILL_DILATE_OFF_GREEN_PX={SAM_KILL_DILATE_OFF_GREEN_PX}"
        )
        stats_lines.append(
            f"% pixels sam_kill=1 = {pct_kill_one:.2f}%  "
            f"(on-green zones: {pct_kill_in_ongreen:.2f}%, "
            f"off-green zones: {pct_kill_in_offgreen:.2f}%)"
        )
        if merge_pre_kill is not None:
            mean_after = float((merge_pre_kill * sam_kill).mean())
            stats_lines.append(
                f"mean(merge_result * sam_kill) = {mean_after:.6f}  "
                f"(mean(merge_result alone) = {float(merge_pre_kill.mean()):.6f})"
            )
        # Chroma-aware split tracing: confirm np.where is picking the right
        # branch per pixel and quantify the difference between the two
        # dilations across the frame.
        if kill_components is not None:
            received = bool(kill_components.get("received_chroma_weight", False))
            stats_lines.append(
                f"compute_sam_kill_mask called with chroma_weight="
                f"{'YES' if received else 'NO'}"
            )
            stats_lines.append(
                "compute_sam_kill_mask runtime kernels: "
                f"close={kill_components.get('close_kernel_px')} "
                f"big={kill_components.get('on_green_dilate_px')} "
                f"small={kill_components.get('off_green_dilate_px')}"
            )
            big = kill_components.get("sam_kill_big")
            small = kill_components.get("sam_kill_small")
            if big is not None and small is not None:
                big_b = big > 0.5
                small_b = small > 0.5
                # Where np.where actually selected each branch within sam_kill=1
                from_big = kill_one & on_green
                from_small = kill_one & off_green
                pct_from_big = float(from_big.sum()) / n_pixels * 100.0
                pct_from_small = float(from_small.sum()) / n_pixels * 100.0
                stats_lines.append(
                    f"% sam_kill=1 selected from sam_kill_big  = {pct_from_big:.2f}%"
                )
                stats_lines.append(
                    f"% sam_kill=1 selected from sam_kill_small = {pct_from_small:.2f}%"
                )
                # Edge zone: pixels where the two dilations actually differ.
                # If this is small, the two kernels are producing nearly
                # identical kill masks regardless of the chroma split.
                edge_zone = big_b != small_b
                edge_count = float(edge_zone.sum())
                pct_edge = edge_count / n_pixels * 100.0
                stats_lines.append(
                    f"edge zone (big!=small): {int(edge_count)} px ({pct_edge:.2f}% of frame) "
                    "— if tiny, the two kernels are basically the same"
                )
                if edge_count > 0:
                    edge_big_pct = (
                        float((edge_zone & on_green).sum()) / edge_count * 100.0
                    )
                    edge_small_pct = (
                        float((edge_zone & off_green).sum()) / edge_count * 100.0
                    )
                    stats_lines.append(
                        f"  in edge zone: on-green={edge_big_pct:.2f}% "
                        f"off-green={edge_small_pct:.2f}%"
                    )
                # Sample pixels — pick one clearly on-green and one clearly
                # off-green near the body to verify np.where picked correctly.
                on_idx = np.argwhere(on_green)
                off_idx = np.argwhere(off_green)
                if len(on_idx) > 0:
                    yo, xo = on_idx[len(on_idx) // 2]
                    stats_lines.append(
                        f"sample on-green pixel ({int(yo)}, {int(xo)}): "
                        f"big={float(big[yo, xo]):.2f} "
                        f"small={float(small[yo, xo]):.2f} "
                        f"kill={float(sam_kill[yo, xo]):.2f}  "
                        f"(np.where should pick big)"
                    )
                if len(off_idx) > 0:
                    yf, xf = off_idx[len(off_idx) // 2]
                    stats_lines.append(
                        f"sample off-green pixel ({int(yf)}, {int(xf)}): "
                        f"big={float(big[yf, xf]):.2f} "
                        f"small={float(small[yf, xf]):.2f} "
                        f"kill={float(sam_kill[yf, xf]):.2f}  "
                        f"(np.where should pick small)"
                    )
    (DEBUG_DIR / "debug_stats.txt").write_text("\n".join(stats_lines) + "\n")


def merge_ck_with_sam_chroma_gated(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: np.ndarray,
    screen_type: str = "green",
    threshold: float = CHROMA_GATE_THRESHOLD,
    soft_band: float = 0.0,
    dilate_px: int = CHROMA_GATE_DILATE_PX,
) -> np.ndarray:
    # WHAT IT DOES: Chroma-gated CK + SAM merge per Berto 2026-05-06.
    #     final = weight * CK + (1 - weight) * SAM_binary
    #   where weight is 1 (use CK) on green-screen pixels, 0 (use SAM) off-green.
    #   On-green: CK is authoritative — NN keys body cleanly on green. SAM has
    #     no authority here, so SAM body-shape errors (the butt notch from
    #     2026-05-05 testing) cannot damage CK.
    #   Off-green: SAM is authoritative — kills false-positive CK pixels in
    #     non-green areas (concrete walls, props) and ADDS body parts CK
    #     missed (foot stepping off the green floor).
    # DEPENDS ON:   ck_alpha and sam_silhouette are (H, W) float32 in [0, 1] with
    #               matching shapes. source_rgb is (H, W, 3) float32 in [0, 1].
    #               compute_chroma_weight, binarize_sam_silhouette.
    # AFFECTS:      every per-frame and per-preview matte when USE_CHROMA_GATED_MERGE
    #               is True. Replaces Path B's chroma-blind max(CK, SAM).
    ck = np.asarray(ck_alpha, dtype=np.float32)
    if sam_silhouette is None:
        return ck.copy()
    sam = binarize_sam_silhouette(sam_silhouette)
    if ck.shape != sam.shape:
        raise ValueError(f"shape mismatch: ck={ck.shape}, sam={sam.shape}")
    weight = compute_chroma_weight(
        source_rgb, screen_type=screen_type,
        threshold=threshold, soft_band=soft_band, dilate_px=dilate_px,
    )
    if weight.shape != ck.shape:
        raise ValueError(f"chroma weight shape mismatch: weight={weight.shape}, ck={ck.shape}")
    merge_result = weight * ck + (1.0 - weight) * sam
    # Outer SAM-based kill mask. Wraps the chroma-gated result so walls /
    # forklift / junk that the chroma weight allowed CK to vote on get killed
    # if they sit far from the SAM body silhouette. Per Berto 2026-05-06.
    _kill_components: Optional[dict] = {} if DEBUG_ENABLED else None
    sam_kill = compute_sam_kill_mask(sam, weight, _components_out=_kill_components)
    final = merge_result * sam_kill
    final = np.clip(final, 0.0, 1.0).astype(np.float32)
    if DEBUG_ENABLED:
        try:
            _write_debug_dump(
                ck, sam, source_rgb, weight, final, screen_type, threshold, dilate_px,
                sam_kill=sam_kill, merge_pre_kill=merge_result,
                kill_components=_kill_components,
            )
        except Exception as _e:
            print(f"[merge debug] dump failed: {_e}")
    return final


def merge_ck_with_sam_active(
    ck_alpha: np.ndarray,
    sam_silhouette: Optional[np.ndarray],
    source_rgb: Optional[np.ndarray] = None,
    screen_type: str = "green",
) -> np.ndarray:
    # WHAT IT DOES: Public dispatcher. Routes to chroma-gated merge when the
    #   module flag USE_CHROMA_GATED_MERGE is True AND source_rgb is provided;
    #   otherwise falls back to Path B (chroma-blind max). Call sites use this
    #   single entry point so the A/B switch is a one-flag flip with no
    #   call-site changes.
    # DEPENDS ON:   USE_CHROMA_GATED_MERGE module flag, merge_ck_with_sam (Path B),
    #               merge_ck_with_sam_chroma_gated.
    # AFFECTS:      every site that imports this dispatcher. Currently:
    #               resolve renderer, resolve viewer, AE viewer (composite + matte).
    if USE_CHROMA_GATED_MERGE and source_rgb is not None:
        print(
            f"[merge] chroma-gated branch  USE_CHROMA_GATED_MERGE=True  "
            f"source_rgb.shape={getattr(source_rgb, 'shape', None)}"
        )
        return merge_ck_with_sam_chroma_gated(
            ck_alpha, sam_silhouette, source_rgb, screen_type=screen_type,
        )
    _reason = "flag=False" if not USE_CHROMA_GATED_MERGE else "source_rgb=None"
    print(f"[merge] Path B fallback branch  ({_reason})")
    return merge_ck_with_sam(ck_alpha, sam_silhouette)


def write_matte_final_dump(alpha_final: np.ndarray, ops_applied) -> None:
    # WHAT IT DOES: Snapshots the chroma-gated debug PNGs (debug_*.png) under matte_-
    #   prefixed names so they survive any subsequent composite-mode re-render that
    #   would overwrite them, then saves the final post-processed alpha as
    #   matte_debug_final_displayed.png + writes matte_debug_stats.txt with the
    #   ordered list of ops applied to the merge output before display.
    # DEPENDS ON:   cv2 (lazy), shutil, numpy, pathlib. DEBUG_DIR mkdir-on-write.
    #               Assumes the caller's most recent merge_ck_with_sam_chroma_gated
    #               call dumped to debug_*.png moments earlier (true for the resolve
    #               viewer matte branch where merge runs once per render before this).
    # AFFECTS:      writes 7 matte_-prefixed files to DEBUG_DIR; no return; no logic
    #               side effect. No-op when DEBUG_ENABLED is False.
    if not DEBUG_ENABLED:
        return
    import cv2 as _cv2
    import shutil as _shutil
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_pairs = [
        ("debug_source_rgb.png", "matte_debug_source_rgb.png"),
        ("debug_ck_alpha.png", "matte_debug_ck_alpha.png"),
        ("debug_sam_silhouette.png", "matte_debug_sam_silhouette.png"),
        ("debug_chroma_score_raw.png", "matte_debug_chroma_score_raw.png"),
        ("debug_chroma_weight_final.png", "matte_debug_chroma_weight_final.png"),
        ("debug_merge_output.png", "matte_debug_merge_output.png"),
        ("debug_sam_kill_mask.png", "matte_debug_sam_kill_mask.png"),
        ("debug_kill_mask_split.png", "matte_debug_kill_mask_split.png"),
    ]
    for src_name, dst_name in snapshot_pairs:
        src_p = DEBUG_DIR / src_name
        dst_p = DEBUG_DIR / dst_name
        try:
            if src_p.exists():
                _shutil.copy2(str(src_p), str(dst_p))
        except Exception:
            pass
    a = np.asarray(alpha_final, dtype=np.float32)
    final_vis = np.clip(a * 255.0, 0.0, 255.0).astype(np.uint8)
    _cv2.imwrite(str(DEBUG_DIR / "matte_debug_final_displayed.png"), final_vis)
    stats = (
        "=== matte view final-displayed dump ===\n"
        f"ops applied to merge_output before display: {list(ops_applied)}\n"
        f"final alpha shape={a.shape}  "
        f"mean={float(a.mean()):.4f}  "
        f"min={float(a.min()):.4f}  "
        f"max={float(a.max()):.4f}\n"
        "Compare matte_debug_merge_output.png vs matte_debug_final_displayed.png:\n"
        "  - If different: a downstream op killed CK content.\n"
        "  - If identical: the merge inputs (matte_debug_ck_alpha + matte_debug_sam_silhouette) explain the output.\n"
    )
    (DEBUG_DIR / "matte_debug_stats.txt").write_text(stats)
