from __future__ import annotations

import io
import threading
from pathlib import Path

import imagecodecs
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from imagebio.core.errors import CorruptFileError, InvalidRequestError, NotFoundError
from imagebio.core.model import CalibrationSource, StackKind
from imagebio.io.readers import raster as raster_module
from imagebio.io.readers.raster import READER
from imagebio.io.registry import SNIFF_BYTES

RNG = np.random.default_rng(7)


def rand(shape: tuple[int, ...], dtype: str = "uint8") -> np.ndarray:
    info = np.iinfo(dtype)
    return RNG.integers(info.min, info.max, shape, dtype=dtype, endpoint=True)


def save(path: Path, array: np.ndarray, mode: str | None = None, **kw: object) -> Path:
    Image.fromarray(array, mode).save(path, **kw)
    return path


def planes(path: Path) -> np.ndarray:
    """All planes of a file as (T, C, Y, X)."""
    with READER.open(path) as ds:
        i = ds.info
        return np.stack([[ds.read_plane(t, c) for c in range(i.size_c)] for t in range(i.size_t)])


# ── modes ────────────────────────────────────────────────────────────────────


def test_gray_8_and_16_bit_png_are_exact(tmp_path: Path):
    g8, g16 = rand((20, 31)), rand((20, 31), "uint16")
    p8 = save(tmp_path / "g8.png", g8)
    p16 = tmp_path / "g16.png"
    p16.write_bytes(imagecodecs.png_encode(g16))
    with READER.open(p8) as ds:
        assert ds.info.dtype == np.uint8 and ds.info.shape == (1, 1, 1, 20, 31)
        assert ds.info.stack_kind is StackKind.IMAGE and not ds.info.rgb
        np.testing.assert_array_equal(ds.read_plane(), g8)
    with READER.open(p16) as ds:
        assert ds.info.dtype == np.uint16
        np.testing.assert_array_equal(ds.read_plane(), g16)


def test_rgb_and_rgba_png(tmp_path: Path):
    rgb, rgba = rand((12, 9, 3)), rand((12, 9, 4))
    for name, data in (("rgb.png", rgb), ("rgba.png", rgba)):
        path = save(tmp_path / name, data)
        with READER.open(path) as ds:
            assert ds.info.rgb and ds.info.size_c == data.shape[2]
            assert [c.name for c in ds.info.channels] == ["red", "green", "blue", "alpha"][: data.shape[2]]
        np.testing.assert_array_equal(planes(path)[0], np.moveaxis(data, -1, 0))


def test_16_bit_colour_png_keeps_its_16_bits(tmp_path: Path):
    """Pillow would reduce these to 8 bits per channel."""
    for name, shape in (("rgb16.png", (10, 14, 3)), ("rgba16.png", (10, 14, 4)), ("la16.png", (10, 14, 2))):
        data = rand(shape, "uint16")
        path = tmp_path / name
        path.write_bytes(imagecodecs.png_encode(data))
        with READER.open(path) as ds:
            assert ds.info.dtype == np.uint16 and ds.info.size_c == shape[2]
            assert ds.info.rgb is (shape[2] != 2)
        np.testing.assert_array_equal(planes(path)[0], np.moveaxis(data, -1, 0))


def test_gray_alpha_is_two_channels_not_rgb(tmp_path: Path):
    la = rand((8, 8, 2))
    path = save(tmp_path / "la.png", la, "LA")
    with READER.open(path) as ds:
        assert ds.info.size_c == 2 and not ds.info.rgb
        assert [c.name for c in ds.info.channels] == ["gray", "alpha"]
    np.testing.assert_array_equal(planes(path)[0], np.moveaxis(la, -1, 0))


def test_palette_is_expanded_not_shown_as_indices(tmp_path: Path):
    indices = rand((16, 16)) % 4
    palette = np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [10, 20, 30]], np.uint8)
    img = Image.fromarray(indices, "P")
    img.putpalette(palette.tobytes())
    img.save(tmp_path / "p.png")
    np.testing.assert_array_equal(planes(tmp_path / "p.png")[0], np.moveaxis(palette[indices], -1, 0))
    with READER.open(tmp_path / "p.png") as ds:
        assert ds.info.rgb and ds.info.size_c == 3 and ds.info.metadata["mode"] == "P"
    # with transparency: RGBA
    img.save(tmp_path / "pt.png", transparency=1)
    out = planes(tmp_path / "pt.png")[0]
    assert out.shape[0] == 4
    np.testing.assert_array_equal(out[:3], np.moveaxis(palette[indices], -1, 0))
    np.testing.assert_array_equal(out[3], np.where(indices == 1, 0, 255))


