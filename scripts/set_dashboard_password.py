"""
Change the dashboard password in place.

  python set_dashboard_password.py

Prompts for the new password (never echoed, never passed as an argument, so it
does not land in your shell history), rewrites only the password_hash line in
coordinator/config.yaml, and leaves secret_key and everything else untouched.

Restart the coordinator afterwards -- the config is read once at startup:

  sudo systemctl restart loom-coordinator
"""

import os
import re
import sys
from getpass import getpass

sys.path.insert(
    0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "coordinator"))
)
from werkzeug.security import generate_password_hash  # noqa: E402

CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "coordinator", "config.yaml")
)

MIN_LENGTH = 10


def main():
    path = os.environ.get("LOOM_CONFIG", CONFIG_PATH)
    if not os.path.exists(path):
        sys.exit(f"No config.yaml at {path}. Copy config.example.yaml first.")

    pw = getpass("New dashboard password: ")
    if len(pw) < MIN_LENGTH:
        sys.exit(f"Use at least {MIN_LENGTH} characters -- this is the only gate on the dashboard.")
    if pw != getpass("Confirm: "):
        sys.exit("Passwords did not match. Nothing changed.")

    with open(path, "r") as f:
        original = f.read()

    # Replace via a function, not a replacement string: a PBKDF2 hash contains
    # '$' separators and re would otherwise try to interpret backreferences.
    new_hash = generate_password_hash(pw)
    updated, count = re.subn(
        r"^(\s*password_hash:\s*).*$",
        lambda m: f'{m.group(1)}"{new_hash}"',
        original,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        sys.exit(
            f"Could not find a 'password_hash:' line in {path}.\n"
            f"Edit it by hand, or regenerate the pair with: gen_api_key.py --dashboard-secret"
        )

    with open(path, "w") as f:
        f.write(updated)
    os.chmod(path, 0o600)

    print(f"Password updated in {path}")
    print("Now restart the coordinator so it reloads the config:")
    print("  sudo systemctl restart loom-coordinator")


if __name__ == "__main__":
    main()
