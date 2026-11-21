@echo off
chcp 65001 >nul
title EVE LP Planner
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\launch.ps1"
if errorlevel 1 pause
exit /b 0
