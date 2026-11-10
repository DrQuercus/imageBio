'use strict';
// Regression tests for the M0 security hotfix (docs/audit/AUDIT.md: LIVE-2, command
// injection in updateDiskInfo, WebSocket origin, invalid MRC, stale thumbnail cache).
// Run with: npm test   (node --test, no extra dependency)

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const WebSocket = require('ws');

const security = require('../security');

const SERVER = path.join(__dirname, '..', 'server.js');

// ─── Fixtures ───────────────────────────────────────────────────────────────
function mrcBuffer(nx, ny, values, { truncate = null, nxHeader = nx, nyHeader = ny } = {}) {
  const hdr = Buffer.alloc(1024);
  hdr.writeInt32LE(nxHeader, 0); hdr.writeInt32LE(nyHeader, 4); hdr.writeInt32LE(1, 8);
  hdr.writeInt32LE(2, 12);                                   // mode 2 (float32)
  hdr.writeInt32LE(nxHeader, 28); hdr.writeInt32LE(nyHeader, 32); hdr.writeInt32LE(1, 36);
  hdr.writeFloatLE(nxHeader * 1.5, 40); hdr.writeFloatLE(nyHeader * 1.5, 44); hdr.writeFloatLE(1.5, 48);
  hdr.writeInt32LE(1, 64); hdr.writeInt32LE(2, 68); hdr.writeInt32LE(3, 72);
  hdr.write('MAP ', 208, 'ascii'); hdr[212] = 0x44; hdr[213] = 0x44;
  const data = Buffer.alloc(nx * ny * 4);
  for (let i = 0; i < nx * ny; i++) data.writeFloatLE(values(i), i * 4);
  const full = Buffer.concat([hdr, data]);
  return truncate == null ? full : full.subarray(0, truncate);
}

function makeTree() {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'cvl-test-')));
  const root = path.join(base, 'root');
  const data = path.join(root, 'data');
  const outside = path.join(base, 'outside');
  fs.mkdirSync(data, { recursive: true });
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, 'secret.jpg'), 'SECRET_TOKEN=abc123');
  fs.writeFileSync(path.join(data, 'img.jpg'), Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 0xff, 0xd9]));
  fs.symlinkSync(path.join(outside, 'secret.jpg'), path.join(data, 'link.jpg'));
  fs.writeFileSync(path.join(data, 'good.mrc'), mrcBuffer(64, 64, i => i % 64));
  fs.writeFileSync(path.join(data, 'trunc.mrc'), mrcBuffer(64, 64, () => 1, { truncate: 1024 + 100 }));
  fs.writeFileSync(path.join(data, 'huge.mrc'), mrcBuffer(4, 4, () => 1, { nxHeader: 60000, nyHeader: 60000 }));
  fs.writeFileSync(path.join(root, 'notes.txt'), 'not an image');
  return { base, root, data, outside };
}

// ─── Server harness ─────────────────────────────────────────────────────────
function startServer(env, { expectExit = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SERVER], {
      env: { PATH: process.env.PATH, PORT: '0', ...env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '';
    const onData = (d) => {
      out += d.toString();
      const m = out.match(/listening on http:\/\/\S+:(\d+)/);
      if (m && !expectExit) resolve({ child, port: Number(m[1]), output: () => out });
    };
    child.stdout.on('data', onData);
    child.stderr.on('data', onData);
    child.on('exit', (code) => {
      if (expectExit) resolve({ code, output: out });
      else reject(new Error(`server exited early (code ${code}):\n${out}`));
    });
    setTimeout(() => reject(new Error(`server did not start:\n${out}`)), 10000).unref();
  });
}

function stopServer(srv) {
  return new Promise((resolve) => {
    if (!srv || srv.child.exitCode !== null) return resolve();
    srv.child.removeAllListeners('exit');
    srv.child.on('exit', () => resolve());
    srv.child.kill('SIGKILL');
  });
}

