# CorridorKey Multi-Object SAM2 — Implementation Plan v2

**Date:** 2026-05-02
**Author:** Berto + Claude
**Status:** Ready to build (4-agent reviewed)
**Branch base:** `feat/two-halo-ui` at commit `71482f3`
**Build branch:** `feat/multi-object-sam2`

## Goal

Extend SAM2 from single-mask to **multi-object**. Two independent silhouettes, each with own dots, own halo behavior. They render as a single matte but operate independently — like two performers in the same shot.

Berto's mental model (verbatim 2026-05-02): "We need two separate masks. Each works independently even though presented as one mask."

## v1 → v2 changes (4-agent review synthesis)

| v1 said | v2 says | Why |
|---|---|---|
| Halos shared on union | **Per-object halos** — call `apply_sam2_gate` twice and union the alphas | Inverted geometry (handstand), large negative HALO FEET, and Berto's mental model ALL break shared-halo |
| OBJ 1 / OBJ 2 | **MASK 1 / MASK 2** | Matches Berto's verbatim language; less "lab coat" |
| 700-line mirror | **AE behind env flag for v1, mirror in single dedicated session** | `corridorkey_viewer_local_despill_trap.md` — mirroring has bitten before |
| Cache SAM2 first (open question) | **Defer caching to post-ship** | APPLY MASK is once-per-anchor-frame, not a hot loop. Architecture refactor first, optimize after eye-test |
| Backward compat at step 5 | **Backward compat in first commit** | Migration bugs found 12 hours deep are nasty |
| Radios + dual color from start | **Tab key toggle + single overlay (MVP) → radios + colors (polish)** | Smaller deployable increment, faster Berto eye-test |
| `np.maximum(g1, g2)` once | Per-object pipeline + atomic write protocol | Adversarial: PNG read-during-write races, live_params clobbers |

## Scope (MVP — 2 objects)

- N=2 hard-coded. Dict storage extends to N>2 trivially.
- Each MASK has: own dots, own SAM2 silhouette (`sam2_mask_obj1.png`, `sam2_mask_obj2.png`).
- Halos applied **per-mask** with the **mask's own bbox**:
  - HALO BODY UP applies to MASK 1 (and MASK 2 if user wants — see UI below)
  - HALO FEET ± applies to MASK 2 with MASK 2's bbox bottom (not the union's)
- Final alpha = `max(apply_sam2_gate(alpha, mask1, halos), apply_sam2_gate(alpha, mask2, halos))`

## Halo binding (the architecture decision)

**Open question for Berto to confirm:** how do halos bind to masks?

Option A (simple, 1 set of halos): single HALO BODY + single HALO FEET applied per-mask via `apply_sam2_gate(alpha, mask_n, halo_px, halo_body_px)`. Each mask gets its own bbox so erosion stays in its own zone. Berto sees 2 halo sliders, both apply to both masks but visibly different per region because of bbox.

Option B (per-mask halos, 4 sliders): HALO BODY 1 / HALO FEET 1 / HALO BODY 2 / HALO FEET 2. More UI, more control, more confusion.

Option C (bind by name): rename HALO BODY → MASK 1 BUFFER, HALO FEET → MASK 2 BUFFER. 2 sliders, each tied to one mask. Berto's exact mental model. UPSIDE: clean. DOWNSIDE: assumes MASK 1 = upper / MASK 2 = feet, locks in semantics.

**Recommend Option A**: simplest, matches Berto's "two halos" mental model from May 1, lets either mask use either halo if needed. Critical: per-MASK bbox for erosion (not union bbox).

## Backward Compat

### live_params.json migration (in `_merge_live_params`, FIRST commit):

```python
def _migrate_legacy_sam_keys(lp: dict) -> dict:
    """If live_params has old single-mask keys, migrate to MASK 1."""
    out = dict(lp)
    if "sam_positive" in lp and "sam_positive_obj1" not in lp:
        out["sam_positive_obj1"] = lp.get("sam_positive", [])
        out["sam_negative_obj1"] = lp.get("sam_negative", [])
        out["sam_anchor_frame_obj1"] = lp.get("sam_anchor_frame")
    return out
```

Single point of migration. Both viewer-side load AND panel `_merge_live_params` call this helper.

### PNG migration (panel-side, on session load):

