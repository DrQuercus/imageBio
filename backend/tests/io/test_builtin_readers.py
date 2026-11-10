"""The default registry: every built-in reader loads and files go to the right one."""

from __future__ import annotations

from pathlib import Path

import mrcfile
import numpy as np
import tifffile
from PIL import Image

from imagebio.io.registry import default_registry, open_dataset, reset_default_registry


def test_all_builtin_readers_are_registered():
    reset_default_registry()
    names = [r.name for r in default_registry().readers]
    assert names == ["mrc", "eer", "tiff", "raster"]
    exts = default_registry().extensions
    assert {".mrc", ".mrcs", ".map", ".eer", ".tif", ".tiff", ".png", ".jpg"} <= exts


def test_files_are_routed_by_content_even_when_misnamed(tmp_path: Path):
    data = (np.arange(48, dtype=np.float32) * 1.5).reshape(6, 8)
    with mrcfile.new(tmp_path / "a.mrc") as m:
        m.set_data(data)
    tifffile.imwrite(tmp_path / "a.tif", data)
    Image.fromarray(data.astype(np.uint8)).save(tmp_path / "a.png")
    for name, reader in (("a.mrc", "mrc"), ("a.tif", "tiff"), ("a.png", "raster")):
        with open_dataset(tmp_path / name) as ds:
            assert ds.info.reader == reader
            assert ds.read_plane().shape == (6, 8)
    # a TIFF named .mrc and a PNG named .tif are still opened by the reader of their content
    (tmp_path / "tiff_inside.mrc").write_bytes((tmp_path / "a.tif").read_bytes())
    (tmp_path / "png_inside.tif").write_bytes((tmp_path / "a.png").read_bytes())
    with open_dataset(tmp_path / "tiff_inside.mrc") as ds:
        assert ds.info.reader == "tiff"
        np.testing.assert_array_equal(ds.read_plane(), data)
    with open_dataset(tmp_path / "png_inside.tif") as ds:
        assert ds.info.reader == "raster"


def test_eer_named_tiff_goes_to_the_eer_reader(tmp_path: Path):
    from fixtures.eer import write_eer

    f = write_eer(tmp_path / "movie.eer", n_fractions=6)
    renamed = tmp_path / "movie.tiff"
    renamed.write_bytes(f.path.read_bytes())
    with open_dataset(renamed) as ds:
        assert ds.info.reader == "eer" and ds.info.metadata["n_fractions"] == 6
