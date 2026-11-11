"""The gallery of a stack: one small picture per plane, and RELION's table of 2D classes."""

from __future__ import annotations

import io
from pathlib import Path

import mrcfile
import numpy as np
import pytest
from PIL import Image

from fixtures.api import ALICE, BOB, build_tree, make_client
from imagebio.core.errors import CorruptFileError
from imagebio.cryo.classes import model_star_name, read_classes

THUMB = "/api/datasets/thumb"
CLASSES = "/api/cryo/classes"

MODEL = """
# version 30001

data_model_general

_rlnReferenceDimensionality                    2
_rlnNrClasses                                  4

# version 30001

data_model_classes

loop_
_rlnReferenceImage #1
_rlnClassDistribution #2
_rlnAccuracyRotations #3
_rlnEstimatedResolution #4
000001@Class2D/job008/run_it025_classes.mrcs     0.412000     1.500000     7.850000
000002@Class2D/job008/run_it025_classes.mrcs     0.000000   999.000000          inf
000003@Class2D/job008/run_it025_classes.mrcs     0.501000     1.200000     6.400000
000004@Class2D/job008/run_it025_classes.mrcs     0.087000     3.100000    14.200000

# version 30001

data_model_class_1

loop_
_rlnSpectralIndex #1
_rlnResolution #2
0 0.0
"""


def stack(n: int = 6, side: int = 64) -> np.ndarray:
    """Plane k is noise with a bright square whose column tells k."""
    rng = np.random.default_rng(12)
    planes = rng.normal(0, 1, (n, side, side)).astype(np.float32)
    for k in range(n):
        planes[k, 8:24, 8 * k : 8 * k + 16] += 25
    return planes


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory):
    t = build_tree(tmp_path_factory.mktemp("gallery"))
    job = t.base / "cryo/Class2D"
    job.mkdir()
    with mrcfile.new(job / "run_it025_classes.mrcs") as m:
        m.set_data(stack())
        m.set_image_stack()
        m.voxel_size = 2.5
    (job / "run_it025_model.star").write_text(MODEL.lstrip("\n"))
    with mrcfile.new(job / "run_it001_classes.mrcs") as m:  # averages whose model file is missing
        m.set_data(stack(2))
        m.set_image_stack()
    with mrcfile.new(job / "bad_classes.mrcs") as m:
        m.set_data(stack(2))
        m.set_image_stack()
    (job / "bad_model.star").write_text(
        "data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassDistribution #2\nx 0.5\n"
    )
    with mrcfile.new(t.base / "cryo/volume.mrc") as m:  # 6 sections of a volume
        m.set_data(stack())
        m.voxel_size = 2.5
    return t


@pytest.fixture(scope="module")
def client(tree):
    with make_client(tree) as c:
        yield c


def picture(response) -> np.ndarray:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    return np.asarray(Image.open(io.BytesIO(response.content)), np.float64)


def test_each_plane_of_a_stack_has_its_own_picture(client) -> None:
    planes = stack()
    seen = []
    for k in range(6):
        image = picture(
            client.get(THUMB, params={"path": "cryo/Class2D/run_it025_classes.mrcs", "t": k, "size": 64})
        )
        assert image.shape == (64, 64)
        # the bright square is where plane k has it, and nowhere else
        bright = image > 128
        expected = planes[k] > 12
        assert (bright == expected).mean() > 0.97, k
        seen.append(image)
    assert not np.array_equal(seen[0], seen[1])

    # smaller: a mean of blocks, not one pixel out of two
    small = picture(
        client.get(THUMB, params={"path": "cryo/Class2D/run_it025_classes.mrcs", "t": 2, "size": 32})
    )
    assert small.shape == (32, 32)
    reference = planes[2].reshape(32, 2, 32, 2).mean(axis=(1, 3))
    assert np.corrcoef(small.ravel(), reference.ravel())[0, 1] > 0.98
    # the sections of a volume the same way
    section = picture(client.get(THUMB, params={"path": "cryo/volume.mrc", "z": 4, "size": 64}))
    assert ((section > 128) == (planes[4] > 12)).mean() > 0.97


