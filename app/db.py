import csv
import io
import json
import logging
import random
import sqlite3
from contextlib import closing
from datetime import datetime
from html import escape as _html_escape
from html.parser import HTMLParser
from pathlib import Path

from config_loader import CONFIG, DB_PATH, EMAILS_JSONL

logger = logging.getLogger('pictotem')


class _BareImageWrapper(HTMLParser):
    """Migration ponctuelle (v2.0.3, voir _wrap_bare_images_for_quill ci-dessous
    et son appel dans init_db) : enveloppe dans son propre <p>...</p> toute
    balise <img> se trouvant directement à la racine du contenu (pas déjà
    nichée dans un <p>/<div>/<td>/etc.). Rendue nécessaire par le passage à
    Quill côté admin (voir static/promo-editor.js) : contrairement à l'ancien
    éditeur (execCommand insertHTML), qui pouvait laisser un <img> "nu" dans
    le flux, Quill fusionne systématiquement au chargement un <img> non
    enveloppé dans le bloc suivant (paragraphe suivant, ou première cellule
    d'un tableau suivant) -- ce qui déplacerait visiblement l'image dans les
    pages promo déjà enregistrées, dès leur première ouverture dans le
    nouvel éditeur. Idempotente : une image déjà nichée (profondeur > 0,
    notamment déjà dans son propre <p>) n'est jamais ré-enveloppée."""

    _VOID_TAGS = {'img', 'br'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._depth = 0

    def _tag_str(self, tag, attrs):
        attr_str = ''.join(f' {n}="{_html_escape(v, quote=True)}"' for n, v in attrs if v is not None)
        return f'<{tag}{attr_str}>'

    def handle_starttag(self, tag, attrs):
        if tag == 'img' and self._depth == 0:
            self.out.append('<p>' + self._tag_str(tag, attrs) + '</p>')
            return
        self.out.append(self._tag_str(tag, attrs))
        if tag not in self._VOID_TAGS:
            self._depth += 1

    def handle_startendtag(self, tag, attrs):
        if tag == 'img' and self._depth == 0:
            self.out.append('<p>' + self._tag_str(tag, attrs) + '</p>')
        else:
            self.out.append(self._tag_str(tag, attrs))

    def handle_endtag(self, tag):
        if tag in self._VOID_TAGS:
            return
        if self._depth > 0:
            self._depth -= 1
        self.out.append(f'</{tag}>')

    def handle_data(self, data):
        self.out.append(_html_escape(data))


def _wrap_bare_images_for_quill(html):
    if not html or '<img' not in html:
        return html or ''
    parser = _BareImageWrapper()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    return ''.join(parser.out)


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
        CREATE TABLE IF NOT EXISTS wallpaper_images (
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

        # Codes invités (voir /admin/guest_codes) : un code numérique aléatoire
        # (longueur paramétrable, settings guest_codes.code_length) associé à
        # un texte libre (250 caractères max). Un QR-code dont le contenu brut
        # correspond exactement à un `code` existant affiche le `texte` associé
        # à la place du contenu brut — voir get_guest_code_text() plus bas et
        # _qr_detect_boxes_robust (app.py).
        conn.execute("""
        CREATE TABLE IF NOT EXISTS guest_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            texte TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_guest_codes_code ON guest_codes(code)')

        # Polices personnalisées (voir /admin/texts) : fichier .ttf/.otf
        # uploadé par l'admin, disponible ensuite dans tous les sélecteurs de
        # police de l'app (app.py : _all_fonts()). `family` est l'identifiant
        # CSS unique généré à l'ajout (voir app.py : _slugify_font_family) —
        # utilisé à la fois comme nom de la règle @font-face et comme valeur
        # de réglage stockée pour chaque fonction utilisant cette police.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_fonts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        # Pages promo (voir /admin/slideshow → Pages promo, app.py) : CRUD
        # v2.0 remplaçant l'ancienne page promo unique (réglages
        # slideshow.promo_* dans `settings` + fond unique dans PROMO_DIR).
        # Bibliothèque de fonds partagée par toutes les pages promo.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_backgrounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS promo_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            frequency INTEGER NOT NULL DEFAULT 6,
            pause_seconds INTEGER NOT NULL DEFAULT 8,
            html_content TEXT NOT NULL DEFAULT '',
            background_id INTEGER,
            overlay_enabled INTEGER NOT NULL DEFAULT 1,
            text_font TEXT NOT NULL DEFAULT 'system-ui, "Segoe UI", sans-serif',
            text_size INTEGER NOT NULL DEFAULT 28,
            text_color TEXT NOT NULL DEFAULT '#ffffff',
            effect TEXT NOT NULL DEFAULT 'fade',
            qr_enabled INTEGER NOT NULL DEFAULT 1,
            qr_text TEXT NOT NULL DEFAULT '',
            qr_size INTEGER NOT NULL DEFAULT 220,
            qr_position TEXT NOT NULL DEFAULT 'center',
            qr_color TEXT NOT NULL DEFAULT '#000000',
            background_bg_color TEXT NOT NULL DEFAULT '#14161a',
            created_at TEXT NOT NULL
        )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_promo_pages_sort_order ON promo_pages(sort_order)')

        # Fonds dégradés (v2.0.1) : promo_backgrounds accueille désormais deux
        # types de fond partagés par les pages promo — 'image' (fichier
        # uploadé, comportement d'origine) et 'gradient' (deux couleurs +
        # angle, généré à la volée côté client, aucun fichier). `kind` par
        # défaut 'image' sur les colonnes ajoutées : les fonds déjà en base
        # (toujours des images avant cette version) restent corrects sans
        # migration de données à écrire.
        for ddl in (
            "ALTER TABLE promo_backgrounds ADD COLUMN kind TEXT NOT NULL DEFAULT 'image'",
            "ALTER TABLE promo_backgrounds ADD COLUMN color1 TEXT",
            "ALTER TABLE promo_backgrounds ADD COLUMN color2 TEXT",
            "ALTER TABLE promo_backgrounds ADD COLUMN angle INTEGER NOT NULL DEFAULT 135",
        ):
            try:
                conn.execute(ddl)
                conn.commit()
            except Exception:
                pass  # colonne déjà présente

        # CSS libre par page promo (v2.0.1) : texte brut, appliqué uniquement
        # à sa propre page via @scope (voir bestof.html -> buildPromoContent),
        # AUCUN filtrage serveur (choix assumé -- contrairement à
        # html_content, qui reste nettoyé par sanitize_promo_html). Défaut
        # '' : les pages existantes n'ont simplement aucun CSS additionnel.
        try:
            conn.execute("ALTER TABLE promo_pages ADD COLUMN custom_css TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except Exception:
            pass  # colonne déjà présente

        # Couleur de fond derrière l'image/dégradé de la page (v2.0.2) :
        # utile pour une image de fond avec de la transparence (PNG) ou
        # lorsqu'aucun fond n'est choisi -- voir bestof.html ->
        # buildPromoContent(). Défaut identique à l'ancien fallback fixe
        # (#14161a) pour ne rien changer visuellement aux pages existantes.
        try:
            conn.execute("ALTER TABLE promo_pages ADD COLUMN background_bg_color TEXT NOT NULL DEFAULT '#14161a'")
            conn.commit()
        except Exception:
            pass  # colonne déjà présente

        # Marge entre le bloc texte/QR et le bord de l'écran (v2.0.5) --
        # remplace l'ancien padding fixe (60px, en dur dans le CSS de
        # bestof.html -> .promo-content) par un réglage par page promo (voir
        # /admin/slideshow -> Pages promo). Défaut 60 : aucun changement
        # visuel pour les pages déjà enregistrées tant que l'admin n'y touche
        # pas.
        try:
            conn.execute("ALTER TABLE promo_pages ADD COLUMN content_padding INTEGER NOT NULL DEFAULT 60")
            conn.commit()
        except Exception:
            pass  # colonne déjà présente

        # Migration v2.0.3 (éditeur -> Quill) : enveloppe les <img> "nues"
        # dans le html_content déjà enregistré -- voir _wrap_bare_images_for_quill
        # ci-dessus. Relit/réécrit chaque page une seule fois par démarrage ;
        # idempotente et bon marché (quelques pages promo tout au plus), donc
        # volontairement PAS conditionnée à un flag "déjà migré" séparé.
        try:
            rows = conn.execute('SELECT id, html_content FROM promo_pages').fetchall()
            for row in rows:
                fixed = _wrap_bare_images_for_quill(row['html_content'] or '')
                if fixed != (row['html_content'] or ''):
                    conn.execute('UPDATE promo_pages SET html_content = ? WHERE id = ?',
                                 (fixed, row['id']))
            conn.commit()
        except Exception:
            logger.exception("Migration <img> nues -> <p><img></p> échouée (ignorée)")

        # Migration v2.0 : reprise ponctuelle de l'ancienne page promo unique
        # vers le nouveau CRUD multi-pages — seulement si promo_pages est
        # encore vide ET qu'un réglage v1 existe (install déjà en prod avant
        # la v2.0). Sur une install neuve, promo_pages reste simplement vide
        # (l'admin crée ses pages promo depuis /admin/slideshow).
        try:
            already = conn.execute('SELECT COUNT(*) AS n FROM promo_pages').fetchone()['n']
            legacy_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'slideshow.promo_enabled'"
            ).fetchone()
            if already == 0 and legacy_row is not None:
                def _legacy(key, default=''):
                    r = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
                    return r['value'] if r else default

                bg_id = None
                bg_filename = _legacy('slideshow.promo_background_filename')
                if bg_filename:
                    cur = conn.execute(
                        'INSERT INTO promo_backgrounds(filename, label, created_at) VALUES(?,?,?)',
                        (bg_filename, 'Fond repris de la v1', datetime.now().isoformat(timespec='seconds'))
                    )
                    bg_id = cur.lastrowid

                legacy_text = _legacy('slideshow.promo_text')
                html_content = ''.join(
                    f'<p>{line}</p>' for line in legacy_text.split(chr(10)) if line.strip()
                ) if legacy_text else ''

                conn.execute("""
                    INSERT INTO promo_pages(
                        active, sort_order, frequency, pause_seconds, html_content,
                        background_id, overlay_enabled, text_font, text_size, text_color,
                        effect, qr_enabled, qr_text, qr_size, qr_position, qr_color, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    1 if _legacy('slideshow.promo_enabled') == '1' else 0,
                    0,
                    int(_legacy('slideshow.promo_frequency', '6') or '6'),
                    int(_legacy('slideshow.delay', '5') or '5'),
                    html_content,
                    bg_id,
                    1 if _legacy('slideshow.promo_overlay_enabled', '1') == '1' else 0,
                    _legacy('slideshow.promo_text_font', 'system-ui, "Segoe UI", sans-serif'),
                    int(_legacy('slideshow.promo_text_size', '28') or '28'),
                    _legacy('slideshow.promo_text_color', '#ffffff'),
                    'fade',
                    1,
                    '',
                    int(_legacy('slideshow.promo_qr_size', '220') or '220'),
                    'center',
                    '#000000',
                    datetime.now().isoformat(timespec='seconds'),
                ))
                conn.commit()
        except Exception:
            logger.exception('Migration page promo v1 -> v2 échouée (ignorée, CRUD reste utilisable vide)')

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


def list_captures(sort='desc', kind='', page=1, page_size=None, media_uid='', tag=''):
    """Retourne (liste, total). page_size=None charge tout (usage interne).
    sort: 'desc'|'asc' (par date) ou 'votes_desc'|'votes_asc' (par score).
    media_uid : filtre optionnel (recherche partielle) sur l'ID média —
    voir champ de recherche de la galerie. tag : filtre optionnel (libellé
    exact, prédéfini ou libre) — voir filtre "Tags" de la galerie."""
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
    if tag:
        conditions.append('id IN (SELECT capture_id FROM capture_tags WHERE label = ?)')
        params.append(tag)
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
    """Supprime la capture de la DB (+ tags et votes associés) et retourne
    le dict (pour que l'appelant supprime les fichiers). Le nettoyage des
    votes (absent avant cette correction) évite de laisser des lignes
    orphelines dans `votes` après suppression d'une capture officielle."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM captures WHERE id = ?', (capture_id,)).fetchone()
        if not row:
            return None
        cap = dict(row)
        conn.execute('DELETE FROM captures WHERE id = ?', (capture_id,))
        conn.execute('DELETE FROM capture_tags WHERE capture_id = ?', (capture_id,))
        conn.execute('DELETE FROM votes WHERE source = ? AND capture_id = ?', ('official', capture_id))
        conn.commit()
    return cap


def list_captures_in_range(start_iso, end_iso):
    """Toutes les captures officielles dont created_at est dans
    [start_iso, end_iso] (bornes incluses). Comparaison lexicographique sur
    les chaînes ISO 8601 — valide car record_capture() écrit toujours au
    format 'YYYY-MM-DDTHH:MM:SS' (timespec='seconds'). Utilisé par
    l'archivage et le nettoyage par plage de dates (/admin/archive)."""
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM captures WHERE created_at >= ? AND created_at <= ? ORDER BY created_at ASC',
            (start_iso, end_iso)
        ).fetchall()
    return [dict(r) for r in rows]


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


# ── Pages promo (diaporama /bestof) ──────────────────────────────────────────
# CRUD v2.0 : plusieurs pages promo en rotation dans /bestof, chacune avec son
# fond (choisi dans la bibliothèque partagée promo_backgrounds), sa fréquence,
# son temps de pause, son texte WYSIWYG, sa police/taille/couleur, son effet
# visuel et son QR code (texte, taille, position, couleur) — voir app.py pour
# la validation des champs et le rendu (/bestof, admin_slideshow).

def list_promo_backgrounds():
    with closing(db_conn()) as conn:
        rows = conn.execute('SELECT * FROM promo_backgrounds ORDER BY created_at DESC').fetchall()
    return [dict(r) for r in rows]


def add_promo_background(filename, label=''):
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        cur = conn.execute(
            "INSERT INTO promo_backgrounds(filename, label, created_at, kind) VALUES(?,?,?,'image')",
            (filename, label, created_at)
        )
        conn.commit()
        return cur.lastrowid


def add_promo_gradient_background(color1, color2, angle=135, label=''):
    """Fond dégradé (deux couleurs + angle), sans fichier — voir kind='gradient'
    sur promo_backgrounds. `angle` en degrés, sens CSS (linear-gradient)."""
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        cur = conn.execute(
            "INSERT INTO promo_backgrounds(filename, label, created_at, kind, color1, color2, angle) "
            "VALUES('', ?, ?, 'gradient', ?, ?, ?)",
            (label, created_at, color1, color2, angle)
        )
        conn.commit()
        return cur.lastrowid


def delete_promo_background_db(bg_id):
    """Supprime de la DB et retourne le dict (l'appelant gère le fichier sur
    disque) — les pages promo qui utilisaient ce fond retombent sur le
    dégradé par défaut (background_id remis à NULL) plutôt que de pointer
    vers un fond qui n'existe plus."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM promo_backgrounds WHERE id = ?', (bg_id,)).fetchone()
        if not row:
            return None
        bg = dict(row)
        conn.execute('UPDATE promo_pages SET background_id = NULL WHERE background_id = ?', (bg_id,))
        conn.execute('DELETE FROM promo_backgrounds WHERE id = ?', (bg_id,))
        conn.commit()
    return bg


# v2.0.2 : QR code par page remplacé par une balise {qrcode=...} inline
# dans le texte WYSIWYG (voir _resolve_inline_qrcodes, app.py) -- les
# colonnes qr_enabled/qr_text/qr_size/qr_position/qr_color restent dans le
# schéma (create_promo_page() et la migration v1->v2 les alimentent encore
# avec leurs valeurs par défaut, pas de DROP COLUMN risqué sur une base
# SQLite en production) mais ne sont plus jamais écrites via
# update_promo_page ni lues côté app -- volontairement absentes d'ici.
_PROMO_PAGE_COLUMNS = (
    'active', 'sort_order', 'frequency', 'pause_seconds', 'html_content',
    'background_id', 'background_bg_color', 'overlay_enabled', 'text_font',
    'text_size', 'text_color', 'effect', 'custom_css', 'content_padding',
)


def _promo_page_row_to_dict(row, bg_by_id):
    d = dict(row)
    bg = bg_by_id.get(d['background_id'])
    d['background_filename'] = bg['filename'] if bg else ''
    d['background_kind']     = bg['kind'] if bg else ''
    d['background_color1']   = bg['color1'] if bg else ''
    d['background_color2']   = bg['color2'] if bg else ''
    d['background_angle']    = bg['angle'] if bg else 135
    return d


def list_promo_pages():
    with closing(db_conn()) as conn:
        rows = conn.execute('SELECT * FROM promo_pages ORDER BY sort_order ASC, id ASC').fetchall()
        bgs = conn.execute('SELECT * FROM promo_backgrounds').fetchall()
    bg_by_id = {b['id']: dict(b) for b in bgs}
    return [_promo_page_row_to_dict(r, bg_by_id) for r in rows]


def get_promo_page(page_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM promo_pages WHERE id = ?', (page_id,)).fetchone()
        if not row:
            return None
        bgs = conn.execute('SELECT * FROM promo_backgrounds').fetchall()
    bg_by_id = {b['id']: dict(b) for b in bgs}
    return _promo_page_row_to_dict(row, bg_by_id)


def create_promo_page():
    """Crée une page promo vide (inactive par défaut, à compléter dans
    l'admin) en fin de rotation (sort_order = max + 1)."""
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        max_order = conn.execute('SELECT COALESCE(MAX(sort_order), -1) AS m FROM promo_pages').fetchone()['m']
        cur = conn.execute("""
            INSERT INTO promo_pages(
                active, sort_order, frequency, pause_seconds, html_content,
                background_id, overlay_enabled, text_font, text_size, text_color,
                effect, qr_enabled, qr_text, qr_size, qr_position, qr_color, created_at
            ) VALUES (0, ?, 6, 8, '', NULL, 1, 'system-ui, "Segoe UI", sans-serif', 28, '#ffffff',
                      'fade', 1, '', 220, 'center', '#000000', ?)
        """, (max_order + 1, created_at))
        conn.commit()
        return cur.lastrowid


def update_promo_page(page_id, **fields):
    """Met à jour uniquement les colonnes listées dans _PROMO_PAGE_COLUMNS
    parmi celles fournies (déjà validées côté app.py) ; ignore silencieusement
    toute clé inconnue plutôt que de planter."""
    cols = [k for k in fields if k in _PROMO_PAGE_COLUMNS]
    if not cols:
        return
    set_clause = ', '.join(f'{c} = ?' for c in cols)
    values = [fields[c] for c in cols] + [page_id]
    with closing(db_conn()) as conn:
        conn.execute(f'UPDATE promo_pages SET {set_clause} WHERE id = ?', values)
        conn.commit()


def delete_promo_page_db(page_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM promo_pages WHERE id = ?', (page_id,)).fetchone()
        if not row:
            return None
        page = dict(row)
        conn.execute('DELETE FROM promo_pages WHERE id = ?', (page_id,))
        conn.commit()
    return page


def move_promo_page(page_id, direction):
    """Échange sort_order avec le voisin immédiat ('up' ou 'down') — raccourci
    pratique pour les boutons ▲▼ de la liste ; le champ « Ordre de passage »
    du formulaire d'édition reste la façon de fixer une valeur précise."""
    with closing(db_conn()) as conn:
        rows = conn.execute('SELECT id, sort_order FROM promo_pages ORDER BY sort_order ASC, id ASC').fetchall()
        ids = [r['id'] for r in rows]
        if page_id not in ids:
            return
        i = ids.index(page_id)
        j = i - 1 if direction == 'up' else i + 1
        if j < 0 or j >= len(ids):
            return
        a, b = rows[i], rows[j]
        conn.execute('UPDATE promo_pages SET sort_order = ? WHERE id = ?', (b['sort_order'], a['id']))
        conn.execute('UPDATE promo_pages SET sort_order = ? WHERE id = ?', (a['sort_order'], b['id']))
        conn.commit()


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


# ── Fond d'écran Windows (images disponibles) ────────────────────────────────
# Images uploadées depuis /admin/application, parmi lesquelles l'admin choisit
# celle à appliquer comme fond d'écran Windows (voir utils.set_windows_wallpaper
# et app.py admin_wallpaper_apply). Réglage 'application.wallpaper_current_filename'
# (table settings) retient laquelle est actuellement appliquée.

def list_wallpaper_images():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM wallpaper_images ORDER BY created_at DESC'
        ).fetchall()
    return [dict(r) for r in rows]


def add_wallpaper_image(filename):
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO wallpaper_images(filename, created_at) VALUES(?,?)',
            (filename, created_at)
        )
        conn.commit()
        return cur.lastrowid


def delete_wallpaper_image_db(image_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM wallpaper_images WHERE id = ?', (image_id,)).fetchone()
        if not row:
            return None
        img = dict(row)
        conn.execute('DELETE FROM wallpaper_images WHERE id = ?', (image_id,))
        conn.commit()
    return img


# ── Polices personnalisées (voir /admin/texts et app.py : _all_fonts(),
# _qr_live_burn_font) ────────────────────────────────────────────────────────

def list_custom_fonts():
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM custom_fonts ORDER BY created_at ASC'
        ).fetchall()
    return [dict(r) for r in rows]


def get_custom_font_by_id(font_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM custom_fonts WHERE id = ?', (font_id,)).fetchone()
    return dict(row) if row else None


def create_custom_font(label, base_family, filename):
    """Insère une police personnalisée, en garantissant l'unicité de
    `family` (contrainte UNIQUE en base) : si `base_family` (slug déjà
    calculé par _slugify_font_family) existe déjà, ajoute un suffixe
    -2, -3... jusqu'à trouver un identifiant libre."""
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        family = base_family
        suffix = 2
        while conn.execute('SELECT 1 FROM custom_fonts WHERE family = ?', (family,)).fetchone():
            family = f'{base_family}-{suffix}'
            suffix += 1
        cur = conn.execute(
            'INSERT INTO custom_fonts(family, label, filename, created_at) VALUES(?,?,?,?)',
            (family, label, filename, created_at)
        )
        conn.commit()
        return dict(id=cur.lastrowid, family=family, label=label, filename=filename, created_at=created_at)


def delete_custom_font_db(font_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM custom_fonts WHERE id = ?', (font_id,)).fetchone()
        if not row:
            return None
        font = dict(row)
        conn.execute('DELETE FROM custom_fonts WHERE id = ?', (font_id,))
        conn.commit()
    return font


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


def list_gallery_combined(sort='desc', kind='', source='', page=1, page_size=None, media_uid='', tag=''):
    """Fusionne captures officielles + uploads invités approuvés pour la
    galerie, uniquement utilisée quand guest_upload.include_in_gallery est
    activé (voir gallery() dans app.py — sinon list_captures() seule est
    utilisée, comportement inchangé). Tri/pagination faits en Python : les
    deux tables ont des schémas différents (pas d'UNION SQL simple), et les
    volumes visés (un événement) ne posent pas de problème de performance.
    Chaque item porte un champ 'source' ('official'|'guest') pour le badge
    et le filtre côté galerie. media_uid : filtre optionnel (recherche par
    ID). tag : filtre optionnel par libellé — les tags ne portant que sur
    les captures officielles, un filtre tag actif exclut d'office tous les
    uploads invités (aucun ne pourra jamais correspondre)."""
    items = []
    if source in ('', 'official'):
        conditions, params = [], []
        if kind in ('photo', 'video'):
            conditions.append('kind=?')
            params.append(kind)
        if tag:
            conditions.append('id IN (SELECT capture_id FROM capture_tags WHERE label = ?)')
            params.append(tag)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ''
        with closing(db_conn()) as conn:
            rows = conn.execute(f'SELECT * FROM captures {where}', params).fetchall()
        for r in rows:
            d = dict(r)
            d['source'] = 'official'
            items.append(d)

    # Les uploads invités sont toujours kind='photo' : sans effet si un
    # filtre 'video' est actif. Aucun tag possible sur un upload invité :
    # un filtre tag actif les exclut entièrement (pas de requête inutile).
    if source in ('', 'guest') and kind in ('', 'photo') and not tag:
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
    """Supprime de la DB (+ votes associés) et retourne le dict (l'appelant
    supprime fichier + thumb). Le nettoyage des votes (absent avant cette
    correction, même défaut que l'ancien delete_capture()) évite de
    laisser des lignes orphelines dans `votes` (source='guest')."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM guest_uploads WHERE id = ?', (upload_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        conn.execute('DELETE FROM guest_uploads WHERE id = ?', (upload_id,))
        conn.execute('DELETE FROM votes WHERE source = ? AND capture_id = ?', ('guest', upload_id))
        conn.commit()
    return item


def list_guest_uploads_in_range(start_iso, end_iso):
    """Tous les uploads invités (quel que soit leur statut — pending/
    approved/rejected) dont created_at est dans [start_iso, end_iso]
    (bornes incluses). Même logique que list_captures_in_range(), utilisée
    par l'archivage et le nettoyage par plage de dates quand l'option
    « médias invités » est cochée (/admin/archive)."""
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT * FROM guest_uploads WHERE created_at >= ? AND created_at <= ? ORDER BY created_at ASC',
            (start_iso, end_iso)
        ).fetchall()
    return [dict(r) for r in rows]


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


# ── Codes invités ─────────────────────────────────────────────────────────────
# Voir /admin/guest_codes : table de correspondance code numérique <-> texte
# libre. Un badge QR-code peut encoder directement ce code numérique plutôt
# que le texte final — la résolution code -> texte se fait à la détection
# (voir _qr_detect_boxes_robust, app.py), donc s'applique uniformément à
# l'aperçu en direct, à l'incrustation sur le média et au tag automatique.

# Clés de tri autorisées pour list_guest_codes()/purge_guest_codes_first_n()
# (whitelist : la clé vient du formulaire admin, jamais interpolée telle
# quelle dans le SQL). Les libellés affichés (FR) vivent côté app.py
# (_GUEST_CODES_SORTS), cette table est la seule source de vérité pour le
# SQL correspondant à chaque clé.
GUEST_CODES_SORT_SQL = {
    'created_desc': 'created_at DESC',
    'created_asc':  'created_at ASC',
    'texte_asc':    'texte COLLATE NOCASE ASC',
    'texte_desc':   'texte COLLATE NOCASE DESC',
    'code_asc':     'code ASC',
    'code_desc':    'code DESC',
    'length_asc':   'LENGTH(texte) ASC, texte COLLATE NOCASE ASC',
    'length_desc':  'LENGTH(texte) DESC, texte COLLATE NOCASE ASC',
}


def list_guest_codes(sort='created_desc', q=''):
    """`sort` : une clé de GUEST_CODES_SORT_SQL (repli sur 'created_desc' si
    inconnue). `q` : filtre optionnel, recherche partielle sur texte OU
    code."""
    order_sql = GUEST_CODES_SORT_SQL.get(sort, GUEST_CODES_SORT_SQL['created_desc'])
    q = (q or '').strip()
    with closing(db_conn()) as conn:
        if q:
            like = f'%{q}%'
            rows = conn.execute(
                f'SELECT * FROM guest_codes WHERE texte LIKE ? OR code LIKE ? ORDER BY {order_sql}',
                (like, like)
            ).fetchall()
        else:
            rows = conn.execute(f'SELECT * FROM guest_codes ORDER BY {order_sql}').fetchall()
    return [dict(r) for r in rows]


def get_guest_code_by_id(guest_code_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM guest_codes WHERE id = ?', (guest_code_id,)).fetchone()
    return dict(row) if row else None


def get_guest_code_text(code):
    """Retourne le `texte` associé à `code` s'il correspond à un code invité
    existant, sinon None (le QR-code doit alors être traité comme du texte
    brut). Appelée à chaque détection QR-code (voir _qr_detect_boxes_robust,
    app.py) : requête indexée (UNIQUE sur `code`), coût négligeable."""
    code = (code or '').strip()
    if not code:
        return None
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT texte FROM guest_codes WHERE code = ?', (code,)).fetchone()
    return row['texte'] if row else None


def generate_guest_code(length):
    """Génère un code numérique aléatoire unique (non déjà présent dans
    guest_codes) de `length` chiffres — même principe que
    generate_media_uid() ci-dessus."""
    try:
        length = max(2, min(int(length), 10))
    except (TypeError, ValueError):
        length = 4
    with closing(db_conn()) as conn:
        for _ in range(30):
            candidate = ''.join(random.choices('0123456789', k=length))
            exists = conn.execute('SELECT 1 FROM guest_codes WHERE code = ?', (candidate,)).fetchone()
            if not exists:
                return candidate
    # Filet de sécurité (ne devrait jamais être atteint) : garantit quand
    # même une terminaison plutôt qu'une boucle infinie.
    return str(int(datetime.now().timestamp() * 1000))[-length:]


def create_guest_code(texte, length):
    """Crée un nouveau code invité : `texte` fourni par l'admin, `code`
    généré aléatoirement (voir generate_guest_code). Retourne le dict créé."""
    code = generate_guest_code(length)
    created_at = datetime.now().isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        cur = conn.execute(
            'INSERT INTO guest_codes(code, texte, created_at) VALUES(?,?,?)',
            (code, texte, created_at)
        )
        conn.commit()
        return {'id': cur.lastrowid, 'code': code, 'texte': texte, 'created_at': created_at}


def update_guest_code_texte(guest_code_id, texte):
    with closing(db_conn()) as conn:
        conn.execute('UPDATE guest_codes SET texte=? WHERE id=?', (texte, guest_code_id))
        conn.commit()


def regenerate_guest_code(guest_code_id, length):
    """Remplace le code (numéro) d'un code invité existant par un nouveau
    code aléatoire unique, sans toucher au texte associé. Retourne le
    nouveau code, ou None si `guest_code_id` est introuvable."""
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT id FROM guest_codes WHERE id = ?', (guest_code_id,)).fetchone()
        if not row:
            return None
    new_code = generate_guest_code(length)
    with closing(db_conn()) as conn:
        conn.execute('UPDATE guest_codes SET code=? WHERE id=?', (new_code, guest_code_id))
        conn.commit()
    return new_code


def delete_guest_code_db(guest_code_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM guest_codes WHERE id = ?', (guest_code_id,)).fetchone()
        if not row:
            return None
        conn.execute('DELETE FROM guest_codes WHERE id = ?', (guest_code_id,))
        conn.commit()
    return dict(row)


def upsert_guest_code(code, texte, created_at=None):
    """Insère ou met à jour (par `code`, déjà validé par l'appelant — voir
    import CSV, app.py) un code invité. Si `code` existe déjà, seul `texte`
    est mis à jour (la date de création d'origine est conservée). Sinon,
    crée une nouvelle ligne avec `created_at` fourni (réimport d'un export
    CSV, pour préserver la date d'origine) ou l'instant présent. Retourne
    (id, 'created'|'updated')."""
    with closing(db_conn()) as conn:
        existing = conn.execute('SELECT id FROM guest_codes WHERE code = ?', (code,)).fetchone()
        if existing:
            conn.execute('UPDATE guest_codes SET texte=? WHERE id=?', (texte, existing['id']))
            conn.commit()
            return existing['id'], 'updated'
        cur = conn.execute(
            'INSERT INTO guest_codes(code, texte, created_at) VALUES(?,?,?)',
            (code, texte, created_at or datetime.now().isoformat(timespec='seconds'))
        )
        conn.commit()
        return cur.lastrowid, 'created'


def purge_guest_codes_by_date(date_from='', date_to=''):
    """Supprime les codes invités dont `created_at` tombe dans
    [date_from, date_to] (bornes 'YYYY-MM-DD', l'une des deux pouvant être
    vide pour une borne ouverte — mais pas les deux, voir appelant).
    Retourne le nombre de lignes supprimées. Même convention de bornage que
    le filtre date du diaporama (voir _slideshow_settings, app.py) :
    date_to est étendu à 23:59:59 pour inclure toute la journée."""
    date_from = (date_from or '').strip()
    date_to = (date_to or '').strip()
    conditions, params = [], []
    if date_from:
        conditions.append('created_at >= ?')
        params.append(date_from)
    if date_to:
        conditions.append('created_at <= ?')
        params.append(date_to + 'T23:59:59')
    if not conditions:
        return 0
    where = ' AND '.join(conditions)
    with closing(db_conn()) as conn:
        cur = conn.execute(f'DELETE FROM guest_codes WHERE {where}', params)
        conn.commit()
        return cur.rowcount


def purge_guest_codes_first_n(n, sort='created_asc'):
    """Supprime les `n` premiers codes invités selon l'ordre `sort` (voir
    GUEST_CODES_SORT_SQL) — p.ex. 'created_asc' = les plus anciens,
    'length_desc' = les textes les plus longs. Retourne le nombre de lignes
    effectivement supprimées (peut être < n s'il y a moins de n lignes)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 0
    if n < 1:
        return 0
    order_sql = GUEST_CODES_SORT_SQL.get(sort, GUEST_CODES_SORT_SQL['created_asc'])
    with closing(db_conn()) as conn:
        rows = conn.execute(f'SELECT id FROM guest_codes ORDER BY {order_sql} LIMIT ?', (n,)).fetchall()
        ids = [r['id'] for r in rows]
        if ids:
            placeholders = ','.join('?' * len(ids))
            conn.execute(f'DELETE FROM guest_codes WHERE id IN ({placeholders})', ids)
            conn.commit()
        return len(ids)


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


def get_tags_for_captures(capture_ids):
    """Version "bulk" de list_capture_tags() pour éviter le N+1 quand on
    affiche une liste de captures (galerie) : une seule requête, retourne
    {capture_id: [label, ...]}."""
    capture_ids = list(capture_ids)
    if not capture_ids:
        return {}
    placeholders = ','.join('?' * len(capture_ids))
    with closing(db_conn()) as conn:
        rows = conn.execute(
            f'SELECT capture_id, label FROM capture_tags WHERE capture_id IN ({placeholders}) ORDER BY id ASC',
            capture_ids
        ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r['capture_id'], []).append(r['label'])
    return result


def list_distinct_tag_labels():
    """Libellés distincts réellement utilisés (prédéfinis ou libres),
    pour peupler le filtre "Tags" de la galerie — indépendant de la liste
    des tags prédéfinis (table tags), qui peut différer de ce qui a été
    effectivement appliqué (tags supprimés depuis, tags libres, ...)."""
    with closing(db_conn()) as conn:
        rows = conn.execute(
            'SELECT DISTINCT label FROM capture_tags ORDER BY label COLLATE NOCASE ASC'
        ).fetchall()
    return [r['label'] for r in rows]


def list_capture_tags_with_media(limit=300):
    """Liste des assignations de tags les plus récentes, avec les infos du
    média concerné (miniature, genre, ID) — alimente la section "Tags
    appliqués" de /admin/tags (journal des tags posés par les invités
    depuis le kiosque)."""
    with closing(db_conn()) as conn:
        rows = conn.execute("""
            SELECT ct.id AS assignment_id, ct.tag_id, ct.label, ct.created_at,
                   c.id AS capture_id, c.kind, c.filename, c.thumb_filename, c.media_uid
            FROM capture_tags ct
            JOIN captures c ON c.id = ct.capture_id
            ORDER BY ct.id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]
