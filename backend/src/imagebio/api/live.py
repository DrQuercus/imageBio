"""The routes of the Live mode (``/api/live/…``).

A watch belongs to the user who made it; somebody else's watch does not exist (404). On every
request the watched directory is resolved again for the current user: losing access to a data
root ends access to its index too.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response

from imagebio.api.security import current_user
from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError, NotFoundError
from imagebio.core.model import json_safe
from imagebio.fs.roots import MAX_PATH_LENGTH, RootSet
from imagebio.live.alerts import AlertRules
from imagebio.live.service import CATEGORIES, MAX_ITEMS, LiveService, WatchInfo
from imagebio.live.thumbs import MAX_PX, MIN_PX

__all__ = ["register_live_routes"]

_NO_CACHE: Final = "private, no-cache"
#: A thumbnail is tied to the content of its file by its ETag: it may be reused for a while.
_THUMB_CACHE: Final = "private, max-age=300"
#: Identifiers and sequence numbers are SQLite integers: anything larger names nothing, and
#: must be refused here rather than fail in the index.
_MAX_INT: Final = 2**63 - 1
_WATCH_ID: Final = PathParam(ge=1, le=_MAX_INT)


def _json(body: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(json_safe(body), allow_nan=False),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": _NO_CACHE},
    )


def _only(request: Request, allowed: frozenset[str]) -> None:
    """Reject unknown or repeated query parameters: a typo must not pass silently."""
    seen: set[str] = set()
    for key, _ in request.query_params.multi_items():
        if key not in allowed:
            raise InvalidRequestError(f"unknown query parameter {key[:64]!r}")
        if key in seen:
            raise InvalidRequestError(f"query parameter {key!r} is given more than once")
        seen.add(key)


def register_live_routes(app: FastAPI, settings: Settings, roots: RootSet, live: LiveService) -> None:
    def own_watch(request: Request, watch_id: int) -> WatchInfo:
        """The watch of the current user, after checking that they may still read its directory."""
        user = current_user(request, settings)
        watch = live.watch(user.name, watch_id)
        resolved = roots.resolve(watch.logical, user)
        if not resolved.is_dir or resolved.real != watch.root_path:
            raise NotFoundError("the watched directory is no longer there")
        live.seen(watch)
        return watch

    @app.get("/api/live/watches")
    def live_watches(request: Request) -> Response:
        _only(request, frozenset())
        user = current_user(request, settings)
        return _json({"watches": [w.to_json() for w in live.watches(user.name)]})

    @app.post("/api/live/watches")
    def live_watch_start(
        request: Request, path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH)
    ) -> Response:
        """Watch a directory (a logical path). Watching it again returns the same watch."""
        _only(request, frozenset({"path"}))
        user = current_user(request, settings)
        resolved = roots.resolve(path, user)
        if not resolved.is_dir:
            raise InvalidRequestError(f"not a directory: {resolved.logical}")
        return _json(live.start_watch(user.name, resolved.logical, resolved.real).to_json())

    @app.get("/api/live/watches/{watch_id}")
    def live_watch_summary(request: Request, watch_id: int = _WATCH_ID) -> Response:
        _only(request, frozenset())
        return _json(live.summary(own_watch(request, watch_id)))

    @app.delete("/api/live/watches/{watch_id}")
    def live_watch_stop(request: Request, watch_id: int = _WATCH_ID, forget: bool = Query(False)) -> Response:
        """Stop one's own watch. What was indexed is kept, unless ``forget``: the watch then
        leaves the list, and the index of its directory goes once nobody else watches it.
        Other users' watches are untouched."""
        _only(request, frozenset({"forget"}))
        user = current_user(request, settings)
        watch = live.stop_watch(user.name, watch_id, forget=forget)
        return _json({**watch.to_json(), "forgotten": forget})

    @app.get("/api/live/watches/{watch_id}/items")
    def live_watch_items(
        request: Request,
        watch_id: int = _WATCH_ID,
        since: int = Query(0, ge=0, le=_MAX_INT),
        limit: int = Query(1000, ge=1, le=MAX_ITEMS),
        category: str = Query("data,image", max_length=64),
    ) -> Response:
        """Exposures that changed since sequence number ``since`` (0: all), oldest change first.
        ``category``: comma-separated, among data, image, gridsquare, foilhole, atlas."""
        _only(request, frozenset({"since", "limit", "category"}))
        categories = tuple(c.strip() for c in category.split(",") if c.strip())
        if not categories or set(categories) - set(CATEGORIES):
            raise InvalidRequestError(f"category must be among {', '.join(CATEGORIES)}")
        return _json(live.items(own_watch(request, watch_id), since, limit, categories))

    @app.get("/api/live/watches/{watch_id}/history")
    def live_watch_history(
        request: Request, watch_id: int = _WATCH_ID, bucket: int = Query(60, ge=1, le=86400)
    ) -> Response:
        """Acquisitions per time bucket, by acquisition time."""
        _only(request, frozenset({"bucket"}))
        return _json(live.history(own_watch(request, watch_id), bucket))

    @app.get("/api/live/alerts/channels")
    def live_alert_channels(request: Request) -> Response:
        """Where alerts can be sent: names and kinds. The addresses stay on the server."""
        _only(request, frozenset())
        current_user(request, settings)
        return _json({"channels": live.alert_channels()})

    @app.get("/api/live/watches/{watch_id}/alerts")
    def live_watch_alerts(request: Request, watch_id: int = _WATCH_ID) -> Response:
        _only(request, frozenset())
        return _json(live.alerts(own_watch(request, watch_id)))

    @app.put("/api/live/watches/{watch_id}/alerts")
    def live_watch_alerts_set(
        request: Request,
        watch_id: int = _WATCH_ID,
        channels: str = Query("", max_length=1024),
        no_image: float | None = Query(None),
        disk_free: float | None = Query(None),
        rate_drop: float | None = Query(None),
    ) -> Response:
        """Replace the alert rules of a watch. ``channels``: comma-separated names; ``no_image``
        in minutes, ``disk_free`` and ``rate_drop`` in percent. What is left out is not watched."""
        _only(request, frozenset({"channels", "no_image", "disk_free", "rate_drop"}))
        names = tuple(c.strip() for c in channels.split(",") if c.strip())
        rules = AlertRules(names, no_image, disk_free, rate_drop)
        return _json(live.set_alerts(own_watch(request, watch_id), rules))

    @app.post("/api/live/watches/{watch_id}/alerts/test")
    def live_watch_alerts_test(request: Request, watch_id: int = _WATCH_ID) -> Response:
        """Send a test notice to the channels of the watch."""
        _only(request, frozenset())
        return _json({"results": live.test_alerts(own_watch(request, watch_id))})

    @app.get("/api/live/thumb")
    def live_thumbnail(
        request: Request,
        path: str = Query(..., min_length=1, max_length=MAX_PATH_LENGTH),
        size: int = Query(256, ge=MIN_PX, le=MAX_PX),
    ) -> Response:
        """A JPEG thumbnail of an image file, binned by averaging. Not tied to a watch: any
        file the user may read."""
        _only(request, frozenset({"path", "size"}))
        user = current_user(request, settings)
        resolved = roots.resolve(path, user)
        if not resolved.is_file:
            raise InvalidRequestError(f"not a file: {resolved.logical}")
        data, file_id = live.thumbnail(resolved.real, size)
        identity = repr((file_id.path, file_id.size, file_id.mtime_ns, "thumb", size)).encode()
        etag = f'W/"{hashlib.sha256(identity).hexdigest()[:32]}"'
        headers = {"ETag": etag, "Cache-Control": _THUMB_CACHE}
        wanted = [t.strip() for t in request.headers.get("if-none-match", "").split(",")]
        if etag in wanted or etag.removeprefix("W/") in wanted:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type="image/jpeg", headers=headers)
