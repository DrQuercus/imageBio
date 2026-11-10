'use strict';
// Security helpers for CryoViewer Live (M0 hotfix, see docs/audit/AUDIT.md LIVE-2 and
// the command injection in the old updateDiskInfo()).
//
// - data roots (CVL_ROOTS): every watch pattern and every served file must resolve
//   (realpath) inside one of them;
// - optional HTTP Basic auth (CVL_AUTH / CVL_AUTH_FILE) + session cookie for the WebSocket;
// - Host header allow-list when bound to loopback (DNS rebinding) and Origin check on the
//   WebSocket handshake (cross-site WebSocket hijacking).

const path = require('path');
const fs = require('fs');
const fsp = fs.promises;
const crypto = require('crypto');

const MAX_PATTERN_LENGTH = 4096;
// Characters that may start a glob construct for chokidar/picomatch (incl. extglobs).
const GLOB_START = /[*?[\]{}()!+@|]/;
const SESSION_COOKIE = 'cvl_session';

// ─── Bind address ───────────────────────────────────────────────────────────
function isLoopbackHost(host) {
  return host === 'localhost' || host === '::1' || /^127\.\d+\.\d+\.\d+$/.test(host);
}

// ─── Data roots ─────────────────────────────────────────────────────────────
function isInside(child, parent) {
  const rel = path.relative(parent, child);
  return rel === '' || (rel !== '..' && !rel.startsWith('..' + path.sep) && !path.isAbsolute(rel));
}

function parseRoots(value, log = console) {
  const roots = [];
  for (const raw of String(value || '').split(path.delimiter)) {
    const p = raw.trim();
    if (!p) continue;
    if (!path.isAbsolute(p)) {
      log.warn(`[Security] Ignoring CVL_ROOTS entry (not absolute): ${p}`);
      continue;
    }
    let real;
    try {
      real = fs.realpathSync(p);
      if (!fs.statSync(real).isDirectory()) throw new Error('not a directory');
    } catch (e) {
      log.warn(`[Security] Ignoring CVL_ROOTS entry ${p}: ${e.message}`);
      continue;
    }
    roots.push({ configured: path.resolve(p), real });
  }
  return roots;
}

function lexicallyInsideRoots(p, roots) {
  const abs = path.resolve(p);
  return roots.some(r => isInside(abs, r.configured) || isInside(abs, r.real));
}

// Real path of `p` if it exists and resolves inside a root, else null.
async function realpathInsideRoots(p, roots) {
  if (typeof p !== 'string' || !p || p.includes('\0')) return null;
  let real;
  try { real = await fsp.realpath(p); } catch { return null; }
  return roots.some(r => isInside(real, r.real)) ? real : null;
}

async function nearestExistingAncestor(dir) {
  let cur = path.resolve(dir);
  for (;;) {
    try {
      await fsp.access(cur);
      return cur;
    } catch {
      const parent = path.dirname(cur);
      if (parent === cur) return null;
      cur = parent;
    }
  }
}

// Validate a client-supplied watch pattern.
// Returns { ok: true, pattern, baseDir, volumeDir } or { ok: false, error }.
async function validateWatchPattern(pattern, roots) {
  if (!roots.length) {
    return { ok: false, error: 'No data roots configured on the server (CVL_ROOTS): watching is disabled.' };
  }
  if (typeof pattern !== 'string') return { ok: false, error: 'Invalid pattern.' };
  pattern = pattern.trim();
  if (!pattern) return { ok: false, error: 'Empty pattern.' };
  if (pattern.length > MAX_PATTERN_LENGTH) return { ok: false, error: 'Pattern too long.' };
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(pattern)) return { ok: false, error: 'Pattern contains control characters.' };
  if (pattern.includes('\\')) return { ok: false, error: 'Backslashes are not allowed in the pattern.' };
  if (!path.isAbsolute(pattern)) return { ok: false, error: 'The pattern must be an absolute path.' };
  // '..' is refused anywhere (also inside braces/extglobs, which could expand outside the root).
  if (pattern.includes('..')) return { ok: false, error: "'..' is not allowed in the pattern." };

  // Directory part before the first glob construct. chokidar only scans below it.
  const g = pattern.search(GLOB_START);
  let baseDir;
  if (g < 0) baseDir = pattern;
  else {
    const prefix = pattern.slice(0, g);
    baseDir = prefix.endsWith('/') ? prefix : path.dirname(prefix);
  }
  baseDir = path.resolve(baseDir);

  if (!lexicallyInsideRoots(baseDir, roots)) {
    return { ok: false, error: `Path is outside the allowed data roots: ${roots.map(r => r.configured).join(', ')}` };
  }
  const existing = await nearestExistingAncestor(baseDir);
  const realExisting = existing && await realpathInsideRoots(existing, roots);
  if (!realExisting) {
    return { ok: false, error: `Path is outside the allowed data roots: ${roots.map(r => r.configured).join(', ')}` };
  }
  return { ok: true, pattern, baseDir, volumeDir: realExisting };
}

