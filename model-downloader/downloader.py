"""
CorridorKey Model Downloader
============================
Fetches AI model weights from Cloudflare R2 via presigned URLs issued by the
CorridorKey license server. Weights are cached in a per-OS directory and
re-downloaded only when the sha256 checksum does not match.

Usage (standalone):
    python downloader.py [--model <filename>] [--license-key <key>]

Usage (as a library):
    from downloader import download_all_models
    download_all_models(license_key="USER_LICENSE_KEY")

This file is compiled to a standalone executable by PyInstaller for distribution.
See packaging/README.md for the PyInstaller invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import urllib.request
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — TODO: replace with real values before production
# ---------------------------------------------------------------------------

# URL of the CorridorKey license/auth server endpoint that issues presigned URLs.
# The server receives a license key and returns a short-lived (≈1h) presigned
# R2 URL for each requested model file.
LICENSE_SERVER_URL = "https://TODO_LICENSE_SERVER/api/v1/presign"

# Path to manifest.json (co-located with this script)
MANIFEST_PATH = pathlib.Path(__file__).parent / "manifest.json"

# Number of download retry attempts on checksum failure
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Cache directory resolution
# ---------------------------------------------------------------------------

def get_cache_dir() -> pathlib.Path:
    """
    Return the OS-appropriate model cache directory, creating it if necessary.

    Windows : %APPDATA%\\CorridorKey\\models\\
    macOS   : ~/.corridorkey/models/
    Linux   : ~/.corridorkey/models/  (fallback; not a primary platform)
    """
    if sys.platform == "win32":
        base = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home()))
        cache = base / "CorridorKey" / "models"
    else:
        cache = pathlib.Path.home() / ".corridorkey" / "models"

    cache.mkdir(parents=True, exist_ok=True)
    return cache


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def load_manifest() -> list[dict]:
    """
    Load and return the list of model entries from manifest.json.

    Each entry is a dict with keys:
        name, filename, sha256, r2_key, size_bytes, required, ...
    """
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"manifest.json not found at {MANIFEST_PATH}")

    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    models = data.get("models", [])
    if not models:
        raise ValueError("manifest.json contains no model entries.")
    return models


# ---------------------------------------------------------------------------
# Checksum verification
# ---------------------------------------------------------------------------

def sha256_file(path: pathlib.Path, chunk_size: int = 1 << 20) -> str:
    """
    Compute and return the lowercase hex SHA-256 digest of a file.

    Args:
        path: Path to the file to hash.
        chunk_size: Read chunk size in bytes (default 1 MB).

    Returns:
        Lowercase hex string of the SHA-256 digest.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def checksum_ok(path: pathlib.Path, expected_sha256: str) -> bool:
    """
    Return True if `path` exists and its SHA-256 matches `expected_sha256`.

    If `expected_sha256` starts with 'TODO_', the check is skipped (returns
    False so the file is always downloaded — useful during development before
    final weights are locked).

    Args:
        path: Local file path to check.
        expected_sha256: Hex SHA-256 string from manifest.json.

    Returns:
        True if the file is present and its checksum matches; False otherwise.
    """
    if expected_sha256.startswith("TODO_"):
        # Checksums not yet set — always re-download (dev mode)
        return False

    if not path.exists():
        return False

    actual = sha256_file(path)
    return actual == expected_sha256.lower()


# ---------------------------------------------------------------------------
# License server — presigned URL request
# ---------------------------------------------------------------------------

def request_presigned_url(license_key: str, r2_key: str) -> str:
    """
    Contact the CorridorKey license server and retrieve a presigned URL for
    the given R2 object key.

    Args:
        license_key: The user's CorridorKey license key.
        r2_key: The R2 object key from manifest.json (e.g. 'models/v0.9/...').

    Returns:
        A presigned HTTPS URL string (≈1 hour expiry).

    Raises:
        RuntimeError: If the server rejects the license or returns an error.

    TODO:
        - Replace the stub with a real HTTP POST to LICENSE_SERVER_URL.
        - Handle HTTP 401 (invalid license), 402 (expired), 429 (rate limit).
        - Consider caching the presigned URL for the duration of this run
          (multiple models → multiple server round-trips, all under the 1h window).
    """
    # TODO: implement real HTTP request, e.g.:
    #
    # import urllib.request, urllib.parse, json
    # payload = json.dumps({"license_key": license_key, "r2_key": r2_key}).encode()
    # req = urllib.request.Request(
    #     LICENSE_SERVER_URL,
    #     data=payload,
    #     headers={"Content-Type": "application/json"},
    #     method="POST",
    # )
    # with urllib.request.urlopen(req, timeout=30) as resp:
    #     body = json.loads(resp.read())
    # return body["presigned_url"]

    raise NotImplementedError(
        "License server request not yet implemented. "
        "Set LICENSE_SERVER_URL and implement request_presigned_url()."
    )


# ---------------------------------------------------------------------------
# Download with progress
# ---------------------------------------------------------------------------

