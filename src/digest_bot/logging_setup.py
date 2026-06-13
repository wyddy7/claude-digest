"""Centralized logging setup — console + rotating file, one place.

Both entrypoints (bot.py, scheduler.py) call setup_logging("<component>") instead
of logging.basicConfig, so logs go to the console AND to a rotating file under
LOG_DIR for after-the-fact observability (what the bot did, when, why a digest
failed). Each process writes its own file (bot.log / scheduler.log) so the two
containers never fight over one handle.

Env knobs:
  LOG_LEVEL    INFO        root level (DEBUG/INFO/WARNING/...)
  LOG_DIR      logs        directory for the rotating files (created if missing)
  LOG_TO_FILE  1           set to 0/false/no to disable the file handler

Prod note: the bot runs in Docker, so LOG_DIR must be a bind-mounted/volume path
to survive container restarts (see auto-docs/for-devops/digest-bot-server.md).
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_NOISY_LIBS = ("httpx", "httpcore", "telegram", "apscheduler")
_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 5


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() not in ("0", "false", "no", "off", "")


def setup_logging(component: str) -> None:
    """Configure root logging for one process. Idempotent — safe to call once at
    startup. component names the rotating file (e.g. 'bot' -> logs/bot.log)."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace any pre-existing handlers (e.g. a prior basicConfig) so repeated
    # calls don't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root.addHandler(console)

    file_enabled = _truthy(os.getenv("LOG_TO_FILE", "1"))
    if file_enabled:
        log_dir = os.getenv("LOG_DIR", "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = RotatingFileHandler(
                os.path.join(log_dir, f"{component}.log"),
                maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter(_FILE_FORMAT))
            root.addHandler(fh)
        except OSError as e:
            # Never let a read-only/unwritable LOG_DIR stop the bot — console still works.
            root.warning("file logging disabled (cannot write %s): %s", log_dir, e)

    # Keep chatty libraries at WARNING even in DEBUG mode.
    for lib in _NOISY_LIBS:
        logging.getLogger(lib).setLevel(logging.WARNING)
