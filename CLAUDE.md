# CLAUDE.md — CorridorKey Plugin

Entry point for any AI (or human) editing this repository.

## AE CEP folder is a Windows JUNCTION (read this before editing AE files)

The Adobe CEP install path:

```
C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel
```

is a **junction** pointing to:

```
D:\New AI Projects\CorridorKey\ae_plugin\cep_panel
```

What this means:
- AE loads files **directly from the engine repo** through the junction.
- Editing files at EITHER path edits the SAME files (they are physically the same).
- `git status` immediately sees AE-related edits — no copy/sync step.
- The dual-edit divergence problem (193/97 incident on 2026-04-26) is now structurally impossible.

**Do NOT** run `install.bat` without checking it first — the old script used `xcopy` and would overwrite (break) the junction. Junction must be recreated with `mklink /J` after any reinstall.

To recreate the junction (e.g., on a fresh machine):

```cmd
rmdir "C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel"
mklink /J "C:\Users\ragsn\AppData\Roaming\Adobe\CEP\extensions\com.corridorkey.panel" "D:\New AI Projects\CorridorKey\ae_plugin\cep_panel"
```

## origin/main has branch protection

GitHub now blocks at the server:
- `git push --force` to origin/main
- `git push --delete origin main`
- `enforce_admins=true` so even admin can't override

This is intentional. If you need to do a legitimate destructive push, temporarily disable in:
GitHub repo Settings → Branches → Edit protection rule.

Always create new commits rather than rewriting history on main.

## If you are about to touch Premiere Pro frame math, STOP.

Before changing anything in `ae_plugin/cep_panel/jsx/host.jsx`
(`ppro_getFrameInfo`, `ppro_getInOutInfo`, `ppro_importFrame`, `ppro_importSequence`)
or the batch frame math in `ae_plugin/ae_processor.py`, read
[ALIGNMENT.md](./ALIGNMENT.md). It documents four Premiere API quirks
and the four-offset compensation scheme tuned to cancel them out.

This alignment has been fixed four separate times in three days. Every
regression was caused by someone editing one of these functions without
knowing about *all four* of the offsets.

## Pre-commit smoke test

Any change to Premiere-side code must pass the four manual tests in
[ALIGNMENT.md § "Mandatory Pre-Commit Smoke Test"](./ALIGNMENT.md) before
commit. Under five minutes.

## Install and run

See [INSTALL.md](./INSTALL.md).

## Rebuild the engine venv

```
setup.bat              # Windows
./setup.sh             # macOS / Linux
```

## Security review

See [CODE_REVIEW_2026-04-14.md](./CODE_REVIEW_2026-04-14.md) for the last
full security + quality pass. All Critical and High items were addressed
in commits `41bd9bd` + `54a6a78`.
