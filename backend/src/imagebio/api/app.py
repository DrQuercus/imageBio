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

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import zlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Final

import numpy as np
import zstandard
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from imagebio import __version__
from imagebio.admin.store import SettingsStore
from imagebio.api.admin import register_admin_routes
from imagebio.api.live import register_live_routes
from imagebio.api.metrics import CONTENT_TYPE, TOKEN_VARIABLE, render_metrics
from imagebio.api.security import (
    MIN_PROXY_SECRET,
    PROXY_SECRET_VARIABLE,
    SecurityMiddleware,
    current_user,
)
from imagebio.core.cache import options_key
from imagebio.core.config import ConfigError, Settings
from imagebio.core.errors import ImageBioError, InvalidRequestError
from imagebio.core.model import FileId, json_safe
from imagebio.cryo.classes import model_star_name, read_classes
from imagebio.cryo.gain import N_ORIENTATIONS
from imagebio.cryo.picks import read_picks
from imagebio.fs.roots import MAX_PATH_LENGTH, ResolvedPath, RootSet, User
from imagebio.io.registry import Registry, default_registry
from imagebio.live.service import LiveService
from imagebio.live.thumbs import MAX_PX, MIN_PX, render_thumbnail
from imagebio.proc.fourier import radial_profile
from imagebio.service.export import export_file_name, export_plane, export_stack
from imagebio.service.planes import MAX_BIN, GainSpec, PlaneService, Processing

__all__ = ["create_app"]

log = logging.getLogger("imagebio.api")

