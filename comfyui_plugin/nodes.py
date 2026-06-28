"""CorridorKey StuntWorks — ComfyUI nodes.

Thin tensor-adapter shell over the SHARED CorridorKey engine + merge code
(the same Python the DaVinci/AE/Premiere hosts call). No keying logic is
reimplemented here — that is the whole point: identical clean key + green-aware
garbage matte across every host.

CorridorKey engine (c) Niko Pueringer / Corridor Digital, CC BY-NC-SA 4.0.
ComfyUI plugin by Roberto & Elvis Lopez / StuntWorks Cinema. See about.py.
"""
from __future__ import annotations

import logging

import numpy as np

from . import engine_bridge as eb
from .about import ABOUT_TEXT, LINK_YOUTUBE, LINK_KOFI, LINK_PLUGIN_GITHUB

logger = logging.getLogger("corridorkey_sw")

SCREEN_TYPES = ["green", "blue"]


# ── checkpoint discovery via ComfyUI folder_paths (with graceful fallback) ──
def _checkpoint_choices():
    try:
        import os
        import folder_paths  # provided by ComfyUI at runtime

        ck_dir = os.path.join(folder_paths.models_dir, "corridorkey")
        os.makedirs(ck_dir, exist_ok=True)
        try:
            folder_paths.add_model_folder_path("corridorkey", ck_dir)
        except Exception:
            pass
        names = []
        for ext in (".safetensors", ".pth"):
            try:
                names += [f for f in folder_paths.get_filename_list("corridorkey") if f.endswith(ext)]
            except Exception:
                pass
        # de-dupe, keep order
        seen, out = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out or ["(place a .safetensors in ComfyUI/models/corridorkey)"]
    except Exception:
        return ["(ComfyUI folder_paths unavailable)"]


def _resolve_checkpoint_path(name: str) -> str:
    import os

    try:
        import folder_paths

        p = folder_paths.get_full_path("corridorkey", name)
        if p:
            return p
        return os.path.join(folder_paths.models_dir, "corridorkey", name)
    except Exception:
        return name


def _default_device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# ── engine cache — keep the model loaded across runs (load is 30-60s) ──
_ENGINE_CACHE: dict = {}


def _get_engine(ckpt_path: str, device: str, img_size: int, use_refiner: bool):
    key = (ckpt_path, device, int(img_size), bool(use_refiner))
    eng = _ENGINE_CACHE.get(key)
    if eng is not None:
        return eng
    EngineClass = eb.get_engine_class()
    eng = EngineClass(
        checkpoint_path=ckpt_path,
        device=device,
        img_size=int(img_size),
        use_refiner=bool(use_refiner),
    )
    _ENGINE_CACHE.clear()  # only ever hold one engine (VRAM)
    _ENGINE_CACHE[key] = eng
    return eng


class CorridorKeySWLoader:
    """Load the CorridorKey neural model once and hand it to the Keyer."""

    CATEGORY = "CorridorKey"
    RETURN_TYPES = ("CK_MODEL",)
    RETURN_NAMES = ("ck_model",)
    FUNCTION = "load"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "checkpoint": (_checkpoint_choices(),),
                "device": ([_default_device(), "cuda", "cpu"],),
                "img_size": ("INT", {"default": 2048, "min": 512, "max": 4096, "step": 64}),
                "use_refiner": ("BOOLEAN", {"default": True}),
            }
        }

    def load(self, checkpoint, device, img_size, use_refiner):
        ckpt_path = _resolve_checkpoint_path(checkpoint)
        eng = _get_engine(ckpt_path, device, img_size, use_refiner)
        return (eng,)


