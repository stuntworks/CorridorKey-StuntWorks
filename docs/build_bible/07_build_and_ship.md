# Part 07. Build and Ship

**Scope: turning the CorridorKey source tree into what each host actually runs. Load this for deploy, install, signing, and distribution.**

---

## Build chain

CorridorKey ships as source, not a frozen installer; there is no PyInstaller/freeze step. "Build" means:

1. Engine venv: `setup.bat` (Windows) or `setup.sh` (macOS/Linux) creates `.venv` and installs pinned `requirements*.txt` (PyTorch with CUDA via `requirements-gpu.txt`, then the rest).
2. `deploy.py` pushes source files to the exact OS folders each host loads from: Fusion `Scripts/Utility` for Resolve, and (for most Adobe-side edits) nothing at all, because the AE junction already makes `ae_plugin/cep_panel/` the live path.
3. `install.py` for first-time setup on a machine that has never had the plugin: copies the Resolve plugin into `Scripts/Utility`, creates the AE/Premiere CEP junction, writes `corridorkey_path.txt`, and (opt-in via `--allow-unsigned`) flips Adobe's `PlayerDebugMode` registry flag.

## Protected build files

`deploy.py`'s `DEPLOYMENTS` path table must track exactly what each host loads from (Resolve only auto-discovers `Fusion/Scripts/Utility`; AE only loads CEP extensions from its extensions folder). `install.py`'s copy-order logic (previously let the root `ae_processor.py` dummy overwrite the canonical `cep_panel` copy; fixed 2026-05-29, must not regress). `CSXS/manifest.xml` (a stray `--` inside an XML comment silently kills the whole extension, with no error UI).
Tag: VERIFIED. Last verified: 2026-07-22 (deploy.py header, install.py header comment, KNOWLEDGE_LOG_ARCHIVE:1605-1610).

## Pre-ship audit

CK ships as a source clone plus a venv build, not a frozen binary, so the template's "audit the frozen artifact" concept does not map directly onto a compiled distribution tree. What genuinely needs checking before any release tag: confirm `origin/main` does not still contain internal docs Berto has scrubbed from the public repo (`git fetch` first and verify with `gh api`, never trust a stale local ref; this bit once, 2026-05-18, when a local `main` pointed at a commit still containing internal docs). Whether `requirements*.txt` pins ever pull a dependency whose license conflicts with the project's own CC-BY-NC-SA-4.0 distribution has not been run as a formal checklist item as of this pass; this is recorded as an open gap, not a completed audit (Part 13).
Tag: VERIFIED (the origin/main incident and its fix). PARTIALLY VERIFIED (no license-of-dependencies audit confirmed run). Last verified: 2026-07-22.

## Signing and trust

The CEP panel is unsigned. Distribution currently relies on Adobe's `PlayerDebugMode` flag, either set by `install.py --allow-unsigned` or manually via registry (`HKEY_CURRENT_USER\Software\Adobe\CSXS.X`) or `defaults write` on macOS, which disables Adobe's signature check entirely on the installing machine. This is a deliberate, documented tradeoff for a free hobby-scale plugin, not an oversight; `install.py`'s own security note says older installer versions silently forced this flag on and that the current default is off unless explicitly requested, and README states plainly that a signed panel (ZXP-signed) is a planned future direction, not yet built. There is no code-signing certificate in the current pipeline.
Tag: VERIFIED. Last verified: 2026-07-22 (install.py header, README.md "For Developers").

## Distribution

GitHub repo `stuntworks/CorridorKey-StuntWorks`, tagged releases (v1.0.0 latest as of the README consulted this pass) alongside an actively-developed `main` branch that may include unfinished work. Test footage is attached to the v0.7.0 release. Install is `git clone` plus `python install.py` run by the end user; there is no packaged installer executable.
Tag: VERIFIED. Last verified: 2026-07-22 (README.md).
