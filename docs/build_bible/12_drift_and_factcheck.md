# Part 12. Drift and Fact-Check

**Scope: values in this bible that move between sessions and releases, and must be verified against the live project before being trusted. Load this before relying on any specific path, version, constant, or line number.**

---

## Structural (does not drift, trust it)

The identity and seven rules (Part 01), the architecture map's shape (Part 02, though its exact paths and current branch/commit drift, see below), the hard part and its governing principle (Part 04), the AI pushback rebuttals (Part 10), and the coding standard (Part 11) are load-bearing and do not need re-verification session to session.

---

## Drift-prone values (verify before trusting)

| Value | Where it lives | How to verify | Last verified |
|---|---|---|---|
| Current branch and commit | git repo | `git status`, `git log -1` | 2026-07-22: branch `feat/mcp-server`, commit `caff84f`, working tree has substantial uncommitted changes on top |
| corridorkey_sam_merge.py module intent vs MERGE_MODE constant | corridorkey_sam_merge.py:1-44 | Read the module docstring AND the MERGE_MODE constant together, they disagree as of this pass | 2026-07-22: docstring (lines 1-25) describes a "v1.0, CK and SAM independent, plugin no longer merges them" design; MERGE_MODE = "garbage_matte" (line 40) and the code below it still implement an active merge. This looks like an in-progress, uncommitted architecture transition, not a stable state. Do not trust either description alone; check both, and check git status for uncommitted changes before relying on this file |
| On-green HSV lower bound | ae_plugin/cep_panel/ae_processor.py lines 357, 1426, 1533; corridorkey_sam_merge.py:1546 | grep for `35, *50, *50` vs `35, *50, *20` | 2026-07-22: all four sites still read hue/sat/val floor (35, 50, 50). A 2026-06-22 memory entry records the floor as lowered to (35, 50, 20) to fix green-spill-on-dark-clothing; that lowered value does not exist anywhere in the current tree. Treat the spill-on-dark-clothing fix as NOT actually shipped until this is reconciled (see Part 13) |
| SAM_BASELINE_SMOOTH_SIGMA | corridorkey_sam_merge.py:95 | grep `SAM_BASELINE_SMOOTH_SIGMA` | 2026-07-22: 1.0 |
| install.py Resolve installer target | install.py | Read which files it copies (core/, ui/, resolve_corridorkey.py vs CorridorKey_Pro.py) | 2026-07-22 (per resolve_plugin/CLAUDE-MAP/INDEX.md Known Issues): still installs the superseded legacy plugin, not the live one |
| README's stated latest release tag | README.md | Check GitHub Releases directly, do not trust a cached README | 2026-07-22: README (as read this pass) names v1.0.0 as the latest tagged release, with main as active development |
| Host app version support | CSXS/manifest.xml HostList, README requirements table | Read manifest.xml directly | 2026-07-22: README states Resolve 18.5+, AE/Premiere 2022+ (22.0+); verify manifest.xml's exact CSXS version range separately, it can lag a new Adobe release |
| ck_broker.py port and secret | ae_plugin/cep_panel/ck_broker_config.json | Read the file on the machine in question (never copy its value between machines or into a document) | 2026-07-22: file exists, auto-generated, machine-local. Value intentionally not recorded here |
| Engine root default path | INSTALL.md, corridorkey_path.txt | Check the actual env var / file on the machine in question | 2026-07-22: INSTALL.md's own resolution order is CORRIDORKEY_ROOT env var, then corridorkey_path.txt, then a sibling CorridorKey/ folder, then a hardcoded D:\New AI Projects\CorridorKey legacy fallback, then ~/CorridorKey |
| braw-decode.exe location | resolve_plugin/CorridorKey_Pro.py:2527-2530 | grep `exe_candidates` | 2026-07-22: ProgramData Fusion Scripts Utility copy, or sibling repo D:/New AI Projects/braw-decode-win/bin/braw-decode.exe |
| File line counts (ae_processor.py, index.html, CorridorKey_Pro.py) | live files | wc -l / editor line count | 2026-07-19 per CLAUDE-MAP scans (ae_processor.py 3152, index.html ~5410, CorridorKey_Pro.py ~6765); these grow between sessions, do not cite an exact number without rechecking |

---

## Why this matters here specifically

This bible was written against an uncommitted, mid-transition working tree (branch `feat/mcp-server`, not `main`, with local changes on top of the last commit). The corridorkey_sam_merge.py docstring-versus-MERGE_MODE contradiction above is a direct symptom of that: a session was partway through rewriting the module's design intent when this bible pass happened. Treat every file:line reference in Parts 02, 03, 04, and 08 as check-against-live, not as a permanent citation, until the branch is merged or this bible is re-verified against a settled `main`.
Tag: STALE-REVERIFY. Last verified: 2026-07-22. Recheck when: `feat/mcp-server` merges to `main`, or before any edit that trusts an exact line number cited in this bible.
