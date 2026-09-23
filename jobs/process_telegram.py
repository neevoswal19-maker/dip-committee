"""Read messages sent to the bot and record any trades in them.

Runs every 15 minutes from .github/workflows/telegram-inbox.yml. Telegram
keeps unread messages for 24 hours, so this has to run through weekends as
well. See src/alerts/telegram_inbox.py for how messages are read.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.alerts import telegram_inbox
from src.config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("process_telegram")


def main() -> int:
    db.assert_encrypted()
    outcomes = telegram_inbox.process_pending(load_config())
    if not outcomes:
        log.info("No new messages")
    for outcome in outcomes:
        log.info("update %s: %s", outcome["update_id"], outcome["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
