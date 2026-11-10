@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Æô¶¯ÏµÍ³.ps1" -Example
if errorlevel 1 pause
