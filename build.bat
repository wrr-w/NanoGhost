@echo off
chcp 65001 >nul
echo ========================================
echo NanoGhost - Build Package
echo ========================================

cd /d "%~dp0"

echo [1/3] Setting up virtual environment...
python scripts/generate_env_template.py || exit /b 1
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

echo [3/3] Selecting build mode...

if /i "%1"=="fast" goto :fast
if /i "%1"=="clean" goto :clean
if /i "%1"=="help" goto :help

:clean
echo Building executable with PyInstaller (full clean)...
pyinstaller --clean build.spec
goto :done

:fast
echo Building executable with PyInstaller (incremental)...
pyinstaller build.spec -y
goto :done

:help
echo Usage: build.bat [fast^|clean^|help]
echo   fast   - Incremental build (quick, reuses cache)
echo   clean  - Full rebuild (slow, removes all cache)
echo   help   - Show this help
echo.
echo Default: clean full rebuild
pyinstaller --clean build.spec
goto :done

:done
echo.
echo ========================================
echo Build Complete!
echo ========================================
echo Executable: dist\NanoGhost.exe
echo.
echo Usage:
echo   build.bat        - Full clean rebuild
echo   build.bat fast   - Quick incremental build
echo ========================================

pause
