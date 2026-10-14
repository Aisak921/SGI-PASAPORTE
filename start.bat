@echo off
REM =====================================================================
REM  SGI-Pasaportes -- Arranque automatico silencioso
REM  Ruta del proyecto: C:\ProgramData\sgicore  (carpeta oculta del sistema)
REM  El programa arranca en segundo plano sin interrumpir al usuario.
REM =====================================================================

:wait_docker
docker info >nul 2>&1
if errorlevel 1 (
    timeout /t 5 /nobreak >nul
    goto wait_docker
)

cd /d "C:\ProgramData\sgicore"
docker compose up -d >nul 2>&1
