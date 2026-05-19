# Handoff: SAM2 matte banding in DaVinci CorridorKey viewer

## The bug to fix

After clicking **APPLY MASK** in the live preview viewer, the **Matte view** shows the body silhouette with **horizontal gray banding inside the body**, especially in the midsection (skirt/hips area). The user (Berto) needs the body interior to be **solid white** with real holes (only) shown as black.

The banding is NOT in the NN keyer's alpha matte by itself. The user verified this:
- **Without SAM2 (no Apply Mask):** body is SOLID WHITE in Matte view — perfect.
- **With SAM2 (after Apply Mask):** body has horizontal gray banding/translucency inside, especially midsection.

So the `alpha = alpha_raw × sam2_gate` multiplication is what introduces the banding. Same alpha is used in Composite and PROCESS RANGE — meaning the rendered output to disk would also have semi-transparent body. The matte view is just the most visible symptom.

## What "good" looks like

User screenshot of NN-keyer-alone matte (no SAM2):
- Solid white body silhouette including midsection
- Real holes (alpha < 0.3 regions) visible as black
- Soft edges from motion blur
- Path: `C:\Users\ragsn\Pictures\Screenshots 1\Screenshot 2026-04-26 221651.png`

User screenshot of post-SAM2 matte (the bug):
- Solid white legs, arms, hair
- Horizontal banding/translucency in midsection
- Path: `C:\Users\ragsn\Pictures\Screenshots 1\Screenshot 2026-04-26 222000.png`

User screenshot of historical "clean" matte (April 17, on different HD clip):
- Solid white body with clean dark hole visible
- Path: `C:\Users\ragsn\Pictures\Screenshots 1\Screenshot 2026-04-17 115100.png`

## Diagnostic evidence already gathered

A previous agent ran SAM2 directly on the test image and saved diagnostic PNGs to `C:\Users\ragsn\AppData\Local\Temp\sam2_diag\`. Findings:

- `sigmoid(masks[best_idx])` (full-res, 4K) — interior mean 0.973, std 0.026 (numerically cleanest)
- `sigmoid(low_res_masks[best_idx])` upscaled bilinearly — interior mean 0.959, std 0.047 (more banding numerically)
- BOTH paths produce visible banding to the user, just different patterns:
  - Full-res `masks` → "waffle" / checkerboard pattern (worse visually)
  - 256×256 `low_res_masks` upscaled → horizontal bands in midsection (current state)

Another agent traced the render pipeline stage-by-stage and saved intermediates to `C:\Users\ragsn\AppData\Local\Temp\matte_diag\`. Found banding score (std of row-mean luminance inside body bbox):
- `alpha_raw` (NN matte alone): 0.287
- `gate` (SAM2 mask, after resize): 0.190
- `product` (alpha_raw × gate): 0.173
- final displayed: 0.173

Note: Agent 3's measurement of alpha_raw banding 0.287 contradicts the user's visual observation that NN-alone is solid. May be a stale alpha.png from before the test, or measurement artifact. **Trust the user's screenshot — alpha_raw IS solid white in body when SAM2 isn't multiplied in.**

## Test setup to reproduce

- **Resolve project:** Berto's stunt footage timeline with 4K BRAW clip "earl green fall.braw" (or similar)
- **Engine:** `D:\New AI Projects\CorridorKey\` (engine venv at `.venv\Scripts\python.exe`)
- **DaVinci plugin:** `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility\CorridorKey.py` (deployed copy of `D:\New AI Projects\CorridorKey\resolve_plugin\CorridorKey_Pro.py`)
- **Viewer source:** `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py` (launched directly by plugin, no deploy needed)
- **SAM2 weights:** `D:\New AI Projects\CorridorKey\sam2_weights\sam2.1_hiera_small.pt`
- **Session dir (fixed for DaVinci):** `C:\Users\ragsn\AppData\Local\Temp\corridorkey_session\` — has fg.png, alpha.png, sam2_gate_raw.png, original.png, live_params.json after a PREVIEW
- **Log file:** `C:\Users\ragsn\AppData\Local\Temp\corridorkey.log`
- **Skill file with proven fixes log:** `D:\AI\skills\corridorkey.md`

To reproduce:
1. Open Resolve, load Berto's project, park playhead on a stunt frame
2. Open CorridorKey panel (Workspace → Scripts → Utility → CorridorKey)
3. Click PREVIEW — viewer launches with single-frame keyed result
4. Click Matte view — solid white body (no SAM2 yet)
5. Click 8-12 green SAM2 dots covering body (head, torso, midsection, arms, legs)
6. Click APPLY MASK
7. Click Matte view → see the banding bug

## Code path

The viewer's `_apply_sam_mask` method (`preview_viewer_v2.py` ~line 1645):
```python
frame_rgb = np.clip(self.session.fg_rgb * 255.0, 0, 255).astype(np.uint8)
# ...
masks, scores, low_res_masks = pred.predict(
    point_coords=np.array(all_pts),
    point_labels=np.array(labels),
    multimask_output=True,
    return_logits=True,
)
best_idx = int(np.argmax(scores))
best = 1.0 / (1.0 + np.exp(-low_res_masks[best_idx].astype(np.float32)))
best = np.clip(best, 0.0, 1.0)
# saves best as sam2_gate_raw.png (uint16), sets self.session.sam2_gate_raw = best
```

The viewer's matte rendering (`render_composite` ~line 316 and `_render_now` Matte branch ~line 1413):
```python
if session.alpha_raw is not None and session.sam2_gate_raw is not None:
    _gate = session.sam2_gate_raw.copy()
    if _gate.shape != session.alpha_raw.shape:
        _gate = cv2.resize(_gate, (session.alpha_raw.shape[1], session.alpha_raw.shape[0]),
                           interpolation=cv2.INTER_LINEAR)
    alpha = np.clip(session.alpha_raw * _gate, 0.0, 1.0)
