# Part 05. Interfaces and Controls

**Scope: how the operator drives CorridorKey across its three hosts. Load this for buttons, dots, sliders, modes, and the (missing) uncertainty signal.**

---

## Primary control surface

Buttons and dots are the entire control surface; there is no typed command language the operator sees.

| Control | Host(s) | Action |
|---|---|---|
| Screen type (green/blue) picker | All | Sets the chroma target for the CK keyer and the inline RGB chroma hint |
| Despill slider | All | Adjusts subtractive-only despill strength on CK's fg estimate |
| Refiner / edge slider | All | Feeds `apply_matte_postproc` edge and feather parameters |
| SHOW PREVIEW (Resolve) / PREVIEW FRAME LIVE (AE, Premiere) | All | Opens the live-slider preview: a separate PySide6 process on Resolve, an in-panel canvas on AE/Premiere |
| PROCESS FRAME / KEY CURRENT FRAME | All | Keys the current playhead frame; imports the result to Track 2 (Resolve), above the source layer (AE), or V2 (Premiere) |
| PROCESS ALL / PROCESS WORK AREA / PROCESS IN-OUT RANGE | All | Batch-keys a frame range. If SAM2 dots are active, the video predictor propagates them across the whole range |
| SCRUB RANGE | Resolve | Fast, low-res preview scrub across a range without a full render |
| Left-click on canvas/preview | All | Adds a SAM2 positive dot (area to keep) |
| Right-click on canvas/preview | All | Adds a SAM2 negative dot (area to exclude) |
| APPLY MASK | All | Runs SAM2 (image predictor, on the anchor frame) and refines the matte with the current dots |
| CLEAR | All | Removes all SAM2 dots and resets the AI mask |
| Tab | All | Switches the active object between MASK 1 (cyan, body/on-green) and MASK 2 (magenta, feet/off-green) for multi-object SAM2; each mask keeps its own dots |
| Disable source clip | Resolve | When checked, hides the source track once the key is placed. Default is unchecked (see Part 08; checking it disables the whole track, not just the clip) |
| Add keyed clip to timeline | Premiere | When unchecked, keyed files land in the CorridorKey bin only, with no timeline placement |
| Output folder browse | Premiere | Overrides the default project-adjacent CorridorKey output folder |

---

## Command vocabulary

No operator-facing typed commands. The internal JSON job vocabulary between the CEP panel and Python (`extract`, `cache`, `sam-apply`, `postproc`, `batch`, `batch-scrub`) is a wire protocol, not something the operator ever types; see Part 03 (CEP Panel, ck_broker) for its shape.

---

## Modes

| Mode | What changes |
|---|---|
| MASK 1 / MASK 2 (multi-object SAM2) | Separate dot sets and separate SAM2 masks for two distinct regions in the same shot (for example body versus feet on a floor the chroma cannot kill) |
| MERGE_MODE constant: garbage_matte / chroma_gated / path_b | Selects the active merge architecture. garbage_matte is the only mode meant to ship; the other two are hot-revert fallbacks, not an operator-facing toggle |
| Match Render (preview) | Forces the preview to run the real video predictor instead of the faster image predictor, so what the operator judges in preview is what will actually render (Part 01 Rule 5) |
| Codec (Resolve v1.0 only) | PNG 8-bit (default) / PNG 16-bit / TIFF 16-bit / EXR 32-bit. AE and Premiere stay PNG 8-bit until v1.1 |

---

## Operator feedback

The busy strip / processing indicator shows that a job is running. The `%TEMP%\corridorkey.log` bridge (`window.onerror` / `unhandledrejection` in the CEP panel) surfaces JS-side errors that would otherwise fail silently. Resolve writes tracebacks to `%TEMP%\corridorkey_error.txt`.

## The uncertainty signal (a real gap)

There is no in-panel confidence or uncertainty indicator for the matte itself, no "SAM2 coverage is low on this frame" flag, no "chroma confidence is marginal here" warning. The operator judges the composited matte entirely by eye, which is consistent with the project's own stated law (artist eye beats a metric, Part 01 Rule 3), but that law describes how humans should judge quality, not a substitute for the panel telling the operator when it is uncertain. The busy strip and the error-log bridge report activity and crashes, not matte confidence. This is recorded here as a genuine, unclosed gap in the control surface, not a solved problem; see Part 13.
Tag: HYPOTHESIS (gap identified, not yet scoped as a build task). Last verified: 2026-07-22. Recheck when: any SAM2/merge confidence signal is proposed or built.

---

## Protected control constants

`CSXS/manifest.xml`'s `HostList` version ranges (widen the range if a newer Adobe version rejects the panel as unsigned-for-this-version; never touch panel logic to fix this). The `Disable source clip` default (unchecked; flipping it back to checked-by-default re-exposes the whole-track-disable footgun, Part 08). The Strap Bridge / Junk Kill slider, confirmed dead (md5-identical output across its full range); do not re-wire without first confirming it actually reaches the active recipe path.
