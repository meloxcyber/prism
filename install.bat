@echo off
setlocal
title PRISM installer
cd /d "%~dp0"

echo.
echo  ===================================
echo    PRISM  -  one-time installation
echo  ===================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  [X] Python was not found.
  echo      Install Python 3.10+ from https://www.python.org/downloads/windows/
  echo      and tick "Add python.exe to PATH" during setup, then run this again.
  pause & exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do echo  [OK] %%v

set "VENV=%LOCALAPPDATA%\PRISM\venv"
echo  [..] Creating virtual environment at %VENV%
python -m venv "%VENV%" || (echo  [X] Could not create venv & pause & exit /b 1)

echo  [..] Installing packages (this can take a minute)...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || (echo  [X] pip install failed & pause & exit /b 1)
echo  [OK] Packages installed

echo.
where claude >nul 2>nul
if errorlevel 1 (
  echo  [!] The Claude Code command 'claude' was not found on PATH.
  echo      PRISM uses it as its brain. Install Claude Code, then run  claude  once in a
  echo      terminal and sign in. If it lives somewhere unusual, put the full path in
  echo      config.json under brain.claude_path.
) else (
  for /f "tokens=*" %%p in ('where claude') do echo  [OK] Claude Code found: %%p
)

echo.
echo  Quick checks you can run now:
echo     PRISM.bat --say "Good morning"      (test the British voice)
echo     PRISM.bat --devices                 (list microphones)
echo     PRISM.bat --calibrate               (tune the clap threshold)
echo.
echo  Then start it with PRISM.bat  -  and clap twice.
echo.
pause
