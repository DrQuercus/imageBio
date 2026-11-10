import io
import os
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import (  # noqa: E402
    CryoFile, State, apply_contrast, auto_contrast, lowpass_filter,
    make_demo_frame, make_histogram, parse_mrc, read_mrc_frame,
)

DTYPES = {0: np.int8, 1: np.int16, 2: np.float32, 6: np.uint16}


def make_mrc(nx=80, ny=60, nz=8, mode=2, big_endian=False):
    dt = DTYPES[mode]
    data = (np.random.randn(nz, ny, nx) * 20 + 100).astype(dt)
    e = '>' if big_endian else '<'
    buf = bytearray(1024)
    struct.pack_into(e + '3i', buf, 0, nx, ny, nz)
    struct.pack_into(e + 'i', buf, 12, mode)
    struct.pack_into(e + '3i', buf, 28, nx, ny, nz)
    struct.pack_into(e + '3f', buf, 40, nx * 1.06, ny * 1.06, nz * 1.06)
    buf[212:216] = bytes([0x11, 0x11, 0, 0]) if big_endian else bytes([0x44, 0x41, 0, 0])
    payload = data.astype(np.dtype(dt).newbyteorder(e)).tobytes()
    return io.BytesIO(bytes(buf) + payload), data.astype(np.float32)


@pytest.mark.parametrize("mode", [0, 1, 2, 6])
def test_mrc_modes_little_endian(mode):
    f, gt = make_mrc(mode=mode)
    h = parse_mrc(f)
    assert (h['nx'], h['ny'], h['nframes']) == (80, 60, 8)
    assert h['apix'] == pytest.approx(1.06, rel=1e-3)
    for i in (0, 3, 7):
        assert np.array_equal(read_mrc_frame(f, h, i), gt[i])


@pytest.mark.parametrize("mode", [1, 2, 6])
def test_mrc_big_endian(mode):
    f, gt = make_mrc(mode=mode, big_endian=True)
    h = parse_mrc(f)
    assert h['endian'] == 'big'
    assert np.array_equal(read_mrc_frame(f, h, 2), gt[2])


def test_mrc_single_frame():
    f, _ = make_mrc(nz=1)
    h = parse_mrc(f)
    assert h['nframes'] == 1
    assert read_mrc_frame(f, h, 0).shape == (60, 80)


def test_lowpass_properties():
    img = np.random.randn(256, 256).astype(np.float32)
    lp20 = lowpass_filter(img, 20, 1.06)
    lp5 = lowpass_filter(img, 5, 1.06)
    assert lp20.shape == img.shape and lp20.dtype == np.float32
    assert lp20.std() < lp5.std() < img.std()      # 20 Å filters more than 5 Å
    assert np.allclose(lowpass_filter(img, 0, 1.06), img)   # 0 = off
    # DC component is preserved by a Gaussian lowpass
    assert lp20.mean() == pytest.approx(img.mean(), abs=1e-4)


def test_contrast_and_gamma():
    img = (np.random.randn(64, 64) * 50 + 100).astype(np.float32)
    vmin, vmax = auto_contrast(img)
    u8 = apply_contrast(img, vmin, vmax)
    assert u8.dtype == np.uint8
    assert apply_contrast(img, vmin, vmax, gamma=2.0).mean() > u8.mean()
    assert apply_contrast(img, vmin, vmax, invert=True).mean() == pytest.approx(255 - u8.mean(), abs=1.5)


def test_histogram_shape():
    c, e = make_histogram(np.random.randn(64, 64).astype(np.float32))
    assert len(c) == 256 and len(e) == 257


def _write_tmp(suffix, writer):
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        path = tmp.name
    writer(path)
    return path


def test_cryofile_mrc_on_disk():
    f, _ = make_mrc(nx=64, ny=48, nz=5)
    path = _write_tmp('.mrc', lambda p: Path(p).write_bytes(f.read()))
    try:
        cf = CryoFile(path)
        assert (cf.nframes, cf.nx, cf.ny) == (5, 64, 48)
        assert cf.get_frame(0).shape == (48, 64)
        assert cf.get_average(0, 5).shape == (48, 64)
        cf.close()
    finally:
        os.unlink(path)


def test_tiff_16bit_and_stack():
    tifffile = pytest.importorskip("tifffile")
    one = (np.random.randn(64, 64) * 1000 + 30000).astype(np.uint16)
    stack = (np.random.randn(10, 64, 64) * 1000 + 30000).astype(np.uint16)
    p1 = _write_tmp('.tif', lambda p: tifffile.imwrite(p, one))
    p2 = _write_tmp('.tif', lambda p: tifffile.imwrite(p, stack))
    try:
        cf1, cf2 = CryoFile(p1), CryoFile(p2)
        assert cf1.nframes == 1 and cf2.nframes == 10
        assert np.array_equal(cf1.get_frame(0), one.astype(np.float32))
        assert np.array_equal(cf2.get_frame(5), stack[5].astype(np.float32))
    finally:
        os.unlink(p1)
        os.unlink(p2)


def test_tiff_rgb_converted_to_gray():
    tifffile = pytest.importorskip("tifffile")
    rgb = np.random.randint(0, 255, (40, 56, 3), dtype=np.uint8)
    p = _write_tmp('.tif', lambda path: tifffile.imwrite(path, rgb, photometric='rgb'))
    try:
        cf = CryoFile(p)
        assert (cf.nx, cf.ny, cf.nframes) == (56, 40, 1)
        assert cf.get_frame(0).shape == (40, 56)
    finally:
        os.unlink(p)


def test_jpeg():
    from PIL import Image
    rgb = np.random.randint(50, 200, (64, 48, 3), dtype=np.uint8)
    p = _write_tmp('.jpg', lambda path: Image.fromarray(rgb).save(path, 'JPEG', quality=90))
    try:
        cf = CryoFile(p)
        assert (cf.nx, cf.ny, cf.nframes) == (48, 64, 1)
    finally:
        os.unlink(p)


def test_demo_frame():
    d = make_demo_frame(20)
    assert len(d['image']) > 1000
    assert 'dmin' in d['stats'] and 'dmax' in d['stats']
    d2 = make_demo_frame(0, vmin=d['stats']['vmin'], vmax=d['stats']['vmax'], gamma=1.5)
    assert len(d2['image']) > 1000


def test_state_browse(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.mrc").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignored")
    cwd, entries = State().browse(str(tmp_path))
    names = {e['name'] for e in entries}
    assert {'..', 'sub', 'a.mrc'} <= names
    assert 'notes.txt' not in names
