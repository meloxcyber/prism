@echo off
title PRISM restart
cd /d "%~dp0"
echo Stopping the running Prism...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*prism.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'stopped ' + $_.ProcessId }"
timeout /t 2 /nobreak >nul
echo Starting Prism (pulls the latest from GitHub)...
start "PRISM" cmd /c "%~dp0PRISM.bat"
