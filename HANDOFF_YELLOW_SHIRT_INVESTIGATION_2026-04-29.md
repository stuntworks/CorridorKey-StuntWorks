# Yellow Shirt → Pink — Investigation Handoff (2026-04-29)

**Status:** Read-only investigation. No code changed.
**Already ruled out:** `despill_opencv` and `despill_torch` (`CorridorKeyModule/core/color_utils.py`). Both short-circuit at `despill=0` and run subtract-only at `despill=1`. Same pink result either way.

---

## 1. The Full FG Color Path (NN → user pixel)

The "fg" channel that ends up on screen travels through these stages, in order, in the live single-frame preview path Berto used for the screenshot:

| # | Site (file:line) | Operation |
|---|---|---|
| 1 | `CorridorKeyModule/core/model_transformer.py:251` | `fg_logits = self.fg_decoder(features)` — 3-channel CNN decoder produces raw FG logits |
| 2 | `model_transformer.py:256` | `fg_logits_up = F.interpolate(...)` — bilinear upsample to input size |
| 3 | `model_transformer.py:266` | `fg_coarse = torch.sigmoid(fg_logits_up)` — sigmoid for refiner input |
| 4 | `model_transformer.py:280` | `delta_logits = self.refiner(rgb, coarse_pred)` — CNN refiner sees **RGB + coarse alpha + coarse fg**, outputs delta logits ×10 (`model_transformer.py:142`) |
| 5 | `model_transformer.py:286` | `delta_fg = delta_logits[:, 1:4]` — refiner per-channel FG correction |
| 6 | `model_transformer.py:291` | `fg_final_logits = fg_logits_up + delta_fg` — add coarse + delta in logit space |
| 7 | `model_transformer.py:295` | `fg_final = torch.sigmoid(fg_final_logits)` — final FG, in **sRGB**, float [0,1] |
| 8 | `inference_engine.py:343-347` | `fg = TF.resize(pred_fg.float(), [h, w], BILINEAR)` — back to source resolution |
| 9 | `inference_engine.py:358` | `processed_fg = cu.despill_torch(fg, despill_strength)` — despill (RULED OUT) |
| 10 | `inference_engine.py:391` | `result["fg"] = fg[i]` — sRGB raw FG returned in dict |
| 11 | `resolve_plugin/CorridorKey_Pro.py:1850` | panel reads `res.get("fg")` |
| 12 | `CorridorKey_Pro.py:1855-1856` | optional `_cu.despill_opencv(fg, ...)` — short-circuits at slider=0 (RULED OUT) |
| 13 | `CorridorKey_Pro.py:1554-1555` | written to `fg.png` as BGR uint8 (lossy: float→uint8 quantization) |
| 14 | `resolve_plugin/preview_viewer_v2.py:296` | viewer reads `fg.png`, BGR→RGB, `_to_float01` |
| 15 | `preview_viewer_v2.py:434-436` | `fg_rgb = session.fg_rgb.copy()`; optional viewer despill (RULED OUT) |
| 16 | `preview_viewer_v2.py:451-452` | `comp = composite_straight(fg_rgb, bg, alpha_3)` — `fg*alpha + bg*(1-alpha)` |

The status bar `mean RGB (97.4, 84.9, 78.1)` Berto sees comes from step 16 output (composite over checker), but the hue is fixed by step 7 — the model FG itself.

---

## 2. Where Hue CAN Shift

Hue can only shift in operations that read more than one channel at once or apply different math per channel.

### Strictly per-channel (cannot shift hue on their own)
- sRGB↔linear (`color_utils.py:52-69`) — same curve on R, G, B.
- `premultiply` / `composite_straight` (`color_utils.py:78, 98`) — multiplies all 3 channels by the same `alpha`. Cannot shift hue of FG alone, BUT can change the *visible* hue of the final pixel by mixing in the checkerboard background where alpha < 1.
- `srgb_to_linear` round-trips and BILINEAR resize (`inference_engine.py:343-347`, viewer `cv2.resize`) — per-channel.
- Despill subtract-only (`color_utils.py:268-270`) — per-channel after the cross-channel `spill_amount` calc, but already ruled out.

