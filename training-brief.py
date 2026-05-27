#!/usr/bin/env python3
"""Morning training brief — loading screen, retries, charts, coach brief."""

import fcntl
import hashlib
import json
import math
import os
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

LOCK_FILE          = "/tmp/training-brief.lock"
CONFIG_FILE        = os.path.expanduser("~/.config/intervals-icu/config.json")
TRAINING_PLAN_FILE = os.path.expanduser("~/.config/intervals-icu/training-plan.json")
CACHE_FILE         = os.path.expanduser("~/.cache/training-brief/cache.json")
NUTR_CACHE_FILE    = os.path.expanduser("~/.cache/training-brief/nutrition-cache.json")
FOOD_LOG_FILE      = os.path.expanduser("~/.local/share/training-brief/food-log.json")
STRETCH_LOG_FILE   = os.path.expanduser("~/.local/share/training-brief/stretch-log.json")
BASE_URL           = "https://intervals.icu/api/v1"
WINDOW_W, WINDOW_H = 1440, 860


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


# ── Food log ─────────────────────────────────────────────────────────────────

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

def _is_quota_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()

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


def get_nutrition_insight(gemini_key):
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
        return cache.get("insight", ""), cache.get("stale", False)

    if not logged and today_data["entry_count"] == 0:
        return "No meals logged this week — log food with `food add` to get nutritional insights.", False

    from google import genai
    client = genai.Client(api_key=gemini_key, http_options={"timeout": 30})
    prompt = (
        "You are a sports nutritionist advising a Cambridge University rower who trains 2–3 times daily. "
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
    try:
        resp    = client.models.generate_content(model="models/gemini-2.5-flash", contents=prompt)
        insight = resp.text.strip()
        _save_cache(NUTR_CACHE_FILE, {"hash": input_hash, "insight": insight, "stale": False})
        return insight, False
    except Exception as e:
        if _is_quota_error(e):
            old = cache.get("insight", "")
            _save_cache(NUTR_CACHE_FILE, {"hash": input_hash, "insight": old, "stale": True})
            return old, True
        raise


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


def fetch_wellness(athlete_id, api_key, days=760):
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

def compute_hi_load_series(activities, days=730, tau=14):
    decay = math.exp(-1 / tau)
    hi_by_date = defaultdict(float)
    for a in activities:
        if "Row" not in a.get("type", ""):
            continue
        date_str   = (a.get("start_date_local") or a.get("start_date") or "")[:10]
        zone_times = a.get("icu_hr_zone_times") or []
        hi_by_date[date_str] += sum(zone_times[i] for i in range(3, min(7, len(zone_times)))) / 60

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


def compute_hi_load_projection(activities, training_plan, tau=14):
    decay = math.exp(-1 / tau)
    hi_by_date = defaultdict(float)
    for a in activities:
        if "Row" not in a.get("type", ""):
            continue
        date_str   = (a.get("start_date_local") or a.get("start_date") or "")[:10]
        zone_times = a.get("icu_hr_zone_times") or []
        hi_by_date[date_str] += sum(zone_times[i] for i in range(3, min(7, len(zone_times)))) / 60

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

    # If the last wellness entry is from today, apply today's planned sessions
    # (which haven't been uploaded yet but will happen later in the day).
    today_str = today.strftime("%Y-%m-%d")
    if last_date == today_str:
        today_tss = tss_by_date.get(today_str, 0)
        ctl = ctl * ctl_decay + today_tss * ctl_acc
        atl = atl * atl_decay + today_tss * atl_acc

    proj_dates    = [last_date]
    proj_ctl      = [round(ctl, 1)]
    proj_atl      = [round(atl, 1)]
    proj_tsb      = [round(ctl - atl, 1)]
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

def build_data_text(wellness, activities, training_plan=None, food_data=None):
    entries = sorted([w for w in wellness if w.get("id")], key=lambda w: w["id"])
    today   = next((w for w in reversed(entries) if w.get("ctl") or w.get("hrv")), {})
    recent  = entries[-7:]

    ctl = today.get("ctl"); atl = today.get("atl")
    tsb = round(ctl - atl, 1) if ctl and atl else None
    hrv = today.get("hrv"); rhr = today.get("restingHR")
    sleep_score = today.get("sleepScore"); sleep_secs = today.get("sleepSecs")

    hrv_vals     = [w.get("hrv") for w in recent if w.get("hrv")]
    hrv_baseline = sum(hrv_vals[:-1]) / len(hrv_vals[:-1]) if len(hrv_vals) > 1 else None

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

    if training_plan:
        sessions = training_plan.get("sessions", [])
        today_planned = [s for s in sessions if s.get("date", "") == today_str]
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

    return "\n".join(lines)


def get_gemini_summary(data_text, gemini_key):
    """Returns ({"overview": ..., "tips": ...}, is_stale)."""
    input_hash = _data_hash(data_text)
    cache      = _load_cache(CACHE_FILE)
    if cache.get("hash") == input_hash and cache.get("overview"):
        return {"overview": cache["overview"], "tips": cache.get("tips", "")}, cache.get("stale", False)

    from google import genai
    client = genai.Client(api_key=gemini_key, http_options={"timeout": 30})
    prompt = (
        "You are an experienced rowing coach writing a personalised daily brief for a Cambridge University rower.\n\n"

        "Write TWO separate sections. Separate them with exactly the line: ---TIPS---\n\n"

        "SECTION 1 — OVERVIEW (2 sentences, plain factual prose):\n"
        "  • One sentence: readiness read from the specific numbers (TSB, HRV vs 7-day baseline, sleep score).\n"
        "  • One sentence: what's been done today and what's still to do — infer completed sessions from "
        "    the logged activity metrics (type/duration/distance/Z4+ time); report actual logged "
        "    distance/duration, not the planned session name distance (e.g. if 35km was logged for a '50km cycle', say 35km).\n\n"

        "SECTION 2 — TIPS (2–3 sentences, genuinely useful and specific):\n"
        "  Give advice that a knowledgeable coach would give, tailored to today's actual context. "
        "  Must NOT be 'sleep more', 'stay hydrated', or any other generic wellness platitude. "
        "  Pick the most relevant angle from: fuelling timing and composition for today's specific session mix "
        "  (e.g. what to eat between a hard morning piece and an afternoon paddle), "
        "  pacing or technique cues for the remaining sessions, "
        "  caffeine strategy around training, warm-down or mobility work to reduce soreness, "
        "  how fatigue affects stroke mechanics and what to watch for, "
        "  mental approach for performing under fatigue, "
        "  or anything else specific and interesting given today's data.\n\n"

        "Hard rules: no TSS/load numbers. No bullet points, no headers, no greetings, no markdown. "
        "Each section plain prose. Overview ≤50 words, Tips ≤80 words.\n\n"
        + data_text
    )
    try:
        resp = client.models.generate_content(model="models/gemini-2.5-flash", contents=prompt)
        text = resp.text.strip()
        if "---TIPS---" in text:
            overview, tips = text.split("---TIPS---", 1)
            overview = overview.strip(); tips = tips.strip()
        else:
            overview = text; tips = ""
        _save_cache(CACHE_FILE, {"hash": input_hash, "overview": overview, "tips": tips, "stale": False})
        return {"overview": overview, "tips": tips}, False
    except Exception as e:
        if _is_quota_error(e):
            old_overview = cache.get("overview", "")
            old_tips     = cache.get("tips", "")
            _save_cache(CACHE_FILE, {"hash": input_hash, "overview": old_overview, "tips": old_tips, "stale": True})
            return {"overview": old_overview, "tips": old_tips}, True
        raise


NUTR_COACH_CACHE = os.path.expanduser("~/.cache/training-brief/nutrition-coach-cache.json")

def get_nutrition_coach(activities, training_plan, gemini_key, coach_summary=None):
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
        return cache.get("brief", ""), cache.get("stale", False)

    from google import genai
    client = genai.Client(api_key=gemini_key, http_options={"timeout": 30})
    prompt = (
        "You are a sports nutritionist advising a Cambridge University rower. "
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
    try:
        resp  = client.models.generate_content(model="models/gemini-2.5-flash", contents=prompt)
        brief = resp.text.strip()
        _save_cache(NUTR_COACH_CACHE, {"hash": input_hash, "brief": brief, "stale": False})
        return brief, False
    except Exception as e:
        if _is_quota_error(e):
            old = cache.get("brief", "")
            _save_cache(NUTR_COACH_CACHE, {"hash": input_hash, "brief": old, "stale": True})
            return old, True
        raise


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


def _build_sessions_html(training_plan, today_str, activities=None):
    sessions = training_plan.get("sessions", [])
    upcoming = [s for s in sessions if s.get("date", "") >= today_str]

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

        rows.append('<hr class="div"><div class="label" style="margin-bottom:6px">Upcoming</div>')
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

    return "".join(rows)


# ── Main HTML builder ────────────────────────────────────────────────────────

def build_html(wellness, activities, training_plan, summary=None, calorie_target=None, food_data=None):
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
    done_today_h = [a for a in activities
                    if (a.get("start_date_local") or a.get("start_date") or "")[:10] == today_str_h]
    session_kcal = sum(int(a.get("calories") or 0) for a in done_today_h)
    kcal_base    = calorie_target if calorie_target else 2700
    kcal_total   = kcal_base + session_kcal
    if session_kcal:
        kcal_sub  = f"{kcal_base:,} base + {session_kcal:,} burned"
        kcal_disp = f"{kcal_total:,} kcal"
    else:
        kcal_sub  = f"{kcal_base:,} base · no sessions yet"
        kcal_disp = f"{kcal_base:,} kcal"
    kcal_stat = _stat("Calorie Target", kcal_disp, "#94a3b8", kcal_sub)

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
    protein_target_g = 150
    carbs_target_g   = round(kcal_total * 0.55 / 4)
    fat_target_g     = round(kcal_total * 0.25 / 9)
    fiber_target_g   = 30

    # Training-adjusted sugar and sodium targets
    training_hours_today = sum(
        (a.get("elapsed_time") or a.get("moving_time") or 0)
        for a in done_today_h
    ) / 3600
    # Sugar: 50g base + ~30g/hr as fast carb fuel around sessions
    sugar_target_g   = round(50 + training_hours_today * 30)
    # Sodium: 2300mg base + ~750mg/hr to replace sweat losses
    sodium_target_mg = round(2300 + training_hours_today * 750)

    if food_data and food_data.get("entry_count", 0) > 0:
        consumed_kcal = int(food_data["calories"])
        cal_pct       = min(round(consumed_kcal / max(kcal_total, 1) * 100), 100)
        cal_raw_pct   = consumed_kcal / max(kcal_total, 1) * 100
        if cal_raw_pct > 100:   cal_color = "#f87171"
        elif cal_raw_pct >= 80: cal_color = "#22c55e"
        else:                   cal_color = "#3b82f6"

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

        # Raw (uncapped) percentages for status colours
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
        n_meals = food_data["entry_count"]

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

        nutr_html = (
            '<hr class="div">'
            '<div class="nutr-section" id="nutr-section" data-tip="Analysing weekly nutrition…" style="cursor:pointer">'
            '<div class="nutr-cal">'
            '<span class="label">Nutrition</span>'
            '<span style="display:flex;align-items:center;gap:6px">'
            f'<span class="nutr-cal-num">{consumed_kcal:,} / {kcal_total:,} kcal</span>'
            '<button id="nutr-toggle" class="nutr-toggle-btn" onclick="event.stopPropagation();toggleNutrExtras()" title="Toggle extra stats">+</button>'
            '</span>'
            '</div>'
            '<div class="bar-wrap">'
            f'<div class="bar-fill" style="width:{cal_pct}%;background:{cal_color}"></div>'
            '</div>'
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
            f'<div class="sub" style="margin-top:3px">{n_meals} meal{"s" if n_meals != 1 else ""} logged</div>'
            '</div>'
        )
    else:
        nutr_html = (
            '<hr class="div">'
            '<div class="nutr-section" id="nutr-section" data-tip="Analysing weekly nutrition…" style="cursor:pointer">'
            '<div class="nutr-cal">'
            '<span class="label">Nutrition</span>'
            '<span class="nutr-cal-num">— no meals logged today</span>'
            '</div>'
            '</div>'
        )

    stats_html = "".join([
        _stat("Form (TSB)", f"{tsb:+.1f}" if tsb is not None else "—",
              _color_tsb(tsb),
              f"Fitness {ctl:.0f} · Fatigue {atl:.0f}" if ctl and atl else None,
              tip=tsb_tip),
        _stat("HRV", f"{int(hrv)}" if hrv else "—",
              _color_hrv(hrv_delta),
              f"7-day avg {hrv_baseline:.0f}" if hrv_baseline else None,
              tip=hrv_tip),
        _stat("Resting HR", f"{int(rhr)} bpm" if rhr else "—", tip=rhr_tip),
        _stat("Sleep", _fmt_sleep(sleep_secs), _color_sleep(sleep_score),
              f"Score {int(sleep_score)}" if sleep_score else None,
              tip=sleep_tip),
        '<hr class="div">',
        _stat("Sleep Avg (14d)", debt_str, debt_color, "avg vs 8h/night target", tip=debt_tip),
        clearance_stat,
        '<hr class="div">',
        kcal_stat,
        nutr_html,
        '<div id="nutr-detail" style="display:none;margin-top:8px;padding:8px 10px;'
        'background:#071a10;border:1px solid #14532d;border-radius:6px;'
        'font-size:12px;color:#86efac;line-height:1.65"></div>',
        _build_sessions_html(training_plan, today_str_h, activities=activities),
    ])

    chart_entries = [w for w in entries if w.get("ctl") or w.get("atl")]
    chart_dates   = [w["id"] for w in chart_entries]
    ctl_vals      = [w.get("ctl") for w in chart_entries]
    atl_vals      = [w.get("atl") for w in chart_entries]
    tsb_vals      = [round(w["ctl"] - w["atl"], 1) if w.get("ctl") and w.get("atl") else None
                     for w in chart_entries]
    hrv_vals_c    = [w.get("hrv") for w in chart_entries]
    rhr_vals_c    = [w.get("restingHR") for w in chart_entries]

    hil_dates, hil_vals           = compute_hi_load_series(activities)
    proj_hil_dates, proj_hil_vals = compute_hi_load_projection(activities, training_plan)
    sleep_dates, sleep_balance    = compute_sleep_balance(entries)
    proj_dates, proj_ctl, proj_atl, proj_tsb, proj_ctl_sig, proj_atl_sig, proj_tsb_sig = \
        compute_projections(entries, training_plan)
    slp_proj_dates, slp_proj_best, slp_proj_med, slp_proj_trend, slp_avg = \
        compute_sleep_projections(entries)

    plan_sessions = [{"date": s["date"], "name": s.get("name", ""), "tss": s.get("tss", 0)}
                     for s in training_plan.get("sessions", [])]

    cal_target_for_chart = calorie_target if calorie_target else 2700
    cal_hist_dates, cal_hist_consumed, cal_hist_target = get_calorie_history(
        cal_target_for_chart, days=14, activities=activities)

    weight_entries = [w for w in entries if w.get("weight") and w.get("id")]
    weight_dates  = [w["id"] for w in weight_entries]
    weight_vals   = [w["weight"] for w in weight_entries]

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
        "calHistTarget": cal_hist_target,
        "weightDates": weight_dates, "weightVals": weight_vals,
    })

    day_str = datetime.now().strftime("%A %d %B")
    loading_span = '<span class="brief-loading">Generating…</span>'
    if isinstance(summary, dict):
        overview_html = summary.get("overview", "").replace("\n", "<br>") or loading_span
        tips_html     = summary.get("tips", "").replace("\n", "<br>") or loading_span
    else:
        overview_html = loading_span
        tips_html     = loading_span

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f172a;color:#e2e8f0;font-family:-apple-system,'Segoe UI',sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}}
.header{{padding:11px 18px;border-bottom:1px solid #1e293b;display:flex;align-items:center;
         gap:10px;flex-shrink:0}}
