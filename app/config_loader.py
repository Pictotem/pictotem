import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

# BASE_DIR centralisé : dossier parent de app/ (contient aussi config/,
# data/, logs/). L'application est lancée via python-embed\python.exe
# app\app.py depuis run.bat, qui se place d'abord dans ce dossier — mais on
# se base sur le chemin réel du fichier plutôt que sur le répertoire courant,
# pour rester correct quel que soit l'endroit d'où le script est appelé.
_env_base = os.environ.get('PHOTOMATON_BASE_DIR', '').strip()
BASE_DIR = Path(_env_base) if _env_base else Path(__file__).resolve().parents[1]

CONFIG_PATH = BASE_DIR / 'config' / 'config.toml'
with open(CONFIG_PATH, 'rb') as _f:
    CONFIG = tomllib.load(_f)


def _resolve_path(value: str) -> Path:
    """Résout un chemin de config relatif à BASE_DIR (ou le garde tel quel s'il est déjà absolu)."""
    p = Path(value)
    return p if p.is_absolute() else (BASE_DIR / p)


DB_PATH       = _resolve_path(CONFIG['storage']['db_path'])
PHOTO_DIR     = _resolve_path(CONFIG['capture']['photo']['save_dir'])
VIDEO_DIR     = _resolve_path(CONFIG['capture']['video']['save_dir'])
THUMBS_DIR    = _resolve_path(CONFIG['storage']['thumbs_dir'])
EXPORTS_DIR   = _resolve_path(CONFIG['storage']['exports_dir'])
EMAILS_JSONL  = _resolve_path(CONFIG['emails']['storage_jsonl'])
MESSAGE_FILE  = _resolve_path(CONFIG['ui']['message_file'])
LOGS_DIR      = _resolve_path(CONFIG['storage']['logs_dir'])
RAW_PHOTO_DIR = _resolve_path(CONFIG['capture']['photo'].get('raw_dir', 'data/photos_raw'))
RAW_VIDEO_DIR = _resolve_path(CONFIG['capture']['video'].get('raw_dir', 'data/videos_raw'))
FRAMES_DIR    = BASE_DIR / 'app' / 'static' / 'frames'

# ffmpeg portable (voir setup_ffmpeg.ps1, telecharge automatiquement par
# run.ps1 dans ffmpeg\ffmpeg.exe) — utilise si present, sinon on retombe sur
# 'ffmpeg' tel quel (PATH global), pratique en dev hors Windows.
_bundled_ffmpeg = BASE_DIR / 'ffmpeg' / 'ffmpeg.exe'
FFMPEG_EXE = str(_bundled_ffmpeg) if _bundled_ffmpeg.is_file() else 'ffmpeg'

ALLOWED_PREVIEW_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
ALLOWED_OVERLAY_EXT = {'.png'}
