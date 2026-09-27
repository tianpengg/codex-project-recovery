@echo off
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -X utf8 "%~dp0recover_projects.py" %*
exit /b %errorlevel%
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python -X utf8 "%~dp0recover_projects.py" %*
exit /b %errorlevel%
:missing
echo Python not found. Install Python 3.10+ with PATH or the py launcher enabled.
exit /b 1
