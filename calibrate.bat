@echo off
title PRISM clap calibration
cd /d "%~dp0"
echo Clap twice a few times from where you normally sit. Runs for 20 seconds.
call PRISM.bat --no-update --calibrate
pause
