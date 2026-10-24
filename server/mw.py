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

When the error happens *after* the response body has started (a GET streaming a
large file, say), the status line is already on the wire and cannot be changed.
In that case the guard logs the error and ends the body instead of letting the
exception escape as a traceback; the client notices the truncation through the
declared ``Content-Length``.
"""

from __future__ import annotations

import base64
import binascii
import errno
import logging
import math

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

#: ``environ`` key the domain controller uses to tell this middleware how long
#: a throttled client should wait before trying again.
RETRY_AFTER_ENVIRON_KEY = "server.mw.retry_after"

#: Set by the domain controller when the database itself is unavailable.  The
#: request is answered with 503 instead of a misleading 401.
UPSTREAM_UNAVAILABLE_ENVIRON_KEY = "server.mw.upstream_unavailable"

#: Client-facing body for the 503 above (the inner 401 page is discarded).
_UNAVAILABLE_BODY = b"Authentication backend unavailable. Please retry later.\n"

#: Fallback wait hint when the domain controller does not provide one.
_UNAVAILABLE_RETRY_AFTER = 5

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

        # RFC 7617 uses one SP between scheme and token, but be tolerant of
        # tabs / repeated spaces, and of tokens folded over several lines
        # (obs-fold): strip all whitespace before decoding.
        parts = header.strip().split(None, 1)
        if not parts or parts[0].lower() != "basic":
            return
        payload = "".join(parts[1].split()) if len(parts) > 1 else ""
        decoded = ""
        canonical_payload = payload
        if payload:
            # Some clients omit the base64 padding (RFC 7617 does not require
            # it to be preserved), so restore it before decoding.
            padded = payload + "=" * (-len(payload) % 4)
            try:
                raw = base64.b64decode(padded, validate=True)
            except (binascii.Error, ValueError):
                raw = None
            if raw is not None:
                try:
                    decoded = raw.decode("utf-8")
                except UnicodeDecodeError:
                    # RFC 7617 allows ISO-8859-1 when the client declares no
                    # charset.  WsgiDAV itself decodes as UTF-8, so re-emit the
                    # credentials canonically instead of letting it raise.
                    decoded = raw.decode("latin-1")
                # Re-emit with canonical padding: WsgiDAV's own base64 decoder
                # rejects an unpadded payload with binascii.Error.
                canonical_payload = base64.b64encode(
                    decoded.encode("utf-8")
                ).decode("ascii")

        if ":" in decoded:
            # Re-emit canonically; this also accepts a lower case scheme.
            environ["HTTP_AUTHORIZATION"] = f"Basic {canonical_payload}"
        else:
            # Present the request as anonymous: the authenticator then answers
            # a clean 401 instead of raising inside base64/split.
            environ.pop("HTTP_AUTHORIZATION", None)

    @staticmethod
    def _os_error_to_dav(exc) -> DAVError:
        if isinstance(exc, ValueError):
            # e.g. "embedded null byte": a bad path, not a server fault.
            return DAVError(HTTP_BAD_REQUEST, _OS_ERROR_MESSAGE[HTTP_BAD_REQUEST])
        status = _OS_ERROR_STATUS.get(exc.errno, HTTP_INTERNAL_ERROR)
        message = _OS_ERROR_MESSAGE.get(status, "Filesystem error.")
        return DAVError(status, message)

    def __call__(self, environ, start_response):
        self.sanitize_basic_auth(environ)
        #: set by the wrapper below when the inner app answers 401 while the
        #: authentication backend is down
        unavailable = {"status": False}

        def start_response_with_extras(status, headers, exc_info=None):
            if environ.get(UPSTREAM_UNAVAILABLE_ENVIRON_KEY):
                unavailable["status"] = True
                seconds = max(
                    1,
                    math.ceil(
                        float(
                            environ.get(
                                UPSTREAM_UNAVAILABLE_ENVIRON_KEY,
                                _UNAVAILABLE_RETRY_AFTER,
                            )
                            or _UNAVAILABLE_RETRY_AFTER
                        )
                    ),
                )
                return start_response(
                    "503 Service Unavailable",
                    [
                        ("Content-Type", "text/plain; charset=utf-8"),
                        ("Content-Length", str(len(_UNAVAILABLE_BODY))),
                        ("Retry-After", str(seconds)),
                    ],
                    exc_info,
                )
            if status.startswith("401"):
                # Tell a throttled client when it may try again.  The hint is
                # set by the domain controller on ``environ`` while the inner
                # app runs.
                retry_after = environ.get(RETRY_AFTER_ENVIRON_KEY)
                if retry_after:
                    headers = list(headers)
                    if not any(name.lower() == "retry-after" for name, _ in headers):
                        seconds = max(1, math.ceil(float(retry_after)))
                        headers.append(("Retry-After", str(seconds)))
            return start_response(status, headers, exc_info)

        # The inner app may be a generator (GET streams the body), so the
        # exception can surface while iterating - mirror what ErrorPrinter does.
        app_iter = None
        sent = False
        try:
            app_iter = self.next_app(environ, start_response_with_extras)
            body_sent = False
            for chunk in app_iter:
                if unavailable["status"]:
                    # The inner app produced a 401 page; answer 503 instead.
                    if not body_sent:
                        body_sent = True
                        yield _UNAVAILABLE_BODY
                        sent = True
                    continue
                yield chunk
                sent = True
            if unavailable["status"] and not body_sent:
                yield _UNAVAILABLE_BODY
                sent = True
        except (OSError, ValueError) as exc:
            logger.warning(
                "filesystem error for %s %s: %s",
                environ.get("REQUEST_METHOD"),
                environ.get("PATH_INFO"),
                exc,
            )
            if sent:
                # The status line and headers are already on the wire, so the
                # error can no longer be turned into a 4xx/5xx response.
                # End the body instead of letting the exception escape as a
                # traceback: the client sees a short/incomplete response (it
                # knows from Content-Length) and the details stay in the log.
                logger.error(
                    "response already started for %s %s - truncating the body",
                    environ.get("REQUEST_METHOD"),
                    environ.get("PATH_INFO"),
                )
                return
            raise self._os_error_to_dav(exc) from exc
        finally:
            close = getattr(app_iter, "close", None)
            if callable(close):
                close()
