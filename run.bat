@echo on
set PYTHON_DIR=%~dp0WPy64-31331b3\python
set APP_DIR=%~dp0app

%PYTHON_DIR%\python.exe -m pip install --upgrade pip
%PYTHON_DIR%\python.exe -m pip install -r %APP_DIR%\requirements.txt

cd /d %APP_DIR%
%PYTHON_DIR%\python.exe -m flask run

pause
