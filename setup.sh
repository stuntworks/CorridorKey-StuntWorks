#!/usr/bin/env bash
# Last modified: 2026-04-14 | Change: Initial macOS/Linux setup bootstrap
# WHAT THIS DOES:
#   Creates / updates the CorridorKey engine's Python venv with all dependencies
#   from requirements.txt + requirements-gpu.txt. Idempotent — safe to run repeatedly.
# DEPENDS-ON: Python 3.10-3.12 on PATH, git, the CorridorKey engine folder present.
# AFFECTS: Creates or populates <engine>/.venv. Does not touch plugin files themselves.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Locate the engine folder ---
if [[ -n "${CORRIDORKEY_ROOT:-}" ]]; then
    CK_ROOT="$CORRIDORKEY_ROOT"
    echo "Using CORRIDORKEY_ROOT = $CK_ROOT"
else
    read -r -p "Enter the full path to your CorridorKey engine folder: " CK_ROOT
fi

if [[ ! -d "$CK_ROOT" ]]; then
    echo "ERROR: Engine folder does not exist: $CK_ROOT"
    echo "Clone the engine first: git clone https://github.com/cnikiforov/CorridorKey.git"
    exit 1
fi

# --- Default CUDA variant (Linux only — macOS always CPU/MPS) ---
OS="$(uname -s)"
CK_CUDA="${CK_CUDA:-cu124}"
if [[ "$OS" == "Darwin" ]]; then
    TORCH_INDEX=""   # macOS has no CUDA wheels
else
    TORCH_INDEX="https://download.pytorch.org/whl/$CK_CUDA"
fi

echo
echo "CorridorKey engine: $CK_ROOT"
echo "OS:                 $OS"
if [[ -n "$TORCH_INDEX" ]]; then
    echo "Torch index URL:    $TORCH_INDEX  (override with CK_CUDA=cu118 etc.)"
else
    echo "Torch index URL:    (default PyPI — macOS has no CUDA)"
fi
echo

# --- Build or reuse the venv ---
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ ! -x "$CK_ROOT/.venv/bin/python" ]]; then
    echo "Creating virtual environment in $CK_ROOT/.venv ..."
    "$PYTHON_BIN" -m venv "$CK_ROOT/.venv"
else
    echo "Reusing existing venv at $CK_ROOT/.venv"
fi

# shellcheck disable=SC1091
source "$CK_ROOT/.venv/bin/activate"
python -m pip install --upgrade pip wheel setuptools

# --- PyTorch ---
echo
echo "Installing PyTorch ..."
if [[ -n "$TORCH_INDEX" ]]; then
    if ! pip install -r "$SCRIPT_DIR/requirements-gpu.txt" --index-url "$TORCH_INDEX"; then
        echo "WARNING: CUDA install failed. Falling back to CPU-only torch."
        pip install -r "$SCRIPT_DIR/requirements-gpu.txt"
    fi
else
    pip install -r "$SCRIPT_DIR/requirements-gpu.txt"
fi

# --- Runtime deps ---
echo
echo "Installing runtime dependencies ..."
pip install -r "$SCRIPT_DIR/requirements.txt"

# --- Write the path-discovery file ---
echo "$CK_ROOT" > "$SCRIPT_DIR/corridorkey_path.txt"
echo
echo "Wrote corridorkey_path.txt — the plugin will now find the engine automatically."

# --- Verify ---
echo
echo "Verifying install ..."
python -c "import torch,cv2,numpy,PIL,timm; print('OK: torch',torch.__version__,'cuda',torch.cuda.is_available())"

echo
echo "==========================================================="
echo "Setup complete. Next:"
echo "  1. python install.py"
echo "  2. Restart Resolve / AE / Premiere."
echo "==========================================================="
