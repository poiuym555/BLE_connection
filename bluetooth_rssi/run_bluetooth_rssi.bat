@echo off
cd /d "%~dp0"
call conda activate bluetooth_rssi
if errorlevel 1 goto error
python -m bluetooth_rssi %*
if errorlevel 1 goto error
goto :eof
:error
pause
