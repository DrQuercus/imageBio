"""Who is asking, and from where: identity from the reverse proxy, Host allow-list, headers.

``auth_mode = "none"`` is the standalone mode (one local user, loopback only). In ``"proxy"``
mode an authenticating reverse proxy is the only client and passes the identity in headers; the
server is bound so that nobody else can reach it (see :class:`imagebio.core.config.Settings`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from starlette.requests import Request

from imagebio.core.config import Settings, is_loopback_host
from imagebio.core.errors import ImageBioError
from imagebio.fs.roots import LOCAL_USER, User

__all__ = ["AuthenticationRequired", "SecurityMiddleware", "current_user"]

_LOOPBACK_NAMES: Final = frozenset({"localhost", "127.0.0.1", "[::1]"})
_NAME: Final = re.compile(r"[^\x00-\x1f\x7f,]{1,256}")
_MAX_GROUPS: Final = 256
_SECURITY_HEADERS: Final = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-resource-policy", b"same-origin"),
)
_OPEN_PATHS: Final = frozenset({"/healthz"})

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class AuthenticationRequired(ImageBioError):
    """The reverse proxy did not provide the identity of the user."""

    status = 401
    code = "authentication_required"


def current_user(request: Request, settings: Settings) -> User:
    """The user of a request. Raises :class:`AuthenticationRequired` in proxy mode when the
    identity header is missing, malformed or sent more than once (an ambiguous identity is no
    identity)."""
    if settings.auth_mode == "none":
        return LOCAL_USER
    names = request.headers.getlist(settings.user_header)
    if len(names) != 1 or not _NAME.fullmatch(names[0].strip()):
        raise AuthenticationRequired("authentication required")
    group_headers = request.headers.getlist(settings.groups_header)
    if len(group_headers) > 1:
        raise AuthenticationRequired("authentication required")
    groups = [g.strip() for g in (group_headers[0] if group_headers else "").split(",") if g.strip()]
    if len(groups) > _MAX_GROUPS:
        raise AuthenticationRequired("authentication required")
    return User(names[0].strip(), frozenset(groups))


def host_name(header: str) -> str:
    """The host of a ``Host`` header, without port, lower-case, without a trailing dot."""
    host = header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else host
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.rstrip(".")


class SecurityMiddleware:
    """Refuses foreign ``Host`` headers (DNS rebinding) and adds the security headers.

    The Host check is enforced when the server is bound to loopback (the allowed names are then
    the loopback names plus ``settings.allowed_hosts``) or when ``allowed_hosts`` is configured.
    """

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], settings: Settings) -> None:
        self.app = app
        loopback = is_loopback_host(settings.host)
        self.allowed = (_LOOPBACK_NAMES | settings.allowed_hosts) if loopback else settings.allowed_hosts
        self.enforce = loopback or bool(settings.allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(k, v) for k, v in _SECURITY_HEADERS if k not in present]
                message["headers"] = headers
            await send(message)

        if self.enforce and scope["path"] not in _OPEN_PATHS:
            hosts = [value for name, value in scope["headers"] if name == b"host"]
            ok = len(hosts) == 1 and host_name(hosts[0].decode("latin-1")) in self.allowed
            if not ok:
                body = json.dumps({"error": {"code": "forbidden_host", "message": "forbidden host"}}).encode()
                await send_with_headers(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send_with_headers)
