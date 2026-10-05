"""
Generates a fresh OMIXA_SECRET_KEY and (optionally) an admin password hash.

Prints to your terminal ONLY. It deliberately never writes a file, so there is
nothing for git to pick up. Paste the output straight into your host's
environment-variable settings (Railway -> Variables) and nowhere else.

    python scripts/generate_secrets.py
    python scripts/generate_secrets.py --admin-password   # prompts, hashes, never echoes
"""
import argparse
import getpass
import secrets

from werkzeug.security import generate_password_hash


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-password", action="store_true",
                    help="prompt for an admin password and print its hash")
    args = ap.parse_args()

    print(f"OMIXA_SECRET_KEY={secrets.token_urlsafe(48)}")
    if args.admin_password:
        pw = getpass.getpass("New admin password: ")
        if len(pw) < 12:
            raise SystemExit("Use at least 12 characters.")
        if pw != getpass.getpass("Repeat: "):
            raise SystemExit("Passwords do not match.")
        print(f"OMIXA_ADMIN_PASSWORD_HASH={generate_password_hash(pw)}")


if __name__ == "__main__":
    main()
