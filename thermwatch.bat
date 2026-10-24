@echo off
rem One sample of the paired Govee thermometers; pass through any flags (--list, --scan-seconds, ...).
chcp 65001 >nul
python "%~dp0thermwatch.py" --tag GR-ThermView %*
