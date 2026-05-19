@echo off
echo ===================================
echo CorridorKey Fusion Plugin Installer
echo ===================================
echo.

set FUSE_SRC=%~dp0CorridorKey.fuse
set FUSE_DEST=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Fuses\CorridorKey.fuse

echo Installing CorridorKey.fuse to:
echo %FUSE_DEST%
echo.

if not exist "%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Fuses" (
    mkdir "%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Fuses"
)

copy /Y "%FUSE_SRC%" "%FUSE_DEST%"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS! Plugin installed.
    echo.
    echo Restart DaVinci Resolve to see CorridorKey in:
    echo   Fusion Page ^> Effects ^> Matte ^> CorridorKey
    echo.
) else (
    echo.
    echo ERROR: Installation failed.
)

pause
