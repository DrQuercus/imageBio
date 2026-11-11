"""Particle coordinates read from files written here by hand, in each program's layout."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from fixtures.api import ALICE, BOB, build_tree, make_client
from imagebio.core.errors import CorruptFileError, InvalidRequestError, UnsupportedFormatError
from imagebio.cryo import picks as picks_module
from imagebio.cryo.picks import micrograph_key, read_picks
from imagebio.cryo.star import find_table

SHAPE = (3000, 4000)  # rows, columns

AUTOPICK = """
# version 30001

data_

loop_
_rlnCoordinateX #1
_rlnCoordinateY #2
_rlnClassNumber #3
_rlnAutopickFigureOfMerit #4
_rlnAnglePsi #5
 1204.000000   688.500000            0     0.731000     0.000000
  310.250000  2901.000000            0     1.250000    45.000000
 3999.000000     0.000000            0    -0.100000    90.000000
"""

PARTICLES = """
# version 30001

data_optics

loop_
_rlnOpticsGroupName #1
_rlnOpticsGroup #2
_rlnImagePixelSize #3
opticsGroup1            1     0.830000

# version 30001

data_particles

loop_
_rlnImageName #1
_rlnMicrographName #2
_rlnCoordinateY #3
_rlnCoordinateX #4
_rlnOpticsGroup #5
000001@Extract/job007/Movies/mic_001.mrcs MotionCorr/job002/Movies/mic_001.mrc   100.0   200.0 1
000002@Extract/job007/Movies/mic_001.mrcs MotionCorr/job002/Movies/mic_001.mrc   110.5   210.5 1
000001@Extract/job007/Movies/mic_002.mrcs MotionCorr/job002/Movies/mic_002.mrc  1500.0  2500.0 1
000001@Extract/job007/Movies/mic_010.mrcs "MotionCorr/job002/Movies/mic 010.mrc"   7.0     8.0 1
000002@Extract/job007/Movies/mic_002.mrcs MotionCorr/job002/Movies/mic_002.mrc  1600.0  2600.0 1
"""

CBOX = """
data_global

_cbox_format_version 1.0

data_cryolo

