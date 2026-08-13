"""
Loom admin helper. Run this ON THE COORDINATOR.

  python gen_api_key.py --dashboard-secret
  python gen_api_key.py --add-device --name my-desktop --platform macos --tags gpu,desktop
  python gen_api_key.py --add-device --name pi-node --platform linux --persistent
  python gen_api_key.py --list-devices
  python gen_api_key.py --rotate-key --name my-desktop
  python gen_api_key.py --remove-device --name old-laptop

It talks to coordinator/loom.db directly and creates the schema itself if the
database does not exist yet, so you can register devices before the coordinator
has ever been started.
"""

import argparse
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from getpass import getpass

from werkzeug.security import generate_password_hash

# Import the coordinator's db module so the schema lives in exactly one place.
COORDINATOR_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "coordinator")
)
sys.path.insert(0, COORDINATOR_DIR)
import db as loomdb  # noqa: E402

VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
VALID_TAG_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")


def _connect():
    loomdb.init_db()
    return loomdb.connect()


def dashboard_secret():
    pw = getpass("Choose a dashboard password: ")
    if len(pw) < 10:
        print("Use at least 10 characters -- this is the only gate on the dashboard.",
              file=sys.stderr)
        sys.exit(1)
    if pw != getpass("Confirm: "):
        print("Passwords did not match.", file=sys.stderr)
        sys.exit(1)

    print("\nPaste these into coordinator/config.yaml under `dashboard:`\n")
    print(f'  secret_key: "{secrets.token_hex(32)}"')
    print(f'  password_hash: "{generate_password_hash(pw)}"')
    print("\nKeep config.yaml out of git -- it is already in .gitignore.")


def _validate_tags(tags):
    parsed = [t.strip() for t in (tags or "").split(",") if t.strip()]
    for t in parsed:
        if not VALID_TAG_RE.match(t):
            print(f"Invalid tag '{t}': use 1-32 chars of [a-zA-Z0-9_.-]", file=sys.stderr)
            sys.exit(1)
    return ",".join(parsed)


def add_device(name, platform, tags, persistent):
    if not VALID_NAME_RE.match(name):
        print("Device name must be 1-64 chars of [a-zA-Z0-9_.-]", file=sys.stderr)
        sys.exit(1)

    api_key = secrets.token_urlsafe(32)
    conn = _connect()
    try:
        if persistent:
            existing = conn.execute(
                "SELECT name FROM devices WHERE persistent = 1"
            ).fetchone()
            if existing:
                # Two persistent nodes both catch untargeted jobs, so ownerless
                # work would land on whichever polled first -- unpredictable.
                print(
                    f"'{existing['name']}' is already the persistent node.\n"
                    f"Only one device should be persistent. Clear the old one first:\n"
                    f"  python gen_api_key.py --set-persistent --name {name}",
                    file=sys.stderr,
                )
                sys.exit(1)

        conn.execute(
            "INSERT INTO devices (name, api_key, platform, tags, persistent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, api_key, platform, _validate_tags(tags), 1 if persistent else 0,
             datetime.now(timezone.utc).isoformat()),
        )
    except sqlite3.IntegrityError:
        print(f"A device named '{name}' already exists. "
              f"Use --rotate-key to issue it a new key.", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(f"Registered '{name}'"
          f"{' (persistent node)' if persistent else ''}.\n")
    print(f"  api_key: {api_key}\n")
    print(f"Paste that into {name}'s agent/config.yaml as `api_key:`.")
    print("It is shown once here, but you can always re-read it from the DB or rotate it.")


def rotate_key(name):
    conn = _connect()
    try:
        api_key = secrets.token_urlsafe(32)
        cur = conn.execute("UPDATE devices SET api_key = ? WHERE name = ?", (api_key, name))
        if cur.rowcount == 0:
            print(f"No device named '{name}'.", file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()
    print(f"Rotated key for '{name}'.\n\n  api_key: {api_key}\n")
    print(f"Update {name}'s agent/config.yaml and restart its agent -- "
          f"the old key stops working immediately.")


def set_persistent(name):
    conn = _connect()
    try:
        row = conn.execute("SELECT id FROM devices WHERE name = ?", (name,)).fetchone()
        if not row:
            print(f"No device named '{name}'.", file=sys.stderr)
            sys.exit(1)
        conn.execute("UPDATE devices SET persistent = 0")
        conn.execute("UPDATE devices SET persistent = 1 WHERE name = ?", (name,))
    finally:
        conn.close()
    print(f"'{name}' is now the persistent node; every other device was cleared.")


def remove_device(name, yes):
    conn = _connect()
    try:
        row = conn.execute("SELECT id FROM devices WHERE name = ?", (name,)).fetchone()
        if not row:
            print(f"No device named '{name}'.", file=sys.stderr)
            sys.exit(1)
        if not yes:
            confirm = input(f"Remove '{name}' and revoke its API key? [y/N] ")
            if confirm.lower() not in ("y", "yes"):
                print("Aborted.")
                return
        conn.execute("DELETE FROM devices WHERE id = ?", (row["id"],))
    finally:
        conn.close()
    print(f"Removed '{name}'. Its agent will start getting 403s on its next heartbeat.")


def list_devices(show_keys):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM devices ORDER BY persistent DESC, name ASC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No devices registered yet. Add one with --add-device.")
        return

    header = f"{'NAME':<20} {'PLATFORM':<9} {'PERSIST':<8} {'TAGS':<20} LAST SEEN"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['name']:<20} {(r['platform'] or '?'):<9} "
              f"{('yes' if r['persistent'] else ''):<8} "
              f"{(r['tags'] or ''):<20} {r['last_seen'] or 'never'}")
        if show_keys:
            print(f"{'':<20} api_key: {r['api_key']}")


def main():
    p = argparse.ArgumentParser(
        description="Loom coordinator admin CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--dashboard-secret", action="store_true",
                   help="generate secret_key + password_hash for config.yaml")
    p.add_argument("--add-device", action="store_true", help="register a new node")
    p.add_argument("--list-devices", action="store_true", help="show registered nodes")
    p.add_argument("--rotate-key", action="store_true", help="issue a fresh API key")
    p.add_argument("--remove-device", action="store_true", help="delete a node and revoke its key")
    p.add_argument("--set-persistent", action="store_true",
                   help="make this the always-on node that catches untargeted jobs")

    p.add_argument("--name")
    p.add_argument("--platform", choices=["macos", "linux", "windows"])
    p.add_argument("--tags", help="comma-separated, e.g. gpu,desktop")
    p.add_argument("--persistent", action="store_true",
                   help="with --add-device: mark as the always-on node")
    p.add_argument("--show-keys", action="store_true",
                   help="with --list-devices: also print API keys")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    args = p.parse_args()

    needs_name = ("add_device", "rotate_key", "remove_device", "set_persistent")
    if any(getattr(args, a) for a in needs_name) and not args.name:
        p.error("--name is required for that action")

    if args.dashboard_secret:
        dashboard_secret()
    elif args.add_device:
        if not args.platform:
            p.error("--add-device requires --platform")
        add_device(args.name, args.platform, args.tags, args.persistent)
    elif args.list_devices:
        list_devices(args.show_keys)
    elif args.rotate_key:
        rotate_key(args.name)
    elif args.remove_device:
        remove_device(args.name, args.yes)
    elif args.set_persistent:
        set_persistent(args.name)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
