import sys
from pathlib import Path

# La distribution Python portable (python-embed\, pilotee par un fichier
# ._pth) n'ajoute PAS automatiquement le dossier du script lance a sys.path,
# contrairement a un python.exe standard. Sans cette ligne, les modules
# voisins (auth, camera, config_loader, db, utils) ne sont pas trouves.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import csv
import io
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import unicodedata
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from html import unescape as html_unescape

import cv2
import numpy as np
import qrcode
import qrcode.constants
from flask import (Flask, Response, abort, jsonify, make_response, redirect,
                   render_template, request, send_file, send_from_directory,
                   session, url_for)
from PIL import Image, ImageDraw, ImageFont, ImageOps

from auth import (admin_password_status, auth_enabled, build_secret_key,
                  check_admin_password, check_gallery_password,
                  check_main_password, client_ip, csrf_protect,
                  gallery_session_key, generate_csrf_token,
                  is_admin_authenticated, is_gallery_authenticated,
                  is_local_request, is_main_authenticated, main_session_key,
                  require_admin_auth, require_gallery_auth, require_main_auth,
                  require_media_auth, set_admin_password)
from camera import (VIDEO_CAPTURE_ACTIVE, CAM_LOCK, clear_overlay_cache,
                    clear_recording_frame, composite_frame_overlay,
                    encode_jpeg, get_frame_overlay_path, get_latest_preview_frame,
                    get_overlay_bgra, publish_recording_frame, read_frame,
                    reset_camera, stream_generator)
from config_loader import (ALLOWED_OVERLAY_EXT, ALLOWED_PREVIEW_EXT, BASE_DIR,
                            CONFIG, EXPORTS_DIR, FFMPEG_EXE, FRAMES_DIR,
                            LOGS_DIR, PHOTO_DIR, RAW_PHOTO_DIR, RAW_VIDEO_DIR,
                            THUMBS_DIR, VIDEO_DIR)
from db import (db_conn, delete_capture, delete_email_by_id, delete_frame_db,
                export_emails_files, get_default_frame, get_frame_by_id_db,
                get_setting, init_db, list_captures, list_captures_in_range,
                list_emails, list_frames,
                record_capture, save_email, set_setting, update_email_by_id,
                upsert_frame,
                list_slideshow_images, add_slideshow_image, delete_slideshow_image_db,
                list_screensaver_images, add_screensaver_image, delete_screensaver_image_db,
                cast_vote, admin_adjust_vote, get_voter_votes,
                add_guest_upload, list_guest_uploads, list_approved_guest_uploads,
                count_guest_uploads_by_token, set_guest_upload_status, delete_guest_upload_db,
                list_guest_uploads_in_range,
                list_gallery_combined,
                list_tags, get_tag_by_id, create_tag, update_tag, delete_tag_db,
                list_capture_tags, count_capture_tags, add_capture_tag, delete_capture_tag,
                get_media_by_uid, get_tags_for_captures, list_distinct_tag_labels,
                list_capture_tags_with_media,
                list_wallpaper_images, add_wallpaper_image, delete_wallpaper_image_db,
                list_guest_codes, get_guest_code_by_id, get_guest_code_text,
                create_guest_code, update_guest_code_texte, regenerate_guest_code,
                delete_guest_code_db, upsert_guest_code, purge_guest_codes_by_date,
                purge_guest_codes_first_n, generate_guest_code,
                list_custom_fonts, create_custom_font, delete_custom_font_db,
                list_promo_backgrounds, add_promo_background, add_promo_gradient_background,
                delete_promo_background_db, list_promo_pages, get_promo_page, create_promo_page,
                update_promo_page, delete_promo_page_db, move_promo_page,
                list_promo_content_images, add_promo_content_image, delete_promo_content_image_db)
from utils import (build_gallery_url, current_stamp, disable_autostart,
                   enable_autostart, generate_qr_png, generate_qr_png_custom,
                   get_network_info, is_autostart_enabled, make_thumb, message_text,
                   print_photo, resolve_dynamic_placeholders, sanitize_promo_html,
                   set_windows_wallpaper, validate_printer)

# ── Application Flask ─────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / 'app' / 'templates'),
    static_folder=str(BASE_DIR / 'app' / 'static'),
)
app.secret_key = build_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

# Point 3 : jinja global pour les tokens CSRF
app.jinja_env.globals['csrf_token'] = generate_csrf_token

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(LOGS_DIR / 'app.log', encoding='utf-8'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger('pictotem')

# Création des répertoires manquants
for _p in [PHOTO_DIR, VIDEO_DIR, THUMBS_DIR, EXPORTS_DIR, LOGS_DIR]:
    _p.mkdir(parents=True, exist_ok=True)
if CONFIG['capture']['photo'].get('save_raw', False):
    RAW_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
if CONFIG['capture']['video'].get('save_raw', False):
    RAW_VIDEO_DIR.mkdir(parents=True, exist_ok=True)


# ── Textes UI ─────────────────────────────────────────────────────────────────
# Point 13 : valeurs calculées une fois au démarrage (CONFIG est immuable)

_UI = CONFIG.get('ui', {})
_BB = _UI.get('bottom_bar', {})

TEXT_DEFAULTS: dict[str, str] = {
    'app_title':          _UI.get('app_title', 'Pictotem'),
    'look_here':          _UI.get('look_here', {}).get('text', 'Regardez ici !'),
    'bottom_left_home':   _BB.get('left_message_home', 'Touchez un bouton pour lancer une capture'),
    'bottom_left_frame':  _BB.get('left_message_frame', 'Choisissez un cadre puis lancez la capture'),
    'bottom_left_replay': _BB.get('left_message_replay', 'Votre capture est prête'),
    'bottom_right':       _BB.get('right_message', 'Scannez pour retrouver vos photos et vidéos'),
    'bottom_right_sub':   '',
    'start_btn':          _UI.get('start_button', {}).get('text', "C'est parti"),
    'btn_photo':          'Prendre une photo',
    'btn_video':          'Prendre une vidéo',
    'btn_choose_frame':   'Choisir un cadre',
    'btn_retour':         'Retour',
    'btn_appliquer':      'Appliquer',
    'btn_imprimer':       'Imprimer',
    'btn_recommencer':    'Annuler et reprendre',
    'btn_valider_reprendre': 'Valider et reprendre',
    'btn_photo_strip':    'Photo strip',
    'btn_tags':           'Tags',
    'processing_title':   'Traitement',
    'processing_text':    'Veuillez patienter quelques instants.',
    'replay_badge':       'REPLAY',
}


def get_ui_texts() -> dict:
    return {k: (get_setting(f'text.{k}', '') or v) for k, v in TEXT_DEFAULTS.items()}


def get_top_bar_settings() -> dict:
    """Hauteur/couleur de la barre de progression tout en haut de l'écran,
    réglables depuis /admin/texts — voir #topCountdownBar dans index.html."""
    raw_h = get_setting('ui.top_bar_height_px', '')
    return {
        'height': int(raw_h) if raw_h.isdigit() else 6,
        'color':  get_setting('ui.top_bar_color', '') or '#f2c94c',
    }


def _about_settings() -> dict:
    """Cartouche 'version' en haut à droite du kiosque : libellé affiché +
    texte libre affiché dans la fenêtre au clic, réglables depuis
    /admin/texts — voir #versionBadge/#versionModal dans index.html."""
    return {
        'version_label': get_setting('about.version_label', '') or 'v1.0',
        'info_text':     get_setting('about.info_text', ''),
    }


def _kiosk_unlock_settings() -> dict:
    """Code PIN + nombre d'appuis déclencheurs pour la sortie de plein écran
    depuis l'interface principale (zone tactile haut-gauche), réglables
    depuis /admin/system — voir _KioskAPI.toggle_fullscreen ci-dessous et
    #kioskUnlockModal / setupKioskUnlock() dans index.html/app.js. Le PIN
    n'est jamais transmis au client : seul le nombre d'appuis (`taps`) est
    injecté dans window.PICTOTEM, la vérification du code se fait ici,
    côté serveur, via le pont pywebview."""
    raw_taps = get_setting('kiosk.unlock_taps', '')
    return {
        'pin':  get_setting('kiosk.unlock_pin', '') or '1234',
        'taps': int(raw_taps) if raw_taps.isdigit() and 2 <= int(raw_taps) <= 15 else 5,
    }


def _kiosk_network_info_settings() -> dict:
    """Nombre d'appuis rapides (zone dédiée, coin bas-gauche de l'interface
    principale) affichant l'IP + le port en écoute — réglable depuis
    /admin/application, SANS mot de passe (contrairement au déverrouillage
    plein écran ci-dessus) : purement informatif, aucune action sensible
    n'est accessible depuis cette fenêtre. Voir get_network_info() (utils.py)
    et setupNetworkInfoTaps() dans static/app.js."""
    raw_taps = get_setting('kiosk.network_info_taps', '')
    return {
        'taps': int(raw_taps) if raw_taps.isdigit() and 2 <= int(raw_taps) <= 15 else 7,
    }


def get_bottom_bar_sizes() -> dict:
    """Tailles (px) réglables depuis /admin/texts pour le texte de droite et le
    QR code de la barre du bas — surchargent right_message_font_size_px /
    qr_size_px de config.toml. Injectées en variables CSS directement dans
    index.html (voir index()), pas de dépendance à un bridge JS."""
    def _size(key, default):
        raw = get_setting(f'ui.{key}', '')
        return int(raw) if raw.isdigit() else int(_BB.get(key, default))
    return {
        'right_font_size': _size('right_message_font_size_px', 16),
        'qr_size':          _size('qr_size_px', 96),
    }


# ── Boutons d'action (kiosque) ────────────────────────────────────────────────
# Réglages depuis /admin/buttons : un socle commun (forme, police, taille de
# police, padding) appliqué à tous les boutons "action-btn", et une couleur de
# fond + graisse de texte propres à chaque rôle visuel (pri/sec/ter, plus
# retake/tags qui utilisaient auparavant un style "ghost" non aligné sur les
# autres — c'est ce décalage qui motive cette page de réglages).

_BUTTON_SHAPES = {'pill': 999, 'rounded': 16, 'square': 6}
_BUTTON_ROLES = [
    ('pri',     'Bouton principal (ex. Prendre une photo, Appliquer)', '#16f062'),
    ('sec',     'Bouton secondaire (ex. Prendre une vidéo, Imprimer)',  '#32c1f9'),
    ('ter',     'Bouton tertiaire (ex. Photo strip, Retour)',           '#ffd726'),
    ('retake',  'Annuler et reprendre',                                 '#6c7a89'),
    ('valider', 'Valider et reprendre',                                 '#f2994a'),
    ('tags',    'Tags',                                                 '#9b59b6'),
]


def _readable_text_color(hex_color: str) -> str:
    """Choisit un texte clair ou sombre selon la luminance du fond, pour que
    la couleur de fond restе librement personnalisable sans jamais produire
    un texte illisible (l'admin ne règle que la couleur de fond, pas celle
    du texte — voir /admin/buttons)."""
    h = (hex_color or '').lstrip('#')
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return '#111111'
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return '#111111' if luminance > 0.55 else '#ffffff'


def _buttons_settings() -> dict:
    shape = get_setting('buttons.shape', 'pill')
    if shape not in _BUTTON_SHAPES:
        shape = 'pill'
    roles = {}
    for key, label, default_bg in _BUTTON_ROLES:
        bg = get_setting(f'buttons.{key}_bg', '') or default_bg
        bold = get_setting(f'buttons.{key}_bold', '1') == '1'
        roles[key] = {'label': label, 'bg': bg, 'bold': bold, 'text': _readable_text_color(bg)}
    return {
        'shape':      shape,
        'radius_px':  _BUTTON_SHAPES[shape],
        'font':       get_setting('buttons.font', '') or _PROMO_FONTS[0][0],
        'font_size':  int(get_setting('buttons.font_size', '') or '38'),
        'padding_y':  int(get_setting('buttons.padding_y', '') or '14'),
        'padding_x':  int(get_setting('buttons.padding_x', '') or '20'),
        'roles':      roles,
    }


# ── Helpers admin ─────────────────────────────────────────────────────────────

def _admin_redirect(success=None, error=None):
    params = {}
    if success:
        params['ok'] = success
    if error:
        params['err'] = error
    return redirect(url_for('admin_frames', **params))


def _admin_emails_redirect(success=None, error=None, sort='desc', search=''):
    params = {'sort': sort}
    if search:
        params['q'] = search
    if success:
        params['ok'] = success
    if error:
        params['err'] = error
    return redirect(url_for('admin_emails', **params))


def _save_frame_file(file_storage, frame_id: str, kind: str):
    if not file_storage or not file_storage.filename:
        return None
    ext = Path(file_storage.filename).suffix.lower()
    if kind == 'overlay' and ext not in ALLOWED_OVERLAY_EXT:
        raise ValueError(f"L'overlay doit être un PNG (reçu : {ext})")
    if kind == 'preview' and ext not in ALLOWED_PREVIEW_EXT:
        raise ValueError(f"La preview doit être une image jpg/png/webp (reçu : {ext})")
    safe_id = re.sub(r'[^a-z0-9_-]', '', frame_id.lower())
    filename = f'{safe_id}-{kind}{ext}'
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    file_storage.save(str(FRAMES_DIR / filename))
    return filename


# ── Filtre Jinja ──────────────────────────────────────────────────────────────

@app.template_filter('format_dt')
def format_dt(value):
    try:
        return datetime.fromisoformat(str(value)).strftime('%d/%m/%Y %H:%M')
    except Exception:
        return str(value)


# ── Routes auth ───────────────────────────────────────────────────────────────

@app.route('/login/admin', methods=['GET', 'POST'])
@csrf_protect
def login_admin():
    if is_admin_authenticated():
        return redirect(url_for('admin_home'))
    error = None
    next_url = request.values.get('next') or '/admin/frames'
    if request.method == 'POST':
        password = (request.form.get('password') or '').strip()
        if check_admin_password(password):
            session['pictotem_admin_auth'] = True
            return redirect(next_url)
        error = 'Mot de passe incorrect'
    return render_template('login.html', config=CONFIG, error=error, next_url=next_url,
                           title='Accès administration', subtitle='Gestion des cadres décoratifs.')


@app.route('/login/main', methods=['GET', 'POST'])
@csrf_protect
def login_main():
    if not auth_enabled() or is_local_request():
        return redirect(url_for('index'))
    error = None
    next_url = request.values.get('next') or '/'
    if request.method == 'POST':
        password = (request.form.get('password') or '').strip()
        if check_main_password(password):
            session[main_session_key()] = True
            return redirect(next_url)
        error = 'Mot de passe incorrect'
    return render_template('login.html', config=CONFIG, error=error, next_url=next_url,
                           title='Accès interface principale', subtitle='Accès distant sécurisé au pictotem.')


@app.route('/login/gallery', methods=['GET', 'POST'])
@csrf_protect
def login_gallery():
    if not auth_enabled() or not is_local_request():
        return redirect(url_for('gallery'))
    error = None
    next_url = request.values.get('next') or '/gallery'
    if request.method == 'POST':
        password = (request.form.get('password') or '').strip()
        if check_gallery_password(password):
            session[gallery_session_key()] = True
            return redirect(next_url)
        error = 'Mot de passe incorrect'
    return render_template('login.html', config=CONFIG, error=error, next_url=next_url,
                           title='Accès galerie locale', subtitle='La galerie est protégée uniquement sur la borne.')


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop(main_session_key(), None)
    session.pop(gallery_session_key(), None)
    session.pop('pictotem_admin_auth', None)
    return redirect(url_for('index'))


# ── Routes principales ────────────────────────────────────────────────────────

@app.route('/')
@require_main_auth
def index():
    welcome_fn = get_setting('welcome_frame_filename', '')
    welcome_frame_url = f'/static/frames/{welcome_fn}' if welcome_fn else ''
    # {ip}/{port} (voir resolve_dynamic_placeholders, utils.py) résolus ici,
    # sur la route PUBLIQUE du kiosque uniquement — get_ui_texts()/
    # _about_settings() sont aussi utilisées telles quelles (non résolues)
    # pour repeupler le formulaire /admin/texts, qui doit continuer à
    # afficher {ip}/{port} en clair pour rester éditable.
    texts = {k: resolve_dynamic_placeholders(v) for k, v in get_ui_texts().items()}
    about = {k: resolve_dynamic_placeholders(v) for k, v in _about_settings().items()}
    return render_template(
        'index.html', config=CONFIG, frames=list_frames(),
        default_frame=get_default_frame(), message=resolve_dynamic_placeholders(message_text()),
        welcome_frame_url=welcome_frame_url, texts=texts,
        idle_timer_enabled=get_setting('idle_timer_enabled', '0') == '1',
        idle_timer_seconds=int(get_setting('idle_timer_seconds', '30')),
        idle_timer_badge_text=resolve_dynamic_placeholders(
            get_setting('idle_timer_badge_text', 'Retour dans {n}s')),
        idle_timer_font_size=int(get_setting('idle_timer_font_size', '13')),
        idle_timer_padding_y=int(get_setting('idle_timer_padding_y', '5')),
        idle_timer_padding_x=int(get_setting('idle_timer_padding_x', '13')),
        screensaver_enabled=get_setting('ui.screensaver_enabled', '0') == '1',
        screensaver_timeout_seconds=(
            int(get_setting('ui.screensaver_timeout_min', '') or '3') * 60
        ),
        hide_print_button=get_setting('ui.hide_print_button', '0') == '1',
        bottom_bar_sizes=get_bottom_bar_sizes(),
        top_bar=get_top_bar_settings(),
        photo_strip=CONFIG.get('capture', {}).get('photo_strip', {}),
        tags_enabled=get_setting('tags.enabled', '0') == '1',
        buttons=_buttons_settings(),
        about=about,
        kiosk_unlock_taps=_kiosk_unlock_settings()['taps'],
        photostrip_step=_photostrip_step_settings(),
        qrcode=_qrcode_settings(),
        qrcode_live_style=_qr_live_style_settings(),
        qrcode_live_error_style=_qr_live_error_style_settings(),
        network_info=get_network_info(),
        kiosk_network_info_taps=_kiosk_network_info_settings()['taps'],
    )


@app.route('/api/frames')
@require_main_auth
def api_frames():
    return jsonify({'frames': list_frames(), 'default_frame': get_default_frame()})


@app.route('/healthz')
@require_main_auth
def healthz():
    try:
        from camera import get_camera
        get_camera()
        return jsonify({'ok': True, 'camera': True})
    except Exception as exc:
        logger.exception('healthz échoué')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/stream.mjpg')
@require_main_auth
def stream_mjpg():
    return Response(stream_generator(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ── Routes capture ────────────────────────────────────────────────────────────

@app.route('/api/capture/photo', methods=['POST'])
@require_main_auth
def capture_photo():
    if not CONFIG['capture']['photo'].get('enabled', True):
        return jsonify({'ok': False, 'error': 'Capture photo désactivée'}), 403
    req = request.get_json(silent=True) or {}
    frame_id = req.get('frame', 'none')
    raw_frame = read_frame()
    stamp = current_stamp()
    filename = f'photo-{stamp}.jpg'

    overlay_path = get_frame_overlay_path(frame_id)
    has_overlay = bool(overlay_path and overlay_path.exists())

    if has_overlay and CONFIG['capture']['photo'].get('save_raw', False):
        RAW_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_PHOTO_DIR / filename).write_bytes(encode_jpeg(raw_frame))
        logger.info('Photo brute sauvegardée : %s', filename)

    display_frame = raw_frame
    if has_overlay:
        h, w = raw_frame.shape[:2]
        overlay = get_overlay_bgra(overlay_path, w, h)
        if overlay is not None:
            display_frame = composite_frame_overlay(raw_frame, overlay)

    detections = _qr_detect_for_capture(display_frame, 'la photo')
    if detections and _qr_burn_settings()['photo']:
        burn_layer = _render_qr_burn_layer(display_frame, detections)
        if burn_layer is not None:
            display_frame = _apply_qr_burn_layer_bgr(display_frame, burn_layer)

    filepath = PHOTO_DIR / filename
    filepath.write_bytes(encode_jpeg(display_frame))
    thumb_name = f'thumb-{stamp}.jpg'
    make_thumb(filepath, THUMBS_DIR / thumb_name)
    capture_id, media_uid = record_capture('photo', filename, thumb_name)
    logger.info('Photo capturée %s (cadre=%s)', filename, frame_id)
    qr_tags = _qr_tag_detections(capture_id, detections)
    return jsonify({'ok': True, 'id': capture_id, 'media_uid': media_uid, 'kind': 'photo',
                    'filename': filename, 'qr_tags': qr_tags,
                    'url': f'/media/photo/{filename}', 'message': resolve_dynamic_placeholders(message_text())})


def _ffmpeg_overlay_pass(input_path: Path, overlay_png_path: Path, output_path: Path, w: int, h: int):
    """Une passe ffmpeg 'overlay' (image STATIQUE composée sur toutes les
    frames d'une vidéo) — utilisée pour le cadre décoratif dans
    capture_video (toujours statique sur toute la durée). L'incrustation
    QR-code, elle, doit suivre le mouvement du QR-code : voir
    _qr_video_burn_track, qui grave une position différente par frame plutôt
    que de passer par cette fonction. Retourne (ok: bool, message_erreur)."""
    try:
        proc = subprocess.run([
            FFMPEG_EXE, '-y', '-i', str(input_path), '-i', str(overlay_png_path),
            '-filter_complex', f'[1:v]scale={w}:{h}[ov];[0:v][ov]overlay=0:0',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', str(output_path),
        ], capture_output=True, text=True)
    except FileNotFoundError:
        logger.error('ffmpeg introuvable (%s) — voir logs\\launcher.log (setup_ffmpeg.ps1).', FFMPEG_EXE)
        return False, "ffmpeg introuvable sur cette machine (nécessaire pour les vidéos)."
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        logger.error('ffmpeg overlay échoué : %s', proc.stderr)
        return False, 'Application du cadre vidéo échouée'
    return True, ''


def _ffmpeg_transcode_to_mp4(input_avi_path: Path, output_path: Path, fps: float):
    """Transcodage AVI (MJPG, produit par cv2.VideoWriter) -> MP4 (H.264) —
    factorise le motif utilisé à la fois pour le transcodage initial de la
    capture et pour la vidéo réincrustée image par image par
    _qr_video_burn_track. Retourne (ok: bool, message_erreur)."""
    try:
        proc = subprocess.run([
            FFMPEG_EXE, '-y', '-r', f'{fps:.3f}', '-i', str(input_avi_path),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', str(output_path),
        ], capture_output=True, text=True)
    except FileNotFoundError:
        logger.error('ffmpeg introuvable (%s) — voir logs\\launcher.log (setup_ffmpeg.ps1).', FFMPEG_EXE)
        return False, "ffmpeg introuvable sur cette machine (nécessaire pour les vidéos)."
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        logger.error('ffmpeg transcode échoué : %s', proc.stderr)
        return False, 'Transcodage vidéo échoué'
    return True, ''


@app.route('/api/capture/video', methods=['POST'])
@require_main_auth
def capture_video():
    if not CONFIG['capture']['video'].get('enabled', True):
        return jsonify({'ok': False, 'error': 'Capture vidéo désactivée'}), 403
    req = request.get_json(silent=True) or {}
    frame_id = req.get('frame', 'none')
    default_duration = int(CONFIG['capture']['video'].get('default_duration_sec', 5))
    max_duration = int(CONFIG['capture']['video'].get('max_duration_sec', 20))
    duration = max(1, min(int(req.get('duration', default_duration)), max_duration))
    stamp = current_stamp()
    final_filename = f'video-{stamp}.mp4'
    final_path = VIDEO_DIR / final_filename
    thumb_name = f'thumb-{stamp}.jpg'
    thumb_path = THUMBS_DIR / thumb_name
    avi_path = VIDEO_DIR / f'video-{stamp}.avi'
    raw_mp4_path = VIDEO_DIR / f'video-{stamp}-noframe.mp4'

    first = read_frame()
    if first is None:
        return jsonify({'ok': False, 'error': 'Caméra indisponible'}), 500
    h, w = first.shape[:2]
    configured_fps = float(CONFIG['camera'].get('fps', 20))

    overlay_path = get_frame_overlay_path(frame_id)
    has_overlay = bool(overlay_path and overlay_path.exists())

    thumb_frame = first
    if has_overlay:
        ov = get_overlay_bgra(overlay_path, w, h)
        if ov is not None:
            thumb_frame = composite_frame_overlay(first, ov)

    cv2.imwrite(str(thumb_path), thumb_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])

    writer = cv2.VideoWriter(str(avi_path), cv2.VideoWriter_fourcc(*'MJPG'), configured_fps, (w, h))
    if not writer.isOpened():
        logger.error('VideoWriter impossible à ouvrir pour %s', avi_path)
        return jsonify({'ok': False, 'error': 'Initialisation VideoWriter échouée'}), 500

    VIDEO_CAPTURE_ACTIVE.set()
    publish_recording_frame(first)  # évite un aperçu figé le temps de la 1ère itération
    try:
        writer.write(first)
        frames_written = 1
        start = time.time()
        consecutive_errors = 0
        while True:
            if time.time() - start >= duration:
                break
            try:
                frame = read_frame()
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.warning('Erreur lecture frame pendant enregistrement (%d)', consecutive_errors)
                if consecutive_errors >= 10:
                    logger.error('Trop d\'erreurs consécutives, enregistrement interrompu')
                    break
                time.sleep(0.05)
                continue
            if frame is None:
                continue
            # Publiée pour l'aperçu live avant le resize (qui ne sert qu'à
            # matcher les dimensions du VideoWriter, pas à l'affichage).
            publish_recording_frame(frame)
            if frame.shape[1] != w or frame.shape[0] != h:
                frame = cv2.resize(frame, (w, h))
            writer.write(frame)
            frames_written += 1
    finally:
        writer.release()
        VIDEO_CAPTURE_ACTIVE.clear()
        clear_recording_frame()

    actual_duration = max(time.time() - start, 0.001)
    effective_fps = max(frames_written / actual_duration, 1.0)
    logger.info('Stats vidéo : demandé=%ss réel=%.3fs frames=%s fps=%.2f cadre=%s',
                duration, actual_duration, frames_written, effective_fps, frame_id)

    # Transcodage AVI → MP4 (Point 4 : args en liste, pas de shell=True)
    ok, err = _ffmpeg_transcode_to_mp4(avi_path, raw_mp4_path, effective_fps)
    avi_path.unlink(missing_ok=True)
    if not ok:
        return jsonify({'ok': False, 'error': err}), 500

    qr_burn_enabled = _qr_burn_settings()['video']
    if (has_overlay or qr_burn_enabled) and CONFIG['capture']['video'].get('save_raw', False):
        RAW_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(raw_mp4_path), str(RAW_VIDEO_DIR / final_filename))
        logger.info('Vidéo brute sauvegardée : %s', final_filename)

    # Cadre décoratif : passe ffmpeg 'overlay' statique (inchangé). Écrit
    # directement dans final_path SAUF si l'incrustation QR-code (suivie,
    # voir plus bas) doit encore s'appliquer par-dessus, auquel cas elle
    # écrit dans un fichier intermédiaire que cette dernière consommera.
    path_for_qr = raw_mp4_path
    if has_overlay:
        step_output = final_path if not qr_burn_enabled else VIDEO_DIR / f'video-{stamp}-frame.mp4'
        ok, err = _ffmpeg_overlay_pass(raw_mp4_path, overlay_path, step_output, w, h)
        raw_mp4_path.unlink(missing_ok=True)
        if not ok:
            return jsonify({'ok': False, 'error': err}), 500
        path_for_qr = step_output
    elif not qr_burn_enabled:
        raw_mp4_path.rename(final_path)
        path_for_qr = final_path

    # Incrustation QR-code : suivi du mouvement (voir le commentaire au-dessus
    # de _qr_video_detect_keyframes) — 2 passes après l'enregistrement, sans
    # impact sur la fluidité/cadence de la capture elle-même. No-op (fichier
    # simplement renommé) si aucun QR-code n'a jamais été décodé dans la
    # vidéo — cas le plus fréquent quand l'option est activée « au cas où ».
    # `keyframes` sert aussi de base au tag automatique ci-dessous (aucune
    # détection supplémentaire nécessaire, uniquement quand l'incrustation
    # vidéo est active — sinon aucune frame de la vidéo n'est jamais
    # analysée, contrairement à la photo/au photo strip qui scannent
    # systématiquement dès que l'add-on est activé).
    keyframes = []
    if qr_burn_enabled:
        keyframes, frame_count = _qr_video_detect_keyframes(path_for_qr, _QR_VIDEO_TRACK_SAMPLE_INTERVAL)
        if not keyframes:
            if path_for_qr != final_path:
                path_for_qr.rename(final_path)
        else:
            qr_track = _qr_video_build_track(frame_count, keyframes, _QR_VIDEO_TRACK_SAMPLE_INTERVAL)
            tracked_avi_path = VIDEO_DIR / f'video-{stamp}-qrtrack.avi'
            if not _qr_video_burn_track(path_for_qr, qr_track, w, h, effective_fps, tracked_avi_path):
                path_for_qr.unlink(missing_ok=True)
                tracked_avi_path.unlink(missing_ok=True)
                return jsonify({'ok': False, 'error': 'Incrustation QR-code (vidéo) échouée'}), 500
            path_for_qr.unlink(missing_ok=True)
            ok, err = _ffmpeg_transcode_to_mp4(tracked_avi_path, final_path, effective_fps)
            tracked_avi_path.unlink(missing_ok=True)
            if not ok:
                return jsonify({'ok': False, 'error': err}), 500

            # Miniature : si un QR-code est suivi dès la 1ère frame, la
            # graver aussi sur la miniature pour qu'elle reflète ce que
            # montre réellement le début de la vidéo finale.
            if qr_track and qr_track[0] is not None:
                box0, text0 = qr_track[0]
                synth_pts0 = [(box0[0], box0[1]), (box0[2], box0[1]), (box0[2], box0[3]), (box0[0], box0[3])]
                try:
                    layer0 = _render_qr_burn_layer(thumb_frame, [{'text': text0, 'points': synth_pts0}])
                    if layer0 is not None:
                        thumb_burned = _apply_qr_burn_layer_bgr(thumb_frame, layer0)
                        cv2.imwrite(str(thumb_path), thumb_burned, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                except Exception:
                    logger.exception('QR live : échec incrustation sur la miniature vidéo.')

    capture_id, media_uid = record_capture('video', final_filename, thumb_name)
    logger.info('Vidéo capturée %s', final_filename)
    # Tag automatique (comme la photo/le photo strip, voir _qr_tag_detections)
    # à partir des textes décodés pendant le suivi ci-dessus — dédoublonnés
    # (un même QR-code tenu tout du long donne un point-clé identique toutes
    # les _QR_VIDEO_TRACK_SAMPLE_INTERVAL frames, un seul tag par texte).
    qr_tags = []
    if keyframes:
        seen_texts = []
        for _idx, _box, text in keyframes:
            if text not in seen_texts:
                seen_texts.append(text)
        qr_tags = _qr_tag_detections(capture_id, [{'text': t} for t in seen_texts])
    return jsonify({'ok': True, 'id': capture_id, 'media_uid': media_uid, 'kind': 'video',
                    'filename': final_filename, 'qr_tags': qr_tags,
                    'url': f'/media/video/{final_filename}', 'message': resolve_dynamic_placeholders(message_text())})


@app.route('/api/capture/<int:capture_id>/retake', methods=['POST'])
@require_main_auth
def capture_retake(capture_id):
    """Supprime la capture tout juste prise si le visiteur préfère
    recommencer avant impression — évite d'accumuler des essais ratés dans
    la galerie. Déclenchable depuis la borne sans mot de passe admin, comme
    les routes de capture elles-mêmes (même niveau de confiance implicite).
    Pas de @csrf_protect, cohérent avec /api/capture/photo et .../video."""
    cap = delete_capture(capture_id)
    if not cap:
        return jsonify({'ok': False, 'error': 'Capture introuvable'}), 404
    media_dir = PHOTO_DIR if cap['kind'] == 'photo' else VIDEO_DIR
    try:
        (media_dir / cap['filename']).unlink(missing_ok=True)
    except Exception:
        logger.warning('Recommencer : suppression fichier échouée : %s', cap['filename'])
    if cap.get('thumb_filename'):
        try:
            (THUMBS_DIR / cap['thumb_filename']).unlink(missing_ok=True)
        except Exception:
            pass
    logger.info('Capture #%d supprimée (recommencer)', capture_id)
    return jsonify({'ok': True})


# ── Tags sur médias ───────────────────────────────────────────────────────────
# Activable depuis /admin/tags. Tags prédéfinis (CRUD admin) + tag "libre"
# saisi par l'invité via clavier virtuel côté kiosque (voir static/app.js,
# bouton "Tags" sur l'écran replay). Portée aux captures officielles
# uniquement (pas aux uploads invités).

def _tags_settings():
    return {
        'enabled':           get_setting('tags.enabled', '0') == '1',
        'free_enabled':      get_setting('tags.free_enabled', '1') == '1',
        'free_min_length':   int(get_setting('tags.free_min_length', '') or '2'),
        'free_max_length':   int(get_setting('tags.free_max_length', '') or '24'),
        'max_per_capture':   int(get_setting('tags.max_per_capture', '') or '5'),
        'show_on_bestof':    get_setting('tags.show_on_bestof', '0') == '1',
        'style_font':        get_setting('tags.style_font', '') or _PROMO_FONTS[0][0],
        'style_bg_color':    get_setting('tags.style_bg_color', '') or '#0d8b8f',
        'style_text_color':  get_setting('tags.style_text_color', '') or '#ffffff',
        'style_font_size':   int(get_setting('tags.style_font_size', '') or '14'),
    }


def _media_id_settings():
    return {
        'length':         int(get_setting('media_id.length', '') or '6'),
        'show_on_bestof': get_setting('media_id.show_on_bestof', '0') == '1',
    }


# ── Add-on : détection de QR-codes → tags automatiques ────────────────────────
# Activable depuis /admin/guest_codes (section dédiée). À chaque photo (capture
# simple ou photo strip), si activé, on scanne l'image finale (avec cadre
# déjà appliqué) à la recherche de QR-codes. Chaque QR-code décodé devient
# un tag libre sur la capture, avec les mêmes bornes que les tags libres
# saisis à la main (tags.free_min_length/free_max_length/max_per_capture,
# voir _tags_settings()) pour rester cohérent avec le reste de la
# fonctionnalité tags. Ne s'applique pas aux vidéos (détection sur une
# image fixe uniquement). N'affecte jamais le succès de la capture : toute
# erreur de détection est journalisée et ignorée. Utilise le même pipeline
# de détection robuste (_qr_detect_boxes_robust, voir plus bas) que
# l'aperçu en direct (/api/qr/live) : un QR-code repéré et lisible avant la
# capture (grâce au détecteur ArUco + tentatives d'agrandissement) doit
# aussi l'être une fois la photo prise, sous peine d'un tag manquant alors
# que le visiteur a vu le contenu s'afficher en direct.

# Détecteur basé sur les marqueurs ArUco, sensiblement plus sensible que le
# détecteur QR classique pour la simple DÉTECTION (pas forcément le
# décodage) de QR-codes de petite taille. Mesuré empiriquement : détecte la
# présence d'un QR-code jusqu'à environ 5% de la largeur de l'image (contre
# ~15% pour le détecteur classique), et jusqu'à ~3-4% avec la tentative sur
# image agrandie ci-dessous — mais ne parvient à le DÉCODER de façon fiable
# qu'au-delà, d'où les tentatives de rattrapage supplémentaires
# (_qr_retry_decode_upscaled, repêchage image entière).
_qr_detector_aruco = cv2.QRCodeDetectorAruco()
# Facteur d'agrandissement numérique de l'image ENTIÈRE, tenté uniquement si
# la détection échoue complètement sur l'image brute (voir api_qr_live) —
# repêche les QR-codes juste sous le seuil de détection du détecteur ArUco.
# Mesuré empiriquement : un facteur 2 retrouve des QR-codes synthétiques
# jusqu'à ~3-4% de la largeur de l'image (contre ~5% sans cette étape).
# Coût mesuré ~70ms sur une image 1280x720 (contre ~40ms pour la passe
# directe) : acceptable pour un polling à 600ms même quand aucun QR-code
# n'est réellement présent dans le champ (cas le plus fréquent).
_QR_LIVE_FULLFRAME_UPSCALE = 2.0


def _qrcode_settings() -> dict:
    return {
        'enabled': get_setting('qrcode.enabled', '0') == '1',
        'live_overlay': get_setting('qrcode.live_overlay', '0') == '1',
    }


def _qr_burn_settings() -> dict:
    """Incrustation (dure, dans le fichier final) de la forme + du texte
    QR-code live sur les médias capturés — option indépendante par type de
    média (photo/vidéo/photo strip), réglable depuis /admin/guest_codes.
    Nécessite « Activer la détection automatique de QR-codes »
    (qrcode.enabled) ci-dessus, comme le tag automatique. Ne concerne jamais
    le message d'erreur (qrcode.live_error_style.*) : voir
    _render_qr_burn_layer."""
    photo = get_setting('qrcode.burn_into_media.photo', '0') == '1'
    video = get_setting('qrcode.burn_into_media.video', '0') == '1'
    strip = get_setting('qrcode.burn_into_media.strip', '0') == '1'
    return {'photo': photo, 'video': video, 'strip': strip, 'any': photo or video or strip}


# ── Apparence du texte QR-code affiché en direct (réglable depuis /admin/guest_codes) ─
QR_LIVE_DIR = BASE_DIR / 'app' / 'static' / 'qr_live'
_QR_LIVE_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp'}
_QR_LIVE_SHAPES = [
    ('pill', 'Pilule (arrondi complet)'),
    ('rounded', 'Coins arrondis'),
    ('square', 'Rectangle (angles droits)'),
    ('circle', 'Cercle'),
    ('oval', 'Ovale'),
    ('star', 'Étoile'),
]
# Formes rectangulaires (pill/rounded/square) : simple border-radius, marge
# standard. Formes non rectangulaires (circle/oval/star) : nécessitent un
# clip-path en plus, ET une marge interne plus généreuse (en em, donc
# proportionnelle à la taille de police choisie) pour que le texte reste
# lisible à l'intérieur du contour plutôt que rogné par ses bords/pointes.
# L'étoile en particulier ne convient bien qu'à un texte court (ses pointes
# rognent tout ce qui déborde de son corps central) — signalé à l'admin
# dans l'aide contextuelle du formulaire.
_QR_LIVE_SHAPE_CSS = {
    'pill':    {'radius': '999px', 'clip': 'none', 'padding': '7px 14px'},
    'rounded': {'radius': '14px',  'clip': 'none', 'padding': '7px 14px'},
    'square':  {'radius': '0px',   'clip': 'none', 'padding': '7px 14px'},
    'circle':  {'radius': '50%',   'clip': 'none', 'padding': '1.5em 1.7em'},
    'oval':    {'radius': '50%',   'clip': 'none', 'padding': '0.9em 2.4em'},
    'star':    {
        'radius': '0px',
        'clip': ('polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, '
                 '50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%)'),
        'padding': '1.9em 2.1em',
    },
}
_QR_LIVE_POSITIONS = [
    ('above', 'Au-dessus du QR-code'),
    ('below', 'En dessous du QR-code'),
    ('left', 'À gauche du QR-code'),
    ('right', 'À droite du QR-code'),
    ('center', 'Superposé (centré sur le QR-code)'),
]


def _scale_padding_css(padding_css: str, scale: float) -> str:
    """Multiplie chaque valeur numérique d'une déclaration CSS padding
    ('7px 14px', '1.5em 1.7em', ...) par `scale`, en conservant l'unité
    (px ou em) de chaque valeur. Utilisé pour la taille réglable de la
    forme d'arrière-plan (bg_size_pct) — la police reste inchangée, seule
    la marge interne (donc la taille de la forme) est mise à l'échelle."""
    parts = []
    for token in padding_css.split():
        m = re.match(r'^([\d.]+)(px|em)$', token)
        if not m:
            parts.append(token)
            continue
        value, unit = float(m.group(1)), m.group(2)
        parts.append(f'{value * scale:.3f}{unit}')
    return ' '.join(parts) if parts else padding_css


def _add_padding_margin_css(padding_css: str, extra_px: int) -> str:
    """Ajoute `extra_px` (marge supplémentaire réglable, voir text_margin_px
    dans _qr_live_style_settings) à chaque valeur numérique d'un padding CSS
    déjà mis à l'échelle par _scale_padding_css ci-dessus — via calc(), qui
    accepte nativement l'addition d'unités différentes (px + em), donc sans
    avoir à convertir les valeurs de base (certaines formes utilisent em)."""
    if not extra_px:
        return padding_css
    parts = []
    for token in padding_css.split():
        m = re.match(r'^([\d.]+)(px|em)$', token)
        if not m:
            parts.append(token)
            continue
        value, unit = m.group(1), m.group(2)
        parts.append(f'calc({value}{unit} + {extra_px}px)')
    return ' '.join(parts) if parts else padding_css


def _qr_live_bg_dim_px(raw: str) -> int:
    """Parse une dimension (largeur/hauteur) de la forme d'arrière-plan en
    px. Chaîne vide/invalide → 0, ce qui signifie « auto » (comportement
    d'origine : la forme s'ajuste au texte via bg_size_pct/padding)."""
    raw = (raw or '').strip()
    if not raw:
        return 0
    try:
        return max(0, min(800, int(raw)))
    except ValueError:
        return 0


def _qr_live_style_settings() -> dict:
    bg_mode = get_setting('qrcode.live_style.bg_mode', '') or 'shape'
    if bg_mode not in ('shape', 'image'):
        bg_mode = 'shape'
    bg_shape = get_setting('qrcode.live_style.bg_shape', '') or 'pill'
    if bg_shape not in _QR_LIVE_SHAPE_CSS:
        bg_shape = 'pill'
    shape_css = _QR_LIVE_SHAPE_CSS[bg_shape]
    raw_size_pct = get_setting('qrcode.live_style.bg_size_pct', '') or '100'
    try:
        bg_size_pct = max(50, min(300, int(raw_size_pct)))
    except ValueError:
        bg_size_pct = 100
    # Largeur/hauteur fixes (px) de la forme, en plus de la mise à l'échelle
    # par pourcentage ci-dessus. 0 = auto (la forme s'ajuste au texte).
    bg_width_px = _qr_live_bg_dim_px(get_setting('qrcode.live_style.bg_width_px', ''))
    bg_height_px = _qr_live_bg_dim_px(get_setting('qrcode.live_style.bg_height_px', ''))
    # Proportionnalité à la taille du QR-code détecté : quand actif, la
    # taille de la forme ET du texte sont recalculées en direct côté client
    # (voir renderQrLiveBoxes dans app.js) à partir de la boîte détectée —
    # ceci remplace bg_size_pct/bg_width_px/bg_height_px/font_size ci-dessus,
    # qui restent néanmoins enregistrés comme valeurs de repli.
    bg_proportional = get_setting('qrcode.live_style.bg_proportional', '0') == '1'
    # Ajustement (%) de cette taille automatique — appliqué comme facteur
    # multiplicatif (1 + pct/100) à la largeur/hauteur/police calculées en
    # direct depuis la boîte détectée (voir renderQrLiveBoxes dans app.js).
    # Sans effet si bg_proportional est désactivé.
    raw_prop_adjust = get_setting('qrcode.live_style.bg_proportional_adjust_pct', '') or '0'
    try:
        bg_proportional_adjust_pct = max(-50, min(50, int(raw_prop_adjust)))
    except ValueError:
        bg_proportional_adjust_pct = 0
    position = get_setting('qrcode.live_style.position', '') or 'above'
    if position not in dict(_QR_LIVE_POSITIONS):
        position = 'above'
    bg_image_filename = get_setting('qrcode.live_style.bg_image_filename', '')
    # Marge additionnelle (px) entre le texte et le bord de la forme, réglable
    # indépendamment de « Taille de la forme (%) » ci-dessus — s'ajoute au
    # padding de base de la forme (voir _add_padding_margin_css), sans effet
    # en mode proportionnel (padding forcé à 0, voir renderQrLiveBoxes dans
    # app.js — la forme colle alors exactement au QR-code détecté).
    raw_text_margin = get_setting('qrcode.live_style.text_margin_px', '') or '0'
    try:
        text_margin_px = max(0, min(60, int(raw_text_margin)))
    except ValueError:
        text_margin_px = 0
    scaled_padding = _scale_padding_css(shape_css['padding'], bg_size_pct / 100)
    return {
        'bg_mode': bg_mode,
        'bg_shape': bg_shape,
        'bg_size_pct': bg_size_pct,
        'bg_width_px': bg_width_px,
        'bg_height_px': bg_height_px,
        'bg_width_css': f'{bg_width_px}px' if bg_width_px else 'auto',
        'bg_height_css': f'{bg_height_px}px' if bg_height_px else 'auto',
        # Forcé à 1/1 uniquement pour la forme « Cercle » : sans ça, une boîte
        # en taille auto (padding + texte) n'est carrée que par coïncidence —
        # border-radius:50% sur une boîte plus large que haute (texte
        # horizontal) donne un OVALE, pas un disque. 'auto' pour les autres
        # formes (aucun effet, y compris quand largeur/hauteur sont fixées
        # explicitement ci-dessus : aspect-ratio ne s'applique jamais quand
        # les deux dimensions sont déjà définies, cf. spec CSS — le choix
        # manuel de l'admin est donc toujours respecté).
        'bg_aspect_ratio_css': '1 / 1' if bg_shape == 'circle' else 'auto',
        'bg_proportional': bg_proportional,
        'bg_proportional_adjust_pct': bg_proportional_adjust_pct,
        'bg_radius_css': shape_css['radius'],
        'bg_clip_css': shape_css['clip'],
        'bg_padding_css': _add_padding_margin_css(scaled_padding, text_margin_px),
        'text_margin_px': text_margin_px,
        'bg_color': get_setting('qrcode.live_style.bg_color', '') or '#0d8b8f',
        'bg_image_filename': bg_image_filename,
        'bg_image_url': f'/static/qr_live/{bg_image_filename}' if bg_image_filename else '',
        'font': get_setting('qrcode.live_style.font', '') or _PROMO_FONTS[0][0],
        'font_size': int(get_setting('qrcode.live_style.font_size', '') or '15'),
        'text_color': get_setting('qrcode.live_style.text_color', '') or '#ffffff',
        'position': position,
    }


# ── Incrustation de la forme/texte QR-code live sur les médias capturés ───────
# Option indépendante par type de média (voir _qr_burn_settings et
# /admin/guest_codes) : quand activée, la même forme + le même texte que l'aperçu
# en direct sont gravés dans la photo/vidéo/photo strip finale, à
# l'emplacement du QR-code réellement détecté sur CE média (nouvelle
# détection, indépendante du polling /api/qr/live) — jamais le message
# d'erreur (qrcode.live_error_style.*), qui n'a de sens que pour signaler un
# problème EN DIRECT : un média déjà enregistré ne peut contenir qu'un
# QR-code lisible ou rien.
#
# Reproduction en Pillow (résolution native de la capture) de ce que
# style.css/.qr-live-label affiche en CSS (résolution de la fenêtre
# navigateur) : mêmes réglages de forme/couleur/police/position, recalculés
# en pixels réels plutôt qu'en unités CSS (em, %, calc()...). Volontairement
# pas pixel-perfect (backdrop-filter, box-shadow, fine bordure ne sont pas
# reproduits — purement cosmétique, invisible à l'échelle d'une photo) mais
# visuellement fidèle : même forme, même texte, mêmes couleurs, même
# position relative au QR-code.

# Police système Windows correspondant à chaque option de _PROMO_FONTS, dans
# le même ordre (police web CSS -> fichier .ttf réel), toujours en gras
# quand une variante existe (font-weight:700 sur .qr-live-label). Chemin
# Windows en dur : l'application ne tourne que sous Windows (comme
# set_windows_wallpaper, _get_local_ip...) — voir C:\Windows\Fonts.
# Volontairement une simple liste (et non un dict indexé par _PROMO_FONTS[i][0])
# : _PROMO_FONTS n'est défini que plus loin dans ce fichier (section
# "Slide promo"), la résolution nom -> fichier doit donc se faire à l'appel
# (_qr_live_burn_font ci-dessous), pas à l'import du module.
_QR_LIVE_BURN_FONT_DIR = Path('C:/Windows/Fonts')
_QR_LIVE_BURN_FONT_FILES_BY_INDEX = [
    'segoeuib.ttf',   # Segoe UI (gras)
    'georgiab.ttf',   # Georgia (gras)
    'trebucbd.ttf',   # Trebuchet MS (gras)
    'impact.ttf',     # Impact (pas de variante gras)
    'courbd.ttf',     # Courier New (gras)
    'comicbd.ttf',    # Comic Sans MS (gras)
]
_qr_live_burn_font_cache: dict = {}

# Largeur de référence utilisée pour mettre à l'échelle les valeurs pensées
# pour l'aperçu caméra en direct (taille de police, marge d'ancrage, rayon
# des coins arrondis...) : ces réglages sont choisis par l'admin en
# regardant l'aperçu (généralement affiché autour de cette résolution) ;
# appliqués tels quels sur une capture haute résolution, ils paraîtraient
# minuscules. Sans effet sur les valeurs déjà en % ou en em (suivent déjà la
# taille de police, elle-même mise à l'échelle) ni sur le mode proportionnel
# (déjà calculé à partir de la taille réelle du QR-code détecté dans
# l'image, donc intrinsèquement à l'échelle).
_QR_LIVE_BURN_REFERENCE_WIDTH = 1280
_QR_LIVE_BURN_GAP_PX = 10  # QR_LIVE_GAP_PX côté client (app.js) — même valeur


def _hex_to_rgb(hex_color: str, default: tuple = (13, 139, 143)) -> tuple:
    h = (hex_color or '').lstrip('#')
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return default


# ── Polices personnalisées (voir /admin/texts, brique « Polices
# personnalisées ») ──────────────────────────────────────────────────────────
# Une police uploadée (.ttf/.otf) par l'admin s'ajoute à la liste des 6
# polices intégrées (_PROMO_FONTS) et apparaît, comme elles, dans TOUS les
# sélecteurs de police de l'app (boutons, tags, QR-codes live + export,
# chiffre d'étape photo strip, slide promo) — voir _all_fonts() ci-dessous,
# qui remplace _PROMO_FONTS partout où une liste d'options est construite ou
# une valeur soumise est validée. Fichier conservé tel quel sur disque, sans
# transformation (pas de variante grasse générée : contrairement aux polices
# Windows intégrées, l'admin fournit directement le fichier qu'il souhaite).
CUSTOM_FONTS_DIR = BASE_DIR / 'app' / 'static' / 'fonts'
_CUSTOM_FONT_ALLOWED_EXT = {'.ttf', '.otf'}


def _custom_font_value(row: dict) -> str:
    """Valeur `font-family` CSS stockée dans les réglages pour une police
    personnalisée — même rôle que le 1er élément des tuples _PROMO_FONTS.
    `row['family']` est un identifiant CSS déjà sûr (voir _slugify_font_family),
    donc pas besoin de guillemets, mais on les ajoute par cohérence avec les
    valeurs _PROMO_FONTS existantes et par sécurité si le slug contenait un
    espace."""
    return f'"{row["family"]}", sans-serif'


def _all_fonts() -> list:
    """Liste complète (valeur CSS, libellé) des polices disponibles partout
    où une police peut être choisie dans l'app : les 6 polices intégrées
    (_PROMO_FONTS) suivies des polices personnalisées ajoutées depuis
    /admin/texts, dans leur ordre d'ajout. À utiliser à la place de
    _PROMO_FONTS pour peupler un <select> ou valider une valeur soumise —
    _PROMO_FONTS reste la source de vérité pour les 6 polices intégrées
    elles-mêmes (résolution de fichier Windows, valeur par défaut...)."""
    return _PROMO_FONTS + [(_custom_font_value(row), f"{row['label']} (police perso)")
                            for row in list_custom_fonts()]


def _slugify_font_family(label: str) -> str:
    """Construit un identifiant CSS sûr (lettres/chiffres/tirets ASCII) à
    partir du libellé saisi par l'admin, préfixé pf- (Pictotem Font) pour ne
    jamais entrer en collision avec un nom de police système existant.
    L'unicité vis-à-vis des polices déjà en base est garantie par l'appelant
    (create_custom_font, avec suffixe -2/-3/... en cas de collision — voir
    db.py)."""
    normalized = unicodedata.normalize('NFKD', label or '').encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', normalized).strip('-').lower()
    return f'pf-{slug}' if slug else 'pf-police'


def _qr_live_burn_font(font_family: str, size_px: int):
    """Charge (avec cache) la police TrueType correspondant à `font_family`
    (valeur brute de qrcode.live_style.font, ou de tout autre réglage de
    police de l'app) à la taille `size_px`. Cherche d'abord parmi les
    polices personnalisées uploadées (fichier fourni tel quel par l'admin),
    puis parmi les 6 polices Windows intégrées (_PROMO_FONTS). Repli sur la
    police par défaut de Pillow (bitmap, taille fixe) si rien ne correspond
    ou si le fichier est introuvable — n'empêche jamais la capture."""
    size_px = max(6, int(size_px))
    cache_key = (font_family, size_px)
    cached = _qr_live_burn_font_cache.get(cache_key)
    if cached is not None:
        return cached
    for row in list_custom_fonts():
        if _custom_font_value(row) != font_family:
            continue
        try:
            font = ImageFont.truetype(str(CUSTOM_FONTS_DIR / row['filename']), size_px)
        except Exception:
            break  # fichier introuvable/corrompu : repli sur les polices intégrées ci-dessous
        _qr_live_burn_font_cache[cache_key] = font
        return font
    filename = _QR_LIVE_BURN_FONT_FILES_BY_INDEX[0]
    for i, (fam, _label) in enumerate(_PROMO_FONTS):
        if fam == font_family and i < len(_QR_LIVE_BURN_FONT_FILES_BY_INDEX):
            filename = _QR_LIVE_BURN_FONT_FILES_BY_INDEX[i]
            break
    try:
        font = ImageFont.truetype(str(_QR_LIVE_BURN_FONT_DIR / filename), size_px)
    except Exception:
        try:
            font = ImageFont.load_default(size=size_px)
        except TypeError:
            font = ImageFont.load_default()
    _qr_live_burn_font_cache[cache_key] = font
    return font


def _qr_live_burn_padding_px(style: dict, font_size_px: float) -> tuple:
    """Padding vertical/horizontal en pixels réels : base de la forme
    choisie (_QR_LIVE_SHAPE_CSS), mise à l'échelle par bg_size_pct, plus la
    marge additionnelle text_margin_px — équivalent numérique de
    bg_padding_css (_scale_padding_css / _add_padding_margin_css) mais
    résolu en px plutôt qu'en chaîne CSS (unité em résolue via
    font_size_px, comme le ferait un navigateur pour cet élément)."""
    base = _QR_LIVE_SHAPE_CSS[style['bg_shape']]['padding'].split()
    values = []
    for token in base:
        m = re.match(r'^([\d.]+)(px|em)$', token)
        if not m:
            values.append(0.0)
            continue
        value, unit = float(m.group(1)), m.group(2)
        values.append(value * font_size_px if unit == 'em' else value)
    pad_v = values[0] if len(values) > 0 else 0.0
    pad_h = values[1] if len(values) > 1 else pad_v
    scale = style['bg_size_pct'] / 100
    pad_v = pad_v * scale + style['text_margin_px']
    pad_h = pad_h * scale + style['text_margin_px']
    return pad_v, pad_h


def _qr_live_burn_anchor(position: str, box: tuple, gap_px: float) -> tuple:
    """Point d'ancrage (ax, ay) + fraction de la boîte à soustraire pour
    obtenir son coin haut-gauche (fx, fy) — équivalent numérique de
    computeQrLabelAnchor() + .qr-live-label.anchor-* (transform: translate)
    dans app.js/style.css, appliqué ici directement en pixels image plutôt
    qu'en pixels écran : pas de scale/offset à inverser, on dessine dans le
    même repère que la détection (contrairement à l'aperçu live, qui doit
    composer avec object-fit: cover sur l'élément <img> de prévisualisation)."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if position == 'below':
        return cx, y1 + gap_px, 0.5, 0.0
    if position == 'left':
        return x0 - gap_px, cy, 1.0, 0.5
    if position == 'right':
        return x1 + gap_px, cy, 0.0, 0.5
    if position == 'center':
        return cx, cy, 0.5, 0.5
    # 'above' (défaut)
    return cx, max(0.0, y0 - gap_px), 0.5, 1.0


def _qr_live_burn_ellipsize(draw, text: str, font, max_width: float) -> str:
    """Tronque `text` avec une ellipse finale si sa largeur dépasse
    max_width — équivalent de text-overflow:ellipsis (CSS), utilisé
    uniquement quand la largeur de la forme est fixe (bg_width_px) et que le
    texte décodé est trop long pour y tenir."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = '…'
    lo, hi = 0, len(text)
    best = ellipsis
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            best = candidate
            lo = mid
        else:
            hi = mid - 1
    return best


def _qr_live_burn_shape_mask(shape: str, w: int, h: int, radius_px: float):
    """Masque 'L' (w×h, blanc=opaque) reproduisant border-radius/clip-path
    de la forme CSS correspondante (_QR_LIVE_SHAPE_CSS) — utilisé pour
    habiller aussi bien un aplat de couleur qu'une image de fond
    personnalisée (voir _qr_live_burn_fill_layer)."""
    w, h = max(1, w), max(1, h)
    mask = Image.new('L', (w, h), 0)
    draw = ImageDraw.Draw(mask)
    if shape == 'pill':
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=min(w, h) / 2, fill=255)
    elif shape == 'rounded':
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=max(0.0, radius_px), fill=255)
    elif shape in ('circle', 'oval'):
        draw.ellipse((0, 0, w - 1, h - 1), fill=255)
    elif shape == 'star':
        star_pct = [
            (50, 0), (61, 35), (98, 35), (68, 57), (79, 91),
            (50, 70), (21, 91), (32, 57), (2, 35), (39, 35),
        ]
        points = [(px / 100 * (w - 1), py / 100 * (h - 1)) for px, py in star_pct]
        draw.polygon(points, fill=255)
    else:  # 'square' ou repli
        draw.rectangle((0, 0, w - 1, h - 1), fill=255)
    return mask


