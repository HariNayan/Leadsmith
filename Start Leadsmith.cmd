@echo off
rem Double-click to open the Leadsmith dashboard. Keep this window open while you use it.
title Leadsmith dashboard
cd /d "%~dp0"
where uv >nul 2>nul || (
  echo uv is not installed. Install it from https://docs.astral.sh/uv/ and try again.
  pause
  exit /b 1
)
uv run leadsmith
echo.
echo The dashboard stopped. Close this window, or press a key to exit.
pause >nul