loop_
_CoordinateX #1
_CoordinateY #2
_CoordinateZ #3
_Width #4
_Height #5
_Depth #6
_EstWidth #7
_EstHeight #8
_Confidence #9
_NumBoxes #10
_Angle #11
100.0 200.0 <NA> 180.0 180.0 <NA> 150.0 160.0 0.91 <NA> <NA>
1000.0 50.0 <NA> 180.0 200.0 <NA> 140.0 150.0 0.12 <NA> <NA>
"""


def write(path: Path, text: str) -> Path:
    path.write_text(text.lstrip("\n"))
    return path


def test_names_of_one_micrograph_are_recognised_through_what_programs_add_to_them() -> None:
    key = micrograph_key("FoilHole_1_Data_2_3_20261110_101500.mrc")
    assert key == "foilhole_1_data_2_3_20261110_101500"
    for other in (
        "MotionCorr/job002/Movies/FoilHole_1_Data_2_3_20261110_101500_fractions.mrc",
        "FoilHole_1_Data_2_3_20261110_101500_EER.eer",
        "J12/motioncorrected/012345678901234567890_FoilHole_1_Data_2_3_20261110_101500_fractions_patch_aligned_doseweighted.mrc",
        "AutoPick/job005/Movies/FoilHole_1_Data_2_3_20261110_101500_autopick.star",
        "C:\\data\\FoilHole_1_Data_2_3_20261110_101500_Fractions.tiff",
    ):
        assert micrograph_key(other) == key, other
    assert micrograph_key("FoilHole_1_Data_2_3_20261110_101501.mrc") != key
    assert micrograph_key("mic_001") == "mic_001" and micrograph_key("_fractions.mrc") == "_fractions"
    assert micrograph_key("123_mic.mrc") == "123_mic"  # a short number in front is part of the name


def test_a_relion_pick_file_gives_centres_and_figures_of_merit(tmp_path: Path) -> None:
    found = read_picks(write(tmp_path / "mic_001_autopick.star", AUTOPICK), "mic_001.mrc", SHAPE)
    assert found.format == "star" and not found.many and found.box is None and found.total == 3
    np.testing.assert_array_equal(found.x, np.array([1204.0, 310.25, 3999.0], np.float32))
    np.testing.assert_array_equal(found.y, np.array([688.5, 2901.0, 0.0], np.float32))
    np.testing.assert_allclose(found.score, [0.731, 1.25, -0.1], rtol=1e-6)  # type: ignore[arg-type]
    assert found.notes == ()
    body = found.to_json()
    assert body["count"] == 3 and body["x"] == [1204.0, 310.2, 3999.0] and body["score"][1] == 1.25


def test_a_file_of_a_whole_data_set_gives_the_particles_of_the_image_shown(tmp_path: Path) -> None:
    star = write(tmp_path / "particles.star", PARTICLES)
    table = find_table(star, ("rlnCoordinateX", "rlnCoordinateY"))
    assert table is not None and table.block == "particles" and len(table.labels) == 5
    assert find_table(star, ("rlnOpticsGroupName",)).block == "optics"  # type: ignore[union-attr]
    assert find_table(star, ("rlnNothing",)) is None

    found = read_picks(star, "mic_002.mrc", SHAPE)
    assert found.many and found.total == 2 and found.score is None
    np.testing.assert_array_equal(found.x, [2500.0, 2600.0])  # the columns are X then Y whatever their order
    np.testing.assert_array_equal(found.y, [1500.0, 1600.0])
    again = read_picks(star, "mic_002_fractions.tiff", SHAPE)  # the movie the micrograph was made from
    np.testing.assert_array_equal(again.x, found.x)
    quoted = read_picks(star, "mic 010.mrc", SHAPE)  # a name with a blank, quoted in the file
    assert (quoted.x.tolist(), quoted.y.tolist()) == ([8.0], [7.0])

    none = read_picks(star, "mic_003.mrc", SHAPE)
    assert none.total == 0 and none.x.size == 0 and none.many
    assert none.notes == ("none of the micrographs named in the file (3 or more) is this image",)


def test_boxes_are_given_by_a_corner_and_become_centres(tmp_path: Path) -> None:
    box = write(
        tmp_path / "mic_001.box", "100\t200\t180\t180\t-3\n1000 50 180 200\n\n# a comment\n0 0 64 64\n"
    )
    found = read_picks(box, "anything.mrc", SHAPE)
    assert found.format == "box" and found.total == 3 and found.score is None and not found.many
    np.testing.assert_array_equal(found.x, [190.0, 1090.0, 32.0])
    np.testing.assert_array_equal(found.y, [290.0, 150.0, 32.0])
    assert found.box == 180.0  # the median of the larger sides: 180, 200, 64
    empty = read_picks(write(tmp_path / "empty.box", ""), "a.mrc", SHAPE)
    assert empty.total == 0 and empty.box is None and empty.to_json()["x"] == []


def test_cryolo_boxes_in_both_layouts(tmp_path: Path) -> None:
    for text in (
        CBOX,
        "100.0 200.0 180.0 180.0 0.91 150.0 160.0\n1000.0 50.0 180.0 200.0 0.12 140.0 150.0\n",
    ):
        found = read_picks(write(tmp_path / "mic_001.cbox", text), "mic_001.mrc", SHAPE)
        assert found.format == "cbox" and found.total == 2
        np.testing.assert_array_equal(found.x, [190.0, 1090.0])
        np.testing.assert_array_equal(found.y, [290.0, 150.0])
        np.testing.assert_allclose(found.score, [0.91, 0.12], rtol=1e-6)  # type: ignore[arg-type]
        assert found.box == 190.0  # median of 180 and 200


def cs_table(n: int, shape: tuple[int, int] = SHAPE) -> np.ndarray:
    dtype = [
        ("uid", "<u8"),
        ("location/micrograph_path", "S120"),
        ("location/micrograph_shape", "<u4", (2,)),
        ("location/center_x_frac", "<f4"),
        ("location/center_y_frac", "<f4"),
        ("pick_stats/ncc_score", "<f4"),
    ]
    table = np.zeros(n, dtype)
    table["uid"] = np.arange(n)
    table["location/micrograph_shape"] = shape
    return table


def test_cryosparc_locations_are_fractions_of_the_micrograph(tmp_path: Path) -> None:
    table = cs_table(4)
    prefix = b"J3/motioncorrected/012345678901234567890_"
    table["location/micrograph_path"] = [
        prefix + b"mic_001_fractions_patch_aligned_doseweighted.mrc",
        prefix + b"mic_002_fractions_patch_aligned_doseweighted.mrc",
        prefix + b"mic_001_fractions_patch_aligned_doseweighted.mrc",
        prefix + b"mic_001_fractions_patch_aligned_doseweighted.mrc",
    ]
    table["location/center_x_frac"] = [0.25, 0.9, 0.5, 1.0]
    table["location/center_y_frac"] = [0.5, 0.9, 0.1, 0.0]
    table["pick_stats/ncc_score"] = [0.4, 0.9, np.nan, 0.6]
    with open(tmp_path / "picked.cs", "wb") as f:
        np.save(f, table)
    found = read_picks(tmp_path / "picked.cs", "mic_001.mrc", SHAPE)
    assert found.format == "cs" and found.many and found.total == 3 and found.notes == ()
    np.testing.assert_allclose(found.x, [1000.0, 2000.0, 4000.0])
    np.testing.assert_allclose(found.y, [1500.0, 300.0, 0.0])
    np.testing.assert_allclose(found.score, [0.4, 0.0, 0.6], rtol=1e-6)  # type: ignore[arg-type]

    # shown at another size than the one the file was made for: scaled, and said
    half = read_picks(tmp_path / "picked.cs", "mic_001.mrc", (1500, 2000))
    np.testing.assert_allclose(half.x, [500.0, 1000.0, 2000.0])
    assert half.notes == (
        "the file was made for a micrograph of 4000 x 3000 pixels: the positions are scaled to this image",
    )
    other = read_picks(tmp_path / "picked.cs", "mic_009.mrc", SHAPE)
    assert other.total == 0 and "none of the 2 micrographs" in other.notes[0]


def test_what_is_not_a_coordinate_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormatError):
        read_picks(write(tmp_path / "notes.txt", "1 2 3 4\n"), "a.mrc", SHAPE)
    cases = {
        "nothing.star": "data_\n\nloop_\n_rlnMicrographName #1\nmic.mrc\n",
        "short.star": AUTOPICK + " 12.0\n",
        "words.star": AUTOPICK.replace("1204.000000", "many"),
        "inf.star": AUTOPICK.replace("688.500000", "inf"),
        "three.box": "1 2 3\n",
        "words.box": "a b c d\n",
        "empty.cbox": "data_cryolo\n\nloop_\n_Width #1\n10\n",
    }
    for name, text in cases.items():
        with pytest.raises(CorruptFileError):
            read_picks(write(tmp_path / name, text), "a.mrc", SHAPE)
    (tmp_path / "binary.star").write_bytes(b"loop_\n_rlnCoordinateX\n_rlnCoordinateY\n" + bytes(200_000))
    with pytest.raises(CorruptFileError, match="line is too long"):
        read_picks(tmp_path / "binary.star", "a.mrc", SHAPE)
    with pytest.raises(CorruptFileError):
        read_picks(tmp_path / "gone.box", "a.mrc", SHAPE)

    np.save(tmp_path / "plain.npy", np.zeros(3))
    (tmp_path / "plain.npy").rename(tmp_path / "plain.cs")
    with pytest.raises(CorruptFileError, match="no particle locations"):
        read_picks(tmp_path / "plain.cs", "a.mrc", SHAPE)
    with open(tmp_path / "pickled.cs", "wb") as f:  # a pickle is never loaded
        np.save(f, np.array([{"x": 1}], dtype=object), allow_pickle=True)
    with pytest.raises(CorruptFileError, match="not a cryoSPARC table"):
        read_picks(tmp_path / "pickled.cs", "a.mrc", SHAPE)
    (tmp_path / "junk.cs").write_bytes(b"not numpy")
    with pytest.raises(CorruptFileError):
        read_picks(tmp_path / "junk.cs", "a.mrc", SHAPE)


def test_the_answer_is_bounded_and_says_what_it_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(picks_module, "MAX_POINTS", 5)
    lines = "".join(f"{10 * n} {20 * n} 100 100\n" for n in range(12))
    found = read_picks(write(tmp_path / "many.box", lines), "a.mrc", SHAPE)
    assert found.x.size == 5 and found.total == 12
    assert found.notes == ("12 particles in the file for this image: the first 5 are shown",)
    # particles outside the image: the file is probably about something else
    off = read_picks(
        write(tmp_path / "off.box", "5000 100 10 10\n-50 100 10 10\n100 100 10 10\n"), "a.mrc", SHAPE
    )
    assert off.notes == (
        "2 of 3 particles lie outside the image: the file may be about another image, or another binning",
    )
    monkeypatch.setattr(picks_module, "_MAX_BOX_BYTES", 10)
    with pytest.raises(InvalidRequestError, match="too large"):
        read_picks(tmp_path / "many.box", "a.mrc", SHAPE)


# ── through the API ───────────────────────────────────────────────────────────

PICKS = "/api/cryo/picks"


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory):
    t = build_tree(tmp_path_factory.mktemp("picks"))
    write(t.base / "cryo/movie_autopick.star", AUTOPICK)
    write(t.base / "cryo/movie.box", "10 20 8 8\n30 40 8 8\n")
    write(t.base / "public/particles.star", PARTICLES.replace("mic_002", "movie"))
    write(t.base / "outside/secret.box", "1 2 3 4\n")
    return t


def test_picks_of_an_image_through_the_api(tree) -> None:
    with make_client(tree) as client:
        r = client.get(PICKS, params={"path": "cryo/movie.mrc", "picks": "cryo/movie.box"})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "format": "box",
            "count": 2,
            "total": 2,
            "x": [14.0, 34.0],
            "y": [24.0, 44.0],
            "score": None,
            "box": 8.0,
            "many": False,
            "notes": [],
            "picks": "cryo/movie.box",
            "image": {"x": 64, "y": 48},
        }
        assert str(tree.base) not in r.text
        again = client.get(
            PICKS,
            params={"path": "cryo/movie.mrc", "picks": "cryo/movie.box"},
            headers={"If-None-Match": r.headers["etag"]},
        )
        assert again.status_code == 304
        star = client.get(
            PICKS, params={"path": "cryo/movie.mrc", "picks": "cryo/movie_autopick.star"}
        ).json()
        assert star["format"] == "star" and star["count"] == 3 and star["score"] == [0.731, 1.25, -0.1]
        assert "3 of 3 particles lie outside the image" in star["notes"][0]  # written for a 4k image
        assert star["picks"] == "cryo/movie_autopick.star"
        # a file of a whole data set, in another root: the particles of this image only
        whole = client.get(PICKS, params={"path": "cryo/movie.mrc", "picks": "public/particles.star"}).json()
        assert whole["many"] is True and whole["x"] == [2500.0, 2600.0]
        # the same image through other reader options is another answer for a cache
        assert (
            client.get(PICKS, params={"path": "cryo/movie.mrc", "picks": "cryo/movie.box"}).headers["etag"]
            != client.get(
                PICKS, params={"path": "cryo/movie.mrc", "picks": "cryo/movie_autopick.star"}
            ).headers["etag"]
        )


@pytest.mark.parametrize(
    ("params", "who", "status", "code"),
    [
        ({"path": "cryo/movie.mrc"}, ALICE, 400, "invalid_request"),  # no coordinate file
        ({"picks": "cryo/movie.box"}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/movie.mrc", "picks": "cryo/movie.box", "flip": "1"}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/movie.mrc", "picks": "cryo/session/notes.txt"}, ALICE, 415, "unsupported_format"),
        ({"path": "cryo/movie.mrc", "picks": "cryo/corrupt.mrc"}, ALICE, 415, "unsupported_format"),
        ({"path": "cryo/movie.mrc", "picks": "cryo/session"}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/movie.mrc", "picks": "cryo/missing.box"}, ALICE, 404, "not_found"),
        ({"path": "cryo/movie.mrc", "picks": "../outside/secret.box"}, ALICE, 403, "access_denied"),
        ({"path": "cryo/movie.mrc", "picks": "/etc/passwd"}, ALICE, 403, "access_denied"),
        ({"path": "cryo/movie.mrc", "picks": "light/rgb.png"}, ALICE, 403, "access_denied"),  # not her root
        ({"path": "cryo/movie.mrc", "picks": "public/particles.star"}, BOB, 403, "access_denied"),  # not his
        ({"path": "cryo/session/notes.txt", "picks": "cryo/movie.box"}, ALICE, 415, "unsupported_format"),
        ({"path": "cryo/movie.mrc", "picks": "cryo/movie.box"}, {}, 401, "authentication_required"),
    ],
)
def test_bad_picks_requests(tree, params, who, status, code) -> None:
    with make_client(tree, auth_mode="proxy", trust_network=True) as client:
        r = client.get(PICKS, params=params, headers=who)
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == code and str(tree.base) not in r.text
