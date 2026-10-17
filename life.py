"""Life: local calendar + todo list for the training dashboard's Life tab.

Plain JSON files in ~/.local/share/training-brief/ (life-events.json, life-todos.json).
Training-plan sessions are merged into the calendar read-only, so one timeline shows
everything. Nothing here talks to the network.
"""
import json
import os
import uuid
from datetime import datetime, timedelta

DATA_DIR = os.path.expanduser("~/.local/share/training-brief")
EVENTS_FILE = os.path.join(DATA_DIR, "life-events.json")
# Shared with the standalone todo panel (~/todo-panel): {"tasks": [{id, text, done, created(ms)}]}.
# We add optional fields (area, due, done_ts) which the panel preserves.
TODOS_FILE = os.path.expanduser("~/todo-panel/todo.json")

AREAS = ["research", "admin", "home", "social", "health", "rest", "other"]
REPEATS = ["", "daily", "weekly"]


def _load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _load_todos():
    try:
        with open(TODOS_FILE) as f:
            d = json.load(f)
        return d.get("tasks", []) if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def _save_todos(tasks):
    tmp = TODOS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"tasks": tasks}, f, indent=2)
    os.replace(tmp, TODOS_FILE)


def _valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _valid_time(s):
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def add_item(obj):
    """obj: {type: 'event'|'todo', title, area, date/due, start, end, repeat}"""
    title = (obj.get("title") or "").strip()
    if not title:
        raise ValueError("title required")
    area = obj.get("area") if obj.get("area") in AREAS else "other"
    if obj.get("type") == "event":
        date = obj.get("date")
        if not _valid_date(date):
            raise ValueError("event needs a date")
        ev = {"id": uuid.uuid4().hex, "title": title, "area": area, "date": date,
              "start": obj.get("start") if _valid_time(obj.get("start")) else "",
              "end": obj.get("end") if _valid_time(obj.get("end")) else "",
              "repeat": obj.get("repeat") if obj.get("repeat") in REPEATS else ""}
        if obj.get("todo_id"):                       # event scheduled from a todo
            ev["todo_id"] = str(obj["todo_id"])
        _save(EVENTS_FILE, _load(EVENTS_FILE) + [ev])
    else:
        due = obj.get("due")
        td = {"id": uuid.uuid4().hex, "text": title, "area": area,
              "due": due if _valid_date(due) else "", "done": False,
              "created": int(datetime.now().timestamp() * 1000)}
        _save_todos([td] + _load_todos())      # newest first, like the panel


def set_todo_due(item_id, date):
    """Set (or clear, with "") a todo's due date; the shared todo.json keeps all other fields."""
    todos = _load_todos()
    for t in todos:
        if t.get("id") == item_id:
            t["due"] = date if _valid_date(date) else ""
    _save_todos(todos)


def update_event(item_id, obj):
    """Change fields of one of the user's own events (title, area, date, start, end, repeat)."""
    events = _load(EVENTS_FILE)
    hit = None
    for e in events:
        if e["id"] == item_id:
            hit = e
            if "title" in obj and str(obj["title"]).strip():
                e["title"] = str(obj["title"]).strip()
            if obj.get("area") in AREAS:
                e["area"] = obj["area"]
            if "date" in obj and _valid_date(obj["date"]):
                e["date"] = obj["date"]
            for k in ("start", "end"):
                if k in obj:
                    e[k] = obj[k] if _valid_time(obj[k]) else ""
            if "repeat" in obj and obj["repeat"] in REPEATS:
                e["repeat"] = obj["repeat"]
    if hit is None:
        raise ValueError("event not found")
    _save(EVENTS_FILE, events)
    return hit


def toggle_todo(item_id):
    todos = _load_todos()
    for t in todos:
        if t.get("id") == item_id:
            t["done"] = not t.get("done")
            t["done_ts"] = datetime.now().isoformat(timespec="seconds") if t["done"] else ""
    _save_todos(todos)


def delete_item(item_id):
    data = _load(EVENTS_FILE)
    kept = [x for x in data if x["id"] != item_id]
    if len(kept) != len(data):
        _save(EVENTS_FILE, kept)
    todos = _load_todos()
    kept = [x for x in todos if x.get("id") != item_id]
    if len(kept) != len(todos):
        _save_todos(kept)


def _expand(events, start, days):
    """Occurrences of events (incl. daily/weekly repeats) in [start, start+days)."""
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        for e in events:
            try:
                first = datetime.strptime(e["date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if d < first:
                continue
            rep = e.get("repeat")
            if (d == first or rep == "daily"
                    or (rep == "weekly" and (d - first).days % 7 == 0)):
                out.append({**e, "date": ds, "repeat_of": e["id"] if rep else ""})
    return out


def life_payload(training_sessions=None, days=7, now=None):
    now = now or datetime.now()
    today = now.date()
    events = _expand(_load(EVENTS_FILE), today, days)
    for s in training_sessions or []:
        ds = s.get("date", "")
        if _valid_date(ds) and today <= datetime.strptime(ds, "%Y-%m-%d").date() < today + timedelta(days=days):
            events.append({"id": "", "title": s.get("name", "training"), "area": "training",
                           "date": ds, "start": "", "end": "", "repeat": "", "readonly": True})
    events.sort(key=lambda e: (e["date"], e.get("start") or "99:99"))

    todos = _load_todos()
    # The panel ticks tasks off without recording when; stamp the first time we see it.
    stamped = False
    for t in todos:
        if t.get("done") and not t.get("done_ts"):
            t["done_ts"] = now.isoformat(timespec="seconds")
            stamped = True
    if stamped:
        _save_todos(todos)
    for t in todos:
        t["title"] = t.get("text", "")
        t["area"] = t.get("area") if t.get("area") in AREAS else "other"
        t["due"] = t.get("due") or ""
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    open_todos = sorted((t for t in todos if not t["done"]),
                        key=lambda t: (t["due"] or "9999", -(t.get("created") or 0)))
    done_week = [t for t in todos if t["done"] and (t.get("done_ts") or "")[:10] >= week_start]
    balance = {a: 0 for a in AREAS + ["training"]}
    for t in done_week:
        balance[t["area"]] = balance.get(t["area"], 0) + 1
    for e in _expand(_load(EVENTS_FILE), today - timedelta(days=today.weekday()), today.weekday() + 1):
        balance[e["area"]] = balance.get(e["area"], 0) + 1
    balance["training"] = sum(1 for x in training_sessions or []
                              if week_start <= x.get("date", "") <= today.strftime("%Y-%m-%d"))
    return {
        "today": today.strftime("%Y-%m-%d"),
        "events": events,
        "todos": open_todos,
        "done_week": len(done_week),
        "balance": balance,
        "areas": AREAS,
        "repeats": REPEATS,
    }
