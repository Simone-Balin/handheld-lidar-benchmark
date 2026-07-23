@echo off
rem Launches the app window with no lingering console. If pythonw is missing,
rem falls back to python (which keeps a console so any error stays visible).
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw gui.py) || (python gui.py & if errorlevel 1 pause)
