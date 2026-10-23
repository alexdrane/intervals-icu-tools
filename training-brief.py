#!/usr/bin/env python3
"""Morning training brief — loading screen, retries, charts, coach brief."""

import difflib
import fcntl
import hashlib
import json
import logging
import math
import os
import queue
import re
import statistics
import sys
import time
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, date

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, WebKit2, GLib, GdkPixbuf

import requests

sys.path.insert(0, os.path.expanduser("~/app-hub"))
try:
    import apphub
except Exception:      # the hub is optional
    apphub = None
try:
    import focus as _focus
except Exception:  # Focus tab is optional
    _focus = None
try:
    import sleep as _sleep
except Exception:  # Sleep view is optional
    _sleep = None
try:
    import life as _life
except Exception:  # Life tab is optional
    _life = None
try:
    import sustain as _sustain
    import sustain_ui as _sus_ui
    _SUS_CSS, _SUS_JS, _SUS_PANEL = _sus_ui.CSS, _sus_ui.JS, _sus_ui.PANEL
except Exception:  # Load tab is optional
    _sustain = None
    _SUS_CSS = _SUS_JS = _SUS_PANEL = ""

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "one-year-ago"))
try:
    import one_year_ago as _oya
except Exception:  # Vs Last Year tab is optional
    _oya = None

try:
    from illness_model import get_illness_data, get_cached_result, get_model_meta
    _ILLNESS_AVAILABLE = True
except ImportError:
    _ILLNESS_AVAILABLE = False

LOCK_FILE          = "/tmp/training-brief.lock"
CONFIG_FILE        = os.path.expanduser("~/.config/intervals-icu/config.json")
TRAINING_PLAN_FILE = os.path.expanduser("~/.config/intervals-icu/training-plan.json")
METRICS_FILE       = os.path.expanduser("~/.config/intervals-icu/test-metrics.json")
BENCHMARKS_FILE    = os.path.expanduser("~/.config/intervals-icu/benchmarks.json")
CACHE_FILE         = os.path.expanduser("~/.cache/training-brief/cache.json")
NUTR_CACHE_FILE    = os.path.expanduser("~/.cache/training-brief/nutrition-cache.json")
CAL_GAP_CACHE_FILE = os.path.expanduser("~/.cache/training-brief/calorie-gap-cache.json")
FOOD_LOG_FILE      = os.path.expanduser("~/.local/share/training-brief/food-log.json")
CLAUDE_USAGE_LOG    = os.path.expanduser("~/.local/share/training-brief/claude-usage.jsonl")
STRETCH_LOG_FILE   = os.path.expanduser("~/.local/share/training-brief/stretch-log.json")
NOTES_LOG_FILE     = os.path.expanduser("~/.local/share/training-brief/notes-log.json")
OLLAMA_CHAT_URL    = "http://localhost:11434/api/chat"
LOCAL_MODEL        = "qwen2.5:7b"
LOCAL_MODEL_FAST   = "qwen2.5:3b"   # for long/narrative prompts where 7B is too slow on CPU
NOTE_CONTEXT_DAYS  = 21             # notes older than this are stale context, not "recent"
KEY_DATE_HORIZON_DAYS = 14             # fixtures further out than this don't steer the tips
REFERENCE_WINDOW_DAYS = 365         # history used to judge whether today's numbers are good
BASE_URL           = "https://intervals.icu/api/v1"
WINDOW_W, WINDOW_H = 1440, 860
EATING_START_H, EATING_END_H = 7, 22   # window used to pace-adjust today's calorie target
LOG_FILE           = os.path.expanduser("~/.cache/training-brief/brief.log")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("training-brief")


# ── Loading / error screens ──────────────────────────────────────────────────

LOADING_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1b1c1e;display:flex;align-items:center;justify-content:center;
     height:100vh;font-family:-apple-system,'Segoe UI',sans-serif}
.wrap{text-align:center}
.spinner{width:38px;height:38px;border:3px solid #2a2c2f;border-top-color:#b79cf0;
         border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 16px}
@keyframes spin{to{transform:rotate(360deg)}}
.msg{color:#56585a;font-size:13px;margin-bottom:6px}
.sub{color:#424446;font-size:11px;min-height:16px}
</style></head><body>
<div class="wrap">
  <div class="spinner"></div>
  <div class="msg">Fetching training data…</div>
  <div class="sub" id="status"></div>
</div></body></html>"""


def error_html(msg):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#1b1c1e;display:flex;align-items:center;justify-content:center;
     height:100vh;font-family:-apple-system,'Segoe UI',sans-serif}}
.wrap{{text-align:center;max-width:420px;padding:24px}}
.icon{{font-size:32px;margin-bottom:16px}}
.msg{{color:#e8837a;font-size:13px;margin-bottom:8px;font-weight:600}}
.detail{{color:#56585a;font-size:11px;line-height:1.6;margin-bottom:20px}}
button{{background:#2a2c2f;color:#a3a6a9;border:1px solid #424446;border-radius:6px;
        padding:6px 16px;font-size:12px;cursor:pointer}}
</style></head><body>
<div class="wrap">
  <div class="icon">⚠</div>
  <div class="msg">Could not load training data</div>
  <div class="detail">{msg}</div>
  <button onclick="document.title='__close__'">Close</button>
</div></body></html>"""


# ── Config / plan ────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ~6500 kcal per kg of bodyweight change: below the pure-fat 7700 figure because a
# resistance-trained surplus partitions part of the gain into leaner tissue, and a
# modest deficit spares some lean mass. Used both ways to map kcal/day <-> kg/week.
KCAL_PER_KG = 6500


def kcal_day_to_kg_week(kcal):
    return kcal * 7 / KCAL_PER_KG


def kg_week_to_kcal_day(kg):
    return round(kg * KCAL_PER_KG / 7)


def set_calorie_goal(surplus_per_day):
    """Persist the daily surplus/deficit goal (kcal/day; negative for a cut)."""
    cfg = load_config()
    cfg["calorie_surplus_target"] = int(round(surplus_per_day))
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)   # atomic: a crash mid-write can't truncate config
    return cfg["calorie_surplus_target"]


def load_training_plan():
    if not os.path.exists(TRAINING_PLAN_FILE):
        default = {"sessions": []}
        os.makedirs(os.path.dirname(TRAINING_PLAN_FILE), exist_ok=True)
        with open(TRAINING_PLAN_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(TRAINING_PLAN_FILE) as f:
        return json.load(f)


def load_test_metrics():
    if not os.path.exists(METRICS_FILE):
        return {"entries": []}
    with open(METRICS_FILE) as f:
        return json.load(f)


DEFAULT_REP_REF = 8   # working-set rep scheme that targets/baselines are quoted at
TIME_UNIT = "s"       # metrics in this unit are entered and displayed as m:ss


def parse_value(v):
    """Accept a number, or a clock string like "17:30" / "1:52.0", returning seconds.

    Times are the natural way to type a 5k or a 500m split; forcing the athlete to
    convert to seconds is how you get a 20:04 PB logged as 2004.
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if ":" in s:
        mins, _, secs = s.partition(":")
        return float(int(mins) * 60 + float(secs))
    return float(s)


EPLEY_MAX_REPS = 10   # Epley extrapolates optimistically past ~10 reps; sets above are scored as 10


def epley_1rm(weight, reps):
    return round(weight * (1 + min(reps, EPLEY_MAX_REPS) / 30), 1) if reps else weight


def rep_normalised(weight, reps, rep_ref=DEFAULT_REP_REF):
    """Weight a set by its reps, expressed as the equivalent load at `rep_ref` reps.

    Raw working weight ranks 100kg×7 above 90kg×12, which is backwards: by Epley
    those are e1RM 123.3 and 126.0. Going through e1RM and back down to a fixed rep
    count keeps targets and baselines meaning "a working-set weight" rather than
    silently becoming 1RM figures.
    """
    if not reps:
        return weight
    return round(epley_1rm(weight, reps) / (1 + rep_ref / 30), 1)


def latest_metric(entries, metric):
    hits = [e for e in entries if e.get("metric") == metric]
    return hits[-1] if hits else None


def metric_history(entries, metric):
    return [e for e in entries if e.get("metric") == metric]


# Benchmark metrics are per-user, not per-checkout — every athlete tracks different
# lifts and tests. Real config lives in BENCHMARKS_FILE (gitignored); this is only
# the placeholder seeded for a fresh install, and intentionally sport-agnostic.
#
# Every metric sits in exactly one tier:
#   "goal" — a headline objective, drawn as a full-width progress chart
#   "key"  — a repeatable test; one axis on the radar, and fed to the coach
#   "bank" — tracked and stored, but off the radar (old PBs, cross-training)
TIERS = ("goal", "key", "bank")
DEFAULT_TIER = "bank"

DEFAULT_BENCHMARKS = {
    "metrics": {
        "back_squat": {"label": "Back squat (working set)", "target": 100, "unit": "kg", "color": "#88dcfd",
                       "tier": "key", "rep_weighted": True},
        "row_2k":     {"label": "2k row",                   "target": 420, "unit": "s",  "color": "#e3b95f",
                       "tier": "goal", "lower_is_better": True},
        "bodyweight": {"label": "Body weight",              "target": 80,  "unit": "kg", "color": "#a3a6a9",
                       "tier": "key"},
    },
}


def save_benchmark_config(cfg):
    os.makedirs(os.path.dirname(BENCHMARKS_FILE), exist_ok=True)
    tmp = BENCHMARKS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, BENCHMARKS_FILE)   # atomic: never leave a half-written config


def load_benchmark_config():
    """Load the user's benchmark metrics, seeding a placeholder on first run.

    Migrates the old `radar_axes` grouping to a per-metric `tier`. Radar axes are
    now derived from the metrics themselves — one axis per "key" metric — so there
    is a single source of truth rather than two that can drift apart.
    """
    if not os.path.exists(BENCHMARKS_FILE):
        save_benchmark_config(DEFAULT_BENCHMARKS)
        return json.loads(json.dumps(DEFAULT_BENCHMARKS))   # deep copy
    with open(BENCHMARKS_FILE) as f:
        cfg = json.load(f)
    cfg.setdefault("metrics", {})

    if any("tier" not in m for m in cfg["metrics"].values()):
        on_radar = {k for ax in cfg.get("radar_axes", []) for k in ax.get("keys", [])}
        for key, meta in cfg["metrics"].items():
            meta.setdefault("tier", "key" if key in on_radar else DEFAULT_TIER)
        cfg.pop("radar_axes", None)
        save_benchmark_config(cfg)
    else:
        cfg.pop("radar_axes", None)

    for meta in cfg["metrics"].values():
        if meta.get("tier") not in TIERS:
            meta["tier"] = DEFAULT_TIER
    return cfg


def mutate_metric_config(action, key, fields=None):
    """Apply a single config edit from the UI. Returns the updated config.

    Removing a metric drops its *definition* only — logged results stay in
    METRICS_FILE, so re-adding the key brings its history back.
    """
    fields = fields or {}
    cfg = load_benchmark_config()
    metrics = cfg["metrics"]

    if action == "remove":
        metrics.pop(key, None)
    else:
        if action == "add" and key in metrics:
            raise ValueError(f"metric '{key}' already exists")
        if action == "update" and key not in metrics:
            raise KeyError(f"unknown metric '{key}'")
        meta = metrics.setdefault(key, {"color": "#88dcfd", "unit": "", "tier": DEFAULT_TIER})
        for f in ("label", "unit", "color", "tier", "notes", "lifetime_date"):
            if f in fields:
                meta[f] = fields[f]
        # Blank clears the field rather than writing 0 — a target of zero is a real
        # value for a lower-is-better metric, so it cannot double as "unset".
        for f in ("target", "baseline", "lifetime"):
            if f in fields:
                val = parse_value(fields[f])
                if val is None:
                    meta.pop(f, None)
                else:
                    meta[f] = val
        if "rep_ref" in fields and fields["rep_ref"] not in (None, ""):
            meta["rep_ref"] = int(fields["rep_ref"])
        for f in ("lower_is_better", "rep_weighted"):
            if f in fields:
                meta[f] = bool(fields[f])
        if meta.get("tier") not in TIERS:
            meta["tier"] = DEFAULT_TIER
        meta.setdefault("label", key.replace("_", " ").title())

    save_benchmark_config(cfg)
    return cfg


def add_metric_entry(entry_dict):
    """Append a new test result to METRICS_FILE."""
    from datetime import date as _date
    data = load_test_metrics()
    when = _date.today().isoformat()
    try:
        d = entry_dict.get("date")
        if d and _date.fromisoformat(d) <= _date.today():
            when = d
    except ValueError:
        pass
    new_entry = {"date": when, "metric": entry_dict["metric"],
                 "value": float(entry_dict["value"])}
    if entry_dict.get("reps"):
        new_entry["reps"] = int(entry_dict["reps"])
    if entry_dict.get("notes"):
        new_entry["notes"] = entry_dict["notes"]
    data["entries"].append(new_entry)
    with open(METRICS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def build_metrics_data(entries, metric_targets, weight_history=None):
    """Build the metrics summary dict passed to the JS tracker.

    `metric_targets` is the user's benchmark config (see load_benchmark_config).
    `weight_history` (if given) replaces manually-logged entries for the
    "bodyweight" key with the wellness weight log, so that benchmark
    auto-updates from actual weigh-ins instead of requiring a manual test entry.
    """
    result = []
    for key, meta in metric_targets.items():
        history = weight_history if (key == "bodyweight" and weight_history) else metric_history(entries, key)
        tier    = meta.get("tier", DEFAULT_TIER)
        lower   = meta.get("lower_is_better", False)
        unit    = meta.get("unit", "")
        if not history:
            # Still emit a card: a metric added from the UI has no results yet, and
            # without a card there is nowhere to edit it or log its first entry.
            # A lifetime PB may exist with no logged result at all — an old best from
            # before you started tracking is not a baseline for the current journey.
            result.append({
                "key": key, "label": meta.get("label", key), "tier": tier,
                "has_data": False, "value": None, "scored": None, "e1rm": None,
                "rep_ref": None, "reps": None,
                "start": meta.get("baseline"), "target": meta.get("target"),
                "pb": meta.get("lifetime"), "pb_date": meta.get("lifetime_date"),
                "pb_source": "lifetime" if meta.get("lifetime") is not None else None,
                "unit": unit, "is_time": unit == TIME_UNIT,
                "color": meta.get("color", "#88dcfd"),
                "journey_pct": None, "date": None, "notes": "",
                "lower": lower,
                "rep_weighted": bool(meta.get("rep_weighted")),
                "spark": [], "history": [],
            })
            continue
        latest  = history[-1]
        first   = history[0]
        raw     = latest["value"]
        reps    = latest.get("reps")
        tgt     = meta.get("target")
        # An explicit "baseline" overrides the first logged value — useful
        # when history predates a target change (e.g. bulk goal reset).
        start   = meta.get("baseline", first["value"])

        # Rep weighting: score the *normalised* load, not the bar weight, so a
        # heavier single-rep-scheme set does not read as progress over a lighter
        # set for more reps. Opt-in per metric; never applied to lower-is-better
        # timed tests, where reps are meaningless.
        rep_ref  = meta.get("rep_ref", DEFAULT_REP_REF)
        weighted = bool(meta.get("rep_weighted")) and not lower
        score    = rep_normalised(raw, reps, rep_ref) if weighted else raw
        e1rm     = epley_1rm(raw, reps) if (weighted and reps) else None

        # Progress bar: fraction of journey from baseline to target completed.
        # A metric may have no target yet (a PB you are simply recording), in which
        # case there is no journey to show rather than a meaningless 0% or 100%.
        if tgt is None:
            journey_pct = None
        else:
            gap = (start - tgt) if lower else (tgt - start)
            moved = (start - score) if lower else (score - start)
            journey_pct = (moved / gap * 100) if gap else 100
            journey_pct = max(0, min(100, journey_pct))

        def _scored(e):
            return (rep_normalised(e["value"], e.get("reps"), rep_ref)
                    if weighted else e["value"])

        # Lifetime PB: the best ever, which is NOT the baseline. An old best from
        # before the current block should not anchor the progress bar, but it is
        # still the number worth beating. Scored, not raw: for a rep-weighted lift
        # the best set is not simply the heaviest bar.
        pick        = min if lower else max
        best_entry  = pick(history, key=_scored)
        best_logged = _scored(best_entry)
        best_date   = best_entry["date"]
        stated      = meta.get("lifetime")
        if stated is not None and ((stated < best_logged) if lower else (stated > best_logged)):
            pb, pb_date, pb_source = stated, meta.get("lifetime_date"), "lifetime"
        else:
            pb, pb_date, pb_source = best_logged, best_date, "logged"

        # Subtitle: reps, then the normalised load the progress bar actually scores,
        # so the percentage is not left unexplained against the bar weight.
        notes = latest.get("notes", "")
        bits = []
        if reps:
            bits.append(f"×{reps}")
        if weighted and reps:
            bits.append(f"{score:g}{meta['unit']} @{rep_ref}")
        if notes:
            bits.append(notes)
        subtitle = " · ".join(bits)

        # Sparkline follows whatever is being scored, or it would contradict the bar.
        full = [{"date": e["date"], "value": _scored(e), "raw": e["value"],
                 "reps": e.get("reps")} for e in history]
        result.append({
            "key":          key,
            "label":        meta["label"],
            "tier":         tier,
            "has_data":     True,
            "value":        raw,
            "scored":       score,
            "e1rm":         e1rm,
            "rep_ref":      rep_ref if weighted else None,
            "rep_weighted": weighted,
            "reps":         reps,
            "start":        start,
            "target":       tgt,
            "pb":           pb,
            "pb_date":      pb_date,
            "pb_source":    pb_source,
            "unit":         unit,
            "is_time":      unit == TIME_UNIT,
            "color":        meta["color"],
            "journey_pct":  None if journey_pct is None else round(journey_pct, 1),
            "date":         latest["date"],
            "notes":        subtitle,
            "lower":        lower,
            "spark":        full[-12:],
            "history":      full,           # goal charts plot the whole series
        })
    return result


# ── Food log ─────────────────────────────────────────────────────────────────

def note_age_days(note):
    return (datetime.now().date() - datetime.strptime(note["ts"][:10], "%Y-%m-%d").date()).days


def fresh_notes(notes):
    """Notes inside the context window. Notes stay "active" until explicitly cleared,
    so without this a note from months ago is still handed to the model as current
    context — which is how a June taper ended up being described as today's plan."""
    out = []
    for n in notes:
        try:
            if note_age_days(n) <= NOTE_CONTEXT_DAYS:
                out.append(n)
        except (KeyError, ValueError):
            out.append(n)   # unparseable timestamp: keep it rather than silently drop
    return out


def load_notes():
    """Notes log: {"active": [{"ts","text"}], "archive": [...]}.
    Active notes feed the LLM prompt and persist until explicitly cleared."""
    if not os.path.exists(NOTES_LOG_FILE):
        return {"active": [], "archive": []}
    try:
        with open(NOTES_LOG_FILE) as f:
            d = json.load(f)
        d.setdefault("active", [])
        d.setdefault("archive", [])
        return d
    except Exception:
        return {"active": [], "archive": []}


def save_notes(notes):
    os.makedirs(os.path.dirname(NOTES_LOG_FILE), exist_ok=True)
    with open(NOTES_LOG_FILE, "w") as f:
        json.dump(notes, f, indent=2)


def add_note(text):
    text = (text or "").strip()
    if not text:
        return load_notes()
    notes = load_notes()
    notes["active"].append({"ts": datetime.now().isoformat(timespec="seconds"), "text": text})
    save_notes(notes)
    log.info("note added (%d active): %s", len(notes["active"]), text[:80])
    return notes


def clear_notes():
    """Move all active notes into the archive (kept for logging) and empty active."""
    notes = load_notes()
    if notes["active"]:
        notes["archive"].extend(notes["active"])
        log.info("cleared %d active notes", len(notes["active"]))
        notes["active"] = []
        save_notes(notes)
    return notes


def render_notes_html():
    """HTML for the active-notes list shown in the Notes panel."""
    import html as _html
    active = load_notes().get("active", [])
    if not active:
        return '<div class="notes-empty">No active notes. Anything you add here feeds the coach brief and persists until cleared.</div>'
    rows = []
    for n in active:
        day = n["ts"][:10]
        rows.append(f'<div class="note-row"><span class="note-day">{day}</span>'
                    f'{_html.escape(n["text"])}</div>')
    return "".join(rows)


# ── Food log & LLM calls — local Qwen via Ollama ────────────────────────────

def _log_ollama_usage(source, duration_ms, parsed_ok, model=LOCAL_MODEL):
    """Append one line of timing info for a local Ollama call to CLAUDE_USAGE_LOG.
    Best-effort — never raises."""
    try:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "model": model,
            "backend": "ollama",
            "duration_ms": duration_ms,
            "ok": parsed_ok,
        }
        os.makedirs(os.path.dirname(CLAUDE_USAGE_LOG), exist_ok=True)
        with open(CLAUDE_USAGE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.exception("failed to log ollama usage for source=%s", source)


# Qwen took over food estimation on this date. Its unedited guesses must not be fed
# back as "ground truth" for later meals — an apple logged at its per-100 g 52 kcal
# kept every later apple at 52-70. Only hand corrections and the earlier
# (Claude-estimated) entries are trusted.
QWEN_FOOD_SINCE = "2026-08-09"


def _trusted_food_entry(day, e):
    return bool(e.get("edited")) or day < QWEN_FOOD_SINCE


def find_similar_food_entries(description, limit=3, days_back=60, min_ratio=0.55):
    """Look back through the food log for entries whose description closely
    matches the given one, most-recent first. Their stored macros reflect any
    manual corrections the user has made since the original model guess,
    so surfacing them lets a repeat entry reuse the corrected numbers instead
    of re-deriving (and repeating) the same estimation mistake."""
    log_data = load_food_log()
    cutoff = datetime.now().date() - timedelta(days=days_back)
    desc_norm = description.strip().lower()
    scored = []
    for day, entries in log_data.items():
        try:
            day_date = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day_date < cutoff:
            continue
        for e in entries:
            other = (e.get("description") or "").strip().lower()
            if not other or not _trusted_food_entry(day, e):
                continue
            ratio = difflib.SequenceMatcher(None, desc_norm, other).ratio()
            if ratio >= min_ratio:
                scored.append((ratio, day, e))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [(day, e) for _, day, e in scored[:limit]]


FOOD_EXCLUDED_FILE = os.path.expanduser("~/.local/share/training-brief/food-excluded.json")


def load_excluded_days():
    """Dates the user flagged as not tracked properly. Their entries stay in the log;
    averages, the calorie chart and the coach just treat the day as missing data."""
    try:
        with open(FOOD_EXCLUDED_FILE) as f:
            d = json.load(f)
        return set(d) if isinstance(d, list) else set()
    except (OSError, ValueError):
        return set()


def toggle_excluded_day(day):
    datetime.strptime(day, "%Y-%m-%d")
    days = load_excluded_days()
    days.symmetric_difference_update({day})
    os.makedirs(os.path.dirname(FOOD_EXCLUDED_FILE), exist_ok=True)
    tmp = FOOD_EXCLUDED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(days), f)
    os.replace(tmp, FOOD_EXCLUDED_FILE)
    return day in days


FOOD_PENDING_FILE = os.path.expanduser("~/.local/share/training-brief/food-pending.json")
FOOD_DRAFT_FILE   = os.path.expanduser("~/.local/share/training-brief/food-draft.txt")
_pending_lock = threading.Lock()


def _load_pending_unlocked():
    try:
        with open(FOOD_PENDING_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save_pending_unlocked(items):
    os.makedirs(os.path.dirname(FOOD_PENDING_FILE), exist_ok=True)
    tmp = FOOD_PENDING_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f)
    os.replace(tmp, FOOD_PENDING_FILE)


def pending_add(item):
    """Entries are written here the moment they are submitted and only removed once logged
    (or rejected as not-food), so a restart or crash can never silently drop one."""
    with _pending_lock:
        _save_pending_unlocked(_load_pending_unlocked() + [item])


def pending_update(item_id, **fields):
    with _pending_lock:
        items = _load_pending_unlocked()
        for it in items:
            if it.get("id") == item_id:
                it.update(fields)
        _save_pending_unlocked(items)


def pending_remove(item_id):
    with _pending_lock:
        _save_pending_unlocked([i for i in _load_pending_unlocked() if i.get("id") != item_id])


def load_pending():
    with _pending_lock:
        return _load_pending_unlocked()


def save_food_draft(text):
    """Unsent food-box text, so a page reload or restart does not wipe a dictated entry."""
    try:
        os.makedirs(os.path.dirname(FOOD_DRAFT_FILE), exist_ok=True)
        if text:
            with open(FOOD_DRAFT_FILE, "w") as f:
                f.write(text)
        elif os.path.exists(FOOD_DRAFT_FILE):
            os.remove(FOOD_DRAFT_FILE)
    except OSError:
        log.exception("could not save food draft")


def load_food_draft():
    try:
        with open(FOOD_DRAFT_FILE) as f:
            return f.read()
    except OSError:
        return ""


def save_food_log(log_data):
    os.makedirs(os.path.dirname(FOOD_LOG_FILE), exist_ok=True)
    with open(FOOD_LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)


# A spoken recap ("so today I've had X followed by Y and I've also had Z") is several
# meals, but the macro prompt treats its input as ONE meal, so it all landed in a
# single entry. Meals are split here, deterministically, at the words people use to
# move from one eating occasion to the next; each piece is then estimated on its own.
_MEAL_BREAK = re.compile(
    r"(?:(?<=[.!?])\s+)"                                             # sentence boundary
    r"|\b(?:followed by|and then|then|later on|later|afterwards|after that|after this"
    r"|on top of that|i(?:'ve| have) also had|also had)\b"
    r"|\bafter (?:the|my|a|an|that) [\w' -]{1,30}? i (?:had|ate)\b"
    r"|(?=\bfor (?:breakfast|brunch|lunch|dinner|tea|supper|pudding|dessert|a snack)\b)",
    re.I)
_MEAL_LABEL = re.compile(r"^for (breakfast|brunch|lunch|dinner|tea|supper|pudding|dessert|a snack)\b[,\s]*", re.I)
_MEAL_TIMES = {"breakfast": "08:30", "brunch": "11:00", "lunch": "13:00", "tea": "18:30",
               "dinner": "19:00", "supper": "19:00", "pudding": "20:00", "dessert": "20:00"}
_MEAL_FILLER = re.compile(
    r"^(?:\s|[,.;:]|\b(?:so|and|also|then|ok(?:ay)?|um+|uh+|er+|right|today|tonight|this "
    r"(?:morning|afternoon|evening)|i(?:'ve| have)?\s*(?:had|eaten)|i ate|i've got|i've)\b)+", re.I)


# Words that carry no food ("just now", "earlier", "ok"): an entry made only of these is
# a mis-typed time or chatter, and the model will happily invent a food for it.
_NON_FOOD_WORDS = {
    "just", "now", "earlier", "today", "tonight", "yesterday", "later", "ok", "okay", "yes",
    "yeah", "no", "thanks", "thank", "you", "and", "then", "also", "the", "it", "that", "this",
    "some", "something", "more", "too", "well", "um", "uh", "hi", "hello", "test", "x"}


def _stem(w):
    return w[:-2] if w.endswith("es") and len(w) > 4 else w[:-1] if w.endswith("s") and len(w) > 3 else w


# Dishes whose ingredients the model names instead (a "sandwich" comes back as bread).
_MEAL_NOUNS = {
    "sandwich", "sarnie", "toastie", "burger", "wrap", "sub", "butty", "bap", "pizza", "curry",
    "stew", "soup", "salad", "smoothie", "shake", "cereal", "porridge", "meal", "breakfast",
    "brunch", "lunch", "dinner", "supper", "snack", "takeaway", "fry", "roast", "pie", "pasty",
    "kebab", "burrito", "taco", "noodles", "stirfry", "sushi", "omelette", "scramble"}


def _content_words(text):
    return [w for w in re.findall(r"[a-z']+", (text or "").lower())
            if len(w) >= 3 and w not in _NON_FOOD_WORDS]


def split_meals(text):
    """[(description, time or None)] — one per eating occasion. Text that doesn't
    split (a normal typed entry) comes back as a single meal, unchanged."""
    out = []
    for piece in _MEAL_BREAK.split(text or ""):
        piece = (piece or "").strip()
        time_hint = None
        m = _MEAL_LABEL.match(piece)
        if m:
            label = m.group(1).lower().replace("a snack", "snack")
            time_hint = _MEAL_TIMES.get(label)
            piece = piece[m.end():]
        piece = _MEAL_FILLER.sub("", piece)
        piece = re.sub(r"(?:[\s,;]|\band\b)+$", "", piece, flags=re.I).strip(" .")
        if len(re.sub(r"\W", "", piece)) >= 3:
            out.append((piece[0].upper() + piece[1:], time_hint))
    if len(out) <= 1:
        return [(text.strip(), None)]
    return out


# Per-100 g nutrition for the foods he actually eats (UK CoFID 2021 by food code,
# USDA / product labels where CoFID has none) plus unit weights — built by
# build_food_table.py. Qwen's own macro guesses were run-to-run noise (one slice of
# toast came out at 65 kcal in one run and 260 in the next), so the model now only
# names each food and its amount; the numbers come from this table.
FOOD_TABLE_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "food_table.json")
_FOOD_TABLE_CACHE = {"mtime": None, "table": {}, "alias": {}}
_FOOD_KEYS = ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")


def load_food_table():
    try:
        mtime = os.path.getmtime(FOOD_TABLE_FILE)
    except OSError:
        return {}, {}
    if _FOOD_TABLE_CACHE["mtime"] != mtime:
        with open(FOOD_TABLE_FILE) as f:
            table = json.load(f)
        alias = {}
        for key, food in table.items():
            for name in [key, *food.get("aliases", [])]:
                alias.setdefault(name.lower(), key)
        _FOOD_TABLE_CACHE.update(mtime=mtime, table=table, alias=alias)
    return _FOOD_TABLE_CACHE["table"], _FOOD_TABLE_CACHE["alias"]


def _resolve_table_item(item, table, alias, description=None):
    """Macros for one model-named item from the table, or None if it isn't a table
    food (or the amount is implausible), in which case the model's estimate stands."""
    key = _match_food_key(item, alias)
    if not key:
        return None
    food = table[key]
    units = food["units"]
    unit = str(item.get("unit") or food["default_unit"]).strip().lower()
    if unit not in units:
        unit = next((u for u in (unit.rstrip("s"), unit.replace("grams", "g"))
                     if u in units), food["default_unit"])
    try:
        amount = float(item.get("amount") or 1)
    except (TypeError, ValueError):
        amount = 1.0
    # A single-serving dish with a "whole" size (a pizza) is eaten whole unless the text says a part:
    # the model once logged "Hawaiian pizza" as one 50 g slice (136 kcal) when it was the whole thing.
    if (description and food["default_unit"] == "whole" and "whole" in units and unit != "whole"
            and not re.search(r"\b(slice|slices|piece|pieces|half|halves|quarter|bit|some|few|bite|bites|share|shared)\b|\d\s*g\b|\bgrams?\b",
                              description.lower())):
        unit, amount = "whole", 1.0
    grams = amount * units[unit]
    if not 0 < grams <= 3000:
        return None
    per = food["per100"]
    out = {"name": item.get("name") or key, "grams": round(grams),
           "source": f"table: {key} ({food['source']})"}
    for k in _FOOD_KEYS:
        out[k] = round(per["kcal" if k == "calories" else k] * grams / 100, 1)
    return out


def _match_food_key(item, alias):
    """Table key for a model-named item. The model often writes a near-miss of the
    key ("back bacon" for "back bacon (grilled)"), and an exact-match miss silently
    dropped the bacon from a bacon sandwich to 0 kcal, so this also tries the key
    without its bracketed note, then a close fuzzy match, on both the food and the
    item's own name."""
    for raw in (item.get("food"), item.get("name")):
        text = str(raw or "").strip().lower()
        if not text or text == "other":
            continue
        if text in alias:
            return alias[text]
        bare = {re.sub(r"\s*\(.*?\)", "", a): k for a, k in alias.items()}
        text_bare = re.sub(r"\s*\(.*?\)", "", text)
        if text_bare in bare:
            return bare[text_bare]
        close = difflib.get_close_matches(text_bare, list(bare), n=1, cutoff=0.8)
        if close:
            return bare[close[0]]
    return None


def _food_menu(table):
    return "\n".join(f"  {key}: {', '.join(u for u in food['units'] if u != 'g')}"
                      for key, food in table.items())


def log_food_via_qwen(description, time_hint=None, day=None):
    """Append one meal entry to today's food-log.json using a local Qwen model
    (via Ollama) to estimate macros, with this process doing the actual file
    write and totals — the model only ever returns a JSON snippet, it never
    touches the filesystem. Returns (ok, error_message)."""
    if not _content_words(description):
        return False, "That doesn't look like a food — describe what you ate (use the date box for a past day)."

    today = datetime.now().strftime("%Y-%m-%d")
    if day and day != today:
        # Backlog: a past day has no "now", so default to midday unless the meal word
        # (breakfast/dinner...) or the description says otherwise.
        now_t = time_hint or "12:00"
    else:
        day = today
        now_t = time_hint or datetime.now().strftime("%H:%M")

    similar = find_similar_food_entries(description)
    if similar:
        examples = []
        for day, e in similar:
            items_str = "; ".join(
                f'{i.get("name")}: {i.get("calories")} kcal, '
                f'{i.get("protein_g")}P/{i.get("carbs_g")}C/{i.get("fat_g")}F g'
                for i in e.get("items", [])
            )
            examples.append(f'- [{day}] "{e.get("description")}" -> {items_str}')
        reference_block = (
            "\n\nYou have logged similar meals before. These figures may include "
            "corrections the user made afterwards, so treat them as ground truth "
            "for any matching item and reuse the same per-item macros rather than "
            "re-estimating from scratch. Only deviate where this description "
            "clearly specifies a different food or quantity:\n" + "\n".join(examples)
        )
    else:
        reference_block = ""

    table, alias = load_food_table()
    # Static part first and byte-identical on every call (instructions + the food
    # list), so Ollama reuses its processed prefix; only the tail below changes.
    prompt = (
        'List what was eaten in ONE meal. Respond with JSON only, no other text.\n\n'
        'JSON schema:\n'
        '{"time": "HH:MM", "items": [{"name": "the food as described", "food": "<exact key '
        'from FOOD LIST, or other>", "amount": N, "unit": "<one of that food\'s units, or g/ml>", '
        '"calories": N, "protein_g": N, "carbs_g": N, "fat_g": N}]}\n\n'
        '- A named dish on the FOOD LIST (pizza, a Hawaiian, a curry) is ONE item: never list its toppings or ingredients separately. '
        'With no quantity stated, a pizza is one whole pizza (unit "whole"), not a slice.\n'
        '- One item per distinct food. "X on toast" needs a separate bread item each time it is '
        'said; a sandwich, toastie or bap has 2 slices of bread (or 1 roll); named cooking fat '
        '(butter, oil) is its own item.\n'
        '- food: the closest FOOD LIST key; "other" only when nothing on the list is that food.\n'
        '- amount + unit: the quantity described. If none is given, amount 1 of the food\'s first '
        'listed unit. Fractions are amounts: "a third of a packet" = 0.33 packet, "half a tin" = 0.5 '
        'tin. Pasta/rice/oats in grams are dry weight unless said to be cooked.\n'
        '- calories/protein_g/carbs_g/fat_g: only needed for "other" foods — your estimate for '
        'the whole portion eaten, never per 100 g. Use 0 for listed foods.\n\n'
        'Example: "2 slices of cheddar on sourdough toast" -> items: '
        '[{"name": "cheddar", "food": "cheddar", "amount": 2, "unit": "slice"}, '
        '{"name": "sourdough toast", "food": "sourdough", "amount": 1, "unit": "slice"}]\n\n'
        f'FOOD LIST (key: units):\n{_food_menu(table)}\n\n'
        '----\n'
        f'Time: use "{now_t}" unless the description implies otherwise.'
        f'{reference_block}\n\n'
        f'Food description: "{description}"'
    )

    t0 = time.monotonic()
    try:
        resp = requests.post(
            OLLAMA_CHAT_URL,
            json={
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "stream": False,
            },
            # Queued in the background, and Ollama serialises requests: behind a dashboard
            # refresh (tips + insight calls, several minutes on CPU) 90s wasn't enough.
            timeout=300,
        )
        resp.raise_for_status()
    except requests.RequestException:
        log.exception("food log ollama call failed")
        _log_ollama_usage("food_log", int((time.monotonic() - t0) * 1000), False)
        return False, "Failed to log food — is Ollama running?"
    duration_ms = int((time.monotonic() - t0) * 1000)

    try:
        content = resp.json()["message"]["content"]
        parsed = json.loads(content)
        items = parsed.get("items") or []
        if not items:
            raise ValueError("no items in response")
    except (KeyError, ValueError, json.JSONDecodeError):
        log.error("food log ollama call returned unusable output: %s", resp.text[-500:])
        _log_ollama_usage("food_log", duration_ms, False)
        return False, "Failed to log food — try again."
    _log_ollama_usage("food_log", duration_ms, True)

    # The model sometimes invents a food when the text names none (or leaks one from the
    # similar-meals examples). At least one returned item must be traceable to the words
    # actually typed; implied extras (the bread in a sandwich) can still ride along.
    typed_words = _content_words(description)
    typed = {_stem(w) for w in typed_words}
    if not (any(_stem(w) in _MEAL_NOUNS or w in _MEAL_NOUNS for w in typed_words)
            or any(_stem(w) in typed for it in items
                   for w in _content_words(str(it.get("name", "")) + " " + str(it.get("food", ""))))):
        log.warning("food log rejected %r: model returned %s", description[:60],
                    [it.get("name") for it in items])
        _log_ollama_usage("food_log", duration_ms, False)
        return False, "Couldn't match any food in that — try describing what you ate."

    # The model sometimes adds an invented catch-all line ("protein shake", 500 kcal) on top of
    # the ingredients it was asked to itemise, double counting them. Drop such a bucket when
    # real ingredients are listed alongside it.
    if len(items) >= 3:
        kept = [it for it in items
                if not (str(it.get("food", "")).lower() in ("other", "")
                        and re.search(r"\b(shake|smoothie|mix|meal|plate|combo|platter)\b", str(it.get("name", "")).lower()))]
        if kept:
            items = kept

    # A named dish that is in the food table (a pizza) is ONE item. The model also lists its toppings
    # ("Hawaiian pizza" -> pizza + pineapple + cheese), which counts them twice; drop those unless "extra" was said.
    dish_keys = [_match_food_key(it, alias) for it in items]
    if any(k and k.startswith("pizza") for k in dish_keys) and "extra" not in description.lower():
        toppings = re.compile(r"\b(pineapple|ham|cheese|mozzarella|tomato|pepperoni|mushrooms?|peppers?|onions?|olives?|sweetcorn|chicken|bacon|sauce)\b", re.I)
        items = [it for it, k in zip(items, dish_keys) if (k and k.startswith("pizza")) or not toppings.search(str(it.get("name", "")))]

    resolved = []
    for it in items:
        row = _resolve_table_item(it, table, alias, description)
        if row is None:
            row = {"name": it.get("name") or "food", "source": "qwen estimate",
                   **{k: it.get(k, 0) or 0 for k in _FOOD_KEYS}}
        resolved.append(row)
    items = resolved

    # A sandwich is bread plus filling, and the model forgets the bread (a "pulled lamb
    # sandwich" once logged as lamb and butter only). Add two slices when none was itemised.
    if (re.search(r"\b(sandwich|sarnie|toastie|butty)\b", description.lower())
            and not any(re.search(r"bread|toast|roll|bun|bap|bagel|baguette|pitta|wrap|tortilla|sourdough|panini|ciabatta",
                                  str(i.get("name", "")).lower()) for i in items)):
        bread = _resolve_table_item({"name": "bread (2 slices, assumed)", "food": "white bread",
                                     "amount": 2, "unit": "slice"}, table, alias)
        if bread:
            items.append(bread)

    entry = {
        "id": str(uuid.uuid4()),
        "time": parsed.get("time") or now_t,
        "description": description,
        "source": "qwen",
        "items": items,
        **{k: sum(i.get(k, 0) or 0 for i in items)
           for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")},
    }
    log_data = load_food_log()
    log_data.setdefault(day, []).append(entry)
    save_food_log(log_data)
    log.info("food logged via qwen: %s", description[:80])
    return True, None


def compute_nutrition_context(activities, calorie_target):
    """Small, cheap calorie/training context shared by the full-page render and
    the food-log quick-add refresh, so the two never drift out of sync."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    done_today = [a for a in activities
                  if (a.get("start_date_local") or a.get("start_date") or "")[:10] == today_str]
    session_kcal = sum(int(a.get("calories") or 0) for a in done_today)
    kcal_base    = calorie_target if calorie_target else 2700
    kcal_total   = kcal_base + session_kcal
    _now = datetime.now()
    day_frac = max(0.0, min((_now.hour + _now.minute / 60 - EATING_START_H) /
                            (EATING_END_H - EATING_START_H), 1.0))
    expected_so_far = kcal_total * day_frac
    training_hours_today = sum(
        (a.get("elapsed_time") or a.get("moving_time") or 0) for a in done_today
    ) / 3600
    return {
        "kcal_base": kcal_base, "session_kcal": session_kcal, "kcal_total": kcal_total,
        "day_frac": day_frac, "expected_so_far": expected_so_far,
        "training_hours_today": training_hours_today,
        "protein_target_g": 150,
        "carbs_target_g":   round(kcal_total * 0.55 / 4),
        "fat_target_g":     round(kcal_total * 0.25 / 9),
        "fiber_target_g":   30,
        # Sugar: 50g base + ~30g/hr as fast carb fuel around sessions.
        "sugar_target_g":   round(50 + training_hours_today * 30),
        # Sodium: 2300mg base + ~750mg/hr to replace sweat losses.
        "sodium_target_mg": round(2300 + training_hours_today * 750),
    }


def render_food_log_list(food_data):
    """Just the logged-meals rows (no input row) — safe to swap in on a refresh
    without disturbing whatever the user is mid-typing in the food-box. Each row
    carries its entry as a data-entry JSON blob so editFood() can populate an
    inline edit form without a round trip to Python."""
    import html as _html
    entries = (food_data or {}).get("entries", [])
    if entries:
        rows = "".join(
            f'<div class="food-entry-row" id="food-row-{_html.escape(e.get("id", ""))}" '
            f'data-entry="{_html.escape(json.dumps({k: e.get(k, 0 if k not in ("id","time","description") else "") for k in ("id","time","description","calories","protein_g","carbs_g","fat_g","fiber_g","sugar_g","sodium_mg")}), quote=True)}">'
            f'<span class="food-entry-time">{_html.escape(e.get("time", ""))}</span>'
            f'<span class="food-entry-desc">{_html.escape(e.get("description") or "")}</span>'
            f'<span class="food-entry-kcal">{e.get("calories", 0):.0f} kcal</span>'
            f'<button class="food-entry-btn" onclick="editFood(\'{e.get("id")}\')" title="Edit">&#9998;</button>'
            f'<button class="food-entry-btn" onclick="delFood(\'{e.get("id")}\')" title="Delete">&#10005;</button>'
            '</div>'
            for e in entries
        )
        return f'<div class="food-entry-list">{rows}</div>'
    return '<div class="food-log-empty">No meals logged yet today.</div>'


def render_food_log(food_data):
    """The whole content of the Nutrition tab's "Logged today" card: today's
    logged meals plus the quick-add input. Used only for the initial page
    build — refreshes after a queued entry go through setFoodLogList instead
    so the input row (and whatever the user is typing into it) is untouched."""
    import html as _html
    return (
        f'<div id="food-log-list">{render_food_log_list(food_data)}</div>'
        '<div class="food-input-row">'
        f'<input type="text" id="food-box" value="{_html.escape(load_food_draft(), quote=True)}" placeholder="Log food… (Enter to add)" '
        '''onfocus="warmFoodModel()" '''
        '''onkeydown="if(event.key==='Enter'){event.preventDefault();submitFood();}">'''
        '<input type="date" id="food-date" title="Backlog: log to this day instead of today (clear for today)" '
        '''onchange="this.style.borderColor=this.value?'#e3b95f':''"'''
        ' style="background:#2a2c2f;color:#d4d6d8;border:1px solid #424446;border-radius:5px;padding:4px">'
        '<button id="food-mic-btn" class="mic-btn" onclick="toggleVoice(\'food\')" title="Speak what you ate">&#127908;</button>'
        '<button id="food-add-btn" onclick="submitFood()">Add</button>'
        '</div>'
        '<div class="food-status" id="food-status" style="display:none"></div>'
    )


def render_weekly_food_log(days_back=6):
    """Last `days_back`+1 days as collapsible per-day sections (today expanded,
    the rest collapsed) so mislogged/duplicate/misdated entries — like the jacket
    potato that turned out to belong to a different day — are easy to spot by eye
    instead of only surfacing when the numbers look off."""
    import html as _html
    log_data = load_food_log()
    excluded_days = load_excluded_days()
    today = datetime.now().date()
    blocks = []
    for i in range(days_back, -1, -1):
        d = today - timedelta(days=i)
        day_str = d.strftime("%Y-%m-%d")
        entries = log_data.get(day_str, [])
        total = sum(e.get("calories", 0) for e in entries)
        excluded = day_str in excluded_days
        if entries:
            rows = "".join(
                '<div class="week-entry-row">'
                f'<span class="food-entry-time">{_html.escape(e.get("time", ""))}</span>'
                f'<span class="food-entry-desc">{_html.escape(e.get("description") or "")}</span>'
                f'<span class="food-entry-kcal">{e.get("calories", 0):.0f} kcal</span>'
                '</div>'
                for e in entries
            )
        else:
            rows = '<div class="food-log-empty">Not logged</div>'
        if entries and i > 0:
            rows += (f'<div class="week-day-actions"><button onclick="toggleExcludeDay(&quot;{day_str}&quot;)">'
                     + ("Count this day again" if excluded else "Don&rsquo;t count this day in averages")
                     + '</button></div>')
        is_today = i == 0
        label_cls = " is-today" if is_today else ""
        label = "Today" if is_today else d.strftime("%A %d %b")
        blocks.append(
            f'<details class="week-day"{" open" if is_today else ""}>'
            f'<summary><span class="week-day-label{label_cls}">{label}</span>'
            f'<span class="week-day-total">{"not counted · " if excluded else ""}{total:.0f} kcal</span></summary>'
            f'<div class="week-day-entries">{rows}</div>'
            '</details>'
        )
    return "".join(blocks)


def nutrition_data(food_data, ctx):
    """The `nutrition` block handed to the JS: consumed/target plus per-macro
    grams, targets and kind. Shared by the initial build and the food-add refresh
    so the dashboard and the quick-add path never compute macros differently."""
    fd = food_data or {}
    kcal_base    = ctx["kcal_base"]
    session_kcal = ctx["session_kcal"]
    kcal_total   = ctx["kcal_total"]
    return {
        "consumed": round(fd.get("calories", 0)),
        "target": round(kcal_total),
        "base": kcal_base, "sessionKcal": session_kcal,
        "paceExpected": round(ctx["expected_so_far"]), "dayFrac": round(ctx["day_frac"], 3),
        "eatStart": EATING_START_H, "eatEnd": EATING_END_H,
        "meals": sorted(({
            "time": e.get("time", ""), "desc": e.get("description") or "",
            "kcal": round(e.get("calories", 0)), "p": round(e.get("protein_g", 0), 1),
            "c": round(e.get("carbs_g", 0), 1), "f": round(e.get("fat_g", 0), 1),
            "items": [i.get("name", "") for i in e.get("items", []) if i.get("name")],
        } for e in fd.get("entries", [])), key=lambda m: m["time"]),
        "macros": [
            {"key": "protein", "label": "Protein", "color": "#88dcfd",
             "g": round(fd.get("protein_g", 0), 1), "target": ctx["protein_target_g"],
             "kcalPerG": 4, "kind": "aim"},
            {"key": "carbs", "label": "Carbs", "color": "#b79cf0",
             "g": round(fd.get("carbs_g", 0), 1), "target": ctx["carbs_target_g"],
             "kcalPerG": 4, "kind": "aim"},
            {"key": "fat", "label": "Fat", "color": "#e3b95f",
             "g": round(fd.get("fat_g", 0), 1), "target": ctx["fat_target_g"],
             "kcalPerG": 9, "kind": "aim"},
            {"key": "fiber", "label": "Fibre", "color": "#6fcf97",
             "g": round(fd.get("fiber_g", 0), 1), "target": ctx["fiber_target_g"], "kind": "aim"},
            {"key": "sugar", "label": "Sugar", "color": "#88dcfd",
             "g": round(fd.get("sugar_g", 0), 1), "target": ctx["sugar_target_g"], "kind": "limit"},
            {"key": "sodium", "label": "Sodium", "color": "#e3b95f", "unit": "mg",
             "g": round(fd.get("sodium_mg", 0)), "target": ctx["sodium_target_mg"], "kind": "limit"},
        ],
    }


def load_food_log():
    if not os.path.exists(FOOD_LOG_FILE):
        return {}
    try:
        with open(FOOD_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def delete_food_entry(entry_id):
    """Remove one entry (by id) from today's food log. There's no agentic
    claude -p to ask for a fix anymore, so this + update_food_entry are the
    in-dashboard replacement for manually correcting a logged meal."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_data = load_food_log()
    log_data[today] = [e for e in log_data.get(today, []) if e.get("id") != entry_id]
    save_food_log(log_data)


def update_food_entry(entry_id, fields):
    """Apply hand-edited time/description/macro totals to today's entry with
    this id. Collapses items to a single item matching the new totals, so the
    entry stays internally consistent and find_similar_food_entries continues
    to surface the corrected numbers for future similar meals."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_data = load_food_log()
    for e in log_data.get(today, []):
        if e.get("id") != entry_id:
            continue
        for k in ("time", "description", "calories", "protein_g", "carbs_g", "fat_g"):
            if k in fields:
                e[k] = fields[k]
        e["edited"] = True
        e["items"] = [{
            "name": e.get("description") or "meal",
            "calories": e.get("calories", 0), "protein_g": e.get("protein_g", 0),
            "carbs_g": e.get("carbs_g", 0), "fat_g": e.get("fat_g", 0),
            "fiber_g": e.get("fiber_g", 0), "sugar_g": e.get("sugar_g", 0),
            "sodium_mg": e.get("sodium_mg", 0),
        }]
        break
    save_food_log(log_data)


def get_today_nutrition():
    log = load_food_log()
    today_key = datetime.now().strftime("%Y-%m-%d")
    entries = log.get(today_key, [])
    totals = {k: sum(e.get(k, 0) for e in entries)
              for k in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")}
    return {
        "entries":     entries,
        "entry_count": len(entries),
        **totals,
    }


def get_weekly_food_summary():
    log = load_food_log()
    lines = []
    for i in range(6, -1, -1):
        d         = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_label = (datetime.now() - timedelta(days=i)).strftime("%A %d %b")
        entries   = [] if d in load_excluded_days() else log.get(d, [])
        if entries:
            def _entry_foods(e):
                items = e.get("items", [])
                if items:
                    return "; ".join(i.get("name", "") for i in items if i.get("name"))
                return e.get("description", "")
            foods = " | ".join(_entry_foods(e) for e in entries)
            cals  = sum(e.get("calories",  0) for e in entries)
            prot  = sum(e.get("protein_g", 0) for e in entries)
            lines.append(f"{day_label}: {foods} ({cals:.0f} kcal, {prot:.0f}g protein)")
        else:
            lines.append(f"{day_label}: not logged")
    return lines


def get_stretch_status():
    """Returns (streak_days, stretched_today)."""
    log = {}
    if os.path.exists(STRETCH_LOG_FILE):
        try:
            with open(STRETCH_LOG_FILE) as f:
                log = json.load(f)
        except Exception:
            pass
    today = datetime.now().date()
    stretched_today = bool(log.get(today.isoformat()))
    streak = 0
    d = today
    while True:
        if log.get(d.isoformat()):
            streak += 1
            d -= timedelta(days=1)
        elif d == today:
            d -= timedelta(days=1)
            if log.get(d.isoformat()):
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        else:
            break
    return streak, stretched_today


def _data_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def get_calorie_history(calorie_base: int, days: int = 14, activities: list = None):
    """Returns (dates, consumed_list, target_list) with per-day training-adjusted targets."""
    log = load_food_log()
    excluded = load_excluded_days()
    # Build per-day session calorie burn from activities
    session_kcal_by_day: dict = {}
    for a in (activities or []):
        d = (a.get("start_date_local") or a.get("start_date") or "")[:10]
        if d:
            session_kcal_by_day[d] = session_kcal_by_day.get(d, 0) + int(a.get("calories") or 0)
    dates, consumed, targets = [], [], []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        entries = log.get(d, [])
        kcal = sum(e.get("calories", 0) for e in entries) if entries and d not in excluded else None
        dates.append(d)
        consumed.append(round(kcal) if kcal is not None else None)
        targets.append(calorie_base + session_kcal_by_day.get(d, 0))
    return dates, consumed, targets


def get_bulk_trend(dates, consumed, targets, surplus_goal: int = 300, window: int = 7,
                    today_str: str = None, day_frac: float = None):
    """Rolling-average surplus over the trailing `window` *complete, logged* days, projected
    to weekly weight change. Deliberately judged as a trailing average against
    maintenance+burn rather than a per-day quota: a heavy training day landing at maintenance
    is fine on its own, it just means lighter days need to pick up more of the surplus for
    the average to hold. ~6500 kcal/kg is used for the projection (lower than the pure-fat
    7700 figure, since a resistance-trained surplus partitions part of the gain into cheaper
    lean tissue).

    Today is deliberately excluded from the average — it's not over yet, so its balance
    isn't a fair data point until it completes. Instead, if today has partial data, its
    likely end-of-day balance is extrapolated from the pace so far and reported separately
    as `today_projected`, without polluting the historical average.
    """
    is_today = [d == today_str for d in dates]
    daily_balance = [(c - t, today) for c, t, today in zip(consumed, targets, is_today)
                      if c is not None and not today]
    today_projected = None
    if today_str in dates:
        idx = dates.index(today_str)
        if consumed[idx] is not None and day_frac and day_frac > 0.02:
            today_projected = round(consumed[idx] / day_frac - targets[idx])

    if not daily_balance:
        if today_projected is None:
            return None
        avg_surplus, logged_days = 0, 0
    else:
        recent = [b for b, _ in daily_balance[-window:]]
        avg_surplus = sum(recent) / len(recent)
        logged_days = len(recent)

    kg_per_week = avg_surplus * 7 / 6500
    if avg_surplus >= surplus_goal * 0.6:
        state = "on track"
    elif avg_surplus >= 0:
        state = "slow but positive"
    else:
        state = "in deficit"
    return {
        "avg_surplus": round(avg_surplus),
        "logged_days": logged_days,
        "window": window,
        "kg_per_week": round(kg_per_week, 2),
        "state": state,
        "today_projected": today_projected,
    }

# ── Voice food logging — recordCLI-style Whisper, then the usual Qwen parse ──

VOICE_MODEL       = "small.en"   # already cached by recordCLI; ~2-4s per clip on CPU
VOICE_SAMPLERATE  = 16000
VOICE_MAX_SECONDS = 90           # a forgotten-open mic stops itself


class VoiceFoodRecorder:
    """Mic → faster-whisper transcript for the food box. Same capture and model
    setup as recordCLI (sounddevice 16 kHz mono float32, int8 CPU Whisper), but a
    single clip per press rather than a live stream. The transcript goes into the
    food box rather than straight to the log: a misheard "15g" as "50g" silently
    skews the day's totals, so it gets a glance before Enter."""

    def __init__(self):
        self._model = None
        self._model_lock = threading.Lock()
        self._chunks = []
        self._stream = None
        self._started = 0.0

    def _load_model(self):
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(VOICE_MODEL, device="cpu", compute_type="int8")
            return self._model

    @property
    def recording(self):
        return self._stream is not None

    def start(self, on_timeout):
        import sounddevice as sd
        self._chunks = []

        def cb(indata, frames, t, status):
            self._chunks.append(indata[:, 0].copy())
            if time.monotonic() - self._started > VOICE_MAX_SECONDS:
                GLib.idle_add(lambda: on_timeout() or False)

        self._stream = sd.InputStream(samplerate=VOICE_SAMPLERATE, channels=1,
                                      dtype="float32", blocksize=1024, callback=cb)
        self._started = time.monotonic()
        self._stream.start()
        # Load Whisper while he's still talking, so stop → text is just the transcribe.
        threading.Thread(target=self._load_model, daemon=True).start()

    def stop(self):
        """Stop capture and return the audio; transcribe() is the slow part."""
        import numpy as np
        stream, self._stream = self._stream, None
        if stream:
            stream.stop()
            stream.close()
        return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype="float32")

    def transcribe(self, audio, prompt=""):
        if len(audio) < VOICE_SAMPLERATE // 2:
            return ""
        segments, _ = self._load_model().transcribe(
            audio, beam_size=5, language="en", vad_filter=True,
            initial_prompt=prompt or None)
        return " ".join(seg.text.strip() for seg in segments).strip()


# Whisper hint for the week planner: the words he plans with, so "3 by 10" and
# "RDL" come out as written rather than as near-homophones.
VOICE_PLAN_PROMPT = ("Training plan. Today 60 minute easy bike, tomorrow gym lower, Monday 3x10 "
                     "erg at threshold, Tuesday easy row, Peloton ride, WaterRower, RDL, hip thrust, "
                     "squat, 2k pace, rest day.")


def _voice_food_prompt():
    """Whisper's initial_prompt biases it toward vocabulary it has just "heard", so
    feeding it his recent meal descriptions is the food-log equivalent of recordCLI's
    glossary: brand names and odd foods he actually eats come out spelled right."""
    food_log, seen, out = load_food_log(), set(), []
    for day in sorted(food_log, reverse=True)[:21]:
        for e in reversed(food_log[day]):
            d = (e.get("description") or "").strip()
            if d and d.lower() not in seen:
                seen.add(d.lower())
                out.append(d)
    # Whisper keeps only the last ~224 tokens of the prompt, so stay well inside it.
    text = "Food log. " + ". ".join(out)
    return text[:700]


def warm_local_model():
    """Best-effort, fire-and-forget: ask Ollama to load LOCAL_MODEL into memory
    (and hold it there for 5min) ahead of an expected call. Sending a /api/generate
    request with no prompt loads the model without generating anything. Meant to be
    called on its own thread — swallows errors since this is purely an optimization."""
    try:
        requests.post(
            "http://localhost:11434/api/generate",
            json={"model": LOCAL_MODEL, "keep_alive": "5m"},
            timeout=30,
        )
    except requests.RequestException:
        pass


def _ollama_p(prompt: str, timeout: int = 240, source: str = "unknown", model: str = LOCAL_MODEL) -> str:
    """Call a local Qwen model (via Ollama) with the given prompt and return the
    stripped reply text. Logs timing to CLAUDE_USAGE_LOG under `source`. Defaults
    to the 7B model; pass model=LOCAL_MODEL_FAST for long/narrative prompts where
    7B's generation time is impractical on CPU."""
    t0 = time.monotonic()
    try:
        resp = requests.post(
            OLLAMA_CHAT_URL,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        _log_ollama_usage(source, int((time.monotonic() - t0) * 1000), False, model=model)
        log.error("ollama call failed for source=%s: %s", source, e)
        raise RuntimeError(f"local model call failed: {e}")
    duration_ms = int((time.monotonic() - t0) * 1000)
    _log_ollama_usage(source, duration_ms, True, model=model)
    return resp.json()["message"]["content"].strip()

def _load_cache(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_cache(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


# The food log is partial by nature (meals get forgotten, days go unlogged). The model used
# to read gaps as eating failures or lecture about logging; it should treat the log as a
# limited sample and say so, never police it.
LOGGING_NOTE = (
    "ABOUT THE DATA: the food log is an incomplete sample — unlogged days and meals are "
    "missing data, NOT days or meals Alex skipped. Never criticise, remind or comment on his "
    "logging habits or on missing entries. Where a conclusion rests on the log, frame it "
    "briefly as \"based on the limited data we have\" and keep it tentative; never present "
    "a gap in the log as a gap in his eating.\n\n"
)


# Fruit/veg presence is computed here, not by the model: counting "days with veg" across a
# week is exactly what the small local model gets wrong (it once claimed a gap on "4 of 7
# days" when only 2 days were logged and both held berries, beetroot and kale).
_FRUIT_VEG_WORDS = (
    "apple", "pear", "banana", "orange", "satsuma", "clementine", "mandarin", "grape", "melon", "mango",
    "pineapple", "kiwi", "peach", "plum", "cherry", "cherries", "berry", "berries", "blueberr", "raspberr",
    "blackberr", "strawberr", "lemon", "lime", "fig", "date", "raisin", "avocado", "tomato", "onion", "garlic",
    "carrot", "beetroot", "kale", "spinach", "cabbage", "broccoli", "cauliflower", "lettuce", "salad", "rocket",
    "cucumber", "courgette", "pepper", "mushroom", "pea", "sweetcorn", "leek", "celery", "asparagus", "aubergine",
    "squash", "pumpkin", "sprout", "greens", "veg", "vegetable", "radish", "turnip", "parsnip", "swede", "fennel",
    "bean", "lentil", "chickpea", "sweet potato")
_FRUIT_VEG_NOT = ("oil", "juice", "jam", "sauce", "ketchup", "crisps", "peanut", "peanut butter", "chickpea flour",
                  "bean sprout flour", "pepper steak", "date syrup", "tomato ketchup", "lemon curd", "fig roll")


def _is_fruit_veg(name):
    n = (name or "").lower()
    return any(w in n for w in _FRUIT_VEG_WORDS) and not any(x in n for x in _FRUIT_VEG_NOT)


def fruit_veg_summary(days=7):
    """(logged_days, [(day, [fruit/veg item names])]) over the last `days`, excluding days
    flagged as not tracked. A day counts as logged if it has any entries."""
    log = load_food_log()
    excluded = load_excluded_days()
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        entries = log.get(d, [])
        if not entries or d in excluded:
            continue
        names = []
        for e in entries:
            for it in e.get("items", []):
                if _is_fruit_veg(it.get("name")) and it["name"] not in names:
                    names.append(it["name"])
        out.append((d, names))
    return out


MIN_DAYS_FOR_PATTERN = 4   # fewer logged days than this and no "consistent gap" can be claimed


def get_nutrition_insight():
    """Returns (insight_str, is_stale)."""
    weekly      = get_weekly_food_summary()
    logged      = [l for l in weekly if "not logged" not in l]
    today_data  = get_today_nutrition()
    today_str   = (
        f"Today so far: {today_data['calories']:.0f} kcal, "
        f"{today_data['protein_g']:.0f}g protein, "
        f"{today_data['carbs_g']:.0f}g carbs, "
        f"{today_data['fat_g']:.0f}g fat — "
        + " | ".join(
            "; ".join(i.get("name", "") for i in e.get("items", []) if i.get("name")) or e.get("description", "")
            for e in today_data["entries"]
        )
    ) if today_data["entry_count"] > 0 else "Today: no meals logged yet."
    current_hour = datetime.now().hour
    time_slot    = "morning" if current_hour < 12 else "afternoon" if current_hour < 18 else "evening"
    time_context = f"Current time: {current_hour:02d}:00 ({time_slot})"
    input_hash  = _data_hash("\n".join(weekly) + today_str + time_context)

    cache = _load_cache(NUTR_CACHE_FILE)
    if cache.get("hash") == input_hash:
        return cache.get("insight", ""), False

    fv = fruit_veg_summary()
    if 0 < len(fv) < MIN_DAYS_FOR_PATTERN:
        with_fv = [(d, n) for d, n in fv if n]
        items = sorted({x for _, n in with_fv for x in n})
        text = (f"Based on the limited data we have ({len(fv)} logged day{'s' if len(fv) != 1 else ''} this week), "
                f"there isn't enough to call any pattern yet. ")
        if with_fv:
            text += (f"Fruit or veg showed up on {len(with_fv)} of those {len(fv)} days"
                     f" ({', '.join(items[:8])}{'…' if len(items) > 8 else ''}).")
        else:
            text += "No fruit or veg has shown up in what's been logged so far, which may just reflect what was logged."
        _save_cache(NUTR_CACHE_FILE, {"hash": input_hash, "insight": text})
        return text, False

    if not logged and today_data["entry_count"] == 0:
        return "There isn't enough food data yet this week for an assessment.", False

    prompt = (
        "You are a sports nutritionist advising a solo rower targeting a 2k erg from 6:29 to 6:10 "
        "over a year, with a lean mass gain goal of 83→90kg (~300 kcal/day surplus, ~2g protein/kg). "
        "Review the last 7 days of food logs and give an honest, balanced weekly assessment.\n\n"
        + LOGGING_NOTE +
        "Rules:\n"
        "- Only flag a nutrient or food group as a genuine concern if it is CONSISTENTLY absent or "
        "  underrepresented across MOST days (4+ out of 7) — not just today or one day.\n"
        "- If a food group appears some days but not others, say so accurately — do not call it a deficiency.\n"
        "- If oily fish, fruit, or veg appeared earlier in the week, acknowledge that explicitly.\n"
        "- Do NOT judge today's intake in isolation — today is not over unless it is evening (after 18:00).\n"
        "- Be accurate, not alarmist. If the week looks broadly reasonable, say so.\n\n"
        "Then suggest ONE specific food or meal that would genuinely fill the most consistent gap, "
        "if one exists. If there is no clear consistent gap, say so and suggest something to maintain "
        "the current pattern.\n\n"
        f"{time_context}\n{today_str}\n\n"
        "VERIFIED FACTS (computed, do not contradict): fruit or veg appeared on "
        f"{sum(1 for _, n in fv if n)} of {len(fv)} logged days"
        + (" (" + "; ".join(f"{d}: {', '.join(n) or 'none'}" for d, n in fv) + ")") + ".\n\n"
        "Last 7 days:\n" + "\n".join(weekly) + "\n\n"
        "Write exactly 3 sentences. Be specific about which days/foods you are referencing. "
        "No fluff, no generic advice, no greetings, no markdown formatting."
    )
    insight = _ollama_p(prompt, source="nutrition_insight")
    _save_cache(NUTR_CACHE_FILE, {"hash": input_hash, "insight": insight})
    return insight, False


def get_calorie_gap_suggestion(food_data, ctx):
    """A one-line meal suggestion sized to bring today back onto the eating pace.

    The gap is measured against the *pace marker* — what you should have eaten by
    now — not the end-of-day total. Sizing a snack to the whole remaining day tells
    you to eat 2,000 kcal at breakfast, which is not advice.

    Returns None when at or ahead of pace. Cheap and cached per day/consumed/gap,
    so it is safe to call on every render.
    """
    consumed  = (food_data or {}).get("calories", 0)
    pace_kcal = ctx["expected_so_far"]
    gap_kcal  = pace_kcal - consumed
    if gap_kcal <= 50:
        return None

    # Macro shortfalls are pro-rated to the same point in the day as the kcal gap;
    # comparing intake-so-far against a full-day macro target overstates every one.
    frac = ctx["day_frac"] or 1.0
    def short(target_key, eaten_key):
        return max(round(ctx[target_key] * frac - (food_data or {}).get(eaten_key, 0)), 0)

    remaining_p = short("protein_target_g", "protein_g")
    remaining_c = short("carbs_target_g",   "carbs_g")
    remaining_f = short("fat_target_g",     "fat_g")

    today_str   = datetime.now().strftime("%Y-%m-%d")
    input_hash  = _data_hash(f"{today_str}|{round(consumed)}|{round(gap_kcal)}")
    cache = _load_cache(CAL_GAP_CACHE_FILE)
    if cache.get("hash") == input_hash:
        return cache.get("suggestion")

    prompt = (
        "You are a sports nutritionist advising a solo rower on a lean bulk "
        "(~2g protein/kg, ~300 kcal/day surplus target). They are behind their eating "
        f"pace for the time of day by {round(gap_kcal)} kcal "
        f"({remaining_p}g protein, {remaining_c}g carbs, {remaining_f}g fat behind pace). "
        f"Their full-day target is {round(ctx['kcal_total'])} kcal and they have eaten "
        f"{round(consumed)} kcal so far.\n\n"
        "Suggest ONE realistic meal or snack, with rough quantities, sized to close the "
        "gap to pace — not the whole remaining day. One sentence, no markdown, no greetings. "
        "The log may simply be incomplete, so do not remark on logging."
    )
    try:
        suggestion = _ollama_p(prompt, source="calorie_gap_suggestion")
    except Exception:
        log.exception("calorie gap suggestion failed")
        return None
    _save_cache(CAL_GAP_CACHE_FILE, {"hash": input_hash, "suggestion": suggestion})
    return suggestion


# ── Data fetching (with retry) ────────────────────────────────────────────────

def fetch_with_retry(fn, status_cb, max_attempts=4):
    """Call fn(), retrying with exponential backoff on failure."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = 2 ** attempt  # 1, 2, 4 s
            status_cb(f"intervals.icu unavailable — retrying in {wait}s… ({attempt+1}/{max_attempts-1})")
            time.sleep(wait)


def fetch_wellness(athlete_id, api_key, days=920):
    oldest = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    newest = datetime.now().strftime("%Y-%m-%d")
    r = requests.get(f"{BASE_URL}/athlete/{athlete_id}/wellness",
                     params={"oldest": oldest, "newest": newest},
                     auth=("API_KEY", api_key), timeout=12)
    r.raise_for_status()
    return r.json()


def fetch_activities(athlete_id, api_key, days=790):
    oldest = datetime.now() - timedelta(days=days)
    r = requests.get(f"{BASE_URL}/athlete/{athlete_id}/activities",
                     params={"oldest": oldest.strftime("%Y-%m-%dT00:00:00"),
                             "newest": datetime.now().strftime("%Y-%m-%dT23:59:59")},
                     auth=("API_KEY", api_key), timeout=12)
    r.raise_for_status()
    acts = r.json()
    acts.sort(key=lambda a: a.get("start_date_local") or a.get("start_date") or "")
    return acts


def fetch_sport_settings(athlete_id, api_key):
    """Per-sport HR zones and thresholds. The brief invites heart-rate advice, so
    without these the model has to invent the very numbers it is being asked to give."""
    r = requests.get(f"{BASE_URL}/athlete/{athlete_id}/sport-settings",
                     auth=("API_KEY", api_key), timeout=12)
    r.raise_for_status()
    return r.json()


# ── Computed series ──────────────────────────────────────────────────────────

_HI_SPORT_WEIGHTS = {
    "Rowing": 1.0, "VirtualRow": 1.0, "Other": 1.0,
    "Run": 0.8, "VirtualRun": 0.8,
    "Ride": 0.7, "VirtualRide": 0.7, "Workout": 0.8,
}


def compute_hi_load_series(activities, days=730, tau=14):
    decay = math.exp(-1 / tau)
    hi_by_date = defaultdict(float)
    for a in activities:
        weight = _HI_SPORT_WEIGHTS.get(a.get("type", ""), 0.0)
        if weight == 0.0:
            continue
        date_str   = (a.get("start_date_local") or a.get("start_date") or "")[:10]
        zone_times = a.get("icu_hr_zone_times") or []
        hi_by_date[date_str] += sum(zone_times[i] for i in range(3, min(7, len(zone_times)))) / 60 * weight

    current = 0.0
    warmup = max(60, tau * 4)  # 4 time constants of warm-up
    d = datetime.now() - timedelta(days=days + warmup)
    cutoff  = datetime.now() - timedelta(days=days)
    while d.date() < cutoff.date():
        current = current * decay + hi_by_date.get(d.strftime("%Y-%m-%d"), 0.0) * (1 - decay)
        d += timedelta(days=1)

    dates, vals = [], []
    while d.date() <= datetime.now().date():
        current = current * decay + hi_by_date.get(d.strftime("%Y-%m-%d"), 0.0) * (1 - decay)
        dates.append(d.strftime("%Y-%m-%d"))
        vals.append(round(current, 2))
        d += timedelta(days=1)
    return dates, vals


def compute_hi_load_projection(activities, training_plan, hil_series=None, tau=14):
    """hil_series: (dates, vals) from compute_hi_load_series; used as anchor to avoid discontinuity."""
    decay = math.exp(-1 / tau)

    if hil_series and hil_series[1]:
        current = hil_series[1][-1]
    else:
        hi_by_date = defaultdict(float)
        for a in activities:
            weight = _HI_SPORT_WEIGHTS.get(a.get("type", ""), 0.0)
            if weight == 0.0:
                continue
            date_str   = (a.get("start_date_local") or a.get("start_date") or "")[:10]
            zone_times = a.get("icu_hr_zone_times") or []
            hi_by_date[date_str] += sum(zone_times[i] for i in range(3, min(7, len(zone_times)))) / 60 * weight
        current = 0.0
        d = datetime.now() - timedelta(days=394)
        while d.date() <= datetime.now().date():
            current = current * decay + hi_by_date.get(d.strftime("%Y-%m-%d"), 0.0) * (1 - decay)
            d += timedelta(days=1)

    last_date = (datetime.now()).strftime("%Y-%m-%d")
    z4_by_date = defaultdict(float)
    for s in training_plan.get("sessions", []):
        z4_by_date[s["date"]] += s.get("z4_mins", 0)

    weeks_ahead    = training_plan.get("projection_weeks", 1)
    today          = datetime.now()
    days_to_sunday = (6 - today.weekday()) % 7 or 7
    end_date       = (today + timedelta(days=days_to_sunday + (weeks_ahead - 1) * 7)).date()

    proj_dates = [last_date]
    proj_hil   = [round(current, 2)]
    d = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    while d.date() <= end_date:
        date_str = d.strftime("%Y-%m-%d")
        current  = current * decay + z4_by_date.get(date_str, 0) * (1 - decay)
        proj_dates.append(date_str)
        proj_hil.append(round(current, 2))
        d += timedelta(days=1)
    return proj_dates, proj_hil


def compute_projections(wellness_entries, training_plan, sigma_tss=35.0):
    entries = sorted([w for w in wellness_entries if w.get("ctl") and w.get("atl")],
                     key=lambda w: w["id"])
    if not entries:
        return [], [], [], [], [], [], []

    last      = entries[-1]
    last_date = last["id"]
    ctl       = last["ctl"]
    atl       = last["atl"]

    ctl_decay = math.exp(-1 / 42);  ctl_acc = 1 - ctl_decay
    atl_decay = math.exp(-1 / 7);   atl_acc = 1 - atl_decay

    weeks_ahead    = training_plan.get("projection_weeks", 1)
    today          = datetime.now()
    days_to_sunday = (6 - today.weekday()) % 7 or 7
    end_date       = (today + timedelta(days=days_to_sunday + (weeks_ahead - 1) * 7)).date()

    tss_by_date = defaultdict(float)
    for s in training_plan.get("sessions", []):
        tss_by_date[s["date"]] += s.get("tss", 0)

    # Anchor must match the last historical value exactly to avoid discontinuity.
    # Apply today's planned TSS to the working variables *after* storing the anchor,
    # so the projection extrapolates from the displayed endpoint.
    today_str = today.strftime("%Y-%m-%d")
    proj_dates    = [last_date]
    proj_ctl      = [round(ctl, 1)]
    proj_atl      = [round(atl, 1)]
    proj_tsb      = [round(ctl - atl, 1)]

    if last_date == today_str:
        today_tss = tss_by_date.get(today_str, 0)
        ctl = ctl * ctl_decay + today_tss * ctl_acc
        atl = atl * atl_decay + today_tss * atl_acc
    proj_ctl_sig  = [0.0]
    proj_atl_sig  = [0.0]
    proj_tsb_sig  = [0.0]

    # Uncertainty propagation: Var_n = Var_{n-1} * decay² + (acc * sigma_tss)²
    # TSB = CTL - ATL; treat as independent (slightly conservative)
    var_ctl = 0.0
    var_atl = 0.0

    d = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    while d.date() <= end_date:
        date_str = d.strftime("%Y-%m-%d")
        tss = tss_by_date.get(date_str, 0)
        ctl = ctl * ctl_decay + tss * ctl_acc
        atl = atl * atl_decay + tss * atl_acc
        var_ctl = var_ctl * ctl_decay**2 + (ctl_acc * sigma_tss)**2
        var_atl = var_atl * atl_decay**2 + (atl_acc * sigma_tss)**2
        proj_dates.append(date_str)
        proj_ctl.append(round(ctl, 1))
        proj_atl.append(round(atl, 1))
        proj_tsb.append(round(ctl - atl, 1))
        proj_ctl_sig.append(round(math.sqrt(var_ctl), 1))
        proj_atl_sig.append(round(math.sqrt(var_atl), 1))
        proj_tsb_sig.append(round(math.sqrt(var_ctl + var_atl), 1))
        d += timedelta(days=1)
    return proj_dates, proj_ctl, proj_atl, proj_tsb, proj_ctl_sig, proj_atl_sig, proj_tsb_sig


def compute_sleep_balance(wellness_entries, target_hours=8.0, tau=5):
    entries = sorted([w for w in wellness_entries if w.get("id")], key=lambda w: w["id"])
    target_secs = target_hours * 3600
    decay = math.exp(-1 / tau)
    current = 0.0
    dates, vals = [], []
    for w in entries:
        daily = (w["sleepSecs"] - target_secs) / 3600 if w.get("sleepSecs") else 0.0
        current = current * decay + daily * (1 - decay)
        dates.append(w["id"])
        vals.append(round(current, 2))
    return dates, vals


def compute_sleep_projections(entries, proj_days=14, target_hours=8.0, tau=5):
    """Project sleep debt EMA forward under three scenarios.

    Returns (dates, best, medium, trend, avg_sleep_hrs) where dates[0] is today
    (the anchor, equal to the last historical EMA value) and dates[1..] are future.
    best   = 9.5h/night,  medium = 8.5h/night,  trend = recent 7-day average.
    """
    sorted_entries = sorted([w for w in entries if w.get("id")], key=lambda w: w["id"])
    decay = math.exp(-1 / tau)
    target_secs = target_hours * 3600

    current = 0.0
    for w in sorted_entries:
        daily = (w["sleepSecs"] - target_secs) / 3600 if w.get("sleepSecs") else 0.0
        current = current * decay + daily * (1 - decay)

    recent = [w for w in sorted_entries[-7:] if w.get("sleepSecs")]
    avg_sleep = sum(w["sleepSecs"] for w in recent) / len(recent) / 3600 if recent else target_hours

    today = date.today()
    dates = [today.isoformat()]
    best_vals  = [round(current, 2)]
    med_vals   = [round(current, 2)]
    trend_vals = [round(current, 2)]

    b, m, t = current, current, current
    for i in range(1, proj_days + 1):
        b = b * decay + (9.5  - target_hours) * (1 - decay)
        m = m * decay + (8.5  - target_hours) * (1 - decay)
        t = t * decay + (avg_sleep - target_hours) * (1 - decay)
        dates.append((today + timedelta(days=i)).isoformat())
        best_vals.append(round(b, 2))
        med_vals.append(round(m, 2))
        trend_vals.append(round(t, 2))

    return dates, best_vals, med_vals, trend_vals, round(avg_sleep, 1)


def sleep_debt_clearance(current_debt, tau=5, target_hours=8.0, threshold=0.1,
                         next_exam_days=None):
    """Return (value_str, sub_str, color, tip_str) for a sleep-clearance recommendation stat.

    Uses the same EMA model as compute_sleep_balance.  current_debt is the most
    recent EMA value (negative = in debt, positive = surplus).
    next_exam_days: if provided, adds a line showing required h/night to clear by that date.
    """
    if current_debt >= -threshold:
        return "Clear", "no active debt", "#6fcf97", "Sleep debt EMA is at or above zero — no recovery needed."

    decay = math.exp(-1 / tau)
    debt = abs(current_debt)

    # At target (8h/night): daily contribution = 0, pure EMA decay
    # d_n = d_0 * decay^n < threshold  =>  n = tau * ln(debt / threshold)
    days_at_target = math.ceil(tau * math.log(debt / threshold))

    # Extra hours above target to clear in exactly N nights
    # Solve: 0 = -debt * decay^n + extra * (1 - decay^n)  =>  extra = debt * decay^n / (1 - decay^n)
    def extra_to_clear(n_nights):
        dn = decay ** n_nights
        return debt * dn / (1 - dn)

    extra_7  = extra_to_clear(7)
    extra_14 = extra_to_clear(14)

    color = "#e8837a" if debt > 1.0 else "#e3b95f"
    value_str = f"~{days_at_target} nights at {target_hours:.0f}h"

    lines = [
        f"clear in 7n → {target_hours + extra_7:.1f}h/night",
        f"14n → {target_hours + extra_14:.1f}h/night",
    ]
    tip_lines = [
        f"EMA debt: {current_debt:.2f}h (τ={tau}d model, {target_hours:.0f}h target).",
        f"At {target_hours:.0f}h/night (target): clears in ~{days_at_target} nights.",
        f"To clear in 7 nights: {target_hours + extra_7:.1f}h/night.",
        f"To clear in 14 nights: {target_hours + extra_14:.1f}h/night.",
    ]

    if next_exam_days is not None and 1 <= next_exam_days <= 30:
        extra_exam = extra_to_clear(next_exam_days)
        needed = target_hours + extra_exam
        needed_capped = min(needed, 12.0)
        lines.insert(0, f"by exam ({next_exam_days}n) → {needed_capped:.1f}h/night")
        tip_lines.append(
            f"To clear before exam in {next_exam_days} nights: {needed_capped:.1f}h/night"
            + (" (capped at 12h)" if needed > 12.0 else "") + "."
        )
        tip_lines.append("These figures account for the exponential carry-over of the model.")

    sub_str = "  ·  ".join(lines)
    tip_str = "  ".join(tip_lines)
    return value_str, sub_str, color, tip_str


# ── Coach brief ──────────────────────────────────────────────────────────────

def _reference_stats(wellness):
    """Anchors for today's wellness numbers, drawn from his own history.

    A bare "Fitness (CTL): 23" tells the model nothing about whether 23 is good, so
    it guesses — and it guessed "high" and "a strong foundation" for what is actually
    his lowest fitness in over a year. Deliberately his own distribution rather than
    population norms: no invented reference values, and it stays true as he changes.

    Returns {"ctl": {...}, "hrv"/"sleep"/"rhr": {"lo","hi","now","verdict"}} with
    raw numbers, shared by the deterministic overview and the model's data text.
    """
    entries = sorted([w for w in wellness if w.get("id")], key=lambda w: w["id"])
    # Capped at roughly a year. The full history reaches back to when he first
    # started logging and CTL was still ramping from nothing, which drags the floor
    # down far enough that a genuine low reads as merely "below usual".
    entries = entries[-REFERENCE_WINDOW_DAYS:]
    if len(entries) < 30:
        return {}
    out = {}

    def stats(key, window=None):
        rows = entries[-window:] if window else entries
        vals = [w[key] for w in rows if w.get(key)]
        return vals if len(vals) >= 15 else None

    ctl_all = stats("ctl")
    if ctl_all:
        now, lo, hi = ctl_all[-1], min(ctl_all), max(ctl_all)
        med = statistics.median(ctl_all)
        where = ("the lowest in this whole period" if now <= lo + 0.5 else
                 "the highest in this whole period" if now >= hi - 0.5 else
                 "well below usual" if now < med - 3 else
                 "well above usual" if now > med + 3 else "about usual")
        out["ctl"] = {"lo": lo, "hi": hi, "med": med, "span": len(ctl_all), "where": where}

    # An explicit band rather than a bare median: given only "90-day median 48" the
    # model manufactures a range around it (it produced "a normal range of 48 to 70").
    def band(key, label, higher_is_better):
        vals = stats(key, 90)
        if not vals:
            return
        q = statistics.quantiles(vals, n=4)
        lo, hi, now = q[0], q[2], vals[-1]
        if round(lo) <= round(now) <= round(hi):
            verdict = "normal"
        elif (now > hi) == higher_is_better:
            verdict = "better"
        else:
            verdict = "worse"
        out[label] = {"lo": lo, "hi": hi, "now": now, "verdict": verdict}

    band("hrv", "hrv", higher_is_better=True)
    band("sleepScore", "sleep", higher_is_better=True)
    band("restingHR", "rhr", higher_is_better=False)
    return out


# intervals.icu files rowing under "Other", so that group carries the WaterRower
# zones. Only the sports he actually trains are rendered; swim settings exist but
# would just be noise in the prompt.
_ZONE_GROUPS = (("Rowing / WaterRower", "Other"), ("Cycling", "Ride"), ("Running", "Run"))


def _hr_zone_lines(sport_settings):
    """Named HR zones, so heart-rate advice is quoted from his settings rather than
    guessed. Asking for "pace by heart rate" without these produced a confident
    "160-170 bpm" for an easy row, when his aerobic band is actually 141-156."""
    if not sport_settings:
        return []
    by_type = {}
    for ss in sport_settings:
        for t in ss.get("types") or []:
            by_type[t] = ss

    lines = []
    for label, key in _ZONE_GROUPS:
        ss = by_type.get(key)
        zones, names = ss and ss.get("hr_zones"), ss and ss.get("hr_zone_names")
        if not zones:
            continue
        lthr = ss.get("lthr")
        bands, low = [], 0
        for i, top in enumerate(zones):
            nm = (names[i] if names and i < len(names) else f"Z{i+1}")
            bands.append(f"{nm} {low}-{top}")
            low = top + 1
        detail = f" (LTHR {lthr})" if lthr else ""
        lines.append(f"  {label}{detail}: " + " · ".join(bands[:5]))
    if not lines:
        return []
    return (["\nHEART-RATE ZONES — if you give a bpm figure it must come from this list, and no "
             "other. Name the ONE band that fits the session and give its range; never list the "
             "zones back, and never pair two bands as a single target:"] + lines)


def _key_date_lines(training_plan):
    """Upcoming fixtures. Without these the model has no horizon to aim advice at —
    it cannot know a 2k test is three weeks out, so every day reads as open-ended."""
    today = datetime.now().date()
    upcoming = []
    for k in (training_plan or {}).get("key_dates", []):
        try:
            d = datetime.strptime(k["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d >= today:
            upcoming.append(((d - today).days, k.get("label", "?"), k["date"]))
    if not upcoming:
        return []
    upcoming.sort()
    days, label, date = upcoming[0]
    # A fixture weeks away still gets treated as imminent: given a strength retest 30
    # days out, the model called it "next week" and "in just a few days". Past a
    # fortnight it gets no aiming instruction, only a note that it is not the point.
    if days > KEY_DATE_HORIZON_DAYS:
        return [f"\nNo fixture in the next {KEY_DATE_HORIZON_DAYS} days (the next is {label}, "
                f"{days} days away, too far off to shape today's advice). Aim the advice at "
                "the current block described at the top instead."]
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    # Phrased as ordinary prose, not a shouty label: the model quotes these headings
    # back verbatim ("For your NEXT KEY DATE of the C2 test..."), so the heading has
    # to be something that still reads naturally if it leaks into the output.
    return ["\nComing up — aim today's advice at this. Quote the gap exactly as written; "
            "do not round it into \"a couple of weeks\" or similar:",
            f"  {label}, {date} ({when})."]


def build_tips_context(wellness, training_plan=None, sport_settings=None):
    """Reference data the tips may draw on beyond the overview: fixtures, HR zones,
    goals and lifts. No raw wellness numbers — those reach the model only through
    the verified overview, which is what stops it re-interpreting them."""
    lines = []
    notes = fresh_notes(load_notes().get("active", []))
    if notes:
        # His own words are the one source of context the numbers can't give (an
        # injury, a change of plan), so they go to the model as written.
        lines.append(f"\nPERSONAL NOTES from the athlete, last {NOTE_CONTEXT_DAYS} days (most recent last):")
        for n in notes:
            d = note_age_days(n)
            lines.append(f"  [{'today' if d == 0 else 'yesterday' if d == 1 else f'{d} days ago'}] {n['text']}")
    return "\n".join(lines + _key_date_lines(training_plan) + _hr_zone_lines(sport_settings)
                     + _benchmark_lines(wellness))


def _benchmark_lines(wellness):
    """Goals and key metrics, for the coach prompt.

    Stored ("bank") metrics are deliberately omitted: they are a record of PBs the
    athlete is not currently working on, and padding the prompt with them dilutes
    the targets that actually matter.
    """
    try:
        cfg = load_benchmark_config()
        weight_history = [{"date": w["id"], "value": w["weight"]}
                          for w in wellness if w.get("weight") and w.get("id")]
        metrics = build_metrics_data(load_test_metrics().get("entries", []),
                                     cfg["metrics"], weight_history=weight_history)
    except Exception:
        log.exception("benchmark lines failed")
        return []

    def fmt(m, v):
        if v is None:
            return "—"
        if m["lower"]:
            mins, secs = divmod(int(v), 60)
            return f"{mins}:{secs:02d}"
        return f"{v:g}{m['unit']}"

    out = []
    for tier, heading in (("goal", "GOALS"), ("key", "KEY BENCHMARKS")):
        rows = [m for m in metrics if m["tier"] == tier]
        if not rows:
            continue
        out.append("")
        out.append(f"{heading}:")
        for m in rows:
            pb = ""
            if m.get("pb") is not None:
                pb = f" · lifetime PB {fmt(m, m['pb'])}"
                if m.get("pb_date"):
                    pb += f" ({m['pb_date']})"
            if not m["has_data"]:
                tgt = f"target {fmt(m, m['target'])}" if m["target"] is not None else "no target set"
                out.append(f"  {m['label']}: no result logged yet ({tgt}){pb}")
                continue
            scored = ""
            if m.get("rep_weighted") and m.get("reps"):
                scored = (f", set {m['value']:g}{m['unit']}×{m['reps']} "
                          f"= {m['scored']:g}{m['unit']} at {m['rep_ref']} reps")
            if m["target"] is None:
                journey = " · no target set"
            else:
                journey = (f" · baseline {fmt(m, m['start'])} → target {fmt(m, m['target'])} "
                           f"({m['journey_pct']:.0f}% of the way)")
            out.append(f"  {m['label']}: {fmt(m, m['value'])} on {m['date']}{scored}{journey}{pb}")
    return out


# The coach brief is split by what a small local model can be trusted with. The
# OVERVIEW is pure fact — training state, readiness numbers, what's left today — and
# every attempt to have a 3B model write it from the numbers produced confident
# inversions (it read 15 sessions in 14 days as "three days without training", and
# rising Fitness as "dropped"), however the prompt was worded. So the overview is
# composed here in Python from computed facts and cannot contradict the data. The
# model only writes the TIPS, from those already-interpreted facts rather than raw
# numbers, and its output is checked against them on the way out (_verify_tips).
#
# Deliberately *not* restated in COACH_ROLE: goals, PBs, bodyweight, current lifts.
# Those all arrive in the data from benchmarks.json, and the old hardcoded copy
# silently went stale and contradicted the live numbers.
COACH_ROLE = (
    "You are Alex's rowing coach. He trains solo — no squad, no coach in the room — on a "
    "WaterRower at home plus a nearby gym. No longer rowing full time, he is chasing a faster 2k "
    "erg through more force production rather than more erg volume.\n"
    "Current block (late Sep–late Oct 2026): a full rebuild after proper time off this summer — "
    "strength first (squat, RDL, hip thrust, three gym sessions a week) with plenty of easy "
    "aerobic work with some threshold erg pieces, and gaining 5+ kg of bodyweight, so eating in a "
    "surplus matters. He plans his sessions week by week.\n"
    "LIFTING IS THE PRIORITY. Everything else is structured around it: never advise cutting, "
    "shortening or lightening a lift to fit aerobic work in. If recovery is short, the aerobic "
    "volume is what gives. Lift first on double days; aerobic work next to leg days stays easy "
    "and on the bike or rower rather than running.\n"
    "Food and wellness logs are partial samples: never criticise or remark on his logging, and "
    "when a point rests on them say \"based on the limited data we have\".\n"
)

TIPS_TASK = (
    "Write the COACHING TIPS paragraph of today's brief.\n\n"
    "The SITUATION paragraph above is already verified and correct — it is the whole truth "
    "about his recent training and readiness. Build on it; do not restate it, and never "
    "describe his training state, Fitness trend, or recovery any differently from how it is "
    "written there.\n\n"
    "Give exactly two pieces of advice for the next day or two. If a TODAY'S CALL is given, both "
    "must carry it out; otherwise aim them at the event named under \"Coming up\". Do not recount "
    "past sessions — the situation paragraph already does. For each, say what to do AND why it works — the reasoning is "
    "the point. Draw on whichever fit: sharpening versus resting before a test, pacing the "
    "WaterRower by heart rate because it is not a Concept2, 2k test pacing and warm-up, squat "
    "depth when the glutes are the limiter, hip thrust and RDL progression, when to deload, "
    "fuelling for the session type, and backing off when illness risk or sleep debt is flagged.\n"
    "Any bpm figure must be one band from the HEART-RATE ZONES list, quoted exactly. Do not "
    "quote any other number that is not in the data above.\n"
    "Never write \"sleep more\", \"stay hydrated\", \"listen to your body\", \"rest is "
    "important\", or anything equally true on any other day.\n\n"
    "Format: exactly 4 sentences, one paragraph of plain prose, addressing him as \"you\". "
    "No heading, no greeting, no markdown, no bullet points, no TSS or load numbers."
)


PLAN_PROMPT_SENTENCE = ("Nothing is on the plan from here, so tell me what you have coming up — "
                        "type your week into the Plan box and I'll build around it.")


def _coach_prompt(data_text, task):
    return (f"{COACH_ROLE}\n===== TODAY'S DATA =====\n{data_text}\n"
            f"===== END DATA =====\n\n{task}")


# Sentence terminator. The trailing lookahead is what makes this decimal-safe: the
# "." in "+18.5" is followed by a digit, never by whitespace or end-of-string, so
# numbers like TSB and split times can't be mistaken for the end of a sentence.
_SENTENCE_END = re.compile(r'[.!?]+(?=\s|$)')


# Platitudes the prompt bans but a 3B model still emits: negative instructions are
# weak at this size, so the ban is enforced here by dropping the offending sentence
# outright rather than trusting the model to have obeyed.
_PLATITUDES = re.compile(
    r'\b(?:stay(?:ing)? |monitor(?:ing)? your )?hydrat|\bdrink (?:more|plenty|enough)\b'
    r'|\bsleep more\b|\bget (?:more|enough|adequate|plenty of) (?:sleep|rest)\b'
    r'|\blisten to your body\b|\brest is (?:important|key|crucial|essential)\b'
    r'|\bstay consistent\b(?!\s+(?:by|with|through))|\beat(?:ing)? (?:well|healthily)\b',
    re.IGNORECASE)


def _split_sentences(text):
    out, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        out.append(text[start:m.end()].strip())
        start = m.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def _clean_section(text, max_sentences, drop_platitudes=False):
    """Strip the markdown scaffolding the model adds despite being told not to,
    and hard-cap the sentence count so a runaway section can't flood the card."""
    text = re.sub(r'^\s{0,3}#{1,6}\s*.*$', '', text, flags=re.MULTILINE)   # ### HEADERS
    text = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s+', '', text, flags=re.MULTILINE)  # bullets
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)                            # bold
    text = re.sub(r'(?<!\w)[*_](.+?)[*_](?!\w)', r'\1', text)                # italics
    text = re.sub(r'\s*\n\s*', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()

    sentences = _split_sentences(text)
    if drop_platitudes:
        kept = [s for s in sentences if not _PLATITUDES.search(s)]
        if len(kept) < len(sentences):
            log.info("coach brief: dropped %d platitude sentence(s)", len(sentences) - len(kept))
        # If it was platitudes all the way down there is nothing worth salvaging;
        # an empty tips section is handled by the caller and beats printing filler.
        sentences = kept
    return " ".join(sentences[:max_sentences]).strip()


_ACTIVITY_NOUNS = {
    "Run": "run", "VirtualRun": "run", "Ride": "ride", "VirtualRide": "ride",
    "Rowing": "row", "VirtualRow": "row", "WeightTraining": "weights session",
    "Walk": "walk", "Hike": "hike", "Swim": "swim", "Workout": "workout",
}


def _activity_day(a):
    return (a.get("start_date_local") or a.get("start_date") or "")[:10]


def _describe_activity(a):
    """"a 59-min run (10.7 km)" — short enough to sit inside a sentence."""
    noun = _ACTIVITY_NOUNS.get(a.get("type", ""), (a.get("type") or "session").lower())
    mins = int((a.get("moving_time") or a.get("elapsed_time") or 0) / 60)
    dist = a.get("distance") or 0
    s = f"a {mins}-min {noun}" if mins else f"a {noun}"
    if dist > 100 and noun not in ("weights session",):
        s += f" ({dist/1000:.1f} km)"
    return s


def _form_phrase(tsb):
    """intervals.icu's own Form bands, so the wording matches what the chart shows."""
    if tsb < -30:
        return "deep in fatigue — the high-risk band"
    if tsb < -10:
        return "carrying training fatigue (the productive-training band)"
    if tsb <= 5:
        return "in the neutral grey zone, neither fresh nor heavily loaded"
    if tsb <= 25:
        return "fresh"
    return "very fresh — long enough off that fitness is starting to fade"


def build_brief_facts(wellness, activities, training_plan=None, illness=None):
    """Everything the overview states, computed once. Each field is either a number
    straight from intervals.icu or a verdict derived by fixed rules — never by a model."""
    entries = sorted([w for w in wellness if w.get("id")], key=lambda w: w["id"])
    today_w = next((w for w in reversed(entries) if w.get("ctl") or w.get("hrv")), {})
    now     = datetime.now()
    today   = now.date()
    today_s = now.strftime("%Y-%m-%d")

    f = {"ctl": today_w.get("ctl"), "atl": today_w.get("atl"),
         "hrv": today_w.get("hrv"), "rhr": today_w.get("restingHR"),
         "sleep_score": today_w.get("sleepScore"), "ref": _reference_stats(wellness)}
    f["tsb"] = round(f["ctl"] - f["atl"], 1) if f["ctl"] and f["atl"] else None

    hrv_vals = [w.get("hrv") for w in entries[-7:] if w.get("hrv")]
    f["hrv_avg"] = statistics.mean(hrv_vals[:-1]) if len(hrv_vals) > 1 else None

    ctl_series = [w["ctl"] for w in entries if w.get("ctl")]
    if len(ctl_series) > 14:
        then, cur = ctl_series[-15], ctl_series[-1]
        f["ctl_then"] = then
        f["ctl_trend"] = "falling" if cur < then - 1 else "rising" if cur > then + 1 else "flat"

    _, sd = compute_sleep_balance(wellness)
    f["sleep_debt"] = sd[-1] if sd else None

    if illness is not None and getattr(illness, "today", None) is not None:
        f["illness_pct"] = round(illness.today * 100)

    past = []
    for a in activities:
        d = _activity_day(a)
        if not d:
            continue
        age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        if age >= 0:
            past.append((age, a))
    past.sort(key=lambda t: t[0])
    f["done_today"] = [a for g, a in past if g == 0]
    before_today    = [(g, a) for g, a in past if g > 0]
    f["n14"]        = sum(1 for g, _ in past if g <= 14)
    if before_today:
        gap = before_today[0][0]
        last_day = [a for g, a in before_today if g == gap]
        f["gap"] = gap
        # The longest session that day stands for it; a 1-min warm-up ride doesn't.
        f["last_session"] = max(last_day, key=lambda a: a.get("moving_time") or 0)
    else:
        f["gap"] = None

    weeks = [0.0, 0.0]
    for g, a in past:
        if g < 14:
            weeks[1 - g // 7] += a.get("icu_training_load") or a.get("training_load") or 0
    f["load_prev"], f["load_this"] = weeks

    sessions = (training_plan or {}).get("sessions", [])
    today_sessions     = [s for s in sessions if s.get("date") == today_s]
    f["planned_today"] = [s.get("name", "session") for s in today_sessions]
    f["planned_kinds"] = [_session_kind(s) for s in today_sessions]
    tomorrow_s         = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    f["lower_tomorrow"] = any(_is_lower(s) for s in sessions if s.get("date") == tomorrow_s)
    f["planned_ahead"] = any(today_s < s.get("date", "") <= (now + timedelta(days=14)).strftime("%Y-%m-%d")
                             for s in sessions)

    f["next_key"] = None
    for k in sorted((training_plan or {}).get("key_dates", []), key=lambda k: k.get("date", "")):
        try:
            d = datetime.strptime(k["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d >= today:
            f["next_key"] = {"label": k.get("label", "key date"), "days": (d - today).days,
                             "date": d}
            break
    return f


def render_overview(f):
    """The OVERVIEW paragraph, three sentences, composed from build_brief_facts()."""
    out = []

    # 1. Training state — the frame every later number is read through.
    if f["gap"] is None and not f["done_today"]:
        s1 = "Nothing is logged in intervals.icu for the last six weeks"
    elif f["n14"] == 0:
        s1 = f"Your last logged session was {f['gap']} days ago"
    else:
        when = ("today" if f["gap"] is None else "yesterday" if f["gap"] == 1
                else f"{f['gap']} days ago")
        last = f["done_today"][0] if f["gap"] is None else f["last_session"]
        s1 = (f"You've logged {f['n14']} session{'s' if f['n14'] != 1 else ''} in the last "
              f"14 days, most recently {_describe_activity(last)} {when}")
        if f["gap"] is not None and f["gap"] >= 4:
            s1 += f" — a {f['gap']}-day gap, so ease back in rather than picking up where you left off"
        prev, this = f["load_prev"], f["load_this"]
        if prev > 0 and this >= 1.8 * prev:
            s1 += ", with this past week's load roughly double the week before"
        elif prev > 0 and this >= 1.25 * prev:
            s1 += ", with load up on the week before"
        elif prev > 0 and this <= 0.6 * prev:
            s1 += ", with load well down on the week before"
    if f["ctl"]:
        trend = f.get("ctl_trend")
        s1 += f". Fitness is {f['ctl']:.0f}"
        if trend:
            s1 += (f" and {trend} ({f['ctl_then']:.0f} two weeks ago)" if trend != "flat"
                   else " and flat over two weeks")
        ref = f["ref"].get("ctl")
        if ref and ref["where"] != "about usual":
            s1 += f", {ref['where']} for the past year (median {ref['med']:.0f})"
    out.append(s1 + ".")

    # 2. Readiness — each figure judged only against its own band.
    if f["tsb"] is not None:
        s2 = f"Form is {f['tsb']:+.1f}, so you're {_form_phrase(f['tsb'])}"
        normal, flagged = [], []
        labels = {"hrv": "HRV", "rhr": "resting HR", "sleep": "sleep score"}
        values = {"hrv": f["hrv"], "rhr": f["rhr"], "sleep": f["sleep_score"]}
        for key in ("hrv", "rhr", "sleep"):
            b, v = f["ref"].get(key), values[key]
            if not v:
                continue
            txt = f"{labels[key]} {v:.0f}"
            if key == "hrv" and f["hrv_avg"]:
                txt += f" (7-day avg {f['hrv_avg']:.0f})"
            if not b or b["verdict"] == "normal":
                normal.append(txt)
            else:
                flagged.append(f"{txt} is {b['verdict']} than your usual {b['lo']:.0f}-{b['hi']:.0f}")
        if normal:
            s2 += "; " + _join_list(normal) + (" are" if len(normal) > 1 else " is") + " within your normal range"
        if flagged:
            s2 += ", but " + _join_list(flagged)
        if f.get("sleep_debt") is not None and f["sleep_debt"] < -1:
            s2 += f", and you're carrying {abs(f['sleep_debt']):.1f}h of sleep debt"
        if f.get("illness_pct") is not None and f["illness_pct"] > 15:
            s2 += f", and illness risk is {'elevated' if f['illness_pct'] > 30 else 'mildly raised'} at {f['illness_pct']}%"
        out.append(s2 + ".")

    # 3. Today and what's next.
    if f["done_today"]:
        s3 = "Today you've already done " + _join_list([_describe_activity(a) for a in f["done_today"]])
    else:
        s3 = "Nothing is logged yet today"
    if f["planned_today"]:
        s3 += "; on the plan: " + _join_list(f"\"{n}\"" for n in f["planned_today"])
    elif not f["planned_ahead"]:
        s3 += " and nothing is planned for the next two weeks"
    k = f["next_key"]
    if k:
        when = ("today" if k["days"] == 0 else "tomorrow" if k["days"] == 1
                else f"in {k['days']} days ({k['date'].strftime('%A')})")
        # Labels look like "C2 test – target 6:25"; the target reads better bracketed.
        head, _, tail = k["label"].partition(" – ")
        s3 += f", with the {head}{f' ({tail})' if tail else ''} {when}"
    out.append(s3 + ".")
    return " ".join(out)


def _join_list(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_NUM = re.compile(r"\d+(?:[.:]\d+)?")
_RANGE = re.compile(r"\b(\d{2,3})\s*(?:-|–|to)\s*(\d{2,3})\b")

# Claims about his training state that the tips must not make unless the facts say
# so — the exact inversions the model produced when it wrote the overview itself.
_LAYOFF_CLAIM = re.compile(
    r"\blay-?off\b|\btime off\b|\bbreak from\b|\bdetrain|\bhaven'?t (?:been )?(?:train|logged|done)"
    r"|\bno (?:recent )?(?:training|sessions)\b|\b(?:getting|coming) back\b|\breturn(?:ing)? to training\b"
    r"|\bafter (?:a|your|the) (?:break|gap|pause)\b|\brest period\b", re.I)
# The overview already recounts what he did; when the tips do it too they garble it
# (a 59-min run became "your rowing session at the WaterRower"), so recaps are dropped.
_RECAP_CLAIM = re.compile(
    r"\byesterday\b|\blast (?:session|run|row|ride)\b|\byou(?:'ve| have) (?:completed|hit|done|logged|had)\b"
    r"|\byour recent (?:run|row|ride|session|rowing)", re.I)
# Benchmarks (e.g. the 30R20 row test) are in the data as history, and the model reads
# them as fixtures ("for the upcoming 30R20 row test"). Test/race talk needs a real one.
_HR_TALK = re.compile(r"\bbpm\b|\bheart|\bHR\b|\bzone\b|\bband\b", re.I)
# A sentence leaning on the one before ("This approach…") is dropped with it, or the
# tips read as a reply to something that isn't there.
_ANAPHORA = re.compile(r"^(?:this|these|that|those|it|doing so|by doing (?:this|so)|such)\b", re.I)
_FIXTURE_TALK = re.compile(r"\b(?:test|retest|race|competition|time trial)\b", re.I)
_SOON_CLAIM = re.compile(
    r"\b(?:next week|this week|(?:a )?few days|couple of days|approaching|looming|imminent|upcoming (?:test|retest))\b", re.I)
_FITNESS_DOWN = re.compile(r"fitness\b[^.]{0,25}\b(?:dropp|declin|fall|fell|decreas|slipp)", re.I)
_FITNESS_UP   = re.compile(r"fitness\b[^.]{0,25}\b(?:improv|rising|rose|increas|climb)", re.I)


def _verify_tips(tips, facts, context):
    """Drop tip sentences that contradict the facts or quote a number the data
    doesn't contain. Sentence-level, so one bad claim costs one sentence, not the tips."""
    allowed = set(_NUM.findall(context))
    # Both ends of a real zone appear in the data individually, so a stitched-together
    # "181-186 bpm" would pass the number check; ranges must match a band exactly.
    bands = set(_RANGE.findall(context))
    k = facts["next_key"]
    kept, dropped_prev = [], False
    for sent in _split_sentences(tips):
        # It re-derives the test's timing and gets it wrong ("the C2 test tomorrow" when
        # it is two days out); the gap is known exactly, so it is corrected, not dropped.
        if k and k["days"] != 1:
            sent = re.sub(r"\btomorrow\b", "today" if k["days"] == 0 else f"in {k['days']} days", sent)
        why = None
        if facts["n14"] and (facts["gap"] is None or facts["gap"] <= 3) and _LAYOFF_CLAIM.search(sent):
            why = "layoff claim while training"
        elif _HR_TALK.search(sent) and any(r not in bands for r in _RANGE.findall(sent)):
            why = "range not a real HR band"
        elif (not k or k["days"] > KEY_DATE_HORIZON_DAYS) and _FIXTURE_TALK.search(sent):
            why = "talks about a test/race when none is scheduled"
        elif (not k or k["days"] > 7) and _SOON_CLAIM.search(sent):
            why = "claims a fixture is imminent"
        elif _RECAP_CLAIM.search(sent):
            why = "recaps past training"
        elif facts.get("ctl_trend") == "rising" and _FITNESS_DOWN.search(sent):
            why = "says fitness falling"
        elif facts.get("ctl_trend") == "falling" and _FITNESS_UP.search(sent):
            why = "says fitness rising"
        else:
            # Small bare integers are ordinary advice ("3 x 10 min", "2 days out").
            bad = [n for n in _NUM.findall(sent)
                   if n not in allowed and not (n.isdigit() and int(n) <= 12)]
            if bad:
                why = f"unsupported numbers {bad}"
        if not why and dropped_prev and _ANAPHORA.search(sent):
            why = "leans on a dropped sentence"
        dropped_prev = bool(why)
        if why:
            log.info("coach tips: dropped sentence (%s): %s", why, sent)
        else:
            kept.append(sent)
    return " ".join(kept)


def _session_kind(s):
    """Planner sessions carry their kind; older hand-written ones are inferred."""
    if s.get("kind"):
        return s["kind"]
    name = s.get("name", "")
    if _MOBILITY_WORDS.search(name):
        return "mobility"
    if _STRENGTH_WORDS.search(name):
        return "strength"
    if (s.get("z4_mins") or 0) > 0:
        return "threshold"
    return "easy" if re.search(r"\beasy\b|\blong\b", name, re.I) else None


_KIND_CALLS = {
    "recovery": ("Today's planned session is a recovery hour: stay in the Recovery band for its sport "
                 "throughout — deliberately easier than a normal easy session, to relax and absorb the "
                 "week's load, not to train."),
    "easy": ("Today's planned session is easy: hold the Aerobic band for its sport the whole way. "
             "The point is aerobic volume that adds no fatigue, so no pushing the pace."),
    "steady": ("Today's planned session is steady (UT1-type): controlled and even, up to the top of "
               "the Tempo band for its sport, not drifting into SubThreshold."),
    "threshold": ("Today's planned session is threshold intervals: the work reps in the "
                  "SubThreshold band, even-paced from first rep to last, easy between reps."),
    "vo2max": "Today's planned session is short hard intervals: full effort on the reps, full recovery between.",
    "strength": ("Today's planned session is strength: quality reps with full depth and control, "
                 "progressing load only when every rep is clean."),
}


def _todays_call(f):
    """What kind of day today should be, decided by fixed rules. Left to the model,
    the call itself went wrong (it prescribed a SuperThreshold row two days before a
    2k test), so the model is only asked to explain and flesh out this decision."""
    k = f["next_key"]
    if f.get("illness_pct") is not None and f["illness_pct"] > 30:
        return ("Keep today easy or take it off: illness risk is elevated, and training hard "
                "into an oncoming illness costs more days than it gains.")
    if k and k["days"] == 0:
        return (f"Today is the {k['label']}: a thorough warm-up with a few short bursts at test "
                "pace, then execute the test with an even-paced plan.")
    if k and k["days"] <= 3:
        return (f"Ease off for the {k['label']} in {k['days']} day{'s' if k['days'] != 1 else ''}: "
                "only short, easy sessions in the Aerobic band, optionally with a few short "
                "sharp efforts to stay primed. No hard, long, or heavy-lifting sessions "
                "until after it.")
    if f["gap"] is not None and f["gap"] >= 4:
        return ("Restart gently: one easy Aerobic-band session today, building back over the "
                "week rather than jumping straight into hard work.")
    # Otherwise the day is whatever he planned — the tips explain how to do it well
    # (left free, the model put a planned easy ride in Tempo).
    calls = [_KIND_CALLS[k] for k in dict.fromkeys(f.get("planned_kinds") or []) if k in _KIND_CALLS]
    kinds = set(f.get("planned_kinds") or [])
    if "strength" in kinds and kinds - {"strength", "rest"}:
        calls.append("Lift first, while fresh; the other session comes after, ideally 6+ hours "
                     "later, and never at the expense of the lift.")
    if f.get("lower_tomorrow"):
        calls.append("Tomorrow is a lower-body lift, which takes priority: keep today easy on the "
                     "bike or rower, no running and nothing hard, so your legs arrive fresh.")
    # He is deliberately building through the high-risk band to get back to ~50
    # Fitness, so a deep Form doesn't cancel the planned day; it makes recovery the
    # thing to get right around it.
    if f["tsb"] is not None and f["tsb"] < -30:
        calls.append("Form is in the high-risk band by design this block, so recovery is the "
                     "priority around the session: keep easy work truly easy, eat in surplus with "
                     "carbs around training, and protect sleep.")
    return " ".join(calls) or None


def get_training_summary(facts, tips_context):
    """Returns ({"overview": ..., "tips": ...}, is_stale).

    `facts` is build_brief_facts(); `tips_context` is the reference data (zones,
    key dates, benchmarks) the tips may draw on beyond the overview itself."""
    overview   = render_overview(facts)
    context    = f"SITUATION (verified):\n{overview}\n{tips_context}"
    call       = _todays_call(facts)
    if call:
        context += ("\n\nTODAY'S CALL (already decided — your advice must follow it, explain "
                    f"why it is right, and add how to do it well; never contradict it):\n  {call}")
    input_hash = _data_hash(context)
    cache      = _load_cache(CACHE_FILE)
    if cache.get("hash") == input_hash and cache.get("overview"):
        return {"overview": cache["overview"], "tips": cache.get("tips", "")}, False

    # One retry if the checks gut the first attempt: a second sample is usually
    # clean, and costs ~15s with the model already warm.
    tips = ""
    for _attempt in range(2):
        raw = _ollama_p(_coach_prompt(context, TIPS_TASK),
                        source="training_summary_tips", model=LOCAL_MODEL)
        candidate = _verify_tips(_clean_section(raw, max_sentences=6, drop_platitudes=True),
                                 facts, context)
        if len(_split_sentences(candidate)) > len(_split_sentences(tips)):
            tips = candidate
        if len(_split_sentences(tips)) >= 2:
            break
    # With nothing scheduled the brief is guesswork until he says what's coming up,
    # so the ask is appended here rather than left to the model, which drops it.
    needs_ask = not facts["planned_today"] and not facts["planned_ahead"]
    tips = " ".join(_split_sentences(tips)[:3 if needs_ask else 4])
    if needs_ask:
        tips = (tips + " " if tips else "") + PLAN_PROMPT_SENTENCE
    if not tips:
        log.warning("coach brief produced empty tips section")
    _save_cache(CACHE_FILE, {"hash": input_hash, "overview": overview, "tips": tips})
    return {"overview": overview, "tips": tips}, False


# ── Week planner — typed or spoken week → sessions ──────────────────────────

# TSS per hour by intensity. Load comes from these fixed rates, not from the model,
# whose load guesses varied run to run; for interval sessions the work minutes are
# costed at the hard rate and the rest of the session at the easy rate.
PLAN_KINDS = {            # (TSS/hour for the session, TSS/hour for work intervals)
    "mobility":  (0, None),    # stretching: recovery work, not training load
    "recovery":  (30, None),
    "easy":      (45, None),
    "steady":    (85, None),   # his UT1-type erg/run: 83-91 TSS/h (avg HR ~170, mostly SubThreshold)
    "threshold": (45, 95),
    "vo2max":    (45, 120),
    "strength":  (30, None),   # intervals.icu scores his lifting ~30 TSS/h (HR-based)
    "rest":      (0, None),
}
PLAN_DEFAULT_MINUTES = {"strength": 60, "rest": 0}


# Running scores far above cycling at the same effort for him (hrTSS with a higher
# run LTHR and more time up the zones), so easy/recovery runs get their own rate.
PLAN_RUN_FACTOR = 1.45     # easy 45 → ~65 TSS/h, recovery 30 → ~44


def plan_session_load(minutes, kind, work_minutes=0, name=""):
    """(tss, z4_mins) for a planned session."""
    base, hard = PLAN_KINDS.get(kind, PLAN_KINDS["easy"])
    if kind in ("easy", "recovery") and _RUN_WORDS.search(name or ""):
        base *= PLAN_RUN_FACTOR
    minutes = max(0, int(minutes or 0))
    work = min(max(0, int(work_minutes or 0)), minutes) if hard else 0
    tss = (minutes - work) * base / 60 + work * (hard or 0) / 60
    return round(tss), work


# Day words as he actually types/says them, typos included ("tommorow", "mondy").
_PLAN_DAY = re.compile(
    r"\b(today|tonight|this (?:morning|afternoon|evening)|tomm?orr?ow|tmrw|"
    r"mon(?:d?ay)?|mondy|tue(?:s(?:day)?)?|wed(?:s|nesday)?|thu(?:rs?(?:day)?)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", re.I)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_STRENGTH_WORDS = re.compile(r"\b(gym|weights?|lift(?:ing)?|strength|squats?|deadlifts?|rdls?|"
                             r"hip thrusts?|bench|upper|lower|legs|pull|push)\b", re.I)
_REST_WORDS = re.compile(r"\b(rest|off|day off|nothing)\b", re.I)
_MOBILITY_WORDS = re.compile(r"\b(stretch(?:ing)?|mobility|yoga|foam roll(?:ing)?)\b", re.I)
_RECOVERY_WORDS = re.compile(r"\b(recovery|very (?:easy|low|light)|low zone|zone ?1|z1|spin to relax|flush)\b", re.I)
_STEADY_WORDS = re.compile(r"\b(steady|tempo|ut1|sweet ?spot)\b", re.I)
_DURATION = re.compile(r"\b(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|mins?|minutes?)\b", re.I)
_REPS = re.compile(r"\b(\d{1,2})\s*(?:x|×|\*|by)\s*(\d{1,4}(?:\.\d+)?)\s*(km|k\b)?", re.I)


def _resolve_plan_day(word, today):
    w = word.lower()
    if w.startswith(("today", "tonight", "this ")):
        return today
    if w.startswith(("tom", "tmrw")):
        return today + timedelta(days=1)
    # A bare weekday is its next occurrence; today's own weekday means today.
    return today + timedelta(days=(_WEEKDAYS.index(w[:3]) - today.weekday()) % 7)


def _split_plan_text(text, today):
    """[(date, segment text)] — cut at every day word. Text before the first day
    word belongs to today. Dates are resolved here, never by the model: given a
    date table it still put "today" on Monday and "Monday" a week late."""
    marks = list(_PLAN_DAY.finditer(text))
    if not marks:
        return [(today, text.strip())]
    segs = []
    if text[:marks[0].start()].strip(" ,.;:-"):
        segs.append((today, text[:marks[0].start()]))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        day = _resolve_plan_day(m.group(1), today)
        # A week is described in order, so "... Friday ..., Saturday long run" said on
        # a Saturday means next Saturday, not today: never step backwards in time.
        while segs and day < segs[-1][0]:
            day += timedelta(days=7)
        segs.append((day, text[m.end():end]))
    out = []
    for d, t in segs:
        t = re.sub(r"^(?:\s|[,.;:-]|\b(?:and|then|is|i will|i'll|morning|evening|afternoon)\b)+", "", t,
                   flags=re.I).strip(" ,.;:-")
        t = re.sub(r"(?:[\s,.;]|\band\b)+$", "", t, flags=re.I)
        if t:
            out.append((d, t))
    return out


def parse_week_plan(text):
    """Turn "today 60 min bike, tomorrow gym lower, Mon 3x10 erg" into draft rows
    {date, name, minutes, kind, work_minutes} for the review grid. Python owns the
    dates and the interval arithmetic; the model only names each segment's session
    and says what kind it is."""
    today = datetime.now().date()
    segs = _split_plan_text(text, today)
    listing = "\n".join(f"{i + 1}: {t}" for i, (_, t) in enumerate(segs))
    prompt = (
        "Each numbered line describes the training for one day. Name each session as JSON. "
        "Respond with JSON only.\n\n"
        'Schema: {"sessions": [{"line": N, "name": "short name"}]}\n'
        "- name: sport first, then only details the line actually states, e.g. \"Bike – 60 min easy\", "
        "\"Erg – 3×10 min\", \"Gym – lower\", \"Rest\". Never add reps, weights or durations that "
        "are not in the line.\n"
        "- Every line gets at least one session; a line with two sessions gets two entries.\n\n"
        f"{listing}"
    )
    t0 = time.monotonic()
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json={
            "model": LOCAL_MODEL, "messages": [{"role": "user", "content": prompt}],
            "format": "json", "stream": False, "options": {"temperature": 0}}, timeout=180)
        resp.raise_for_status()
        raw = json.loads(resp.json()["message"]["content"]).get("sessions") or []
    except Exception:
        _log_ollama_usage("plan_week", int((time.monotonic() - t0) * 1000), False)
        log.exception("week plan parse failed")
        raise RuntimeError("Couldn't read that plan — is Ollama running?")
    _log_ollama_usage("plan_week", int((time.monotonic() - t0) * 1000), True)

    by_line = defaultdict(list)
    for r in raw:
        try:
            by_line[int(r.get("line"))].append(r)
        except (TypeError, ValueError):
            continue

    rows = []
    for i, (day, seg) in enumerate(segs, start=1):
        sessions = by_line.get(i) or [{"name": seg[:60]}]
        for r in sessions:
            name = str(r.get("name") or seg).strip()[:60]
            # With one session on the day his own words are the evidence; with two,
            # each session's name is. Either way the model's name is checked against
            # them, so an invented "5x4" on a gym session can't turn it into intervals.
            evidence = seg if len(sessions) == 1 else name
            reps = _REPS.search(evidence) if _REPS.search(seg) else None
            if reps is None and _REPS.search(name):
                name = _REPS.sub("", name).replace("  ", " ").strip(" –-")
            if _MOBILITY_WORDS.search(evidence) and not _STRENGTH_WORDS.search(evidence):
                kind, work = "mobility", 0
            elif _REST_WORDS.search(evidence) and not _STRENGTH_WORDS.search(evidence):
                kind, work = "rest", 0
            elif _STRENGTH_WORDS.search(evidence):
                kind, work = "strength", 0
            elif reps and not reps.group(3) and float(reps.group(2)) <= 30:
                # "3x10" / "3x10m" are minutes; "8x500m" (>30) and "3x1k" are distances.
                length = float(reps.group(2))
                kind, work = ("threshold" if length >= 6 else "vo2max"), round(int(reps.group(1)) * length)
            elif reps:
                kind, work = "vo2max", 12
            elif _RECOVERY_WORDS.search(evidence):
                kind, work = "recovery", 0
            elif _STEADY_WORDS.search(evidence):
                kind, work = "steady", 0
            elif _RUN_WORDS.search(evidence) and not re.search(r"\b(easy|gentle|jog)\b", evidence, re.I):
                # His runs sit in Tempo/SubThreshold unless he says otherwise.
                kind, work = "steady", 0
            else:
                kind, work = "easy", 0
            # Stated duration wins — read with the reps removed, so the "10" of "3x10
            # min" isn't taken as the session length. Otherwise: intervals get 30 min
            # of warm-up, recoveries and cool-down on top of the work.
            dur = _DURATION.search(_REPS.sub(" ", evidence))
            if kind == "rest":
                minutes = 0
            elif dur:
                v = float(dur.group(1))
                minutes = round(v * 60 if dur.group(2).lower().startswith("h") else v)
                if work and minutes < work + 10:      # "20 min easy then 5x3": a part, not the total
                    minutes += work
            else:
                minutes = work + 30 if work else 60
            rows.append({"date": day.isoformat(), "name": name or seg[:60],
                         "minutes": max(minutes, work), "kind": kind, "work_minutes": work})
    return rows


# Main lifts make a lower day outright; accessories (hip thrust, lunge) only when the
# session isn't an upper one, so "upper + hip thrust" doesn't protect the day before.
_LOWER_WORDS = re.compile(r"\b(lower|legs?|squats?|deadlifts?|rdls?|full body)\b", re.I)
_LOWER_ACCESSORY = re.compile(r"\b(hip thrusts?|lunges?|split squats?)\b", re.I)
_RUN_WORDS = re.compile(r"\brun(?:ning)?\b|\bjog", re.I)
MIN_LIFTS_PER_WEEK = 4


def _is_lower(s):
    name = s.get("name", "")
    if _session_kind(s) != "strength":
        return False
    return bool(_LOWER_WORDS.search(name)) or (
        bool(_LOWER_ACCESSORY.search(name)) and not re.search(r"\bupper\b", name, re.I))


def _is_hard(s):
    # "steady" counts: his UT1 sessions sit mostly in SubThreshold, and that residual
    # fatigue lands on the next day's squat just as intervals do.
    return _session_kind(s) in ("steady", "threshold", "vo2max")


def plan_lifting_conflicts(rows):
    """Warnings for a draft week, judged together with what's already planned around
    it. Lifting is the priority, so the checks protect lower-body sessions from the
    work most likely to blunt them — hard intervals and running the day before (the
    eccentric load and residual fatigue land on the squat), and hard work before the
    lift on the same day — and flag a week that has lost a gym session."""
    drafted = {r["date"] for r in rows}
    around = [x for x in load_training_plan().get("sessions", []) if x.get("date") not in drafted]
    week = sorted(around + [r for r in rows if r.get("kind") != "rest"], key=lambda x: x.get("date", ""))
    by_day = defaultdict(list)
    for x in week:
        by_day[x["date"]].append(x)

    def day_name(d):
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a")

    warnings = []
    for d in sorted(drafted):
        today = by_day.get(d, [])
        nxt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        if any(_is_lower(x) for x in by_day.get(nxt, [])):
            for x in today:
                if _is_hard(x):
                    warnings.append(f"{day_name(d)}: {x['name']} is the day before a lower-body lift — "
                                    "move it after the lift, or to the day after.")
                elif _RUN_WORDS.search(x.get("name", "")):
                    warnings.append(f"{day_name(d)}: running the day before a lower-body lift — "
                                    "swap for bike or row.")
        if any(_is_lower(x) for x in today) and any(_is_hard(x) for x in today):
            warnings.append(f"{day_name(d)}: hard or UT1 work on a lower-body day — lift first and "
                            "keep them 6+ hours apart, or move it.")
    # Only judge the gym count when the draft actually plans most of a week.
    if len(drafted) >= 5:
        first = datetime.strptime(min(drafted), "%Y-%m-%d")
        span = {(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}
        lifts = sum(1 for d in span for x in by_day.get(d, []) if _session_kind(x) == "strength")
        if lifts < MIN_LIFTS_PER_WEEK:
            warnings.append(f"Only {lifts} gym session{'s' if lifts != 1 else ''} in this week — "
                            f"lifting is the priority, plan at least {MIN_LIFTS_PER_WEEK}.")
    return warnings


def save_week_plan(rows):
    """Write reviewed rows into training-plan.json. Every date that appears in the
    draft is replaced wholesale — re-planning a day overwrites it rather than
    stacking a second copy — and rest days clear their date without adding a session."""
    plan = load_training_plan()
    dates = {r["date"] for r in rows}
    sessions = [x for x in plan.get("sessions", []) if x.get("date") not in dates]
    for r in rows:
        if r.get("kind") == "rest" or not str(r.get("name", "")).strip():
            continue
        tss, z4 = plan_session_load(r.get("minutes"), r.get("kind"), r.get("work_minutes"),
                                    r.get("name", ""))
        sessions.append({"date": r["date"], "name": str(r["name"]).strip(),
                         "tss": tss, "z4_mins": z4, "kind": r.get("kind", "easy")})
    plan["sessions"] = sorted(sessions, key=lambda x: x.get("date", ""))
    with open(TRAINING_PLAN_FILE, "w") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    log.info("week plan saved: %d row(s) over %s", len(rows), ", ".join(sorted(dates)))


NUTR_COACH_CACHE = os.path.expanduser("~/.cache/training-brief/nutrition-coach-cache.json")

def get_nutrition_coach(activities, training_plan, coach_summary=None):
    """Returns (brief_str, is_stale)."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    weekly    = get_weekly_food_summary()
    food_data = get_today_nutrition()

    done_today = [a for a in activities
                  if (a.get("start_date_local") or a.get("start_date") or "")[:10] == today_str]
    sessions_text = []
    for a in done_today:
        atype  = a.get("type", "")
        mins   = int((a.get("elapsed_time") or a.get("moving_time") or 0) / 60)
        dist_m = a.get("distance") or 0
        dist   = f"{dist_m/1000:.1f}km" if dist_m > 100 else ""
        zones  = a.get("icu_hr_zone_times") or []
        z4plus = int(sum(zones[i] for i in range(3, min(7, len(zones)))) / 60) if zones else 0
        parts  = [atype] + ([f"{mins}min"] if mins else []) + ([dist] if dist else []) + ([f"{z4plus}min Z4+"] if z4plus else [])
        sessions_text.append(" · ".join(parts))

    sessions_planned = [s["name"] for s in training_plan.get("sessions", [])
                        if s.get("date", "") >= today_str]

    today_food = (
        f"Today so far: {food_data['calories']:.0f} kcal, "
        f"{food_data['protein_g']:.0f}g protein, "
        f"{food_data['carbs_g']:.0f}g carbs, "
        f"{food_data['fat_g']:.0f}g fat — "
        + " | ".join(
            "; ".join(i.get("name", "") for i in e.get("items", []) if i.get("name")) or e.get("description", "")
            for e in food_data["entries"]
        )
    ) if food_data["entry_count"] > 0 else "Today: no meals logged yet."

    coach_ctx = ""
    if coach_summary:
        coach_ctx = (f"Training coach's assessment: {coach_summary.get('overview','')} "
                     f"{coach_summary.get('tips','')}\n\n")

    current_hour = datetime.now().hour
    time_slot    = "morning" if current_hour < 12 else "afternoon" if current_hour < 18 else "evening"
    time_context = f"Current time: {current_hour:02d}:00 ({time_slot})"
    input_str  = coach_ctx + today_food + str(sessions_text) + str(sessions_planned) + "\n".join(weekly) + time_context
    input_hash = _data_hash(input_str)
    cache      = _load_cache(NUTR_COACH_CACHE)
    if cache.get("hash") == input_hash:
        return cache.get("brief", ""), False

    prompt = (
        "You are a sports nutritionist advising a solo rower (post-graduation, no squad) targeting a 2k erg "
        "from 6:29 to 6:10 over the next year, with a secondary goal of gaining 7kg of lean mass (83→90kg). "
        "Write a concise nutrition-focused daily note (3 sentences, plain prose, no greetings, no headers, no markdown).\n\n"
        + LOGGING_NOTE +
        "Focus on: what they should eat NOW or next given the sessions done and still ahead, "
        "and one specific food or meal recommendation.\n\n"
        "CRITICAL — time of day awareness: do NOT judge today's intake as insufficient or low "
        "unless it is evening (after 18:00) and the day is essentially over. "
        "If it is morning or afternoon, the day is not done — comment only on what to eat next, "
        "not whether total intake is adequate.\n\n"
        "Do NOT comment on sleep, HRV, form, or training load metrics — only food and nutrition.\n\n"
        + coach_ctx
        + f"{time_context}\n"
        f"Sessions completed today: {', '.join(sessions_text) if sessions_text else 'none yet'}\n"
        f"Sessions still planned today/this week: {', '.join(sessions_planned) if sessions_planned else 'none'}\n"
        "Do NOT quote or repeat session names verbatim — describe sessions by type and intensity only.\n"
        f"{today_food}\n\n"
        "Last 7 days of eating:\n" + "\n".join(weekly) + "\n\n"
        "Be specific: name actual foods and quantities. No generic advice."
    )
    brief = _ollama_p(prompt, source="nutrition_coach", model=LOCAL_MODEL_FAST)
    _save_cache(NUTR_COACH_CACHE, {"hash": input_hash, "brief": brief})
    return brief, False


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _color_tsb(v):
    if v is None:  return "#888"
    if v > 10:     return "#6fcf97"
    if v < -20:    return "#e8837a"
    return "#e3b95f"

def _color_hrv(delta):
    if delta is None: return "#e8e9ea"
    if delta < -5:    return "#e8837a"
    return "#6fcf97"

def _color_sleep(score):
    if score is None: return "#888"
    if score >= 80:   return "#6fcf97"
    if score >= 60:   return "#e3b95f"
    return "#e8837a"

def _fmt_sleep(secs):
    if not secs: return "—"
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h {m:02d}m"

def _stat(label, value, color="#e8e9ea", sub=None, tip=None, link=None):
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    tip_attr = f' data-tip="{tip}"' if tip else ""
    cursor   = ' style="cursor:help"' if tip else ""
    if link:
        tab, _, anchor = link.partition(":")
        cursor = f' onclick="goTo(\'{tab}\',\'{anchor}\')"'
    return (f'<div class="stat{" stat-link" if link else ""}"{tip_attr}{cursor}>'
            f'<div class="label">{label}</div>'
            f'<div class="value" style="color:{color}">{value}</div>'
            f'{sub_html}</div>')


def _build_sessions_html(training_plan, today_str, activities=None, days_ahead=7):
    sessions = training_plan.get("sessions", [])
    # A full plan runs to hundreds of sessions; rendering all of them buries the
    # week you can actually act on and costs ~120 KB of DOM.
    horizon = (datetime.strptime(today_str, "%Y-%m-%d")
               + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    pending  = [s for s in sessions if s.get("date", "") >= today_str]
    upcoming = [s for s in pending if s["date"] <= horizon]
    beyond   = len(pending) - len(upcoming)

    rows = []

    # ── Z4+ mobility nudge ────────────────────────────────────────────────────
    done_today = [a for a in (activities or [])
                  if (a.get("start_date_local") or a.get("start_date") or "")[:10] == today_str]
    z4_today = sum(
        sum(z for z in (a.get("icu_hr_zone_times") or [])[3:7]) / 60
        for a in done_today
    )
    if z4_today >= 10:
        rows.append(
            '<hr class="div">'
            '<div style="background:#1c1008;border:1px solid #78350f;border-radius:6px;'
            'padding:7px 9px;margin-bottom:4px">'
            '<div style="font-size:10px;color:#e3b95f;font-weight:600;margin-bottom:3px">'
            '⚡ MOBILITY RECOMMENDED</div>'
            f'<div style="font-size:11px;color:#e3b95f;line-height:1.4">'
            f'{int(z4_today)}min Z4+ today — 10min mobility or stretching before bed will '
            f'reduce tomorrow\'s soreness.</div>'
            '</div>'
        )

    # ── Stretch streak ────────────────────────────────────────────────────────
    streak, stretched_today = get_stretch_status()
    if streak > 0 or stretched_today:
        flame = "🔥" * min(streak, 3)
        streak_color = "#6fcf97" if stretched_today else "#e3b95f"
        streak_label = f"{streak}d streak {flame}" if streak > 0 else "Start your streak today"
        done_badge = (
            '<span style="font-size:9px;color:#6fcf97;margin-left:6px">✓ done today</span>'
            if stretched_today else
            '<span style="font-size:9px;color:#56585a;margin-left:6px">not yet today</span>'
        )
        rows.append(
            '<hr class="div">'
            f'<div style="display:flex;align-items:center;justify-content:space-between">'
            f'<div class="label">Stretch</div>'
            f'<div style="font-size:11px;color:{streak_color};font-weight:600">{streak_label}</div>'
            f'</div>'
            f'<div style="font-size:10px;color:#56585a;margin-top:2px">'
            f'Run <code style="color:#a3a6a9">stretch log</code> to record a session{done_badge}</div>'
        )
    else:
        rows.append(
            '<hr class="div">'
            '<div style="display:flex;align-items:center;justify-content:space-between">'
            '<div class="label">Stretch</div>'
            '<div style="font-size:11px;color:#56585a">No streak yet</div>'
            '</div>'
            '<div style="font-size:10px;color:#56585a;margin-top:2px">'
            'Run <code style="color:#a3a6a9">stretch log</code> to start one</div>'
        )

    # ── Upcoming sessions ─────────────────────────────────────────────────────
    if upcoming:
        by_date = defaultdict(list)
        for s in upcoming:
            by_date[s["date"]].append(s)

        rows.append('<hr class="div"><div class="label" style="margin-bottom:6px">Next 7 days</div>')
        for date in sorted(by_date):
            if date == today_str:
                day_label = "Today"
                label_color = "#a3a6a9"
            else:
                d = datetime.strptime(date, "%Y-%m-%d")
                day_label = d.strftime("%a %-d %b")
                label_color = "#56585a"
            rows.append(
                f'<div style="font-size:10px;color:{label_color};'
                f'text-transform:uppercase;letter-spacing:.05em;'
                f'margin:5px 0 3px">{day_label}</div>'
            )
            for s in by_date[date]:
                name = s.get("name", "Session")
                z4 = s.get("z4_mins", 0)
                dot_color = "#e8837a" if z4 >= 10 else ("#e3b95f" if z4 >= 3 else "#56585a")
                rows.append(
                    f'<div style="display:flex;align-items:flex-start;gap:6px;'
                    f'margin-bottom:4px;line-height:1.3">'
                    f'<span style="color:{dot_color};margin-top:2px;flex-shrink:0">●</span>'
                    f'<span style="font-size:12px;color:#a3a6a9">{name}</span>'
                    f'</div>'
                )

        if beyond:
            rows.append(
                f'<div style="font-size:10px;color:#56585a;margin-top:7px;font-style:italic">'
                f'+{beyond} further session{"s" if beyond != 1 else ""} in the plan</div>'
            )
    else:
        rows.append('<hr class="div"><div class="label" style="margin-bottom:6px">Next 7 days</div>'
                    '<div style="font-size:11px;color:#56585a;font-style:italic">Nothing planned yet.</div>')

    rows.append(
        '<div class="plan-input">'
        '<textarea id="plan-box" rows="2" placeholder="Plan the week… e.g. today 60 min bike, '
        'tomorrow gym lower, Mon 3×10 erg (Ctrl+Enter)"></textarea>'
        '<div class="plan-btns">'
        '<button id="plan-mic-btn" class="mic-btn" onclick="toggleVoice(\'plan\')" '
        'title="Speak your week">&#127908;</button>'
        '<button onclick="submitPlan()">Plan</button>'
        '</div></div>'
        '<div class="food-status" id="plan-status" style="display:none"></div>'
        '<div id="plan-draft"></div>'
    )
    return "".join(rows)


# ── Main HTML builder ────────────────────────────────────────────────────────

def _color_illness(p):
    if p is None: return "#888"
    if p < 0.20:  return "#6fcf97"
    if p < 0.50:  return "#e3b95f"
    return "#e8837a"


def _illness_stat(illness):
    """Render the illness risk stat with stable IDs for JS updates."""
    if illness and illness.today is not None:
        p     = illness.today
        p_y   = illness.yesterday
        val   = f"{round(p * 100)}%"
        color = _color_illness(p)
        delta = round((p - p_y) * 100)
        sign  = "+" if delta >= 0 else ""
        sub   = f"{sign}{delta}pp vs yesterday"
        tip   = None
        if illness.bands:
            last = illness.bands[-1]
            tip  = f"Last episode: {last['start']} – {last['end']}"
    elif illness is not None:
        val, color, sub, tip = "—", "#888", "awaiting today's data", None
    else:
        val, color, sub, tip = "—", "#888", "model loading…", None

    sub_html = f'<div class="sub" id="illness-sub">{sub}</div>'
    tip_attr = f' data-tip="{tip}"' if tip else ""
    return (f'<div class="stat stat-link"{tip_attr} onclick="selectTab(\'illness\')" title="Open illness detail">'
            f'<div class="label">Illness Risk</div>'
            f'<div class="value" id="illness-val" style="color:{color}">{val}</div>'
            f'{sub_html}'
            f'<div style="position:relative;width:100%;height:28px;margin-top:5px;border-radius:3px;overflow:hidden">'
            f'<canvas id="illness-spark" style="position:absolute;top:0;left:0;width:100%;height:100%"></canvas>'
            f'</div>'
            f'</div>')


def _focus_stat():
    """KPI tile for the Focus tab: today's check-ins and the latest mood/energy."""
    if not _focus:
        return ""
    try:
        p = _focus.focus_payload()
    except Exception:
        return ""
    done = len(p["today_slots"])
    L = p.get("latest")
    sub = (f"mood {L['mood'] or '-'} · energy {L['energy'] or '-'}"
           if L else "no check-ins yet")
    return _stat("Focus", f"{done}/3 today", "#88dcfd" if done else "#a3a6a9", sub, link="focus")


def build_html(wellness, activities, training_plan, summary=None, calorie_target=None,
               food_data=None, illness=None, surplus_target=0):
    try:
        _cfg_anchor = (load_config().get("oya_anchor") or "")
        oya_html = _oya.render_panel(activities, wellness,
                                     anchor=date.fromisoformat(_cfg_anchor) if _cfg_anchor else None) \
            if _oya else '<div style="padding:22px 26px;color:#75777a">one_year_ago module not available.</div>'
    except Exception:
        log.warning("Vs Last Year panel failed to render", exc_info=True)
        oya_html = '<div style="padding:22px 26px;color:#e0766b">Could not build the year comparison; see the log.</div>'
    entries     = sorted([w for w in wellness if w.get("id")], key=lambda w: w["id"])
    today_entry = next((w for w in reversed(entries) if w.get("ctl") or w.get("hrv")), {})
    recent      = entries[-7:]

    ctl         = today_entry.get("ctl")
    atl         = today_entry.get("atl")
    tsb         = round(ctl - atl, 1) if ctl and atl else None
    hrv         = today_entry.get("hrv")
    rhr         = today_entry.get("restingHR")
    sleep_score = today_entry.get("sleepScore")
    sleep_secs  = today_entry.get("sleepSecs")

    hrv_vals     = [w.get("hrv") for w in recent if w.get("hrv")]
    hrv_baseline = sum(hrv_vals[:-1]) / len(hrv_vals[:-1]) if len(hrv_vals) > 1 else None
    hrv_delta    = (hrv - hrv_baseline) if hrv and hrv_baseline else None

    sleep_14d = [w.get("sleepSecs") for w in entries[-14:] if w.get("sleepSecs")]
    if sleep_14d:
        avg_daily_debt = sum(8.0 - s / 3600 for s in sleep_14d) / len(sleep_14d)
        sign           = "deficit" if avg_daily_debt > 0 else "surplus"
        debt_str       = f"{abs(avg_daily_debt):.1f}h {sign}"
        debt_color     = "#e8837a" if avg_daily_debt > 1 else ("#e3b95f" if avg_daily_debt > 0 else "#6fcf97")
    else:
        debt_str, debt_color = "—", "#888"

    today_str_h = datetime.now().strftime("%Y-%m-%d")
    # Pace target: what fraction of the day's kcal "should" be eaten by now, so intra-day
    # displays judge against expected-so-far rather than the full-day total (which reads
    # as a false deficit early in the day even when perfectly on track). Eating window
    # rather than midnight-to-midnight, since no one eats 00:00-07:00. Shared with the
    # food-log quick-add refresh via compute_nutrition_context so the two stay in sync.
    nutr_ctx        = compute_nutrition_context(activities, calorie_target)
    kcal_base       = nutr_ctx["kcal_base"]
    session_kcal    = nutr_ctx["session_kcal"]
    kcal_total      = nutr_ctx["kcal_total"]
    day_frac        = nutr_ctx["day_frac"]
    expected_so_far = nutr_ctx["expected_so_far"]
    kcal_sub     = f"{kcal_base:,} base"
    kcal_sub    += f" + {session_kcal:,} burned" if session_kcal else " · no sessions yet"
    kcal_disp = f"{kcal_total:,} kcal"
    kcal_stat = _stat("Calorie Target", kcal_disp, "#a3a6a9", kcal_sub, link="nutrition:nutr-cal")

    cal_target_for_chart = calorie_target if calorie_target else 2700
    cal_hist_dates, cal_hist_consumed, cal_hist_target = get_calorie_history(
        cal_target_for_chart, days=14, activities=activities)
    # Today's entry in the 14-day history is a full-day target even though the day isn't
    # over — swap in the pace-adjusted expectation just for today so its bar/cumulative
    # balance isn't judged against a target that hasn't finished accruing yet.
    cal_hist_pace_target = list(cal_hist_target)
    if cal_hist_dates and cal_hist_dates[-1] == today_str_h:
        cal_hist_pace_target[-1] = round(expected_so_far)

    bulk_trend = get_bulk_trend(cal_hist_dates, cal_hist_consumed, cal_hist_target,
                                 surplus_goal=surplus_target, today_str=today_str_h,
                                 day_frac=day_frac) if surplus_target else None
    if bulk_trend:
        bt_color = {"on track": "#6fcf97", "slow but positive": "#e3b95f", "in deficit": "#e8837a"}[bulk_trend["state"]]
        proj_sub = ""
        if bulk_trend["today_projected"] is not None:
            proj_sub = f" · today proj. {bulk_trend['today_projected']:+,} kcal"
        # Keep the tile scannable: one line of label, one of sub. The window size,
        # logging coverage and today's projection live in the hover tip.
        bt_stat = _stat(
            "Bulk Trend",
            f"{bulk_trend['avg_surplus']:+,} kcal/day",
            bt_color,
            f"{bulk_trend['kg_per_week']:+.2f} kg/wk · {bulk_trend['state']}",
            tip=(f"{bulk_trend['window']}-day average over prior days · "
                 f"{bulk_trend['logged_days']}/{bulk_trend['window']} days logged"
                 f"{proj_sub}"),
            link="nutrition:nutr-bulk",
        )
    else:
        bt_stat = ""
    bulk_label_suffix = f" · rolling avg vs +{surplus_target:,}/day goal" if surplus_target else ""

    # Contextual hover tooltips
    if tsb is not None:
        if tsb > 10:   tsb_tip = f"TSB {tsb:+.1f}: well-rested, fitness banked — good for racing or hard sessions."
        elif tsb > 0:  tsb_tip = f"TSB {tsb:+.1f}: slightly fresh — optimal training zone."
        elif tsb > -10: tsb_tip = f"TSB {tsb:+.1f}: moderate fatigue accumulation. Normal training load."
        elif tsb > -20: tsb_tip = f"TSB {tsb:+.1f}: significant fatigue. Expect reduced top-end output."
        else:           tsb_tip = f"TSB {tsb:+.1f}: heavy fatigue. Prioritise recovery before key sessions."
        tsb_tip += f"\nCTL (fitness) = {ctl:.0f}, ATL (fatigue) = {atl:.0f}." if ctl and atl else ""
    else:
        tsb_tip = None

    if hrv and hrv_baseline:
        pct = (hrv - hrv_baseline) / hrv_baseline * 100
        if pct > 5:    hrv_tip = f"HRV {int(hrv)} is {pct:.0f}% above your 7-day avg — nervous system well recovered."
        elif pct > -5: hrv_tip = f"HRV {int(hrv)} is within normal range of your 7-day avg ({hrv_baseline:.0f})."
        else:          hrv_tip = f"HRV {int(hrv)} is {abs(pct):.0f}% below your 7-day avg — signs of residual stress or fatigue."
    else:
        hrv_tip = None

    if rhr:
        rhr_tip = (f"Resting HR {int(rhr)} bpm. Elevated RHR (vs your norm) can indicate fatigue, illness, or dehydration. "
                   "Lower is generally better for aerobic athletes.")
    else:
        rhr_tip = None

    if sleep_secs:
        sleep_hrs = sleep_secs / 3600
        if sleep_score and sleep_score >= 80:   slp_qual = "good quality"
        elif sleep_score and sleep_score >= 60: slp_qual = "moderate quality"
        else:                                   slp_qual = "poor quality"
        sleep_tip = f"{sleep_hrs:.1f}h of {slp_qual} sleep. 7–9h is optimal for athletic recovery. " \
                    f"Sleep deprivation reduces power output and reaction time within 24h."
    else:
        sleep_tip = None

    if sleep_14d:
        debt_tip = (f"Average {abs(avg_daily_debt):.1f}h {'short' if avg_daily_debt > 0 else 'over'} "
                    f"your 8h/night target across the last {len(sleep_14d)} days. "
                    "Chronic sleep restriction compounds even when individual nights feel manageable.")
    else:
        debt_tip = None

    # Sleep clearance recommendation (uses EMA model, same τ=5 as chart)
    _slp_dates, _slp_vals = compute_sleep_balance(entries)
    current_sleep_debt = _slp_vals[-1] if _slp_vals else 0.0
    _today = date.today()
    _key_dates = training_plan.get("key_dates", []) if training_plan else []
    _next_exam_days = None
    for kd in sorted(_key_dates, key=lambda x: x.get("date", "")):
        kd_date = datetime.strptime(kd["date"], "%Y-%m-%d").date()
        delta = (kd_date - _today).days
        if 1 <= delta <= 30:
            _next_exam_days = delta
            break
    clearance_val, clearance_sub, clearance_color, clearance_tip = sleep_debt_clearance(
        current_sleep_debt, next_exam_days=_next_exam_days)
    clearance_stat = _stat("Sleep Clearance", clearance_val, clearance_color, clearance_sub, tip=clearance_tip, link="sleep")

    # The "Logged today" card content — entries + quick-add only. The calorie bar
    # and macro rows the old widget drew are now their own dashboard cards.
    food_log_html   = render_food_log(food_data)
    weekly_log_html = render_weekly_food_log()

    # Three fragments, three homes: the KPI strip, the Nutrition tab, the Today tab.
    kpi_html = "".join([
        _stat("Form (TSB)", f"{tsb:+.1f}" if tsb is not None else "—",
              _color_tsb(tsb),
              f"Fitness {ctl:.0f} · Fatigue {atl:.0f}" if ctl and atl else None,
              tip=tsb_tip, link="trends:w-tsb"),
        _stat("HRV", f"{int(hrv)}" if hrv else "—",
              _color_hrv(hrv_delta),
              f"7-day avg {hrv_baseline:.0f}" if hrv_baseline else None,
              tip=hrv_tip, link="trends:w-hrv"),
        _stat("Resting HR", f"{int(rhr)} bpm" if rhr else "—", tip=rhr_tip, link="trends:w-hrv"),
        _illness_stat(illness),
        _stat("Sleep", _fmt_sleep(sleep_secs), _color_sleep(sleep_score),
              f"Score {int(sleep_score)}" if sleep_score else None,
              tip=sleep_tip, link="sleep"),
        _stat("Sleep Avg (14d)", debt_str, debt_color, "per night vs 8h target", tip=debt_tip, link="sleep"),
        clearance_stat,
        kcal_stat,
        bt_stat,
        _focus_stat(),
    ])


    sessions_html = _build_sessions_html(training_plan, today_str_h, activities=activities)

    chart_entries = [w for w in entries if w.get("ctl") or w.get("atl")]
    chart_dates   = [w["id"] for w in chart_entries]
    ctl_vals      = [w.get("ctl") for w in chart_entries]
    atl_vals      = [w.get("atl") for w in chart_entries]
    tsb_vals      = [round(w["ctl"] - w["atl"], 1) if w.get("ctl") and w.get("atl") else None
                     for w in chart_entries]
    hrv_vals_c    = [w.get("hrv") for w in chart_entries]
    rhr_vals_c    = [w.get("restingHR") for w in chart_entries]

    hil_dates, hil_vals           = compute_hi_load_series(activities)
    proj_hil_dates, proj_hil_vals = compute_hi_load_projection(activities, training_plan, hil_series=(hil_dates, hil_vals))
    sleep_dates, sleep_balance    = compute_sleep_balance(entries)
    proj_dates, proj_ctl, proj_atl, proj_tsb, proj_ctl_sig, proj_atl_sig, proj_tsb_sig = \
        compute_projections(entries, training_plan)
    slp_proj_dates, slp_proj_best, slp_proj_med, slp_proj_trend, slp_avg = \
        compute_sleep_projections(entries)

    plan_sessions = [{"date": s["date"], "name": s.get("name", ""), "tss": s.get("tss", 0)}
                     for s in training_plan.get("sessions", [])]


    weight_entries = [w for w in entries if w.get("weight") and w.get("id")]
    weight_dates  = [w["id"] for w in weight_entries]
    weight_vals   = [w["weight"] for w in weight_entries]
    weight_by_date = dict(zip(weight_dates, weight_vals))
    weight_vals_full = [weight_by_date.get(d) for d in chart_dates]

    today_dt = datetime.now()
    illness_7d_dates = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    illness_by_date  = illness.by_date if illness else {}
    illness_7d_vals  = [round(illness_by_date.get(d, 0.0), 4) for d in illness_7d_dates]

    # Full posterior series + fitted hyperparameters, for the Illness tab.
    illness_all_dates = sorted(illness_by_date)
    illness_all_vals  = [illness_by_date[d] for d in illness_all_dates]
    illness_meta      = get_model_meta() if _ILLNESS_AVAILABLE else None
    sick_thresh_str   = f"{illness_meta['sick_thresh']:.1f}" if illness_meta else "0.4"
    # Resting HR over the same days, so risk can be read against its driver.
    rhr_by_date       = {w["id"]: w.get("restingHR") for w in entries if w.get("id")}
    illness_rhr       = [rhr_by_date.get(d) for d in illness_all_dates]

    benchmark_cfg = load_benchmark_config()
    # "bodyweight" is auto-sourced from the wellness weight log (see
    # weight_history below), not manually logged, so leave it off the form.
    metric_options_html = "\n".join(
        f'<option value="{key}">{meta["label"]}</option>'
        for key, meta in benchmark_cfg["metrics"].items() if key != "bodyweight")

    chart_data = json.dumps({
        "focus": (_focus.focus_payload() if _focus else None),
        "sleep": (_sleep.sleep_payload(wellness) if _sleep else None),
        "sustain": (_sustain.sustain_payload(
            wellness, activities, illness,
            illness_paused=bool((training_plan or {}).get("illness") and
                                not (training_plan or {})["illness"].get("cleared"))) if _sustain else None),
        "life": (_life.life_payload((training_plan or {}).get("sessions", [])) if _life else None),
        "dates": chart_dates, "ctl": ctl_vals, "atl": atl_vals, "tsb": tsb_vals,
        "hrv": hrv_vals_c, "rhr": rhr_vals_c,
        "hilDates": hil_dates, "hil": hil_vals,
        "projHilDates": proj_hil_dates, "projHil": proj_hil_vals,
        "sleepDates": sleep_dates, "sleepBalance": sleep_balance,
        "slpProjDates": slp_proj_dates, "slpProjBest": slp_proj_best,
        "slpProjMed": slp_proj_med, "slpProjTrend": slp_proj_trend,
        "slpAvg": slp_avg,
        "projDates": proj_dates, "projCtl": proj_ctl, "projAtl": proj_atl, "projTsb": proj_tsb,
        "projCtlSig": proj_ctl_sig, "projAtlSig": proj_atl_sig, "projTsbSig": proj_tsb_sig,
        "planSessions": plan_sessions,
        "calHistDates": cal_hist_dates, "calHistConsumed": cal_hist_consumed,
        "calHistTarget": cal_hist_target, "calHistPaceTarget": cal_hist_pace_target,
        "calTodayIdx": (len(cal_hist_dates) - 1
                         if cal_hist_dates and cal_hist_dates[-1] == today_str_h else None),
        "weightDates": weight_dates, "weightVals": weight_vals,
        "weightValsFull": weight_vals_full,
        "illnessBands": illness.bands if illness else [],
        "illness7dDates": illness_7d_dates, "illness7dVals": illness_7d_vals,
        "illnessAllDates": illness_all_dates, "illnessAllVals": illness_all_vals,
        "illnessRhr": illness_rhr, "illnessMeta": illness_meta,
        # Gap to the pace marker, not to the end-of-day total.
        "calGapKcal": round(nutr_ctx["expected_so_far"] - (food_data or {}).get("calories", 0)),
        "calPaceKcal": round(nutr_ctx["expected_so_far"]),
        "nutrition": nutrition_data(food_data, nutr_ctx),
        "bulk": (bulk_trend | {
            "surplusGoal": surplus_target,
            "kcalPerKg": KCAL_PER_KG,
        }) if bulk_trend else {"surplusGoal": surplus_target, "kcalPerKg": KCAL_PER_KG},
        "metrics": build_metrics_data(load_test_metrics().get("entries", []), benchmark_cfg["metrics"],
                                       weight_history=[{"date": d, "value": v} for d, v in zip(weight_dates, weight_vals)]),
    })

    day_str = datetime.now().strftime("%A %d %B")
    loading_span = '<span class="brief-loading">Generating…</span>'
    if isinstance(summary, dict):
        overview_html = summary.get("overview", "").replace("\n", "<br>") or loading_span
        tips_html     = summary.get("tips", "").replace("\n", "<br>") or loading_span
    else:
        overview_html = loading_span
        tips_html     = loading_span

    notes_html = render_notes_html()

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#1b1c1e;color:#e8e9ea;font-family:-apple-system,'Segoe UI',sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}}
.header{{padding:11px 18px;border-bottom:1px solid #2a2c2f;display:flex;align-items:center;
         gap:10px;flex-shrink:0}}
.header h1{{font-size:14px;font-weight:600}}
.header .date{{font-size:12px;color:#75777a}}
select{{background:#2a2c2f;color:#a3a6a9;border:1px solid #424446;border-radius:5px;
        padding:3px 8px;font-size:12px;cursor:pointer;outline:none}}
select:hover{{border-color:#56585a}}
#appSwitch{{margin-left:auto}}
#periodSelect{{margin-left:8px}}
#periodSelect[hidden]{{display:none}}

/* ── tabs ─────────────────────────────────────────────────────────────────── */
.tabs{{display:flex;gap:2px;margin-left:14px}}
.tab{{background:none;border:1px solid transparent;border-radius:5px;color:#75777a;
      font-family:inherit;font-size:11.5px;padding:4px 11px;cursor:pointer;
      transition:color .15s,background .15s,border-color .15s}}
.tab:hover{{color:#a3a6a9;background:#2a2c2f}}
.tab:focus-visible{{outline:2px solid #88dcfd;outline-offset:1px}}
.tab[aria-selected="true"]{{color:#88dcfd;border-color:#88dcfd;background:#1a3e4c}}

.tab-body{{flex:1;min-height:0;overflow-y:auto}}
.panel{{display:none}}
.panel.active{{display:block}}

/* ── KPI strip (was a 185px vertical rail) ────────────────────────────────── */
.stats{{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;align-items:start;
        padding:10px 6px;border-bottom:1px solid #2a2c2f;flex-shrink:0}}
.stat{{padding:0 14px;border-left:1px solid #2a2c2f;min-width:0}}
.stat:first-child{{border-left:none}}
.stat-link{{cursor:pointer}}
.stat-link:hover .label{{color:#88dcfd}}
.back-link{{color:#a3a6a9;text-decoration:none;font-size:12px;margin-right:12px}}
.back-link:hover{{color:#88dcfd}}
.label{{font-size:9.5px;color:#75777a;text-transform:uppercase;letter-spacing:.06em;
        margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.value{{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
/* Tiles are ~1/10th of the window now, not a 185px column. Clamp the sub to two
   lines so one verbose stat cannot set the height of the whole strip. */
.sub{{font-size:10.5px;color:#75777a;margin-top:2px;line-height:1.35;
      display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
hr.div{{border:none;border-top:1px solid #2a2c2f;margin:8px 0 10px}}
.charts{{padding:12px 14px;display:grid;
         grid-template-columns:1fr 1fr;grid-template-rows:repeat(3,180px);
         gap:14px 12px;min-width:0}}
.chart-cell{{display:flex;flex-direction:column;min-height:0}}
.chart-cell.wide{{grid-column:1 / -1}}
.chart-label{{font-size:10px;color:#75777a;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:3px;flex-shrink:0;display:flex;align-items:center;gap:8px}}
.chart-wrap{{flex:1;min-height:0;position:relative}}
canvas{{position:absolute;top:0;left:0;width:100%;height:100%}}
/* ── Today: brief + dials + sleep + sessions + notes ──────────────────────── */
.today-dash{{display:grid;gap:16px;padding:18px 22px;align-items:start;
             grid-template-columns:minmax(0,1.5fr) minmax(260px,1fr);
             grid-template-areas:"brief dials" "sleep sessions" "notes sessions";}}
.today-brief{{grid-area:brief;min-width:0}}
.today-side{{grid-area:dials;min-width:0}}
.today-sleep{{grid-area:sleep;min-width:0}}
.today-sessions{{grid-area:sessions;min-width:0;max-height:calc(100vh - 300px);overflow-y:auto;
                 padding-right:4px}}
.today-notes{{grid-area:notes;min-width:0;display:flex;flex-direction:column;gap:8px}}
.today-brief .brief-text, .today-brief .tips-text{{max-width:64ch}}
.today-sleep .chart-wrap{{position:relative;height:150px;margin-top:5px}}
.today-sessions hr.div:first-child{{display:none}}
@media (max-width:1000px){{
  .today-dash{{grid-template-columns:1fr;
               grid-template-areas:"brief" "dials" "sleep" "sessions" "notes";}}
}}

/* Compact nutrition dials shared with the Nutrition tab's data */
.today-dials{{display:flex;flex-direction:column;gap:12px;
             background:#1f2123;border:1px solid #2a2c2f;border-radius:10px;padding:14px 16px}}
.dials-top{{display:flex;align-items:center;gap:14px}}
.dial-ring{{position:relative;width:104px;height:104px;flex-shrink:0}}
.dial-ring canvas{{position:absolute;inset:0;width:100%;height:100%}}
.dial-ring-mid{{position:absolute;inset:0;display:flex;flex-direction:column;
                align-items:center;justify-content:center;text-align:center}}
.dial-ring-mid .big{{font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}}
.dial-ring-mid .small{{font-size:9px;color:#75777a;margin-top:2px}}
.dial-macros{{flex:1;min-width:0;display:flex;flex-direction:column;gap:7px}}
.dm-row{{display:grid;grid-template-columns:38px 1fr auto;align-items:center;gap:7px;font-size:10px}}
.dm-name{{color:#a3a6a9}}
.dm-track{{height:6px;background:#1b1c1e;border-radius:3px;overflow:hidden}}
.dm-fill{{height:100%;border-radius:3px}}
.dm-val{{color:#75777a;font-variant-numeric:tabular-nums;white-space:nowrap}}
.dial-foot{{display:flex;justify-content:space-between;font-size:10px;color:#75777a;
            border-top:1px solid #2a2c2f;padding-top:9px;font-variant-numeric:tabular-nums}}
.dial-foot b{{color:#e8e9ea}}
.dial-link{{margin-top:2px;font-size:10px;color:#88dcfd;cursor:pointer;background:none;border:none;
            padding:0;text-align:left}}
.dial-link:hover{{text-decoration:underline}}

/* ── Nutrition: the old rail widget, given room ───────────────────────────── */
.nutr-grid{{display:grid;grid-template-columns:minmax(340px,440px) minmax(0,1fr);
            gap:34px;padding:18px 22px;align-items:start}}
.nutr-col{{min-width:0}}
.nutr-grid hr.div:first-child{{display:none}}
/* Prose set across a 1400px window is unreadable — hold it to a sane measure. */
.nutr-grid .tips-text{{max-width:70ch;font-size:14px;line-height:1.7}}

/* ── Nutrition dashboard ──────────────────────────────────────────────────── */
.nutr-dash{{padding:16px 20px;display:grid;gap:14px;
            grid-template-columns:repeat(12,1fr);align-items:start}}
.nutr-card{{background:#1f2123;border:1px solid #2a2c2f;border-radius:10px;padding:14px 16px;
            min-width:0}}
.cal-ring-card{{grid-column:span 3}}
.nutr-card:nth-child(3){{grid-column:span 4}}   /* composition pie (after pace + ring cards) */
.pace-card{{grid-column:span 12!important}}
.macro-break-card{{grid-column:span 5}}
.bulk-card{{grid-column:span 5}}
.weight-card{{grid-column:span 7}}
.gap-slot{{grid-column:span 7}}
.foodlog-card{{grid-column:span 5}}
.weeklog-card{{grid-column:span 7}}
@media (max-width:1100px){{
  .cal-ring-card, .nutr-card:nth-child(3), .macro-break-card, .bulk-card,
  .weight-card, .gap-slot, .foodlog-card, .weeklog-card {{ grid-column:span 12 }}
}}
.nutr-card-h{{font-size:10px;color:#75777a;text-transform:uppercase;letter-spacing:.06em;
              margin-bottom:10px}}
.nutr-card-sub{{color:#56585a;text-transform:none;letter-spacing:0;margin-left:6px}}

.cal-ring-wrap{{position:relative;width:150px;height:150px;margin:2px auto 8px}}
.cal-ring-wrap canvas{{position:absolute;inset:0;width:100%;height:100%}}
.cal-ring-mid{{position:absolute;inset:0;display:flex;flex-direction:column;
               align-items:center;justify-content:center;text-align:center;pointer-events:none}}
.cal-ring-mid .big{{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}}
.cal-ring-mid .small{{font-size:10px;color:#75777a;margin-top:3px}}
.cal-ring-legend{{display:flex;flex-direction:column;gap:4px;font-size:11px}}
.cal-ring-legend .row{{display:flex;justify-content:space-between;color:#a3a6a9;
                       font-variant-numeric:tabular-nums}}
.cal-ring-legend .row b{{color:#e8e9ea;font-weight:600}}

.compo-row{{display:flex;align-items:center;gap:14px}}
.compo-pie-wrap{{position:relative;width:120px;height:120px;flex-shrink:0}}
.compo-pie-wrap canvas{{position:absolute;inset:0;width:100%;height:100%}}
.compo-legend{{display:flex;flex-direction:column;gap:6px;flex:1;min-width:0}}
.compo-legend .row{{display:flex;align-items:center;gap:8px;font-size:12px;color:#d4d6d8}}
.compo-legend .sw{{width:10px;height:10px;border-radius:3px;flex-shrink:0}}
.compo-legend .pct{{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600;color:#e8e9ea}}
.compo-legend .gg{{font-size:10px;color:#75777a;font-variant-numeric:tabular-nums}}

.macro-break{{display:flex;flex-direction:column;gap:11px}}
.mb-row{{display:grid;grid-template-columns:64px 1fr 96px;align-items:center;gap:10px}}
.mb-name{{font-size:11px;color:#a3a6a9}}
.mb-track{{position:relative;height:8px;background:#1b1c1e;border-radius:4px;overflow:hidden}}
.mb-fill{{height:100%;border-radius:4px;transition:width .5s ease}}
.mb-target-mark{{position:absolute;top:-2px;bottom:-2px;width:2px;background:#75777a}}
.mb-val{{font-size:10.5px;color:#75777a;text-align:right;font-variant-numeric:tabular-nums;
         white-space:nowrap}}
.mb-val b{{color:#e8e9ea;font-weight:600}}
.mb-dot{{display:inline-block;width:6px;height:6px;border-radius:50%;margin-left:5px}}

.goal-adjuster{{margin-bottom:12px}}
.goal-readout{{display:flex;align-items:baseline;gap:6px;margin-bottom:6px}}
.goal-kg{{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}}
.goal-kg-unit{{font-size:11px;color:#75777a}}
.goal-kcal{{margin-left:auto;font-size:12px;color:#a3a6a9;font-variant-numeric:tabular-nums}}
#goal-slider{{width:100%;accent-color:#88dcfd;cursor:pointer}}
.goal-scale{{display:flex;justify-content:space-between;font-size:9px;color:#56585a;
             text-transform:uppercase;letter-spacing:.05em;margin-top:2px}}
.bulk-stats{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.bulk-stat{{background:#1b1c1e;border:1px solid #2a2c2f;border-radius:7px;padding:8px 10px}}
.bulk-stat .k{{font-size:9px;color:#75777a;text-transform:uppercase;letter-spacing:.05em}}
.bulk-stat .v{{font-size:15px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}}

.weight-wrap{{position:relative;height:150px}}
.weight-wrap canvas{{position:absolute;inset:0;width:100%;height:100%}}
.gap-slot .tips-text{{font-size:13px;line-height:1.6;max-width:none}}
.insight-card{{grid-column:span 5}}
.insight-card .tips-text{{font-size:13px;line-height:1.65;max-width:none}}
@media (max-width:1100px){{ .insight-card{{grid-column:span 12}} }}

/* Logged-today card: entries + quick-add only */
.food-entry-list{{display:flex;flex-direction:column;max-height:230px;overflow-y:auto}}
.food-entry-row{{display:flex;gap:8px;font-size:11.5px;padding:5px 0;border-bottom:1px solid #25282b}}
.food-entry-time{{color:#56585a;width:38px;flex-shrink:0;font-variant-numeric:tabular-nums}}
.food-entry-desc{{color:#d4d6d8;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.food-entry-kcal{{color:#75777a;flex-shrink:0;font-variant-numeric:tabular-nums}}
.food-entry-btn{{background:none;border:none;color:#424446;font-size:11px;cursor:pointer;padding:0 2px;
  flex-shrink:0;line-height:1}}
.food-entry-btn:hover{{color:#88dcfd}}
.food-entry-edit-form{{display:flex;gap:4px;width:100%;align-items:center}}
.food-entry-edit-form input{{background:#1b1c1e;border:1px solid #424446;border-radius:4px;color:#e8e9ea;
  font-size:11px;padding:3px 5px;font-family:inherit;outline:none;min-width:0}}
.food-entry-edit-form input:focus{{border-color:#88dcfd}}
.food-entry-edit-form .fe-time{{width:42px;flex-shrink:0}}
.food-entry-edit-form .fe-desc{{flex:1;min-width:0}}
.food-entry-edit-form .fe-num{{width:44px;flex-shrink:0}}
.food-entry-edit-form button{{background:#2a2c2f;border:1px solid #424446;color:#a3a6a9;border-radius:4px;
  font-size:11px;padding:3px 7px;cursor:pointer;flex-shrink:0}}
.food-entry-edit-form button:hover{{background:#424446;color:#e8e9ea}}
.food-log-empty{{font-size:11.5px;color:#56585a;font-style:italic;padding:2px 0 6px}}
.week-day{{border-bottom:1px solid #25282b}}
.week-day:last-child{{border-bottom:none}}
.week-day summary{{display:flex;align-items:center;justify-content:space-between;gap:8px;
                    padding:7px 0;cursor:pointer;list-style:none;font-size:12px}}
.week-day summary::-webkit-details-marker{{display:none}}
.week-day summary::before{{content:'\\25b8';color:#56585a;margin-right:6px;font-size:10px}}
.week-day[open] summary::before{{content:'\\25be'}}
.week-day-label{{color:#d4d6d8;flex:1}}
.week-day-label.is-today{{color:#88dcfd}}
.week-day-total{{color:#75777a;font-variant-numeric:tabular-nums;flex-shrink:0}}
.week-day-entries{{padding:0 0 8px 16px}}
.week-entry-row{{display:flex;gap:8px;font-size:11px;padding:3px 0;color:#a3a6a9}}
.week-entry-row .food-entry-time{{color:#56585a;width:38px;flex-shrink:0;font-variant-numeric:tabular-nums}}
.week-entry-row .food-entry-desc{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.week-entry-row .food-entry-kcal{{color:#75777a;flex-shrink:0;font-variant-numeric:tabular-nums}}
.food-input-row{{display:flex;gap:6px;margin-top:10px}}
.food-input-row input{{flex:1;min-width:0;background:#1b1c1e;border:1px solid #424446;border-radius:5px;
  color:#e8e9ea;font-size:12px;padding:6px 9px;font-family:inherit;outline:none}}
.food-input-row input:focus{{border-color:#88dcfd}}
.food-input-row button{{background:#2a2c2f;border:1px solid #424446;color:#a3a6a9;border-radius:5px;
  padding:6px 13px;font-size:11.5px;cursor:pointer;font-family:inherit}}
.food-input-row button:hover{{background:#424446;color:#e8e9ea}}
.mic-btn.recording{{background:#7f1d1d;border-color:#e8837a;color:#fecaca;animation:micpulse 1.2s ease-in-out infinite}}
.mic-btn.busy{{opacity:.5;cursor:wait}}
@keyframes micpulse{{0%,100%{{box-shadow:0 0 0 0 rgba(248,113,113,.5)}}50%{{box-shadow:0 0 0 4px rgba(248,113,113,0)}}}}
.food-status{{font-size:10.5px;margin-top:6px}}
.plan-input{{display:flex;gap:6px;align-items:flex-end;margin-top:12px}}
#plan-box{{flex:1;min-width:0;background:#1d1f22;color:#e8e9ea;border:1px solid #2a2c2f;border-radius:5px;
          padding:5px 7px;font-size:12px;font-family:inherit;resize:vertical}}
.plan-btns{{display:flex;gap:5px}}
.plan-btns button{{background:#2a2c2f;color:#e8e9ea;border:1px solid #424446;border-radius:5px;padding:5px 9px;
          font-size:11px;cursor:pointer}}
.plan-btns button:hover{{background:#424446}}
.plan-grid{{width:100%;border-collapse:collapse;margin-top:8px;font-size:11px}}
.plan-grid th{{color:#75777a;font-weight:500;text-align:left;font-size:9px;text-transform:uppercase;padding:0 3px 3px}}
.plan-grid td{{padding:2px 3px}}
.plan-grid input,.plan-grid select{{width:100%;box-sizing:border-box;background:#1b1c1e;color:#e8e9ea;
          border:1px solid #424446;border-radius:4px;padding:3px 4px;font-size:11px;font-family:inherit}}
.plan-grid .pg-date{{width:112px}} .plan-grid .pg-min,.plan-grid .pg-work{{width:48px}}
.plan-grid .pg-del{{background:none;border:none;color:#75777a;cursor:pointer}}
.plan-grid .pg-del:hover{{color:#e8837a}}
.plan-warn{{font-size:11px;color:#e3b95f;margin-top:5px;line-height:1.35}}
.food-status.pending{{color:#a3a6a9}} .food-status.error{{color:#e8837a}}

/* ── Catch-up-to-pace suggestion ──────────────────────────────────────────── */
.gap-card{{background:#1c1008;border:1px solid #78350f;border-radius:9px;
           padding:12px 14px;margin-bottom:14px}}
.gap-head{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px}}
.gap-head .brief-label{{color:#e3b95f}}
.gap-amount{{font-size:15px;font-weight:700;color:#e3b95f;font-variant-numeric:tabular-nums}}
.gap-text{{font-size:13.5px;line-height:1.65;color:#e3b95f}}

/* ── Cards: give panel content edges so it does not float in dead space ───── */
.card{{background:#1f2123;border:1px solid #2a2c2f;border-radius:9px;padding:14px 16px}}

/* ── Illness (experimental) ───────────────────────────────────────────────── */
.ill-panel{{padding:16px 22px;display:flex;flex-direction:column;gap:14px}}
.ill-head{{display:flex;align-items:center;justify-content:space-between}}
.badge-exp{{margin-left:9px;font-size:8.5px;letter-spacing:.08em;color:#e3b95f;
            background:#2a2110;border:1px solid #4a3a12;border-radius:4px;padding:2px 6px;
            text-transform:uppercase;vertical-align:1px}}
.ill-today{{font-size:12px;color:#75777a;font-variant-numeric:tabular-nums}}
.ill-today b{{font-size:20px;font-weight:600;margin-right:7px}}

.ill-charts{{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px}}
.ill-charts .chart-wrap{{height:190px}}
.swatch-x{{color:#56585a;letter-spacing:0;text-transform:none;font-size:9.5px}}

.ill-lower{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);gap:30px;
            align-items:start}}
.ill-stats{{display:grid;grid-template-columns:repeat(auto-fill,minmax(146px,1fr));gap:8px}}
.ill-stat{{background:#1f2123;border:1px solid #2a2c2f;border-radius:7px;padding:8px 10px}}
.ill-stat .k{{font-size:9px;color:#75777a;text-transform:uppercase;letter-spacing:.06em}}
.ill-stat .v{{font-size:15px;font-weight:600;color:#e8e9ea;margin-top:2px;
              font-variant-numeric:tabular-nums}}
.ill-stat .u{{font-size:10px;color:#56585a;font-weight:400;margin-left:2px}}

.ill-episodes{{display:flex;flex-direction:column;gap:5px;max-height:150px;overflow-y:auto}}
.ill-ep{{display:flex;align-items:baseline;gap:10px;font-size:11.5px;color:#a3a6a9;
         padding:5px 9px;background:#1f2123;border:1px solid #2a2c2f;border-radius:6px;
         font-variant-numeric:tabular-nums}}
.ill-ep .dur{{margin-left:auto;color:#56585a;font-size:10.5px}}
.ill-ep .pk{{color:#e8837a}}

.ill-about{{max-width:74ch}}
.ill-about p{{font-size:12.5px;line-height:1.65;color:#a3a6a9;margin-bottom:9px}}
.ill-about em{{color:#d4d6d8;font-style:normal;font-weight:500}}
.ill-warn{{border-left:2px solid #78350f;padding-left:10px;color:#a8a29e !important}}

/* ── Notes: was one carousel panel of four ────────────────────────────────── */
.notes-panel-full{{padding:18px 22px;display:flex;flex-direction:column;gap:8px;max-width:90ch}}
.notes-hint{{font-size:9px;color:#56585a;text-transform:none;letter-spacing:0}}
.notes-list{{min-height:0;overflow-y:auto;font-size:12px;color:#d4d6d8;line-height:1.5;
             max-height:calc(100vh - 300px)}}
.notes-empty{{color:#56585a;font-style:italic;font-size:12px}}
.note-row{{padding:2px 0;border-bottom:1px solid #25282b}}
.note-added{{color:#6fcf97;font-size:11px;padding:3px 0}}
.note-day{{color:#75777a;font-size:10px;margin-right:7px}}
.notes-input{{display:flex;gap:6px;align-items:flex-end}}
#note-box{{flex:1;background:#1d1f22;color:#e8e9ea;border:1px solid #2a2c2f;border-radius:5px;
          padding:5px 7px;font-size:12px;font-family:inherit;resize:none}}
.notes-input button{{background:#2a2c2f;color:#e8e9ea;border:none;border-radius:5px;padding:5px 10px;
          font-size:11px;cursor:pointer}}
.notes-input button:hover{{background:#424446}}
.notes-input button.notes-clear{{background:transparent;color:#75777a}}
.notes-input button.notes-clear:hover{{color:#e8837a}}
.brief-label{{font-size:10px;color:#75777a;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}}
.brief-text{{font-size:15px;color:#a3a6a9;line-height:1.65}}
.tips-text{{color:#d4d6d8;font-size:15px;line-height:1.65}}
.brief-loading{{color:#56585a;font-style:italic}}
.stat-tooltip{{position:fixed;background:#2a2c2f;border:1px solid #424446;border-radius:7px;
               padding:8px 11px;font-size:12px;color:#a3a6a9;line-height:1.55;pointer-events:none;
               display:none;z-index:999;max-width:280px;
               box-shadow:0 4px 16px rgba(0,0,0,.5);white-space:pre-line}}
.footer{{padding:7px 18px;border-top:1px solid #2a2c2f;display:flex;justify-content:flex-end;flex-shrink:0}}
button{{background:#2a2c2f;color:#a3a6a9;border:1px solid #424446;border-radius:5px;
        padding:4px 14px;font-size:12px;cursor:pointer}}
button:hover{{background:#424446;color:#e8e9ea}}
#stale-warn {{ display:none; position:fixed; bottom:10px; right:10px; font-size:16px; cursor:default;
  z-index:9999; }}
#stale-warn .stale-tip {{ display:none; position:absolute; bottom:24px; right:0; background:#2a2c2f;
  border:1px solid #e3b95f; border-radius:6px; padding:6px 9px; font-size:11px; color:#e3b95f;
  white-space:nowrap; pointer-events:none; }}
#stale-warn:hover .stale-tip {{ display:block; }}
/* ── Performance tracker ─────────────────────────────────────────────────── */
.tracker-view {{ display:flex; flex-direction:column; height:auto;
  padding:18px 22px; gap:10px; }}
.tracker-top {{ display:flex; gap:12px; align-items:flex-start; }}
.tracker-radar-col {{ flex-shrink:0; display:flex; flex-direction:column; align-items:center; gap:4px; }}
.tracker-header {{ font-size:10px; color:#75777a; font-weight:600; letter-spacing:.05em;
  text-transform:uppercase; }}
.tracker-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(175px,1fr));
  gap:7px; flex:1; }}
.metric-card {{ background:#2a2c2f; border:1px solid #3a3e43; border-radius:7px;
  padding:9px 11px; }}
.mc-label {{ font-size:10px; color:#75777a; margin-bottom:2px; }}
.mc-val {{ font-size:20px; font-weight:700; line-height:1; }}
.mc-date {{ font-size:9px; color:#424446; margin-top:3px; }}
.mc-bar-wrap {{ height:3px; background:#1b1c1e; border-radius:2px; margin-top:5px; }}
.mc-bar-fill {{ height:100%; border-radius:2px; transition:width .6s ease; }}
.mc-spark {{ width:100%; height:24px; margin-top:4px; display:block; }}
.mc-notes {{ font-size:9px; color:#56585a; margin-top:2px; font-style:italic;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.mc-journey {{ font-size:9px; color:#56585a; display:flex; justify-content:space-between;
  margin-top:3px; }}
/* The blanket `canvas{{position:absolute;top:0;left:0}}` rule is meant for the
   chart canvases, which sit inside a position:relative .chart-wrap. The radar has
   no positioned ancestor, so it would anchor to the viewport and cover the header. */
#c-radar {{ display:block; position:static; }}
.log-form {{ display:flex; flex-wrap:wrap; gap:5px; align-items:center;
  padding:8px 10px; background:#2a2c2f; border-radius:7px; border:1px solid #424446; }}
.log-form select, .log-form input {{
  -webkit-appearance:none; appearance:none;
  background:#1b1c1e; border:1px solid #424446; border-radius:4px;
  color:#a3a6a9; font-size:11px; padding:3px 7px; outline:none; }}
.log-form select {{ width:170px; cursor:pointer; }}
.log-form select option {{ background:#1b1c1e; color:#a3a6a9; }}
.log-form input[type=number] {{ width:72px; }}
.log-form input[type=text]   {{ flex:1; min-width:80px; }}
.log-form select:focus, .log-form input:focus {{ border-color:#56585a; color:#e8e9ea; }}
.log-form .log-btn {{ background:#2c6a83; border-color:#34a2ce; color:#c7d2fe;
  font-size:11px; padding:3px 12px; }}
.log-form .log-btn:hover {{ background:#2d84a6; }}
.log-added {{ font-size:10px; color:#6fcf97; padding:2px 0; }}

/* ── Modular tracker: goals / key metrics / stored ─────────────────────────── */
.tracker-cols {{ flex:1; min-width:0; }}
.tier-head {{ display:flex; align-items:baseline; gap:10px; margin-bottom:7px; }}
.tier-hint {{ font-size:9.5px; color:#56585a; }}
.radar-hint {{ font-size:9px; color:#56585a; margin-top:2px; }}
.add-metric-btn {{ margin-left:auto; font-size:10.5px; padding:2px 9px;
  background:#2a2c2f; border:1px solid #424446; color:#a3a6a9; border-radius:5px; cursor:pointer; }}
.add-metric-btn:hover {{ background:#424446; color:#e8e9ea; }}

.goals-section {{ display:flex; flex-direction:column; gap:12px; margin-bottom:6px; }}
.goal-empty {{ font-size:11.5px; color:#56585a; background:#1f2123; border:1px dashed #3a3e43;
  border-radius:8px; padding:12px 14px; }}
.goal-card {{ background:#1f2123; border:1px solid #3a3e43; border-radius:9px; padding:12px 14px; }}
.goal-head {{ display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:8px; }}
.goal-label {{ font-size:13px; font-weight:600; color:#e8e9ea; }}
.goal-sub {{ font-size:10px; color:#75777a; margin-top:2px; font-variant-numeric:tabular-nums; }}
.goal-now {{ text-align:right; font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; }}
.goal-pct {{ display:block; font-size:10px; color:#75777a; font-weight:400; }}
.goal-wrap {{ position:relative; height:150px; }}
.tier-pill {{ margin-left:8px; font-size:8px; letter-spacing:.08em; text-transform:uppercase;
  color:#d3d5d7; background:#2b2d30; border:1px solid #424446; border-radius:4px; padding:2px 5px;
  vertical-align:2px; font-weight:600; }}

.mc-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:6px; }}
.mc-actions {{ display:flex; align-items:center; gap:3px; flex-shrink:0; }}
.mc-none {{ font-size:10.5px; color:#56585a; font-style:italic; margin:6px 0; }}
.mc-pb {{ display:flex; align-items:baseline; gap:6px; margin-top:5px; font-size:10px; }}
.pb-tag {{ color:#b79cf0; text-transform:uppercase; letter-spacing:.05em; font-size:8.5px;
  border:1px solid #424446; background:#2b2d30; border-radius:3px; padding:1px 4px; }}
.pb-val {{ color:#d3d5d7; font-weight:600; font-variant-numeric:tabular-nums; }}
.pb-date {{ color:#56585a; margin-left:auto; font-variant-numeric:tabular-nums; }}
.metric-card.stored {{ opacity:.62; }}
.metric-card.stored:hover {{ opacity:1; }}
.metric-card.empty {{ border-style:dashed; }}

.tier-btns {{ display:flex; gap:1px; }}
.tier-btn, .edit-btn {{ background:none; border:1px solid transparent; border-radius:4px;
  color:#56585a; font-size:11px; line-height:1; padding:2px 4px; cursor:pointer; }}
.tier-btn:hover, .edit-btn:hover {{ color:#a3a6a9; background:#1b1c1e; }}
.tier-btn.active {{ color:#88dcfd; border-color:#88dcfd; background:#1a3e4c; }}

.metric-form {{ margin-top:9px; padding:10px; background:#1b1c1e; border:1px solid #424446;
  border-radius:7px; display:flex; flex-wrap:wrap; gap:7px; }}
.metric-form label {{ display:flex; flex-direction:column; gap:2px; font-size:9px; color:#75777a;
  text-transform:uppercase; letter-spacing:.05em; }}
.metric-form label.chk {{ flex-direction:row; align-items:center; gap:5px; text-transform:none;
  letter-spacing:0; font-size:10.5px; color:#a3a6a9; }}
.metric-form input[type=text], .metric-form input[type=number], .metric-form select {{
  -webkit-appearance:none; appearance:none; background:#2a2c2f; border:1px solid #424446;
  border-radius:4px; color:#e8e9ea; font-size:11px; padding:4px 6px; width:104px; outline:none; }}
.metric-form input:focus, .metric-form select:focus {{ border-color:#88dcfd; }}
.metric-form-actions {{ display:flex; gap:6px; align-items:flex-end; width:100%; }}
.metric-form-actions button {{ font-size:11px; padding:3px 11px; }}
.metric-form-actions .danger {{ margin-left:auto; background:transparent; border-color:#7f1d1d;
  color:#e8837a; }}
.metric-form-actions .danger:hover {{ background:#7f1d1d; color:#fecaca; }}
.metric-err {{ margin-top:8px; font-size:11px; color:#e8837a; background:#2a1010;
  border:1px solid #7f1d1d; border-radius:6px; padding:7px 10px; }}

/* ══ Theme layer: flat surfaces, one accent, status-only colour ═══════════════ */
:root{{color-scheme:dark}}
::-webkit-scrollbar{{width:8px;height:8px}}
::-webkit-scrollbar-thumb{{background:#323436;border-radius:4px}}
::-webkit-scrollbar-track{{background:transparent}}

.header{{border-bottom:none;padding:18px 26px 4px;gap:14px}}
.header h1{{font-size:15px;font-weight:600;letter-spacing:-.01em}}
.header .date{{font-size:12px;color:#75777a}}
.tabs{{gap:22px;margin-left:26px}}
.tab{{border:none;border-radius:0;background:none;padding:6px 0;font-size:13px;color:#75777a;
      border-bottom:2px solid transparent}}
.tab:hover{{background:none;color:#d4d6d8}}
.tab[aria-selected="true"]{{background:none;color:#e8e9ea;border-bottom-color:#88dcfd}}

.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:14px 18px;border-bottom:none;padding:14px 26px 18px;align-items:start}}
.stat{{border-left:none;padding:0;min-width:0}}
.label{{font-size:10px;letter-spacing:.08em;margin-bottom:4px}}
.value{{font-size:17px;font-weight:600;overflow:hidden;text-overflow:ellipsis}}
.sub{{font-size:11px;-webkit-line-clamp:2}}
#illness-sub + div{{display:none}}
.metric-card.empty,.goal-empty{{border:1px dashed rgba(159,165,173,.28)!important;background:transparent!important}}
.pace-top{{display:flex;justify-content:space-between;font-size:13px;margin:2px 0 8px;color:#a3a6a9}}
.pace-top b{{color:#e8e9ea;font-size:15px}}
.pace-bar{{position:relative;height:34px;border-radius:9px;background:rgba(159,165,173,.1);overflow:visible}}
.pace-seg{{position:absolute;top:0;bottom:0;border-radius:6px;display:flex;cursor:default;overflow:hidden;transition:filter .12s}}
.pace-seg i{{display:block;height:100%}}
.pace-seg span{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10.5px;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.65);pointer-events:none}}
.pace-legend{{display:flex;gap:14px;align-items:center;font-size:11px;color:#a3a6a9;margin-top:6px}}
.pace-legend i{{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}}
.pace-seg:hover{{filter:brightness(1.25)}}
.pace-rest{{position:absolute;top:0;bottom:0;cursor:default}}
.pace-marker{{position:absolute;top:-5px;bottom:-5px;width:10px;margin-left:-5px;cursor:default;
  background:linear-gradient(90deg,transparent 4px,#e8e9ea 4px,#e8e9ea 6px,transparent 6px)}}
.pace-target{{position:absolute;top:0;bottom:0;width:0;border-right:2px dashed rgba(159,165,173,.5)}}
.pace-axis{{position:relative;height:18px;margin-top:6px;font-size:10.5px;color:#75777a}}
.pace-axis span{{position:absolute;transform:translateX(-50%);white-space:nowrap}}
.pace-axis span:first-child{{left:0;transform:none}}
.pace-axis span.r{{right:0;left:auto;transform:none}}
.pace-tip{{position:fixed;display:none;z-index:999;max-width:320px;background:#2a2c2f;border-radius:8px;padding:9px 12px;
  font-size:12px;line-height:1.55;color:#d4d6d8;pointer-events:none;box-shadow:0 6px 24px rgba(0,0,0,.45)}}
.pace-tip b{{color:#e8e9ea}}
.week-day-actions{{margin-top:6px}}
.week-day-actions button{{font-size:11px;padding:3px 10px}}
.flash{{animation:flashbg 1.6s ease-out}}
@keyframes flashbg{{0%{{background:rgba(139,219,250,.18)}}100%{{background:transparent}}}}
.stat-link:hover .value{{color:#e8e9ea}}

.nutr-card,.today-dials,.card,.goal-card,.metric-card,.ill-stat,.bulk-stat,.stat-tooltip{{
  background:rgba(159,165,173,.055);border:none;border-radius:12px}}
.bulk-stat,.ill-stat{{background:rgba(159,165,173,.05);border-radius:9px}}
.gap-card{{background:rgba(227,185,95,.07);border:none;border-radius:12px}}
.nutr-card,.today-dials,.card{{padding:16px 18px}}
.stat-tooltip{{background:#2a2c2f;box-shadow:0 6px 24px rgba(0,0,0,.4)}}
.brief-label,.chart-label,.nutr-card-h{{font-size:10.5px;font-weight:500;letter-spacing:.08em;color:#75777a}}
.today-dash,.nutr-dash{{gap:18px}}

input,select,textarea{{color-scheme:dark;background:rgba(159,165,173,.08);border:1px solid transparent;
  border-radius:8px;color:#e8e9ea;padding:6px 10px;font-family:inherit;font-size:13px;outline:none}}
input:focus,select:focus,textarea:focus{{border-color:#88dcfd}}
input::placeholder,textarea::placeholder{{color:#75777a}}
button{{background:rgba(159,165,173,.1);border:none;border-radius:8px;color:#d4d6d8;
  padding:6px 14px;font-family:inherit;font-size:12.5px;cursor:pointer}}
button:hover{{background:rgba(159,165,173,.18)}}
.food-input-row input,.food-input-row button,.plan-btns button,#plan-box,#note-box,
.food-entry-edit-form input,.food-entry-edit-form button,.metric-form,.metric-form input{{
  border:1px solid transparent;background:rgba(159,165,173,.08);border-radius:8px}}
.btn{{background:rgba(139,219,250,.16);color:#ccf1ff;border:none;border-radius:8px;padding:6px 14px;
  font-size:12.5px;cursor:pointer}}
.btn:hover{{background:rgba(139,219,250,.26)}}
#life-lists,#focus-root{{font-size:13.5px;line-height:1.5}}
.perf-toolbar{{display:flex;align-items:center;gap:8px;padding:2px 0 14px}}
.pm-overlay{{position:fixed;inset:0;background:rgba(10,11,12,.62);display:flex;align-items:center;justify-content:center;z-index:60}}
.pm-dialog{{width:min(480px,92vw);max-height:90vh;overflow-y:auto;background:#212325;border-radius:16px;padding:22px 24px;
  box-shadow:0 20px 60px rgba(0,0,0,.5)}}
.pm-title{{font-size:16px;font-weight:600;margin-bottom:14px}}
.pm-field{{display:flex;flex-direction:column;gap:5px;margin-bottom:12px;flex:1;min-width:0}}
.pm-field>label,.pm-field label:first-child{{font-size:11px;color:#a3a6a9;letter-spacing:.03em}}
.pm-field input[type=text],.pm-field input[type=number],.pm-field input[type=date],.pm-field select{{width:100%;padding:8px 10px;font-size:13px}}
.pm-row{{display:flex;gap:10px;align-items:flex-start}}
.pm-opt{{color:#75777a;font-weight:400;text-transform:none}}
.pm-hint{{font-size:11.5px;color:#75777a;min-height:16px}}
.pm-hint b{{color:#d4d6d8;font-weight:600}}
.pm-err{{color:#e8837a;font-size:12px;min-height:16px}}
.pm-actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:6px}}
.pm-more{{margin:2px 0 10px;font-size:12px;color:#a3a6a9}}
.pm-more summary{{cursor:pointer;margin-bottom:10px}}
.pm-dialog label.chk,.metric-form label.chk{{display:flex;flex-direction:row;align-items:center;gap:6px;font-size:12px;color:#a3a6a9;margin:4px 0}}
.sl-chip{{display:inline-block;padding:5px 11px;margin:0 6px 6px 0;border-radius:8px;background:rgba(159,165,173,.08);
  color:#a3a6a9;cursor:pointer;font-size:12.5px;user-select:none}}
.sl-chip:hover{{background:rgba(159,165,173,.16)}}
.sl-chip.on{{background:rgba(139,219,250,.22);color:#e8e9ea}}
.sl-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin-top:14px}}
.sl-tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:12px}}
.sl-tile .v{{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}}
.sl-tile .d{{font-size:11.5px;color:#75777a;margin-top:2px}}
#sleep-root{{font-size:13.5px}}
#life-lists{{margin-top:16px}}
#life-form{{gap:8px!important}}
{_SUS_CSS}
</style></head>
<body>
<div class="header">
  <h1>Training Dashboard</h1>
  <span class="date">{day_str}</span>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" aria-selected="true" data-panel="today">Today</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="trends">Trends</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="nutrition">Nutrition</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="perf">Performance</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="oya">Vs Last Year</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="focus">Focus</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="life">Life</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="sustain">Load</button>
  </div>
  <select id="appSwitch" onchange="openApp(this.value)" title="Open another app"><option value="">Apps</option><option value="journal">Paper Journal</option><option value="calendar">Calendar</option><option value="todo">Todo</option><option value="maths">Maths Panel</option><option value="revise">Revision Brief</option></select>
  <select id="periodSelect" onchange="setPeriod(this.value)">
    <option value="14">14 days</option>
    <option value="30">30 days</option>
    <option value="90" selected>90 days</option>
    <option value="180">6 months</option>
    <option value="365">1 year</option>
    <option value="730">2 years</option>
  </select>
</div>

<div class="stats">{kpi_html}</div>

<div class="tab-body">

  <section class="panel active" data-panel="today">
    <div class="today-dash">

      <div class="today-brief">
        <div class="brief-label">Overview</div>
        <div class="brief-text" id="brief-overview">{overview_html}</div>
        <div class="brief-label" style="margin-top:16px">Coach's Tips</div>
        <div class="tips-text" id="brief-tips">{tips_html}</div>
      </div>

      <div class="today-side">
        <div class="today-dials" id="today-dials"></div>
      </div>

      <div class="today-sleep">
        <div class="brief-label">Sleep Debt <span class="nutr-card-sub">EMA τ=5d vs 8h/night · <span style="color:#6fcf97">─</span> 9.5h <span style="color:#e3b95f">─</span> 8.5h <span style="color:#e8837a">─</span> trend</span></div>
        <div class="chart-wrap" id="w-slp"><canvas id="c-slp"></canvas></div>
      </div>

      <div class="today-sessions">{sessions_html}</div>

      <div class="today-notes">
        <div class="brief-label">Notes <span class="notes-hint">— context for the coach · persists until cleared</span></div>
        <div class="notes-list" id="notes-list">{notes_html}</div>
        <div class="notes-input">
          <textarea id="note-box" rows="2" placeholder="Add context, how you feel, or an upcoming session… (Ctrl+Enter to add)"></textarea>
          <button onclick="submitNote()">Add</button>
          <button class="notes-clear" onclick="clearNotes()">Clear all</button>
        </div>
      </div>

    </div>
  </section>

  <section class="panel" data-panel="trends">
    <div class="charts">
      <div class="chart-cell">
        <div class="chart-label">Fitness (CTL) · Fatigue (ATL)</div>
        <div class="chart-wrap" id="w-ctl"><canvas id="c-ctl"></canvas></div>
      </div>
      <div class="chart-cell">
        <div class="chart-label">Form (TSB) — zone coloured</div>
        <div class="chart-wrap" id="w-tsb"><canvas id="c-tsb"></canvas></div>
      </div>
      <div class="chart-cell">
        <div class="chart-label">HRV · Resting HR</div>
        <div class="chart-wrap" id="w-hrv"><canvas id="c-hrv"></canvas></div>
      </div>
      <div class="chart-cell">
        <div class="chart-label">Z4+ High-intensity Load</div>
        <div class="chart-wrap" id="w-hil"><canvas id="c-hil"></canvas></div>
      </div>
      <div class="chart-cell">
        <div class="chart-label">Calorie Intake vs Target (14 days){bulk_label_suffix}</div>
        <div class="chart-wrap" id="w-cal"><canvas id="c-cal"></canvas></div>
        <div id="w-cal-cum" style="height:16px;flex-shrink:0;position:relative;margin-top:3px" title="Cumulative balance vs pace target">
          <canvas id="c-cal-cum" style="position:absolute;top:0;left:0;width:100%;height:100%"></canvas>
        </div>
      </div>
      <div class="chart-cell">
        <div class="chart-label">Body Weight (kg)</div>
        <div class="chart-wrap" id="w-wt"><canvas id="c-wt"></canvas></div>
      </div>
    </div>
  </section>

  <section class="panel" data-panel="nutrition">
    <div class="nutr-dash">

      <div class="nutr-card pace-card" id="nutr-pace">
        <div class="nutr-card-h">Today&rsquo;s pace <span class="nutr-card-sub">each block is a meal &middot; hover for details</span></div>
        <div id="pace-bar-host"></div>
        <div class="pace-tip" id="pace-tip"></div>
      </div>

      <div class="nutr-card cal-ring-card" id="nutr-cal">
        <div class="nutr-card-h">Energy today</div>
        <div class="cal-ring-wrap"><canvas id="c-cal-ring"></canvas>
          <div class="cal-ring-mid" id="cal-ring-mid"></div>
        </div>
        <div class="cal-ring-legend" id="cal-ring-legend"></div>
      </div>

      <div class="nutr-card">
        <div class="nutr-card-h">Where the calories come from</div>
        <div class="compo-row">
          <div class="compo-pie-wrap"><canvas id="c-macro-pie"></canvas></div>
          <div class="compo-legend" id="macro-pie-legend"></div>
        </div>
      </div>

      <div class="nutr-card macro-break-card">
        <div class="nutr-card-h">Macro &amp; micro breakdown <span class="nutr-card-sub">vs target for the day</span></div>
        <div class="macro-break" id="macro-break"></div>
      </div>

      <div class="nutr-card bulk-card" id="nutr-bulk">
        <div class="nutr-card-h">Body composition goal</div>
        <div class="goal-adjuster">
          <div class="goal-readout">
            <span class="goal-kg" id="goal-kg">—</span>
            <span class="goal-kg-unit">kg/week</span>
            <span class="goal-kcal" id="goal-kcal"></span>
          </div>
          <input type="range" id="goal-slider" min="-750" max="750" step="50" value="0"
                 oninput="onGoalSlider(this.value)" onchange="commitGoal(this.value)">
          <div class="goal-scale"><span>cut</span><span>maintain</span><span>bulk</span></div>
        </div>
        <div class="bulk-stats" id="bulk-stats"></div>
      </div>

      <div class="nutr-card weight-card">
        <div class="nutr-card-h">Body weight <span class="nutr-card-sub" id="weight-sub"></span></div>
        <div class="weight-wrap" id="weight-wrap"><canvas id="c-nutr-weight"></canvas></div>
      </div>

      <div class="nutr-card gap-slot">
        <div class="gap-card" id="cal-gap-card" style="display:none">
          <div class="gap-head">
            <span class="nutr-card-h" style="margin:0">Catch up to pace</span>
            <span class="gap-amount" id="cal-gap-amount"></span>
          </div>
          <div class="gap-text" id="cal-gap-text"><span class="brief-loading">Sizing a snack…</span></div>
        </div>
        <div class="nutr-card-h">Nutrition coach</div>
        <div class="tips-text" id="brief-nutr"><span class="brief-loading">Generating…</span></div>
      </div>

      <div class="nutr-card foodlog-card">
        <div class="nutr-card-h">Logged today</div>
        <div id="food-log-body">{food_log_html}</div>
      </div>

      <div class="nutr-card weeklog-card">
        <div class="nutr-card-h">This week <span class="nutr-card-sub">tap a day to expand</span></div>
        <div id="weekly-log-body">{weekly_log_html}</div>
      </div>

      <div class="nutr-card insight-card" id="nutr-insight-card" style="display:none">
        <div class="nutr-card-h">Weekly nutrition analysis</div>
        <div class="tips-text" id="nutr-insight"></div>
      </div>

    </div>
  </section>

  <section class="panel" data-panel="perf">
    <div class="tracker-view" id="tracker-view">
      <div class="perf-toolbar">
        <button class="btn" onclick="openLogModal()">+ Log result</button>
        <button onclick="openAddMetric()">+ New metric</button>
        <span id="log-confirm" class="log-added"></span>
      </div>
      <div class="goals-section" id="goals-section"></div>

      <div class="tracker-top">
        <div class="tracker-radar-col">
          <div class="tracker-header">Profile</div>
          <canvas id="c-radar"></canvas>
          <div class="radar-hint">key metrics only</div>
        </div>
        <div class="tracker-cols">
          <div class="tier-head">
            <span class="tracker-header">Key metrics</span>
            <span class="tier-hint">★ on the profile · fed to the coach</span>
          </div>
          <div class="tracker-grid" id="tracker-cards" style="align-content:start"></div>

          <div class="tier-head" style="margin-top:14px">
            <span class="tracker-header">Stored</span>
            <span class="tier-hint">☆ tracked, off the profile</span>
            
          </div>
          <div class="tracker-grid" id="bank-cards" style="align-content:start"></div>
        </div>
      </div>

      <div class="metric-err" id="metric-err" style="display:none"></div>
    </div>
  </section>

  <div class="pm-overlay" id="pm-overlay" style="display:none" onclick="if(event.target===this)pmClose()">
    <div class="pm-dialog" id="pm-dialog"></div>
  </div>

  <section class="panel" data-panel="oya">
    {oya_html}
  </section>

  <section class="panel" data-panel="sleep">
    <div style="padding:22px 26px;max-width:1180px">
      <a href="#" class="back-link" onclick="selectTab('today');return false">&larr; Back</a>
      <div id="sleep-root" style="margin-top:10px"></div>
    </div>
  </section>

  <section class="panel" data-panel="life">
    <div style="padding:22px 26px;max-width:1040px">
      <div class="brief-label">Add</div>
      <div id="life-form" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0 4px">
        <select id="lf-type"><option value="todo">Todo</option><option value="event">Event</option></select>
        <input type="text" id="lf-title" placeholder="What?" style="flex:1;min-width:200px">
        <select id="lf-area"></select>
        <input type="date" id="lf-date" title="Due date (todo, optional) or date (event)">
        <input type="time" id="lf-start" title="Start (events)">
        <input type="time" id="lf-end" title="End (events)">
        <select id="lf-repeat"><option value="">no repeat</option><option value="daily">daily</option><option value="weekly">weekly</option></select>
        <button class="btn" id="lf-add">Add</button>
      </div>
      <div id="life-lists"></div>
    </div>
  </section>

  <section class="panel" data-panel="focus">
    <div id="focus-root" style="padding:22px 26px;max-width:900px"></div>
  </section>

  {_SUS_PANEL}

  <section class="panel" data-panel="illness">
    <div class="ill-panel">
      <div class="ill-head">
        <a href="#" class="back-link" onclick="selectTab('today');return false">&larr; Back</a>
        <div class="brief-label" style="margin:0">Illness Risk<span class="badge-exp">Experimental</span></div>
        <div class="ill-today" id="ill-today"></div>
      </div>

      <div class="ill-charts">
        <div class="chart-cell">
          <div class="chart-label">P(sick) posterior · full history
            <span class="swatch-x" style="color:#e8837a">─ risk</span>
            <span class="swatch-x" style="color:#56585a">┈ p = {sick_thresh_str} episode threshold</span>
          </div>
          <div class="chart-wrap" id="w-ill-risk"><canvas id="c-ill-risk"></canvas></div>
        </div>
        <div class="chart-cell">
          <div class="chart-label">Resting HR — the only signal the model reads
            <span class="swatch-x" style="color:#e3b95f">─ RHR</span>
            <span class="swatch-x" style="color:#e8837a">▮ detected episode</span>
          </div>
          <div class="chart-wrap" id="w-ill-rhr"><canvas id="c-ill-rhr"></canvas></div>
        </div>
      </div>

      <div class="ill-lower">
        <div>
          <div class="brief-label">Key statistics</div>
          <div class="ill-stats" id="ill-stats"></div>
          <div class="brief-label" style="margin-top:16px">Detected episodes</div>
          <div class="ill-episodes" id="ill-episodes"></div>
        </div>

        <div class="ill-about">
          <div class="brief-label">How it works</div>
          <p>A Gaussian process fits a slowly-varying <em>healthy baseline</em> for your resting
          heart rate, using training load (ATL) as a covariate so that a hard block does not read
          as illness. A two-state hidden Markov model then asks, for each day, whether the
          residual above that baseline is better explained by a <em>healthy</em> or a
          <em>sick</em> state. The two are fitted together: days the HMM calls sick are held out,
          the baseline is refit on the rest, and the loop repeats.</p>

          <p>Runs are cached. A warm start re-fits from yesterday's hyperparameters each morning;
          a full global search re-runs weekly.</p>

          <div class="brief-label" style="margin-top:14px">Known limitations</div>
          <p class="ill-warn">The model reads <em>resting HR only</em> — HRV and sleep never enter
          it, so a night of good recovery will not move this number on its own.</p>
          <p class="ill-warn">The sick state is a plain Gaussian, and EM lets it grow a wider
          spread than the healthy state. It therefore absorbs outliers in <em>both</em>
          directions: a resting HR well <em>below</em> your baseline can nudge risk upward, which
          is backwards. Constraining it naively suppresses real multi-week episodes, because the
          outer loop relies on that width to mask sick days before refitting the baseline. Fixing
          this properly needs a lengthscale floor on the GP as well, so the baseline cannot track
          an illness. Until then, treat small day-to-day moves as noise and watch the episodes.</p>
        </div>
      </div>
    </div>
  </section>


</div>
<div class="footer">
  <button id="refresh-btn" onclick="triggerRefresh()" style="margin-right:8px">Refresh</button>
  <button onclick="document.title='__close__'">Dismiss</button>
</div>
<div id="tooltip" style="position:fixed;background:#2a2c2f;border:1px solid #424446;
     border-radius:7px;padding:8px 12px;font-size:11px;pointer-events:none;
     display:none;z-index:999;min-width:130px;box-shadow:0 4px 12px rgba(0,0,0,.4)"></div>
<div class="stat-tooltip" id="stat-tip"></div>
<div id="stale-warn">⚠<span class="stale-tip">Advice may be outdated — daily API quota exceeded.<br>Will refresh automatically when quota resets.</span></div>
<script>
const DATA = {chart_data};
const CHART_META = {{}};
const OVERLAYS   = {{}};
const tooltip    = document.getElementById('tooltip');
let currentDays  = 90;

// ── Catmull-Rom smooth line ──────────────────────────────────────────────────
function drawSmooth(ctx, pts) {{
  if (pts.length < 2) return;
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 0; i < pts.length - 1; i++) {{
    const p0 = pts[Math.max(0,i-1)], p1 = pts[i], p2 = pts[i+1], p3 = pts[Math.min(pts.length-1,i+2)];
    ctx.bezierCurveTo(p1.x+(p2.x-p0.x)/6, p1.y+(p2.y-p0.y)/6,
                      p2.x-(p3.x-p1.x)/6, p2.y-(p3.y-p1.y)/6, p2.x, p2.y);
  }}
}}

function plotSeries(ctx, xOf, yOf, data, color, width, dashed) {{
  ctx.strokeStyle = color; ctx.lineWidth = width||1.6; ctx.lineJoin = 'round';
  if (dashed) ctx.setLineDash([5,4]);
  let seg = [];
  const flush = () => {{ if (seg.length>=2) {{ ctx.beginPath(); drawSmooth(ctx,seg); ctx.stroke(); }} seg=[]; }};
  for (let i=0;i<data.length;i++) {{ const v=data[i]; if(v==null){{flush();}}else{{seg.push({{x:xOf(i),y:yOf(v)}});}} }}
  flush(); ctx.setLineDash([]);
}}

function setupCanvas(cid, wid) {{
  const wrap=document.getElementById(wid), canvas=document.getElementById(cid);
  // Null when the canvas is absent or its panel is hidden (zero client size), so a
  // chart that now lives on a different tab can be in drawAll's list harmlessly.
  if (!wrap || !canvas) return null;
  const dpr=window.devicePixelRatio||1, W=wrap.clientWidth, H=wrap.clientHeight;
  if (!W || !H) return null;
  canvas.width=W*dpr; canvas.height=H*dpr;
  const ctx=canvas.getContext('2d'); ctx.scale(dpr,dpr);
  return {{ctx,W,H}};
}}

function chartAxes(ctx, W, H, lo, hi, ticks, PAD, dates) {{
  const span=hi-lo, cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const xOf=i=>PAD.left+(i/Math.max(dates.length-1,1))*cW;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  for (let t=0;t<=ticks;t++) {{
    const v=lo+span*t/ticks, y=yOf(v);
    ctx.strokeStyle='#2a2c2f'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(PAD.left,y); ctx.lineTo(PAD.left+cW,y); ctx.stroke();
    ctx.fillStyle='#56585a'; ctx.font='9px system-ui'; ctx.textAlign='right';
    ctx.fillText(Math.round(v),PAD.left-3,y+3);
  }}
  if (lo<0&&hi>0) {{
    const y0=yOf(0); ctx.strokeStyle='#56585a'; ctx.lineWidth=1;
    ctx.setLineDash([3,3]); ctx.beginPath(); ctx.moveTo(PAD.left,y0); ctx.lineTo(PAD.left+cW,y0); ctx.stroke(); ctx.setLineDash([]);
  }}
  const n=dates.length, lbls=[0,Math.floor(n*.25),Math.floor(n*.5),Math.floor(n*.75),n-1];
  ctx.fillStyle='#56585a'; ctx.font='9px system-ui'; ctx.textAlign='center';
  for (const i of lbls) {{
    if(i>=dates.length) continue;
    const d=new Date(dates[i]+'T00:00:00');
    ctx.fillText(d.toLocaleDateString('en-GB',{{day:'numeric',month:'short'}}),xOf(i),H-5);
  }}
  return {{xOf,yOf,cW,cH}};
}}

function sliceByDays(dates,...arrays) {{
  const cutoff=new Date(Date.now()-currentDays*86400000).toISOString().slice(0,10);
  let idx=dates.findIndex(d=>d>=cutoff); if(idx<0) idx=0;
  return [dates.slice(idx),...arrays.map(a=>a.slice(idx))];
}}

function drawTodayLine(ctx,x,PAD,H) {{
  ctx.strokeStyle='#424446'; ctx.lineWidth=1; ctx.setLineDash([2,3]);
  ctx.beginPath(); ctx.moveTo(x,PAD.top); ctx.lineTo(x,H-PAD.bottom); ctx.stroke(); ctx.setLineDash([]);
}}

function tsbZoneColor(v,a) {{
  const al=a||1;
  if(v===null) return `rgba(100,116,139,${{al}})`;
  if(v>10)  return `rgba(74,222,128,${{al}})`;
  if(v>0)   return `rgba(134,239,172,${{al}})`;
  if(v>-10) return `rgba(251,191,36,${{al}})`;
  if(v>-20) return `rgba(249,115,22,${{al}})`;
  return `rgba(239,68,68,${{al}})`;
}}

// ── Projection band helper ────────────────────────────────────────────────────
function drawProjBand(ctx, xOf, yOf, mid, sigma, fillStyle) {{
  if (!mid.length || !sigma.length) return;
  ctx.fillStyle = fillStyle;
  ctx.beginPath();
  ctx.moveTo(xOf(0), yOf(mid[0] + sigma[0]));
  for (let i=1; i<mid.length; i++) ctx.lineTo(xOf(i), yOf(mid[i]+sigma[i]));
  for (let i=mid.length-1; i>=0; i--) ctx.lineTo(xOf(i), yOf(mid[i]-sigma[i]));
  ctx.closePath(); ctx.fill();
}}

// ── CTL/ATL ──────────────────────────────────────────────────────────────────
function drawCtl() {{
  const _g=setupCanvas('c-ctl', 'w-ctl'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:6,right:10,bottom:22,left:36}};
  const [dates,ctl,atl]=sliceByDays(DATA.dates,DATA.ctl,DATA.atl);
  const sigCtl=DATA.projCtlSig, sigAtl=DATA.projAtlSig;
  const allVals=[...ctl,...atl,
    ...DATA.projCtl.map((v,i)=>v+sigCtl[i]), ...DATA.projCtl.map((v,i)=>v-sigCtl[i]),
    ...DATA.projAtl.map((v,i)=>v+sigAtl[i]), ...DATA.projAtl.map((v,i)=>v-sigAtl[i]),
  ].filter(v=>v!=null);
  if(!allVals.length) return;
  const lo=Math.min(...allVals)*0.95, hi=Math.max(...allVals)*1.05;
  const fullDates=[...dates,...DATA.projDates.filter(d=>d>dates[dates.length-1])];
  const {{yOf}}=chartAxes(ctx,W,H,lo,hi,4,PAD,fullDates);
  const cW=W-PAD.left-PAD.right, nHist=dates.length;
  const xOf=i=>PAD.left+(i/Math.max(fullDates.length-1,1))*cW;
  drawTodayLine(ctx,xOf(nHist-1),PAD,H);
  drawProjBand(ctx,i=>xOf(nHist-1+i),yOf,DATA.projCtl,sigCtl,'rgba(34,211,238,.18)');
  drawProjBand(ctx,i=>xOf(nHist-1+i),yOf,DATA.projAtl,sigAtl,'rgba(248,113,113,.18)');
  plotSeries(ctx,i=>xOf(i),yOf,ctl,'#b79cf0',1.8);
  plotSeries(ctx,i=>xOf(i),yOf,atl,'#e8837a',1.8);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.projCtl,'#b79cf0',1.2,true);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.projAtl,'#e8837a',1.2,true);
  const ctlTip=[...ctl,...DATA.projCtl.slice(1)], atlTip=[...atl,...DATA.projAtl.slice(1)];
  const ctlSigTip=[...ctl.map(()=>null),...sigCtl.slice(1)];
  const atlSigTip=[...atl.map(()=>null),...sigAtl.slice(1)];
  CHART_META['c-ctl']={{dates:fullDates,PAD,yOf,series:[
    {{label:'CTL',data:ctlTip,sigmaData:ctlSigTip,color:'#b79cf0',fmt:v=>Math.round(v)+''}},
    {{label:'ATL',data:atlTip,sigmaData:atlSigTip,color:'#e8837a',fmt:v=>Math.round(v)+''}},
  ]}};
}}

// ── TSB ───────────────────────────────────────────────────────────────────────
function drawTsb() {{
  const _g=setupCanvas('c-tsb', 'w-tsb'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:6,right:10,bottom:22,left:36}};
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const [dates,tsb]=sliceByDays(DATA.dates,DATA.tsb);
  const sigTsb=DATA.projTsbSig;
  const fullDates=[...dates,...DATA.projDates.filter(d=>d>dates[dates.length-1])];
  const allVals=[...tsb,
    ...DATA.projTsb.map((v,i)=>v+sigTsb[i]), ...DATA.projTsb.map((v,i)=>v-sigTsb[i]),
  ].filter(v=>v!=null);
  if(!allVals.length) return;
  const lo=Math.min(...allVals,-5)*1.1, hi=Math.max(...allVals,5)*1.1, span=hi-lo;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(fullDates.length-1,1))*cW;
  const zones=[[10,hi,'rgba(74,222,128,.08)'],[0,10,'rgba(134,239,172,.08)'],
               [-10,0,'rgba(251,191,36,.08)'],[-20,-10,'rgba(249,115,22,.08)'],[lo,-20,'rgba(239,68,68,.08)']];
  for(const[zlo,zhi,fill]of zones) {{
    if(zhi<=lo||zlo>=hi) continue;
    ctx.fillStyle=fill; ctx.fillRect(PAD.left,yOf(Math.min(zhi,hi)),cW,yOf(Math.max(zlo,lo))-yOf(Math.min(zhi,hi)));
  }}
  chartAxes(ctx,W,H,lo,hi,4,PAD,fullDates);
  const nHist=dates.length;
  drawTodayLine(ctx,xOf(nHist-1),PAD,H);
  for(let i=0;i<tsb.length-1;i++) {{
    const v0=tsb[i],v1=tsb[i+1]; if(v0==null||v1==null) continue;
    const p0={{x:xOf(Math.max(0,i-1)),y:yOf(tsb[Math.max(0,i-1)]??v0)}};
    const p1={{x:xOf(i),y:yOf(v0)}}, p2={{x:xOf(i+1),y:yOf(v1)}};
    const p3={{x:xOf(Math.min(tsb.length-1,i+2)),y:yOf(tsb[Math.min(tsb.length-1,i+2)]??v1)}};
    ctx.strokeStyle=tsbZoneColor((v0+v1)/2); ctx.lineWidth=2; ctx.lineJoin='round';
    ctx.beginPath(); ctx.moveTo(p1.x,p1.y);
    ctx.bezierCurveTo(p1.x+(p2.x-p0.x)/6,p1.y+(p2.y-p0.y)/6,p2.x-(p3.x-p1.x)/6,p2.y-(p3.y-p1.y)/6,p2.x,p2.y);
    ctx.stroke();
  }}
  drawProjBand(ctx,i=>xOf(nHist-1+i),yOf,DATA.projTsb,sigTsb,'rgba(159,165,173,.22)');
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.projTsb,'#a3a6a9',1.2,true);
  for(const s of DATA.planSessions) {{
    const di=fullDates.indexOf(s.date); if(di<0) continue;
    ctx.fillStyle='#e3b95f'; ctx.beginPath(); ctx.arc(xOf(di),PAD.top+6,3,0,Math.PI*2); ctx.fill();
  }}
  const tsbTip=[...tsb,...DATA.projTsb.slice(1)];
  const tsbSigTip=[...tsb.map(()=>null),...sigTsb.slice(1)];
  CHART_META['c-tsb']={{dates:fullDates,PAD,yOf,series:[
    {{label:'Form',data:tsbTip,sigmaData:tsbSigTip,colorFn:tsbZoneColor,fmt:v=>(v>=0?'+':'')+v.toFixed(1)}},
  ]}};
}}

// ── HRV ───────────────────────────────────────────────────────────────────────
function drawHrv() {{
  const _g=setupCanvas('c-hrv', 'w-hrv'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:6,right:10,bottom:22,left:36}};
  const [dates,hrv,rhr]=sliceByDays(DATA.dates,DATA.hrv,DATA.rhr);
  const allVals=[...hrv,...rhr].filter(v=>v!=null); if(!allVals.length) return;
  const lo=Math.min(...allVals)*0.95, hi=Math.max(...allVals)*1.05, span=hi-lo;
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(dates.length-1,1))*cW;
  // Shade illness episodes before drawing data lines
  const bands = DATA.illnessBands || [];
  if (bands.length) {{
    const cutoff = dates[0];
    bands.forEach(function(b) {{
      const s = b.start > cutoff ? b.start : cutoff;
      const e = b.end   < dates[dates.length-1] ? b.end : dates[dates.length-1];
      const si = dates.findIndex(d=>d>=s);
      let ei   = dates.findIndex(d=>d>e); if(ei<0) ei=dates.length-1;
      if(si<0||si>ei) return;
      ctx.fillStyle='rgba(239,68,68,0.13)';
      ctx.fillRect(xOf(si), PAD.top, xOf(ei)-xOf(si), cH);
    }});
  }}
  chartAxes(ctx,W,H,lo,hi,4,PAD,dates);
  plotSeries(ctx,xOf,yOf,hrv,'#88dcfd',1.8);
  plotSeries(ctx,xOf,yOf,rhr,'#e3b95f',1.6);
  CHART_META['c-hrv']={{dates,PAD,yOf,series:[
    {{label:'HRV',data:hrv,color:'#88dcfd',fmt:v=>Math.round(v)+''}},
    {{label:'RHR',data:rhr,color:'#e3b95f',fmt:v=>Math.round(v)+' bpm'}},
  ]}};
}}

// ── Z4+ ───────────────────────────────────────────────────────────────────────
function drawHil() {{
  const _g=setupCanvas('c-hil', 'w-hil'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:6,right:10,bottom:22,left:36}};
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const cutoff=new Date(Date.now()-currentDays*86400000).toISOString().slice(0,10);
  let idx=DATA.hilDates.findIndex(d=>d>=cutoff); if(idx<0) idx=0;
  const dates=DATA.hilDates.slice(idx), hil=DATA.hil.slice(idx);
  const lastHistDate=dates[dates.length-1]||'';
  const projExtra=DATA.projHilDates.filter(d=>d>lastHistDate);
  const projExtraHil=DATA.projHil.slice(DATA.projHilDates.indexOf(projExtra[0]));
  const fullDates=[...dates,...projExtra];
  const allVals=[...hil,...DATA.projHil].filter(v=>v!=null); if(!allVals.length) return;
  const lo=0, hi=Math.max(...allVals)*1.15, span=hi-lo;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(fullDates.length-1,1))*cW;
  chartAxes(ctx,W,H,lo,hi,4,PAD,fullDates);
  const nHist=dates.length;
  drawTodayLine(ctx,xOf(nHist-1),PAD,H);
  plotSeries(ctx,i=>xOf(i),yOf,hil,'#88dcfd',1.8);
  if(projExtra.length) {{
    const anchor=[hil[hil.length-1],...projExtraHil];
    plotSeries(ctx,i=>xOf(nHist-1+i),yOf,anchor,'#88dcfd',1.2,true);
  }}
  const hilTip=[...hil,...projExtraHil];
  CHART_META['c-hil']={{dates:fullDates,PAD,yOf,series:[
    {{label:'Z4+ load',data:hilTip,color:'#88dcfd',fmt:v=>v.toFixed(1)+' min'}},
  ]}};
}}

// ── Sleep ─────────────────────────────────────────────────────────────────────
function drawSleep() {{
  const _g=setupCanvas('c-slp', 'w-slp'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:4,right:10,bottom:22,left:36}};
  const cutoff=new Date(Date.now()-currentDays*86400000).toISOString().slice(0,10);
  let idx=DATA.sleepDates.findIndex(d=>d>=cutoff); if(idx<0) idx=0;
  const dates=DATA.sleepDates.slice(idx), bal=DATA.sleepBalance.slice(idx);

  // Merge historical + projection date axis
  const projDates=DATA.slpProjDates.filter(d=>d>dates[dates.length-1]);
  const fullDates=[...dates,...projDates];

  const allVals=[...bal,...DATA.slpProjBest,...DATA.slpProjMed,...DATA.slpProjTrend]
    .filter(v=>v!=null);
  if(!allVals.length) return;
  const lo=Math.min(...allVals,-0.5)*1.15, hi=Math.max(...allVals,0.3)*1.15, span=hi-lo;
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(fullDates.length-1,1))*cW;
  const nHist=dates.length;

  chartAxes(ctx,W,H,lo,hi,3,PAD,fullDates);
  drawTodayLine(ctx,xOf(nHist-1),PAD,H);

  // Historical fill
  for(let i=0;i<bal.length-1;i++) {{
    const v0=bal[i],v1=bal[i+1]; if(v0==null||v1==null) continue;
    ctx.fillStyle=((v0+v1)/2>=0)?'rgba(74,222,128,.18)':'rgba(239,68,68,.18)';
    ctx.beginPath();
    ctx.moveTo(xOf(i),yOf(0)); ctx.lineTo(xOf(i),yOf(v0)); ctx.lineTo(xOf(i+1),yOf(v1)); ctx.lineTo(xOf(i+1),yOf(0));
    ctx.closePath(); ctx.fill();
  }}

  // Historical line
  const pts=bal.map((v,i)=>v!=null?{{x:xOf(i),y:yOf(v)}}:null).filter(Boolean);
  ctx.strokeStyle='#d3d5d7'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); drawSmooth(ctx,pts); ctx.stroke();

  // Projection lines (dashed), anchored at today (nHist-1)
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.slpProjBest, '#6fcf97',1.2,true);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.slpProjMed,  '#e3b95f',1.2,true);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.slpProjTrend,'#e8837a',1.2,true);

  // Tooltip data: history + best-case projection for hover
  const balTip=[...bal,...DATA.slpProjBest.slice(1)];
  const medTip=[...bal.map(()=>null),...DATA.slpProjMed.slice(1)];
  const trendTip=[...bal.map(()=>null),...DATA.slpProjTrend.slice(1)];
  CHART_META['c-slp']={{dates:fullDates,PAD,yOf,series:[
    {{label:'Sleep debt',  data:balTip,   color:'#d3d5d7',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
    {{label:'Best (9.5h)', data:[...bal.map(()=>null),...DATA.slpProjBest.slice(1)],  color:'#6fcf97',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
    {{label:'Med (8.5h)',  data:medTip,   color:'#e3b95f',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
    {{label:'Trend (' + DATA.slpAvg + 'h)',data:trendTip,color:'#e8837a',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
  ]}};
}}

// ── Hover system ──────────────────────────────────────────────────────────────
function addHoverToChart(cid, wid) {{
  if(OVERLAYS[cid]) return;
  const wrap=document.getElementById(wid);
  const ov=document.createElement('canvas');
  ov.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%;cursor:crosshair;z-index:10';
  wrap.appendChild(ov); OVERLAYS[cid]=ov;

  ov.addEventListener('mousemove',e=>{{
    const meta=CHART_META[cid]; if(!meta) return;
    const {{dates,PAD,yOf,series}}=meta;
    const rect=ov.getBoundingClientRect();
    const W=rect.width, H=rect.height, mouseX=e.clientX-rect.left;
    const cW=W-PAD.left-PAD.right, n=dates.length;
    let idx=Math.round((mouseX-PAD.left)/cW*(n-1));
    idx=Math.max(0,Math.min(n-1,idx));
    const dpr=window.devicePixelRatio||1;
    ov.width=W*dpr; ov.height=H*dpr;
    const ctx=ov.getContext('2d'); ctx.scale(dpr,dpr);
    const xPos=PAD.left+(idx/Math.max(n-1,1))*cW;
    ctx.strokeStyle='rgba(159,165,173,.4)'; ctx.lineWidth=1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(xPos,PAD.top); ctx.lineTo(xPos,H-PAD.bottom); ctx.stroke(); ctx.setLineDash([]);
    for(const s of series) {{
      const v=s.data[idx]; if(v==null) continue;
      const yPos=yOf(v), col=s.colorFn?s.colorFn(v):s.color;
      ctx.fillStyle=col; ctx.beginPath(); ctx.arc(xPos,yPos,4,0,Math.PI*2); ctx.fill();
      ctx.strokeStyle='#1b1c1e'; ctx.lineWidth=1.5; ctx.stroke();
    }}
    const d=new Date(dates[idx]+'T00:00:00');
    const dStr=d.toLocaleDateString('en-GB',{{weekday:'short',day:'numeric',month:'short'}});
    const isProj=dates[idx]>new Date().toISOString().slice(0,10);
    let html=`<div style="color:#75777a;margin-bottom:5px;font-size:10px">${{dStr}}${{isProj?' <span style="color:#e3b95f">(proj)</span>':''}}</div>`;
    for(const s of series) {{
      const v=s.data[idx]; if(v==null) continue;
      const col=s.colorFn?s.colorFn(v):s.color;
      const sigV=s.sigmaData?s.sigmaData[idx]:null;
      html+=`<div style="display:flex;justify-content:space-between;gap:14px;margin-bottom:1px">
        <span style="color:${{col}}">${{s.label}}</span>
        <span style="color:#e8e9ea;font-weight:600">${{s.fmt?s.fmt(v):v.toFixed(1)}}${{sigV!=null?' <span style="color:#75777a;font-size:10px;font-weight:400">\xb1'+sigV.toFixed(1)+'</span>':''}}</span></div>`;
    }}
    // Calorie chart, today's bar: how far in the red vs today's full-day target,
    // plus a precomputed (cached, haiku) meal suggestion sized to close the gap.
    if(cid==='c-cal' && meta.todayIdx!=null && idx===meta.todayIdx) {{
      const consumedS=series.find(s=>s.label==='Consumed'), targetS=series.find(s=>s.label==='Target');
      const c=consumedS&&consumedS.data[idx], t=targetS&&targetS.data[idx];
      if(c!=null && t!=null && t-c>50) {{
        html+=`<div style="margin-top:6px;padding-top:6px;border-top:1px solid #424446;color:#e8837a">
          ${{Math.round(t-c)}} kcal behind today's target</div>`;
        if(DATA.calGapSuggestion) {{
          html+=`<div style="margin-top:4px;font-size:10px;color:#a3a6a9;max-width:220px;white-space:normal">${{DATA.calGapSuggestion}}</div>`;
        }}
      }}
    }}
    tooltip.innerHTML=html; tooltip.style.display='block';
    let tx=e.clientX+16; if(tx+160>window.innerWidth) tx=e.clientX-175;
    tooltip.style.left=tx+'px'; tooltip.style.top=Math.max(8,e.clientY-20)+'px';
  }});

  ov.addEventListener('mouseleave',()=>{{
    const dpr=window.devicePixelRatio||1;
    ov.width=ov.clientWidth*dpr; ov.height=ov.clientHeight*dpr;
    ov.getContext('2d').clearRect(0,0,ov.width,ov.height);
    tooltip.style.display='none';
  }});
}}

function drawCalHistory() {{
  const _g=setupCanvas('c-cal', 'w-cal'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:4,right:46,bottom:22,left:40}};
  const dates=DATA.calHistDates, consumed=DATA.calHistConsumed, target=DATA.calHistTarget;
  // paceTarget matches target for every complete day; today (if incomplete) is swapped
  // to the pace-adjusted expected-so-far value, so today's bar/cumulative balance isn't
  // judged against a full-day total that hasn't finished accruing yet.
  const paceTarget=DATA.calHistPaceTarget||target;
  if(!dates||!dates.length) return;
  const n=dates.length;
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;

  // Primary axis: kcal consumed vs target. Must also include paceTarget — early in the
  // day it can sit well below every full-day target/consumed value, and leaving it out
  // of the range meant the pace marker got clipped to the bottom of the chart.
  const vals=consumed.filter(v=>v!=null).concat(target).concat(paceTarget);
  const lo=Math.min(...vals)*0.85, hi=Math.max(...vals)*1.08, span=hi-lo;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(n-1,1))*cW;

  // Cumulative balance, judged against pace target not full-day target. Rendered as its
  // own thin strip below the main chart (drawCalCumStrip) rather than overlaid here —
  // sharing the kcal axis made the fill/line badly distort whenever cumulative balance
  // and daily kcal were on very different scales.
  const cumBal=[]; let running=0;
  for(let i=0;i<n;i++) {{
    if(consumed[i]!=null) running+=consumed[i]-paceTarget[i];
    cumBal.push(consumed[i]!=null?running:null);
  }}

  // Bars (surplus/deficit per day, judged against pace target)
  const barW=Math.max(2,(cW/n)*0.6);
  for(let i=0;i<n;i++) {{
    const v=consumed[i]; if(v==null) continue;
    const x=xOf(i);
    ctx.fillStyle=v>=paceTarget[i]?'rgba(34,197,94,.55)':'rgba(239,68,68,.55)';
    const yTop=Math.min(yOf(v),yOf(paceTarget[i])), yBot=Math.max(yOf(v),yOf(paceTarget[i]));
    ctx.fillRect(x-barW/2,yTop,barW,Math.max(yBot-yTop,1));
  }}
  // Target line (full-day target — reference only, today's bar is judged against pace above)
  ctx.strokeStyle='rgba(159,165,173,.4)'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
  ctx.beginPath();
  target.forEach((v,i)=>i===0?ctx.moveTo(xOf(i),yOf(v)):ctx.lineTo(xOf(i),yOf(v)));
  ctx.stroke(); ctx.setLineDash([]);
  // Pace marker on today's bar (if today is still incomplete)
  for(let i=0;i<n;i++) {{
    if(paceTarget[i]===target[i]) continue;
    const x=xOf(i), y=yOf(paceTarget[i]);
    ctx.strokeStyle='#e8e9ea'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(x-barW/2-2,y); ctx.lineTo(x+barW/2+2,y); ctx.stroke();
  }}
  // Consumed line
  ctx.strokeStyle='#e8e9ea'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); let s2=false;
  for(let i=0;i<n;i++) {{ if(consumed[i]==null) continue; s2?ctx.lineTo(xOf(i),yOf(consumed[i])):ctx.moveTo(xOf(i),yOf(consumed[i])); s2=true; }}
  ctx.stroke();

  chartAxes(ctx,W,H,lo,hi,3,PAD,dates);

  // Build weight lookup by date for hover
  const wtMap={{}};
  if(DATA.weightDates) DATA.weightDates.forEach((d,i)=>wtMap[d]=DATA.weightVals[i]);
  const weightByDate=dates.map(d=>wtMap[d]??null);

  CHART_META['c-cal']={{dates,PAD,yOf,todayIdx:DATA.calTodayIdx,series:[
    {{label:'Consumed',data:consumed,color:'#e8e9ea',fmt:v=>Math.round(v)+' kcal'}},
    {{label:'Target',data:target,color:'#75777a',fmt:v=>Math.round(v)+' kcal'}},
    ...(paceTarget.some((v,i)=>v!==target[i]) ? [{{label:'Pace target (today)',data:paceTarget.map((v,i)=>v!==target[i]?v:null),color:'#a3a6a9',fmt:v=>Math.round(v)+' kcal'}}] : []),
    {{label:'Weight',data:weightByDate,color:'#88dcfd',fmt:v=>v!=null?v.toFixed(1)+' kg':'—'}},
  ]}};

  drawCalCumStrip(dates, cumBal, PAD);
}}

function drawCalCumStrip(dates, cumBal, mainPAD) {{
  const canvas=document.getElementById('c-cal-cum');
  if(!canvas) return;
  const dpr=window.devicePixelRatio||1;
  const wrap=canvas.parentElement;
  const W=wrap.clientWidth, H=wrap.clientHeight;
  if(!W||!H) return;
  canvas.width=W*dpr; canvas.height=H*dpr;
  const ctx=canvas.getContext('2d'); ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);

  const n=dates.length;
  // Share the main chart's left/right padding so each day's strip cell lines up with its bar above.
  const cW=W-mainPAD.left-mainPAD.right;
  const xOf=i=>mainPAD.left+(i/Math.max(n-1,1))*cW;
  const vals=cumBal.filter(v=>v!=null);
  if(!vals.length) return;
  const maxAbs=Math.max(...vals.map(Math.abs), 1);
  const midY=H/2, halfH=H/2-1;
  const barW=Math.max(2,(cW/n)*0.6);

  ctx.strokeStyle='rgba(159,165,173,.25)'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(mainPAD.left,midY); ctx.lineTo(W-mainPAD.right,midY); ctx.stroke();

  for(let i=0;i<n;i++) {{
    const v=cumBal[i]; if(v==null) continue;
    const x=xOf(i), h=Math.abs(v)/maxAbs*halfH;
    ctx.fillStyle=v>=0?'rgba(34,197,94,.65)':'rgba(239,68,68,.65)';
    if(v>=0) ctx.fillRect(x-barW/2, midY-h, barW, h);
    else     ctx.fillRect(x-barW/2, midY, barW, h);
  }}
}}

// ── GP regression (RBF kernel) for the weight chart ──────────────────────────
// Weigh-ins are sparse and irregularly spaced, so a spline/polyline through
// raw points is noisy and visually misleading. A GP posterior mean + 95% CI
// gives a principled trend line whose uncertainty band narrows near
// measurements and widens over unmeasured stretches.
function dayNum(d) {{ return Math.floor(new Date(d+'T00:00:00').getTime()/86400000); }}

// `kernels` is a list of {{l: lengthscale, sf: amplitude}} RBF components,
// summed. A single short lengthscale reverts to the flat prior mean within
// a few lengthscales of any data — a long-lengthscale component carries the
// slow trend across gaps instead of collapsing to a flat line, while a
// short-lengthscale component still lets the fit snap to local measurements.
function gpPredict(xTrain, yTrain, xQuery, kernels, sigmaN) {{
  const n = xTrain.length;
  const yMean = yTrain.reduce((a,b)=>a+b,0)/n;
  const yC = yTrain.map(y=>y-yMean);
  const kern = (a,b)=>{{
    const d=a-b;
    return kernels.reduce((s,{{l,sf}})=>s+sf*sf*Math.exp(-(d*d)/(2*l*l)),0);
  }};
  const priorVar = kernels.reduce((s,{{sf}})=>s+sf*sf,0);
  // Covariance matrix + measurement-noise jitter on the diagonal.
  const K = Array.from({{length:n}},(_,i)=>Array.from({{length:n}},(_,j)=>kern(xTrain[i],xTrain[j])+(i===j?sigmaN*sigmaN:0)));
  // Cholesky decomposition K = Lc Lc^T.
  const Lc = Array.from({{length:n}},()=>new Array(n).fill(0));
  for (let i=0;i<n;i++) {{
    for (let j=0;j<=i;j++) {{
      let sum = K[i][j];
      for (let k=0;k<j;k++) sum -= Lc[i][k]*Lc[j][k];
      Lc[i][j] = (i===j) ? Math.sqrt(Math.max(sum,1e-10)) : sum/Lc[j][j];
    }}
  }}
  // Solve Lc z = yC, then Lc^T alpha = z.
  const z = new Array(n);
  for (let i=0;i<n;i++) {{ let sum=yC[i]; for (let k=0;k<i;k++) sum-=Lc[i][k]*z[k]; z[i]=sum/Lc[i][i]; }}
  const alpha = new Array(n);
  for (let i=n-1;i>=0;i--) {{ let sum=z[i]; for (let k=i+1;k<n;k++) sum-=Lc[k][i]*alpha[k]; alpha[i]=sum/Lc[i][i]; }}
  const mean = new Array(xQuery.length), std = new Array(xQuery.length);
  for (let q=0;q<xQuery.length;q++) {{
    const kStar = xTrain.map(x=>kern(xQuery[q],x));
    mean[q] = yMean + kStar.reduce((s,k,i)=>s+k*alpha[i],0);
    const v = new Array(n);
    for (let i=0;i<n;i++) {{ let sum=kStar[i]; for (let k=0;k<i;k++) sum-=Lc[i][k]*v[k]; v[i]=sum/Lc[i][i]; }}
    const vtv = v.reduce((s,x)=>s+x*x,0);
    std[q] = Math.sqrt(Math.max(priorVar - vtv, 1e-6));
  }}
  return {{mean, std}};
}}

function drawWeight() {{
  const _g=setupCanvas('c-wt', 'w-wt'); if(!_g) return; const {{ctx,W,H}}=_g;
  const PAD={{top:4,right:10,bottom:22,left:36}};
  if(!DATA.weightDates||!DATA.weightDates.length) {{
    ctx.fillStyle='#424446'; ctx.font='11px sans-serif'; ctx.textAlign='center';
    ctx.fillText('No weight data logged',W/2,H/2); return;
  }}
  // Aligned to the full calendar-day axis (DATA.dates) so the fit is
  // evaluated on a uniform grid regardless of how sparse logging was.
  const [sDates,sValsFull]=sliceByDays(DATA.dates,DATA.weightValsFull);
  const logged=sValsFull.filter(v=>v!=null);
  if(!logged.length) return;
  // Fit against the *full* logged history (not just the visible window) so
  // the trend near the edges of the chart isn't starved of context, then
  // evaluate the posterior only over the visible dense day-grid.
  const xTrain = DATA.weightDates.map(dayNum);
  const yTrain = DATA.weightVals;
  const xQuery = sDates.map(dayNum);
  const yStd = Math.sqrt(yTrain.reduce((s,v)=>{{const d=v-yTrain.reduce((a,b)=>a+b,0)/yTrain.length; return s+d*d;}},0)/yTrain.length);
  const ampl = Math.max(yStd,0.5);
  const kernels = [
    {{l:60, sf:ampl*0.9}},  // slow trend — carries the shape across gaps instead of reverting to the flat mean
    {{l:6,  sf:ampl*0.45}}, // local wiggle — lets the fit snap to nearby measurements
  ];
  const {{mean,std}} = gpPredict(xTrain, yTrain, xQuery, kernels, /*sigmaN*/0.15);
  const bandHi = mean.map((m,i)=>m+1.96*std[i]), bandLo = mean.map((m,i)=>m-1.96*std[i]);
  const lo=Math.min(...bandLo,...logged)*0.997, hi=Math.max(...bandHi,...logged)*1.003, span=hi-lo||1;
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(sDates.length-1,1))*cW;
  chartAxes(ctx,W,H,lo,hi,3,PAD,sDates);
  // 95% CI band
  ctx.fillStyle='rgba(139,219,250,.15)';
  ctx.beginPath();
  bandHi.forEach((v,i)=>i===0?ctx.moveTo(xOf(i),yOf(v)):ctx.lineTo(xOf(i),yOf(v)));
  for (let i=bandLo.length-1;i>=0;i--) ctx.lineTo(xOf(i),yOf(bandLo[i]));
  ctx.closePath(); ctx.fill();
  // GP mean — smooth is safe here since xQuery is an evenly-spaced daily grid.
  const meanPts = mean.map((m,i)=>({{x:xOf(i),y:yOf(m)}}));
  ctx.strokeStyle='#88dcfd'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); drawSmooth(ctx,meanPts); ctx.stroke();
  // Scatter of actual measurements
  sValsFull.forEach((v,i)=>{{
    if(v==null) return;
    ctx.fillStyle='#b3e9ff'; ctx.strokeStyle='#1b1c1e'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.arc(xOf(i),yOf(v),2.5,0,Math.PI*2); ctx.fill(); ctx.stroke();
  }});
  CHART_META['c-wt']={{dates:sDates,PAD,yOf,series:[
    {{label:'Weight (fit)',data:mean,color:'#88dcfd',fmt:v=>v.toFixed(1)+' kg'}},
    {{label:'Logged',data:sValsFull,color:'#b3e9ff',fmt:v=>v.toFixed(1)+' kg'}},
  ]}};
}}

function drawIllnessSpark() {{
  var canvas = document.getElementById('illness-spark');
  if (!canvas) return;
  var dates = DATA.illness7dDates || [];
  var vals  = DATA.illness7dVals  || [];
  if (!dates.length) return;
  var dpr = window.devicePixelRatio || 1;
  var wrap = canvas.parentElement;
  var W = (wrap || canvas).clientWidth, H = (wrap || canvas).clientHeight;
  if (!W || !H) return;
  canvas.width = W * dpr; canvas.height = H * dpr;
  var ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  var PAD = {{l:3, r:3, t:3, b:3}};
  var cW = W - PAD.l - PAD.r, cH = H - PAD.t - PAD.b;
  var n = vals.length;
  var xOf = function(i) {{ return PAD.l + (i / Math.max(n - 1, 1)) * cW; }};
  // Auto-scale to the week's actual range — illness risk usually sits well
  // under 1.0, so a fixed 0-1 scale flattens real day-to-day variation.
  var dataLo = Math.min.apply(null, vals), dataHi = Math.max.apply(null, vals);
  var lo = Math.max(0, Math.min(dataLo - 0.03, 0.05));
  var hi = Math.min(1, Math.max(dataHi + 0.03, lo + 0.1));
  var span = hi - lo;
  var yOf = function(v) {{ return PAD.t + (1 - (Math.min(Math.max(v, lo), hi) - lo) / span) * cH; }};
  // 0.5 threshold line (only if it falls within the visible range)
  if (0.5 >= lo && 0.5 <= hi) {{
    ctx.strokeStyle = 'rgba(239,68,68,0.25)'; ctx.lineWidth = 0.5; ctx.setLineDash([2,2]);
    ctx.beginPath(); ctx.moveTo(PAD.l, yOf(0.5)); ctx.lineTo(W - PAD.r, yOf(0.5)); ctx.stroke(); ctx.setLineDash([]);
  }}
  // Fill under curve
  ctx.fillStyle = 'rgba(239,68,68,0.10)';
  ctx.beginPath(); ctx.moveTo(xOf(0), H - PAD.b);
  for (var i = 0; i < n; i++) ctx.lineTo(xOf(i), yOf(vals[i]));
  ctx.lineTo(xOf(n - 1), H - PAD.b); ctx.closePath(); ctx.fill();
  // Line
  ctx.strokeStyle = '#e8837a'; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
  ctx.beginPath();
  for (var i = 0; i < n; i++) {{ i === 0 ? ctx.moveTo(xOf(i), yOf(vals[i])) : ctx.lineTo(xOf(i), yOf(vals[i])); }}
  ctx.stroke();
  // Day dots
  for (var i = 0; i < n; i++) {{
    var v = vals[i];
    ctx.fillStyle = v < 0.20 ? '#6fcf97' : v < 0.50 ? '#e3b95f' : '#e8837a';
    ctx.beginPath(); ctx.arc(xOf(i), yOf(v), 2, 0, Math.PI * 2); ctx.fill();
  }}
  // Day-of-week labels (Mon…Sun)
  var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  ctx.fillStyle = '#424446'; ctx.font = '7px system-ui'; ctx.textAlign = 'center';
  for (var i = 0; i < n; i++) {{
    var d = new Date(dates[i] + 'T00:00:00');
    ctx.fillText(days[d.getDay()], xOf(i), H - 0.5);
  }}
}}

function drawAll() {{
  // Sleep moved to the Today tab; it is drawn from that tab's handler.
  drawCtl(); drawTsb(); drawHrv(); drawHil(); drawCalHistory(); drawWeight();
  drawIllnessSpark();
  ['c-ctl','c-tsb','c-hrv','c-hil','c-cal','c-wt'].forEach(function(id) {{
    addHoverToChart(id, id.replace('c-','w-'));
  }});
}}

function setPeriod(days) {{ currentDays=parseInt(days); drawAll(); }}
function setCoachBrief(overview, tips) {{
  var o = document.getElementById('brief-overview');
  var t = document.getElementById('brief-tips');
  if (o) o.innerHTML = overview;
  if (t) t.innerHTML = tips;
}}
function setNutritionInsight(text) {{
  var card = document.getElementById('nutr-insight-card');
  var body = document.getElementById('nutr-insight');
  if (!body) return;
  body.innerHTML = text.replace(/&#10;/g, '<br>');
  if (card) card.style.display = 'block';
}}
function setNutritionCoach(text) {{
  var el = document.getElementById('brief-nutr');
  if (el) el.innerHTML = text;
}}
function setNotes(html) {{
  var el = document.getElementById('notes-list');
  if (el) el.innerHTML = html;
}}
function setFoodLogList(html) {{
  var el = document.getElementById('food-log-list');
  if (el) el.innerHTML = html;
}}
function setWeeklyLog(html) {{
  var el = document.getElementById('weekly-log-body');
  if (el) el.innerHTML = html;
}}
(function() {{
  var box = document.getElementById('food-box'), tmr = null;
  if (box) box.addEventListener('input', function() {{
    clearTimeout(tmr);
    tmr = setTimeout(function() {{ document.title = '__fooddraft__' + Date.now() + '|' + encodeURIComponent(box.value); }}, 500);
  }});
}})();
function toggleExcludeDay(day) {{ document.title = '__excludeday__' + Date.now() + '|' + day; }}
function submitFood() {{
  var box = document.getElementById('food-box');
  if (!box) return;
  var v = box.value.trim();
  if (!v) return;
  // Box stays enabled so entries can be queued up back-to-back — the Python
  // side serializes them through a single worker so food-log.json never races.
  box.value = '';
  var dateEl = document.getElementById('food-date');
  if (dateEl && dateEl.value) v = dateEl.value + '::' + v;   // backlog day (sticky until cleared)
  document.title = '__logfood__' + Date.now() + '|' + encodeURIComponent(v);
}}
var _foodEditBackup = {{}};
function editFood(id) {{
  var row = document.getElementById('food-row-' + id);
  if (!row) return;
  var data = JSON.parse(row.getAttribute('data-entry'));
  _foodEditBackup[id] = row.innerHTML;
  function esc(s) {{ return String(s).replace(/"/g, '&quot;'); }}
  row.innerHTML =
    '<div class="food-entry-edit-form">' +
    '<input class="fe-time" value="' + esc(data.time) + '">' +
    '<input class="fe-desc" value="' + esc(data.description) + '">' +
    '<input class="fe-num fe-cal" type="number" value="' + data.calories + '" title="kcal">' +
    '<input class="fe-num fe-prot" type="number" value="' + data.protein_g + '" title="protein g">' +
    '<input class="fe-num fe-carb" type="number" value="' + data.carbs_g + '" title="carbs g">' +
    '<input class="fe-num fe-fat" type="number" value="' + data.fat_g + '" title="fat g">' +
    '<button onclick="saveFoodEdit(\\'' + id + '\\')">Save</button>' +
    '<button onclick="cancelFoodEdit(\\'' + id + '\\')">Cancel</button>' +
    '</div>';
}}
function cancelFoodEdit(id) {{
  var row = document.getElementById('food-row-' + id);
  if (row && _foodEditBackup[id] !== undefined) row.innerHTML = _foodEditBackup[id];
}}
function saveFoodEdit(id) {{
  var row = document.getElementById('food-row-' + id);
  if (!row) return;
  var payload = {{
    id: id,
    time: row.querySelector('.fe-time').value,
    description: row.querySelector('.fe-desc').value,
    calories: parseFloat(row.querySelector('.fe-cal').value) || 0,
    protein_g: parseFloat(row.querySelector('.fe-prot').value) || 0,
    carbs_g: parseFloat(row.querySelector('.fe-carb').value) || 0,
    fat_g: parseFloat(row.querySelector('.fe-fat').value) || 0,
  }};
  delete _foodEditBackup[id];
  document.title = '__editfood__' + Date.now() + '|' + encodeURIComponent(JSON.stringify(payload));
}}
function delFood(id) {{
  if (!confirm('Delete this entry?')) return;
  document.title = '__delfood__' + Date.now() + '|' + id;
}}
var _foodModelWarmUntil = 0;
function warmFoodModel() {{
  // Fired on focusing the food box: ask Ollama to load the model into RAM now,
  // so the load happens while the user is typing instead of after they hit
  // Enter. Re-pinging while already warm just extends keep_alive, so throttle
  // to once per 60s rather than once per focus event.
  var now = Date.now();
  if (now < _foodModelWarmUntil) return;
  _foodModelWarmUntil = now + 60000;
  document.title = '__warmfood__' + now;
}}
function setFoodQueued(n) {{
  var status = document.getElementById('food-status');
  if (!status) return;
  if (n > 0) {{
    status.style.display = 'block';
    status.textContent = n === 1 ? 'Logging…' : 'Logging… (' + n + ' queued)';
    status.className = 'food-status pending';
  }} else {{
    status.style.display = 'none';
  }}
}}
var _voiceState = {{food: 'idle', plan: 'idle'}};   // idle | recording | transcribing
var _voiceBox = {{food: 'food-box', plan: 'plan-box'}};
var _voiceStatus = {{food: 'food-status', plan: 'plan-status'}};
function toggleVoice(target) {{
  if (_voiceState[target] === 'transcribing') return;
  var starting = _voiceState[target] === 'idle';
  // One title write only: a second one in the same tick (e.g. warmFoodModel) can
  // replace it before the title-change signal fires. Python warms Qwen on start.
  document.title = '__voice__' + Date.now() + '|' + target + '|' + (starting ? 'start' : 'stop');
}}
function setVoiceState(target, state, msg) {{
  _voiceState[target] = state;
  var btn = document.getElementById(target + '-mic-btn');
  var status = document.getElementById(_voiceStatus[target]);
  if (btn) {{
    btn.className = 'mic-btn' + (state === 'recording' ? ' recording' : state === 'transcribing' ? ' busy' : '');
    btn.innerHTML = state === 'recording' ? '&#9632;' : '&#127908;';
  }}
  if (status) {{
    if (msg) {{ status.style.display = 'block'; status.textContent = msg; status.className = 'food-status pending'; }}
    else if (state === 'idle') {{ status.style.display = 'none'; }}
  }}
}}
function voiceError(target, msg) {{
  if (target === 'food') setFoodError(msg); else setPlanStatus(msg, true);
}}
function setVoiceTranscript(target, text) {{
  setVoiceState(target, 'idle', target === 'food' ? 'Check the transcript, then Enter to log'
                                                  : 'Check the transcript, then Plan');
  var box = document.getElementById(_voiceBox[target]);
  if (!box || !text) return;
  // Appended, not replaced, so a second clip can add to a half-typed entry.
  box.value = box.value.trim() ? box.value.trim() + ', ' + text : text;
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
}}
function setPlanStatus(msg, isError) {{
  var el = document.getElementById('plan-status');
  if (!el) return;
  el.style.display = msg ? 'block' : 'none';
  el.textContent = msg || '';
  el.className = 'food-status ' + (isError ? 'error' : 'pending');
}}
function submitPlan() {{
  var box = document.getElementById('plan-box');
  if (!box || !box.value.trim()) return;
  setPlanStatus('Reading your plan…');
  document.title = '__planweek__' + Date.now() + '|' + encodeURIComponent(box.value.trim());
}}
var _planKinds = ['mobility', 'recovery', 'easy', 'steady', 'threshold', 'vo2max', 'strength', 'rest'];
function showPlanDraft(rows, warnings) {{
  setPlanStatus('Check and edit, then Save — saving replaces whatever was planned on these days.');
  var el = document.getElementById('plan-draft');
  if (!el) return;
  var warnHtml = (warnings || []).map(function(w) {{
    return '<div class="plan-warn">&#9888; ' + String(w).replace(/</g, '&lt;') + '</div>'; }}).join('');
  function esc(v) {{ return String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'); }}
  var html = '<table class="plan-grid"><tr><th>Day</th><th>Session</th><th>min</th><th>type</th><th>work</th><th></th></tr>';
  rows.forEach(function(r, i) {{
    var d = new Date(r.date + 'T12:00:00');
    var day = d.toLocaleDateString('en-GB', {{weekday: 'short', day: 'numeric'}});
    html += '<tr data-i="' + i + '">' +
      '<td><input class="pg-date" type="date" value="' + esc(r.date) + '" title="' + esc(day) + '"></td>' +
      '<td><input class="pg-name" value="' + esc(r.name) + '"></td>' +
      '<td><input class="pg-min" type="number" value="' + esc(r.minutes) + '"></td>' +
      '<td><select class="pg-kind">' + _planKinds.map(function(k) {{
        return '<option' + (k === r.kind ? ' selected' : '') + '>' + k + '</option>'; }}).join('') + '</select></td>' +
      '<td><input class="pg-work" type="number" value="' + esc(r.work_minutes) + '" title="minutes of hard intervals"></td>' +
      '<td><button class="pg-del" onclick="this.closest(\\'tr\\').remove()" title="Remove">&#10005;</button></td></tr>';
  }});
  html += '</table>' + warnHtml + '<div class="plan-btns" style="margin-top:6px">' +
    '<button onclick="savePlanDraft()">Save</button>' +
    '<button onclick="cancelPlanDraft()">Cancel</button></div>';
  el.innerHTML = html;
}}
function savePlanDraft() {{
  var rows = [];
  document.querySelectorAll('#plan-draft tr[data-i]').forEach(function(tr) {{
    rows.push({{
      date: tr.querySelector('.pg-date').value,
      name: tr.querySelector('.pg-name').value,
      minutes: parseInt(tr.querySelector('.pg-min').value) || 0,
      kind: tr.querySelector('.pg-kind').value,
      work_minutes: parseInt(tr.querySelector('.pg-work').value) || 0,
    }});
  }});
  if (!rows.length) {{ cancelPlanDraft(); return; }}
  setPlanStatus('Saving…');
  document.title = '__saveplan__' + Date.now() + '|' + encodeURIComponent(JSON.stringify(rows));
}}
function cancelPlanDraft() {{
  var el = document.getElementById('plan-draft');
  if (el) el.innerHTML = '';
  setPlanStatus('');
}}
function setFoodError(msg) {{
  var status = document.getElementById('food-status');
  if (status) {{ status.style.display = 'block'; status.textContent = msg; status.className = 'food-status error'; }}
}}
function submitNote() {{
  var box = document.getElementById('note-box');
  if (!box) return;
  var v = box.value.trim();
  if (!v) return;
  // nonce ensures notify::title fires even for repeated text
  document.title = '__note__' + Date.now() + '|' + encodeURIComponent(v);
  box.value = '';
}}
function clearNotes() {{
  document.title = '__clearnotes__' + Date.now();
}}
function setIllnessPending() {{
  var val = document.getElementById('illness-val');
  var sub = document.getElementById('illness-sub');
  if (val) {{ val.textContent = '—'; val.style.color = '#888'; }}
  if (sub) sub.textContent = 'awaiting today\\'s data';
}}
function setIllnessData(todayP, yesterdayP, bands, spark7d) {{
  DATA.illnessBands = bands;
  if (spark7d) {{
    DATA.illness7dDates = spark7d.dates;
    DATA.illness7dVals  = spark7d.vals;
  }}
  var val = document.getElementById('illness-val');
  var sub = document.getElementById('illness-sub');
  if (!val) return;
  var pct = Math.round(todayP * 100);
  val.textContent = pct + '%';
  val.style.color = todayP < 0.20 ? '#6fcf97' : todayP < 0.50 ? '#e3b95f' : '#e8837a';
  if (sub) {{
    var delta = Math.round((todayP - yesterdayP) * 100);
    sub.textContent = (delta >= 0 ? '+' : '') + delta + 'pp vs yesterday';
  }}
  drawHrv();
  drawIllnessSpark();
}}

// ── Illness tab (experimental) ────────────────────────────────────────────────
function illBands(ctx, dates, xOf, yTop, hgt) {{
  (DATA.illnessBands || []).forEach(function(b) {{
    var si = dates.findIndex(function(d) {{ return d >= b.start; }});
    var ei = dates.findIndex(function(d) {{ return d > b.end; }});
    if (si < 0) return;
    if (ei < 0) ei = dates.length - 1;
    if (ei <= si) ei = si + 1;
    ctx.fillStyle = 'rgba(239,68,68,0.15)';
    ctx.fillRect(xOf(si), yTop, xOf(ei) - xOf(si), hgt);
  }});
}}

function drawIllnessRisk() {{
  var g = setupCanvas('c-ill-risk', 'w-ill-risk');
  if (!g) return;
  var ctx = g.ctx, W = g.W, H = g.H;
  var dates = DATA.illnessAllDates || [], vals = DATA.illnessAllVals || [];
  if (!dates.length) return;
  var PAD = {{top:8, right:10, bottom:20, left:34}};
  var cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  var xOf = function(i) {{ return PAD.left + (i / Math.max(dates.length - 1, 1)) * cW; }};
  var yOf = function(v) {{ return PAD.top + (1 - v) * cH; }};

  illBands(ctx, dates, xOf, PAD.top, cH);
  chartAxes(ctx, W, H, 0, 1, 4, PAD, dates);

  var thr = (DATA.illnessMeta && DATA.illnessMeta.sick_thresh) || 0.4;
  ctx.strokeStyle = '#56585a'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(PAD.left, yOf(thr)); ctx.lineTo(W - PAD.right, yOf(thr));
  ctx.stroke(); ctx.setLineDash([]);

  ctx.fillStyle = 'rgba(248,113,113,.13)';
  ctx.beginPath(); ctx.moveTo(xOf(0), yOf(0));
  vals.forEach(function(v, i) {{ ctx.lineTo(xOf(i), yOf(v)); }});
  ctx.lineTo(xOf(vals.length - 1), yOf(0)); ctx.closePath(); ctx.fill();

  plotSeries(ctx, xOf, yOf, vals, '#e8837a', 1.4);
  ctx.fillStyle = '#e8837a';
  ctx.beginPath(); ctx.arc(xOf(vals.length - 1), yOf(vals[vals.length - 1]), 2.6, 0, 7); ctx.fill();
}}

function drawIllnessRhr() {{
  var g = setupCanvas('c-ill-rhr', 'w-ill-rhr');
  if (!g) return;
  var ctx = g.ctx, W = g.W, H = g.H;
  var dates = DATA.illnessAllDates || [], rhr = DATA.illnessRhr || [];
  var seen = rhr.filter(function(v) {{ return v != null; }});
  if (!seen.length) return;
  var PAD = {{top:8, right:10, bottom:20, left:34}};
  var lo = Math.min.apply(null, seen) - 2, hi = Math.max.apply(null, seen) + 2;
  var cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  var xOf = function(i) {{ return PAD.left + (i / Math.max(dates.length - 1, 1)) * cW; }};
  var yOf = function(v) {{ return PAD.top + (1 - (v - lo) / (hi - lo)) * cH; }};

  illBands(ctx, dates, xOf, PAD.top, cH);
  chartAxes(ctx, W, H, lo, hi, 4, PAD, dates);

  var meta = DATA.illnessMeta;
  if (meta && meta.baseline_rhr >= lo && meta.baseline_rhr <= hi) {{
    ctx.strokeStyle = '#424446'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(PAD.left, yOf(meta.baseline_rhr));
    ctx.lineTo(W - PAD.right, yOf(meta.baseline_rhr)); ctx.stroke(); ctx.setLineDash([]);
  }}
  plotSeries(ctx, xOf, yOf, rhr, '#e3b95f', 1.3);
}}

// ── Sleep view ────────────────────────────────────────────────────────────────
var SS = {{date: null, rating: null, latency: '', wakings: '', factors: [], note: ''}};
function sleepEsc(s) {{ return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }}
function sleepFmt(h) {{ var m = Math.round(h * 60); return Math.floor(m / 60) + 'h ' + ('0' + (m % 60)).slice(-2) + 'm'; }}
function sleepDelta(v, unit, goodWhenHigh) {{
  if (v === null || v === undefined) return '';
  var good = goodWhenHigh ? v >= 0 : v <= 0;
  return '<span style="color:' + (Math.abs(v) < 0.05 ? '#75777a' : good ? '#6fcf97' : '#e3b95f') + '">' + (v > 0 ? '+' : '') + v + unit + '</span>';
}}
function sleepBars(nights, target) {{
  var W = 640, H = 150, bw = W / nights.length, max = Math.max(10, Math.ceil(Math.max.apply(null, nights.map(function(n) {{ return n.hours; }}))));
  var s = '<svg viewBox="0 0 ' + W + ' ' + (H + 34) + '" style="width:100%;height:auto">';
  var ty = H - target / max * H;
  s += '<line x1="0" x2="' + W + '" y1="' + ty + '" y2="' + ty + '" stroke="#56585a" stroke-dasharray="4 4"/>'
     + '<text x="2" y="' + (ty - 4) + '" fill="#75777a" font-size="10">' + target + 'h target</text>';
  nights.forEach(function(n, i) {{
    var h = n.hours / max * H, c = n.hours >= target ? '#6fcf97' : n.hours >= target - 1 ? '#e3b95f' : '#e8837a';
    s += '<rect x="' + (i * bw + 2) + '" y="' + (H - h) + '" width="' + (bw - 4) + '" height="' + h + '" rx="3" fill="' + c + '" opacity="0.8"><title>' + n.date + ': ' + sleepFmt(n.hours) + (n.rating ? ', rested ' + n.rating + '/5' : '') + '</title></rect>';
    s += '<text x="' + (i * bw + bw / 2) + '" y="' + (H + 16) + '" text-anchor="middle" fill="' + (n.rating ? '#d4d6d8' : '#424446') + '" font-size="11">' + (n.rating || '·') + '</text>';
  }});
  s += '<text x="0" y="' + (H + 31) + '" fill="#75777a" font-size="10">' + nights[0].date.slice(5) + '</text>'
     + '<text x="' + W + '" y="' + (H + 31) + '" text-anchor="end" fill="#75777a" font-size="10">' + nights[nights.length - 1].date.slice(5) + '</text>'
     + '<text x="0" y="' + (H + 16) + '" fill="#75777a" font-size="9" dx="-0">&nbsp;</text></svg>';
  return s;
}}
function slMean(a) {{ var x = a.filter(function(v) {{ return v != null; }}); return x.length ? x.reduce(function(s, v) {{ return s + v; }}, 0) / x.length : null; }}
function slRoll(vals, w) {{ return vals.map(function(_, i) {{ return slMean(vals.slice(Math.max(0, i - w + 1), i + 1)); }}); }}
function slLine(vals, dates, o) {{
  var W = 360, H = o.h || 120, L = 34, R = 8, T = 8, B = 18, n = vals.length;
  var nums = vals.filter(function(v) {{ return v != null; }}).concat(o.ref != null ? [o.ref] : []);
  if (n < 2 || !nums.length) return '<div class="pm-hint">Not enough data yet.</div>';
  var lo = Math.min.apply(null, nums), hi = Math.max.apply(null, nums), pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  var x = function(i) {{ return L + i / (n - 1) * (W - L - R); }}, y = function(v) {{ return T + (1 - (v - lo) / (hi - lo)) * (H - T - B); }};
  var fmt = o.fmt || function(v) {{ return v.toFixed(0); }};
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">';
  for (var g = 0; g <= 2; g++) {{ var gv = lo + (hi - lo) * g / 2, gy = y(gv);
    s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + gy + '" y2="' + gy + '" stroke="#2a2c2f"/><text x="' + (L - 5) + '" y="' + (gy + 3) + '" text-anchor="end" fill="#75777a" font-size="9">' + fmt(gv) + '</text>'; }}
  if (o.ref != null) s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(o.ref) + '" y2="' + y(o.ref) + '" stroke="#56585a" stroke-dasharray="4 4"/>';
  var path = function(a) {{ var d = '', pen = false; a.forEach(function(v, i) {{ if (v == null) {{ pen = false; return; }} d += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); pen = true; }}); return d; }};
  s += '<path d="' + path(vals) + '" fill="none" stroke="' + o.color + '" stroke-width="1.2" opacity="' + (o.avg ? 0.4 : 1) + '"/>';
  if (o.avg) s += '<path d="' + path(slRoll(vals, o.avg)) + '" fill="none" stroke="' + o.color + '" stroke-width="2.2"/>';
  s += '<text x="' + L + '" y="' + (H - 3) + '" fill="#75777a" font-size="9">' + dates[0].slice(5) + '</text><text x="' + (W - R) + '" y="' + (H - 3) + '" text-anchor="end" fill="#75777a" font-size="9">' + dates[n - 1].slice(5) + '</text></svg>';
  return s;
}}
function slColumns(labels, vals, o) {{
  var W = 360, H = o.h || 120, L = 8, R = 8, T = 14, B = 18, n = vals.length, max = Math.max.apply(null, vals.concat([o.ref || 0, 1])) * 1.1;
  var bw = (W - L - R) / n, s = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">';
  if (o.ref != null) {{ var ry = T + (1 - o.ref / max) * (H - T - B); s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + ry + '" y2="' + ry + '" stroke="#56585a" stroke-dasharray="4 4"/>'; }}
  vals.forEach(function(v, i) {{
    var h = v / max * (H - T - B), c = o.colorFn ? o.colorFn(v) : '#88dcfd';
    s += '<rect x="' + (L + i * bw + 3) + '" y="' + (H - B - h) + '" width="' + (bw - 6) + '" height="' + h + '" rx="3" fill="' + c + '" opacity="0.85"/>'
      + '<text x="' + (L + i * bw + bw / 2) + '" y="' + (H - B - h - 3) + '" text-anchor="middle" fill="#d4d6d8" font-size="9">' + (o.valFmt ? o.valFmt(v, i) : v) + '</text>'
      + '<text x="' + (L + i * bw + bw / 2) + '" y="' + (H - 4) + '" text-anchor="middle" fill="#75777a" font-size="9">' + labels[i] + '</text>';
  }});
  return s + '</svg>';
}}
function slScatter(pts, o) {{
  var W = 360, H = 160, L = 34, R = 10, T = 8, B = 24;
  if (pts.length < 8) return '<div class="pm-hint">Needs at least 8 nights with data (have ' + pts.length + ').</div>';
  var xs = pts.map(function(p) {{ return p.x; }}), ys = pts.map(function(p) {{ return p.y; }});
  var xlo = Math.min.apply(null, xs) - 0.2, xhi = Math.max.apply(null, xs) + 0.2, ylo = Math.min.apply(null, ys) * 0.95, yhi = Math.max.apply(null, ys) * 1.05;
  var x = function(v) {{ return L + (v - xlo) / (xhi - xlo) * (W - L - R); }}, y = function(v) {{ return T + (1 - (v - ylo) / (yhi - ylo)) * (H - T - B); }};
  var mx = slMean(xs), my = slMean(ys), sxx = 0, syy = 0, sxy = 0;
  pts.forEach(function(p) {{ sxx += (p.x - mx) * (p.x - mx); syy += (p.y - my) * (p.y - my); sxy += (p.x - mx) * (p.y - my); }});
  var slope = sxy / sxx, icpt = my - slope * mx, r = sxy / Math.sqrt(sxx * syy);
  var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto;display:block">';
  for (var g = 0; g <= 2; g++) {{ var gv = ylo + (yhi - ylo) * g / 2; s += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(gv) + '" y2="' + y(gv) + '" stroke="#2a2c2f"/><text x="' + (L - 5) + '" y="' + (y(gv) + 3) + '" text-anchor="end" fill="#75777a" font-size="9">' + gv.toFixed(0) + '</text>'; }}
  for (var k = Math.ceil(xlo); k <= xhi; k++) s += '<text x="' + x(k) + '" y="' + (H - 8) + '" text-anchor="middle" fill="#75777a" font-size="9">' + k + 'h</text>';
  pts.forEach(function(p) {{ s += '<circle cx="' + x(p.x) + '" cy="' + y(p.y) + '" r="3.2" fill="#88dcfd" opacity="0.7"><title>' + p.t + '</title></circle>'; }});
  s += '<line x1="' + x(xlo) + '" y1="' + y(slope * xlo + icpt) + '" x2="' + x(xhi) + '" y2="' + y(slope * xhi + icpt) + '" stroke="#e3b95f" stroke-width="1.6"/></svg>';
  var word = Math.abs(r) < 0.2 ? 'little relationship' : (r > 0 ? 'longer sleep goes with higher values' : 'longer sleep goes with lower values');
  return s + '<div class="pm-hint">r = ' + r.toFixed(2) + ' over ' + pts.length + ' nights &middot; ' + word + '. Correlation, not cause.</div>';
}}
function sleepCharts(N, target) {{
  var dates = N.map(function(n) {{ return n.date; }}), hrs = N.map(function(n) {{ return n.hours; }});
  var card = function(title, sub, body) {{ return '<div class="card"><div class="brief-label">' + title + (sub ? ' <span style="text-transform:none;color:#75777a">' + sub + '</span>' : '') + '</div>' + body + '</div>'; }};
  var debt = hrs.map(function(_, i) {{ if (i < 6) return null; var s = 0; for (var j = i - 6; j <= i; j++) s += target - hrs[j]; return s; }});
  var wd = [[], [], [], [], [], [], []];
  N.forEach(function(n) {{ wd[(new Date(n.date + 'T12:00:00').getDay() + 6) % 7].push(n.hours); }});
  var wdAvg = wd.map(function(a) {{ return slMean(a) || 0; }});
  var bins = [[0, 5, '<5'], [5, 6, '5-6'], [6, 7, '6-7'], [7, 8, '7-8'], [8, 9, '8-9'], [9, 10, '9-10'], [10, 99, '10+']];
  var counts = bins.map(function(b) {{ return hrs.filter(function(h) {{ return h >= b[0] && h < b[1]; }}).length; }});
  var okc = function(h) {{ return h >= target ? '#6fcf97' : h >= target - 1 ? '#e3b95f' : '#e8837a'; }};
  var hrvPts = N.filter(function(n) {{ return n.hrv; }}).map(function(n) {{ return {{x: n.hours, y: n.hrv, t: n.date + ': ' + sleepFmt(n.hours) + ', HRV ' + Math.round(n.hrv)}}; }});
  var f1 = function(v) {{ return v.toFixed(1); }};
  return '<div class="sl-grid">'
    + card('Duration trend', '(' + N.length + ' nights, bold = 7-night average)', slLine(hrs, dates, {{color: '#88dcfd', avg: 7, ref: target, fmt: f1}}))
    + card('Rolling 7-night debt', '(hours owed vs ' + target + 'h; above 0 = behind)', slLine(debt, dates, {{color: '#e3b95f', ref: 0, fmt: f1}}))
    + card('Sleep score', '(bold = 7-night average)', slLine(N.map(function(n) {{ return n.score; }}), dates, {{color: '#b79cf0', avg: 7}}))
    + card('HRV', '(bold = 7-night average)', slLine(N.map(function(n) {{ return n.hrv; }}), dates, {{color: '#6fcf97', avg: 7}}))
    + card('Resting HR', '(bold = 7-night average)', slLine(N.map(function(n) {{ return n.rhr; }}), dates, {{color: '#e8837a', avg: 7}}))
    + card('Average by weekday', '(hours; weekend catch-up shows here)', slColumns(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'], wdAvg, {{ref: target, colorFn: okc, valFmt: function(v) {{ return v ? v.toFixed(1) : ''; }}}}))
    + card('How long you usually sleep', '(nights per duration band)', slColumns(bins.map(function(b) {{ return b[2]; }}), counts, {{valFmt: function(v) {{ return v || ''; }}}}))
    + card('Duration vs that morning&rsquo;s HRV', '', slScatter(hrvPts, {{}}))
    + '</div>';
}}
function sleepChips(list, selected, kind) {{
  return list.map(function(o) {{
    var on = Array.isArray(selected) ? selected.indexOf(o) >= 0 : selected === o;
    return '<span class="sl-chip' + (on ? ' on' : '') + '" data-k="' + kind + '" data-v="' + sleepEsc(o) + '">' + sleepEsc(o) + '</span>';
  }}).join('');
}}
function renderSleep() {{
  var root = document.getElementById('sleep-root'), D = DATA.sleep;
  if (!root) return;
  if (!D || !D.last) {{ root.textContent = 'No sleep data yet.'; return; }}
  var L = D.last, S = D.stats, O = D.options;
  if (SS.date !== L.date) {{
    SS = {{date: L.date, rating: L.rating || null, latency: L.latency || '', wakings: L.wakings || '', factors: (L.factors || []).slice(), note: L.note || ''}};
  }}
  var tile = function(lab, v, d) {{ return '<div class="card sl-tile"><div class="brief-label">' + lab + '</div><div class="v">' + v + '</div><div class="d">' + d + '</div></div>'; }};
  var h = '<div class="brief-label">Last night &middot; ' + L.date + '</div><div class="sl-tiles" style="margin-top:8px">'
    + tile('Duration', sleepFmt(L.hours), sleepDelta(Math.round((L.hours - D.target_h) * 10) / 10, 'h', true) + ' vs ' + D.target_h + 'h target')
    + tile('Sleep score', L.score ? Math.round(L.score) : '&mdash;', S.avg_score14 ? '14-night avg ' + S.avg_score14 : '')
    + tile('HRV', L.hrv ? Math.round(L.hrv) : '&mdash;', S.hrv_delta !== null ? sleepDelta(S.hrv_delta, '', true) + ' vs prior 7 nights' : '')
    + tile('Resting HR', L.rhr ? L.rhr + ' bpm' : '&mdash;', S.rhr_delta !== null ? sleepDelta(S.rhr_delta, '', false) + ' vs prior 7 nights' : '')
    + tile('7-night sleep debt', Math.abs(S.debt7).toFixed(1) + 'h', S.debt7 > 0 ? 'owed vs ' + D.target_h + 'h/night' : 'ahead of target')
    + tile('Consistency', S.sd_h14 !== null ? '&plusmn;' + S.sd_h14.toFixed(1) + 'h' : '&mdash;', 'spread of duration, 14 nights')
    + '</div>';
  h += '<div class="card" style="margin-top:14px"><div class="brief-label">Last 30 nights <span style="text-transform:none;color:#75777a">(bar = duration, digit = how rested you rated it)</span></div>' + sleepBars(D.nights.slice(-30), D.target_h) + '</div>' + sleepCharts(D.nights, D.target_h);
  h += '<div class="card" style="margin-top:14px"><div class="brief-label">How did you sleep? &middot; night ending ' + L.date + '</div>'
    + '<div style="margin:10px 0 4px;color:#a3a6a9">How rested do you feel?</div>'
    + [1,2,3,4,5].map(function(r) {{ var lab = ['wrecked','poor','okay','good','great'][r-1];
        return '<span class="sl-chip' + (SS.rating === r ? ' on' : '') + '" data-k="rating" data-v="' + r + '">' + r + ' &middot; ' + lab + '</span>'; }}).join('')
    + '<div style="margin:8px 0 4px;color:#a3a6a9">Time to fall asleep</div>' + sleepChips(O.latency, SS.latency, 'latency')
    + '<div style="margin:8px 0 4px;color:#a3a6a9">Times woken in the night</div>' + sleepChips(O.wakings, SS.wakings, 'wakings')
    + '<div style="margin:8px 0 4px;color:#a3a6a9">What helped?</div>' + sleepChips(O.helps, SS.factors, 'factor')
    + '<div style="margin:8px 0 4px;color:#a3a6a9">What might have hurt?</div>' + sleepChips(O.hurts, SS.factors, 'factor')
    + '<input type="text" id="sl-note" placeholder="Anything else (optional)" value="' + sleepEsc(SS.note) + '" style="width:100%;margin-top:8px">'
    + '<div style="margin-top:12px"><button class="btn" id="sl-save">' + (L.rating ? 'Update rating' : 'Save rating') + '</button>'
    + (L.rating ? ' <span style="color:#6fcf97;font-size:12px">saved</span>' : '') + '</div></div>';
  h += '<div class="card" style="margin-top:14px"><div class="brief-label">What your ratings say</div>';
  if (D.insights.length) {{
    D.insights.forEach(function(x) {{ h += '<div style="padding:5px 0">' + sleepEsc(x.text) + ' <span style="color:#75777a;font-size:11.5px">(' + x.n + ')</span></div>'; }});
    h += '<div style="color:#75777a;font-size:11.5px;margin-top:6px">Small samples: patterns, not proof.</div>';
  }} else {{
    h += '<div style="color:#a3a6a9;padding:4px 0">' + S.rated_total + ' night' + (S.rated_total === 1 ? '' : 's') + ' rated. Comparisons (duration, and each factor) appear once there are at least 3 nights on each side.</div>';
  }}
  h += '</div><div style="color:#75777a;font-size:11.5px;margin-top:14px">The feed has duration, score, HRV and resting HR only &mdash; no sleep stages or bed/wake times, so those aren&rsquo;t shown.</div>';
  root.innerHTML = h;
}}
(function() {{
  var root = document.getElementById('sleep-root');
  if (!root) return;
  root.addEventListener('click', function(e) {{
    var c = e.target.closest('[data-k]');
    if (c) {{
      var k = c.dataset.k, v = c.dataset.v;
      if (k === 'rating') SS.rating = parseInt(v, 10);
      else if (k === 'factor') {{ var i = SS.factors.indexOf(v); if (i >= 0) SS.factors.splice(i, 1); else SS.factors.push(v); }}
      else SS[k] = (SS[k] === v ? '' : v);
      renderSleep();
      return;
    }}
    if (e.target.id === 'sl-save') {{
      if (!SS.rating) {{ e.target.textContent = 'Pick a rating first'; return; }}
      var n = document.getElementById('sl-note'); SS.note = n ? n.value : '';
      document.title = '__sleeprate__' + Date.now() + '|' + encodeURIComponent(JSON.stringify({{date: SS.date, rating: SS.rating, latency: SS.latency, wakings: SS.wakings, factors: SS.factors, note: SS.note}}));
    }}
  }});
  root.addEventListener('input', function(e) {{ if (e.target.id === 'sl-note') SS.note = e.target.value; }});
}})();

// ── Life tab ──────────────────────────────────────────────────────────────────
function lifeEsc(s) {{ return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }}
function lifeSend(cmd, obj) {{ document.title = '__' + cmd + '__' + Date.now() + '|' + encodeURIComponent(JSON.stringify(obj)); }}
var LIFE_COL = {{research:'#88dcfd', admin:'#b79cf0', home:'#34d399', social:'#88dcfd', health:'#6fcf97',
                 rest:'#a3a6a9', other:'#75777a', training:'#e3b95f'}};
function lifeChip(area) {{
  return '<span style="color:' + (LIFE_COL[area] || '#75777a') + ';font-size:11px;text-transform:uppercase">' + lifeEsc(area) + '</span>';
}}
function lifeInit() {{
  var L = DATA.life, sel = document.getElementById('lf-area');
  if (!L || !sel || sel.options.length) return;
  L.areas.forEach(function(a) {{ var o = document.createElement('option'); o.value = a; o.textContent = a; sel.appendChild(o); }});
  document.getElementById('lf-add').onclick = function() {{
    var type = document.getElementById('lf-type').value, title = document.getElementById('lf-title').value.trim();
    if (!title) return;
    var d = document.getElementById('lf-date').value;
    if (type === 'event' && !d) d = DATA.life.today;
    var obj = {{type: type, title: title, area: sel.value, start: document.getElementById('lf-start').value,
                end: document.getElementById('lf-end').value, repeat: document.getElementById('lf-repeat').value}};
    obj[type === 'event' ? 'date' : 'due'] = d;
    lifeSend('lifeadd', obj);
    document.getElementById('lf-title').value = '';
  }};
  document.getElementById('lf-title').addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') document.getElementById('lf-add').click();
  }});
  document.getElementById('life-lists').addEventListener('click', function(e) {{
    var b = e.target.closest('[data-act]');
    if (b) lifeSend('lifeact', {{act: b.dataset.act, id: b.dataset.id}});
  }});
}}
function renderLife() {{
  var L = DATA.life, root = document.getElementById('life-lists');
  if (!root) return;
  if (!L) {{ root.textContent = 'life.py not available.'; return; }}
  lifeInit();
  var days = {{}}, order = [];
  L.events.forEach(function(e) {{ if (!days[e.date]) {{ days[e.date] = []; order.push(e.date); }} days[e.date].push(e); }});
  var h = '<div style="display:flex;gap:18px;flex-wrap:wrap"><div class="card" style="flex:1;min-width:300px">'
    + '<div class="brief-label">Next 7 days</div>';
  if (!order.length) h += '<div style="color:#75777a;padding:6px 0">Nothing scheduled.</div>';
  order.forEach(function(d) {{
    var dt = new Date(d + 'T12:00:00');
    h += '<div style="margin-top:10px;color:' + (d === L.today ? '#88dcfd' : '#a3a6a9') + ';font-size:12px">'
      + dt.toLocaleDateString('en-GB', {{weekday:'short', day:'numeric', month:'short'}}) + (d === L.today ? ' (today)' : '') + '</div>';
    days[d].forEach(function(e) {{
      h += '<div style="padding:3px 0 3px 8px;border-left:2px solid ' + (LIFE_COL[e.area] || '#424446') + '">'
        + (e.start ? '<span style="color:#75777a">' + e.start + (e.end ? '&ndash;' + e.end : '') + '</span> ' : '')
        + lifeEsc(e.title) + ' ' + lifeChip(e.area) + (e.repeat ? ' <span style="color:#75777a;font-size:11px">&#8635; ' + e.repeat + '</span>' : '')
        + (e.id ? ' <a href="#" data-act="del" data-id="' + e.id + '" style="color:#75777a;text-decoration:none">&times;</a>' : '') + '</div>';
    }});
  }});
  h += '</div><div class="card" style="flex:1;min-width:300px"><div class="brief-label">Todo (' + L.todos.length + ' open)</div>';
  if (!L.todos.length) h += '<div style="color:#75777a;padding:6px 0">Nothing open.</div>';
  L.todos.forEach(function(t) {{
    var overdue = t.due && t.due < L.today;
    h += '<div style="padding:4px 0;border-bottom:1px solid #2a2c2f">'
      + '<a href="#" data-act="toggle" data-id="' + t.id + '" style="text-decoration:none;color:#a3a6a9">&#9744;</a> '
      + lifeEsc(t.title) + ' ' + lifeChip(t.area)
      + (t.due ? ' <span style="font-size:11px;color:' + (overdue ? '#e3b95f' : '#75777a') + '">' + (overdue ? 'overdue ' : 'due ') + t.due + '</span>' : '')
      + ' <a href="#" data-act="del" data-id="' + t.id + '" style="color:#75777a;text-decoration:none">&times;</a></div>';
  }});
  h += '</div></div>';
  var keys = Object.keys(L.balance), max = Math.max.apply(null, keys.map(function(k) {{ return L.balance[k]; }}).concat([1]));
  h += '<div class="card" style="margin-top:18px"><div class="brief-label">This week by area <span style="text-transform:none;color:#75777a">(todos done + events + training sessions)</span></div>';
  keys.forEach(function(k) {{
    var v = L.balance[k];
    h += '<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px"><span style="width:70px;color:#a3a6a9">' + k + '</span>'
      + '<div style="height:10px;border-radius:5px;background:' + (LIFE_COL[k] || '#75777a') + ';width:' + Math.round(v / max * 220) + 'px;min-width:' + (v ? 4 : 0) + 'px"></div>'
      + '<span style="color:#75777a">' + v + '</span></div>';
  }});
  root.innerHTML = h + '</div>';
}}

// ── Focus tab ─────────────────────────────────────────────────────────────────
function focusNow() {{ document.title = '__focusnow__' + Date.now(); }}
function renderFocus() {{
  var root = document.getElementById('focus-root');
  var F = DATA.focus;
  if (!root) return;
  if (!F) {{ root.textContent = 'focus.py not available.'; return; }}
  var bar = F.bar, col = bar >= 60 ? '#6fcf97' : bar >= 40 ? '#e3b95f' : '#e3b95f';
  var label = bar >= 60 ? 'Restorative-leaning' : bar >= 40 ? 'Balanced' : 'Cheap-stimulation-leaning';
  var L = F.latest, mood = L ? L.mood : null, en = L ? L.energy : null;
  var low = (mood && mood <= 2) || (en && en <= 2);
  var mismatch = (bar >= 60 && low) ? 'Plenty of restorative activity, but mood/energy is low \u2014 worth noticing why.'
    : (bar < 40 && mood && en && mood >= 4 && en >= 4) ? 'Cheap-stimulation-heavy, yet you feel good \u2014 fine, just note it.' : '';
  var slotNames = ['morning', 'midday', 'evening'];
  var h = '<div class="brief-label">Activity balance <span style="text-transform:none;color:#75777a">(what you did, not how you feel)</span></div>'
    + '<div style="position:relative;height:18px;background:#2a2c2f;border-radius:9px;margin:8px 0 4px">'
    + '<div style="position:absolute;left:50%;top:-3px;bottom:-3px;width:2px;background:#56585a"></div>'
    + '<div style="position:absolute;top:0;bottom:0;border-radius:9px;background:' + col + ';'
    + 'left:' + Math.min(50, bar) + '%;width:' + Math.abs(bar - 50) + '%"></div></div>'
    + '<div style="color:#a3a6a9;font-size:12px">' + label + ' &middot; a decaying tally of the tags you tick, not a dopamine reading</div>'
    + '<div class="brief-label" style="margin-top:16px">How you feel (latest check-in)</div>'
    + '<div style="font-size:15px">' + (L ? 'Mood <b>' + (mood || '-') + '</b>/5 &nbsp; Energy <b>' + (en || '-') + '</b>/5 <span style="color:#75777a;font-size:12px">&middot; ' + L.date + ' ' + L.slot + '</span>' : '<span style="color:#75777a">no check-in yet</span>') + '</div>'
    + (mismatch ? '<div style="margin-top:6px;color:#e3b95f;font-size:13px">' + mismatch + '</div>' : '');
  h += '<div class="brief-label" style="margin-top:20px">Today</div><div style="display:flex;gap:8px;align-items:center">';
  slotNames.forEach(function(s) {{
    var done = F.today_slots.indexOf(s) >= 0;
    h += '<span style="padding:5px 12px;border-radius:6px;border:1px solid ' + (done ? '#6fcf97' : '#424446')
      + ';color:' + (done ? '#bbf7d0' : '#75777a') + '">' + s + ' ' + F.slots[s] + (done ? ' ✓' : '') + '</span>';
  }});
  h += '<button class="btn" onclick="focusNow()">Check in now</button></div>';
  var entries = F.entries.slice().reverse().slice(0, 21);
  h += '<div class="brief-label" style="margin-top:20px">Recent check-ins</div>';
  if (!entries.length) h += '<div style="color:#75777a">No check-ins yet.</div>';
  entries.forEach(function(e) {{
    var rest = F.tags.restorative, cheap = F.tags.cheap;
    var tags = (e.tags || []).map(function(t) {{
      var c = cheap.indexOf(t) >= 0 ? '#e3b95f' : rest.indexOf(t) >= 0 ? '#6fcf97' : '#a3a6a9';
      return '<span style="color:' + c + '">' + t + '</span>';
    }}).join(', ');
    var gap = (e.want && e.like) ? ' &middot; wanted ' + e.want + ' / liked ' + e.like : '';
    h += '<div style="padding:8px 0;border-bottom:1px solid #2a2c2f"><span style="color:#75777a">'
      + e.date + ' ' + e.slot + '</span> &nbsp; mood ' + (e.mood || '-') + ' &middot; energy ' + (e.energy || '-')
      + gap + (e.stuck ? ' &middot; <span style="color:#e3b95f">STORER</span>' : '')
      + (tags ? '<br>' + tags : '') + (e.note ? '<br><i style="color:#d4d6d8">' + e.note.replace(/</g, '&lt;') + '</i>' : '') + '</div>';
  }});
  root.innerHTML = h;
}}

function renderIllnessTab() {{
  var meta  = DATA.illnessMeta;
  var vals  = DATA.illnessAllVals || [];
  var bands = DATA.illnessBands || [];
  var today = vals.length ? vals[vals.length - 1] : null;

  var head = document.getElementById('ill-today');
  if (head) {{
    if (today == null) {{
      head.innerHTML = '<b style="color:#75777a">—</b> awaiting data for today';
    }} else {{
      var col = today < 0.20 ? '#6fcf97' : today < 0.50 ? '#e3b95f' : '#e8837a';
      head.innerHTML = '<b style="color:' + col + '">' + (today * 100).toFixed(1) +
                       '%</b> posterior probability of illness today';
    }}
  }}

  function stat(k, v, u) {{
    return '<div class="ill-stat"><div class="k">' + k + '</div>' +
           '<div class="v">' + v + (u ? '<span class="u">' + u + '</span>' : '') + '</div></div>';
  }}
  var recent = vals.slice(-7);
  var mean7  = recent.length ? recent.reduce(function(a,b){{return a+b;}},0)/recent.length : null;
  var peak   = vals.length ? Math.max.apply(null, vals) : null;
  var over   = meta ? vals.filter(function(v) {{ return v > meta.sick_thresh; }}).length : 0;

  var rows = [
    stat('7-day mean risk', mean7 == null ? '—' : (mean7*100).toFixed(1), '%'),
    stat('Peak risk on record', peak == null ? '—' : (peak*100).toFixed(0), '%'),
    stat('Days above threshold', over, ' of ' + vals.length),
    stat('Episodes detected', bands.length),
  ];
  if (meta) {{
    rows.push(
      stat('GP lengthscale', meta.lengthscale.toFixed(1), ' d'),
      stat('GP amplitude', meta.amplitude.toFixed(2), ' bpm'),
      stat('Observation noise', meta.noise.toFixed(2), ' bpm'),
      stat('Baseline resting HR', meta.baseline_rhr.toFixed(1), ' bpm'),
      stat('ATL coefficient', (meta.atl_coef >= 0 ? '+' : '') + meta.atl_coef.toFixed(3), ' bpm/SD'),
      stat('Training window', meta.train_days, ' d'),
      stat('Last full re-fit', meta.last_full_run || '—'),
      stat('Last warm re-fit', meta.last_warm_run || '—')
    );
  }}
  document.getElementById('ill-stats').innerHTML = rows.join('');

  var byDate = {{}};
  (DATA.illnessAllDates || []).forEach(function(d, i) {{ byDate[d] = vals[i]; }});
  var eps = bands.slice().reverse().map(function(b) {{
    var d0 = new Date(b.start), d1 = new Date(b.end);
    var days = Math.round((d1 - d0) / 86400000) + 1;
    var pk = 0;
    Object.keys(byDate).forEach(function(d) {{
      if (d >= b.start && d <= b.end && byDate[d] > pk) pk = byDate[d];
    }});
    return '<div class="ill-ep"><span>' + b.start + ' → ' + b.end + '</span>' +
           '<span class="pk">peak ' + (pk*100).toFixed(0) + '%</span>' +
           '<span class="dur">' + days + 'd</span></div>';
  }});
  document.getElementById('ill-episodes').innerHTML =
    eps.length ? eps.join('') : '<div class="notes-empty">No episodes detected.</div>';
}}

// ── Tabs ──────────────────────────────────────────────────────────────────────
(function () {{
  var last = 0;
  function ping() {{ var n = Date.now(); if (n - last > 10000) {{ last = n; document.title = '__active__' + n; }} }}
  ['mousemove', 'keydown', 'scroll', 'click', 'wheel'].forEach(function (e) {{ document.addEventListener(e, ping, true); }});
  setInterval(function () {{ var a = document.activeElement; if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && (a.value || '').length) ping(); }}, 10000);
  try {{ var body = document.querySelector('.tab-body');
    if (body) {{ var y = +sessionStorage.getItem('tabScroll'); setTimeout(function () {{ body.scrollTop = y || 0; }}, 200);
      body.addEventListener('scroll', function () {{ sessionStorage.setItem('tabScroll', body.scrollTop); }}); }} }} catch (e) {{}}
}})();
setInterval(function () {{          // the eating pace moves with the clock, so keep it current without a reload
  var n = DATA && DATA.nutrition; if (!n || n.eatStart == null) return;
  var d = new Date(), h = d.getHours() + d.getMinutes() / 60, f = Math.max(0, Math.min((h - n.eatStart) / (n.eatEnd - n.eatStart), 1));
  n.dayFrac = Math.round(f * 1000) / 1000; n.paceExpected = Math.round(n.target * f);
  if (typeof renderNutritionDash === 'function') renderNutritionDash();
  if (typeof renderTodayDials === 'function') renderTodayDials();
}}, 60000);
function openApp(id) {{ if (!id) return; document.title = '__openapp__' + Date.now() + '|' + id; var s = document.getElementById('appSwitch'); if (s) s.value = ''; }}
var trackerRendered = false;
var illnessRendered = false;

// Stat -> detail: open the tab, then bring the relevant section into view.
function goTo(tab, anchor) {{
  selectTab(tab);
  if (!anchor) return;
  setTimeout(function() {{
    var el = document.getElementById(anchor);
    if (!el) return;
    el.scrollIntoView({{block: 'center', behavior: 'smooth'}});
    var host = el.closest('.chart-cell') || el;
    host.classList.add('flash');
    setTimeout(function() {{ host.classList.remove('flash'); }}, 1600);
  }}, 120);
}}

{_SUS_JS}

function selectTab(name) {{
  document.querySelectorAll('.tab').forEach(function(t) {{
    t.setAttribute('aria-selected', String(t.dataset.panel === name));
  }});
  document.querySelectorAll('.panel').forEach(function(p) {{
    p.classList.toggle('active', p.dataset.panel === name);
  }});

  var period = document.getElementById('periodSelect');
  if (period) period.hidden = (name !== 'trends');

  // Survive a full-page refresh (the GTK bridge reloads the whole document).
  try {{ sessionStorage.setItem('activeTab', name); }} catch (e) {{}}

  // A canvas inside a hidden panel has zero client size, so charts must be
  // (re)drawn when their panel becomes visible, not merely on load.
  if (name === 'today') {{
    requestAnimationFrame(function() {{
      renderTodayDials();
      drawSleep();
      addHoverToChart('c-slp', 'w-slp');   // idempotent; sleep's tooltip lives here now
    }});
  }} else if (name === 'trends') {{
    requestAnimationFrame(drawAll);
  }} else if (name === 'perf') {{
    requestAnimationFrame(function() {{
      if (!trackerRendered) {{ renderTracker(); trackerRendered = true; }}
      else {{ drawRadar(); }}
    }});
  }} else if (name === 'illness') {{
    requestAnimationFrame(function() {{
      if (!illnessRendered) {{ renderIllnessTab(); illnessRendered = true; }}
      drawIllnessRisk();
      drawIllnessRhr();
    }});
  }} else if (name === 'nutrition') {{
    requestAnimationFrame(renderNutritionDash);
  }} else if (name === 'focus') {{
    requestAnimationFrame(renderFocus);
  }} else if (name === 'sustain') {{
    requestAnimationFrame(renderSustain);
  }} else if (name === 'life') {{
    requestAnimationFrame(renderLife);
  }} else if (name === 'sleep') {{
    requestAnimationFrame(renderSleep);
  }}
}}

(function() {{
  document.querySelectorAll('.tab').forEach(function(t) {{
    t.addEventListener('click', function() {{ selectTab(t.dataset.panel); }});
  }});

  var planBox = document.getElementById('plan-box');
  if (planBox) {{
    planBox.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); submitPlan(); }}
    }});
  }}

  var noteBox = document.getElementById('note-box');
  if (noteBox) {{
    noteBox.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); submitNote(); }}
    }});
  }}

  // Refresh reloads the whole document; restore whichever tab was open, else
  // draw the default (Today) so its dials and sleep chart render on first paint.
  var saved = null;
  try {{ saved = sessionStorage.getItem('activeTab'); }} catch (e) {{}}
  if (!saved || !document.querySelector('.tab[data-panel="' + saved + '"]')) {{
    var def = document.querySelector('.tab[aria-selected="true"]');
    saved = def ? def.dataset.panel : 'today';
  }}
  selectTab(saved);
}})();

// Stat hover tooltips
(function() {{
  var tip = document.getElementById('stat-tip');
  document.querySelectorAll('[data-tip]').forEach(function(el) {{
    el.addEventListener('mouseenter', function(e) {{
      tip.textContent = el.getAttribute('data-tip');
      tip.style.display = 'block';
    }});
    el.addEventListener('mousemove', function(e) {{
      var x = e.clientX + 14, y = e.clientY - 10;
      if (x + 270 > window.innerWidth) x = e.clientX - 280;
      tip.style.left = x + 'px'; tip.style.top = y + 'px';
    }});
    el.addEventListener('mouseleave', function() {{ tip.style.display = 'none'; }});
  }});
}})();

window.addEventListener('load', drawAll);
window.addEventListener('resize', drawAll);

function showStaleWarning() {{
  var el = document.getElementById('stale-warn');
  if (el) el.style.display = 'block';
}}

// ── Nutrition dashboard ───────────────────────────────────────────────────────
function drawDonut(cid, segments, opts) {{
  var canvas = document.getElementById(cid);
  if (!canvas) return;
  var W = canvas.clientWidth, H = canvas.clientHeight;
  if (!W || !H) return;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = W * dpr; canvas.height = H * dpr;
  var ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  var cx = W/2, cy = H/2, R = Math.min(W, H)/2 - 2, r = R * (opts && opts.inner || 0.62);
  var total = segments.reduce(function(s, x) {{ return s + Math.max(x.value, 0); }}, 0);
  var a0 = -Math.PI/2;
  if (total <= 0) {{
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2*Math.PI); ctx.arc(cx, cy, r, 0, 2*Math.PI, true);
    ctx.fillStyle = '#2a2c2f'; ctx.fill('evenodd');
    return;
  }}
  segments.forEach(function(seg) {{
    var frac = Math.max(seg.value, 0) / total;
    var a1 = a0 + frac * 2 * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, R, a0, a1);
    ctx.arc(cx, cy, r, a1, a0, true);
    ctx.closePath();
    ctx.fillStyle = seg.color;
    ctx.fill();
    a0 = a1;
  }});
  if (opts && opts.markerValue != null) {{            // a tick across the ring, e.g. the eating pace
    var am = -Math.PI/2 + Math.min(Math.max(opts.markerValue / total, 0), 1) * 2 * Math.PI;
    ctx.beginPath();
    ctx.moveTo(cx + (r - 4) * Math.cos(am), cy + (r - 4) * Math.sin(am));
    ctx.lineTo(cx + (R + 4) * Math.cos(am), cy + (R + 4) * Math.sin(am));
    ctx.strokeStyle = opts.markerColor || '#e8e9ea'; ctx.lineWidth = 2.5; ctx.lineCap = 'round'; ctx.stroke();
  }}
}}

function macroByKey(k) {{
  return (DATA.nutrition && DATA.nutrition.macros || []).filter(function(m) {{ return m.key === k; }})[0];
}}

function renderPaceBar() {{
  var n = DATA.nutrition, host = document.getElementById('pace-bar-host');
  if (!host || !n) return;
  var esc = function(s) {{ return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }};
  var fmt = function(v) {{ return Math.round(v).toLocaleString(); }};
  var meals = n.meals || [], scale = Math.max(n.target, n.consumed * 1.03, 1);
  var pct = function(v) {{ return v / scale * 100; }};
  var acc = 0, segs = '';
  meals.forEach(function(m, i) {{
    var w = pct(m.kcal), left = pct(acc); acc += m.kcal;
    var mk = m.p * 4 + m.c * 4 + m.f * 9;
    var parts = mk > 0
      ? [[m.p * 4, '#88dcfd'], [m.c * 4, '#b79cf0'], [m.f * 9, '#e3b95f']].map(function(x) {{ return '<i style="flex:' + x[0] + ';background:' + x[1] + '"></i>'; }}).join('')
      : '<i style="flex:1;background:#75777a"></i>';
    segs += '<div class="pace-seg" data-i="' + i + '" style="left:' + left + '%;width:calc(' + w + '% - 2px)">' + parts
      + (w > 6 ? '<span>' + esc(m.time) + '</span>' : '') + '</div>';
  }});
  var behind = n.paceExpected - n.consumed;
  var near = Math.abs(behind) < 50;
  var status = near ? 'on pace' : (behind > 0 ? fmt(behind) + ' kcal behind pace' : fmt(-behind) + ' kcal ahead of pace');
  var col = near ? '#6fcf97' : (behind > 0 ? '#e3b95f' : '#b79cf0');
  host.innerHTML = '<div class="pace-top"><span><b>' + fmt(n.consumed) + '</b> of ' + fmt(n.target) + ' kcal</span><span style="color:' + col + '">' + status + '</span></div>'
    + '<div class="pace-bar" id="pace-bar">' + segs
    + '<div class="pace-rest" id="pace-rest" style="left:' + pct(n.consumed) + '%;right:0"></div>'
    + '<div class="pace-marker" id="pace-marker" style="left:' + pct(n.paceExpected) + '%"></div>'
    + '<div class="pace-target" style="left:' + pct(n.target) + '%"></div></div>'
    + '<div class="pace-axis"><span>0</span><span style="left:' + pct(n.paceExpected) + '%">pace ' + fmt(n.paceExpected) + '</span><span class="r">' + fmt(n.target) + ' target</span></div>'
    + '<div class="pace-legend"><span><i style="background:#88dcfd"></i>Protein</span><span><i style="background:#b79cf0"></i>Carbs</span><span><i style="background:#e3b95f"></i>Fat</span><span style="color:#75777a">each block is split by share of its calories</span></div>';
  var bar = document.getElementById('pace-bar'), tip = document.getElementById('pace-tip');
  var show = function(html, e) {{
    tip.innerHTML = html; tip.style.display = 'block';
    var x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 10);
    tip.style.left = x + 'px'; tip.style.top = (e.clientY + 16) + 'px';
  }};
  bar.addEventListener('mousemove', function(e) {{
    var seg = e.target.closest('.pace-seg');
    if (seg) {{
      var m = meals[parseInt(seg.dataset.i, 10)];
      show('<b>' + esc(m.time) + '</b> &middot; ' + esc(m.desc.length > 70 ? m.desc.slice(0, 70) + '…' : m.desc)
        + '<br><b>' + fmt(m.kcal) + ' kcal</b> &middot; P ' + Math.round(m.p) + 'g &middot; C ' + Math.round(m.c) + 'g &middot; F ' + Math.round(m.f) + 'g'
        + ((m.p * 4 + m.c * 4 + m.f * 9) > 0 ? '<br><span style="color:#a3a6a9">calories from protein ' + Math.round(m.p * 4 / (m.p * 4 + m.c * 4 + m.f * 9) * 100) + '% &middot; carbs ' + Math.round(m.c * 4 / (m.p * 4 + m.c * 4 + m.f * 9) * 100) + '% &middot; fat ' + Math.round(m.f * 9 / (m.p * 4 + m.c * 4 + m.f * 9) * 100) + '%</span>' : '')
        + (m.items.length ? '<br><span style="color:#a3a6a9">' + esc(m.items.join(', ')) + '</span>' : '')
        + '<br><span style="color:#75777a">' + Math.round(m.kcal / Math.max(n.consumed, 1) * 100) + '% of what you’ve eaten today</span>', e);
    }} else if (e.target.closest('#pace-marker')) {{
      show('<b>Pace</b>: about ' + fmt(n.paceExpected) + ' kcal by now<br>(' + Math.round(n.dayFrac * 100) + '% through the ' + n.eatStart + ':00–' + n.eatEnd + ':00 eating window)<br>'
        + (near ? 'You are on pace.' : behind > 0 ? 'You are ' + fmt(behind) + ' kcal behind.' : 'You are ' + fmt(-behind) + ' kcal ahead.'), e);
    }} else if (e.target.closest('#pace-rest')) {{
      show('<b>' + fmt(Math.max(n.target - n.consumed, 0)) + ' kcal</b> still to eat for today’s ' + fmt(n.target) + ' kcal target<br><span style="color:#a3a6a9">base ' + fmt(n.base) + (n.sessionKcal ? ' + ' + fmt(n.sessionKcal) + ' training' : '') + '</span>', e);
    }} else {{ tip.style.display = 'none'; }}
  }});
  bar.addEventListener('mouseleave', function() {{ tip.style.display = 'none'; }});
}}

function renderNutritionDash() {{
  var n = DATA.nutrition;
  if (!n) return;
  renderPaceBar();

  // ── Energy ring: eaten vs remaining to target ──
  var remaining = Math.max(n.target - n.consumed, 0);
  drawDonut('c-cal-ring', [
    {{value: n.consumed,  color: '#88dcfd'}},
    {{value: remaining,   color: '#2a2c2f'}}
  ], {{inner: 0.72, markerValue: n.paceExpected, markerColor: '#e8e9ea'}});
  var ringWrap = document.querySelector('.cal-ring-wrap');
  if (ringWrap) ringWrap.title = 'White tick = eating pace: about ' + Math.round(n.paceExpected).toLocaleString() + ' kcal by now';
  var isOver = n.consumed > n.target;
  var mid = document.getElementById('cal-ring-mid');
  if (mid) {{
    mid.innerHTML = '<div class="big" style="color:' + (isOver ? '#e3b95f' : '#e8e9ea') + '">' +
      Math.round(n.consumed).toLocaleString() + '</div>' +
      '<div class="small">of ' + Math.round(n.target).toLocaleString() + ' kcal</div>';
  }}
  var leg = document.getElementById('cal-ring-legend');
  if (leg) {{
    var row = function(k, v) {{ return '<div class="row"><span>' + k + '</span><span>' + v + '</span></div>'; }};
    leg.innerHTML =
      row('Base target', n.base + ' kcal') +
      (n.sessionKcal ? row('Training burn', '+' + n.sessionKcal + ' kcal') : '') +
      row('Eaten', '<b>' + Math.round(n.consumed).toLocaleString() + '</b>') +
      row('Pace tick (by now)', Math.round(n.paceExpected).toLocaleString() + ' kcal') +
      row(isOver ? 'Over by' : 'Remaining',
          '<b>' + Math.abs(n.target - n.consumed).toLocaleString() + ' kcal</b>');
  }}

  // ── Composition pie: share of energy from each macro ──
  var comp = ['protein', 'carbs', 'fat'].map(function(k) {{
    var m = macroByKey(k);
    return {{key: k, label: m.label, color: m.color, kcal: m.g * m.kcalPerG, g: m.g}};
  }});
  var kcalTot = comp.reduce(function(s, c) {{ return s + c.kcal; }}, 0) || 1;
  drawDonut('c-macro-pie', comp.map(function(c) {{ return {{value: c.kcal, color: c.color}}; }}), {{inner: 0.55}});
  var pl = document.getElementById('macro-pie-legend');
  if (pl) {{
    pl.innerHTML = comp.map(function(c) {{
      return '<div class="row"><span class="sw" style="background:' + c.color + '"></span>' +
        c.label + '<span class="gg">' + Math.round(c.g) + 'g</span>' +
        '<span class="pct">' + Math.round(c.kcal / kcalTot * 100) + '%</span></div>';
    }}).join('');
  }}

  // ── Full macro/micro breakdown (was hidden behind a hover) ──
  var mb = document.getElementById('macro-break');
  if (mb) {{
    mb.innerHTML = n.macros.map(function(m) {{
      var unit = m.unit || 'g';
      var pct = m.target ? m.g / m.target * 100 : 0;
      var col = m.kind === 'limit'
        ? (pct > 100 ? '#e8837a' : pct > 75 ? '#e3b95f' : '#6fcf97')
        : (pct >= 100 ? '#6fcf97' : pct >= 75 ? '#e3b95f' : '#e8837a');
      return '<div class="mb-row">' +
        '<span class="mb-name">' + m.label + '</span>' +
        '<span class="mb-track">' +
          '<span class="mb-fill" style="width:' + Math.min(pct, 100) + '%;background:' + m.color + '"></span>' +
        '</span>' +
        '<span class="mb-val"><b>' + Math.round(m.g) + '</b> / ' + Math.round(m.target) + unit +
          '<span class="mb-dot" style="background:' + col + '"></span></span>' +
        '</div>';
    }}).join('');
  }}

  renderBulk();
  renderCalGap();
  drawNutrWeight();
}}

function renderBulk() {{
  var b = DATA.bulk || {{}};
  var goal = b.surplusGoal || 0;
  var slider = document.getElementById('goal-slider');
  if (slider && !slider.dataset.touched) slider.value = goal;
  paintGoalReadout(goal);

  var stats = document.getElementById('bulk-stats');
  if (!stats) return;
  if (b.avg_surplus == null) {{
    stats.innerHTML = '<div class="bulk-stat" style="grid-column:span 2">' +
      '<div class="k">Trend</div><div class="v" style="font-size:12px;color:#75777a">' +
      'Log a few full days to see your rolling surplus</div></div>';
    return;
  }}
  var stateCol = {{'on track':'#6fcf97','slow but positive':'#e3b95f','in deficit':'#e8837a'}}[b.state] || '#a3a6a9';
  var proj = (b.today_projected != null)
    ? '<div class="bulk-stat"><div class="k">Today projected</div><div class="v" style="color:' +
      (b.today_projected >= 0 ? '#6fcf97' : '#e8837a') + '">' +
      (b.today_projected >= 0 ? '+' : '') + b.today_projected + '</div></div>'
    : '';
  stats.innerHTML =
    '<div class="bulk-stat"><div class="k">7-day avg surplus</div><div class="v" style="color:' + stateCol + '">' +
      (b.avg_surplus >= 0 ? '+' : '') + b.avg_surplus + '<span style="font-size:10px;color:#75777a"> kcal/d</span></div></div>' +
    '<div class="bulk-stat"><div class="k">Projected change</div><div class="v" style="color:' + stateCol + '">' +
      (b.kg_per_week >= 0 ? '+' : '') + b.kg_per_week + '<span style="font-size:10px;color:#75777a"> kg/wk</span></div></div>' +
    '<div class="bulk-stat"><div class="k">Status</div><div class="v" style="font-size:13px;color:' + stateCol + '">' +
      b.state + '</div></div>' +
    (proj || '<div class="bulk-stat"><div class="k">Logged days</div><div class="v">' +
      b.logged_days + '/' + b.window + '</div></div>');
}}

function paintGoalReadout(kcalPerDay) {{
  var perKg = (DATA.bulk && DATA.bulk.kcalPerKg) || 6500;
  var kg = kcalPerDay * 7 / perKg;
  var kgEl = document.getElementById('goal-kg'), kcalEl = document.getElementById('goal-kcal');
  if (kgEl) {{
    kgEl.textContent = (kg >= 0 ? '+' : '') + kg.toFixed(2);
    kgEl.style.color = kg > 0.02 ? '#6fcf97' : kg < -0.02 ? '#e8837a' : '#a3a6a9';
  }}
  if (kcalEl) kcalEl.textContent = (kcalPerDay >= 0 ? '+' : '') + kcalPerDay + ' kcal/day';
}}

function onGoalSlider(v) {{
  document.getElementById('goal-slider').dataset.touched = '1';
  paintGoalReadout(parseInt(v, 10));
}}

function commitGoal(v) {{
  document.title = '__setgoal__' + Date.now() + '|' + encodeURIComponent(v);
}}

function drawNutrWeight() {{
  var g = setupCanvas('c-nutr-weight', 'weight-wrap');
  if (!g) return;
  var ctx = g.ctx, W = g.W, H = g.H;
  var dates = DATA.weightDates || [], vals = DATA.weightVals || [];
  var sub = document.getElementById('weight-sub');
  if (dates.length < 2) {{
    if (sub) sub.textContent = 'not enough weigh-ins yet';
    return;
  }}
  var PAD = {{top:10, right:12, bottom:20, left:40}};
  var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  var pad = (hi - lo) * 0.15 || 0.5; lo -= pad; hi += pad;
  var cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  var xOf = function(i) {{ return PAD.left + (i / Math.max(dates.length - 1, 1)) * cW; }};
  var yOf = function(v) {{ return PAD.top + (1 - (v - lo) / (hi - lo)) * cH; }};

  ctx.strokeStyle = '#2a2c2f'; ctx.lineWidth = 1;
  ctx.fillStyle = '#56585a'; ctx.font = '9px system-ui';
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (var i = 0; i <= 3; i++) {{
    var v = lo + (hi - lo) * i / 3, y = Math.round(yOf(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();
    ctx.fillText(v.toFixed(1), PAD.left - 5, y);
  }}
  // linear fit, to read the direction against the goal
  var n = vals.length, sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (var k = 0; k < n; k++) {{ sx += k; sy += vals[k]; sxx += k*k; sxy += k*vals[k]; }}
  var slope = (n*sxy - sx*sy) / (n*sxx - sx*sx || 1), icpt = (sy - slope*sx) / n;
  ctx.strokeStyle = '#424446'; ctx.setLineDash([4,3]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(xOf(0), yOf(icpt)); ctx.lineTo(xOf(n-1), yOf(icpt + slope*(n-1)));
  ctx.stroke(); ctx.setLineDash([]);

  ctx.strokeStyle = '#d3d5d7'; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.beginPath();
  vals.forEach(function(v, i) {{ i ? ctx.lineTo(xOf(i), yOf(v)) : ctx.moveTo(xOf(i), yOf(v)); }});
  ctx.stroke();
  ctx.fillStyle = '#d3d5d7';
  ctx.beginPath(); ctx.arc(xOf(n-1), yOf(vals[n-1]), 3, 0, 7); ctx.fill();

  if (sub) {{
    var perWk = slope * 7;
    sub.textContent = vals[n-1].toFixed(1) + ' kg · ' +
      (perWk >= 0 ? '+' : '') + perWk.toFixed(2) + ' kg/wk over ' + n + ' weigh-ins';
  }}
}}

// Compact nutrition dials for the Today tab — a calorie ring plus P/C/F bars,
// reading from the same DATA.nutrition the Nutrition tab uses.
function renderTodayDials() {{
  var host = document.getElementById('today-dials');
  var n = DATA.nutrition;
  if (!host || !n) return;
  if (n.consumed <= 0) {{
    host.innerHTML = '<div class="brief-label">Nutrition</div>' +
      '<div style="font-size:11.5px;color:#56585a">Nothing logged yet today. ' +
      'Log food from the Nutrition tab.</div>';
    return;
  }}
  host.innerHTML =
    '<div class="brief-label" style="margin:0">Nutrition today</div>' +
    '<div class="dials-top">' +
      '<div class="dial-ring"><canvas id="c-dial-ring"></canvas>' +
        '<div class="dial-ring-mid" id="dial-ring-mid"></div></div>' +
      '<div class="dial-macros" id="dial-macros"></div>' +
    '</div>' +
    '<div class="dial-foot" id="dial-foot"></div>' +
    '<button class="dial-link" onclick="selectTab(\\'nutrition\\')">Full nutrition dashboard →</button>';

  var remaining = Math.max(n.target - n.consumed, 0);
  drawDonut('c-dial-ring', [
    {{value: n.consumed, color: '#88dcfd'}},
    {{value: remaining,  color: '#2a2c2f'}}
  ], {{inner: 0.72}});
  var over = n.consumed > n.target;
  document.getElementById('dial-ring-mid').innerHTML =
    '<div class="big" style="color:' + (over ? '#e3b95f' : '#e8e9ea') + '">' +
    Math.round(n.consumed / n.target * 100) + '%</div><div class="small">of kcal</div>';

  document.getElementById('dial-macros').innerHTML =
    ['protein', 'carbs', 'fat'].map(function(k) {{
      var m = macroByKey(k);
      var pct = m.target ? Math.min(m.g / m.target * 100, 100) : 0;
      return '<div class="dm-row"><span class="dm-name">' + m.label + '</span>' +
        '<span class="dm-track"><span class="dm-fill" style="width:' + pct +
        '%;background:' + m.color + '"></span></span>' +
        '<span class="dm-val">' + Math.round(m.g) + '/' + Math.round(m.target) + 'g</span></div>';
    }}).join('');

  var b = DATA.bulk || {{}};
  var foot = document.getElementById('dial-foot');
  if (b.avg_surplus != null) {{
    var col = {{'on track':'#6fcf97','slow but positive':'#e3b95f','in deficit':'#e8837a'}}[b.state] || '#a3a6a9';
    foot.innerHTML = '<span>7-day surplus <b style="color:' + col + '">' +
      (b.avg_surplus >= 0 ? '+' : '') + b.avg_surplus + '</b> kcal/d</span>' +
      '<span><b style="color:' + col + '">' + (b.kg_per_week >= 0 ? '+' : '') + b.kg_per_week +
      '</b> kg/wk</span>';
  }} else {{
    foot.innerHTML = '<span>Eaten <b>' + Math.round(n.consumed).toLocaleString() +
      '</b> / ' + Math.round(n.target).toLocaleString() + ' kcal</span>';
  }}
}}

/** Behind the pace marker? Show what to eat to catch up. Hidden when on pace. */
function renderCalGap() {{
  var card = document.getElementById('cal-gap-card');
  if (!card) return;
  var gap = DATA.calGapKcal;
  if (!(gap > 50)) {{ card.style.display = 'none'; return; }}
  card.style.display = 'block';
  document.getElementById('cal-gap-amount').textContent =
    Math.round(gap).toLocaleString() + ' kcal behind pace';
  var txt = document.getElementById('cal-gap-text');
  txt.innerHTML = DATA.calGapSuggestion
    ? DATA.calGapSuggestion
    : '<span class="brief-loading">Sizing a snack…</span>';
}}
window.addEventListener('load', renderCalGap);


function triggerRefresh() {{
  var btn = document.getElementById('refresh-btn');
  if (btn) {{ btn.textContent = 'Refreshing…'; btn.disabled = true; }}
  document.title = '__refresh__';
}}

/** "17:30" -> 1050. Plain numbers pass through. */
function parseClock(raw) {{
  var s = String(raw).trim();
  if (s.indexOf(':') < 0) return parseFloat(s);
  var p = s.split(':');
  var mins = parseInt(p[0], 10), secs = parseFloat(p[1]);
  if (isNaN(mins) || isNaN(secs)) return NaN;
  return mins * 60 + secs;
}}

// ── Performance: log / add dialogs ────────────────────────────────────────────
var PM = {{mode: null, tier: 'bank'}};
function pmOpen(mode, html) {{
  PM.mode = mode;
  document.getElementById('pm-dialog').innerHTML = html;
  document.getElementById('pm-overlay').style.display = 'flex';
  var f = document.querySelector('#pm-dialog [data-autofocus]'); if (f) f.focus();
}}
function pmClose() {{ PM.mode = null; var o = document.getElementById('pm-overlay'); if (o) o.style.display = 'none'; }}
document.addEventListener('keydown', function(e) {{
  if (!PM.mode) return;
  if (e.key === 'Escape') pmClose();
  else if (e.key === 'Enter' && e.target.tagName !== 'SELECT' && e.target.tagName !== 'BUTTON') {{
    if (PM.mode === 'log') submitLog(); else if (PM.mode === 'add') submitAddMetric();
  }}
}});
function metricOptions(sel) {{
  return [['key', 'Key metrics'], ['goal', 'Goals'], ['bank', 'Stored']].map(function(g) {{
    var ms = (DATA.metrics || []).filter(function(m) {{ return m.tier === g[0] && m.key !== 'bodyweight'; }});
    if (!ms.length) return '';
    return '<optgroup label="' + g[1] + '">' + ms.map(function(m) {{
      return '<option value="' + escAttr(m.key) + '"' + (m.key === sel ? ' selected' : '') + '>' + escAttr(m.label) + '</option>';
    }}).join('') + '</optgroup>';
  }}).join('');
}}
function openLogModal(key) {{
  var today = new Date().toLocaleDateString('en-CA');
  pmOpen('log',
    '<div class="pm-title">Log a result</div>' +
    '<div class="pm-field"><label>Metric</label><select id="pm-metric" onchange="pmSync()">' +
      '<option value="">Choose a metric…</option>' + metricOptions(key) + '</select>' +
      '<div class="pm-hint" id="pm-hint"></div></div>' +
    '<div class="pm-row">' +
      '<div class="pm-field"><label>Result</label><input id="pm-value" type="text" data-autofocus placeholder="value"></div>' +
      '<div class="pm-field" id="pm-reps-wrap"><label>Reps</label><input id="pm-reps" type="number" min="1" step="1" placeholder="e.g. 8"></div>' +
      '<div class="pm-field"><label>Date</label><input id="pm-date" type="date" value="' + today + '" max="' + today + '"></div>' +
    '</div>' +
    '<div class="pm-field"><label>Notes <span class="pm-opt">optional</span></label><input id="pm-notes" type="text" placeholder="how it felt, conditions…"></div>' +
    '<div class="pm-err" id="pm-err"></div>' +
    '<div class="pm-actions"><button onclick="pmClose()">Cancel</button><button class="btn" onclick="submitLog()">Save result</button></div>');
  pmSync();
}}
function pmSync() {{
  var sel = document.getElementById('pm-metric'); if (!sel) return;
  var m = metricByKey(sel.value);
  var v = document.getElementById('pm-value');
  v.placeholder = m ? (m.is_time ? 'm:ss  (e.g. 6:29)' : (m.unit || 'value')) : 'value';
  document.getElementById('pm-reps-wrap').style.display = (m && m.rep_weighted) ? '' : 'none';
  var h = '';
  if (m) {{
    h = m.has_data
      ? 'Last <b>' + fmtVal(m, m.value) + '</b>' + (m.reps ? ' ×' + m.reps : '') + ' · ' + m.date + (m.pb != null ? ' · best ' + fmtVal(m, m.pb) : '')
      : 'No result yet — the first one sets the baseline.';
    if (m.lower) h += ' · lower is better';
  }}
  document.getElementById('pm-hint').innerHTML = h;
}}
function submitLog() {{
  var key = document.getElementById('pm-metric').value, m = metricByKey(key);
  var raw = document.getElementById('pm-value').value.trim();
  var value = (m && m.is_time) ? parseClock(raw) : parseFloat(raw);
  var err = document.getElementById('pm-err');
  if (!key) {{ err.textContent = 'Choose which metric this is for.'; return; }}
  if (isNaN(value)) {{ err.textContent = (m && m.is_time) ? 'Enter a time like 6:29.' : 'Enter a number.'; return; }}
  var payload = {{metric: key, value: value, notes: document.getElementById('pm-notes').value.trim(),
                  reps: parseInt(document.getElementById('pm-reps').value, 10) || null}};
  var d = document.getElementById('pm-date').value;
  if (d && d !== new Date().toLocaleDateString('en-CA')) payload.date = d;
  document.title = '__logmetric__' + Date.now() + '|' + encodeURIComponent(JSON.stringify(payload));
  pmClose();
  var conf = document.getElementById('log-confirm');
  if (conf) {{ conf.textContent = '✓ logged ' + (m ? m.label : ''); setTimeout(function() {{ conf.textContent = ''; }}, 3000); }}
}}

// ── Performance tracker ───────────────────────────────────────────────────────
function renderTracker() {{
  renderGoals();
  drawRadar();
  renderMetricCards();
}}

// ── Metric config: tiers, edit, add, remove ───────────────────────────────────
function metricCfg(action, key, fields) {{
  var payload = JSON.stringify({{action: action, key: key, fields: fields || null}});
  document.title = '__metriccfg__' + Date.now() + '|' + encodeURIComponent(payload);
}}

function metricError(msg) {{
  var el = document.getElementById('metric-err');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(function() {{ el.style.display = 'none'; }}, 5000);
}}

function setTier(key, tier) {{ metricCfg('update', key, {{tier: tier}}); }}

function escAttr(s) {{
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}}

function metricByKey(key) {{
  return (DATA.metrics || []).filter(function(m) {{ return m.key === key; }})[0];
}}

// Units the athlete can pick from. "s" renders and accepts m:ss, which is how a 5k
// time or a 500m split is actually written down.
var UNIT_PRESETS = [
  ['kg', 'kg — weight'],
  ['lb', 'lb — weight'],
  ['W',  'W — power'],
  ['s',  'time / split (m:ss)'],
  ['m',  'm — distance'],
  ['km', 'km — distance'],
  ['bpm','bpm — heart rate'],
  ['%',  '% — percent'],
  ['',   '(none)']
];

function unitSelect(id, current) {{
  var known = UNIT_PRESETS.some(function(u) {{ return u[0] === current; }});
  var opts = UNIT_PRESETS.map(function(u) {{
    return '<option value="' + escAttr(u[0]) + '"' + (u[0] === current ? ' selected' : '') +
           '>' + u[1] + '</option>';
  }}).join('');
  if (!known && current) {{
    opts += '<option value="' + escAttr(current) + '" selected>' + escAttr(current) + ' — custom</option>';
  }}
  return '<label>Unit<select id="' + id + '">' + opts + '</select></label>';
}}

/** Values in a time unit are typed as m:ss; everything else is a plain number. */
function valInput(id, value, isTime) {{
  var shown = (value == null || value === '') ? ''
            : (isTime ? formatTime(value, 's') : value);
  return '<input id="' + id + '" type="text" inputmode="' + (isTime ? 'text' : 'decimal') +
         '" placeholder="' + (isTime ? 'm:ss' : 'number') + '" value="' + escAttr(shown) + '">';
}}

/** Inline editor, rendered into the card so the value stays next to the field. */
function openMetricEdit(key) {{
  var m = metricByKey(key);
  if (!m) return;
  var host = document.getElementById('edit-' + key);
  if (!host) return;
  if (host.style.display === 'block') {{ host.style.display = 'none'; return; }}
  var t = m.is_time;
  host.style.display = 'block';
  host.innerHTML =
    '<label>Label<input id="ed-label-' + key + '" type="text" value="' + escAttr(m.label) + '"></label>' +
    '<label>Baseline' + valInput('ed-base-' + key, m.start, t) + '</label>' +
    '<label>Target' + valInput('ed-tgt-' + key, m.target, t) + '</label>' +
    '<label>Lifetime PB' + valInput('ed-pb-' + key, m.pb_source === 'lifetime' ? m.pb : null, t) + '</label>' +
    '<label>PB date<input id="ed-pbd-' + key + '" type="text" placeholder="YYYY-MM-DD" value="' +
      escAttr(m.pb_source === 'lifetime' ? (m.pb_date || '') : '') + '"></label>' +
    unitSelect('ed-unit-' + key, m.unit) +
    (m.rep_weighted
      ? '<label>Ref reps<input id="ed-ref-' + key + '" type="number" step="1" value="' + escAttr(m.rep_ref || 8) + '"></label>'
      : '') +
    '<label class="chk"><input id="ed-rw-' + key + '" type="checkbox"' + (m.rep_weighted ? ' checked' : '') +
      '> rep-weighted</label>' +
    '<label class="chk"><input id="ed-lb-' + key + '" type="checkbox"' + (m.lower ? ' checked' : '') +
      '> lower is better</label>' +
    '<div class="metric-form-actions">' +
      '<button class="log-btn" onclick="saveMetricEdit(\\'' + key + '\\')">Save</button>' +
      '<button onclick="openMetricEdit(\\'' + key + '\\')">Cancel</button>' +
      '<button class="danger" onclick="removeMetric(\\'' + key + '\\')">Remove</button>' +
    '</div>';
}}

function saveMetricEdit(key) {{
  var g = function(p) {{ return document.getElementById(p + key); }};
  var refEl = g('ed-ref-');
  metricCfg('update', key, {{
    label:           g('ed-label-').value.trim(),
    baseline:        g('ed-base-').value.trim(),
    target:          g('ed-tgt-').value.trim(),
    lifetime:        g('ed-pb-').value.trim(),
    lifetime_date:   g('ed-pbd-').value.trim(),
    unit:            g('ed-unit-').value,
    rep_ref:         refEl ? refEl.value : null,
    rep_weighted:    g('ed-rw-').checked,
    lower_is_better: g('ed-lb-').checked
  }});
}}

function removeMetric(key) {{
  var m = metricByKey(key);
  var n = m && m.history ? m.history.length : 0;
  var warn = 'Remove "' + (m ? m.label : key) + '" from the tracker?';
  if (n) warn += '\\n\\n' + n + ' logged result' + (n === 1 ? '' : 's') +
                 ' stay on disk — re-adding the key "' + key + '" restores them.';
  if (window.confirm(warn)) metricCfg('remove', key);
}}

function slugify(s) {{
  return s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}}

/** A time unit implies lower-is-better and m:ss entry. */
function addUnitChanged() {{
  var isTime = document.getElementById('am-unit').value === 's';
  ['am-base', 'am-tgt', 'am-pb'].forEach(function(id) {{
    var el = document.getElementById(id);
    if (el) el.placeholder = isTime ? 'm:ss' : 'number';
  }});
  if (isTime) document.getElementById('am-lb').checked = true;
}}

function syncAddKey() {{
  var keyEl = document.getElementById('am-key');
  if (keyEl && !keyEl.dataset.touched) {{
    keyEl.value = slugify(document.getElementById('am-label').value);
  }}
}}

function openAddMetric() {{
  PM.tier = 'bank';
  var tiers = [['goal', 'Goal', 'charted against a target'], ['key', 'Key metric', 'on the profile, fed to the coach'],
               ['bank', 'Stored', 'tracked quietly, e.g. PBs']];
  pmOpen('add',
    '<div class="pm-title">New metric</div>' +
    '<div class="pm-field"><label>Name</label><input id="am-label" type="text" data-autofocus placeholder="e.g. 5k run" oninput="syncAddKey()"></div>' +
    '<div class="pm-row">' + unitSelect('am-unit', '').replace('<label>Unit', '<label>Measured in').replace('<label>', '<div class="pm-field"><label>').replace('</select></label>', '</select></div>') +
      '<div class="pm-field"><label>Better is</label><label class="chk"><input id="am-lb" type="checkbox"> lower (times, splits)</label></div></div>' +
    '<div class="pm-row">' +
      '<div class="pm-field"><label>Baseline <span class="pm-opt">where you are now</span></label><input id="am-base" type="text" placeholder="number"></div>' +
      '<div class="pm-field"><label>Target <span class="pm-opt">where you want to be</span></label><input id="am-tgt" type="text" placeholder="number"></div></div>' +
    '<div class="pm-field"><label>Where should it live?</label><div id="am-tiers">' + tiers.map(function(x) {{
      return '<span class="sl-chip' + (x[0] === PM.tier ? ' on' : '') + '" data-tier="' + x[0] + '" title="' + x[2] + '">' + x[1] + '</span>';
    }}).join('') + '</div><div class="pm-hint" id="am-tier-hint">tracked quietly, e.g. PBs</div></div>' +
    '<details class="pm-more"><summary>More options</summary>' +
      '<div class="pm-row"><div class="pm-field"><label>Lifetime best</label><input id="am-pb" type="text" placeholder="number"></div>' +
      '<div class="pm-field"><label>Best set on</label><input id="am-pbd" type="text" placeholder="YYYY-MM-DD"></div></div>' +
      '<label class="chk"><input id="am-rw" type="checkbox"> compare across rep ranges (weights)</label>' +
      '<div class="pm-field"><label>Key <span class="pm-opt">auto from name</span></label><input id="am-key" type="text" onchange="this.dataset.touched=1"></div>' +
    '</details>' +
    '<div class="pm-err" id="pm-err"></div>' +
    '<div class="pm-actions"><button onclick="pmClose()">Cancel</button><button class="btn" onclick="submitAddMetric()">Add metric</button></div>');
  document.getElementById('am-unit').addEventListener('change', addUnitChanged);
  document.getElementById('am-tiers').addEventListener('click', function(e) {{
    var c = e.target.closest('[data-tier]'); if (!c) return;
    PM.tier = c.dataset.tier;
    Array.prototype.forEach.call(this.children, function(x) {{ x.classList.toggle('on', x === c); }});
    document.getElementById('am-tier-hint').textContent = c.title;
  }});
}}

function submitAddMetric() {{
  var err = document.getElementById('pm-err');
  var label = document.getElementById('am-label').value.trim();
  var key   = document.getElementById('am-key').value.trim() || slugify(label);
  if (!label) {{ err.textContent = 'Give the metric a name.'; return; }}
  if (!/^[a-z0-9_]+$/.test(key)) {{ err.textContent = 'Key must be lowercase letters, digits and underscores.'; return; }}
  metricCfg('add', key, {{
    label:           label,
    unit:            document.getElementById('am-unit').value,
    baseline:        document.getElementById('am-base').value.trim(),
    target:          document.getElementById('am-tgt').value.trim(),
    lifetime:        document.getElementById('am-pb').value.trim(),
    lifetime_date:   document.getElementById('am-pbd').value.trim(),
    tier:            PM.tier,
    lower_is_better: document.getElementById('am-lb').checked,
    rep_weighted:    document.getElementById('am-rw').checked
  }});
  pmClose();
}}

// ── Goal charts ───────────────────────────────────────────────────────────────
function renderGoals() {{
  var host = document.getElementById('goals-section');
  if (!host) return;
  var goals = (DATA.metrics || []).filter(function(m) {{ return m.tier === 'goal'; }});
  if (!goals.length) {{
    host.innerHTML = '<div class="goal-empty">No goals yet — set a metric\\u2019s tier to ' +
                     '<b>Goal</b> to track it here with a full-size progress chart.</div>';
    return;
  }}
  host.innerHTML = goals.map(function(m) {{
    var cur = m.has_data ? (m.lower ? formatTime(m.value, m.unit) : m.value + m.unit) : '—';
    var tgt = m.lower ? formatTime(m.target, m.unit) : m.target + m.unit;
    var pct = Math.round(Math.max(0, Math.min(100, m.journey_pct)));
    return '<div class="goal-card">' +
      '<div class="goal-head">' +
        '<div>' +
          '<div class="goal-label">' + m.label +
            '<span class="tier-pill">Goal</span></div>' +
          '<div class="goal-sub">baseline ' + (m.lower ? formatTime(m.start, m.unit) : m.start + m.unit) +
            ' → target ' + tgt + '</div>' +
        '</div>' +
        '<div class="goal-now"><span style="color:' + m.color + '">' + cur + '</span>' +
          '<span class="goal-pct">' + pct + '%</span></div>' +
      '</div>' +
      '<div class="goal-wrap" id="gw-' + m.key + '"><canvas id="gc-' + m.key + '"></canvas></div>' +
      '<div class="metric-tiers">' + logButton(m) + tierButtons(m) + editButton(m) + '</div>' +
      '<div class="metric-form" id="edit-' + m.key + '" style="display:none"></div>' +
    '</div>';
  }}).join('');
  goals.forEach(function(m) {{ drawGoalChart(m); }});
}}

function drawGoalChart(m) {{
  var g = setupCanvas('gc-' + m.key, 'gw-' + m.key);
  if (!g) return;
  var ctx = g.ctx, W = g.W, H = g.H;
  var hist = m.history || [];
  if (!hist.length) return;

  var PAD = {{top:10, right:12, bottom:20, left:44}};
  var vals = hist.map(function(e) {{ return e.value; }});
  var refs = [m.target, m.start, m.pb].filter(function(v) {{ return v != null; }});
  var lo = Math.min.apply(null, vals.concat(refs));
  var hi = Math.max.apply(null, vals.concat(refs));
  var padv = (hi - lo) * 0.12 || 1;
  lo -= padv; hi += padv;

  var cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  var xOf = function(i) {{ return PAD.left + (i / Math.max(hist.length - 1, 1)) * cW; }};
  // For lower-is-better metrics, flip the axis so "up" always means "better".
  var yOf = function(v) {{
    var f = (v - lo) / (hi - lo);
    return PAD.top + (m.lower ? f : 1 - f) * cH;
  }};

  ctx.strokeStyle = '#2a2c2f'; ctx.lineWidth = 1;
  ctx.fillStyle = '#56585a'; ctx.font = '9px system-ui';
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (var i = 0; i <= 3; i++) {{
    var v = lo + (hi - lo) * i / 3, y = Math.round(yOf(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();
    ctx.fillText(m.lower ? formatTime(v, m.unit) : v.toFixed(0), PAD.left - 6, y);
  }}

  [[m.start, '#56585a', 'baseline'],
   [m.pb_source === 'lifetime' ? m.pb : null, '#b79cf0', 'lifetime PB'],
   [m.target, '#6fcf97', 'target']].forEach(function(t) {{
    if (t[0] == null || t[0] < lo || t[0] > hi) return;
    var y = Math.round(yOf(t[0])) + 0.5;
    ctx.strokeStyle = t[1]; ctx.setLineDash([4,3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = t[1]; ctx.textAlign = 'left'; ctx.font = '9px system-ui';
    ctx.fillText(t[2], PAD.left + 4, y - 6);
    ctx.textAlign = 'right';
  }});

  ctx.strokeStyle = m.color; ctx.lineWidth = 2; ctx.lineJoin = 'round';
  ctx.beginPath();
  vals.forEach(function(v, i) {{ i ? ctx.lineTo(xOf(i), yOf(v)) : ctx.moveTo(xOf(i), yOf(v)); }});
  ctx.stroke();
  vals.forEach(function(v, i) {{
    ctx.fillStyle = i === vals.length - 1 ? m.color : '#424446';
    ctx.beginPath(); ctx.arc(xOf(i), yOf(v), i === vals.length - 1 ? 3.5 : 2, 0, 7); ctx.fill();
  }});

  ctx.fillStyle = '#56585a'; ctx.font = '9px system-ui'; ctx.textBaseline = 'top';
  ctx.textAlign = 'left';  ctx.fillText(hist[0].date, PAD.left, H - PAD.bottom + 5);
  ctx.textAlign = 'right'; ctx.fillText(hist[hist.length-1].date, W - PAD.right, H - PAD.bottom + 5);
}}

function tierButtons(m) {{
  var defs = [['goal','◎','Goal'], ['key','★','Key metric'], ['bank','☆','Stored']];
  return '<div class="tier-btns">' + defs.map(function(d) {{
    var on = m.tier === d[0] ? ' active' : '';
    return '<button class="tier-btn' + on + '" title="' + d[2] +
           '" onclick="setTier(\\'' + m.key + '\\',\\'' + d[0] + '\\')">' + d[1] + '</button>';
  }}).join('') + '</div>';
}}

function logButton(m) {{
  return '<button class="edit-btn" title="Log a result" onclick="openLogModal(&quot;' + m.key + '&quot;)">+</button>';
}}

function editButton(m) {{
  return '<button class="edit-btn" title="Edit target and baseline" ' +
         'onclick="openMetricEdit(\\'' + m.key + '\\')">✎</button>';
}}

function drawRadar() {{
  var canvas = document.getElementById('c-radar');
  if (!canvas) return;
  var metrics = DATA.metrics || [];

  // The n-gon is derived from the metrics themselves: one axis per "key" metric.
  // There is no separate radar_axes list to drift out of sync with the tiers.
  var axes = metrics.filter(function(m) {{ return m.tier === 'key'; }});

  // null = never logged, which is distinct from a logged metric sitting at 0%
  // progress. Collapsing both to 0 made unmeasured axes indistinguishable from
  // measured-but-stalled ones.
  var vals = axes.map(function(m) {{
    return (m.has_data && isFinite(m.journey_pct)) ? m.journey_pct : null;
  }});

  if (axes.length < 3) {{
    var c2 = canvas.getContext('2d');
    c2.setTransform(1,0,0,1,0,0);
    c2.clearRect(0, 0, canvas.width, canvas.height);
    c2.fillStyle = '#56585a';
    c2.font = '12px system-ui';
    c2.textAlign = 'center';
    c2.fillText('Star 3+ key metrics to draw the profile', canvas.width/2, canvas.height/2);
    return;
  }}

  var dpr = window.devicePixelRatio || 1;
  var S = 260;
  canvas.style.width  = S + 'px';
  canvas.style.height = S + 'px';
  canvas.width  = S * dpr;
  canvas.height = S * dpr;
  var ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  var W = S, H = S;
  // Leave room for the axis labels, which sit outside the outermost ring —
  // at r = W/2 - 28 the longer ones ("Row Threshold") ran off the canvas.
  var cx = W/2, cy = H/2, r = W/2 - 52;
  var n = axes.length;

  // Background fill
  ctx.fillStyle = '#1b1c1e';
  ctx.fillRect(0, 0, W, H);

  // Grid rings
  [0.25, 0.5, 0.75, 1.0].forEach(function(t) {{
    ctx.beginPath();
    for (var i=0; i<n; i++) {{
      var a = i * 2*Math.PI/n - Math.PI/2;
      var x = cx + t*r*Math.cos(a), y = cy + t*r*Math.sin(a);
      if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }}
    ctx.closePath();
    ctx.strokeStyle = t===1.0 ? '#424446' : '#2a2c2f';
    ctx.lineWidth = 1; ctx.stroke();
  }});

  // Spokes and labels — dashed/dimmed for axes with no logged data at all,
  // so "unmeasured" reads differently from "measured at 0%".
  for (var i=0; i<n; i++) {{
    var a = i * 2*Math.PI/n - Math.PI/2;
    var hasData = vals[i] !== null;
    ctx.strokeStyle='#2a2c2f'; ctx.lineWidth=1;
    if (!hasData) ctx.setLineDash([2,2]);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a)); ctx.stroke();
    ctx.setLineDash([]);
    var lx = cx + (r+14)*Math.cos(a), ly = cy + (r+14)*Math.sin(a);
    ctx.fillStyle = hasData ? '#75777a' : '#424446';
    ctx.font='9px system-ui'; ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText(axes[i].label + (hasData ? '' : ' (–)'), lx, ly);
  }}

  // Data polygon
  ctx.beginPath();
  for (var i=0; i<n; i++) {{
    var pct = vals[i] !== null ? Math.min(vals[i], 120) / 100 : 0;
    var a = i * 2*Math.PI/n - Math.PI/2;
    var x = cx + pct*r*Math.cos(a), y = cy + pct*r*Math.sin(a);
    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }}
  ctx.closePath();
  ctx.fillStyle='rgba(139,219,250,0.15)'; ctx.fill();
  ctx.strokeStyle='#88dcfd'; ctx.lineWidth=2; ctx.stroke();

  // Target ring (100%)
  ctx.beginPath();
  for (var i=0; i<n; i++) {{
    var a = i * 2*Math.PI/n - Math.PI/2;
    if (i===0) ctx.moveTo(cx+r*Math.cos(a), cy+r*Math.sin(a));
    else ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a));
  }}
  ctx.closePath(); ctx.strokeStyle='rgba(139,219,250,0.35)'; ctx.lineWidth=1.5;
  ctx.setLineDash([3,3]); ctx.stroke(); ctx.setLineDash([]);

  // Dots
  for (var i=0; i<n; i++) {{
    if (vals[i] === null) continue;
    var pct = Math.min(vals[i], 120) / 100;
    var a = i * 2*Math.PI/n - Math.PI/2;
    ctx.beginPath(); ctx.arc(cx+pct*r*Math.cos(a), cy+pct*r*Math.sin(a), 3, 0, 2*Math.PI);
    ctx.fillStyle='#88dcfd'; ctx.fill();
  }}
}}

function fmtVal(m, v) {{ return v == null ? '—' : formatTime(v, m.unit); }}

function pbRow(m) {{
  if (m.pb == null) return '';
  var tag = m.pb_source === 'lifetime' ? 'lifetime PB' : 'PB';
  return '<div class="mc-pb"><span class="pb-tag">' + tag + '</span>' +
         '<span class="pb-val">' + fmtVal(m, m.pb) + '</span>' +
         (m.pb_date ? '<span class="pb-date">' + m.pb_date + '</span>' : '') + '</div>';
}}

function metricCard(m) {{
  var color = m.color || '#88dcfd';

  if (!m.has_data) {{
    return '<div class="metric-card empty">' +
      '<div class="mc-head"><div class="mc-label">' + m.label + '</div>' +
        '<div class="mc-actions">' + logButton(m) + tierButtons(m) + editButton(m) + '</div></div>' +
      pbRow(m) +
      '<div class="mc-none">No result logged yet — the first will set the baseline</div>' +
      '<div class="mc-journey"><span>' + fmtVal(m, m.start) +
        '</span><span>→ ' + fmtVal(m, m.target) + '</span></div>' +
      '<div class="metric-form" id="edit-' + m.key + '" style="display:none"></div>' +
      '</div>';
  }}

  var hasJourney = m.journey_pct != null;
  var jpct    = hasJourney ? Math.max(0, Math.min(100, m.journey_pct)) : 0;
  var jpctCol = jpct >= 66 ? '#6fcf97' : jpct >= 33 ? '#e3b95f' : '#e8837a';
  var repsStr = (m.reps && !m.lower) ? (' <span style="font-size:11px;color:#56585a">×' + m.reps + '</span>') : '';
  var pctStr  = hasJourney
    ? ' <span style="font-size:10px;color:' + jpctCol + '">' + Math.round(jpct) + '%</span>'
    : ' <span style="font-size:10px;color:#56585a">no target</span>';

  return '<div class="metric-card' + (m.tier === 'bank' ? ' stored' : '') + '">' +
    '<div class="mc-head"><div class="mc-label">' + m.label + '</div>' +
      '<div class="mc-actions">' + logButton(m) + tierButtons(m) + editButton(m) + '</div></div>' +
    '<div><span class="mc-val" style="color:' + color + '">' + fmtVal(m, m.value) + '</span>' +
    repsStr + pctStr + '</div>' +
    (hasJourney
      ? '<div class="mc-bar-wrap"><div class="mc-bar-fill" style="width:' + jpct +
        '%;background:' + color + '"></div></div>' +
        '<div class="mc-journey"><span>' + fmtVal(m, m.start) + '</span><span>→ ' +
        fmtVal(m, m.target) + '</span></div>'
      : '') +
    pbRow(m) +
    (m.notes ? '<div class="mc-notes">' + m.notes + '</div>' : '') +
    '<div class="mc-date">Tested ' + m.date + '</div>' +
    renderSparkSVG(m.spark, m.lower, color) +
    '<div class="metric-form" id="edit-' + m.key + '" style="display:none"></div>' +
    '</div>';
}}

function renderMetricCards() {{
  var keyBox  = document.getElementById('tracker-cards');
  var bankBox = document.getElementById('bank-cards');
  if (!keyBox || !bankBox) return;
  var metrics = DATA.metrics || [];

  var keys = metrics.filter(function(m) {{ return m.tier === 'key'; }});
  var bank = metrics.filter(function(m) {{ return m.tier === 'bank'; }});

  keyBox.innerHTML = keys.length
    ? keys.map(metricCard).join('')
    : '<div class="mc-none" style="padding:8px">No key metrics — press ★ on a stored metric to promote it.</div>';
  bankBox.innerHTML = bank.length
    ? bank.map(metricCard).join('')
    : '<div class="mc-none" style="padding:8px">Nothing stored. Use + Add metric for PBs you want on record.</div>';
}}

function formatTime(secs, unit) {{
  if (unit !== 's') return secs + unit;
  var m = Math.floor(secs/60), s = Math.round(secs%60);
  return m + ':' + (s<10?'0':'') + s;
}}

function renderSparkSVG(spark, lowerBetter, color) {{
  if (!spark || spark.length < 2) return '';
  var vals = spark.map(function(e){{return e.value;}});
  var mn = Math.min.apply(null,vals), mx = Math.max.apply(null,vals);
  var range = mx - mn || 1;
  var W = 166, H = 26, pad = 3;
  var pts = vals.map(function(v,i) {{
    var x = pad + (i/(vals.length-1)) * (W-2*pad);
    var frac = lowerBetter ? (mx - v) / range : (v - mn) / range;
    var y = pad + (1 - frac) * (H - 2*pad);
    return x + ',' + y;
  }});
  var trend = lowerBetter ? (vals[0] - vals[vals.length-1]) : (vals[vals.length-1] - vals[0]);
  var trendCol = trend > 0 ? '#6fcf97' : trend < 0 ? '#e8837a' : '#56585a';
  return '<svg class="mc-spark" viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg">' +
    '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round"/>' +
    '<circle cx="' + pts[pts.length-1].split(',')[0] + '" cy="' + pts[pts.length-1].split(',')[1] + '" r="2.5" fill="' + trendCol + '"/>' +
    '</svg>';
}}
</script></body></html>"""


# ── GTK window ───────────────────────────────────────────────────────────────

_ICON_PATH = os.path.expanduser("~/.local/share/icons/training-brief.svg")


class BriefWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Training Dashboard")
        self.set_default_size(WINDOW_W, WINDOW_H)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_resizable(True)
        self.connect("destroy", Gtk.main_quit)
        if os.path.exists(_ICON_PATH):
            try:
                self.set_icon(GdkPixbuf.Pixbuf.new_from_file(_ICON_PATH))
            except Exception:
                pass

        self.wv = WebKit2.WebView()
        self.wv.get_settings().set_enable_javascript(True)
        self.wv.connect("notify::title", self._on_title_changed)
        self.add(self.wv)
        self.show_all()

        self.wv.load_html(LOADING_HTML, "file:///")
        threading.Thread(target=self._fetch_and_render, daemon=True).start()

        # Food entries are processed one at a time by a single worker so
        # concurrent local-model calls never race each other writing food-log.json;
        # the queue lets the UI accept new entries immediately instead of
        # blocking on the ~20s Ollama round trip.
        self._food_queue = queue.Queue()
        # Quiet auto-refresh: rebuild in the background and swap the page only while the user is idle.
        self._last_input = time.monotonic()
        self._last_render = time.monotonic()
        self._render_date = datetime.now().date()
        self._refreshing = False
        self._sig = None
        self._last_probe = 0.0
        self.connect("focus-in-event", self._on_focus_in)
        GLib.timeout_add_seconds(30, self._auto_tick)
        if apphub:
            apphub.listen("dashboard", self._on_hub)
        self._food_queue_depth = 0
        threading.Thread(target=self._food_worker, daemon=True).start()
        for it in load_pending():                         # entries that never finished before the last exit
            log.info("restoring pending food entry %s (%d meals done)", it["id"][:8], it.get("done", 0))
            self._food_queue_depth += 1
            self._food_queue.put((it["id"], it["text"], it.get("day"), it.get("done", 0), 0))

        # Focus check-ins are written by a separate popup process; push new ones in place.
        self._focus_mtime = 0
        if _focus:
            GLib.timeout_add_seconds(20, self._poll_focus)
        if _life:
            GLib.timeout_add_seconds(5, self._poll_life)

    def _push_life(self):
        sessions = []
        try:
            sessions = load_training_plan().get("sessions", [])
        except Exception:
            pass
        payload = json.dumps(_life.life_payload(sessions))
        GLib.idle_add(lambda: self.wv.run_javascript(
            f"DATA.life={payload}; renderLife();", None, None, None) or False)

    def _poll_life(self):
        try:
            m = (os.path.getmtime(_life.TODOS_FILE),
                 os.path.getmtime(_life.EVENTS_FILE) if os.path.exists(_life.EVENTS_FILE) else 0)
        except OSError:
            return True
        if m != getattr(self, "_life_mtime", None):
            first = getattr(self, "_life_mtime", None) is None
            self._life_mtime = m
            if not first:                # initial state is already in the page
                self._push_life()
        return True

    def _poll_focus(self):
        try:
            m = os.path.getmtime(_focus.LOG_FILE)
        except OSError:
            return True
        if m != self._focus_mtime:
            self._focus_mtime = m
            payload = json.dumps(_focus.focus_payload())
            self.wv.run_javascript(
                f"if (window.DATA) {{ DATA.focus={payload}; renderFocus(); }}", None, None, None)
        return True

    def _on_title_changed(self, wv, _):
        t = wv.get_title() or ""
        if t == "__close__":
            Gtk.main_quit()
        elif t == "__refresh__":
            self._refresh()
        elif t.startswith("__note__"):
            import urllib.parse
            _nonce, _, enc = t[len("__note__"):].partition("|")
            text = urllib.parse.unquote(enc)
            add_note(text)
            self._notes_changed()
        elif t.startswith("__clearnotes__"):
            clear_notes()
            self._notes_changed()
        elif t.startswith("__excludeday__"):
            try:
                toggle_excluded_day(t.partition("|")[2])
                threading.Thread(target=self._refresh, daemon=True).start()
            except Exception:
                log.exception("exclude day failed")
        elif t.startswith("__susexplain__"):
            import urllib.parse as _up
            _nonce, _, enc = t.partition("|")
            try:
                ctx = json.loads(_up.unquote(enc) or "{}")
            except ValueError:
                ctx = {}
            threading.Thread(target=self._sustain_explain, args=(ctx,), daemon=True).start()
        elif t.startswith("__sleeprate__"):
            import urllib.parse as _up
            _nonce, _, enc = t.partition("|")
            try:
                _sleep.save_rating(json.loads(_up.unquote(enc)))
                wellness = self._last_data[0] if getattr(self, "_last_data", None) else []
                payload = json.dumps(_sleep.sleep_payload(wellness))
                self.wv.run_javascript(f"DATA.sleep={payload}; renderSleep();", None, None, None)
            except Exception:
                log.exception("sleep rating failed")
        elif t.startswith("__lifeadd__") or t.startswith("__lifeact__"):
            import urllib.parse as _up
            _nonce, _, enc = t.partition("|")
            try:
                obj = json.loads(_up.unquote(enc))
                if t.startswith("__lifeadd__"):
                    _life.add_item(obj)
                elif obj.get("act") == "toggle":
                    _life.toggle_todo(obj["id"])
                elif obj.get("act") == "del":
                    _life.delete_item(obj["id"])
                self._push_life()
            except Exception:
                log.exception("life command failed")
        elif t.startswith("__focusnow__"):
            import subprocess as _sp
            _sp.Popen([sys.executable, os.path.join(os.path.dirname(os.path.realpath(__file__)), "focus.py"),
                       "popup", "--force"])
        elif t.startswith("__logmetric__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__logmetric__"):].partition("|")
            try:
                add_metric_entry(json.loads(_up.unquote(enc)))
                self._push_metrics()
            except Exception:
                log.exception("logmetric failed")
        elif t.startswith("__metriccfg__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__metriccfg__"):].partition("|")
            try:
                payload = json.loads(_up.unquote(enc))
                mutate_metric_config(payload["action"], payload["key"], payload.get("fields"))
                self._push_metrics()
            except Exception as exc:
                log.exception("metriccfg failed")
                msg = json.dumps(str(exc))
                GLib.idle_add(lambda: self.wv.run_javascript(
                    f"metricError({msg});", None, None, None) or False)
        elif t.startswith("__setgoal__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__setgoal__"):].partition("|")
            try:
                kcal = set_calorie_goal(float(_up.unquote(enc)))
                # The bulk trend and pace targets depend on the goal — re-render.
                if getattr(self, "_last_data", None):
                    threading.Thread(target=self._refresh, daemon=True).start()
                else:
                    GLib.idle_add(lambda: self.wv.run_javascript(
                        f"DATA.bulk.surplusGoal={kcal}; renderBulk();", None, None, None) or False)
            except Exception:
                log.exception("setgoal failed")
        elif t.startswith("__logfood__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__logfood__"):].partition("|")
            description = _up.unquote(enc)
            day = None
            m = re.match(r"^(\d{4}-\d{2}-\d{2})::(.*)$", description, re.S)
            if m:
                day, description = m.group(1), m.group(2)
                if day > datetime.now().strftime("%Y-%m-%d"):
                    day = None          # never log into the future
            item_id = uuid.uuid4().hex
            pending_add({"id": item_id, "text": description, "day": day, "done": 0})
            save_food_draft("")                      # it is safely queued, so the box draft is no longer needed
            self._food_queue_depth += 1
            self._food_queue.put((item_id, description, day, 0, 0))
            GLib.idle_add(
                lambda n=self._food_queue_depth: self.wv.run_javascript(
                    f"setFoodQueued({n})", None, None, None) or False)
        elif t.startswith("__openapp__"):
            if apphub:
                apphub.request(t.partition("|")[2])
        elif t.startswith("__active__"):
            self._last_input = time.monotonic()
        elif t.startswith("__fooddraft__"):
            import urllib.parse as _up
            save_food_draft(_up.unquote(t.partition("|")[2]))
        elif t.startswith("__voice__"):
            _nonce, _, rest = t[len("__voice__"):].partition("|")
            target, _, action = rest.partition("|")
            self._voice_input(target, action)
        elif t.startswith("__planweek__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__planweek__"):].partition("|")
            threading.Thread(target=self._plan_week, args=(_up.unquote(enc),), daemon=True).start()
        elif t.startswith("__saveplan__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__saveplan__"):].partition("|")
            try:
                save_week_plan(json.loads(_up.unquote(enc)))
                threading.Thread(target=self._refresh, daemon=True).start()
            except Exception:
                log.exception("save week plan failed")
                GLib.idle_add(lambda: self.wv.run_javascript(
                    "setPlanStatus('Saving the plan failed.', true)", None, None, None) or False)
        elif t.startswith("__warmfood__"):
            threading.Thread(target=warm_local_model, daemon=True).start()
        elif t.startswith("__delfood__"):
            _nonce, _, entry_id = t[len("__delfood__"):].partition("|")
            try:
                delete_food_entry(entry_id)
                self._refresh_nutrition_ui()
            except Exception:
                log.exception("delete food entry failed")
        elif t.startswith("__editfood__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__editfood__"):].partition("|")
            try:
                payload = json.loads(_up.unquote(enc))
                entry_id = payload.pop("id")
                update_food_entry(entry_id, payload)
                self._refresh_nutrition_ui()
            except Exception:
                log.exception("edit food entry failed")

    def _plan_week(self, text):
        """Worker thread: parse the typed/spoken week and hand the draft to the grid."""
        def js(code):
            GLib.idle_add(lambda: self.wv.run_javascript(code, None, None, None) or False)
        try:
            rows = parse_week_plan(text)
        except Exception as e:
            js(f"setPlanStatus({json.dumps(str(e))}, true)")
            return
        if not rows:
            js("setPlanStatus('No sessions found in that — try naming the day for each one.', true)")
            return
        for r in rows:
            r["tss"], _ = plan_session_load(r["minutes"], r["kind"], r["work_minutes"], r["name"])
        js(f"showPlanDraft({json.dumps(rows)}, {json.dumps(plan_lifting_conflicts(rows))})")

    def _voice_input(self, target, action):
        """Mic buttons: "start" opens the stream, "stop" transcribes off the GTK
        thread and hands the text to that target's box (food or plan). One recorder
        is shared, so only one mic can be live at a time. Runs on the main thread."""
        def js(code):
            GLib.idle_add(lambda: self.wv.run_javascript(code, None, None, None) or False)

        if target not in ("food", "plan"):
            return
        if not hasattr(self, "_voice"):
            self._voice = VoiceFoodRecorder()
        if action == "start" and not self._voice.recording:
            try:
                self._voice.start(on_timeout=lambda: self._voice_input(target, "stop"))
            except Exception as e:
                log.exception("voice food: mic start failed")
                js(f"setVoiceState('{target}', 'idle'); voiceError('{target}', {json.dumps(f'Microphone unavailable: {e}')})")
                return
            # Qwen loads while he talks, so the parse after Enter/Plan isn't a cold start.
            threading.Thread(target=warm_local_model, daemon=True).start()
            js(f"setVoiceState('{target}', 'recording', 'Listening… press ■ when done')")
        elif action == "stop" and self._voice.recording:
            audio = self._voice.stop()
            js(f"setVoiceState('{target}', 'transcribing', 'Transcribing…')")

            def work():
                try:
                    text = self._voice.transcribe(
                        audio, _voice_food_prompt() if target == "food" else VOICE_PLAN_PROMPT)
                except Exception:
                    log.exception("voice food: transcription failed")
                    js(f"setVoiceState('{target}', 'idle'); voiceError('{target}', 'Transcription failed — try again.')")
                    return
                log.info("voice transcript (%s): %s", target, text[:120])
                if text:
                    if target == "food":
                        save_food_draft(text)        # survives a reload/restart until it is submitted
                    js(f"setVoiceTranscript('{target}', {json.dumps(text)})")
                else:
                    js(f"setVoiceState('{target}', 'idle'); voiceError('{target}', 'Didn\\'t catch anything — try again.')")
            threading.Thread(target=work, daemon=True).start()

    def _push_metrics(self):
        """Rebuild the metrics payload from disk and re-render the tracker in place."""
        benchmark_cfg = load_benchmark_config()
        wellness = self._last_data[0] if getattr(self, "_last_data", None) else []
        weight_history = [{"date": w["id"], "value": w["weight"]}
                          for w in wellness if w.get("weight") and w.get("id")]
        new_metrics = build_metrics_data(load_test_metrics().get("entries", []),
                                         benchmark_cfg["metrics"],
                                         weight_history=weight_history)
        metrics_json = json.dumps(new_metrics)
        GLib.idle_add(lambda: self.wv.run_javascript(
            f"DATA.metrics={metrics_json}; renderTracker();", None, None, None) or False)

    def _food_worker(self):
        """Runs for the lifetime of the app on its own thread: pulls queued food
        descriptions one at a time and delegates each to the local model in turn,
        so entries are never processed concurrently (which would race writing
        food-log.json) while the UI stays free to accept more entries meanwhile."""
        while True:
            item_id, description, day, done, attempt = self._food_queue.get()
            retry = False
            try:
                meals = split_meals(description)
                if len(meals) > 1:
                    log.info("food entry split into %d meals", len(meals))
                ok, err = True, None
                for idx, (meal, time_hint) in enumerate(meals):
                    if idx < done:
                        continue                         # logged before a restart/retry
                    m_ok, m_err = log_food_via_qwen(meal, time_hint, day)
                    if not m_ok:
                        ok, err = False, m_err
                        break
                    done = idx + 1
                    pending_update(item_id, done=done)
                    if len(meals) > 1:
                        self._refresh_nutrition_ui()     # each meal appears as it lands
            except Exception:
                log.exception("food log failed")
                ok, err = False, "Failed to log food — try again."
            rejected = (not ok) and (err or "").startswith(("That doesn't look like a food", "Couldn't match any food"))
            if ok or rejected:
                pending_remove(item_id)                  # finished, or deliberately not a food
            elif attempt < 3:
                retry = True                             # model/Ollama trouble: try again, keep it pending
            self._food_queue_depth = max(0, self._food_queue_depth - (0 if retry else 1))
            if retry:
                log.warning("food entry %s will retry (attempt %d): %s", item_id[:8], attempt + 1, err)
                threading.Timer(45, lambda i=(item_id, description, day, done, attempt + 1): self._food_queue.put(i)).start()
            elif not ok:
                msg = (err or "Failed to log food — try again.").replace("\\", "\\\\").replace("'", "\\'")
                if not rejected:
                    msg += " (kept — it will be retried when the app next starts)"
                GLib.idle_add(
                    lambda m=msg: self.wv.run_javascript(f"setFoodError('{m}')", None, None, None) or False)
            else:
                self._refresh_nutrition_ui()
                GLib.idle_add(
                    lambda n=self._food_queue_depth: self.wv.run_javascript(
                        f"setFoodQueued({n})", None, None, None) or False)
            self._food_queue.task_done()

    def _refresh_nutrition_ui(self):
        """After a food quick-add/edit/delete: refresh the logged-today list and
        re-render the nutrition dashboard + Today dials from the recomputed macro
        data, all in place, so the user never has to hit refresh. Callers may run
        on the GTK main thread (edit/delete) or a worker thread (quick-add), so
        everything here must be either instant or pushed to its own thread —
        nothing may block synchronously on an LLM call."""
        ctx = getattr(self, "_nutr_ctx", None)
        if not ctx:
            return
        food_data = get_today_nutrition()
        log_html  = render_food_log_list(food_data).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
        week_html = render_weekly_food_log().replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
        nutr_json = json.dumps(nutrition_data(food_data, ctx))
        GLib.idle_add(
            lambda: self.wv.run_javascript(
                f"setFoodLogList('{log_html}'); setWeeklyLog('{week_html}'); "
                f"DATA.nutrition={nutr_json}; "
                f"renderNutritionDash(); renderTodayDials();",
                None, None, None) or False)
        # Gap may have closed (or changed) — refresh the chart-hover suggestion too.
        # This calls the local model, so it must never run on the GTK main thread.
        def _refresh_gap():
            try:
                suggestion = get_calorie_gap_suggestion(food_data, ctx)
                js_val = ("'" + suggestion.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "&#10;") + "'") if suggestion else "null"
                gap = round(ctx["expected_so_far"] - (food_data or {}).get("calories", 0))
                GLib.idle_add(
                    lambda: self.wv.run_javascript(
                        f"DATA.calGapSuggestion={js_val}; DATA.calGapKcal={gap}; renderCalGap();",
                        None, None, None) or False)
            except Exception:
                log.exception("calorie gap suggestion refresh failed")
        threading.Thread(target=_refresh_gap, daemon=True).start()

    def _notes_changed(self):
        """Refresh the notes list in the UI and reprocess all briefs with the new context."""
        notes_html = render_notes_html().replace("\\", "\\\\").replace("'", "\\'")
        GLib.idle_add(
            lambda: self.wv.run_javascript(
                f"setNotes('{notes_html}')", None, None, None) or False)
        if getattr(self, "_last_data", None):
            wellness, activities, training_plan, food_data, illness = self._last_data
            # Show the briefs regenerating, then re-run the LLM with note context included
            GLib.idle_add(
                lambda: self.wv.run_javascript(
                    "setCoachBrief('Regenerating…', '');"
                    "setNutritionCoach('Regenerating…');", None, None, None) or False)
            threading.Thread(
                target=self._fetch_claude,
                args=(wellness, activities, training_plan, food_data, illness),
                daemon=True).start()

    def _refresh(self):
        # force_illness_full=False (default) lets get_illness_data's own cache logic decide:
        # instant cache hit if already run today, warm-start (~5s) if the day has rolled
        # over, full DE re-run (~30s) only on its own weekly schedule. A manual refresh
        # shouldn't force the expensive full re-run every time — only things that can
        # actually change within a day (food log, activities, pace) need a full rebuild.
        self.wv.load_html(LOADING_HTML, "file:///")
        threading.Thread(target=self._fetch_and_render, daemon=True).start()

    def _status(self, msg):
        # Update the loading screen status text via JS (safe from any thread via idle_add)
        escaped = msg.replace("'", "\\'")
        GLib.idle_add(
            lambda: self.wv.run_javascript(
                f"var el=document.getElementById('status');if(el)el.textContent='{escaped}';",
                None, None, None) or False
        )

    def _data_signature(self):
        """Cheap fingerprint of the newest intervals.icu data (last 2 days of wellness, last 3 of activities)."""
        cfg = load_config()
        auth = ("API_KEY", cfg["api_key"])
        base = f"{BASE_URL}/athlete/{cfg['athlete_id']}"
        today = datetime.now()
        w = requests.get(f"{base}/wellness", params={"oldest": (today - timedelta(days=2)).strftime("%Y-%m-%d"),
                         "newest": today.strftime("%Y-%m-%d")}, auth=auth, timeout=8).json()
        a = requests.get(f"{base}/activities", params={"oldest": (today - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00"),
                         "newest": today.strftime("%Y-%m-%dT23:59:59")}, auth=auth, timeout=8).json()
        keep = ("id", "ctl", "atl", "hrv", "restingHR", "sleepSecs", "sleepScore", "weight", "steps", "updated")
        return hashlib.md5(json.dumps([[{k: x.get(k) for k in keep} for x in w],
                                       [(x.get("id"), x.get("icu_training_load"), x.get("moving_time")) for x in a]],
                                      sort_keys=True, default=str).encode()).hexdigest()

    def _probe_worker(self):
        try:
            sig = self._data_signature()
        except Exception as e:
            log.warning("auto-refresh probe failed: %s", e)
            return
        if self._sig is not None and sig != self._sig and not self._refreshing and time.monotonic() - self._last_input >= 20:
            log.info("new intervals.icu data: refreshing")
            self._refreshing = True
            self._fetch_and_render(quiet=True)
        self._sig = sig

    def _auto_tick(self):
        now = time.monotonic()
        if self._refreshing or now - self._last_input < 60:          # never while the user is active/typing
            return True
        if datetime.now().date() != self._render_date or now - self._last_render > 1800:
            self._refreshing = True
            threading.Thread(target=self._fetch_and_render, kwargs={"quiet": True}, daemon=True).start()
        elif now - self._last_probe > 120:
            self._last_probe = now
            threading.Thread(target=self._probe_worker, daemon=True).start()
        return True

    def _on_hub(self, m):
        """Another app asked for this one (and maybe a particular tab)."""
        self.present()
        if m.get("tab"):
            self.wv.run_javascript(f"goTo({json.dumps(m['tab'])}, {json.dumps(m.get('anchor') or '')})", None, None, None)

    def _on_focus_in(self, *_):
        """Coming back to the window after a while: bring it up to date."""
        if not self._refreshing and time.monotonic() - self._last_render > 180:
            self._refreshing = True
            threading.Thread(target=self._fetch_and_render, kwargs={"quiet": True}, daemon=True).start()
        return False

    def _load(self, html):
        self._last_render = time.monotonic()
        self._render_date = datetime.now().date()
        self._refreshing = False
        GLib.idle_add(lambda: self.wv.load_html(html, "file:///") or False)

    def _fetch_and_render(self, force_illness_full=False, quiet=False):
        config     = load_config()
        athlete_id = config["athlete_id"]
        api_key    = config["api_key"]

        try:
            wellness = fetch_with_retry(
                lambda: fetch_wellness(athlete_id, api_key),
                self._status, max_attempts=4)
            activities = fetch_with_retry(
                lambda: fetch_activities(athlete_id, api_key),
                self._status, max_attempts=4)
        except Exception as e:
            if quiet:                                   # a failed background refresh keeps the current page
                log.warning("auto-refresh failed: %s", e)
                self._refreshing = False
                return
            self._load(error_html(str(e)))
            return

        training_plan   = load_training_plan()
        calorie_target  = config.get("calorie_baseline", 2700)
        surplus_target  = config.get("calorie_surplus_target", 0)
        food_data       = get_today_nutrition()
        food_data["_calorie_target"] = calorie_target

        # Grab cached illness data synchronously (instant — no model run)
        illness = get_cached_result() if _ILLNESS_AVAILABLE else None

        # Zone settings change maybe twice a year, so a failure here just means the
        # brief gives no bpm figures that day — not a reason to fail the whole render.
        try:
            self._sport_settings = fetch_sport_settings(athlete_id, api_key)
        except Exception:
            log.warning("sport-settings fetch failed; HR advice will omit zones",
                        exc_info=True)
            self._sport_settings = getattr(self, "_sport_settings", None)

        # Stash so briefs can be reprocessed (e.g. after a note) without re-fetching
        self._last_data = (wellness, activities, training_plan, food_data, illness)
        # Stash so a food-log quick-add can refresh #nutr-body without a full re-fetch
        self._nutr_ctx = compute_nutrition_context(activities, calorie_target)

        # Load charts immediately — coach brief and illness model fill in asynchronously
        self._load(build_html(wellness, activities, training_plan,
                               summary=None, calorie_target=calorie_target,
                               food_data=food_data, illness=illness,
                               surplus_target=surplus_target))

        threading.Thread(
            target=self._fetch_claude,
            args=(wellness, activities, training_plan, food_data, illness),
            daemon=True).start()

        if _ILLNESS_AVAILABLE:
            threading.Thread(
                target=self._fetch_illness,
                args=(wellness, force_illness_full),
                daemon=True).start()

    def _fetch_claude(self, wellness, activities, training_plan, food_data=None, illness=None):
        time.sleep(0.8)
        import re
        def md_to_html(s):
            """Convert **bold** and *italic* to HTML, then escape for JS string."""
            s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
            s = re.sub(r'\*(.+?)\*',     r'<em>\1</em>',         s)
            return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "<br>")
        def strip_md(s):
            """Strip markdown markers for plain-text contexts (tooltips)."""
            s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
            s = re.sub(r'\*(.+?)\*',     r'\1', s)
            return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "&#10;")
        esc     = md_to_html
        esc_tip = strip_md

        def _js(code):
            GLib.idle_add(lambda c=code: self.wv.run_javascript(c, None, None, None) or False)

        # Critical path: coach brief, then the nutrition coach (which uses it as context)
        def coach_worker():
            coach_summary = None
            try:
                coach_summary, _ = get_training_summary(
                    build_brief_facts(wellness, activities, training_plan, illness=illness),
                    build_tips_context(wellness, training_plan,
                                       sport_settings=getattr(self, '_sport_settings', None)))
                _js(f"setCoachBrief('{esc(coach_summary.get('overview', ''))}', "
                    f"'{esc(coach_summary.get('tips', ''))}')")
            except Exception:
                log.exception("coach brief failed")
                _js("setCoachBrief('Brief unavailable.', '')")
            try:
                nutr_brief, _ = get_nutrition_coach(activities, training_plan,
                                                    coach_summary=coach_summary)
                if nutr_brief:
                    _js(f"setNutritionCoach('{esc(nutr_brief)}')")
            except Exception:
                log.exception("nutrition coach brief failed")
                _js("setNutritionCoach('Nutrition brief unavailable.')")

        def insight_worker():
            try:
                insight, _ = get_nutrition_insight()
                if insight:
                    _js(f"setNutritionInsight('{esc_tip(insight)}')")
            except Exception:
                log.exception("nutrition insight failed")

        # Feeds both the calorie-chart hover tooltip and the Nutrition tab's
        # catch-up card: if today is behind the eating pace, precompute a haiku
        # meal suggestion so hovering is instant rather than firing a network
        # call per mousemove.
        def gap_worker():
            try:
                ctx = getattr(self, "_nutr_ctx", None)
                if not ctx:
                    return
                suggestion = get_calorie_gap_suggestion(food_data, ctx)
                if suggestion:
                    _js(f"DATA.calGapSuggestion='{esc_tip(suggestion)}'; renderCalGap()")
            except Exception:
                log.exception("calorie gap suggestion failed")

        # Fire the independent LLM calls on separate threads. NOTE: with the local
        # Ollama model this no longer buys real parallelism the way concurrent
        # claude -p API calls did — Ollama serializes requests against one CPU-bound
        # model instance, so wall time here is closer to the sum of all 4 calls than
        # the max. Kept threaded so the UI thread isn't blocked either way.
        # Sessions are no longer pulled out of free-text notes: the Plan box is the
        # one way into the plan, and doing both double-booked days under different names.
        workers = [threading.Thread(target=w, daemon=True)
                   for w in (coach_worker, insight_worker, gap_worker)]
        for w in workers: w.start()
        for w in workers: w.join()

    def _sustain_explain(self, ctx):
        """Qwen rewords model-code facts for the Load tab; numbers are verified, else fall back."""
        text, src = "", "fallback"
        try:
            wellness, activities, plan, _food, illness = self._last_data
            ill = (plan or {}).get("illness")
            p = _sustain.sustain_payload(wellness, activities, illness,
                                         illness_paused=bool(ill and not ill.get("cleared")))
            if p.get("ok"):
                facts = _sustain.explain_facts(p, ctx)
                text = " ".join(facts)
                try:
                    raw = _ollama_p(_sustain.explain_prompt(facts), timeout=300, source="sustain_explain")
                    checked = _sustain.verify_explanation(raw, facts, ctx.get("mode") or p["mode0"])
                    if len(checked.split()) >= 25:
                        text, src = checked, "qwen"
                except Exception:
                    log.warning("sustain explain: qwen failed, using fallback", exc_info=True)
        except Exception:
            log.exception("sustain explain failed")
        GLib.idle_add(lambda: self.wv.run_javascript(
            f"susExplainDone({json.dumps(text)}, {json.dumps(src)})", None, None, None) or False)

    def _fetch_illness(self, wellness, force_full=False):
        # Lower this thread's OS scheduling priority — the GP/HMM fit (esp. the weekly
        # full DE re-run) is CPU-heavy for tens of seconds and was starving the coach
        # brief / nutrition coach threads of CPU time on the same run. Deprioritizing
        # just this thread (per-thread nice on Linux, via its native TID) keeps the
        # illness computation from delaying the more time-sensitive coach prompts.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
        except (AttributeError, OSError):
            pass
        try:
            result = get_illness_data(wellness, force_full=force_full)
        except Exception:
            return
        if result.today is None:
            # No wellness data for today yet (e.g. wearable hasn't synced) — tell the
            # UI explicitly rather than letting it fall back to a misleading "0%".
            GLib.idle_add(lambda: self.wv.run_javascript(
                "setIllnessPending()", None, None, None) or False)
            return
        today_dt    = datetime.now()
        spark_dates = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        spark_vals  = [round(result.by_date.get(d, 0.0), 4) for d in spark_dates]
        today_p     = float(result.today)
        yesterday_p = float(result.yesterday) if result.yesterday is not None else today_p
        bands_json  = json.dumps(result.bands)
        spark_json  = json.dumps({"dates": spark_dates, "vals": spark_vals})
        GLib.idle_add(
            lambda: self.wv.run_javascript(
                f"setIllnessData({today_p:.4f}, {yesterday_p:.4f}, {bands_json}, {spark_json})",
                None, None, None) or False)


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    GLib.set_prgname("training-brief")  # dock matches this to training-brief.desktop
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("training-brief is already running", file=sys.stderr)
        sys.exit(0)
    lock_fh.write(str(os.getpid()))
    lock_fh.flush()

    BriefWindow()
    Gtk.main()


if __name__ == "__main__":
    main()
