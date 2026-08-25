import io
import logging
import os
import re
import socket
import subprocess
from datetime import datetime
from html import escape
from html.parser import HTMLParser
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


def get_network_info() -> dict:
    """IP locale + port d'écoute de l'application, pour l'affichage de
    diagnostic sur l'interface principale (voir kiosk.network_info_taps
    dans app.py, réglable depuis /admin/application) — même détection d'IP
    que build_gallery_url() ci-dessous, sans se limiter au repli 127.0.0.1
    (affiché tel quel si la détection échoue, plutôt que masqué)."""
    return {
        'ip': _get_local_ip(),
        'port': int(CONFIG['server'].get('port', 80)),
    }


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


# Cache QR personnalisés (pages promo) : clé = (contenu, couleur, taille de
# rendu). Distinct de _QR_CACHE ci-dessus (QR fixe noir/blanc → galerie).
_QR_CACHE_CUSTOM: dict = {}


def generate_qr_png_custom(data: str, fill_color: str = '#000000',
                            back_color: str = '#ffffff', box_size: int = 10) -> io.BytesIO:
    """QR code personnalisable (contenu, couleur) pour les pages promo — voir
    /admin/slideshow → Pages promo. Distinct de generate_qr_png (QR fixe noir
    sur blanc vers la galerie, utilisé ailleurs dans l'app) : ici le contenu
    ET la couleur varient par page promo, donc la clé de cache inclut les
    deux. `box_size` fixe la résolution de rendu — le QR est ensuite affiché
    à la taille voulue via les attributs HTML width/height (comme
    generate_qr_png), pas besoin de régénérer juste pour changer l'affichage."""
    data = data or ''
    fill_color = fill_color or '#000000'
    back_color = back_color or '#ffffff'
    key = (data, fill_color, back_color, box_size)
    if key not in _QR_CACHE_CUSTOM:
        logger.info('Génération QR code personnalisé (page promo)')
        qr = qrcode.QRCode(box_size=box_size, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        _QR_CACHE_CUSTOM[key] = buf.getvalue()
    return io.BytesIO(_QR_CACHE_CUSTOM[key])


# ── Texte WYSIWYG des pages promo ────────────────────────────────────────────
# Nettoyage du HTML produit par l'éditeur (voir static/promo-editor.js) avant
# stockage en base : liste blanche de balises de mise en forme basique
# (gras/italique/souligné/listes/paragraphes), seul attribut toléré :
# style="text-align: ..." — pas de script, pas de lien, pas de gestionnaire
# d'évènement. L'admin est seule à pouvoir écrire ce contenu, mais autant
# rester prudent (défense en profondeur, coût quasi nul).

_PROMO_HTML_ALLOWED_TAGS = {
    'p': set(), 'br': set(), 'b': set(), 'strong': set(), 'i': set(), 'em': set(),
    'u': set(), 'ul': set(), 'ol': set(), 'li': set(), 'div': {'style'}, 'span': {'style'},
    # v2.0.1 : image (sélecteur médiathèque/captures du WYSIWYG -- voir
    # static/promo-editor.js) et tableaux basiques.
    'img': {'src', 'alt', 'style'},
    'table': set(), 'thead': set(), 'tbody': set(), 'tr': set(),
    'td': {'colspan', 'rowspan'}, 'th': {'colspan', 'rowspan'},
}
_PROMO_HTML_STYLE_RE = re.compile(r'^text-align\s*:\s*(left|center|right|justify)\s*;?$')
# v2.0.2 : redimensionnement/alignement d'image (voir static/promo-editor.js
# -- poignée de redimensionnement + boutons gauche/centre/droite/normal).
# Contrairement à _PROMO_HTML_STYLE_RE ci-dessus (une seule déclaration,
# correspondance exacte de toute la valeur), le style d'une image peut
# combiner plusieurs déclarations : chacune est validée indépendamment.
# Le style qui arrive ici est TOUJOURS généré par promo-editor.js (jamais
# tapé à la main par l'admin), mais on valide quand même strictement, au
# cas où le HTML aurait été modifié hors de l'éditeur (collé depuis
# ailleurs, etc.).
_PROMO_HTML_IMG_STYLE_PROPS = {
    'width':         re.compile(r'^[1-9][0-9]{0,3}px$'),
    'height':        re.compile(r'^(auto|[1-9][0-9]{0,3}px)$'),
    'float':         re.compile(r'^(left|right)$'),
    'display':       re.compile(r'^(block|inline-block)$'),
    'margin-top':    re.compile(r'^(0|auto|[1-9][0-9]{0,2}px)$'),
    'margin-right':  re.compile(r'^(0|auto|[1-9][0-9]{0,2}px)$'),
    'margin-bottom': re.compile(r'^(0|auto|[1-9][0-9]{0,2}px)$'),
    'margin-left':   re.compile(r'^(0|auto|[1-9][0-9]{0,2}px)$'),
}


def _sanitize_img_style(raw_style: str) -> str:
    kept = []
    for decl in raw_style.split(';'):
        if ':' not in decl:
            continue
        prop, _, val = decl.partition(':')
        prop = prop.strip().lower()
        val = val.strip()
        rx = _PROMO_HTML_IMG_STYLE_PROPS.get(prop)
        if rx and rx.match(val):
            kept.append(f'{prop}:{val}')
    return ';'.join(kept)
# src d'image : uniquement un chemin relatif au même site -- jamais de
# protocole (javascript:, data:...), jamais "//hôte-externe/..." qui serait
# interprété comme une URL protocole-relative vers un autre domaine.
# Cohérent avec le fait que le sélecteur ne propose que des chemins locaux
# (/static/..., /media/...).
_PROMO_HTML_IMG_SRC_RE = re.compile(r'^/(?!/)[A-Za-z0-9_\-./%]+$')
_PROMO_HTML_SPAN_RE = re.compile(r'^[1-9][0-9]?$')


class _PromoHtmlSanitizer(HTMLParser):
    """`_skip_depth` compte les balises NON autorisées actuellement ouvertes
    (compteur simple, pas une pile par nom de balise : suffisant ici, le
    contenu vient soit de l'éditeur WYSIWYG soit d'un admin de confiance) —
    tant qu'il est > 0, le texte rencontré (handle_data) est ignoré, pour ne
    jamais laisser fuir en clair le contenu d'un <script>/<style> retiré."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip_depth = 0

    def _emit_start(self, tag, attrs):
        allowed_attrs = _PROMO_HTML_ALLOWED_TAGS[tag]
        kept = []
        for name, value in attrs:
            if (name == 'style' and tag in ('div', 'span') and 'style' in allowed_attrs and value
                    and _PROMO_HTML_STYLE_RE.match(value.strip())):
                kept.append(f'style="{escape(value.strip(), quote=True)}"')
            elif name == 'style' and tag == 'img' and 'style' in allowed_attrs and value:
                cleaned = _sanitize_img_style(value)
                if cleaned:
                    kept.append(f'style="{escape(cleaned, quote=True)}"')
            elif (name == 'src' and 'src' in allowed_attrs and value
                    and _PROMO_HTML_IMG_SRC_RE.match(value.strip())):
                kept.append(f'src="{escape(value.strip(), quote=True)}"')
            elif name == 'alt' and 'alt' in allowed_attrs and value is not None:
                kept.append(f'alt="{escape(value, quote=True)}"')
            elif (name in ('colspan', 'rowspan') and name in allowed_attrs and value
                    and _PROMO_HTML_SPAN_RE.match(value.strip())):
                kept.append(f'{name}="{value.strip()}"')
        attr_str = (' ' + ' '.join(kept)) if kept else ''
        self.out.append(f'<{tag}{attr_str}>')

    def handle_starttag(self, tag, attrs):
        if tag in _PROMO_HTML_ALLOWED_TAGS:
            self._emit_start(tag, attrs)
        else:
            self._skip_depth += 1

    def handle_startendtag(self, tag, attrs):
        if tag in _PROMO_HTML_ALLOWED_TAGS:
            self._emit_start(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _PROMO_HTML_ALLOWED_TAGS:
            if tag not in ('br', 'img'):
                self.out.append(f'</{tag}>')
        elif self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.out.append(escape(data))


def sanitize_promo_html(raw_html: str) -> str:
    """Nettoie le HTML d'une page promo avant stockage (voir
    _PROMO_HTML_ALLOWED_TAGS ci-dessus) — retourne une chaîne vide si le
    contenu est vide ou si le nettoyage échoue plutôt que de faire planter
    l'enregistrement de la page."""
    if not raw_html:
        return ''
    parser = _PromoHtmlSanitizer()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        logger.exception('Échec de nettoyage HTML page promo, contenu ignoré')
        return ''
    return ''.join(parser.out)


