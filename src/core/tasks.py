"""THE TASK QUEUE for each Plant Head (plain code).

Each user starts the day with 10 tasks (data/tasks_seed.json): 2-3 red, 3 orange, the rest low.
The screen shows the TOP 5 OPEN tasks (red first). When a task is done, snoozed or closed it leaves the top 5 and the
next one moves up. Some outcomes CREATE new tasks (a follow-up for me, or a request in the other plant's queue).
State lives in workspace/tasks/<plant>.json so it survives restarts; "Reset demo" restores the seed.
"""
import json
from datetime import datetime

from src import config

PRIORITY_ORDER = {"red": 0, "orange": 1, "low": 2}
USERS = {"AUS": "Dana Whitfield", "DAL": "Marcus Bell"}        # the two app users (Plant Heads)


def _file(plant_id):
    d = config.WORKSPACE / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{plant_id}.json"


def reset(plant_id=None):
    seed = json.loads((config.DATA_DIR / "tasks_seed.json").read_text())
    for pl in ([plant_id] if plant_id else USERS):
        _file(pl).write_text(json.dumps([t for t in seed if t["plant_id"] == pl], indent=1))


def load(plant_id) -> list:
    f = _file(plant_id)
    if not f.exists():
        reset(plant_id)
    return json.loads(f.read_text())


def save(plant_id, tasks):
    _file(plant_id).write_text(json.dumps(tasks, indent=1))


def get(task_id) -> dict:
    plant_id = task_id.split("-")[0]
    return next(t for t in load(plant_id) if t["id"] == task_id)


def open_tasks(plant_id) -> list:
    ts = [t for t in load(plant_id) if t["status"] == "open"]
    return sorted(ts, key=lambda t: (PRIORITY_ORDER[t["priority"]], t["order"]))


def top5(plant_id) -> list:
    return open_tasks(plant_id)[:5]


def finished(plant_id) -> list:
    ts = [t for t in load(plant_id) if t["status"] != "open"]
    return sorted(ts, key=lambda t: t.get("closed_at", ""), reverse=True)


def update(task_id, status, outcome: str, **extra):
    """status: done | closed | snoozed | waiting (waiting = sent, awaiting someone else)."""
    plant_id = task_id.split("-")[0]
    ts = load(plant_id)
    for t in ts:
        if t["id"] == task_id:
            t.update(status=status, outcome=outcome, closed_at=datetime.now().isoformat(timespec="seconds"), **extra)
            t.setdefault("history", []).append({"at": t["closed_at"], "status": status, "outcome": outcome})
    save(plant_id, ts)


def add(plant_id, priority, type_, title, why, params, source, first=False) -> str:
    """Create a new task (follow-up or cross-plant request). Returns its id. Never duplicates an open task.
    first=True puts it at the top of its colour (e.g. another plant is waiting on us)."""
    ts = load(plant_id)
    for t in ts:
        if t["status"] == "open" and t["title"] == title:
            return t["id"]
    n = 1 + max([int(t["id"].split("-")[1]) for t in ts] or [0])
    tid = f"{plant_id}-{n:02d}"
    ts.append(dict(id=tid, plant_id=plant_id, priority=priority, type=type_, title=title, why=why, params=params,
                   status="open", order=(-100 - len(ts)) if first else len(ts) + 100, created=config.TODAY.isoformat(), source=source))
    save(plant_id, ts)
    return tid
