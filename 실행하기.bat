@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "main.py" goto FILE_ERROR
if not exist "requirements.txt" goto FILE_ERROR
if not exist "windows_launcher.pyw" goto FILE_ERROR

if not exist ".venv\Scripts\python.exe" goto CREATE_ENV

:CHECK_PACKAGES
".venv\Scripts\python.exe" -c "import PyQt6; from google import genai; import pdfplumber, pypdf, pymupdf, pytesseract, PIL, reportlab" >nul 2>&1
if not errorlevel 1 goto START_APP
goto INSTALL_PACKAGES

:CREATE_ENV
cls
echo ================================================
echo   First-time setup - please wait
echo ================================================
echo.
echo [1/3] Preparing Python...
where py >nul 2>&1
if not errorlevel 1 goto CREATE_WITH_PY
where python >nul 2>&1
if not errorlevel 1 goto CREATE_WITH_PYTHON
goto NO_PYTHON

:CREATE_WITH_PY
py -3 -m venv ".venv"
if errorlevel 1 goto SETUP_ERROR
goto INSTALL_PACKAGES

:CREATE_WITH_PYTHON
python -m venv ".venv"
if errorlevel 1 goto SETUP_ERROR
goto INSTALL_PACKAGES

:INSTALL_PACKAGES
echo [2/3] Installing required packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto SETUP_ERROR
goto START_APP

:START_APP
echo [3/3] Starting the app...
start "" ".venv\Scripts\pythonw.exe" "windows_launcher.pyw"
if errorlevel 1 goto START_ERROR
exit /b 0

:NO_PYTHON
cls
echo Python was not found.
echo The Python download page will open now.
echo Select "Add python.exe to PATH" during installation.
echo Then double-click this file again.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:FILE_ERROR
cls
echo Required app files were not found.
echo Extract the entire ZIP file, then run this file inside that folder.
pause
exit /b 1

:SETUP_ERROR
cls
echo Setup failed. Check your internet connection and try again.
echo If it fails again, send a screenshot of this window.
pause
exit /b 1

:START_ERROR
cls
echo The app could not start.
echo Send the "execution error" text file in this folder.
pause
exit /b 1
