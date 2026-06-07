@echo off
chcp 65001 >nul
echo ========================================
echo NanoGhost - Build Package
echo ========================================

cd /d "%~dp0"

echo [1/3] Setting up virtual environment...
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)
call venv\Scripts\activate.bat
pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency install failed!
    pause
    exit /b 1
)

echo.
echo [2/3] Installing PyInstaller...
pip install pyinstaller
if errorlevel 1 (
    echo PyInstaller install failed!
    pause
    exit /b 1
)

echo.
echo [3/3] Building executable with PyInstaller...
pyinstaller --clean build.spec

echo.
echo ========================================
echo Build Complete!
echo ========================================
echo Executable: dist\NanoGhost.exe
echo.
echo Usage:
echo   dist\NanoGhost.exe "hello"
echo   dist\NanoGhost.exe --gateway -I instance_dir --port 8000
echo ========================================

pause