function request(port, urlPath, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: urlPath, headers }, (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    req.on('error', reject);
    req.end();
  });
}

const q = (p) => encodeURIComponent(p);

// WebSocket client that records every message.
function connectWs(port, headers = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}`, { headers });
    const messages = [];
    const waiters = [];
    ws.on('message', (raw) => {
      const msg = JSON.parse(raw.toString());
      messages.push(msg);
      for (const w of waiters.slice()) {
        if (w.pred(msg)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(msg); }
      }
    });
    ws.waitFor = (pred, timeout = 20000) => {
      const found = messages.find(pred);
      if (found) return Promise.resolve(found);
      return new Promise((res, rej) => {
        const w = { pred, resolve: res };
        waiters.push(w);
        setTimeout(() => rej(new Error('timeout waiting for WS message; got: ' +
          JSON.stringify(messages.map(m => m.type)))), timeout).unref();
      });
    };
    ws.messages = messages;
    ws.on('open', () => resolve(ws));
    ws.on('unexpected-response', (req, res) => reject(Object.assign(new Error(`HTTP ${res.statusCode}`), { status: res.statusCode })));
    ws.on('error', (e) => reject(e));
  });
}

async function watch(ws, pattern) {
  ws.messages.length = 0;
  ws.send(JSON.stringify({ type: 'start_watch', pattern }));
  return ws.waitFor(m => m.type === 'watch_error' || m.type === 'watch_started', 5000);
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ─── Unit tests: pattern validation ─────────────────────────────────────────
test('validateWatchPattern confines patterns to the data roots', async () => {
  const t = makeTree();
  const roots = security.parseRoots(t.root, { warn() {} });
  const ok = async (p) => (await security.validateWatchPattern(p, roots)).ok;

  assert.equal(await ok(`${t.root}/**/*.mrc`), true);
  assert.equal(await ok(`${t.data}/*.jpg`), true);
  assert.equal(await ok(`${t.root}/not-yet-created/session/**/*.mrc`), true);
  assert.equal(await ok(t.root), true);

  assert.equal(await ok('/etc/*.conf'), false);
  assert.equal(await ok(`${t.outside}/*.jpg`), false);
  assert.equal(await ok(`${t.root}/../outside/*.jpg`), false);
  assert.equal(await ok(`${t.root}/{data,../outside}/*.jpg`), false);
  assert.equal(await ok(`${path.dirname(t.root)}/{root,outside}/*.jpg`), false);
  assert.equal(await ok(`${t.root}x/*.jpg`), false);           // sibling with a common prefix
  assert.equal(await ok('data/*.jpg'), false);                 // relative
  assert.equal(await ok(`${t.root}/a\nb/*.mrc`), false);       // control character
  assert.equal(await ok(`${t.root}/a\\*.mrc`), false);         // backslash escape
  assert.equal(await ok(42), false);
  assert.equal((await security.validateWatchPattern(`${t.root}/*.mrc`, [])).ok, false);

  // A symlinked directory inside the root that points outside is refused.
  fs.symlinkSync(t.outside, path.join(t.root, 'escape'));
  assert.equal(await ok(`${t.root}/escape/*.jpg`), false);
});

// ─── Integration tests: real server, no auth, loopback ──────────────────────
test('server without auth (loopback)', async (t) => {
  const tree = makeTree();
  const marker1 = path.join(tree.base, 'pwned1');
  const marker2 = path.join(tree.base, 'pwned2');
  const srv = await startServer({ HOST: '127.0.0.1', CVL_ROOTS: tree.root });
  t.after(() => stopServer(srv));
  const ws = await connectWs(srv.port);
  t.after(() => ws.terminate());

  await t.test('command injection through the watch pattern executes nothing', async () => {
    let r = await watch(ws, `/nonexistent_cvl/$(touch ${marker1})/x.mrc`);
    assert.equal(r.type, 'watch_error');
    r = await watch(ws, `/nonexistent_cvl/\`touch ${marker1}\`/x.mrc`);
    assert.equal(r.type, 'watch_error');
    // Inside the root, the (non-existent) path is accepted, but disk info must not shell out.
    r = await watch(ws, `${tree.root}/"; touch ${marker2}; echo "/$(touch ${marker2})/x.mrc`);
    assert.equal(r.type, 'watch_started');
    await sleep(1500);
    assert.equal(fs.existsSync(marker1), false);
    assert.equal(fs.existsSync(marker2), false);
  });

  await t.test('patterns outside the roots are refused', async () => {
    for (const p of ['/etc/*.conf', `${tree.outside}/*.jpg`, `${tree.root}/../outside/*.jpg`, 'relative/*.jpg']) {
      const r = await watch(ws, p);
      assert.equal(r.type, 'watch_error', p);
      assert.match(r.message, /\S/);
    }
  });

  await t.test('watching the root lists images but not symlinks escaping it', async () => {
    const r = await watch(ws, `${tree.root}/**/*`);
    assert.equal(r.type, 'watch_started');
    await ws.waitFor(m => m.type === 'scan_complete');
    await sleep(500);
    const list = JSON.parse((await request(srv.port, '/api/images?count=500')).body.toString());
    const names = list.images.map(i => i.name).sort();
    assert.deepEqual(names, ['good.mrc', 'huge.mrc', 'img.jpg', 'trunc.mrc']);
    const good = list.images.find(i => i.name === 'good.mrc');
    assert.equal(good.nx, 64);
    assert.equal(good.pixelSize, 1.5);
  });

  await t.test('arbitrary file read is refused', async () => {
    for (const ep of ['/api/thumbnail', '/api/image-data']) {
      for (const p of ['/etc/passwd', path.join(tree.outside, 'secret.jpg'), path.join(tree.data, 'link.jpg')]) {
        const res = await request(srv.port, `${ep}?path=${q(p)}`);
        assert.equal(res.status, 403, `${ep} ${p}`);
        assert.ok(!res.body.toString().includes('SECRET_TOKEN'));
      }
      // Inside the root but not part of the watch.
      const res = await request(srv.port, `${ep}?path=${q(path.join(tree.root, 'notes.txt'))}`);
      assert.equal(res.status, 404);
      assert.equal((await request(srv.port, ep)).status, 400);
    }
  });

  await t.test('valid files are served; invalid MRC files give 422', async () => {
    const jpg = await request(srv.port, `/api/image-data?path=${q(path.join(tree.data, 'img.jpg'))}`);
    assert.equal(jpg.status, 200);
    assert.equal(jpg.headers['content-type'], 'image/jpeg');

    const good = await request(srv.port, `/api/image-data?path=${q(path.join(tree.data, 'good.mrc'))}`);
    assert.equal(good.status, 200);
    assert.equal(good.headers['x-nx'], '64');
    assert.equal(good.body.length, 64 * 64 * 4);
    assert.equal(good.body.readFloatLE(5 * 4), 5);

    for (const name of ['trunc.mrc', 'huge.mrc']) {
      for (const ep of ['/api/image-data', '/api/thumbnail']) {
        const res = await request(srv.port, `${ep}?path=${q(path.join(tree.data, name))}`);
        assert.equal(res.status, 422, `${ep} ${name}`);
      }
    }
  });

  await t.test('thumbnail cache follows file changes', async () => {
    const p = path.join(tree.data, 'good.mrc');
    const a = await request(srv.port, `/api/thumbnail?path=${q(p)}&size=32`);
    assert.equal(a.status, 200);
    assert.equal(a.headers['cache-control'], 'private, no-cache');
    const again = await request(srv.port, `/api/thumbnail?path=${q(p)}&size=32`, { 'If-None-Match': a.headers.etag });
    assert.equal(again.status, 304);

    fs.writeFileSync(p, mrcBuffer(64, 64, i => Math.floor(i / 64)));   // vertical -> horizontal gradient
    const later = new Date(Date.now() + 5000);
    fs.utimesSync(p, later, later);
    const b = await request(srv.port, `/api/thumbnail?path=${q(p)}&size=32`);
    assert.equal(b.status, 200);
    assert.notEqual(b.headers.etag, a.headers.etag);
    assert.notDeepEqual(b.body, a.body);
  });

  await t.test('foreign Host header is refused (DNS rebinding)', async () => {
    const res = await request(srv.port, '/api/images', { Host: 'evil.example' });
    assert.equal(res.status, 403);
    await assert.rejects(connectWs(srv.port, { Host: 'evil.example' }), { status: 403 });
    assert.equal((await request(srv.port, '/api/images', { Host: `localhost:${srv.port}` })).status, 200);
  });

  await t.test('cross-origin WebSocket is refused', async () => {
    await assert.rejects(connectWs(srv.port, { Origin: 'http://evil.example' }), { status: 403 });
    const same = await connectWs(srv.port, { Origin: `http://127.0.0.1:${srv.port}` });
    same.terminate();
  });
});

