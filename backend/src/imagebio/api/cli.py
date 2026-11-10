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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
