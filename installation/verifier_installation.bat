@echo off
rem Controle l'installation : Python, dependances, module pipeline_wp2, configuration, puis suite de tests.
pushd "%~dp0.."
call FREDD.bat verifier
echo.
echo --- Suite de tests (quelques minutes) ---
call FREDD.bat tests
popd
pause
