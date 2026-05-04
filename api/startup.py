import logging
import os
import sys
import threading

from api.db import run_startup_checks


logger = logging.getLogger(__name__)

_startup_lock = threading.Lock()
_startup_ran = False


def _should_run_startup_checks() -> bool:
    if os.getenv("DJANGO_SKIP_STARTUP_CHECKS", "").strip().lower() in {"1", "true", "yes"}:
        return False

    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command in {"check", "collectstatic", "makemigrations", "migrate", "shell", "test"}:
            return False
        if command == "runserver":
            return os.getenv("RUN_MAIN") == "true" or os.getenv("WERKZEUG_RUN_MAIN") == "true"

    server_software = os.getenv("SERVER_SOFTWARE", "").lower()
    if "gunicorn" in server_software:
        return True

    executable = os.path.basename(sys.argv[0]).lower()
    return "gunicorn" in executable


def run_startup_checks_once() -> None:
    global _startup_ran
    if _startup_ran or not _should_run_startup_checks():
        return

    with _startup_lock:
        if _startup_ran:
            return
        try:
            run_startup_checks(raise_on_fatal=False)
        except Exception:
            logger.exception("Startup checks raised unexpectedly")
        finally:
            _startup_ran = True
