# tests/test_mw.py
import base64
import errno
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import conf, db, passwords
from server.mw import RequestGuard
from tests.test_users import fake_connect


def make_environ(method, path, *, credentials=None, raw_auth=None, body=b""):
    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_HOST": "localhost",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if raw_auth is not None:
        environ["HTTP_AUTHORIZATION"] = raw_auth
    elif credentials:
        user, password = credentials
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        environ["HTTP_AUTHORIZATION"] = f"Basic {token}"
    return environ


def wsgi_request(app, environ):
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    captured["body"] = b"".join(app(environ, start_response))
    captured["code"] = int(captured["status"].split(" ", 1)[0])
    return captured


class SanitizeBasicAuthTests(unittest.TestCase):
    def _sanitize(self, header):
        environ = {"HTTP_AUTHORIZATION": header} if header is not None else {}
        RequestGuard.sanitize_basic_auth(environ)
        return environ.get("HTTP_AUTHORIZATION")

    def test_valid_header_is_kept(self):
        token = base64.b64encode(b"alice:pw").decode()
        self.assertEqual(self._sanitize(f"Basic {token}"), f"Basic {token}")

    def test_lowercase_scheme_is_normalised(self):
        token = base64.b64encode(b"alice:pw").decode()
        self.assertEqual(self._sanitize(f"basic {token}"), f"Basic {token}")

    def test_invalid_base64_is_dropped(self):
        self.assertIsNone(self._sanitize("Basic @@@not-base64@@@"))

    def test_payload_without_colon_is_dropped(self):
        token = base64.b64encode(b"nocolon").decode()
        self.assertIsNone(self._sanitize(f"Basic {token}"))

    def test_empty_payload_is_dropped(self):
        self.assertIsNone(self._sanitize("Basic "))
        self.assertIsNone(self._sanitize("Basic"))

    def test_other_scheme_is_left_alone(self):
        self.assertEqual(self._sanitize("Bearer abc.def"), "Bearer abc.def")

    def test_missing_header_is_a_no_op(self):
        self.assertIsNone(self._sanitize(None))


class OsErrorMappingTests(unittest.TestCase):
    def test_errno_mapping(self):
        cases = {
            errno.ENOSPC: 507,
            errno.ENAMETOOLONG: 414,
            errno.EACCES: 403,
            errno.EPERM: 403,
            errno.ENOENT: 404,
            errno.ENOTDIR: 409,
        }
        for code, expected in cases.items():
            error = RequestGuard._os_error_to_dav(OSError(code, "boom"))
            self.assertEqual(error.value, expected, code)

    def test_unknown_errno_becomes_500(self):
        error = RequestGuard._os_error_to_dav(OSError(12345, "weird"))
        self.assertEqual(error.value, 500)

    def test_message_does_not_leak_details(self):
        error = RequestGuard._os_error_to_dav(OSError(errno.ENAMETOOLONG, "boom"))
        self.assertNotIn("boom", error.get_user_info())
        self.assertIsNone(error.src_exception)


class RequestGuardEndToEndTests(unittest.TestCase):
    """Drive the real WSGI stack, no network involved."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.share = Path(self._tmp.name) / "share"
        self.share.mkdir()
        self.store = {"alice": passwords.hash_password("pw")}
        for patcher in (
            mock.patch.object(conf, "SHARE_DIR", self.share),
            mock.patch.object(db, "connect", fake_connect(self.store)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

        from server.app import create_dav_app

        self.app = create_dav_app(conf.build_config())

    def test_malformed_authorization_gets_a_clean_401(self):
        headers = [
            "Basic @@@not-base64@@@",
            "Basic",
            "Basic ",
            "Basic " + base64.b64encode(b"nocolon").decode(),
        ]
        for header in headers:
            result = wsgi_request(
                self.app, make_environ("PROPFIND", "/alice/", raw_auth=header)
            )
            self.assertEqual(result["code"], 401, header)
            self.assertNotIn(b"Source exception", result["body"], header)
            self.assertNotIn(b"Traceback", result["body"], header)

    def test_overlong_name_is_a_414_not_a_500(self):
        long_path = "/alice/" + "n" * 300 + ".txt"
        result = wsgi_request(
            self.app,
            make_environ("PUT", long_path, credentials=("alice", "pw"), body=b"x"),
        )
        self.assertEqual(result["code"], 414)
        self.assertNotIn(b"OSError", result["body"])
        self.assertNotIn(b"Source exception", result["body"])

    def test_normal_requests_still_work(self):
        result = wsgi_request(
            self.app,
            make_environ("PUT", "/alice/ok.txt", credentials=("alice", "pw"), body=b"hi"),
        )
        self.assertEqual(result["code"], 201)
        result = wsgi_request(
            self.app, make_environ("PROPFIND", "/alice/", credentials=("alice", "pw"))
        )
        self.assertEqual(result["code"], 207)

    def test_throttled_login_answers_with_retry_after(self):
        # exhaust the default attempt budget with wrong passwords
        for _ in range(10):
            wsgi_request(
                self.app,
                make_environ("PROPFIND", "/alice/", credentials=("alice", "nope")),
            )
        result = wsgi_request(
            self.app, make_environ("PROPFIND", "/alice/", credentials=("alice", "pw"))
        )
        self.assertEqual(result["code"], 401)
        headers = {name.lower(): value for name, value in result["headers"]}
        self.assertIn("retry-after", headers)
        self.assertGreaterEqual(int(headers["retry-after"]), 1)

    def test_wrong_password_is_still_401(self):
        result = wsgi_request(
            self.app, make_environ("PROPFIND", "/alice/", credentials=("alice", "nope"))
        )
        self.assertEqual(result["code"], 401)

    def test_version_banner_is_suppressed(self):
        from wsgidav import util as wsgidav_util

        self.assertEqual(wsgidav_util.public_wsgidav_info, "WsgiDAV")


if __name__ == "__main__":
    unittest.main()
