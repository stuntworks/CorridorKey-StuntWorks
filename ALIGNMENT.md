# Frame Alignment — Canonical Reference

**Last modified:** 2026-04-14
**Scope:** CorridorKey Plugin for Adobe Premiere Pro (After Effects is simpler — see bottom).

> **STOP.** If you are about to change anything in `ppro_getFrameInfo`,
> `ppro_getInOutInfo`, `ppro_importFrame`, `ppro_importSequence`, or the batch
> frame math in `ae_processor.py`, read this file first. This alignment has
> been fixed four separate times in 2026-04-12 to 2026-04-14. Every regression
> came from someone touching these functions without understanding *all four*
> of the offsets below. If you only fix the one you see, you will break the
> others.

---

## The Four Offsets

Premiere's scripting API has four behaviors that fight frame-accurate keying.
Each needs a specific compensation and the compensations interact. The
current code is tuned so all four cancel out and the keyed frame lands
exactly on the playhead.

### 1. `playerPos` returns the *next* frame boundary, not the current one.

When the user parks the playhead on frame `N`, `seq.getPlayerPosition().seconds`
returns the time of frame `N + 1`. Discovered 2026-04-13 (commit `804295a`).

**Compensation:**
`host.jsx` → `ppro_getFrameInfo` subtracts one *sequence-frame* of time:
```js
sourceTimeSec = sourceTimeSec - (1.0 / fps);
```

Without this, the extracted frame is the one *after* the playhead.

### 2. The source clip's fps can differ from the sequence's fps.

A 23.976 fps sequence can host a 24 fps clip. Any frame math done on the JSX
side uses `seq.getSettings().videoFrameRate`, which is the *sequence* fps. If
you use that to index into the *source* media, every frame past ~zero is off
by `(seq_fps - source_fps) / source_fps`. This causes *drift* across the
batch: frame 1 is off by a hair, frame 47 is off by two frames.

