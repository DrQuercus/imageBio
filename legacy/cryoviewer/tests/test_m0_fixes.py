"""Regression tests for the M0 fixes (docs/audit/AUDIT.md, CV-*)."""
import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
from eer_fixture import write_eer  # noqa: E402

mrcfile = pytest.importorskip("mrcfile")


def _strict_json(raw):
    def bad(c):
        raise ValueError(f"non-JSON constant {c}")
    return json.loads(raw, parse_constant=bad)


def write_mrc(path, data, voxel=1.25):
    with mrcfile.new(str(path), overwrite=True) as m:
        m.set_data(np.asarray(data, dtype=np.float32))
        if voxel:
            m.voxel_size = voxel
    return path


@pytest.fixture
def srv(monkeypatch):
    servers = []

    def start(roots=None, auth=None, allowed_hosts=server.LOOPBACK_NAMES, max_upload=4 * 1024**2,
              trusted_hosts=None):
        st = server.State(roots=roots)
        monkeypatch.setattr(server, 'ST', st)
        monkeypatch.setattr(server.Handler, 'auth', auth)
        monkeypatch.setattr(server.Handler, 'allowed_hosts', allowed_hosts)
        monkeypatch.setattr(server.Handler, 'trusted_hosts', trusted_hosts)
        monkeypatch.setattr(server.Handler, 'max_upload', max_upload)
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1], st

    yield start
    for h in servers:
        h.shutdown()
        h.server_close()


def request(port, path, method='GET', headers=None, body=None, timeout=10):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data
    finally:
        c.close()


def get_json(port, path, **kw):
    status, headers, data = request(port, path, **kw)
    return status, _strict_json(data)


def q(p):
    from urllib.parse import quote
    return quote(str(p), safe='')


