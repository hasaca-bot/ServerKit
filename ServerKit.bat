@echo off
rem Windows entry point: opens the ServerKit menu (install / start / stop / status).
rem Pass a command to skip the menu, e.g.  ServerKit.bat start
title ServerKit
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\ServerKit.ps1" %*
if "%~1"=="" exit /b 0
if errorlevel 1 pause
