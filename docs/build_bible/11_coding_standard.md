# Part 11. Coding Standard

**Scope: the code style, comment conventions, file discipline, and danger-zone tags used across this project. Load this when writing or editing code. This part is constant across all StuntWorks projects, not CorridorKey-specific, except where noted.**

---

## The standard (HRCS)

- Plain-English comment blocks on functions and danger zones. Comments say what a thing does and why, in words a non-author can follow, not restatements of the code.
- Tags on comment blocks where relevant: `DEPENDS-ON` (what this code relies on), `AFFECTS` (what breaks if this changes), `ISOLATED` (no side effects, safe to reason about alone), `DANGER ZONE` (change with care, with a note on what makes it dangerous).
- File size target around 500 lines. Split before a file grows dangerous to edit.
- English-only naming throughout.
- No em dashes in any output.

---

## The danger-zone comment pattern (already in active use in this repo)

```
# DANGER ZONE [level]: [what makes it dangerous]
# WHAT IT DOES: [plain description]
# DEPENDS-ON: [upstream]
# AFFECTS: [downstream / blast radius]
```

CorridorKey already uses this pattern in the live code, not just as a template ideal. Examples confirmed in the tree at last pass: `resolve_plugin/CorridorKey_Pro.py`'s `_try_braw_decode_exe` block (`DANGER ZONE FRAGILE`, describing the byte-exact stream-read contract), and the inline `generate_alpha_hint` comment (`DANGER ZONE FRAGILE\HIGH\CRITICAL: Do NOT swap to AlphaHintGenerator (HSV)`). Treat any existing `DANGER ZONE` comment in this repo as load-bearing documentation, not decoration; it exists because something specific already broke there.
Tag: VERIFIED. Last verified: 2026-07-22.

---

## Where this project already violates or strains the 500-line target

Several live files are far past the 500-line target: `ae_plugin/cep_panel/ae_processor.py` (3152 lines at last pass), `ae_plugin/cep_panel/index.html` (roughly 5410 lines, per the CLAUDE-MAP), `resolve_plugin/CorridorKey_Pro.py` (roughly 6765 lines), and `corridorkey_sam_merge.py`. This is recorded honestly rather than pretended away: these files grew this large under real deadline pressure and have not been split. Do not treat their current size as license to keep adding to them past the target; treat it as an acknowledged debt. Any new logic should go into a new, smaller module where the architecture allows it, rather than growing one of these four further.
Tag: VERIFIED (line counts as of last CLAUDE-MAP scan). Last verified: 2026-07-19 (ae_plugin/CLAUDE-MAP/INDEX.md, resolve_plugin/CLAUDE-MAP/INDEX.md). Recheck when: before trusting an exact line count for an edit; these files change often.

---

## The project root context file

`CLAUDE.md` at the repo root is the canonical entry point for this project. `ae_plugin/CLAUDE.md` and `resolve_plugin/CLAUDE.md` are thin pointers to their respective `CLAUDE-MAP/INDEX.md` files (a per-directory, agent-oriented architecture map, generated and watched separately from this bible). This bible does not duplicate the CLAUDE-MAP content; where the two overlap (architecture, known issues), this bible's Part 02 and Part 03 summarize the same ground with an eye toward failure history and evidence tags, while the CLAUDE-MAP files are the faster, denser lookup for "which exact symbol/line." Read both when doing a code change; they are not redundant, they serve different jobs.

---

## The one-hop editing discipline

Prefer changes that touch one file and add no new cross-file logic. Trivial edits (small, one existing file, no new logic, not safety-critical) are safe to make directly. Anything larger, or anything touching an item on the Part 00/01 protected list, stops for confirmation first.

---

## What not to do

- Do not write comments that restate the code. Say what and why.
- Do not let a file grow far past the size target without splitting; where it already has (see above), do not make it worse.
- Do not edit a danger-zone or protected element as if it were an ordinary change.
- Do not duplicate project rules across multiple root files. One canonical file, thin pointers everywhere else.
- Do not use em dashes.
