import csv
import io
import json
import logging
import random
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config_loader import CONFIG, DB_PATH, EMAILS_JSONL

logger = logging.getLogger('pictotem')


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db_conn()) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            filename TEXT NOT NULL,
            thumb_filename TEXT,
            created_at TEXT NOT NULL,
            printed INTEGER NOT NULL DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS frames (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            preview_filename TEXT,
            overlay_filename TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """)
        # Point 10 : index sur les colonnes fréquemment filtrées/triées
        conn.execute('CREATE INDEX IF NOT EXISTS idx_captures_created_at ON captures(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_captures_kind       ON captures(kind)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_emails_created_at   ON emails(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_emails_email        ON emails(email)')

        # Migration : ajout colonne is_default si absente
        try:
            conn.execute('ALTER TABLE frames ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0')
            conn.commit()
        except Exception:
            pass  # colonne déjà présente

        # Migration : ajout colonne vote_score sur captures
        try:
            conn.execute('ALTER TABLE captures ADD COLUMN vote_score INTEGER NOT NULL DEFAULT 0')
            conn.commit()
        except Exception:
            pass  # colonne déjà présente

        conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT 'official',
            capture_id INTEGER NOT NULL,
            voter_token TEXT NOT NULL,
            value INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source, capture_id, voter_token)
        )
        """)

        # Migration : vote sur les uploads invités. Les IDs de captures et de
        # guest_uploads sont indépendants (deux AUTOINCREMENT séparés) et
        # peuvent donc coïncider — la colonne 'source' ci-dessus lève
        # l'ambiguïté. Sur une base déjà existante (créée avant cette
        # fonctionnalité), la table votes n'a pas cette colonne ni la bonne
        # contrainte UNIQUE : on la reconstruit (toutes les lignes existantes
        # sont forcément des votes 'official', aucune perte de données).
        votes_cols = [r['name'] for r in conn.execute('PRAGMA table_info(votes)').fetchall()]
        if 'source' not in votes_cols:
            conn.execute('ALTER TABLE votes RENAME TO votes_old')
            conn.execute("""
            CREATE TABLE votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'official',
                capture_id INTEGER NOT NULL,
                voter_token TEXT NOT NULL,
                value INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source, capture_id, voter_token)
            )
            """)
            conn.execute("""
                INSERT INTO votes (id, source, capture_id, voter_token, value, created_at)
                SELECT id, 'official', capture_id, voter_token, value, created_at FROM votes_old
            """)
            conn.execute('DROP TABLE votes_old')
            conn.commit()
            logger.info('Migration votes : colonne source ajoutée (%d vote(s) conservé(s), source=official).',
                        conn.execute('SELECT COUNT(*) FROM votes').fetchone()[0])
        conn.execute('CREATE INDEX IF NOT EXISTS idx_votes_capture_id ON votes(source, capture_id)')

        # Migration : ajout colonne vote_score sur guest_uploads
        try:
            conn.execute('ALTER TABLE guest_uploads ADD COLUMN vote_score INTEGER NOT NULL DEFAULT 0')
            conn.commit()
        except Exception:
            pass  # colonne déjà présente

        conn.execute(
            'INSERT OR IGNORE INTO frames(id, label, preview_filename, overlay_filename, sort_order) VALUES(?,?,?,?,?)',
            ('none', 'Aucun cadre', None, None, 0)
        )
        is_fresh = conn.execute('SELECT COUNT(*) FROM frames WHERE id != "none"').fetchone()[0] == 0
        if is_fresh:
            config_frames = CONFIG.get('ui', {}).get('frame_gallery', {}).get('frames', [])
            for i, frame in enumerate(config_frames):
                if frame.get('id') == 'none':
                    continue
                preview_fn = Path(frame.get('preview', '')).name or None
                overlay_fn = Path(frame.get('overlay', '')).name or None
                conn.execute(
                    'INSERT OR IGNORE INTO frames(id, label, preview_filename, overlay_filename, sort_order) VALUES(?,?,?,?,?)',
                    (frame['id'], frame.get('label', frame['id']), preview_fn, overlay_fn, i + 1)
                )

        if not conn.execute('SELECT 1 FROM frames WHERE is_default = 1').fetchone():
            default_id = CONFIG.get('ui', {}).get('frame_gallery', {}).get('default_frame', 'none')
            conn.execute('UPDATE frames SET is_default = 1 WHERE id = ?', (default_id,))

        conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS slideshow_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS screensaver_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS guest_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            thumb_filename TEXT,
            original_filename TEXT,
            kind TEXT NOT NULL DEFAULT 'photo',
            guest_token TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            vote_score INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_guest_uploads_status ON guest_uploads(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_guest_uploads_token  ON guest_uploads(guest_token)')

        # Migration : ID unique par média (captures + uploads invités), voir
        # generate_media_uid() plus bas — chaîne de chiffres, longueur
        # paramétrable via /admin/tags (settings media_id.*). Placée après la
        # création de guest_uploads ci-dessus : sur une install fraîche, la
        # table n'existe pas encore plus haut dans cette fonction.
        try:
            conn.execute('ALTER TABLE captures ADD COLUMN media_uid TEXT')
            conn.commit()
        except Exception:
            pass  # colonne déjà présente
        try:
            conn.execute('ALTER TABLE guest_uploads ADD COLUMN media_uid TEXT')
            conn.commit()
        except Exception:
            pass  # colonne déjà présente
        conn.execute('CREATE INDEX IF NOT EXISTS idx_captures_media_uid ON captures(media_uid)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_guest_uploads_media_uid ON guest_uploads(media_uid)')

        # Tags sur médias (voir /admin/tags) : liste de tags prédéfinis +
        # assignations sur les captures officielles. tag_id NULL = tag
        # "libre" (saisi via clavier virtuel côté kiosque) — label stocké
        # tel quel dans capture_tags.label dans les deux cas (dénormalisé,
        # résiste à la suppression ultérieure d'un tag prédéfini).
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS capture_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id INTEGER NOT NULL,
            tag_id INTEGER,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_capture_tags_capture_id ON capture_tags(capture_id)')
        conn.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key, default=''):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    with closing(db_conn()) as conn:
        conn.execute(
            'INSERT INTO settings(key, value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, value)
        )
        conn.commit()


