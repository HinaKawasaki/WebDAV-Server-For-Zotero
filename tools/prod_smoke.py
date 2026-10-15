#!/usr/bin/env python3
"""Production smoke test for a deployed Zotero WebDAV server.

Drives the running server over HTTP - by default through the SSH tunnel, i.e.
the same path Zotero uses - and checks authentication, per-user isolation, the
DAV method matrix, locking, dead properties, copy/move, path encoding,
throughput and concurrency.

All artefacts live under ``/<user>/__selftest/`` and are removed afterwards.
Configuration comes from the environment, so no credential is stored here:

    WEBDAV_URL            base URL                (default http://127.0.0.1:8000)
    WEBDAV_USER           account to test         (required)
    WEBDAV_PASSWORD       its password            (required)
    WEBDAV_OTHER_USER     a second account        (optional; enables isolation checks)
    WEBDAV_OTHER_PASSWORD its password            (optional)
    SMOKE_BIG_MB          size of the upload test (default 10, 0 disables)
    SMOKE_TIMEOUT         per-request timeout, s  (default 60)

Exit code 0 when everything passes, 1 otherwise.

    WEBDAV_USER=hello WEBDAV_PASSWORD=... python3 tools/prod_smoke.py
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import http.client
import os
import sys
import time
from urllib.parse import quote, urlsplit

DEFAULT_URL = "http://127.0.0.1:8000"
BIG_MB = int(os.environ.get("SMOKE_BIG_MB", "10"))
TIMEOUT = int(os.environ.get("SMOKE_TIMEOUT", "60"))

RESULTS: list = []
FAILED: list = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


class Client:
    def __init__(self, base_url, user, password):
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.scheme = parts.scheme or "http"
        self.prefix = parts.path.rstrip("/")
        self.user = user
        self.password = password

    def call(self, method, path, *, user=None, password=None, auth=True,
             headers=None, body=None, raw_auth=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT)
        head = dict(headers or {})
        if raw_auth is not None:
            head["Authorization"] = raw_auth
        elif auth:
            credentials = f"{user or self.user}:{password if password is not None else self.password}"
            head["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
        if body is not None:
            head.setdefault("Content-Length", str(len(body)))
        try:
            conn.request(method, self.prefix + path, body=body, headers=head)
            response = conn.getresponse()
            payload = response.read()
            info = {k.lower(): v for k, v in response.getheaders()}
            return response.status, info, payload
        finally:
            conn.close()

    def expect(self, name, method, path, want, **kwargs):
        status, _info, body = self.call(method, path, **kwargs)
        wanted = want if isinstance(want, (tuple, list, set)) else (want,)
        ok = status in wanted
        record(name, ok, f"got={status} want={'/'.join(str(w) for w in wanted)}"
               + (f" body={body[:60]!r}" if not ok else ""))
        return status


def main() -> int:
    base_url = os.environ.get("WEBDAV_URL", DEFAULT_URL)
    user = os.environ.get("WEBDAV_USER")
    password = os.environ.get("WEBDAV_PASSWORD")
    if not user or not password:
        print("error: WEBDAV_USER and WEBDAV_PASSWORD are required", file=sys.stderr)
        return 2

    other_user = os.environ.get("WEBDAV_OTHER_USER")
    other_password = os.environ.get("WEBDAV_OTHER_PASSWORD")
    client = Client(base_url, user, password)
    base = f"/{user}"
    self_test = f"{base}/__selftest"

    print(f"target      : {base_url}{base}/")
    print(f"big upload  : {BIG_MB} MiB" if BIG_MB else "big upload  : disabled")
    print()

    client.call("DELETE", self_test)  # clean slate, result ignored

    print("########## A. authentication ##########")
    client.expect("A1 no credentials -> 401", "PROPFIND", f"{base}/", 401, auth=False)
    client.expect("A2 wrong password -> 401", "PROPFIND", f"{base}/", 401, password="definitely-wrong")
    client.expect("A3 empty password -> 401", "PROPFIND", f"{base}/", 401, password="")
    client.expect("A4 malformed Authorization -> 401", "PROPFIND", f"{base}/", 401,
                  raw_auth="Basic @@@not-base64@@@")
    client.expect("A5 scheme only -> 401", "PROPFIND", f"{base}/", 401, raw_auth="Basic")
    client.expect("A6 correct credentials -> 207", "PROPFIND", f"{base}/", 207)

    print("\n########## B. isolation ##########")
    client.expect("B1 shared root / -> 403", "PROPFIND", "/", 403)
    client.expect("B2 plain .. traversal -> 403", "PROPFIND", f"{base}/../x/", 403)
    client.expect("B3 encoded .. traversal -> 403", "PROPFIND", f"{base}/%2e%2e/x/", 403)
    client.expect("B4 prefix sibling -> 403", "PROPFIND", f"/{user}_evil/", 403)
    if other_user and other_password:
        client.expect("B5 the other account can reach its own root -> 207",
                      "PROPFIND", f"/{other_user}/", 207,
                      user=other_user, password=other_password)
    if other_user:
        client.expect("B5b other user's prefix -> 403", "PROPFIND", f"/{other_user}/", 403)
        client.expect("B6 GET other user -> 403", "GET", f"/{other_user}/x", 403)
        client.expect("B7 PUT other user -> 403", "PUT", f"/{other_user}/x", 403, body=b"x")
    else:
        print("(skipped: set WEBDAV_OTHER_USER to enable the cross-user checks)")

    print("\n########## C. DAV method matrix ##########")
    client.expect("C1 MKCOL -> 201", "MKCOL", f"{self_test}/", 201)
    client.expect("C2 MKCOL again -> 405", "MKCOL", f"{self_test}/", 405)
    client.expect("C3 PUT -> 201", "PUT", f"{self_test}/a.txt", 201, body=b"smoke")
    status, _info, body = client.call("GET", f"{self_test}/a.txt")
    record("C4 GET returns the body", status == 200 and body == b"smoke", f"status={status}")
    status, info, _ = client.call("HEAD", f"{self_test}/a.txt")
    record("C5 HEAD reports Content-Length",
           status == 200 and info.get("content-length") == "5",
           f"status={status} len={info.get('content-length')}")
    client.expect("C6 PUT overwrite -> 204", "PUT", f"{self_test}/a.txt", 204, body=b"smoke2")
    status, _info, body = client.call("PROPFIND", f"{self_test}/", headers={"Depth": "1"})
    record("C7 PROPFIND Depth:1 lists the file", status == 207 and b"a.txt" in body,
           f"status={status}")
    status, info, _ = client.call("OPTIONS", f"{base}/")
    record("C8 OPTIONS advertises DAV", status == 200 and "1" in info.get("dav", ""),
           f"status={status} dav={info.get('dav')}")

    print("\n########## D. dead properties ##########")
    set_body = (b'<?xml version="1.0" encoding="utf-8" ?><D:propertyupdate xmlns:D="DAV:" '
                b'xmlns:X="http://example.com/smoke"><D:set><D:prop><X:tag>smoke-ok</X:tag>'
                b'</D:prop></D:set></D:propertyupdate>')
    client.expect("D1 PROPPATCH -> 207", "PROPPATCH", f"{self_test}/a.txt", 207, body=set_body,
                  headers={"Content-Type": "application/xml"})
    find_body = (b'<?xml version="1.0" encoding="utf-8" ?><D:propfind xmlns:D="DAV:" '
                 b'xmlns:X="http://example.com/smoke"><D:prop><X:tag/></D:prop></D:propfind>')
    status, _info, body = client.call("PROPFIND", f"{self_test}/a.txt", body=find_body,
                                      headers={"Depth": "0", "Content-Type": "application/xml"})
    record("D2 dead property readable back", status == 207 and b"smoke-ok" in body,
           f"status={status}")

    print("\n########## E. locking ##########")
    lock_body = (b'<?xml version="1.0" encoding="utf-8" ?><D:lockinfo xmlns:D="DAV:">'
                 b'<D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype>'
                 b'<D:owner><D:href>smoke</D:href></D:owner></D:lockinfo>')
    status, info, _ = client.call("LOCK", f"{self_test}/a.txt", body=lock_body,
                                  headers={"Timeout": "Second-120",
                                           "Content-Type": "application/xml"})
    token = info.get("lock-token", "").strip("<>")
    record("E1 LOCK returns a token", status == 200 and bool(token), f"status={status}")
    if token:
        client.expect("E2 PUT without token -> 423", "PUT", f"{self_test}/a.txt", 423, body=b"no")
        client.expect("E3 PUT with token -> 204", "PUT", f"{self_test}/a.txt", 204, body=b"yes",
                      headers={"If": f"(<{token}>)"})
        client.expect("E4 UNLOCK -> 204", "UNLOCK", f"{self_test}/a.txt", 204,
                      headers={"Lock-Token": f"<{token}>"})

    print("\n########## F. COPY / MOVE ##########")
    client.expect("F1 MKCOL sub -> 201", "MKCOL", f"{self_test}/sub/", 201)
    client.expect("F2 COPY -> 201", "COPY", f"{self_test}/a.txt", 201,
                  headers={"Destination": f"{base_url}{self_test}/sub/copy.txt"})
    client.expect("F3 MOVE -> 201", "MOVE", f"{self_test}/sub/copy.txt", 201,
                  headers={"Destination": f"{base_url}{self_test}/sub/moved.txt"})
    if other_user:
        client.expect("F4 COPY into another user -> 403", "COPY", f"{self_test}/a.txt", 403,
                      headers={"Destination": f"{base_url}/{other_user}/hacked.txt"})
        client.expect("F5 MOVE into another user -> 403", "MOVE", f"{self_test}/a.txt", 403,
                      headers={"Destination": f"{base_url}/{other_user}/hacked.txt"})
        client.expect("F6 source survived the cross-user attempts", "GET", f"{self_test}/a.txt", 200)

    print("\n########## G. names and paths ##########")
    unicode_path = f"{self_test}/{quote('中文 文件 #1.txt')}"
    client.expect("G1 PUT unicode/space/# -> 201", "PUT", unicode_path, 201, body=b"u")
    client.expect("G2 GET it back -> 200", "GET", unicode_path, 200)
    client.expect("G3 MKCOL a -> 201", "MKCOL", f"{self_test}/a/", 201)
    client.expect("G3b MKCOL a/b -> 201", "MKCOL", f"{self_test}/a/b/", 201)
    client.expect("G3c MKCOL a/b/c without parent -> 409", "MKCOL", f"{self_test}/x/y/z/", 409)
    client.expect("G4 PUT into missing parent -> 409", "PUT", f"{self_test}/nope/x", 409, body=b"x")
    status, _info, _ = client.call("PUT", f"{self_test}/{'n' * 300}.txt", body=b"x")
    record("G5 over-long name is not a server error", status < 500, f"status={status}")

    print("\n########## H. throughput and concurrency ##########")
    if BIG_MB:
        payload = bytes(range(256)) * (BIG_MB * 1024 * 1024 // 256)
        digest = hashlib.sha256(payload).hexdigest()
        started = time.time()
        status, _info, _ = client.call("PUT", f"{self_test}/big.bin", body=payload)
        upload = len(payload) / max(time.time() - started, 1e-6) / 1e6
        record(f"H1 PUT {BIG_MB} MiB -> 201", status == 201, f"status={status} {upload:.1f} MB/s")
        started = time.time()
        status, _info, body = client.call("GET", f"{self_test}/big.bin")
        download = len(body) / max(time.time() - started, 1e-6) / 1e6
        record(f"H2 GET {BIG_MB} MiB integrity",
               status == 200 and hashlib.sha256(body).hexdigest() == digest,
               f"status={status} {download:.2f} MB/s")
        if upload > 5 * download and download < 2:
            print(f"     note: download is {upload / max(download, 1e-6):.0f}x slower than upload - "
                  f"typical for a capped cloud egress")
    else:
        print("(skipped: SMOKE_BIG_MB=0)")

    def propfind(_):
        return client.call("PROPFIND", f"{self_test}/")[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(propfind, range(16)))
    record("H3 16 concurrent PROPFIND all 207", all(c == 207 for c in codes), f"{set(codes)}")

    def put_one(index):
        return client.call("PUT", f"{self_test}/conc-{index}.bin", body=b"x" * (1024 * 1024))[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(put_one, range(8)))
    record("H4 8 concurrent 1 MiB PUT all 201", all(c == 201 for c in codes), f"{set(codes)}")

    print("\n########## I. cleanup ##########")
    client.expect("I1 DELETE selftest tree -> 204", "DELETE", f"{self_test}/", 204)
    client.expect("I2 selftest gone -> 404", "PROPFIND", f"{self_test}/", 404)
    client.expect("I3 user root still reachable -> 207", "PROPFIND", f"{base}/", 207)

    print("\n" + "=" * 62)
    print(f"total {len(RESULTS)}   passed {len(RESULTS) - len(FAILED)}   failed {len(FAILED)}")
    for name in FAILED:
        print("  FAILED:", name)
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
