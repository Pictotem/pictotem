"""API REST (lecture seule) d'accès à la base des ID captures + tags associés
(tables `captures` et `capture_tags`, voir db.py) — un petit serveur HTTP
indépendant du serveur Flask principal (app.py), avec son propre port
d'écoute, ses propres identifiants et son propre cycle de vie. Même
architecture que app/guest_api.py (API codes invités), simplement portée à
un autre jeu de données : pensée pour un usage externe (logiciel tiers,
script d'export...) qui a besoin de lire, pour chaque capture officielle,
son ID média, son type, ses tags et son score de votes — séparément du back
office (protégé par la session admin habituelle, voir auth.py).

Réglages (voir _cfg() ci-dessous pour les valeurs par défaut, [media_api]
dans config.toml) : port d'écoute, login/mot de passe (HTTP Basic),
démarrage automatique avec l'application ou manuel, durée de rétention du
journal de connexions. Tous surchargeables depuis le back office
(/admin/tags, bloc "API REST — accès aux ID média & tags", voir app.py :
_block_ctx_tags_media_api et les routes /admin/tags/api/*) — même priorité
que le reste de l'application : variable d'environnement > réglage en base
(settings) > config.toml (voir _get_api_login/_get_api_password, même
principe que auth._get_admin_password).

Endpoints exposés (authentification HTTP Basic obligatoire sur les deux) :
  GET /api/media          -> liste complète
                              [{id, capture_id, type, tags, date, votes}, ...]
  GET /api/media/<id>     -> un seul média (recherché par son ID média,
                              c.-à-d. media_uid — voir la tuile "Tags & ID
                              média"), ou 404

  id     : ID média unique (media_uid), l'identifiant recherchable/affiché
           côté back office et galerie — null si la capture est antérieure
           à la mise en place de cette fonctionnalité.
  type   : 'photo' | 'photo_strip' | 'video'.
  tags   : tous les tags (prédéfinis et libres) posés sur ce média, dans
           l'ordre où ils ont été appliqués.
  date   : date de capture (ISO 8601, horodatage local de l'application).
  votes  : score de votes officiels actuellement cumulé sur ce média.

Portée aux captures officielles uniquement (pas aux uploads invités), comme
le système de tags lui-même (voir db.py).

Cycle de vie pilotable (actif/inactif) : start_media_api_server() /
stop_media_api_server() / media_api_is_running() ci-dessous, appelées au
choix automatiquement au lancement de l'application (si media_api.autostart,
voir app.py : bloc `if __name__ == '__main__'`) ou manuellement depuis le
back office — le serveur tourne alors dans son propre thread (werkzeug
`make_server`, qui permet un arrêt propre, contrairement à `app.run()`)
sans jamais bloquer ni interférer avec le serveur principal.

Toutes les requêtes reçues (authentifiées ou non) sont journalisées dans
logs/media_api.log — date, IP, méthode + chemin, statut, résumé des données
renvoyées — jamais le mot de passe. Rotation quotidienne avec purge
automatique au-delà de media_api.log_retention_days (voir _reload_log_handler)."""

import json
import logging
import os
import secrets
import socket
import threading
from logging.handlers import TimedRotatingFileHandler

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from config_loader import CONFIG, LOGS_DIR
from db import get_media_with_tags_by_uid, list_media_with_tags

# ── Réglages ──────────────────────────────────────────────────────────────
# Priorité, comme le reste de l'authentification de l'application (voir
# auth._get_admin_password) : variable d'environnement > réglage en base
# (modifiable depuis le back office, sans redémarrage) > config.toml.

_DEFAULTS = {
    'port': 8082,
    'login': 'lecture',
    'password': 'changeme-media-api-password',
    'log_retention_days': 30,
}

_UNSET = object()


def _cfg() -> dict:
    return CONFIG.get('media_api', {})


def media_api_autostart() -> bool:
    from db import get_setting
    raw = get_setting('media_api.autostart', '')
    if raw in ('0', '1'):
        return raw == '1'
    return bool(_cfg().get('autostart', False))


