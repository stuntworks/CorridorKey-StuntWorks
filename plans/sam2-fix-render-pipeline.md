# Blueprint: SAM2 Fix — Render Pipeline Wiring
**Project:** CorridorKey Pro (DaVinci Resolve Plugin)
**Date:** 2026-04-20
**Branch:** main (direct mode — no separate PR branch needed for these 4 changes)
**Status:** REVIEWED — cleared for build

---

## Objective

When the user clicks green/red SAM2 points in the preview viewer and hits APPLY MASK,
those masks must be used when rendering single frames and ranges. Currently they are ignored
and the render falls back to chroma key. This is the main selling point of the product.

---

## Context Brief (read this cold — no prior session needed)

### The two processes

| Process | File | What it does |
|---------|------|-------------|
| Panel (Resolve Fusion) | `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` | Runs inside Resolve. UI, timeline placement, neural network keyer. |
| Viewer (Qt subprocess) | `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py` | Launched by panel with `--session SESSION_DIR`. Live preview. Runs SAM2. |

### Shared session directory

`SESSION_DIR = Path(tempfile.gettempdir()) / f"corridorkey_session_{panel_pid}"`

Both processes read/write files here:
- `fg.png` — keyed foreground (panel writes, viewer reads)
- `alpha.png` — SAM2 mask (viewer writes after APPLY MASK, panel reads at render)
- `meta.json` — frame metadata (panel writes when launching viewer)
- `live_params.json` — slider + SAM2 state (viewer writes, panel reads at render)

### The neural network input

`proc.process_frame(frame_rgb, alpha_hint, settings)` — alpha_hint is the "coarse matte"
the NN refines into a perfect key. SAM2 mask = alpha_hint. This is the architecture.

### Existing code that works — DO NOT TOUCH

- `run_sam2_video_propagation()` — panel line 437. Fully implemented. Exports frames,
  loads SAM2VideoPredictor, calls propagate_in_video(), returns `{range_idx: mask}`.
- PROCESS RANGE loop — panel line 1028. Checks `sam2_video_masks[range_idx]`, uses as alpha_hint.
- `_apply_sam_mask()` — viewer line 1066. Runs SAM2ImagePredictor, writes `alpha.png`.
- `generate_alpha_hint()` — panel line 385. Dispatches to chroma or SAM2 based on alpha_method.

---

## Root Causes (exactly two)

### Root cause A — SAM2 points never reach live_params.json

`_save_live_params_now()` fires only on a slider debounce timer. Clicking APPLY MASK
does NOT trigger it. If the user doesn't move a slider after APPLY MASK, `live_params.json`
never gets the SAM2 click points. Panel reads empty `sam_points` → falls back to chroma key.

### Root cause B — Anchor frame is always None

`sam_points["frame"]` is always None. `run_sam2_video_propagation()` gets
`anchor_frame_abs=None` → defaults `anchor_rel=0` → propagation starts from the FIRST
frame of the range, not the frame the user clicked on. Wrong masks on early frames.

---

## The Fix — 6 Changes (revised from original 4 after architect review)

### Step 1 — Viewer: write alpha_method + SAM2 points explicitly to live_params.json
**File:** `preview_viewer_v2.py`
**Function:** `_save_live_params_now()`
**Change:** When `self._sam_display_pts` is non-empty, add to payload:
- `sam_positive` and `sam_negative` (pixel coords, already planned)
- `alpha_method: 1` — explicit signal so panel knows to use SAM2 mode

When `self._sam_display_pts` is empty, write `sam_positive: []`, `sam_negative: []`.
Do NOT write `alpha_method: 0` here — let the panel fall back on its own UI value.

```python
if self._sam_display_pts:
    ih, iw = self.session.shape_hw
    payload["sam_positive"] = [[int(nx*iw), int(ny*ih)] for nx, ny, v in self._sam_display_pts if v]
    payload["sam_negative"] = [[int(nx*iw), int(ny*ih)] for nx, ny, v in self._sam_display_pts if not v]
    payload["alpha_method"] = 1          # explicit: panel must use SAM2 mode
else:
    payload["sam_positive"] = []
    payload["sam_negative"] = []
    # no alpha_method key — panel uses its UI value
```

NOTE: `_save_live_params_now()` reads `self._sam_display_pts` at call time. Because Qt
runs single-threaded, there is no race condition — every call (timer or direct) reads the
same in-memory list and writes the same result.

---

### Step 2 — Viewer: write anchor frame to live_params.json
**File:** `preview_viewer_v2.py`
**Function:** `_save_live_params_now()`
**Change:** When SAM2 points exist, also write `sam_anchor_frame` from meta.json.

meta.json contains `timeline_frame` = the absolute timeline frame number the panel was on
when it launched the viewer. This is what `run_sam2_video_propagation()` needs as
`anchor_frame_abs` (it compares against `in_f`/`out_f` which are also absolute timeline frames).