_OPTION_PREFIX: Final = "opt."
_MAX_OPTIONS: Final = 16
_OPTION_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_INT: Final = re.compile(r"-?\d{1,18}")
_FLOAT: Final = re.compile(r"-?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?")
_COMPRESS_PROBE: Final = 256 * 1024
_CACHE_CONTROL: Final = "private, no-cache"
_GAIN_PARAMS: Final = frozenset({"gain", "gain_orient", "gain_inv"})
_PROCESSING_PARAMS: Final = frozenset({"t_end", "lp", "hp", "apix", "fbin", "sharp"}) | _GAIN_PARAMS
_STATS_PARAMS: Final = frozenset({"path", "t", "c", "z"}) | _PROCESSING_PARAMS
_PLANE_PARAMS: Final = _STATS_PARAMS | {"bin", "x", "y", "w", "h"}
_SPECTRUM_PARAMS: Final = frozenset({"path", "t", "c", "z", "t_end", "tile"}) | _GAIN_PARAMS
_GAIN_CHECK_PARAMS: Final = frozenset({"path"}) | _GAIN_PARAMS
#: Where "npm run build" (frontend/) puts the web application.
STATIC_DIR: Final = Path(__file__).resolve().parent.parent / "static"
#: The page only loads its own scripts and styles and only talks to this server.
_APP_CSP: Final = (
    "default-src 'self'; img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


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


def create_app(
    settings: Settings, registry: Registry | None = None, static_dir: Path | None = STATIC_DIR
) -> FastAPI:
    """The application. ``static_dir`` holds the built web interface (``None``: API only)."""
    reg = registry or default_registry()
    # what an administrator changed in the settings page lies over the file (admin/store.py)
    store = SettingsStore(settings)
    effective = store.effective
    roots = RootSet(effective.roots)
    service = PlaneService(settings, reg)
    live = LiveService(effective, reg, service)
    store.on_change(lambda new: roots.replace(new.roots))
    store.on_change(live.reconfigure)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        live.start()  # resumes the watches that were active when the server stopped
        try:
            yield
        finally:
            live.close()

    app = FastAPI(
        title="imageBio",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.live = live
    app.state.roots = roots
    app.state.store = store
    metrics_token = os.environ.get(TOKEN_VARIABLE) or None
    proxy_secret = os.environ.get(PROXY_SECRET_VARIABLE) or None
    if proxy_secret is not None and settings.auth_mode == "proxy" and len(proxy_secret) < MIN_PROXY_SECRET:
        raise ConfigError(
            f"{PROXY_SECRET_VARIABLE} is too short: at least {MIN_PROXY_SECRET} characters, generated"
        )
    app.add_middleware(
        SecurityMiddleware, settings=settings, metrics_token=metrics_token, proxy_secret=proxy_secret
    )

    # ── errors: JSON, no internals ───────────────────────────────────────────
    @app.exception_handler(ImageBioError)
    async def _imagebio_error(request: Request, exc: ImageBioError) -> JSONResponse:
        if exc.code == ImageBioError.code:  # not an error written to be told: nothing of it is
            log.error("internal error on %s: %s", request.url.path, exc)
            return _error(500, "internal_error", "internal error")
        if exc.status >= 500:  # a failure of the server that has a public name and message
            log.warning("%s on %s: %s", exc.code, request.url.path, exc)
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

    @app.exception_handler(sqlite3.OperationalError)
    async def _index_busy(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        # the only database is the index of the Live mode; a writer that waited in vain for
        # another one (a scan of a very large directory) is asked to come back, not told "500"
        log.warning("live index unavailable on %s: %s", request.url.path, exc)
        return _error(
            503, "live_index_busy", "the live index is busy: try again in a moment", {"Retry-After": "5"}
        )

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

    def gain_of(
        request: Request,
        gain: str | None = Query(None, min_length=1, max_length=MAX_PATH_LENGTH),
        gain_orient: int = Query(0, ge=0, lt=N_ORIENTATIONS),
        gain_inv: bool = Query(False),
    ) -> GainSpec | None:
        """The gain reference of a request: a logical path, authorised like any other file."""
        if gain is None:
            return None
        return GainSpec.of(file_of(request, gain).real, gain_orient, gain_inv)

    def processing_of(
        t_end: int | None = Query(None, ge=0),
        lp: float | None = Query(None, gt=0),
        hp: float | None = Query(None, gt=0),
        apix: float | None = Query(None, gt=0),
        fbin: bool = Query(False),
        sharp: bool = Query(False),
        gain: GainSpec | None = Depends(gain_of),
    ) -> Processing:
        return Processing(
            t_end=t_end, lowpass=lp, highpass=hp, apix=apix, gain=gain, fourier_bin=fbin, sharp=sharp
        )

    # ── endpoints ────────────────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        """Figures for a monitoring system (``api/metrics.py``). Off unless configured; then
        for this computer only, or for the bearer of the token."""
        _check_query(request, frozenset())
        if not settings.metrics:
            raise StarletteHTTPException(status_code=404)
        if metrics_token is not None:
            given = request.headers.get("authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {metrics_token}".encode()):
                return _error(401, "authentication_required", "a metrics token is required")
        elif request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            return _error(403, "access_denied", "metrics are served to this computer only")
        body = render_metrics(service.cache_stats(), live.metrics())
        return Response(body, media_type=CONTENT_TYPE, headers={"Cache-Control": "no-store"})

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        """Who the user is, their groups (from the proxy, and given in the settings page),
        whether they administer the server, and the data roots they read."""
        _check_query(request, frozenset())
        user = current_user(request, settings)
        given = store.groups_of(user.name) if settings.auth_mode == "proxy" else frozenset()
        return {
            "user": user.name,
            "groups": sorted(user.groups),
            "groups_given": sorted(given),
            "admin": user.admin,
            "auth_mode": settings.auth_mode,
            "roots": [{"name": r.name, "available": roots.available(r)} for r in roots.visible_roots(user)],
        }

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

    def array_response(request: Request, array: Any, headers: dict[str, str]) -> Response:
        """A 2-D array as raw little-endian bytes, compressed when it pays off."""
        if sys.byteorder == "big":
            array = array.astype(array.dtype.newbyteorder("<"))
        body, encoding = _encode(
            np.ascontiguousarray(array).data.cast("B"), request.headers.get("accept-encoding", "")
        )
        headers = {**headers, "X-Dtype": array.dtype.name, "X-Shape": f"{array.shape[0]},{array.shape[1]}"}
        if encoding:
            headers["Content-Encoding"] = encoding
        return Response(
            bytes(body) if isinstance(body, memoryview) else body,
            media_type="application/octet-stream",
            headers=headers,
        )

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
        proc: Processing = Depends(processing_of),
    ) -> Response:
        options = _check_query(request, _PLANE_PARAMS)
        resolved = file_of(request, path)
        given = [v is not None for v in (x, y, w, h)]
        if any(given) and not all(given):
            raise InvalidRequestError("a region needs x, y, w and h together")
        region = (x, y, w, h) if all(given) else None
        # "2": filtered reduced views are reduced before they are filtered since lot A (ADR 013)
        tag = ("plane", 2, options_key(options), t, c, z, bin, region, proc.key())
        etag = _etag(FileId.of(resolved.real), *tag)
        base_headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL, "Vary": "Accept-Encoding"}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=base_headers)
        result = service.plane(resolved.real, t, c, z, bin=bin, region=region, options=options, proc=proc)  # type: ignore[arg-type]
        headers = {
            **base_headers,
            "ETag": _etag(result.file_id, *tag),
            "X-Bin": str(result.bin),
            "X-Region": ",".join(map(str, result.region)),
            "X-Pixel-Size": json.dumps(result.calibration.x.to_json(), ensure_ascii=True),
        }
        return array_response(request, result.array, headers)

    @app.get("/api/datasets/stats")
    def dataset_stats(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        bin: int = Query(1, ge=1, le=MAX_BIN),
        proc: Processing = Depends(processing_of),
    ) -> Response:
        """Statistics of the full-resolution plane, or (``bin``) of the whole plane as
        ``/plane`` serves it at that bin."""
        options = _check_query(request, _STATS_PARAMS | {"bin"})
        resolved = file_of(request, path)
        stats, file_id = service.stats(resolved.real, t, c, z, options, proc, bin)
        etag = _etag(file_id, "stats", 2, options_key(options), t, c, z, proc.key(), bin)
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        return Response(
            json.dumps(stats.to_json(), allow_nan=False), media_type="application/json", headers=headers
        )

    def spectrum_of(
        request: Request, path: str, t: int, c: int, z: int, proc: Processing, tile: int
    ) -> tuple[Any, str]:
        options = _check_query(request, _SPECTRUM_PARAMS)
        resolved = file_of(request, path)
        power, file_id = service.spectrum(resolved.real, t, c, z, tile=tile, options=options, proc=proc)
        return power, _etag(file_id, "spectrum", options_key(options), t, c, z, proc.key(), tile)

    @app.get("/api/datasets/spectrum")
    def dataset_spectrum(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        t_end: int | None = Query(None, ge=0),
        tile: int = Query(512, ge=16, le=2048),
        gain: GainSpec | None = Depends(gain_of),
    ) -> Response:
        """Power spectrum of a plane: a square float32 image, zero frequency at the centre.
        A period of d Å lies at (side x Å-per-pixel / d) pixels from the centre."""
        power, etag = spectrum_of(request, path, t, c, z, Processing(t_end=t_end, gain=gain), tile)
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL, "Vary": "Accept-Encoding"}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        return array_response(request, power, headers)

    @app.get("/api/datasets/spectrum/profile")
    def dataset_spectrum_profile(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        t_end: int | None = Query(None, ge=0),
        tile: int = Query(512, ge=16, le=2048),
        gain: GainSpec | None = Depends(gain_of),
    ) -> Response:
        """Rotational average of the power spectrum: frequency (cycles per pixel) and power."""
        power, etag = spectrum_of(request, path, t, c, z, Processing(t_end=t_end, gain=gain), tile)
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        frequency, profile = radial_profile(power)
        # a power beyond float32 is not a number JSON can carry: it becomes null
        body = {"side": int(power.shape[0]), "frequency": frequency.tolist(), "power": profile.tolist()}
        return Response(
            json.dumps(json_safe(body), allow_nan=False), media_type="application/json", headers=headers
        )

    @app.get("/api/datasets/ctf")
    def dataset_ctf(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        t_end: int | None = Query(None, ge=0),
        apix: float | None = Query(None, gt=0),
        kv: float | None = Query(None),
        cs: float | None = Query(None),
        amp: float | None = Query(None),
        gain: GainSpec | None = Depends(gain_of),
    ) -> Response:
        """A quick estimate of the CTF of a plane from its power spectrum: defocus (µm,
        positive underfocus), the resolution up to which its Thon rings are found, and the
        zeros of the fit. ``kv``, ``cs`` (mm) and ``amp`` default to the server's microscope.
        ``found`` is false when the spectrum shows no rings. Astigmatism is not measured."""
        options = _check_query(request, _SPECTRUM_PARAMS - {"tile"} | {"apix", "kv", "cs", "amp"})
        resolved = file_of(request, path)
        scope = {
            "kv": store.effective.kv if kv is None else kv,
            "cs_mm": store.effective.cs_mm if cs is None else cs,
            "amplitude": store.effective.amplitude_contrast if amp is None else amp,
        }
        proc = Processing(t_end=t_end, gain=gain, apix=apix)
        found, file_id = service.ctf(resolved.real, t, c, z, options=options, proc=proc, **scope)
        gain_key = gain.key() if gain is not None else None
        etag = _etag(file_id, "ctf", options_key(options), t, c, z, t_end, gain_key, apix, *scope.values())
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        body = {"found": found is not None, **scope, **(found.to_json() if found is not None else {})}
        return Response(
            json.dumps(json_safe(body), allow_nan=False), media_type="application/json", headers=headers
        )

    @app.get("/api/cryo/gain/check")
    def cryo_gain_check(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        gain: GainSpec | None = Depends(gain_of),
    ) -> Response:
        """Which of the eight orientations of a gain reference flattens a movie best.
        ``current`` echoes ``gain_orient``; the check itself does not depend on it."""
        options = _check_query(request, _GAIN_CHECK_PARAMS)
        resolved = file_of(request, path)
        if gain is None:
            raise InvalidRequestError("a gain reference is required: gain=<path>")
        result = service.gain_check(resolved.real, gain, options)
        etag = _etag(result.file_id, "gain_check", options_key(options), gain.key())
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        body = {
            **result.check.to_json(current=gain.orientation),
            "frames": result.frames,
            "defects": result.defects,
            "listed_defects": result.listed_defects,
            "gain_shape": {"y": result.gain_shape[0], "x": result.gain_shape[1]},
        }
        return Response(
            json.dumps(json_safe(body), allow_nan=False), media_type="application/json", headers=headers
        )

    @app.get("/api/datasets/export")
    def dataset_export(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        c: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        scope: str = Query("plane", pattern="^(plane|stack)$"),
        proc: Processing = Depends(processing_of),
    ) -> Response:
        """An ImageJ TIFF to open in Fiji, with the calibration: the plane ``(t, c, z)`` as the
        viewer shows it (``scope=plane``, after the processing asked for), or every plane of
        the file as stored (``scope=stack``)."""
        options = _check_query(request, _STATS_PARAMS | {"scope"})
        resolved = file_of(request, path)
        if scope == "stack":
            data = export_stack(service, resolved.real, options=options)
            suffix = ""
        else:
            data = export_plane(service, resolved.real, t, c, z, options=options, proc=proc)
            info, _ = service.info(resolved.real, options)
            parts = [
                f"_{axis}{value + 1}"
                for axis, value, size in (("t", t, info.size_t), ("z", z, info.size_z))
                if size > 1
            ]
            if info.size_c > 1 and not info.rgb:
                parts.append(f"_c{c + 1}")
            suffix = "".join(parts)
        name = export_file_name(resolved.logical, suffix)
        return Response(
            data,
            media_type="image/tiff",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/api/datasets/thumb")
    def dataset_thumb(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        t: int = Query(0, ge=0),
        z: int = Query(0, ge=0),
        size: int = Query(128, ge=MIN_PX, le=MAX_PX),
    ) -> Response:
        """One plane of a file as a small JPEG, binned by averaging and stretched on its own
        values: a tile of the gallery of a stack (particles, class averages, sections)."""
        options = _check_query(request, frozenset({"path", "t", "z", "size"}))
        resolved = file_of(request, path)
        _, file_id = service.info(resolved.real, options)
        etag = _etag(file_id, "thumb", options_key(options), t, z, size)
        headers = {"ETag": etag, "Cache-Control": "private, max-age=300"}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        data = render_thumbnail(service, resolved.real, size, t=t, z=z, options=options)
        return Response(data, media_type="image/jpeg", headers=headers)

    @app.get("/api/cryo/classes")
    def cryo_classes(
        request: Request, path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH)
    ) -> Response:
        """What RELION says of the class averages ``path`` (``*_classes.mrcs``): the share of
        the particles and the resolution of each class, from the model file next to it.
        ``classes`` is null when there is no such file."""
        _check_query(request, frozenset({"path"}))
        user, resolved = authorise(request, path)
        if not resolved.is_file:
            raise InvalidRequestError(f"not a file: {resolved.logical}")
        directory, _, name = resolved.logical.rpartition("/")
        model_name = model_star_name(name)
        body: dict[str, Any] = {"model": None, "classes": None}
        if model_name is not None:
            try:
                model = roots.resolve(f"{directory}/{model_name}", user)
            except ImageBioError:
                model = None
            if model is not None and model.is_file:
                body = {"model": model.logical, "classes": read_classes(model.real)}
        return Response(
            json.dumps(json_safe(body), allow_nan=False),
            media_type="application/json",
            headers={"Cache-Control": "private, no-cache"},
        )

    @app.get("/api/cryo/picks")
    def cryo_picks(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        picks: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
    ) -> Response:
        """The particles that a coordinate file (``picks``: STAR, .box, .cbox or .cs, a logical
        path like any other) holds for the image ``path``: centres in pixels of that image."""
        options = _check_query(request, frozenset({"path", "picks"}))
        image = file_of(request, path)
        chosen = file_of(request, picks)
        info, file_id = service.info(image.real, options)
        picks_id = FileId.of(chosen.real)
        etag = _etag(file_id, "picks", options_key(options), picks_id.etag)
        headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        found = read_picks(chosen.real, image.logical.rsplit("/", 1)[-1], (info.size_y, info.size_x))
        body = {**found.to_json(), "picks": chosen.logical, "image": {"x": info.size_x, "y": info.size_y}}
        return Response(
            json.dumps(json_safe(body), allow_nan=False), media_type="application/json", headers=headers
        )

    register_live_routes(app, settings, roots, live)
    register_admin_routes(app, settings, store, live)

    # ── web interface ────────────────────────────────────────────────────────
    index = static_dir / "index.html" if static_dir else None
    if index is not None and index.is_file() and static_dir is not None:
        if (static_dir / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def web_app() -> FileResponse:
            return FileResponse(
                index,
                media_type="text/html",
                headers={"Content-Security-Policy": _APP_CSP, "Cache-Control": "no-cache"},
            )

    else:

        @app.get("/", include_in_schema=False)
        def web_app_missing() -> JSONResponse:
            return _error(
                404, "ui_not_built", "the web interface is not built (run 'npm run build' in frontend/)"
            )

    return app