**Compensation:**
JSX returns the source-media window as **seconds** (`startSeconds`,
`endSeconds`, `sourceTimeSeconds`). Python opens the clip, reads
`cv2.CAP_PROP_FPS` (the *source's* native fps), and computes frame indices
*there*. No conversion happens on the JSX side. No mismatch possible.

Discovered 2026-04-14 (this file is the fix for it).

### 3. `overwriteClip` places a still at its *projectItem's* current duration.

Premiere's default duration for an imported still PNG is ~5 seconds. If you
import a PNG and call `overwriteClip` without trimming first, the clip on
the timeline will be 5 seconds long, not one frame. The user sees a purple
block spanning their whole work area.

**Compensation:**
In `ppro_importFrame`, trim the projectItem *before* placement:
```js
var oneFrameSec = 1.0 / Number(fps || 24);
var tIn = new Time(); tIn.seconds = 0;
var tOut = new Time(); tOut.seconds = oneFrameSec;
imported.setInPoint(tIn, 4);   // media type 4 = video
imported.setOutPoint(tOut, 4);
```

### 5. Imported PNG sequence needs its frame rate FORCED to match V1.

Premiere's numbered-stills import ignores the PNG sequence's "intended" frame
rate and applies its own default (usually the project fps). If V1's native
fps differs from the project fps (24-fps source clip on a 23.976 sequence,
or a conformed clip), V2 will drift relative to V1 because Premiere
interprets the two clips at different rates.

**Compensation:**
`ppro_getInOutInfo` reads V1's `FootageInterpretation.frameRate` and
returns it as `sourceFrameRate`. `ppro_importSequence` applies that exact
rate to the imported PNG sequence via `setOverrideFrameRate` (or
`setFootageInterpretation` fallback) *before* placement. V1 and V2 now
conform identically and stay locked across the whole range.

Discovered 2026-04-14 (this was the "frames don't line up" drift Berto
saw on 47-frame batch even though single-frame was perfect).

### 4. Placement needs `+1` frame nudge for *batch* but NOT for single frame.

The -1 offset from Behavior 1 already compensates for the next-boundary
quirk in *single-frame* mode. Stacking a +1 nudge on placement in
`ppro_importFrame` stacks a second offset and lands one frame forward of
the playhead.

For *batch* mode the math works out the other way: the sequence import
needs a +1 frame nudge on placement because the dummy `output_00000.png`
consumes one timeline frame.

**Compensation:**
- `ppro_importFrame` — **no nudge**, place at playhead exactly.
- `ppro_importSequence` — **+1 frame nudge** on placement.

Discovered 2026-04-14 (the screenshot "off by one frame forward" was this).

---

## Related Issues That Aren't Frame Alignment But Look Like It

- **Premiere drops the first frame of an imported PNG sequence.** Not a
  real drop — the importer skips the lowest-numbered file in a numbered-
  stills import. Compensation: `ae_processor.py` writes a dummy
  `output_00000.png` (copy of the first real keyed frame) so the user's
  actual range survives.
- **Importer ignores `targetBin` for numbered-stills imports.** The code
  has to import to root, diff `root.children` before/after, find the new
  item, and `moveBin()` it into the CorridorKey bin.
- **Mattes mixed into the main output folder confuse the importer.** Two
  PNG patterns in the same dir (`output_NNNNN.png` + `matte_NNNNN.png`)
  make Premiere guess wrong. Mattes now live in `outDir/mattes/`.

---

## Mandatory Pre-Commit Smoke Test

Any change to Premiere-side code must pass this manual test before commit.
Under five minutes.

**Setup:**
- Any Premiere project with a green-screen clip on V1, at least 2 seconds long.
- Source clip with a distinct visual landmark somewhere in the middle (a
  hand cross, a door slam — something you can pick out by sight).

**Test A — Single-frame alignment:**
1. Park playhead on the landmark frame.
2. Click **KEY CURRENT FRAME**.
3. Expected: a 1-frame purple PNG appears on V2 directly above the
   playhead, NOT one frame earlier, NOT one frame later, NOT spanning the
   whole clip.
4. Scrub one frame left and one frame right. The V2 keyed frame still
   lines up visually with the V1 source landmark.

**Test B — Batch drift:**
1. Select the full clip (no sequence in/out markers).
2. Click **PROCESS IN/OUT RANGE**.
3. Wait for the batch to finish and the sequence to land on V2.
4. Park playhead at start of V2 sequence — keyed subject should match V1.
5. Park playhead at END of V2 sequence — keyed subject should STILL match
   V1. If it slips, you have drift again (Behavior 2).

**Test C — Different fps:**
1. Create a 23.976 fps sequence. Drop a 24 fps clip on V1. Repeat Test B.
2. Create a 29.97 fps sequence. Drop a 23.976 fps clip on V1. Repeat Test B.
3. Both must pass. If the sequence fps matters, you broke Behavior 2.

**Test D — Single-frame duration:**
1. Single frame on V2 must be exactly 1 frame wide. Zoom in on the
   timeline if unsure. If it is longer than one frame, you broke
   Behavior 3.

Keep screenshots of Test A and Test B passing as your commit evidence.

---

## After Effects

AE is simpler because the comp fps always matches how we index the source
media. No next-boundary quirk. No fps mismatch. The code path in
`ae_processor.py` still uses `--frame N` and it still works. If you touch
AE code, the only real risk is messing up `comp.frameDuration` arithmetic
in `ae_getFrameInfo` / `ae_getWorkAreaInfo`. Park playhead at a known spot,
click KEY CURRENT FRAME, verify the new layer starts at exactly
`comp.time` and ends at `comp.time + comp.frameDuration`.

---

## History

| Date       | Commit     | What broke / what fixed it |
|------------|------------|-----------------------------|
| 2026-04-12 | `c011926`  | First alignment fix: -1 extract, +1 batch placement |
| 2026-04-13 | `804295a`  | Timecode reports NEXT boundary — documented Behavior 1 |
| 2026-04-13 | `ab63d6d`  | `-1 not +1` confirmed by testing |
| 2026-04-14 | `41bd9bd`  | Full rewrite reintroduced alignment bugs |
| 2026-04-14 | `54a6a78`  | Frame drift fixed (Behavior 2), single-frame placement + duration fixed (Behaviors 3, 4) |
| 2026-04-14 | (next)     | Batch drift second source: imported sequence frame rate override (Behavior 5) |
