# setup_ffmpeg.ps1 - Prepare un ffmpeg portable (build "essentials" statique,
# gyan.dev) dans ffmpeg\ffmpeg.exe. Execute automatiquement par run.ps1 quand
# ce fichier est absent. Necessite une connexion internet (une seule fois :
# les lancements suivants sont entierement hors-ligne).
# Seule la capture video en a besoin (transcodage AVI->MP4, overlay) ; la
# capture photo fonctionne sans. Si ce script echoue, run.ps1 continue quand
# meme (voir l'appel dans run.ps1) : seule la video sera indisponible.
#
# Note : pas de $ErrorActionPreference = "Stop" global, comme les autres
# scripts de ce dossier (setup_python.ps1, run.ps1) -- on prefere des
# -ErrorAction Stop cibles + throw explicites, pour ne jamais interrompre le
# script sur un simple message benin transitant par stderr.

$root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$ffmpegDir = Join-Path $root "ffmpeg"
$ffmpegExe = Join-Path $ffmpegDir "ffmpeg.exe"

if (Test-Path $ffmpegExe) {
    Write-Host "ffmpeg deja present dans ffmpeg\."
    exit 0
}

$zipUrl      = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$zipPath     = Join-Path $env:TEMP "pictotem-ffmpeg-essentials.zip"
$extractPath = Join-Path $env:TEMP ("pictotem-ffmpeg-extract-" + [guid]::NewGuid().ToString())

try {
    Write-Host "Telechargement de ffmpeg (portable, ~90 Mo, une seule fois)..."
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing -ErrorAction Stop

    Write-Host "Extraction..."
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force -ErrorAction Stop

    # Le nom du dossier a l'interieur de l'archive change a chaque version
    # (ex. ffmpeg-7.1-essentials_build\bin\ffmpeg.exe) -- on cherche le
    # binaire au lieu de supposer un chemin fixe.
    $foundExe = Get-ChildItem -Path $extractPath -Recurse -Filter "ffmpeg.exe" -ErrorAction Stop | Select-Object -First 1
    if (-not $foundExe) {
        throw "ffmpeg.exe introuvable dans l'archive telechargee (structure inattendue)."
    }

    New-Item -ItemType Directory -Path $ffmpegDir -Force -ErrorAction Stop | Out-Null
    Copy-Item -Path $foundExe.FullName -Destination $ffmpegExe -Force -ErrorAction Stop

    # ffprobe n'est pas utilise par l'application aujourd'hui, mais on le
    # copie aussi quand il est present : utile pour un futur diagnostic video
    # sans nouveau telechargement.
    $foundProbe = Get-ChildItem -Path (Split-Path $foundExe.FullName -Parent) -Filter "ffprobe.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($foundProbe) {
        Copy-Item -Path $foundProbe.FullName -Destination (Join-Path $ffmpegDir "ffprobe.exe") -Force -ErrorAction Stop
    }

    if (-not (Test-Path $ffmpegExe)) {
        throw "Copie de ffmpeg.exe echouee (fichier absent apres copie)."
    }

    Write-Host ""
    Write-Host "ffmpeg portable pret dans ffmpeg\."
}
finally {
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $extractPath -Recurse -Force -ErrorAction SilentlyContinue
}