def set_media_api_autostart(enabled: bool) -> None:
    from db import set_setting
    set_setting('media_api.autostart', '1' if enabled else '0')


def media_api_port() -> int:
    from db import get_setting
    raw = get_setting('media_api.port', '') or str(_cfg().get('port', _DEFAULTS['port']))
    try:
        port = int(raw)
        return port if 1 <= port <= 65535 else _DEFAULTS['port']
    except (TypeError, ValueError):
        return _DEFAULTS['port']


def set_media_api_port(port) -> bool:
    """Retourne False (aucun changement) si `port` n'est pas un entier valide."""
    from db import set_setting
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not (1 <= port <= 65535):
        return False
    set_setting('media_api.port', str(port))
    return True


def media_api_log_retention_days() -> int:
    from db import get_setting
    raw = get_setting('media_api.log_retention_days', '') or str(_cfg().get('log_retention_days', _DEFAULTS['log_retention_days']))
    try:
        days = int(raw)
        return days if 1 <= days <= 3650 else _DEFAULTS['log_retention_days']
    except (TypeError, ValueError):
        return _DEFAULTS['log_retention_days']


def set_media_api_log_retention_days(days) -> bool:
    from db import set_setting
    try:
        days = int(days)
    except (TypeError, ValueError):
        return False
    if not (1 <= days <= 3650):
        return False
    set_setting('media_api.log_retention_days', str(days))
    return True


def _get_api_login() -> str:
    env = os.environ.get('PICTOTEM_MEDIA_API_LOGIN')
    if env:
        return env
    from db import get_setting
    db_value = get_setting('media_api.login', '')
    if db_value:
        return db_value
    return str(_cfg().get('login', _DEFAULTS['login']))


def set_media_api_login(login: str) -> None:
    from db import set_setting
    set_setting('media_api.login', (login or '').strip())


def _get_api_password() -> str:
    env = os.environ.get('PICTOTEM_MEDIA_API_PASSWORD')
    if env:
        return env
    from db import get_setting
    db_value = get_setting('media_api.password', _UNSET)
    if db_value is not _UNSET:
        return db_value
    return str(_cfg().get('password', _DEFAULTS['password']))


def set_media_api_password(password: str) -> None:
    from db import set_setting
    set_setting('media_api.password', (password or '').strip())


def media_api_credentials_status() -> dict:
    """Origine des identifiants actuellement actifs, pour affichage
    informatif côté back office (jamais le mot de passe en clair) — même
    principe que auth.admin_password_status()."""
    if os.environ.get('PICTOTEM_MEDIA_API_LOGIN') or os.environ.get('PICTOTEM_MEDIA_API_PASSWORD'):
        return {'source': 'env'}
    from db import get_setting
    if get_setting('media_api.login', '') or get_setting('media_api.password', _UNSET) is not _UNSET:
        return {'source': 'db'}
    return {'source': 'config'}


# ── Journal de connexions ───────────────────────────────────────────────────
# Fichier dédié (logs/media_api.log), séparé de logs/app.log — rotation
# quotidienne avec purge automatique au-delà de media_api.log_retention_days
# (TimedRotatingFileHandler, backupCount). Le handler est reconstruit à
# chaque démarrage du serveur (voir start_media_api_server) pour que la
# rétention réglée depuis le back office s'applique dès le prochain
# démarrage manuel de l'API, sans exiger un redémarrage complet de
# l'application.

_api_logger = logging.getLogger('pictotem.media_api')
_api_logger.setLevel(logging.INFO)
_api_logger.propagate = False


def _reload_log_handler() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    for h in list(_api_logger.handlers):
        _api_logger.removeHandler(h)
        h.close()
    handler = TimedRotatingFileHandler(
        LOGS_DIR / 'media_api.log', when='midnight',
        backupCount=media_api_log_retention_days(), encoding='utf-8',
    )
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    _api_logger.addHandler(handler)


