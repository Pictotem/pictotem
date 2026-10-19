"""Export / import du paramétrage complet de Pictotem (codes invités inclus).

Le bundle est un ZIP contenant :
  - manifest.json : réglages (table `settings`), tables de configuration et
    liste des fichiers embarqués ;
  - files/<clé>/<nom> : les images / polices référencées par ces tables
    (cadres, fonds promo, écran de veille, etc.).

Ne contient JAMAIS de contenu d'événement (captures, votes, e-mails, uploads
invités, tags posés sur les captures, photos de rétrospective).

Module volontairement sans dépendance sur app.py : les connexions SQLite et
les dossiers sont passés en paramètres (voir `config_dirs` dans app.py),
ce qui le rend testable seul.
"""
import json
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import PurePosixPath

FORMAT = 'pictotem-config'
VERSION = 1

# table -> (clé de dossier pour ses fichiers, colonnes contenant un nom de fichier)
CONFIG_TABLES = {
    'guest_codes':             (None, ()),
    'frames':                  ('frames', ('preview_filename', 'overlay_filename')),
    'charte_colors':           (None, ()),
    'tags':                    (None, ()),
    'custom_fonts':            ('fonts', ('filename',)),
    'promo_backgrounds':       ('promo', ('filename',)),
    'promo_content_images':    ('promo_content', ('filename',)),
    'promo_pages':             (None, ()),
    'promo_page_schedules':    (None, ()),
    'slideshow_images':        ('slideshow', ('filename',)),
    'screensaver_images':      ('screensaver', ('filename',)),
    'other_images':            ('other_images', ('filename',)),
    'wallpaper_images':        ('wallpapers', ('filename',)),
    'retrospective_backgrounds': ('retrospective_bg', ('filename',)),
    'retrospective_overlays':  ('retrospective_overlay', ('filename',)),
    'retrospective_schedules': (None, ()),
}

# Réglages qui désignent un fichier (clé de réglage -> clé de dossier)
SETTING_FILES = {
    'welcome_frame_filename': 'frames',
    'application.wallpaper_current_filename': 'wallpapers',
    'slideshow.promo_background_filename': 'promo',
}

_SECRET_RE = re.compile(r'(password|\.login$|unlock_pin)', re.I)


def is_secret_key(key):
    return bool(_SECRET_RE.search(key))


def _safe_name(name):
    """Nom de fichier simple (pas de chemin) ou None."""
    if not name or not isinstance(name, str):
        return None
    base = PurePosixPath(name.replace('\\', '/')).name
    return base if base and base not in ('.', '..') else None