def test_bilevel_is_0_and_255(tmp_path: Path):
    bits = rand((9, 13)) > 127
    Image.fromarray(bits).save(tmp_path / "b.png")
    with READER.open(tmp_path / "b.png") as ds:
        assert ds.info.dtype == np.uint8 and ds.info.metadata["mode"] == "1"
        np.testing.assert_array_equal(ds.read_plane(), bits.astype(np.uint8) * 255)


def test_jpeg_gray_rgb_and_cmyk(tmp_path: Path):
    gray = save(tmp_path / "g.jpg", rand((24, 32)), quality=95)
    rgb = save(tmp_path / "c.jpeg", rand((24, 32, 3)), quality=95)
    np.testing.assert_array_equal(planes(gray)[0, 0], np.asarray(Image.open(gray)))
    np.testing.assert_array_equal(planes(rgb)[0], np.moveaxis(np.asarray(Image.open(rgb)), -1, 0))
    Image.fromarray(rand((8, 8, 4)), "CMYK").save(tmp_path / "cmyk.jpg")
    with READER.open(tmp_path / "cmyk.jpg") as ds:
        assert ds.info.rgb and ds.info.size_c == 3
        assert any("CMYK converted to RGB" in w for w in ds.info.warnings)
        expected = np.asarray(Image.open(tmp_path / "cmyk.jpg").convert("RGB"))
        np.testing.assert_array_equal(ds.read_plane(c=2), expected[..., 2])


def test_bmp_and_webp(tmp_path: Path):
    data = rand((10, 10, 3))
    np.testing.assert_array_equal(planes(save(tmp_path / "a.bmp", data))[0], np.moveaxis(data, -1, 0))
    path = save(tmp_path / "a.webp", data, lossless=True)
    np.testing.assert_array_equal(planes(path)[0], np.moveaxis(data, -1, 0))


def test_animations_expose_frames_on_t(tmp_path: Path):
    frames = [rand((12, 12, 4)) | np.array([0, 0, 0, 255], np.uint8) for _ in range(3)]
    images = [Image.fromarray(f) for f in frames]
    images[0].save(
        tmp_path / "a.png", save_all=True, append_images=images[1:], duration=40, default_image=False
    )
    with READER.open(tmp_path / "a.png") as ds:
        i = ds.info
        assert (i.size_t, i.size_c, i.rgb, i.stack_kind) == (3, 4, True, StackKind.STACK)
        assert i.calibration.t is not None and i.calibration.t.value == pytest.approx(0.04)
        for t in (2, 0, 1, 1):  # any order
            for c in range(4):
                np.testing.assert_array_equal(ds.read_plane(t, c), frames[t][..., c])
    gif = [Image.fromarray(np.full((6, 6), v, np.uint8)) for v in (0, 120, 250)]
    gif[0].save(tmp_path / "a.gif", save_all=True, append_images=gif[1:])
    with READER.open(tmp_path / "a.gif") as ds:
        assert ds.info.size_t == 3
        assert [int(ds.read_plane(t, 0)[0, 0]) for t in range(3)] == [0, 120, 250]
        assert int(ds.read_plane(1, 3)[0, 0]) == 255


# ── pixel size ───────────────────────────────────────────────────────────────


def test_pixel_size_from_resolution(tmp_path: Path):
    data = rand((8, 8))
    with READER.open(save(tmp_path / "cal.png", data, dpi=(25400, 12700))) as ds:  # 1 µm and 2 µm pixels
        cal = ds.info.calibration
        assert cal.known and cal.x.source is CalibrationSource.METADATA
        assert cal.x.to_angstrom() == pytest.approx(1e4, rel=1e-3)
        assert cal.y.to_angstrom() == pytest.approx(2e4, rel=1e-3)
    with READER.open(save(tmp_path / "cal.jpg", data, dpi=(300, 300))) as ds:
        assert ds.info.calibration.x.value == pytest.approx(25400 / 300)


@pytest.mark.parametrize("dpi", [72, 96])
def test_screen_dpi_is_a_placeholder_not_a_pixel_size(tmp_path: Path, dpi: int):
    for name in ("p.png", "p.jpg", "p.bmp"):
        with READER.open(save(tmp_path / name, rand((8, 8)), dpi=(dpi, dpi))) as ds:
            assert not ds.info.calibration.known
            assert any("placeholder" in w for w in ds.info.warnings), name


def test_no_resolution_means_unknown(tmp_path: Path):
    with READER.open(save(tmp_path / "n.png", rand((8, 8)))) as ds:
        assert not ds.info.calibration.known and ds.info.calibration.x.unit == "px"
        assert any("pixel size unknown" in w for w in ds.info.warnings)


def test_exif_orientation_is_reported_not_applied(tmp_path: Path):
    data = rand((10, 20))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotated 90° for display
    Image.fromarray(data).save(tmp_path / "o.png", exif=exif)
    with READER.open(tmp_path / "o.png") as ds:
        assert ds.info.shape[-2:] == (10, 20) and ds.info.metadata["exif_orientation"] == 6
        assert any("EXIF orientation 6 is not applied" in w for w in ds.info.warnings)
        np.testing.assert_array_equal(ds.read_plane(), data)


