# RESEARCH BRIEF: Multi-Object SAM2 for CorridorKey

**Target:** Kimi swarm research agents  
**Project:** CorridorKey-StuntWorks (open-source green-screen plugin)

## PROJECT CONTEXT

CorridorKey is an open-source green-screen plugin for DaVinci Resolve, After Effects, and Premiere Pro. The plugin:
- Uses Niko Pueringer's CorridorKey neural-net keyer for base matte generation
- Integrates SAM2 (Meta's Segment Anything 2) as a "garbage matte" to clean up NN imperfections
- Current architecture: one SAM2 silhouette per shot, combined with NN matte
- Architecture being upgraded: multiple independent SAM2 silhouettes per shot (e.g., upper body + feet)

**Current Pain Points:**
1. SAM2 grabs false-positive floor patches near dot placements
2. SAM2 takes 2-5s per click (no caching/reuse)
3. No way to mask separate regions independently

## RESEARCH GOALS

### 1. SAM2 Multi-Object Best Practices
Investigate production usage of SAM2VideoPredictor's `obj_id` parameter:
- How are video editing/vfx tools using multi-object SAM2?
- Per-object propagation patterns across video frames
- Gotchas with object state management and predictor reuse
- Open-source projects using multi-object SAM2 we should study

### 2. SAM2 Caching & Performance Optimization
Find strategies to eliminate 2-5s per-click latency:
- Predictor reuse patterns (frame-to-frame, click-to-click)
- Benchmarks: cached vs uncached on 1080p/4K footage
- Memory vs speed tradeoffs (GPU VRAM usage)
- Existing libraries/wrappers that handle SAM2 caching

### 3. Recent Research: SAM2-Matte & MatAnyone 2 (CVPR 2026)
Investigate these as potential architecture replacements:
- Code availability (GitHub, official releases)
- Licensing constraints (Apache 2.0, MIT, non-commercial)
- Could they replace NN+SAM2 combine layer entirely?
- Quality comparisons vs NN+SAM2 on green-screen footage
- GPU requirements and processing time per frame

### 4. SAM2 Silhouette Quality Improvements
Techniques to reduce false-positive floor patches:
- Connected component filtering post-SAM2
- Chroma-aware trimap refinement
- NN-matte-aware silhouette correction
- Dot placement heuristics to avoid floor confusion
- Post-processing filters that preserve hair detail while removing artifacts

### 5. Existing Video Editor SAM2 Plugins
Research shipped implementations:
- Anyone shipping SAM2 in Resolve/AE/Premiere plugins?
- UX patterns: dot placement, mask visualization, performance
- Technical approaches: locally hosted vs cloud API
- Licensing models and failure modes encountered

### 6. SAM2 Alternatives for Green-Screen
Alternative models to consider:
- DEVA (Video Object Segmentation)
- Grounded-SAM-2 (text-prompted)
- OneFormer-Video
- Depth Anything 2 (depth-aware matting)
- Traditional alternatives: Chroma key algorithms, despill techniques
- Hybrid approaches that could complement or replace SAM2

## OUTPUT FORMAT

For each finding, provide:

**Source link** (URL or citation)

**One-paragraph summary** (what it is, key findings/claims)

**Relevance to CorridorKey** (specific code section or feature it informs)

**Risk assessment** if adopted (license, maintenance burden, GPU cost, implementation complexity)

## PRIORITIES

- Focus on last 6 months (2025-2026) unless foundational
- Prioritize code/projects over papers unless code is available
- Emphasize video/matting use cases over general segmentation
- Note Python/PyTorch implementations (our stack)

## STARTING POINTS

- https://github.com/facebookresearch/sam2
- https://docs.ultralytics.com/models/sam-2/
- https://docs.clore.ai/guides/vision-models/sam2-video
- https://github.com/IDEA-Research/Grounded-SAM-2
- https://arxiv.org/html/2504.04519v1
- https://arxiv.org/abs/2509.11772
- https://arxiv.org/abs/2601.12147
- https://studio.aifilms.ai/blog/matanyone-2-video-matting
- https://medium.com/tier-iv-tech-blog/high-performance-sam2-inference-framework-with-tensorrt-9b01dbab4bf7
- https://arxiv.org/html/2411.18977v2

## PROJECT FILES TO REFERENCE

Our implementation is in `D:\New AI Projects\CorridorKey\`:
- PLAN_MULTI_OBJECT_SAM2_2026-05-02.md (our plan)
- RESEARCH_SAM2_2026-05-02.md (previous research)
- corridorkey_module/sam2_helper.py (current SAM2 integration)
- corridorkey_module/engine.py (NN+SAM2 combine logic)
- ui/davinci/sam2_panel.py (UI for dot placement)

## DELIVERABLE

Markdown document with findings organized by research goal (1-6). Each finding should be immediately actionable for our architecture decisions.
