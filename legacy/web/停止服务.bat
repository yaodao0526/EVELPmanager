@echo off
chcp 65001 >nul
title Stop EVE LP Planner
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop.ps1"
powershell -NoProfile -Command "Start-Sleep -Seconds 2"