# ── CV-1: a second client is not blocked by an idle keep-alive connection ────
def test_idle_keepalive_does_not_block_other_clients(srv):
    port, _ = srv()
    idle = socket.create_connection(('127.0.0.1', port))
    try:
        idle.sendall(b'GET /api/list HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        assert b'200' in idle.recv(4096).split(b'\r\n')[0]
        # the idle connection stays open; another client must still be served
        status, body = get_json(port, '/api/list', timeout=3)
        assert status == 200 and body == {'list': []}
    finally:
        idle.close()


# ── DNS rebinding: Host allow-list on loopback ──────────────────────────────
def test_foreign_host_header_refused(srv):
    port, _ = srv()
    assert request(port, '/api/list', headers={'Host': 'evil.example'})[0] == 403
    assert request(port, '/', headers={'Host': f'evil.example:{port}'})[0] == 403
    assert request(port, '/api/list', headers={'Host': f'localhost:{port}'})[0] == 200


def test_basic_auth(srv):
    port, _ = srv(auth='alice:s3cret')
    status, headers, _ = request(port, '/api/list')
    assert status == 401 and headers['WWW-Authenticate'].startswith('Basic')
    bad = 'Basic ' + base64.b64encode(b'alice:nope').decode()
    good = 'Basic ' + base64.b64encode(b'alice:s3cret').decode()
    assert request(port, '/api/list', headers={'Authorization': bad})[0] == 401
    assert request(port, '/api/list', headers={'Authorization': good})[0] == 200


# ── CV-10: data roots, no tracebacks ────────────────────────────────────────
def test_roots_confine_browse_and_load(srv, tmp_path):
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    (root / 'sub').mkdir(parents=True); outside.mkdir()
    write_mrc(outside / 'secret.mrc', np.ones((8, 8)))
    os.symlink(outside / 'secret.mrc', root / 'sub' / 'escape.mrc')
    os.symlink(outside, root / 'escape_dir')
    port, _ = srv(roots=[str(root)])

    status, body = get_json(port, '/api/browse?dir=/etc')
    assert status == 403 and 'trace' not in body
    assert get_json(port, f'/api/browse?dir={q(root / "escape_dir")}')[0] == 403
    assert get_json(port, f'/api/browse?dir={q(root / ".." / "outside")}')[0] == 403
    for p in (outside / 'secret.mrc', root / 'sub' / 'escape.mrc', '/etc/passwd'):
        assert get_json(port, f'/api/load?path={q(p)}')[0] == 403
        assert get_json(port, f'/api/frame?path={q(p)}')[0] == 403

    status, body = get_json(port, f'/api/browse?dir={q(root)}')
    names = {e['name'] for e in body['entries']}
    assert 'escape_dir' not in names and '..' not in names      # cannot leave the root
    names = {e['name'] for e in get_json(port, f'/api/browse?dir={q(root / "sub")}')[1]['entries']}
    assert 'escape.mrc' not in names
    status, body = get_json(port, '/api/browse?dir=~')            # "home" = first root
    assert status == 200 and body['cwd'] == str(root)


def test_errors_have_no_traceback(srv, tmp_path):
    (tmp_path / 'bad.mrc').write_bytes(b'not an mrc file')
    port, _ = srv()
    for path in (tmp_path / 'bad.mrc', tmp_path / 'missing.mrc'):
        status, body = get_json(port, f'/api/load?path={q(path)}')
        assert status in (400, 404) and 'error' in body and 'trace' not in body


# ── CV-3 / CV-4: symlinks ───────────────────────────────────────────────────
def test_symlinked_file_loads_and_displays(srv, tmp_path):
    (tmp_path / 'raw').mkdir(); (tmp_path / 'Movies').mkdir()
    write_mrc(tmp_path / 'raw' / 'm.mrc', np.random.rand(3, 32, 40))
    link = tmp_path / 'Movies' / 'm.mrc'
    os.symlink(tmp_path / 'raw' / 'm.mrc', link)                  # RELION-style link
    os.symlink(tmp_path / 'nowhere.mrc', tmp_path / 'Movies' / 'broken.mrc')
    port, _ = srv(roots=[str(tmp_path)])

    status, body = get_json(port, f'/api/browse?dir={q(tmp_path / "Movies")}')
    assert status == 200                                          # broken link does not break listing
    assert [e['name'] for e in body['entries'] if e['kind'] == 'file'] == ['m.mrc']
    status, body = get_json(port, f'/api/load?path={q(link)}')
    assert status == 200 and body['key'] == str(link)
    status, body = get_json(port, f'/api/frame?path={q(link)}&idx=1')
    assert status == 200 and len(body['image']) > 100


# ── CV-17 / CV-19: NaN/Inf and hot pixels ───────────────────────────────────
def test_nan_inf_image_displays_with_valid_json(srv, tmp_path):
    d = np.random.rand(2, 32, 32).astype(np.float32)
    d[0, 3, 3] = np.nan; d[0, 4, 4] = np.inf; d[1, :, :5] = -np.inf
    write_mrc(tmp_path / 'nan.mrc', d)
    port, _ = srv()
    status, body = get_json(port, f'/api/load?path={q(tmp_path / "nan.mrc")}')
    assert status == 200
    for idx in (0, 1):
        status, body = get_json(port, f'/api/frame?path={q(tmp_path / "nan.mrc")}&idx={idx}&lp=10')
        assert status == 200, body
        assert all(np.isfinite(v) for v in body['stats'].values())
    status, body = get_json(port, f'/api/frame?path={q(tmp_path / "nan.mrc")}&auto=0&vmin=nan&vmax=inf')
    assert status == 200


def test_hot_pixel_does_not_squash_display_range(srv, tmp_path):
    d = np.random.normal(100, 5, (1, 128, 128)).astype(np.float32)
    d[0, 10, 10] = 1e6
    write_mrc(tmp_path / 'hot.mrc', d)
    port, _ = srv()
    get_json(port, f'/api/load?path={q(tmp_path / "hot.mrc")}')
    status, body = get_json(port, f'/api/frame?path={q(tmp_path / "hot.mrc")}')
    st = body['stats']
    assert st['max'] == pytest.approx(1e6)
    assert st['dmax'] < 200                      # slider / histogram range is robust
    edges = body['histogram']['edges']
    assert edges[-1] < 200


# ── CV-7: frame average over a range ────────────────────────────────────────
def test_average_over_range(tmp_path):
    d = np.stack([np.full((8, 8), k, np.float32) for k in range(6)])
    cf = server.CryoFile(write_mrc(tmp_path / 'r.mrc', d))
    assert np.allclose(cf.get_average(2, 5), 3.0)             # frames 3..5 (1-based)
    assert np.allclose(cf.get_average(0, 6), 2.5)
    cf.close()


def test_ui_uses_avg_range_and_no_innerhtml_with_names():
    ui = server.UI
    assert 'onAvgRange()' in ui and 'S.avgS = a - 1' in ui
    assert 'innerHTML' not in ui                   # names are inserted with textContent
    assert 'fonts.googleapis.com' not in ui        # works on offline networks


# ── CV-2: real EER decoding ─────────────────────────────────────────────────
def test_eer_is_decoded_for_real(tmp_path):
    pytest.importorskip("imagecodecs")
    truth = write_eer(tmp_path / 'movie.eer', nfractions=120, shape=(64, 64))
    cf = server.CryoFile(tmp_path / 'movie.eer')
    info = cf.info()
    assert info['is_eer'] and info['eer_fractions'] == 120
    g = info['eer_group']
    assert g == 3 and cf.nframes == 40                         # at most 50 frames of grouped fractions
    assert info['apix_known'] is False                         # EER has no specimen pixel size
    for idx in (0, 7, 39):
        assert np.array_equal(cf.get_frame(idx), truth[idx * g:(idx + 1) * g].sum(0))
    assert np.allclose(cf.get_average(0, cf.nframes), truth.sum(0) / cf.nframes)
    cf.close()


def test_eer_through_http(srv, tmp_path):
    pytest.importorskip("imagecodecs")
    write_eer(tmp_path / 'movie.eer', nfractions=10, shape=(32, 32))
    port, _ = srv()
    status, body = get_json(port, f'/api/load?path={q(tmp_path / "movie.eer")}')
    assert status == 200 and body['info']['nframes'] == 10
    status, body = get_json(port, f'/api/frame?path={q(tmp_path / "movie.eer")}&avg=1&avg_s=0&avg_e=10')
    assert status == 200


# ── CV-15 partial: pixel size provenance ────────────────────────────────────
def test_unknown_pixel_size_is_flagged(tmp_path):
    cf = server.CryoFile(write_mrc(tmp_path / 'nov.mrc', np.ones((1, 8, 8)), voxel=None))
    assert cf.info()['apix_known'] is False
    cf2 = server.CryoFile(write_mrc(tmp_path / 'v.mrc', np.ones((1, 8, 8)), voxel=1.5))
    assert cf2.info()['apix_known'] is True and cf2.info()['apix'] == pytest.approx(1.5)
    cf.close(); cf2.close()


# ── CV-6: bounded memory ────────────────────────────────────────────────────
def test_frame_cache_bounded_in_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(server.CryoFile, 'MAX_CACHE_BYTES', 3 * 64 * 64 * 4)
    cf = server.CryoFile(write_mrc(tmp_path / 'big.mrc', np.random.rand(10, 64, 64)))
    for i in range(10):
        cf.get_frame(i)
    assert cf._cbytes <= 3 * 64 * 64 * 4 and len(cf._cache) == 3
    cf.close()


def test_open_files_are_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(server.State, 'MAX_OPEN', 2)
    st = server.State()
    paths = [write_mrc(tmp_path / f'{i}.mrc', np.ones((1, 4, 4))) for i in range(4)]
    for p in paths:
        st.open(str(p))
    assert len(st.files) == 2
    _, cf = st.get(str(paths[0]))                              # reopened transparently
    assert cf.nframes == 1


# ── CV-25 + CSRF: uploads ───────────────────────────────────────────────────
def test_upload_checks(srv, tmp_path):
    write_mrc(tmp_path / 'u.mrc', np.random.rand(2, 16, 16))
    data = (tmp_path / 'u.mrc').read_bytes()
    port, st = srv(max_upload=len(data) + 10)
    hdr = {'X-Filename': 'u.mrc', 'Content-Type': 'application/octet-stream'}

    status, _, _ = request(port, '/api/upload', 'POST', {**hdr, 'Origin': 'http://evil.example'}, data)
    assert status == 403
    status, _, _ = request(port, '/api/elsewhere', 'POST', hdr, data)
    assert status == 404
    status, _, _ = request(port, '/api/upload', 'POST', hdr, data + b'x' * 100)
    assert status == 413

    status, _, body = request(port, '/api/upload', 'POST', {**hdr, 'Origin': f'http://127.0.0.1:{port}'}, data)
    assert status == 200, body
    key = _strict_json(body)['key']
    first_tmp = st.uploads[key][0]
    assert Path(first_tmp).parent == Path(st.upload_dir)
    status, _, _ = request(port, '/api/upload', 'POST', hdr, data)            # same name again
    assert status == 200
    assert not Path(first_tmp).exists()                                       # old temp file removed
    status, body = get_json(port, f'/api/frame?path={q(key)}')
    assert status == 200


# ── startup policy ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('args, message', [
    (['--bind', '0.0.0.0'], 'without --root'),
    (['--bind', '0.0.0.0', '--root', '/tmp'], 'without authentication'),
])
def test_network_bind_requires_root_and_auth(args, message):
    r = subprocess.run([sys.executable, str(Path(server.__file__)), '--no-browser', *args],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode != 0 and message in r.stderr