# ── Captures ──────────────────────────────────────────────────────────────────

def record_capture(kind, filename, thumb_filename=None):
    created_at = datetime.now().isoformat(timespec='seconds')
    media_uid = generate_media_uid(get_setting('media_id.length', '6'))
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO captures(kind, filename, thumb_filename, created_at, printed, media_uid) '
            'VALUES(?,?,?,?,0,?)',
            (kind, filename, thumb_filename, created_at, media_uid)
        )
        conn.commit()
        return cur.lastrowid, media_uid


def list_captures(sort='desc', kind='', page=1, page_size=None, media_uid=''):
    """Retourne (liste, total). page_size=None charge tout (usage interne).
    sort: 'desc'|'asc' (par date) ou 'votes_desc'|'votes_asc' (par score).
    media_uid : filtre optionnel (recherche partielle) sur l'ID média —
    voir champ de recherche de la galerie."""
    if sort in ('votes_desc', 'votes_asc'):
        col = 'vote_score'
        order = 'DESC' if sort == 'votes_desc' else 'ASC'
    else:
        col = 'created_at'
        order = 'DESC' if sort.lower() == 'desc' else 'ASC'

    conditions, params = [], []
    if kind in ('photo', 'video'):
        conditions.append('kind=?')
        params.append(kind)
    if media_uid:
        conditions.append('media_uid LIKE ?')
        params.append(f'%{media_uid}%')
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ''

    with closing(db_conn()) as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM captures {where}', params).fetchone()[0]
        if page_size:
            rows = conn.execute(
                f'SELECT * FROM captures {where} ORDER BY {col} {order}, id {order} LIMIT ? OFFSET ?',
                params + [page_size, (page - 1) * page_size]
            ).fetchall()
        else:
            rows = conn.execute(
                f'SELECT * FROM captures {where} ORDER BY {col} {order}, id {order}', params
            ).fetchall()
    return [dict(r) for r in rows], total


