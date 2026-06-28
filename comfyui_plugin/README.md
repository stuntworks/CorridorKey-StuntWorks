# CorridorKey — StuntWorks Cinema build (ComfyUI)

AI green screen keyer for ComfyUI. The neural **CorridorKey** chroma key plus the
**green-aware garbage matte** — a clean key *and* clean junk removal, the same
shared pipeline that ships in the StuntWorks DaVinci Resolve and After Effects tools.

> **Credits.** The CorridorKey engine was created by **Niko Pueringer / Corridor Digital**
> ([GitHub](https://github.com/nikopueringer/CorridorKey) · [corridordigital.com](https://corridordigital.com)).
> Licensed **CC BY-NC-SA 4.0 (NonCommercial)** — this build is **free and cannot be sold**.
> ComfyUI plugin by **Roberto & Elvis Lopez / StuntWorks Cinema**.
> Tutorials on YouTube: **[@StuntWorksCinema](https://www.youtube.com/@StuntWorksCinema)**.

---

## What makes this build unique

Other ComfyUI CorridorKey nodes expose only the raw neural keyer — you bring the mask.
This build ships the **green-aware garbage matte**: it combines

1. the CorridorKey neural chroma key,
2. a SAM2 subject silhouette (bring your own — any ComfyUI segmentation node), and
3. the **green-screen geography** (where the screen actually is).

Result: the set is cut cleaner than a raw subject mask can manage, and the key stays
locked to the subject on every frame. It is the *exact same merge code*
(`merge_ck_with_garbage_matte`) as the DaVinci/AE hosts — not a reimplementation.

## Nodes

| Node | In | Out |
|------|----|-----|
| **CorridorKey Loader** | checkpoint, device, img_size, refiner | `CK_MODEL` |
| **CorridorKey Keyer** | `CK_MODEL`, IMAGE, screen_type, despill, refiner, despeckle, *(optional)* alpha_hint MASK | IMAGE (fg), MASK (ck_alpha) |
| **CorridorKey Garbage Merge** | IMAGE, ck_alpha MASK, sam_mask MASK, screen_type | MASK (clean_alpha), MASK (**green-aware garbage_matte**, white = junk) |
| **CorridorKey About** | — | STRING (credits + links) |

If you don't feed the Keyer an `alpha_hint`, it auto-generates an HSV chroma hint
(same detector as the DaVinci/AE hosts), so the Keyer works one-click.

## Typical graph

```
Load Video/Image ─► CorridorKey Loader ─┐
                                        ├─► CorridorKey Keyer ─► fg (IMAGE)
                    (source IMAGE) ──────┘                    └─► ck_alpha (MASK) ─┐
                                                                                   │
SAM2 node ─► sam_mask (MASK) ──────────────────────────────────────────────┐      │
source IMAGE ──────────────────────────────────────────────────────────────┴──────┴─► CorridorKey Garbage Merge
                                                                                          ├─► clean_alpha (MASK)  ← perfect key
                                                                                          └─► garbage_matte (MASK) ← set-cut holdout
```

`clean_alpha` is the perfect key; `garbage_matte` is the green-aware holdout (white = junk).

## Install

1. Copy this folder into `ComfyUI/custom_nodes/` (e.g. `ComfyUI/custom_nodes/comfyui-corridorkey-stuntworks`).
2. Put the CorridorKey model at `ComfyUI/models/corridorkey/CorridorKey_v1.0.safetensors`.
3. Point the nodes at the CorridorKey engine repo (the folder containing `CorridorKeyModule/`):
   set the `CORRIDORKEY_ROOT` environment variable, **or** drop a `corridorkey_path.txt`
   next to the node containing that path.
4. Restart ComfyUI.

Requires a CUDA GPU (4GB+ VRAM; ~23GB at native 2048×2048). SAM2 is *not* bundled —
use any ComfyUI SAM2 / segmentation node to produce `sam_mask`.

## About / links

Open **Help → About CorridorKey (StuntWorks)** in ComfyUI, or drop a **CorridorKey About**
node, for credits and clickable links (Corridor Digital, StuntWorks Cinema YouTube, Ko-fi).

- Engine: Niko Pueringer / Corridor Digital — https://github.com/nikopueringer/CorridorKey
- Niko / Corridor on YouTube: https://www.youtube.com/@CorridorDigital
- Plugin: StuntWorks Cinema — https://github.com/stuntworks/CorridorKey-Plugin
- Tutorials: https://www.youtube.com/@StuntWorksCinema
- Support: https://ko-fi.com/stuntworks

SAM2 (Segment Anything Model 2) © Meta AI, used under Apache 2.0.
