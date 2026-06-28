# CorridorKey — Architecture & Distribution

CorridorKey is one AI keying engine shipped to multiple host applications. This
document is the canonical layout, build, and distribution plan. Decided
2026-06-28, research-verified against how real multi-host creative plugins ship
(Gyroflow, ntsc-rs, BorisFX, Topaz).

## Core principle

**One repository. One shared engine. A thin adapter per host. A clean, separate
installer per host built by CI.** Not a branch per host (divergence + merge
hell). Not a repo per host (needless sync overhead for one team). Fix the engine
once → every host gets it the same day.

## The hosts (4 apps, 3 adapters)

| Host app | Adapter | Install mechanism | Notes |
|---|---|---|---|
| After Effects **+** Premiere Pro | Adobe CEP panel (one ZXP, `manifest.xml` lists AEFT + PPRO) | ZXP via aescripts Manager / Anastasiy / ZXPInstaller | Premiere CEP retires ~Dec 2026 → UXP; keep panel logic host-agnostic for a skin-swap port |
| DaVinci Resolve | OFX plugin (one `.ofx.bundle`, also loads in Nuke/VEGAS/Fusion) | OS installer (.exe / .pkg) dropping the bundle in the shared OFX path | **Resolve Studio only** — free Resolve will not load OFX |
| ComfyUI | Custom nodes | git clone into `custom_nodes/` or ComfyUI Manager | Pin `torch` as a range; ComfyUI owns its venv |

The shared engine (PyTorch model + inference) lives in one place and is consumed
by all three.

## Target repo layout

```
corridorkey/
├── engine/                 # shared, pip-installable AI core (the one brain)
│   ├── corridorkey_engine/ #   model load, inference, model-cache resolver
│   ├── pyproject.toml
│   └── tests/
├── hosts/
│   ├── adobe-cep/          # AE + Premiere CEP panel (one ZXP)
│   ├── resolve-ofx/        # DaVinci OFX plugin (+ Nuke/VEGAS/Fusion)
│   └── comfyui-nodes/      # ComfyUI custom nodes
├── model-downloader/       # standalone fetch-weights-once tool (PyInstaller)
├── packaging/              # per-host build scripts (called by CI)
├── .github/workflows/      # ci.yml (test on PR) + release.yml (build all on tag)
├── CHANGELOG.md
└── VERSION                 # single source of truth for version
```

Today's folders map to this as: `CorridorKeyModule/` → `engine/`,
`ae_plugin/cep_panel/` → `hosts/adobe-cep/`, `resolve_plugin/`+`fusion_*` →
`hosts/resolve-ofx/`, `comfyui_plugin/` → `hosts/comfyui-nodes/`. The rename is a
**later, tested cutover** (see Migration) — not required to start shipping
installers.

## Model distribution (the hard part — bigger than layout)

Model weights (380MB+) **never** go in git, in a plugin bundle, or in git-LFS
(GitHub 5GB/file cap + bandwidth). Instead:

1. Weights live in **Cloudflare R2** (S3-compatible, zero egress fees).
2. The license/auth server issues a **presigned URL** (≈1h expiry) after a
   license check.
3. `model-downloader` runs once post-install, pulls weights to ONE shared cache:
   - Windows: `%APPDATA%/CorridorKey/models/`
   - macOS:   `~/.corridorkey/models/`
4. Every host adapter reads that same cache — never its own copy.
5. First-run checksum check skips re-download.

The weights are the real IP (Niko's engine). Encrypt-at-rest in the cache is an
option if the threat model warrants.

## Build / release pipeline

`git tag vX.Y.Z` → `.github/workflows/release.yml` fans out:

- `build-adobe-cep`   → signed `CorridorKey-vX.Y.Z-Adobe.zxp` (self-signed cert; sign per-OS)
- `build-resolve-win` → `CorridorKey-vX.Y.Z-Resolve-Win.exe` (OFX bundle in an NSIS/WiX installer)
- `build-resolve-mac` → `CorridorKey-vX.Y.Z-Resolve-Mac.pkg`
- `build-comfyui`     → `CorridorKey-vX.Y.Z-ComfyUI.zip` + publish to ComfyUI Registry
- `draft-release`     → attaches all artifacts + CHANGELOG notes to a GitHub Release

The model downloader is built separately and shipped alongside (never carries weights).

## Migration phases (each tested before the next; nothing breaks the live panel)

The live AE panel is **symlinked** from
`%APPDATA%/Adobe/CEP/extensions/com.corridorkey.panel` → `ae_plugin/cep_panel`.
A blind folder move kills it, so:

1. **Scaffold + blueprint** (this branch) — packaging/ + CI skeleton + this doc, all additive. Live tree untouched.
2. **Per-host installer pipeline** — make `packaging/` actually build each host's installer from current folder paths.
3. **Model-download system** — R2 bucket + presigned-URL flow + downloader (needs the R2 account).
4. **Folder cutover** — rename to `engine/`+`hosts/`, fix internal paths, re-point the CEP symlink, test all hosts, then make this the new main.

## Open product decisions (owner calls, not engineering)

- Cloudflare R2 + a license/auth server (model gating) — paid infra.
- Storefronts: aescripts+aeplugins (Adobe market), own site + ToolFarm (Resolve).
- Whether to adopt the "satellite" inference-server model (one background process
  serves all hosts; cleanest for OFX, which is native C++ and can't import PyTorch).
