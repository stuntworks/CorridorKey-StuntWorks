@echo off
REM Last modified: 2026-04-14 | Change: Initial Windows setup bootstrap
REM WHAT THIS DOES:
REM   Creates / updates the CorridorKey engine's Python venv with all dependencies
REM   from requirements.txt + requirements-gpu.txt. Idempotent — safe to run repeatedly.
REM DEPENDS-ON: Python 3.10-3.12 on PATH, git installed, the CorridorKey engine folder present.
REM AFFECTS: Creates or populates <engine>\.venv. Does not touch plugin files themselves.

setlocal enabledelayedexpansion

REM --- Locate the engine folder ---
REM Priority: %CORRIDORKEY_ROOT% env var, then ask the user.
if defined CORRIDORKEY_ROOT (
    set "CK_ROOT=%CORRIDORKEY_ROOT%"
    echo Using CORRIDORKEY_ROOT = !CK_ROOT!
) else (
    set /p CK_ROOT=Enter the full path to your CorridorKey engine folder:
)

if not exist "%CK_ROOT%" (
    echo.
    echo ERROR: Engine folder does not exist: %CK_ROOT%
    echo Clone the engine first: git clone https://github.com/cnikiforov/CorridorKey.git
    exit /b 1
)

REM --- Default to CUDA 12.4 unless overridden ---
if "%CK_CUDA%"=="" set "CK_CUDA=cu124"
set "TORCH_INDEX=https://download.pytorch.org/whl/%CK_CUDA%"

echo.
echo CorridorKey engine: %CK_ROOT%
echo PyTorch CUDA build: %CK_CUDA% (set CK_CUDA=cu118 or cu121 to change)
echo Torch index URL:    %TORCH_INDEX%
echo.

REM --- Build or reuse the venv ---
if not exist "%CK_ROOT%\.venv\Scripts\python.exe" (
    echo Creating virtual environment in %CK_ROOT%\.venv ...
    python -m venv "%CK_ROOT%\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create venv. Is python on PATH?
        exit /b 1
    )
) else (
    echo Reusing existing venv at %CK_ROOT%\.venv
)

REM --- Activate and upgrade pip ---
call "%CK_ROOT%\.venv\Scripts\activate.bat"
python -m pip install --upgrade pip wheel setuptools

REM --- Install GPU PyTorch first (from the CUDA index) ---
echo.
echo Installing PyTorch (%CK_CUDA%) ...
pip install -r "%~dp0requirements-gpu.txt" --index-url %TORCH_INDEX%
if errorlevel 1 (
    echo WARNING: CUDA install failed. Falling back to CPU-only torch.
    pip install -r "%~dp0requirements-gpu.txt"
)

REM --- Install the rest ---
echo.
echo Installing runtime dependencies ...
pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo ERROR: Failed to install runtime dependencies.
    exit /b 1
)

REM --- Write the path-discovery file so the plugin can find the engine without env vars ---
echo %CK_ROOT% > "%~dp0corridorkey_path.txt"
echo.
echo Wrote corridorkey_path.txt — the plugin will now find the engine automatically.

REM --- Verify ---
echo.
echo Verifying install ...
python -c "import torch,cv2,numpy,PIL,timm; print('OK: torch',torch.__version__,'cuda',torch.cuda.is_available())"
if errorlevel 1 (
    echo.
    echo ERROR: One or more imports failed. See message above.
    exit /b 1
)

echo.
echo ===========================================================
echo Setup complete. Next:
echo   1. Run: python install.py
echo   2. Restart Resolve / AE / Premiere.
echo ===========================================================
endlocal
