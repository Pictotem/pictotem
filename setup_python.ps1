# setup_python.ps1 - Prepare un Python portable (distribution "embeddable"
# officielle python.org) avec les dependances de l'application, dans
# python-embed\. Execute automatiquement par run.bat au premier lancement.
# Necessite une connexion internet (une seule fois : les lancements suivants
# sont entierement hors-ligne).
#
# Note : ce script ne force PAS $ErrorActionPreference = "Stop" globalement.
# pip et python.exe ecrivent regulierement des avertissements benins sur leur
# sortie d'erreur standard (ex. "WARNING: Cache entry deserialization
# failed...") ; sous Windows PowerShell, si le script appelant redirige cette
# sortie (2>&1) avec ErrorActionPreference=Stop actif, un simple avertissement
# native suffit a interrompre tout le script. On verifie donc explicitement
# $LASTEXITCODE apres chaque appel natif plutot que de se fier au flux d'erreur.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pyVersion  = "3.11.9"
$pyDir      = Join-Path $root "python-embed"
$zipUrl     = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
$zipPath    = Join-Path $root "python-embed.zip"
$getPipUrl  = "https://bootstrap.pypa.io/get-pip.py"
$getPipPath = Join-Path $root "get-pip.py"

Write-Host "Telechargement de Python $pyVersion (portable, ~10 Mo)..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing -ErrorAction Stop

Write-Host "Extraction..."
if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force -ErrorAction Stop }
Expand-Archive -Path $zipPath -DestinationPath $pyDir -Force -ErrorAction Stop
Remove-Item $zipPath -Force -ErrorAction Stop

# La distribution "embeddable" desactive site-packages par defaut : on
# l'active en reecrivant le fichier ._pth (pythonXY._pth).
$pthFile = Get-ChildItem -Path $pyDir -Filter "python*._pth" -ErrorAction Stop | Select-Object -First 1
if (-not $pthFile) {
    throw "Fichier ._pth introuvable dans $pyDir (distribution Python inattendue)."
}
$stdlibZip = ($pyVersion -replace '\.\d+$', '') -replace '\.', ''
@"
python$stdlibZip.zip
.
Lib\site-packages

import site
"@ | Set-Content -Path $pthFile.FullName -Encoding ASCII -ErrorAction Stop

Write-Host "Installation de pip..."
Invoke-WebRequest -Uri $getPipUrl -OutFile $getPipPath -UseBasicParsing -ErrorAction Stop
& "$pyDir\python.exe" $getPipPath --no-warn-script-location
if ($LASTEXITCODE -ne 0) {
    throw "Installation de pip echouee (code $LASTEXITCODE)."
}
Remove-Item $getPipPath -Force -ErrorAction Stop

Write-Host "Installation des dependances de l'application (Flask, OpenCV...)..."
& "$pyDir\python.exe" -m pip install --no-warn-script-location -r (Join-Path $root "app\requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Installation des dependances echouee (code $LASTEXITCODE)."
}

Write-Host ""
Write-Host "Python portable pret dans python-embed\."