def _qr_live_burn_fill_layer(style: dict, w: int, h: int):
    """Contenu du fond, avant découpe par le masque de forme : aplat de
    couleur (bg_mode='shape') ou image personnalisée étirée aux dimensions
    de la forme (bg_mode='image' — background-size:100% 100% en CSS, donc
    resize direct, pas de recadrage)."""
    w, h = max(1, w), max(1, h)
    if style['bg_mode'] == 'image' and style['bg_image_filename']:
        img_path = QR_LIVE_DIR / style['bg_image_filename']
        if img_path.exists():
            try:
                custom = Image.open(img_path).convert('RGBA')
                return custom.resize((w, h), Image.LANCZOS)
            except Exception:
                logger.exception('QR live : image de fond illisible pour incrustation (%s).', img_path)
    return Image.new('RGBA', (w, h), _hex_to_rgb(style['bg_color']) + (255,))


def _qr_live_burn_draw_one(canvas, img_w: int, img_h: int, points, style: dict, text: str) -> bool:
    """Dessine, en place sur `canvas` (image RGBA Pillow de la taille de la
    capture), la forme + le texte configurés (qrcode.live_style.*) à
    l'emplacement du QR-code repéré par `points` (coins renvoyés par le
    détecteur ArUco, voir _qr_detect_boxes_robust). Retourne False si rien
    n'a été dessiné (texte vide, boîte dégénérée...)."""
    text = (text or '').strip()
    if not text:
        return False
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    box = (min(xs), min(ys), max(xs), max(ys))
    img_scale = max(0.3, img_w / _QR_LIVE_BURN_REFERENCE_WIDTH)
    draw = ImageDraw.Draw(canvas)

    proportional = style['bg_proportional']
    if proportional:
        adjust = 1 + style['bg_proportional_adjust_pct'] / 100
        box_w = max(0.0, box[2] - box[0]) * adjust
        box_h = max(0.0, box[3] - box[1]) * adjust
        if box_w <= 0 or box_h <= 0:
            return False
        font_size_px = max(8.0, min(box_h * 0.32, box_w * 0.16))
        pad_v = pad_h = 0.0
        font = _qr_live_burn_font(style['font'], round(font_size_px))
    else:
        font_size_px = max(6.0, style['font_size'] * img_scale)
        font = _qr_live_burn_font(style['font'], round(font_size_px))
        pad_v, pad_h = _qr_live_burn_padding_px(style, font_size_px)
        box_w = style['bg_width_px'] * img_scale if style['bg_width_px'] else \
            min(draw.textlength(text, font=font) + 2 * pad_h, img_w * 0.7)
        box_h = style['bg_height_px'] * img_scale if style['bg_height_px'] else \
            font_size_px * 1.25 + 2 * pad_v
        if style['bg_shape'] == 'circle' and not style['bg_width_px'] and not style['bg_height_px']:
            box_w = box_h = max(box_w, box_h)

    if box_w < 4 or box_h < 4:
        return False

    gap_px = _QR_LIVE_BURN_GAP_PX * img_scale
    ax, ay, fx, fy = _qr_live_burn_anchor(style['position'], box, gap_px)
    left = ax - box_w * fx
    top = ay - box_h * fy

    mask = _qr_live_burn_shape_mask(style['bg_shape'], round(box_w), round(box_h), 14 * img_scale)
    fill = _qr_live_burn_fill_layer(style, round(box_w), round(box_h))
    canvas.paste(fill, (round(left), round(top)), mask)

    # Texte dessiné sur un calque local à la boîte (box_w × box_h), collé
    # ensuite via paste(..., mask=calque) — équivalent d'overflow:hidden sur
    # .qr-live-label (CSS) : tout ce qui dépasserait de la boîte (texte trop
    # long en mode proportionnel notamment, où le rétrécissement automatique
    # de la police n'a que 2 bornes, largeur ET hauteur) est silencieusement
    # rogné au lieu de déborder sur l'image, comme le fait le navigateur.
    # paste(..., mask=texte) utilise le canal alpha du calque (anti-aliasing
    # du texte inclus) plutôt qu'un simple pochoir binaire, et gère nativement
    # une position (left, top) hors cadre (négative ou dépassant l'image).
    label = text
    if style['bg_width_px'] and not proportional:
        label = _qr_live_burn_ellipsize(draw, label, font, max(4.0, box_w - 2 * pad_h))
    box_w_i, box_h_i = max(1, round(box_w)), max(1, round(box_h))
    text_layer = Image.new('RGBA', (box_w_i, box_h_i), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_layer)
    bbox = tdraw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (box_w_i - tw) / 2 - bbox[0]
    ty = (box_h_i - th) / 2 - bbox[1]
    tdraw.text((tx, ty), label, font=font, fill=_hex_to_rgb(style['text_color']) + (255,))
    canvas.paste(text_layer, (round(left), round(top)), text_layer)
    return True


def _render_qr_burn_layer(image_bgr, detections: list):
    """Calque RGBA (mêmes dimensions que image_bgr) avec la forme + le texte
    de chaque QR-code DÉCODÉ dessinés à son emplacement — jamais le message
    d'erreur (voir l'en-tête de cette section). Retourne None si aucune
    détection décodée n'a donné lieu à un dessin."""
    decoded = [d for d in detections if d.get('text')]
    if not decoded:
        return None
    h, w = image_bgr.shape[:2]
    style = _qr_live_style_settings()
    canvas = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    drew = False
    for d in decoded:
        try:
            if _qr_live_burn_draw_one(canvas, w, h, d['points'], style, d['text']):
                drew = True
        except Exception:
            logger.exception('QR live : échec incrustation forme/texte sur le média.')
    return canvas if drew else None


def _apply_qr_burn_layer_bgr(image_bgr, layer):
    """Compose `layer` (RGBA, voir _render_qr_burn_layer) sur une image BGR
    (numpy, OpenCV) et renvoie le résultat au même format — utilisé pour la
    photo, le photo strip et chaque frame de la vidéo (voir
    _qr_video_burn_track ci-dessous pour la vidéo)."""
    if layer is None:
        return image_bgr
    base = Image.fromarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)).convert('RGBA')
    composited = Image.alpha_composite(base, layer)
    return cv2.cvtColor(np.array(composited.convert('RGB')), cv2.COLOR_RGB2BGR)


# ── Incrustation QR-code sur la vidéo : suivi du mouvement ────────────────────
# La vidéo est le seul média où le QR-code peut bouger PENDANT la capture
# (contrairement à la photo/au photo strip, figés à l'instant du
# déclenchement) — sans suivi, la forme+texte resterait à la position de la
# toute première frame, décalée dès que le visiteur bouge le QR-code
# présenté. Compromis retenu (choisi avec l'utilisateur, voir /admin/guest_codes) :
# détection ÉCHANTILLONNÉE (1 frame sur _QR_VIDEO_TRACK_SAMPLE_INTERVAL, pas
# sur CHAQUE frame — bien plus coûteux pour un gain de fluidité imperceptible
# une fois interpolé) + interpolation linéaire de la position entre deux
# détections proches, pour un mouvement toujours fluide à l'écran malgré
# l'échantillonnage.
#
# Traitement en 2 passes après l'enregistrement (donc SANS impact sur la
# fluidité/cadence de la capture elle-même, qui reste un simple
# read_frame()/writer.write() en boucle comme avant) :
#   passe 1 (_qr_video_detect_keyframes) : relit la vidéo déjà transcodée
#     (+ cadre décoratif éventuel), détection échantillonnée -> liste de
#     points-clés (frame, position, texte) ;
#   construction (_qr_video_build_track) : à partir de ces points-clés,
#     calcule la position à afficher pour CHAQUE frame (interpolation ou
#     maintien courte durée) ;
#   passe 2 (_qr_video_burn_track) : nouvelle lecture, incrustation frame
#     par frame (réutilise _render_qr_burn_layer/_apply_qr_burn_layer_bgr,
#     déjà utilisées pour la photo/le photo strip) dans un nouveau fichier,
#     transcodé en MP4 par l'appelant (capture_video).
_QR_VIDEO_TRACK_SAMPLE_INTERVAL = 3
_QR_VIDEO_TRACK_HOLD_FRAMES = _QR_VIDEO_TRACK_SAMPLE_INTERVAL * 2
_QR_VIDEO_TRACK_MAX_INTERP_GAP = _QR_VIDEO_TRACK_SAMPLE_INTERVAL * 4


def _qr_video_detect_keyframes(video_path: Path, sample_interval: int) -> tuple:
    """Passe 1 : parcourt `video_path` frame par frame, lance la détection
    QR-code (_qr_detect_boxes_robust) une frame sur `sample_interval`
    seulement. Ne garde qu'un point-clé par frame échantillonnée (le
    premier QR-code décodé, s'il y en a plusieurs — un seul suivi à la
    fois). Retourne (liste de (frame_idx, box, text), nombre total de
    frames lues)."""
    keyframes = []
    frame_idx = 0
    cap = cv2.VideoCapture(str(video_path))
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % sample_interval == 0:
                try:
                    detections = _qr_detect_boxes_robust(frame)
                except Exception:
                    logger.exception('QR live : détection échouée (frame vidéo %d).', frame_idx)
                    detections = []
                for d in detections:
                    if d.get('text'):
                        xs = [p[0] for p in d['points']]
                        ys = [p[1] for p in d['points']]
                        keyframes.append((frame_idx, (min(xs), min(ys), max(xs), max(ys)), d['text']))
                        break
            frame_idx += 1
    finally:
        cap.release()
    return keyframes, frame_idx


def _qr_video_build_track(frame_count: int, keyframes: list, sample_interval: int) -> list:
    """À partir des points-clés (frame_idx, box, text) où un QR-code a été
    décodé, construit la position à afficher pour CHAQUE frame de la vidéo :
    interpolation linéaire entre deux points-clés suffisamment proches
    (écart <= _QR_VIDEO_TRACK_MAX_INTERP_GAP frames), maintien courte durée
    (_QR_VIDEO_TRACK_HOLD_FRAMES) de part et d'autre d'un point-clé isolé —
    le QR-code a momentanément disparu du champ —, rien au-delà : il est
    alors considéré hors champ plutôt que de laisser l'étiquette « flotter »
    sur toute la vidéo. Retourne une liste de (box, text) | None, un
    élément par frame (0..frame_count-1)."""
    track = [None] * frame_count
    if not keyframes:
        return track
    hold = _QR_VIDEO_TRACK_HOLD_FRAMES
    max_gap = _QR_VIDEO_TRACK_MAX_INTERP_GAP
    for i, (idx, box, text) in enumerate(keyframes):
        if 0 <= idx < frame_count:
            track[idx] = (box, text)
        if i + 1 >= len(keyframes):
            continue
        idx2, box2, text2 = keyframes[i + 1]
        gap = idx2 - idx
        if gap <= 1:
            continue
        if gap <= max_gap:
            for f in range(idx + 1, idx2):
                t = (f - idx) / gap
                box_i = tuple(box[k] + (box2[k] - box[k]) * t for k in range(4))
                if 0 <= f < frame_count:
                    track[f] = (box_i, text)
        else:
            for f in range(idx + 1, min(idx + 1 + hold, idx2, frame_count)):
                track[f] = (box, text)
            for f in range(max(idx2 - hold, idx + 1), idx2):
                if track[f] is None:
                    track[f] = (box2, text2)
    last_idx, last_box, last_text = keyframes[-1]
    for f in range(last_idx + 1, min(last_idx + 1 + hold, frame_count)):
        if track[f] is None:
            track[f] = (last_box, last_text)
    return track


