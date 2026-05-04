import copy
import threading
import uuid


_tasks: dict[str, dict] = {}
_tasks_lock = threading.RLock()


def new_task() -> str:
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "pending",
            "progress": 0,
            "log": [],
            "error": None,
        }
    return task_id


def get_task(task_id: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        return copy.deepcopy(task) if task else None


def task_progress(task_id: str, pct: int, msg: str = "") -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return None
        task["progress"] = max(0, min(100, int(pct)))
        if msg:
            task["log"].append(msg)
        return copy.deepcopy(task)


def task_log(task_id: str, msg: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return None
        task["log"].append(msg)
        return copy.deepcopy(task)


def task_done(task_id: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return None
        task["status"] = "done"
        task["progress"] = 100
        return copy.deepcopy(task)


def task_error(task_id: str, msg: str) -> dict | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return None
        task["status"] = "error"
        task["error"] = msg
        return copy.deepcopy(task)
