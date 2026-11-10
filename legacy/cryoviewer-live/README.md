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
npm install
npm run build

# Run
node server.js
# → http://localhost:4200
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

- **Port**: Set `PORT` env variable (default: 4200)
- **Polling interval**: File watcher polls every 2s (configurable in `server.js`)

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