```python
if self._sam_display_pts:
    # ... sam_positive, sam_negative, alpha_method as above ...
    try:
        meta_path = self.session.session_dir / "meta.json"
        if meta_path.exists():
            import json as _json
            meta = _json.loads(meta_path.read_text())
            payload["sam_anchor_frame"] = meta.get("timeline_frame")
    except Exception:
        pass   # silently fall back — propagation defaults anchor to range frame 0
```

---

### Step 3 — Viewer: force-save immediately after APPLY MASK
**File:** `preview_viewer_v2.py`
**Function:** `_apply_sam_mask()`
**Change:** After `self._render_now()`, call `self._save_live_params_now()`.

This guarantees the file is written to disk when APPLY MASK finishes, regardless of
whether the user moves a slider afterward.

```python
# At end of _apply_sam_mask(), after self._render_now():
self._save_live_params_now()   # write SAM2 points + anchor NOW, not on timer
self.status.setText(f"SAM2 mask applied — {len(pos_pts)}+ {len(neg_pts)}-")
```

NOTE: Steps 1, 2, 3 are all in the viewer. They are implemented as a single edit to
`preview_viewer_v2.py`. Do NOT split across multiple editing passes.

---

### Step 4 — Panel: read alpha_method + SAM2 points + anchor from live_params.json
**File:** `CorridorKey.py` (installed)
**Function:** `_merge_live_params()`
**Change:** After reading despill/despeckle, also read SAM2 state.

```python
if "sam_positive" in lp or "sam_negative" in lp:
    sam_points["positive"] = [tuple(p) for p in lp.get("sam_positive", [])]
    sam_points["negative"] = [tuple(p) for p in lp.get("sam_negative", [])]
    sam_points["frame"]    = lp.get("sam_anchor_frame", None)
    # Switch to SAM2 mode only when we have an explicit signal from viewer
    if lp.get("alpha_method") == 1:
        out["alpha_method"] = 1
```

Key difference from original plan: only set `alpha_method=1` when the viewer has
EXPLICITLY written it (Step 1). Don't infer from non-empty point lists — points could
be leftovers from a previous session.

---

### Step 5 — Panel: single frame reads alpha.png with frame guard
**File:** `CorridorKey.py` (installed)
**Function:** `generate_alpha_hint()`
**Change:** Replace the `alpha_method == 1` branch with a guarded stamp approach.

**Frame guard is required:** SESSION_DIR persists for the panel process lifetime.
alpha.png could be from a DIFFERENT frame if the user opened PREVIEW multiple times.
Compare `sam_points["frame"]` (the anchor) against the current frame `cf` before using alpha.png.

```python
elif settings["alpha_method"] == 1:
    alpha_png = SESSION_DIR / "alpha.png"
    anchor = sam_points.get("frame")
    # Use stamp only if anchor matches the frame being rendered
    # (anchor == None means frame tracking not set up — fall through to re-run)
    if alpha_png.exists() and anchor is not None:
        # cf is the current timeline frame — must match anchor
        # For single frame: passed as 'cf' in process_current_frame()
        # We access it via settings["_render_frame"] — see Step 6
        render_frame = settings.get("_render_frame")
        if render_frame is None or render_frame == anchor:
            import cv2
            mask = cv2.imread(str(alpha_png), cv2.IMREAD_GRAYSCALE)
            if mask is not None:
                h, w = frame.shape[:2]
                if mask.shape != (h, w):
                    # Threshold BEFORE resize to keep hard edges (avoid gray halos)
                    _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
                    mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
                return mask
    # Fallback: re-run SAM2 from click points
    if sam_points["positive"] or sam_points["negative"]:
        return generate_sam2_mask(frame, sam_points["positive"], sam_points["negative"])
    # Final fallback: chroma key
    from core.alpha_hint_generator import AlphaHintGenerator
    return AlphaHintGenerator(screen_type=settings["screen_type"]).generate_hint(frame)
```

---

### Step 6 — Panel: pass current frame number into settings for generate_alpha_hint
**File:** `CorridorKey.py` (installed)
**Functions:** `process_current_frame()` and `process_range()`
**Change:** Add `_render_frame` to settings before calling `generate_alpha_hint()`.

In `process_current_frame()` (around line 843 where `ah = generate_alpha_hint(frame, settings)`):
```python
settings["_render_frame"] = cf   # inject current timeline frame for SAM2 frame guard
ah = generate_alpha_hint(frame, settings)
```

