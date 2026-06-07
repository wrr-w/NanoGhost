@echo off
chcp 65001 >nul
echo ========================================
echo NanoGhost - Running Built Application
echo ========================================

cd /d "%~dp0dist"

if not exist ".env" (
    echo [*] No .env found, copying .env.example...
    if exist "..\.env" (
        copy "..\.env" ".env" >nul
    )
)

echo Starting NanoGhost...
echo.
echo Interactive CLI mode:
echo   NanoGhost.exe
echo.
echo Gateway mode:
echo   NanoGhost.exe --gateway -I ..\instances\default --port 8000
echo.

NanoGhost.exe %* 2> error.txt
if errorlevel 1 (
    echo.
    echo Program exited with error!
    echo Error output:
    type error.txt
)

echo.
echo Press any key to exit...
pause >nul
