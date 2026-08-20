@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
if errorlevel 1 (
    echo.
    echo La mise a jour a echoue. Voir logs\update.log pour le detail.
    pause
)
