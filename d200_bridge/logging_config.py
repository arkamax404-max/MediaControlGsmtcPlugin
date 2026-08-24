import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


SAFE_EVENTS = {"companion_listening", "startup_failed"}


class SafeEventFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._d200_owned = True

    def filter(self, record):
        if record.msg not in SAFE_EVENTS or record.args:
            record.msg = "redacted_event"
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True

def configure_logging(log_directory, token=None, console=True, max_bytes=2 * 1024 * 1024):
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1, min(max_bytes, 2 * 1024 * 1024))
    logger = logging.getLogger("d200_bridge")
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        if getattr(handler, "_d200_owned", False):
            handler.close()
            logger.removeHandler(handler)
    formatter = logging.Formatter("%(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        directory / "companion.log", maxBytes=max_bytes, backupCount=4,
        encoding="utf-8",
    )
    file_handler._d200_owned = True
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SafeEventFilter())
    logger.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler()
        console_handler._d200_owned = True
        console_handler.setFormatter(formatter)
        console_handler.addFilter(SafeEventFilter())
        logger.addHandler(console_handler)
    return logger
