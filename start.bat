@echo off
REM =====================================================================
REM  SGI-Pasaportes — Arranque automático
REM  Agrega este archivo al Programador de tareas de Windows
REM  para que se ejecute al iniciar sesión (ver README para instrucciones).
REM =====================================================================

echo [SGI] Esperando que Docker Desktop esté listo...
:wait_docker
docker info >nul 2>&1
if errorlevel 1 (
    timeout /t 5 /nobreak >nul
    goto wait_docker
)

echo [SGI] Docker listo. Levantando servicios...
cd /d "%~dp0"
docker compose up -d

echo [SGI] Servicios levantados. Portal disponible en:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr /v "169\."') do (
    for /f "tokens=1 delims= " %%b in ("%%a") do echo     http://%%b:8080
)
echo.
echo [SGI] Listo.
