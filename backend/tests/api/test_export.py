"""Export to Fiji: the files are read back with tifffile, as ImageJ would read them."""

from __future__ import annotations

import io

import mrcfile
import numpy as np
import pytest
import tifffile

from fixtures.api import ALICE, BOB, Tree, build_tree, make_client
from imagebio.service import export as export_module
from imagebio.service.export import export_file_name

EXPORT = "/api/datasets/export"


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Tree:
    t = build_tree(tmp_path_factory.mktemp("export"))
    rng = np.random.default_rng(41)
    volume = rng.integers(-500, 500, (4, 20, 30), dtype=np.int16)
    with mrcfile.new(t.base / "public/volume.mrc") as m:  # 4 sections, 2.0 Å in x and y, 3.0 Å between them
        m.set_data(volume)
        m.voxel_size = (2.0, 2.0, 3.0)
    tifffile.imwrite(t.base / "public/int32.tif", rng.integers(-70000, 70000, (12, 18)).astype(np.int32))
    tifffile.imwrite(
        t.base / "public/microns.tif",
        rng.integers(0, 255, (10, 14), dtype=np.uint8),
        resolution=(1 / 0.325, 1 / 0.325),
        metadata={"unit": "um"},
        imagej=True,
    )
    with mrcfile.new(t.base / "public/weird name (1) <b>.mrc") as m:
        m.set_data(np.ones((4, 4), np.float32))
    t.volume = volume  # type: ignore[attr-defined]
    return t


@pytest.fixture(scope="module")
def client(tree: Tree):
    with make_client(tree) as c:
        yield c


def read(response) -> tifffile.TiffFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/tiff"
    return tifffile.TiffFile(io.BytesIO(response.content))


def pixels_per_unit(tif: tifffile.TiffFile) -> float:
    numerator, denominator = tif.pages[0].tags["XResolution"].value
    return numerator / denominator


def test_the_plane_on_screen_is_exported_with_its_values_and_its_pixel_size(client, tree: Tree) -> None:
    r = client.get(EXPORT, params={"path": "cryo/movie.mrc", "t": 3})
    with read(r) as tif:
        assert tif.is_imagej
        np.testing.assert_array_equal(tif.asarray(), tree.movie[3])  # float32, as stored
        assert tif.imagej_metadata["unit"] == "nm"  # 1.25 Å per pixel: 0.125 nm
        assert pixels_per_unit(tif) == pytest.approx(8.0, rel=1e-6)
    assert r.headers["content-disposition"] == 'attachment; filename="movie_t4.tif"'
    assert r.headers["cache-control"] == "private, no-store"

    # after the processing asked for: here the mean of frames 2 to 4
    mean = client.get(EXPORT, params={"path": "cryo/movie.mrc", "t": 1, "t_end": 3})
    with read(mean) as tif:
        expected = tree.movie[1:4].astype(np.float64).mean(axis=0).astype(np.float32)
        np.testing.assert_array_equal(tif.asarray(), expected)
    # reduced views are for the screen: the export is the full plane whatever the reduction
    reduced = client.get(EXPORT, params={"path": "cryo/movie.mrc", "t": 3, "fbin": "true"})
    with read(reduced) as tif:
        np.testing.assert_array_equal(tif.asarray(), tree.movie[3])


def test_sample_types_and_units_are_those_imagej_reads(client, tree: Tree) -> None:
    with read(client.get(EXPORT, params={"path": "public/gray16.tif"})) as tif:
        assert tif.asarray().dtype == np.uint16
        np.testing.assert_array_equal(tif.asarray(), tree.gray16)
        # no pixel size known: no unit, and one pixel per pixel
        assert "unit" not in tif.imagej_metadata and pixels_per_unit(tif) == 1.0
    # a pixel size typed by the user for a file that has none
    with read(client.get(EXPORT, params={"path": "public/gray16.tif", "apix": 2.5})) as tif:
        assert tif.imagej_metadata["unit"] == "nm" and pixels_per_unit(tif) == pytest.approx(4.0, rel=1e-6)
    # 32-bit integers are not an ImageJ type: floats, which hold these values exactly
    with read(client.get(EXPORT, params={"path": "public/int32.tif"})) as tif:
        data = tif.asarray()
        assert data.dtype == np.float32
        np.testing.assert_array_equal(
            data, tifffile.imread(tree.base / "public/int32.tif").astype(np.float32)
        )
    # light microscopy: micrometres stay micrometres
    with read(client.get(EXPORT, params={"path": "public/microns.tif"})) as tif:
        assert tif.imagej_metadata["unit"] == "um" and pixels_per_unit(tif) == pytest.approx(
            1 / 0.325, rel=1e-5
        )
    # colour pictures in colour
    with read(client.get(EXPORT, params={"path": "light/rgb.png"})) as tif:
        assert tif.pages[0].photometric.name == "RGB"
        np.testing.assert_array_equal(tif.asarray(), tree.rgb)