def delete_capture(capture_id):
    """Supprime la capture de la DB et retourne le dict (pour que l'appelant supprime les fichiers)."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM captures WHERE id = ?', (capture_id,)).fetchone()
        if not row:
            return None
        cap = dict(row)
        conn.execute('DELETE FROM captures WHERE id = ?', (capture_id,))
        conn.execute('DELETE FROM capture_tags WHERE capture_id = ?', (capture_id,))
        conn.commit()
    return cap


# ── Frames ────────────────────────────────────────────────────────────────────

def list_frames():
    with closing(db_conn()) as conn:
        rows = conn.execute('SELECT * FROM frames ORDER BY sort_order ASC, id ASC').fetchall()
    return [
        {
            'id': r['id'],
            'label': r['label'],
            'preview': f'/static/frames/{r["preview_filename"]}' if r['preview_filename'] else '',
            'preview_filename': r['preview_filename'],
            'overlay': f'/static/frames/{r["overlay_filename"]}' if r['overlay_filename'] else '',
            'overlay_filename': r['overlay_filename'],
            'sort_order': r['sort_order'],
            'is_default': bool(r['is_default']),
        }
        for r in rows
    ]


def get_frame_by_id_db(frame_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM frames WHERE id = ?', (frame_id,)).fetchone()
    return dict(row) if row else None


def get_default_frame():
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT id FROM frames WHERE is_default = 1 LIMIT 1').fetchone()
    if row:
        return row['id']
    return CONFIG.get('ui', {}).get('frame_gallery', {}).get('default_frame', 'none')


def upsert_frame(frame_id, label, preview_filename, overlay_filename, sort_order):
    with closing(db_conn()) as conn:
        conn.execute(
            'INSERT INTO frames(id, label, preview_filename, overlay_filename, sort_order) VALUES(?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET label=excluded.label, preview_filename=excluded.preview_filename, '
            'overlay_filename=excluded.overlay_filename, sort_order=excluded.sort_order',
            (frame_id, label, preview_filename, overlay_filename, sort_order)
        )
        conn.commit()


def delete_frame_db(frame_id):
    """Supprime de la DB et retourne le dict du cadre (l'appelant gère les fichiers + cache)."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM frames WHERE id = ?', (frame_id,)).fetchone()
        if not row:
            return None
        frame = dict(row)
        conn.execute('DELETE FROM frames WHERE id = ?', (frame_id,))
        conn.commit()
    return frame


# ── Emails ────────────────────────────────────────────────────────────────────

def save_email(email):
    email = email.strip().lower()
    if not email:
        return
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        if CONFIG['emails'].get('deduplicate', True):
            if conn.execute('SELECT 1 FROM emails WHERE email = ? LIMIT 1', (email,)).fetchone():
                return
        conn.execute('INSERT INTO emails(email, created_at) VALUES(?,?)', (email, created_at))
        conn.commit()
    with open(EMAILS_JSONL, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'email': email, 'created_at': created_at}, ensure_ascii=False) + '\n')


def list_emails(sort='desc', search=''):
    order = 'DESC' if sort.lower() == 'desc' else 'ASC'
    with closing(db_conn()) as conn:
        if search:
            rows = conn.execute(
                f'SELECT * FROM emails WHERE email LIKE ? ORDER BY created_at {order}, id {order}',
                (f'%{search}%',)
            ).fetchall()
        else:
            rows = conn.execute(
                f'SELECT * FROM emails ORDER BY created_at {order}, id {order}'
            ).fetchall()
    return [dict(r) for r in rows]


def delete_email_by_id(email_id):
    with closing(db_conn()) as conn:
        conn.execute('DELETE FROM emails WHERE id = ?', (email_id,))
        conn.commit()


def update_email_by_id(email_id, new_email):
    new_email = new_email.strip().lower()
    if not new_email:
        return
    with closing(db_conn()) as conn:
        conn.execute('UPDATE emails SET email = ? WHERE id = ?', (new_email, email_id))
        conn.commit()


# ── Votes ─────────────────────────────────────────────────────────────────────
# source='official' (table captures) ou 'guest' (table guest_uploads, photos
# invités approuvées et visibles dans la galerie). Les deux tables ont chacune
# leur propre AUTOINCREMENT, donc un même id peut désigner deux médias
# différents selon la source — d'où la colonne source sur votes et son
# inclusion dans la contrainte UNIQUE (voir migration dans init_db()).

_VOTE_TABLES = {'official': 'captures', 'guest': 'guest_uploads'}