```python
def _migrate_legacy_sam_png(session_dir):
    """If session has old sam2_mask.png, copy to sam2_mask_obj1.png and remove."""
    old = session_dir / "sam2_mask.png"
    new = session_dir / "sam2_mask_obj1.png"
    if old.exists() and not new.exists():
        # Atomic: copy then unlink
        shutil.copy2(old, new)
        old.unlink()
```

Run once at session load. Idempotent.

## Storage

### Session class (in viewer):
```python
session.sam2_gates = {1: None, 2: None}   # dict[int, ndarray | None]

def reload_pngs(self):
    ...
    # Read both per-object PNGs
    for obj_id in (1, 2):
        path = self.session_dir / f"sam2_mask_obj{obj_id}.png"
        self.sam2_gates[obj_id] = _load_mask(path) if path.exists() else None
```

### live_params.json keys:
```json
{
  "sam_positive_obj1": [[x,y], ...],
  "sam_negative_obj1": [[x,y], ...],
  "sam_anchor_frame_obj1": int,
  "sam_positive_obj2": [[x,y], ...],
  "sam_negative_obj2": [[x,y], ...],
  "sam_anchor_frame_obj2": int,
  "sam_active_object": 1 | 2
}
```

## Atomic Write Protocol (Adversarial agent flagged)

### live_params.json:
- Already uses `tmp + os.replace` (atomic). Verify still does after refactor.
- Single writer (the viewer). Panel reads only.
- If user clicks faster than save debounce, latest click wins (existing behavior).

### Per-object PNGs:
- Panel writes via tmp + rename:
```python
tmp_path = session_dir / f"sam2_mask_obj{obj_id}.png.tmp"
cv2.imwrite(str(tmp_path), mask)
os.replace(tmp_path, session_dir / f"sam2_mask_obj{obj_id}.png")
```
- Viewer poll loop reads both paths. If only one exists → use that one. If both exist → union. If neither → no SAM2.

### Cross-PNG race:
- Viewer reads `obj1.png`, then `obj2.png`. If panel was writing `obj2.png` between the reads, viewer sees old `obj1.png` + new `obj2.png` (briefly inconsistent).
- Mitigation: viewer reads both into a snapshot dict, then renders. If mtime changed between two reads, re-read. Simpler: poll one tick later, eventual consistency in <1s.

## Engine (sam2_combine.py)

### New helper:
```python
def union_sam2_gates(*gates):
    """OR-combine multiple SAM2 silhouettes. None-tolerant."""
    valid = [g for g in gates if g is not None]
    if not valid:
        return None
    out = valid[0]
    for g in valid[1:]:
        out = np.maximum(out, g)
    return out
```

### Per-object halo application:
**Caller responsibility, not engine.** Caller invokes `apply_sam2_gate` once per gate, with that gate's bbox naturally driving per-mask erosion logic.

```python
# In viewer / panel render path:
alpha_a = apply_sam2_gate(alpha, gate1, halo_px=hf, halo_body_px=hb) if gate1 is not None else None
alpha_b = apply_sam2_gate(alpha, gate2, halo_px=hf, halo_body_px=hb) if gate2 is not None else None
# Union the alphas (each is alpha * gate_n_with_halo)
final = union_alpha(alpha_a, alpha_b)  # = np.maximum(a, b) with None-tolerance
```

This means each mask's bbox is computed from itself (not the union), so:
- Negative HALO FEET on MASK 2 erodes only MASK 2's bbox bottom (the feet)
- Negative HALO FEET on MASK 1 erodes only MASK 1's bbox bottom (which the user typically won't crank below 0)

## UI

### Active mask selector
- **MVP (Session 1)**: invisible Tab key toggle. Status bar shows `MASK 1 active` or `MASK 2 active`.
- **Polish (Session 2)**: visible radio buttons `MASK 1 / MASK 2` styled like FG SOURCE.

### Buttons
- "APPLY MASK" → "APPLY MASK 1" / "APPLY MASK 2" (auto-label per active)
- "CLEAR" → "CLEAR 1" / "CLEAR 2"

### SHOW SAM2 overlay
- **MVP**: single combined cyan outline (union of both silhouettes).
- **Polish**: dual-color outlines — cyan for MASK 1, yellow for MASK 2.

### Dot colors
- **MVP**: green dots for both masks (existing).
- **Polish**: green for MASK 1 active, magenta for MASK 2 active. Inactive masks' dots dim to 40% alpha.

