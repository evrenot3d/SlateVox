@echo off
chcp 65001 >nul
title Dogru mu Samet - Kurulum
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kur.ps1"
pause
