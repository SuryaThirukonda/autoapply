@echo off
rem goat desktop launcher: starts the Electron shell, which starts the Python agent and shows the dashboard.
cd /d "%~dp0app"
if not exist node_modules\electron (
  echo Installing the desktop shell, one time only
  call npm install
)
start "" npx electron .
