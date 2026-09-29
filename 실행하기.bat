@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 지문·문제 재구성 스튜디오

rem 이 파일이 있는 프로젝트 폴더로 자동 이동합니다.
pushd "%~dp0"

if not exist "main.py" goto missing_files
if not exist "requirements.txt" goto missing_files
if not exist "windows_launcher.pyw" goto missing_files

rem 이미 준비된 경우 설치 과정을 건너뜁니다.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import PyQt6; from google import genai; import pdfplumber, pypdf, pytesseract, PIL, reportlab" >nul 2>nul
    if not errorlevel 1 goto launch
)

cls
echo ============================================================
echo   지문·문제 재구성 스튜디오 - 최초 실행 준비
echo ============================================================
echo.
echo 처음 한 번만 필요한 설치입니다. 잠시 기다려 주세요.
echo 설치가 끝나면 앱이 자동으로 열립니다.
echo.

if exist ".venv\Scripts\python.exe" goto install_packages

where py >nul 2>nul
if not errorlevel 1 goto create_with_py

where python >nul 2>nul
if not errorlevel 1 goto create_with_python

goto no_python

:create_with_py
echo [1/3] Python 환경을 준비하는 중...
py -3 -m venv ".venv"
if errorlevel 1 goto venv_error
goto install_packages

:create_with_python
echo [1/3] Python 환경을 준비하는 중...
python -m venv ".venv"
if errorlevel 1 goto venv_error
goto install_packages

:install_packages
echo [2/3] 필요한 프로그램을 설치하는 중...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto install_error
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto install_error

goto launch

:launch
echo [3/3] 앱을 실행합니다...
start "" ".venv\Scripts\pythonw.exe" "windows_launcher.pyw"
if errorlevel 1 goto launch_error
popd
exit /b 0

:no_python
cls
echo Python이 설치되어 있지 않습니다.
echo.
echo 지금 Python 설치 페이지를 엽니다.
echo 설치 화면에서 반드시 "Add python.exe to PATH"를 선택해 주세요.
echo 설치를 마친 뒤 이 파일을 다시 더블클릭하면 됩니다.
echo.
start "" "https://www.python.org/downloads/windows/"
pause
popd
exit /b 1

:missing_files
cls
echo 앱 실행에 필요한 파일을 찾을 수 없습니다.
echo ZIP 파일의 압축을 모두 푼 폴더 안에서 실행해 주세요.
echo.
pause
popd
exit /b 1

:venv_error
cls
echo Python 환경을 만들지 못했습니다.
echo Python을 다시 설치한 뒤 실행해 주세요.
echo.
pause
popd
exit /b 1

:install_error
cls
echo 필요한 프로그램 설치 중 오류가 발생했습니다.
echo 인터넷 연결을 확인한 뒤 이 파일을 다시 더블클릭해 주세요.
echo.
pause
popd
exit /b 1

:launch_error
cls
echo 앱을 실행하지 못했습니다.
echo 프로젝트 폴더의 "실행오류.txt" 파일이 있다면 보내 주세요.
echo.
pause
popd
exit /b 1
