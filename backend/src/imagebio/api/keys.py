"""Routes for scripts and assistants (ADR 015): one's personal keys, the description of the
API (OpenAPI), and the files that plug an assistant in (a client with an MCP server, a skill).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, Response

from imagebio.admin.keys import DAYS_DEFAULT, KeyStore
from imagebio.api.admin import json_body
from imagebio.api.security import current_user, proxy_groups
from imagebio.core.config import Settings
from imagebio.core.errors import InvalidRequestError, NotFoundError

__all__ = ["AI_FILES", "register_key_routes"]

#: What an administrator or a user downloads to plug an assistant in, from ``imagebio/ai``.
AI_FILES: Final = {
    "imagebio_ai.py": "text/x-python",
    "SKILL.md": "text/markdown",
}
_AI_DIR: Final = Path(__file__).resolve().parent.parent / "ai"


def _json(body: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(body, allow_nan=False, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def register_key_routes(app: FastAPI, settings: Settings, keys: KeyStore) -> None:
    def standalone() -> bool:
        return settings.auth_mode == "none"

    @app.get("/api/me/keys")
    def my_keys(request: Request) -> Response:
        """One's personal keys still valid (never the keys themselves). In standalone mode
        there are none: the API answers this computer without one."""
        user = current_user(request, settings)
        if standalone():
            return _json({"standalone": True, "keys": []})
        return _json({"standalone": False, "keys": [k.to_json() for k in keys.of(user.name)]})

    @app.post("/api/me/keys")
    async def my_key_make(request: Request) -> Response:
        """Make a key: ``{"name": …, "days": …}``. The key is in the answer, and only there."""
        user = current_user(request, settings)  # a key cannot get here: it only reads
        if standalone():
            raise InvalidRequestError("standalone mode: the API answers this computer without a key")
        body = await json_body(request)
        if set(body) - {"name", "days"}:
            raise InvalidRequestError("expected {name, days}")
        secret, info = keys.create(
            user.name, body.get("name"), body.get("days", DAYS_DEFAULT), proxy_groups(request, settings)
        )
        return _json({"key": secret, **info.to_json()}, status=201)

    @app.delete("/api/me/keys/{key_id}")
    def my_key_revoke(request: Request, key_id: str = PathParam(pattern="^[0-9a-f]{12}$")) -> Response:
        """Revoke one of one's keys."""
        user = current_user(request, settings)
        keys.revoke(user.name, key_id)
        return _json({"keys": [k.to_json() for k in keys.of(user.name)]})

    @app.get("/api/openapi.json", include_in_schema=False)
    def openapi(request: Request) -> Response:
        """The description of the API (OpenAPI 3), for programs and assistants. To users of
        the server only."""
        current_user(request, settings)
        return _json(app.openapi())

    @app.get("/api/ai/{name}", include_in_schema=False)
    def ai_file(request: Request, name: str) -> FileResponse:
        """A file that plugs an assistant in: the client (commands and an MCP server) or the
        skill that tells an assistant how to use it."""
        current_user(request, settings)
        if name not in AI_FILES:
            raise NotFoundError("no such file")
        return FileResponse(
            _AI_DIR / name,
            media_type=AI_FILES[name],
            filename=name,
            headers={"Cache-Control": "no-store"},
        )