# ── Emplacements dynamiques ({ip}/{port}) dans les textes admin ─────────────
# Convention déjà utilisée par idle_timer_badge_text ("Retour dans {n}s",
# substitué côté client) : ici {ip}/{port} sont substitués côté SERVEUR, au
# moment où le texte est servi au public (kiosque, galerie, /bestof, QR
# codes, texte associé à un code invité...) — jamais quand la même valeur
# est relue pour repeupler un formulaire d'édition admin, qui doit continuer
# à afficher {ip}/{port} tels quels pour rester éditable. C'est donc à
# l'appelant (app.py, sur chaque route PUBLIQUE concernée) d'appeler cette
# fonction — jamais dans get_setting()/les fonctions de lecture partagées
# par l'admin et le public, sous peine de casser l'édition.
def resolve_dynamic_placeholders(raw: str) -> str:
    """Remplace {ip} et {port} par l'adresse réseau et le port courants de
    l'application, résolus à CHAQUE appel (jamais stockés résolus en base) :
    restent donc corrects même si l'IP change (DHCP, changement de réseau,
    redémarrage du PC...), sans repasser par l'admin. No-op si `raw` ne
    contient aucun des deux emplacements (évite la détection réseau — un
    socket UDP, voir get_network_info — pour la quasi-totalité des textes,
    qui n'en ont pas besoin)."""
    if not raw or ('{ip}' not in raw and '{port}' not in raw):
        return raw or ''
    info = get_network_info()
    return raw.replace('{ip}', info['ip']).replace('{port}', str(info['port']))


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


