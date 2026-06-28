# Changelog

All notable changes to CorridorKey are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Packaging pipeline scaffolded: per-host build scripts in `packaging/`, GitHub
  Actions CI + release workflow in `.github/workflows/`, and a standalone
  `model-downloader/` tool. All are skeletons with documented TODOs; real
  signing/compiling is a later phase.
- `VERSION` file as single source of truth for the release version string.
- `model-downloader/manifest.json` declares the three model weights (CorridorKey
  engine, SAM 2.1 base-plus, SAM 2.1 small) with R2 keys and TODO checksum
  placeholders.
- `ARCHITECTURE.md` — canonical multi-host layout, distribution plan, and
  migration phases (2026-06-28).

### Fixed
- One-push render hang caused by an uncaught exception in the panel's render
  queue; requests that timed out now surface a user-visible error instead of
  silently stalling.
- SAM inference timeout: added a configurable watchdog (default 60 s) that aborts
  a stalled SAM session and resets state so the panel recovers without a reload.

### Changed
- Repository cleaned of ~380 MB of binary model weights that had been committed
  directly; weights now live exclusively in Cloudflare R2 and are fetched by the
  model downloader at first run.

## [0.8.0] — 2026-04-14

### Added
- Multi-object SAM 2.1 propagation support (dot + box prompts per object).
- BiRefNet fallback matte pass for edge cases where SAM under-segments.
- Live preview wiring in the CEP panel (Golobulus bridge).
- Initial OFX plugin skeleton for DaVinci Resolve (`resolve_plugin/`).
- ComfyUI custom-nodes adapter (`comfyui_plugin/`).

[Unreleased]: https://github.com/stuntworks/corridorkey/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/stuntworks/corridorkey/releases/tag/v0.8.0