def test_thumbnails_are_cached_by_the_browser_and_tied_to_their_plane(client) -> None:
    params = {"path": "cryo/Class2D/run_it025_classes.mrcs", "t": 1, "size": 64}
    first = client.get(THUMB, params=params)
    assert first.headers["cache-control"] == "private, max-age=300"
    again = client.get(THUMB, params=params, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    other = client.get(THUMB, params={**params, "t": 2})
    assert other.headers["etag"] != first.headers["etag"]
    assert client.get(THUMB, params={**params, "size": 48}).headers["etag"] != first.headers["etag"]


@pytest.mark.parametrize(
    ("params", "who", "status", "code"),
    [
        (
            {"path": "cryo/Class2D/run_it025_classes.mrcs", "t": 6},
            ALICE,
            400,
            "invalid_request",
        ),  # 6 planes: 0..5
        ({"path": "cryo/Class2D/run_it025_classes.mrcs", "z": 1}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/Class2D/run_it025_classes.mrcs", "t": -1}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/Class2D/run_it025_classes.mrcs", "size": 8}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/Class2D/run_it025_classes.mrcs", "size": 4096}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/Class2D/run_it025_classes.mrcs", "c": 0}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/Class2D"}, ALICE, 400, "invalid_request"),
        ({"path": "cryo/Class2D/run_it025_model.star"}, ALICE, 415, "unsupported_format"),
        ({"path": "cryo/missing.mrcs"}, ALICE, 404, "not_found"),
        ({"path": "../outside/secret.mrc"}, ALICE, 403, "access_denied"),
        ({"path": "cryo/Class2D/run_it025_classes.mrcs"}, BOB, 403, "access_denied"),
        ({"path": "cryo/Class2D/run_it025_classes.mrcs"}, {}, 401, "authentication_required"),
    ],
)
def test_bad_thumbnail_and_class_requests(tree, params, who, status, code) -> None:
    with make_client(tree, auth_mode="proxy", trust_network=True) as proxied:
        r = proxied.get(THUMB, params=params, headers=who)
        assert r.status_code == status, r.text
        assert r.json()["error"]["code"] == code and str(tree.base) not in r.text
        if status in (401, 403, 404):  # the same refusals for the class table
            c = proxied.get(CLASSES, params={"path": params["path"]}, headers=who)
            assert c.status_code == status and c.json()["error"]["code"] == code


def test_the_class_table_of_relion(tmp_path: Path) -> None:
    assert model_star_name("run_it025_classes.mrcs") == "run_it025_model.star"
    assert model_star_name("RUN_IT025_CLASSES.MRCS") == "RUN_IT025_model.star"
    assert model_star_name("particles.mrcs") is None and model_star_name("classes.mrc") is None
    model = tmp_path / "run_it025_model.star"
    model.write_text(MODEL.lstrip("\n"))
    assert read_classes(model) == [
        {"index": 0, "distribution": 0.412, "resolution": 7.85},
        {"index": 1, "distribution": 0.0, "resolution": None},  # an empty class has no resolution
        {"index": 2, "distribution": 0.501, "resolution": 6.4},
        {"index": 3, "distribution": 0.087, "resolution": 14.2},
    ]
    for text in (
        "data_model_general\n\n_rlnNrClasses 4\n",  # no class table
        "data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassDistribution #2\nrun_classes.mrcs 0.5\n",
        "data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassDistribution #2\n000000@a.mrcs 0.5\n",
        "data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassDistribution #2\n000001@a.mrcs half\n",
    ):
        model.write_text(text)
        with pytest.raises(CorruptFileError):
            read_classes(model)


def test_the_class_table_through_the_api(client, tree) -> None:
    r = client.get(CLASSES, params={"path": "cryo/Class2D/run_it025_classes.mrcs"})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "cryo/Class2D/run_it025_model.star"
    assert [c["index"] for c in body["classes"]] == [0, 1, 2, 3]
    assert body["classes"][2] == {"index": 2, "distribution": 0.501, "resolution": 6.4}
    assert str(tree.base) not in r.text
    # no model file next to the averages, or a file that is not class averages: nothing to say
    for path in ("cryo/Class2D/run_it001_classes.mrcs", "cryo/movie.mrc"):
        assert client.get(CLASSES, params={"path": path}).json() == {"model": None, "classes": None}
    bad = client.get(CLASSES, params={"path": "cryo/Class2D/bad_classes.mrcs"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "corrupt_file"
    assert client.get(CLASSES, params={"path": "cryo/Class2D"}).status_code == 400
    assert client.get(CLASSES, params={"path": "cryo/movie.mrc", "t": 1}).status_code == 400
