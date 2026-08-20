# update.ps1 - Arrete Pictotem, met a jour le dossier vers la derniere
# version de la branche "main" (GitHub), puis relance l'application. Appele
# par update.bat. Journalise le deroule dans logs\update.log (distinct de
# logs\launcher.log et logs\app.log).
#
# IMPORTANT : la mise a jour reinitialise ce dossier a l'identique
# d'origin/main (git reset --hard), SANS jamais s'arreter sur un message de
# conflit ("local changes would be overwritten", etc.), quel que soit l'etat
# local -- c'est le but de ce script (une mise a jour qui reussit toujours).
# Seule exception : config\config.toml (mots de passe, camera, imprimante,
# ports...) est sauvegarde avant la reinitialisation puis restaure juste
# apres -- vos reglages locaux ne sont donc jamais perdus, meme si le
# fichier a change entre-temps sur GitHub (dans ce cas, comparez a l'occasion
# avec la version du depot pour recuperer d'eventuelles nouvelles options).
# Les donnees (data\), les logs et python-embed\/ffmpeg\ ne sont pas suivis
# par git (voir .gitignore) et ne sont de toute facon jamais affectes.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logsDir = Join-Path $root "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -ErrorAction Stop | Out-Null }
$updateLog = Join-Path $logsDir "update.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $updateLog -Value $line
}

function Write-Relayed {
    param($Line)
    $text = [string]$Line
    if ($text.Trim() -ne '') {
        Write-Host $text
        Add-Content -Path $updateLog -Value $text
    }
}

Write-Log "===================================================="
Write-Log "Mise a jour de Pictotem"

try {
    if (-not (Test-Path (Join-Path $root ".git"))) {
        throw "Ce dossier n'est pas un depot git (pas de sous-dossier .git) -- mise a jour impossible."
    }

    # ── 1. Arret du programme s'il tourne ──────────────────────────────────
    # On cible via la ligne de commande du processus (python-embed\python.exe
    # lance sur CE app\app.py precisement) pour ne jamais toucher a un autre
    # python.exe present sur la machine.
    $appScript = Join-Path $root "app\app.py"
    Write-Log "Recherche d'une instance de Pictotem en cours d'execution..."
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($appScript.ToLower()) }

    if ($procs) {
        foreach ($p in $procs) {
            Write-Log "Arret du processus Pictotem (PID $($p.ProcessId))..."
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
        Write-Log "Programme arrete."
    }
    else {
        Write-Log "Aucune instance de Pictotem en cours d'execution."
    }

    # ── 2. Mise a jour via git ──────────────────────────────────────────────
    # config\config.toml est mis de cote avant la reinitialisation puis
    # restaure juste apres (copie de fichier, pas de git) : vos reglages
    # locaux (mots de passe, camera, imprimante...) ne sont jamais ecrases,
    # meme si le reset --hard remplace tout le reste sans condition.
    $configPath = Join-Path $root "config\config.toml"
    $configBackup = $null
    if (Test-Path $configPath) {
        $configBackup = Join-Path $env:TEMP ("pictotem_config_backup_{0}.toml" -f ([guid]::NewGuid()))
        Copy-Item -Path $configPath -Destination $configBackup -Force
        Write-Log "Configuration locale (config\config.toml) mise de cote avant la mise a jour."
    }

    Write-Log "Recuperation des dernieres modifications (origin/main)..."
    & git -C $root checkout main --quiet 2>&1 | ForEach-Object { Write-Relayed $_ }
    & git -C $root fetch origin main --quiet 2>&1 | ForEach-Object { Write-Relayed $_ }
    if ($LASTEXITCODE -ne 0) { throw "git fetch a echoue (code $LASTEXITCODE) -- verifiez la connexion internet." }

    Write-Log "Reinitialisation sur origin/main (toute modification locale est ecrasee silencieusement)..."
    & git -C $root reset --hard origin/main --quiet 2>&1 | ForEach-Object { Write-Relayed $_ }
    if ($LASTEXITCODE -ne 0) { throw "git reset --hard a echoue (code $LASTEXITCODE)." }

    if ($configBackup -and (Test-Path $configBackup)) {
        Copy-Item -Path $configBackup -Destination $configPath -Force
        Remove-Item -Path $configBackup -Force -ErrorAction SilentlyContinue
        Write-Log "Configuration locale (config\config.toml) restauree."
    }

    $newCommit = (& git -C $root rev-parse --short HEAD 2>$null).Trim()
    Write-Log "Mise a jour terminee (commit $newCommit)."

    # ── 3. Relance ───────────────────────────────────────────────────────────
    Write-Log "Relance de Pictotem (run.bat)..."
    Start-Process -FilePath (Join-Path $root "run.bat") -WorkingDirectory $root
    Write-Log "Pictotem relance."
}
catch {
    Write-Log "ERREUR : $($_.Exception.Message)"
    Write-Host ""
    Write-Host "La mise a jour a echoue. Detail dans logs\update.log." -ForegroundColor Red
    Read-Host "Appuyez sur Entree pour fermer"
    exit 1
}
