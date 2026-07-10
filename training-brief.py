#!/usr/bin/env python3
"""Morning training brief — loading screen, retries, charts, coach brief."""

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timedelta, date

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, WebKit2, GLib, GdkPixbuf

import requests

try:
    from illness_model import get_illness_data, get_cached_result
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
STRETCH_LOG_FILE   = os.path.expanduser("~/.local/share/training-brief/stretch-log.json")
NOTES_LOG_FILE     = os.path.expanduser("~/.local/share/training-brief/notes-log.json")
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
body{background:#0f172a;display:flex;align-items:center;justify-content:center;
     height:100vh;font-family:-apple-system,'Segoe UI',sans-serif}
.wrap{text-align:center}
.spinner{width:38px;height:38px;border:3px solid #1e293b;border-top-color:#22d3ee;
         border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 16px}
@keyframes spin{to{transform:rotate(360deg)}}
.msg{color:#475569;font-size:13px;margin-bottom:6px}
.sub{color:#334155;font-size:11px;min-height:16px}
</style></head><body>
<div class="wrap">
  <div class="spinner"></div>
  <div class="msg">Fetching training data…</div>
  <div class="sub" id="status"></div>
</div></body></html>"""


def error_html(msg):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f172a;display:flex;align-items:center;justify-content:center;
     height:100vh;font-family:-apple-system,'Segoe UI',sans-serif}}
.wrap{{text-align:center;max-width:420px;padding:24px}}
.icon{{font-size:32px;margin-bottom:16px}}
.msg{{color:#f87171;font-size:13px;margin-bottom:8px;font-weight:600}}
.detail{{color:#475569;font-size:11px;line-height:1.6;margin-bottom:20px}}
button{{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:6px;
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


def epley_1rm(weight, reps):
    return round(weight * (1 + reps / 30), 1) if reps else weight


def latest_metric(entries, metric):
    hits = [e for e in entries if e.get("metric") == metric]
    return hits[-1] if hits else None


def metric_history(entries, metric):
    return [e for e in entries if e.get("metric") == metric]


# Benchmark metrics + radar groupings are per-user, not per-checkout — every
# athlete tracks different lifts/tests. Real config lives in BENCHMARKS_FILE
# (gitignored); this is only the placeholder seeded for a fresh install, and
# intentionally sport-agnostic rather than shaped around any one person's plan.
DEFAULT_BENCHMARKS = {
    "metrics": {
        "back_squat": {"label": "Back squat (working set)", "target": 100, "unit": "kg", "color": "#a78bfa"},
        "row_2k":     {"label": "2k row",                   "target": 420, "unit": "s",  "color": "#f59e0b", "lower_is_better": True},
        "bodyweight": {"label": "Body weight",              "target": 80,  "unit": "kg", "color": "#94a3b8"},
    },
    "radar_axes": [
        {"label": "Strength",  "keys": ["back_squat"]},
        {"label": "Endurance", "keys": ["row_2k"]},
        {"label": "Body",      "keys": ["bodyweight"]},
    ],
}


def load_benchmark_config():
    """Load the user's benchmark metrics + radar groupings, seeding a
    placeholder file on first run (mirrors load_training_plan)."""
    if not os.path.exists(BENCHMARKS_FILE):
        os.makedirs(os.path.dirname(BENCHMARKS_FILE), exist_ok=True)
        with open(BENCHMARKS_FILE, "w") as f:
            json.dump(DEFAULT_BENCHMARKS, f, indent=2)
        return DEFAULT_BENCHMARKS
    with open(BENCHMARKS_FILE) as f:
        cfg = json.load(f)
    cfg.setdefault("metrics", {})
    cfg.setdefault("radar_axes", [])
    return cfg


def add_metric_entry(entry_dict):
    """Append a new test result to METRICS_FILE."""
    from datetime import date as _date
    data = load_test_metrics()
    new_entry = {"date": _date.today().isoformat(), "metric": entry_dict["metric"],
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
        if not history:
            continue
        latest  = history[-1]
        first   = history[0]
        raw     = latest["value"]
        reps    = latest.get("reps")
        tgt     = meta["target"]
        lower   = meta.get("lower_is_better", False)
        # An explicit "baseline" overrides the first logged value — useful
        # when history predates a target change (e.g. bulk goal reset).
        start   = meta.get("baseline", first["value"])
        # Progress bar: fraction of journey from baseline to target completed
        gap = (start - tgt) if lower else (tgt - start)
        moved = (start - raw) if lower else (raw - start)
        journey_pct = (moved / gap * 100) if gap else 100
        journey_pct = max(0, min(100, journey_pct))
        # Subtitle: reps + notes
        notes = latest.get("notes", "")
        subtitle = (f"×{reps} · {notes}" if reps and notes
                    else f"×{reps}" if reps else notes)
        # Historical sparkline
        spark = [{"date": e["date"], "value": e["value"]} for e in history[-12:]]
        result.append({
            "key":          key,
            "label":        meta["label"],
            "value":        raw,
            "reps":         reps,
            "start":        start,
            "target":       tgt,
            "unit":         meta["unit"],
            "color":        meta["color"],
            "journey_pct":  round(journey_pct, 1),
            "date":         latest["date"],
            "notes":        subtitle,
            "lower":        lower,
            "spark":        spark,
        })
    return result


# ── Food log ─────────────────────────────────────────────────────────────────

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


# ── Food log — quick-add via claude -p ──────────────────────────────────────

def log_food_via_claude(description):
    """Append one meal entry to today's food-log.json by delegating to a
    sandboxed `claude -p` call, restricted to Read/Edit and to the food-log's
    own directory so it cannot touch anything else on the machine.
    Returns (ok, error_message)."""
    today = datetime.now().strftime("%Y-%m-%d")
    now_t = datetime.now().strftime("%H:%M")
    food_log_dir = os.path.dirname(FOOD_LOG_FILE)
    prompt = (
        f'Append ONE new meal entry to {FOOD_LOG_FILE} under the date key "{today}" '
        f'(create the key, or the file, if missing). Do not modify any existing '
        f'entries or other dates.\n\n'
        'Schema for each entry in the date\'s list:\n'
        '{"id": "<uuid4>", "time": "HH:MM", "description": "<free text summary>", '
        '"items": [{"name": "...", "calories": N, "protein_g": N, "carbs_g": N, '
        '"fat_g": N, "fiber_g": N, "sugar_g": N, "sodium_mg": N}], '
        '"calories": <sum of items>, "protein_g": <sum>, "carbs_g": <sum>, '
        '"fat_g": <sum>, "fiber_g": <sum>, "sugar_g": <sum>, "sodium_mg": <sum>}\n\n'
        f'Use time "{now_t}" unless the description implies otherwise. Split into one '
        'item per distinct food, estimate realistic macros with standard nutrition '
        'knowledge (pasta/rice/oats quantities are dry weight unless said to be cooked), '
        'and make sure entry-level totals equal the sum of items.\n\n'
        f'Food description: "{description}"\n\n'
        'Only edit this one file. Do not print anything or ask questions — just make the edit.'
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt,
             "--model", "claude-haiku-4-5-20251001",
             "--tools", "Read,Edit",
             "--add-dir", food_log_dir,
             "--permission-mode", "acceptEdits",
             "--no-session-persistence",
             "--strict-mcp-config"],
            cwd=food_log_dir, capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        log.error("food log claude call timed out")
        return False, "Timed out logging food — try again."
    if result.returncode != 0:
        log.error("food log claude call failed (%d): %s", result.returncode, result.stderr[-2000:])
        return False, "Failed to log food — try again."
    log.info("food logged via claude -p: %s", description[:80])
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


def render_nutrition_body(food_data, ctx):
    """Renders the dynamic contents of #nutr-body: calorie bar, macro rows, the
    today's-meals dropdown and the quick-add input. Pure function of
    (food_data, ctx) — called both on initial page build and after a food entry
    is logged, so #nutr-body can be refreshed in place without disturbing the
    click/tooltip listener bound to the outer #nutr-section."""
    import html as _html

    kcal_total           = ctx["kcal_total"]
    expected_so_far      = ctx["expected_so_far"]
    day_frac             = ctx["day_frac"]
    training_hours_today = ctx["training_hours_today"]

    protein_target_g = ctx["protein_target_g"]
    carbs_target_g   = ctx["carbs_target_g"]
    fat_target_g     = ctx["fat_target_g"]
    fiber_target_g   = ctx["fiber_target_g"]
    sugar_target_g   = ctx["sugar_target_g"]
    sodium_target_mg = ctx["sodium_target_mg"]

    entries = (food_data or {}).get("entries", [])
    if entries:
        rows = "".join(
            '<div class="food-entry-row">'
            f'<span class="food-entry-time">{_html.escape(e.get("time", ""))}</span>'
            f'<span class="food-entry-desc">{_html.escape(e.get("description") or "")}</span>'
            f'<span class="food-entry-kcal">{e.get("calories", 0):.0f} kcal</span>'
            '</div>'
            for e in entries
        )
        food_list_html = (
            '<details class="food-list" onclick="event.stopPropagation()">'
            f'<summary>{len(entries)} meal{"s" if len(entries) != 1 else ""} logged &#9662;</summary>'
            f'<div class="food-entry-list">{rows}</div>'
            '</details>'
        )
    else:
        food_list_html = ''

    food_input_html = (
        '<div class="food-input-row" onclick="event.stopPropagation()">'
        '<input type="text" id="food-box" placeholder="Log food… (Enter to add)" '
        '''onkeydown="if(event.key==='Enter'){event.preventDefault();submitFood();}">'''
        '<button id="food-add-btn" onclick="submitFood()">Add</button>'
        '</div>'
        '<div class="food-status" id="food-status" style="display:none"></div>'
    )

    if food_data and food_data.get("entry_count", 0) > 0:
        consumed_kcal = int(food_data["calories"])
        cal_pct       = min(round(consumed_kcal / max(kcal_total, 1) * 100), 100)
        pace_pct      = consumed_kcal / max(expected_so_far, 1) * 100
        if day_frac <= 0.02:    cal_color = "#3b82f6"   # too early in the window to judge pace
        elif pace_pct >= 100:   cal_color = "#22c55e"
        elif pace_pct >= 85:    cal_color = "#fbbf24"
        else:                   cal_color = "#f87171"

        p_g  = food_data["protein_g"]
        c_g  = food_data["carbs_g"]
        fa_g = food_data["fat_g"]
        fi_g  = food_data.get("fiber_g",  0)
        su_g  = food_data.get("sugar_g",  0)
        na_mg = food_data.get("sodium_mg", 0)

        p_kcal  = round(p_g  * 4)
        c_kcal  = round(c_g  * 4)
        fa_kcal = round(fa_g * 9)
        macro_kcal_total = max(p_kcal + c_kcal + fa_kcal, 1)

        p_pct_cal  = round(p_kcal  / macro_kcal_total * 100)
        c_pct_cal  = round(c_kcal  / macro_kcal_total * 100)
        fa_pct_cal = round(fa_kcal / macro_kcal_total * 100)

        p_raw  = p_g  / max(protein_target_g, 1) * 100
        c_raw  = c_g  / max(carbs_target_g,   1) * 100
        f_raw  = fa_g / max(fat_target_g,     1) * 100
        fi_raw = fi_g / max(fiber_target_g,   1) * 100
        su_raw = su_g / max(sugar_target_g,   1) * 100
        na_raw = na_mg/ max(sodium_target_mg, 1) * 100

        p_pct  = min(round(p_raw),  100)
        c_pct  = min(round(c_raw),  100)
        f_pct  = min(round(f_raw),  100)
        fi_pct = min(round(fi_raw), 100)
        su_pct = min(round(su_raw), 100)
        na_pct = min(round(na_raw), 100)

        def _aim_dot(pct):
            """Green = hit target, amber = ≥75%, red = under."""
            if pct >= 100: return "#4ade80"
            if pct >= 75:  return "#fbbf24"
            return "#ef4444"

        def _limit_dot(pct):
            """Green = well under limit, amber = approaching, red = over."""
            if pct > 100: return "#ef4444"
            if pct > 75:  return "#fbbf24"
            return "#4ade80"

        tick = "box-shadow:inset -2px 0 0 rgba(255,255,255,0.25)"

        p_tip  = f"Protein: {p_g:.0f} / {protein_target_g}g target · {p_kcal} kcal · {p_pct_cal}% of calories"
        c_tip  = f"Carbohydrates: {c_g:.0f} / {carbs_target_g}g target · {c_kcal} kcal · {c_pct_cal}% of calories"
        fa_tip = f"Fat: {fa_g:.0f} / {fat_target_g}g target · {fa_kcal} kcal · {fa_pct_cal}% of calories"
        fi_tip = f"Fibre: {fi_g:.0f} / {fiber_target_g}g target · supports digestion and glycaemic control"
        su_base_note = f" ({training_hours_today:.1f}h training today: base 50g + {sugar_target_g - 50}g fuel allowance)" if training_hours_today > 0.1 else " (sedentary limit — log sessions to adjust)"
        na_base_note = f" ({training_hours_today:.1f}h training today: base 2300mg + {sodium_target_mg - 2300}mg sweat replacement)" if training_hours_today > 0.1 else " (sedentary limit — log sessions to adjust)"
        su_tip = f"Sugar: {su_g:.0f} / {sugar_target_g}g adjusted limit · fast carbs are valid fuel around sessions{su_base_note}"
        na_tip = f"Sodium: {na_mg:.0f} / {sodium_target_mg}mg adjusted limit · replace sweat losses; excess still raises blood pressure{na_base_note}"

        return (
            '<div class="nutr-cal">'
            '<span class="label">Nutrition</span>'
            '<span style="display:flex;align-items:center;gap:6px">'
            f'<span class="nutr-cal-num">{consumed_kcal:,} / {kcal_total:,} kcal</span>'
            '<button id="nutr-toggle" class="nutr-toggle-btn" onclick="event.stopPropagation();toggleNutrExtras()" title="Toggle extra stats">+</button>'
            '<button class="nutr-toggle-btn" onclick="event.stopPropagation();toggleNutrModal()" title="Expand">&#10530;</button>'
            '</span>'
            '</div>'
            f'<div class="bar-wrap" data-tip="Expected ~{round(expected_so_far):,} kcal by now ({round(day_frac*100)}% through the {EATING_START_H}:00-{EATING_END_H}:00 eating window)">'
            f'<div class="bar-fill" style="width:{cal_pct}%;background:{cal_color}"></div>'
            f'<div class="bar-pace-marker" style="left:{round(day_frac*100, 1)}%"></div>'
            '</div>'
            f'<div class="sub" style="margin-top:2px">pace: ~{round(expected_so_far):,} kcal expected by now</div>'
            f'<div class="macro-row" data-tip="{p_tip}">'
            '<span class="macro-name">P</span>'
            f'<div class="macro-bar-wrap" style="{tick}">'
            f'<div class="macro-bar-fill" style="width:{p_pct}%;background:#a78bfa"></div>'
            '</div>'
            f'<span class="macro-val">{p_g:.0f}/{protein_target_g}g</span>'
            f'<span style="font-size:7px;color:{_aim_dot(p_raw)};margin-left:2px;flex-shrink:0">●</span>'
            '</div>'
            f'<div class="macro-row" data-tip="{c_tip}">'
            '<span class="macro-name">C</span>'
            f'<div class="macro-bar-wrap" style="{tick}">'
            f'<div class="macro-bar-fill" style="width:{c_pct}%;background:#fbbf24"></div>'
            '</div>'
            f'<span class="macro-val">{c_g:.0f}/{carbs_target_g}g</span>'
            f'<span style="font-size:7px;color:{_aim_dot(c_raw)};margin-left:2px;flex-shrink:0">●</span>'
            '</div>'
            f'<div class="macro-row" data-tip="{fa_tip}">'
            '<span class="macro-name">F</span>'
            f'<div class="macro-bar-wrap" style="{tick}">'
            f'<div class="macro-bar-fill" style="width:{f_pct}%;background:#fb923c"></div>'
            '</div>'
            f'<span class="macro-val">{fa_g:.0f}/{fat_target_g}g</span>'
            f'<span style="font-size:7px;color:{_aim_dot(f_raw)};margin-left:2px;flex-shrink:0">●</span>'
            '</div>'
            f'<div class="macro-row nutr-extra" style="display:none" data-tip="{fi_tip}">'
            '<span class="macro-name" style="color:#4ade80">Fi</span>'
            f'<div class="macro-bar-wrap" style="{tick}">'
            f'<div class="macro-bar-fill" style="width:{fi_pct}%;background:#4ade80"></div>'
            '</div>'
            f'<span class="macro-val">{fi_g:.0f}/{fiber_target_g}g</span>'
            f'<span style="font-size:7px;color:{_aim_dot(fi_raw)};margin-left:2px;flex-shrink:0">●</span>'
            '</div>'
            f'<div class="macro-row nutr-extra" style="display:none" data-tip="{su_tip}">'
            '<span class="macro-name" style="color:#f472b6">Su</span>'
            f'<div class="macro-bar-wrap" style="{tick}">'
            f'<div class="macro-bar-fill" style="width:{su_pct}%;background:#f472b6"></div>'
            '</div>'
            f'<span class="macro-val">{su_g:.0f}/{sugar_target_g}g</span>'
            f'<span style="font-size:7px;color:{_limit_dot(su_raw)};margin-left:2px;flex-shrink:0">●</span>'
            '</div>'
            f'<div class="macro-row nutr-extra" style="display:none" data-tip="{na_tip}">'
            '<span class="macro-name" style="color:#67e8f9">Na</span>'
            f'<div class="macro-bar-wrap" style="{tick}">'
            f'<div class="macro-bar-fill" style="width:{na_pct}%;background:#67e8f9"></div>'
            '</div>'
            f'<span class="macro-val">{na_mg:.0f}/{sodium_target_mg}mg</span>'
            f'<span style="font-size:7px;color:{_limit_dot(na_raw)};margin-left:2px;flex-shrink:0">●</span>'
            '</div>'
            f'<div class="macro-row nutr-extra" style="display:none">'
            f'<span style="font-size:10px;color:#475569">P {p_pct_cal}% · C {c_pct_cal}% · F {fa_pct_cal}%</span>'
            '</div>'
            + food_list_html + food_input_html
        )
    else:
        return (
            '<div class="nutr-cal">'
            '<span class="label">Nutrition</span>'
            '<span style="display:flex;align-items:center;gap:6px">'
            '<span class="nutr-cal-num">— no meals logged today</span>'
            '<button class="nutr-toggle-btn" onclick="event.stopPropagation();toggleNutrModal()" title="Expand">&#10530;</button>'
            '</span>'
            '</div>'
            + food_input_html
        )


def load_food_log():
    if not os.path.exists(FOOD_LOG_FILE):
        return {}
    try:
        with open(FOOD_LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


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
        entries   = log.get(d, [])
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
        kcal = sum(e.get("calories", 0) for e in entries) if entries else None
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

def _claude_p(prompt: str, timeout: int = 90) -> str:
    """Call claude -p with the given prompt and return stripped stdout."""
    result = subprocess.run(
        ["claude", "-p", "--model", "claude-haiku-4-5-20251001"],
        input=prompt, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "claude -p failed"
        log.error("claude -p failed (rc=%d): %s", result.returncode, err)
        raise RuntimeError(err)
    return result.stdout.strip()

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

    if not logged and today_data["entry_count"] == 0:
        return "No meals logged this week — log food with `food add` to get nutritional insights.", False

    prompt = (
        "You are a sports nutritionist advising a solo rower targeting a 2k erg from 6:29 to 6:10 "
        "over a year, with a lean mass gain goal of 83→90kg (~300 kcal/day surplus, ~2g protein/kg). "
        "Review the last 7 days of food logs and give an honest, balanced weekly assessment.\n\n"
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
        "Last 7 days:\n" + "\n".join(weekly) + "\n\n"
        "Write exactly 3 sentences. Be specific about which days/foods you are referencing. "
        "No fluff, no generic advice, no greetings, no markdown formatting."
    )
    insight = _claude_p(prompt)
    _save_cache(NUTR_CACHE_FILE, {"hash": input_hash, "insight": insight})
    return insight, False


def get_calorie_gap_suggestion(food_data, ctx):
    """If today is meaningfully short of its calorie target, returns a one-line
    meal suggestion (haiku model, cached per day/consumed-total) sized to close
    the remaining kcal/protein/carbs/fat gap. Returns None if on/ahead of target.
    Cheap and cached — safe to call on every render, no per-hover network call."""
    consumed = (food_data or {}).get("calories", 0)
    gap_kcal = ctx["kcal_total"] - consumed
    if gap_kcal <= 50:
        return None

    remaining_p = max(round(ctx["protein_target_g"] - (food_data or {}).get("protein_g", 0)), 0)
    remaining_c = max(round(ctx["carbs_target_g"]   - (food_data or {}).get("carbs_g",   0)), 0)
    remaining_f = max(round(ctx["fat_target_g"]     - (food_data or {}).get("fat_g",     0)), 0)

    today_str   = datetime.now().strftime("%Y-%m-%d")
    input_hash  = _data_hash(f"{today_str}|{round(consumed)}|{round(gap_kcal)}")
    cache = _load_cache(CAL_GAP_CACHE_FILE)
    if cache.get("hash") == input_hash:
        return cache.get("suggestion")

    prompt = (
        "You are a sports nutritionist advising a solo rower on a lean bulk "
        "(~2g protein/kg, ~300 kcal/day surplus target). It is late in the day and they "
        f"are short of today's targets by {round(gap_kcal)} kcal "
        f"({remaining_p}g protein, {remaining_c}g carbs, {remaining_f}g fat still remaining).\n\n"
        "Suggest ONE realistic meal or snack, with rough quantities, that would close this gap. "
        "One sentence, no markdown, no greetings."
    )
    try:
        suggestion = _claude_p(prompt, timeout=90)
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
        return "Clear", "no active debt", "#4ade80", "Sleep debt EMA is at or above zero — no recovery needed."

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

    color = "#f87171" if debt > 1.0 else "#fbbf24"
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


# ── Gemini coach brief ───────────────────────────────────────────────────────

def build_data_text(wellness, activities, training_plan=None, food_data=None, illness=None):
    entries = sorted([w for w in wellness if w.get("id")], key=lambda w: w["id"])
    today   = next((w for w in reversed(entries) if w.get("ctl") or w.get("hrv")), {})
    recent  = entries[-7:]

    ctl = today.get("ctl"); atl = today.get("atl")
    tsb = round(ctl - atl, 1) if ctl and atl else None
    hrv = today.get("hrv"); rhr = today.get("restingHR")
    sleep_score = today.get("sleepScore"); sleep_secs = today.get("sleepSecs")

    hrv_vals     = [w.get("hrv") for w in recent if w.get("hrv")]
    hrv_baseline = sum(hrv_vals[:-1]) / len(hrv_vals[:-1]) if len(hrv_vals) > 1 else None

    # Sleep-debt EMA (τ=5d, 8h target): negative = in debt, positive = surplus
    _sd_dates, _sd_vals = compute_sleep_balance(wellness)
    sleep_debt = _sd_vals[-1] if _sd_vals else None

    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    week_rows  = [a for a in activities if "Row" in a.get("type", "")
                  and (a.get("start_date_local") or a.get("start_date") or "")[:10] >= week_start]
    zone_totals = [0] * 7
    for a in week_rows:
        for i, t in enumerate((a.get("icu_hr_zone_times") or [])[:7]):
            zone_totals[i] += t
    zone_str = "  ".join(f"Z{i+1}:{zone_totals[i]//60}m" for i in range(7) if zone_totals[i] > 60)

    def fmt_sleep(s):
        if not s: return "unknown"
        h, m = divmod(int(s) // 60, 60)
        return f"{h}h {m:02d}m"

    lines = [
        f"Date: {datetime.now().strftime('%A %d %B %Y')}",
        f"Fitness (CTL): {ctl:.0f}" if ctl else "Fitness (CTL): unknown",
        f"Fatigue (ATL): {atl:.0f}" if atl else "Fatigue (ATL): unknown",
        f"Form (TSB): {tsb:+.1f}" if tsb is not None else "Form (TSB): unknown",
        f"HRV: {int(hrv)}" + (f"  (7-day avg: {hrv_baseline:.0f})" if hrv_baseline else "") if hrv else "HRV: unknown",
        f"Resting HR: {int(rhr)} bpm" if rhr else "Resting HR: unknown",
        f"Sleep: {fmt_sleep(sleep_secs)}" + (f", score {int(sleep_score)}" if sleep_score else ""),
        (f"Sleep debt (EMA τ=5d vs 8h target): {sleep_debt:+.1f}h "
         f"({'in deficit — prioritise recovery sleep' if sleep_debt < -0.5 else 'surplus' if sleep_debt > 0.5 else 'roughly balanced'})"
         if sleep_debt is not None else "Sleep debt: unknown"),
        f"This week zone time: {zone_str}" if zone_str else "This week: no rowing yet",
        f"Sessions last 14 days: {len(activities)}",
    ]

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Sessions already logged in intervals.icu today
    done_today = [a for a in activities
                  if (a.get("start_date_local") or a.get("start_date") or "")[:10] == today_str]
    if done_today:
        lines.append("\nSessions COMPLETED TODAY (logged in intervals.icu — infer which planned session each matches):")
        for a in done_today:
            atype  = a.get("type", "unknown")
            mins   = int((a.get("elapsed_time") or a.get("moving_time") or 0) / 60)
            dist_m = a.get("distance") or 0
            dist   = f"{dist_m/1000:.1f}km" if dist_m > 100 else ""
            tss    = a.get("icu_training_load") or a.get("training_load") or 0
            zones  = a.get("icu_hr_zone_times") or []
            z4plus = int(sum(zones[i] for i in range(3, min(7, len(zones)))) / 60) if zones else 0
            parts  = [atype]
            if mins:   parts.append(f"{mins}min")
            if dist:   parts.append(dist)
            if tss:    parts.append(f"TSS {tss:.0f}")
            if z4plus: parts.append(f"{z4plus}min Z4+")
            lines.append(f"  {' · '.join(parts)}")
        lines.append("  (Match each completed session to a planned session by type, duration, and intensity.)")
    else:
        lines.append("\nNothing logged in intervals.icu yet today.")

    sessions       = (training_plan or {}).get("sessions", [])
    today_planned  = [s for s in sessions if s.get("date", "") == today_str]
    future_planned = [s for s in sessions if s.get("date", "") > today_str]

    if today_planned:
        lines.append("\nToday's planned sessions (cross-reference with completed list above to determine what's still to do):")
        for s in today_planned:
            lines.append(f"  \"{s['name']}\"")

    if future_planned:
        lines.append("\nUpcoming sessions (future days):")
        by_date = defaultdict(list)
        for s in future_planned:
            by_date[s["date"]].append(s)
        for date in sorted(by_date):
            day_label = datetime.strptime(date, "%Y-%m-%d").strftime("%A %d %b")
            for s in by_date[date]:
                lines.append(f"  {day_label}: \"{s['name']}\"")
    else:
        lines.append("\nNo upcoming sessions are scheduled in the training plan. Do NOT invent or assume "
                     "future sessions — instead ask the athlete what's coming up so it can be planned around.")

    if illness and illness.today is not None:
        today_pct     = round(illness.today * 100)
        yesterday_pct = round(illness.yesterday * 100) if illness.yesterday is not None else today_pct
        trend = "rising" if illness.today > illness.yesterday + 0.05 else \
                "falling" if illness.today < illness.yesterday - 0.05 else "stable"
        level = "elevated — factor this into how hard today's session should be" if illness.today > 0.3 else \
                "mildly raised — worth a cautious eye" if illness.today > 0.15 else "low"
        lines.append(f"\nIllness risk (GP+HMM model): {today_pct}% today (was {yesterday_pct}% yesterday, "
                     f"trend: {trend}, level: {level}).")
        if illness.today > 0.15 and illness.bands:
            last = illness.bands[-1]
            lines.append(f"Last detected illness episode: {last['start']} – {last['end']}.")

    active_notes = load_notes().get("active", [])
    if active_notes:
        lines.append("\nRECENT PERSONAL NOTES from the athlete (weight these heavily as current context; "
                     "most recent last; relative day labels are from today's perspective):")
        today_date = datetime.now().date()
        for n in active_notes:
            day = n["ts"][:10]
            note_date = datetime.strptime(day, "%Y-%m-%d").date()
            delta = (today_date - note_date).days
            if delta == 0:
                rel = "today"
            elif delta == 1:
                rel = "yesterday"
            else:
                rel = f"{delta} days ago"
            day_name = note_date.strftime("%A")
            lines.append(f"  [{day} {day_name}, {rel}] {n['text']}")

    return "\n".join(lines)


def get_claude_summary(data_text):
    """Returns ({"overview": ..., "tips": ...}, is_stale)."""
    input_hash = _data_hash(data_text)
    cache      = _load_cache(CACHE_FILE)
    if cache.get("hash") == input_hash and cache.get("overview"):
        return {"overview": cache["overview"], "tips": cache.get("tips", "")}, False

    prompt = (
        "You are an experienced rowing coach writing a personalised daily brief for a post-graduation rower "
        "training solo (no squad) with a WaterRower at home and a nearby gym.\n\n"

        "ATHLETE CONTEXT: Alex has just graduated from Cambridge, bladed at May Bumps 2026, and is targeting "
        "a 2k erg improvement from 6:29 to 6:10 (376W → 437W, +61W) by June 2027 to make a good university "
        "crew if starting a PhD. Current squat ~90kg (glute-limited at depth), RDL ~100kg+, bodyweight 83kg "
        "targeting 90kg. Training plan phases: Phase 1 Jul–Sep (glute fix + base, target 6:25), "
        "Phase 2 Oct–Jan (strength build + threshold, target 6:18), "
        "Phase 3 Feb–Apr (VO2max intervals, target 6:12), Phase 4 May–Jun (race-specific, target 6:10). "
        "Secondary goals: two half marathons (Feb and Apr/May) and a cycling sportive (Sep/Oct). "
        "No squad structure — self-accountability is important; the brief should reinforce consistency.\n\n"

        "Write TWO separate sections. Separate them with exactly the line: ---TIPS---\n\n"

        "SECTION 1 — OVERVIEW (3–4 sentences, plain factual prose):\n"
        "  • Readiness read from the specific numbers (TSB, HRV vs 7-day baseline, sleep score, "
        "    sleep-debt EMA, and illness risk — call out elevated illness risk or a significant sleep deficit if present), "
        "    and what that combination means for how hard today should be.\n"
        "  • What's been done today and what's still to do — infer completed sessions from "
        "    the logged activity metrics (type/duration/distance/Z4+ time); report actual logged "
        "    distance/duration, not the planned session name distance.\n"
        "  • If NO upcoming sessions are scheduled, do not assume any — instead ask the athlete directly what "
        "    sessions are coming up (mention they can type it into the Notes box below and you'll plan around it). "
        "    If sessions ARE scheduled, briefly orient them to what's ahead.\n\n"

        "SECTION 2 — TIPS (3–5 sentences, genuinely useful and specific):\n"
        "  Give advice that a knowledgeable coach would give, tailored to today's actual context and phase of "
        "  the plan. Develop the reasoning — say why, not just what. "
        "  Must NOT be 'sleep more', 'stay hydrated', or any other generic wellness platitude. "
        "  Draw on the most relevant angles from: fuelling for the specific session type (gym vs threshold vs VO2max), "
        "  strength progression cues (squat depth, hip thrust loading, when to deload), "
        "  pacing the WaterRower by HR rather than pace since it's not a C2, "
        "  threshold and VO2max execution cues, running economy for HM prep, "
        "  how to manage fatigue without a squad keeping you accountable, "
        "  managing training load when illness risk is elevated or sleep debt is high, "
        "  acting on anything raised in the athlete's recent personal notes, "
        "  or anything else specific and useful given today's data and plan phase.\n\n"

        "Hard rules: no TSS/load numbers. No bullet points, no headers, no greetings, no markdown. "
        "Each section plain prose. Overview ≤90 words, Tips ≤140 words.\n\n"
        + data_text
    )
    text = _claude_p(prompt)
    if not text.strip():
        raise RuntimeError("claude -p returned empty output")
    if "---TIPS---" in text:
        overview, tips = text.split("---TIPS---", 1)
        overview = overview.strip(); tips = tips.strip()
    else:
        # Model didn't emit the separator — degrade loudly, don't blank the tips.
        log.warning("coach brief missing ---TIPS--- separator; falling back to paragraph split")
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        if len(blocks) >= 2:
            overview = blocks[0]
            tips     = "\n\n".join(blocks[1:])
        else:
            overview = text.strip(); tips = ""
    if not tips:
        log.warning("coach brief produced empty tips section")
    _save_cache(CACHE_FILE, {"hash": input_hash, "overview": overview, "tips": tips})
    return {"overview": overview, "tips": tips}, False


EXTRACT_CACHE_FILE = os.path.expanduser("~/.cache/training-brief/extract-cache.json")

def extract_sessions_from_notes(active_notes):
    """Conservatively pull *clearly-described* upcoming sessions out of the active
    notes and append them to training-plan.json. Returns the list of added
    sessions. Does nothing (returns []) unless a note unambiguously describes a
    future session — never invents or duplicates entries."""
    if not active_notes:
        return []

    plan      = load_training_plan()
    sessions  = plan.get("sessions", [])
    existing  = {(s.get("date"), s.get("name", "").strip().lower()) for s in sessions}
    today_str = datetime.now().strftime("%Y-%m-%d")
    notes_blob = "\n".join(f"[{n['ts'][:10]}] {n['text']}" for n in active_notes)

    # Skip the LLM call if neither the notes nor the upcoming plan have changed
    # since the last extraction (avoids a ~5s call on every refresh).
    state_key = notes_blob + "||" + "|".join(sorted(
        f"{d}:{n}" for d, n in existing if d and d >= today_str))
    state_hash = _data_hash(state_key)
    if _load_cache(EXTRACT_CACHE_FILE).get("hash") == state_hash:
        return []

    prompt = (
        "You maintain a rower's training plan. Read the athlete's free-text notes below and extract ONLY "
        "concrete upcoming training sessions that are clearly described (a session on a stated or clearly "
        "implied date). Do NOT invent sessions, infer vague intentions, or include sessions already in the plan.\n\n"
        f"Today is {today_str}. Resolve relative dates (\"tomorrow\", \"Friday\") to absolute YYYY-MM-DD.\n\n"
        "Sessions ALREADY in the plan (do not duplicate):\n"
        + ("\n".join(f"  {s.get('date')}: {s.get('name')}" for s in sessions if s.get("date", "") >= today_str) or "  (none upcoming)")
        + "\n\nAthlete's notes:\n" + notes_blob + "\n\n"
        "Output a STRICT JSON array (no prose, no code fences). Each item: "
        '{"date":"YYYY-MM-DD","name":"<short session name>","tss":<int estimate>,"z4_mins":<int estimate>}. '
        "TSS scales with DURATION, not just intensity — a short maximal effort is still LOW TSS. "
        "Guide: easy paddle/recovery ~30-45; steady 60-90min ~60-80; long row 2h+ ~100-130; "
        "a SHORT all-out race or sprint (a few minutes up to ~15min, e.g. a bumps race) is only ~20-45 "
        "even though it feels maximal — do NOT inflate short efforts. Include the paddle to/from in the estimate. "
        "z4_mins = minutes actually spent at Z4+ (a 5-min sprint is ~5, an easy paddle is 0). "
        "If no clear upcoming session is described, output exactly: []"
    )

    raw = _claude_p(prompt).strip()
    # Extract the first JSON array, tolerating code fences or trailing prose
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        log.info("session extraction: no JSON array in model output")
        return []
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        log.warning("session extraction: could not parse model JSON: %s", raw[:200])
        return []
    if not isinstance(parsed, list):
        return []

    added = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        date = str(item.get("date", "")).strip()
        name = str(item.get("name", "")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not name:
            continue
        if date < today_str:                       # only future/today
            continue
        if (date, name.lower()) in existing:        # dedupe
            continue
        try:
            tss = int(item.get("tss", 60)); z4 = int(item.get("z4_mins", 0))
        except (TypeError, ValueError):
            tss, z4 = 60, 0
        entry = {"date": date, "name": name, "tss": tss, "z4_mins": z4}
        sessions.append(entry); existing.add((date, name.lower())); added.append(entry)

    if added:
        plan["sessions"] = sorted(sessions, key=lambda s: s.get("date", ""))
        with open(TRAINING_PLAN_FILE, "w") as f:
            json.dump(plan, f, indent=2)
        log.info("session extraction added %d session(s): %s",
                 len(added), "; ".join(f"{a['date']} {a['name']}" for a in added))
        # `existing` now includes the added sessions; cache the post-add state so an
        # unchanged re-run is a cache hit rather than one more (empty) LLM call.
        final_key = notes_blob + "||" + "|".join(sorted(
            f"{d}:{n}" for d, n in existing if d and d >= today_str))
        state_hash = _data_hash(final_key)
    _save_cache(EXTRACT_CACHE_FILE, {"hash": state_hash})
    return added


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
    brief = _claude_p(prompt)
    _save_cache(NUTR_COACH_CACHE, {"hash": input_hash, "brief": brief})
    return brief, False


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _color_tsb(v):
    if v is None:  return "#888"
    if v > 10:     return "#4ade80"
    if v < -20:    return "#f87171"
    return "#fbbf24"

def _color_hrv(delta):
    if delta is None: return "#e2e8f0"
    if delta < -5:    return "#f87171"
    return "#4ade80"

def _color_sleep(score):
    if score is None: return "#888"
    if score >= 80:   return "#4ade80"
    if score >= 60:   return "#fbbf24"
    return "#f87171"

def _fmt_sleep(secs):
    if not secs: return "—"
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h {m:02d}m"

def _stat(label, value, color="#e2e8f0", sub=None, tip=None):
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    tip_attr = f' data-tip="{tip}"' if tip else ""
    cursor   = ' style="cursor:help"' if tip else ""
    return (f'<div class="stat"{tip_attr}{cursor}>'
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
            '<div style="font-size:10px;color:#fbbf24;font-weight:600;margin-bottom:3px">'
            '⚡ MOBILITY RECOMMENDED</div>'
            f'<div style="font-size:11px;color:#d97706;line-height:1.4">'
            f'{int(z4_today)}min Z4+ today — 10min mobility or stretching before bed will '
            f'reduce tomorrow\'s soreness.</div>'
            '</div>'
        )

    # ── Stretch streak ────────────────────────────────────────────────────────
    streak, stretched_today = get_stretch_status()
    if streak > 0 or stretched_today:
        flame = "🔥" * min(streak, 3)
        streak_color = "#4ade80" if stretched_today else "#fbbf24"
        streak_label = f"{streak}d streak {flame}" if streak > 0 else "Start your streak today"
        done_badge = (
            '<span style="font-size:9px;color:#4ade80;margin-left:6px">✓ done today</span>'
            if stretched_today else
            '<span style="font-size:9px;color:#475569;margin-left:6px">not yet today</span>'
        )
        rows.append(
            '<hr class="div">'
            f'<div style="display:flex;align-items:center;justify-content:space-between">'
            f'<div class="label">Stretch</div>'
            f'<div style="font-size:11px;color:{streak_color};font-weight:600">{streak_label}</div>'
            f'</div>'
            f'<div style="font-size:10px;color:#475569;margin-top:2px">'
            f'Run <code style="color:#94a3b8">stretch log</code> to record a session{done_badge}</div>'
        )
    else:
        rows.append(
            '<hr class="div">'
            '<div style="display:flex;align-items:center;justify-content:space-between">'
            '<div class="label">Stretch</div>'
            '<div style="font-size:11px;color:#475569">No streak yet</div>'
            '</div>'
            '<div style="font-size:10px;color:#475569;margin-top:2px">'
            'Run <code style="color:#94a3b8">stretch log</code> to start one</div>'
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
                label_color = "#94a3b8"
            else:
                d = datetime.strptime(date, "%Y-%m-%d")
                day_label = d.strftime("%a %-d %b")
                label_color = "#475569"
            rows.append(
                f'<div style="font-size:10px;color:{label_color};'
                f'text-transform:uppercase;letter-spacing:.05em;'
                f'margin:5px 0 3px">{day_label}</div>'
            )
            for s in by_date[date]:
                name = s.get("name", "Session")
                z4 = s.get("z4_mins", 0)
                dot_color = "#f87171" if z4 >= 10 else ("#fbbf24" if z4 >= 3 else "#475569")
                rows.append(
                    f'<div style="display:flex;align-items:flex-start;gap:6px;'
                    f'margin-bottom:4px;line-height:1.3">'
                    f'<span style="color:{dot_color};margin-top:2px;flex-shrink:0">●</span>'
                    f'<span style="font-size:12px;color:#94a3b8">{name}</span>'
                    f'</div>'
                )

        if beyond:
            rows.append(
                f'<div style="font-size:10px;color:#475569;margin-top:7px;font-style:italic">'
                f'+{beyond} further session{"s" if beyond != 1 else ""} in the plan</div>'
            )

    return "".join(rows)


# ── Main HTML builder ────────────────────────────────────────────────────────

def _color_illness(p):
    if p is None: return "#888"
    if p < 0.20:  return "#4ade80"
    if p < 0.50:  return "#fbbf24"
    return "#f87171"


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
    cursor   = ' style="cursor:help"' if tip else ""
    return (f'<div class="stat"{tip_attr}{cursor}>'
            f'<div class="label">Illness Risk</div>'
            f'<div class="value" id="illness-val" style="color:{color}">{val}</div>'
            f'{sub_html}'
            f'<div style="position:relative;width:100%;height:28px;margin-top:5px;border-radius:3px;overflow:hidden">'
            f'<canvas id="illness-spark" style="position:absolute;top:0;left:0;width:100%;height:100%"></canvas>'
            f'</div>'
            f'</div>')


def build_html(wellness, activities, training_plan, summary=None, calorie_target=None,
               food_data=None, illness=None, surplus_target=0):
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
        debt_str       = f"{abs(avg_daily_debt):.1f}h/night {sign}"
        debt_color     = "#f87171" if avg_daily_debt > 1 else ("#fbbf24" if avg_daily_debt > 0 else "#4ade80")
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
    kcal_stat = _stat("Calorie Target", kcal_disp, "#94a3b8", kcal_sub)

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
        bt_color = {"on track": "#4ade80", "slow but positive": "#fbbf24", "in deficit": "#f87171"}[bulk_trend["state"]]
        proj_sub = ""
        if bulk_trend["today_projected"] is not None:
            proj_sub = f" · today proj. {bulk_trend['today_projected']:+,} kcal"
        bt_stat = _stat(
            f"Bulk Trend ({bulk_trend['window']}d avg, prior days)",
            f"{bulk_trend['avg_surplus']:+,} kcal/day",
            bt_color,
            f"→ {bulk_trend['kg_per_week']:+.2f} kg/wk proj. · {bulk_trend['logged_days']}/{bulk_trend['window']} days logged · {bulk_trend['state']}{proj_sub}",
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
    clearance_stat = _stat("Sleep Clearance", clearance_val, clearance_color, clearance_sub, tip=clearance_tip)

    # ── Nutrition section ─────────────────────────────────────────────────────
    nutr_body_html = render_nutrition_body(food_data, nutr_ctx)
    nutr_html = (
        '<hr class="div">'
        '<div class="nutr-section" id="nutr-section" data-tip="Analysing weekly nutrition…" style="cursor:pointer">'
        f'<div id="nutr-body">{nutr_body_html}</div>'
        '</div>'
    )

    # Three fragments, three homes: the KPI strip, the Nutrition tab, the Today tab.
    kpi_html = "".join([
        _stat("Form (TSB)", f"{tsb:+.1f}" if tsb is not None else "—",
              _color_tsb(tsb),
              f"Fitness {ctl:.0f} · Fatigue {atl:.0f}" if ctl and atl else None,
              tip=tsb_tip),
        _stat("HRV", f"{int(hrv)}" if hrv else "—",
              _color_hrv(hrv_delta),
              f"7-day avg {hrv_baseline:.0f}" if hrv_baseline else None,
              tip=hrv_tip),
        _stat("Resting HR", f"{int(rhr)} bpm" if rhr else "—", tip=rhr_tip),
        _illness_stat(illness),
        _stat("Sleep", _fmt_sleep(sleep_secs), _color_sleep(sleep_score),
              f"Score {int(sleep_score)}" if sleep_score else None,
              tip=sleep_tip),
        _stat("Sleep Avg (14d)", debt_str, debt_color, "avg vs 8h/night target", tip=debt_tip),
        clearance_stat,
        kcal_stat,
        bt_stat,
    ])

    nutr_panel_html = "".join([
        nutr_html,
        '<div id="nutr-detail" style="display:none;margin-top:8px;padding:8px 10px;'
        'background:#071a10;border:1px solid #14532d;border-radius:6px;'
        'font-size:12px;color:#86efac;line-height:1.65"></div>',
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

    benchmark_cfg = load_benchmark_config()
    # "bodyweight" is auto-sourced from the wellness weight log (see
    # weight_history below), not manually logged, so leave it off the form.
    metric_options_html = "\n".join(
        f'<option value="{key}">{meta["label"]}</option>'
        for key, meta in benchmark_cfg["metrics"].items() if key != "bodyweight")
    radar_axes_json = json.dumps(benchmark_cfg["radar_axes"])

    chart_data = json.dumps({
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
body{{background:#0f172a;color:#e2e8f0;font-family:-apple-system,'Segoe UI',sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}}
.header{{padding:11px 18px;border-bottom:1px solid #1e293b;display:flex;align-items:center;
         gap:10px;flex-shrink:0}}
.header h1{{font-size:14px;font-weight:600}}
.header .date{{font-size:12px;color:#64748b}}
select{{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:5px;
        padding:3px 8px;font-size:12px;cursor:pointer;outline:none}}
select:hover{{border-color:#475569}}
#periodSelect{{margin-left:auto}}
#periodSelect[hidden]{{display:none}}

/* ── tabs ─────────────────────────────────────────────────────────────────── */
.tabs{{display:flex;gap:2px;margin-left:14px}}
.tab{{background:none;border:1px solid transparent;border-radius:5px;color:#64748b;
      font-family:inherit;font-size:11.5px;padding:4px 11px;cursor:pointer;
      transition:color .15s,background .15s,border-color .15s}}
.tab:hover{{color:#94a3b8;background:#1e293b}}
.tab:focus-visible{{outline:2px solid #a78bfa;outline-offset:1px}}
.tab[aria-selected="true"]{{color:#a78bfa;border-color:#a78bfa;background:#1e1b4b}}

.tab-body{{flex:1;min-height:0;overflow-y:auto}}
.panel{{display:none}}
.panel.active{{display:block}}

/* ── KPI strip (was a 185px vertical rail) ────────────────────────────────── */
.stats{{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;
        padding:9px 6px;border-bottom:1px solid #1e293b;flex-shrink:0}}
.stat{{padding:0 14px;border-left:1px solid #1e293b;min-width:0}}
.stat:first-child{{border-left:none}}
.label{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:1px}}
.value{{font-size:17px;font-weight:600}}
.sub{{font-size:11px;color:#64748b;margin-top:1px}}
hr.div{{border:none;border-top:1px solid #1e293b;margin:8px 0 10px}}
.charts{{padding:12px 14px;display:grid;
         grid-template-columns:1fr 1fr;grid-template-rows:repeat(3,170px) 200px;
         gap:14px 12px;min-width:0}}
.chart-cell{{display:flex;flex-direction:column;min-height:0}}
.chart-cell.wide{{grid-column:1 / -1}}
.chart-label{{font-size:10px;color:#64748b;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:3px;flex-shrink:0;display:flex;align-items:center;gap:8px}}
.chart-wrap{{flex:1;min-height:0;position:relative}}
canvas{{position:absolute;top:0;left:0;width:100%;height:100%}}
/* ── Today: brief on the left, the week's plan on the right ───────────────── */
.today-grid{{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);
             gap:26px;padding:18px 22px;align-items:start}}
.today-col{{min-width:0}}
.today-col hr.div:first-child{{display:none}}

/* ── Nutrition: the old rail widget, given room ───────────────────────────── */
.nutr-grid{{display:grid;grid-template-columns:minmax(320px,420px) minmax(0,1fr);
            gap:32px;padding:18px 22px;align-items:start}}
.nutr-col{{min-width:0}}
.nutr-grid .nutr-section{{margin-top:0}}
.nutr-grid hr.div:first-child{{display:none}}

/* ── Notes: was one carousel panel of four ────────────────────────────────── */
.notes-panel-full{{padding:18px 22px;display:flex;flex-direction:column;gap:8px;max-width:90ch}}
.notes-hint{{font-size:9px;color:#475569;text-transform:none;letter-spacing:0}}
.notes-list{{min-height:0;overflow-y:auto;font-size:12px;color:#cbd5e1;line-height:1.5;
             max-height:calc(100vh - 300px)}}
.notes-empty{{color:#475569;font-style:italic;font-size:12px}}
.note-row{{padding:2px 0;border-bottom:1px solid #16213a}}
.note-added{{color:#4ade80;font-size:11px;padding:3px 0}}
.note-day{{color:#64748b;font-size:10px;margin-right:7px}}
.notes-input{{display:flex;gap:6px;align-items:flex-end}}
#note-box{{flex:1;background:#0f1b30;color:#e2e8f0;border:1px solid #1e293b;border-radius:5px;
          padding:5px 7px;font-size:12px;font-family:inherit;resize:none}}
.notes-input button{{background:#1e293b;color:#e2e8f0;border:none;border-radius:5px;padding:5px 10px;
          font-size:11px;cursor:pointer}}
.notes-input button:hover{{background:#334155}}
.notes-input button.notes-clear{{background:transparent;color:#64748b}}
.notes-input button.notes-clear:hover{{color:#f87171}}
.brief-label{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}}
.brief-text{{font-size:15px;color:#94a3b8;line-height:1.65}}
.tips-text{{color:#cbd5e1;font-size:15px;line-height:1.65}}
.brief-loading{{color:#475569;font-style:italic}}
.stat-tooltip{{position:fixed;background:#1e293b;border:1px solid #334155;border-radius:7px;
               padding:8px 11px;font-size:12px;color:#94a3b8;line-height:1.55;pointer-events:none;
               display:none;z-index:999;max-width:280px;
               box-shadow:0 4px 16px rgba(0,0,0,.5);white-space:pre-line}}
.footer{{padding:7px 18px;border-top:1px solid #1e293b;display:flex;justify-content:flex-end;flex-shrink:0}}
button{{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:5px;
        padding:4px 14px;font-size:12px;cursor:pointer}}
button:hover{{background:#334155;color:#e2e8f0}}
.nutr-section {{ margin-top: 2px; }}
.nutr-cal {{ display:flex; justify-content:space-between; font-size:11px; margin-bottom:4px; }}
.nutr-cal-num {{ color:#94a3b8; }}
.bar-wrap {{ position:relative; height:6px; background:#1e293b; border-radius:3px; margin-bottom:6px; cursor:help; }}
.bar-fill {{ height:100%; border-radius:3px; transition:width 0.6s ease; }}
.bar-pace-marker {{ position:absolute; top:-2px; bottom:-2px; width:2px; background:#e2e8f0; opacity:0.85; }}
.macro-row {{ display:flex; align-items:center; gap:6px; margin-bottom:5px; }}
.macro-name {{ font-size:10px; color:#64748b; width:14px; flex-shrink:0; }}
.macro-bar-wrap {{ flex:1; height:4px; background:#1e293b; border-radius:2px; overflow:hidden; }}
.macro-bar-fill {{ height:100%; border-radius:2px; transition:width 0.6s ease; }}
.macro-val {{ font-size:10px; color:#475569; white-space:nowrap; }}
.nutr-toggle-btn {{ background:none; border:1px solid #334155; border-radius:3px; color:#64748b;
  font-size:11px; line-height:1; padding:0 4px; cursor:pointer; margin:0; }}
.nutr-toggle-btn:hover {{ background:#1e293b; color:#94a3b8; border-color:#475569; }}
.nutr-toggle-btn.active {{ color:#a78bfa; border-color:#a78bfa; }}
.food-list {{ margin-top:5px; }}
.food-list summary {{ font-size:10px; color:#64748b; cursor:pointer; list-style:none; }}
.food-list summary::-webkit-details-marker {{ display:none; }}
.food-list summary:hover {{ color:#94a3b8; }}
.food-entry-list {{ margin-top:4px; max-height:120px; overflow-y:auto; }}
.food-entry-row {{ display:flex; align-items:baseline; gap:6px; font-size:10px; color:#64748b;
  padding:2px 0; border-bottom:1px solid #16213a; }}
.food-entry-time {{ color:#475569; flex-shrink:0; width:32px; }}
.food-entry-desc {{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.food-entry-kcal {{ color:#475569; flex-shrink:0; }}
.food-input-row {{ display:flex; gap:5px; margin-top:6px; }}
.food-input-row input {{ flex:1; background:#0f172a; border:1px solid #334155; border-radius:5px;
  color:#e2e8f0; font-size:11px; padding:4px 7px; min-width:0; }}
.food-input-row input:focus {{ outline:none; border-color:#a78bfa; }}
.food-input-row input:disabled {{ opacity:0.5; }}
.food-input-row button {{ padding:4px 10px; font-size:11px; }}
.food-status {{ font-size:10px; margin-top:4px; }}
.food-status.pending {{ color:#94a3b8; }}
.food-status.error {{ color:#f87171; }}
#nutr-backdrop {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.65); z-index:999; }}
.nutr-section.nutr-modal {{
  position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
  width:640px; max-width:92vw; max-height:85vh; overflow-y:auto;
  background:#0f172a; border:1px solid #334155; border-radius:12px;
  padding:26px 30px; z-index:1000; box-shadow:0 20px 60px rgba(0,0,0,.6);
  cursor:default;
}}
.nutr-modal .nutr-cal {{ font-size:20px; margin-bottom:14px; }}
.nutr-modal .bar-wrap {{ height:12px; margin-bottom:16px; }}
.nutr-modal .macro-row {{ gap:12px; margin-bottom:14px; }}
.nutr-modal .macro-name {{ font-size:16px; width:22px; }}
.nutr-modal .macro-bar-wrap {{ height:8px; }}
.nutr-modal .macro-val {{ font-size:16px; }}
.nutr-modal .sub {{ font-size:14px; }}
.nutr-modal .nutr-toggle-btn {{ font-size:15px; padding:2px 8px; }}
.nutr-modal .food-list summary {{ font-size:15px; }}
.nutr-modal .food-entry-list {{ max-height:220px; }}
.nutr-modal .food-entry-row {{ font-size:14px; padding:5px 0; }}
.nutr-modal .food-entry-time {{ width:44px; }}
.nutr-modal .food-input-row {{ margin-top:14px; }}
.nutr-modal .food-input-row input {{ font-size:15px; padding:9px 12px; }}
.nutr-modal .food-input-row button {{ font-size:15px; padding:9px 16px; }}
.nutr-modal .food-status {{ font-size:13px; }}
#stale-warn {{ display:none; position:fixed; bottom:10px; right:10px; font-size:16px; cursor:default;
  z-index:9999; }}
#stale-warn .stale-tip {{ display:none; position:absolute; bottom:24px; right:0; background:#1e293b;
  border:1px solid #f59e0b; border-radius:6px; padding:6px 9px; font-size:11px; color:#fcd34d;
  white-space:nowrap; pointer-events:none; }}
#stale-warn:hover .stale-tip {{ display:block; }}
/* ── Performance tracker ─────────────────────────────────────────────────── */
.tracker-view {{ display:flex; flex-direction:column; height:auto;
  padding:18px 22px; gap:10px; }}
.tracker-top {{ display:flex; gap:12px; align-items:flex-start; }}
.tracker-radar-col {{ flex-shrink:0; display:flex; flex-direction:column; align-items:center; gap:4px; }}
.tracker-header {{ font-size:10px; color:#64748b; font-weight:600; letter-spacing:.05em;
  text-transform:uppercase; }}
.tracker-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(175px,1fr));
  gap:7px; flex:1; }}
.metric-card {{ background:#1e293b; border:1px solid #1e3a5f; border-radius:7px;
  padding:9px 11px; }}
.mc-label {{ font-size:10px; color:#64748b; margin-bottom:2px; }}
.mc-val {{ font-size:20px; font-weight:700; line-height:1; }}
.mc-date {{ font-size:9px; color:#334155; margin-top:3px; }}
.mc-bar-wrap {{ height:3px; background:#0f172a; border-radius:2px; margin-top:5px; }}
.mc-bar-fill {{ height:100%; border-radius:2px; transition:width .6s ease; }}
.mc-spark {{ width:100%; height:24px; margin-top:4px; display:block; }}
.mc-notes {{ font-size:9px; color:#475569; margin-top:2px; font-style:italic;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.mc-journey {{ font-size:9px; color:#475569; display:flex; justify-content:space-between;
  margin-top:3px; }}
/* The blanket `canvas{{position:absolute;top:0;left:0}}` rule is meant for the
   chart canvases, which sit inside a position:relative .chart-wrap. The radar has
   no positioned ancestor, so it would anchor to the viewport and cover the header. */
#c-radar {{ display:block; position:static; }}
.log-form {{ display:flex; flex-wrap:wrap; gap:5px; align-items:center;
  padding:8px 10px; background:#1e293b; border-radius:7px; border:1px solid #334155; }}
.log-form select, .log-form input {{
  -webkit-appearance:none; appearance:none;
  background:#0f172a; border:1px solid #334155; border-radius:4px;
  color:#94a3b8; font-size:11px; padding:3px 7px; outline:none; }}
.log-form select {{ width:170px; cursor:pointer; }}
.log-form select option {{ background:#0f172a; color:#94a3b8; }}
.log-form input[type=number] {{ width:72px; }}
.log-form input[type=text]   {{ flex:1; min-width:80px; }}
.log-form select:focus, .log-form input:focus {{ border-color:#475569; color:#e2e8f0; }}
.log-form .log-btn {{ background:#312e81; border-color:#4338ca; color:#c7d2fe;
  font-size:11px; padding:3px 12px; }}
.log-form .log-btn:hover {{ background:#3730a3; }}
.log-added {{ font-size:10px; color:#4ade80; padding:2px 0; }}
</style></head>
<body>
<div class="header">
  <h1>Training Dashboard</h1>
  <span class="date">{day_str}</span>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" aria-selected="false" data-panel="today">Today</button>
    <button class="tab" role="tab" aria-selected="true"  data-panel="trends">Trends</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="nutrition">Nutrition</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="perf">Performance</button>
    <button class="tab" role="tab" aria-selected="false" data-panel="notes">Notes</button>
  </div>
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

  <section class="panel" data-panel="today">
    <div class="today-grid">
      <div class="today-col">
        <div class="brief-label">Overview</div>
        <div class="brief-text" id="brief-overview">{overview_html}</div>
        <div class="brief-label" style="margin-top:18px">Coach's Tips</div>
        <div class="tips-text" id="brief-tips">{tips_html}</div>
      </div>
      <div class="today-col">{sessions_html}</div>
    </div>
  </section>

  <section class="panel active" data-panel="trends">
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
      <div class="chart-cell wide">
        <div class="chart-label">Sleep Debt — exp. weighted τ=5d vs 8h/night · <span style="color:#4ade80">─</span> 9.5h  <span style="color:#fbbf24">─</span> 8.5h  <span style="color:#f87171">─</span> trend</div>
        <div class="chart-wrap" id="w-slp"><canvas id="c-slp"></canvas></div>
      </div>
    </div>
  </section>

  <section class="panel" data-panel="nutrition">
    <div class="nutr-grid">
      <div class="nutr-col">{nutr_panel_html}</div>
      <div class="nutr-col">
        <div class="brief-label">Nutrition Coach</div>
        <div class="tips-text" id="brief-nutr"><span class="brief-loading">Generating…</span></div>
      </div>
    </div>
  </section>

  <section class="panel" data-panel="perf">
    <div class="tracker-view" id="tracker-view">
      <div class="tracker-top">
        <div class="tracker-radar-col">
          <div class="tracker-header">Profile</div>
          <canvas id="c-radar"></canvas>
        </div>
        <div class="tracker-grid" id="tracker-cards" style="align-content:start"></div>
      </div>
      <div class="log-form" id="log-form">
          <select id="log-metric">
            <option value="">— metric —</option>
            {metric_options_html}
          </select>
          <input id="log-value" type="number" step="0.5" placeholder="Value">
          <input id="log-reps"  type="number" step="1"   placeholder="×reps">
          <input id="log-notes" type="text" placeholder="Notes (optional)">
          <button class="log-btn" onclick="submitMetric()">Add</button>
          <span id="log-confirm" class="log-added"></span>
      </div>
    </div>
  </section>

  <section class="panel" data-panel="notes">
    <div class="notes-panel-full">
      <div class="brief-label">Notes <span class="notes-hint">— context for the coach · persists until cleared</span></div>
      <div class="notes-list" id="notes-list">{notes_html}</div>
      <div class="notes-input">
        <textarea id="note-box" rows="2" placeholder="Add context, how you feel, or an upcoming session… (Ctrl+Enter to add)"></textarea>
        <button onclick="submitNote()">Add</button>
        <button class="notes-clear" onclick="clearNotes()">Clear all</button>
      </div>
    </div>
  </section>

</div>
<div class="footer">
  <button id="refresh-btn" onclick="triggerRefresh()" style="margin-right:8px">Refresh</button>
  <button onclick="document.title='__close__'">Dismiss</button>
</div>
<div id="tooltip" style="position:fixed;background:#1e293b;border:1px solid #334155;
     border-radius:7px;padding:8px 12px;font-size:11px;pointer-events:none;
     display:none;z-index:999;min-width:130px;box-shadow:0 4px 12px rgba(0,0,0,.4)"></div>
<div class="stat-tooltip" id="stat-tip"></div>
<div id="nutr-backdrop" onclick="toggleNutrModal()"></div>
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
  const dpr=window.devicePixelRatio||1, W=wrap.clientWidth, H=wrap.clientHeight;
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
    ctx.strokeStyle='#1e293b'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(PAD.left,y); ctx.lineTo(PAD.left+cW,y); ctx.stroke();
    ctx.fillStyle='#475569'; ctx.font='9px system-ui'; ctx.textAlign='right';
    ctx.fillText(Math.round(v),PAD.left-3,y+3);
  }}
  if (lo<0&&hi>0) {{
    const y0=yOf(0); ctx.strokeStyle='#475569'; ctx.lineWidth=1;
    ctx.setLineDash([3,3]); ctx.beginPath(); ctx.moveTo(PAD.left,y0); ctx.lineTo(PAD.left+cW,y0); ctx.stroke(); ctx.setLineDash([]);
  }}
  const n=dates.length, lbls=[0,Math.floor(n*.25),Math.floor(n*.5),Math.floor(n*.75),n-1];
  ctx.fillStyle='#475569'; ctx.font='9px system-ui'; ctx.textAlign='center';
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
  ctx.strokeStyle='#334155'; ctx.lineWidth=1; ctx.setLineDash([2,3]);
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
  const {{ctx,W,H}}=setupCanvas('c-ctl','w-ctl');
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
  plotSeries(ctx,i=>xOf(i),yOf,ctl,'#22d3ee',1.8);
  plotSeries(ctx,i=>xOf(i),yOf,atl,'#f87171',1.8);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.projCtl,'#22d3ee',1.2,true);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.projAtl,'#f87171',1.2,true);
  const ctlTip=[...ctl,...DATA.projCtl.slice(1)], atlTip=[...atl,...DATA.projAtl.slice(1)];
  const ctlSigTip=[...ctl.map(()=>null),...sigCtl.slice(1)];
  const atlSigTip=[...atl.map(()=>null),...sigAtl.slice(1)];
  CHART_META['c-ctl']={{dates:fullDates,PAD,yOf,series:[
    {{label:'CTL',data:ctlTip,sigmaData:ctlSigTip,color:'#22d3ee',fmt:v=>Math.round(v)+''}},
    {{label:'ATL',data:atlTip,sigmaData:atlSigTip,color:'#f87171',fmt:v=>Math.round(v)+''}},
  ]}};
}}

// ── TSB ───────────────────────────────────────────────────────────────────────
function drawTsb() {{
  const {{ctx,W,H}}=setupCanvas('c-tsb','w-tsb');
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
  drawProjBand(ctx,i=>xOf(nHist-1+i),yOf,DATA.projTsb,sigTsb,'rgba(148,163,184,.22)');
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.projTsb,'#94a3b8',1.2,true);
  for(const s of DATA.planSessions) {{
    const di=fullDates.indexOf(s.date); if(di<0) continue;
    ctx.fillStyle='#fbbf24'; ctx.beginPath(); ctx.arc(xOf(di),PAD.top+6,3,0,Math.PI*2); ctx.fill();
  }}
  const tsbTip=[...tsb,...DATA.projTsb.slice(1)];
  const tsbSigTip=[...tsb.map(()=>null),...sigTsb.slice(1)];
  CHART_META['c-tsb']={{dates:fullDates,PAD,yOf,series:[
    {{label:'Form',data:tsbTip,sigmaData:tsbSigTip,colorFn:tsbZoneColor,fmt:v=>(v>=0?'+':'')+v.toFixed(1)}},
  ]}};
}}

// ── HRV ───────────────────────────────────────────────────────────────────────
function drawHrv() {{
  const {{ctx,W,H}}=setupCanvas('c-hrv','w-hrv');
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
  plotSeries(ctx,xOf,yOf,hrv,'#f472b6',1.8);
  plotSeries(ctx,xOf,yOf,rhr,'#fb923c',1.6);
  CHART_META['c-hrv']={{dates,PAD,yOf,series:[
    {{label:'HRV',data:hrv,color:'#f472b6',fmt:v=>Math.round(v)+''}},
    {{label:'RHR',data:rhr,color:'#fb923c',fmt:v=>Math.round(v)+' bpm'}},
  ]}};
}}

// ── Z4+ ───────────────────────────────────────────────────────────────────────
function drawHil() {{
  const {{ctx,W,H}}=setupCanvas('c-hil','w-hil');
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
  plotSeries(ctx,i=>xOf(i),yOf,hil,'#a78bfa',1.8);
  if(projExtra.length) {{
    const anchor=[hil[hil.length-1],...projExtraHil];
    plotSeries(ctx,i=>xOf(nHist-1+i),yOf,anchor,'#a78bfa',1.2,true);
  }}
  const hilTip=[...hil,...projExtraHil];
  CHART_META['c-hil']={{dates:fullDates,PAD,yOf,series:[
    {{label:'Z4+ load',data:hilTip,color:'#a78bfa',fmt:v=>v.toFixed(1)+' min'}},
  ]}};
}}

// ── Sleep ─────────────────────────────────────────────────────────────────────
function drawSleep() {{
  const {{ctx,W,H}}=setupCanvas('c-slp','w-slp');
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
  ctx.strokeStyle='#67e8f9'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); drawSmooth(ctx,pts); ctx.stroke();

  // Projection lines (dashed), anchored at today (nHist-1)
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.slpProjBest, '#4ade80',1.2,true);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.slpProjMed,  '#fbbf24',1.2,true);
  plotSeries(ctx,i=>xOf(nHist-1+i),yOf,DATA.slpProjTrend,'#f87171',1.2,true);

  // Tooltip data: history + best-case projection for hover
  const balTip=[...bal,...DATA.slpProjBest.slice(1)];
  const medTip=[...bal.map(()=>null),...DATA.slpProjMed.slice(1)];
  const trendTip=[...bal.map(()=>null),...DATA.slpProjTrend.slice(1)];
  CHART_META['c-slp']={{dates:fullDates,PAD,yOf,series:[
    {{label:'Sleep debt',  data:balTip,   color:'#67e8f9',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
    {{label:'Best (9.5h)', data:[...bal.map(()=>null),...DATA.slpProjBest.slice(1)],  color:'#4ade80',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
    {{label:'Med (8.5h)',  data:medTip,   color:'#fbbf24',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
    {{label:'Trend (' + DATA.slpAvg + 'h)',data:trendTip,color:'#f87171',fmt:v=>(v>=0?'+':'')+v.toFixed(1)+'h'}},
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
    ctx.strokeStyle='rgba(148,163,184,.4)'; ctx.lineWidth=1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(xPos,PAD.top); ctx.lineTo(xPos,H-PAD.bottom); ctx.stroke(); ctx.setLineDash([]);
    for(const s of series) {{
      const v=s.data[idx]; if(v==null) continue;
      const yPos=yOf(v), col=s.colorFn?s.colorFn(v):s.color;
      ctx.fillStyle=col; ctx.beginPath(); ctx.arc(xPos,yPos,4,0,Math.PI*2); ctx.fill();
      ctx.strokeStyle='#0f172a'; ctx.lineWidth=1.5; ctx.stroke();
    }}
    const d=new Date(dates[idx]+'T00:00:00');
    const dStr=d.toLocaleDateString('en-GB',{{weekday:'short',day:'numeric',month:'short'}});
    const isProj=dates[idx]>new Date().toISOString().slice(0,10);
    let html=`<div style="color:#64748b;margin-bottom:5px;font-size:10px">${{dStr}}${{isProj?' <span style="color:#fbbf24">(proj)</span>':''}}</div>`;
    for(const s of series) {{
      const v=s.data[idx]; if(v==null) continue;
      const col=s.colorFn?s.colorFn(v):s.color;
      const sigV=s.sigmaData?s.sigmaData[idx]:null;
      html+=`<div style="display:flex;justify-content:space-between;gap:14px;margin-bottom:1px">
        <span style="color:${{col}}">${{s.label}}</span>
        <span style="color:#e2e8f0;font-weight:600">${{s.fmt?s.fmt(v):v.toFixed(1)}}${{sigV!=null?' <span style="color:#64748b;font-size:10px;font-weight:400">\xb1'+sigV.toFixed(1)+'</span>':''}}</span></div>`;
    }}
    // Calorie chart, today's bar: how far in the red vs today's full-day target,
    // plus a precomputed (cached, haiku) meal suggestion sized to close the gap.
    if(cid==='c-cal' && meta.todayIdx!=null && idx===meta.todayIdx) {{
      const consumedS=series.find(s=>s.label==='Consumed'), targetS=series.find(s=>s.label==='Target');
      const c=consumedS&&consumedS.data[idx], t=targetS&&targetS.data[idx];
      if(c!=null && t!=null && t-c>50) {{
        html+=`<div style="margin-top:6px;padding-top:6px;border-top:1px solid #334155;color:#f87171">
          ${{Math.round(t-c)}} kcal behind today's target</div>`;
        if(DATA.calGapSuggestion) {{
          html+=`<div style="margin-top:4px;font-size:10px;color:#94a3b8;max-width:220px;white-space:normal">${{DATA.calGapSuggestion}}</div>`;
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
  const {{ctx,W,H}}=setupCanvas('c-cal','w-cal');
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
  ctx.strokeStyle='rgba(148,163,184,.4)'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
  ctx.beginPath();
  target.forEach((v,i)=>i===0?ctx.moveTo(xOf(i),yOf(v)):ctx.lineTo(xOf(i),yOf(v)));
  ctx.stroke(); ctx.setLineDash([]);
  // Pace marker on today's bar (if today is still incomplete)
  for(let i=0;i<n;i++) {{
    if(paceTarget[i]===target[i]) continue;
    const x=xOf(i), y=yOf(paceTarget[i]);
    ctx.strokeStyle='#e2e8f0'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(x-barW/2-2,y); ctx.lineTo(x+barW/2+2,y); ctx.stroke();
  }}
  // Consumed line
  ctx.strokeStyle='#e2e8f0'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); let s2=false;
  for(let i=0;i<n;i++) {{ if(consumed[i]==null) continue; s2?ctx.lineTo(xOf(i),yOf(consumed[i])):ctx.moveTo(xOf(i),yOf(consumed[i])); s2=true; }}
  ctx.stroke();

  chartAxes(ctx,W,H,lo,hi,3,PAD,dates);

  // Build weight lookup by date for hover
  const wtMap={{}};
  if(DATA.weightDates) DATA.weightDates.forEach((d,i)=>wtMap[d]=DATA.weightVals[i]);
  const weightByDate=dates.map(d=>wtMap[d]??null);

  CHART_META['c-cal']={{dates,PAD,yOf,todayIdx:DATA.calTodayIdx,series:[
    {{label:'Consumed',data:consumed,color:'#e2e8f0',fmt:v=>Math.round(v)+' kcal'}},
    {{label:'Target',data:target,color:'#64748b',fmt:v=>Math.round(v)+' kcal'}},
    ...(paceTarget.some((v,i)=>v!==target[i]) ? [{{label:'Pace target (today)',data:paceTarget.map((v,i)=>v!==target[i]?v:null),color:'#94a3b8',fmt:v=>Math.round(v)+' kcal'}}] : []),
    {{label:'Weight',data:weightByDate,color:'#a78bfa',fmt:v=>v!=null?v.toFixed(1)+' kg':'—'}},
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

  ctx.strokeStyle='rgba(148,163,184,.25)'; ctx.lineWidth=1;
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
  const {{ctx,W,H}}=setupCanvas('c-wt','w-wt');
  const PAD={{top:4,right:10,bottom:22,left:36}};
  if(!DATA.weightDates||!DATA.weightDates.length) {{
    ctx.fillStyle='#334155'; ctx.font='11px sans-serif'; ctx.textAlign='center';
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
  ctx.fillStyle='rgba(167,139,250,.15)';
  ctx.beginPath();
  bandHi.forEach((v,i)=>i===0?ctx.moveTo(xOf(i),yOf(v)):ctx.lineTo(xOf(i),yOf(v)));
  for (let i=bandLo.length-1;i>=0;i--) ctx.lineTo(xOf(i),yOf(bandLo[i]));
  ctx.closePath(); ctx.fill();
  // GP mean — smooth is safe here since xQuery is an evenly-spaced daily grid.
  const meanPts = mean.map((m,i)=>({{x:xOf(i),y:yOf(m)}}));
  ctx.strokeStyle='#a78bfa'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); drawSmooth(ctx,meanPts); ctx.stroke();
  // Scatter of actual measurements
  sValsFull.forEach((v,i)=>{{
    if(v==null) return;
    ctx.fillStyle='#c4b5fd'; ctx.strokeStyle='#0f172a'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.arc(xOf(i),yOf(v),2.5,0,Math.PI*2); ctx.fill(); ctx.stroke();
  }});
  CHART_META['c-wt']={{dates:sDates,PAD,yOf,series:[
    {{label:'Weight (fit)',data:mean,color:'#a78bfa',fmt:v=>v.toFixed(1)+' kg'}},
    {{label:'Logged',data:sValsFull,color:'#c4b5fd',fmt:v=>v.toFixed(1)+' kg'}},
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
  ctx.strokeStyle = '#f87171'; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
  ctx.beginPath();
  for (var i = 0; i < n; i++) {{ i === 0 ? ctx.moveTo(xOf(i), yOf(vals[i])) : ctx.lineTo(xOf(i), yOf(vals[i])); }}
  ctx.stroke();
  // Day dots
  for (var i = 0; i < n; i++) {{
    var v = vals[i];
    ctx.fillStyle = v < 0.20 ? '#4ade80' : v < 0.50 ? '#fbbf24' : '#f87171';
    ctx.beginPath(); ctx.arc(xOf(i), yOf(v), 2, 0, Math.PI * 2); ctx.fill();
  }}
  // Day-of-week labels (Mon…Sun)
  var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  ctx.fillStyle = '#334155'; ctx.font = '7px system-ui'; ctx.textAlign = 'center';
  for (var i = 0; i < n; i++) {{
    var d = new Date(dates[i] + 'T00:00:00');
    ctx.fillText(days[d.getDay()], xOf(i), H - 0.5);
  }}
}}

function drawAll() {{
  drawCtl(); drawTsb(); drawHrv(); drawHil(); drawCalHistory(); drawWeight(); drawSleep();
  drawIllnessSpark();
  ['c-ctl','c-tsb','c-hrv','c-hil','c-cal','c-wt','c-slp'].forEach(function(id) {{
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
  var el = document.getElementById('nutr-section');
  var detail = document.getElementById('nutr-detail');
  // Tooltip: first sentence only + click hint
  if (el) {{
    var clean = text.replace(/&#10;/g, ' ');
    var first = clean.split('. ')[0] + '.';
    el.setAttribute('data-tip', first + '\\n\\nClick for full analysis.');
    el.addEventListener('click', function() {{
      if (!detail) return;
      if (detail.style.display === 'none') {{
        detail.innerHTML = text.replace(/&#10;/g, '<br>');
        detail.style.display = 'block';
      }} else {{
        detail.style.display = 'none';
      }}
    }});
  }}
}}
function setNutritionCoach(text) {{
  var el = document.getElementById('brief-nutr');
  if (el) el.innerHTML = text;
}}
function setNotes(html) {{
  var el = document.getElementById('notes-list');
  if (el) el.innerHTML = html;
}}
function setNutritionBody(html) {{
  var el = document.getElementById('nutr-body');
  if (el) el.innerHTML = html;
}}
function submitFood() {{
  var box = document.getElementById('food-box');
  var btn = document.getElementById('food-add-btn');
  if (!box) return;
  var v = box.value.trim();
  if (!v) return;
  box.disabled = true;
  if (btn) btn.disabled = true;
  var status = document.getElementById('food-status');
  if (status) {{ status.style.display = 'block'; status.textContent = 'Logging…'; status.className = 'food-status pending'; }}
  document.title = '__logfood__' + Date.now() + '|' + encodeURIComponent(v);
}}
function setFoodError(msg) {{
  var box = document.getElementById('food-box');
  var btn = document.getElementById('food-add-btn');
  var status = document.getElementById('food-status');
  if (box) box.disabled = false;
  if (btn) btn.disabled = false;
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
  val.style.color = todayP < 0.20 ? '#4ade80' : todayP < 0.50 ? '#fbbf24' : '#f87171';
  if (sub) {{
    var delta = Math.round((todayP - yesterdayP) * 100);
    sub.textContent = (delta >= 0 ? '+' : '') + delta + 'pp vs yesterday';
  }}
  drawHrv();
  drawIllnessSpark();
}}

// ── Tabs ──────────────────────────────────────────────────────────────────────
var trackerRendered = false;

function selectTab(name) {{
  document.querySelectorAll('.tab').forEach(function(t) {{
    t.setAttribute('aria-selected', String(t.dataset.panel === name));
  }});
  document.querySelectorAll('.panel').forEach(function(p) {{
    p.classList.toggle('active', p.dataset.panel === name);
  }});

  var period = document.getElementById('periodSelect');
  if (period) period.hidden = (name !== 'trends');

  // A canvas inside a hidden panel has zero client size, so charts must be
  // (re)drawn when their panel becomes visible, not merely on load.
  if (name === 'trends') {{
    requestAnimationFrame(drawAll);
  }} else if (name === 'perf') {{
    requestAnimationFrame(function() {{
      if (!trackerRendered) {{ renderTracker(); trackerRendered = true; }}
      else {{ drawRadar(); }}
    }});
  }}
}}

(function() {{
  document.querySelectorAll('.tab').forEach(function(t) {{
    t.addEventListener('click', function() {{ selectTab(t.dataset.panel); }});
  }});

  var noteBox = document.getElementById('note-box');
  if (noteBox) {{
    noteBox.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{ e.preventDefault(); submitNote(); }}
    }});
  }}
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

function toggleNutrExtras() {{
  var extras = document.querySelectorAll('.nutr-extra');
  var btn    = document.getElementById('nutr-toggle');
  var showing = extras[0] && extras[0].style.display !== 'none';
  extras.forEach(function(el) {{ el.style.display = showing ? 'none' : 'flex'; }});
  if (btn) {{ btn.textContent = showing ? '+' : '−'; btn.classList.toggle('active', !showing); }}
}}

function toggleNutrModal(forceClose) {{
  var sec = document.getElementById('nutr-section');
  var bd  = document.getElementById('nutr-backdrop');
  if (!sec) return;
  var open = forceClose ? false : !sec.classList.contains('nutr-modal');
  sec.classList.toggle('nutr-modal', open);
  if (bd) bd.style.display = open ? 'block' : 'none';
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') toggleNutrModal(true);
}});

function triggerRefresh() {{
  var btn = document.getElementById('refresh-btn');
  if (btn) {{ btn.textContent = 'Refreshing…'; btn.disabled = true; }}
  document.title = '__refresh__';
}}

function submitMetric() {{
  var metric = document.getElementById('log-metric').value;
  var value  = parseFloat(document.getElementById('log-value').value);
  var reps   = parseInt(document.getElementById('log-reps').value) || null;
  var notes  = document.getElementById('log-notes').value.trim();
  if (!metric || isNaN(value)) {{ return; }}
  var payload = JSON.stringify({{metric:metric, value:value, reps:reps, notes:notes}});
  document.title = '__logmetric__' + Date.now() + '|' + encodeURIComponent(payload);
  // Clear inputs and confirm
  document.getElementById('log-value').value = '';
  document.getElementById('log-reps').value  = '';
  document.getElementById('log-notes').value = '';
  document.getElementById('log-metric').value = '';
  var conf = document.getElementById('log-confirm');
  if (conf) {{ conf.textContent = '✓ logged'; setTimeout(function(){{ conf.textContent=''; }}, 2500); }}
}}

// ── Performance tracker ───────────────────────────────────────────────────────
function renderTracker() {{
  drawRadar();
  renderMetricCards();
}}

function drawRadar() {{
  var canvas = document.getElementById('c-radar');
  if (!canvas) return;
  var metrics = DATA.metrics || [];

  // User-configured radar groupings (~/.config/intervals-icu/benchmarks.json)
  // — the polygon below is drawn with as many sides as there are axes, so
  // this list alone controls the shape.
  var axes = {radar_axes_json};

  var byKey = {{}};
  metrics.forEach(function(m) {{ byKey[m.key] = m; }});

  // null = no metric in this axis has ever been logged, distinct from a
  // logged metric sitting at 0% progress since baseline. Averaged the same
  // way both used to collapse to 0, making unmeasured axes indistinguishable
  // from measured-but-stalled ones.
  var vals = axes.map(function(ax) {{
    var pcts = ax.keys.map(function(k) {{ return byKey[k] ? byKey[k].pct : null; }}).filter(function(v){{return v!==null;}});
    return pcts.length ? pcts.reduce(function(a,b){{return a+b;}},0)/pcts.length : null;
  }});

  var dpr = window.devicePixelRatio || 1;
  var S = 180;
  canvas.style.width  = S + 'px';
  canvas.style.height = S + 'px';
  canvas.width  = S * dpr;
  canvas.height = S * dpr;
  var ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
  var W = S, H = S;
  var cx = W/2, cy = H/2, r = W/2 - 28;
  var n = axes.length;

  // Background fill
  ctx.fillStyle = '#0f172a';
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
    ctx.strokeStyle = t===1.0 ? '#334155' : '#1e293b';
    ctx.lineWidth = 1; ctx.stroke();
  }});

  // Spokes and labels — dashed/dimmed for axes with no logged data at all,
  // so "unmeasured" reads differently from "measured at 0%".
  for (var i=0; i<n; i++) {{
    var a = i * 2*Math.PI/n - Math.PI/2;
    var hasData = vals[i] !== null;
    ctx.strokeStyle='#1e293b'; ctx.lineWidth=1;
    if (!hasData) ctx.setLineDash([2,2]);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a)); ctx.stroke();
    ctx.setLineDash([]);
    var lx = cx + (r+14)*Math.cos(a), ly = cy + (r+14)*Math.sin(a);
    ctx.fillStyle = hasData ? '#64748b' : '#334155';
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
  ctx.fillStyle='rgba(167,139,250,0.15)'; ctx.fill();
  ctx.strokeStyle='#a78bfa'; ctx.lineWidth=2; ctx.stroke();

  // Target ring (100%)
  ctx.beginPath();
  for (var i=0; i<n; i++) {{
    var a = i * 2*Math.PI/n - Math.PI/2;
    if (i===0) ctx.moveTo(cx+r*Math.cos(a), cy+r*Math.sin(a));
    else ctx.lineTo(cx+r*Math.cos(a), cy+r*Math.sin(a));
  }}
  ctx.closePath(); ctx.strokeStyle='rgba(167,139,250,0.35)'; ctx.lineWidth=1.5;
  ctx.setLineDash([3,3]); ctx.stroke(); ctx.setLineDash([]);

  // Dots
  for (var i=0; i<n; i++) {{
    if (vals[i] === null) continue;
    var pct = Math.min(vals[i], 120) / 100;
    var a = i * 2*Math.PI/n - Math.PI/2;
    ctx.beginPath(); ctx.arc(cx+pct*r*Math.cos(a), cy+pct*r*Math.sin(a), 3, 0, 2*Math.PI);
    ctx.fillStyle='#a78bfa'; ctx.fill();
  }}
}}

function renderMetricCards() {{
  var container = document.getElementById('tracker-cards');
  if (!container) return;
  var metrics = DATA.metrics || [];
  if (!metrics.length) {{
    container.innerHTML = '<div style="color:#475569;font-size:12px;padding:8px">No test results yet — use the form below to log your first result.</div>';
    return;
  }}

  container.innerHTML = metrics.map(function(m) {{
    var jpct     = Math.max(0, Math.min(100, m.journey_pct));
    var valStr   = m.lower ? formatTime(m.value, m.unit) : (m.value + m.unit);
    var repsStr  = (m.reps && !m.lower) ? (' <span style="font-size:11px;color:#475569">×' + m.reps + '</span>') : '';
    var startStr = m.lower ? formatTime(m.start, m.unit) : (m.start + m.unit);
    var tgtStr   = m.lower ? formatTime(m.target, m.unit) : (m.target + m.unit);
    var color    = m.color || '#a78bfa';
    var spark    = renderSparkSVG(m.spark, m.lower, color);
    var jpctCol  = jpct >= 66 ? '#4ade80' : jpct >= 33 ? '#fbbf24' : '#f87171';
    return '<div class="metric-card">' +
      '<div class="mc-label">' + m.label + '</div>' +
      '<div><span class="mc-val" style="color:' + color + '">' + valStr + '</span>' +
      repsStr + ' <span style="font-size:10px;color:' + jpctCol + '">' + Math.round(jpct) + '%</span></div>' +
      '<div class="mc-bar-wrap">' +
        '<div class="mc-bar-fill" style="width:' + jpct + '%;background:' + color + '"></div>' +
      '</div>' +
      '<div class="mc-journey"><span>' + startStr + '</span><span>→ ' + tgtStr + '</span></div>' +
      (m.notes ? '<div class="mc-notes">' + m.notes + '</div>' : '') +
      '<div class="mc-date">Tested ' + m.date + '</div>' +
      spark +
      '</div>';
  }}).join('');
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
  var trendCol = trend > 0 ? '#4ade80' : trend < 0 ? '#f87171' : '#475569';
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
        elif t.startswith("__logmetric__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__logmetric__"):].partition("|")
            try:
                entry_dict = json.loads(_up.unquote(enc))
                add_metric_entry(entry_dict)
                benchmark_cfg = load_benchmark_config()
                wellness = self._last_data[0] if getattr(self, "_last_data", None) else []
                weight_history = [{"date": w["id"], "value": w["weight"]}
                                   for w in wellness if w.get("weight") and w.get("id")]
                new_metrics = build_metrics_data(load_test_metrics().get("entries", []), benchmark_cfg["metrics"],
                                                  weight_history=weight_history)
                metrics_json = json.dumps(new_metrics).replace("\\", "\\\\").replace("'", "\\'")
                GLib.idle_add(
                    lambda: self.wv.run_javascript(
                        f"DATA.metrics={metrics_json}; renderTracker();",
                        None, None, None) or False)
            except Exception:
                log.exception("logmetric failed")
        elif t.startswith("__logfood__"):
            import urllib.parse as _up
            _nonce, _, enc = t[len("__logfood__"):].partition("|")
            description = _up.unquote(enc)
            threading.Thread(target=self._log_food, args=(description,), daemon=True).start()

    def _log_food(self, description):
        """Runs off the GTK thread: delegates the food entry to claude -p, then
        refreshes #nutr-body in place so the user never has to hit refresh."""
        try:
            ok, err = log_food_via_claude(description)
        except Exception:
            log.exception("food log failed")
            ok, err = False, "Failed to log food — try again."
        if not ok:
            msg = (err or "Failed to log food — try again.").replace("\\", "\\\\").replace("'", "\\'")
            GLib.idle_add(
                lambda: self.wv.run_javascript(f"setFoodError('{msg}')", None, None, None) or False)
            return
        self._refresh_nutrition_ui()

    def _refresh_nutrition_ui(self):
        """Rebuild #nutr-body from the current food log + cached nutrition context
        (set in _fetch_and_render) and push it into the running page."""
        ctx = getattr(self, "_nutr_ctx", None)
        if not ctx:
            return
        food_data = get_today_nutrition()
        body_html = render_nutrition_body(food_data, ctx).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
        GLib.idle_add(
            lambda: self.wv.run_javascript(f"setNutritionBody('{body_html}')", None, None, None) or False)
        # Gap may have closed (or changed) — refresh the chart-hover suggestion too.
        try:
            suggestion = get_calorie_gap_suggestion(food_data, ctx)
            js_val = ("'" + suggestion.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "&#10;") + "'") if suggestion else "null"
            GLib.idle_add(
                lambda: self.wv.run_javascript(f"DATA.calGapSuggestion={js_val}", None, None, None) or False)
        except Exception:
            log.exception("calorie gap suggestion refresh failed")

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

    def _load(self, html):
        GLib.idle_add(lambda: self.wv.load_html(html, "file:///") or False)

    def _fetch_and_render(self, force_illness_full=False):
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
            self._load(error_html(str(e)))
            return

        training_plan   = load_training_plan()
        calorie_target  = config.get("calorie_baseline", 2700)
        surplus_target  = config.get("calorie_surplus_target", 0)
        food_data       = get_today_nutrition()
        food_data["_calorie_target"] = calorie_target

        # Grab cached illness data synchronously (instant — no model run)
        illness = get_cached_result() if _ILLNESS_AVAILABLE else None

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
                coach_summary, _ = get_claude_summary(
                    build_data_text(wellness, activities, training_plan, illness=illness))
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

        # Feeds the calorie-chart hover tooltip: if today is meaningfully behind
        # target, precompute a haiku meal suggestion so hovering is instant rather
        # than firing a network call per mousemove.
        def gap_worker():
            try:
                ctx = getattr(self, "_nutr_ctx", None)
                if not ctx:
                    return
                suggestion = get_calorie_gap_suggestion(food_data, ctx)
                if suggestion:
                    _js(f"DATA.calGapSuggestion='{esc_tip(suggestion)}'")
            except Exception:
                log.exception("calorie gap suggestion failed")

        # Let the LLM log any clearly-described upcoming sessions from the notes
        def extract_worker():
            try:
                added = extract_sessions_from_notes(load_notes().get("active", []))
                if added:
                    summary = "; ".join(f"{a['date']} {a['name']}" for a in added)
                    msg = ("✓ Added to plan: " + summary).replace("\\", "\\\\").replace("'", "\\'")
                    _js("var l=document.getElementById('notes-list');"
                        "if(l){var d=document.createElement('div');d.className='note-added';"
                        "d.textContent='" + msg + "';l.insertBefore(d,l.firstChild);}")
            except Exception:
                log.exception("session extraction failed")

        # Run the independent LLM calls concurrently — each claude -p call is ~4-5s,
        # so this cuts a full regenerate from ~4 calls in series to ~2 on the critical path.
        workers = [threading.Thread(target=w, daemon=True)
                   for w in (coach_worker, insight_worker, extract_worker, gap_worker)]
        for w in workers: w.start()
        for w in workers: w.join()

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
