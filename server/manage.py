# server/manage.py
"""Command line administration for the Zotero WebDAV server.

Examples::

    python -m server.manage initdb --admin-user admin
    python -m server.manage adduser alice
    python -m server.manage passwd alice
    python -m server.manage deluser alice --purge
    python -m server.manage listusers
    python -m server.manage adopt-shared alice --from ./zotero --apply

Passwords are read from ``--password``, from the ``WEBDAV_NEW_PASSWORD``
environment variable, or prompted for interactively.  There is no built-in
default account.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import authcache, conf, davstate, db, migration, passwords, storage, users

logger = logging.getLogger("server.manage")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _prompt_password(args) -> str:
    if getattr(args, "generate", False):
        password = passwords.generate_password()
        print(f"[*] generated password for {args.username!r}: {password}")
        return password
    if getattr(args, "password", None):
        return args.password
    env_password = os.environ.get("WEBDAV_NEW_PASSWORD")
    if env_password:
        return env_password
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("error: passwords do not match")
    return first


def _settings() -> dict:
    conf.validate()
    return conf.db_settings()


def _webdav_url(username: str) -> str:
    return f"http://<host>:{conf.PORT}/{storage.directory_name(username)}/"


def _describe_layout() -> None:
    print(f"[*] storage base directory: {conf.SHARE_DIR}")
    if conf.PER_USER_STORAGE:
        print("[*] layout: one directory per account, served at /<username>/")
    else:
        print("[!] layout: WEBDAV_PER_USER_STORAGE is disabled (shared root)")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _invalidate_auth_cache() -> None:
    """Make running servers forget cached password verifications."""
    if not authcache.invalidate(conf.AUTH_CACHE_EPOCH_FILE):
        print(
            "[!] could not touch the auth cache generation file; a password "
            "change may take up to WEBDAV_AUTH_CACHE_SECONDS to apply"
        )


def cmd_initdb(args) -> int:
    settings = _settings()
    Path(conf.SHARE_DIR).mkdir(parents=True, exist_ok=True)
    users.ensure_schema(settings)
    schema = users.check_schema(settings)
    print(
        f"[*] user table ready in {settings['database']}.{settings['table']} "
        f"(password column: {schema['password_column']}, "
        f"hash length: {schema['required_password_length']})"
    )
    for warning in schema["warnings"]:
        print(f"[!] {warning}")
    for note in schema.get("notes", []):
        print(f"[*] {note}")

    state = davstate.ensure_schema(settings, conf.PROPERTIES_TABLE, conf.LOCKS_TABLE)
    print(
        f"[*] DAV properties: table={state['properties_table']} "
        f"({'persistent' if state['properties_ready'] else 'in memory only'}), "
        f"locks: table={state['locks_table']} "
        f"({'persistent' if state['locks_ready'] else 'in memory only'})"
    )
    for warning in state["warnings"]:
        print(f"[!] {warning}")

    _describe_layout()

    if args.admin_user:
        args.username = args.admin_user
        password = _prompt_password(args)
        try:
            users.add_user(settings, args.admin_user, password)
        except users.UserExistsError:
            print(f"[!] user {args.admin_user!r} already exists - password unchanged")
        else:
            directory = storage.ensure_user_directory(conf.SHARE_DIR, args.admin_user)
            _invalidate_auth_cache()
            print(f"[*] created admin user {args.admin_user!r}")
            print(f"[*] storage directory: {directory}")
            print(f"[*] Zotero WebDAV URL: {_webdav_url(args.admin_user)}")
    elif users.count_users(settings) == 0:
        print(
            "[!] no users exist yet. Create one with:\n"
            "    python -m server.manage adduser <name>"
        )
    return 0


def cmd_adduser(args) -> int:
    settings = _settings()
    Path(conf.SHARE_DIR).mkdir(parents=True, exist_ok=True)
    users.ensure_schema(settings)
    password = _prompt_password(args)
    try:
        users.add_user(settings, args.username, password)
    except users.UserExistsError as exc:
        print(f"[!] {exc}")
        return 1
    directory = storage.ensure_user_directory(conf.SHARE_DIR, args.username)
    _invalidate_auth_cache()
    print(f"[*] created user {args.username!r}")
    print(f"[*] storage directory: {directory}")
    print(f"[*] Zotero WebDAV URL: {_webdav_url(args.username)}")
    return 0


def cmd_passwd(args) -> int:
    settings = _settings()
    password = _prompt_password(args)
    try:
        users.set_password(settings, args.username, password)
    except users.UserNotFoundError as exc:
        print(f"[!] {exc}")
        return 1
    _invalidate_auth_cache()
    print(f"[*] password updated for {args.username!r}")
    return 0


def cmd_deluser(args) -> int:
    settings = _settings()
    if not users.delete_user(settings, args.username):
        print(f"[!] user {args.username!r} does not exist")
        return 1
    _invalidate_auth_cache()
    print(f"[*] deleted user {args.username!r}")

    directory = storage.user_directory(conf.SHARE_DIR, args.username)
    if not directory.exists():
        return 0
    if args.purge:
        shutil.rmtree(directory)
        print(f"[*] removed storage directory {directory}")
    else:
        print(f"[*] storage directory kept: {directory} (use --purge to delete)")
    return 0


def cmd_listusers(args) -> int:
    settings = _settings()
    names = users.list_users(settings)
    if not names:
        print("(no users)")
        return 0
    width = max(len(name) for name in names)
    for name in names:
        print(f"{name:<{width}}  /{storage.directory_name(name)}/")
    return 0


def cmd_adopt_shared(args) -> int:
    """Move the contents of a legacy shared root into one user's directory."""
    settings = _settings()
    users.ensure_schema(settings)

    username = args.username
    if users.get_stored_password(settings, username) is None:
        print(f"[!] user {username!r} does not exist - create it first")
        return 1

    destination = storage.user_directory(conf.SHARE_DIR, username)
    try:
        plan = migration.plan_migration(args.source, destination, copy=args.copy)
    except ValueError as exc:
        print(f"[!] {exc}")
        return 1

    if not plan.entries:
        print("[*] nothing to migrate")
        return 0
    if plan.conflicts and not args.force:
        print("[!] destination already contains: " + ", ".join(plan.conflicts))
        print("    re-run with --force to overwrite them")
        return 1

    print(f"[*] account     : {username} -> /{storage.directory_name(username)}/")
    print(f"[*] legacy root : {plan.source}")
    print(f"[*] destination : {plan.destination}")
    for line in plan.describe():
        print(f"    {line}")

    if not args.apply:
        print("\n[!] dry run - nothing was changed. Re-run with --apply to proceed.")
        return 0

    print("\n[!] stop the WebDAV service before migrating live data")
    migration.apply_migration(plan)
    for line in plan.describe():
        print(f"[*] {line}")
    print("[*] migration finished")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.manage",
        description="Administer WebDAV users stored in MariaDB/MySQL.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="enable debug logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("initdb", help="create the schema and optionally an admin")
    init.add_argument("--admin-user", help="create this user (if it does not exist)")
    init.add_argument("--password", help="password for --admin-user")
    init.add_argument("--generate", action="store_true", help="generate a password")
    init.set_defaults(func=cmd_initdb)

    add = sub.add_parser("adduser", help="create a user and its storage directory")
    add.add_argument("username")
    add.add_argument("--password")
    add.add_argument("--generate", action="store_true")
    add.set_defaults(func=cmd_adduser)

    change = sub.add_parser("passwd", help="change a user's password")
    change.add_argument("username")
    change.add_argument("--password")
    change.add_argument("--generate", action="store_true")
    change.set_defaults(func=cmd_passwd)

    delete = sub.add_parser("deluser", help="delete a user")
    delete.add_argument("username")
    delete.add_argument(
        "--purge", action="store_true", help="also delete the storage directory"
    )
    delete.set_defaults(func=cmd_deluser)

    listing = sub.add_parser("listusers", help="list user names and their URL prefix")
    listing.set_defaults(func=cmd_listusers)

    adopt = sub.add_parser(
        "adopt-shared",
        help="move a legacy shared root into one account's directory",
    )
    adopt.add_argument("username")
    adopt.add_argument(
        "--from", dest="source", required=True, help="legacy shared root directory"
    )
    adopt.add_argument(
        "--apply", action="store_true", help="actually migrate (default: dry run)"
    )
    adopt.add_argument(
        "--copy", action="store_true", help="copy instead of moving"
    )
    adopt.add_argument(
        "--force", action="store_true", help="overwrite existing destination entries"
    )
    adopt.set_defaults(func=cmd_adopt_shared)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except (conf.ConfigurationError, db.DatabaseError, TypeError, ValueError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