```

Then optional `clean_matte_opencv` despeckle (`D:\New AI Projects\CorridorKey\CorridorKeyModule\core\color_utils.py` line 305) — this multiplies original alpha × safe_zone (binary→dilated→blurred), so it preserves alpha values inside the safe zone. It does NOT solidify the body interior.

## What's been tried and FAILED

1. **Switch from `low_res_masks[best_idx]` to `masks[best_idx]`** — got severe waffle/checkerboard pattern visually, even worse. Reverted.
2. **Add `cv2.resize` to Matte branch with `cv2.INTER_LINEAR`** — necessary for matte to render at all (was throwing silent shape error before). Banding still appears. KEPT (necessary).
3. **Switch from `cv2.INTER_LINEAR` to `cv2.INTER_CUBIC`** — no visible improvement. Reverted to LINEAR.
4. **Feed SAM2 the raw greenscreen frame (`original.png`) instead of NN-clean fg.png** — hypothesis was SAM2 needed green-vs-actor contrast in midsection. Did NOT solve banding. Reverted.
5. **Apply Gaussian blur to the gate** (kernel ~15px sigma ~10px at 4K) — did not solve banding. Reverted.
6. **Apply contrast curve in Matte view** — would have made body solid but user pushed back, said it would hide real holes. Reverted.

## Constraints from user

- **Cannot artificially make matte solid** if it hides real holes — user needs to see holes so they can add SAM2 dots to fill them
- **Cannot just be a Matte-view cosmetic fix** — the same banded alpha is what gets composited and rendered to disk. Must fix the actual key, not just the visualization.
- **Same footage 3 days running** — banding wasn't there earlier today, appeared after a series of fixes I made. User certain it's a regression, not footage-specific.
- **Composite still works** — checkerboard backgrounds visible, body keyed (semi-transparent in midsection though). Suggests issue is alpha values, not the rendering pipeline.

## What I think the next agent should investigate

**Most likely root causes I didn't fully chase:**

1. **The NN keyer alpha output may have changed.** User said previously alpha was solid. Today, after applying SAM2, banding appears. If the alpha value in the body interior was at 1.0 yesterday but is at ~0.7 today (possibly from refiner_strength change or Resolve fallback render path for BRAW), then SAM2 multiplication wouldn't darken it further. But Berto's screenshot of NN-alone matte shows solid — contradicting this. **Worth verifying:** dump alpha.png pixel values in the midsection area, see if they're 1.0 or lower. If lower, the issue is in the panel's call to `processor.process_frame()` and the ProcessingSettings.

2. **SAM2 model loading state.** SAM2 might benefit from `predictor.reset_state()` calls or a fresh model load each Apply Mask. Currently the model is loaded fresh each call but state persistence between calls might still be an issue.

3. **SAM2 image preprocessing.** The viewer feeds SAM2 a uint8 RGB image. SAM2 might expect specific normalization (mean/std) that's not being applied. Check `SAM2ImagePredictor.set_image()` source for what it does internally.

4. **A different SAM2 model variant.** Currently using `sam2.1_hiera_small.pt`. If a "large" variant is available, that might produce cleaner gates. Check `D:\New AI Projects\CorridorKey\sam2_weights\` for alternatives.

5. **SAM2 multimask_output selection.** Currently picks `argmax(scores)`. The 3 candidate masks might differ in banding — picking the "largest area" candidate might be more robust than highest IoU score.

6. **The panel's `process_current_frame` (CorridorKey_Pro.py ~line 1493+) might have changed.** Compare against `git log` and look for any recent commits to the keying call. The `ProcessingSettings(refiner_strength=...)` value is loaded from the panel's Refiner slider — verify it matches what was set yesterday.

## Files modified today (for reference / potential bisect target)

- `D:\New AI Projects\CorridorKey\resolve_plugin\preview_viewer_v2.py` (heavily modified — see header line 1)
- `D:\New AI Projects\CorridorKey\resolve_plugin\CorridorKey_Pro.py` (modified — writes original.png, panel sliders removed)
- `D:\New AI Projects\CorridorKey\deploy.py` (new file, replaces write_plugin.py)
- `D:\New AI Projects\CorridorKey\PLAN_2026-04-26.md` (TODO list)

Backup of every plugin deploy is at `D:\New AI Projects\CorridorKey\.deploy_backups\<TIMESTAMP>\`. Revert with `python deploy.py --revert <TIMESTAMP>`.

Backup of every viewer edit is at `C:\Users\ragsn\.claude\file-history\` (search for `preview_viewer_v2.py` snapshots).

## What we know works

- Composite view renders correctly when alpha is good
- Original view shows raw greenscreen frame
- Foreground view shows NN-clean FG
- SCRUB RANGE keys multiple frames and the viewer scrub bar works
- NN keyer alone (no SAM2) produces a solid white body matte for this footage
- AE plugin (different host, same engine) reportedly didn't have this banding

## The actual question for the next agent

Why does `alpha_raw × sam2_gate` produce horizontal banding inside the body for this specific 4K BRAW stunt footage in DaVinci Resolve, and how do we get a solid-white body matte in the Matte view while preserving the ability to see real holes (alpha < 0.3 regions) and without compromising the rendered output?

Good luck.