def test_a_whole_stack_is_exported_as_a_hyperstack(client, tree: Tree) -> None:
    r = client.get(EXPORT, params={"path": "light/hyper.tif", "scope": "stack"})
    with read(r) as tif:
        meta = tif.imagej_metadata
        assert (meta["frames"], meta["slices"], meta["channels"]) == (2, 3, 2)
        assert tif.series[0].axes == "TZCYX"
        np.testing.assert_array_equal(tif.asarray(), tree.hyper)
    assert r.headers["content-disposition"] == 'attachment; filename="hyper.tif"'
    one = client.get(EXPORT, params={"path": "light/hyper.tif", "t": 1, "z": 2, "c": 1})
    assert one.headers["content-disposition"] == 'attachment; filename="hyper_t2_z3_c2.tif"'
    with read(one) as tif:
        np.testing.assert_array_equal(tif.asarray(), tree.hyper[1, 2, 1])

    volume = tree.volume  # type: ignore[attr-defined]
    with read(client.get(EXPORT, params={"path": "public/volume.mrc", "scope": "stack"})) as tif:
        assert tif.asarray().dtype == np.int16
        np.testing.assert_array_equal(tif.asarray(), volume)
        assert tif.imagej_metadata["slices"] == 4
        assert tif.imagej_metadata["unit"] == "nm" and tif.imagej_metadata["spacing"] == pytest.approx(0.3)
        assert pixels_per_unit(tif) == pytest.approx(5.0, rel=1e-6)  # 2 Å: 0.2 nm


def test_what_cannot_be_exported_is_refused(client, tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_module, "MAX_EXPORT_BYTES", 5000)
    big = client.get(EXPORT, params={"path": "light/hyper.tif", "scope": "stack"})
    assert big.status_code == 400 and "too large to be exported" in big.json()["error"]["message"]
    assert client.get(EXPORT, params={"path": "light/hyper.tif"}).status_code == 200  # one plane is fine
    colour = client.get(EXPORT, params={"path": "light/rgb.png", "scope": "stack"})
    assert colour.status_code == 400 and "plane by plane" in colour.json()["error"]["message"]
    for params in (
        {"path": "cryo/movie.mrc", "t": 5},  # five frames: 0 to 4
        {"path": "cryo/movie.mrc", "scope": "everything"},
        {"path": "cryo/movie.mrc", "bin": 2},
        {"path": "cryo"},
    ):
        assert client.get(EXPORT, params=params).status_code == 400, params
    assert client.get(EXPORT, params={"path": "cryo/session/notes.txt"}).status_code == 415
    assert client.get(EXPORT, params={"path": "cryo/corrupt.mrc"}).status_code in (415, 422)


def test_the_name_of_the_download_is_made_safe() -> None:
    assert export_file_name("cryo/session 1/FoilHole_1_Data_2.mrc", "_t3") == "FoilHole_1_Data_2_t3.tif"
    assert export_file_name("weird name (1) <b>.mrc") == "weird_name_1_b.tif"
    assert (
        export_file_name('a"\r\nSet-Cookie: x=1.tif') == "a_Set-Cookie_x_1.tif"
    )  # nothing a header could carry
    assert export_file_name(".hidden") == "hidden.tif" and export_file_name("...") == "image.tif"
    assert export_file_name("é" * 300 + ".mrc") == "image.tif"
    assert len(export_file_name("a" * 500 + ".mrc")) == 124


def test_export_is_authorised_like_any_reading(tree: Tree) -> None:
    with make_client(tree, auth_mode="proxy", trust_network=True) as proxied:
        ok = proxied.get(EXPORT, params={"path": "public/weird name (1) <b>.mrc"}, headers=ALICE)
        assert ok.status_code == 200
        assert ok.headers["content-disposition"] == 'attachment; filename="weird_name_1_b.tif"'
        for params, who, status in (
            ({"path": "cryo/movie.mrc"}, BOB, 403),  # not his root
            ({"path": "../outside/secret.mrc"}, ALICE, 403),
            ({"path": "cryo/movie.mrc"}, {}, 401),
            ({"path": "cryo/movie.mrc", "gain": "light/rgb.png"}, ALICE, 403),  # a gain she may not read
        ):
            r = proxied.get(EXPORT, params=params, headers=who)
            assert r.status_code == status and str(tree.base) not in r.text
