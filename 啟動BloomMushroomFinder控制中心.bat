@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Bloom Mushroom Finder Control Center

if not exist "pyproject.toml" goto :project_missing

if not exist ".venv\Scripts\python.exe" (
  echo [Bloom Mushroom Finder] 第一次啟動，正在建立 Python 環境...
  call "安裝環境.bat"
  if errorlevel 1 goto :setup_failed
)

set "PYTHONPATH=%~dp0scripts\uv_python_compat"
".venv\Scripts\python.exe" -c "import bloompilot, fastapi, uvicorn" >nul 2>&1
if errorlevel 1 goto :environment_broken

if /i "%~1"=="--check" (
  where node.exe >nul 2>&1
  if errorlevel 1 goto :node_missing
  where npm.cmd >nul 2>&1
  if errorlevel 1 goto :node_missing
  ".venv\Scripts\python.exe" -c "from bloompilot.web.server import create_app; print('Bloom Mushroom Finder backend check: OK')"
  if errorlevel 1 exit /b 1
  if not exist "frontend\node_modules\electron\package.json" goto :frontend_missing
  pushd "frontend"
  call npm.cmd run typecheck
  set "CHECK_RESULT=%errorlevel%"
  popd
  exit /b !CHECK_RESULT!
)

where node.exe >nul 2>&1
if errorlevel 1 goto :node_missing
where npm.cmd >nul 2>&1
if errorlevel 1 goto :node_missing

if not exist "frontend\node_modules\electron\package.json" (
  echo [Bloom Mushroom Finder] 第一次啟動，正在安裝 Electron / React 前端...
  pushd "frontend"
  call npm.cmd install --no-audit --no-fund
  set "NPM_RESULT=%errorlevel%"
  popd
  if not "!NPM_RESULT!"=="0" goto :frontend_setup_failed
)

pushd "frontend"
call npm.cmd run start
set "RUNTIME_RESULT=%errorlevel%"
popd
if not "%RUNTIME_RESULT%"=="0" goto :runtime_failed
exit /b 0

:project_missing
echo [Bloom Mushroom Finder] 找不到 pyproject.toml；請確認整個專案目錄完整。
pause
exit /b 1

:setup_failed
echo [Bloom Mushroom Finder] Python 環境安裝失敗。
pause
exit /b 1

:environment_broken
echo [Bloom Mushroom Finder] .venv 或 FastAPI backend 不完整，請重新執行 安裝環境.bat。
pause
exit /b 1

:node_missing
echo [Bloom Mushroom Finder] 找不到 Node.js / npm。Electron UI 需要 Node.js 18 以上版本。
pause
exit /b 1

:frontend_missing
echo [Bloom Mushroom Finder] 尚未安裝 frontend node_modules，請先正常啟動一次或執行 安裝環境.bat。
exit /b 1

:frontend_setup_failed
echo [Bloom Mushroom Finder] Electron / React 前端安裝失敗。
pause
exit /b 1

:runtime_failed
echo [Bloom Mushroom Finder] 程式啟動失敗，錯誤訊息如上。
pause
exit /b 1
