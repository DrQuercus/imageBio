# CryoViewer Live 🔬

Real-time cryo-EM data collection monitor. Watch your micrographs appear live during acquisition with stats, thumbnails, and a full preview with LP filter and ruler.

## Features

- **Live directory monitoring** — Recursive glob patterns (e.g. `/srv/glacios/session/movies/**/*.mrc`)
- **Real-time stats** — Image count, acquisition rate (img/h), time since last image with color-coded beacon
- **Thumbnail grid** — Lazy-loaded, resizable tiles with newest-first sorting
- **Full preview** — Pan, zoom, Gaussian low-pass filter, brightness/contrast, invert, ruler tool
- **WebSocket-based** — Instant updates, auto-reconnect

## Quick Start

```bash
# Install & build
npm ci
npm run build

# Run (data roots are mandatory: only patterns inside them can be watched)
CVL_ROOTS=/srv/glacios node server.js
# → http://localhost:4200  (listens on 127.0.0.1 by default)

# Tests
npm test
```

## Deploy with systemd

```bash
sudo cp cryoviewer-live.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cryoviewer-live
```

## Usage

1. Open the app in your browser
2. Enter a glob pattern for your MRC files:
   - `/srv/glacios/collecte1/movies/**/*.mrc`
   - `/data/session_20250404/frames/*.mrc`
   - `/srv/glacios/collecte1/movies/*/data/*.mrc`
3. Click **▶ Watch** — the app will scan and monitor the path
4. New images appear automatically with stats updating every 2 seconds
5. Click any thumbnail to open the full preview with LP filter and ruler

## Status Beacon

| Color  | Meaning              |
|--------|----------------------|
| 🟢 Green  | Last image < 30s ago — collection active |
| 🟠 Orange | Last image < 10min ago — possible pause |
| 🔴 Red    | Last image > 10min ago — collection may have stopped |

## Preview Shortcuts

| Key | Action |
|-----|--------|
| ← → | Navigate images |
| Scroll | Zoom |
| Drag | Pan |
| R | Toggle ruler |
| I | Invert |
| Esc | Close |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `4200` | HTTP port |
| `HOST` | `127.0.0.1` | Bind address. Listening on anything other than loopback requires auth (below). |
| `CVL_ROOTS` | *(empty)* | Allowed data roots, separated by `:`. Watch patterns and served files must resolve (realpath, symlinks included) inside one of them. Empty means watching is disabled. |
| `CVL_AUTH` / `CVL_AUTH_FILE` | *(none)* | `user:password` for HTTP Basic auth, given directly or in a file (preferred, `chmod 600`). |
| `CVL_ALLOWED_HOSTS` | loopback names when `HOST` is loopback | Optional comma-separated allow-list of `Host` headers. |
| `CVL_INSECURE_NO_AUTH` | — | `1` allows a network bind without auth. Not recommended. |

The file watcher polls every 3 s (`server.js`).

## Security

Since the M0 hotfix (see `docs/audit/AUDIT.md` at the repository root):

- No shell command is ever run. The old `df` fallback allowed remote command execution.
- Watch patterns must be absolute, contain no `..` and stay inside `CVL_ROOTS`.
- `/api/thumbnail` and `/api/image-data` only serve files that belong to the current watch and resolve inside `CVL_ROOTS`. A symlink pointing outside the roots is neither listed nor served.
- The server listens on `127.0.0.1` by default, and refuses to start on a network address without authentication.
- WebSocket handshakes are checked for authentication, same-origin and (on loopback) the `Host` header.
- Invalid, unsupported (big-endian, float16) or incomplete MRC files return HTTP 422 instead of garbage.
- The thumbnail cache is keyed on file size and mtime.

There are two ways to expose the service to the lab network:

1. **Recommended: reverse proxy with TLS.** Keep `HOST=127.0.0.1` and put a proxy in front, for example Caddy (`caddy hash-password` produces the hash):

   ```
   cryoviewer.lab.example {
       tls internal
       basic_auth {
           alice <bcrypt-hash>
       }
       reverse_proxy 127.0.0.1:4200
   }
   ```

2. **Quick: built-in Basic auth.** Set `HOST=0.0.0.0` and `CVL_AUTH_FILE=/etc/cryoviewer-live/auth`. Without TLS, the password travels in clear text on the network.

Known limitation: the watch is still global to the server. Any authenticated user can replace or stop it. This is fixed by design in the new backend (milestone M3).

## Development

```bash
# Run backend + Vite dev server with hot reload
npm run dev
# → Frontend: http://localhost:5173 (proxies API to :4200)
```

## Architecture

```
cryoviewer-live/
├── server.js          # Express + WebSocket + chokidar file watcher
├── security.js        # data roots, auth, Host/Origin checks
├── test/              # npm test (node:test)
├── src/
│   ├── App.jsx        # Main app with WS connection management
│   ├── components/
│   │   ├── StatsBar.jsx      # Live stats + status beacon
│   │   ├── ImageGrid.jsx     # Thumbnail grid with lazy loading
│   │   └── PreviewModal.jsx  # Full preview: LP filter, ruler, pan/zoom
│   └── index.css      # Dark theme styles
├── vite.config.js     # Vite build + dev proxy
└── dist/              # Built frontend (served by Express)
```
