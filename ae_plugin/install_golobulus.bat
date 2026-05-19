@echo off
echo ================================================
echo CorridorKey After Effects Plugin (Golobulus)
echo ================================================
echo.

set AE_PLUGINS=C:\Program Files\Adobe\Common\Plug-ins\7.0\MediaCore
set GOLOBULUS_SRC=%~dp0golobulus_plugin\Golobulus.aex\install
set EFFECT_SRC=%~dp0golobulus\CorridorKey.py

echo Step 1: Installing Golobulus plugin...
echo Destination: %AE_PLUGINS%\Golobulus
echo.

if not exist "%AE_PLUGINS%" (
    echo ERROR: AE plugins folder not found at:
    echo %AE_PLUGINS%
    echo.
    echo Please check your After Effects installation.
    pause
    exit /b 1
)

:: Create Golobulus folder
if not exist "%AE_PLUGINS%\Golobulus" (
    mkdir "%AE_PLUGINS%\Golobulus"
)

:: Copy Golobulus files
xcopy /E /I /Y "%GOLOBULUS_SRC%\*" "%AE_PLUGINS%\Golobulus\"

echo.
echo Step 2: Installing CorridorKey effect...

:: Create effects folder in Golobulus
if not exist "%AE_PLUGINS%\Golobulus\effects" (
    mkdir "%AE_PLUGINS%\Golobulus\effects"
)

:: Copy CorridorKey effect
copy /Y "%EFFECT_SRC%" "%AE_PLUGINS%\Golobulus\effects\"

echo.
if %ERRORLEVEL% EQU 0 (
    echo ================================================
    echo SUCCESS! Plugin installed.
    echo ================================================
    echo.
    echo Restart After Effects, then apply:
    echo   Effects ^> Golobulus ^> CorridorKey
    echo.
    echo The effect will appear on your layer with:
    echo   - Screen Type selector
    echo   - Despill slider
    echo   - Edge Refiner
    echo   - Auto Despeckle toggle
    echo.
) else (
    echo ERROR: Installation failed.
)

pause
