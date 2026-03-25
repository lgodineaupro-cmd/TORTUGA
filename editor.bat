@echo off
cd /d "%~dp0"
echo ============================================
echo   Syllabus Editor - Demarrage...
echo ============================================
echo.

:: Kill any existing editor on port 9092
netstat -ano 2>nul | findstr ":9092 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo Port 9092 deja utilise, fermeture du processus...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9092 " ^| findstr "LISTENING"') do (
        taskkill /PID %%a /F >nul 2>&1
    )
    ping -n 2 127.0.0.1 >nul
)

echo.
echo   Adresse : http://localhost:9092/
echo.
echo   (Ctrl+C pour arreter le serveur)
echo ============================================
echo.
python editor_app.py
echo.
echo Serveur arrete.
pause
