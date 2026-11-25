import random

import pytest

from brscan.output import BLANK_INK, ink_ratio

PIL = pytest.importorskip("PIL")


def page(tmp_path, name, draw=None, paper=(246, 244, 240)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (2550, 3300), paper)
    if draw:
        draw(ImageDraw.Draw(im), im)
    path = tmp_path / f"{name}.jpg"
    im.save(path, "JPEG", quality=85)
    return path


def specks(d, im):
    rng = random.Random(1)
    for _ in range(300):
        x, y = rng.randrange(im.width), rng.randrange(im.height)
        d.point((x, y), fill=(40, 40, 40))


def bleed_through(d, im):
    for y in range(400, 2800, 60):
        d.rectangle((300, y, 2200, y + 24), fill=(205, 203, 200))


def short_line(d, im):
    d.rectangle((300, 1500, 900, 1530), fill=(30, 30, 30))


def text_page(d, im):
    for y in range(400, 2800, 60):
        d.rectangle((300, y, 2200, y + 24), fill=(20, 20, 20))


@pytest.mark.parametrize("draw", [None, specks, bleed_through])
def test_blank_pages(tmp_path, draw):
    assert ink_ratio(page(tmp_path, "p", draw)) < BLANK_INK


@pytest.mark.parametrize("draw", [short_line, text_page])
def test_pages_with_content(tmp_path, draw):
    assert ink_ratio(page(tmp_path, "p", draw)) > BLANK_INK


def test_blank_colored_paper(tmp_path):
    assert ink_ratio(page(tmp_path, "p", paper=(120, 160, 220))) < BLANK_INK
