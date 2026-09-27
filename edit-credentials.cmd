@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "config\credentials.json" copy /y "config\credentials.example.json" "config\credentials.json" >nul
echo Edit the Splunk credentials, save the file, then close Notepad to continue.
start /wait "" notepad.exe "config\credentials.json"
endlocal
