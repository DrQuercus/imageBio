"""Command line: ``imagebio serve`` and ``imagebio info``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from imagebio import __version__
from imagebio.core.config import ConfigError, Settings
from imagebio.core.errors import ImageBioError


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
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    if args.config:
        settings = Settings.load(args.config)
        if args.port is not None:
            from dataclasses import replace

            settings = replace(settings, port=args.port)
        return settings
    for directory in args.local:
        if not Path(directory).expanduser().is_dir():
            raise ConfigError(f"not a directory: {directory}")
    return Settings.local(args.local, **({"port": args.port} if args.port is not None else {}))


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
        if args.command == "info":
            from imagebio.io.registry import open_dataset

            with open_dataset(args.file) as dataset:
                print(json.dumps(dataset.info.to_json(), indent=2, ensure_ascii=False))
            return 0
        settings = _settings(args)
    except (ConfigError, ImageBioError) as e:
        print(f"imagebio: {e}", file=sys.stderr)
        return 2

    import uvicorn

    from imagebio.api.app import create_app

    app = create_app(settings)
    roots = ", ".join(r.name for r in settings.roots) or "none"
    print(
        f"imageBio {__version__} on http://{settings.host}:{settings.port}  "
        f"(auth: {settings.auth_mode}; roots: {roots})"
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", server_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
