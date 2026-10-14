@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  rem Bilesenler henuz kurulmamis: kurulum penceresi acilir, bitince program kendiliginden baslar.
  start "Dogru mu Samet - Kurulum" "%~dp0KUR.bat"
  exit /b 0
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -c "import webview, safetensors" 2>nul
if errorlevel 1 (
  echo Pencere bileseni kuruluyor, bir kerelik...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q pywebview safetensors
)
rem Uygulama penceresi acilir; pencere kapaninca her sey kapanir.
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