class CorridorKeySWKeyer:
    """Run the CorridorKey neural keyer. RGB in -> despilled FG + alpha matte.

    If no alpha_hint mask is supplied, an HSV chroma hint is auto-generated
    (same detector as the DaVinci/AE hosts) so this works one-click."""

    CATEGORY = "CorridorKey"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("fg", "ck_alpha")
    FUNCTION = "key"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "ck_model": ("CK_MODEL",),
                "image": ("IMAGE",),
                "screen_type": (SCREEN_TYPES,),
                "despill": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05}),
                "refiner_scale": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.1}),
                "auto_despeckle": ("BOOLEAN", {"default": True}),
                "despeckle_size": ("INT", {"default": 400, "min": 0, "max": 5000, "step": 50}),
            },
            "optional": {
                "alpha_hint": ("MASK",),
            },
        }

    def key(self, ck_model, image, screen_type, despill, refiner_scale,
            auto_despeckle, despeckle_size, alpha_hint=None):
        frames = eb.image_to_numpy_frames(image)
        hints = eb.mask_to_numpy_frames(alpha_hint) if alpha_hint is not None else None

        fg_out, alpha_out = [], []
        for i, img in enumerate(frames):
            if hints is not None:
                hint = hints[i] if i < len(hints) else hints[-1]
                if hint.shape != img.shape[:2]:
                    import cv2
                    hint = cv2.resize(hint, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
            else:
                hint = eb.generate_chroma_hint(img, screen_type)

            result = ck_model.process_frame(
                img,
                hint.astype(np.float32),
                refiner_scale=float(refiner_scale),
                input_is_linear=False,
                despill_strength=float(despill),
                auto_despeckle=bool(auto_despeckle),
                despeckle_size=int(despeckle_size),
                generate_comp=False,
            )
            fg = np.clip(np.asarray(result["fg"], dtype=np.float32), 0.0, 1.0)
            # despeckled alpha lives in processed[...,3]; raw NN alpha is result["alpha"]
            if auto_despeckle and result.get("processed") is not None:
                alpha = np.asarray(result["processed"], dtype=np.float32)[..., 3]
            else:
                a = np.asarray(result["alpha"], dtype=np.float32)
                alpha = a[..., 0] if a.ndim == 3 else a
            fg_out.append(fg[..., :3])
            alpha_out.append(np.clip(alpha, 0.0, 1.0))

        return (eb.numpy_frames_to_image(fg_out), eb.numpy_masks_to_mask(alpha_out))


class CorridorKeySWGarbageMerge:
    """The green-aware garbage matte — the StuntWorks clean-junk pipeline.
    Mirrors the DaVinci host exactly (same shared functions):

      SAM mask -> process_sam_matte (MARGIN / SOFTEN)
               -> merge_ck_with_sam_active (green-aware: CK on green, SAM off-green)
               -> compute_garbage_matte (LOOSE dilated holdout: gives the subject
                  room so the wire/hair isn't choked, AND zeroes the off-green
                  satellite speckles/polka-dots outside the holdout)
               -> small-island despeckle (kills any leftover detached dots)

    Outputs the clean keyed alpha AND the loose holdout (white = keep region).
    Bring the SAM mask from any ComfyUI segmentation node. Feed optional
    tweak_add / tweak_subtract masks (paint or animate upstream) to adjust the
    holdout per shot or per frame — the ComfyUI stand-in for AE keyframing."""

    CATEGORY = "CorridorKey"
    RETURN_TYPES = ("MASK", "MASK")
    RETURN_NAMES = ("clean_alpha", "holdout")
    FUNCTION = "merge"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "ck_alpha": ("MASK",),
                "sam_mask": ("MASK",),
                "screen_type": (SCREEN_TYPES,),
                # GREEN-AWARE holdout: loose ONLY over green, tight where there's no green.
                # garbage_grow = looseness on green (keeps wire/hair). offgreen_grow =
                # hug-tight off green (kills the dirt near the subject past the screen edge).
                "garbage_grow": ("INT", {"default": 30, "min": 0, "max": 200, "step": 1}),
                "offgreen_grow": ("INT", {"default": 3, "min": 0, "max": 50, "step": 1}),
                "garbage_feather": ("INT", {"default": 8, "min": 0, "max": 30, "step": 1}),
                "y_top_pct": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "y_bot_pct": ("INT", {"default": 100, "min": 0, "max": 100, "step": 1}),
                # SAM looseness (DaVinci MARGIN / SOFTEN)
                "sam_margin": ("FLOAT", {"default": 0.0, "min": -50.0, "max": 50.0, "step": 1.0}),
                "sam_soften": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 30.0, "step": 0.5}),
                # leftover-dot cleanup
                "despeckle_dots": ("BOOLEAN", {"default": True}),
                "dot_size_px": ("INT", {"default": 200, "min": 0, "max": 5000, "step": 50}),
            },
            "optional": {
                "tweak_add": ("MASK",),       # union into the holdout (paint/animate)
                "tweak_subtract": ("MASK",),  # cut out of the holdout
                # DaVinci/AE matte-quality parity (hair-safe):
                # fill_holes  -> close enclosed holes inside the SAM body (hair edge untouched)
                # kill_loose_junk -> drop junk NOT connected to the body, by connectivity
                #                    (NOT erosion) so attached wire/hair survives
                # junk_thickness -> structures fatter than this (off-body) get opened away
                "fill_holes": ("BOOLEAN", {"default": True}),
                "kill_loose_junk": ("BOOLEAN", {"default": True}),
                "junk_thickness": ("INT", {"default": 8, "min": 0, "max": 50, "step": 1}),
                "shirt_rescue": ("BOOLEAN", {"default": True}),
            },
        }

    def merge(self, image, ck_alpha, sam_mask, screen_type, garbage_grow, offgreen_grow,
              garbage_feather, y_top_pct, y_bot_pct, sam_margin, sam_soften,
              despeckle_dots, dot_size_px, tweak_add=None, tweak_subtract=None,
              fill_holes=True, kill_loose_junk=True, junk_thickness=8, shirt_rescue=True):
        tk = eb.get_matte_toolkit()
        merge_active = tk["merge_active"]
        process_sam_matte = tk["process_sam_matte"]
        compute_garbage_matte = tk["compute_garbage_matte"]

        srcs = eb.image_to_numpy_frames(image)
        cks = eb.mask_to_numpy_frames(ck_alpha)
        sams = eb.mask_to_numpy_frames(sam_mask)
        adds = eb.mask_to_numpy_frames(tweak_add) if tweak_add is not None else None
        subs = eb.mask_to_numpy_frames(tweak_subtract) if tweak_subtract is not None else None
        n = max(len(srcs), len(cks), len(sams))

        clean_out, hold_out = [], []
        for i in range(n):
            src = srcs[i] if i < len(srcs) else srcs[-1]
            ck = cks[i] if i < len(cks) else cks[-1]
            sam = sams[i] if i < len(sams) else sams[-1]
            h, w = src.shape[:2]
            ck, sam = _match_hw(ck, h, w), _match_hw(sam, h, w)

            # optional operator tweak (paint/keyframe): add room or cut junk
            if adds is not None:
                a = _match_hw(adds[i] if i < len(adds) else adds[-1], h, w)
                sam = np.maximum(sam, a)
            if subs is not None:
                s = _match_hw(subs[i] if i < len(subs) else subs[-1], h, w)
                sam = sam * (1.0 - np.clip(s, 0.0, 1.0))

            # MARGIN / SOFTEN on the SAM silhouette
            sam_proc = process_sam_matte(sam.astype(np.float32),
                                         margin_px=float(sam_margin),
                                         softness_sigma=float(sam_soften))

            # green-aware merge (CK detail on green, SAM carries off-green)
            res = merge_active(ck.astype(np.float32), sam_proc.astype(np.float32),
                               src.astype(np.float32), screen_type=screen_type,
                               return_garbage=True)
            final = res[0] if isinstance(res, tuple) else res

            # GREEN-AWARE holdout (Berto's rule): loose only over green, tight off green.
            # Build a loose holdout AND a tight holdout, then blend by the green map so
            # off-green pixels hug the silhouette (kills dirt past the screen edge) while
            # on-green keeps the room for wire/hair.
            loose = compute_garbage_matte(sam_proc, expand_px=int(garbage_grow),
                                          feather_px=int(garbage_feather),
                                          y_top_pct=int(y_top_pct), y_bot_pct=int(y_bot_pct),
                                          crop_mode="body")
            tight = compute_garbage_matte(sam_proc, expand_px=int(offgreen_grow),
                                          feather_px=int(garbage_feather),
                                          y_top_pct=int(y_top_pct), y_bot_pct=int(y_bot_pct),
                                          crop_mode="body")
            if loose is None or tight is None:
                holdout = np.ones((h, w), dtype=np.float32)
            else:
                g = _green_map(src, screen_type)            # 1 = green behind, 0 = no green
                holdout = np.clip(tight + (loose - tight) * g, 0.0, 1.0).astype(np.float32)
            final = np.clip(final * holdout, 0.0, 1.0)

            if shirt_rescue:
                final = eb.shirt_rescue(final, sam_proc, src)

            # DaVinci/AE matte-quality parity (hair-safe), same order as the hosts:
            # 1) fill enclosed holes inside the SAM body (soft hair edge untouched)
            if fill_holes:
                final = eb.fill_body_holes(final, sam_proc)
            # 2) kill junk NOT connected to the body by connectivity, not erosion
            if kill_loose_junk:
                keep = eb.kill_loose_junk_keepmask(final, sam_proc, w, int(junk_thickness))
                if keep is not None:
                    final = np.clip(final * keep, 0.0, 1.0)

            # kill any leftover detached polka-dots (small islands)
            if despeckle_dots and dot_size_px > 0:
                final = _despeckle_islands(final, int(dot_size_px))

            clean_out.append(final.astype(np.float32))
            hold_out.append(np.clip(holdout, 0.0, 1.0).astype(np.float32))

        return (eb.numpy_masks_to_mask(clean_out), eb.numpy_masks_to_mask(hold_out))


