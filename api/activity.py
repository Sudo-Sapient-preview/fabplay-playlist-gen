import threading
from datetime import datetime, timezone


_activity: list[dict] = []
_activity_lock = threading.RLock()
_MAX_ACTIVITY_ITEMS = 50


def log_activity(event: str, brand: str) -> dict:
    entry = {
        "event": event,
        "brand": brand,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _activity_lock:
        _activity.insert(0, entry)
        del _activity[_MAX_ACTIVITY_ITEMS:]
    return entry


def get_activity() -> list[dict]:
    with _activity_lock:
        return [dict(item) for item in _activity]
