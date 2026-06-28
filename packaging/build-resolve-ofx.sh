#!/usr/bin/env bash
# =============================================================================
# build-resolve-ofx.sh — Build and package the CorridorKey OFX plugin
#                        for DaVinci Resolve (also loads in Nuke / VEGAS / Fusion)
#
# Output (Windows) : dist/CorridorKey-v<VERSION>-Resolve-Win.exe
# Output (macOS)   : dist/CorridorKey-v<VERSION>-Resolve-Mac.pkg
#
# IMPORTANT: DaVinci Resolve STUDIO only. The free version of Resolve does not
# load third-party OFX plugins. Make this requirement prominent in the installer
# UI and product page.
#
# Prerequisites (see packaging/README.md for install instructions):
#   - CMake >= 3.24
#   - Ninja (optional but recommended for faster builds)
#   - DaVinci Resolve OFX SDK (headers + lib)  — from Blackmagic Design developer portal
#   - NSIS  (Windows only — for .exe installer)
#   - Xcode Command Line Tools  (macOS only — for pkgbuild / productbuild)
#
# Usage:
#   bash packaging/build-resolve-ofx.sh
#
# Environment variables (optional overrides):
#   OFX_SDK_PATH   path to the OFX SDK directory (default: /opt/openfx-sdk or %OFX_SDK%)
#   BUILD_TYPE     Debug | Release  (default: Release)
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
echo "Building CorridorKey Resolve OFX v${VERSION}"

# ---------------------------------------------------------------------------
# 2. Detect OS  (determines installer tool + output filename)
# ---------------------------------------------------------------------------
OS_TYPE="$(uname -s)"
case "$OS_TYPE" in
  Darwin*)  PLATFORM="mac" ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT) PLATFORM="win" ;;
  *)        PLATFORM="linux" ;;   # Linux OFX is possible but not a primary target
esac
echo "Platform detected: $PLATFORM"

# ---------------------------------------------------------------------------
# 3. Locate OFX plugin source
#    Today's path: resolve_plugin/
#    Future (after folder cutover): hosts/resolve-ofx/
# ---------------------------------------------------------------------------
OFX_SRC="$REPO_ROOT/resolve_plugin"
if [[ ! -d "$OFX_SRC" ]]; then
  echo "ERROR: OFX plugin source not found at $OFX_SRC" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Prepare output dirs
# ---------------------------------------------------------------------------
DIST_DIR="$REPO_ROOT/dist"
BUILD_DIR="$REPO_ROOT/_build/resolve-ofx"
mkdir -p "$DIST_DIR" "$BUILD_DIR"

if [[ "$PLATFORM" == "win" ]]; then
  OUTPUT_INSTALLER="$DIST_DIR/CorridorKey-v${VERSION}-Resolve-Win.exe"
else
  OUTPUT_INSTALLER="$DIST_DIR/CorridorKey-v${VERSION}-Resolve-Mac.pkg"
fi

# ---------------------------------------------------------------------------
# 5. CMake configure
#    TODO: fill in real CMake options once CMakeLists.txt exists in resolve_plugin/
# ---------------------------------------------------------------------------
echo "[1/4] CMake configure..."
OFX_SDK_PATH="${OFX_SDK_PATH:-/opt/openfx-sdk}"
BUILD_TYPE="${BUILD_TYPE:-Release}"

# TODO: uncomment and adjust once CMakeLists.txt is ready:
# cmake -S "$OFX_SRC" \
#       -B "$BUILD_DIR" \
#       -G Ninja \
#       -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
#       -DOFX_SDK_PATH="$OFX_SDK_PATH" \
#       -DCORRIDORKEY_VERSION="$VERSION"
echo "      (TODO: uncomment cmake configure step)"

# ---------------------------------------------------------------------------
# 6. CMake build
# ---------------------------------------------------------------------------
echo "[2/4] CMake build..."
# TODO: uncomment:
# cmake --build "$BUILD_DIR" --config "$BUILD_TYPE" --parallel
echo "      (TODO: uncomment cmake build step)"

# OFX bundle output expected at:
OFX_BUNDLE="$BUILD_DIR/CorridorKey.ofx.bundle"
# TODO: assert bundle exists after build

# ---------------------------------------------------------------------------
# 7. Wrap in OS installer
# ---------------------------------------------------------------------------
echo "[3/4] Creating OS installer..."

if [[ "$PLATFORM" == "win" ]]; then
  # Windows — NSIS (.exe) installer
  # TODO: write corridorkey.nsi script that:
  #   - Copies CorridorKey.ofx.bundle into  C:\Program Files\Common Files\OFX\Plugins\
  #   - Adds an uninstaller
  #   - Shows the "Resolve Studio required" notice on the installer splash
  #
  # makensis packaging/corridorkey.nsi \
  #   /DVERSION="$VERSION" \
  #   /DOFX_BUNDLE="$OFX_BUNDLE" \
  #   /DOUTPUT="$OUTPUT_INSTALLER"
  echo "      (TODO: NSIS installer — see packaging/corridorkey.nsi)"

elif [[ "$PLATFORM" == "mac" ]]; then
  # macOS — pkgbuild + productbuild (.pkg) installer
  # OFX install path on macOS: /Library/OFX/Plugins/
  #
  # TODO:
  # pkgbuild --root "$OFX_BUNDLE" \
  #          --identifier "com.stuntworks.corridorkey.ofx" \
  #          --version "$VERSION" \
  #          --install-location "/Library/OFX/Plugins/CorridorKey.ofx.bundle" \
  #          "$DIST_DIR/_corridorkey-component.pkg"
  # productbuild --distribution packaging/distribution.xml \
  #              --package-path "$DIST_DIR" \
  #              "$OUTPUT_INSTALLER"
  echo "      (TODO: pkgbuild/productbuild — see packaging/distribution.xml)"
fi

echo "      Placeholder installer written to $OUTPUT_INSTALLER"
echo "PLACEHOLDER — not a real installer" > "$OUTPUT_INSTALLER"

# ---------------------------------------------------------------------------
# 8. Verify installer (smoke test)
# ---------------------------------------------------------------------------
echo "[4/4] Verifying installer artifact..."
if [[ -f "$OUTPUT_INSTALLER" ]]; then
  echo "      Artifact exists: $OUTPUT_INSTALLER ($(wc -c < "$OUTPUT_INSTALLER") bytes)"
else
  echo "ERROR: installer not found at $OUTPUT_INSTALLER" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "Build complete: $OUTPUT_INSTALLER"
echo ""
echo "Reminder: Resolve Studio is required. Free Resolve will NOT load this plugin."
echo ""
echo "Next steps:"
echo "  - Write CMakeLists.txt in resolve_plugin/ (OFX C++ build)."
echo "  - Write packaging/corridorkey.nsi  (Windows NSIS script)."
echo "  - Write packaging/distribution.xml (macOS productbuild spec)."
echo "  - Test install on a Resolve Studio 19+ machine."
