# server/mw.py
"""Turn unguarded bad input into ordinary DAV errors.

WsgiDAV 4.3.3 lets two kinds of bad input raise an exception that escapes all
the way out, so ``ErrorPrinter`` answers ``500 Internal Server Error`` *and*
echoes the exception text into the response body:

* a malformed ``Authorization: Basic ...`` header (invalid base64, a payload
  without ``:``, an empty payload, or the scheme without a value)
* an ``OSError`` raised by the filesystem layer, e.g. for a file name that is
  longer than the filesystem allows

:class:`RequestGuard` sits directly inside ``ErrorPrinter``: it normalises the
authorization header (so the authenticator answers a clean 401) and converts
filesystem ``OSError``\\s into :class:`~wsgidav.dav_error.DAVError`, keeping the
details in the log instead of the response.
"""

from __future__ import annotations

import base64
import binascii
import errno
import logging

# Importing wsgidav.util first is required: wsgidav.dav_error imports it back.
from wsgidav import util  # noqa: F401
from wsgidav.dav_error import (
    DAVError,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_FORBIDDEN,
    HTTP_INSUFFICIENT_STORAGE,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_REQUEST_URI_TOO_LONG,
)
from wsgidav.mw.base_mw import BaseMiddleware

logger = logging.getLogger(__name__)

__all__ = ["RequestGuard"]

#: errno -> HTTP status for errors coming from the filesystem layer.
_OS_ERROR_STATUS = {
    errno.ENOSPC: HTTP_INSUFFICIENT_STORAGE,
    errno.ENAMETOOLONG: HTTP_REQUEST_URI_TOO_LONG,
    errno.EACCES: HTTP_FORBIDDEN,
    errno.EPERM: HTTP_FORBIDDEN,
    errno.ELOOP: HTTP_FORBIDDEN,
    errno.ENOENT: HTTP_NOT_FOUND,
    errno.ENOTDIR: HTTP_CONFLICT,
    errno.EINVAL: HTTP_BAD_REQUEST,
}

#: Client facing text - deliberately generic, the details go to the log.
_OS_ERROR_MESSAGE = {
    HTTP_INSUFFICIENT_STORAGE: "Insufficient storage space.",
    HTTP_REQUEST_URI_TOO_LONG: "The requested path or file name is too long.",
    HTTP_FORBIDDEN: "The requested path is not allowed.",
    HTTP_NOT_FOUND: "The requested resource does not exist.",
    HTTP_CONFLICT: "The requested path conflicts with an existing resource.",
    HTTP_BAD_REQUEST: "The requested path is invalid.",
}


class RequestGuard(BaseMiddleware):
    """Normalise bad input before it can become a 500."""

    def is_disabled(self):
        return False

    @staticmethod
    def sanitize_basic_auth(environ) -> None:
        """Make sure WsgiDAV only ever sees a usable ``Basic`` header."""
        header = environ.get("HTTP_AUTHORIZATION")
        if not header:
            return

        scheme, _separator, payload = header.partition(" ")
        if scheme.lower() != "basic":
            return

        payload = payload.strip()
        decoded = ""
        if payload:
            try:
                decoded = base64.b64decode(payload, validate=True).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                decoded = ""

        if ":" in decoded:
            # Re-emit canonically; this also accepts a lower case scheme.
            environ["HTTP_AUTHORIZATION"] = f"Basic {payload}"
        else:
            # Present the request as anonymous: the authenticator then answers
            # a clean 401 instead of raising inside base64/split.
            environ.pop("HTTP_AUTHORIZATION", None)

    @staticmethod
    def _os_error_to_dav(exc: OSError) -> DAVError:
        status = _OS_ERROR_STATUS.get(exc.errno, HTTP_INTERNAL_ERROR)
        message = _OS_ERROR_MESSAGE.get(status, "Filesystem error.")
        return DAVError(status, message)

    def __call__(self, environ, start_response):
        self.sanitize_basic_auth(environ)

        # The inner app may be a generator (GET streams the body), so the
        # exception can surface while iterating - mirror what ErrorPrinter does.
        app_iter = None
        try:
            app_iter = self.next_app(environ, start_response)
            for chunk in app_iter:
                yield chunk
        except OSError as exc:
            logger.warning(
                "filesystem error for %s %s: %s",
                environ.get("REQUEST_METHOD"),
                environ.get("PATH_INFO"),
                exc,
            )
            raise self._os_error_to_dav(exc) from exc
        finally:
            close = getattr(app_iter, "close", None)
            if callable(close):
                close()
