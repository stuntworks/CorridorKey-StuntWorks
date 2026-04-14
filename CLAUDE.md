# CLAUDE.md — CorridorKey Plugin

Entry point for any AI (or human) editing this repository.

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
