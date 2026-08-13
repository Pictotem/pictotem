import csv
import io
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config_loader import CONFIG, DB_PATH, EMAILS_JSONL

logger = logging.getLogger('photomaton')


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
            capture_id INTEGER NOT NULL,
            voter_token TEXT NOT NULL,
            value INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(capture_id, voter_token)
        )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_votes_capture_id ON votes(capture_id)')

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
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO captures(kind, filename, thumb_filename, created_at, printed) VALUES(?,?,?,?,0)',
            (kind, filename, thumb_filename, created_at)
        )
        conn.commit()
        return cur.lastrowid


def list_captures(sort='desc', kind='', page=1, page_size=None):
    """Retourne (liste, total). page_size=None charge tout (usage interne).
    sort: 'desc'|'asc' (par date) ou 'votes_desc'|'votes_asc' (par score)."""
    if sort in ('votes_desc', 'votes_asc'):
        col = 'vote_score'
        order = 'DESC' if sort == 'votes_desc' else 'ASC'
    else:
        col = 'created_at'
        order = 'DESC' if sort.lower() == 'desc' else 'ASC'

    with closing(db_conn()) as conn:
        if kind in ('photo', 'video'):
            total = conn.execute('SELECT COUNT(*) FROM captures WHERE kind=?', (kind,)).fetchone()[0]
            if page_size:
                rows = conn.execute(
                    f'SELECT * FROM captures WHERE kind=? ORDER BY {col} {order}, id {order} LIMIT ? OFFSET ?',
                    (kind, page_size, (page - 1) * page_size)
                ).fetchall()
            else:
                rows = conn.execute(
                    f'SELECT * FROM captures WHERE kind=? ORDER BY {col} {order}, id {order}', (kind,)
                ).fetchall()
        else:
            total = conn.execute('SELECT COUNT(*) FROM captures').fetchone()[0]
            if page_size:
                rows = conn.execute(
                    f'SELECT * FROM captures ORDER BY {col} {order}, id {order} LIMIT ? OFFSET ?',
                    (page_size, (page - 1) * page_size)
                ).fetchall()
            else:
                rows = conn.execute(
                    f'SELECT * FROM captures ORDER BY {col} {order}, id {order}'
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

def cast_vote(capture_id, voter_token, value):
    """Vote +1 ou -1. Si même valeur déjà votée : annule le vote (toggle).
    Retourne (new_score, your_vote) avec your_vote=0 si annulé."""
    if value not in (1, -1):
        raise ValueError('value must be +1 or -1')
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        existing = conn.execute(
            'SELECT value FROM votes WHERE capture_id=? AND voter_token=?',
            (capture_id, voter_token)
        ).fetchone()
        if existing is None:
            conn.execute(
                'INSERT INTO votes(capture_id, voter_token, value, created_at) VALUES(?,?,?,?)',
                (capture_id, voter_token, value, created_at)
            )
            conn.execute('UPDATE captures SET vote_score = vote_score + ? WHERE id = ?',
                         (value, capture_id))
            your_vote = value
        elif existing['value'] == value:
            conn.execute('DELETE FROM votes WHERE capture_id=? AND voter_token=?',
                         (capture_id, voter_token))
            conn.execute('UPDATE captures SET vote_score = vote_score - ? WHERE id = ?',
                         (value, capture_id))
            your_vote = 0
        else:
            delta = value - existing['value']
            conn.execute('UPDATE votes SET value=? WHERE capture_id=? AND voter_token=?',
                         (value, capture_id, voter_token))
            conn.execute('UPDATE captures SET vote_score = vote_score + ? WHERE id = ?',
                         (delta, capture_id))
            your_vote = value
        conn.commit()
        new_score = conn.execute(
            'SELECT vote_score FROM captures WHERE id=?', (capture_id,)
        ).fetchone()['vote_score']
    return new_score, your_vote


def admin_adjust_vote(capture_id, delta):
    """Ajuste directement vote_score de delta (admin uniquement)."""
    with closing(db_conn()) as conn:
        conn.execute('UPDATE captures SET vote_score = vote_score + ? WHERE id = ?',
                     (delta, capture_id))
        conn.commit()
        row = conn.execute('SELECT vote_score FROM captures WHERE id=?', (capture_id,)).fetchone()
    return row['vote_score'] if row else 0


def get_voter_votes(voter_token):
    """Retourne {capture_id: value} pour ce voter_token."""
    if not voter_token:
        return {}
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT capture_id, value FROM votes WHERE voter_token=?', (voter_token,)
        ).fetchall()
    return {r['capture_id']: r['value'] for r in rows}


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
