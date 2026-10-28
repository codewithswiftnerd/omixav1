"""
Send "your Pro access ends soon" reminders. Run once a day.

  python scripts/send_expiry_reminders.py

On Railway: create a separate service from this repo with a cron schedule (for example `0 8 * * *`), start command
`python scripts/send_expiry_reminders.py`, and give it the SAME SMTP_* variables and Firebase variables as the web
service. Safe to run more than once a day: a reminder already sent is never repeated.
Exit code 0 = finished without failures, 1 = some sends failed (they are retried next run), 2 = not configured.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounts import reminders, store as store_mod   # noqa: E402
from utils import mailer                              # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(mailer.status_line())
    problems = mailer.config_problems()
    if problems:
        for p in problems:
            print(" -", p)
        return 2
    store = store_mod.get_store()
    if store is None:
        print("Accounts store unavailable:", store_mod.store_error())
        return 2
    counts = reminders.send_expiry_reminders(store)
    print(f"Reminders: {counts['sent']} sent, {counts['failed']} failed, {counts['skipped']} not due")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
