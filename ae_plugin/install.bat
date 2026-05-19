@echo off
echo =====================================
echo CorridorKey After Effects Installer
echo =====================================
echo.

set PANEL_SRC=%~dp0cep_panel
set PANEL_DEST=%APPDATA%\Adobe\CEP\extensions\com.corridorkey.panel

echo Installing CEP Panel to:
echo %PANEL_DEST%
echo.

:: Enable unsigned extensions (required for development)
echo Enabling unsigned extensions...
reg add "HKCU\Software\Adobe\CSXS.11" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
reg add "HKCU\Software\Adobe\CSXS.10" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
reg add "HKCU\Software\Adobe\CSXS.9" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1

:: Create extensions folder if needed
if not exist "%APPDATA%\Adobe\CEP\extensions" (
    mkdir "%APPDATA%\Adobe\CEP\extensions"
)

:: Remove old version
if exist "%PANEL_DEST%" (
    rmdir /s /q "%PANEL_DEST%"
)

:: Copy panel
xcopy /E /I /Y "%PANEL_SRC%" "%PANEL_DEST%"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo SUCCESS! Panel installed.
    echo.
    echo Restart After Effects, then go to:
    echo   Window ^> Extensions ^> CorridorKey
    echo.
) else (
    echo.
    echo ERROR: Installation failed.
)

echo.
echo =====================================
echo Golobulus Effect (Optional)
echo =====================================
echo.
echo For Rotobrush-style effect, install Golobulus:
echo   https://github.com/mobile-bungalow/golobulus-rs
echo.
echo Then copy corridorkey_effect.py to your Golobulus effects folder.
echo.

pause