// ─── Host header allow-list ─────────────────────────────────────────────────
function parseAllowedHosts(env, bindHost) {
  if (env.CVL_ALLOWED_HOSTS) {
    const set = new Set(env.CVL_ALLOWED_HOSTS.split(',').map(h => h.trim().toLowerCase()).filter(Boolean));
    return set.size ? set : null;
  }
  // Bound to loopback: only accept loopback names (protects against DNS rebinding).
  if (isLoopbackHost(bindHost)) return new Set(['localhost', '127.0.0.1', '[::1]']);
  return null; // network-bound: auth is mandatory, no Host restriction unless configured
}

function hostOnly(hostHeader) {
  const h = String(hostHeader || '').toLowerCase();
  if (h.startsWith('[')) {
    const end = h.indexOf(']');
    return end > 0 ? h.slice(0, end + 1) : h;
  }
  return h.replace(/:\d+$/, '');
}

function hostAllowed(hostHeader, allowedHosts) {
  if (!allowedHosts) return true;
  if (!hostHeader) return false;
  return allowedHosts.has(hostOnly(hostHeader));
}

// ─── Authentication ─────────────────────────────────────────────────────────
function sha256(s) { return crypto.createHash('sha256').update(s, 'utf8').digest(); }

function safeEqual(a, b) {
  const da = sha256(String(a));
  const db = sha256(String(b));
  return crypto.timingSafeEqual(da, db);
}

function loadAuth(env) {
  let spec = env.CVL_AUTH || '';
  if (env.CVL_AUTH_FILE) spec = fs.readFileSync(env.CVL_AUTH_FILE, 'utf8').trim();
  if (!spec) return null;
  const i = spec.indexOf(':');
  if (i <= 0 || i === spec.length - 1) throw new Error('CVL_AUTH / CVL_AUTH_FILE must contain "user:password"');
  const user = spec.slice(0, i);
  const credentials = spec;
  const cookieValue = crypto.createHmac('sha256', sha256(credentials)).update('cvl-session-v1').digest('hex');
  return { user, credentials, cookieValue, realm: 'CryoViewer Live' };
}

function parseCookies(header) {
  const out = {};
  for (const part of String(header || '').split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

function checkBasic(authorization, auth) {
  if (!authorization || !/^Basic\s+/i.test(authorization)) return false;
  let decoded;
  try { decoded = Buffer.from(authorization.replace(/^Basic\s+/i, ''), 'base64').toString('utf8'); }
  catch { return false; }
  return safeEqual(decoded, auth.credentials);
}

function checkCookie(cookieHeader, auth) {
  const v = parseCookies(cookieHeader)[SESSION_COOKIE];
  return Boolean(v) && safeEqual(v, auth.cookieValue);
}

function isAuthenticated(req, auth) {
  if (!auth) return true;
  return checkBasic(req.headers.authorization, auth) || checkCookie(req.headers.cookie, auth);
}

// Express middleware: Host check, then auth (sets the session cookie used by the WebSocket).
function httpGuard({ auth, allowedHosts }) {
  return (req, res, next) => {
    if (!hostAllowed(req.headers.host, allowedHosts)) return res.status(403).type('text').send('Forbidden host');
    if (!auth) return next();
    if (checkBasic(req.headers.authorization, auth)) {
      if (!checkCookie(req.headers.cookie, auth)) {
        res.append('Set-Cookie', `${SESSION_COOKIE}=${auth.cookieValue}; HttpOnly; SameSite=Strict; Path=/`);
      }
      return next();
    }
    if (checkCookie(req.headers.cookie, auth)) return next();
    res.set('WWW-Authenticate', `Basic realm="${auth.realm}", charset="UTF-8"`);
    return res.status(401).type('text').send('Authentication required');
  };
}

// ws verifyClient: Host check, same-origin check, auth.
function wsVerifier({ auth, allowedHosts }) {
  return (info, cb) => {
    const req = info.req;
    if (!hostAllowed(req.headers.host, allowedHosts)) return cb(false, 403, 'Forbidden host');
    if (info.origin) {
      let originHost;
      try { originHost = new URL(info.origin).host.toLowerCase(); } catch { return cb(false, 403, 'Bad origin'); }
      if (originHost !== String(req.headers.host || '').toLowerCase()) return cb(false, 403, 'Cross-origin WebSocket refused');
    }
    if (!isAuthenticated(req, auth)) return cb(false, 401, 'Authentication required');
    return cb(true);
  };
}

module.exports = {
  isLoopbackHost,
  isInside,
  parseRoots,
  realpathInsideRoots,
  validateWatchPattern,
  parseAllowedHosts,
  hostAllowed,
  loadAuth,
  httpGuard,
  wsVerifier,
};
