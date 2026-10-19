# setup_git.ps1 - Prepare un Git portable (MinGit, build officiel Git for
# Windows) dans git\cmd\git.exe. Execute automatiquement par run.ps1 et
# update.ps1 quand git n'est disponible ni dans git\ ni dans le PATH.
# Necessite une connexion internet (une seule fois). Seule la mise a jour
# (update.bat / back office) en a besoin ; si ce script echoue, run.ps1
# continue quand meme.
#
# Note : pas de $ErrorActionPreference = "Stop" global, comme les autres
# scripts de ce dossier -- -ErrorAction Stop cibles + throw explicites.

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$gitDir = Join-Path $root "git"
$gitExe = Join-Path $gitDir "cmd\git.exe"

if (Test-Path $gitExe) {
    Write-Host "git deja present dans git\."
    exit 0
}

# PowerShell 5.1 negocie par defaut TLS 1.0/1.1 : GitHub exige TLS 1.2+.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$zipPath     = Join-Path $env:TEMP "pictotem-mingit.zip"
$extractPath = Join-Path $env:TEMP ("pictotem-mingit-extract-" + [guid]::NewGuid().ToString())

try {
    Write-Host "Recherche de la derniere version de MinGit..."
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" `
        -Headers @{ "User-Agent" = "Pictotem" } -UseBasicParsing -ErrorAction Stop
    $asset = $release.assets |
        Where-Object { $_.name -match '^MinGit-[\d\.]+-64-bit\.zip$' } |
        Select-Object -First 1
    if (-not $asset) {
        throw "Archive MinGit 64 bits introuvable dans la derniere release de Git for Windows."
    }

    Write-Host ("Telechargement de {0} (~{1} Mo, une seule fois)..." -f $asset.name, [math]::Round($asset.size / 1MB))
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing -ErrorAction Stop

    Write-Host "Extraction..."
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force -ErrorAction Stop

    if (Test-Path $gitDir) { Remove-Item -Path $gitDir -Recurse -Force -ErrorAction Stop }
    New-Item -ItemType Directory -Path $gitDir -Force -ErrorAction Stop | Out-Null
    Copy-Item -Path (Join-Path $extractPath "*") -Destination $gitDir -Recurse -Force -ErrorAction Stop

    if (-not (Test-Path $gitExe)) {
        throw "git.exe absent apres extraction (structure d'archive inattendue)."
    }

    Write-Host ""
    Write-Host "git portable pret dans git\."
}
finally {
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $extractPath -Recurse -Force -ErrorAction SilentlyContinue
}
