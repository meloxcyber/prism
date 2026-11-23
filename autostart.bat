@echo off
rem Creates (or removes) a Startup-folder shortcut so PRISM launches, minimised, when you sign in to Windows.
rem   autostart.bat          -> install
rem   autostart.bat remove   -> uninstall
setlocal
cd /d "%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\PRISM.lnk"

if /i "%~1"=="remove" (
  if exist "%LNK%" del "%LNK%" && echo Removed PRISM from startup.
  exit /b 0
)

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%~dp0PRISM.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.WindowStyle=7;" ^
  "$s.Description='PRISM voice assistant';" ^
  "$s.Save()"
if exist "%LNK%" (echo PRISM will now start minimised when you sign in.) else (echo Could not create the shortcut.)
pause
