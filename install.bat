@echo off
setlocal EnableDelayedExpansion
title PRISM installer
cd /d "%~dp0"

echo.
echo  ===================================
echo    PRISM  -  one-time installation
echo  ===================================
echo.

rem ---------- 1. find a REAL Python (the Microsoft Store stub answers 'where' but can't run) ----------
set "PY="
call :probe python
if not defined PY call :probe py -3
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if not defined PY (
  echo  [..] Python not found. Installing Python 3.12 for your user account...
  where winget >nul 2>nul
  if not errorlevel 1 (
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-source-agreements --accept-package-agreements
  ) else (
    echo  [..] winget unavailable - downloading the official installer from python.org
    curl -L -# -o "%TEMP%\python-3.12-installer.exe" https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
    "%TEMP%\python-3.12-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
  )
  rem the new PATH isn't visible to this window yet, so probe the install folder directly
  call :probe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not defined PY call :probe py -3
  if not defined PY (
    echo  [X] Python install didn't complete. Install it from https://www.python.org/downloads/windows/
    echo      ^(tick "Add python.exe to PATH"^) and run this again.
    pause & exit /b 1
  )
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo  [OK] %%v

rem ---------- 2. venv + packages (kept out of OneDrive) ----------
set "VENV=%LOCALAPPDATA%\PRISM\venv"
echo  [..] Creating virtual environment at %VENV%
%PY% -m venv "%VENV%" || (echo  [X] Could not create venv & pause & exit /b 1)

echo  [..] Installing packages (this can take a minute)...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || (echo  [X] pip install failed & pause & exit /b 1)
echo  [OK] Packages installed

rem ---------- 3. the brain ----------
echo.
where claude >nul 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\.local\bin\claude.exe" (
    echo  [OK] Claude Code found: %USERPROFILE%\.local\bin\claude.exe
  ) else (
    echo  [!] The Claude Code command 'claude' was not found on PATH.
    echo      PRISM uses it as its brain. Install Claude Code, then run  claude  once in a
    echo      terminal and sign in. If it lives somewhere unusual, put the full path in
    echo      config.json under brain.claude_path.
  )
) else (
  for /f "tokens=*" %%p in ('where claude') do echo  [OK] Claude Code found: %%p
)

rem ---------- 4. git (for self-updates) ----------
where git >nul 2>nul
if errorlevel 1 (
  echo  [!] git not found - PRISM will run, but won't self-update. Install Git for Windows to enable that.
) else (
  echo  [OK] git found - PRISM.bat will pull updates on launch
)

echo.
echo  Quick checks:
echo     test_voice.bat        hear the British voice
echo     calibrate.bat         tune the clap threshold for your room
echo     PRISM.bat --devices   list microphones
echo.
echo  Then start it with PRISM.bat  -  and clap twice.
echo.
pause
exit /b 0

:probe
rem usage: call :probe <python command>   -> sets PY if it really runs
%* -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%*"
exit /b 0
