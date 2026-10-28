"""
Check the email configuration and optionally send ONE test message through the real SMTP server.

  python scripts/send_test_email.py                  # validate configuration only (sends nothing)
  python scripts/send_test_email.py you@example.com  # validate, then send a test message to that address

On Railway, run it where the variables exist:  railway run python scripts/send_test_email.py you@example.com
Output names missing variables and failure reasons; it never prints the password or other secrets.
Exit code 0 = configured (and, if an address was given, the server ACCEPTED the message).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_ENV", "development")   # this tool does not need the production secret-key check

from config import Config            # noqa: E402
from utils import mailer             # noqa: E402


def main() -> int:
    print(mailer.status_line())
    problems = mailer.config_problems()
    if problems:
        for p in problems:
            print(" -", p)
        return 2
    if len(sys.argv) < 2:
        print("Configuration looks valid. Pass a recipient address to send a test message.")
        return 0
    to = sys.argv[1].strip()
    if not mailer.valid_address(to):
        print("That recipient address is not valid.")
        return 2
    result = mailer.send_email(
        to, "Omixa email test",
        "This is a test message from the Omixa server.\n\nIf you can read it, SMTP delivery from "
        f"{Config.MAIL_DEFAULT_SENDER} works.\n")
    if result.ok:
        print("The mail server accepted the message. Check the inbox (and spam folder) of the recipient.")
        return 0
    print(f"Sending failed: {result.code} - {result.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
