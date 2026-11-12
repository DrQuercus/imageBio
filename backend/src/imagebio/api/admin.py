"""The routes of the settings page (``/api/admin/…``): administrators only (ADR 014).

Administrators see what users never do: the paths of the data roots on the server, the
addresses of the alert channels, the configuration. Secrets are never sent, only whether they
are set. Changes are JSON bodies, read only once the administrator is known, and bounded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from imagebio import __version__
from imagebio.admin.store import SECTIONS, SettingsStore
from imagebio.api.metrics import TOKEN_VARIABLE
from imagebio.api.security import PROXY_SECRET_VARIABLE, current_user
from imagebio.core.config import Settings
from imagebio.core.errors import AccessDeniedError, ImageBioError, InvalidRequestError
from imagebio.fs.roots import User
from imagebio.live.service import LiveService

__all__ = ["MAX_BODY_BYTES", "register_admin_routes"]

MAX_BODY_BYTES: Final = 256 * 1024
_SECTION: Final = PathParam(pattern=f"^({'|'.join(SECTIONS)})$")


class PayloadTooLargeError(ImageBioError):
    status = 413
    code = "payload_too_large"


def _json(body: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(body, allow_nan=False, ensure_ascii=False),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


async def _body(request: Request) -> dict[str, Any]:
    """The JSON object of a request, read once its sender is known to be an administrator,
    and no larger than ``MAX_BODY_BYTES``."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise InvalidRequestError("expected a JSON body (Content-Type: application/json)")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_BODY_BYTES:
            raise PayloadTooLargeError(f"the body is larger than {MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(data)
        # text that cannot be written back (a lone surrogate) is refused here, not when saved
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise InvalidRequestError("the body is not valid JSON") from None
    except UnicodeEncodeError:
        raise InvalidRequestError("the body holds characters that are not text") from None
    if not isinstance(body, dict):
        raise InvalidRequestError("expected a JSON object")
    return body


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidRequestError("'version' must be the version of the settings the change was made on")
    return value


def configuration(settings: Settings) -> dict[str, Any]:
    """What the file sets and the settings page cannot change, to read. Secrets: whether they
    are set, never their value."""
    smtp = settings.smtp
    processing = settings.processing
    return {
        "version": __version__,
        "server": {
            "host": settings.host,
            "port": settings.port,
            "allowed_hosts": sorted(settings.allowed_hosts),
            "trust_network": settings.trust_network,
            "metrics": settings.metrics,
            "metrics_token": bool(os.environ.get(TOKEN_VARIABLE)),
            "root_parents": [str(p) for p in settings.root_parents],
        },
        "auth": {
            "mode": settings.auth_mode,
            "user_header": settings.user_header,
            "groups_header": settings.groups_header,
            "admins": sorted(settings.admins),
            "admin_groups": sorted(settings.admin_groups),
            "proxy_secret": bool(os.environ.get(PROXY_SECRET_VARIABLE)),
        },
        "cache": {
            "plane_bytes": settings.plane_cache_bytes,
            "thumb_bytes": settings.thumb_cache_bytes,
            "max_open_datasets": settings.max_open_datasets,
            "max_response_pixels": settings.max_response_pixels,
        },
        "live": {
            "state_dir": str(settings.live_state_dir),
            "interval_s": settings.live_interval_s,
            "min_stable_s": settings.live_min_stable_s,
            "scan": settings.live_scan,
        },
        "smtp": None
        if smtp is None
        else {
            "host": smtp.host,
            "port": smtp.port,
            "from": smtp.sender,
            "starttls": smtp.starttls,
            "username": smtp.username,
            "password_env": smtp.password_env,
            "password_set": bool(smtp.password_env and os.environ.get(smtp.password_env)),
        },
        "processing": None
        if processing is None
        else {
            "slots": processing.slots,
            "steps": {
                kind: Path(step.argv[0]).name if step is not None and step.argv else None
                for kind, step in (("motion", processing.motion), ("ctf", processing.ctf))
            },
        },
    }


def register_admin_routes(app: FastAPI, settings: Settings, store: SettingsStore, live: LiveService) -> None:
    def administrator(request: Request) -> User:
        user = current_user(request, settings)
        if not user.admin:
            raise AccessDeniedError("this page is for the administrators of the server")
        return user

    def state() -> dict[str, Any]:
        sections = store.sections()
        accounts = store.accounts(also=live.watch_users())
        groups = sorted(
            {g for r in sections["roots"]["value"] for g in r["groups"]}
            | {g for a in accounts for g in a["groups"] + a["proxy_groups"]}
        )
        updated_at, updated_by = store.updated()
        return {
            "version": store.version,
            "updated_at": updated_at,
            "updated_by": updated_by,
            "sections": sections,
            "root_parents": store.root_parents(),
            "accounts": accounts,
            "groups": groups,
            "configuration": configuration(settings),
        }

    @app.get("/api/admin/settings")
    def admin_settings(request: Request) -> Response:
        """Everything the settings page shows to an administrator."""
        administrator(request)
        return _json(state())

    @app.put("/api/admin/settings/{section}")
    async def admin_section_set(request: Request, section: str = _SECTION) -> Response:
        """Replace a section: ``{"version": n, "value": …}`` (as the file would write it)."""
        user = administrator(request)
        body = await _body(request)
        if set(body) != {"version", "value"}:
            raise InvalidRequestError("expected {version, value}")
        version = _version(body["version"])
        await run_in_threadpool(store.set_section, section, body["value"], by=user.name, version=version)
        return _json(await run_in_threadpool(state))

    @app.delete("/api/admin/settings/{section}")
    async def admin_section_reset(
        request: Request, section: str = _SECTION, version: int = Query(..., ge=0)
    ) -> Response:
        """Go back to what the configuration file says for a section."""
        user = administrator(request)
        await run_in_threadpool(store.reset_section, section, by=user.name, version=version)
        return _json(await run_in_threadpool(state))

    @app.put("/api/admin/accounts")
    async def admin_account_set(request: Request) -> Response:
        """Give a user groups and the administrator role: ``{"version", "name", "groups",
        "admin"}``. The user need not have come yet: the name is the one the proxy gives."""
        user = administrator(request)
        body = await _body(request)
        if set(body) != {"version", "name", "groups", "admin"}:
            raise InvalidRequestError("expected {version, name, groups, admin}")
        name, groups, admin = body["name"], body["groups"], body["admin"]
        if not isinstance(name, str) or not isinstance(groups, list) or not isinstance(admin, bool):
            raise InvalidRequestError("name is text, groups a list of names, admin true or false")
        await run_in_threadpool(
            store.set_account, name, groups, admin, by=user.name, version=_version(body["version"])
        )
        return _json(await run_in_threadpool(state))

    @app.delete("/api/admin/accounts")
    async def admin_account_remove(
        request: Request,
        name: str = Query(..., min_length=1, max_length=256),
        version: int = Query(..., ge=0),
    ) -> Response:
        """Take back the groups and the role given here (those of the proxy and of the file stay)."""
        user = administrator(request)
        await run_in_threadpool(store.remove_account, name, by=user.name, version=version)
        return _json(await run_in_threadpool(state))

    @app.get("/api/admin/log")
    def admin_log(request: Request) -> Response:
        """The latest changes made in the settings page, newest first: when, who, what."""
        administrator(request)
        return _json({"entries": store.log()})