# Pas d'appel à _reload_log_handler() ici, au niveau module : ce module est
# importé par app.py AVANT que init_db() n'ait tourné (voir le bloc
# `if __name__ == '__main__'`), et media_api_log_retention_days() ci-dessus
# interroge la table `settings` — absente sur une toute première
# installation avant init_db(). Le handler est donc construit seulement au
# premier démarrage réel du serveur (voir start_media_api_server()
# ci-dessous), qui n'intervient jamais avant init_db().


def _client_ip() -> str:
    # Même logique que auth.client_ip(), reprise ici plutôt qu'importée pour
    # ne pas coupler ce module (serveur indépendant) aux sessions Flask de
    # l'application principale — la seule chose reprise de la config est le
    # réglage trust_proxy, déjà partagé par toute l'authentification.
    if CONFIG.get('auth', {}).get('trust_proxy', False):
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return request.remote_addr or ''


def _log_request(status_code: int, detail: str = '') -> None:
    path = request.full_path.rstrip('?') if request.query_string else request.path
    _api_logger.info(
        '%s %s %s -> %s%s',
        _client_ip(), request.method, path, status_code,
        f' | {detail}' if detail else '',
    )


def _summarize_media(payload: list, limit: int = 20) -> str:
    ids = [str(item['id'] or item['capture_id']) for item in payload[:limit]]
    suffix = f', … (+{len(payload) - limit})' if len(payload) > limit else ''
    return f'{len(payload)} média(s) renvoyé(s) : {", ".join(ids)}{suffix}'


# ── Application Flask dédiée ──────────────────────────────────────────────

media_api_app = Flask('pictotem_media_api')


@media_api_app.before_request
def _require_basic_auth():
    auth = request.authorization
    login_ok = bool(auth) and secrets.compare_digest(auth.username or '', _get_api_login())
    password_ok = bool(auth) and secrets.compare_digest(auth.password or '', _get_api_password())
    if not (login_ok and password_ok):
        _log_request(401, f"échec d'authentification (login {'reçu' if auth else 'absent'})")
        resp = jsonify({'error': 'Authentification requise.'})
        resp.status_code = 401
        resp.headers['WWW-Authenticate'] = 'Basic realm="Pictotem - ID media et tags"'
        return resp


@media_api_app.route('/api/media', methods=['GET'])
def api_list_media():
    payload = list_media_with_tags()
    _log_request(200, _summarize_media(payload))
    return jsonify(payload)


@media_api_app.route('/api/media/<media_id>', methods=['GET'])
def api_get_media(media_id):
    payload = get_media_with_tags_by_uid(media_id)
    if not payload:
        _log_request(404, f'média {media_id!r} introuvable')
        return jsonify({'error': 'Média introuvable.'}), 404
    _log_request(200, json.dumps(payload, ensure_ascii=False))
    return jsonify(payload)


@media_api_app.errorhandler(404)
def _api_not_found(_e):
    return jsonify({'error': 'Route inconnue. Voir GET /api/media ou GET /api/media/<id>.'}), 404


# ── Cycle de vie du serveur (actif / inactif, démarrage auto ou manuel) ────
# werkzeug.serving.make_server (plutôt que media_api_app.run(), qui bloque
# sans offrir de coupure propre) : permet un .shutdown() immédiat depuis
# stop_media_api_server() ci-dessous, appelé aussi bien par le bouton manuel
# du back office qu'à l'extinction de l'application.

_server_lock = threading.Lock()
_server_thread = None  # instance de _ServerThread, ou None si arrêté
_last_error = ''       # dernier message d'erreur au démarrage (affichage back office)


