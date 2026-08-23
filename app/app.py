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
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime, timedelta

import cv2
import numpy as np
from flask import (Flask, Response, abort, jsonify, make_response, redirect,
                   render_template, request, send_file, send_from_directory,
                   session, url_for)
from PIL import Image, ImageOps

from auth import (auth_enabled, build_secret_key, check_admin_password,
                  check_gallery_password, check_main_password, client_ip,
                  csrf_protect, gallery_session_key, generate_csrf_token,
                  is_admin_authenticated, is_gallery_authenticated,
                  is_local_request, is_main_authenticated, main_session_key,
                  require_admin_auth, require_gallery_auth, require_main_auth,
                  require_media_auth)
from camera import (VIDEO_CAPTURE_ACTIVE, CAM_LOCK, clear_overlay_cache,
                    clear_recording_frame, composite_frame_overlay,
                    encode_jpeg, get_frame_overlay_path, get_overlay_bgra,
                    publish_recording_frame, read_frame, reset_camera,
                    stream_generator)
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
                list_capture_tags_with_media)
from utils import (build_gallery_url, current_stamp, disable_autostart,
                   enable_autostart, generate_qr_png, is_autostart_enabled,
                   make_thumb, message_text, print_photo, validate_printer)

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
    return render_template(
        'index.html', config=CONFIG, frames=list_frames(),
        default_frame=get_default_frame(), message=message_text(),
        welcome_frame_url=welcome_frame_url, texts=get_ui_texts(),
        idle_timer_enabled=get_setting('idle_timer_enabled', '0') == '1',
        idle_timer_seconds=int(get_setting('idle_timer_seconds', '30')),
        idle_timer_badge_text=get_setting('idle_timer_badge_text', 'Retour dans {n}s'),
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
        about=_about_settings(),
        kiosk_unlock_taps=_kiosk_unlock_settings()['taps'],
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

    filepath = PHOTO_DIR / filename
    filepath.write_bytes(encode_jpeg(display_frame))
    thumb_name = f'thumb-{stamp}.jpg'
    make_thumb(filepath, THUMBS_DIR / thumb_name)
    capture_id, media_uid = record_capture('photo', filename, thumb_name)
    logger.info('Photo capturée %s (cadre=%s)', filename, frame_id)
    qr_tags = _scan_and_tag_qr_codes(capture_id, display_frame)
    return jsonify({'ok': True, 'id': capture_id, 'media_uid': media_uid, 'kind': 'photo',
                    'filename': filename, 'qr_tags': qr_tags,
                    'url': f'/media/photo/{filename}', 'message': message_text()})


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
    try:
        proc = subprocess.run([
            FFMPEG_EXE, '-y', '-r', f'{effective_fps:.3f}', '-i', str(avi_path),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', str(raw_mp4_path),
        ], capture_output=True, text=True)
    except FileNotFoundError:
        avi_path.unlink(missing_ok=True)
        logger.error('ffmpeg introuvable (%s) — voir logs\\launcher.log (setup_ffmpeg.ps1).', FFMPEG_EXE)
        return jsonify({'ok': False, 'error': "ffmpeg introuvable sur cette machine (nécessaire pour les vidéos)."}), 500
    avi_path.unlink(missing_ok=True)
    if proc.returncode != 0 or not raw_mp4_path.exists() or raw_mp4_path.stat().st_size == 0:
        logger.error('ffmpeg transcode échoué : %s', proc.stderr)
        return jsonify({'ok': False, 'error': 'Transcodage vidéo échoué'}), 500

    if has_overlay and CONFIG['capture']['video'].get('save_raw', False):
        RAW_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(raw_mp4_path), str(RAW_VIDEO_DIR / final_filename))
        logger.info('Vidéo brute sauvegardée : %s', final_filename)

    if has_overlay:
        try:
            proc2 = subprocess.run([
                FFMPEG_EXE, '-y', '-i', str(raw_mp4_path), '-i', str(overlay_path),
                '-filter_complex', f'[1:v]scale={w}:{h}[ov];[0:v][ov]overlay=0:0',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', str(final_path),
            ], capture_output=True, text=True)
        except FileNotFoundError:
            raw_mp4_path.unlink(missing_ok=True)
            logger.error('ffmpeg introuvable (%s) — voir logs\\launcher.log (setup_ffmpeg.ps1).', FFMPEG_EXE)
            return jsonify({'ok': False, 'error': "ffmpeg introuvable sur cette machine (nécessaire pour les vidéos)."}), 500
        raw_mp4_path.unlink(missing_ok=True)
        if proc2.returncode != 0 or not final_path.exists() or final_path.stat().st_size == 0:
            logger.error('ffmpeg overlay échoué : %s', proc2.stderr)
            return jsonify({'ok': False, 'error': 'Application du cadre vidéo échouée'}), 500
    else:
        raw_mp4_path.rename(final_path)

    capture_id, media_uid = record_capture('video', final_filename, thumb_name)
    logger.info('Vidéo capturée %s', final_filename)
    return jsonify({'ok': True, 'id': capture_id, 'media_uid': media_uid, 'kind': 'video',
                    'filename': final_filename,
                    'url': f'/media/video/{final_filename}', 'message': message_text()})


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
# Activable depuis /admin/tags (section dédiée). À chaque photo (capture
# simple ou photo strip), si activé, on scanne l'image finale (avec cadre
# déjà appliqué) à la recherche de QR-codes via cv2.QRCodeDetector — déjà
# une dépendance du projet (camera.py), donc aucun paquet supplémentaire à
# installer sur la distribution Python portable du Pi/PC. Chaque QR-code
# décodé devient un tag libre sur la capture, avec les mêmes bornes que les
# tags libres saisis à la main (tags.free_min_length/free_max_length/
# max_per_capture, voir _tags_settings()) pour rester cohérent avec le
# reste de la fonctionnalité tags. Ne s'applique pas aux vidéos (détection
# sur une image fixe uniquement). N'affecte jamais le succès de la
# capture : toute erreur de détection est journalisée et ignorée.

