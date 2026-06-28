# CorridorKey Model Downloader

A standalone utility that fetches the AI model weights needed by CorridorKey.
Weights are **never** included in the plugin installers or in this git repository.

---

## Why a separate downloader?

| Reason | Detail |
|--------|--------|
| Size | CorridorKey.pth (~280 MB) + two SAM 2.1 variants (~40–80 MB each) = ~380 MB total |
| Licensing | Weights are proprietary / Niko's IP. They must be gated behind a license check before delivery. |
| Bandwidth | Cloudflare R2 has zero egress fees; GitHub Releases has a 2 GB cap per file and charges beyond the free tier. |
| Updates | New model versions can be pushed to R2 without re-releasing the plugin itself. |

---

## Where weights are stored (on-disk cache)

| OS | Cache path |
|----|-----------|
| Windows | `%APPDATA%\CorridorKey\models\` |
| macOS | `~/.corridorkey/models/` |

Every host adapter (Adobe CEP panel, Resolve OFX, ComfyUI nodes) points to this
same directory. There is never a second copy.

---

## Download flow

1. **License check** — the downloader sends the user's license key to the
   CorridorKey auth/license server (TODO: endpoint TBD). The server validates the
   key and returns a short-lived **presigned URL** (≈1 hour expiry) for each
   model file in `manifest.json`.

2. **Checksum skip** — before downloading, the downloader computes the SHA-256 of
   any existing file in the cache. If it matches the manifest, the file is
   skipped. Re-running the downloader after a partial download or after an OS
   reinstall is safe and fast.

3. **Download + verify** — the downloader streams each file from the presigned R2
   URL, shows a progress bar, and verifies the SHA-256 after completion. A
   mismatched checksum triggers a retry (up to 3 attempts), then a hard error.

4. **Ready signal** — on success, the downloader writes a `models.lock` file
   (JSON) to the cache directory listing the verified files and their checksums.
   Host adapters check for this file at startup and surface a clear error if
   weights are missing.

---

## Shipping the downloader

`downloader.py` is intended to be compiled into a standalone executable with
**PyInstaller** so end users do not need a Python installation:

```bash
# TODO: add --onefile build to packaging/build-model-downloader.sh
pyinstaller --onefile model-downloader/downloader.py --name CorridorKeyModelDownloader
```

The resulting binary is shipped alongside each plugin installer (not bundled
inside it).

---

## Model weights are NOT in git

`.gitignore` (repo root) must exclude:

```
*.pth
*.pt
*.ckpt
*.safetensors
models/
```

Never commit model files. The CI release pipeline does not touch weights at all.

---

## Adding a new model

1. Upload the `.pth` / `.pt` file to the R2 bucket under the agreed key prefix.
2. Add an entry to `model-downloader/manifest.json` with the correct `sha256`,
   `r2_key`, and `size_bytes`.
3. Bump the `VERSION` file and add a CHANGELOG entry.
4. Tag a release — the downloader will pull the new file on first run.
