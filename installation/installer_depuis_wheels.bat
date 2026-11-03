@echo off
rem A lancer DANS l'environnement sécurisé, depuis le dossier du depot, avec le dossier "wheels" a cote de README.md.
rem Cree un environnement "venv" (FREDD.bat le trouve tout seul) et installe sans acces Internet.
pushd "%~dp0.."
if not exist wheels (
  echo Le dossier "wheels" est introuvable. Voir installation\INSTALLATION_HORS_LIGNE.md.
  popd
  pause
  exit /b 1
)
python --version
python -m venv venv
if errorlevel 1 goto erreur
venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements-lock.txt
if errorlevel 1 goto erreur
venv\Scripts\python.exe -m pip install --no-index --find-links wheels pytest
echo.
echo Installation terminee. Etape suivante : installation\verifier_installation.bat
popd
pause
exit /b 0
:erreur
echo.
echo Echec de l'installation. Noter le message ci-dessus et voir installation\INSTALLATION_HORS_LIGNE.md.
popd
pause
exit /b 1
