@echo off
rem Serve the thermwatch store for browsing; pass through --port.
chcp 65001 >nul
python "%~dp0thermview.py" --tag GR-ThermView %*
