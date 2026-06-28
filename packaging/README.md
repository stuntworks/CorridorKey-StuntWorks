# CorridorKey — Packaging & Build

This directory contains the per-host build scripts invoked by the GitHub Actions
release workflow (`.github/workflows/release.yml`).

---

## How it fits together

```
git tag v0.9.0
  └─ release.yml fans out four parallel build jobs:
       build-adobe-cep   → packaging/build-adobe-cep.sh    → dist/…-Adobe.zxp
       build-resolve-win → packaging/build-resolve-ofx.sh  → dist/…-Resolve-Win.exe
       build-resolve-mac → packaging/build-resolve-ofx.sh  → dist/…-Resolve-Mac.pkg
       build-comfyui     → packaging/build-comfyui.sh       → dist/…-ComfyUI.zip
       draft-release     → attaches all artifacts to a GitHub Release
```

The version string is read from the top-level `VERSION` file (a single line:
`0.9.0`). Every script sources or reads that file — there is no other place to
bump the version.

---

## Scripts

| Script | Host | Output |
|--------|------|--------|
| `build-adobe-cep.sh` | After Effects + Premiere Pro | `.zxp` (ZXP archive) |
| `build-resolve-ofx.sh` | DaVinci Resolve (Win + Mac) | `.exe` (Win) / `.pkg` (Mac) |
| `build-comfyui.sh` | ComfyUI | `.zip` of custom nodes |

All scripts write their output into a `dist/` directory at the repo root (created
on demand). They are safe to run locally for manual builds.

---

## Developer prerequisites

### Adobe CEP (`build-adobe-cep.sh`)

| Tool | Purpose | Install |
|------|---------|---------|
| **Node.js** ≥ 18 | Panel build toolchain | https://nodejs.org |
| **Bolt CEP** (npm package) | CEP project builder/bundler | `npm install -g bolt-cep` |
| **ZXPSignCmd** | Signs the `.zxp` archive | Download from Adobe Exchange; must be on `$PATH` as `ZXPSignCmd` |
| A `.p12` code-signing certificate | Ships a signed extension | Self-signed cert created by the script for CI; swap for a real cert before publishing to aescripts |

> **macOS vs Windows signing:** `ZXPSignCmd` is available for both platforms but
> the flags differ slightly between OS versions. The script documents where the
> per-OS branching needs to happen (TODO).

### DaVinci Resolve OFX (`build-resolve-ofx.sh`)

| Tool | Purpose | Install |
|------|---------|---------|
| **CMake** ≥ 3.24 | Configures the C++ OFX build | https://cmake.org |
| **Ninja** | Fast build backend (optional but recommended) | `brew install ninja` / choco install |
| **DaVinci Resolve SDK** | OFX headers + link targets | Download from Blackmagic Design developer portal |
| **NSIS** (Windows) | Wraps the `.ofx.bundle` in an `.exe` installer | https://nsis.sourceforge.io |
| **pkgbuild / productbuild** (macOS) | Creates a `.pkg` installer | Ships with Xcode Command Line Tools |

> **Resolve Studio only.** The free version of DaVinci Resolve does not load
> third-party OFX plugins. The installer should make this requirement prominent.

### ComfyUI nodes (`build-comfyui.sh`)

| Tool | Purpose |
|------|---------|
| **Python 3.10+** | Runs the zip + checksum logic |
| **zip / zipfile** | Bundling (Python stdlib — no extra install) |

No compilation step. The nodes are pure Python and ship as a source zip.

---

## Model weights — NEVER bundled

Model weights (CorridorKey engine `~280MB`, SAM 2.1 `~80MB+`) are **not** included
in any of these installers. They are too large for git, for git-LFS at scale, and
for plugin bundles.

Weights live in **Cloudflare R2**. A separate `model-downloader/` tool (PyInstaller
standalone) runs once after the plugin installs and pulls weights to the shared
OS cache:

- Windows: `%APPDATA%\CorridorKey\models\`
- macOS:   `~/.corridorkey/models/`

Every host adapter reads the same cache directory. See `model-downloader/README.md`
for the full flow.

---

## Running locally

```bash
# After Effects build (from repo root)
bash packaging/build-adobe-cep.sh

# ComfyUI zip (no special tools needed)
bash packaging/build-comfyui.sh

# Resolve (requires cmake + SDK)
bash packaging/build-resolve-ofx.sh
```

Artifacts land in `dist/` at the repo root.
