@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Bloom Mushroom Finder - Python Environment Setup
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_project.ps1"
if errorlevel 1 (
  echo.
  echo Environment setup failed. Please review the error above.
  pause
  exit /b 1
)
echo.
echo Bloom Mushroom Finder Python environment and Android phone tools are ready.
pause