def download_file(url: str, dest: pathlib.Path, expected_bytes: int = 0) -> None:
    """
    Download `url` to `dest`, displaying a simple byte-count progress line.

    Args:
        url: The presigned HTTPS URL to download from.
        dest: Local file path to write to (parent directory must exist).
        expected_bytes: Total file size for progress display (0 = unknown).

    TODO:
        - Replace the urllib-based download with `requests` + `tqdm` for a
          proper progress bar and better error handling.
        - Add resume support (Range header) for large files interrupted mid-download.
        - Stream directly to disk; do not buffer entire file in memory.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")

    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            total = int(response.headers.get("Content-Length", expected_bytes) or 0)
            downloaded = 0
            chunk_size = 1 << 20  # 1 MB

            with tmp.open("wb") as fh:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(
                            f"\r    {downloaded / 1e6:.1f} MB / {total / 1e6:.1f} MB"
                            f"  ({pct:.0f}%)",
                            end="",
                            flush=True,
                        )

        print()  # newline after progress
        tmp.rename(dest)

    except Exception:
        # Clean up partial download on failure
        if tmp.exists():
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# Single-model download orchestration
# ---------------------------------------------------------------------------

def download_model(
    entry: dict,
    cache_dir: pathlib.Path,
    license_key: str,
) -> pathlib.Path:
    """
    Ensure one model file is present in `cache_dir` with a valid checksum.

    Algorithm:
        1. Check if file already exists and checksum matches → skip.
        2. Request a presigned URL from the license server.
        3. Download the file, retrying up to MAX_RETRIES times on checksum failure.
        4. Verify final checksum; raise RuntimeError if it never matches.

    Args:
        entry: A model entry dict from manifest.json.
        cache_dir: Local directory to store downloaded weights.
        license_key: The user's CorridorKey license key.

    Returns:
        pathlib.Path to the verified local model file.

    Raises:
        RuntimeError: If the file cannot be verified after MAX_RETRIES attempts.
    """
    filename = entry["filename"]
    expected_sha256 = entry["sha256"]
    r2_key = entry["r2_key"]
    expected_bytes = entry.get("size_bytes", 0)

    dest = cache_dir / filename

    # --- 1. Skip if already present and verified ---
    if checksum_ok(dest, expected_sha256):
        print(f"  [OK] {filename} — already cached and verified.")
        return dest

    print(f"  [DOWNLOAD] {filename}")

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            print(f"    Retry {attempt}/{MAX_RETRIES}...")

        # --- 2. Get presigned URL ---
        url = request_presigned_url(license_key, r2_key)

        # --- 3. Download ---
        download_file(url, dest, expected_bytes)

        # --- 4. Verify checksum ---
        if expected_sha256.startswith("TODO_"):
            print(f"    WARNING: sha256 not set in manifest — skipping checksum.")
            return dest

        actual = sha256_file(dest)
        if actual == expected_sha256.lower():
            print(f"    Checksum OK: {actual[:16]}...")
            return dest

        print(
            f"    Checksum MISMATCH (attempt {attempt}):"
            f" expected {expected_sha256[:16]}... got {actual[:16]}..."
        )
        dest.unlink(missing_ok=True)

    raise RuntimeError(
        f"Failed to download a valid copy of {filename} after {MAX_RETRIES} attempts."
    )


# ---------------------------------------------------------------------------
# Top-level: download all models
# ---------------------------------------------------------------------------

def download_all_models(
    license_key: str,
    cache_dir: Optional[pathlib.Path] = None,
    required_only: bool = False,
) -> None:
    """
    Download all models listed in manifest.json to the local cache.

    Args:
        license_key: The user's CorridorKey license key.
        cache_dir: Override the default OS cache directory (mainly for tests).
        required_only: If True, skip models with required=False (optional variants).
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()

    print(f"Cache directory: {cache_dir}")

    models = load_manifest()

    for entry in models:
        if required_only and not entry.get("required", True):
            print(f"  [SKIP] {entry['filename']} (optional)")
            continue
        download_model(entry, cache_dir, license_key)

    print("\nAll models ready.")
    _write_lock_file(models, cache_dir)


def _write_lock_file(models: list[dict], cache_dir: pathlib.Path) -> None:
    """
    Write models.lock to cache_dir so host adapters can verify weights at startup.

    The lock file is a JSON object mapping filename → sha256.
    """
    lock = {
        entry["filename"]: entry["sha256"]
        for entry in models
        if (cache_dir / entry["filename"]).exists()
    }
    lock_path = cache_dir / "models.lock"
    with lock_path.open("w", encoding="utf-8") as fh:
        json.dump(lock, fh, indent=2)
    print(f"Lock file written: {lock_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="CorridorKey Model Downloader",
        description=(
            "Download CorridorKey AI model weights from Cloudflare R2. "
            "Requires a valid CorridorKey license key."
        ),
    )
    parser.add_argument(
        "--license-key",
        metavar="KEY",
        default=os.environ.get("CORRIDORKEY_LICENSE_KEY", ""),
        help=(
            "Your CorridorKey license key. "
            "Can also be set via the CORRIDORKEY_LICENSE_KEY environment variable."
        ),
    )
    parser.add_argument(
        "--model",
        metavar="FILENAME",
        default=None,
        help=(
            "Download only the model with this filename (e.g. CorridorKey.pth). "
            "If omitted, all models in the manifest are downloaded."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        metavar="PATH",
        default=None,
        help="Override the default OS cache directory.",
    )
    parser.add_argument(
        "--required-only",
        action="store_true",
        help="Download only required models; skip optional SAM variants.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """
    CLI entry point.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    args = _parse_args(argv)

    if not args.license_key:
        print(
            "ERROR: No license key provided. "
            "Pass --license-key or set CORRIDORKEY_LICENSE_KEY.",
            file=sys.stderr,
        )
        return 1

    cache_dir = pathlib.Path(args.cache_dir) if args.cache_dir else None

    try:
        if args.model:
            # Download a single named model
            models = load_manifest()
            matching = [m for m in models if m["filename"] == args.model]
            if not matching:
                print(
                    f"ERROR: No model named '{args.model}' found in manifest.",
                    file=sys.stderr,
                )
                return 1
            if cache_dir is None:
                cache_dir = get_cache_dir()
            download_model(matching[0], cache_dir, args.license_key)
        else:
            download_all_models(
                license_key=args.license_key,
                cache_dir=cache_dir,
                required_only=args.required_only,
            )
    except NotImplementedError as exc:
        print(f"NOT IMPLEMENTED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
