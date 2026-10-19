@echo off
chcp 65001 >nul
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }; SlateVox durduruldu. } else { Zaten calismiyor. }"
taskkill /F /IM llama-server.exe >nul 2>&1
timeout /t 3 >nul
