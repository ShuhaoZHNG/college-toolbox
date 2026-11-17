@echo off
chcp 65001 >nul
rem Use this launcher if the target app runs as Administrator (e.g. some IDEs / games).
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~dp0run.bat'"
