import logging
import threading
import time

import cv2
import numpy as np

from config_loader import CONFIG, FRAMES_DIR
from db import get_frame_by_id_db, get_setting

logger = logging.getLogger('pictotem')

CAM_LOCK = threading.Lock()
CAM = None
VIDEO_CAPTURE_ACTIVE = threading.Event()

# Frame la plus récente capturée par la boucle d'enregistrement vidéo (voir
# capture_video() dans app.py), republiée vers l'aperçu live pendant que
# VIDEO_CAPTURE_ACTIVE est actif — évite une deuxième lecture caméra
# concurrente (un seul flux USB, un seul lecteur à la fois) tout en gardant
# l'aperçu vivant au lieu de rester figé pendant l'enregistrement.
_LATEST_RECORDING_FRAME = None
_RECORDING_FRAME_LOCK = threading.Lock()

# Point 6 : cache thread-safe avec limite de taille
_OVERLAY_CACHE: dict = {}
_OVERLAY_CACHE_LOCK = threading.Lock()
_OVERLAY_CACHE_MAX = 50

# Lazy-generated JPEG affiché quand la caméra est indisponible
_ERROR_JPEG: bytes | None = None


def _make_error_jpeg() -> bytes:
    w, h = 640, 360
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (20, 20, 20)
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, 'Camara indisponible', (w // 2 - 150, h // 2 - 12), font, 0.75, (200, 200, 200), 2)
    cv2.putText(img, 'Verifiez la connexion USB', (w // 2 - 145, h // 2 + 25), font, 0.5, (140, 140, 140), 1)
    _, buf = cv2.imencode('.jpg', img)
    return buf.tobytes()


def get_error_jpeg() -> bytes:
    global _ERROR_JPEG
    if _ERROR_JPEG is None:
        _ERROR_JPEG = _make_error_jpeg()
    return _ERROR_JPEG


# ── Overlay cache ─────────────────────────────────────────────────────────────

def get_overlay_bgra(path, w: int, h: int):
    """Charge et met en cache l'overlay redimensionné. Thread-safe, borné à _OVERLAY_CACHE_MAX entrées."""
    key = (str(path), w, h)
    with _OVERLAY_CACHE_LOCK:
        if key in _OVERLAY_CACHE:
            return _OVERLAY_CACHE[key]

    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        logger.warning('Impossible de charger l\'overlay : %s', path)
        return None
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    if img.shape[:2] != (h, w):
        img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

    with _OVERLAY_CACHE_LOCK:
        if len(_OVERLAY_CACHE) >= _OVERLAY_CACHE_MAX:
            oldest = next(iter(_OVERLAY_CACHE))
            del _OVERLAY_CACHE[oldest]
        _OVERLAY_CACHE[key] = img
    return img


def clear_overlay_cache(frame_id: str):
    with _OVERLAY_CACHE_LOCK:
        keys = [k for k in list(_OVERLAY_CACHE) if frame_id in str(k[0])]
        for k in keys:
            _OVERLAY_CACHE.pop(k, None)


def composite_frame_overlay(frame_bgr, overlay_bgra):
    alpha  = overlay_bgra[:, :, 3].astype(np.float32) / 255.0
    alpha3 = np.stack([alpha, alpha, alpha], axis=2)
    result = (
        frame_bgr.astype(np.float32) * (1.0 - alpha3)
        + overlay_bgra[:, :, :3].astype(np.float32) * alpha3
    )
    return np.clip(result, 0, 255).astype(np.uint8)


def get_frame_overlay_path(frame_id):
    if not frame_id or frame_id == 'none':
        return None
    frame = get_frame_by_id_db(frame_id)
    if not frame or not frame.get('overlay_filename'):
        return None
    return FRAMES_DIR / frame['overlay_filename']


# ── Camera ────────────────────────────────────────────────────────────────────
# Réglages caméra modifiables depuis l'admin (/admin/system), stockés en base
# et prioritaires sur config.toml. Chaîne vide en base = pas de surcharge.

def _cam_int(key: str, hard_default: int) -> int:
    raw = get_setting(f'camera.{key}', '')
    if raw.strip().lstrip('-').isdigit():
        return int(raw)
    return int(CONFIG['camera'].get(key, hard_default))


def _cam_bool(key: str, hard_default: bool) -> bool:
    raw = get_setting(f'camera.{key}', '')
    if raw in ('0', '1'):
        return raw == '1'
    return bool(CONFIG['camera'].get(key, hard_default))


def camera_backend_flag():
    # DirectShow : backend webcam standard d'OpenCV sous Windows.
    return cv2.CAP_DSHOW


def reset_camera():
    """Force la fermeture de la caméra active : le prochain appel à
    get_camera() rouvre le périphérique avec les réglages courants (utilisé
    par l'admin juste après un changement de caméra/résolution)."""
    global CAM
    with CAM_LOCK:
        if CAM is not None:
            try:
                CAM.release()
            except Exception:
                pass
            CAM = None


def apply_portrait(frame):
    rotation = _cam_int('rotation', 0)
    if rotation == 90:
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    elif rotation == 180:
        frame = cv2.rotate(frame, cv2.ROTATE_180)
    elif rotation == 270:
        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if _cam_bool('preview_mirror', False):
        frame = cv2.flip(frame, 1)
    return frame


def maybe_crop_portrait(frame):
    if not CONFIG['camera'].get('force_portrait_crop', False):
        return frame
    ratio = CONFIG['camera'].get('portrait_ratio', '9:16')
    rw, rh = ratio.split(':')
    return _crop_to_aspect(frame, int(rw), int(rh))


def _crop_to_aspect(frame, aspect_w, aspect_h):
    h, w = frame.shape[:2]
    target = aspect_w / aspect_h
    current = w / h
    if current > target:
        new_w = int(h * target)
        x1 = max(0, (w - new_w) // 2)
        return frame[:, x1:x1 + new_w]
    new_h = int(w / target)
    y1 = max(0, (h - new_h) // 2)
    return frame[y1:y1 + new_h, :]


def get_camera():
    global CAM
    if CAM is None or not CAM.isOpened():
        device = _cam_int('device', 0)
        logger.info('Ouverture de la caméra %s', device)
        CAM = cv2.VideoCapture(device, camera_backend_flag())
        CAM.set(cv2.CAP_PROP_FRAME_WIDTH,  _cam_int('width', 1920))
        CAM.set(cv2.CAP_PROP_FRAME_HEIGHT, _cam_int('height', 1080))
        CAM.set(cv2.CAP_PROP_FPS,          int(CONFIG['camera'].get('fps', 20)))
        if not CAM.isOpened():
            raise RuntimeError("Impossible d'ouvrir la caméra")
        for _ in range(int(CONFIG['camera'].get('warmup_frames', 5))):
            CAM.read()
    return CAM


def read_frame():
    with CAM_LOCK:
        cam = get_camera()
        ok, frame = cam.read()
        if not ok or frame is None:
            logger.warning('Lecture de frame échouée, réouverture de la caméra')
            try:
                cam.release()
            except Exception:
                pass
            globals()['CAM'] = None
            cam = get_camera()
            ok, frame = cam.read()
        if not ok or frame is None:
            raise RuntimeError('Impossible de lire la caméra')
        frame = apply_portrait(frame)
        frame = maybe_crop_portrait(frame)
        return frame


def publish_recording_frame(frame):
    """Appelé par la boucle d'enregistrement vidéo (app.py) à chaque frame
    capturée, pour que stream_generator() puisse la réutiliser sans relire la
    caméra une deuxième fois pendant l'enregistrement."""
    global _LATEST_RECORDING_FRAME
    with _RECORDING_FRAME_LOCK:
        _LATEST_RECORDING_FRAME = frame


def _pop_recording_frame():
    with _RECORDING_FRAME_LOCK:
        return _LATEST_RECORDING_FRAME


def clear_recording_frame():
    global _LATEST_RECORDING_FRAME
    with _RECORDING_FRAME_LOCK:
        _LATEST_RECORDING_FRAME = None


def encode_jpeg(frame, quality=None):
    quality = quality or int(CONFIG['camera'].get('jpeg_quality', 92))
    ok, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError('Échec encodage JPEG')
    return buffer.tobytes()


# Point 7 : générateur MJPEG avec gestion de la déconnexion caméra
def stream_generator():
    _camera_was_down = False
    fps = max(int(CONFIG['camera'].get('fps', 20)), 1)
    frame_interval = 1 / fps
    while True:
        try:
            if VIDEO_CAPTURE_ACTIVE.is_set():
                # Ne pas relire la caméra ici : la boucle d'enregistrement
                # (capture_video() dans app.py) la lit déjà en continu et
                # publie chaque frame via publish_recording_frame() — on la
                # réutilise pour garder l'aperçu vivant sans contention.
                frame = _pop_recording_frame()
                if frame is None:
                    time.sleep(0.05)
                    continue
                jpg = encode_jpeg(frame, quality=82)
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
                time.sleep(frame_interval)
                continue
            frame = read_frame()
            jpg = encode_jpeg(frame, quality=82)
            if _camera_was_down:
                logger.info('Caméra reconnectée')
                _camera_was_down = False
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
            time.sleep(frame_interval)
        except RuntimeError as exc:
            if not _camera_was_down:
                logger.warning('Caméra indisponible dans le stream : %s', exc)
                _camera_was_down = True
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + get_error_jpeg() + b'\r\n')
            time.sleep(1.0)
        except Exception:
            logger.exception('stream : erreur inattendue')
            time.sleep(0.5)