.header h1{{font-size:14px;font-weight:600}}
.header .date{{font-size:12px;color:#64748b;flex:1}}
select{{background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:5px;
        padding:3px 8px;font-size:12px;cursor:pointer;outline:none}}
select:hover{{border-color:#475569}}
.body{{display:flex;flex:1;overflow:hidden;min-height:0}}
.stats{{width:185px;padding:12px 14px;border-right:1px solid #1e293b;overflow-y:auto;flex-shrink:0}}
.stat{{margin-bottom:11px}}
.label{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:1px}}
.value{{font-size:18px;font-weight:600}}
.sub{{font-size:11px;color:#64748b;margin-top:1px}}
hr.div{{border:none;border-top:1px solid #1e293b;margin:8px 0 10px}}
.right{{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}}
.charts{{flex:1;padding:8px 10px 4px;display:grid;
         grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr 1fr 0.5fr;
         gap:8px;min-width:0;min-height:0}}
.chart-cell{{display:flex;flex-direction:column;min-height:0}}
.chart-cell.wide{{grid-column:1 / -1}}
.chart-label{{font-size:10px;color:#64748b;text-transform:uppercase;
              letter-spacing:.06em;margin-bottom:3px;flex-shrink:0;display:flex;align-items:center;gap:8px}}
.chart-wrap{{flex:1;min-height:0;position:relative}}
canvas{{position:absolute;top:0;left:0;width:100%;height:100%}}
.brief-carousel{{border-top:1px solid #1e293b;flex-shrink:0;height:120px;position:relative;overflow:hidden}}
.brief-track{{display:flex;height:100%;transition:transform .45s cubic-bezier(.4,0,.2,1);will-change:transform}}
.brief-panel{{min-width:100%;padding:10px 18px 24px;overflow-y:auto;box-sizing:border-box}}
.brief-panel.tips-panel{{background:#0b1929}}
.brief-panel.nutr-panel{{background:#071a10}}
.brief-label{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}}
.brief-text{{font-size:15px;color:#94a3b8;line-height:1.65}}
.tips-text{{color:#cbd5e1;font-size:15px;line-height:1.65}}
.brief-loading{{color:#475569;font-style:italic}}
.brief-dots{{position:absolute;bottom:6px;right:14px;display:flex;gap:7px;align-items:center}}
.dot{{width:6px;height:6px;border-radius:50%;background:#1e293b;cursor:pointer;transition:background .2s}}
.dot.active{{background:#475569}}
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
.bar-wrap {{ height:6px; background:#1e293b; border-radius:3px; margin-bottom:6px; overflow:hidden; }}
.bar-fill {{ height:100%; border-radius:3px; transition:width 0.6s ease; }}
.macro-row {{ display:flex; align-items:center; gap:6px; margin-bottom:5px; }}
.macro-name {{ font-size:10px; color:#64748b; width:14px; flex-shrink:0; }}
.macro-bar-wrap {{ flex:1; height:4px; background:#1e293b; border-radius:2px; overflow:hidden; }}
.macro-bar-fill {{ height:100%; border-radius:2px; transition:width 0.6s ease; }}
.macro-val {{ font-size:10px; color:#475569; white-space:nowrap; }}
.nutr-toggle-btn {{ background:none; border:1px solid #334155; border-radius:3px; color:#64748b;
  font-size:11px; line-height:1; padding:0 4px; cursor:pointer; margin:0; }}
.nutr-toggle-btn:hover {{ background:#1e293b; color:#94a3b8; border-color:#475569; }}
.nutr-toggle-btn.active {{ color:#a78bfa; border-color:#a78bfa; }}
#stale-warn {{ display:none; position:fixed; bottom:10px; right:10px; font-size:16px; cursor:default;
  z-index:9999; }}
#stale-warn .stale-tip {{ display:none; position:absolute; bottom:24px; right:0; background:#1e293b;
  border:1px solid #f59e0b; border-radius:6px; padding:6px 9px; font-size:11px; color:#fcd34d;
  white-space:nowrap; pointer-events:none; }}
#stale-warn:hover .stale-tip {{ display:block; }}
</style></head>
<body>
<div class="header">
  <h1>Morning Brief</h1>
  <span class="date">{day_str}</span>
  <select id="periodSelect" onchange="setPeriod(this.value)">
    <option value="14">14 days</option>
    <option value="30">30 days</option>
    <option value="90" selected>90 days</option>
    <option value="180">6 months</option>
    <option value="365">1 year</option>
    <option value="730">2 years</option>
  </select>
</div>
<div class="body">
  <div class="stats">{stats_html}</div>
  <div class="right">
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
        <div class="chart-label">Calorie Intake vs Target (14 days) · faint = cumulative balance</div>
        <div class="chart-wrap" id="w-cal"><canvas id="c-cal"></canvas></div>
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
    <div class="brief-carousel" id="brief-carousel">
      <div class="brief-track" id="brief-track">
        <div class="brief-panel">
          <div class="brief-label">Overview</div>
          <div class="brief-text" id="brief-overview">{overview_html}</div>
        </div>
        <div class="brief-panel tips-panel">
          <div class="brief-label">Coach's Tips</div>
          <div class="tips-text" id="brief-tips">{tips_html}</div>
        </div>
        <div class="brief-panel nutr-panel">
          <div class="brief-label">Nutrition</div>
          <div class="tips-text" id="brief-nutr"><span class="brief-loading">Generating…</span></div>
        </div>
      </div>
      <div class="brief-dots">
        <span class="dot active" onclick="briefGoTo(0)"></span>
        <span class="dot" onclick="briefGoTo(1)"></span>
        <span class="dot" onclick="briefGoTo(2)"></span>
      </div>
    </div>
  </div>
</div>
<div class="footer">
  <button id="refresh-btn" onclick="triggerRefresh()" style="margin-right:8px">Refresh</button>
  <button onclick="document.title='__close__'">Dismiss</button>
</div>
<div id="tooltip" style="position:fixed;background:#1e293b;border:1px solid #334155;
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
  if(!dates||!dates.length) return;
  const n=dates.length;
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;

  // Primary axis: kcal consumed vs target
  const vals=consumed.filter(v=>v!=null).concat(target);
  const lo=Math.min(...vals)*0.85, hi=Math.max(...vals)*1.08, span=hi-lo;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(n-1,1))*cW;

  // Cumulative balance (right axis)
  const cumBal=[]; let running=0;
  for(let i=0;i<n;i++) {{
    if(consumed[i]!=null) running+=consumed[i]-target[i];
    cumBal.push(consumed[i]!=null?running:null);
  }}
  const cumVals=cumBal.filter(v=>v!=null);
  const cLo=Math.min(...cumVals,0), cHi=Math.max(...cumVals,0);
  const cSpan=Math.max(cHi-cLo,1);
  const yCum=v=>PAD.top+(1-(v-cLo)/cSpan)*cH;

  // Draw cumulative balance fill (faint)
  const zeroY=yCum(0);
  ctx.save();
  for(let i=0;i<n-1;i++) {{
    const v0=cumBal[i],v1=cumBal[i+1]; if(v0==null||v1==null) continue;
    const x0=xOf(i),x1=xOf(i+1);
    const avg=(v0+v1)/2;
    ctx.fillStyle=avg>=0?'rgba(34,197,94,.10)':'rgba(239,68,68,.10)';
    ctx.beginPath(); ctx.moveTo(x0,zeroY); ctx.lineTo(x0,yCum(v0)); ctx.lineTo(x1,yCum(v1)); ctx.lineTo(x1,zeroY); ctx.closePath(); ctx.fill();
  }}
  // Cumulative line (faint)
  ctx.strokeStyle='rgba(148,163,184,.35)'; ctx.lineWidth=1; ctx.lineJoin='round';
  ctx.beginPath();
  let started=false;
  for(let i=0;i<n;i++) {{ if(cumBal[i]==null) continue; started?ctx.lineTo(xOf(i),yCum(cumBal[i])):ctx.moveTo(xOf(i),yCum(cumBal[i])); started=true; }}
  ctx.stroke();
  // Zero line for cumulative
  ctx.strokeStyle='rgba(148,163,184,.2)'; ctx.lineWidth=0.5; ctx.setLineDash([3,4]);
  ctx.beginPath(); ctx.moveTo(PAD.left,zeroY); ctx.lineTo(W-PAD.right,zeroY); ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();

  // Right axis labels for cumulative balance
  ctx.fillStyle='rgba(148,163,184,.45)'; ctx.font='9px sans-serif'; ctx.textAlign='left';
  const cStep=(cHi-cLo)/3||1;
  for(let t=0;t<=3;t++) {{
    const v=cLo+t*cStep, y=yCum(v);
    if(y<PAD.top||y>H-PAD.bottom) continue;
    const label=(v>=0?'+':'')+Math.round(v/100)/10+'k';
    ctx.fillText(label,W-PAD.right+3,y+3);
  }}

  // Bars (surplus/deficit per day)
  const barW=Math.max(2,(cW/n)*0.6);
  for(let i=0;i<n;i++) {{
    const v=consumed[i]; if(v==null) continue;
    const x=xOf(i);
    ctx.fillStyle=v>=target[i]?'rgba(34,197,94,.55)':'rgba(239,68,68,.55)';
    const yTop=Math.min(yOf(v),yOf(target[i])), yBot=Math.max(yOf(v),yOf(target[i]));
    ctx.fillRect(x-barW/2,yTop,barW,Math.max(yBot-yTop,1));
  }}
  // Target line
  ctx.strokeStyle='rgba(148,163,184,.4)'; ctx.lineWidth=1; ctx.setLineDash([4,4]);
  ctx.beginPath();
  target.forEach((v,i)=>i===0?ctx.moveTo(xOf(i),yOf(v)):ctx.lineTo(xOf(i),yOf(v)));
  ctx.stroke(); ctx.setLineDash([]);
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

  CHART_META['c-cal']={{dates,PAD,yOf,series:[
    {{label:'Consumed',data:consumed,color:'#e2e8f0',fmt:v=>Math.round(v)+' kcal'}},
    {{label:'Target',data:target,color:'#64748b',fmt:v=>Math.round(v)+' kcal'}},
    {{label:'Cum. balance',data:cumBal,color:'rgba(148,163,184,.7)',fmt:v=>(v>=0?'+':'')+Math.round(v)+' kcal'}},
    {{label:'Weight',data:weightByDate,color:'#a78bfa',fmt:v=>v!=null?v.toFixed(1)+' kg':'—'}},
  ]}};
}}

function drawWeight() {{
  const {{ctx,W,H}}=setupCanvas('c-wt','w-wt');
  const PAD={{top:4,right:10,bottom:22,left:36}};
  const cutoff=new Date(Date.now()-currentDays*86400000).toISOString().slice(0,10);
  const dates=DATA.weightDates, vals=DATA.weightVals;
  if(!dates||!vals||!vals.length) {{
    ctx.fillStyle='#334155'; ctx.font='11px sans-serif'; ctx.textAlign='center';
    ctx.fillText('No weight data logged',W/2,H/2); return;
  }}
  let idx=dates.findIndex(d=>d>=cutoff); if(idx<0) idx=0;
  const sDates=dates.slice(idx), sVals=vals.slice(idx);
  if(!sVals.length) return;
  const lo=Math.min(...sVals)*0.997, hi=Math.max(...sVals)*1.003, span=hi-lo||1;
  const cW=W-PAD.left-PAD.right, cH=H-PAD.top-PAD.bottom;
  const yOf=v=>PAD.top+(1-(v-lo)/span)*cH;
  const xOf=i=>PAD.left+(i/Math.max(sDates.length-1,1))*cW;
  // Fill under
  ctx.fillStyle='rgba(167,139,250,.12)';
  ctx.beginPath(); ctx.moveTo(xOf(0),H-PAD.bottom);
  sVals.forEach((v,i)=>ctx.lineTo(xOf(i),yOf(v)));
  ctx.lineTo(xOf(sVals.length-1),H-PAD.bottom); ctx.closePath(); ctx.fill();
  // Line
  const pts=sVals.map((v,i)=>v!=null?{{x:xOf(i),y:yOf(v)}}:null).filter(Boolean);
  ctx.strokeStyle='#a78bfa'; ctx.lineWidth=1.5; ctx.lineJoin='round';
  ctx.beginPath(); drawSmooth(ctx,pts); ctx.stroke();
  // Dots
  pts.forEach(p=>{{
    ctx.fillStyle='#a78bfa'; ctx.beginPath(); ctx.arc(p.x,p.y,2.5,0,Math.PI*2); ctx.fill();
  }});
  chartAxes(ctx,W,H,lo,hi,3,PAD,sDates);
  CHART_META['c-wt']={{dates:sDates,PAD,yOf,series:[
    {{label:'Weight',data:sVals,color:'#a78bfa',fmt:v=>v.toFixed(1)+' kg'}},
  ]}};
}}

function drawAll() {{
  drawCtl(); drawTsb(); drawHrv(); drawHil(); drawCalHistory(); drawWeight(); drawSleep();
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

// ── Brief carousel ────────────────────────────────────────────────────────────
(function() {{
  var cur = 0, total = 3, hovered = false, timer = null;
  var track = document.getElementById('brief-track');
  var carousel = document.getElementById('brief-carousel');
  var dots = document.querySelectorAll('.dot');
  var swipeX = 0;

  function goTo(n) {{
    cur = ((n % total) + total) % total;
    track.style.transform = 'translateX(-' + (cur * 100) + '%)';
    dots.forEach(function(d, i) {{ d.classList.toggle('active', i === cur); }});
  }}
  window.briefGoTo = goTo;

  function next() {{ if (!hovered) goTo(cur + 1); }}
  timer = setInterval(next, 9000);

  carousel.addEventListener('mouseenter', function() {{ hovered = true; }});
  carousel.addEventListener('mouseleave', function() {{ hovered = false; }});

  // Drag / swipe
  track.addEventListener('mousedown', function(e) {{ swipeX = e.clientX; }});
  track.addEventListener('mouseup', function(e) {{
    var dx = e.clientX - swipeX;
    if (Math.abs(dx) > 40) goTo(dx < 0 ? cur + 1 : cur - 1);
  }});
  track.style.cursor = 'grab';
  track.addEventListener('mousedown', function() {{ track.style.cursor = 'grabbing'; }});
  track.addEventListener('mouseup',   function() {{ track.style.cursor = 'grab'; }});
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

function triggerRefresh() {{
  var btn = document.getElementById('refresh-btn');
  if (btn) {{ btn.textContent = 'Refreshing…'; btn.disabled = true; }}
  document.title = '__refresh__';
}}
</script></body></html>"""


# ── GTK window ───────────────────────────────────────────────────────────────

_ICON_PATH = os.path.expanduser("~/.local/share/icons/training-brief.svg")


class BriefWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Morning Brief")
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
        t = wv.get_title()
        if t == "__close__":
            Gtk.main_quit()
        elif t == "__refresh__":
            self._refresh()

    def _refresh(self):
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

    def _fetch_and_render(self):
        config     = load_config()
        athlete_id = config["athlete_id"]
        api_key    = config["api_key"]
        gemini_key = config.get("gemini_api_key", "")

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
        food_data       = get_today_nutrition()
        food_data["_calorie_target"] = calorie_target

        # Load charts immediately — coach brief fills in asynchronously
        self._load(build_html(wellness, activities, training_plan,
                               summary=None, calorie_target=calorie_target,
                               food_data=food_data))

        if gemini_key:
            threading.Thread(
                target=self._fetch_gemini,
                args=(wellness, activities, training_plan, gemini_key, food_data),
                daemon=True).start()

    def _fetch_gemini(self, wellness, activities, training_plan, gemini_key, food_data=None):
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

        any_stale     = False
        coach_summary = None
        try:
            coach_summary, stale = get_gemini_summary(
                build_data_text(wellness, activities, training_plan), gemini_key)
            any_stale = any_stale or stale
            overview = esc(coach_summary.get("overview", ""))
            tips     = esc(coach_summary.get("tips", ""))
            GLib.idle_add(
                lambda: self.wv.run_javascript(
                    f"setCoachBrief('{overview}', '{tips}')", None, None, None) or False)
        except Exception:
            GLib.idle_add(
                lambda: self.wv.run_javascript(
                    "setCoachBrief('Brief unavailable.', '')", None, None, None) or False)

        try:
            insight, stale = get_nutrition_insight(gemini_key)
            any_stale = any_stale or stale
            if insight:
                e = esc_tip(insight)
                GLib.idle_add(
                    lambda: self.wv.run_javascript(
                        f"setNutritionInsight('{e}')", None, None, None) or False)
        except Exception:
            pass

        try:
            nutr_brief, stale = get_nutrition_coach(activities, training_plan, gemini_key,
                                                    coach_summary=coach_summary)
            any_stale = any_stale or stale
            if nutr_brief:
                en = esc(nutr_brief)
                GLib.idle_add(
                    lambda: self.wv.run_javascript(
                        f"setNutritionCoach('{en}')", None, None, None) or False)
        except Exception:
            GLib.idle_add(
                lambda: self.wv.run_javascript(
                    "setNutritionCoach('Nutrition brief unavailable.')", None, None, None) or False)

        if any_stale:
            GLib.idle_add(
                lambda: self.wv.run_javascript(
                    "showStaleWarning()", None, None, None) or False)


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
