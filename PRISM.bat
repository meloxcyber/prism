@echo off
title PRISM
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\PRISM\venv"
set "REPO=https://github.com/meloxcyber/prism.git"

if not exist "%VENV%\Scripts\python.exe" (
  echo Run install.bat first.
  pause & exit /b 1
)

rem ---------- argument parsing: --no-update is ours, everything else goes to prism.py ----------
set "ARGS="
set "SKIP_UPDATE="
:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--no-update" (set "SKIP_UPDATE=1") else (set ARGS=%ARGS% %1)
shift
goto :parse
:parsed

rem ---------- self-update from GitHub (skipped with:  PRISM.bat --no-update) ----------
if defined SKIP_UPDATE goto :run
where git >nul 2>nul || (echo [PRISM] git not found - skipping update check. & goto :run)

if not exist ".git" (
  echo [PRISM] Linking this folder to %REPO% ...
  git init -q
  git remote add origin "%REPO%"
  git fetch -q --depth 1 origin main && git reset -q --hard origin/main && git branch -q -M main && git branch -q --set-upstream-to=origin/main main
  if errorlevel 1 (echo [PRISM] Could not link to GitHub - continuing with local files.) else (echo [PRISM] Linked. Future launches auto-update.)
  goto :deps
)

for /f %%h in ('git rev-parse HEAD 2^>nul') do set "BEFORE=%%h"
git pull -q --ff-only origin main
for /f %%h in ('git rev-parse HEAD 2^>nul') do set "AFTER=%%h"
if not "%BEFORE%"=="%AFTER%" (
  echo [PRISM] Updated to %AFTER:~0,7%
  goto :deps
)
goto :run

:deps
"%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt

:run
"%VENV%\Scripts\python.exe" prism.py %ARGS%
if errorlevel 1 pause
