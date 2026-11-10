"""Roles, exposures and categories from file names alone."""

from __future__ import annotations

from datetime import datetime

import pytest

from imagebio.live.layout import PRIMARY_ROLES, Member, category_of, classify, name_time, primary_rank

EXPOSURE = "FoilHole_21049436_Data_21047839_21047841_20261110_101500"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f"{EXPOSURE}.mrc", Member("image", EXPOSURE)),
        (f"{EXPOSURE}.xml", Member("xml", EXPOSURE)),
        (f"{EXPOSURE}.jpg", Member("preview", EXPOSURE)),
        (f"{EXPOSURE}_Fractions.mrc", Member("movie", EXPOSURE)),
        (f"{EXPOSURE}_fractions.tiff", Member("movie", EXPOSURE)),
        (f"{EXPOSURE}_EER.eer", Member("movie", EXPOSURE)),
        ("movie_001.eer", Member("movie", "movie_001")),  # an EER is a movie whatever its name
        ("tilt.mrc.mdoc", Member("mdoc", "tilt")),
        ("tilt.mrc", Member("image", "tilt")),
        ("stack.ST", Member("image", "stack")),
        ("image.TIF", Member("image", "image")),
        ("photo.JPEG", Member("preview", "photo")),
        ("EpuSession.dm", None),
        ("20261110_EER_GainReference.gain", None),
        ("notes.txt", None),
        (".hidden.mrc", None),
        (".FoilHole.mrc.Xy12ab", None),  # a file rsync is still writing
        ("backup.mrc~", None),
        ("noextension", None),
        (".mrc", None),
    ],
)
def test_classify(name: str, expected: Member | None) -> None:
    assert classify(name) == expected


def test_the_tile_is_drawn_from_the_image_then_the_movie_then_the_preview() -> None:
    assert PRIMARY_ROLES == ("image", "movie", "preview")
    assert [primary_rank(r) for r in ("image", "movie", "preview", "xml", "mdoc", "other")] == [
        0,
        1,
        2,
        3,
        3,
        3,
    ]


@pytest.mark.parametrize(
    ("directory", "exposure", "expected"),
    [
        ("Images-Disc1/GridSquare_21049200/Data", EXPOSURE, "data"),
        ("Data", EXPOSURE, "data"),
        ("Images-Disc1/GridSquare_21049200/Data", "something_else", "image"),
        ("Images-Disc1/GridSquare_21049200/FoilHoles", "FoilHole_21049436_20261110_101400", "foilhole"),
        ("Images-Disc1/GridSquare_21049200", "GridSquare_20261110_100000", "gridsquare"),
        ("Atlas", "Atlas_1", "atlas"),
        ("Atlas/Tiles", "Tile_001", "atlas"),
        ("", "micrograph_001", "image"),
        ("some/Data/deeper", EXPOSURE, "image"),
    ],
)
def test_category(directory: str, exposure: str, expected: str) -> None:
    assert category_of(directory, exposure) == expected


def test_the_time_written_in_an_epu_name() -> None:
    expected = datetime(2026, 11, 10, 10, 15, 0).timestamp()
    assert name_time(EXPOSURE) == expected
    assert name_time(f"{EXPOSURE}_Fractions") == expected
    assert name_time("GridSquare_20261110_100000") == datetime(2026, 11, 10, 10, 0, 0).timestamp()
    # an identifier that looks like a date does not win over the real one, which comes last
    assert name_time("FoilHole_20251231_235959_Data_1_2_20261110_101500") == expected
    for name in (
        "micrograph_001",
        "FoilHole_21049436",
        "x_20261340_101500",
        "x_20261110_256199",
        "x_120261110_101500",
    ):
        assert name_time(name) is None
