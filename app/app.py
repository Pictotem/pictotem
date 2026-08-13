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
from contextlib import closing
from datetime import datetime

import cv2
from flask import (Flask, Response, abort, jsonify, make_response, redirect,
                   render_template, request, send_file, send_from_directory,
                   session, url_for)

from auth import (auth_enabled, build_secret_key, check_admin_password,
                  check_gallery_password, check_main_password, csrf_protect,
                  gallery_session_key, generate_csrf_token,
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
                get_setting, init_db, list_captures, list_emails, list_frames,
                record_capture, save_email, set_setting, update_email_by_id,
                upsert_frame,
                list_slideshow_images, add_slideshow_image, delete_slideshow_image_db,
                list_screensaver_images, add_screensaver_image, delete_screensaver_image_db,
                cast_vote, admin_adjust_vote, get_voter_votes)
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
    capture_id = record_capture('photo', filename, thumb_name)
    logger.info('Photo capturée %s (cadre=%s)', filename, frame_id)
    return jsonify({'ok': True, 'id': capture_id, 'kind': 'photo', 'filename': filename,
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

    capture_id = record_capture('video', final_filename, thumb_name)
    logger.info('Vidéo capturée %s', final_filename)
    return jsonify({'ok': True, 'id': capture_id, 'kind': 'video', 'filename': final_filename,
                    'url': f'/media/video/{final_filename}', 'message': message_text()})


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

    captures, total = list_captures(sort, kind, page=page, page_size=page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)

    voter_token = request.cookies.get('voter_token', '')
    new_token = False
    if not voter_token:
        voter_token = secrets.token_hex(16)
        new_token = True

    voter_votes = get_voter_votes(voter_token)
    vote_enabled = get_setting('vote.enabled', '1') == '1'
    vote_cfg = _vote_cfg()

    resp = make_response(render_template(
        'gallery.html', captures=captures, sort=sort, kind=kind, config=CONFIG,
        email_cookie=email_cookie, gallery_text=get_setting('gallery_text', ''),
        page=page, total_pages=total_pages, total=total,
        vote_enabled=vote_enabled, voter_votes=voter_votes, vote_cfg=vote_cfg,
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


# ── Admin — général ───────────────────────────────────────────────────────────

@app.route('/admin')
@require_admin_auth
def admin_home():
    return render_template('admin_home.html', config=CONFIG)


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
    if not capture_id or value not in (1, -1):
        return jsonify({'ok': False, 'error': 'Paramètres invalides'}), 400
    try:
        new_score, your_vote = cast_vote(int(capture_id), voter_token, int(value))
        return jsonify({'ok': True, 'score': new_score, 'your_vote': your_vote})
    except Exception:
        logger.exception('Erreur vote capture #%s', capture_id)
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


# ── Slideshow /bestof ─────────────────────────────────────────────────────────

SLIDESHOW_DIR = BASE_DIR / 'app' / 'static' / 'slideshow'
_SLIDESHOW_ALLOWED_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}


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
    return render_template('bestof.html', config=CONFIG)


@app.route('/api/bestof/slides')
def api_bestof_slides():
    s = _slideshow_settings()

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

    sql = f"SELECT kind, filename, vote_score FROM captures WHERE {' AND '.join(conditions)} ORDER BY {order_clause}"
    with closing(db_conn()) as conn:
        rows = conn.execute(sql, params).fetchall()

    captures = [
        {
            'type':  r['kind'],
            'url':   url_for('media_photo' if r['kind'] == 'photo' else 'media_video',
                             filename=r['filename']),
            'score': r['vote_score'],
        }
        for r in rows
    ]

    slideshow_imgs = [
        {'type': 'image', 'url': f'/static/slideshow/{img["filename"]}'}
        for img in list_slideshow_images()
    ]

    return jsonify({
        'captures':         captures,
        'slideshow_images': slideshow_imgs,
        'delay':            s['delay'],
        'order':            s['order'],
        'refresh_interval': s['refresh_interval'],
    })


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
#   ]
# }
# Utilisé à la fois par l'import ZIP admin (ci-dessous) et par le chargement
# automatique au démarrage depuis le dossier pack/ (voir _auto_import_startup_pack).

def _import_pack_from_dir(base_dir: Path) -> tuple[int, int]:
    """Importe cadres + accueil depuis un dossier déjà
    extrait (contenant pack.json, cherché n'importe où dans l'arbre — ou à
    défaut les PNG en vrac). Renvoie (importés, ignorés)."""
    from PIL import Image

    pack_json_candidates = list(base_dir.rglob('pack.json'))
    root = pack_json_candidates[0].parent if pack_json_candidates else base_dir

    welcome_filename = None
    default_id = None

    if pack_json_candidates:
        pack_data = json.loads(pack_json_candidates[0].read_text(encoding='utf-8'))
        specs = pack_data['frames']
        welcome_filename = pack_data.get('welcome')
        default_id = pack_data.get('default')
    else:
        pngs = sorted(p for p in root.rglob('*.png'))
        specs = []
        for i, p in enumerate(pngs):
            if 'accueil' in p.stem.lower() or 'welcome' in p.stem.lower():
                welcome_filename = p.name
            else:
                specs.append({
                    'filename': p.name,
                    'id': re.sub(r'[^a-z0-9]+', '-', p.stem.lower()).strip('-'),
                    'label': p.stem.replace('_', ' '),
                    'sort_order': (i + 1) * 10,
                })

    imported = skipped = 0
    logger.info('Pack import : base_dir=%s, %d spec(s), welcome=%s, default=%s',
                root, len(specs), welcome_filename, default_id)
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

    return imported, skipped


def _auto_import_startup_pack():
    """Charge automatiquement, à chaque démarrage, le pack (cadres + accueil
    + image de démarrage) posé dans le dossier pack/ à la racine — pratique
    pour préparer le thème d'un événement sans repasser par l'admin. Ne fait
    rien si pack/ est absent ou ne contient pas de pack.json."""
    pack_dir = BASE_DIR / 'pack'
    if not pack_dir.is_dir() or not any(pack_dir.rglob('pack.json')):
        return
    try:
        imported, skipped = _import_pack_from_dir(pack_dir)
        logger.info('Pack de démarrage chargé (pack/) : %d cadre(s), %d ignoré(s).', imported, skipped)
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
    imported = skipped = 0
    try:
        with zipfile.ZipFile(file.stream) as zf:
            if zf.testzip() is not None:
                return _admin_redirect(error='ZIP corrompu.')
            # Sécurité : refuser les chemins traversants
            for name in zf.namelist():
                if name.startswith('/') or '..' in name:
                    return _admin_redirect(error='ZIP non autorisé (chemin invalide).')
            zf.extractall(tmpdir)
        imported, skipped = _import_pack_from_dir(Path(tmpdir))
    except zipfile.BadZipFile:
        return _admin_redirect(error='Fichier ZIP invalide.')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    msg = f'{imported} cadre(s) importé(s)'
    if skipped:
        msg += f', {skipped} ignoré(s) (voir logs)'
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
    l'interface principale de sortir/rentrer du plein écran, protégé par le
    mot de passe admin — pour laisser le personnel accéder ponctuellement à
    Windows sans fermer l'application.

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

    def toggle_fullscreen(self, password: str):
        if not check_admin_password(password or ''):
            logger.warning('Bascule plein écran refusée (mot de passe incorrect).')
            return {'ok': False, 'error': 'Mot de passe incorrect.'}
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
