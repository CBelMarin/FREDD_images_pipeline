<#
 Prepare un Python 3.12 PORTABLE avec toutes les dependances, a executer sur un poste
 CONNECTE a Internet (Windows 64 bits). Produit python-portable.zip a importer dans l'environnement sécurisé.

 Usage (PowerShell, depuis la racine du depot) :
   powershell -ExecutionPolicy Bypass -File installation\preparer_python_portable.ps1
   powershell -ExecutionPolicy Bypass -File installation\preparer_python_portable.ps1 -Version 3.12.0 -Sortie C:\prep

 Etat : ecrit d'apres README.md, paragraphes 4 bis et 4 ter ; a valider sur un poste connecte.
#>
param(
  [string]$Version = "3.12.0",
  [string]$Sortie = (Join-Path $PWD "preparation_vm")
)
$ErrorActionPreference = "Stop"
$racine = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dossier = Join-Path $Sortie "python"
New-Item -ItemType Directory -Force -Path $Sortie | Out-Null
if (Test-Path $dossier) { Remove-Item -Recurse -Force $dossier }

$zip = Join-Path $Sortie "python-$Version-embed-amd64.zip"
Write-Host "1/5 Telechargement de Python $Version (version embarquee)"
Invoke-WebRequest "https://www.python.org/ftp/python/$Version/python-$Version-embed-amd64.zip" -OutFile $zip
Expand-Archive $zip -DestinationPath $dossier

Write-Host "2/5 Activation de site-packages (necessaire a pip)"
$pth = Get-ChildItem $dossier -Filter "python*._pth" | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName

Write-Host "3/5 Installation de pip"
$getpip = Join-Path $Sortie "get-pip.py"
Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
& (Join-Path $dossier "python.exe") $getpip

Write-Host "4/5 Installation des dependances figees (requirements-lock.txt)"
& (Join-Path $dossier "python.exe") -m pip install -r (Join-Path $racine "requirements-lock.txt")
& (Join-Path $dossier "python.exe") -m pip install pytest==9.1.1

Write-Host "5/5 sitecustomize.py (l'embarque ignore PYTHONPATH sans lui) puis archive"
$site = Join-Path $dossier "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $site | Out-Null
@'
import os, sys
for p in reversed(os.environ.get("PYTHONPATH", "").split(os.pathsep)):
    if p and p not in sys.path:
        sys.path.insert(0, p)
'@ | Set-Content (Join-Path $site "sitecustomize.py") -Encoding ASCII

$archive = Join-Path $Sortie "python-portable.zip"
if (Test-Path $archive) { Remove-Item $archive }
Compress-Archive -Path $dossier -DestinationPath $archive
Write-Host ""
Write-Host "Termine : $archive"
Write-Host "Importer ce zip et le code du depot dans l'environnement sécurisé : voir installation\INSTALLATION_HORS_LIGNE.md"
