import logging
import os
import secrets
from functools import wraps

from flask import abort, redirect, request, session, url_for

from config_loader import CONFIG
from db import get_setting, set_setting

logger = logging.getLogger('pictotem')


# ── Point 1 : lecture des mots de passe avec priorité aux variables d'env ────

def _get_main_password() -> str:
    return os.environ.get('PICTOTEM_MAIN_PASSWORD') or str(CONFIG.get('auth', {}).get('main_password', ''))


def _get_gallery_password() -> str:
    return os.environ.get('PICTOTEM_GALLERY_PASSWORD') or str(CONFIG.get('auth', {}).get('gallery_password', ''))


# Mot de passe admin (back office) : géré depuis la Tuile "Admin" du back
# office (voir app.py admin_access / admin_set_password), stocké en base via
# settings (clé 'admin.master_password'), PLUS géré dans config.toml. Ordre
# de priorité :
#   1. Variable d'env PICTOTEM_ADMIN_PASSWORD (recovery/déploiement) ;
#   2. Réglage en base 'admin.master_password', s'il a déjà été défini une
#      fois depuis la Tuile "Admin" (même vide = protection désactivée
#      volontairement — voir set_admin_password) ;
#   3. À défaut (aucun réglage en base pour l'instant, mise à jour depuis une
#      version antérieure), ancienne valeur config.toml [admin] password —
#      purement une compatibilité ascendante pour éviter un verrouillage
#      accidentel ; elle cesse d'être utilisée dès qu'un mot de passe est
#      défini une première fois depuis la Tuile "Admin".
_ADMIN_PASSWORD_SETTING_KEY = 'admin.master_password'
_ADMIN_PASSWORD_UNSET = object()


def _get_admin_password() -> str:
    env_password = os.environ.get('PICTOTEM_ADMIN_PASSWORD')
    if env_password:
        return env_password
    db_value = get_setting(_ADMIN_PASSWORD_SETTING_KEY, _ADMIN_PASSWORD_UNSET)
    if db_value is not _ADMIN_PASSWORD_UNSET:
        return db_value
    return str(CONFIG.get('admin', {}).get('password', ''))


def admin_password_status() -> dict:
    """Renseigne l'origine du mot de passe admin actuellement actif, pour
    affichage informatif sur la Tuile "Admin" (jamais le mot de passe en
    clair). 'source' vaut 'env' | 'db' | 'config_legacy' | 'unset'."""
    if os.environ.get('PICTOTEM_ADMIN_PASSWORD'):
        return {'set': True, 'source': 'env'}
    db_value = get_setting(_ADMIN_PASSWORD_SETTING_KEY, _ADMIN_PASSWORD_UNSET)
    if db_value is not _ADMIN_PASSWORD_UNSET:
        return {'set': bool(db_value), 'source': 'db'}
    legacy = str(CONFIG.get('admin', {}).get('password', '')).strip()
    return {'set': bool(legacy), 'source': 'config_legacy' if legacy else 'unset'}


def set_admin_password(new_password: str) -> None:
    """Définit (ou, avec une chaîne vide, supprime) le mot de passe admin en
    base — devient dès cet appel l'unique source de vérité pour
    _get_admin_password() ci-dessus, quel que soit le contenu de
    config.toml. La session en cours (déjà authentifiée pour arriver sur
    cette page) n'est pas invalidée par ce changement."""
    set_setting(_ADMIN_PASSWORD_SETTING_KEY, (new_password or '').strip())


# ── Point 2 : clé secrète robuste ────────────────────────────────────────────

def build_secret_key() -> str:
    """Retourne la secret_key configurée, ou génère une clé éphémère en avertissant."""
    key = CONFIG['server'].get('secret_key', '').strip()
    if not key or key in ('change-me', 'change-me-production-secret'):
        key = secrets.token_hex(32)
        logger.warning(
            'SECRET KEY non configurée ou par défaut — clé éphémère générée. '
            'Les sessions seront invalidées à chaque redémarrage. '
            'Définissez server.secret_key dans config.toml ou la variable PICTOTEM_SECRET_KEY.'
        )
    env_key = os.environ.get('PICTOTEM_SECRET_KEY', '').strip()
    return env_key if env_key else key


# ── Point 3 : protection CSRF ─────────────────────────────────────────────────

def generate_csrf_token() -> str:
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


def _validate_csrf():
    token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token', '')
    if not token or token != session.get('_csrf_token'):
        logger.warning('CSRF check échoué pour %s %s depuis %s', request.method, request.path, request.remote_addr)
        abort(403)


def csrf_protect(f):
    """Décorateur : valide le token CSRF sur toutes les requêtes POST."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'POST':
            _validate_csrf()
        return f(*args, **kwargs)
    return decorated


# ── Auth helpers ──────────────────────────────────────────────────────────────

def auth_enabled() -> bool:
    return bool(CONFIG.get('auth', {}).get('enabled', False))


def client_ip() -> str:
    if CONFIG.get('auth', {}).get('trust_proxy', False):
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return request.remote_addr or ''


def is_local_request() -> bool:
    local_addresses = set(CONFIG.get('auth', {}).get('local_addresses', ['127.0.0.1', '::1']))
    return client_ip() in local_addresses


def main_session_key() -> str:
    return CONFIG.get('auth', {}).get('main_session_name', 'pictotem_main_auth')


def gallery_session_key() -> str:
    return CONFIG.get('auth', {}).get('gallery_session_name', 'pictotem_gallery_auth')


def is_main_authenticated() -> bool:
    if not auth_enabled():
        return True
    if is_local_request():
        return True
    return bool(session.get(main_session_key(), False))


def is_gallery_authenticated() -> bool:
    # Accès distant (téléphones via QR) = libre ; accès local (écran kiosque) = mot de passe
    if not auth_enabled():
        return True
    if not is_local_request():
        return True
    return bool(session.get(gallery_session_key(), False))


def is_admin_authenticated() -> bool:
    if not _get_admin_password():
        return True
    return bool(session.get('pictotem_admin_auth', False))


# ── Décorateurs de route ──────────────────────────────────────────────────────

def require_main_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_main_authenticated():
            return f(*args, **kwargs)
        return redirect(url_for('login_main', next=request.full_path.rstrip('?')))
    return wrapped


def require_gallery_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_gallery_authenticated():
            return f(*args, **kwargs)
        return redirect(url_for('login_gallery', next=request.full_path.rstrip('?')))
    return wrapped


def require_admin_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_admin_authenticated():
            return f(*args, **kwargs)
        return redirect(url_for('login_admin', next=request.full_path.rstrip('?')))
    return wrapped


def require_media_auth(f):
    """Accès autorisé si l'auth principale (kiosque) OU galerie est satisfaite."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if is_main_authenticated() or is_gallery_authenticated():
            return f(*args, **kwargs)
        return redirect(url_for('login_gallery', next=request.full_path.rstrip('?')))
    return wrapped


# ── Helpers de login ──────────────────────────────────────────────────────────

def check_main_password(password: str) -> bool:
    expected = _get_main_password()
    return bool(expected and password == expected)


def check_gallery_password(password: str) -> bool:
    expected = _get_gallery_password()
    return bool(expected and password == expected)


def check_admin_password(password: str) -> bool:
    expected = _get_admin_password()
    return bool(expected and password == expected)
