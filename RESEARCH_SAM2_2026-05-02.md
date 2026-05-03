# SAM2 Research Findings — 2026-05-02

**Source:** Web search + sources cited inline
**Purpose:** Inform the multi-object SAM2 build (PLAN_MULTI_OBJECT_SAM2_2026-05-02.md) with current best practices and emerging tools.

## Key findings

### 1. SAM2 has NATIVE multi-object support — use it, don't reinvent

`SAM2VideoPredictor` accepts an `obj_id` parameter. Multiple objects tracked simultaneously, independent per-object inference, can add new objects after tracking starts.

**Net for our plan:** rewrite plan §3-4 to use SAM2's built-in obj_id propagation instead of running SAM2 twice and unioning the masks. Simpler architecture, less code, native multi-mask scrub for free.

Sources:
- https://github.com/facebookresearch/sam2 — SAM2 official repo
- https://docs.ultralytics.com/models/sam-2/ — Ultralytics SAM2 docs
- https://docs.clore.ai/guides/vision-models/sam2-video — Multi-object workflow guide

### 2. Predictor caching = predictor reuse + reset_predictor()

Reuse a single `SAM2ImagePredictor` instance across prompts. Call `reset_predictor()` between images. Run in inference mode + autocast for speed.

**Net for our plan:** validates the cached-predictor pattern Agent 3 proposed earlier (punchlist task #4). The 2-5s APPLY MASK cost drops to 30-80ms on cache hit.

Sources:
- https://github.com/facebookresearch/sam2 — official caching pattern in examples/
- https://learnopencv.com/sam-2/ — performance walkthrough
- https://medium.com/tier-iv-tech-blog/high-performance-sam2-inference-framework-with-tensorrt-9b01dbab4bf7 — TensorRT optimization for production SAM2

### 3. SAM2-Matte (CVPR 2026) — unified segmentation + matting

Replaces the NN+SAM2 two-step combine with a single model that outputs both segmentation AND alpha matte. Could obsolete CorridorKey's combine layer entirely in a v2.0.

**Status:** paper at https://arxiv.org/abs/2601.12147. Code availability TBD.
**Risk:** licensing unclear, training data unclear. Don't bet v0.8 on it; track for v2.0.

### 4. MatAnyone 2 (CVPR 2026) — AI video matting

Alternative AI matting model. Could replace CorridorKey's NN keyer for non-green-screen footage entirely.

Source: https://studio.aifilms.ai/blog/matanyone-2-video-matting

### 5. No major SAM2 plugin for Resolve / AE / Premiere yet

The market is open. CorridorKey-StuntWorks has a credible shot at being first. Confirms strategy doc's positioning.

Search returned no production-grade SAM2 video-editor plugins. ComfyUI nodes exist but ComfyUI is a separate ecosystem.

### 6. Other relevant projects to track

- **Grounded-SAM-2** (https://github.com/IDEA-Research/Grounded-SAM-2) — combines SAM2 with Grounding DINO for prompt-driven object detection. Auto-locates "person" without manual dots. Future feature: "click an actor name, get the silhouette."
- **Seg2Track-SAM2** (https://arxiv.org/abs/2509.11772) — multi-object tracking + segmentation, zero-shot generalization.
- **SAM2MOT** (https://arxiv.org/html/2504.04519v1) — multi-object tracking by segmentation.
- **Det-SAM2** (https://arxiv.org/html/2411.18977v2) — self-prompting segmentation framework.

## Action items for the multi-object plan

1. **Rewrite §3 (Architecture)** of `PLAN_MULTI_OBJECT_SAM2_2026-05-02.md` to use `SAM2VideoPredictor.add_new_points_or_box(obj_id=...)` instead of two parallel predictor invocations.
2. **Promote SAM2 caching (punchlist #4)** out of "deferred" — research confirms the pattern works and is documented; build it before multi-object since it directly affects APPLY MASK latency that multi-object will double.
3. **Track SAM2-Matte and MatAnyone 2** in a follow-up review when code is released. Could obsolete the entire combine architecture in v2.0.
4. **Don't ship a Grounded-SAM-2-style auto-detect feature** — Berto explicitly rejected auto-detect in past sessions. Keep manual dots.

## Kimi research swarm prompt (for further investigation)

Berto can paste this into the Kimi swarm to get deeper research:

```
You are an AI research swarm helping refine an open-source green-screen plugin
called CorridorKey-StuntWorks (https://github.com/stuntworks/CorridorKey-StuntWorks).

CONTEXT:
- The plugin works in DaVinci Resolve, After Effects, and Premiere Pro.
- It uses Niko Pueringer's CorridorKey neural-net keyer for the matte.
- It integrates SAM2 (Meta's Segment Anything 2) as a "garbage matte" — the
  user clicks dots to define foreground/background, SAM2 produces a silhouette
  that's combined with the NN matte.
- Current single-mask architecture is being upgraded to multi-object (two or
  more independent SAM2 silhouettes per shot — e.g., one for upper body, one
  for feet — each with its own dot prompts and halo controls).
- Current pain points: (1) SAM2 sometimes grabs floor patches near where the
  user placed dots, creating visible artifacts; (2) running SAM2 takes 2-5s
  per click; (3) no way to have separate masks for separate regions.
- Plan file: PLAN_MULTI_OBJECT_SAM2_2026-05-02.md in the repo.

RESEARCH GOALS — find anything published, open-sourced, or written about in
the last 12 months that could help us:

1. SAM2 multi-object best practices. How are people using SAM2VideoPredictor's
   obj_id parameter in production? Any open-source projects we should study?
   What gotchas around per-object propagation across video frames?

2. SAM2 caching / predictor reuse patterns. How do production users avoid the
   2-5s rebuild cost per click? Any benchmarks for cached vs uncached on
   1080p / 4K? Any libraries or wrappers that handle this?

3. SAM2-Matte and MatAnyone 2 (both CVPR 2026). Are either available as code?
   Could they replace the NN+SAM2 combine architecture entirely? What are the
   licensing constraints (Apache, MIT, non-commercial)?

4. SAM2 silhouette quality fixes. Any techniques to reduce false-positive
   "floor patches" SAM2 grabs near dot placements? Connected component
   filtering, chroma-aware trim, NN-matte-aware silhouette refinement?

5. Existing SAM2 plugins for video editors. Has anyone shipped SAM2 in a
   Resolve, AE, or Premiere plugin? What's their UX, what licensing, what
   technical approach? Any failure modes documented?

6. SAM2 alternatives for green-screen workflows. Other models (DEVA, Grounded-
   SAM-2, OneFormer-Video, Depth Anything 2, MatAnyone, etc.) that we should
   consider as a backup or upgrade path.

FOR EACH FINDING return:
- Source link
- One-paragraph summary
- Direct relevance to the plugin (specific section of code or feature it could
  inform)
- Risk level if we adopted it (license, maintenance burden, GPU cost, etc.)

Prioritize findings from the last 6 months. Skip anything older than 2024
unless it's foundational.

Output format: markdown with headings per topic, citations at the end of each
finding. Don't summarize my prompt back to me — just give the findings.
```

## Round 2 findings (2026-05-02 deeper sweep)

### SAM 3 / SAM 3.1 exists

Meta released SAM 3 — successor to SAM 2. Doubles cgF1 scores in concept segmentation performance in videos compared to SAM 2. Faster, more accessible real-time tracking. Major upgrade path for v2.0+.

Source: https://ai.meta.com/blog/segment-anything-model-3/

### Sammie-Roto 2 — the existing competitor product

Open-source GUI for AI rotoscoping / masking on GitHub: https://github.com/Zarxrax/Sammie-Roto-2

Tech stack:
- SAM2 for video segmentation
- MatAnyone + MatAnyone 2 for matting
- VideoMaMa for matting
- MiniMax-Remover for object removal

Active 2026 development: v2.2.0 (MatAnyone 2), v2.3.0 (VideoMaMa), v2.3.1 (live preview during segmentation), v2.3.2 (improved temporal stability), v2.3.3 (perf optimizations).

**This is what CorridorKey-StuntWorks is competing against.** Worth studying for UX patterns and feature gap analysis.

Also: **RotoTrackID** (https://github.com/nameshigawa/RotoTrackID) — YOLO + SAM hybrid for per-object alpha mattes from video. Smaller scope than Sammie-Roto.

### Performance: `vos_optimized=True`

SAM2's `build_sam2_video_predictor(vos_optimized=True)` enables torch.compile of the entire model. Reported as a "major speedup" for video object segmentation. Easy win for our APPLY MASK + SCRUB latency.

Source: https://github.com/facebookresearch/sam2 (README perf section)

### HuggingFace sam2-studio

Official HuggingFace SAM2 wrapper: https://github.com/huggingface/sam2-studio. Cleaner Python APIs over Meta's reference implementation. Worth evaluating as a replacement for our direct facebookresearch/sam2 dependency.

### Blog: SAM2 limits for VFX rotoscoping

https://blog.electricsheep.tv/we-tested-sam2-for-rotoscoping-this-is-what-we-found/

Key quote: "SAM2 falls short for VFX rotoscoping — fine details, multiple mattes, higher resolution edges, temporal consistency."

This is exactly what TWO HALO + multi-object SAM2 + the planned upgrades aim to fix.

### Caching: GitHub issue facebookresearch/sam2 #565

https://github.com/facebookresearch/sam2/issues/565 — "Loading embeddings to speed up video predictions." Direct guidance for the caching task (punchlist #4). Should read before implementing.

### HuggingFace SAM2 demos to study

- EVF-SAM-2 (text prompts → segmentation): https://huggingface.co/spaces/wondervictor/evf-sam2
- Florence2 + SAM2 (vision-language → segmentation): https://huggingface.co/spaces/SkalskiP/florence-sam
- SAM2 Video Predictor + SAM2Long: https://huggingface.co/spaces/Mar2Ding/SAM2Long-Demo

### Awesome list

https://github.com/gaomingqi/Awesome-Video-Object-Segmentation — comprehensive curated list of papers, datasets, projects in video object segmentation. Use for ongoing research scans.

## Action items added (round 2)

5. **Study Sammie-Roto 2 UX** — closest competitor with multi-object + matting + object removal. Inform our v0.8 / v1.0 UX decisions. Don't copy, but understand the bar.
6. **Try `vos_optimized=True`** in panel SAM2 invocation — could be a 10-30 min change for substantial speedup.
7. **Track SAM 3 / 3.1 release** — successor model with major perf gains. Likely v2.0 upgrade path.
8. **Read HuggingFace sam2-studio** repo for cleaner API patterns. Possibly replace direct facebookresearch/sam2 dependency.

## All source links (for reference)

- https://github.com/facebookresearch/sam2
- https://docs.ultralytics.com/models/sam-2/
- https://docs.clore.ai/guides/vision-models/sam2-video
- https://learnopencv.com/sam-2/
- https://github.com/IDEA-Research/Grounded-SAM-2
- https://arxiv.org/abs/2509.11772 — Seg2Track-SAM2
- https://arxiv.org/html/2504.04519v1 — SAM2MOT
- https://arxiv.org/html/2411.18977v2 — Det-SAM2
- https://arxiv.org/abs/2601.12147 — SAM2-Matte (CVPR 2026)
- https://studio.aifilms.ai/blog/matanyone-2-video-matting — MatAnyone 2
- https://medium.com/tier-iv-tech-blog/high-performance-sam2-inference-framework-with-tensorrt-9b01dbab4bf7
- https://huggingface.co/docs/transformers/model_doc/sam2
- https://www.runcomfy.com/comfyui-nodes/ComfyUI_LayerStyle_Advance/layer-mask-sam2-video-ultra

---

## Kimi swarm findings (2026-05-02 evening)

External research swarm fed RESEARCH_BRIEF_MULTI_OBJECT_SAM2.md. Most findings reinforce what we already had. New / actionable items below.

### NEW — DEVA / XMem hybrid for hair flicker

Source: https://github.com/hkchengrex/Tracking-Anything-with-DEVA

SAM2 + XMem hybrid for temporal consistency. SAM2 for initialization / major changes, XMem for frame-to-frame propagation. Reduces hair flicker and floor artifacts via temporal smoothing.

```python
sam2_mask = get_sam2_mask(frame)
xmem_mask = xmem.track(frame, obj_id)
final_mask = sam2_mask * 0.7 + xmem_mask * 0.3
```

Risk: MEDIUM. MIT license. More complex integration but proven in production.

**Action:** Track for v0.9. Don't add to v0.8 multi-object scope.

### NEW — Path to 4K support

CPU offloading reduces VRAM 60% (8.2GB → 3.2GB at 1080p). 720p processing + mask upscaling = 3-4× speedup with minimal quality loss.

Strategy: 720p for preview/scrub, 1080p+ for final render. 4K direct OOMs on 24GB GPU; needs tile-based processing not yet in SAM2.

**Action:** v0.9+ resolution-switching layer.

### NEW — Concrete latency benchmarks (RTX 4090, 1080p)

| Configuration | Latency | FPS | VRAM |
|---|---|---|---|
| Current (uncached) | 2000-5000ms | 0.2-0.5 | ~6GB |
| + Predictor caching | 30-80ms | 12-30 | ~6GB |
| + vos_optimized=True | 20-50ms | 20-50 | ~6GB |
| + CPU offloading | 25-60ms | 16-40 | ~3GB |
| + 720p processing | 8-20ms | 50-125 | ~2GB |
| + TensorRT export | 5-10ms | 100-200 | ~2GB |

**Action:** Confirms punchlist #4 (caching) is the highest-leverage win. APPLY MASK from 2-5s to 30-80ms.

### Reinforced (already in our plan / punchlist)

- `vos_optimized=True` — 30 second change, 2-3× speedup. Already in research doc above.
- Predictor caching via `init_state()` once + reuse — punchlist #4.
- Native `obj_id` API for multi-object — already in PLAN_MULTI_OBJECT_SAM2_2026-05-02.md v2.
- No existing SAM2 plugins for NLEs — confirms market gap. CorridorKey first to market.
- Per-object undo via per-object snapshot stack — pattern from Segment-and-Track-Anything (z-x-yang). Maps to punchlist #5 (Ctrl+Z for SAM2 dots).

### Rejected / wrong fit

- HSV-based chroma masking on top of SAM2 — same trap CorridorKey already hit (alpha-hint HSV trap memory).
- Grounded-SAM-2 auto-detect — Berto explicitly rejected auto-detect UX.
- SAM2-Matte (CVPR 2026) — paper only, no code, monitor for v2.0.
- MatAnyone 2 — wrong problem domain (arbitrary backgrounds, not green-screen).
- Domain-specific retrained segmentation — training overhead unjustified vs SAM2 zero-shot.

### Kimi P0/P1/P2 priority ordering

**P0 (next code session, ~2-3 hr):**
1. `vos_optimized=True` flag — 30 sec change, 2-3× speedup
2. Predictor caching — `init_state()` once on video load
3. Native `obj_id` API — replaces dual-predictor architecture in plan v2

**P1 (this week, ~4-6 hr):**
1. 720p preview + mask upscaling
2. 30-frame batching to prevent OOM
3. Deterministic overlap resolution (higher obj_id wins)

**P2 (v0.9):**
1. SAM2 + XMem / DEVA hybrid for hair flicker
2. TensorRT export pipeline
3. Per-object undo/redo

### Sources added by Kimi swarm

- https://github.com/dinglufe/sam2-video — caching pattern reference
- https://github.com/z-x-yang/Segment-and-Track-Anything — per-object undo/redo state mgmt
- https://github.com/hkchengrex/Tracking-Anything-with-DEVA — XMem hybrid for temporal smoothing
- https://github.com/kadirnar/segment-anything-video — HSV chroma trap example (avoid)
- https://huggingface.co/facebook/sam2-hiera-large — obj_id docs
- https://github.com/facebookresearch/sam2/blob/main/notebooks/video_predictor_example.ipynb — CPU offload notebook
- https://arxiv.org/abs/2401.10214 — domain-specific retraining paper (rejected)