def _green_map(src_rgb: np.ndarray, screen_type: str) -> np.ndarray:
    """Soft 0..1 map: 1 where the green/blue screen is behind, 0 where there is none.
    Same HSV detect + close/dilate/blur the shared merge uses for G_soft, so the
    holdout is loose only where the screen actually exists."""
    import cv2

    u8 = (np.clip(src_rgb, 0.0, 1.0) * 255).astype(np.uint8)
    if u8.ndim == 2:
        u8 = np.stack([u8, u8, u8], axis=-1)
    h, w = u8.shape[:2]
    scale = float(w) / 1920.0
    hsv = cv2.cvtColor(cv2.cvtColor(u8, cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2HSV)
    if screen_type == "blue":
        lo, hi = np.array([100, 50, 50]), np.array([130, 255, 255])
    else:
        lo, hi = np.array([35, 50, 50]), np.array([85, 255, 255])
    binar = cv2.inRange(hsv, lo, hi)
    rc = max(3, int(round(9 * scale)) | 1)
    rd = max(3, int(round(15 * scale)) | 1)
    sg = max(1.0, 8.0 * scale)
    binar = cv2.morphologyEx(binar, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (rc, rc)))
    binar = cv2.dilate(binar, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (rd, rd)))
    g = cv2.GaussianBlur(binar.astype(np.float32) / 255.0, (0, 0), sg)
    return np.clip(g, 0.0, 1.0)


