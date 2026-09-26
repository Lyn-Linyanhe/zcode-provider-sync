@echo off
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
) else (
  start "" python "%~dp0app.py"
)