def _probe_port_free(host: str, port: int) -> str:
    """Tente un bind/close jetable sur (host, port) — renvoie '' si libre,
    ou un message d'erreur explicite sinon. Sert de garde-fou AVANT
    make_server() ci-dessous : werkzeug, sur un échec de bind, affiche un
    message puis appelle sys.exit(1) au lieu de lever OSError (voir
    BaseWSGIServer.__init__) — un sys.exit(1) qu'un simple except OSError
    ne rattrape pas et qui, appelé au démarrage de l'application (thread
    principal, voir app.py) ou depuis une requête admin (thread Flask),
    aurait des conséquences bien plus graves qu'un message d'erreur.
    Ce pré-contrôle évite d'atteindre make_server() dans le cas courant
    (port déjà occupé) ; voir aussi le filet de sécurité supplémentaire
    dans start_media_api_server() pour la rare condition de course
    restante entre ce test et le bind réel."""
    probe_host = '0.0.0.0' if host in ('', '0.0.0.0') else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind((probe_host, port))
    except OSError as exc:
        return str(exc)
    finally:
        s.close()
    return ''


class _ServerThread(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True, name='media-api-server')
        # Construit (et bind) le serveur ICI, dans le thread appelant : une
        # erreur de bind (port déjà utilisé...) lève alors une OSError
        # immédiate et synchrone, avant même de démarrer le thread — voir
        # start_media_api_server() ci-dessous, qui s'appuie sur cette
        # garantie pour renvoyer un message d'erreur précis plutôt qu'un
        # thread démarré puis mort en silence.
        self._srv = make_server(host, port, media_api_app, threaded=True)

    def run(self):
        self._srv.serve_forever()

    def shutdown(self):
        self._srv.shutdown()


def media_api_is_running() -> bool:
    with _server_lock:
        return _server_thread is not None and _server_thread.is_alive()


def media_api_last_error() -> str:
    return _last_error


def start_media_api_server(host: str = None) -> tuple:
    """Démarre le serveur API s'il n'est pas déjà actif. Retourne (ok, message)."""
    global _server_thread, _last_error
    host = host or CONFIG.get('server', {}).get('host', '0.0.0.0')
    with _server_lock:
        if _server_thread is not None and _server_thread.is_alive():
            return True, 'API déjà active.'
        _reload_log_handler()
        port = media_api_port()
        probe_error = _probe_port_free(host, port)
        if probe_error:
            _last_error = f'Port {port} indisponible ({probe_error}).'
            _api_logger.warning('Échec du démarrage : %s', _last_error)
            return False, _last_error
        try:
            thread = _ServerThread(host, port)
        except (OSError, SystemExit) as exc:
            # Filet de sécurité si le port s'est libéré puis réoccupé entre
            # _probe_port_free() ci-dessus et ce bind réel (fenêtre de
            # course très étroite) — voir _probe_port_free pour pourquoi
            # SystemExit doit aussi être rattrapé ici (comportement propre
            # à werkzeug sur un échec de bind).
            _last_error = f'Port {port} indisponible ({exc if isinstance(exc, OSError) else "port déjà utilisé"}).'
            _api_logger.warning('Échec du démarrage : %s', _last_error)
            return False, _last_error
        thread.start()
        _server_thread = thread
        _last_error = ''
        _api_logger.info('Démarrage du serveur API (port %s).', port)
        return True, f'API démarrée sur le port {port}.'


def stop_media_api_server() -> tuple:
    global _server_thread
    with _server_lock:
        if _server_thread is None or not _server_thread.is_alive():
            _server_thread = None
            return True, 'API déjà inactive.'
        _server_thread.shutdown()
        _server_thread.join(timeout=5)
        _server_thread = None
        _api_logger.info('Arrêt du serveur API.')
        return True, 'API arrêtée.'


def media_api_status() -> dict:
    """Vue d'ensemble pour le back office (/admin/tags, bloc API) — voir
    app.py : _block_ctx_tags_media_api."""
    return {
        'running': media_api_is_running(),
        'autostart': media_api_autostart(),
        'port': media_api_port(),
        'login': _get_api_login(),
        'log_retention_days': media_api_log_retention_days(),
        'credentials_source': media_api_credentials_status()['source'],
        'last_error': media_api_last_error(),
    }