### Cross-channel (could shift hue)
- **`fg_decoder` in the model itself** (`model_transformer.py:186, 251`). Output channel `output_dim=3`. Each output channel is a Conv1×1 over the same 256-channel embedding. The 3 output channels are **independent linear combinations** of the embedding — there is nothing tying R, G, B to the input image's R, G, B. The model is free to output any color it learned to associate with "this is a foreground pixel and the green-screen residue should look like X."
- **`CNNRefinerModule`** (`model_transformer.py:99, 129-142`). Sees the full RGB image plus coarse alpha + coarse fg, outputs a 4-channel delta scaled ×10. The FG-delta channels are independent per-output-channel convs over a hidden_channels=64 representation. Same story: cross-channel.
- **`composite_straight` over checkerboard** (`preview_viewer_v2.py:449, 452`): the checker is gray (R=G=B≈0.15 or 0.55), so it tints the visible pixel toward gray when alpha<1. Cannot turn yellow into pink by itself, but can dilute saturation (this matches the slightly desaturated 97/85/78 reading).

---

## 3. Most Likely Root Cause

**The neural net's `fg_decoder` is producing the pink directly.** Evidence:

1. **Despill ruled out at both 0.0 and 1.0** — same pink. So everything after step 9 is innocent.
2. **There is no other cross-channel operation upstream of the model output.** Steps 1–7 are entirely inside the model. The pre-processing (`inference_engine.py:227-243`) is BILINEAR resize (per-channel), optional linear→sRGB (per-channel), then ImageNet normalize (per-channel). None of those can shift hue.
3. **Background prior:** the FG decoder at `model_transformer.py:186` is an independent decoder head (`output_dim=3`), separate from the alpha decoder. It's a learned reconstruction of "what color should this fg pixel be after the green is removed." The training data almost certainly under-represents yellow wardrobe on green, so the network's best guess on a yellow shirt is "warm skin tone" → peach/pink.
4. **The status RGB (97.4, 84.9, 78.1)** = (0.382, 0.333, 0.306) — that's R > G > B, R 13% above G — classic peach/skin. Yellow shirt would be R ≈ G > B with R-G gap < 5%.
5. **The refiner reinforces this.** `model_transformer.py:280` feeds the refiner the original RGB plus coarse pred. Output is scaled ×10 (`model_transformer.py:142`). Even small per-channel deltas get amplified an order of magnitude; the refiner can pull G down and R up on a pixel that "looks like skin to it."

**Secondary suspect (much less likely):** the BGR↔RGB trip through `fg.png` at `CorridorKey_Pro.py:1554` and `preview_viewer_v2.py:296`. Both use `cv2.cvtColor(..., COLOR_BGR2RGB)`/`COLOR_RGB2BGR`. If one were missing or doubled, you'd see a swapped-R/B effect — yellow (high R, high G) would become cyan (high G, high B), not pink. Visual symptom doesn't match. **Skip this hypothesis unless the primary one is disproven.**

---

## 4. Concrete Repro Test (one experiment)

Berto runs ONE single-frame preview on the yellow-shirt clip with these settings:

- Despill slider = 0 (already what the screenshot shows)
- Despeckle off
- SAM2 not engaged
- Background = "black" or "white" in the viewer (NOT checker — removes the gray dilution variable)

Then in `resolve_plugin/preview_viewer_v2.py`, look at `session.fg_rgb` directly *before* any compositing. Either:

**a) Save the raw FG to disk.** Add a one-shot `cv2.imwrite("D:/tmp/raw_fg_dump.png", cv2.cvtColor((session.fg_rgb*255).astype(np.uint8), cv2.COLOR_RGB2BGR))` at line 434 and inspect the PNG in any color picker. **(Read-only investigation forbids actually editing — Berto/me would do this in a follow-up session.)**

