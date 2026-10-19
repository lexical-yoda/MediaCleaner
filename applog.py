from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

LOG_PATH = Path(__file__).parent / "mediacleaner.log"

_secrets: list[str] = []


def register_secret(value: str | None) -> None:
    """Mask this value wherever it would appear in the log.

    Used for the API keys: httpx error messages include the full request URL,
    query string and all, so a logged exception would otherwise leak them.
    """
    if value and len(value) >= 6 and value not in _secrets:
        _secrets.append(value)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in _secrets:
            text = text.replace(secret, "***")
        return text


def setup_logging() -> None:
    """Send every log record from the app to mediacleaner.log.

    The TUI owns the terminal, so nothing can be printed while it runs; the log
    file is the only place a failure leaves a trace. Uncaught exceptions from
    the main thread and from worker threads are logged too.
    """
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    # httpx logs every request URL at INFO, API key included. The app logs its
    # own request failures, so these only need to report their own warnings.
    for noisy in ("httpx", "httpcore", "asyncio", "markdown_it"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def _log_uncaught(exc_type, exc, tb) -> None:
        logging.getLogger("uncaught").critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def _log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("uncaught").critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_thread_uncaught
