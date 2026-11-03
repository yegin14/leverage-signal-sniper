@echo off
chcp 65001 >nul
title LEVERAGE SIGNAL SNIPER - SUB-SECOND
color 0A

echo ===============================================================
echo        ⚡ LEVERAGE SIGNAL SNIPER - SUB-SECOND ENGINE ⚡
echo              Kaldıraçlı Kripto Sinyal Tarayıcısı
echo ===============================================================
echo.

if not exist ".env" (
    echo [!] .env dosyasi bulunamadi. Varsayilan ayarlarla baslatiliyor...
)

echo [*] Python calistiriliyor...
python main.py

pause