### Discoverability hint
- When user clicks INSIDE an existing MASK silhouette and the toggle is on the same MASK, label color flip on the other MASK button (suggest switching).

### Old-session toast
- First load of a session that had old `sam2_mask.png`: status bar shows "Your existing mask is now MASK 1. Add MASK 2 anytime." auto-dismiss after 4s.

### No-op feedback
- APPLY MASK with zero dots in active mask: status bar "MASK N has no dots — click on the actor first." Don't run SAM2.

## File Changes Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `sam2_combine.py` | Add `union_sam2_gates` | +12 | Very low |
| `resolve_plugin/preview_viewer_v2.py` | Active-mask state, dot dict, click handlers, save/load, SHOW SAM2 (start: combined; polish: dual), Tab→radios | ~250 | Medium |
| `ae_plugin/cep_panel/preview_viewer_v2.py` | **DEFERRED to v1.1** behind `CK_MULTI_OBJECT=1` env flag. AE reads only `sam2_mask_obj1.png` (legacy compat). | ~10 (compat shim only) | Low |
| `resolve_plugin/CorridorKey_Pro.py` | APPLY MASK active-only, 5 dispatch sites union-read, `_merge_live_params` migration, scrub deferred (single-mask only in v1) | ~80 | Medium |
| New `_smoke_multi_object.py` | Tests for union helper, per-object bbox erosion, missing PNG fallback | ~80 | Low |

Total Resolve+engine: ~430 lines. AE (v1.1): ~250 in a separate dedicated session.

## Build Order (per Pragmatist agent)

### Session 1 (~5 hr) — prove the data path
1. Tag `pre-multiobject` on `feat/two-halo-ui`. Branch `feat/multi-object-sam2`. (5 min)
2. `union_sam2_gates` helper in `sam2_combine.py` + smoke test. (30 min)
3. **`_merge_live_params` migration shim FIRST** + load-old-session test. (30 min)
4. Resolve viewer: `sam2_gates` dict + per-object dot lists + Tab key toggle + status-bar active label. (90 min)
5. Panel: APPLY MASK active-only, writes per-object PNG via tmp+rename. (60 min)
6. Panel: 5 render dispatch sites — call `apply_sam2_gate` per object, union the alphas. (60 min)
7. Smoke + atomic-write check. Deploy. **CHECKPOINT 1 — Berto eye-test**: stunt-vest clip, MASK 1 body / MASK 2 feet. Verify (a) dots persist per mask, (b) APPLY MASK only updates active, (c) union render shows both, (d) HALO FEET negative shrinks feet only without affecting upper body. **Go/no-go gate.**

### Session 2 (~4 hr) — UI polish (only if Session 1 passes)
8. Radio buttons replace Tab key. (45 min)
9. Per-mask dot colors + inactive dim. (45 min)
10. SHOW SAM2 dual-color overlay. (60 min)
11. CLEAR active-only with status feedback. (30 min)
12. Discoverability + no-op feedback + old-session toast. (45 min)
13. Deploy. **CHECKPOINT 2 — Berto eye-test**: hair clip + stunt-vest, full UI.

### Session 3 (~3 hr) — AE mirror + ship prep
14. AE viewer mirror in single focused commit. Same patterns, same naming. (2 hr)
15. AE backward-compat test on a real old AE session. (30 min)
16. Decide: merge to main OR ship behind `CK_MULTI_OBJECT=1` flag. **CHECKPOINT 3 — Berto eye-test**: AE Premiere session.

### Multi-mask scrub (now IN scope per Berto 2026-05-02)
SCRUB RANGE / video propagation must run PER MASK independently. Implementation:
- Panel's video propagation reads each MASK's anchor frame + dots, runs SAM2 video tracker, writes per-frame masks per object: `cache/<frame_idx>/sam2_mask_obj1.png`, `.../sam2_mask_obj2.png`.
- Render path reads both per-frame masks, unions per-frame.
- Each MASK has its own anchor frame (could differ — user might place MASK 1 dots on frame 1 and MASK 2 dots on frame 5).
- If only one MASK has dots, propagate only that one (skip the other to save time).

Estimated: +2 hours added to Session 3 (was AE mirror only) OR moved to a Session 4. Bumps total to ~14-16 hours.