def _despeckle_islands(alpha: np.ndarray, min_area_px: int) -> np.ndarray:
    """Zero connected components smaller than min_area_px. Detached off-green
    speckles die; the subject (one big component, hair attached) survives."""
    import cv2

    a = np.clip(alpha, 0.0, 1.0).astype(np.float32)
    binar = (a > 0.1).astype(np.uint8)
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(binar, connectivity=8)
    if n <= 2:
        return a
    keep = np.zeros_like(binar)
    for ci in range(1, n):
        if stats[ci, cv2.CC_STAT_AREA] >= min_area_px:
            keep[lbl == ci] = 1
    return (a * keep).astype(np.float32)


class CorridorKeySWAbout:
    """About / credits / links for CorridorKey StuntWorks. Outputs the about
    text as a STRING so it can be wired to a display/Note node in-graph."""

    CATEGORY = "CorridorKey"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("about",)
    FUNCTION = "about"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def about(self):
        text = (
            ABOUT_TEXT
            + f"\nYouTube: {LINK_YOUTUBE}\nKo-fi: {LINK_KOFI}\nPlugin: {LINK_PLUGIN_GITHUB}\n"
        )
        return {"ui": {"text": [text]}, "result": (text,)}


def _match_hw(arr: np.ndarray, h: int, w: int) -> np.ndarray:
    if arr.shape[:2] == (h, w):
        return arr
    import cv2

    return cv2.resize(arr.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
