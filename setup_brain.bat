@echo off
title PRISM - connect the Claude Code brain
cd /d "%~dp0"
echo.
echo  PRISM uses Claude Code (your Claude login) as its brain.
echo.
set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"
where claude >nul 2>nul && for /f "tokens=*" %%p in ('where claude') do set "CLAUDE=%%p"
if exist "%CLAUDE%" goto :login

echo  [..] Installing Claude Code (official installer from claude.ai)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
if not exist "%USERPROFILE%\.local\bin\claude.exe" (
  echo  [X] Install didn't finish. See https://docs.claude.com/en/docs/claude-code/setup
  pause & exit /b 1
)
set "CLAUDE=%USERPROFILE%\.local\bin\claude.exe"

:login
echo  [OK] Claude Code: %CLAUDE%
echo.
echo  Next: Claude Code opens below. Sign in with your Claude account when it asks
echo  (a browser tab opens). Once you see the prompt, type /exit and restart PRISM.
echo.
pause
"%CLAUDE%"
echo.
echo  Done. Restart PRISM.bat - the BRAIN light on the HUD should turn green.
pause