def cast_vote(item_id, voter_token, value, source='official'):
    """Vote +1 ou -1 sur une capture officielle ou un upload invité.
    Si même valeur déjà votée : annule le vote (toggle).
    Retourne (new_score, your_vote) avec your_vote=0 si annulé."""
    if value not in (1, -1):
        raise ValueError('value must be +1 or -1')
    table = _VOTE_TABLES.get(source)
    if table is None:
        raise ValueError('source must be "official" or "guest"')
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        existing = conn.execute(
            'SELECT value FROM votes WHERE source=? AND capture_id=? AND voter_token=?',
            (source, item_id, voter_token)
        ).fetchone()
        if existing is None:
            conn.execute(
                'INSERT INTO votes(source, capture_id, voter_token, value, created_at) VALUES(?,?,?,?,?)',
                (source, item_id, voter_token, value, created_at)
            )
            conn.execute(f'UPDATE {table} SET vote_score = vote_score + ? WHERE id = ?',
                         (value, item_id))
            your_vote = value
        elif existing['value'] == value:
            conn.execute('DELETE FROM votes WHERE source=? AND capture_id=? AND voter_token=?',
                         (source, item_id, voter_token))
            conn.execute(f'UPDATE {table} SET vote_score = vote_score - ? WHERE id = ?',
                         (value, item_id))
            your_vote = 0
        else:
            delta = value - existing['value']
            conn.execute('UPDATE votes SET value=? WHERE source=? AND capture_id=? AND voter_token=?',
                         (value, source, item_id, voter_token))
            conn.execute(f'UPDATE {table} SET vote_score = vote_score + ? WHERE id = ?',
                         (delta, item_id))
            your_vote = value
        conn.commit()
        row = conn.execute(f'SELECT vote_score FROM {table} WHERE id=?', (item_id,)).fetchone()
        new_score = row['vote_score'] if row else 0
    return new_score, your_vote


def admin_adjust_vote(item_id, delta, source='official'):
    """Ajuste directement vote_score de delta (admin uniquement)."""
    table = _VOTE_TABLES.get(source, 'captures')
    with closing(db_conn()) as conn:
        conn.execute(f'UPDATE {table} SET vote_score = vote_score + ? WHERE id = ?',
                     (delta, item_id))
        conn.commit()
        row = conn.execute(f'SELECT vote_score FROM {table} WHERE id=?', (item_id,)).fetchone()
    return row['vote_score'] if row else 0


def get_voter_votes(voter_token):
    """Retourne {"source:capture_id": value} pour ce voter_token — clé
    composite car les ids ne sont uniques qu'au sein d'une même source."""
    if not voter_token:
        return {}
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT source, capture_id, value FROM votes WHERE voter_token=?', (voter_token,)
        ).fetchall()
    return {f'{r["source"]}:{r["capture_id"]}': r['value'] for r in rows}


# ── Slideshow ─────────────────────────────────────────────────────────────────

def list_slideshow_images():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM slideshow_images ORDER BY created_at DESC'
        ).fetchall()
    return [dict(r) for r in rows]


def add_slideshow_image(filename):
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO slideshow_images(filename, created_at) VALUES(?,?)',
            (filename, created_at)
        )
        conn.commit()
        return cur.lastrowid


def delete_slideshow_image_db(image_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM slideshow_images WHERE id = ?', (image_id,)).fetchone()
        if not row:
            return None
        img = dict(row)
        conn.execute('DELETE FROM slideshow_images WHERE id = ?', (image_id,))
        conn.commit()
    return img


# ── Écran de veille (images dédiées) ─────────────────────────────────────────

def list_screensaver_images():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM screensaver_images ORDER BY created_at DESC'
        ).fetchall()
    return [dict(r) for r in rows]


def add_screensaver_image(filename):
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO screensaver_images(filename, created_at) VALUES(?,?)',
            (filename, created_at)
        )
        conn.commit()
        return cur.lastrowid


