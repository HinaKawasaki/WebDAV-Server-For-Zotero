# server/start_server.py
"""Start the WebDAV server.

From the project root::

    python -m server.start_server

(``python server/start_server.py`` works as well.)
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):
    # Support `python server/start_server.py` in addition to `-m server.start_server`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cheroot import wsgi

from server import conf, db, users
from server.app import create_dav_app

logger = logging.getLogger("server.start_server")


def setup_logging() -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    if conf.LOG_FILE:
        path = Path(conf.LOG_FILE).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=getattr(logging, conf.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def prepare() -> None:
    """Validate the configuration and make sure database and storage exist."""
    conf.validate()
    conf.SHARE_DIR.mkdir(parents=True, exist_ok=True)
    settings = conf.db_settings()
    db.ping(settings)
    users.ensure_schema(settings)
    schema = users.check_schema(settings)
    for warning in schema["warnings"]:
        logger.warning("%s", warning)


def main(argv=None) -> int:
    setup_logging()

    try:
        prepare()
    except (conf.ConfigurationError, db.DatabaseError) as exc:
        logger.error("startup failed: %s", exc)
        return 1

    logger.info("effective configuration: %s", conf.redacted())

    app = create_dav_app(conf.build_config())
    server = wsgi.Server(
        bind_addr=(conf.HOST, conf.PORT),
        wsgi_app=app,
        numthreads=conf.NUM_THREADS,
        request_queue_size=conf.REQUEST_QUEUE_SIZE,
        timeout=conf.SOCKET_TIMEOUT,
        shutdown_timeout=conf.SHUTDOWN_TIMEOUT,
    )

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        # Do NOT call server.stop() here: this handler runs in the main thread
        # while cheroot may hold one of its internal locks, which deadlocks.
        # Waking the main thread and stopping from there is safe.
        logger.info("received signal %s - shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    layout = "per-user /<username>/" if conf.PER_USER_STORAGE else "shared root"
    logger.info(
        "serving %s (%s) on http://%s:%s/",
        conf.SHARE_DIR,
        layout,
        conf.HOST,
        conf.PORT,
    )

    # Run the blocking cheroot loop in a worker so the main thread stays free
    # to handle signals and to perform an orderly shutdown.
    worker = threading.Thread(target=server.start, name="cheroot", daemon=True)
    worker.start()

    try:
        while not stop_event.wait(1.0):
            if not worker.is_alive():
                logger.error("the server loop exited unexpectedly")
                return 1
    except KeyboardInterrupt:
        logger.info("interrupted")

    logger.info("stopping server")
    with contextlib.suppress(Exception):
        server.stop()
    worker.join(timeout=conf.SHUTDOWN_TIMEOUT + 5)
    if worker.is_alive():
        logger.warning(
            "server loop did not stop within %ss", conf.SHUTDOWN_TIMEOUT + 5
        )
    logger.info("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
