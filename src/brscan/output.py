"""Page post-processing: auto-crop the scanner over-scan and assemble output.

Crop and assembly use Pillow / img2pdf, whose wheels bundle their own codecs --
no ImageMagick or other system libraries required.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

# Per-channel difference (0-255) below which a pixel counts as "background".
# Mirrors the old ImageMagick `-fuzz 8%` trim.
_FUZZ = 24


def crop(path: Path) -> None:
    """Trim uniform blank/gray padding (the scanner over-scan tail) in place."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    try:
        with Image.open(path) as im:
            dpi = im.info.get("dpi", (300, 300))
            rgb = im.convert("RGB")
        bg_color = rgb.getpixel((2, rgb.height - 2))
        bg = Image.new("RGB", rgb.size, bg_color)
        diff = ImageChops.difference(rgb, bg).convert("L")
        mask = diff.point(lambda p: 255 if p > _FUZZ else 0)
        bbox = mask.getbbox()
        if bbox and bbox != (0, 0, rgb.width, rgb.height):
            rgb.crop(bbox).save(path, "JPEG", quality=92, dpi=dpi)
    except Exception:
        # Cropping is best-effort; leave the original page untouched on any error.
        pass


def assemble_pdf(out_path: Path, pages: List[Path]) -> None:
    import img2pdf

    with open(out_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in pages]))


def save_jpegs(pages: List[Path], out_path: Path, note=None) -> None:
    stem = out_path.with_suffix("")
    for i, page in enumerate(pages, 1):
        dest = stem.parent / f"{stem.name}-p{i:02d}.jpg"
        shutil.copy(page, dest)
        if note:
            note(f"saved {dest}")