def delete_screensaver_image_db(image_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM screensaver_images WHERE id = ?', (image_id,)).fetchone()
        if not row:
            return None
        img = dict(row)
        conn.execute('DELETE FROM screensaver_images WHERE id = ?', (image_id,))
        conn.commit()
    return img


# ── Upload invités (partage depuis smartphone) ───────────────────────────────
# Distinct des captures officielles de la borne (table captures) : les photos
# envoyées par les invités rejoignent uniquement le pool /bestof (voir
# api_bestof_slides dans app.py), jamais la gallery ni la table captures.
# status : 'pending' (en attente de modération) ou 'approved' (publié dans
# /bestof). Pas de statut "rejected" séparé : un refus supprime directement
# la ligne et le fichier (voir admin_guest_upload_delete).

def add_guest_upload(filename, thumb_filename, original_filename, guest_token,
                      size_bytes, status, kind='photo'):
    created_at = datetime.now().isoformat(timespec='seconds')
    media_uid = generate_media_uid(get_setting('media_id.length', '6'))
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO guest_uploads(filename, thumb_filename, original_filename, kind, '
            'guest_token, status, size_bytes, created_at, media_uid) VALUES(?,?,?,?,?,?,?,?,?)',
            (filename, thumb_filename, original_filename, kind, guest_token, status,
             size_bytes, created_at, media_uid)
        )
        conn.commit()
        return cur.lastrowid


def list_guest_uploads(status=''):
    with closing(db_conn()) as conn:
        if status:
            rows = conn.execute(
                'SELECT * FROM guest_uploads WHERE status = ? ORDER BY created_at DESC', (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM guest_uploads ORDER BY created_at DESC'
            ).fetchall()
    return [dict(r) for r in rows]


def list_approved_guest_uploads():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            "SELECT * FROM guest_uploads WHERE status = 'approved' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def count_guest_uploads_by_token(guest_token):
    with closing(db_conn()) as conn:
        row = conn.execute(
            'SELECT COUNT(*) FROM guest_uploads WHERE guest_token = ?', (guest_token,)
        ).fetchone()
    return row[0] if row else 0


def set_guest_upload_status(upload_id, status):
    with closing(db_conn()) as conn:
        conn.execute('UPDATE guest_uploads SET status = ? WHERE id = ?', (status, upload_id))
        conn.commit()
        row = conn.execute('SELECT * FROM guest_uploads WHERE id = ?', (upload_id,)).fetchone()
    return dict(row) if row else None


def list_gallery_combined(sort='desc', kind='', source='', page=1, page_size=None, media_uid=''):
    """Fusionne captures officielles + uploads invités approuvés pour la
    galerie, uniquement utilisée quand guest_upload.include_in_gallery est
    activé (voir gallery() dans app.py — sinon list_captures() seule est
    utilisée, comportement inchangé). Tri/pagination faits en Python : les
    deux tables ont des schémas différents (pas d'UNION SQL simple), et les
    volumes visés (un événement) ne posent pas de problème de performance.
    Chaque item porte un champ 'source' ('official'|'guest') pour le badge
    et le filtre côté galerie. media_uid : filtre optionnel (recherche par ID)."""
    items = []
    if source in ('', 'official'):
        with closing(db_conn()) as conn:
            if kind in ('photo', 'video'):
                rows = conn.execute('SELECT * FROM captures WHERE kind=?', (kind,)).fetchall()
            else:
                rows = conn.execute('SELECT * FROM captures').fetchall()
        for r in rows:
            d = dict(r)
            d['source'] = 'official'
            items.append(d)

    # Les uploads invités sont toujours kind='photo' : sans effet si un
    # filtre 'video' est actif.
    if source in ('', 'guest') and kind in ('', 'photo'):
        with closing(db_conn()) as conn:
            rows = conn.execute("SELECT * FROM guest_uploads WHERE status = 'approved'").fetchall()
        for r in rows:
            d = dict(r)
            d['source'] = 'guest'
            d['printed'] = 0
            items.append(d)

    if media_uid:
        items = [it for it in items if media_uid.lower() in (it.get('media_uid') or '').lower()]

    if sort in ('votes_desc', 'votes_asc'):
        items.sort(key=lambda x: (x['vote_score'], x['created_at']), reverse=(sort == 'votes_desc'))
    else:
        items.sort(key=lambda x: x['created_at'], reverse=(sort != 'asc'))

    total = len(items)
    if page_size:
        start = (page - 1) * page_size
        items = items[start:start + page_size]
    return items, total


def delete_guest_upload_db(upload_id):
    """Supprime de la DB et retourne le dict (l'appelant supprime fichier + thumb)."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM guest_uploads WHERE id = ?', (upload_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        conn.execute('DELETE FROM guest_uploads WHERE id = ?', (upload_id,))
        conn.commit()
    return item


def export_emails_files():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT email, created_at FROM emails ORDER BY datetime(created_at) DESC'
        ).fetchall()
    payload = [{'email': r['email'], 'created_at': r['created_at']} for r in rows]
    json_path = Path(CONFIG['emails']['export_json'])
    csv_path  = Path(CONFIG['emails']['export_csv'])
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['email', 'created_at'])
        writer.writeheader()
        writer.writerows(payload)
    return csv_path, json_path


# ── ID unique média ───────────────────────────────────────────────────────────
# Chaîne de chiffres (facile à lire/taper sur un champ de recherche tactile),
# longueur réglable depuis /admin/tags (media_id.length). Générée à la
# capture (captures) et à l'upload invité (guest_uploads) — voir
# record_capture() / add_guest_upload(). Unicité vérifiée dans les deux
# tables à la fois (mêmes chances d'affichage/recherche côté galerie).

def generate_media_uid(length):
    try:
        length = max(3, min(int(length), 12))
    except (TypeError, ValueError):
        length = 6
    with closing(db_conn()) as conn:
        for _ in range(30):
            candidate = ''.join(random.choices('0123456789', k=length))
            exists = conn.execute(
                'SELECT 1 FROM captures WHERE media_uid = ? '
                'UNION SELECT 1 FROM guest_uploads WHERE media_uid = ?',
                (candidate, candidate)
            ).fetchone()
            if not exists:
                return candidate
    # Filet de sécurité (ne devrait jamais être atteint) : garantit quand
    # même une terminaison plutôt qu'une boucle infinie.
    return str(int(datetime.now().timestamp() * 1000))[-length:]


def get_media_by_uid(media_uid):
    """Recherche un média (capture officielle ou upload invité approuvé) par
    son ID unique. Retourne un dict avec 'source' ('official'|'guest') ou None."""
    media_uid = (media_uid or '').strip()
    if not media_uid:
        return None
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM captures WHERE media_uid = ?', (media_uid,)).fetchone()
        if row:
            d = dict(row)
            d['source'] = 'official'
            return d
        row = conn.execute(
            "SELECT * FROM guest_uploads WHERE media_uid = ? AND status = 'approved'", (media_uid,)
        ).fetchone()
        if row:
            d = dict(row)
            d['source'] = 'guest'
            return d
    return None


# ── Tags ──────────────────────────────────────────────────────────────────────
# Fonctionnalité activable via /admin/tags (settings tags.*). Tags prédéfinis
# (table tags, CRUD admin) + tags "libres" saisis par l'invité via clavier
# virtuel côté kiosque (tag_id NULL dans capture_tags). Portée aux captures
# officielles uniquement (pas aux uploads invités).

def list_tags():
    with closing(db_conn()) as conn:
        rows = conn.execute('SELECT * FROM tags ORDER BY sort_order ASC, label ASC').fetchall()
    return [dict(r) for r in rows]


def get_tag_by_id(tag_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM tags WHERE id = ?', (tag_id,)).fetchone()
    return dict(row) if row else None


def create_tag(label, sort_order=0):
    with closing(db_conn()) as conn:
        cur = conn.execute('INSERT INTO tags(label, sort_order) VALUES(?,?)', (label, sort_order))
        conn.commit()
        return cur.lastrowid


def update_tag(tag_id, label, sort_order):
    with closing(db_conn()) as conn:
        conn.execute('UPDATE tags SET label=?, sort_order=? WHERE id=?', (label, sort_order, tag_id))
        conn.commit()


def delete_tag_db(tag_id):
    """Supprime le tag prédéfini. Les assignations déjà faites (capture_tags)
    sont conservées telles quelles (label dénormalisé) : l'historique des
    médias déjà tagués n'est pas affecté, seul le tag disparaît de la liste
    proposée pour les prochaines captures."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM tags WHERE id = ?', (tag_id,)).fetchone()
        if not row:
            return None
        conn.execute('DELETE FROM tags WHERE id = ?', (tag_id,))
        conn.commit()
    return dict(row)


def list_capture_tags(capture_id):
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM capture_tags WHERE capture_id = ? ORDER BY id ASC', (capture_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_capture_tags(capture_id):
    with closing(db_conn()) as conn:
        row = conn.execute(
            'SELECT COUNT(*) FROM capture_tags WHERE capture_id = ?', (capture_id,)
        ).fetchone()
    return row[0] if row else 0


def add_capture_tag(capture_id, tag_id=None, label=''):
    """Assigne un tag (prédéfini si tag_id fourni, sinon libre) à une
    capture. Empêche les doublons de tag prédéfini sur une même capture
    (une seule ligne par tag_id non NULL) ; les tags libres, eux, n'ont pas
    cette contrainte (deux textes libres différents sont deux lignes
    distinctes, tag_id restant NULL dans les deux cas)."""
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        if tag_id is not None:
            existing = conn.execute(
                'SELECT id FROM capture_tags WHERE capture_id = ? AND tag_id = ?',
                (capture_id, tag_id)
            ).fetchone()
            if existing:
                return existing['id']
        cur = conn.execute(
            'INSERT INTO capture_tags(capture_id, tag_id, label, created_at) VALUES(?,?,?,?)',
            (capture_id, tag_id, label, created_at)
        )
        conn.commit()
        return cur.lastrowid


def delete_capture_tag(assignment_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM capture_tags WHERE id = ?', (assignment_id,)).fetchone()
        if not row:
            return None
        conn.execute('DELETE FROM capture_tags WHERE id = ?', (assignment_id,))
        conn.commit()
    return dict(row)
