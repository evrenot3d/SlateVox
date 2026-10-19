@echo off
chcp 65001 >nul
title SlateVox - Kurulum dosyasi olusturuluyor
cd /d "%~dp0"

rem Surum numarasi engine.py icindeki VERSION satirindan alinir
set "VER="
for /f tokens^=2^ delims^=^" %%v in ('findstr /b /c:"VERSION = " ..\engine.py') do set "VER=%%v"
if not defined VER set "VER=0.0.0"
echo Surum: %VER%

rem ---- 1) Kuruluma gomulecek Python: resmi Python 3.12, nuget paketi, bir kez indirilir
if not exist "python\python.exe" (
  echo Kuruluma eklenecek Python indiriliyor, bir kerelik...
  if exist "_py" rmdir /s /q "_py"
  curl.exe -L --fail -o "_py.zip" "https://www.nuget.org/api/v2/package/python/3.12.10"
  if errorlevel 1 goto pyfail
  rem Windows'un kendi tar araci ZIP acar; olmazsa PowerShell Expand-Archive denenir
  mkdir "_py" >nul 2>&1
  tar -xf "_py.zip" -C "_py" >nul 2>&1
  if not exist "_py\tools\python.exe" powershell -NoProfile -Command "Expand-Archive -LiteralPath '_py.zip' -DestinationPath '_py' -Force"
  if not exist "_py\tools\python.exe" goto pyfail
  move "_py\tools" "python" >nul
  rmdir /s /q "_py"
  del "_py.zip"
)
if not exist "python\pythonw.exe" (
  echo HATA: python\pythonw.exe bulunamadi.
  goto pyfail
)
if not exist "python\Lib\site-packages\webview" (
  echo Pencere bileseni pywebview ekleniyor...
  "python\python.exe" -m ensurepip --upgrade >nul
  "python\python.exe" -m pip install --disable-pip-version-check -q --no-warn-script-location pywebview
  if errorlevel 1 goto pyfail
)

rem ---- 2) Inno Setup
call :findiscc
if not defined ISCC (
  echo Inno Setup bulunamadi. Ucretsiz kurulum araci winget ile kuruluyor, bir kerelik...
  winget install -e --id JRSoftware.InnoSetup --scope user --silent --accept-package-agreements --accept-source-agreements
  call :findiscc
)
if not defined ISCC (
  echo.
  echo Inno Setup kurulamadi. https://jrsoftware.org/isdl.php adresinden kurup bu dosyayi yeniden calistirin.
  pause
  exit /b 1
)

rem ---- 3) Kurulum dosyasi
"%ISCC%" /DAppVer=%VER% SlateVox.iss
if errorlevel 1 (
  echo.
  echo HATA: kurulum dosyasi olusturulamadi.
  pause
  exit /b 1
)
echo.
echo Hazir: %~dp0cikti\SlateVox-Kurulum-%VER%.exe
explorer "%~dp0cikti"
pause
exit /b 0

:pyfail
echo.
echo HATA: Python hazirlanamadi. Internet baglantinizi denetleyip yeniden deneyin.
pause
exit /b 1

:findiscc
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
exit /b 0
