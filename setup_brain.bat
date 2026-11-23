@echo off
title PRISM - connect the Claude Code brain
cd /d "%~dp0"
color 0B
echo.
echo   ==========================================
echo     PRISM  -  connecting the Claude Code brain
echo   ==========================================
echo.

set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if exist "%CLAUDE%" goto :have
for /f "delims=" %%p in ('where claude 2^>nul') do (set "CLAUDE=%%p" & goto :have)

echo   [1/2] Installing Claude Code (official installer from claude.ai)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
if not exist "%CLAUDE%" (
  echo.
  echo   [X] The installer didn't finish. Check your internet connection and run this again,
  echo       or see https://docs.claude.com/en/docs/claude-code/setup
  echo.
  pause
  exit /b 1
)

:have
echo   [OK] Claude Code: "%CLAUDE%"
"%CLAUDE%" --version
echo.

"%CLAUDE%" auth status --json 2>nul | findstr /c:"\"loggedIn\": true" >nul
if not errorlevel 1 (
  echo   [OK] Already signed in. Prism's brain is ready.
  goto :done
)

echo   [2/2] Signing in with your Claude account...
echo         A browser tab will open - click Authorize, then come back here.
echo.
"%CLAUDE%" auth login --claudeai
echo.
"%CLAUDE%" auth status --json 2>nul | findstr /c:"\"loggedIn\": true" >nul
if errorlevel 1 (
  echo   [!] Sign-in didn't complete. Run setup_brain.bat again to retry.
  echo.
  pause
  exit /b 1
)
echo   [OK] Signed in.

:done
echo.
echo   Prism picks this up automatically within a few seconds -
echo   the BRAIN light on the HUD turns green. No restart needed.
echo.
timeout /t 12
