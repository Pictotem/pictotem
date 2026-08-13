import io
import logging
import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path

import qrcode
from PIL import Image

from config_loader import CONFIG, MESSAGE_FILE

logger = logging.getLogger('pictotem')

# Cache QR : clé = URL complète. Quand l'IP change, la nouvelle URL
# manque dans le cache → régénération automatique. Les anciennes entrées
# deviennent orphelines mais sont négligeables (quelques Ko).
_QR_CACHE: dict[str, bytes] = {}


def _get_local_ip() -> str:
    """Retourne l'IP de l'interface réseau principale (connexion UDP fictive, aucun paquet envoyé)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0)
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'


def build_gallery_url() -> str:
    """Construit l'URL de la galerie avec l'IP réseau courante.

    Priorité : IP détectée automatiquement. Le port vient de config.toml,
    le chemin de gallery_path. Retombe sur base_url si la détection échoue.
    """
    ip = _get_local_ip()
    port = int(CONFIG['server'].get('port', 80))
    gallery_path = CONFIG['server'].get('gallery_path', '/gallery')
    if ip == '127.0.0.1':
        # Détection échouée : repli sur la valeur de config
        fallback = CONFIG['server'].get('base_url', 'http://127.0.0.1').rstrip('/')
        logger.debug('IP locale non détectée, repli sur base_url : %s', fallback)
        return fallback + gallery_path
    if port == 80:
        return f'http://{ip}{gallery_path}'
    return f'http://{ip}:{port}{gallery_path}'


def generate_qr_png(url: str) -> io.BytesIO:
    """Génère le QR code pour url ou le retourne depuis le cache."""
    if url not in _QR_CACHE:
        logger.info('Génération QR code pour %s', url)
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        _QR_CACHE[url] = buf.getvalue()
    return io.BytesIO(_QR_CACHE[url])


def make_thumb(src_path, dst_path, size=(480, 800)):
    with Image.open(src_path) as im:
        im = im.convert('RGB')
        im.thumbnail(size)
        im.save(dst_path, format='JPEG', quality=82)


def current_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def current_stamp() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def message_text() -> str:
    if CONFIG['ui'].get('message_mode') == 'file' and MESSAGE_FILE.exists():
        return MESSAGE_FILE.read_text(encoding='utf-8').strip()
    return CONFIG['ui'].get('message_text', '').strip()


def _sanitized_printer_name() -> str:
    # Sanitation du nom d'imprimante (config admin, défense en profondeur).
    # Les noms d'imprimantes Windows contiennent souvent des espaces.
    raw_printer = CONFIG['print'].get('printer_name', '').strip()
    return re.sub(r'[^a-zA-Z0-9_.\- ]', '', raw_printer)


def print_photo(path: Path) -> tuple[bool, str]:
    """Impression via mspaint /pt (impression silencieuse native Windows,
    sans dépendance externe). Sans printer_name configuré, utilise
    l'imprimante par défaut de Windows."""
    if not CONFIG['print'].get('enabled', False):
        return False, 'Impression désactivée'

    printer_name = _sanitized_printer_name()
    copies = max(1, int(CONFIG['print'].get('copies', 1)))

    cmd = ['mspaint.exe', '/pt', str(path)]
    if printer_name:
        cmd.append(printer_name)

    logger.info('Commande impression : %s (x%s)', ' '.join(cmd), copies)
    last_output = ''
    for _ in range(copies):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            logger.warning('mspaint introuvable — impression indisponible.')
            return False, 'Impression indisponible : mspaint introuvable sur cette machine.'
        if proc.returncode != 0:
            return False, (proc.stdout or proc.stderr).strip() or 'Échec de l\'impression'
        last_output = (proc.stdout or proc.stderr).strip()
    return True, last_output or 'Impression envoyée'


def validate_printer():
    """Vérifie au démarrage que l'imprimante configurée existe (via PowerShell Get-Printer)."""
    if not CONFIG['print'].get('enabled', False):
        return
    printer_name = _sanitized_printer_name()
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-Command', 'Get-Printer | Select-Object -ExpandProperty Name'],
            capture_output=True, text=True, timeout=5,
        )
        available = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if printer_name:
            if printer_name in available:
                logger.info('Imprimante "%s" disponible.', printer_name)
            else:
                logger.warning(
                    'Imprimante "%s" introuvable parmi : %s',
                    printer_name, ', '.join(available) or '(aucune imprimante détectée)'
                )
        else:
            logger.info('Imprimante par défaut utilisée (%d imprimante(s) détectée(s) sur ce PC).', len(available))
    except FileNotFoundError:
        logger.warning('PowerShell introuvable — vérification imprimante impossible.')
    except subprocess.TimeoutExpired:
        logger.warning('Délai dépassé lors de la vérification de l\'imprimante.')
    except Exception as exc:
        logger.warning('Vérification imprimante échouée : %s', exc)


# ── Démarrage automatique Windows ────────────────────────────────────────────
# Raccourci dans le dossier Démarrage de l'utilisateur courant (pas de droits
# admin requis, contrairement à une entrée registre HKLM ou une tâche
# planifiée système). Se lance à l'ouverture de session Windows.

_STARTUP_SHORTCUT_NAME = 'Pictotem.lnk'


def _startup_shortcut_path() -> Path | None:
    appdata = os.environ.get('APPDATA')
    if not appdata:
        return None
    return (Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
            / 'Startup' / _STARTUP_SHORTCUT_NAME)


def is_autostart_enabled() -> bool:
    path = _startup_shortcut_path()
    return bool(path and path.exists())


def enable_autostart(run_bat_path: Path) -> tuple[bool, str]:
    """Crée le raccourci de démarrage automatique vers run.bat, via PowerShell
    (COM WScript.Shell — pas de dépendance Python supplémentaire)."""
    shortcut_path = _startup_shortcut_path()
    if shortcut_path is None:
        return False, 'Démarrage automatique indisponible sur cette plateforme (Windows requis).'
    try:
        shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f'Dossier Démarrage inaccessible : {exc}'

    ps_cmd = (
        '$ws = New-Object -ComObject WScript.Shell; '
        f'$sc = $ws.CreateShortcut("{shortcut_path}"); '
        f'$sc.TargetPath = "{run_bat_path}"; '
        f'$sc.WorkingDirectory = "{run_bat_path.parent}"; '
        '$sc.WindowStyle = 7; '
        '$sc.Save()'
    )
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return False, 'PowerShell introuvable.'
    except subprocess.TimeoutExpired:
        return False, 'Délai dépassé lors de la création du raccourci.'

    if proc.returncode != 0 or not shortcut_path.exists():
        error = (proc.stderr or proc.stdout).strip() or 'Création du raccourci échouée.'
        logger.warning('Échec activation démarrage automatique : %s', error)
        return False, error

    logger.info('Démarrage automatique Windows activé (%s).', shortcut_path)
    return True, 'Démarrage automatique activé.'


def disable_autostart() -> tuple[bool, str]:
    shortcut_path = _startup_shortcut_path()
    if shortcut_path is None:
        return False, 'Démarrage automatique indisponible sur cette plateforme (Windows requis).'
    try:
        shortcut_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning('Échec désactivation démarrage automatique : %s', exc)
        return False, f'Suppression du raccourci échouée : {exc}'
    logger.info('Démarrage automatique Windows désactivé.')
    return True, 'Démarrage automatique désactivé.'
