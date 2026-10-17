"""Sleep: derived metrics from the intervals.icu wellness feed + a local qualitative log.

The feed gives per-night duration, sleep score, resting HR and HRV - no stages or
bed/wake times, so nothing here pretends to measure them.  Alex's own ratings live in
~/.local/share/training-brief/sleep-log.json, keyed by the wellness date (the morning
the night ended on), and are never written back to intervals.icu.
"""
import json
import os
import statistics
from datetime import datetime

DATA_DIR = os.path.expanduser("~/.local/share/training-brief")
SLEEP_LOG = os.path.join(DATA_DIR, "sleep-log.json")

TARGET_H = 8.0
LATENCY = ["<15 min", "15-30", "30-60", "60+"]
WAKINGS = ["0", "1", "2", "3+"]
HELPS = ["wind-down routine", "read before bed", "cool dark room", "early to bed", "exercise earlier"]
HURTS = ["screens late", "late meal", "alcohol", "late caffeine", "hot room", "noise",
         "stress", "pain/illness", "hard training late"]
MIN_GROUP = 3        # per-side sample before any comparison is shown


def load_log():
    try:
        with open(SLEEP_LOG) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_rating(obj):
    date = obj.get("date")
    datetime.strptime(date, "%Y-%m-%d")                     # raises on bad input
    rating = int(obj.get("rating"))
    if not 1 <= rating <= 5:
        raise ValueError("rating must be 1-5")
    allowed = set(HELPS + HURTS)
    entry = {
        "rating": rating,
        "latency": obj.get("latency") if obj.get("latency") in LATENCY else "",
        "wakings": obj.get("wakings") if obj.get("wakings") in WAKINGS else "",
        "factors": [f for f in obj.get("factors", []) if f in allowed],
        "note": (obj.get("note") or "").strip()[:500],
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    log = load_log()
    log[date] = entry
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SLEEP_LOG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=1)
    os.replace(tmp, SLEEP_LOG)


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _insights(nights):
    """Plain comparisons over rated nights; only shown when both sides have enough data."""
    rated = [n for n in nights if n.get("rating")]
    out = []
    long_, short = ([n["rating"] for n in rated if n["hours"] >= TARGET_H],
                    [n["rating"] for n in rated if n["hours"] < TARGET_H])
    if len(long_) >= MIN_GROUP and len(short) >= MIN_GROUP:
        out.append({"text": f"Rested rating averages {_mean(long_):.1f} on nights of {TARGET_H:.0f}h+ "
                            f"vs {_mean(short):.1f} on shorter nights",
                    "n": f"{len(long_)} vs {len(short)} nights"})
    diffs = []
    for f in HELPS + HURTS:
        w = [n["rating"] for n in rated if f in n.get("factors", [])]
        wo = [n["rating"] for n in rated if f not in n.get("factors", [])]
        if len(w) >= MIN_GROUP and len(wo) >= MIN_GROUP:
            diffs.append((abs(_mean(w) - _mean(wo)), f, _mean(w), _mean(wo), len(w), len(wo)))
    for _, f, a, b, nw, nwo in sorted(diffs, reverse=True)[:4]:
        out.append({"text": f"With “{f}”: {a:.1f} rested vs {b:.1f} without",
                    "n": f"{nw} vs {nwo} nights"})
    return out


def sleep_payload(wellness, now=None):
    now = now or datetime.now()
    log = load_log()
    rows = sorted((w for w in wellness if w.get("id") and w.get("sleepSecs")), key=lambda w: w["id"])
    nights = []
    for w in rows[-90:]:
        r = log.get(w["id"], {})
        nights.append({"date": w["id"], "hours": round(w["sleepSecs"] / 3600, 2),
                       "score": w.get("sleepScore"), "rhr": w.get("restingHR"), "hrv": w.get("hrv"),
                       "rating": r.get("rating"), "latency": r.get("latency"),
                       "wakings": r.get("wakings"), "factors": r.get("factors", []),
                       "note": r.get("note", "")})
    if not nights:
        return {"nights": [], "last": None}
    last = nights[-1]
    prev = nights[:-1][-7:]
    d14 = [n["hours"] for n in nights[-14:]]
    s14 = [n["score"] for n in nights[-14:] if n["score"]]
    rated14 = [n["rating"] for n in nights[-14:] if n["rating"]]
    base_hrv = _mean([n["hrv"] for n in prev if n["hrv"]])
    base_rhr = _mean([n["rhr"] for n in prev if n["rhr"]])
    return {
        "target_h": TARGET_H,
        "nights": nights,
        "last": last,
        "stats": {
            "avg_h14": round(_mean(d14), 2),
            "sd_h14": round(statistics.pstdev(d14), 2) if len(d14) > 1 else None,
            "avg_score14": round(_mean(s14), 1) if s14 else None,
            "debt7": round(sum(TARGET_H - n["hours"] for n in nights[-7:]), 1),
            "hrv_delta": round(last["hrv"] - base_hrv, 1) if last["hrv"] and base_hrv else None,
            "rhr_delta": round(last["rhr"] - base_rhr, 1) if last["rhr"] and base_rhr else None,
            "avg_rating14": round(_mean(rated14), 1) if rated14 else None,
            "rated_total": sum(1 for n in nights if n["rating"]),
        },
        "insights": _insights(nights),
        "options": {"latency": LATENCY, "wakings": WAKINGS, "helps": HELPS, "hurts": HURTS},
    }
