@echo off
rem AI HUB telepítése – dupla kattintás. Rendszergazdai jog nem kell.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
