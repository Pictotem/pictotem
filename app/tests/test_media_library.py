import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    return path


def _legacy_seed(path):
    """Base 'ancienne version' : init_db puis données dans les anciennes tables,
    flag de migration retiré pour rejouer la migration."""
    db.init_db()
    c = sqlite3.connect(path)
    c.execute("DELETE FROM settings WHERE key='media_library.migrated'")
    c.execute("DELETE FROM media_library")
    c.execute("INSERT INTO other_images(filename,label,created_at) VALUES('a.png','A','2025-01-01')")
    c.execute("INSERT INTO screensaver_images(filename,created_at,active) VALUES('s.png','2025-01-01',0)")
    c.execute("INSERT INTO promo_backgrounds(id,filename,label,created_at,kind) VALUES(7,'bg.png','Fond','2025-01-01','image')")
    c.execute("INSERT INTO promo_backgrounds(id,filename,label,created_at,kind,color1,color2,angle) "
              "VALUES(8,'','Deg','2025-01-02','gradient','#000','#fff',90)")
    c.execute("INSERT INTO retrospective_overlays(filename,active,sort_order,created_at) VALUES('o.png',1,3,'2025-01-01')")
    c.execute("INSERT INTO promo_pages(background_id,created_at) VALUES(7,'2025-01-01')")
    c.commit()
    c.close()


def test_migration_and_background_remap(tmpdb):
    _legacy_seed(tmpdb)
    db.init_db()
    assert [m['label'] for m in db.list_other_images()] == ['A']
    assert db.list_screensaver_images(active_only=True) == []
    assert db.list_retrospective_overlays()[0]['sort_order'] == 3
    bgs = {b['label']: b for b in db.list_promo_backgrounds()}
    assert bgs['Deg']['kind'] == 'gradient'
    page = db.list_promo_pages()[0]
    assert page['background_id'] == bgs['Fond']['id']
    assert page['background_filename'] == 'bg.png'
    # idempotent
    db.init_db()
    assert len(db.list_media()) == 5


def test_delete_background_resets_pages(tmpdb):
    _legacy_seed(tmpdb)
    db.init_db()
    bg = [b for b in db.list_promo_backgrounds() if b['filename'] == 'bg.png'][0]
    item = db.delete_media_db(bg['id'])
    assert item['filename'] == 'bg.png'
    assert db.list_promo_pages()[0]['background_id'] is None


def test_crud_and_urls(tmpdb):
    db.init_db()
    mid = db.add_media('other', 'x.png', 'X')
    assert db.media_url(db.get_media(mid)) == '/static/other_images/x.png'
    db.rename_media(mid, 'Y')
    assert db.get_media(mid)['label'] == 'Y'
    first = db.add_retrospective_overlay('1.png')
    second = db.add_retrospective_overlay('2.png')
    db.move_retrospective_overlay(second, 'up')
    assert [o['id'] for o in db.list_retrospective_overlays()] == [second, first]


def test_rotate_image_file(tmp_path):
    from PIL import Image
    import image_ops

    p = tmp_path / 'x.png'
    Image.new('RGB', (40, 20), 'red').save(p)
    image_ops.rotate_image_file(p, 90)
    assert Image.open(p).size == (20, 40)
    image_ops.rotate_image_file(p, 180)
    assert Image.open(p).size == (20, 40)
    image_ops.rotate_image_file(p, -90)
    assert Image.open(p).size == (40, 20)
    with pytest.raises(ValueError):
        image_ops.rotate_image_file(p, 45)
    assert not list(tmp_path.glob('*.tmp'))
