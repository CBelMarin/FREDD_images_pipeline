@echo off
rem Pipeline FREDD : double-cliquer pour ouvrir le menu (verifier, simulation, traitement, visionneuse).
title Pipeline FREDD
rem pushd (et non cd) : fonctionne aussi depuis un dossier reseau (\\serveur\...), que cd refuse.
pushd "%~dp0"
rem Python : d abord un dossier "python" (Python portable), puis "venv", puis celui du profil, puis le systeme.
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\python-3.12.0-embed-amd64\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONPATH=%~dp0"
"%PY%" fredd.py %*
if errorlevel 1 (
  echo.
  echo Une erreur s'est produite. Si Python est introuvable : voir README.md, paragraphe 4 ter.
  pause
)
popd
