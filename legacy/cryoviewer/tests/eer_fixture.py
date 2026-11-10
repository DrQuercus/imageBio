"""Synthetic EER writer for tests (EER v1: 7-bit run length + 2+2 sub-pixel bits, LSB-first).

Same encoding as docs/research/eer_synthetic_encoder.py (verified bit-exact against
imagecodecs.eer_decode): a minimal little-endian TIFF with one strip per fraction,
compression 65001 and the acquisition XML in tag 65001.
"""
import struct

import numpy as np


def _encode_fraction(positions, subpix, npixels):
    bits = []
    prev = -1
    for p, s in zip(positions, subpix):
        skip = p - prev - 1
        while skip >= 127:
            bits.append((127, 7)); skip -= 127
        bits.append((skip, 7)); bits.append((int(s), 4)); prev = p
    skip = npixels - prev - 1          # the last run must land exactly on npixels
    while skip >= 127:
        bits.append((127, 7)); skip -= 127
    bits.append((skip, 7))
    acc = n = 0
    out = bytearray()
    for v, nb in bits:
        acc |= v << n; n += nb
        while n >= 8:
            out.append(acc & 255); acc >>= 8; n -= 8
    if n:
        out.append(acc & 255)
    if len(out) % 2:
        out.append(0)                  # the decoder requires an even byte count
    return bytes(out)


def write_eer(path, nfractions=12, shape=(64, 64), events=150, seed=0):
    """Write a synthetic EER file; returns the per-fraction ground truth (nfrac, ny, nx)."""
    ny, nx = shape
    rng = np.random.default_rng(seed)
    truth = np.zeros((nfractions, ny, nx), np.int64)
    segs = []
    for f in range(nfractions):
        pos = np.sort(rng.choice(ny * nx, events, replace=False))
        segs.append(_encode_fraction(pos, rng.integers(0, 16, len(pos)), ny * nx))
        truth[f].flat[pos] = 1
    xml = (f'<metadata><item name="numberOfFrames">{nfractions}</item>'
           '<item name="exposureTime" unit="s">4.0</item></metadata>').encode()
    buf = bytearray(b'II*\x00' + b'\x00' * 4)
    ifd_ptr = 4
    xml_off = len(buf); buf += xml
    for i, sg in enumerate(segs):
        if len(buf) % 2: buf.append(0)
        off = len(buf); buf += sg
        if len(buf) % 2: buf.append(0)
        tags = [(256, 4, 1, nx), (257, 4, 1, ny), (258, 3, 1, 8), (259, 3, 1, 65001),
                (262, 3, 1, 1), (273, 4, 1, off), (277, 3, 1, 1), (278, 4, 1, ny),
                (279, 4, 1, len(sg))]
        if i == 0:
            tags.append((65001, 7, len(xml), xml_off))
        ifd = len(buf); struct.pack_into('<I', buf, ifd_ptr, ifd)
        buf += struct.pack('<H', len(tags))
        for code, typ, cnt, val in tags:
            if typ == 3:
                buf += struct.pack('<HHIHH', code, typ, cnt, val, 0)
            else:
                buf += struct.pack('<HHII', code, typ, cnt, val)
        ifd_ptr = len(buf); buf += b'\x00' * 4
    with open(path, 'wb') as fh:
        fh.write(buf)
    return truth
