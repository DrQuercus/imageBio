"""Who is asking, and from where: identity from the reverse proxy, Host allow-list, headers.

``auth_mode = "none"`` is the standalone mode (one local user, loopback only). In ``"proxy"``
mode an authenticating reverse proxy is the only client and passes the identity in headers; the
server is bound so that nobody else can reach it (see :class:`imagebio.core.config.Settings`).

"Nobody else" stops at the machine: a loopback address, or the address of a container, can
still be reached by every account of the computer it is on. Where such accounts exist, the
proxy and the server share a secret (environment variable ``IMAGEBIO_PROXY_SECRET``) that the
proxy adds to every request (header ``X-Imagebio-Proxy-Secret``): without it nothing is
believed, and nothing is served but the open routes.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from starlette.requests import Request

from imagebio.admin.keys import PREFIX as KEY_PREFIX
from imagebio.core.config import Settings, is_loopback_host
from imagebio.core.errors import AccessDeniedError, ImageBioError
from imagebio.fs.roots import LOCAL_USER, User

__all__ = [
    "MIN_PROXY_SECRET",
    "PROXY_SECRET_HEADER",
    "PROXY_SECRET_VARIABLE",
    "AuthenticationRequired",
    "SecurityMiddleware",
    "current_user",
]

PROXY_SECRET_VARIABLE: Final = "IMAGEBIO_PROXY_SECRET"  # noqa: S105 - the name of a variable
PROXY_SECRET_HEADER: Final = "X-Imagebio-Proxy-Secret"  # noqa: S105 - the name of a header
MIN_PROXY_SECRET: Final = 24  # characters: it is generated, never typed

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
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_KEY_METHODS: Final = frozenset({"GET", "HEAD"})
_BEARER: Final = "bearer "
_DEFAULT_PORTS: Final = {"http": "80", "https": "443"}
_SECRET_HEADER: Final = PROXY_SECRET_HEADER.lower().encode()

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class AuthenticationRequired(ImageBioError):
    """The reverse proxy did not provide the identity of the user."""

    status = 401
    code = "authentication_required"


def personal_key(request: Request) -> str | None:
    """The personal key a request carries (``Authorization: Bearer ibk_…``), or None. Several
    Authorization headers with a key among them are no identity at all."""
    values = request.headers.getlist("authorization")
    keys = [v[len(_BEARER) :].strip() for v in values if v[: len(_BEARER)].lower() == _BEARER]
    keys = [k for k in keys if k.startswith(KEY_PREFIX)]
    if not keys:
        return None
    if len(values) != 1:
        raise AuthenticationRequired("authentication required")
    return keys[0]


def proxy_groups(request: Request, settings: Settings) -> frozenset[str]:
    """The groups the proxy gives the user of a request (none in standalone mode)."""
    if settings.auth_mode == "none":
        return frozenset()
    given = request.headers.getlist(settings.groups_header)
    return frozenset(g.strip() for g in (given[0] if len(given) == 1 else "").split(",") if g.strip())


def _key_user(request: Request, settings: Settings, key: str) -> User:
    """The user a personal key stands for: its owner, reading only (ADR 015)."""
    keys = getattr(request.app.state, "keys", None)
    found = keys.check(key) if keys is not None else None
    # a key and an identity from the proxy together: no telling which is meant
    if found is None or request.headers.getlist(settings.user_header):
        raise AuthenticationRequired("authentication required")
    if request.method not in _KEY_METHODS:
        raise AccessDeniedError("a personal key only reads")
    store = getattr(request.app.state, "store", None)
    given = store.groups_of(found.user) if store is not None else frozenset()
    return User(found.user, frozenset(found.groups) | given, admin=False, by_key=True)


def current_user(request: Request, settings: Settings) -> User:
    """The user of a request. Raises :class:`AuthenticationRequired` in proxy mode when the
    identity header is missing, malformed or sent more than once (an ambiguous identity is no
    identity).

    The groups are those the proxy gives, and those an administrator gave the user in the
    settings page (``admin/store.py``); so is the administrator role, besides the file's."""
    if settings.auth_mode == "none":
        return LOCAL_USER
    key = personal_key(request)
    if key is not None:
        return _key_user(request, settings, key)
    names = request.headers.getlist(settings.user_header)
    if len(names) != 1 or not _NAME.fullmatch(names[0].strip(" \t")):
        raise AuthenticationRequired("authentication required")
    group_headers = request.headers.getlist(settings.groups_header)
    if len(group_headers) > 1:
        raise AuthenticationRequired("authentication required")
    groups = [g.strip() for g in (group_headers[0] if group_headers else "").split(",") if g.strip()]
    if len(groups) > _MAX_GROUPS:
        raise AuthenticationRequired("authentication required")
    # spaces and tabs only: "alice" followed by a no-break space is not "alice"
    name, given = names[0].strip(" \t"), frozenset(groups)
    if not name:
        raise AuthenticationRequired("authentication required")
    store = getattr(request.app.state, "store", None)
    if store is None:  # an application without a settings page: the file alone
        return User(name, given, admin=name in settings.admins or bool(settings.admin_groups & given))
    store.refresh()  # another server on the same state directory may have changed a role
    store.saw(name, given)
    return User(name, given | store.groups_of(name), admin=store.is_admin(name, given))


