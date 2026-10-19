@echo off
rem AI HUB frissítése – új verzió keresése a GitHubon, rákérdez, majd frissít.
"%~dp0.venv\Scripts\pythonw.exe" "%~dp0updater.py" ui