_qr_detector = cv2.QRCodeDetector()


def _qrcode_settings() -> dict:
    return {
        'enabled': get_setting('qrcode.enabled', '0') == '1',
    }


def _scan_and_tag_qr_codes(capture_id: int, image) -> list:
    """Détecte les QR-codes présents dans `image` (tableau BGR déjà composé
    avec son cadre) et les ajoute comme tags libres sur `capture_id`.
    Retourne la liste des textes ajoutés (peut être vide). No-op immédiat
    si l'add-on est désactivé."""
    if get_setting('qrcode.enabled', '0') != '1':
        return []
    try:
        found, decoded_texts, _points, _straight = _qr_detector.detectAndDecodeMulti(image)
    except Exception:
        logger.exception('QR-code : détection échouée pour la capture #%d.', capture_id)
        return []
    if not found or not decoded_texts:
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


@app.route('/api/capture/photostrip', methods=['POST'])
@require_main_auth
def capture_photostrip():
    ps_cfg = CONFIG.get('capture', {}).get('photo_strip', {})
    if not ps_cfg.get('enabled', True):
        return jsonify({'ok': False, 'error': 'Photo strip désactivé'}), 403
    req = request.get_json(silent=True) or {}
    frame_id = req.get('frame', 'none')
    shots = max(2, min(int(ps_cfg.get('shots', 3)), 6))
    interval = max(0.3, float(ps_cfg.get('interval_sec', 1.2)))

    overlay_path = get_frame_overlay_path(frame_id)
    has_overlay = bool(overlay_path and overlay_path.exists())

    frames = []
    for i in range(shots):
        raw = read_frame()
        display = raw
        if has_overlay:
            h, w = raw.shape[:2]
            overlay = get_overlay_bgra(overlay_path, w, h)
            if overlay is not None:
                display = composite_frame_overlay(raw, overlay)
        frames.append(display)
        if i < shots - 1:
            time.sleep(interval)

    strip_img = _compose_photo_strip(frames, ps_cfg)
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
    logger.info('Photo strip capturé %s (%d prises, cadre=%s)', filename, shots, frame_id)
    qr_tags = _scan_and_tag_qr_codes(capture_id, strip_img)
    return jsonify({'ok': True, 'id': capture_id, 'media_uid': media_uid, 'kind': 'photo',
                    'filename': filename, 'qr_tags': qr_tags,
                    'url': f'/media/photo/{filename}', 'message': message_text()})


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
        email_cookie=email_cookie, gallery_text=get_setting('gallery_text', ''),
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
    return render_template(
        'admin_archive.html', config=CONFIG,
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


@app.route('/admin/archive/export', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_archive_export():
    start_iso, end_iso, err = _resolve_archive_range(request.form)
    if err:
        return redirect(url_for('admin_archive', err=err))
    include_guests = bool(request.form.get('include_guests'))
    zip_path, count = _build_archive_zip(start_iso, end_iso, include_guests)
    if count == 0:
        zip_path.unlink(missing_ok=True)
        return redirect(url_for('admin_archive', err='Aucun média dans cet intervalle.'))
    logger.info('Archive admin : %d média(s) exporté(s) (%s -> %s, invités %s).',
                count, start_iso, end_iso, 'inclus' if include_guests else 'exclus')
    return send_file(zip_path, mimetype='application/zip', as_attachment=True, download_name=zip_path.name)


@app.route('/admin/archive/cleanup', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_archive_cleanup():
    start_iso, end_iso, err = _resolve_archive_range(request.form)
    if err:
        return redirect(url_for('admin_archive', err=err))
    if not request.form.get('confirm'):
        return redirect(url_for('admin_archive', err='Cochez la case de confirmation pour supprimer.'))

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
    return redirect(url_for('admin_archive', ok=msg))


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
        return _admin_redirect(error='Aucun fichier sélectionné.')
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_OVERLAY_EXT:
        return _admin_redirect(error='Le cadre d\'accueil doit être un PNG.')
    filename = f'welcome-frame{ext}'
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    file.save(str(FRAMES_DIR / filename))
    set_setting('welcome_frame_filename', filename)
    return _admin_redirect(success='Cadre d\'accueil mis à jour.')


@app.route('/admin/welcome-frame/remove', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_welcome_frame_remove():
    fn = get_setting('welcome_frame_filename', '')
    if fn:
        (FRAMES_DIR / fn).unlink(missing_ok=True)
        set_setting('welcome_frame_filename', '')
    return _admin_redirect(success='Cadre d\'accueil supprimé.')


@app.route('/admin/texts', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_texts():
    if request.method == 'POST':
        for key in TEXT_DEFAULTS:
            value = (request.form.get(f'text_{key}') or '').strip()
            set_setting(f'text.{key}', value)
        set_setting('idle_timer_enabled', '1' if request.form.get('idle_timer_enabled') else '0')
        raw_secs = (request.form.get('idle_timer_seconds') or '30').strip()
        set_setting('idle_timer_seconds', str(max(5, int(raw_secs))) if raw_secs.isdigit() else '30')
        badge_text = (request.form.get('idle_timer_badge_text') or 'Retour dans {n}s').strip()
        set_setting('idle_timer_badge_text', badge_text)
        for key, default in [('idle_timer_font_size', '13'), ('idle_timer_padding_y', '5'), ('idle_timer_padding_x', '13')]:
            raw = (request.form.get(key) or default).strip()
            set_setting(key, str(max(1, int(raw))) if raw.isdigit() else default)

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

        return redirect(url_for('admin_texts', ok='Paramètres mis à jour.'))
    return render_template('admin_texts.html', config=CONFIG,
                           texts=get_ui_texts(),
                           defaults=TEXT_DEFAULTS,
                           idle_timer_enabled=get_setting('idle_timer_enabled', '0') == '1',
                           idle_timer_seconds=int(get_setting('idle_timer_seconds', '30')),
                           idle_timer_badge_text=get_setting('idle_timer_badge_text', 'Retour dans {n}s'),
                           idle_timer_font_size=int(get_setting('idle_timer_font_size', '13')),
                           idle_timer_padding_y=int(get_setting('idle_timer_padding_y', '5')),
                           idle_timer_padding_x=int(get_setting('idle_timer_padding_x', '13')),
                           hide_print_button=get_setting('ui.hide_print_button', '0') == '1',
                           bottom_bar_sizes=get_bottom_bar_sizes(),
                           top_bar=get_top_bar_settings(),
                           about=_about_settings(),
                           alert_success=request.args.get('ok'),
                           alert_error=request.args.get('err'))


@app.route('/admin/gallery', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_gallery():
    if request.method == 'POST':
        text = (request.form.get('gallery_text') or '').strip()
        set_setting('gallery_text', text)
        return redirect(url_for('admin_gallery', ok='Texte mis à jour.'))
    return render_template('admin_gallery.html', config=CONFIG,
                           gallery_text=get_setting('gallery_text', ''),
                           alert_success=request.args.get('ok'),
                           alert_error=request.args.get('err'))


@app.route('/admin/system', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_system():
    if request.method == 'POST':
        # Caméra — validation légère avant écriture, on ignore les champs
        # vides ou invalides plutôt que d'écrire une valeur incohérente.
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

        # Écran / kiosque — appliqué au prochain lancement de run.bat.
        set_setting('ui.kiosk_mode', '1' if request.form.get('ui_kiosk_mode') else '0')
        raw_screen = (request.form.get('ui_kiosk_screen') or '0').strip()
        set_setting('ui.kiosk_screen', raw_screen if raw_screen.isdigit() else '0')

        # Déverrouillage plein écran (interface principale) — champ vidé =
        # retour à la valeur par défaut (voir _kiosk_unlock_settings()).
        raw_pin = re.sub(r'\D', '', request.form.get('kiosk_unlock_pin') or '')[:8]
        set_setting('kiosk.unlock_pin', raw_pin)
        raw_taps = (request.form.get('kiosk_unlock_taps') or '').strip()
        set_setting('kiosk.unlock_taps', raw_taps if raw_taps.isdigit() and 2 <= int(raw_taps) <= 15 else '')

        reset_camera()
        return redirect(url_for('admin_system', ok='Réglages enregistrés. Caméra relancée avec les nouvelles valeurs.'))

    def _cfg_int(key, default):
        raw = get_setting(f'camera.{key}', '')
        return int(raw) if raw.isdigit() else int(CONFIG['camera'].get(key, default))

    camera_values = {
        'device':         _cfg_int('device', 0),
        'width':          _cfg_int('width', 1920),
        'height':         _cfg_int('height', 1080),
        'rotation':       _cfg_int('rotation', 0),
        'preview_mirror': (get_setting('camera.preview_mirror', '') or ('1' if CONFIG['camera'].get('preview_mirror', False) else '0')) == '1',
    }
    ui_cfg = CONFIG.get('ui', {})
    _kiosk_mode_raw = get_setting('ui.kiosk_mode', '')
    _kiosk_screen_raw = get_setting('ui.kiosk_screen', '')
    screen_values = {
        'kiosk_mode':   (_kiosk_mode_raw == '1') if _kiosk_mode_raw in ('0', '1') else bool(ui_cfg.get('kiosk_mode', True)),
        'kiosk_screen': int(_kiosk_screen_raw) if _kiosk_screen_raw.isdigit() else int(ui_cfg.get('kiosk_screen', 0)),
    }
    return render_template(
        'admin_system.html', config=CONFIG,
        camera=camera_values, screen=screen_values,
        kiosk_unlock=_kiosk_unlock_settings(),
        autostart_enabled=is_autostart_enabled(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
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
        return redirect(url_for('admin_system', ok=msg))
    return redirect(url_for('admin_system', err=msg))


@app.route('/admin/system/fullscreen', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_toggle_fullscreen():
    """Bascule immédiate du plein écran de la fenêtre native déjà lancée
    (distinct de ui_kiosk_mode ci-dessus, qui ne s'applique qu'au prochain
    lancement de run.bat). Déjà protégé par l'authentification admin, donc
    pas de mot de passe supplémentaire ici (contrairement à l'appel JS
    équivalent déclenché depuis l'interface principale)."""
    result = _kiosk_api._do_toggle('back office')
    if result.get('ok'):
        return redirect(url_for('admin_system', ok='Plein écran basculé.'))
    return redirect(url_for('admin_system', err=result.get('error', 'Bascule impossible.')))


@app.route('/admin/frames')
@require_admin_auth
def admin_frames():
    welcome_fn = get_setting('welcome_frame_filename', '')
    welcome_frame_url = f'/static/frames/{welcome_fn}' if welcome_fn else ''
    return render_template(
        'admin_frames.html', config=CONFIG, frames=list_frames(),
        welcome_frame_url=welcome_frame_url,
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


@app.route('/admin/frames/new', methods=['POST'])
@require_admin_auth
@csrf_protect
def admin_frame_create():
    frame_id = re.sub(r'[^a-z0-9_-]', '', (request.form.get('id') or '').strip().lower().replace(' ', '-'))
    label = (request.form.get('label') or '').strip()
    sort_order = int(request.form.get('sort_order') or 99)
    if not frame_id or not label:
        return _admin_redirect(error='Identifiant et libellé sont obligatoires.')
    if get_frame_by_id_db(frame_id):
        return _admin_redirect(error=f'Un cadre avec l\'identifiant "{frame_id}" existe déjà.')
    try:
        preview_fn = _save_frame_file(request.files.get('preview'), frame_id, 'preview')
        overlay_fn = _save_frame_file(request.files.get('overlay'), frame_id, 'overlay')
    except ValueError as exc:
        return _admin_redirect(error=str(exc))
    upsert_frame(frame_id, label, preview_fn, overlay_fn, sort_order)
    return _admin_redirect(success=f'Cadre "{label}" ajouté.')


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


@app.route('/admin/votes', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_votes():
    if request.method == 'POST':
        set_setting('vote.enabled',       '1' if request.form.get('enabled') else '0')
        set_setting('vote.blue_max',      str(max(1, int(request.form.get('blue_max',  '10') or '10'))))
        set_setting('vote.green_max',     str(max(1, int(request.form.get('green_max', '10') or '10'))))
        set_setting('vote.red_max',       str(max(1, int(request.form.get('red_max',   '20') or '20'))))
        for key in ('color_neg_max', 'color_neg_mid', 'color_zero', 'color_pos_mid', 'color_pos_max'):
            val = request.form.get(key, '').strip()
            if val.startswith('#') and len(val) == 7:
                set_setting(f'vote.{key}', val)
        return redirect(url_for('admin_votes', ok='Paramètres mis à jour.'))
    cfg = _vote_cfg()
    return render_template(
        'admin_votes.html', config=CONFIG,
        vote_enabled=get_setting('vote.enabled', '1') == '1',
        cfg=cfg,
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


# ── Admin — tags & ID média ───────────────────────────────────────────────────

@app.route('/admin/tags', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_tags():
    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'settings':
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
            return redirect(url_for('admin_tags', ok='Paramètres mis à jour.'))

        if action == 'media_id_settings':
            raw_len = (request.form.get('media_id_length') or '').strip()
            if raw_len.isdigit() and 3 <= int(raw_len) <= 12:
                set_setting('media_id.length', raw_len)
            set_setting('media_id.show_on_bestof', '1' if request.form.get('show_on_bestof') else '0')
            return redirect(url_for('admin_tags', ok='Réglages ID média mis à jour.'))

        if action == 'display_settings':
            set_setting('tags.show_on_bestof', '1' if request.form.get('show_on_bestof') else '0')
            font_value = request.form.get('style_font', '')
            if font_value in dict(_PROMO_FONTS):
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
            return redirect(url_for('admin_tags', ok="Réglages d'affichage mis à jour."))

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

        if action == 'qrcode_settings':
            set_setting('qrcode.enabled', '1' if request.form.get('enabled') else '0')
            return redirect(url_for('admin_tags', ok='Réglages QR-code mis à jour.'))

    return render_template(
        'admin_tags.html', config=CONFIG,
        settings=_tags_settings(), media_id=_media_id_settings(), tags=list_tags(),
        assignments=list_capture_tags_with_media(), tags_fonts=_PROMO_FONTS,
        qrcode_settings=_qrcode_settings(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


# ── Admin — boutons d'action (kiosque) ────────────────────────────────────────

@app.route('/admin/buttons', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_buttons():
    if request.method == 'POST':
        shape = request.form.get('shape', 'pill')
        if shape in _BUTTON_SHAPES:
            set_setting('buttons.shape', shape)
        font_value = request.form.get('font', '')
        if font_value in dict(_PROMO_FONTS):
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
        return redirect(url_for('admin_buttons', ok='Paramètres mis à jour.'))

    return render_template(
        'admin_buttons.html', config=CONFIG,
        settings=_buttons_settings(), fonts=_PROMO_FONTS,
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


# ── Slideshow /bestof ─────────────────────────────────────────────────────────

SLIDESHOW_DIR = BASE_DIR / 'app' / 'static' / 'slideshow'
_SLIDESHOW_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}

# ── Slide promo (info QR) ────────────────────────────────────────────────────
# Slide auto-générée (fond + QR code galerie + texte), insérée périodiquement
# dans le diaporama /bestof pour informer les invités : présence du photobox,
# galerie en ligne avec vote, upload de photos depuis smartphone. Rendue
# côté client (bestof.html) à partir des réglages ci-dessous — pas d'image
# à régénérer côté serveur, tout est piloté par CSS/JS.

PROMO_DIR = BASE_DIR / 'app' / 'static' / 'promo'
_PROMO_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp'}
_PROMO_FONTS = [
    ('system-ui, "Segoe UI", sans-serif', 'Par défaut (Segoe UI)'),
    ('Georgia, "Times New Roman", serif', 'Georgia (serif)'),
    ('"Trebuchet MS", sans-serif',        'Trebuchet MS'),
    ('Impact, "Arial Narrow", sans-serif', 'Impact'),
    ('"Courier New", monospace',          'Courier New'),
    ('"Comic Sans MS", cursive',          'Comic Sans MS'),
]
_PROMO_DEFAULT_TEXT = (
    "Un photobox est à votre disposition !\n"
    "Retrouvez toutes les photos sur la galerie en ligne et votez pour vos favorites.\n"
    "Vous pouvez aussi envoyer vos propres photos depuis votre smartphone."
)


def _promo_settings():
    return {
        'enabled':         get_setting('slideshow.promo_enabled', '0') == '1',
        'frequency':       max(1, int(get_setting('slideshow.promo_frequency', '6') or '6')),
        'background':      get_setting('slideshow.promo_background_filename', ''),
        'overlay_enabled': get_setting('slideshow.promo_overlay_enabled', '1') == '1',
        'qr_size':         max(60, int(get_setting('slideshow.promo_qr_size', '220') or '220')),
        'text':            get_setting('slideshow.promo_text', '') or _PROMO_DEFAULT_TEXT,
        'text_size':       max(10, int(get_setting('slideshow.promo_text_size', '28') or '28')),
        'text_font':       get_setting('slideshow.promo_text_font', _PROMO_FONTS[0][0]),
        'text_color':      get_setting('slideshow.promo_text_color', '#ffffff'),
    }


def _promo_public_data():
    """Représentation JSON de la page promo, partagée par /api/bestof/slides
    (rafraîchissement complet, cadencé par slideshow.refresh_interval) et
    /api/bestof/promo-settings (interrogée à cadence fixe et rapide par le
    kiosque déjà ouvert, pour appliquer les changements du back office sans
    attendre — voir bestof.html)."""
    p = _promo_settings()
    return {
        'enabled':         p['enabled'],
        'frequency':       p['frequency'],
        'background_url':  (url_for('static', filename=f'promo/{p["background"]}')
                             if p['background'] else ''),
        'overlay_enabled': p['overlay_enabled'],
        'qr_url':          url_for('qr_png'),
        'qr_size':         p['qr_size'],
        'text':            p['text'],
        'text_size':       p['text_size'],
        'text_font':       p['text_font'],
        'text_color':     p['text_color'],
    }


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
        'promo':            _promo_public_data(),
        'show_media_id':    _media_id_settings()['show_on_bestof'],
        'show_tags':        tags_cfg['show_on_bestof'],
        'tags_style':       {
            'font':       tags_cfg['style_font'],
            'bg_color':   tags_cfg['style_bg_color'],
            'text_color': tags_cfg['style_text_color'],
            'font_size':  tags_cfg['style_font_size'],
        },
    })


@app.route('/api/bestof/promo-settings')
def api_bestof_promo_settings():
    """Réglages légers de la page promo, interrogés à cadence fixe par le
    kiosque déjà ouvert (voir bestof.html) — indépendant de
    slideshow.refresh_interval, qui ne cadence que le rafraîchissement complet
    (captures, images intermédiaires) et peut être réglé sur une valeur lente,
    voire désactivée, sans que ça ralentisse l'application des changements
    faits dans /admin/slideshow → Page promo."""
    return jsonify(_promo_public_data())


@app.route('/admin/slideshow', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_slideshow():
    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'settings':
            set_setting('slideshow.type',           request.form.get('type', 'both'))
            set_setting('slideshow.delay',          str(max(1, int(request.form.get('delay', '5') or '5'))))
            set_setting('slideshow.order',          request.form.get('order', 'chrono'))
            set_setting('slideshow.date_from',      request.form.get('date_from', '').strip())
            set_setting('slideshow.date_to',        request.form.get('date_to', '').strip())
            set_setting('slideshow.vote_min', request.form.get('vote_min', '').strip())
            set_setting('slideshow.vote_max', request.form.get('vote_max', '').strip())
            set_setting('slideshow.refresh_interval',
                        str(max(0, int(request.form.get('refresh_interval', '300') or '300'))))
            return redirect(url_for('admin_slideshow', ok='Paramètres mis à jour.'))

        if action == 'promo_settings':
            set_setting('slideshow.promo_enabled', '1' if request.form.get('promo_enabled') else '0')
            set_setting('slideshow.promo_overlay_enabled',
                        '1' if request.form.get('promo_overlay_enabled') else '0')
            raw_freq = (request.form.get('promo_frequency') or '').strip()
            if raw_freq.isdigit() and int(raw_freq) > 0:
                set_setting('slideshow.promo_frequency', raw_freq)
            raw_qr = (request.form.get('promo_qr_size') or '').strip()
            if raw_qr.isdigit() and int(raw_qr) >= 60:
                set_setting('slideshow.promo_qr_size', raw_qr)
            set_setting('slideshow.promo_text', request.form.get('promo_text', '').strip())
            raw_tsize = (request.form.get('promo_text_size') or '').strip()
            if raw_tsize.isdigit() and int(raw_tsize) >= 10:
                set_setting('slideshow.promo_text_size', raw_tsize)
            font_value = request.form.get('promo_text_font', '')
            if font_value in dict(_PROMO_FONTS):
                set_setting('slideshow.promo_text_font', font_value)
            color_value = request.form.get('promo_text_color', '').strip()
            if re.fullmatch(r'#[0-9a-fA-F]{6}', color_value):
                set_setting('slideshow.promo_text_color', color_value)
            return redirect(url_for('admin_slideshow', ok='Réglages de la page promo mis à jour.'))

        if action == 'promo_upload':
            file = request.files.get('background')
            if not file or not file.filename:
                return redirect(url_for('admin_slideshow', err='Aucun fichier sélectionné.'))
            ext = Path(file.filename).suffix.lower()
            if ext not in _PROMO_ALLOWED_EXT:
                return redirect(url_for('admin_slideshow', err='Format non supporté (PNG, JPG, WEBP).'))
            PROMO_DIR.mkdir(parents=True, exist_ok=True)
            # Un seul fond actif à la fois : on retire l'ancien fichier avant d'enregistrer le nouveau.
            old = get_setting('slideshow.promo_background_filename', '')
            if old:
                (PROMO_DIR / old).unlink(missing_ok=True)
            safe = f'promo-bg-{int(datetime.now().timestamp())}{ext}'
            file.save(str(PROMO_DIR / safe))
            set_setting('slideshow.promo_background_filename', safe)
            return redirect(url_for('admin_slideshow', ok='Fond mis à jour.'))

        if action == 'promo_bg_delete':
            old = get_setting('slideshow.promo_background_filename', '')
            if old:
                (PROMO_DIR / old).unlink(missing_ok=True)
                set_setting('slideshow.promo_background_filename', '')
                return redirect(url_for('admin_slideshow', ok='Fond supprimé.'))
            return redirect(url_for('admin_slideshow', err='Aucun fond à supprimer.'))

        if action == 'upload':
            file = request.files.get('image')
            if not file or not file.filename:
                return redirect(url_for('admin_slideshow', err='Aucun fichier sélectionné.'))
            ext = Path(file.filename).suffix.lower()
            if ext not in _SLIDESHOW_ALLOWED_EXT:
                return redirect(url_for('admin_slideshow', err='Format non supporté (PNG, JPG, WEBP, GIF).'))
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(file.filename).stem) + ext
            # Rendre le nom unique si collision
            SLIDESHOW_DIR.mkdir(parents=True, exist_ok=True)
            dest = SLIDESHOW_DIR / safe
            if dest.exists():
                safe = f'{Path(safe).stem}_{int(datetime.now().timestamp())}{ext}'
                dest = SLIDESHOW_DIR / safe
            file.save(str(dest))
            add_slideshow_image(safe)
            return redirect(url_for('admin_slideshow', ok=f'Image « {safe} » ajoutée.'))

        if action == 'delete':
            image_id = int(request.form.get('image_id', 0))
            img = delete_slideshow_image_db(image_id)
            if img:
                (SLIDESHOW_DIR / img['filename']).unlink(missing_ok=True)
                return redirect(url_for('admin_slideshow', ok=f'Image supprimée.'))
            return redirect(url_for('admin_slideshow', err='Image introuvable.'))

    s = _slideshow_settings()
    images = list_slideshow_images()
    return render_template(
        'admin_slideshow.html', config=CONFIG,
        settings=s, images=images,
        promo=_promo_settings(), promo_fonts=_PROMO_FONTS,
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


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
    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'settings':
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
            return redirect(url_for('admin_guest_uploads', ok='Paramètres mis à jour.'))

        if action == 'regenerate_token':
            set_setting('guest_upload.token', secrets.token_urlsafe(6))
            return redirect(url_for('admin_guest_uploads',
                                    ok="Nouveau lien généré — l'ancien lien/QR ne fonctionne plus."))

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

    s = _guest_upload_settings()
    share_url = (request.host_url.rstrip('/') + url_for('guest_upload_page', token=s['token'])
                if s['token'] else None)
    return render_template(
        'admin_guest_uploads.html', config=CONFIG, settings=s, share_url=share_url,
        pending=list_guest_uploads('pending'), approved=list_guest_uploads('approved'),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


@app.route('/admin/guest-uploads/qr.png')
@require_admin_auth
def admin_guest_upload_qr():
    s = _guest_upload_settings()
    if not s['token']:
        abort(404)
    share_url = request.host_url.rstrip('/') + url_for('guest_upload_page', token=s['token'])
    return send_file(generate_qr_png(share_url), mimetype='image/png', download_name='partage-qr.png')


@app.route('/admin/screensaver', methods=['GET', 'POST'])
@require_admin_auth
@csrf_protect
def admin_screensaver():
    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'settings':
            set_setting('ui.screensaver_enabled', '1' if request.form.get('enabled') else '0')
            raw_timeout = (request.form.get('timeout_min') or '').strip()
            if raw_timeout.isdigit() and int(raw_timeout) > 0:
                set_setting('ui.screensaver_timeout_min', raw_timeout)
            raw_delay = (request.form.get('delay') or '').strip()
            if raw_delay.isdigit() and int(raw_delay) > 0:
                set_setting('ui.screensaver_delay', raw_delay)
            set_setting('ui.screensaver_include_captures', '1' if request.form.get('include_captures') else '0')
            return redirect(url_for('admin_screensaver', ok='Paramètres mis à jour.'))

        if action == 'upload':
            file = request.files.get('image')
            if not file or not file.filename:
                return redirect(url_for('admin_screensaver', err='Aucun fichier sélectionné.'))
            ext = Path(file.filename).suffix.lower()
            if ext not in _SCREENSAVER_ALLOWED_EXT:
                return redirect(url_for('admin_screensaver', err='Format non supporté (PNG, JPG, WEBP, GIF).'))
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(file.filename).stem) + ext
            SCREENSAVER_DIR.mkdir(parents=True, exist_ok=True)
            dest = SCREENSAVER_DIR / safe
            if dest.exists():
                safe = f'{Path(safe).stem}_{int(datetime.now().timestamp())}{ext}'
                dest = SCREENSAVER_DIR / safe
            file.save(str(dest))
            add_screensaver_image(safe)
            return redirect(url_for('admin_screensaver', ok=f'Image « {safe} » ajoutée.'))

        if action == 'delete':
            image_id = int(request.form.get('image_id', 0))
            img = delete_screensaver_image_db(image_id)
            if img:
                (SCREENSAVER_DIR / img['filename']).unlink(missing_ok=True)
                return redirect(url_for('admin_screensaver', ok='Image supprimée.'))
            return redirect(url_for('admin_screensaver', err='Image introuvable.'))

    return render_template(
        'admin_screensaver.html', config=CONFIG,
        settings=_screensaver_settings(), images=list_screensaver_images(),
        alert_success=request.args.get('ok'),
        alert_error=request.args.get('err'),
    )


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
        return _admin_redirect(error='Aucun fichier sélectionné.')
    if not file.filename.lower().endswith('.zip'):
        return _admin_redirect(error='Le fichier doit être un ZIP.')

    tmpdir = tempfile.mkdtemp()
    counts = None
    try:
        with zipfile.ZipFile(file.stream) as zf:
            if zf.testzip() is not None:
                return _admin_redirect(error='ZIP corrompu.')
            # Sécurité : refuser les chemins traversants
            for name in zf.namelist():
                if name.startswith('/') or '..' in name:
                    return _admin_redirect(error='ZIP non autorisé (chemin invalide).')
            zf.extractall(tmpdir)
        counts = _import_pack_from_dir(Path(tmpdir))
    except zipfile.BadZipFile:
        return _admin_redirect(error='Fichier ZIP invalide.')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    msg = f"{counts['frames_imported']} cadre(s) importé(s)"
    if counts['screensaver_imported']:
        msg += f", {counts['screensaver_imported']} image(s) de veille importée(s)"
    skipped_total = counts['frames_skipped'] + counts['screensaver_skipped']
    if skipped_total:
        msg += f', {skipped_total} ignoré(s) (voir logs)'
    return _admin_redirect(success=msg)


# ── Démarrage ─────────────────────────────────────────────────────────────────

def _port_is_free(host: str, port: int) -> bool:
    bind_host = '' if host == '0.0.0.0' else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((bind_host, port))
            return True
        except OSError:
            return False


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

    if not _port_is_free(_host, _port):
        # Port 80 souvent indisponible sous Windows (IIS, Skype, etc.) —
        # on bascule avant même de tenter le bind plutôt que de planter.
        logger.warning(
            'Port %s indisponible sur %s, bascule sur le port 8080 '
            '— modifiez server.port dans config.toml pour changer ce comportement.',
            _port, _host,
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