### Deferred to post-ship
- **SAM2 caching** (punchlist task #4) — multi-object doubles APPLY MASK time but it's once-per-anchor, not blocking
- **Strip SUBTRACT/EDGE GUARD** (punchlist task #3) — separate refactor, keep diffs clean

## Open Questions (decide before / during build)

1. **Halo binding** — Option A (single sliders, per-mask bbox) recommended. Berto can override.
2. **MASK 2 active dot color** — magenta or yellow? Polish session decision.
3. **Polish: SHOW SAM2 per-mask toggle** — checkbox 1 / checkbox 2 to toggle each outline independently, or always-both?
4. **Old-session migration error handling** — what if `sam2_mask.png` exists but is corrupt? Skip migration with warning?
5. **AE flag default** — `CK_MULTI_OBJECT=0` (off, AE legacy) or `=1` (on, multi-object)?

## Risks (4-agent flagged)

| Risk | Severity | Mitigation in plan |
|---|---|---|
| Inverted-geometry halo bbox bug | CRITICAL | Per-mask bbox (not union) — already in v2 |
| Live params + PNG write races | MAJOR | Atomic tmp+rename protocol — already in v2 |
| Multi-mask scrub broken | CRITICAL | NOW IN v1 SCOPE per Berto 2026-05-02 — stunt footage is motion-heavy, single-mask scrub is a release blocker |
| Mirror divergence between Resolve / AE | MAJOR | AE deferred to dedicated session, behind flag |
| 2× APPLY MASK latency | MINOR | Acceptable for once-per-anchor; caching as v1.1 |
| Stale OBJ 2 PNG persisting | MINOR | CLEAR deletes PNG; SHOW SAM2 dual overlay shows current state |
| Berto confused by new toggle on old session | MINOR | One-time toast on old-session load |

## Continuation Notes (for next session)

1. Read this file (`PLAN_MULTI_OBJECT_SAM2.md`) first — v2 with 4-agent review baked in.
2. Read `corridorkey_punchlist.md` and `corridorkey_sam2_role_garbage_matte.md` for historical context.
3. Branch state: `feat/two-halo-ui` is shipped+deployed up to `71482f3`. Tag `pre-multiobject` before branching.
4. Build branch: `feat/multi-object-sam2` from that tip.
5. Berto's verbal ask: "two separate masks, each works independently even though presented as one mask. Like two people in the shot."
6. **Start with `_merge_live_params` migration shim — it's commit 1.** Then engine union helper. Then viewer storage refactor with Tab toggle. Then panel dispatch.
7. **Always per-mask bbox for halo, never union bbox.** Critical for negative HALO FEET correctness.
8. **Each commit pushes to `origin/feat/multi-object-sam2`.** Memory + context can be lost; GitHub is the durable backup.
9. Per-session memory note: `session-handoff-2026-MM-DD-multiobject.md` with branch tip SHA + what's working + open questions.

## What the agents agreed on (load-bearing decisions)

- **Halos per-mask, not union** (Architecture, Adversarial, Berto-Product)
- **Naming MASK 1 / MASK 2** (Berto-Product, Pragmatist)
- **AE deferred / flagged** (Architecture, Pragmatist)
- **Backward compat in first commit** (Pragmatist, Adversarial)
- **Per-object PNG with atomic write** (Adversarial, Architecture)
- **Dict storage, IntEnum-friendly** (Architecture)
- **None-tolerant union helper** (Adversarial confirmed; Architecture original)

## What the agents disagreed on (and how Berto resolved)

- **SAM2 caching dependency.** Architecture: do first. Pragmatist: defer. **Plan adopts Pragmatist (defer to post-ship).**
- **Per-mask halo sliders (4 total).** Berto-Product suggests bind-by-name (Option C). Architecture says shared with per-mask bbox (Option A). **Berto 2026-05-02: try Option A first; if testing shows it doesn't behave right, swap to C (~1 hour change).**
- **Tab key vs radio for MVP.** Pragmatist: Tab. Architecture: radio. **Plan adopts Tab for MVP, radio for polish.**
- **Scrub: ship vs hide vs defer.** Berto-Product: release-blocker. Pragmatist: deferrable. **Berto 2026-05-02: IN v1 scope. Stunt footage is motion-heavy. Single-mask scrub via MASK 1 is not acceptable.**
