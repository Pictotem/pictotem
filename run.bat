@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
if errorlevel 1 (
    echo.
    echo Le lancement a echoue. Voir logs\launcher.log pour le detail.
    pause
)
