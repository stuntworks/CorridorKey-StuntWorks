#!/usr/bin/env bash
# =============================================================================
# build-comfyui.sh — Package the CorridorKey ComfyUI custom nodes
#
# Output : dist/CorridorKey-v<VERSION>-ComfyUI.zip
#
# Install method for end users:
#   Option A (manual): unzip into ComfyUI/custom_nodes/CorridorKey/
#   Option B (manager): TODO — publish to ComfyUI Registry so users can
#                       install via ComfyUI Manager with one click.
#
# Prerequisites:
#   - Python 3.10+ (uses stdlib zipfile — no extra packages needed)
#   - OR: system zip utility (used as fallback)
#
# Usage:
#   bash packaging/build-comfyui.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve paths relative to repo root
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# 1. Read version
# ---------------------------------------------------------------------------
VERSION_FILE="$REPO_ROOT/VERSION"
if [[ ! -f "$VERSION_FILE" ]]; then
  echo "ERROR: $VERSION_FILE not found." >&2
  exit 1
fi
VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
echo "Building CorridorKey ComfyUI nodes v${VERSION}"

# ---------------------------------------------------------------------------
# 2. Locate ComfyUI plugin source
#    Today's path: comfyui_plugin/
#    Future (after folder cutover): hosts/comfyui-nodes/
# ---------------------------------------------------------------------------
NODES_SRC="$REPO_ROOT/comfyui_plugin"
if [[ ! -d "$NODES_SRC" ]]; then
  echo "ERROR: ComfyUI nodes source not found at $NODES_SRC" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Prepare dist/ directory
# ---------------------------------------------------------------------------
DIST_DIR="$REPO_ROOT/dist"
mkdir -p "$DIST_DIR"
OUTPUT_ZIP="$DIST_DIR/CorridorKey-v${VERSION}-ComfyUI.zip"

# ---------------------------------------------------------------------------
# 4. Build the zip — exclude runtime/cache files
#    Excluded patterns:
#      __pycache__/      — Python bytecode cache directories
#      *.pyc             — compiled bytecode files
#      corridorkey_path.txt — local machine path file written at install time;
#                             must NOT be bundled (it would contain the dev's path)
#      .DS_Store         — macOS metadata
#      *.egg-info        — any accidental pip dev-installs
# ---------------------------------------------------------------------------
echo "[1/2] Creating zip archive..."

# Use Python's zipfile module for cross-platform consistency.
# The nodes are placed inside a top-level 'CorridorKey/' folder in the zip
# so that unzipping into custom_nodes/ produces custom_nodes/CorridorKey/.
python3 - <<PYEOF
import zipfile
import os
import sys

src = "${NODES_SRC}"
out = "${OUTPUT_ZIP}"
prefix = "CorridorKey"  # top-level folder name inside the zip

EXCLUDE_NAMES  = {"__pycache__", ".DS_Store"}
EXCLUDE_EXTS   = {".pyc", ".pyo"}
EXCLUDE_FILES  = {"corridorkey_path.txt"}

with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    file_count = 0
    for dirpath, dirnames, filenames in os.walk(src):
        # Skip excluded directories in-place so os.walk doesn't descend
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_NAMES]

        for filename in filenames:
            if filename in EXCLUDE_FILES:
                continue
            if os.path.splitext(filename)[1] in EXCLUDE_EXTS:
                continue
            if filename in EXCLUDE_NAMES:
                continue

            full_path = os.path.join(dirpath, filename)
            # arcname: strip the leading src path, prepend the zip prefix folder
            rel = os.path.relpath(full_path, src)
            arcname = os.path.join(prefix, rel)
            zf.write(full_path, arcname)
            file_count += 1

print(f"      Packed {file_count} files into {out}")
PYEOF

# ---------------------------------------------------------------------------
# 5. Verify the zip is readable and list its top-level contents
# ---------------------------------------------------------------------------
echo "[2/2] Verifying zip..."
python3 -c "
import zipfile, sys
with zipfile.ZipFile('$OUTPUT_ZIP') as zf:
    names = zf.namelist()
    top = sorted({n.split('/')[0] for n in names})
    print(f'      Top-level entries: {top}')
    print(f'      Total files in archive: {len(names)}')
"

# ---------------------------------------------------------------------------
# 6. TODO — Publish to ComfyUI Registry
#    The ComfyUI Registry (https://registry.comfy.org) requires:
#      - A pyproject.toml in the nodes directory with [tool.comfy] metadata.
#      - The `comfy-cli` tool: pip install comfy-cli
#      - A registry API token (set as CI secret COMFYUI_REGISTRY_TOKEN).
#
#    Once ready, uncomment and test:
#    # comfy node publish --token "$COMFYUI_REGISTRY_TOKEN"
#
#    Until then, distribution is manual zip download from GitHub Releases.
# ---------------------------------------------------------------------------
echo ""
echo "TODO: wire ComfyUI Registry publish step (see script comment above)."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "Build complete: $OUTPUT_ZIP"
echo ""
echo "Install instructions for end users:"
echo "  1. Unzip into <ComfyUI>/custom_nodes/  →  custom_nodes/CorridorKey/"
echo "  2. Run the model downloader to fetch weights."
echo "  3. Restart ComfyUI."
