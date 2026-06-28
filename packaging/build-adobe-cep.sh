#!/usr/bin/env bash
# =============================================================================
# build-adobe-cep.sh — Build and sign the CorridorKey Adobe CEP extension
#
# Output : dist/CorridorKey-v<VERSION>-Adobe.zxp
# Hosts  : After Effects (AEFT) + Premiere Pro (PPRO) — one ZXP covers both
#           because manifest.xml lists both CSXSExtension Host entries.
#
# Prerequisites (see packaging/README.md for install instructions):
#   - Node.js >= 18
#   - bolt-cep  (npm install -g bolt-cep)
#   - ZXPSignCmd on $PATH  (download from Adobe Exchange)
#   - openssl on $PATH     (usually pre-installed on mac/linux)
#
# Usage:
#   bash packaging/build-adobe-cep.sh
#
# Environment variables (optional overrides):
#   CERT_P12    path to an existing .p12 certificate file
#   CERT_PASS   password for the .p12 file
#               (if not set, a self-signed cert is generated — OK for dev,
#                NOT for distribution; swap in a real cert before publishing)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Resolve paths relative to repo root (this script lives in packaging/)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# 1. Read version from the single source of truth
# ---------------------------------------------------------------------------
VERSION_FILE="$REPO_ROOT/VERSION"
if [[ ! -f "$VERSION_FILE" ]]; then
  echo "ERROR: $VERSION_FILE not found. Cannot determine version." >&2
  exit 1
fi
VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
echo "Building CorridorKey Adobe CEP v${VERSION}"

# ---------------------------------------------------------------------------
# 2. Locate the CEP panel source
#    Today's path: ae_plugin/cep_panel/
#    Future (after folder cutover): hosts/adobe-cep/
# ---------------------------------------------------------------------------
PANEL_SRC="$REPO_ROOT/ae_plugin/cep_panel"
if [[ ! -d "$PANEL_SRC" ]]; then
  echo "ERROR: CEP panel source not found at $PANEL_SRC" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Prepare dist/ output directory
# ---------------------------------------------------------------------------
DIST_DIR="$REPO_ROOT/dist"
mkdir -p "$DIST_DIR"

OUTPUT_ZXP="$DIST_DIR/CorridorKey-v${VERSION}-Adobe.zxp"

# ---------------------------------------------------------------------------
# 4. Build the panel with Bolt CEP
#    TODO: confirm the exact bolt-cep command once the panel adopts it.
#    If the panel currently has its own build step (e.g. npm run build),
#    replace the bolt command with that invocation.
# ---------------------------------------------------------------------------
echo "[1/4] Building panel with Bolt CEP..."
# TODO: adjust flags once panel's package.json is set up:
#   bolt build --host AEFT --host PPRO --out "$DIST_DIR/panel-build"
#
# For now, use the panel source directory directly as the unsigned payload.
UNSIGNED_DIR="$DIST_DIR/_unsigned_panel"
rm -rf "$UNSIGNED_DIR"
cp -r "$PANEL_SRC" "$UNSIGNED_DIR"
echo "      (TODO: replace copy with real bolt-cep build step)"

# ---------------------------------------------------------------------------
# 5. Code-signing certificate setup
#    CI: generate a self-signed cert. Production: supply CERT_P12 + CERT_PASS.
#
#    macOS vs Windows note:
#      - ZXPSignCmd works on both platforms but the -selfSignedCert flag
#        generates a certificate in the current directory.
#      - On macOS the Keychain may require an additional codesign pass;
#        on Windows the signing is entirely via ZXPSignCmd.
#      TODO: add per-OS branching here once CI matrix is proven.
# ---------------------------------------------------------------------------
echo "[2/4] Preparing signing certificate..."

CERT_DIR="$DIST_DIR/_cert"
mkdir -p "$CERT_DIR"

if [[ -n "${CERT_P12:-}" ]]; then
  echo "      Using supplied certificate: $CERT_P12"
  P12_FILE="$CERT_P12"
  P12_PASS="${CERT_PASS:-}"
else
  echo "      Generating self-signed certificate (dev only — not for distribution)."
  P12_FILE="$CERT_DIR/corridorkey-dev.p12"
  P12_PASS="corridorkey-dev"
  # TODO: ZXPSignCmd -selfSignedCert flag generates a self-signed PFX/P12.
  # The exact call depends on the installed ZXPSignCmd version:
  #   ZXPSignCmd -selfSignedCert US NY "CorridorKey" "StuntWorks" "$P12_PASS" "$P12_FILE"
  echo "      (TODO: uncomment ZXPSignCmd -selfSignedCert call above)"
fi

# ---------------------------------------------------------------------------
# 6. Package and sign with ZXPSignCmd
#    TODO: uncomment once cert handling in step 5 is wired up.
# ---------------------------------------------------------------------------
echo "[3/4] Signing and packaging ZXP..."
# ZXPSignCmd -sign "$UNSIGNED_DIR" "$OUTPUT_ZXP" "$P12_FILE" "$P12_PASS"
echo "      (TODO: uncomment ZXPSignCmd -sign call above)"
echo "      Placeholder output written to $OUTPUT_ZXP"
# Placeholder so CI artifacts step does not fail on a missing file:
echo "PLACEHOLDER — not a real ZXP" > "$OUTPUT_ZXP"

# ---------------------------------------------------------------------------
# 7. Verify the ZXP (optional sanity check)
#    TODO: uncomment once signing is live.
# ---------------------------------------------------------------------------
echo "[4/4] Verifying signed ZXP..."
# ZXPSignCmd -verify "$OUTPUT_ZXP" -certinfo
echo "      (TODO: uncomment ZXPSignCmd -verify call above)"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "Build complete: $OUTPUT_ZXP"
echo ""
echo "Next steps:"
echo "  - Replace the self-signed cert with a real Adobe-trusted cert."
echo "  - Wire the Bolt CEP build step (step 4 above)."
echo "  - Test install via ZXPInstaller or Anastasiy's Extension Manager."
