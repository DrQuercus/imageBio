"""Command line: ``imagebio serve``, ``info``, ``simulate``, ``backup`` and ``restore``."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from imagebio import __version__
from imagebio.api.security import PROXY_SECRET_VARIABLE
from imagebio.core.config import AlertChannel, ConfigError, Settings
from imagebio.core.errors import ImageBioError
from imagebio.live.index import IndexVersionError, hold_state_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imagebio", description="imageBio backend")
    parser.add_argument("--version", action="version", version=f"imagebio {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the HTTP server")
    source = serve.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--config", metavar="FILE", help="TOML configuration file (data roots, proxy authentication)"
    )
    source.add_argument(
        "--local", metavar="DIR", nargs="+",
        help="standalone mode: serve these directories to this computer only, without authentication",
    )  # fmt: skip
    serve.add_argument(
        "--port", type=int, help="port to listen on (default: 8000, or the one of the configuration)"
    )
    serve.add_argument(
        "--ntfy", metavar="URL", help="with --local: an ntfy topic the Live mode may send alerts to"
    )
    serve.add_argument(
        "--webhook", metavar="URL", help="with --local: a webhook the Live mode may send alerts to"
    )

    info = sub.add_parser("info", help="print what a reader sees in a file (debugging)")
    info.add_argument("file", type=Path)

    simulate = sub.add_parser(
        "simulate",
        help="write an EPU-like session into a directory, as a microscope would (to try the Live mode)",
    )
    simulate.add_argument("directory", type=Path, help="where to write; created if missing")
    simulate.add_argument("--rate", type=float, default=600.0, help="exposures per hour (default: 600)")
    simulate.add_argument(
        "--count", type=int, default=0, help="stop after this many exposures (default: never)"
    )
    simulate.add_argument("--size", type=int, default=512, help="side of the images in pixels (default: 512)")
    simulate.add_argument("--seed", type=int, default=0)

    backup = sub.add_parser(
        "backup", help="copy the Live index and the configuration into a new dated directory"
    )
    backup.add_argument("destination", type=Path, help="directory that receives the backups")
    backup.add_argument(
        "--config", metavar="FILE", help="configuration of the server (default: its defaults)"
    )
    backup.add_argument(
        "--keep", type=int, metavar="N", help="then remove the oldest backups there, keeping N"
    )
    restore = sub.add_parser("restore", help="put the Live index of a backup back (server stopped)")
    restore.add_argument("source", type=Path, help="a directory written by 'imagebio backup'")
    restore.add_argument(
        "--config", metavar="FILE", help="configuration of the server (default: its defaults)"
    )
    restore.add_argument(
        "--settings",
        action="store_true",
        help="also put back the settings of the settings page (accounts, roots...): without it, "
        "groups and roles changed since the backup stay as they are",
    )
    restore.add_argument(
        "--force", action="store_true", help="replace the index that is there (it is kept aside)"
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    if args.config:
        if args.ntfy or args.webhook:
            raise ConfigError(
                "--ntfy and --webhook go with --local; with --config, declare [[alerts.channels]]"
            )
        settings = Settings.load(args.config)
        if args.port is not None:
            from dataclasses import replace

            settings = replace(settings, port=args.port)
        return settings
    for directory in args.local:
        if not Path(directory).expanduser().is_dir():
            raise ConfigError(f"not a directory: {directory}")
    channels = [
        AlertChannel(kind, kind, url)  # type: ignore[arg-type]
        for kind, url in (("ntfy", args.ntfy), ("webhook", args.webhook))
        if url
    ]
    overrides: dict[str, object] = {"alert_channels": tuple(channels)}
    if args.port is not None:
        overrides["port"] = args.port
    return Settings.local(args.local, **overrides)


def _simulate(args: argparse.Namespace) -> int:
    """Run the simulated microscope against the real clock until interrupted."""
    import time

    from imagebio.live.simulator import MicroscopeSim, SimSettings

    if not 1 <= args.rate <= 36000 or not 16 <= args.size <= 4096 or args.count < 0:
        print("imagebio: --rate must be 1 to 36000, --size 16 to 4096, --count 0 or more", file=sys.stderr)
        return 2
    sim = MicroscopeSim(
        args.directory, SimSettings(rate_per_hour=args.rate, shape=(args.size, args.size), seed=args.seed)
    )
    print(f"simulated microscope: {args.rate:g} exposures per hour into {args.directory} (Ctrl-C to stop)")
    shown = 0
    try:
        while True:
            sim.step(time.time())
            if args.count and len(sim.exposures) >= args.count:
                sim.stop()
                if not sim.busy:
                    break
            if len(sim.exposures) != shown:
                shown = len(sim.exposures)
                print(f"  exposure {shown}: {sim.exposures[-1]}")
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    print(f"{len(sim.exposures)} exposures started, {len(sim.partial())} file(s) left unfinished")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "simulate":
        return _simulate(args)
    try:
        if args.command in ("backup", "restore"):
            from imagebio.live import backup as backups

            configured = Settings.load(args.config) if args.config else Settings()
            if args.command == "backup":
                made = backups.backup(
                    configured, args.destination, Path(args.config) if args.config else None
                )
                print(f"backup written to {made}")
                if args.keep is not None:
                    removed = backups.prune(args.destination, args.keep)
                    print(f"{len(removed)} older backup(s) removed")
            else:
                manifest = backups.restore(
                    configured, args.source, force=args.force, with_settings=args.settings
                )
                restored = manifest["restored"]
                done = []
                if restored["index"]:
                    done.append(f"the Live index ({manifest['index']['watches']} watch(es))")
                if restored["settings"]:
                    done.append("the settings of the settings page")
                print(f"backup of {manifest['created']}: restored {' and '.join(done)}; start the server")
                if manifest.get("settings") and not restored["settings"]:
                    print("its settings page was not restored (add --settings to put it back too)")
            return 0
        if args.command == "info":
            from imagebio.io.registry import open_dataset

            with open_dataset(args.file) as dataset:
                print(json.dumps(dataset.info.to_json(), indent=2, ensure_ascii=False))
            return 0
        settings = _settings(args)
        from imagebio.api.app import create_app

        app = create_app(settings)
        app.state.live.check()
        # held as long as the server runs: a restore waits for it to stop, settings included
        settings.live_state_dir.mkdir(parents=True, exist_ok=True)
        state_lock = hold_state_dir(settings.live_state_dir)
    except (ConfigError, ImageBioError, IndexVersionError) as e:
        print(f"imagebio: {e}", file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as e:
        print(
            f"imagebio: the Live index cannot be opened ({e}): if another server is not using it, "
            "restore a backup (imagebio restore ... --force)",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    roots = ", ".join(r.name for r in app.state.store.effective.roots) or "none"
    auth: str = settings.auth_mode
    if auth == "proxy":  # whether anybody who reaches the port is believed, or only the proxy
        auth += " with" if os.environ.get(PROXY_SECRET_VARIABLE) else " without"
        auth += " a shared secret"
    print(f"imageBio {__version__} on http://{settings.host}:{settings.port}  (auth: {auth}; roots: {roots})")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", server_header=False)
    if state_lock is not None:
        state_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