// ─── Integration tests: Basic auth ──────────────────────────────────────────
test('server with Basic auth', async (t) => {
  const tree = makeTree();
  const srv = await startServer({ HOST: '127.0.0.1', CVL_ROOTS: tree.root, CVL_AUTH: 'alice:s3cret:with:colons' });
  t.after(() => stopServer(srv));
  const basic = (cred) => 'Basic ' + Buffer.from(cred).toString('base64');

  const anon = await request(srv.port, '/api/images');
  assert.equal(anon.status, 401);
  assert.match(anon.headers['www-authenticate'], /^Basic /);
  assert.equal((await request(srv.port, '/api/images', { Authorization: basic('alice:wrong') })).status, 401);
  assert.equal((await request(srv.port, '/api/thumbnail?path=/etc/passwd')).status, 401);

  const ok = await request(srv.port, '/api/images', { Authorization: basic('alice:s3cret:with:colons') });
  assert.equal(ok.status, 200);
  const cookie = (ok.headers['set-cookie'] || [])[0];
  assert.match(cookie, /^cvl_session=[0-9a-f]{64}; HttpOnly; SameSite=Strict; Path=\/$/);
  const cookieHeader = cookie.split(';')[0];
  assert.equal((await request(srv.port, '/api/images', { Cookie: cookieHeader })).status, 200);
  assert.equal((await request(srv.port, '/api/images', { Cookie: 'cvl_session=deadbeef' })).status, 401);

  await assert.rejects(connectWs(srv.port), { status: 401 });
  (await connectWs(srv.port, { Cookie: cookieHeader })).terminate();
  (await connectWs(srv.port, { Authorization: basic('alice:s3cret:with:colons') })).terminate();
});

// ─── Startup policy ─────────────────────────────────────────────────────────
test('network bind without auth is refused', async () => {
  const r = await startServer({ HOST: '0.0.0.0' }, { expectExit: true });
  assert.equal(r.code, 1);
  assert.match(r.output, /Refusing to listen on 0\.0\.0\.0 without authentication/);
});

test('network bind with auth or explicit opt-out starts', async (t) => {
  const a = await startServer({ HOST: '0.0.0.0', CVL_AUTH: 'u:p' });
  t.after(() => stopServer(a));
  const b = await startServer({ HOST: '0.0.0.0', CVL_INSECURE_NO_AUTH: '1' });
  t.after(() => stopServer(b));
  assert.ok(a.port > 0 && b.port > 0);
});

test('without CVL_ROOTS, watching is disabled', async (t) => {
  const srv = await startServer({ HOST: '127.0.0.1' });
  t.after(() => stopServer(srv));
  const ws = await connectWs(srv.port);
  t.after(() => ws.terminate());
  const r = await watch(ws, '/tmp/*.mrc');
  assert.equal(r.type, 'watch_error');
  assert.match(r.message, /CVL_ROOTS/);
});