**b) Skip the edit — read the existing fg.png.** That file IS `session.fg_rgb` round-tripped through uint8. Path: `<SESSION_DIR>/fg.png`. Open it directly in Photoshop/Affinity and color-pick the shirt.

If the fg.png shirt is already pink/peach: model is the source. If yellow: it was downstream (very unlikely given the audit above, but completes the proof).

---

## 5. Three Concrete Fix Paths (ranked)

### Path A — Substitute source RGB for model FG inside the matte (most likely viable)

**Idea:** Keep the model's alpha. Replace the FG color with the source frame's color (the original greenscreen RGB), then apply the existing despill subtract on top. This is the "Mocha keylight" approach: the alpha is the only learned thing; color is real.

**Where:** new operation between `inference_engine.py:347` (after FG resize) and the despill at `:358`. Or simpler — inject `original_rgb` into `preview_viewer_v2.render_composite` at `preview_viewer_v2.py:434` and replace `session.fg_rgb` with `session.original_rgb`. The viewer already reads `original.png` (`CorridorKey_Pro.py:1563`) and uses it for the Original tab (`preview_viewer_v2.py:600`).

**Complexity:** small — one substitution + threaded through 5 combine sites + render path (`CorridorKey_Pro.py:2117` writes `fg.png` from NN; would need to write the source instead, OR apply substitution at composite time only).

**Risk:** loses the model's "green-soaked-fabric reconstruction" — if subject is *actually* drenched in green spill, raw source will look greener than model FG. Mitigated by the existing despill stage which now subtracts cleanly (post-2026-04-29 fix).

**Variant:** add a "FG SOURCE" toggle: NN-FG vs. Source-FG vs. blend. Lets user choose per shot.

### Path B — Add a learned color-correction undo (less likely viable)

**Idea:** detect "model shifted hue" by comparing model FG against source RGB inside the matte and apply a per-pixel color rotation back toward source hue.

**Where:** new function in `color_utils.py`, called from same site as despill.

**Complexity:** medium. Needs robust "trust source vs. trust model" logic — under heavy real spill, source is wrong; under unseen wardrobe, model is wrong.

**Risk:** high — easy to over-correct on real spill, where the model FG is actually doing useful work removing green. Could regress every other clip.

### Path C — Retrain the model with yellow-wardrobe data (out of scope)

**Idea:** the proper fix is to retrain `GreenFormer` with a yellow / tan / orange wardrobe set so its FG decoder learns those colors.

**Complexity:** out of scope. Berto doesn't own the training pipeline; the checkpoint comes from `nikopueringer/CorridorKey_v1.0` on HF (`backend.py:32`).

---

## 6. CLEANUP CANDIDATES — NOT APPLIED

(Per RULE 2 — flagged, not changed.)

- `inference_engine.py:282` (`_postprocess_opencv`) hard-codes `green_limit_mode="average"`; the parameter exists in `despill_opencv` but isn't surfaced to the engine API. Same for `_postprocess_torch:358` (no `green_limit_mode` parameter at all in `despill_torch` — torch path is hard-`average`). Inconsistency, not the bug.
- `model_transformer.py:258-262` — commented-out "humility clamp" (logits clamp ±3.0). Removed in Phase 3 per comment. Not the bug, but worth noting: without that clamp, refiner deltas can push fg logits anywhere, amplifying any cross-channel hue shift the decoder produces.
- The `processed` key in engine output (`inference_engine.py:367`) is premultiplied linear-RGBA — **viewer never reads it** (only reads `fg.png` written from `out["fg"]`). Documented hazard from `corridorkey_engine_alpha_keys.md` — engine despeckle wasted for live preview. Not the bug, but means any "fix in `processed`" would never reach the viewer.

---

## 7. Bottom Line

The pink is baked into the model's foreground decoder output, not introduced by despill or compositing. Confirmation step is to read the on-disk `fg.png` directly with a color picker. Recommended fix is **Path A** — replace model FG with source RGB inside the matte at composite time, optionally toggleable. This makes the alpha the only "learned" thing and uses real color from the plate, which is what pro keyers (Keylight, Primatte, IBK) do by default.