def host_name(header: str) -> str:
    """The host of a ``Host`` header, without port, lower-case, without a trailing dot."""
    host = header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else host
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.rstrip(".")


def host_and_port(authority: str) -> tuple[str, str]:
    """The host (as :func:`host_name` gives it) and the port of ``host[:port]``; the port is
    empty when it is not written."""
    text = authority.strip().lower()
    name = host_name(text)
    rest = text[text.find("]") + 1 :] if text.startswith("[") else text
    port = rest.rsplit(":", 1)[1] if rest.count(":") == 1 else ""
    return name, port


class SecurityMiddleware:
    """Refuses foreign ``Host`` headers (DNS rebinding), refuses requests that change something
    when another site sent them, and adds the security headers.

    The Host check is enforced when the server is bound to loopback (the allowed names are then
    the loopback names plus ``settings.allowed_hosts``) or when ``allowed_hosts`` is configured.

    Requests other than GET and HEAD carry no secret of their own (the identity comes from the
    reverse proxy, or there is none in standalone mode), so a page of another site could send
    them on behalf of a user. A browser says where such a request comes from (``Origin``,
    ``Sec-Fetch-Site``): it is refused unless it comes from this application, which means the
    same host *and the same port* (another program on this computer, or another service of
    the same machine, is another origin), and the same scheme when it is known.

    Behind a reverse proxy the application is known by the ``Host`` the proxy passes on, by
    ``X-Forwarded-Host`` when the proxy rewrites ``Host``, or by the public names of
    ``allowed_hosts``; its scheme by ``X-Forwarded-Proto``.
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        settings: Settings,
        metrics_token: str | None = None,
        proxy_secret: str | None = None,
    ) -> None:
        self.app = app
        #: What the proxy must present with every request (proxy mode only); None: nothing.
        self.proxy_secret = proxy_secret.encode() if proxy_secret and settings.auth_mode == "proxy" else None
        loopback = is_loopback_host(settings.host)
        self.allowed = (_LOOPBACK_NAMES | settings.allowed_hosts) if loopback else settings.allowed_hosts
        self.enforce = loopback or bool(settings.allowed_hosts)
        self.proxied = settings.auth_mode == "proxy"
        # a scraper with the metrics token asks by whatever name it knows the server under
        self.open_paths = _OPEN_PATHS | ({"/metrics"} if settings.metrics and metrics_token else set())
        self.public_names = settings.allowed_hosts

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

        async def refuse(code: str, message: str, status: int = 403) -> None:
            body = json.dumps({"error": {"code": code, "message": message}}).encode()
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        hosts = [value.decode("latin-1") for name, value in scope["headers"] if name == b"host"]
        known_host = len(hosts) == 1 and host_name(hosts[0]) in self.allowed
        if self.enforce and scope["path"] not in self.open_paths and not known_host:
            await refuse("forbidden_host", "forbidden host")
            return
        if self.proxy_secret is not None and scope["path"] not in self.open_paths:
            # not from the proxy: neither its identity headers nor what it says of the outside
            # can be believed
            given = [value for name, value in scope["headers"] if name == _SECRET_HEADER]
            if len(given) != 1 or not hmac.compare_digest(given[0], self.proxy_secret):
                await refuse("authentication_required", "authentication required", 401)
                return
        if scope["method"] not in _KEY_METHODS and any(
            name == b"authorization"
            and value[: len(_BEARER)].lower() == _BEARER.encode()
            and value[len(_BEARER) :].strip().startswith(KEY_PREFIX.encode())
            for name, value in scope["headers"]
        ):  # checked again where the identity is made: a key never writes
            await refuse("read_only_key", "a personal key only reads")
            return
        if scope["method"] not in _SAFE_METHODS and not self._same_site(scope, hosts):
            await refuse("cross_site_request", "cross-site request refused")
            return
        await self.app(scope, receive, send_with_headers)

    def _same_site(self, scope: Scope, hosts: list[str]) -> bool:
        """Whether a request that changes something comes from this application. A client that
        is not a browser sends neither header and is let through: it has no user to betray."""
        headers = {name: value.decode("latin-1") for name, value in scope["headers"]}
        site = headers.get(b"sec-fetch-site")
        if site is not None and site.strip().lower() not in ("same-origin", "none"):
            return False
        origin = headers.get(b"origin")
        if origin is None:
            return True
        scheme, separator, authority = origin.strip().lower().partition("://")
        if not separator or scheme not in _DEFAULT_PORTS or "/" in authority or "@" in authority:
            return False  # "null", or not an origin at all
        name, port = host_and_port(authority)
        port = port or _DEFAULT_PORTS[scheme]
        # what this application is called from outside: scheme (when known), names and ports
        own_scheme: str | None = str(scope.get("scheme", "http"))
        own = list(hosts)
        if self.proxied:  # the proxy is the only client: what it says of the outside is trusted
            forwarded = headers.get(b"x-forwarded-proto", "").split(",")[0].strip().lower()
            own_scheme = forwarded if forwarded in _DEFAULT_PORTS else None
            own += [h for h in headers.get(b"x-forwarded-host", "").split(",") if h.strip()]
            own += sorted(self.public_names)
        if own_scheme is not None and scheme != own_scheme:
            return False
        default = _DEFAULT_PORTS.get(own_scheme or scheme, "")
        return any((name, port) == (n, p or default) for n, p in map(host_and_port, own))
