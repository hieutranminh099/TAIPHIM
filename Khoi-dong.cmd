@echo off
setlocal
cd /d "%~dp0" || goto fail

if exist ".venv\Scripts\python.exe" goto python_ready
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv || goto fail
    goto python_ready
)
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
    echo Cai Python 3.10 tro len tu https://www.python.org/downloads/windows/
    echo Sau do mo lai Khoi-dong.cmd.
    goto fail
)
python -m venv .venv || goto fail

:python_ready
if not exist ".venv\requirements-da-cai.txt" goto install
fc /b "requirements.txt" ".venv\requirements-da-cai.txt" >nul 2>&1
if errorlevel 1 goto install
goto start

:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto fail
copy /y "requirements.txt" ".venv\requirements-da-cai.txt" >nul || goto fail

:start
echo Mo http://127.0.0.1:8765/ trong trinh duyet. Nhan Ctrl+C de dung.
".venv\Scripts\python.exe" -m tramtai || goto fail
exit /b 0

:fail
echo Khong khoi dong duoc. Hay xem thong bao o tren.
pause
exit /b 1
