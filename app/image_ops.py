"""Opérations sur fichiers image (Pillow uniquement, sans dépendance à l'app)."""
import os
from pathlib import Path

from PIL import Image, ImageOps

ROTATE_ANGLES = {90, 180, -90}


def rotate_image_file(path: Path, angle: int) -> None:
    """Pivote l'image en place (angle horaire : 90, 180 ou -90). Lève ValueError si impossible."""
    if angle not in ROTATE_ANGLES:
        raise ValueError('Angle invalide.')
    with Image.open(path) as im:
        fmt = im.format or 'PNG'
        if getattr(im, 'is_animated', False):
            raise ValueError('Les images animées ne peuvent pas être pivotées.')
        im = ImageOps.exif_transpose(im)
        out = im.rotate(-angle, expand=True)
        save_kw = {}
        if fmt == 'JPEG':
            out = out.convert('RGB')
            save_kw = {'quality': 92, 'optimize': True}
        tmp = path.with_name(path.name + '.rot.tmp')
        try:
            out.save(tmp, format=fmt, **save_kw)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)