def _qr_video_burn_track(video_path: Path, track: list, w: int, h: int, fps: float,
                          output_avi_path: Path) -> bool:
    """Passe 2 : relit `video_path` frame par frame et grave, pour chaque
    frame ayant une position suivie (voir _qr_video_build_track), la
    forme+texte à cette position exacte — réutilise le même rendu que la
    photo/le photo strip (_render_qr_burn_layer / _apply_qr_burn_layer_bgr),
    avec 4 coins synthétiques reconstruits depuis la boîte suivie/interpolée
    (ces fonctions n'ont besoin que d'un rectangle englobant, pas des coins
    réels du QR-code). Écrit le résultat dans un .avi MJPG (même format que
    l'enregistrement brut), à transcoder en MP4 par l'appelant. Retourne
    False si le fichier de sortie n'a pas pu être ouvert en écriture."""
    cap = cv2.VideoCapture(str(video_path))
    writer = cv2.VideoWriter(str(output_avi_path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (w, h))
    if not writer.isOpened():
        cap.release()
        logger.error('QR live : VideoWriter impossible à ouvrir pour %s (incrustation suivie).', output_avi_path)
        return False
    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            entry = track[frame_idx] if frame_idx < len(track) else None
            if entry is not None:
                box, text = entry
                synth_pts = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
                try:
                    layer = _render_qr_burn_layer(frame, [{'text': text, 'points': synth_pts}])
                except Exception:
                    logger.exception('QR live : échec incrustation suivie (frame vidéo %d).', frame_idx)
                    layer = None
                if layer is not None:
                    frame = _apply_qr_burn_layer_bgr(frame, layer)
            writer.write(frame)
            frame_idx += 1
    finally:
        cap.release()
        writer.release()
    return True


# ── Message d'erreur QR-code (détecté mais illisible) ─────────────────────────
# Affiché à la place du contenu décodé quand un QR-code est repéré (marqueurs
# trouvés par le détecteur ArUco) mais reste illisible malgré les tentatives
# de rattrapage (voir _qr_detect_boxes_robust / api_qr_live) — texte OU image
# au choix, avec sa propre police/taille/couleurs, indépendant du style du
# contenu décodé avec succès (_qr_live_style_settings ci-dessus), avec lequel
# il ne partage que la forme/taille/position (bg_shape, bg_width_px,
# bg_height_px, bg_proportional, position — voir style.css .qr-live-label).
def _qr_live_error_style_settings() -> dict:
    raw_enabled = get_setting('qrcode.live_error_style.enabled', '')
    if raw_enabled == '':
        # Jamais réglé sous cette clé (mise à jour depuis une version
        # antérieure) : reprend l'ancien réglage qrcode.too_small_message_enabled
        # (activé par défaut) le temps d'un premier enregistrement depuis
        # /admin/guest_codes — voir le même principe pour le mot de passe admin.
        enabled = get_setting('qrcode.too_small_message_enabled', '1') == '1'
    else:
        enabled = raw_enabled == '1'
    mode = get_setting('qrcode.live_error_style.mode', '') or 'text'
    if mode not in ('text', 'image'):
        mode = 'text'
    text = get_setting('qrcode.live_error_style.text', '').strip() or 'QR-code détecté mais illisible'
    image_filename = get_setting('qrcode.live_error_style.image_filename', '')
    raw_size = get_setting('qrcode.live_error_style.font_size', '') or '15'
    try:
        font_size = max(8, int(raw_size))
    except ValueError:
        font_size = 15
    return {
        'enabled': enabled,
        'mode': mode,
        'text': text,
        'image_filename': image_filename,
        'image_url': f'/static/qr_live/{image_filename}' if image_filename else '',
        'text_color': get_setting('qrcode.live_error_style.text_color', '') or '#ffffff',
        'bg_color': get_setting('qrcode.live_error_style.bg_color', '') or '#b26e0a',
        'font': get_setting('qrcode.live_error_style.font', '') or _PROMO_FONTS[0][0],
        'font_size': font_size,
    }


def _qr_detect_for_capture(image, capture_label: str = '') -> list:
    """Détection QR-code pour une capture (photo/photo strip/1ère frame
    vidéo) : ne s'exécute que si l'add-on est activé (qrcode.enabled) —
    partagée par le tag automatique (_qr_tag_detections) ET l'incrustation
    forme+texte sur le média (_render_qr_burn_layer), qui n'analysent donc
    l'image qu'une seule fois par capture. Utilise le pipeline de détection
    robuste partagé avec l'aperçu en direct (_qr_detect_boxes_robust) : un
    QR-code lu en direct doit aussi l'être sur le média final."""
    if get_setting('qrcode.enabled', '0') != '1':
        return []
    try:
        return _qr_detect_boxes_robust(image)
    except Exception:
        logger.exception('QR-code : détection échouée pour %s.', capture_label or 'la capture')
        return []


def _qr_tag_detections(capture_id: int, detections: list) -> list:
    """Ajoute comme tags libres sur `capture_id` les QR-codes déjà détectés
    (voir _qr_detect_for_capture, qui gère l'activation de l'add-on).
    Retourne la liste des textes ajoutés (peut être vide). Anciennement
    _scan_and_tag_qr_codes, qui recalculait sa propre détection."""
    decoded_texts = [d['text'] for d in detections if d.get('text')]
    if not decoded_texts:
        return []

    tags_cfg = _tags_settings()
    added, seen = [], set()
    for text in decoded_texts:
        text = (text or '').strip().replace('\n', ' ').replace('\r', ' ')
        if not text or text in seen:
            continue
        seen.add(text)
        if len(text) < tags_cfg['free_min_length']:
            continue
        if len(text) > tags_cfg['free_max_length']:
            text = text[:tags_cfg['free_max_length']]
        if count_capture_tags(capture_id) >= tags_cfg['max_per_capture']:
            break
        try:
            add_capture_tag(capture_id, tag_id=None, label=text)
            added.append(text)
        except Exception:
            logger.exception('QR-code : échec ajout du tag "%s" sur la capture #%d.', text, capture_id)

    if added:
        logger.info('QR-code(s) détecté(s) sur la capture #%d, ajouté(s) comme tag(s) : %s',
                    capture_id, ', '.join(added))
    return added


def _qr_retry_decode_upscaled(frame, pts, target_side=400, max_scale=10.0):
    """Deuxième chance de décodage pour un QR-code repéré (marqueurs ArUco
    trouvés) mais dont le décodage a échoué au premier passage — typiquement
    un code trop petit dans l'image brute. Recadre la zone détectée (avec
    marge) puis l'agrandit numériquement (interpolation cubique) avant de
    retenter la détection+décodage sur ce recadrage seul. Mesuré
    empiriquement : rattrape le décodage pour des QR-codes descendant
    jusqu'à ~5% de la largeur de l'image (contre ~10% sans cette étape),
    y compris en présence de flou/bruit/compression JPEG typiques d'une
    caméra réelle — mais ne fait pas de miracle en dessous : un code
    peut rester illisible s'il n'a physiquement pas assez de pixels captés
    par le capteur. Coût négligeable (recadrage minuscule), tenté
    uniquement pour les zones déjà repérées mais non décodées."""
    h, w = frame.shape[:2]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    margin = 12
    x0 = max(0, int(min(xs)) - margin)
    y0 = max(0, int(min(ys)) - margin)
    x1 = min(w, int(max(xs)) + margin)
    y1 = min(h, int(max(ys)) + margin)
    if x1 <= x0 or y1 <= y0:
        return ''
    crop = frame[y0:y1, x0:x1]
    side = max(crop.shape[:2])
    if side <= 0:
        return ''
    scale = min(max_scale, target_side / side)
    if scale <= 1.0:
        return ''  # déjà assez grand : le premier échec n'est pas dû à la taille
    try:
        upscaled = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        found2, points2 = _qr_detector_aruco.detectMulti(upscaled)
        if not found2 or points2 is None or not len(points2):
            return ''
        _ok2, texts2, _straight2 = _qr_detector_aruco.decodeMulti(upscaled, points2)
        if texts2 is None or not len(texts2):
            return ''
        return (texts2[0] or '').strip()
    except Exception:
        logger.exception('QR live : nouvelle tentative (recadrage agrandi) échouée.')
        return ''


def _qr_detect_boxes_robust(image) -> list:
    """Pipeline de détection/décodage QR-code robuste, partagé entre
    l'aperçu en direct (/api/qr/live) et le marquage automatique/l'incrustation
    post-capture (_qr_detect_for_capture), pour qu'un QR-code lu pendant le
    cadrage soit aussi lu sur le média final : passe directe (détecteur
    ArUco) → si rien trouvé, nouvelle tentative sur l'image entière
    agrandie (_QR_LIVE_FULLFRAME_UPSCALE) → pour chaque zone repérée mais
    non décodée, nouvelle tentative sur un recadrage agrandi
    (_qr_retry_decode_upscaled). Retourne une liste de dicts
    {'text': str|None, 'points': [...]} — 'text' vaut None quand le code
    est repéré (marqueurs trouvés) mais reste illisible malgré ces
    tentatives. Si le texte brut décodé correspond exactement à un code
    invité existant (/admin/guest_codes, table guest_codes), il est
    remplacé ici par le texte associé (voir _guest_code_resolve, fin de
    cette fonction) — résolution centralisée à ce point unique de
    détection pour s'appliquer uniformément à l'aperçu en direct, à
    l'incrustation sur le média et au tag automatique."""
    try:
        found, decoded_texts, points, _straight = _qr_detector_aruco.detectAndDecodeMulti(image)
    except Exception:
        logger.exception('QR : détection échouée.')
        return []

    if not found or points is None or not len(points):
        # Rien trouvé sur l'image brute : nouvelle tentative sur l'image
        # ENTIÈRE agrandie numériquement. Les points trouvés sont dans
        # l'espace de l'image agrandie : remis à l'échelle de l'image
        # d'origine juste après, pour que le reste de la fonction
        # (recadrage, coordonnées renvoyées à l'appelant) continue de
        # raisonner dans l'espace de l'image brute sans distinction de cas.
        try:
            factor = _QR_LIVE_FULLFRAME_UPSCALE
            big = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            found2, points2 = _qr_detector_aruco.detectMulti(big)
            if found2 and points2 is not None and len(points2):
                _ok2, texts2, _straight2 = _qr_detector_aruco.decodeMulti(big, points2)
                found = True
                decoded_texts = texts2
                points = [[(float(p[0]) / factor, float(p[1]) / factor) for p in pts] for pts in points2]
        except Exception:
            logger.exception('QR : nouvelle tentative (image entière agrandie) échouée.')

    results = []
    if found and points is not None:
        texts = list(decoded_texts) if decoded_texts is not None else []
        for i, pts in enumerate(points):
            text = (texts[i] if i < len(texts) else '') or ''
            text = text.strip().replace('\n', ' ').replace('\r', ' ')
            if not text:
                # Détecté mais pas décodé au premier passage : nouvelle
                # tentative sur un recadrage agrandi avant de conclure que
                # le code est réellement trop petit à lire.
                text = _qr_retry_decode_upscaled(image, pts)
            text = text or None
            if text:
                # Le contenu brut du QR-code peut être soit un texte direct,
                # soit un code invité (voir /admin/guest_codes) : dans ce
                # second cas, c'est le texte associé qui doit être affiché/
                # incrusté/tagué, jamais le code numérique lui-même.
                mapped = get_guest_code_text(text)
                if mapped is not None:
                    text = resolve_dynamic_placeholders(mapped)
            results.append({'text': text, 'points': pts})
    return results


# ── Add-on : contenu QR-code affiché en direct sur l'aperçu caméra ────────────
# Activable indépendamment du tag automatique ci-dessus (voir admin_tags.html,
# case "live_overlay") : pendant que le visiteur cadre sa prise (accueil,
# choix du cadre), le contenu décodé d'un QR-code présenté devant l'objectif
# s'affiche au-dessus de celui-ci sur l'écran du kiosque (voir
# pollQrLive/renderQrLiveBoxes dans app.js). Interrogé par polling côté
# client (pas de scan en continu côté serveur) et scanne la DERNIÈRE frame
# déjà lue par le flux MJPEG live (get_latest_preview_frame, voir
# camera.py/publish_preview_frame) — aucune lecture caméra supplémentaire,
# aucune contention avec le flux principal.
@app.route('/api/qr/live')
@require_main_auth
def api_qr_live():
    if get_setting('qrcode.live_overlay', '0') != '1':
        return jsonify({'ok': True, 'enabled': False, 'boxes': []})
    frame = get_latest_preview_frame()
    if frame is None:
        return jsonify({'ok': True, 'enabled': True, 'boxes': []})
    h, w = frame.shape[:2]
    too_small_enabled = _qr_live_error_style_settings()['enabled']
    detections = _qr_detect_boxes_robust(frame)

    boxes = []
    for det in detections:
        pts = det['points']
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        if det['text']:
            box = {'x0': min(xs), 'y0': min(ys), 'x1': max(xs), 'y1': max(ys)}
            box['text'] = det['text'] if len(det['text']) <= 80 else det['text'][:80] + '…'
            box['too_small'] = False
            boxes.append(box)
        elif too_small_enabled:
            # Marqueurs du QR-code repérés, mais code trop petit dans
            # l'image pour être décodé même après agrandissement —
            # signalé au visiteur (désactivable depuis /admin/guest_codes),
            # plutôt que silencieusement ignoré.
            box = {'x0': min(xs), 'y0': min(ys), 'x1': max(xs), 'y1': max(ys)}
            box['text'] = None
            box['too_small'] = True
            boxes.append(box)
        # sinon (trop petit + message désactivé) : zone ignorée, comme si
        # rien n'avait été détecté.
    return jsonify({'ok': True, 'enabled': True, 'frame_width': w, 'frame_height': h, 'boxes': boxes})


@app.route('/api/capture/<int:capture_id>/tags', methods=['GET', 'POST'])
@require_main_auth
def api_capture_tags(capture_id):
    """Consultation/assignation des tags d'une capture, appelée par le
    modal "Tags" du kiosque juste après la prise de vue (voir applyReplayUi
    dans static/app.js). Pas de @csrf_protect, cohérent avec les autres
    routes du flux de capture (retake, capture/photo, ...)."""
    s = _tags_settings()
    if request.method == 'GET':
        return jsonify({
            'ok': True,
            'settings': s,
            'available_tags': list_tags(),
            'assigned': list_capture_tags(capture_id),
        })

    if not s['enabled']:
        return jsonify({'ok': False, 'error': 'Fonction tags désactivée'}), 403

    data = request.get_json(silent=True) or {}
    if count_capture_tags(capture_id) >= s['max_per_capture']:
        return jsonify({'ok': False, 'error': f"Maximum {s['max_per_capture']} tag(s) par média"}), 400

    tag_id = data.get('tag_id')
    if tag_id is not None:
        tag = get_tag_by_id(int(tag_id))
        if not tag:
            return jsonify({'ok': False, 'error': 'Tag introuvable'}), 404
        assignment_id = add_capture_tag(capture_id, tag_id=tag['id'], label=tag['label'])
    else:
        if not s['free_enabled']:
            return jsonify({'ok': False, 'error': 'Tag libre désactivé'}), 403
        free_text = (data.get('free_text') or '').strip()
        if len(free_text) < s['free_min_length'] or len(free_text) > s['free_max_length']:
            return jsonify({
                'ok': False,
                'error': f"Le texte doit contenir entre {s['free_min_length']} et "
                         f"{s['free_max_length']} caractères",
            }), 400
        assignment_id = add_capture_tag(capture_id, tag_id=None, label=free_text)

    return jsonify({'ok': True, 'assigned': list_capture_tags(capture_id), 'assignment_id': assignment_id})


@app.route('/api/capture/<int:capture_id>/tags/<int:assignment_id>/delete', methods=['POST'])
@require_main_auth
def api_capture_tag_delete(capture_id, assignment_id):
    row = delete_capture_tag(assignment_id)
    if not row or row['capture_id'] != capture_id:
        return jsonify({'ok': False, 'error': 'Assignation introuvable'}), 404
    return jsonify({'ok': True, 'assigned': list_capture_tags(capture_id)})


def _compose_photo_strip(frames, ps_cfg):
    """Empile verticalement les prises d'un photo strip sur un fond uni,
    avec une marge régulière (gap_px) autour et entre chaque prise —
    résultat classique de bande photobooth. Toutes les frames proviennent
    de la même caméra/résolution (aucun redimensionnement nécessaire)."""
    gap = max(0, int(ps_cfg.get('gap_px', 16)))
    bg_hex = str(ps_cfg.get('background_color', '#ffffff')).lstrip('#')
    try:
        bg_bgr = (int(bg_hex[4:6], 16), int(bg_hex[2:4], 16), int(bg_hex[0:2], 16))
    except (ValueError, IndexError):
        bg_bgr = (255, 255, 255)

    h, w = frames[0].shape[:2]
    n = len(frames)
    total_h = gap + n * (h + gap)
    total_w = w + 2 * gap
    canvas = np.full((total_h, total_w, 3), bg_bgr, dtype=np.uint8)
    y = gap
    for f in frames:
        fh, fw = f.shape[:2]
        canvas[y:y + fh, gap:gap + fw] = f
        y += fh + gap
    return canvas


# ── Photo strip : grand chiffre d'étape (paramétrable B-O) ────────────────────
# Affiché plein écran pendant chaque prise du strip, en synchro avec le
# petit label "Photo x/N" (voir _qr_detect_for_capture plus haut pour le
# contexte général du strip). Modèle "{n}"/"{total}" identique à la
# convention idle_timer_badge_text ("Retour dans {n}s"). Position résolue
# côté serveur en propriétés CSS explicites (top/left/right/bottom/
# transform) — plus simple à maintenir qu'un jeu de classes CSS par preset.

_PHOTOSTRIP_STEP_POSITIONS = {
    'center':       {'top': '50%',   'left': '50%',  'right': 'auto', 'bottom': 'auto',  'transform': 'translate(-50%, -50%)'},
    'top':          {'top': '110px', 'left': '50%',  'right': 'auto', 'bottom': 'auto',  'transform': 'translateX(-50%)'},
    'bottom':       {'top': 'auto',  'left': '50%',  'right': 'auto', 'bottom': '190px', 'transform': 'translateX(-50%)'},
    'top-left':     {'top': '110px', 'left': '40px', 'right': 'auto', 'bottom': 'auto',  'transform': 'none'},
    'top-right':    {'top': '110px', 'left': 'auto', 'right': '40px', 'bottom': 'auto',  'transform': 'none'},
    'bottom-left':  {'top': 'auto',  'left': '40px', 'right': 'auto', 'bottom': '190px', 'transform': 'none'},
    'bottom-right': {'top': 'auto',  'left': 'auto', 'right': '40px', 'bottom': '190px', 'transform': 'none'},
}
_PHOTOSTRIP_STEP_POSITION_LABELS = [
    ('center', 'Centre'), ('top', 'Haut'), ('bottom', 'Bas'),
    ('top-left', 'Haut gauche'), ('top-right', 'Haut droite'),
    ('bottom-left', 'Bas gauche'), ('bottom-right', 'Bas droite'),
]


def _photostrip_step_settings() -> dict:
    position = get_setting('photostrip_step.position', '') or 'center'
    if position not in _PHOTOSTRIP_STEP_POSITIONS:
        position = 'center'
    css = _PHOTOSTRIP_STEP_POSITIONS[position]
    return {
        'text':      get_setting('photostrip_step.text', '') or '{n}',
        'font':      get_setting('photostrip_step.font', '') or _PROMO_FONTS[0][0],
        'font_size': int(get_setting('photostrip_step.font_size', '') or '160'),
        'position':  position,
        'css_top':       css['top'],
        'css_left':      css['left'],
        'css_right':     css['right'],
        'css_bottom':    css['bottom'],
        'css_transform': css['transform'],
    }


# ── Photo strip : capture pilotée par le client, prise par prise ──────────────
# Historique : la version précédente prenait les N clichés dans une seule
# requête HTTP bloquante (boucle for + time.sleep côté serveur), pendant que
# le client SIMULAIT l'avancement "Photo x/N" avec son propre setInterval
# calé sur les mêmes durées — un pur pari de calibrage, sans aucune
# confirmation réelle du serveur entre chaque prise. Tout décalage (charge
# CPU, contention caméra avec l'aperçu live, temps de composition/encodage)
# désynchronisait l'affichage de la réalité, d'où le bug "1/3 avant 2/3"
# remonté par l'utilisateur. Nouveau flux en 3 requêtes séquentielles
# (start → shot ×N → finish), chaque "shot" ne renvoyant OK qu'une fois la
# prise réellement effectuée côté serveur : l'affichage client (petit label
# + grand chiffre, voir applyPhotoStripStep dans app.js) n'avance donc plus
# JAMAIS en avance sur la réalité, par construction. État de session
# conservé en mémoire (process unique, un seul kiosque actif à la fois) et
# purgé après un délai si une session est abandonnée (borne redémarrée en
# cours de strip, etc.).

_PHOTOSTRIP_SESSIONS: dict = {}
_PHOTOSTRIP_LOCK = threading.Lock()
_PHOTOSTRIP_SESSION_TTL = 120  # secondes avant purge d'une session abandonnée


def _purge_expired_photostrip_sessions():
    now = time.time()
    expired = [tok for tok, s in _PHOTOSTRIP_SESSIONS.items()
               if now - s['created_at'] > _PHOTOSTRIP_SESSION_TTL]
    for tok in expired:
        _PHOTOSTRIP_SESSIONS.pop(tok, None)
    if expired:
        logger.info('Photo strip : %d session(s) abandonnée(s) purgée(s).', len(expired))


@app.route('/api/capture/photostrip/start', methods=['POST'])
@require_main_auth
def capture_photostrip_start():
    ps_cfg = CONFIG.get('capture', {}).get('photo_strip', {})
    if not ps_cfg.get('enabled', True):
        return jsonify({'ok': False, 'error': 'Photo strip désactivé'}), 403
    req = request.get_json(silent=True) or {}
    frame_id = req.get('frame', 'none')
    shots = max(2, min(int(ps_cfg.get('shots', 3)), 6))
    interval = max(0.3, float(ps_cfg.get('interval_sec', 1.2)))
    with _PHOTOSTRIP_LOCK:
        _purge_expired_photostrip_sessions()
        token = secrets.token_hex(8)
        _PHOTOSTRIP_SESSIONS[token] = {
            'frame_id': frame_id,
            'shots': shots,
            'frames': [],
            'created_at': time.time(),
        }
    logger.info('Photo strip : session démarrée (%d prises, cadre=%s)', shots, frame_id)
    return jsonify({'ok': True, 'token': token, 'shots': shots, 'interval_sec': interval})


@app.route('/api/capture/photostrip/shot', methods=['POST'])
@require_main_auth
def capture_photostrip_shot():
    req = request.get_json(silent=True) or {}
    token = req.get('token', '')
    with _PHOTOSTRIP_LOCK:
        session = _PHOTOSTRIP_SESSIONS.get(token)
    if not session:
        return jsonify({'ok': False, 'error': 'Session photo strip introuvable ou expirée'}), 404
    if len(session['frames']) >= session['shots']:
        return jsonify({'ok': False, 'error': 'Photo strip déjà complet'}), 400

    raw = read_frame()
    display = raw
    overlay_path = get_frame_overlay_path(session['frame_id'])
    if overlay_path and overlay_path.exists():
        h, w = raw.shape[:2]
        overlay = get_overlay_bgra(overlay_path, w, h)
        if overlay is not None:
            display = composite_frame_overlay(raw, overlay)

    with _PHOTOSTRIP_LOCK:
        session = _PHOTOSTRIP_SESSIONS.get(token)
        if not session:
            return jsonify({'ok': False, 'error': 'Session photo strip introuvable ou expirée'}), 404
        session['frames'].append(display)
        shot_index = len(session['frames'])
        shots = session['shots']
    return jsonify({'ok': True, 'shot_index': shot_index, 'shots': shots})


@app.route('/api/capture/photostrip/finish', methods=['POST'])
@require_main_auth
def capture_photostrip_finish():
    req = request.get_json(silent=True) or {}
    token = req.get('token', '')
    with _PHOTOSTRIP_LOCK:
        session = _PHOTOSTRIP_SESSIONS.pop(token, None)
    if not session:
        return jsonify({'ok': False, 'error': 'Session photo strip introuvable ou expirée'}), 404
    frames = session['frames']
    if len(frames) != session['shots']:
        return jsonify({'ok': False, 'error': 'Photo strip incomplet'}), 400

    ps_cfg = CONFIG.get('capture', {}).get('photo_strip', {})
    strip_img = _compose_photo_strip(frames, ps_cfg)

    detections = _qr_detect_for_capture(strip_img, 'le photo strip')
    if detections and _qr_burn_settings()['strip']:
        burn_layer = _render_qr_burn_layer(strip_img, detections)
        if burn_layer is not None:
            strip_img = _apply_qr_burn_layer_bgr(strip_img, burn_layer)

    stamp = current_stamp()
    filename = f'strip-{stamp}.jpg'
    filepath = PHOTO_DIR / filename
    quality = int(CONFIG['camera'].get('jpeg_quality', 92))
    ok, buf = cv2.imencode('.jpg', strip_img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return jsonify({'ok': False, 'error': 'Échec encodage du photo strip'}), 500
    filepath.write_bytes(buf.tobytes())

    thumb_name = f'thumb-{stamp}.jpg'
    make_thumb(filepath, THUMBS_DIR / thumb_name)
    capture_id, media_uid = record_capture('photo', filename, thumb_name)
    logger.info('Photo strip capturé %s (%d prises, cadre=%s)', filename, len(frames), session['frame_id'])
    qr_tags = _qr_tag_detections(capture_id, detections)
    return jsonify({'ok': True, 'id': capture_id, 'media_uid': media_uid, 'kind': 'photo',
                    'filename': filename, 'qr_tags': qr_tags,
                    'url': f'/media/photo/{filename}', 'message': resolve_dynamic_placeholders(message_text())})


@app.route('/api/capture/photostrip/cancel', methods=['POST'])
@require_main_auth
def capture_photostrip_cancel():
    """Nettoyage best-effort si le visiteur interrompt un strip en cours
    (retour à l'accueil, etc.) — la purge par TTL reste le filet de sécurité
    si ce endpoint n'est jamais appelé."""
    req = request.get_json(silent=True) or {}
    token = req.get('token', '')
    with _PHOTOSTRIP_LOCK:
        _PHOTOSTRIP_SESSIONS.pop(token, None)
    return jsonify({'ok': True})


@app.route('/api/print/<int:capture_id>', methods=['POST'])
@require_main_auth
def api_print(capture_id):
    with closing(db_conn()) as conn:
        row = conn.execute('SELECT * FROM captures WHERE id = ?', (capture_id,)).fetchone()
        if not row:
            abort(404)
        row = dict(row)
        if row['kind'] != 'photo':
            return jsonify({'ok': False, 'error': 'Impression disponible uniquement pour les photos'}), 400
        path = PHOTO_DIR / row['filename']
        ok, output = print_photo(path)
        if ok:
            conn.execute('UPDATE captures SET printed = 1 WHERE id = ?', (capture_id,))
            conn.commit()
    return jsonify({'ok': ok, 'output': output})


# ── Galerie ───────────────────────────────────────────────────────────────────

@app.route('/gallery')
@require_gallery_auth
def gallery():
    if not CONFIG['gallery'].get('enabled', True):
        abort(404)
    sort = request.args.get('sort', CONFIG['gallery'].get('sort', 'desc'))
    kind = request.args.get('kind', '')
    page_size = int(CONFIG['gallery'].get('page_size', 60))
    page = max(1, int(request.args.get('page', 1)))
    email_cookie = request.cookies.get(CONFIG['emails']['cookie_name'], '')
    media_id_query = request.args.get('q', '').strip()
    tag_query = request.args.get('tag', '').strip()

    # Intégration des uploads invités dans la galerie officielle (voir
    # section "Upload invités" plus bas) : paramétrable indépendamment de
    # leur diffusion dans /bestof, désactivée par défaut — comportement de
    # la galerie inchangé tant que ce n'est pas activé explicitement.
    guest_cfg = _guest_upload_settings()
    guest_in_gallery = guest_cfg['enabled'] and guest_cfg['include_in_gallery']
    source = request.args.get('source', '') if guest_in_gallery else ''

    if guest_in_gallery:
        captures, total = list_gallery_combined(sort, kind, source, page=page, page_size=page_size,
                                                media_uid=media_id_query, tag=tag_query)
    else:
        captures, total = list_captures(sort, kind, page=page, page_size=page_size,
                                        media_uid=media_id_query, tag=tag_query)
    total_pages = max(1, (total + page_size - 1) // page_size)

    # Tags assignés par capture (chips sur les cartes) — une seule requête
    # "bulk" plutôt qu'une par capture (voir get_tags_for_captures).
    official_ids = [c['id'] for c in captures if c.get('source', 'official') == 'official']
    tags_by_capture = get_tags_for_captures(official_ids)
    for c in captures:
        c['tags'] = tags_by_capture.get(c['id'], []) if c.get('source', 'official') == 'official' else []
    distinct_tags = list_distinct_tag_labels()

    voter_token = request.cookies.get('voter_token', '')
    new_token = False
    if not voter_token:
        voter_token = secrets.token_hex(16)
        new_token = True

    voter_votes = get_voter_votes(voter_token)
    vote_enabled = get_setting('vote.enabled', '1') == '1'
    vote_cfg = _vote_cfg()

    # Lien vers l'upload invité (voir section "Upload invités" plus bas),
    # affiché dans la galerie uniquement si la fonctionnalité est activée
    # depuis /admin/guest-uploads — sans quoi la galerie reste inchangée.
    guest_upload_url = (url_for('guest_upload_page', token=guest_cfg['token'])
                        if guest_cfg['enabled'] and guest_cfg['token'] else None)

    resp = make_response(render_template(
        'gallery.html', captures=captures, sort=sort, kind=kind, config=CONFIG,
        email_cookie=email_cookie, gallery_text=resolve_dynamic_placeholders(get_setting('gallery_text', '')),
        page=page, total_pages=total_pages, total=total,
        vote_enabled=vote_enabled, voter_votes=voter_votes, vote_cfg=vote_cfg,
        guest_upload_url=guest_upload_url,
        guest_in_gallery=guest_in_gallery, source=source,
        media_id_query=media_id_query,
        tag_query=tag_query, distinct_tags=distinct_tags,
    ))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    if new_token:
        resp.set_cookie('voter_token', voter_token, max_age=365*24*3600,
                        samesite='Lax', httponly=True)
    return resp


@app.route('/qr.png')
def qr_png():
    url = build_gallery_url()
    return send_file(generate_qr_png(url), mimetype='image/png', download_name='gallery-qr.png')


@app.route('/api/settings/email', methods=['POST'])
def set_email():
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip()
    if email and CONFIG['emails'].get('enabled', True):
        save_email(email)
    resp = jsonify({'ok': True, 'email': email})
    max_age = int(CONFIG['emails'].get('cookie_max_age_days', 30)) * 24 * 3600
    resp.set_cookie(CONFIG['emails']['cookie_name'], email, max_age=max_age, samesite='Lax')
    return resp


# ── Médias ────────────────────────────────────────────────────────────────────

@app.route('/media/thumb/<path:filename>')
@require_media_auth
def media_thumb(filename):
    return send_from_directory(THUMBS_DIR, filename)


@app.route('/media/photo/<path:filename>')
@require_media_auth
def media_photo(filename):
    return send_from_directory(PHOTO_DIR, filename)


@app.route('/media/video/<path:filename>')
@require_media_auth
def media_video(filename):
    return send_from_directory(VIDEO_DIR, filename)


@app.route('/download/photo/<path:filename>')
@require_media_auth
def download_photo(filename):
    return send_from_directory(PHOTO_DIR, filename, as_attachment=True)


@app.route('/download/video/<path:filename>')
@require_media_auth
def download_video(filename):
    return send_from_directory(VIDEO_DIR, filename, as_attachment=True)


@app.route('/download/guest/<path:filename>')
@require_media_auth
def download_guest(filename):
    return send_from_directory(GUEST_UPLOAD_DIR, filename, as_attachment=True)


# ── Admin — général / tableau de bord ────────────────────────────────────────

def _dir_size_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob('*'):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ('o', 'Ko', 'Mo', 'Go'):
        if size < 1024:
            return f'{size:.0f} {unit}' if unit == 'o' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} To'


@app.route('/admin')
@require_admin_auth
def admin_home():
    folders = [
        ('Photos',            PHOTO_DIR),
        ('Photos brutes',     RAW_PHOTO_DIR),
        ('Vidéos',            VIDEO_DIR),
        ('Vidéos brutes',     RAW_VIDEO_DIR),
        ('Miniatures',        THUMBS_DIR),
        ('Uploads invités',   GUEST_UPLOAD_DIR),
        ('Exports',           EXPORTS_DIR),
    ]
    disk_rows = []
    total_bytes = 0
    for label, path in folders:
        n = _dir_size_bytes(path)
        total_bytes += n
        disk_rows.append({'label': label, 'bytes': n, 'human': _human_size(n)})

    try:
        free_bytes = shutil.disk_usage(BASE_DIR).free
    except Exception:
        free_bytes = None

    _, photo_total = list_captures(kind='photo', page_size=1)
    _, video_total = list_captures(kind='video', page_size=1)
    guest_pending  = len(list_guest_uploads('pending'))
    guest_approved = len(list_guest_uploads('approved'))

    return render_template(
        'admin_home.html', config=CONFIG,
        disk_rows=disk_rows,
        total_human=_human_size(total_bytes),
        free_human=_human_size(free_bytes) if free_bytes is not None else None,
        photo_total=photo_total, video_total=video_total,
        email_total=len(list_emails()),
        guest_pending=guest_pending, guest_approved=guest_approved,
        ffmpeg_ok=Path(FFMPEG_EXE).is_file() if FFMPEG_EXE.endswith('.exe') else True,
        printer_configured=bool(CONFIG.get('print', {}).get('printer_name', '').strip()),
        camera_device=get_setting('camera.device', '') or CONFIG.get('camera', {}).get('device', 0),
        tuiles=_admin_home_tuiles(), hidden_tuiles=_admin_hidden_native_pages(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


@app.route('/admin/dashboard/purge-raw', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_purge_raw():
    """Supprime les photos/vidéos brutes (sauvegardes pré-overlay, voir
    capture.photo.save_raw / capture.video.save_raw) — sans risque : ce sont
    des doublons des versions déjà publiées (avec cadre appliqué), jamais
    affichés dans la galerie."""
    removed = 0
    for d in (RAW_PHOTO_DIR, RAW_VIDEO_DIR):
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        logger.warning('Purge brut : suppression échouée : %s', f)
    logger.info('Purge admin : %d fichier(s) brut(s) supprimé(s).', removed)
    return redirect(url_for('admin_home', ok=f'{removed} fichier(s) brut(s) supprimé(s).'))


@app.route('/admin/dashboard/purge-old', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_purge_old():
    """Supprime les captures officielles (photo + vidéo) plus anciennes que
    N jours — irréversible, utilisé pour libérer de l'espace disque après un
    événement. Les uploads invités ne sont pas concernés (voir modération
    dédiée dans /admin/guest-uploads)."""
    raw_days = (request.form.get('days') or '').strip()
    if not raw_days.isdigit() or int(raw_days) < 1:
        return redirect(url_for('admin_home', err='Nombre de jours invalide.'))
    days = int(raw_days)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
    with closing(db_conn()) as conn:
        rows = conn.execute('SELECT id FROM captures WHERE created_at < ?', (cutoff,)).fetchall()
    count = 0
    for row in rows:
        cap = delete_capture(row['id'])
        if not cap:
            continue
        media_dir = PHOTO_DIR if cap['kind'] == 'photo' else VIDEO_DIR
        (media_dir / cap['filename']).unlink(missing_ok=True)
        if cap.get('thumb_filename'):
            (THUMBS_DIR / cap['thumb_filename']).unlink(missing_ok=True)
        count += 1
    logger.info('Purge admin : %d capture(s) de plus de %d jour(s) supprimée(s).', count, days)
    return redirect(url_for('admin_home', ok=f'{count} capture(s) supprimée(s) (plus de {days} jour(s)).'))


# ── Admin — captures (Point 15) ───────────────────────────────────────────────

@app.route('/admin/captures')
@require_admin_auth
def admin_captures():
    sort = request.args.get('sort', 'desc')
    kind = request.args.get('kind', '')
    page_size = 40
    page = max(1, int(request.args.get('page', 1)))
    captures, total = list_captures(sort, kind, page=page, page_size=page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    return render_template(
        'admin_captures.html', config=CONFIG, captures=captures,
        sort=sort, kind=kind, page=page, total_pages=total_pages, total=total,
        vote_cfg=_vote_cfg(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


@app.route('/admin/captures/<int:capture_id>/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_capture_delete(capture_id):
    cap = delete_capture(capture_id)
    if not cap:
        abort(404)
    media_dir = PHOTO_DIR if cap['kind'] == 'photo' else VIDEO_DIR
    try:
        (media_dir / cap['filename']).unlink(missing_ok=True)
    except Exception:
        logger.warning('Impossible de supprimer le fichier média : %s', cap['filename'])
    if cap.get('thumb_filename'):
        try:
            (THUMBS_DIR / cap['thumb_filename']).unlink(missing_ok=True)
        except Exception:
            pass
    logger.info('Capture #%d supprimée par l\'admin', capture_id)
    return redirect(url_for('admin_captures',
                            ok=f'Capture #{capture_id} supprimée.',
                            sort=request.form.get('sort', 'desc'),
                            kind=request.form.get('kind', ''),
                            page=request.form.get('page', 1)))


# ── Admin — archivage / nettoyage par plage de dates ─────────────────────────
# Deux outils distincts partageant la même page (/admin/archive) : export ZIP
# (médias + manifeste tags/votes/date) et suppression définitive, tous deux
# bornés par un intervalle [date+heure début, date+heure fin] saisi via deux
# <input type="datetime-local">.

def _parse_archive_range(form) -> tuple[str, str, str]:
    """Valide les champs range_start/range_end (format natif de
    <input type=datetime-local> : 'YYYY-MM-DDTHH:MM') et les convertit en
    bornes ISO 8601 comparables à captures.created_at ('YYYY-MM-DDTHH:MM:SS').
    La minute de fin est incluse en entier (:00 -> :59) pour que la borne
    saisie couvre bien toute cette minute. Retourne (start_iso, end_iso, err)
    — err est une chaîne non vide en cas de problème, les deux ISO valent
    alors ''."""
    raw_start = (form.get('range_start') or '').strip()
    raw_end   = (form.get('range_end') or '').strip()
    if not raw_start or not raw_end:
        return '', '', 'Sélectionnez une date de début et une date de fin.'
    try:
        start_dt = datetime.strptime(raw_start, '%Y-%m-%dT%H:%M')
        end_dt   = datetime.strptime(raw_end, '%Y-%m-%dT%H:%M')
    except ValueError:
        return '', '', 'Format de date invalide.'
    if start_dt > end_dt:
        return '', '', 'La date de début doit précéder (ou égaler) la date de fin.'
    return start_dt.strftime('%Y-%m-%dT%H:%M:00'), end_dt.strftime('%Y-%m-%dT%H:%M:59'), ''


# Bornes couvrant la totalité des dates plausibles pour created_at (format
# ISO 8601, comparaison lexicographique) — utilisées quand la case "Tout"
# est cochée, pour réutiliser telles quelles les fonctions écrites pour un
# intervalle borné (_build_archive_zip, list_captures_in_range...).
_ARCHIVE_RANGE_ALL = ('0000-01-01T00:00:00', '9999-12-31T23:59:59')


def _resolve_archive_range(form) -> tuple[str, str, str]:
    """Résout l'intervalle demandé par le formulaire : la totalité des
    captures si la case 'Tout' (range_all) est cochée — auquel cas les
    champs de dates sont ignorés, même vides —, sinon les deux champs
    datetime-local (voir _parse_archive_range)."""
    if form.get('range_all'):
        return _ARCHIVE_RANGE_ALL[0], _ARCHIVE_RANGE_ALL[1], ''
    return _parse_archive_range(form)


def _build_archive_zip(start_iso: str, end_iso: str, include_guests: bool = True):
    """Construit une archive ZIP (fichiers média + manifeste manifest.csv/
    .json avec source, tags, score de votes, date/heure de capture) pour
    toutes les captures officielles de [start_iso, end_iso], sous media/,
    et — si include_guests — les uploads invités (quel que soit leur
    statut) de la même plage, sous guest_media/ (fichiers stockés dans
    GUEST_UPLOAD_DIR, définitions plus bas dans ce fichier). Les uploads
    invités n'ont pas de tags (fonctionnalité réservée aux captures
    officielles côté kiosque) : colonne laissée vide pour ces lignes.
    Écrite dans EXPORTS_DIR (nom horodaté, jamais nettoyée automatiquement
    — même logique que emails.csv/.json). Retourne (zip_path, nb_médias)."""
    captures = list_captures_in_range(start_iso, end_iso)
    tags_map = get_tags_for_captures([c['id'] for c in captures])
    guests = list_guest_uploads_in_range(start_iso, end_iso) if include_guests else []

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    zip_path = EXPORTS_DIR / f'archive-{stamp}.zip'

    fieldnames = ['id', 'source', 'status', 'media_uid', 'kind', 'filename', 'created_at', 'vote_score', 'tags']
    manifest_rows = []
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cap in captures:
            media_dir = PHOTO_DIR if cap['kind'] == 'photo' else VIDEO_DIR
            src = media_dir / cap['filename']
            if src.is_file():
                zf.write(src, arcname=f"media/{cap['filename']}")
            else:
                logger.warning('Archive admin : fichier introuvable, ignoré : %s', src)
            manifest_rows.append({
                'id':          cap['id'],
                'source':      'official',
                'status':      '',
                'media_uid':   cap.get('media_uid') or '',
                'kind':        cap['kind'],
                'filename':    cap['filename'],
                'created_at':  cap['created_at'],
                'vote_score':  cap.get('vote_score', 0),
                'tags':        ', '.join(tags_map.get(cap['id'], [])),
            })

        for up in guests:
            src = GUEST_UPLOAD_DIR / up['filename']
            if src.is_file():
                zf.write(src, arcname=f"guest_media/{up['filename']}")
            else:
                logger.warning('Archive admin : fichier invité introuvable, ignoré : %s', src)
            manifest_rows.append({
                'id':          up['id'],
                'source':      'guest',
                'status':      up.get('status') or '',
                'media_uid':   up.get('media_uid') or '',
                'kind':        up['kind'],
                'filename':    up['filename'],
                'created_at':  up['created_at'],
                'vote_score':  up.get('vote_score', 0),
                'tags':        '',
            })

        manifest_csv = io.StringIO()
        writer = csv.DictWriter(manifest_csv, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(manifest_rows)
        zf.writestr('manifest.csv', manifest_csv.getvalue())
        zf.writestr('manifest.json', json.dumps(manifest_rows, ensure_ascii=False, indent=2))

    return zip_path, len(manifest_rows)


@app.route('/admin/archive')
@require_admin_auth
def admin_archive():
    blocks, block_context = _admin_render_blocks('archive')
    return render_template(
        'admin_archive.html', config=CONFIG,
        blocks=blocks, current_page='archive', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('archive', 'Archive & nettoyage'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/archive/export', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_archive_export():
    start_iso, end_iso, err = _resolve_archive_range(request.form)
    if err:
        return _admin_block_redirect('archive_export', err=err)
    include_guests = bool(request.form.get('include_guests'))
    zip_path, count = _build_archive_zip(start_iso, end_iso, include_guests)
    if count == 0:
        zip_path.unlink(missing_ok=True)
        return _admin_block_redirect('archive_export', err='Aucun média dans cet intervalle.')
    logger.info('Archive admin : %d média(s) exporté(s) (%s -> %s, invités %s).',
                count, start_iso, end_iso, 'inclus' if include_guests else 'exclus')
    return send_file(zip_path, mimetype='application/zip', as_attachment=True, download_name=zip_path.name)


@app.route('/admin/archive/cleanup', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_archive_cleanup():
    start_iso, end_iso, err = _resolve_archive_range(request.form)
    if err:
        return _admin_block_redirect('archive_cleanup', err=err)
    if not request.form.get('confirm'):
        return _admin_block_redirect('archive_cleanup', err='Cochez la case de confirmation pour supprimer.')

    include_guests = bool(request.form.get('include_guests'))

    captures = list_captures_in_range(start_iso, end_iso)
    count = 0
    for cap in captures:
        deleted = delete_capture(cap['id'])
        if not deleted:
            continue
        media_dir = PHOTO_DIR if deleted['kind'] == 'photo' else VIDEO_DIR
        try:
            (media_dir / deleted['filename']).unlink(missing_ok=True)
        except Exception:
            logger.warning('Nettoyage admin : suppression fichier échouée : %s', deleted['filename'])
        if deleted.get('thumb_filename'):
            (THUMBS_DIR / deleted['thumb_filename']).unlink(missing_ok=True)
        count += 1

    guest_count = 0
    if include_guests:
        for up in list_guest_uploads_in_range(start_iso, end_iso):
            deleted_up = delete_guest_upload_db(up['id'])
            if not deleted_up:
                continue
            try:
                (GUEST_UPLOAD_DIR / deleted_up['filename']).unlink(missing_ok=True)
            except Exception:
                logger.warning('Nettoyage admin : suppression fichier invité échouée : %s', deleted_up['filename'])
            if deleted_up.get('thumb_filename'):
                (THUMBS_DIR / deleted_up['thumb_filename']).unlink(missing_ok=True)
            guest_count += 1

    logger.info('Nettoyage admin : %d capture(s) + %d média(s) invité(s) supprimé(s) (%s -> %s).',
                count, guest_count, start_iso, end_iso)
    msg = f'{count} capture(s) supprimée(s)'
    if include_guests:
        msg += f' + {guest_count} média(s) invité(s) supprimé(s)'
    msg += ' (tags et votes inclus).'
    return _admin_block_redirect('archive_cleanup', ok=msg)


# ── Admin — cadres ────────────────────────────────────────────────────────────

@app.route('/admin/exports/emails.csv')
@require_admin_auth
def export_emails_csv():
    csv_path, _ = export_emails_files()
    return send_file(csv_path, mimetype='text/csv', as_attachment=True, download_name='emails.csv')


@app.route('/admin/exports/emails.json')
@require_admin_auth
def export_emails_json():
    _, json_path = export_emails_files()
    return send_file(json_path, mimetype='application/json', as_attachment=True, download_name='emails.json')


@app.route('/admin/frames/<frame_id>/set-default', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_frame_set_default(frame_id):
    if not get_frame_by_id_db(frame_id):
        abort(404)
    with closing(db_conn()) as conn:
        conn.execute('UPDATE frames SET is_default = 0')
        conn.execute('UPDATE frames SET is_default = 1 WHERE id = ?', (frame_id,))
        conn.commit()
    return _admin_redirect(success=f'Cadre "{frame_id}" défini par défaut.')


@app.route('/admin/welcome-frame', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_welcome_frame_upload():
    file = request.files.get('welcome_frame')
    if not file or not file.filename:
        return _admin_block_redirect('frames_welcome', err='Aucun fichier sélectionné.')
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_OVERLAY_EXT:
        return _admin_block_redirect('frames_welcome', err='Le cadre d\'accueil doit être un PNG.')
    filename = f'welcome-frame{ext}'
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    file.save(str(FRAMES_DIR / filename))
    set_setting('welcome_frame_filename', filename)
    return _admin_block_redirect('frames_welcome', ok='Cadre d\'accueil mis à jour.')


@app.route('/admin/welcome-frame/remove', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_welcome_frame_remove():
    fn = get_setting('welcome_frame_filename', '')
    if fn:
        (FRAMES_DIR / fn).unlink(missing_ok=True)
        set_setting('welcome_frame_filename', '')
    return _admin_block_redirect('frames_welcome', ok='Cadre d\'accueil supprimé.')


@app.route('/admin/texts', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_texts():
    if request.method == 'POST':
        for key in TEXT_DEFAULTS:
            value = (request.form.get(f'text_{key}') or '').strip()
            set_setting(f'text.{key}', value)

        set_setting('ui.hide_print_button', '1' if request.form.get('hide_print_button') else '0')
        raw_rfs = (request.form.get('bottom_right_font_size') or '').strip()
        if raw_rfs.isdigit() and int(raw_rfs) > 0:
            set_setting('ui.right_message_font_size_px', raw_rfs)
        raw_qr = (request.form.get('bottom_qr_size') or '').strip()
        if raw_qr.isdigit() and int(raw_qr) > 0:
            set_setting('ui.qr_size_px', raw_qr)

        raw_tbh = (request.form.get('top_bar_height') or '').strip()
        if raw_tbh.isdigit() and int(raw_tbh) > 0:
            set_setting('ui.top_bar_height_px', raw_tbh)
        top_bar_color = (request.form.get('top_bar_color') or '').strip()
        if re.match(r'^#[0-9a-fA-F]{6}$', top_bar_color):
            set_setting('ui.top_bar_color', top_bar_color)

        version_label = (request.form.get('about_version_label') or '').strip()
        set_setting('about.version_label', version_label)
        info_text = (request.form.get('about_info_text') or '').strip()
        set_setting('about.info_text', info_text)

        ps_text = (request.form.get('photostrip_step_text') or '{n}').strip()
        set_setting('photostrip_step.text', ps_text or '{n}')
        ps_font = (request.form.get('photostrip_step_font') or '').strip()
        set_setting('photostrip_step.font', ps_font or _PROMO_FONTS[0][0])
        raw_ps_size = (request.form.get('photostrip_step_font_size') or '160').strip()
        set_setting('photostrip_step.font_size', str(max(20, int(raw_ps_size))) if raw_ps_size.isdigit() else '160')
        ps_position = (request.form.get('photostrip_step_position') or 'center').strip()
        if ps_position not in _PHOTOSTRIP_STEP_POSITIONS:
            ps_position = 'center'
        set_setting('photostrip_step.position', ps_position)

        return redirect(url_for('admin_texts', ok='Paramètres mis à jour.'))
    blocks, block_context = _admin_render_blocks('texts')
    return render_template('admin_texts.html', config=CONFIG,
                           texts=get_ui_texts(),
                           defaults=TEXT_DEFAULTS,
                           hide_print_button=get_setting('ui.hide_print_button', '0') == '1',
                           bottom_bar_sizes=get_bottom_bar_sizes(),
                           top_bar=get_top_bar_settings(),
                           about=_about_settings(),
                           photostrip_step=_photostrip_step_settings(),
                           photostrip_step_fonts=_all_fonts(),
                           photostrip_step_positions=_PHOTOSTRIP_STEP_POSITION_LABELS,
                           blocks=blocks, current_page='texts', admin_pages=_admin_all_pages(),
                           page_label=_admin_page_label('texts', "Textes de l'interface"),
                           alert_success=request.args.get('ok'),
                           alert_error=request.args.get('err'),
                           **block_context)


@app.route('/admin/texts/fonts/upload', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_font_upload():
    """Ajoute une police personnalisée (.ttf/.otf), immédiatement disponible
    dans tous les sélecteurs de police de l'app — voir _all_fonts()."""
    file = request.files.get('font_file')
    if not file or not file.filename:
        return _admin_block_redirect('texts_custom_fonts', err='Aucun fichier sélectionné.')
    ext = Path(file.filename).suffix.lower()
    if ext not in _CUSTOM_FONT_ALLOWED_EXT:
        return _admin_block_redirect('texts_custom_fonts', err='Format non supporté (TTF ou OTF uniquement).')
    label = (request.form.get('font_label') or '').strip() or Path(file.filename).stem
    label = label[:80]
    CUSTOM_FONTS_DIR.mkdir(parents=True, exist_ok=True)
    base_family = _slugify_font_family(label)
    safe_filename = f'{base_family}-{int(datetime.now().timestamp() * 1000)}{ext}'
    file.save(str(CUSTOM_FONTS_DIR / safe_filename))
    try:
        ImageFont.truetype(str(CUSTOM_FONTS_DIR / safe_filename), 24)
    except Exception:
        (CUSTOM_FONTS_DIR / safe_filename).unlink(missing_ok=True)
        return _admin_block_redirect('texts_custom_fonts', err='Fichier de police invalide ou illisible.')
    create_custom_font(label, base_family, safe_filename)
    return _admin_block_redirect('texts_custom_fonts', ok=f'Police « {label} » ajoutée.')


@app.route('/admin/texts/fonts/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_font_delete():
    """Retire une police personnalisée (fichier + entrée DB). Les fonctions
    qui l'utilisaient actuellement basculent silencieusement sur la police
    par défaut de Pillow/du navigateur, comme pour toute police invalide —
    aucune erreur bloquante, à corriger manuellement si besoin depuis la
    page concernée."""
    try:
        font_id = int(request.form.get('font_id', ''))
    except ValueError:
        return _admin_block_redirect('texts_custom_fonts', err='Police invalide.')
    font = delete_custom_font_db(font_id)
    if not font:
        return _admin_block_redirect('texts_custom_fonts', err='Police introuvable.')
    (CUSTOM_FONTS_DIR / font['filename']).unlink(missing_ok=True)
    _qr_live_burn_font_cache.clear()
    return _admin_block_redirect('texts_custom_fonts', ok=f"Police « {font['label']} » supprimée.")


@app.route('/fonts.css')
def custom_fonts_css():
    """Feuille de style dynamique déclarant une règle @font-face pour
    chaque police personnalisée ajoutée depuis /admin/texts — incluse par
    toutes les pages (kiosque + back office) qui affichent ou proposent une
    police afin que le navigateur puisse effectivement la charger et
    l'afficher (les 6 polices intégrées, elles, n'en ont pas besoin :
    system-ui/Georgia/etc. sont déjà connues du navigateur). Pas
    d'authentification requise : lue par l'interface principale du kiosque,
    non protégée par mot de passe admin ; ne révèle rien de sensible (juste
    des noms de fichiers de polices)."""
    parts = []
    for row in list_custom_fonts():
        url = url_for('static', filename=f"fonts/{row['filename']}")
        parts.append(f"@font-face {{ font-family: '{row['family']}'; src: url('{url}'); font-display: swap; }}")
    return Response('\n'.join(parts), mimetype='text/css')


@app.route('/admin/gallery')
@require_admin_auth
def admin_gallery():
    """Tuile « Galerie » du back office. Pas de POST ici : le seul bloc de
    cette page poste vers sa propre route dédiée ci-dessous."""
    blocks, block_context = _admin_render_blocks('gallery')
    return render_template('admin_gallery.html', config=CONFIG,
                           blocks=blocks, current_page='gallery', admin_pages=_admin_all_pages(),
                           page_label=_admin_page_label('gallery', 'Administration galerie'),
                           alert_success=request.args.get('ok'),
                           alert_error=request.args.get('err'),
                           **block_context)


@app.route('/admin/gallery/text', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_gallery_set_text():
    text = (request.form.get('gallery_text') or '').strip()
    set_setting('gallery_text', text)
    return _admin_block_redirect('gallery_text', ok='Texte mis à jour.')


def _admin_system_camera_values() -> dict:
    def _cfg_int(key, default):
        raw = get_setting(f'camera.{key}', '')
        return int(raw) if raw.isdigit() else int(CONFIG['camera'].get(key, default))
    return {
        'device':         _cfg_int('device', 0),
        'width':          _cfg_int('width', 1920),
        'height':         _cfg_int('height', 1080),
        'rotation':       _cfg_int('rotation', 0),
        'preview_mirror': (get_setting('camera.preview_mirror', '') or ('1' if CONFIG['camera'].get('preview_mirror', False) else '0')) == '1',
    }


def _admin_system_screen_values() -> dict:
    ui_cfg = CONFIG.get('ui', {})
    _kiosk_mode_raw = get_setting('ui.kiosk_mode', '')
    _kiosk_screen_raw = get_setting('ui.kiosk_screen', '')
    return {
        'kiosk_mode':   (_kiosk_mode_raw == '1') if _kiosk_mode_raw in ('0', '1') else bool(ui_cfg.get('kiosk_mode', True)),
        'kiosk_screen': int(_kiosk_screen_raw) if _kiosk_screen_raw.isdigit() else int(ui_cfg.get('kiosk_screen', 0)),
    }


@app.route('/admin/system')
@require_admin_auth
def admin_system():
    """Tuile « Caméra & écran » du back office. Pas de POST ici : chaque
    section (caméra, écran/kiosque, déverrouillage plein écran) poste vers
    sa propre route dédiée ci-dessous — voir commentaire de admin_application
    plus bas sur le système de blocs déplaçables."""
    blocks, block_context = _admin_render_blocks('system')
    return render_template(
        'admin_system.html', config=CONFIG,
        blocks=blocks, current_page='system', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('system', 'Caméra & écran'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/system/camera', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_system_set_camera():
    """Réglages caméra — validation légère avant écriture, on ignore les
    champs vides ou invalides plutôt que d'écrire une valeur incohérente.
    Appliqué à chaud (reset_camera()), contrairement à l'écran/kiosque
    ci-dessous qui n'est pris en compte qu'au prochain lancement de
    run.bat."""
    raw_device = (request.form.get('camera_device') or '').strip()
    if raw_device.isdigit():
        set_setting('camera.device', raw_device)

    raw_width = (request.form.get('camera_width') or '').strip()
    if raw_width.isdigit() and int(raw_width) > 0:
        set_setting('camera.width', raw_width)

    raw_height = (request.form.get('camera_height') or '').strip()
    if raw_height.isdigit() and int(raw_height) > 0:
        set_setting('camera.height', raw_height)

    raw_rotation = (request.form.get('camera_rotation') or '0').strip()
    if raw_rotation in ('0', '90', '180', '270'):
        set_setting('camera.rotation', raw_rotation)

    set_setting('camera.preview_mirror', '1' if request.form.get('camera_preview_mirror') else '0')

    reset_camera()
    return _admin_block_redirect('system_camera', ok='Réglages caméra enregistrés. Caméra relancée avec les nouvelles valeurs.')


@app.route('/admin/system/screen', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_system_set_screen():
    """Écran / mode kiosque — appliqué au prochain lancement de run.bat
    (pas d'effet immédiat, contrairement à la caméra ci-dessus)."""
    set_setting('ui.kiosk_mode', '1' if request.form.get('ui_kiosk_mode') else '0')
    raw_screen = (request.form.get('ui_kiosk_screen') or '0').strip()
    set_setting('ui.kiosk_screen', raw_screen if raw_screen.isdigit() else '0')
    return _admin_block_redirect('system_screen', ok='Réglages écran enregistrés (pris en compte au prochain lancement de run.bat).')


@app.route('/admin/system/kiosk_unlock', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_system_set_kiosk_unlock():
    """Déverrouillage plein écran (interface principale) — champ vidé =
    retour à la valeur par défaut (voir _kiosk_unlock_settings())."""
    raw_pin = re.sub(r'\D', '', request.form.get('kiosk_unlock_pin') or '')[:8]
    set_setting('kiosk.unlock_pin', raw_pin)
    raw_taps = (request.form.get('kiosk_unlock_taps') or '').strip()
    set_setting('kiosk.unlock_taps', raw_taps if raw_taps.isdigit() and 2 <= int(raw_taps) <= 15 else '')
    return _admin_block_redirect('system_kiosk_unlock', ok='Réglage de déverrouillage enregistré.')


# ── Blocs admin repliables / réorganisables / déplaçables entre tuiles ───────
# Chaque bloc-fonction (une section .admin-block, ex. « Fond d'écran
# Windows ») peut être réduit à son titre, réordonné dans sa page, ou
# déplacé vers une autre tuile — voir /admin/ui/... ci-dessous et
# static/admin-blocks.js. Portée volontairement limitée pour l'instant aux
# blocs qui ont DÉJÀ leur propre <form> et leur propre route dédiée
# (condition nécessaire pour qu'un bloc déplacé continue de fonctionner :
# son formulaire poste toujours vers la même route, peu importe la page qui
# l'affiche) — les tuiles dont plusieurs sections partagent un unique
# <form>/une unique route (Caméra & écran, une partie de Textes...)
# rejoindront ce système au fur et à mesure qu'elles seront scindées en
# routes indépendantes, par petites livraisons successives.
#
# Un bloc est identifié par un id stable (clé de _ADMIN_BLOCKS), rendu
# depuis un fragment de template dédié (templates/blocks/<id>.html) inclus
# par la page qui le possède actuellement — voir _admin_render_blocks.
# Contrairement à _PROMO_FONTS, _ADMIN_BLOCKS n'a pas besoin d'être défini
# avant les fonctions ci-dessous : il n'est lu qu'au moment des requêtes.
_ADMIN_BLOCKS = {
    'app_fullscreen':      {'title': "Plein écran — bascule immédiate",         'default_page': 'application', 'template': 'blocks/app_fullscreen.html',      'context_fn': None},
    'app_restart':         {'title': "Redémarrage de l'application",           'default_page': 'application', 'template': 'blocks/app_restart.html',         'context_fn': None},
    'app_restart_update':  {'title': "Redémarrage avec mise à jour (Git)",      'default_page': 'application', 'template': 'blocks/app_restart_update.html',  'context_fn': None},
    'app_autostart':       {'title': "Démarrage automatique Windows",           'default_page': 'application', 'template': 'blocks/app_autostart.html',       'context_fn': '_block_ctx_app_autostart'},
    'app_wallpaper':       {'title': "Fond d'écran Windows",                    'default_page': 'media',       'template': 'blocks/app_wallpaper.html',       'context_fn': '_block_ctx_app_wallpaper'},
    'app_idle_timer':      {'title': "Mise en veille automatique",              'default_page': 'application', 'template': 'blocks/app_idle_timer.html',      'context_fn': '_block_ctx_app_idle_timer'},
    'app_network_info':    {'title': "Informations réseau (interface principale)", 'default_page': 'application', 'template': 'blocks/app_network_info.html', 'context_fn': '_block_ctx_app_network_info'},
    'access_password':     {'title': "Mot de passe du back office",             'default_page': 'access',      'template': 'blocks/access_password.html',     'context_fn': '_block_ctx_access_password'},
    'access_clear_password': {'title': "Supprimer la protection",               'default_page': 'access',      'template': 'blocks/access_clear_password.html', 'context_fn': '_block_ctx_access_password'},
    'archive_export':      {'title': "Archiver un intervalle",                  'default_page': 'archive',     'template': 'blocks/archive_export.html',      'context_fn': None},
    'archive_cleanup':     {'title': "Nettoyer un intervalle",                  'default_page': 'archive',     'template': 'blocks/archive_cleanup.html',     'context_fn': None, 'variant': 'danger'},
    'texts_custom_fonts':  {'title': "Polices personnalisées",                  'default_page': 'texts',       'template': 'blocks/texts_custom_fonts.html',  'context_fn': '_block_ctx_texts_custom_fonts'},
    'system_camera':       {'title': "Caméra",                                  'default_page': 'system',      'template': 'blocks/system_camera.html',       'context_fn': '_block_ctx_system_camera'},
    'system_screen':       {'title': "Écran / mode kiosque",                    'default_page': 'system',      'template': 'blocks/system_screen.html',       'context_fn': '_block_ctx_system_screen'},
    'system_kiosk_unlock': {'title': "Déverrouillage plein écran (interface principale)", 'default_page': 'system', 'template': 'blocks/system_kiosk_unlock.html', 'context_fn': '_block_ctx_system_kiosk_unlock'},
    'gallery_text':        {'title': "Texte d'introduction",                    'default_page': 'gallery',     'template': 'blocks/gallery_text.html',        'context_fn': '_block_ctx_gallery_text'},
    'frames_welcome':      {'title': "Cadre d'accueil",                         'default_page': 'frames',      'template': 'blocks/frames_welcome.html',      'context_fn': '_block_ctx_frames_welcome'},
    'frames_import':       {'title': "Importer un pack",                        'default_page': 'frames',      'template': 'blocks/frames_import.html',       'context_fn': None},
    'frames_new':          {'title': "Ajouter un cadre",                        'default_page': 'frames',      'template': 'blocks/frames_new.html',          'context_fn': None},
    'votes_activation':    {'title': "Activation",                              'default_page': 'votes',       'template': 'blocks/votes_activation.html',    'context_fn': '_block_ctx_votes_activation'},
    'votes_thresholds_colors': {'title': "Seuils & couleurs",                   'default_page': 'votes',       'template': 'blocks/votes_thresholds_colors.html', 'context_fn': '_block_ctx_votes_thresholds_colors'},
    'buttons_style':       {'title': "Style des boutons",                       'default_page': 'buttons',     'template': 'blocks/buttons_style.html',       'context_fn': '_block_ctx_buttons_style'},
    'tags_settings':       {'title': "Activation & règles du tag libre",        'default_page': 'tags',        'template': 'blocks/tags_settings.html',       'context_fn': '_block_ctx_tags_settings'},
    'tags_media_id':       {'title': "ID unique par média",                     'default_page': 'tags',        'template': 'blocks/tags_media_id.html',       'context_fn': '_block_ctx_tags_media_id'},
    'tags_display':        {'title': "Affichage sur /bestof",                   'default_page': 'tags',        'template': 'blocks/tags_display.html',        'context_fn': '_block_ctx_tags_display'},
    'slideshow_settings':      {'title': "Paramètres",                          'default_page': 'slideshow',   'template': 'blocks/slideshow_settings.html',      'context_fn': '_block_ctx_slideshow_settings'},
    'screensaver_settings':    {'title': "Paramètres",                          'default_page': 'screensaver', 'template': 'blocks/screensaver_settings.html',    'context_fn': '_block_ctx_screensaver_settings'},
    'guest_uploads_settings':   {'title': "Paramètres",                         'default_page': 'guest_uploads', 'template': 'blocks/guest_uploads_settings.html',   'context_fn': '_block_ctx_guest_uploads_settings'},
    'guest_uploads_share_link': {'title': "Lien de partage",                    'default_page': 'guest_uploads', 'template': 'blocks/guest_uploads_share_link.html', 'context_fn': '_block_ctx_guest_uploads_share_link'},
    'guest_codes_code_settings':    {'title': "Réglages",                                    'default_page': 'guest_codes', 'template': 'blocks/guest_codes_code_settings.html',    'context_fn': '_block_ctx_guest_codes_code_settings'},
    'guest_codes_qr_export':        {'title': "Génération de QR-codes imprimables",          'default_page': 'guest_codes', 'template': 'blocks/guest_codes_qr_export.html',        'context_fn': '_block_ctx_guest_codes_qr_export'},
    'guest_codes_purge_date':       {'title': "Purger par plage de dates",                   'default_page': 'guest_codes', 'template': 'blocks/guest_codes_purge_date.html',       'context_fn': None},
    'guest_codes_purge_first_n':    {'title': "Purger les N premiers",                       'default_page': 'guest_codes', 'template': 'blocks/guest_codes_purge_first_n.html',    'context_fn': '_block_ctx_guest_codes_purge_first_n'},
    'guest_codes_qrcode_settings':  {'title': "Add-on — Détection de QR-codes",              'default_page': 'guest_codes', 'template': 'blocks/guest_codes_qrcode_settings.html',  'context_fn': '_block_ctx_guest_codes_qrcode_settings'},
    'guest_codes_qr_live':          {'title': "Apparence du QR-code affiché en direct",      'default_page': 'guest_codes', 'template': 'blocks/guest_codes_qr_live.html',          'context_fn': '_block_ctx_guest_codes_qr_live'},
}
# (slug, libellé affiché dans le menu « Déplacer vers », endpoint Flask) —
# seulement les tuiles qui exécutent déjà la boucle de rendu de blocs
# (_admin_render_blocks) ci-dessous peuvent être une destination : un bloc
# déplacé vers une tuile qui ne connaît pas ce système resterait invisible.
_ADMIN_PAGES = [
    ('application', 'Application',          'admin_application'),
    ('access',      'Admin',                'admin_access'),
    ('archive',     'Archive & nettoyage',  'admin_archive'),
    ('texts',       'Textes',               'admin_texts'),
    ('system',      'Caméra & écran',       'admin_system'),
    ('gallery',     'Galerie',              'admin_gallery'),
    ('frames',      'Cadres',               'admin_frames'),
    ('votes',       'Votes',                'admin_votes'),
    ('buttons',     'Boutons',              'admin_buttons'),
    ('tags',        'Tags & ID média',      'admin_tags'),
    ('media',       'Médiathèque',          'admin_media'),
    ('slideshow',   'Slideshow Best Of',    'admin_slideshow'),
    ('screensaver', 'Écran de veille',      'admin_screensaver'),
    ('guest_uploads', 'Upload invités',     'admin_guest_uploads'),
    ('guest_codes',   'Codes invités',      'admin_guest_codes'),
]
_ADMIN_PAGE_SLUGS = {slug for slug, _label, _endpoint in _ADMIN_PAGES}

# Description par défaut de chaque tuile native (texte actuellement en dur
# dans admin_home.html avant la vague 11) — sert de valeur de repli à
# ui.page_desc.<id> tant que l'admin n'a pas renommé la tuile. Purement
# affiché sur /admin, aucune logique n'en dépend.
_ADMIN_PAGE_DESCRIPTIONS = {
    'application':    "Plein écran, redémarrage de l'application, démarrage automatique Windows",
    'access':         "Mot de passe unique protégeant l'accès au back office",
    'archive':        "Exporter en ZIP (médias, tags, votes, dates) ou supprimer par plage de dates",
    'texts':          "Boutons, messages et libellés de l'interface kiosque",
    'system':         "Choix de la caméra, résolution, rotation, miroir, écran kiosque",
    'gallery':        "Texte d'introduction affiché dans la galerie",
    'frames':         "Cadres de capture, cadre d'accueil, cadre par défaut",
    'votes':          "Activer les votes sur la galerie, couleurs et seuils du gradient",
    'buttons':        "Forme, police et taille communes ; couleur et graisse propres à chaque bouton",
    'tags':           "Tags prédéfinis/libre sur les captures, ID unique recherchable et affichable",
    'media':          "Fond d'écran Windows, images intermédiaires du diaporama, images de l'écran de veille",
    'slideshow':      "Diaporama public /bestof — type, délai, ordre, dates, pages promo",
    'screensaver':    "Diaporama plein écran après inactivité sur l'accueil — délai",
    'guest_uploads':  "Lien de partage /share, modération, quota — alimente le best-of et/ou la galerie",
    'guest_codes':    "Codes numériques associés à un texte ; détection, apparence et incrustation QR-code",
}

# Tuiles natives dont le contenu n'est JAMAIS entièrement composé de blocs
# (liste CRUD, upload, formulaire fixe...) — voir chaque template pour le
# détail. Ne peuvent donc jamais être "vides" et ne sont jamais
# supprimables/masquables, quel que soit l'état de leurs blocs — voir
# _admin_page_deletable. admin_captures/admin_emails ne sont volontairement
# PAS dans ce registre du tout (jamais dans _ADMIN_PAGES) : ce sont des
# pages de contenu pur, hors système de blocs et hors CRUD de tuiles,
# comme pour le reste de la migration blocs.
_ADMIN_NATIVE_ALWAYS_NONEMPTY = {
    'texts', 'frames', 'tags', 'slideshow', 'media',
    'guest_uploads', 'guest_codes',
}


def _admin_page_label(page_id: str, default: str = '') -> str:
    return get_setting(f'ui.page_label.{page_id}', default)


def _admin_page_desc(page_id: str, default: str = '') -> str:
    return get_setting(f'ui.page_desc.{page_id}', default)


def _admin_custom_page_ids() -> list:
    raw = get_setting('ui.custom_pages', '')
    try:
        return json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []


def _admin_all_page_ids() -> list:
    """Tous les ids de tuiles valides (natives + personnalisées), SANS
    filtrer les masquées — c'est le référentiel de validation (move_block,
    /admin/ui/block_order/tuile_order, résolution d'URL) : une tuile
    native masquée reste une cible valide pour un bloc qui y est déjà
    assigné, seulement invisible sur /admin et dans les menus "Déplacer
    vers" (voir _admin_all_pages, qui filtre les masquées)."""
    return [slug for slug, _label, _endpoint in _ADMIN_PAGES] + _admin_custom_page_ids()


def _admin_page_hidden(slug: str) -> bool:
    return get_setting(f'ui.page_hidden.{slug}', '0') == '1'


def _admin_tuile_order(candidate_ids: list) -> list:
    """Ordre d'affichage des TUILES elles-mêmes sur /admin — même principe
    que _admin_page_order ci-dessous pour les blocs à l'intérieur d'une
    page, mais appliqué au registre des tuiles (clé ui.tuile_order)."""
    raw = get_setting('ui.tuile_order', '')
    try:
        stored = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        stored = []
    ordered = [p for p in stored if p in candidate_ids]
    ordered += [p for p in candidate_ids if p not in ordered]
    return ordered


def _admin_all_pages(include_hidden: bool = False) -> list:
    """(id, libellé, endpoint_ou_None) pour chaque tuile gérée (natives +
    personnalisées), libellés résolus via _admin_page_label (repli sur le
    libellé natif d'origine, ou "Tuile sans nom" pour une personnalisée
    dont le libellé aurait été vidé), ordonnés selon ui.tuile_order.
    Remplace _ADMIN_PAGES en dur comme valeur passée aux templates
    (admin_pages=...) partout où une tuile est référencée : menus
    "Déplacer vers" de chaque page à blocs, et grille de /admin. Les
    tuiles natives masquées (voir _admin_page_hidden) sont exclues sauf
    include_hidden=True — jamais le cas pour une tuile personnalisée
    (supprimée = retirée du registre pour de vrai, jamais "masquée",
    voir _admin_page_deletable / la route /admin/tuiles/delete)."""
    items = []
    for slug, default_label, endpoint in _ADMIN_PAGES:
        if not include_hidden and _admin_page_hidden(slug):
            continue
        items.append((slug, _admin_page_label(slug, default_label), endpoint))
    for page_id in _admin_custom_page_ids():
        items.append((page_id, _admin_page_label(page_id, 'Tuile sans nom'), None))
    by_id = {p[0]: p for p in items}
    ordered_ids = _admin_tuile_order(list(by_id.keys()))
    return [by_id[pid] for pid in ordered_ids]


def _admin_home_tuiles() -> list:
    """Version enrichie de _admin_all_pages() pour la grille réordonnable de
    /admin : ajoute description, URL déjà résolue et éligibilité à la
    suppression (voir _admin_page_deletable) — inutile pour les menus
    "Déplacer vers" des autres pages (qui n'utilisent que id/libellé),
    donc gardé séparé plutôt que d'alourdir _admin_all_pages()."""
    result = []
    for page_id, label, _endpoint in _admin_all_pages():
        default_desc = _ADMIN_PAGE_DESCRIPTIONS.get(page_id, '')
        deletable, _reason = _admin_page_deletable(page_id)
        result.append({
            'id': page_id,
            'label': label,
            'description': _admin_page_desc(page_id, default_desc),
            'url': _admin_page_url(page_id),
            'deletable': deletable,
            'native': page_id in _ADMIN_PAGE_SLUGS,
        })
    return result


def _admin_hidden_native_pages() -> list:
    """Tuiles natives actuellement masquées (voir /admin/tuiles/delete) —
    alimente le panneau "Tuiles masquées" de /admin, affiché seulement
    s'il y en a."""
    return [{'id': slug, 'label': _admin_page_label(slug, default_label)}
            for slug, default_label, _endpoint in _ADMIN_PAGES if _admin_page_hidden(slug)]


def _admin_block_page(block_id: str) -> str:
    page = get_setting(f'ui.block_page.{block_id}', _ADMIN_BLOCKS[block_id]['default_page'])
    # Filet de sécurité (vague 11) : si la page enregistrée n'existe plus
    # (tuile personnalisée supprimée entre-temps, données obsolètes),
    # retombe sur la page d'origine du bloc plutôt que de renvoyer un id
    # invalide — même esprit que le repli déjà présent ci-dessous dans
    # _admin_block_redirect.
    if page not in _admin_all_page_ids():
        return _ADMIN_BLOCKS[block_id]['default_page']
    return page


_ADMIN_PAGE_ENDPOINTS = {slug: endpoint for slug, _label, endpoint in _ADMIN_PAGES}


def _admin_page_url(page_id: str, **kwargs) -> str:
    """Résout l'URL de n'importe quelle tuile (native ou personnalisée) à
    partir de son id — natif : route dédiée existante (_ADMIN_PAGE_ENDPOINTS,
    inchangé) ; personnalisée : route générique partagée admin_custom_page
    (voir /admin/custom/<page_id>)."""
    if page_id in _ADMIN_PAGE_ENDPOINTS:
        return url_for(_ADMIN_PAGE_ENDPOINTS[page_id], **kwargs)
    return url_for('admin_custom_page', page_id=page_id, **kwargs)


def _admin_page_deletable(page_id: str) -> tuple:
    """(bool, raison_si_non). Une tuile n'est supprimable (masquée pour une
    tuile native, réellement retirée du registre pour une personnalisée)
    que si elle n'a aucun contenu fixe hors blocs (voir
    _ADMIN_NATIVE_ALWAYS_NONEMPTY) ET qu'aucun bloc ne lui est
    actuellement assigné — voir la route /admin/tuiles/delete."""
    if page_id in _ADMIN_NATIVE_ALWAYS_NONEMPTY:
        return False, "Cette tuile contient du contenu qui n'est pas un bloc, elle ne peut pas être supprimée."
    remaining = [bid for bid in _ADMIN_BLOCKS if _admin_block_page(bid) == page_id]
    if remaining:
        return False, f"Déplacez d'abord le(s) {len(remaining)} bloc(s) restant(s) avant de supprimer cette tuile."
    return True, ''


def _admin_block_redirect(block_id: str, **kwargs):
    """Redirige vers la page qui affiche ACTUELLEMENT `block_id`, et non
    vers sa page d'origine (`default_page`) — à utiliser par la route
    propre à un bloc déplaçable après traitement de son formulaire. Un
    bloc déplacé (voir admin_move_block) continue de poster vers la même
    route quelle que soit la page qui l'affiche ; c'est cette fonction qui
    garantit que l'utilisateur revient bien sur cette page-là plutôt que
    sur la tuile d'origine du bloc."""
    page = _admin_block_page(block_id)
    return redirect(_admin_page_url(page, **kwargs))


def _admin_block_collapsed(block_id: str) -> bool:
    return get_setting(f'ui.block_collapsed.{block_id}', '0') == '1'


def _admin_page_order(page: str, candidate_ids: list) -> list:
    """Ordre d'affichage des blocs `candidate_ids` sur `page` : l'ordre
    enregistré (voir /admin/ui/block_order), filtré aux ids toujours
    candidats sur cette page, puis les candidats absents de cet ordre
    enregistré (bloc tout juste déplacé ici, ou jamais réordonné) ajoutés à
    la suite dans leur ordre naturel."""
    raw = get_setting(f'ui.page_order.{page}', '')
    try:
        stored = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        stored = []
    ordered = [b for b in stored if b in candidate_ids]
    ordered += [b for b in candidate_ids if b not in ordered]
    return ordered


def _admin_block_context(block_id: str) -> dict:
    fn_name = _ADMIN_BLOCKS[block_id]['context_fn']
    return globals()[fn_name]() if fn_name else {}


def _admin_render_blocks(page: str, locally_visible: list = None) -> tuple:
    """Calcule, pour `page`, la liste ordonnée des blocs à afficher (chacun
    avec son id/titre/template/état replié) ainsi que le contexte Jinja
    fusionné dont leurs fragments ont besoin. `locally_visible`, si fourni,
    restreint aux ids de cette liste — utilisé par une route dont un bloc
    n'est affiché que sous condition (ex. access_clear_password seulement
    si un mot de passe est actuellement défini) : la route calcule
    elle-même cette condition et exclut le bloc en amont plutôt que
    d'afficher un bloc vide."""
    candidates = [b for b in _ADMIN_BLOCKS if _admin_block_page(b) == page]
    if locally_visible is not None:
        candidates = [b for b in candidates if b in locally_visible]
    ids = _admin_page_order(page, candidates)
    context = {}
    for bid in ids:
        context.update(_admin_block_context(bid))
    blocks = [{'id': bid, 'title': _ADMIN_BLOCKS[bid]['title'], 'template': _ADMIN_BLOCKS[bid]['template'],
               'variant': _ADMIN_BLOCKS[bid].get('variant'),
               'collapsed': _admin_block_collapsed(bid)} for bid in ids]
    return blocks, context


def _block_ctx_app_autostart() -> dict:
    return {'autostart_enabled': is_autostart_enabled()}


def _block_ctx_app_wallpaper() -> dict:
    return {'wallpaper_images': _wallpaper_images_view()}


def _block_ctx_app_idle_timer() -> dict:
    return {
        'idle_timer_enabled': get_setting('idle_timer_enabled', '0') == '1',
        'idle_timer_seconds': int(get_setting('idle_timer_seconds', '30')),
        'idle_timer_badge_text': get_setting('idle_timer_badge_text', 'Retour dans {n}s'),
        'idle_timer_font_size': int(get_setting('idle_timer_font_size', '13')),
        'idle_timer_padding_y': int(get_setting('idle_timer_padding_y', '5')),
        'idle_timer_padding_x': int(get_setting('idle_timer_padding_x', '13')),
    }


def _block_ctx_app_network_info() -> dict:
    return {'network_info_taps': _kiosk_network_info_settings()['taps']}


def _block_ctx_access_password() -> dict:
    return {'password_status': admin_password_status()}


def _block_ctx_texts_custom_fonts() -> dict:
    return {'custom_fonts': list_custom_fonts()}


def _block_ctx_system_camera() -> dict:
    return {'camera': _admin_system_camera_values()}


def _block_ctx_system_screen() -> dict:
    return {'screen': _admin_system_screen_values()}


def _block_ctx_system_kiosk_unlock() -> dict:
    return {'kiosk_unlock': _kiosk_unlock_settings()}


def _block_ctx_gallery_text() -> dict:
    return {'gallery_text': get_setting('gallery_text', '')}


def _block_ctx_frames_welcome() -> dict:
    welcome_fn = get_setting('welcome_frame_filename', '')
    return {'welcome_frame_url': f'/static/frames/{welcome_fn}' if welcome_fn else ''}


def _block_ctx_votes_activation() -> dict:
    return {'vote_enabled': get_setting('vote.enabled', '1') == '1'}


def _block_ctx_votes_thresholds_colors() -> dict:
    return {'cfg': _vote_cfg()}


def _block_ctx_buttons_style() -> dict:
    # Clés préfixées 'buttons_' (plutôt que les génériques 'settings'/'fonts')
    # pour ne jamais entrer en collision avec le contexte d'un autre bloc
    # fusionné sur la même page par _admin_render_blocks (context.update),
    # si ce bloc est un jour déplacé à côté d'un autre bloc à réglages —
    # voir la même précaution sur les blocs tags_* ci-dessous.
    return {'buttons_settings': _buttons_settings(), 'buttons_fonts': _all_fonts()}


def _block_ctx_tags_settings() -> dict:
    return {'tags_settings': _tags_settings()}


def _block_ctx_tags_media_id() -> dict:
    return {'media_id': _media_id_settings()}


def _block_ctx_tags_display() -> dict:
    # Même source de données que _block_ctx_tags_settings (clé 'tags_settings'
    # partagée à l'identique, sans risque si les deux blocs cohabitent sur
    # une même page) ; 'tags_fonts' plutôt que 'fonts' pour ne pas entrer en
    # collision avec la clé 'buttons_fonts' d'un bloc buttons_style déplacé ici.
    return {'tags_settings': _tags_settings(), 'tags_fonts': _all_fonts()}


def _block_ctx_slideshow_settings() -> dict:
    return {'slideshow_settings': _slideshow_settings()}


def _block_ctx_screensaver_settings() -> dict:
    return {'screensaver_settings': _screensaver_settings()}


def _block_ctx_guest_uploads_settings() -> dict:
    return {'guest_uploads_settings': _guest_upload_settings()}


def _block_ctx_guest_uploads_share_link() -> dict:
    # Même source que _block_ctx_guest_uploads_settings (clé
    # 'guest_uploads_settings' partagée à l'identique) ; share_url calculé
    # ici plutôt que dans la route GET, puisque ce bloc peut désormais être
    # affiché depuis n'importe quelle tuile qui l'héberge.
    s = _guest_upload_settings()
    share_url = (request.host_url.rstrip('/') + url_for('guest_upload_page', token=s['token'])
                if s['token'] else None)
    return {'guest_uploads_settings': s, 'guest_uploads_share_url': share_url}


def _block_ctx_guest_codes_code_settings() -> dict:
    return {'guest_codes_settings': _guest_codes_settings()}


def _block_ctx_guest_codes_qr_export() -> dict:
    return {
        'guest_codes_qr_export': _guest_codes_qr_export_settings(),
        'guest_codes_qr_formats': _GUEST_CODES_QR_FORMATS,
        'guest_codes_qr_text_contents': _GUEST_CODES_QR_TEXT_CONTENTS,
        'guest_codes_qr_text_positions': _GUEST_CODES_QR_TEXT_POSITIONS,
        'guest_codes_qr_size_units': _GUEST_CODES_QR_SIZE_UNITS,
        'guest_codes_fonts': _all_fonts(),
    }


def _block_ctx_guest_codes_purge_first_n() -> dict:
    # Copie namensée de _GUEST_CODES_SORTS (déjà passé au niveau page par la
    # route GET sous 'guest_codes_sorts', mais uniquement sur /admin/guest_codes)
    # : ce bloc doit continuer à afficher un menu d'ordre correct même
    # déplacé sur une autre tuile, dont la route GET ne passe pas cette
    # variable — voir le principe déjà appliqué à screensaver_settings
    # (vague 8) pour le même genre de dépendance.
    return {'guest_codes_purge_sorts': _GUEST_CODES_SORTS}


def _block_ctx_guest_codes_qrcode_settings() -> dict:
    return {
        'guest_codes_qrcode_settings': _qrcode_settings(),
        'guest_codes_qrcode_burn_settings': _qr_burn_settings(),
    }


def _block_ctx_guest_codes_qr_live() -> dict:
    # Fusionne qrcode_live_style et qrcode_live_error_style dans UN seul
    # bloc (2 routes de sauvegarde distinctes, voir admin_guest_codes_set_
    # qrcode_live_style / _error_style) : le formulaire du message d'erreur
    # lit en direct (JS) forme/taille/marge du formulaire "Apparence" —
    # couplage identique au principe déjà appliqué à votes_activation /
    # buttons_style (vagues 4-5), voir templates/blocks/guest_codes_qr_live.html.
    return {
        'guest_codes_qrcode_live_style': _qr_live_style_settings(),
        'guest_codes_qrcode_live_error_style': _qr_live_error_style_settings(),
        'guest_codes_qrcode_live_positions': _QR_LIVE_POSITIONS,
        'guest_codes_qrcode_live_shapes': _QR_LIVE_SHAPES,
        'guest_codes_fonts': _all_fonts(),
    }


@app.route('/admin/ui/block_collapse', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_ui_block_collapse():
    """Persiste l'état replié/déplié d'un bloc — appelé en AJAX par
    static/admin-blocks.js à chaque clic sur le chevron d'un bloc."""
    block_id = request.form.get('block_id', '')
    if block_id not in _ADMIN_BLOCKS:
        return jsonify(ok=False, error='bloc inconnu'), 404
    set_setting(f'ui.block_collapsed.{block_id}', '1' if request.form.get('collapsed') == '1' else '0')
    return jsonify(ok=True)


@app.route('/admin/ui/block_order', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_ui_block_order():
    """Persiste l'ordre d'affichage des blocs d'une page — appelé en AJAX
    par static/admin-blocks.js après un glisser-déposer. `order` : ids
    séparés par des virgules, dans le nouvel ordre voulu."""
    page = request.form.get('page', '')
    if page not in _admin_all_page_ids():
        return jsonify(ok=False, error='page inconnue'), 404
    order = [b for b in (request.form.get('order', '')).split(',') if b in _ADMIN_BLOCKS]
    set_setting(f'ui.page_order.{page}', json.dumps(order))
    return jsonify(ok=True)


@app.route('/admin/ui/move_block', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_move_block():
    """Déplace un bloc vers une autre tuile — action explicite (menu
    déroulant + bouton), sans glisser-déposer en direct entre pages : un
    bloc n'a jamais été conçu pour être injecté dynamiquement dans le DOM
    d'une autre page (scripts/identifiants qui lui sont propres), donc le
    déplacement se fait ici côté serveur (le bloc est ensuite rendu
    normalement, comme tous les autres, par la route de sa page cible) suivi
    d'un rechargement classique de page — beaucoup plus sûr qu'une
    manipulation du DOM en JavaScript entre deux pages. `target_page` peut
    être une tuile native OU personnalisée (vague 11) — voir _admin_page_url."""
    block_id = request.form.get('block_id', '')
    target_page = request.form.get('target_page', '')
    if block_id not in _ADMIN_BLOCKS:
        return redirect(url_for('admin_home', err='Bloc inconnu.'))
    if target_page not in _admin_all_page_ids():
        return redirect(url_for('admin_home', err='Page de destination inconnue.'))
    current_page = _admin_block_page(block_id)
    set_setting(f'ui.block_page.{block_id}', target_page)
    if target_page == current_page:
        return redirect(_admin_page_url(target_page))
    target_label = dict((pid, label) for pid, label, _ep in _admin_all_pages(include_hidden=True)).get(target_page, target_page)
    return redirect(_admin_page_url(target_page, ok=f"Bloc « {_ADMIN_BLOCKS[block_id]['title']} » déplacé vers « {target_label} »."))


@app.route('/admin/ui/tuile_order', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_ui_tuile_order():
    """Persiste l'ordre d'affichage des TUILES elles-mêmes sur /admin —
    même contrat que /admin/ui/block_order ci-dessus mais pour
    _admin_tuile_order. `order` : ids séparés par des virgules."""
    order = [p for p in (request.form.get('order', '')).split(',') if p in _admin_all_page_ids()]
    set_setting('ui.tuile_order', json.dumps(order))
    return jsonify(ok=True)


@app.route('/admin/tuiles/create', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tuiles_create():
    """Crée une tuile personnalisée vide (aucun contenu natif, aucun bloc
    au départ) — voir GET /admin/custom/<page_id> (admin_custom_page) pour
    la route générique qui l'affiche. Redirige directement vers la
    nouvelle tuile plutôt que vers /admin : l'action naturelle suivante
    est d'y déplacer des blocs existants."""
    label = (request.form.get('label') or '').strip()[:60]
    if not label:
        return redirect(url_for('admin_home', err='Le libellé de la tuile est obligatoire.'))
    description = (request.form.get('description') or '').strip()[:200]
    existing = set(_admin_all_page_ids())
    page_id = 'custom_' + secrets.token_hex(4)
    while page_id in existing:  # collision quasi impossible, filet de sécurité
        page_id = 'custom_' + secrets.token_hex(4)
    set_setting(f'ui.page_label.{page_id}', label)
    if description:
        set_setting(f'ui.page_desc.{page_id}', description)
    custom_pages = _admin_custom_page_ids()
    custom_pages.append(page_id)
    set_setting('ui.custom_pages', json.dumps(custom_pages))
    order = _admin_tuile_order(_admin_all_page_ids())
    order.append(page_id)
    set_setting('ui.tuile_order', json.dumps(order))
    return redirect(url_for('admin_custom_page', page_id=page_id, ok=f"Tuile « {label} » créée."))


@app.route('/admin/tuiles/rename', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tuiles_rename():
    """Renomme n'importe quelle tuile (native ou personnalisée) : libellé
    (obligatoire) et description (optionnelle), visibles partout — /admin,
    menus "Déplacer vers" de toutes les pages à blocs, et le <h1>/<title>
    de la tuile elle-même (voir page_label= passé par chaque route GET)."""
    page_id = request.form.get('page_id', '')
    if page_id not in _admin_all_page_ids():
        return redirect(url_for('admin_home', err='Tuile inconnue.'))
    label = (request.form.get('label') or '').strip()[:60]
    if not label:
        return redirect(url_for('admin_home', err='Le libellé de la tuile est obligatoire.'))
    set_setting(f'ui.page_label.{page_id}', label)
    set_setting(f'ui.page_desc.{page_id}', (request.form.get('description') or '').strip()[:200])
    return redirect(url_for('admin_home', ok=f"Tuile renommée « {label} »."))


@app.route('/admin/tuiles/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tuiles_delete():
    """Supprime une tuile — voir _admin_page_deletable pour les conditions
    (jamais de contenu natif fixe, plus aucun bloc assigné). Personnalisée :
    retirée du registre pour de vrai (sa route redevient un 404). Native :
    masquée (ui.page_hidden.<slug> = '1') — sa route reste fonctionnelle si
    on la visite directement, pour ne jamais casser un lien déjà ouvert ;
    réversible via /admin/tuiles/unhide."""
    page_id = request.form.get('page_id', '')
    if page_id not in _admin_all_page_ids():
        return redirect(url_for('admin_home', err='Tuile inconnue.'))
    ok, reason = _admin_page_deletable(page_id)
    if not ok:
        return redirect(url_for('admin_home', err=reason))
    label = _admin_page_label(page_id, dict((s, l) for s, l, _e in _ADMIN_PAGES).get(page_id, page_id))
    if page_id in _ADMIN_PAGE_SLUGS:
        set_setting(f'ui.page_hidden.{page_id}', '1')
    else:
        custom_pages = [p for p in _admin_custom_page_ids() if p != page_id]
        set_setting('ui.custom_pages', json.dumps(custom_pages))
    order = [p for p in _admin_tuile_order(_admin_all_page_ids()) if p != page_id]
    set_setting('ui.tuile_order', json.dumps(order))
    return redirect(url_for('admin_home', ok=f"Tuile « {label} » supprimée."))


@app.route('/admin/tuiles/unhide', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tuiles_unhide():
    """Réaffiche une tuile native masquée (voir /admin/tuiles/delete) —
    sans effet sur une tuile personnalisée (elle n'a jamais d'état masqué,
    la supprimer la retire du registre pour de vrai)."""
    page_id = request.form.get('page_id', '')
    if page_id not in _ADMIN_PAGE_SLUGS:
        return redirect(url_for('admin_home', err='Tuile inconnue.'))
    set_setting(f'ui.page_hidden.{page_id}', '0')
    label = _admin_page_label(page_id, dict((s, l) for s, l, _e in _ADMIN_PAGES).get(page_id, page_id))
    return redirect(url_for('admin_home', ok=f"Tuile « {label} » réaffichée."))


@app.route('/admin/custom/<page_id>')
@require_admin_auth
def admin_custom_page(page_id):
    """Route générique partagée par TOUTES les tuiles personnalisées (voir
    /admin/tuiles/create) — l'équivalent d'une route dédiée admin_application()
    etc. mais paramétrée par page_id plutôt que hardcodée une fois par
    tuile : une tuile personnalisée n'a par construction aucun contenu
    natif, seulement des blocs (voir templates/admin_custom_page.html)."""
    if page_id not in _admin_custom_page_ids():
        abort(404)
    blocks, block_context = _admin_render_blocks(page_id)
    label = _admin_page_label(page_id, 'Tuile sans nom')
    return render_template(
        'admin_custom_page.html', config=CONFIG,
        blocks=blocks, current_page=page_id, admin_pages=_admin_all_pages(),
        page_label=label, page_desc=_admin_page_desc(page_id, ''),
        alert_success=request.args.get('ok'), alert_error=request.args.get('err'),
        **block_context,
    )


# ── Fond d'écran Windows (réglable depuis la tuile « Application ») ───────────
WALLPAPER_DIR = BASE_DIR / 'app' / 'static' / 'wallpapers'
_WALLPAPER_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.bmp'}


def _wallpaper_images_view() -> list:
    """Images disponibles pour le fond d'écran Windows, avec leur URL et un
    marqueur is_current (comparé au réglage application.wallpaper_current_filename,
    mis à jour uniquement par admin_wallpaper_apply lors d'une application
    réussie — voir /admin/application)."""
    current = get_setting('application.wallpaper_current_filename', '')
    images = list_wallpaper_images()
    for img in images:
        img['url'] = f"/static/wallpapers/{img['filename']}"
        img['is_current'] = img['filename'] == current
    return images


# ── Médiathèque (v2.0.1) ──────────────────────────────────────────────────────
# Tuile dédiée centralisant tous les médias autres que les captures visiteurs :
# fond d'écran Windows (bloc app_wallpaper, déplacé ici via son default_page —
# voir _ADMIN_BLOCKS), images intermédiaires du diaporama /bestof et images
# dédiées à l'écran de veille (CRUD natif, ex-hébergé respectivement dans
# « Slideshow Best Of » et « Écran de veille » avant cette version). Les
# fonctions de base (list_slideshow_images, add_slideshow_image, ... et leurs
# équivalents screensaver) sont réutilisées telles quelles.
@app.route('/admin/media', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_media():
    """Tuile « Médiathèque »."""
    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'upload_slideshow':
            file = request.files.get('image')
            if not file or not file.filename:
                return redirect(url_for('admin_media', err='Aucun fichier sélectionné.'))
            ext = Path(file.filename).suffix.lower()
            if ext not in _SLIDESHOW_ALLOWED_EXT:
                return redirect(url_for('admin_media', err='Format non supporté (PNG, JPG, WEBP, GIF).'))
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(file.filename).stem) + ext
            SLIDESHOW_DIR.mkdir(parents=True, exist_ok=True)
            dest = SLIDESHOW_DIR / safe
            if dest.exists():
                safe = f'{Path(safe).stem}_{int(datetime.now().timestamp())}{ext}'
                dest = SLIDESHOW_DIR / safe
            file.save(str(dest))
            add_slideshow_image(safe)
            return redirect(url_for('admin_media', ok=f'Image « {safe} » ajoutée.'))

        if action == 'delete_slideshow':
            image_id = int(request.form.get('image_id', 0))
            img = delete_slideshow_image_db(image_id)
            if img:
                (SLIDESHOW_DIR / img['filename']).unlink(missing_ok=True)
                return redirect(url_for('admin_media', ok='Image supprimée.'))
            return redirect(url_for('admin_media', err='Image introuvable.'))

        if action == 'upload_screensaver':
            file = request.files.get('image')
            if not file or not file.filename:
                return redirect(url_for('admin_media', err='Aucun fichier sélectionné.'))
            ext = Path(file.filename).suffix.lower()
            if ext not in _SCREENSAVER_ALLOWED_EXT:
                return redirect(url_for('admin_media', err='Format non supporté (PNG, JPG, WEBP, GIF).'))
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(file.filename).stem) + ext
            SCREENSAVER_DIR.mkdir(parents=True, exist_ok=True)
            dest = SCREENSAVER_DIR / safe
            if dest.exists():
                safe = f'{Path(safe).stem}_{int(datetime.now().timestamp())}{ext}'
                dest = SCREENSAVER_DIR / safe
            file.save(str(dest))
            add_screensaver_image(safe)
            return redirect(url_for('admin_media', ok=f'Image « {safe} » ajoutée.'))

        if action == 'delete_screensaver':
            image_id = int(request.form.get('image_id', 0))
            img = delete_screensaver_image_db(image_id)
            if img:
                (SCREENSAVER_DIR / img['filename']).unlink(missing_ok=True)
                return redirect(url_for('admin_media', ok='Image supprimée.'))
            return redirect(url_for('admin_media', err='Image introuvable.'))

        abort(404)

    blocks, block_context = _admin_render_blocks('media')
    return render_template(
        'admin_media.html', config=CONFIG,
        blocks=blocks, current_page='media', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('media', 'Médiathèque'),
        slideshow_images=list_slideshow_images(),
        screensaver_images=list_screensaver_images(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/application')
@require_admin_auth
def admin_application():
    """Tuile « Application » du back office — actions liées au processus de
    l'application lui-même (plein écran, redémarrage, démarrage automatique
    Windows, fond d'écran Windows, informations réseau sur le kiosque),
    séparées des réglages caméra/écran de /admin/system. Pas de POST ici :
    chaque action poste vers sa propre route dédiée ci-dessous."""
    blocks, block_context = _admin_render_blocks('application')
    return render_template(
        'admin_application.html', config=CONFIG,
        blocks=blocks, current_page='application', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('application', 'Application'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/application/idle_timer', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_set_idle_timer():
    """Timer de retour automatique à l'accueil (barre de progression +
    décompte sur les écrans intermédiaires) — déplacé depuis la tuile
    « Textes » : c'est un comportement de l'application (minuterie,
    retour à l'accueil) plutôt qu'un texte affiché."""
    set_setting('idle_timer_enabled', '1' if request.form.get('idle_timer_enabled') else '0')
    raw_secs = (request.form.get('idle_timer_seconds') or '30').strip()
    set_setting('idle_timer_seconds', str(max(5, int(raw_secs))) if raw_secs.isdigit() else '30')
    badge_text = (request.form.get('idle_timer_badge_text') or 'Retour dans {n}s').strip()
    set_setting('idle_timer_badge_text', badge_text)
    for key, default in [('idle_timer_font_size', '13'), ('idle_timer_padding_y', '5'), ('idle_timer_padding_x', '13')]:
        raw = (request.form.get(key) or default).strip()
        set_setting(key, str(max(1, int(raw))) if raw.isdigit() else default)
    return _admin_block_redirect('app_idle_timer', ok='Mise en veille automatique mise à jour.')


@app.route('/admin/access')
@require_admin_auth
def admin_access():
    """Tuile « Admin » du back office — gestion du mot de passe unique qui
    protège l'ensemble du back office (voir auth.py : _get_admin_password,
    admin_password_status, set_admin_password). Remplace la gestion de ce
    mot de passe dans config.toml (section [admin], conservée en lecture
    seule pour compatibilité ascendante tant qu'aucun mot de passe n'a été
    défini depuis cette page — voir commentaire dans auth._get_admin_password)."""
    password_status = admin_password_status()
    visible = ['access_password']
    if password_status['set'] and password_status['source'] != 'env':
        visible.append('access_clear_password')
    blocks, block_context = _admin_render_blocks('access', locally_visible=visible)
    return render_template(
        'admin_access.html', config=CONFIG,
        blocks=blocks, current_page='access', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('access', 'Admin'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/access/password', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_set_password():
    """Définit ou change le mot de passe admin unique. Un mot de passe vide
    est refusé ici (utiliser admin_clear_password ci-dessous, action
    distincte et explicite, pour désactiver volontairement la protection)."""
    new_password = (request.form.get('new_password') or '').strip()
    confirm_password = (request.form.get('confirm_password') or '').strip()
    if not new_password:
        return _admin_block_redirect(
            'access_password',
            err="Le mot de passe ne peut pas être vide (utilisez « Supprimer la protection » ci-dessous pour désactiver le mot de passe).",
        )
    if new_password != confirm_password:
        return _admin_block_redirect('access_password', err='Les deux mots de passe saisis ne correspondent pas.')
    set_admin_password(new_password)
    logger.info("Mot de passe admin modifié depuis /admin/access (back office).")
    return _admin_block_redirect('access_password', ok='Mot de passe mis à jour.')


@app.route('/admin/access/password/clear', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_clear_password():
    """Désactive volontairement la protection par mot de passe du back
    office (accès libre à /admin ensuite, tant qu'aucun mot de passe n'est
    redéfini depuis cette même page)."""
    set_admin_password('')
    logger.warning("Protection par mot de passe du back office désactivée depuis /admin/access.")
    return _admin_block_redirect(
        'access_clear_password',
        ok='Protection par mot de passe désactivée — le back office est désormais accessible sans mot de passe.',
    )


@app.route('/admin/system/autostart', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_toggle_autostart():
    """Inscrit/désinscrit le lancement automatique de l'application à
    l'ouverture de session Windows (raccourci dans le dossier Démarrage de
    l'utilisateur courant vers run.bat — voir utils.py)."""
    if is_autostart_enabled():
        ok, msg = disable_autostart()
    else:
        ok, msg = enable_autostart(BASE_DIR / 'run.bat')
    if ok:
        return _admin_block_redirect('app_autostart', ok=msg)
    return _admin_block_redirect('app_autostart', err=msg)


@app.route('/admin/system/fullscreen', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_toggle_fullscreen():
    """Bascule immédiate du plein écran de la fenêtre native déjà lancée
    (distinct de ui_kiosk_mode dans /admin/system, qui ne s'applique qu'au
    prochain lancement de run.bat). Déjà protégé par l'authentification
    admin, donc pas de mot de passe supplémentaire ici (contrairement à
    l'appel JS équivalent déclenché depuis l'interface principale)."""
    result = _kiosk_api._do_toggle('back office')
    if result.get('ok'):
        return _admin_block_redirect('app_fullscreen', ok='Plein écran basculé.')
    return _admin_block_redirect('app_fullscreen', err=result.get('error', 'Bascule impossible.'))


@app.route('/admin/system/restart', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_restart_app():
    """Redémarre le processus de l'application depuis le back office —
    contrairement au réglage caméra dans /admin/system (rechargé à chaud
    via reset_camera()), certains changements ne sont pris en compte qu'au
    démarrage : fichiers Python, gabarits Jinja (mis en cache tant que le
    processus tourne), fichier config.toml modifié à la main, etc.
    Le redémarrage effectif (voir _do_restart_app) est différé de
    quelques centaines de ms pour laisser cette réponse atteindre le
    navigateur avant que le processus ne se remplace lui-même."""
    logger.info('Redémarrage programmé depuis /admin/application (back office).')
    threading.Timer(0.8, _do_restart_app).start()
    return _admin_block_redirect(
        'app_restart',
        ok="Redémarrage en cours... la borne va être indisponible quelques secondes.",
    )


@app.route('/admin/system/restart_update', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_restart_update_app():
    """Redémarre l'application en récupérant d'abord la dernière version
    depuis le dépôt Git (branche main) — même mécanisme que le double-clic
    manuel sur update.bat (voir update.ps1) : arrête l'instance en cours,
    'git fetch' + 'git reset --hard origin/main' (config.toml local
    préservé via sauvegarde/restauration), puis relance via run.bat.

    Contrairement à admin_restart_app ci-dessus, on ne relance PAS
    directement le processus courant ici (pas de _do_restart_app) :
    update.ps1 s'en charge lui-même (il cherche puis arrête le python.exe
    en cours d'exécution sur CE app\\app.py), une fois la mise à jour
    terminée — inutile et plus fragile de dupliquer cette logique ici.

    Lancé dans une fenêtre de commande VISIBLE (CREATE_NEW_CONSOLE, pas
    détaché) et non en arrière-plan silencieux : update.ps1 peut se
    terminer sur une pause interactive en cas d'échec (pas de connexion
    internet, etc.) — invisible et donc bloquée indéfiniment sans console,
    alors qu'une fenêtre visible sur la borne permet de constater l'échec
    et de la fermer."""
    if not (BASE_DIR / '.git').is_dir():
        return _admin_block_redirect(
            'app_restart_update',
            err="Ce dossier n'est pas un dépôt Git (pas de sous-dossier .git) — mise à jour impossible.",
        )
    update_bat = BASE_DIR / 'update.bat'
    if not update_bat.exists():
        return _admin_block_redirect('app_restart_update', err='update.bat introuvable.')
    logger.info('Mise à jour Git + redémarrage programmés depuis /admin/application (back office).')
    try:
        subprocess.Popen(
            ['cmd', '/c', str(update_bat)],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except Exception:
        logger.exception("Échec du lancement de update.bat depuis le back office.")
        return _admin_block_redirect(
            'app_restart_update',
            err="Échec du lancement de la mise à jour (voir logs\\app.log).",
        )
    return _admin_block_redirect(
        'app_restart_update',
        ok="Mise à jour en cours (fenêtre ouverte sur la borne)... l'application redémarrera automatiquement.",
    )


@app.route('/admin/application/wallpaper/upload', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_wallpaper_upload():
    """Ajoute une ou plusieurs images à la liste des fonds d'écran Windows
    disponibles (voir /admin/application) — ne les applique pas
    automatiquement, c'est admin_wallpaper_apply ci-dessous qui s'en charge,
    sur une image déjà présente dans cette liste."""
    files = [f for f in request.files.getlist('wallpaper_images') if f and f.filename]
    if not files:
        return _admin_block_redirect('app_wallpaper', err='Aucun fichier sélectionné.')
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    for i, file in enumerate(files):
        ext = Path(file.filename).suffix.lower()
        if ext not in _WALLPAPER_ALLOWED_EXT:
            continue
        safe = f'wallpaper-{int(datetime.now().timestamp() * 1000)}-{i}{ext}'
        file.save(str(WALLPAPER_DIR / safe))
        add_wallpaper_image(safe)
        added += 1
    if not added:
        return _admin_block_redirect('app_wallpaper', err='Format non supporté (PNG, JPG, BMP).')
    skipped = len(files) - added
    msg = f"{added} image(s) ajoutée(s)."
    if skipped:
        msg += f' {skipped} ignorée(s) (format non supporté).'
    return _admin_block_redirect('app_wallpaper', ok=msg)


@app.route('/admin/application/wallpaper/apply', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_wallpaper_apply():
    """Applique immédiatement l'une des images déjà ajoutées comme fond
    d'écran du Bureau Windows (voir utils.set_windows_wallpaper — API
    native SystemParametersInfoW, effet immédiat et persistant)."""
    try:
        image_id = int(request.form.get('image_id', ''))
    except ValueError:
        return _admin_block_redirect('app_wallpaper', err='Image invalide.')
    images = {img['id']: img for img in list_wallpaper_images()}
    img = images.get(image_id)
    if not img:
        return _admin_block_redirect('app_wallpaper', err='Image introuvable.')
    ok, msg = set_windows_wallpaper(WALLPAPER_DIR / img['filename'])
    if not ok:
        return _admin_block_redirect('app_wallpaper', err=msg)
    set_setting('application.wallpaper_current_filename', img['filename'])
    logger.info('Fond d\'écran Windows changé depuis /admin/application : %s', img['filename'])
    return _admin_block_redirect('app_wallpaper', ok=msg)


@app.route('/admin/application/wallpaper/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_wallpaper_delete():
    """Retire une image de la liste (fichier + entrée DB). N'affecte pas le
    fond d'écran Windows actuellement appliqué si c'est une autre image —
    si c'est CELLE-LÀ, le réglage application.wallpaper_current_filename est
    simplement effacé (Windows garde l'image en place jusqu'au prochain
    changement, rien n'est fait côté Bureau ici)."""
    try:
        image_id = int(request.form.get('image_id', ''))
    except ValueError:
        return _admin_block_redirect('app_wallpaper', err='Image invalide.')
    img = delete_wallpaper_image_db(image_id)
    if not img:
        return _admin_block_redirect('app_wallpaper', err='Image introuvable.')
    (WALLPAPER_DIR / img['filename']).unlink(missing_ok=True)
    if get_setting('application.wallpaper_current_filename', '') == img['filename']:
        set_setting('application.wallpaper_current_filename', '')
    return _admin_block_redirect('app_wallpaper', ok='Image supprimée.')


@app.route('/admin/application/network_info_taps', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_set_network_info_taps():
    """Nombre d'appuis (zone dédiée, coin bas-gauche de l'interface
    principale) déclenchant l'affichage de l'IP + du port en écoute — voir
    _kiosk_network_info_settings() et setupNetworkInfoTaps() dans
    static/app.js. Aucun mot de passe : purement informatif."""
    raw_taps = (request.form.get('network_info_taps') or '').strip()
    set_setting('kiosk.network_info_taps',
                raw_taps if raw_taps.isdigit() and 2 <= int(raw_taps) <= 15 else '')
    return _admin_block_redirect('app_network_info', ok='Réglage mis à jour.')


@app.route('/admin/frames')
@require_admin_auth
def admin_frames():
    """Tuile « Cadres ». La section « Cadres existants » (liste/CRUD, en
    bas de page) reste hors du système de blocs : ce n'est pas un réglage
    mais la gestion du contenu lui-même (comme admin_captures/admin_emails),
    donc toujours affichée ici, indépendamment des blocs déplacés."""
    blocks, block_context = _admin_render_blocks('frames')
    return render_template(
        'admin_frames.html', config=CONFIG, frames=list_frames(),
        blocks=blocks, current_page='frames', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('frames', 'Gestion des cadres'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/frames/new', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_frame_create():
    frame_id = re.sub(r'[^a-z0-9_-]', '', (request.form.get('id') or '').strip().lower().replace(' ', '-'))
    label = (request.form.get('label') or '').strip()
    sort_order = int(request.form.get('sort_order') or 99)
    if not frame_id or not label:
        return _admin_block_redirect('frames_new', err='Identifiant et libellé sont obligatoires.')
    if get_frame_by_id_db(frame_id):
        return _admin_block_redirect('frames_new', err=f'Un cadre avec l\'identifiant "{frame_id}" existe déjà.')
    try:
        preview_fn = _save_frame_file(request.files.get('preview'), frame_id, 'preview')
        overlay_fn = _save_frame_file(request.files.get('overlay'), frame_id, 'overlay')
    except ValueError as exc:
        return _admin_block_redirect('frames_new', err=str(exc))
    upsert_frame(frame_id, label, preview_fn, overlay_fn, sort_order)
    return _admin_block_redirect('frames_new', ok=f'Cadre "{label}" ajouté.')


@app.route('/admin/frames/<frame_id>/edit', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_frame_edit(frame_id):
    if frame_id == 'none':
        return _admin_redirect(error='Le cadre "aucun" ne peut pas être modifié.')
    frame = get_frame_by_id_db(frame_id)
    if not frame:
        abort(404)
    label = (request.form.get('label') or '').strip() or frame['label']
    sort_order = int(request.form.get('sort_order') or frame['sort_order'])
    preview_fn = frame['preview_filename']
    overlay_fn = frame['overlay_filename']
    try:
        new_preview = _save_frame_file(request.files.get('preview'), frame_id, 'preview')
        if new_preview:
            preview_fn = new_preview
        new_overlay = _save_frame_file(request.files.get('overlay'), frame_id, 'overlay')
        if new_overlay:
            overlay_fn = new_overlay
            clear_overlay_cache(frame_id)
    except ValueError as exc:
        return _admin_redirect(error=str(exc))
    upsert_frame(frame_id, label, preview_fn, overlay_fn, sort_order)
    return _admin_redirect(success=f'Cadre "{frame_id}" mis à jour.')


@app.route('/admin/frames/<frame_id>/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_frame_delete(frame_id):
    if frame_id == 'none':
        return _admin_redirect(error='Le cadre "aucun" ne peut pas être supprimé.')
    frame = delete_frame_db(frame_id)
    if not frame:
        abort(404)
    for field in ('preview_filename', 'overlay_filename'):
        fn = frame.get(field)
        if fn:
            try:
                (FRAMES_DIR / fn).unlink(missing_ok=True)
            except Exception:
                logger.warning('Impossible de supprimer le fichier cadre : %s', fn)
    clear_overlay_cache(frame_id)
    return _admin_redirect(success=f'Cadre "{frame_id}" supprimé.')


# ── Admin — emails ────────────────────────────────────────────────────────────

@app.route('/admin/emails')
@require_admin_auth
def admin_emails():
    sort   = request.args.get('sort', 'desc')
    search = request.args.get('q', '').strip()
    all_emails = list_emails()
    emails     = list_emails(sort, search)
    return render_template('admin_emails.html', config=CONFIG,
                           emails=emails, sort=sort, search=search,
                           total=len(all_emails),
                           alert_success=request.args.get('ok'),
                           alert_error=request.args.get('err'))


@app.route('/admin/emails/new', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_email_add():
    sort   = request.form.get('sort', 'desc')
    search = request.form.get('search', '')
    email  = (request.form.get('email') or '').strip().lower()
    if not email:
        return _admin_emails_redirect(error='Email vide.', sort=sort, search=search)
    save_email(email)
    return _admin_emails_redirect(success=f'"{email}" ajouté.', sort=sort, search=search)


@app.route('/admin/emails/<int:email_id>/edit', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_email_edit(email_id):
    sort      = request.form.get('sort', 'desc')
    search    = request.form.get('search', '')
    new_email = (request.form.get('email') or '').strip().lower()
    if not new_email:
        return _admin_emails_redirect(error='Email invalide.', sort=sort, search=search)
    update_email_by_id(email_id, new_email)
    return _admin_emails_redirect(success='Email mis à jour.', sort=sort, search=search)


@app.route('/admin/emails/<int:email_id>/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_email_delete(email_id):
    sort   = request.form.get('sort', 'desc')
    search = request.form.get('search', '')
    delete_email_by_id(email_id)
    return _admin_emails_redirect(success='Email supprimé.', sort=sort, search=search)


@app.route('/admin/emails/delete-all', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_emails_delete_all():
    all_emails = list_emails()
    count = len(all_emails)
    from config_loader import EMAILS_JSONL
    with closing(db_conn()) as conn:
        conn.execute('DELETE FROM emails')
        conn.commit()
    if EMAILS_JSONL.exists():
        EMAILS_JSONL.write_text('', encoding='utf-8')
    return _admin_emails_redirect(success=f'{count} email(s) supprimé(s).')


@app.route('/admin/emails/export/csv')
@require_admin_auth
def admin_emails_export_csv():
    rows   = list_emails(sort='asc')
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['id', 'email', 'created_at'])
    writer.writeheader()
    writer.writerows(rows)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=emails.csv'})


@app.route('/admin/emails/export/json')
@require_admin_auth
def admin_emails_export_json():
    rows = list_emails(sort='asc')
    return Response(json.dumps(rows, ensure_ascii=False, indent=2),
                    mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename=emails.json'})


# ── Votes ─────────────────────────────────────────────────────────────────────

def _vote_cfg():
    return {
        'blue_max':      int(get_setting('vote.blue_max',      '10')),
        'green_max':     int(get_setting('vote.green_max',     '10')),
        'red_max':       int(get_setting('vote.red_max',       '20')),
        'color_neg_max': get_setting('vote.color_neg_max', '#00008b'),
        'color_neg_mid': get_setting('vote.color_neg_mid', '#add8e6'),
        'color_zero':    get_setting('vote.color_zero',    '#888888'),
        'color_pos_mid': get_setting('vote.color_pos_mid', '#ffa500'),
        'color_pos_max': get_setting('vote.color_pos_max', '#cc0000'),
    }


@app.route('/api/vote', methods=['POST'])
def api_vote():
    voter_token = request.cookies.get('voter_token', '')
    if not voter_token:
        return jsonify({'ok': False, 'error': 'Token manquant'}), 400
    data = request.get_json(silent=True) or {}
    capture_id = data.get('capture_id')
    value = data.get('value')
    source = data.get('source', 'official')
    if source not in ('official', 'guest'):
        return jsonify({'ok': False, 'error': 'source invalide'}), 400
    if not capture_id or value not in (1, -1):
        return jsonify({'ok': False, 'error': 'Paramètres invalides'}), 400
    try:
        new_score, your_vote = cast_vote(int(capture_id), voter_token, int(value), source=source)
        return jsonify({'ok': True, 'score': new_score, 'your_vote': your_vote})
    except Exception:
        logger.exception('Erreur vote %s #%s', source, capture_id)
        return jsonify({'ok': False, 'error': 'Erreur serveur'}), 500


@app.route('/admin/captures/<int:capture_id>/vote-adjust', methods=['POST'])
@require_admin_auth
def admin_vote_adjust(capture_id):
    data = request.get_json(silent=True) or {}
    delta = data.get('delta')
    if delta not in (1, -1):
        return jsonify({'ok': False, 'error': 'delta invalide'}), 400
    new_score = admin_adjust_vote(capture_id, delta)
    return jsonify({'ok': True, 'score': new_score})


@app.route('/admin/votes')
@require_admin_auth
def admin_votes():
    """Tuile « Votes ». Pas de POST ici : chacun des 2 blocs poste vers sa
    propre route dédiée ci-dessous. « Seuils » et « Couleurs aux points
    clés » restent un SEUL bloc (et non 2) car l'aperçu du gradient en JS
    lit en direct les deux ensembles de champs — les séparer sur des
    tuiles différentes casserait cet aperçu (voir templates/blocks/
    votes_thresholds_colors.html)."""
    blocks, block_context = _admin_render_blocks('votes')
    return render_template(
        'admin_votes.html', config=CONFIG,
        blocks=blocks, current_page='votes', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('votes', 'Système de votes'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/votes/activation', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_votes_set_activation():
    set_setting('vote.enabled', '1' if request.form.get('enabled') else '0')
    return _admin_block_redirect('votes_activation', ok='Paramètres mis à jour.')


@app.route('/admin/votes/thresholds_colors', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_votes_set_thresholds_colors():
    set_setting('vote.blue_max',      str(max(1, int(request.form.get('blue_max',  '10') or '10'))))
    set_setting('vote.green_max',     str(max(1, int(request.form.get('green_max', '10') or '10'))))
    set_setting('vote.red_max',       str(max(1, int(request.form.get('red_max',   '20') or '20'))))
    for key in ('color_neg_max', 'color_neg_mid', 'color_zero', 'color_pos_mid', 'color_pos_max'):
        val = request.form.get(key, '').strip()
        if val.startswith('#') and len(val) == 7:
            set_setting(f'vote.{key}', val)
    return _admin_block_redirect('votes_thresholds_colors', ok='Paramètres mis à jour.')


# ── Admin — tags & ID média ───────────────────────────────────────────────────

@app.route('/admin/tags', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_tags():
    """Tuile « Tags & ID média ». « Activation & règles du tag libre »,
    « ID unique par média » et « Affichage sur /bestof » ont chacune leur
    propre route dédiée ci-dessous et rejoignent le système de blocs.
    « Tags prédéfinis » (CRUD) et « Tags appliqués » (journal) restent
    ici, hors du système de blocs : ce n'est pas un réglage mais la
    gestion du contenu lui-même (comme admin_captures/admin_emails)."""
    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'tag_new':
            label = (request.form.get('label') or '').strip()
            sort_order = int(request.form.get('sort_order') or 0)
            if not label:
                return redirect(url_for('admin_tags', err='Le libellé est obligatoire.'))
            create_tag(label, sort_order)
            return redirect(url_for('admin_tags', ok=f'Tag « {label} » ajouté.'))

        if action == 'tag_edit':
            tag_id = int(request.form.get('tag_id', 0))
            if not get_tag_by_id(tag_id):
                abort(404)
            label = (request.form.get('label') or '').strip()
            sort_order = int(request.form.get('sort_order') or 0)
            if not label:
                return redirect(url_for('admin_tags', err='Le libellé est obligatoire.'))
            update_tag(tag_id, label, sort_order)
            return redirect(url_for('admin_tags', ok='Tag mis à jour.'))

        if action == 'tag_delete':
            tag_id = int(request.form.get('tag_id', 0))
            tag = delete_tag_db(tag_id)
            if tag:
                return redirect(url_for('admin_tags', ok=f"Tag « {tag['label']} » supprimé."))
            return redirect(url_for('admin_tags', err='Tag introuvable.'))

        if action == 'assignment_delete':
            assignment_id = int(request.form.get('assignment_id', 0))
            row = delete_capture_tag(assignment_id)
            if row:
                return redirect(url_for('admin_tags', ok=f"Tag « {row['label']} » retiré du média."))
            return redirect(url_for('admin_tags', err='Assignation introuvable.'))

        abort(404)

    blocks, block_context = _admin_render_blocks('tags')
    return render_template(
        'admin_tags.html', config=CONFIG,
        blocks=blocks, current_page='tags', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('tags', 'Tags & ID média'),
        tags=list_tags(), assignments=list_capture_tags_with_media(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/tags/settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tags_set_settings():
    set_setting('tags.enabled',      '1' if request.form.get('enabled') else '0')
    set_setting('tags.free_enabled', '1' if request.form.get('free_enabled') else '0')
    raw_min = (request.form.get('free_min_length') or '').strip()
    raw_max = (request.form.get('free_max_length') or '').strip()
    if raw_min.isdigit() and int(raw_min) >= 1:
        set_setting('tags.free_min_length', raw_min)
    if raw_max.isdigit() and int(raw_max) >= 1:
        set_setting('tags.free_max_length', raw_max)
    raw_max_tags = (request.form.get('max_per_capture') or '').strip()
    if raw_max_tags.isdigit() and int(raw_max_tags) >= 1:
        set_setting('tags.max_per_capture', raw_max_tags)
    return _admin_block_redirect('tags_settings', ok='Paramètres mis à jour.')


@app.route('/admin/tags/media_id', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tags_set_media_id():
    raw_len = (request.form.get('media_id_length') or '').strip()
    if raw_len.isdigit() and 3 <= int(raw_len) <= 12:
        set_setting('media_id.length', raw_len)
    set_setting('media_id.show_on_bestof', '1' if request.form.get('show_on_bestof') else '0')
    return _admin_block_redirect('tags_media_id', ok='Réglages ID média mis à jour.')


@app.route('/admin/tags/display', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_tags_set_display():
    set_setting('tags.show_on_bestof', '1' if request.form.get('show_on_bestof') else '0')
    font_value = request.form.get('style_font', '')
    if font_value in dict(_all_fonts()):
        set_setting('tags.style_font', font_value)
    bg_value = request.form.get('style_bg_color', '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', bg_value):
        set_setting('tags.style_bg_color', bg_value)
    text_value = request.form.get('style_text_color', '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', text_value):
        set_setting('tags.style_text_color', text_value)
    raw_fs = (request.form.get('style_font_size') or '').strip()
    if raw_fs.isdigit() and int(raw_fs) >= 8:
        set_setting('tags.style_font_size', raw_fs)
    return _admin_block_redirect('tags_display', ok="Réglages d'affichage mis à jour.")


# ── Admin — codes invités ──────────────────────────────────────────────────────
# Correspondance code numérique <-> texte libre (voir get_guest_code_text,
# db.py) + réglages QR-code déplacés depuis /admin/tags : détection
# automatique, apparence du texte affiché en direct, message d'erreur.

def _guest_codes_settings() -> dict:
    raw_len = get_setting('guest_codes.code_length', '4')
    try:
        length = max(2, min(int(raw_len), 10))
    except (TypeError, ValueError):
        length = 4
    return {'code_length': length}


# Clés/libellés (FR) de tri de la liste des codes invités — voir
# db.GUEST_CODES_SORT_SQL pour le SQL associé à chaque clé (seule source de
# vérité pour la validité d'une clé). Réutilisée telle quelle comme liste
# d'ordres possibles pour la purge « N premiers » (même sémantique : les N
# premiers de la liste triée ainsi).
_GUEST_CODES_SORTS = [
    ('created_desc', 'Date de création (récent → ancien)'),
    ('created_asc',  'Date de création (ancien → récent)'),
    ('texte_asc',    'Texte (A → Z)'),
    ('texte_desc',   'Texte (Z → A)'),
    ('code_asc',     'Code (croissant)'),
    ('code_desc',    'Code (décroissant)'),
    ('length_asc',   'Longueur du texte (court → long)'),
    ('length_desc',  'Longueur du texte (long → court)'),
]

# Garde-fou sur l'ajout en masse (action guest_codes_bulk_add) : une ligne
# du textarea = une génération de code (jusqu'à 30 tentatives aléatoires
# chacune, voir generate_guest_code, db.py) + une écriture en base — borne
# le coût d'un collage accidentel massif plutôt que de le refuser en bloc.
_GUEST_CODES_BULK_MAX = 500


def _admin_guest_codes_redirect(success=None, error=None, sort='created_desc', q=''):
    """Redirection standard des actions CRUD/import/purge de cette page —
    préserve le tri et le filtre en cours (mêmes principe que
    _admin_emails_redirect ci-dessus), pour ne pas perdre la vue en cours
    après une action sur un très grand nombre de codes."""
    params = {'sort': sort}
    if q:
        params['q'] = q
    if success:
        params['ok'] = success
    if error:
        params['err'] = error
    return redirect(url_for('admin_guest_codes', **params))


# ── Génération de QR-codes imprimables (codes invités) ────────────────────────
# Un média par ligne (bouton à côté de chaque code) ou une archive ZIP pour
# toute la liste actuellement triée/filtrée — voir admin_guest_code_qr_file
# et admin_guest_codes_qr_archive plus bas. Le QR-code ENCODE toujours
# `row['code']` (jamais le texte associé) : c'est ce que /admin/guest_codes
# résout ensuite en texte via get_guest_code_text (db.py). Le texte
# optionnel n'est qu'une étiquette lisible imprimée à côté, pour l'humain.
#
# Réglages persistés (comme le reste de l'app) plutôt que ressaisis à
# chaque génération — un seul jeu de réglages pour le bouton unitaire ET
# l'archive complète, cohérent avec le principe déjà appliqué à l'export
# CSV (mêmes tri/filtre pour les deux).

_GUEST_CODES_QR_FORMATS = [
    ('png', 'PNG (image, fond opaque)'),
    ('jpg', 'JPG (image, fond opaque)'),
    ('svg', 'SVG (vectoriel)'),
]
_GUEST_CODES_QR_TEXT_CONTENTS = [
    ('texte', 'Le texte associé'),
    ('code',  'Le code numérique'),
    ('both',  'Les deux (texte — code)'),
]
# Sous-ensemble de _QR_LIVE_POSITIONS (sans 'center' : superposer du texte
# lisible sur le QR-code lui-même le rendrait illisible par un scanner).
_GUEST_CODES_QR_TEXT_POSITIONS = [
    ('below', 'En dessous du QR-code'),
    ('above', 'Au-dessus du QR-code'),
    ('left',  'À gauche du QR-code'),
    ('right', 'À droite du QR-code'),
]
_GUEST_CODES_QR_SIZE_UNITS = [('cm', 'cm'), ('mm', 'mm')]
# Au-delà, le texte affiché à côté du QR est tronqué (avec « … ») — sans
# cette borne, un texte du champ libre (jusqu'à 250 caractères) produirait
# un média disproportionné (voir _guest_code_qr_text_content).
_GUEST_CODES_QR_TEXT_MAX_CHARS = 60


def _guest_codes_qr_export_settings() -> dict:
    fmt = get_setting('guest_codes.qr_export.format', 'png')
    if fmt not in dict(_GUEST_CODES_QR_FORMATS):
        fmt = 'png'
    unit = get_setting('guest_codes.qr_export.size_unit', 'cm')
    if unit not in dict(_GUEST_CODES_QR_SIZE_UNITS):
        unit = 'cm'
    try:
        size_value = max(0.5, min(30.0, float(get_setting('guest_codes.qr_export.size_value', '3') or '3')))
    except (TypeError, ValueError):
        size_value = 3.0
    try:
        dpi = max(72, min(1200, int(float(get_setting('guest_codes.qr_export.dpi', '300') or '300'))))
    except (TypeError, ValueError):
        dpi = 300
    text_content = get_setting('guest_codes.qr_export.text_content', 'texte')
    if text_content not in dict(_GUEST_CODES_QR_TEXT_CONTENTS):
        text_content = 'texte'
    text_position = get_setting('guest_codes.qr_export.text_position', 'below')
    if text_position not in dict(_GUEST_CODES_QR_TEXT_POSITIONS):
        text_position = 'below'
    try:
        text_size_mm = max(1.0, min(30.0, float(get_setting('guest_codes.qr_export.text_size_mm', '4') or '4')))
    except (TypeError, ValueError):
        text_size_mm = 4.0
    try:
        text_gap_mm = max(0.0, min(30.0, float(get_setting('guest_codes.qr_export.text_gap_mm', '2') or '2')))
    except (TypeError, ValueError):
        text_gap_mm = 2.0
    return {
        'format':        fmt,
        'size_value':    size_value,
        'size_unit':     unit,
        'dpi':           dpi,
        'bg_color':      get_setting('guest_codes.qr_export.bg_color', '') or '#ffffff',
        'code_color':    get_setting('guest_codes.qr_export.code_color', '') or '#000000',
        'text_enabled':  get_setting('guest_codes.qr_export.text_enabled', '0') == '1',
        'text_content':  text_content,
        'text_position': text_position,
        'text_font':     get_setting('guest_codes.qr_export.text_font', '') or _PROMO_FONTS[0][0],
        'text_size_mm':  text_size_mm,
        'text_color':    get_setting('guest_codes.qr_export.text_color', '') or '#000000',
        'text_gap_mm':   text_gap_mm,
    }


def _guest_codes_qr_mm(size_value: float, unit: str) -> float:
    return size_value * 10.0 if unit == 'cm' else size_value


def _px_to_mm(px: float, dpi: int) -> float:
    return px / dpi * 25.4


def _slugify_filename_part(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-').lower()
    return text[:max_len] or 'code'


def _guest_code_qr_text_content(row: dict, mode: str) -> str:
    """Texte optionnel imprimé à côté du QR-code — tronqué au-delà de
    _GUEST_CODES_QR_TEXT_MAX_CHARS car ce texte dimensionne directement le
    média généré (voir _guest_code_qr_layout) : un texte trop long
    produirait un fichier disproportionné plutôt qu'un badge imprimable.
    Ne concerne jamais le contenu ENCODÉ dans le QR-code lui-même (toujours
    row['code'], voir _guest_code_qr_matrix)."""
    if mode == 'code':
        text = row['code']
    elif mode == 'both':
        text = f"{row['texte']} — {row['code']}"
    else:
        text = row['texte']
    text = (text or '').strip()
    if len(text) > _GUEST_CODES_QR_TEXT_MAX_CHARS:
        text = text[:_GUEST_CODES_QR_TEXT_MAX_CHARS].rstrip() + '…'
    return text


def _guest_code_qr_matrix(code: str):
    """Matrice de modules (liste de listes de bool, marge de sécurité — le
    « quiet zone » — incluse) du QR-code encodant `code`. Niveau de
    correction M (15 %) : marge raisonnable pour un média imprimé/manipulé,
    sans conséquence sur la taille vu le payload minuscule (2 à 10
    chiffres, voir l'analyse faite pour la fonctionnalité codes invités)."""
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(code)
    qr.make(fit=True)
    return qr.get_matrix()


def _guest_code_qr_layout(code: str, texte: str, settings: dict) -> dict:
    """Calcule, en pixels à la résolution settings['dpi'] (résolution de
    travail interne — sert aussi de base géométrique pour le SVG, où elle
    n'affecte pas la qualité puisque le rendu final est vectoriel), la
    disposition complète du média à générer : taille du QR, position du
    texte optionnel (mesurée via les vraies métriques de police, comme
    _qr_live_burn_draw_one plus haut) et taille finale du canevas. Point de
    calcul unique partagé par le rendu raster (PNG/JPG) et SVG, pour que
    les deux formats produisent exactement la même disposition."""
    dpi = settings['dpi']
    qr_size_mm = _guest_codes_qr_mm(settings['size_value'], settings['size_unit'])
    qr_size_px = max(10, round(qr_size_mm / 25.4 * dpi))
    matrix = _guest_code_qr_matrix(code)
    n = len(matrix)
    module_px = qr_size_px / n

    text = (_guest_code_qr_text_content({'code': code, 'texte': texte}, settings['text_content'])
            if settings['text_enabled'] else '')

    layout = {
        'dpi': dpi, 'matrix': matrix, 'module_px': module_px, 'qr_size_px': qr_size_px,
        'text': text, 'bg_color': settings['bg_color'], 'code_color': settings['code_color'],
        'text_color': settings['text_color'], 'text_position': settings['text_position'],
    }

    if not text:
        layout.update(canvas_w=qr_size_px, canvas_h=qr_size_px, qr_x=0, qr_y=0)
        return layout

    text_size_px = max(4, round(settings['text_size_mm'] / 25.4 * dpi))
    gap_px = round(settings['text_gap_mm'] / 25.4 * dpi)
    font = _qr_live_burn_font(settings['text_font'], text_size_px)
    probe = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    bbox = probe.textbbox((0, 0), text, font=font)
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])

    position = settings['text_position']
    if position in ('above', 'below'):
        canvas_w = max(qr_size_px, text_w)
        canvas_h = qr_size_px + gap_px + text_h
        qr_x = (canvas_w - qr_size_px) / 2
        if position == 'above':
            qr_y, text_top = text_h + gap_px, 0
        else:
            qr_y, text_top = 0, qr_size_px + gap_px
        text_x = canvas_w / 2 - text_w / 2 - bbox[0]
        text_y = text_top - bbox[1]
    else:  # left / right
        canvas_h = max(qr_size_px, text_h)
        canvas_w = qr_size_px + gap_px + text_w
        qr_y = (canvas_h - qr_size_px) / 2
        if position == 'left':
            text_left, qr_x = 0, text_w + gap_px
        else:
            text_left, qr_x = qr_size_px + gap_px, 0
        text_x = text_left - bbox[0]
        text_y = canvas_h / 2 - text_h / 2 - bbox[1]

    layout.update(canvas_w=round(canvas_w), canvas_h=round(canvas_h),
                   qr_x=round(qr_x), qr_y=round(qr_y), font=font,
                   text_x=round(text_x), text_y=round(text_y),
                   text_font_family=settings['text_font'], text_size_mm=settings['text_size_mm'])
    return layout


def _guest_code_qr_module_runs(matrix):
    """Fusionne les modules sombres consécutifs de chaque ligne en
    segments (row_index, col_start, col_end_exclusive) — moins de
    rectangles à dessiner/écrire (PNG et SVG), et aucune ligne de jointure
    visible entre deux modules adjacents de même couleur."""
    for row_i, row in enumerate(matrix):
        run_start = None
        for col_i, dark in enumerate(row + [False]):  # sentinelle : force la clôture du dernier segment
            if dark and run_start is None:
                run_start = col_i
            elif not dark and run_start is not None:
                yield row_i, run_start, col_i
                run_start = None


def _render_guest_code_qr_raster(layout: dict):
    canvas = Image.new('RGB', (layout['canvas_w'], layout['canvas_h']),
                        _hex_to_rgb(layout['bg_color'], (255, 255, 255)))
    draw = ImageDraw.Draw(canvas)
    module_px = layout['module_px']
    code_rgb = _hex_to_rgb(layout['code_color'], (0, 0, 0))
    for row_i, col_start, col_end in _guest_code_qr_module_runs(layout['matrix']):
        y0 = layout['qr_y'] + row_i * module_px
        y1 = y0 + module_px
        x0 = layout['qr_x'] + col_start * module_px
        x1 = layout['qr_x'] + col_end * module_px
        draw.rectangle([x0, y0, x1, y1], fill=code_rgb)
    if layout['text']:
        draw.text((layout['text_x'], layout['text_y']), layout['text'], font=layout['font'],
                   fill=_hex_to_rgb(layout['text_color'], (0, 0, 0)))
    return canvas


def _render_guest_code_qr_svg(layout: dict) -> str:
    """Même disposition que _render_guest_code_qr_raster (calculée une
    seule fois par _guest_code_qr_layout), convertie en mm — un utilisateur
    SVG (viewer, imprimante) affiche alors le média à sa taille physique
    réelle. Limite connue : le texte est un élément <text> natif (modifiable
    dans un éditeur vectoriel), rendu avec la police installée sur la
    machine qui ouvre le fichier — pas nécessairement identique au rendu
    PNG/JPG (polices Windows résolues côté serveur, voir _qr_live_burn_font)
    si cette police n'est pas installée chez le lecteur du SVG ; l'espacement
    (marge texte/QR) peut donc varier légèrement. Le centrage, lui, reste
    correct quelle que soit la police effectivement utilisée par le lecteur
    puisqu'il repose sur text-anchor/dominant-baseline (voir plus bas) plutôt
    que sur des coordonnées figées calculées pour une police précise."""
    from xml.sax.saxutils import escape
    dpi = layout['dpi']
    canvas_w_mm = _px_to_mm(layout['canvas_w'], dpi)
    canvas_h_mm = _px_to_mm(layout['canvas_h'], dpi)
    module_mm = _px_to_mm(layout['module_px'], dpi)
    qr_x_mm = _px_to_mm(layout['qr_x'], dpi)
    qr_y_mm = _px_to_mm(layout['qr_y'], dpi)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w_mm:.3f}mm" '
        f'height="{canvas_h_mm:.3f}mm" viewBox="0 0 {canvas_w_mm:.3f} {canvas_h_mm:.3f}">',
        f'<rect x="0" y="0" width="{canvas_w_mm:.3f}" height="{canvas_h_mm:.3f}" fill="{layout["bg_color"]}"/>',
    ]
    for row_i, col_start, col_end in _guest_code_qr_module_runs(layout['matrix']):
        x = qr_x_mm + col_start * module_mm
        y = qr_y_mm + row_i * module_mm
        w = (col_end - col_start) * module_mm
        parts.append(f'<rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{module_mm:.3f}" '
                     f'fill="{layout["code_color"]}"/>')
    if layout['text']:
        position = layout['text_position']
        # Ancrage self-centrant côté SVG plutôt que des coordonnées figées
        # calculées à partir de la largeur/hauteur mesurée côté serveur
        # (police Windows via PIL, voir _qr_live_burn_font) : le lecteur du
        # SVG peut ne pas avoir cette police installée et la substituer par
        # une autre, de largeur différente. Avec un x/y figé (ancien
        # comportement), tout écart de largeur décale visiblement le texte
        # par rapport au centre du QR-code — c'est ce qui produisait un
        # texte "au-dessus"/"en dessous" non centré signalé par l'utilisateur.
        # text-anchor="middle" (centrage horizontal) et dominant-baseline=
        # "central" (centrage vertical) laissent le moteur de rendu SVG
        # centrer lui-même les glyphes réellement affichés, quelle que soit
        # leur largeur/hauteur effective — donc toujours centré même si la
        # police diffère de celle utilisée pour le calcul de mise en page.
        if position in ('above', 'below'):
            text_anchor, text_x_mm = 'middle', canvas_w_mm / 2
        else:
            text_anchor, text_x_mm = 'start', _px_to_mm(layout['text_x'], dpi)
        if position in ('left', 'right'):
            dominant_baseline, text_y_mm = 'central', canvas_h_mm / 2
        else:
            dominant_baseline, text_y_mm = 'hanging', _px_to_mm(layout['text_y'], dpi)
        # font-weight="bold" : aligné sur le rendu PNG/JPG, qui utilise
        # toujours la variante grasse des polices Windows (voir
        # _QR_LIVE_BURN_FONT_FILES_BY_INDEX) — sans quoi le texte SVG,
        # rendu en graisse normale, serait plus étroit que prévu.
        parts.append(
            f"<text x=\"{text_x_mm:.3f}\" y=\"{text_y_mm:.3f}\" "
            f"font-family='{layout['text_font_family']}' font-weight=\"bold\" "
            f"font-size=\"{layout['text_size_mm']:.3f}mm\" "
            f"fill=\"{layout['text_color']}\" dominant-baseline=\"{dominant_baseline}\" "
            f"text-anchor=\"{text_anchor}\">{escape(layout['text'])}</text>"
        )
    parts.append('</svg>')
    return '\n'.join(parts)


def _guest_code_qr_bytes(row: dict, settings: dict):
    """Retourne (bytes, mimetype, extension) pour l'export imprimable d'un
    code invité — utilisé par le téléchargement unitaire ET par l'archive
    ZIP (mêmes réglages courants pour les deux, voir _guest_codes_qr_export_settings)."""
    layout = _guest_code_qr_layout(row['code'], row['texte'], settings)
    if settings['format'] == 'svg':
        return _render_guest_code_qr_svg(layout).encode('utf-8'), 'image/svg+xml', 'svg'
    canvas = _render_guest_code_qr_raster(layout)
    buf = io.BytesIO()
    if settings['format'] == 'jpg':
        canvas.save(buf, format='JPEG', quality=92)
        return buf.getvalue(), 'image/jpeg', 'jpg'
    canvas.save(buf, format='PNG')
    return buf.getvalue(), 'image/png', 'png'


def _guest_code_qr_filename(row: dict, ext: str) -> str:
    return f"{row['code']}_{_slugify_filename_part(row['texte'])}.{ext}"


@app.route('/admin/guest_codes/<int:guest_code_id>/qr-file')
@require_admin_auth
def admin_guest_code_qr_file(guest_code_id):
    row = get_guest_code_by_id(guest_code_id)
    if not row:
        abort(404)
    data, mimetype, ext = _guest_code_qr_bytes(row, _guest_codes_qr_export_settings())
    return Response(data, mimetype=mimetype, headers={
        'Content-Disposition': f'attachment; filename="{_guest_code_qr_filename(row, ext)}"'
    })


@app.route('/admin/guest_codes/qr-archive.zip')
@require_admin_auth
def admin_guest_codes_qr_archive():
    sort = request.args.get('sort', 'created_desc')
    if sort not in dict(_GUEST_CODES_SORTS):
        sort = 'created_desc'
    q = request.args.get('q', '')
    rows = list_guest_codes(sort=sort, q=q)
    if not rows:
        return _admin_guest_codes_redirect(error='Aucun code invité à exporter.', sort=sort, q=q)
    settings = _guest_codes_qr_export_settings()
    buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            data, _mimetype, ext = _guest_code_qr_bytes(row, settings)
            name = _guest_code_qr_filename(row, ext)
            if name in used_names:  # collision improbable (code toujours unique) — filet de sécurité
                name = f"{row['id']}_{name}"
            used_names.add(name)
            zf.writestr(name, data)
    buf.seek(0)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    logger.info('Codes invités : archive QR générée (%d fichier(s), format %s).', len(rows), settings['format'])
    return Response(buf.getvalue(), mimetype='application/zip', headers={
        'Content-Disposition': f'attachment; filename="codes-invites-qr-{stamp}.zip"'
    })


@app.route('/admin/guest_codes', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes():
    """Tuile « Codes invités ». « Réglages », « Génération de QR-codes
    imprimables », « Purger par plage de dates », « Purger les N premiers »,
    « Add-on — Détection de QR-codes » et « Apparence du QR-code affiché en
    direct » (qui fusionne qrcode_live_style et qrcode_live_error_style : le
    message d'erreur lit en direct forme/taille/marge du premier formulaire,
    voir templates/blocks/guest_codes_qr_live.html) ont chacune leur propre
    route dédiée ci-dessous et rejoignent le système de blocs. « Codes
    invités » (CRUD + import CSV) reste ici, hors du système de blocs : ce
    n'est pas un réglage mais la gestion du contenu lui-même (comme
    admin_captures/admin_emails)."""
    if request.method == 'POST':
        action = request.form.get('action', 'guest_code_new')
        sort = request.form.get('sort', 'created_desc')
        q = request.form.get('q', '')

        if action == 'guest_code_new':
            texte = (request.form.get('texte') or '').strip()[:250]
            if not texte:
                return _admin_guest_codes_redirect(error='Le texte est obligatoire.', sort=sort, q=q)
            row = create_guest_code(texte, _guest_codes_settings()['code_length'])
            return _admin_guest_codes_redirect(success=f"Code « {row['code']} » créé.", sort=sort, q=q)

        if action == 'guest_codes_bulk_add':
            # Une ligne = un texte -> exactement le même chemin que
            # guest_code_new ci-dessus (create_guest_code génère le code
            # aléatoire à la longueur réglée et horodate created_at à
            # l'instant présent), simplement répété par ligne non vide.
            raw_lines = (request.form.get('bulk_texts') or '').splitlines()
            length = _guest_codes_settings()['code_length']
            created, truncated = 0, False
            for line in raw_lines:
                texte = line.strip()[:250]
                if not texte:
                    continue
                if created >= _GUEST_CODES_BULK_MAX:
                    truncated = True
                    break
                create_guest_code(texte, length)
                created += 1
            if created == 0:
                return _admin_guest_codes_redirect(error='Aucun texte valide dans la liste.', sort=sort, q=q)
            msg = f'{created} code(s) créé(s) à partir de la liste.'
            if truncated:
                msg += f' Limité aux {_GUEST_CODES_BULK_MAX} premières lignes non vides.'
            return _admin_guest_codes_redirect(success=msg, sort=sort, q=q)

        if action == 'guest_code_edit':
            guest_code_id = int(request.form.get('guest_code_id', 0))
            if not get_guest_code_by_id(guest_code_id):
                abort(404)
            texte = (request.form.get('texte') or '').strip()[:250]
            if not texte:
                return _admin_guest_codes_redirect(error='Le texte est obligatoire.', sort=sort, q=q)
            update_guest_code_texte(guest_code_id, texte)
            return _admin_guest_codes_redirect(success='Code invité mis à jour.', sort=sort, q=q)

        if action == 'guest_code_regenerate':
            guest_code_id = int(request.form.get('guest_code_id', 0))
            new_code = regenerate_guest_code(guest_code_id, _guest_codes_settings()['code_length'])
            if new_code is None:
                return _admin_guest_codes_redirect(error='Code introuvable.', sort=sort, q=q)
            return _admin_guest_codes_redirect(success=f'Nouveau code généré : {new_code}.', sort=sort, q=q)

        if action == 'guest_code_delete':
            guest_code_id = int(request.form.get('guest_code_id', 0))
            row = delete_guest_code_db(guest_code_id)
            if row:
                return _admin_guest_codes_redirect(success=f"Code « {row['code']} » supprimé.", sort=sort, q=q)
            return _admin_guest_codes_redirect(error='Code introuvable.', sort=sort, q=q)

        if action == 'guest_codes_import':
            file = request.files.get('csv_file')
            if not file or not file.filename:
                return _admin_guest_codes_redirect(error='Aucun fichier CSV sélectionné.', sort=sort, q=q)
            try:
                raw = file.read().decode('utf-8-sig')
            except UnicodeDecodeError:
                return _admin_guest_codes_redirect(
                    error="Fichier illisible : encodage non reconnu (attendu UTF-8).", sort=sort, q=q)
            reader = csv.DictReader(io.StringIO(raw))
            fieldnames = [(name or '').strip().lower() for name in (reader.fieldnames or [])]
            if 'texte' not in fieldnames:
                return _admin_guest_codes_redirect(
                    error="CSV invalide : colonne « texte » manquante (attendu : texte,code,created_at).",
                    sort=sort, q=q)
            length = _guest_codes_settings()['code_length']
            created = updated = skipped = 0
            for raw_row in reader:
                row = {(k or '').strip().lower(): v for k, v in raw_row.items()}
                texte = (row.get('texte') or '').strip()[:250]
                if not texte:
                    skipped += 1
                    continue
                code = (row.get('code') or '').strip()
                created_at = (row.get('created_at') or '').strip() or None
                if code:
                    if not re.fullmatch(r'\d{2,10}', code):
                        skipped += 1  # code non conforme (pas uniquement des chiffres, ou longueur hors 2-10)
                        continue
                    _id, kind = upsert_guest_code(code, texte, created_at)
                else:
                    code = generate_guest_code(length)
                    upsert_guest_code(code, texte, created_at)
                    kind = 'created'
                if kind == 'created':
                    created += 1
                else:
                    updated += 1
            logger.info('Codes invités : import CSV — %d créé(s), %d mis à jour, %d ignoré(s).',
                        created, updated, skipped)
            msg = f'Import terminé : {created} créé(s), {updated} mis à jour, {skipped} ignoré(s).'
            return _admin_guest_codes_redirect(success=msg, sort=sort, q=q)

        abort(404)

    blocks, block_context = _admin_render_blocks('guest_codes')
    list_sort = request.args.get('sort', 'created_desc')
    if list_sort not in dict(_GUEST_CODES_SORTS):
        list_sort = 'created_desc'
    list_q = request.args.get('q', '')
    return render_template(
        'admin_guest_codes.html', config=CONFIG,
        blocks=blocks, current_page='guest_codes', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('guest_codes', 'Codes invités'),
        guest_codes=list_guest_codes(sort=list_sort, q=list_q),
        guest_codes_sorts=_GUEST_CODES_SORTS,
        guest_codes_bulk_max=_GUEST_CODES_BULK_MAX,
        guest_codes_sort=list_sort,
        guest_codes_q=list_q,
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/guest_codes/code_settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_set_code_settings():
    sort = request.form.get('sort', 'created_desc')
    q = request.form.get('q', '')
    raw_len = (request.form.get('code_length') or '').strip()
    if raw_len.isdigit() and 2 <= int(raw_len) <= 10:
        set_setting('guest_codes.code_length', raw_len)
    return _admin_block_redirect('guest_codes_code_settings', sort=sort, q=q, ok='Réglages mis à jour.')


@app.route('/admin/guest_codes/qr_export_settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_set_qr_export_settings():
    sort = request.form.get('sort', 'created_desc')
    q = request.form.get('q', '')
    fmt = request.form.get('format', 'png')
    if fmt in dict(_GUEST_CODES_QR_FORMATS):
        set_setting('guest_codes.qr_export.format', fmt)
    unit = request.form.get('size_unit', 'cm')
    if unit in dict(_GUEST_CODES_QR_SIZE_UNITS):
        set_setting('guest_codes.qr_export.size_unit', unit)
    raw_size = (request.form.get('size_value') or '').strip()
    try:
        if 0.5 <= float(raw_size) <= 30:
            set_setting('guest_codes.qr_export.size_value', raw_size)
    except ValueError:
        pass
    raw_dpi = (request.form.get('dpi') or '').strip()
    if raw_dpi.isdigit() and 72 <= int(raw_dpi) <= 1200:
        set_setting('guest_codes.qr_export.dpi', raw_dpi)
    bg_color = (request.form.get('bg_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', bg_color):
        set_setting('guest_codes.qr_export.bg_color', bg_color)
    code_color = (request.form.get('code_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', code_color):
        set_setting('guest_codes.qr_export.code_color', code_color)
    set_setting('guest_codes.qr_export.text_enabled', '1' if request.form.get('text_enabled') else '0')
    text_content = request.form.get('text_content', 'texte')
    if text_content in dict(_GUEST_CODES_QR_TEXT_CONTENTS):
        set_setting('guest_codes.qr_export.text_content', text_content)
    text_position = request.form.get('text_position', 'below')
    if text_position in dict(_GUEST_CODES_QR_TEXT_POSITIONS):
        set_setting('guest_codes.qr_export.text_position', text_position)
    text_font = request.form.get('text_font', '')
    if text_font in dict(_all_fonts()):
        set_setting('guest_codes.qr_export.text_font', text_font)
    raw_text_size = (request.form.get('text_size_mm') or '').strip()
    try:
        if 1 <= float(raw_text_size) <= 30:
            set_setting('guest_codes.qr_export.text_size_mm', raw_text_size)
    except ValueError:
        pass
    text_color = (request.form.get('text_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', text_color):
        set_setting('guest_codes.qr_export.text_color', text_color)
    raw_gap = (request.form.get('text_gap_mm') or '').strip()
    try:
        if 0 <= float(raw_gap) <= 30:
            set_setting('guest_codes.qr_export.text_gap_mm', raw_gap)
    except ValueError:
        pass
    return _admin_block_redirect('guest_codes_qr_export', sort=sort, q=q, ok='Réglages de génération QR mis à jour.')


@app.route('/admin/guest_codes/purge_date', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_purge_date():
    sort = request.form.get('sort', 'created_desc')
    q = request.form.get('q', '')
    date_from = (request.form.get('purge_date_from') or '').strip()
    date_to = (request.form.get('purge_date_to') or '').strip()
    if not date_from and not date_to:
        return _admin_block_redirect('guest_codes_purge_date', sort=sort, q=q,
                                      err='Indiquez au moins une date (début ou fin).')
    count = purge_guest_codes_by_date(date_from, date_to)
    return _admin_block_redirect('guest_codes_purge_date', sort=sort, q=q, ok=f'{count} code(s) supprimé(s).')


@app.route('/admin/guest_codes/purge_first_n', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_purge_first_n():
    sort = request.form.get('sort', 'created_desc')
    q = request.form.get('q', '')
    raw_n = (request.form.get('purge_n') or '').strip()
    purge_sort = request.form.get('purge_sort', 'created_asc')
    if purge_sort not in dict(_GUEST_CODES_SORTS):
        purge_sort = 'created_asc'
    if not raw_n.isdigit() or int(raw_n) < 1:
        return _admin_block_redirect('guest_codes_purge_first_n', sort=sort, q=q, err='Nombre invalide.')
    count = purge_guest_codes_first_n(int(raw_n), purge_sort)
    return _admin_block_redirect('guest_codes_purge_first_n', sort=sort, q=q, ok=f'{count} code(s) supprimé(s).')


@app.route('/admin/guest_codes/qrcode_settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_set_qrcode_settings():
    set_setting('qrcode.enabled', '1' if request.form.get('enabled') else '0')
    set_setting('qrcode.live_overlay', '1' if request.form.get('live_overlay') else '0')
    set_setting('qrcode.burn_into_media.photo', '1' if request.form.get('burn_photo') else '0')
    set_setting('qrcode.burn_into_media.video', '1' if request.form.get('burn_video') else '0')
    set_setting('qrcode.burn_into_media.strip', '1' if request.form.get('burn_strip') else '0')
    return _admin_block_redirect('guest_codes_qrcode_settings', ok='Réglages QR-code mis à jour.')


@app.route('/admin/guest_codes/qrcode_live_style', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_set_qrcode_live_style():
    bg_mode = request.form.get('bg_mode', 'shape')
    set_setting('qrcode.live_style.bg_mode', bg_mode if bg_mode in ('shape', 'image') else 'shape')
    bg_shape = request.form.get('bg_shape', 'pill')
    set_setting('qrcode.live_style.bg_shape', bg_shape if bg_shape in _QR_LIVE_SHAPE_CSS else 'pill')
    raw_bg_size = (request.form.get('bg_size_pct') or '100').strip()
    set_setting('qrcode.live_style.bg_size_pct',
                str(max(50, min(300, int(raw_bg_size)))) if raw_bg_size.isdigit() else '100')
    raw_bg_width = (request.form.get('bg_width_px') or '').strip()
    set_setting('qrcode.live_style.bg_width_px',
                str(max(20, min(800, int(raw_bg_width)))) if raw_bg_width.isdigit() else '')
    raw_bg_height = (request.form.get('bg_height_px') or '').strip()
    set_setting('qrcode.live_style.bg_height_px',
                str(max(20, min(800, int(raw_bg_height)))) if raw_bg_height.isdigit() else '')
    raw_text_margin = (request.form.get('text_margin_px') or '0').strip()
    set_setting('qrcode.live_style.text_margin_px',
                str(max(0, min(60, int(raw_text_margin)))) if raw_text_margin.isdigit() else '0')
    set_setting('qrcode.live_style.bg_proportional',
                '1' if request.form.get('bg_proportional') else '0')
    raw_prop_adjust = (request.form.get('bg_proportional_adjust_pct') or '0').strip()
    try:
        prop_adjust = max(-50, min(50, int(raw_prop_adjust)))
    except ValueError:
        prop_adjust = 0
    set_setting('qrcode.live_style.bg_proportional_adjust_pct', str(prop_adjust))
    bg_color = (request.form.get('bg_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', bg_color):
        set_setting('qrcode.live_style.bg_color', bg_color)
    font_value = request.form.get('font', '')
    if font_value in dict(_all_fonts()):
        set_setting('qrcode.live_style.font', font_value)
    raw_size = (request.form.get('font_size') or '').strip()
    if raw_size.isdigit() and int(raw_size) >= 8:
        set_setting('qrcode.live_style.font_size', raw_size)
    text_color = (request.form.get('text_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', text_color):
        set_setting('qrcode.live_style.text_color', text_color)
    position = request.form.get('position', 'above')
    set_setting('qrcode.live_style.position', position if position in dict(_QR_LIVE_POSITIONS) else 'above')

    file = request.files.get('bg_image')
    if file and file.filename:
        ext = Path(file.filename).suffix.lower()
        if ext not in _QR_LIVE_ALLOWED_EXT:
            return _admin_block_redirect('guest_codes_qr_live',
                                          err="Format non supporté pour l'image de fond (PNG, JPG, WEBP).")
        QR_LIVE_DIR.mkdir(parents=True, exist_ok=True)
        old = get_setting('qrcode.live_style.bg_image_filename', '')
        if old:
            (QR_LIVE_DIR / old).unlink(missing_ok=True)
        safe = f'qr-live-bg-{int(datetime.now().timestamp() * 1000)}{ext}'
        file.save(str(QR_LIVE_DIR / safe))
        set_setting('qrcode.live_style.bg_image_filename', safe)
    elif request.form.get('bg_image_delete'):
        old = get_setting('qrcode.live_style.bg_image_filename', '')
        if old:
            (QR_LIVE_DIR / old).unlink(missing_ok=True)
            set_setting('qrcode.live_style.bg_image_filename', '')

    return _admin_block_redirect('guest_codes_qr_live', ok='Apparence du texte QR-code mise à jour.')


@app.route('/admin/guest_codes/qrcode_live_error_style', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_codes_set_qrcode_live_error_style():
    set_setting('qrcode.live_error_style.enabled',
                '1' if request.form.get('error_enabled') else '0')
    error_mode = request.form.get('error_mode', 'text')
    set_setting('qrcode.live_error_style.mode', error_mode if error_mode in ('text', 'image') else 'text')
    error_text = (request.form.get('error_text') or '').strip()
    set_setting('qrcode.live_error_style.text', error_text or 'QR-code détecté mais illisible')
    error_font_value = request.form.get('error_font', '')
    if error_font_value in dict(_all_fonts()):
        set_setting('qrcode.live_error_style.font', error_font_value)
    raw_error_size = (request.form.get('error_font_size') or '').strip()
    if raw_error_size.isdigit() and int(raw_error_size) >= 8:
        set_setting('qrcode.live_error_style.font_size', raw_error_size)
    error_text_color = (request.form.get('error_text_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', error_text_color):
        set_setting('qrcode.live_error_style.text_color', error_text_color)
    error_bg_color = (request.form.get('error_bg_color') or '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', error_bg_color):
        set_setting('qrcode.live_error_style.bg_color', error_bg_color)

    error_file = request.files.get('error_image')
    if error_file and error_file.filename:
        ext = Path(error_file.filename).suffix.lower()
        if ext not in _QR_LIVE_ALLOWED_EXT:
            return _admin_block_redirect('guest_codes_qr_live',
                                          err="Format non supporté pour l'image du message d'erreur (PNG, JPG, WEBP).")
        QR_LIVE_DIR.mkdir(parents=True, exist_ok=True)
        old = get_setting('qrcode.live_error_style.image_filename', '')
        if old:
            (QR_LIVE_DIR / old).unlink(missing_ok=True)
        safe = f'qr-live-error-{int(datetime.now().timestamp() * 1000)}{ext}'
        error_file.save(str(QR_LIVE_DIR / safe))
        set_setting('qrcode.live_error_style.image_filename', safe)
    elif request.form.get('error_image_delete'):
        old = get_setting('qrcode.live_error_style.image_filename', '')
        if old:
            (QR_LIVE_DIR / old).unlink(missing_ok=True)
            set_setting('qrcode.live_error_style.image_filename', '')

    return _admin_block_redirect('guest_codes_qr_live', ok="Message d'erreur QR-code mis à jour.")


@app.route('/admin/guest_codes/export/csv')
@require_admin_auth
def admin_guest_codes_export_csv():
    """Export CSV des codes invités — mêmes tri/filtre que la liste
    actuellement affichée (query params sort/q), même principe que
    admin_emails_export_csv ci-dessus. Colonnes réimportables telles
    quelles par l'action 'guest_codes_import'."""
    sort = request.args.get('sort', 'created_desc')
    if sort not in dict(_GUEST_CODES_SORTS):
        sort = 'created_desc'
    q = request.args.get('q', '')
    rows = list_guest_codes(sort=sort, q=q)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['texte', 'code', 'created_at'])
    writer.writeheader()
    writer.writerows({'texte': r['texte'], 'code': r['code'], 'created_at': r['created_at']} for r in rows)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=codes-invites.csv'})


# ── Admin — boutons d'action (kiosque) ────────────────────────────────────────

@app.route('/admin/buttons')
@require_admin_auth
def admin_buttons():
    """Tuile « Boutons ». Pas de POST ici : l'unique bloc de cette page
    poste vers sa propre route dédiée ci-dessous. « Réglages communs » et
    « Réglages par bouton » restent un SEUL bloc (et non 2) car l'aperçu
    en direct (voir templates/blocks/buttons_style.html) lit les deux
    ensembles de champs à la fois — les séparer sur des tuiles
    différentes casserait cet aperçu."""
    blocks, block_context = _admin_render_blocks('buttons')
    return render_template(
        'admin_buttons.html', config=CONFIG,
        blocks=blocks, current_page='buttons', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('buttons', 'Boutons'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/buttons/style', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_buttons_set_style():
    shape = request.form.get('shape', 'pill')
    if shape in _BUTTON_SHAPES:
        set_setting('buttons.shape', shape)
    font_value = request.form.get('font', '')
    if font_value in dict(_all_fonts()):
        set_setting('buttons.font', font_value)
    raw_fs = (request.form.get('font_size') or '').strip()
    if raw_fs.isdigit() and int(raw_fs) >= 10:
        set_setting('buttons.font_size', raw_fs)
    raw_py = (request.form.get('padding_y') or '').strip()
    if raw_py.isdigit():
        set_setting('buttons.padding_y', raw_py)
    raw_px = (request.form.get('padding_x') or '').strip()
    if raw_px.isdigit():
        set_setting('buttons.padding_x', raw_px)
    for key, _label, _default_bg in _BUTTON_ROLES:
        bg_value = request.form.get(f'{key}_bg', '').strip()
        if re.fullmatch(r'#[0-9a-fA-F]{6}', bg_value):
            set_setting(f'buttons.{key}_bg', bg_value)
        set_setting(f'buttons.{key}_bold', '1' if request.form.get(f'{key}_bold') else '0')
    return _admin_block_redirect('buttons_style', ok='Paramètres mis à jour.')


# ── Slideshow /bestof ─────────────────────────────────────────────────────────

SLIDESHOW_DIR = BASE_DIR / 'app' / 'static' / 'slideshow'
_SLIDESHOW_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}

# ── Pages promo (info QR) ────────────────────────────────────────────────────
# CRUD v2.0 : plusieurs pages promo (fond + texte WYSIWYG + QR code) en
# rotation dans le diaporama /bestof pour informer les invités : présence du
# photobox, galerie en ligne avec vote, upload de photos depuis smartphone —
# ou tout autre message/QR défini par l'admin. Chaque page a sa propre
# fréquence d'apparition, son temps de pause, son effet visuel. Rendues côté
# client (bestof.html) à partir de _promo_page_public() ci-dessous — pas
# d'image à régénérer côté serveur pour le fond/texte, seul le QR est un PNG
# généré à la volée (voir promo_qr_inline_png / _resolve_inline_qrcodes,
# balise {qrcode=...} dans le texte WYSIWYG). CRUD complet : db.py
# (list_promo_pages, create_promo_page, update_promo_page,
# delete_promo_page_db, move_promo_page, list_promo_backgrounds,
# add_promo_background, delete_promo_background_db).

PROMO_DIR = BASE_DIR / 'app' / 'static' / 'promo'
_PROMO_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp'}
# v2.0.8 : bibliothèque d'images dédiées au TEXTE des pages promo (voir
# promo_content_images, db.py) -- dossier séparé de PROMO_DIR ci-dessus
# (réservé aux fonds plein écran) pour ne jamais mélanger les deux
# bibliothèques, bien que gérées par le même formulaire de page promo.
PROMO_CONTENT_DIR = BASE_DIR / 'app' / 'static' / 'promo_content'
_PROMO_FONTS = [
    ('system-ui, "Segoe UI", sans-serif', 'Par défaut (Segoe UI)'),
    ('Georgia, "Times New Roman", serif', 'Georgia (serif)'),
    ('"Trebuchet MS", sans-serif',        'Trebuchet MS'),
    ('Impact, "Arial Narrow", sans-serif', 'Impact'),
    ('"Courier New", monospace',          'Courier New'),
    ('"Comic Sans MS", cursive',          'Comic Sans MS'),
]
# Tailles proposées par le sélecteur "Taille" de la barre d'outils WYSIWYG
# (voir static/promo-editor.js -- attributor `size` enregistré SANS liste
# blanche, donc n'importe quelle valeur reste utilisable en mode source ;
# cette liste ne sert qu'à peupler le <select> de raccourcis). Remplace
# l'ancien champ indépendant "Taille du texte (px)" (min 10, max 120).
_PROMO_TEXT_SIZES = [12, 14, 16, 18, 20, 24, 28, 32, 36, 42, 48, 56, 64, 80, 96, 120]
# Effets visuels disponibles à l'apparition d'une page promo (voir bestof.html
# → .slide-promo.effect-* pour les animations CSS correspondantes). 'fade' ne
# rajoute rien : le fondu-enchaîné entre slides existe déjà globalement.
_PROMO_EFFECTS = [
    ('fade',     'Fondu (par défaut)'),
    ('slide-up', 'Glissement vers le haut'),
    ('zoom-in',  'Zoom avant'),
    ('kenburns', 'Ken Burns (fond en lent zoom continu)'),
    ('bounce',   'Rebond'),
]


def _promo_media_library() -> list:
    """Médias « site » proposés par le sélecteur d'image du WYSIWYG des
    pages promo (voir static/promo-editor.js) : le contenu de la tuile «
    Médiathèque » (fond d'écran Windows, images intermédiaires, images de
    l'écran de veille) et la bibliothèque de fonds des pages promo elle-même
    — pas de nouvelle source de données, une vue agrégée de CRUD déjà
    existants."""
    items = []
    for img in list_wallpaper_images():
        items.append({'url': f"/static/wallpapers/{img['filename']}", 'label': img['filename']})
    for img in list_slideshow_images():
        items.append({'url': f"/static/slideshow/{img['filename']}", 'label': img['filename']})
    for img in list_screensaver_images():
        items.append({'url': f"/static/screensaver/{img['filename']}", 'label': img['filename']})
    for bg in list_promo_backgrounds():
        if (bg.get('kind') or 'image') == 'image' and bg.get('filename'):
            items.append({'url': f"/static/promo/{bg['filename']}", 'label': bg.get('label') or bg['filename']})
    return items


def _promo_capture_library(limit: int = 60) -> list:
    """Dernières captures visiteurs (photos), 2e source du sélecteur d'image
    du WYSIWYG — vignette via /media/thumb (légère), insertion de l'URL
    /media/photo (pleine résolution) dans le contenu. BUG corrigé : la
    vignette d'une capture est stockée sous un nom DIFFÉRENT de la photo
    elle-même (colonne dédiée thumb_filename, voir record_capture/db.py) --
    interroger /media/thumb avec `filename` (comme avant ce correctif)
    pointait vers un fichier inexistant dans THUMBS_DIR et cassait
    systématiquement l'affichage. Repli sur la photo elle-même si aucune
    vignette n'a été générée (thumb_filename NULL)."""
    with closing(db_conn()) as conn:
        rows = conn.execute(
            "SELECT filename, thumb_filename FROM captures WHERE kind = 'photo' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            'url':       url_for('media_photo', filename=r['filename']),
            'thumb_url': (url_for('media_thumb', filename=r['thumb_filename'])
                          if r['thumb_filename'] else url_for('media_photo', filename=r['filename'])),
            'label':     r['filename'],
        }
        for r in rows
    ]


def _promo_page_public(page: dict) -> dict:
    """Représentation JSON d'une page promo, partagée par /api/bestof/slides
    (rafraîchissement complet, cadencé par slideshow.refresh_interval) et
    /api/bestof/promo-pages (interrogée à cadence fixe et rapide par le
    kiosque déjà ouvert, pour appliquer les changements du back office sans
    attendre — voir bestof.html)."""
    return {
        'id':              page['id'],
        'frequency':       max(1, page['frequency']),
        'pause_seconds':   max(1, page['pause_seconds']),
        'background_kind':   page.get('background_kind') or '',
        'background_url':  (url_for('static', filename=f'promo/{page["background_filename"]}')
                             if page.get('background_kind') == 'image' and page['background_filename'] else ''),
        'background_color1': page.get('background_color1') or '',
        'background_color2': page.get('background_color2') or '',
        'background_angle':  page.get('background_angle') or 135,
        'background_bg_color': page.get('background_bg_color') or '#14161a',
        'overlay_enabled': bool(page['overlay_enabled']),
        # v2.0.6 : 4 marges indépendantes (remplace l'ancien réglage unique
        # content_padding, voir db.py) -- voir /admin/slideshow -> Pages promo.
        'content_padding_top':    page.get('content_padding_top', 60),
        'content_padding_right':  page.get('content_padding_right', 60),
        'content_padding_bottom': page.get('content_padding_bottom', 60),
        'content_padding_left':   page.get('content_padding_left', 60),
        # v2.0.4 : text_font/text_size/text_color ne sont plus exposés ici --
        # la mise en forme du texte vit désormais entièrement dans
        # html_content (styles en ligne posés par le WYSIWYG), voir
        # admin_promo_page_update ci-dessus.
        'html_content':    _resolve_inline_qrcodes(resolve_dynamic_placeholders(page['html_content'] or '')),
        'effect':          page['effect'],
        'custom_css':      page.get('custom_css') or '',
    }


_INLINE_QR_HEX_RE = re.compile(r'#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})')


@app.route('/promo/qr-inline.png')
def promo_qr_inline_png():
    """QR code inline pour la balise {qrcode="...", taille="...", color="...",
    bgcolor="..."} saisie directement dans le texte WYSIWYG d'une page promo
    (voir _resolve_inline_qrcodes ci-dessous, qui construit cette URL avec des
    paramètres déjà validés). Remplace l'ancienne route dédiée par page
    /promo/qr/<id>.png. Public (comme /qr.png) : affiché sur /bestof, sans
    authentification -- accepte du texte/couleurs arbitraires en paramètres
    (simple génération d'image, aucune donnée admin en jeu), plafonné pour
    éviter tout abus."""
    data = (request.args.get('text', '') or '').strip()[:1000] or build_gallery_url()
    color = request.args.get('color', '#000000') or '#000000'
    if not _INLINE_QR_HEX_RE.fullmatch(color):
        color = '#000000'
    bgcolor = request.args.get('bgcolor', '#ffffff') or '#ffffff'
    if not _INLINE_QR_HEX_RE.fullmatch(bgcolor):
        bgcolor = '#ffffff'
    return send_file(generate_qr_png_custom(data, fill_color=color, back_color=bgcolor),
                      mimetype='image/png', download_name='promo-qr.png')


# Le guillemet délimiteur peut être un " brut (texte tapé hors du WYSIWYG,
# tests) ou &quot; (texte tel que stocké réellement : sanitize_promo_html,
# utils.py, échappe tout texte via html.escape() avant stockage -- voir
# _PromoHtmlSanitizer.handle_data). D'où l'alternative dans les deux regex
# ci-dessous, et le html_unescape() sur chaque valeur extraite.
_INLINE_QR_QUOTE = r'(?:"|&quot;)'
_INLINE_QR_RE = re.compile(
    r'\{qrcode\s*=\s*' + _INLINE_QR_QUOTE + r'(.*?)' + _INLINE_QR_QUOTE + r'([^}]*)\}',
    re.S,
)
_INLINE_QR_ATTR_RE = re.compile(
    r'(\w+)\s*=\s*' + _INLINE_QR_QUOTE + r'(.*?)' + _INLINE_QR_QUOTE,
    re.S,
)


def _resolve_inline_qrcodes(html: str) -> str:
    """Résout, dans le texte WYSIWYG déjà passé par resolve_dynamic_placeholders,
    toute balise {qrcode="texte", taille="150", color="#000000",
    bgcolor="#ffffff"} en une image <img> pointant vers /promo/qr-inline.png
    -- remplace l'ancien système de QR dédié par page (un seul QR, position
    fixe, réglages séparés dans l'admin, route promo_qr_png aujourd'hui
    supprimée). L'admin peut désormais placer, dans le
    texte lui-même, autant de QR codes qu'elle veut, où elle veut (comme une
    image insérée). Résolu uniquement à l'affichage public, jamais stocké
    résolu ni appliqué en relecture du formulaire admin -- voir l'appelant
    (_promo_page_public), jamais admin_promo_page_update."""
    if not html or '{qrcode' not in html:
        return html or ''

    def _replace(m):
        raw_text = html_unescape(m.group(1) or '')
        attrs = {k: html_unescape(v) for k, v in _INLINE_QR_ATTR_RE.findall(m.group(2) or '')}
        text = resolve_dynamic_placeholders(raw_text) if raw_text.strip() else ''

        raw_size = (attrs.get('taille') or attrs.get('size') or '150').strip()
        size = int(raw_size) if raw_size.isdigit() else 150
        size = max(30, min(1000, size))

        color = (attrs.get('color') or '#000000').strip()
        if not _INLINE_QR_HEX_RE.fullmatch(color):
            color = '#000000'
        bgcolor = (attrs.get('bgcolor') or '#ffffff').strip()
        if not _INLINE_QR_HEX_RE.fullmatch(bgcolor):
            bgcolor = '#ffffff'

        url = url_for('promo_qr_inline_png', text=text, color=color, bgcolor=bgcolor)
        return (f'<img class="promo-inline-qr" src="{url}" width="{size}" height="{size}" '
                f'alt="QR code" style="vertical-align:middle">')

    return _INLINE_QR_RE.sub(_replace, html)


def _slideshow_settings():
    return {
        'type':            get_setting('slideshow.type',           'both'),
        'delay':           int(get_setting('slideshow.delay',      '5')),
        'order':           get_setting('slideshow.order',          'chrono'),
        'date_from':       get_setting('slideshow.date_from',      ''),
        'date_to':         get_setting('slideshow.date_to',        ''),
        'vote_min':         get_setting('slideshow.vote_min',          ''),
        'vote_max':         get_setting('slideshow.vote_max',          ''),
        'refresh_interval': int(get_setting('slideshow.refresh_interval', '300')),
    }


@app.route('/bestof')
def bestof():
    resp = make_response(render_template('bestof.html', config=CONFIG))
    # /bestof est typiquement ouvert une fois et laissé tourner en continu sur
    # un écran dédié (JS interne pour rafraîchir les données) : sans ceci, le
    # navigateur peut mettre en cache le HTML/CSS/JS de la page et ignorer
    # silencieusement les mises à jour de l'application tant que la page n'est
    # pas explicitement rechargée.
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/bestof/slides')
def api_bestof_slides():
    s = _slideshow_settings()
    tags_cfg = _tags_settings()

    # Construire la requête captures
    conditions, params = ['1=1'], []
    if s['type'] in ('photo', 'video'):
        conditions.append('kind = ?')
        params.append(s['type'])
    if s['date_from']:
        conditions.append('created_at >= ?')
        params.append(s['date_from'])
    if s['date_to']:
        conditions.append('created_at <= ?')
        params.append(s['date_to'] + 'T23:59:59')
    if s['vote_min'] != '':
        conditions.append('vote_score >= ?')
        params.append(int(s['vote_min']))
    if s['vote_max'] != '':
        conditions.append('vote_score <= ?')
        params.append(int(s['vote_max']))

    order_clause = {
        'votes_desc': 'vote_score DESC, created_at DESC, id DESC',
        'votes_asc':  'vote_score ASC,  created_at ASC,  id ASC',
        'random':     'created_at ASC, id ASC',   # client shuffles
    }.get(s['order'], 'created_at ASC, id ASC')   # chrono par défaut

    sql = (f"SELECT id, kind, filename, vote_score, media_uid FROM captures "
           f"WHERE {' AND '.join(conditions)} ORDER BY {order_clause}")
    with closing(db_conn()) as conn:
        rows = conn.execute(sql, params).fetchall()

    tags_map = get_tags_for_captures([r['id'] for r in rows])
    captures = [
        {
            'type':  r['kind'],
            'url':   url_for('media_photo' if r['kind'] == 'photo' else 'media_video',
                             filename=r['filename']),
            'score': r['vote_score'],
            'media_uid': r['media_uid'],
            'tags': tags_map.get(r['id'], []),
        }
        for r in rows
    ]

    slideshow_imgs = [
        {'type': 'image', 'url': f'/static/slideshow/{img["filename"]}'}
        for img in list_slideshow_images()
    ]

    # Photos envoyées par les invités (voir section "Upload invités"
    # ci-dessous), une fois approuvées — mélangées aux images intermédiaires,
    # sans distinction côté client (même format {type, url}).
    guest_cfg = _guest_upload_settings()
    if guest_cfg['enabled'] and guest_cfg['include_in_bestof']:
        slideshow_imgs += [
            {'type': 'image', 'url': url_for('media_guest', filename=g['filename']), 'source': 'guest'}
            for g in list_approved_guest_uploads()
        ]

    return jsonify({
        'captures':         captures,
        'slideshow_images': slideshow_imgs,
        'delay':            s['delay'],
        'order':            s['order'],
        'refresh_interval': s['refresh_interval'],
        'promo_pages':       [_promo_page_public(p) for p in list_promo_pages() if p['active']],
        'show_media_id':    _media_id_settings()['show_on_bestof'],
        'show_tags':        tags_cfg['show_on_bestof'],
        'tags_style':       {
            'font':       tags_cfg['style_font'],
            'bg_color':   tags_cfg['style_bg_color'],
            'text_color': tags_cfg['style_text_color'],
            'font_size':  tags_cfg['style_font_size'],
        },
    })


@app.route('/api/bestof/promo-pages')
def api_bestof_promo_pages():
    """Pages promo actives, interrogées à cadence fixe par le kiosque déjà
    ouvert (voir bestof.html) — indépendant de slideshow.refresh_interval,
    qui ne cadence que le rafraîchissement complet (captures, images
    intermédiaires) et peut être réglé sur une valeur lente, voire désactivé,
    sans que ça ralentisse l'application des changements faits dans
    /admin/slideshow → Pages promo."""
    return jsonify({'pages': [_promo_page_public(p) for p in list_promo_pages() if p['active']]})


@app.route('/admin/slideshow')
@require_admin_auth
def admin_slideshow():
    """Tuile « Slideshow Best Of ». « Paramètres » a sa propre route dédiée
    ci-dessous et rejoint le système de blocs. « Images intermédiaires » a
    déménagé dans la tuile « Médiathèque » (voir admin_media). « Pages promo »
    et « Bibliothèque de fonds » restent ici, hors système de blocs : ce ne
    sont pas des réglages mais la gestion de contenu lui-même (CRUD à
    plusieurs entrées, comme admin_captures/admin_frames)."""
    blocks, block_context = _admin_render_blocks('slideshow')
    return render_template(
        'admin_slideshow.html', config=CONFIG,
        blocks=blocks, current_page='slideshow', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('slideshow', 'Slideshow Best Of'),
        promo_pages=list_promo_pages(),
        promo_backgrounds=list_promo_backgrounds(),
        promo_content_images=[
            {'id': i['id'], 'url': f"/static/promo_content/{i['filename']}", 'label': i.get('label') or i['filename']}
            for i in list_promo_content_images()
        ],
        promo_fonts=_all_fonts(),
        promo_text_sizes=_PROMO_TEXT_SIZES,
        promo_effects=_PROMO_EFFECTS,
        media_library=_promo_media_library(),
        capture_library=_promo_capture_library(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/slideshow/settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_slideshow_set_settings():
    set_setting('slideshow.type',           request.form.get('type', 'both'))
    set_setting('slideshow.delay',          str(max(1, int(request.form.get('delay', '5') or '5'))))
    set_setting('slideshow.order',          request.form.get('order', 'chrono'))
    set_setting('slideshow.date_from',      request.form.get('date_from', '').strip())
    set_setting('slideshow.date_to',        request.form.get('date_to', '').strip())
    set_setting('slideshow.vote_min', request.form.get('vote_min', '').strip())
    set_setting('slideshow.vote_max', request.form.get('vote_max', '').strip())
    set_setting('slideshow.refresh_interval',
                str(max(0, int(request.form.get('refresh_interval', '300') or '300'))))
    return _admin_block_redirect('slideshow_settings', ok='Paramètres mis à jour.')


# ── Pages promo — CRUD ───────────────────────────────────────────────────────

@app.route('/admin/slideshow/promo/create', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_page_create():
    create_promo_page()
    return redirect(url_for('admin_slideshow', ok='Page promo créée — complétez-la ci-dessous.'))


@app.route('/admin/slideshow/promo/<int:page_id>/update', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_page_update(page_id):
    if not get_promo_page(page_id):
        abort(404)

    fields = {
        'active':          1 if request.form.get('active') else 0,
        'overlay_enabled': 1 if request.form.get('overlay_enabled') else 0,
    }

    raw_order = (request.form.get('sort_order') or '').strip()
    if raw_order.lstrip('-').isdigit():
        fields['sort_order'] = int(raw_order)

    raw_freq = (request.form.get('frequency') or '').strip()
    if raw_freq.isdigit() and int(raw_freq) > 0:
        fields['frequency'] = int(raw_freq)

    raw_pause = (request.form.get('pause_seconds') or '').strip()
    if raw_pause.isdigit() and int(raw_pause) > 0:
        fields['pause_seconds'] = int(raw_pause)

    # v2.0.4 : "Taille du texte"/"Police"/"Couleur du texte" (champs
    # indépendants) supprimés -- toute la mise en forme du texte passe
    # désormais par la barre d'outils WYSIWYG elle-même (voir
    # static/promo-editor.js et admin_slideshow.html), enregistrée comme
    # n'importe quel autre style dans html_content (sanitize_promo_html,
    # utils.py). Les colonnes text_size/text_font/text_color restent en
    # base (valeurs historiques, plus lues nulle part) -- pas de migration
    # nécessaire pour les supprimer.
    bg_color_value = request.form.get('background_bg_color', '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', bg_color_value):
        fields['background_bg_color'] = bg_color_value

    # v2.0.7 : couleur de fond du champ texte WYSIWYG -- aide à l'édition
    # uniquement (voir db.py), jamais exposée à _promo_page_public()/
    # bestof.html ci-dessous : aucun effet sur la page publique.
    editor_bg_value = request.form.get('editor_bg_color', '').strip()
    if re.fullmatch(r'#[0-9a-fA-F]{6}', editor_bg_value):
        fields['editor_bg_color'] = editor_bg_value

    # v2.0.6 : 4 marges indépendantes (haut/droite/bas/gauche) plutôt qu'un
    # réglage unique -- voir /admin/slideshow -> Pages promo et db.py
    # (content_padding_top/right/bottom/left).
    for side in ('top', 'right', 'bottom', 'left'):
        raw_side = (request.form.get(f'content_padding_{side}') or '').strip()
        if raw_side.isdigit() and 0 <= int(raw_side) <= 300:
            fields[f'content_padding_{side}'] = int(raw_side)

    effect_value = request.form.get('effect', '')
    if effect_value in dict(_PROMO_EFFECTS):
        fields['effect'] = effect_value

    raw_bg = (request.form.get('background_id') or '').strip()
    if raw_bg == '':
        fields['background_id'] = None
    elif raw_bg.isdigit():
        fields['background_id'] = int(raw_bg)

    fields['html_content'] = sanitize_promo_html(request.form.get('html_content', ''))

    # CSS libre : aucun filtrage (voir commentaire sur la colonne, db.py) --
    # simple plafond de taille pour éviter un abus de stockage, pas une
    # validation de contenu.
    fields['custom_css'] = (request.form.get('custom_css', '') or '')[:20000]

    update_promo_page(page_id, **fields)
    return redirect(url_for('admin_slideshow', ok='Page promo mise à jour.'))


@app.route('/admin/slideshow/promo/<int:page_id>/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_page_delete(page_id):
    page = delete_promo_page_db(page_id)
    if not page:
        return redirect(url_for('admin_slideshow', err='Page promo introuvable.'))
    return redirect(url_for('admin_slideshow', ok='Page promo supprimée.'))


@app.route('/admin/slideshow/promo/<int:page_id>/move', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_page_move(page_id):
    direction = request.form.get('direction', '')
    if direction in ('up', 'down'):
        move_promo_page(page_id, direction)
    return redirect(url_for('admin_slideshow', ok='Ordre mis à jour.'))


@app.route('/admin/slideshow/promo/bg_upload', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_bg_upload():
    file = request.files.get('background')
    if not file or not file.filename:
        return redirect(url_for('admin_slideshow', err='Aucun fichier sélectionné.'))
    ext = Path(file.filename).suffix.lower()
    if ext not in _PROMO_ALLOWED_EXT:
        return redirect(url_for('admin_slideshow', err='Format non supporté (PNG, JPG, WEBP).'))
    PROMO_DIR.mkdir(parents=True, exist_ok=True)
    safe = f'promo-bg-{int(datetime.now().timestamp())}{ext}'
    file.save(str(PROMO_DIR / safe))
    label = (request.form.get('label', '') or '').strip()[:80]
    add_promo_background(safe, label)
    return redirect(url_for('admin_slideshow', ok='Fond ajouté à la bibliothèque.'))


@app.route('/admin/slideshow/promo/bg/gradient_add', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_bg_gradient_add():
    """Ajoute un fond dégradé (deux couleurs + angle, aucun fichier) à la
    bibliothèque partagée — voir add_promo_gradient_background (db.py).
    Remplace l'ancien dégradé par défaut unique et codé en dur : la
    bibliothèque peut désormais contenir autant de dégradés que voulu."""
    color1 = (request.form.get('color1') or '').strip()
    color2 = (request.form.get('color2') or '').strip()
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', color1) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color2):
        return redirect(url_for('admin_slideshow', err='Couleurs de dégradé invalides.'))
    raw_angle = (request.form.get('angle') or '135').strip()
    angle = int(raw_angle) % 360 if raw_angle.lstrip('-').isdigit() else 135
    label = (request.form.get('label', '') or '').strip()[:80]
    add_promo_gradient_background(color1, color2, angle, label)
    return redirect(url_for('admin_slideshow', ok='Dégradé ajouté à la bibliothèque.'))


@app.route('/admin/slideshow/promo/bg/<int:bg_id>/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_bg_delete(bg_id):
    bg = delete_promo_background_db(bg_id)
    if bg:
        (PROMO_DIR / bg['filename']).unlink(missing_ok=True)
        return redirect(url_for('admin_slideshow', ok='Fond supprimé de la bibliothèque.'))
    return redirect(url_for('admin_slideshow', err='Fond introuvable.'))


# v2.0.8 : bibliothèque d'images dédiées au texte des pages promo -- voir
# promo_content_images (db.py) et le sélecteur d'image du WYSIWYG (onglet «
# Mes images », admin_slideshow.html / static/promo-editor.js). Distincte de
# la bibliothèque de fonds ci-dessus (PROMO_DIR/promo_backgrounds) : ces
# images sont pensées pour être insérées dans le texte lui-même, pas comme
# fond plein écran d'une page.

@app.route('/admin/slideshow/promo/content_image/upload', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_content_image_upload():
    file = request.files.get('image')
    if not file or not file.filename:
        return redirect(url_for('admin_slideshow', err='Aucun fichier sélectionné.'))
    ext = Path(file.filename).suffix.lower()
    if ext not in _PROMO_ALLOWED_EXT:
        return redirect(url_for('admin_slideshow', err='Format non supporté (PNG, JPG, WEBP).'))
    PROMO_CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    safe = f'promo-img-{int(datetime.now().timestamp())}{ext}'
    file.save(str(PROMO_CONTENT_DIR / safe))
    label = (request.form.get('label', '') or '').strip()[:80]
    add_promo_content_image(safe, label)
    return redirect(url_for('admin_slideshow', ok='Image ajoutée -- disponible dans le sélecteur du WYSIWYG (onglet « Mes images »).'))


@app.route('/admin/slideshow/promo/content_image/<int:image_id>/delete', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_promo_content_image_delete(image_id):
    img = delete_promo_content_image_db(image_id)
    if img:
        (PROMO_CONTENT_DIR / img['filename']).unlink(missing_ok=True)
        return redirect(url_for('admin_slideshow', ok='Image supprimée de la bibliothèque.'))
    return redirect(url_for('admin_slideshow', err='Image introuvable.'))


# ── Écran de veille (interface principale) ──────────────────────────────────
# Diaporama plein écran déclenché côté client (voir static/app.js) après N
# minutes d'inactivité sur l'accueil du kiosque. Images dédiées, gérées ici
# (distinctes des images intermédiaires du slideshow /bestof ci-dessus), mêlées
# aux captures des visiteurs pour varier le contenu.

SCREENSAVER_DIR = BASE_DIR / 'app' / 'static' / 'screensaver'
_SCREENSAVER_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}


def _screensaver_settings():
    return {
        'enabled':          get_setting('ui.screensaver_enabled', '0') == '1',
        'timeout_min':      int(get_setting('ui.screensaver_timeout_min', '') or '3'),
        'delay':            int(get_setting('ui.screensaver_delay', '') or '5'),
        'include_captures': get_setting('ui.screensaver_include_captures', '1') == '1',
    }


@app.route('/api/screensaver/settings')
def api_screensaver_settings():
    """Réglages légers (activé/délai), interrogés périodiquement par le
    kiosque déjà ouvert pour appliquer sans rechargement de page un
    changement fait dans /admin/screensaver."""
    s = _screensaver_settings()
    return jsonify({
        'enabled':         s['enabled'],
        'timeout_seconds': s['timeout_min'] * 60,
    })


@app.route('/api/screensaver/slides')
def api_screensaver_slides():
    s = _screensaver_settings()
    captures = []
    if s['include_captures']:
        with closing(db_conn()) as conn:
            rows = conn.execute(
                "SELECT kind, filename FROM captures ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        captures = [
            {
                'type': r['kind'],
                'url':  url_for('media_photo' if r['kind'] == 'photo' else 'media_video',
                                filename=r['filename']),
            }
            for r in rows
        ]
    screensaver_imgs = [
        {'type': 'image', 'url': f'/static/screensaver/{img["filename"]}'}
        for img in list_screensaver_images()
    ]
    return jsonify({
        'captures':           captures,
        'screensaver_images': screensaver_imgs,
        'delay':              s['delay'],
    })


# ── Upload invités (partage depuis smartphone) ───────────────────────────────
# Interface publique, distincte de la galerie et des captures officielles de
# la borne : les invités envoient leurs propres photos (prises sur leur
# téléphone) via un lien/QR séparé (/share/<token>). Le token est un secret
# régénérable depuis le back office — seule barrière d'accès, pensée pour un
# scan QR rapide en évènement plutôt qu'un mot de passe. Les photos envoyées
# ne rejoignent jamais la table captures ni la galerie admin : elles
# alimentent uniquement le diaporama /bestof, et seulement une fois
# approuvées (modération activable/désactivable). Toute la fonctionnalité
# s'active/se désactive en un clic (/admin/guest-uploads) sans jamais
# impacter la galerie ni le best-of existants.

GUEST_UPLOAD_DIR = BASE_DIR / 'data' / 'guest_uploads'
_GUEST_UPLOAD_ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
_GUEST_TOKEN_COOKIE = 'guest_upload_token'

# Anti-abus léger, en mémoire (process unique, pas de dépendance externe) :
# limite le nombre d'envois par adresse IP sur une fenêtre glissante d'une
# minute, en complément du quota persistant par invité (guest_token,
# ci-dessous) qui protège lui contre un simple changement de cookie.
_GUEST_RATE_LIMIT: dict[str, list[float]] = {}
_GUEST_RATE_LOCK = threading.Lock()
_GUEST_RATE_MAX_PER_MINUTE = 10


def _guest_rate_limited(ip: str) -> bool:
    now = time.time()
    with _GUEST_RATE_LOCK:
        hist = [t for t in _GUEST_RATE_LIMIT.get(ip, []) if now - t < 60]
        if len(hist) >= _GUEST_RATE_MAX_PER_MINUTE:
            _GUEST_RATE_LIMIT[ip] = hist
            return True
        hist.append(now)
        _GUEST_RATE_LIMIT[ip] = hist
        return False


def _guest_bool(key: str, default: bool) -> bool:
    raw = get_setting(f'guest_upload.{key}', '')
    return (raw == '1') if raw in ('0', '1') else bool(default)


def _guest_upload_settings():
    _cfg = CONFIG.get('guest_upload', {})
    raw_size  = get_setting('guest_upload.max_file_size_mb', '')
    raw_quota = get_setting('guest_upload.max_uploads_per_guest', '')
    return {
        'enabled':            _guest_bool('enabled', _cfg.get('enabled', False)),
        'require_moderation': _guest_bool('require_moderation', _cfg.get('require_moderation', True)),
        'include_in_bestof':  _guest_bool('include_in_bestof', True),
        'include_in_gallery': _guest_bool('include_in_gallery', _cfg.get('include_in_gallery', False)),
        'max_file_size_mb':   int(raw_size)  if raw_size.isdigit()  else int(_cfg.get('max_file_size_mb', 15)),
        'max_per_guest':      int(raw_quota) if raw_quota.isdigit() else int(_cfg.get('max_uploads_per_guest', 12)),
        'token':              get_setting('guest_upload.token', '') or _cfg.get('upload_token', ''),
    }


def _process_guest_image(file_storage, max_bytes: int, max_side: int = 2400):
    """Lit, valide et normalise une image envoyée par un invité.
    Retourne (image_pillow, taille_octets_lue) — image_pillow est None si le
    fichier dépasse max_bytes ou n'est pas une image valide.

    Sécurité/vie privée :
    - lecture bornée à max_bytes+1, indépendante du header Content-Length
      (potentiellement falsifié par le client) ;
    - Image.verify() rejette tout fichier renommé avec une extension image
      mais qui n'en est pas une ;
    - ré-encodage systématique en JPEG, ce qui supprime au passage toutes
      les métadonnées EXIF (dont la géolocalisation GPS, parfois présente
      dans les photos de smartphone) avant stockage/diffusion publique.
    """
    raw = file_storage.stream.read(max_bytes + 1)
    if not raw or len(raw) > max_bytes:
        return None, len(raw)
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')
    except Exception:
        return None, len(raw)
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img, len(raw)


@app.route('/share/<token>')
def guest_upload_page(token):
    s = _guest_upload_settings()
    if not s['enabled'] or not s['token'] or token != s['token']:
        abort(404)
    guest_token = request.cookies.get(_GUEST_TOKEN_COOKIE, '')
    new_cookie = False
    if not guest_token:
        guest_token = secrets.token_hex(16)
        new_cookie = True
    used = count_guest_uploads_by_token(guest_token)
    resp = make_response(render_template(
        'guest_upload.html', config=CONFIG, token=token,
        max_file_size_mb=s['max_file_size_mb'],
        max_per_guest=s['max_per_guest'],
        used=used, remaining=max(0, s['max_per_guest'] - used),
        require_moderation=s['require_moderation'],
    ))
    if new_cookie:
        resp.set_cookie(_GUEST_TOKEN_COOKIE, guest_token, max_age=365 * 24 * 3600,
                        samesite='Lax', httponly=True)
    return resp


@app.route('/share/<token>/upload', methods=['POST'])
@csrf_protect
def guest_upload_submit(token):
    s = _guest_upload_settings()
    if not s['enabled'] or not s['token'] or token != s['token']:
        abort(404)

    guest_token = request.cookies.get(_GUEST_TOKEN_COOKIE, '')
    if not guest_token:
        return jsonify({'ok': False, 'error': 'Session expirée, rechargez la page.'}), 400

    if _guest_rate_limited(client_ip()):
        return jsonify({'ok': False, 'error': "Trop d'envois, patientez une minute."}), 429

    used = count_guest_uploads_by_token(guest_token)
    if used >= s['max_per_guest']:
        return jsonify({'ok': False,
                        'error': f"Limite de {s['max_per_guest']} photo(s) par invité atteinte."}), 403

    file = request.files.get('photo')
    if not file or not file.filename:
        return jsonify({'ok': False, 'error': 'Aucun fichier reçu.'}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in _GUEST_UPLOAD_ALLOWED_EXT:
        return jsonify({'ok': False, 'error': 'Format non supporté (JPG, PNG, WEBP).'}), 400

    max_bytes = s['max_file_size_mb'] * 1024 * 1024
    img, size_read = _process_guest_image(file, max_bytes)
    if img is None:
        if size_read > max_bytes:
            return jsonify({'ok': False,
                            'error': f"Fichier trop volumineux (max {s['max_file_size_mb']} Mo)."}), 413
        return jsonify({'ok': False, 'error': 'Fichier image invalide ou corrompu.'}), 400

    GUEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = current_stamp()
    unique = secrets.token_hex(4)
    filename = f'guest-{stamp}-{unique}.jpg'
    filepath = GUEST_UPLOAD_DIR / filename
    img.save(filepath, format='JPEG', quality=88)

    thumb_name = f'guest-thumb-{stamp}-{unique}.jpg'
    make_thumb(filepath, THUMBS_DIR / thumb_name)

    status = 'pending' if s['require_moderation'] else 'approved'
    original_name = Path(file.filename).name[:180]
    upload_id = add_guest_upload(filename, thumb_name, original_name, guest_token,
                                 filepath.stat().st_size, status)
    logger.info('Upload invité #%s reçu (%s, statut=%s)', upload_id, filename, status)

    return jsonify({
        'ok': True,
        'status': status,
        'remaining': max(0, s['max_per_guest'] - (used + 1)),
        'message': ('Merci ! Votre photo est en attente de validation.' if status == 'pending'
                    else 'Merci ! Votre photo a été ajoutée au diaporama.'),
    })


@app.route('/media/guest/<path:filename>')
@require_media_auth
def media_guest(filename):
    return send_from_directory(GUEST_UPLOAD_DIR, filename)


@app.route('/admin/guest-uploads', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_guest_uploads():
    """Tuile « Upload invités ». « Paramètres » et « Lien de partage » ont
    chacune leur propre route dédiée ci-dessous et rejoignent le système de
    blocs. « Photos reçues » (modération : approbation/suppression) reste
    ici, hors du système de blocs : ce n'est pas un réglage mais la gestion
    du contenu lui-même (comme admin_captures/admin_emails)."""
    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'approve':
            item = set_guest_upload_status(int(request.form.get('upload_id', 0)), 'approved')
            if item:
                return redirect(url_for('admin_guest_uploads', ok='Photo approuvée, visible dans /bestof.'))
            return redirect(url_for('admin_guest_uploads', err='Photo introuvable.'))

        if action == 'delete':
            item = delete_guest_upload_db(int(request.form.get('upload_id', 0)))
            if item:
                (GUEST_UPLOAD_DIR / item['filename']).unlink(missing_ok=True)
                if item.get('thumb_filename'):
                    (THUMBS_DIR / item['thumb_filename']).unlink(missing_ok=True)
                return redirect(url_for('admin_guest_uploads', ok='Photo supprimée.'))
            return redirect(url_for('admin_guest_uploads', err='Photo introuvable.'))

        abort(404)

    blocks, block_context = _admin_render_blocks('guest_uploads')
    return render_template(
        'admin_guest_uploads.html', config=CONFIG,
        blocks=blocks, current_page='guest_uploads', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('guest_uploads', 'Upload invités'),
        pending=list_guest_uploads('pending'), approved=list_guest_uploads('approved'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/guest-uploads/settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_uploads_set_settings():
    set_setting('guest_upload.enabled', '1' if request.form.get('enabled') else '0')
    set_setting('guest_upload.require_moderation',
               '1' if request.form.get('require_moderation') else '0')
    set_setting('guest_upload.include_in_bestof',
               '1' if request.form.get('include_in_bestof') else '0')
    set_setting('guest_upload.include_in_gallery',
               '1' if request.form.get('include_in_gallery') else '0')
    raw_size = (request.form.get('max_file_size_mb') or '').strip()
    if raw_size.isdigit() and int(raw_size) > 0:
        set_setting('guest_upload.max_file_size_mb', raw_size)
    raw_quota = (request.form.get('max_per_guest') or '').strip()
    if raw_quota.isdigit() and int(raw_quota) > 0:
        set_setting('guest_upload.max_uploads_per_guest', raw_quota)
    return _admin_block_redirect('guest_uploads_settings', ok='Paramètres mis à jour.')


@app.route('/admin/guest-uploads/regenerate_token', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_guest_uploads_regenerate_token():
    set_setting('guest_upload.token', secrets.token_urlsafe(6))
    return _admin_block_redirect('guest_uploads_share_link',
                                  ok="Nouveau lien généré — l'ancien lien/QR ne fonctionne plus.")


@app.route('/admin/guest-uploads/qr.png')
@require_admin_auth
def admin_guest_upload_qr():
    s = _guest_upload_settings()
    if not s['token']:
        abort(404)
    share_url = request.host_url.rstrip('/') + url_for('guest_upload_page', token=s['token'])
    return send_file(generate_qr_png(share_url), mimetype='image/png', download_name='partage-qr.png')


@app.route('/admin/screensaver')
@require_admin_auth
def admin_screensaver():
    """Tuile « Écran de veille ». « Paramètres » a sa propre route dédiée
    ci-dessous et rejoint le système de blocs. « Images » a déménagé dans la
    tuile « Médiathèque » (voir admin_media) : cette route est désormais
    GET seule, elle n'a plus aucune action POST propre."""
    blocks, block_context = _admin_render_blocks('screensaver')
    return render_template(
        'admin_screensaver.html', config=CONFIG,
        blocks=blocks, current_page='screensaver', admin_pages=_admin_all_pages(),
        page_label=_admin_page_label('screensaver', 'Écran de veille'),
        settings=_screensaver_settings(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
        **block_context,
    )


@app.route('/admin/screensaver/settings', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_screensaver_set_settings():
    set_setting('ui.screensaver_enabled', '1' if request.form.get('enabled') else '0')
    raw_timeout = (request.form.get('timeout_min') or '').strip()
    if raw_timeout.isdigit() and int(raw_timeout) > 0:
        set_setting('ui.screensaver_timeout_min', raw_timeout)
    raw_delay = (request.form.get('delay') or '').strip()
    if raw_delay.isdigit() and int(raw_delay) > 0:
        set_setting('ui.screensaver_delay', raw_delay)
    set_setting('ui.screensaver_include_captures', '1' if request.form.get('include_captures') else '0')
    return _admin_block_redirect('screensaver_settings', ok='Paramètres mis à jour.')


# ── Admin — import de pack de frames ─────────────────────────────────────────
# Format pack.json :
# {
#   "name": "...",
#   "welcome": "Accueil.png",   (optionnel) cadre d'accueil, PNG
#   "default": "id-du-cadre",   (optionnel) cadre sélectionné par défaut
#   "frames": [
#     { "filename": "Cadre_X.png", "id": "x", "label": "Cadre X", "sort_order": 10 },
#     ...
#   ],
#   "screensaver": ["Fond1.jpg", "Fond2.png", ...]   (optionnel) images d'écran de veille
# }
# "frames" est optionnel : tout PNG déposé dans un sous-dossier "frames/" est
# importé comme cadre (id/label auto-générés depuis le nom de fichier) même
# sans être listé dans pack.json — comme "screensaver/" pour les images de
# veille. Les deux méthodes (liste explicite + sous-dossier) sont combinables.
# Sans pack.json (mode "vrac") : les PNG à la racine sont traités comme cadres
# (ou accueil si le nom contient "accueil"/"welcome"), et tout fichier image
# placé dans un sous-dossier "screensaver/" est importé comme image de veille —
# que pack.json soit présent ou non.
# Utilisé à la fois par l'import ZIP admin (ci-dessous) et par le chargement
# automatique au démarrage depuis le dossier pack/ (voir _auto_import_startup_pack).

def _import_pack_from_dir(base_dir: Path) -> dict:
    """Importe cadres + accueil + images de veille depuis un dossier déjà
    extrait (contenant pack.json, cherché n'importe où dans l'arbre — ou à
    défaut les PNG en vrac). Renvoie un dict de compteurs."""
    from PIL import Image

    pack_json_candidates = list(base_dir.rglob('pack.json'))
    root = pack_json_candidates[0].parent if pack_json_candidates else base_dir

    welcome_filename = None
    default_id = None
    screensaver_specs = []

    # Dossiers de convention (frames/, screensaver/) — calculés en amont pour
    # pouvoir les exclure du scan "en vrac" ci-dessous (sinon une image posée
    # dans screensaver/ serait aussi importée comme cadre, et vice-versa).
    screensaver_dir_candidates = [p for p in root.rglob('*') if p.is_dir() and p.name.lower() == 'screensaver']
    frames_dir_candidates = [p for p in root.rglob('*') if p.is_dir() and p.name.lower() == 'frames']
    convention_dirs = screensaver_dir_candidates + frames_dir_candidates

    def _in_convention_dir(p: Path) -> bool:
        return any(d in p.parents for d in convention_dirs)

    if pack_json_candidates:
        pack_data = json.loads(pack_json_candidates[0].read_text(encoding='utf-8'))
        specs = list(pack_data.get('frames', []))
        welcome_filename = pack_data.get('welcome')
        default_id = pack_data.get('default')
        screensaver_specs = list(pack_data.get('screensaver', []))
    else:
        pngs = sorted(p for p in root.rglob('*.png') if not _in_convention_dir(p))
        specs = []
        for i, p in enumerate(pngs):
            if 'accueil' in p.stem.lower() or 'welcome' in p.stem.lower():
                welcome_filename = p.name
            else:
                specs.append({
                    'filename': str(p.relative_to(root)),
                    'id': re.sub(r'[^a-z0-9]+', '-', p.stem.lower()).strip('-'),
                    'label': p.stem.replace('_', ' '),
                    'sort_order': (i + 1) * 10,
                })

    # Sous-dossier frames/ (convention indépendante de pack.json, comme screensaver/) :
    # tout PNG déposé là est ajouté comme cadre, avec id/label auto-générés à
    # partir du nom de fichier s'il n'est pas déjà référencé explicitement.
    already_referenced = {(root / spec['filename']).resolve() for spec in specs}
    next_sort = (max((int(s.get('sort_order', 0)) for s in specs), default=0) // 10 + 1) * 10
    for fr_dir in frames_dir_candidates:
        for f in sorted(fr_dir.iterdir()):
            if f.is_file() and f.suffix.lower() == '.png' and f.resolve() not in already_referenced:
                specs.append({
                    'filename': str(f.relative_to(root)),
                    'id': re.sub(r'[^a-z0-9]+', '-', f.stem.lower()).strip('-'),
                    'label': f.stem.replace('_', ' '),
                    'sort_order': next_sort,
                })
                already_referenced.add(f.resolve())
                next_sort += 10

    # Sous-dossier screensaver/ (convention indépendante de pack.json)
    screensaver_paths = {(root / fn).resolve(): fn for fn in screensaver_specs}
    for ss_dir in screensaver_dir_candidates:
        for f in sorted(ss_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in _SCREENSAVER_ALLOWED_EXT:
                screensaver_paths.setdefault(f.resolve(), str(f.relative_to(root)))

    imported = skipped = 0
    logger.info('Pack import : base_dir=%s, %d spec(s), welcome=%s, default=%s, %d image(s) de veille',
                root, len(specs), welcome_filename, default_id, len(screensaver_paths))
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    # Cadre d'accueil
    if welcome_filename:
        welcome_src = root / welcome_filename
        if welcome_src.exists() and welcome_src.suffix.lower() == '.png':
            dest_fn = 'welcome-frame.png'
            shutil.copy2(welcome_src, FRAMES_DIR / dest_fn)
            set_setting('welcome_frame_filename', dest_fn)
            logger.info('Pack import : cadre d\'accueil mis à jour (%s)', welcome_filename)
        else:
            logger.warning('Pack import : welcome introuvable ou non-PNG : %s', welcome_filename)

    # Cadres normaux
    for spec in specs:
        try:
            src = root / spec['filename']
            if not src.exists() or src.suffix.lower() != '.png':
                logger.warning('Pack import : fichier introuvable ou non-PNG : %s', spec['filename'])
                skipped += 1
                continue

            frame_id = re.sub(r'[^a-z0-9_-]', '', spec['id'].lower())
            if not frame_id:
                skipped += 1
                continue

            overlay_fn = f'{frame_id}-overlay.png'
            preview_fn = f'{frame_id}-preview.jpg'

            shutil.copy2(src, FRAMES_DIR / overlay_fn)

            img = Image.open(src).convert('RGBA')
            img.thumbnail((400, 400), Image.LANCZOS)
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            bg.save(str(FRAMES_DIR / preview_fn), 'JPEG', quality=85)

            upsert_frame(frame_id, spec['label'], preview_fn, overlay_fn,
                         int(spec.get('sort_order', 99)))
            imported += 1
        except Exception:
            logger.exception('Pack import : erreur sur %s', spec.get('filename', '?'))
            skipped += 1

    # Cadre par défaut
    if default_id and imported > 0:
        with closing(db_conn()) as conn:
            if conn.execute('SELECT 1 FROM frames WHERE id = ?', (default_id,)).fetchone():
                conn.execute('UPDATE frames SET is_default = 0')
                conn.execute('UPDATE frames SET is_default = 1 WHERE id = ?', (default_id,))
                conn.commit()
                logger.info('Pack import : cadre par défaut → %s', default_id)

    # Images d'écran de veille
    ss_imported = ss_skipped = 0
    if screensaver_paths:
        existing_filenames = {img['filename'] for img in list_screensaver_images()}
        SCREENSAVER_DIR.mkdir(parents=True, exist_ok=True)
        for src_path, rel_name in screensaver_paths.items():
            try:
                if not src_path.exists() or src_path.suffix.lower() not in _SCREENSAVER_ALLOWED_EXT:
                    logger.warning('Pack import : image de veille introuvable ou format non supporté : %s', rel_name)
                    ss_skipped += 1
                    continue
                ext = src_path.suffix.lower()
                safe = re.sub(r'[^a-zA-Z0-9_-]', '_', src_path.stem) + ext
                dest = SCREENSAVER_DIR / safe
                shutil.copy2(src_path, dest)
                if safe not in existing_filenames:
                    add_screensaver_image(safe)
                    existing_filenames.add(safe)
                ss_imported += 1
            except Exception:
                logger.exception('Pack import : erreur sur image de veille %s', rel_name)
                ss_skipped += 1

    return {
        'frames_imported': imported,
        'frames_skipped': skipped,
        'screensaver_imported': ss_imported,
        'screensaver_skipped': ss_skipped,
    }


def _auto_import_startup_pack():
    """Charge automatiquement, à chaque démarrage, le pack (cadres + accueil
    + images de veille) posé dans le dossier pack/ à la racine — pratique
    pour préparer le thème d'un événement sans repasser par l'admin. Ne fait
    rien si pack/ est absent, ou s'il ne contient ni pack.json ni sous-dossier
    frames/ ou screensaver/."""
    pack_dir = BASE_DIR / 'pack'
    if not pack_dir.is_dir():
        return
    has_pack_json = any(pack_dir.rglob('pack.json'))
    subdirs = {p.name.lower() for p in pack_dir.rglob('*') if p.is_dir()}
    if not has_pack_json and 'frames' not in subdirs and 'screensaver' not in subdirs:
        return
    try:
        counts = _import_pack_from_dir(pack_dir)
        logger.info(
            'Pack de démarrage chargé (pack/) : %d cadre(s) importé(s) (%d ignoré(s)), '
            '%d image(s) de veille importée(s) (%d ignorée(s)).',
            counts['frames_imported'], counts['frames_skipped'],
            counts['screensaver_imported'], counts['screensaver_skipped'],
        )
    except Exception:
        logger.exception('Échec du chargement du pack de démarrage (pack/).')


@app.route('/admin/frames/import', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_frames_import():
    import tempfile
    import zipfile

    file = request.files.get('pack')
    if not file or not file.filename:
        return _admin_block_redirect('frames_import', err='Aucun fichier sélectionné.')
    if not file.filename.lower().endswith('.zip'):
        return _admin_block_redirect('frames_import', err='Le fichier doit être un ZIP.')

    tmpdir = tempfile.mkdtemp()
    counts = None
    try:
        with zipfile.ZipFile(file.stream) as zf:
            if zf.testzip() is not None:
                return _admin_block_redirect('frames_import', err='ZIP corrompu.')
            # Sécurité : refuser les chemins traversants
            for name in zf.namelist():
                if name.startswith('/') or '..' in name:
                    return _admin_block_redirect('frames_import', err='ZIP non autorisé (chemin invalide).')
            zf.extractall(tmpdir)
        counts = _import_pack_from_dir(Path(tmpdir))
    except zipfile.BadZipFile:
        return _admin_block_redirect('frames_import', err='Fichier ZIP invalide.')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    msg = f"{counts['frames_imported']} cadre(s) importé(s)"
    if counts['screensaver_imported']:
        msg += f", {counts['screensaver_imported']} image(s) de veille importée(s)"
    skipped_total = counts['frames_skipped'] + counts['screensaver_skipped']
    if skipped_total:
        msg += f', {skipped_total} ignoré(s) (voir logs)'
    return _admin_block_redirect('frames_import', ok=msg)


# ── Démarrage ─────────────────────────────────────────────────────────────────

def _port_is_free(host: str, port: int) -> bool:
    bind_host = '' if host == '0.0.0.0' else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((bind_host, port))
            return True
        except OSError:
            return False


# Délai maximum toléré, au démarrage, avant de conclure que le port configuré
# est réellement pris par une AUTRE application (voir _wait_port_free
# ci-dessous — corrige un bug de redémarrage depuis le B-O découvert en
# pratique : sans cette tolérance, la nouvelle instance perdait l'accès au
# port habituel).
_PORT_WAIT_TIMEOUT_S = 5.0
_PORT_WAIT_INTERVAL_S = 0.25


def _wait_port_free(host: str, port: int, timeout: float = _PORT_WAIT_TIMEOUT_S) -> bool:
    """Comme _port_is_free ci-dessus, mais réessaie pendant `timeout`
    secondes avant de conclure à l'indisponibilité, au lieu d'un test
    unique et immédiat.

    Nécessaire pour le redémarrage de l'application depuis le B-O (tuile
    Application, voir _do_restart_app) : celui-ci lance la nouvelle
    instance AVANT que l'ancienne ne se termine (os._exit), pour ne jamais
    laisser l'application complètement arrêtée en cas d'échec du lancement.
    Un très bref recouvrement entre les deux processus est donc normal et
    attendu — l'ancienne instance peut mettre quelques centaines de
    millisecondes (voire plus sur une machine chargée) à réellement libérer
    le port après le lancement de la nouvelle. Avec un test unique et
    immédiat (comportement d'origine), la nouvelle instance concluait à
    tort que le port était pris par une AUTRE application et basculait
    silencieusement sur le port 8080 (voir plus bas) : l'interface kiosque
    continuait de fonctionner normalement (elle se connecte au port
    réellement choisi, quel qu'il soit), mais le back office devenait
    injoignable à l'adresse habituelle — symptôme observé en pratique
    (« je perds l'accès au B-O » après un redémarrage depuis celui-ci).
    Sans effet sur le cas normal (port réellement occupé par une autre
    application dès le premier lancement) au-delà d'ajouter jusqu'à
    `timeout` secondes avant la bascule sur 8080, déjà loggée."""
    deadline = time.monotonic() + timeout
    while True:
        if _port_is_free(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PORT_WAIT_INTERVAL_S)


class _KioskAPI:
    """Exposée en JS via window.pywebview.api (voir static/app.js). Permet à
    l'interface principale de sortir/rentrer du plein écran, protégé par un
    code PIN dédié (réglable depuis /admin/system, indépendant du mot de
    passe admin) saisi via un clavier numérique virtuel — pour laisser le
    personnel accéder ponctuellement à Windows sans fermer l'application.

    IMPORTANT : ne jamais stocker l'objet Window pywebview sur un attribut
    public (sans underscore) de cette classe. pywebview parcourt récursivement
    les attributs non-underscore de js_api pour construire le pont JS
    (webview/util.py:get_functions) ; y référencer le Window déclenche une
    récursion dans Window.dom.body -> Element -> evaluate_js() qui attend
    l'événement "loaded" et provoque un timeout de 20s
    (WebViewException: Main window failed to start) pendant l'injection
    elle-même. D'où l'attribut _window, préfixé, ignoré par ce parcours."""
    _window = None

    def _do_toggle(self, source: str):
        """Bascule effective, partagée entre l'appel JS (mot de passe requis,
        voir toggle_fullscreen ci-dessous) et la route admin /admin/system
        (déjà authentifiée, voir admin_toggle_fullscreen)."""
        if self._window is None:
            logger.warning('Bascule plein écran (%s) demandée mais aucune fenêtre référencée.', source)
            return {'ok': False, 'error': 'Fenêtre indisponible.'}
        try:
            self._window.toggle_fullscreen()
            logger.info('Mode plein écran basculé (%s).', source)
        except Exception:
            logger.exception('Échec du basculement plein écran (%s).', source)
            return {'ok': False, 'error': 'Erreur interne lors du basculement (voir logs\\app.log).'}
        return {'ok': True}

    def toggle_fullscreen(self, pin: str):
        expected_pin = get_setting('kiosk.unlock_pin', '') or '1234'
        if not pin or str(pin) != expected_pin:
            logger.warning('Bascule plein écran refusée (code PIN incorrect).')
            return {'ok': False, 'error': 'Code incorrect.'}
        return self._do_toggle('interface principale')


# Instance unique, partagée entre _run_native_window() (qui y attache la
# fenêtre réelle) et la route admin /admin/system/fullscreen (qui déclenche
# la bascule depuis le back office, sans passer par le pont JS).
_kiosk_api = _KioskAPI()


def _do_restart_app():
    """Relance l'application — appelée en différé, depuis un thread à part
    (voir threading.Timer dans /admin/system/restart), jamais directement
    dans le thread d'une requête Flask.

    Lance d'abord une INSTANCE NEUVE, indépendante, du même interpréteur
    avec les mêmes arguments (subprocess.Popen détaché — n'hérite pas du
    cycle de vie du processus courant), puis termine immédiatement et sans
    condition le processus actuel via os._exit() (libère aussitôt le port
    et la caméra pour la nouvelle instance).

    Volontairement PAS de os.execv() ici (remplacement « en place » du
    processus courant) : sous Windows, os.execv est émulé par la libc
    (spawn + attente + sortie avec le code de sortie de l'enfant) et son
    comportement s'est avéré, dans les faits, pas fiable à 100 % selon les
    versions de Python/plateformes (le processus d'origine peut ne pas
    attendre correctement, laissant l'appli fermée sans relance — voir
    bpo-19124). Un Popen détaché suivi d'un os._exit() sépare clairement
    les deux étapes (démarrage du nouveau, arrêt de l'ancien) sans dépendre
    de cette émulation.

    Volontairement PAS de _kiosk_api._window.destroy() avant l'arrêt non
    plus : fermer la fenêtre ferait sortir webview.start() (thread
    principal), qui atteindrait alors la fin du bloc
    `if __name__ == '__main__'` et laisserait l'interpréteur se terminer
    de lui-même — une sortie concurrente à celle provoquée ici, sans
    bénéfice réel puisque os._exit() ci-dessous termine de toute façon tout
    le processus (fenêtre comprise) en un seul appel, immédiat et garanti."""
    logger.info("Redémarrage de l'application : lancement d'une nouvelle instance...")
    try:
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable] + sys.argv,
            cwd=str(BASE_DIR),
            creationflags=creationflags,
            close_fds=True,
            # DETACHED_PROCESS = aucune console pour la nouvelle instance
            # (contrairement au tout premier lancement, fait depuis la
            # console de run.ps1) : sans cette redirection explicite,
            # stdin/stdout/stderr y seraient rattachés à des handles de
            # console invalides — inoffensif pour logger (protégé en
            # interne contre ce cas), mais pas garanti pour d'éventuels
            # écrits directs d'une bibliothèque tierce (ex. cv2). DEVNULL
            # lève toute ambiguïté ; les logs restent disponibles dans
            # logs\app.log (FileHandler, inchangé) quel que soit ce réglage.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("Échec du lancement de la nouvelle instance — redémarrage annulé, l'application actuelle continue de tourner.")
        return
    logger.info("Nouvelle instance lancée, arrêt de l'instance actuelle.")
    time.sleep(0.2)  # laisse le message de log ci-dessus s'écrire sur disque
    os._exit(0)


def _run_native_window(port: int):
    """Affiche l'interface dans une fenêtre native (pywebview, WebView2 sous
    Windows) — aucun navigateur externe (Edge/Chrome...) requis. Flask tourne
    déjà en arrière-plan (voir __main__) ; cette fonction bloque le thread
    principal jusqu'à la fermeture de la fenêtre, comme l'exigent les
    toolkits GUI sous Windows.
    ui.kiosk_mode : plein écran (True, par défaut) ou fenêtre normale (False,
    pratique en développement — active aussi les DevTools).
    ui.kiosk_screen : écran utilisé (0 = premier/gauche, 1 = deuxième, etc.).
    Le plein écran peut aussi être basculé depuis l'interface elle-même
    (mot de passe admin requis), voir _KioskAPI et static/app.js."""
    import webview

    url = f'http://127.0.0.1:{port}/'
    ui_cfg = CONFIG.get('ui', {})
    # Surcharges admin (/admin/system), prioritaires sur config.toml.
    _kiosk_mode_raw = get_setting('ui.kiosk_mode', '')
    kiosk_mode = (_kiosk_mode_raw == '1') if _kiosk_mode_raw in ('0', '1') else bool(ui_cfg.get('kiosk_mode', True))
    _kiosk_screen_raw = get_setting('ui.kiosk_screen', '')
    screen_index = int(_kiosk_screen_raw) if _kiosk_screen_raw.isdigit() else int(ui_cfg.get('kiosk_screen', 0))

    screen = None
    try:
        screens = webview.screens
        if 0 <= screen_index < len(screens):
            screen = screens[screen_index]
        elif screen_index > 0:
            logger.warning(
                'ui.kiosk_screen=%s mais seulement %s écran(s) détecté(s) — écran principal utilisé.',
                screen_index, len(screens),
            )
    except Exception:
        logger.warning('Détection des écrans impossible, écran principal utilisé.', exc_info=True)

    window_kwargs = dict(
        title=ui_cfg.get('app_title', 'Pictotem'),
        url=url,
        fullscreen=kiosk_mode,
    )
    if screen is not None:
        window_kwargs['screen'] = screen
    if not kiosk_mode:
        window_kwargs['width'] = 1280
        window_kwargs['height'] = 800

    window = webview.create_window(js_api=_kiosk_api, **window_kwargs)
    _kiosk_api._window = window
    try:
        webview.start(debug=not kiosk_mode)
    except Exception:
        logger.exception(
            "Impossible d'afficher la fenêtre native. Le runtime Microsoft Edge "
            'WebView2 est-il installé ? (préinstallé sur Windows 11 et Windows 10 '
            'à jour ; sinon : https://developer.microsoft.com/microsoft-edge/webview2/)'
        )
        raise


if __name__ == '__main__':
    init_db()
    _auto_import_startup_pack()
    validate_printer()
    _host = CONFIG['server']['host']
    _port = int(CONFIG['server']['port'])

    if not _wait_port_free(_host, _port):
        # Port 80 souvent indisponible sous Windows (IIS, Skype, etc.) —
        # on bascule avant même de tenter le bind plutôt que de planter.
        # _wait_port_free (et non _port_is_free directement) tolère un bref
        # recouvrement avec une ancienne instance en cours d'arrêt (voir
        # redémarrage depuis le B-O, _do_restart_app) avant de conclure que
        # le port est pris par une autre application.
        logger.warning(
            'Port %s indisponible sur %s après %.0fs d\'attente, bascule sur le port 8080 '
            '— modifiez server.port dans config.toml pour changer ce comportement.',
            _port, _host, _PORT_WAIT_TIMEOUT_S,
        )
        _port = 8080

    logger.info('Démarrage pictotem sur %s:%s', _host, _port)

    # Flask tourne en arrière-plan (thread démon) ; la fenêtre native occupe
    # le thread principal — obligatoire pour l'event loop GUI sous Windows.
    # La fermeture de la fenêtre met fin au processus (et donc au serveur).
    threading.Thread(
        target=lambda: app.run(host=_host, port=_port, threaded=True, use_reloader=False),
        daemon=True,
    ).start()
    _run_native_window(_port)
