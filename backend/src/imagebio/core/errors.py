"""Error types shared by every layer.

The message of an :class:`ImageBioError` is *public*: the API returns it to the client.
It must never contain an absolute server path (use the file name or the logical path),
a traceback or any other internal detail. Internal context goes to the log instead.
"""

from __future__ import annotations


class ImageBioError(Exception):
    """Base class. ``status`` is the HTTP status the API answers with, ``code`` a stable slug."""

    status: int = 500
    code: str = "internal_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.__class__.__doc__ or self.code)

    @property
    def public_message(self) -> str:
        return str(self.args[0])


class InvalidRequestError(ImageBioError):
    """The request is malformed (bad parameter, plane index out of range, ...)."""

    status = 400
    code = "invalid_request"


class AccessDeniedError(ImageBioError):
    """The path is outside the data roots the user may read."""

    status = 403
    code = "access_denied"


class NotFoundError(ImageBioError):
    """The file or directory does not exist."""

    status = 404
    code = "not_found"


class ConflictError(ImageBioError):
    """What the request was based on changed meanwhile (another administrator saved first)."""

    status = 409
    code = "conflict"


class UnsupportedFormatError(ImageBioError):
    """No reader can open this file."""

    status = 415
    code = "unsupported_format"


class CorruptFileError(ImageBioError):
    """The file is invalid, truncated or still being written."""

    status = 422
    code = "corrupt_file"
