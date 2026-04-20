# SAM2 Fix Plan — CorridorKey Pro
**Date:** 2026-04-20
**Status:** APPROVED — do not touch code until Berto confirms this plan

---

## The Feature (Non-Negotiable)

Click green on what you want. Click red on what you don't.
Hit APPLY MASK. SAM2 tracks the subject through every frame in the range.
The NN uses those masks to produce a perfect key on every frame.
This is the main selling point. It must work exactly like Magic Mask in Resolve.

---

## What Already Exists (Do Not Break)

| Component | File | Status |
|-----------|------|--------|
| `run_sam2_video_propagation()` | CorridorKey.py line 437 | FULLY BUILT — exports frames, loads SAM2VideoPredictor, propagates, returns per-frame masks |
| `_apply_sam_mask()` | preview_viewer_v2.py line 1066 | WORKS — runs SAM2ImagePredictor, writes `alpha.png` to session dir |
| Per-frame mask loop | CorridorKey.py line 1028–1058 | WORKS — checks `sam2_video_masks[range_idx]`, uses it as alpha hint for NN |
| Session dir sharing | panel launches viewer with `--session SESSION_DIR` | WORKS — both processes use the same temp folder |

The architecture is correct. The pipeline code exists. The wiring is broken in two specific places.

---

## Root Cause (Exactly Two Problems)

### Problem 1 — SAM2 points never reach live_params.json

`_save_live_params_now()` in the viewer fires on a debounce timer after **slider changes only**.
When the user clicks APPLY MASK, SAM2 runs and the result is shown in the viewer — but the
save timer does NOT fire. If the user closes the viewer without moving a slider, `live_params.json`
never contains the SAM2 click points.

Panel reads `live_params.json` at render time → `sam_points` is empty → `alpha_method` stays 0
→ falls back to chroma key → SAM2 is never used.

### Problem 2 — Anchor frame is always None

`sam_points["frame"]` is set to `None` (our earlier fix). `run_sam2_video_propagation()`
receives `anchor_frame_abs=None` → defaults to `anchor_rel=0` → propagation anchors to the
FIRST frame of the range, not the frame the user actually clicked on.

If the user clicks points while looking at frame 500, and the range is 480–530, propagation
starts from frame 480 instead of frame 500. The mask on early frames is wrong because the
anchor is in the wrong place.

---

## The Fix — 4 Small Changes

### Change 1 — Viewer: force-save immediately after APPLY MASK
**File:** `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py`
**Where:** `_apply_sam_mask()` — after the call to `self._render_now()` on line ~1112
**What:** Add `self._save_live_params_now()` immediately after render.

This guarantees the SAM2 click points are in `live_params.json` the moment APPLY MASK finishes,
regardless of whether the user moves a slider afterward.

```python
# After self._render_now() in _apply_sam_mask():
self._save_live_params_now()   # write points to disk NOW, don't wait for timer
```

---

### Change 2 — Viewer: write the anchor frame number
**File:** `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py`
**Where:** `_save_live_params_now()` — add to payload alongside sam_positive/sam_negative
**What:** Read the current frame number from `meta.json` and write it as `sam_anchor_frame`.

`meta.json` is written by the panel when it launches the viewer. It contains `frame_num`
(the source-video frame offset) and `timeline_frame` (the absolute timeline frame).
We want `timeline_frame` — that's the value `run_sam2_video_propagation()` receives as
`anchor_frame_abs` and compares against `in_f`/`out_f`.

```python
# In _save_live_params_now(), inside the if self._sam_display_pts: block:
try:
    meta_path = self.session.session_dir / "meta.json"
    if meta_path.exists():
        import json as _json
        meta = _json.loads(meta_path.read_text())
        payload["sam_anchor_frame"] = meta.get("timeline_frame")
except Exception:
    pass
```

---

