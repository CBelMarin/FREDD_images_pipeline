@echo off
rem Visionneuse FREDD : double-cliquer pour ouvrir la page de visualisation des DICOM / E2E.
rem Fermer cette fenetre arrete la visionneuse. Rien ne sort du poste (127.0.0.1 uniquement).
title Visionneuse FREDD - fermer cette fenetre pour arreter
rem pushd (et non cd) : fonctionne aussi depuis un dossier reseau (\\serveur\...), que cd refuse.
pushd "%~dp0"
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\python-3.12.0-embed-amd64\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONPATH=%~dp0"
"%PY%" visionneuse.py
if errorlevel 1 (
  echo.
  echo La visionneuse n a pas pu demarrer. Verifier que Python est installe.
  pause
)
popd
