import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

from unittest.mock import MagicMock  # noqa: E402

# OpenCV / qrcode ne servent pas à ces routes : stub si absents de l'environnement de test.
for _mod in ('cv2', 'qrcode', 'qrcode.constants'):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = MagicMock()

import app as app_module  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'app.db')
    photos, thumbs = tmp_path / 'photos', tmp_path / 'thumbs'
    photos.mkdir()
    thumbs.mkdir()
    monkeypatch.setattr(app_module, 'PHOTO_DIR', photos)
    monkeypatch.setattr(app_module, 'THUMBS_DIR', thumbs)
    db.init_db()
    (photos / 'p.jpg').write_bytes(b'PHOTO-BYTES')
    (thumbs / 't.jpg').write_bytes(b'THUMB-BYTES')
    conn = sqlite3.connect(tmp_path / 'app.db')
    conn.execute(
        "INSERT INTO captures(kind, filename, thumb_filename, created_at, printed, media_uid) "
        "VALUES('photo','p.jpg','t.jpg','2025-01-01T10:00:00',0,'123456')")
    conn.execute(
        "INSERT INTO captures(kind, filename, thumb_filename, created_at, printed, media_uid) "
        "VALUES('photo','p.jpg',NULL,'2025-01-01T10:00:00',0,'654321')")
    conn.commit()
    conn.close()
    return app_module.app.test_client()


def test_media_served_without_login(client):
    r = client.get('/api/media/123456')
    assert r.status_code == 200
    assert r.data == b'PHOTO-BYTES'


def test_thumb_served_without_login(client):
    r = client.get('/api/media/123456/thumb')
    assert r.status_code == 200
    assert r.data == b'THUMB-BYTES'


def test_thumb_missing_is_404(client):
    assert client.get('/api/media/654321/thumb').status_code == 404


def test_info(client):
    r = client.get('/api/media/123456/info')
    assert r.status_code == 200
    body = r.get_json()
    assert body['media_uid'] == '123456'
    assert body['kind'] == 'photo'
    assert body['url'] == '/api/media/123456'


def test_unknown_uid_is_404(client):
    assert client.get('/api/media/999999').status_code == 404
    assert client.get('/api/media/999999/info').status_code == 404