# ── robustness ───────────────────────────────────────────────────────────────


def test_sniff():
    buf = io.BytesIO()
    Image.fromarray(rand((4, 4))).save(buf, "PNG")
    assert READER.sniff(Path("x"), buf.getvalue()[:SNIFF_BYTES])
    assert READER.sniff(Path("x"), b"\xff\xd8\xff\xe0" + bytes(20))
    assert READER.sniff(Path("x"), b"GIF89a" + bytes(20))
    assert READER.sniff(Path("x"), b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    for junk in (b"", b"II*\x00", b"RIFF\x00\x00\x00\x00WAVE", bytes(64), b"BM"):
        assert not READER.sniff(Path("x"), junk)


def test_truncated_and_garbage_files(tmp_path: Path):
    good = save(tmp_path / "good.png", rand((64, 64, 3))).read_bytes()
    cut = tmp_path / "cut.png"
    cut.write_bytes(good[: len(good) // 2])
    with READER.open(cut) as ds, pytest.raises(CorruptFileError, match=r"cut\.png: cannot decode"):
        ds.read_plane()
    (tmp_path / "junk.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))
    (tmp_path / "text.jpg").write_bytes(b"hello world")
    (tmp_path / "empty.gif").write_bytes(b"")
    for name in ("junk.png", "text.jpg", "empty.gif"):
        with pytest.raises(CorruptFileError) as e:
            READER.open(tmp_path / name).read_plane()
        assert str(tmp_path) not in str(e.value) and name in str(e.value)
    with pytest.raises(NotFoundError) as nf:
        READER.open(tmp_path / "missing.png")
    assert str(tmp_path) not in str(nf.value)
    with pytest.raises(InvalidRequestError):
        READER.open(tmp_path / "good.png", {"group": 2})


@settings(max_examples=60, deadline=None, derandomize=True)
@given(data=st.data())
def test_damaged_files_only_raise_imagebio_errors(
    tmp_path_factory: pytest.TempPathFactory, data: st.DataObject
):
    buf = io.BytesIO()
    fmt = data.draw(st.sampled_from(["PNG", "JPEG", "GIF", "BMP"]))
    Image.fromarray((np.arange(256, dtype=np.uint8) * 7).reshape(16, 16)).save(buf, fmt)  # deterministic
    raw = bytearray(buf.getvalue())
    for _ in range(data.draw(st.integers(1, 6))):
        raw[data.draw(st.integers(0, len(raw) - 1))] = data.draw(st.integers(0, 255))
    raw = raw[: data.draw(st.integers(1, len(raw)))]
    path = tmp_path_factory.mktemp("fuzz") / f"f.{fmt.lower()}"
    path.write_bytes(bytes(raw))
    try:
        with READER.open(path) as ds:
            for c in range(ds.info.size_c):
                assert ds.read_plane(0, c).shape == (ds.info.size_y, ds.info.size_x)
    except CorruptFileError:
        pass


def test_decompression_bomb_is_refused_before_decoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = save(tmp_path / "big.png", np.zeros((200, 300), np.uint8))
    monkeypatch.setattr(raster_module, "MAX_DECODED_BYTES", 200 * 300 - 1)
    with pytest.raises(CorruptFileError, match=r"big\.png: image too large to decode \(300 × 200 pixels"):
        READER.open(path)
    monkeypatch.setattr(raster_module, "MAX_DECODED_BYTES", 200 * 300)
    READER.open(path).close()
    # a header announcing a gigantic image is refused on the header alone
    monkeypatch.undo()
    raw = bytearray(path.read_bytes())
    raw[16:24] = (60000).to_bytes(4, "big") * 2
    import zlib

    raw[29:33] = zlib.crc32(raw[12:29]).to_bytes(4, "big")
    (tmp_path / "bomb.png").write_bytes(bytes(raw))
    with pytest.raises(CorruptFileError, match="too large"):
        READER.open(tmp_path / "bomb.png")


def test_channels_are_decoded_once_and_reads_are_thread_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = rand((64, 64, 3))
    path = save(tmp_path / "c.png", data)
    ds = READER.open(path)
    calls = 0
    original = raster_module.RasterDataset._decode

    def counting(self: raster_module.RasterDataset, t: int) -> np.ndarray:
        nonlocal calls
        calls += 1
        return original(self, t)

    monkeypatch.setattr(raster_module.RasterDataset, "_decode", counting)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for c in (0, 1, 2) * 5:
                np.testing.assert_array_equal(ds.read_plane(0, c), data[..., c])
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and calls == 1
    ds.close()
    ds.close()
    with pytest.raises(ValueError, match="closed"):
        ds.read_plane()