# ── Fond d'écran Windows ─────────────────────────────────────────────────────
# Change le fond d'écran du Bureau Windows via l'API native
# SystemParametersInfoW (ctypes, aucune dépendance supplémentaire) — même
# effet qu'un changement fait depuis les Paramètres Windows : immédiat et
# persistant après redémarrage. Voir /admin/application (gestion des images
# et bouton "Appliquer" — app.py admin_wallpaper_apply).

_SPI_SETDESKWALLPAPER = 20
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDCHANGE = 0x02


def set_windows_wallpaper(image_path: Path) -> tuple[bool, str]:
    if os.name != 'nt':
        return False, 'Fond d\'écran indisponible sur cette plateforme (Windows requis).'
    if not image_path.exists():
        return False, 'Image introuvable.'
    try:
        import ctypes
        # SystemParametersInfoW attend un chemin absolu ; les formats PNG/JPG
        # sont acceptés nativement depuis Windows 7 (pas besoin de convertir
        # en BMP), le BMP restant accepté aussi pour compatibilité.
        ok = ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETDESKWALLPAPER, 0, str(image_path.resolve()),
            _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE,
        )
    except Exception as exc:
        logger.warning('Échec changement de fond d\'écran : %s', exc)
        return False, f'Échec du changement de fond d\'écran : {exc}'
    if not ok:
        logger.warning('Échec changement de fond d\'écran (SystemParametersInfoW a retourné 0) : %s', image_path)
        return False, 'Échec du changement de fond d\'écran (Windows a refusé la demande).'
    logger.info('Fond d\'écran Windows changé : %s', image_path)
    return True, 'Fond d\'écran changé.'