### Change 3 — Panel: read sam_anchor_frame from live_params.json
**File:** `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
**Where:** `_merge_live_params()` — where we already read sam_positive/sam_negative (line ~325)
**What:** Also read `sam_anchor_frame` and store in `sam_points["frame"]`.

```python
# Replace:
sam_points["frame"] = None
# With:
sam_points["frame"] = lp.get("sam_anchor_frame", None)
```

---

### Change 4 — Panel: single frame uses alpha.png directly
**File:** `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py`
**Where:** `generate_alpha_hint()` — the `alpha_method == 1` branch (line ~390)
**What:** For single frame render, read `SESSION_DIR/alpha.png` directly instead of re-running
SAM2. The viewer already computed and saved this mask. Re-running SAM2 from click points is
slow (loads 300MB model), fragile (requires exact same frame), and unnecessary.

```python
elif settings["alpha_method"] == 1:
    # Try the mask the viewer already saved first — fast path, no re-run
    alpha_png = SESSION_DIR / "alpha.png"
    if alpha_png.exists():
        import cv2
        mask = cv2.imread(str(alpha_png), cv2.IMREAD_GRAYSCALE)
        if mask is not None:
            h, w = frame.shape[:2]
            if mask.shape != (h, w):
                mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
            return mask
    # Fallback: re-run SAM2 from click points if alpha.png is missing
    if sam_points["positive"] or sam_points["negative"]:
        return generate_sam2_mask(frame, sam_points["positive"], sam_points["negative"])
    # Final fallback: chroma key
    from core.alpha_hint_generator import AlphaHintGenerator
    return AlphaHintGenerator(screen_type=settings["screen_type"]).generate_hint(frame)
```

---

## What Does NOT Change

- `run_sam2_video_propagation()` — do not touch, already correct
- `_apply_sam_mask()` — only add one line at the end (Change 1)
- All timeline placement code — do not touch
- Frame extraction / VideoCapture logic — do not touch
- The NN processor — do not touch
- PROCESS RANGE loop — do not touch

---

## Build Order

Do all 4 changes in one session. They depend on each other:

1. Change 2 before Change 3 (write before read)
2. Change 1 before testing (points must reach disk)
3. Change 4 is independent but do it in the same pass

**Commit order:**
1. Edit `preview_viewer_v2.py` (Changes 1 + 2 together — one file, one commit)
2. Edit `CorridorKey.py` installed (Changes 3 + 4 together — one file, one commit)
3. `cp` installed file to repo copy
4. `git add resolve_plugin/CorridorKey_Pro.py && git commit`
5. Single final commit message: "fix: SAM2 points + anchor frame reach render pipeline"

---

## Test Protocol (in order, stop if any step fails)

### Test A — Single Frame
1. Open PREVIEW on a green screen frame
2. Click CLICK TO MASK (activate)
3. Click green on the actor, red on the background
4. Click APPLY MASK — verify preview shows correct mask
5. Close viewer
6. Click SINGLE FRAME
7. **Expected:** rendered PNG uses SAM2 mask edges, not chroma key blur
8. **Fail signal:** output looks identical to chroma key (no improvement on edges)

### Test B — Process Range
1. Same setup as Test A — click points, APPLY MASK, close viewer
2. Set IN and OUT points around your test range
3. Click PROCESS RANGE
4. **Expected:** log shows "SAM2 mode — running video propagation" then "SAM2 video: N masks ready"
5. **Expected:** rendered frames track the subject through the range
6. **Fail signal:** log shows "SAM2 propagation returned no masks — falling back to chroma hint"

### Test C — No SAM2 (regression)
1. Do NOT open the viewer
2. Click SINGLE FRAME → must still produce a chroma key result (no crash, no hang)
3. Click PROCESS RANGE → must still process all frames with chroma key (no SAM2 code path hit)

### Test D — APPLY MASK then slider move then render
1. Click points, APPLY MASK
2. Move the Despill slider slightly
3. Click SINGLE FRAME
4. **Expected:** SAM2 mask still used (not lost because slider triggered a live_params.json overwrite)
5. This tests that the slider write does not clear the SAM2 points we just wrote

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Slider move overwrites live_params.json and clears SAM2 points | MEDIUM | `_save_live_params_now()` always writes both slider values AND sam points (our Change 1/2 write into the same payload dict) |
| `meta.json` doesn't contain `timeline_frame` | LOW | Change 2 uses `.get("timeline_frame")` which returns None safely — propagation falls back to anchor_rel=0 |
| `alpha.png` in SESSION_DIR is stale from a previous frame | LOW | Test D catches this. Long-term fix: store the timeline frame in meta.json and verify it matches before using the cached mask |
| VRAM full during SAM2 video propagation | LOW | `offload_video_to_cpu=True` already set in `run_sam2_video_propagation()` |

---

## What We Do NOT Build Yet

- Backward propagation (from anchor toward frame 0 of range) — SAM2VideoPredictor handles this automatically, nothing to build
- Multiple anchor frames / multi-click sessions — future feature
- Mask cache between sessions (re-use masks without re-running SAM2) — future feature
- Progress bar for SAM2 propagation — status() calls already exist, good enough for now

---

**STOP. Do not write any code until Berto reads this and says go.**
