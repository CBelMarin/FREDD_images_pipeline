@echo off
rem Telecharge toutes les dependances (fichiers .whl) sur un poste CONNECTE, pour une installation
rem hors-ligne dans l'environnement sécurisé. A lancer avec LA MEME version de Python et le MEME systeme que l'environnement sécurisé.
rem Si ce n'est pas le cas, voir README.md, paragraphe 4 (options --platform et --python-version).
pushd "%~dp0.."
python --version
if exist wheels rmdir /s /q wheels
python -m pip download -r requirements-lock.txt -d wheels
python -m pip download pytest==9.1.1 -d wheels
if errorlevel 1 (
  echo Echec du telechargement.
  popd
  pause
  exit /b 1
)
echo.
echo Termine : dossier "wheels" cree. L'importer dans l'environnement sécurisé avec le code du depot.
popd
pause
