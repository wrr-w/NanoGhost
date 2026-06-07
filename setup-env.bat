@echo off
chcp 65001 >nul
echo ========================================
echo NanoGhost - Setup Virtual Environment
echo ========================================

cd /d "%~dp0"

if not exist "venv" (
    echo [1/2] Creating virtual environment...
    python -m venv venv
) else (
    echo [1/2] Virtual environment exists, skipping...
)

echo [2/2] Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt

echo.
echo ========================================
echo Setup Complete!
echo ========================================
echo Run CLI:
echo   python run.py
echo.
echo Run Gateway:
echo   python run.py --gateway -I instances\default --port 8000
echo.
echo Or use the nanoghost CLI:
echo   call venv\Scripts\activate.bat
echo   nanoghost instance create default
echo   nanoghost gateway start -I default
echo ========================================

pause
