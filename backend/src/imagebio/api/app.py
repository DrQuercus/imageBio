"""HTTP API (FastAPI).

Stateless: every request names its file by *logical* path and is authorised on its own, before
any cache is consulted. Pixels travel as raw little-endian arrays; contrast and colour maps are
the client's job (GPU), so changing them costs no request.

Endpoints (all ``GET``)::

    /healthz                        liveness, no authentication
    /api/me                         user, groups, authentication mode
    /api/fs/roots                   data roots visible to the user
    /api/fs/list?path=              directory listing (offset, limit, hidden)
    /api/datasets/info?path=        DatasetInfo + file identity
    /api/datasets/plane?path=       raw plane bytes (t, c, z, bin, x, y, w, h)
    /api/datasets/stats?path=       robust statistics and histogram of a plane (t, c, z)

Reader options are query parameters prefixed with ``opt.`` (``opt.group=4`` for an EER movie).
Errors are JSON, ``{"error": {"code": ..., "message": ...}}``, and never contain a server path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import zlib
from collections.abc import Mapping
from typing import Any, Final

import numpy as np
import zstandard
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from imagebio import __version__
from imagebio.api.security import SecurityMiddleware, current_user
from imagebio.core.cache import options_key
from imagebio.core.config import Settings
from imagebio.core.errors import ImageBioError, InvalidRequestError
from imagebio.core.model import FileId
from imagebio.fs.roots import MAX_PATH_LENGTH, ResolvedPath, RootSet, User
from imagebio.io.registry import Registry, default_registry
from imagebio.service.planes import MAX_BIN, PlaneService

__all__ = ["create_app"]

log = logging.getLogger("imagebio.api")

_OPTION_PREFIX: Final = "opt."
_MAX_OPTIONS: Final = 16
_OPTION_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_INT: Final = re.compile(r"-?\d{1,18}")
_FLOAT: Final = re.compile(r"-?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?")
_COMPRESS_PROBE: Final = 256 * 1024
_CACHE_CONTROL: Final = "private, no-cache"


def _error(status: int, code: str, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def _check_query(request: Request, allowed: frozenset[str]) -> dict[str, Any]:
    """Reject unknown or repeated query parameters; return the reader options (``opt.*``)."""
    options: dict[str, Any] = {}
    seen: set[str] = set()
    for key, value in request.query_params.multi_items():
        if key in seen:
            raise InvalidRequestError(f"query parameter {key!r} is given more than once")
        seen.add(key)
        if key.startswith(_OPTION_PREFIX):
            name = key[len(_OPTION_PREFIX) :]
            if not _OPTION_NAME.fullmatch(name) or len(options) >= _MAX_OPTIONS or len(value) > 256:
                raise InvalidRequestError("invalid reader option")
            options[name] = _option_value(value)
        elif key not in allowed:
            raise InvalidRequestError(f"unknown query parameter {key[:64]!r}")
    return options


def _option_value(text: str) -> Any:
    if text in ("true", "false"):
        return text == "true"
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


def _accepts(header: str) -> dict[str, float]:
    """Content codings of an ``Accept-Encoding`` header with their q-values."""
    out: dict[str, float] = {}
    for part in header.split(","):
        name, _, params = part.strip().partition(";")
        q = 1.0
        for param in params.split(";"):
            k, _, v = param.strip().partition("=")
            if k.strip().lower() == "q":
                try:
                    q = float(v)
                except ValueError:
                    q = 0.0
        if name.strip():
            out[name.strip().lower()] = q
    return out


def _encode(body: memoryview, accept_encoding: str) -> tuple[bytes | memoryview, str | None]:
    """Compress ``body`` with zstd or gzip when the client accepts it and it pays off.

    Counting-mode data shrink several times; float noise barely does. A probe on the first
    bytes decides. zstd is fast enough (about 1 GB/s) to be used for any gain above 10 %.
    gzip is some thirty times slower, slower than a local network: it is only used when the
    data shrink by half or more.
    """
    accepted = _accepts(accept_encoding)
    if len(body) < 1024:
        return body, None
    if accepted.get("zstd", 0.0) > 0:
        compressor = zstandard.ZstdCompressor(level=1)
        probe = body[:_COMPRESS_PROBE]
        if len(compressor.compress(probe)) > 0.9 * len(probe):
            return body, None
        return compressor.compress(body), "zstd"
    if accepted.get("gzip", accepted.get("*", 0.0)) > 0:
        probe = body[: _COMPRESS_PROBE // 4]
        if len(zlib.compress(probe, 1)) > 0.5 * len(probe):
            return body, None
        gzip = zlib.compressobj(1, zlib.DEFLATED, 31)  # gzip container
        return gzip.compress(body) + gzip.flush(), "gzip"
    return body, None


def _etag(file_id: FileId, *parts: object) -> str:
    digest = hashlib.sha256(repr((file_id.path, file_id.size, file_id.mtime_ns, parts)).encode()).hexdigest()
    return f'W/"{digest[:32]}"'


def _not_modified(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match", "")
    wanted = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == wanted or tag.strip() == "*" for tag in header.split(","))


def create_app(settings: Settings, registry: Registry | None = None) -> FastAPI:
    reg = registry or default_registry()
    roots = RootSet(settings.roots)
    service = PlaneService(settings, reg)
    app = FastAPI(title="imageBio", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.service = service
    app.state.roots = roots
    app.add_middleware(SecurityMiddleware, settings=settings)

    # ── errors: JSON, no internals ───────────────────────────────────────────
    @app.exception_handler(ImageBioError)
    async def _imagebio_error(request: Request, exc: ImageBioError) -> JSONResponse:
        if exc.status >= 500:
            log.error("internal error on %s: %s", request.url.path, exc)
            return _error(500, "internal_error", "internal error")
        return _error(exc.status, exc.code, exc.public_message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())[1:]) or "request"
        return _error(
            400, "invalid_request", f"invalid parameter {where[:64]!r}: {first.get('msg', 'invalid')}"
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, code.replace("_", " "))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unexpected error on %s", request.url.path, exc_info=exc)
        return _error(500, "internal_error", "internal error")

    # ── helpers ──────────────────────────────────────────────────────────────
    def authorise(request: Request, path: str) -> tuple[User, ResolvedPath]:
        """Identity, then path resolution inside the roots of that user: always first."""
        user = current_user(request, settings)
        return user, roots.resolve(path, user)

    def file_of(request: Request, path: str) -> ResolvedPath:
        _, resolved = authorise(request, path)
        if not resolved.is_file:
            raise InvalidRequestError(f"not a file: {resolved.logical}")
        return resolved

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        _check_query(request, frozenset())
        user = current_user(request, settings)
        return {"user": user.name, "groups": sorted(user.groups), "auth_mode": settings.auth_mode}

    @app.get("/api/fs/roots")
    def fs_roots(request: Request) -> dict[str, Any]:
        _check_query(request, frozenset())
        user = current_user(request, settings)
        return {
            "roots": [{"name": r.name, "available": roots.available(r)} for r in roots.visible_roots(user)]
        }

    @app.get("/api/fs/list")
    def fs_list(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        offset: int = Query(0, ge=0),
        limit: int = Query(1000, ge=1, le=10_000),
        hidden: bool = Query(False),
    ) -> dict[str, Any]:
        _check_query(request, frozenset({"path", "offset", "limit", "hidden"}))
        user = current_user(request, settings)
        listing = roots.list_dir(
            path, user, supports=reg.supports, offset=offset, limit=limit, show_hidden=hidden
        )
        return listing.to_json()

    @app.get("/api/datasets/info")
    def dataset_info(
        request: Request, path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH)
    ) -> Response:
        options = _check_query(request, frozenset({"path"}))
        resolved = file_of(request, path)
        info, file_id = service.info(resolved.real, options)
        etag = _etag(file_id, "info", options_key(options))
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        body = {
            "path": resolved.logical,
            "file": {
                "name": resolved.logical.rsplit("/", 1)[-1],
                "size": file_id.size,
                "mtime": file_id.mtime_ns / 1e9,
                "etag": file_id.etag,
            },
            "info": info.to_json(),
        }
        return Response(json.dumps(body, allow_nan=False), media_type="application/json", headers=headers)

    @app.get("/api/datasets/plane")
    def dataset_plane(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        bin: int = Query(1, ge=1, le=MAX_BIN),
        x: int | None = Query(None, ge=0),
        y: int | None = Query(None, ge=0),
        w: int | None = Query(None, ge=1),
        h: int | None = Query(None, ge=1),
    ) -> Response:
        options = _check_query(request, frozenset({"path", "t", "c", "z", "bin", "x", "y", "w", "h"}))
        resolved = file_of(request, path)
        given = [v is not None for v in (x, y, w, h)]
        if any(given) and not all(given):
            raise InvalidRequestError("a region needs x, y, w and h together")
        region = (x, y, w, h) if all(given) else None
        etag = _etag(FileId.of(resolved.real), "plane", options_key(options), t, c, z, bin, region)
        base_headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL, "Vary": "Accept-Encoding"}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=base_headers)
        result = service.plane(resolved.real, t, c, z, bin=bin, region=region, options=options)  # type: ignore[arg-type]
        array = result.array
        if sys.byteorder == "big":  # little-endian on the wire, whatever the server
            array = array.astype(array.dtype.newbyteorder("<"))
        body, encoding = _encode(
            memoryview(np.ascontiguousarray(array)).cast("B"), request.headers.get("accept-encoding", "")
        )
        headers = {
            **base_headers,
            "ETag": _etag(result.file_id, "plane", options_key(options), t, c, z, bin, region),
            "X-Dtype": array.dtype.name,
            "X-Shape": f"{array.shape[0]},{array.shape[1]}",
            "X-Bin": str(result.bin),
            "X-Region": ",".join(map(str, result.region)),
            "X-Pixel-Size": json.dumps(result.calibration.x.to_json(), ensure_ascii=True),
        }
        if encoding:
            headers["Content-Encoding"] = encoding
        return Response(
            bytes(body) if isinstance(body, memoryview) else body,
            media_type="application/octet-stream",
            headers=headers,
        )

    @app.get("/api/datasets/stats")
    def dataset_stats(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
    ) -> Response:
        options = _check_query(request, frozenset({"path", "t", "c", "z"}))
        resolved = file_of(request, path)
        stats, file_id = service.stats(resolved.real, t, c, z, options)
        etag = _etag(file_id, "stats", options_key(options), t, c, z)
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        return Response(
            json.dumps(stats.to_json(), allow_nan=False), media_type="application/json", headers=headers
        )

    return app
