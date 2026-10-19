import logging
import platform

import textual

from applog import LOG_PATH, setup_logging
from config import load_config
from app import MediaCleanerApp

log = logging.getLogger("main")

if __name__ == "__main__":
    setup_logging()
    log.info(
        "=== MediaCleaner starting (python %s, textual %s, log %s) ===",
        platform.python_version(), textual.__version__, LOG_PATH,
    )
    config = load_config()
    app = MediaCleanerApp(config)
    app.run()
    log.info("=== MediaCleaner exited (return code %s) ===", app.return_code)