def _columns(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _table_exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def export_bundle(conn, dirs, zip_path, include_secrets=True):
    """Écrit le bundle dans `zip_path`. `dirs` : {clé de dossier: Path}.
    Retourne un dict de comptes {'settings', 'tables': {...}, 'files'}."""
    conn.row_factory = sqlite3.Row
    settings = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM settings ORDER BY key')}
    if not include_secrets:
        settings = {k: v for k, v in settings.items() if not is_secret_key(k)}

    tables, wanted = {}, set()   # wanted : (dirkey, filename)
    for table, (dirkey, fcols) in CONFIG_TABLES.items():
        if not _table_exists(conn, table):
            continue
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
        tables[table] = rows
        for row in rows:
            for c in fcols:
                n = _safe_name(row.get(c))
                if dirkey and n:
                    wanted.add((dirkey, n))
    for skey, dirkey in SETTING_FILES.items():
        n = _safe_name(settings.get(skey))
        if n:
            wanted.add((dirkey, n))

    embedded, missing = [], []
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for dirkey, name in sorted(wanted):
            base = dirs.get(dirkey)
            src = (base / name) if base else None
            if src is not None and src.is_file():
                zf.write(src, f'files/{dirkey}/{name}')
                embedded.append(f'{dirkey}/{name}')
            else:
                missing.append(f'{dirkey}/{name}')
        manifest = {
            'format': FORMAT, 'version': VERSION,
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'includes_secrets': bool(include_secrets),
            'settings': settings, 'tables': tables,
            'files': embedded, 'missing_files': missing,
        }
        zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return {'settings': len(settings), 'tables': {t: len(r) for t, r in tables.items()},
            'files': len(embedded), 'missing_files': len(missing)}


def import_bundle(conn, dirs, zip_file, mode='merge', include_secrets=True):
    """Importe un bundle. mode : 'merge' (écrase les lignes de même id/code,
    garde le reste) ou 'replace' (vide d'abord les tables de configuration
    présentes dans le bundle). Lève ValueError si le fichier n'est pas un
    bundle valide. Les colonnes absentes de la base cible sont ignorées
    (compatibilité entre versions)."""
    if mode not in ('merge', 'replace'):
        raise ValueError('Mode inconnu.')
    try:
        zf = zipfile.ZipFile(zip_file)
    except zipfile.BadZipFile:
        raise ValueError('Fichier ZIP invalide.')
    with zf:
        for name in zf.namelist():
            if name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name:
                raise ValueError('ZIP non autorisé (chemin invalide).')
        if 'manifest.json' not in zf.namelist():
            raise ValueError("Ce ZIP n'est pas un export de configuration (manifest.json absent).")
        try:
            manifest = json.loads(zf.read('manifest.json').decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ValueError('manifest.json illisible.')
        if manifest.get('format') != FORMAT:
            raise ValueError("Ce ZIP n'est pas un export de configuration Pictotem.")
        if int(manifest.get('version', 0)) > VERSION:
            raise ValueError("Export créé par une version plus récente de l'application.")

        counts = {'settings': 0, 'tables': {}, 'files': 0}
        settings = manifest.get('settings') or {}
        if not include_secrets:
            settings = {k: v for k, v in settings.items() if not is_secret_key(k)}

        conn.row_factory = sqlite3.Row
        try:
            for table, rows in (manifest.get('tables') or {}).items():
                if table not in CONFIG_TABLES or not _table_exists(conn, table):
                    continue
                cols = set(_columns(conn, table))
                if mode == 'replace':
                    conn.execute(f'DELETE FROM "{table}"')
                n = 0
                for row in rows:
                    data = {k: v for k, v in row.items() if k in cols}
                    if not data:
                        continue
                    names = ','.join(f'"{k}"' for k in data)
                    conn.execute(f'INSERT OR REPLACE INTO "{table}"({names}) VALUES({",".join("?" * len(data))})',
                                 list(data.values()))
                    n += 1
                counts['tables'][table] = n
            for k, v in settings.items():
                conn.execute('INSERT INTO settings(key, value) VALUES(?,?) '
                             'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, str(v)))
            counts['settings'] = len(settings)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        for entry in zf.namelist():
            parts = PurePosixPath(entry).parts
            if len(parts) != 3 or parts[0] != 'files':
                continue
            base = dirs.get(parts[1])
            name = _safe_name(parts[2])
            if base is None or not name:
                continue
            base.mkdir(parents=True, exist_ok=True)
            (base / name).write_bytes(zf.read(entry))
            counts['files'] += 1
    return counts


if __name__ == '__main__':
    # Usage : python config_transfer.py export <base_dir> <db> <out.zip> [--no-secrets]
    import sys
    from pathlib import Path
    if len(sys.argv) >= 5 and sys.argv[1] == 'export':
        base = Path(sys.argv[2])
        st = base / 'app' / 'static'
        d = {'frames': st / 'frames', 'fonts': st / 'fonts', 'promo': st / 'promo',
             'promo_content': st / 'promo_content', 'slideshow': st / 'slideshow',
             'screensaver': st / 'screensaver', 'other_images': st / 'other_images',
             'wallpapers': st / 'wallpapers', 'retrospective_bg': st / 'retrospective_bg',
             'retrospective_overlay': st / 'retrospective_overlay'}
        with closing(sqlite3.connect(f'file:{sys.argv[3]}?mode=ro', uri=True)) as c:
            print(export_bundle(c, d, sys.argv[4], include_secrets='--no-secrets' not in sys.argv))
