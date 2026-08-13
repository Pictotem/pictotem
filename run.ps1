# run.ps1 - Point d'entree reel de l'application (appele par run.bat).
# Journalise le deroule du lancement et toute erreur dans logs\launcher.log,
# en plus de les afficher dans la console. Complementaire de logs\app.log,
# qui ne couvre que ce qui se passe une fois l'application Flask demarree
# (rien n'y est ecrit si Python echoue avant meme d'arriver la).
#
# Note : pas de $ErrorActionPreference = "Stop" global. pip, Werkzeug (le
# serveur de developpement Flask) et python.exe ecrivent regulierement des
# messages benins sur leur sortie d'erreur standard (avertissements, bannieres
# de demarrage). Avec Stop actif, la moindre ligne native sur stderr suffirait
# a interrompre tout le script des qu'elle transite par 2>&1. On se fie donc
# aux codes de sortie ($LASTEXITCODE) et aux erreurs explicitement levees
# (throw, -ErrorAction Stop) pour detecter les vrais echecs.

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logsDir = Join-Path $root "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -ErrorAction Stop | Out-Null }
$launcherLog = Join-Path $logsDir "launcher.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $launcherLog -Value $line
}

function Write-Relayed {
    # Relaie chaque ligne de sortie d'une commande (y compris les
    # avertissements sur la sortie d'erreur standard) vers la console ET le
    # log, sans jamais interrompre le script.
    param($Line)
    $text = [string]$Line
    Write-Host $text
    Add-Content -Path $launcherLog -Value $text
}

Write-Log "===================================================="
Write-Log "Lancement de Photomaton"

try {
    $pythonExe = Join-Path $root "python-embed\python.exe"

    if (-not (Test-Path $pythonExe)) {
        Write-Log "Python portable absent -- preparation en cours (telechargement, une seule fois)..."
        & (Join-Path $root "setup_python.ps1") 2>&1 | ForEach-Object { Write-Relayed $_ }
        if (-not (Test-Path $pythonExe)) {
            throw "La preparation de Python portable a echoue (python.exe toujours absent apres setup_python.ps1)."
        }
        Write-Log "Python portable pret."
    }
    else {
        # Python deja present d'un lancement precedent : on s'assure quand
        # meme que les dependances installees correspondent a
        # requirements.txt (rapide si deja a jour -- pip ne reinstalle que ce
        # qui manque ou a change). Evite d'oublier une reinstallation apres
        # l'ajout d'une dependance au projet (ex. pywebview).
        Write-Log "Verification des dependances Python..."
        & $pythonExe -m pip install --no-warn-script-location -q -r (Join-Path $root "app\requirements.txt") 2>&1 | ForEach-Object { Write-Relayed $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "Verification/installation des dependances Python echouee (code $LASTEXITCODE)."
        }
    }

    # ffmpeg : necessaire uniquement pour la capture video (transcodage,
    # overlay) -- non bloquant si l'installation automatique echoue, la photo
    # continue de fonctionner normalement.
    $ffmpegExe = Join-Path $root "ffmpeg\ffmpeg.exe"
    if (-not (Test-Path $ffmpegExe)) {
        Write-Log "ffmpeg portable absent -- telechargement en cours (une seule fois, ~90 Mo, necessaire pour la capture video)..."
        try {
            & (Join-Path $root "setup_ffmpeg.ps1") 2>&1 | ForEach-Object { Write-Relayed $_ }
        }
        catch {
            Write-Log "ATTENTION : setup_ffmpeg.ps1 a leve une erreur ($($_.Exception.Message))."
        }
        if (Test-Path $ffmpegExe) {
            Write-Log "ffmpeg pret."
        }
        else {
            Write-Log "ATTENTION : ffmpeg n'a pas pu etre installe automatiquement. La capture video sera indisponible (la capture photo fonctionne normalement)."
        }
    }

    Write-Log "Demarrage de l'application (app\app.py)..."
    & $pythonExe (Join-Path $root "app\app.py") 2>&1 | ForEach-Object { Write-Relayed $_ }
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "L'application s'est arretee avec le code d'erreur $exitCode (voir aussi logs\app.log)."
    }
    Write-Log "Application terminee normalement."
}
catch {
    Write-Log "ERREUR : $($_.Exception.Message)"
    Write-Host ""
    Write-Host "Une erreur est survenue. Detail dans logs\launcher.log et logs\app.log." -ForegroundColor Red
    Read-Host "Appuyez sur Entree pour fermer"
    exit 1
}