In `process_range()` PROCESS RANGE loop (around line 1047 where alpha hint is generated
in the fallback path — line 1060):
Not needed for PROCESS RANGE — the SAM2 video propagation path doesn't call
`generate_alpha_hint()`. The loop uses `sam2_video_masks[range_idx]` directly.
Only the fallback `else: ah = generate_alpha_hint(frame, settings)` needs it, and
for that case alpha.png is irrelevant anyway (we're in chroma fallback).

---

## Files Changed

| File | Changes | Notes |
|------|---------|-------|
| `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py` | Steps 1, 2, 3 | NOT in git — edit directly |
| `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` | Steps 4, 5, 6 | Installed file — edit, then cp to repo |
| `D:\New AI Projects\CorridorKey-Plugin\resolve_plugin\CorridorKey_Pro.py` | Copy of above | `cp installed → repo`, then git commit |

---

## Build Order

**Session rule:** Do all edits in one session. Two files. One commit.

1. Edit `preview_viewer_v2.py`: Steps 1 + 2 + 3 together (one Read, then Edit)
2. Edit installed `CorridorKey.py`: Steps 4 + 5 + 6 together (one Read, then Edit)
3. `cp` installed → repo copy
4. `git add resolve_plugin/CorridorKey_Pro.py`
5. `git commit -m "fix: SAM2 wiring — points + anchor + alpha.png reach render pipeline"`
6. Do NOT push until Test Protocol passes

---

## Test Protocol

Run in order. Stop at first failure. Fix before continuing.

### Test A — live_params.json written correctly
1. Open PREVIEW
2. Click CLICK TO MASK (activate), click green on subject, red on background
3. Click APPLY MASK — verify viewer shows masked result
4. Without moving any slider, close viewer
5. Open `SESSION_DIR\live_params.json` in a text editor
6. **Must contain:** `sam_positive`, `sam_negative` (non-empty arrays), `alpha_method: 1`, `sam_anchor_frame` (non-null integer)
7. **Fail:** any of those keys missing or `sam_anchor_frame: null`

### Test B — Single Frame uses SAM2 mask
1. Pass Test A first
2. With live_params.json confirmed correct, click SINGLE FRAME
3. Check panel log — must show `alpha_method` resolving to 1 (add a log line to confirm)
4. Check rendered PNG — edges must be SAM2 hard-cut, not chroma key blur
5. **Fail:** rendered output identical to chroma key result

### Test C — Process Range video propagation
1. Set IN/OUT around a 20–45 frame range that includes the anchor frame
2. Click PROCESS RANGE
3. Check log — must show:
   - `"SAM2 mode — running video propagation for full range..."`
   - `"SAM2 video: N masks ready"` (N = range length)
4. Check rendered frames — subject tracked through range, not just stamped
5. **Fail:** log shows `"SAM2 propagation returned no masks — falling back to chroma hint"`

### Test D — Slider move after APPLY MASK does not lose SAM2 points
1. Click APPLY MASK
2. Move Despill slider
3. Close viewer
4. Inspect live_params.json — SAM2 keys must still be present (not wiped by slider write)
5. **Fail:** `sam_positive` is empty array or key is missing

### Test E — Regression: no SAM2 (chroma key still works)
1. Do NOT open PREVIEW at all
2. Click SINGLE FRAME → must produce chroma key result, no crash
3. Click PROCESS RANGE → must process all frames with chroma key, no SAM2 code hit
4. **Fail:** crash, hang, or empty output

### Test F — Stale alpha.png guard
1. Open PREVIEW on frame 100, APPLY MASK, close viewer
2. Move playhead to frame 200
3. Click SINGLE FRAME (frame 200, different from anchor 100)
4. **Expected:** falls back to click-point re-run OR chroma key (frame guard rejects stale alpha.png)
5. **Fail:** frame 200 silently uses the mask from frame 100 (visual mismatch)

---

## Risk Register

| Risk | Severity | Mitigation in this plan |
|------|----------|------------------------|
| Slider write clears SAM2 points | HIGH | `_save_live_params_now()` always reads `self._sam_display_pts` at call time — single-threaded Qt, no race condition. Every save includes current points if they exist. |
| alpha_method not signaled to panel | HIGH | Step 1 writes `alpha_method: 1` explicitly. Panel reads it in Step 4. |
| Stale alpha.png from previous frame | HIGH | Step 5 frame guard rejects alpha.png if `anchor != render_frame`. |
| Resolution mismatch / gray halos | MEDIUM | Step 5 thresholds to binary BEFORE resize, uses INTER_NEAREST. Hard edges preserved. |
| anchor_frame_abs outside range | MEDIUM | `run_sam2_video_propagation()` already clamps: `if in_f <= anchor_frame_abs < out_f else 0`. Confirmed in code (line 446–449). |
| meta.json missing timeline_frame | LOW | Step 2 uses `.get("timeline_frame")` — returns None silently. Propagation falls back to anchor_rel=0. |

---

## What This Plan Explicitly Does NOT Cover

- Backward propagation (from anchor toward start of range) — SAM2VideoPredictor handles this automatically when you call propagate_in_video() without specifying direction. Nothing to build.
- Multiple SAM2 sessions / multiple anchor frames — future feature.
- Mask persistence cache (skip re-running SAM2 on second render) — future feature.
- Progress bar for SAM2 propagation — status() calls already provide updates. Good enough.
- Resolve API version differences — existing code handles this. Do not touch.

---

## Rollback

All changes are in two files. If anything breaks:
1. `git stash` or `git checkout` to restore `CorridorKey_Pro.py` in repo
2. `cp` repo copy back to installed: `cp D:\...\CorridorKey_Pro.py "C:\ProgramData\...\CorridorKey.py"`
3. For viewer (not in git): restore from backup at `D:\...\preview_viewer_v2.py.bak` (make backup before editing)

---

**CLEARED FOR BUILD. Berto must say "go" before any code is written.**
