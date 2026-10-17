#!/usr/bin/env python3
"""One-year-ago tracker: this training block vs the same weeks a year earlier.

Pulls activities + wellness from intervals.icu, aggregates by week (Monday start,
aligned by 364 days so weekdays match), and compares sessions, hours, distance,
HR-zone time and Fitness (CTL).

    python3 one_year_ago.py            # text table, last 8 weeks vs a year ago
    python3 one_year_ago.py --weeks 12 --json out.json
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

CONFIG = os.path.expanduser("~/.config/intervals-icu/config.json")
ALIGN_DAYS = 364  # same weekday one year ago
# Daily dog walks etc. would swamp session counts; they are reported separately.
NON_TRAINING_TYPES = {"Walk"}
ZONES = 7
SPORT_GROUP = {"Ride": "Bike", "VirtualRide": "Bike", "Rowing": "Row", "VirtualRow": "Row",
               "Run": "Run", "VirtualRun": "Run", "WeightTraining": "Gym", "Workout": "Workout",
               "Hike": "Hike"}


import bisect
from concurrent.futures import ThreadPoolExecutor

CACHE_DIR = os.path.expanduser("~/.cache/one-year-ago")
OTHER_CACHE = os.path.join(CACHE_DIR, "other_as_row_zone_secs.json")
FALLBACK_ROW_ZONES = [141, 156, 164, 174, 179, 184, 200]


def _row_zones(activities):
    """Zone upper bounds from the most recent Rowing activity (the athlete's rowing zones)."""
    for a in sorted(activities, key=lambda a: a.get("start_date_local") or "", reverse=True):
        if a.get("type") == "Rowing" and a.get("icu_hr_zones"):
            return list(a["icu_hr_zones"])[:ZONES]
    return FALLBACK_ROW_ZONES


def _zone_secs_from_hr(hr, zones):
    """Seconds per zone. Zone i holds HR <= zones[i] (upper bounds), the last zone is open-ended."""
    out = [0] * ZONES
    for h in hr:
        if h is not None:
            out[min(bisect.bisect_left(zones[:ZONES - 1], h), ZONES - 1)] += 1
    return out


def _load_cache():
    try:
        return json.load(open(OTHER_CACHE))
    except Exception:
        return {"zones": None, "acts": {}}


def prepare(activities, fetch=False):
    """Garmin 'Other' activities were all rowing (confirmed by Alex). Retype them as Rowing and re-score
    their zone time against the rowing HR zones: intervals.icu scored them with run zones (Z2 from 153 bpm)
    so they would otherwise understate zone time. Re-scoring needs the HR stream; results are cached on disk
    and the dashboard passes fetch=False so it never touches the network here.
    Returns (activities copy, number of Other activities that could not be re-scored)."""
    zones = _row_zones(activities)
    cache = _load_cache()
    if cache.get("zones") != zones:
        cache = {"zones": zones, "acts": {}}
    others = [a for a in activities if a.get("type") == "Other" and a.get("name") not in ("Bike",)]
    missing = [a for a in others if a["id"] not in cache["acts"] and a.get("start_date_local", "") >= "2025-09-01"]
    if fetch and missing:
        c = json.load(open(CONFIG))
        auth = ("API_KEY", c["api_key"])

        def one(a):
            r = requests.get(f"https://intervals.icu/api/v1/activity/{a['id']}/streams",
                             params={"types": "heartrate"}, auth=auth, timeout=30)
            r.raise_for_status()
            hr = [x for st in r.json() if st["type"] == "heartrate" for x in st["data"]]
            return a["id"], _zone_secs_from_hr(hr, zones)

        with ThreadPoolExecutor(4) as ex:
            for aid, zs in ex.map(one, missing):
                cache["acts"][aid] = zs
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump(cache, open(OTHER_CACHE, "w"))
    out, unscored = [], 0
    for a in activities:
        if a in others:
            b = dict(a)
            b["type"] = "Rowing"
            if a["id"] in cache["acts"]:
                b["icu_hr_zone_times"] = cache["acts"][a["id"]]
            elif a.get("start_date_local", "") >= "2025-09-01":
                unscored += 1
            out.append(b)
        else:
            out.append(a)
    return out, unscored


def sport_group(t, a=None):
    """Group activity types by the type intervals.icu stores. 'Other' is handled earlier by prepare()."""
    return SPORT_GROUP.get(t, t or "Other")


def _client():
    c = json.load(open(CONFIG))
    auth = ("API_KEY", c["api_key"])
    base = f"https://intervals.icu/api/v1/athlete/{c['athlete_id']}"

    def get(path, **params):
        r = requests.get(base + path, params=params, auth=auth, timeout=30)
        r.raise_for_status()
        return r.json()

    return get


def monday(d):
    return d - timedelta(days=d.weekday())


def fetch_window(get, start, end):
    acts = get("/activities", oldest=str(start), newest=str(end))
    well = get("/wellness", oldest=str(start), newest=str(end))
    return acts, {w["id"]: w for w in well}


def aggregate(acts, wellness, week_starts):
    """One dict per week start, in the order given."""
    weeks = {ws: {"week": str(ws), "sessions": 0, "hours": 0.0, "km": 0.0,
                  "zone_secs": [0] * ZONES, "walk_hours": 0.0, "longest_session_min": 0,
                  "ctl_end": None, "by_type": defaultdict(float), "sport": {}, "load": 0.0} for ws in week_starts}
    for a in acts:
        d = datetime.fromisoformat(a["start_date_local"]).date()
        w = weeks.get(monday(d))
        if w is None:
            continue
        secs = a.get("moving_time") or 0
        if a.get("type") in NON_TRAINING_TYPES:
            w["walk_hours"] += secs / 3600
            continue
        w["sessions"] += 1
        w["load"] += a.get("icu_training_load") or 0
        w["hours"] += secs / 3600
        w["km"] += (a.get("distance") or 0) / 1000
        w["longest_session_min"] = max(w["longest_session_min"], secs // 60)
        w["by_type"][a.get("type") or "Other"] += secs / 3600
        sp = w["sport"].setdefault(sport_group(a.get("type"), a), {"sessions": 0, "secs": 0, "zone_secs": [0] * ZONES})
        sp["sessions"] += 1
        sp["secs"] += secs
        for i, z in enumerate((a.get("icu_hr_zone_times") or [])[:ZONES]):
            w["zone_secs"][i] += z or 0
            sp["zone_secs"][i] += z or 0
    for ws, w in weeks.items():
        end = ws + timedelta(days=6)
        for back in range(7):  # last day in the week that has a CTL value
            rec = wellness.get(str(end - timedelta(days=back)))
            if rec and rec.get("ctl") is not None:
                w["ctl_end"] = round(rec["ctl"], 1)
                break
        w["by_type"] = {k: round(v, 2) for k, v in w["by_type"].items()}
        w["hours"] = round(w["hours"], 2)
        w["km"] = round(w["km"], 1)
        w["walk_hours"] = round(w["walk_hours"], 2)
    return [weeks[ws] for ws in week_starts]


def build(n_weeks=8, today=None):
    today = today or date.today()
    this_monday = monday(today)
    now_weeks = [this_monday - timedelta(weeks=i) for i in range(n_weeks - 1, -1, -1)]
    ago_weeks = [w - timedelta(days=ALIGN_DAYS) for w in now_weeks]
    get = _client()
    out = {"generated": str(today), "now": None, "ago": None}
    for key, weeks in (("now", now_weeks), ("ago", ago_weeks)):
        acts, well = fetch_window(get, weeks[0], weeks[-1] + timedelta(days=6))
        acts, _ = prepare(acts, fetch=True)
        out[key] = aggregate(acts, well, weeks)
    return out


def print_table(data):
    def zsum(w, lo, hi):
        return sum(w["zone_secs"][lo:hi]) / 3600

    hdr = f"{'wk':>2} | {'':<5} {'sess':>4} {'hrs':>5} {'km':>5} {'Z1':>5} {'Z2':>5} {'Z3+':>5} {'CTL':>5} {'long':>5}"
    print(hdr)
    print("-" * len(hdr))
    for i, (n, a) in enumerate(zip(data["now"], data["ago"]), 1):
        for tag, w in (("now", n), ("ago", a)):
            print(f"{i:>2} | {tag:<5} {w['sessions']:>4} {w['hours']:>5.1f} {w['km']:>5.0f} "
                  f"{zsum(w,0,1):>5.1f} {zsum(w,1,2):>5.1f} {zsum(w,2,7):>5.1f} "
                  f"{(w['ctl_end'] if w['ctl_end'] is not None else float('nan')):>5.1f} {w['longest_session_min']:>5}")
        print()


def _window_stats(acts, wellness, start, end):
    """Aggregate activities with start date in [start, end] (inclusive) as one block."""
    ws = monday(start)
    blk = aggregate([a for a in acts
                     if start <= datetime.fromisoformat(a["start_date_local"]).date() <= end],
                    wellness, [ws])[0]
    ctl = None
    for back in range((end - start).days + 1):
        rec = wellness.get(str(end - timedelta(days=back)))
        if rec and rec.get("ctl") is not None:
            ctl = round(rec["ctl"], 1)
            break
    blk["ctl_end"] = ctl
    return blk


def summary(today=None, anchor=None):
    """This week-to-date vs same weekday-to-date a year ago, plus the last 3 completed weeks."""
    today = today or date.today()
    this_mon = monday(today)
    get = _client()
    # anchor = Monday of the comparison week 1; the last 3 completed weeks map onto the 3 from there.
    shift_days = (this_mon - timedelta(weeks=3) - monday(anchor)).days if anchor else ALIGN_DAYS
    lo = this_mon - timedelta(weeks=3) - timedelta(days=shift_days)
    hi_now = today
    now_acts, now_well = fetch_window(get, this_mon - timedelta(weeks=3), hi_now)
    now_acts, _ = prepare(now_acts, fetch=True)
    ago_acts, ago_well = fetch_window(get, lo, today - timedelta(days=shift_days))
    ago_acts, _ = prepare(ago_acts, fetch=True)
    shift = timedelta(days=shift_days)
    out = {"today": str(today), "anchor": str(monday(anchor)) if anchor else None,
           "this_week": {"now": _window_stats(now_acts, now_well, this_mon, today),
                         "ago": _window_stats(ago_acts, ago_well, this_mon - shift, today - shift),
                         "days_in": (today - this_mon).days + 1}}
    last3 = [this_mon - timedelta(weeks=i) for i in (3, 2, 1)]
    out["last3"] = {
        "now": [_window_stats(now_acts, now_well, w, w + timedelta(days=6)) for w in last3],
        "ago": [_window_stats(ago_acts, ago_well, w - shift, w - shift + timedelta(days=6)) for w in last3]}
    return out


def print_summary(sm):
    def row(tag, w):
        z = w["zone_secs"]
        return (f"  {tag:<4} {w['sessions']:>3} sess {w['hours']:>5.1f} h {w['km']:>5.0f} km  "
                f"Z1 {z[0]/3600:>4.1f}  Z2 {z[1]/3600:>4.1f}  Z3+ {sum(z[2:])/3600:>4.1f}  "
                f"CTL {w['ctl_end'] if w['ctl_end'] is not None else '-':>5}  longest {w['longest_session_min']} min")
    tw = sm["this_week"]
    label = f"weeks from restart anchor {sm['anchor']}" if sm.get("anchor") else "same days a year ago"
    print(f"THIS WEEK (day {tw['days_in']} of 7) vs {label}")
    print(row("now", tw["now"])); print(row("ago", tw["ago"]))
    print("\nLAST 3 COMPLETED WEEKS")
    for i, (n, a) in enumerate(zip(sm["last3"]["now"], sm["last3"]["ago"]), 1):
        print(f" week -{4-i}  (w/c {n['week']})")
        print(row("now", n)); print(row("ago", a))
    def tot(ws):
        t = {"sessions": 0, "hours": 0.0, "km": 0.0, "zone_secs": [0]*ZONES, "ctl_end": ws[-1]["ctl_end"],
             "longest_session_min": max(w["longest_session_min"] for w in ws)}
        for w in ws:
            t["sessions"] += w["sessions"]; t["hours"] += w["hours"]; t["km"] += w["km"]
            t["zone_secs"] = [x + y for x, y in zip(t["zone_secs"], w["zone_secs"])]
        return t
    print(" 3-week total")
    print(row("now", tot(sm["last3"]["now"]))); print(row("ago", tot(sm["last3"]["ago"])))


# ---------------------------------------------------------------------------
# Dashboard panel: pure rendering from lists the dashboard already holds.
# ---------------------------------------------------------------------------
DEFAULT_ANCHOR = date(2025, 10, 13)  # Monday of the week last year's training properly restarted

_CSS = """
<style>
.oya{padding:22px 26px;max-width:1180px;color:#e8e9ea}
.oya h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#75777a;margin:22px 0 8px;font-weight:600}
.oya h2:first-child{margin-top:0}
.oya .note{font-size:12px;color:#75777a;margin:4px 0 12px;line-height:1.5}
.oya .cmp{background:#1f2123;border:1px solid #2a2c2f;border-radius:9px;padding:14px 16px;margin-bottom:12px}
.oya table{border-collapse:collapse;width:100%;font-size:13px;table-layout:fixed}
.oya th:first-child{width:230px}
.oya th.zh{color:#4fc3d9}
.oya td.zb{padding:5px 14px}
.oya th.sec,.oya td.sec{color:#75777a;font-weight:400;width:62px}
.oya tr.now td.sec{color:#9a9c9f}
.oya th:nth-child(5){text-align:left;padding-left:14px}
.oya table.ps th:nth-child(5){text-align:right;padding-left:10px}
.oya table.ps th:first-child{width:230px}
.oya th{font-size:10px;color:#75777a;text-transform:uppercase;letter-spacing:.05em;font-weight:600;text-align:right;padding:4px 10px}
.oya th:first-child,.oya td:first-child{text-align:left}
.oya td{padding:5px 10px;text-align:right;font-variant-numeric:tabular-nums;border-top:1px solid #2a2c2f}
.oya td.lbl{color:#9a9c9f}
.oya tr.now td{color:#e8e9ea;font-weight:600}
.oya tr.ago td{color:#9a9c9f}
.oya .up{color:#5ecb8a}.oya .down{color:#e0766b}.oya .flat{color:#75777a}
.oya .zbar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:#2a2c2f;min-width:120px}
.oya .zbar i{display:block;height:100%}
.oya .z1{background:#5b6b7a}.oya .z2{background:#4fc3d9}.oya .z3{background:#e0a84f}
.oya .race{background:#1f2123;border:1px solid #2a2c2f;border-radius:9px;padding:12px 16px 10px;margin-bottom:12px}
.oya .race .rt{display:flex;justify-content:space-between;font-size:11px;color:#75777a;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.oya .race .rt b{color:#e8e9ea;text-transform:none;letter-spacing:0;font-size:12px;font-weight:600}
.oya .lane{display:grid;grid-template-columns:150px 1fr 74px;align-items:center;gap:10px;margin:6px 0}
.oya .lane .ln{font-size:12px;color:#9a9c9f;text-align:right}
.oya .lane.win .ln{color:#e8e9ea;font-weight:600}
.oya .lane .lv{font-size:12px;color:#9a9c9f;font-variant-numeric:tabular-nums}
.oya .lane.win .lv{color:#e8e9ea;font-weight:600}
.oya .water{position:relative;height:40px;border-radius:20px;overflow:hidden;border:1px solid #2a3b45;
  background:
    repeating-linear-gradient(100deg,rgba(255,255,255,.035) 0 2px,transparent 2px 18px),
    linear-gradient(#1a2a33,#1e3340 50%,#1a2a33)}
.oya .boat{position:absolute;top:4px;left:calc((100% - 112px) * var(--f));animation:oyasail 1.8s cubic-bezier(.2,.7,.2,1)}
.oya .boat svg{display:block;overflow:visible}
.oya .boat svg .ot{animation:oyaT .9s ease-in-out 2}
.oya .boat svg .ob{animation:oyaB .9s ease-in-out 2}
@keyframes oyaT{0%,100%{transform:rotate(0)}25%{transform:rotate(-15deg)}75%{transform:rotate(15deg)}}
@keyframes oyaB{0%,100%{transform:rotate(0)}25%{transform:rotate(15deg)}75%{transform:rotate(-15deg)}}
.oya .finish{position:absolute;top:0;bottom:0;left:calc((100% - 8px) * var(--f));width:8px;opacity:.85;
  background:repeating-conic-gradient(#e8e9ea 0 25%,#17181a 0 50%) 0 0/8px 8px}
.oya .wake{position:absolute;top:13px;height:14px;left:0;width:calc((100% - 112px) * var(--f) + 24px);
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.07) 60%,rgba(255,255,255,.2));
  border-radius:0 7px 7px 0;filter:blur(1.5px);animation:oyawake 1.8s cubic-bezier(.2,.7,.2,1)}
.oya .lane.win .water{box-shadow:0 0 0 1px rgba(79,195,217,.35)}
@keyframes oyasail{from{left:0}}
@keyframes oyawake{from{width:24px}}
@media (prefers-reduced-motion:reduce){.oya .boat,.oya .wake,.oya .boat svg .ot,.oya .boat svg .ob{animation:none}}
.oya .legend{font-size:11px;color:#75777a;margin-top:6px}
.oya .legend b{display:inline-block;width:8px;height:8px;border-radius:2px;margin:0 4px 0 10px;vertical-align:baseline}
.oya .legend b:first-child{margin-left:0}
</style>
"""


def _zbar(w):
    z = w["zone_secs"]
    tot = sum(z) or 1
    parts = [("z1", z[0]), ("z2", z[1]), ("z3", sum(z[2:]))]
    return '<div class="zbar">' + "".join(
        f'<i class="{c}" style="width:{100 * v / tot:.1f}%" title="{v / 3600:.1f} h"></i>' for c, v in parts) + "</div>"


def _delta(now, ago, unit="", fmt="{:+.1f}", higher_is_better=True):
    if now is None or ago is None:
        return '<span class="flat">–</span>'
    d = now - ago
    cls = "flat" if abs(d) < 1e-9 else ("up" if (d > 0) == higher_is_better else "down")
    return f'<span class="{cls}">{fmt.format(d)}{unit}</span>'


def _row(tag, w, cls):
    z = w["zone_secs"]
    ctl = f"{w['ctl_end']:.0f}" if w["ctl_end"] is not None else "–"
    return (f'<tr class="{cls}"><td class="lbl">{tag}</td><td>{z[0] / 3600:.1f}</td><td>{z[1] / 3600:.1f}</td>'
            f'<td>{sum(z[2:]) / 3600:.1f}</td><td class="zb">{_zbar(w)}</td><td class="sec">{w["hours"]:.1f}</td>'
            f'<td class="sec">{w["sessions"]}</td><td class="sec">{w["km"]:.0f}</td><td class="sec">{ctl}</td></tr>')


def _delta_row(n, a):
    return ('<tr><td class="lbl">Δ</td>'
            f'<td>{_delta(n["zone_secs"][0] / 3600, a["zone_secs"][0] / 3600)}</td>'
            f'<td>{_delta(n["zone_secs"][1] / 3600, a["zone_secs"][1] / 3600)}</td>'
            f'<td>{_delta(sum(n["zone_secs"][2:]) / 3600, sum(a["zone_secs"][2:]) / 3600)}</td><td></td>'
            f'<td class="sec">{_delta(n["hours"], a["hours"])}</td>'
            f'<td class="sec">{_delta(n["sessions"], a["sessions"], fmt="{:+.0f}")}</td>'
            f'<td class="sec">{_delta(n["km"], a["km"], fmt="{:+.0f}")}</td>'
            f'<td class="sec">{_delta(n["ctl_end"], a["ctl_end"], fmt="{:+.0f}")}</td></tr>')


_HEAD = ('<tr><th></th><th class="zh">Z1 h</th><th class="zh">Z2 h</th><th class="zh">Z3+ h</th><th>Time in zone</th>'
         '<th class="sec">Hours</th><th class="sec">Sess</th><th class="sec">Km</th><th class="sec">CTL</th></tr>')


def _total(ws):
    t = {"sessions": 0, "hours": 0.0, "km": 0.0, "zone_secs": [0] * ZONES, "ctl_end": ws[-1]["ctl_end"],
         "longest_session_min": max(w["longest_session_min"] for w in ws), "sport": {}, "load": 0.0}
    for w in ws:
        t["load"] += w["load"]
        t["sessions"] += w["sessions"]
        t["hours"] += w["hours"]
        t["km"] += w["km"]
        t["zone_secs"] = [x + y for x, y in zip(t["zone_secs"], w["zone_secs"])]
        for k, v in w["sport"].items():
            d = t["sport"].setdefault(k, {"sessions": 0, "secs": 0, "zone_secs": [0] * ZONES})
            d["sessions"] += v["sessions"]
            d["secs"] += v["secs"]
            d["zone_secs"] = [x + y for x, y in zip(d["zone_secs"], v["zone_secs"])]
    return t


def _ps(w):
    n = w["sessions"] or 1
    z = w["zone_secs"]
    return {"hours": w["hours"] / n, "z1": z[0] / 3600 / n, "z2": z[1] / 3600 / n,
            "z3": sum(z[2:]) / 3600 / n, "q": (sum(z[1:]) / 3600) / n}


def _boat(color):
    """Top-down sweep eight, bow to the right. One oar per rower, alternating sides from the bow
    (seat 1 starboard = bottom of the picture), each pivoting at its rigger with the handle crossing
    the hull and the blade outboard. Seats run bow (right) to stroke (left); cox at the stern."""
    seats = [99 - 11 * i for i in range(8)]  # seat 1 (bow) .. seat 8 (stroke)
    oars, rowers = [], []
    for i, x in enumerate(seats):
        if i % 2 == 0:   # bow seat and every second seat: starboard, blade at the bottom
            px, py, bx, by, hx, hy, cls, rot = x, 19, x - 8, 30.5, x + 3.6, 14, "ob", 30
        else:            # port side, blade at the top
            px, py, bx, by, hx, hy, cls, rot = x, 13, x - 8, 1.5, x + 3.6, 18, "ot", -30
        oars.append(
            f'<g class="{cls}" style="transform-origin:{px}px {py}px">'
            f'<line x1="{bx}" y1="{by}" x2="{hx}" y2="{hy}" stroke="#cfd2d6" stroke-width="1.1" stroke-linecap="round"/>'
            f'<rect x="{bx - 3.5}" y="{by - 1.3}" width="6" height="2.6" rx="1.2" fill="{color}" '
            f'transform="rotate({rot} {bx} {by})"/></g>')
        rowers.append(f'<circle cx="{x}" cy="16" r="2.3" fill="#e8e9ea"/>')
    return ('<svg width="112" height="32" viewBox="0 0 112 32">'
            + "".join(oars) +
            f'<path d="M2 16 Q8 13.2 18 13 L96 13 Q108 14 111 16 Q108 18 96 19 L18 19 Q8 18.8 2 16Z" fill="{color}"/>'
            '<path d="M18 14.2 L96 14.2" stroke="rgba(255,255,255,.35)" stroke-width="0.8"/>'
            + "".join(rowers) +
            '<circle cx="9" cy="16" r="2.3" fill="#f2c94c"/></svg>')


def _race(title, lanes, finish=None, sub=""):
    """lanes: [(label, value, colour)]. Boats advance in proportion to value; optional finish line value."""
    top = max([v for _, v, _ in lanes] + ([finish] if finish else []) + [1]) * 1.08
    best = max(v for _, v, _ in lanes)
    out = [f'<div class="race"><div class="rt"><span>{title}</span><b>{sub}</b></div>']
    for label, v, col in lanes:
        f = max(0.0, min(1.0, v / top))
        win = " win" if v == best and best > 0 else ""
        fin = f'<div class="finish" style="--f:{finish / top:.4f}"></div>' if finish else ""
        out.append(f'<div class="lane{win}"><div class="ln">{label}</div>'
                   f'<div class="water">{fin}<div class="wake" style="--f:{f:.4f}"></div>'
                   f'<div class="boat" style="--f:{f:.4f}">{_boat(col)}</div></div>'
                   f'<div class="lv">{v:.0f} load</div></div>')
    out.append("</div>")
    return "".join(out)


def _lead(now, then, unit="load"):
    d = now - then
    if abs(d) < 0.5:
        return "Dead heat"
    return f"Now ahead by {d:.0f} {unit}" if d > 0 else f"Last year ahead by {-d:.0f} {unit}"


def _per_session_card(tn, ta):
    a, b = _ps(tn), _ps(ta)
    head = ('<tr><th></th><th class="zh">Z1 h / sess</th><th class="zh">Z2 h / sess</th><th class="zh">Z3+ h / sess</th>'
            '<th class="zh">Z2+ h / sess</th><th class="sec">Hours / sess</th></tr>')

    def r(tag, d, cls):
        return (f'<tr class="{cls}"><td class="lbl">{tag}</td><td>{d["z1"]:.2f}</td><td>{d["z2"]:.2f}</td>'
                f'<td>{d["z3"]:.2f}</td><td>{d["q"]:.2f}</td><td class="sec">{d["hours"]:.2f}</td></tr>')

    delta = ('<tr><td class="lbl">Δ</td>' + "".join(
        f'<td{c}>{_delta(a[k], b[k], fmt="{:+.2f}")}</td>' for k, c in
        (("z1", ""), ("z2", ""), ("z3", ""), ("q", ""), ("hours", ' class="sec"'))) + "</tr>")
    return ('<div class="cmp"><div class="note" style="margin-top:0">Per session, 3-week totals divided by '
            "session count (Z2+ = Z2 and above)</div>"
            f'<table class="ps">{head}{r("Now", a, "now")}{r("Then", b, "ago")}{delta}</table></div>')


def _by_sport_card(tn, ta):
    groups = sorted(set(tn["sport"]) | set(ta["sport"]),
                    key=lambda k: -(tn["sport"].get(k, {}).get("secs", 0) + ta["sport"].get(k, {}).get("secs", 0)))
    empty = {"sessions": 0, "secs": 0, "zone_secs": [0] * ZONES}
    head = ('<tr><th>Sport</th><th class="zh">Sess now</th><th class="zh">Sess then</th>'
            '<th class="zh">Z2+ h now</th><th class="zh">Z2+ h then</th>'
            '<th class="sec">Min/sess now</th><th class="sec">Min/sess then</th></tr>')
    rows = []
    for g in groups:
        n, a = tn["sport"].get(g, empty), ta["sport"].get(g, empty)
        qn, qa = sum(n["zone_secs"][1:]) / 3600, sum(a["zone_secs"][1:]) / 3600
        mn = f'{n["secs"] / 60 / n["sessions"]:.0f}' if n["sessions"] else "–"
        ma = f'{a["secs"] / 60 / a["sessions"]:.0f}' if a["sessions"] else "–"
        rows.append(f'<tr class="now"><td class="lbl">{g}</td><td>{n["sessions"]}</td><td>{a["sessions"]}</td>'
                    f'<td>{qn:.1f}</td><td>{qa:.1f}</td><td class="sec">{mn}</td><td class="sec">{ma}</td></tr>')
    return ('<div class="cmp"><div class="note" style="margin-top:0">By sport, 3-week totals. Last year\'s Garmin &#8216;Other&#8217; activities were rowing '
            "(confirmed), so they are shown as Row, with zone time re-scored against your rowing HR zones "
            "from the heart-rate data. Garmin &#8216;Cardio&#8217; sessions show as Workout.</div>"
            f'<table class="ps">{head}{"".join(rows)}</table></div>')


def _view(activities, wellness, today, shift_days, ago_label):
    this_mon = monday(today)
    shift = timedelta(days=shift_days)
    tw_now = _window_stats(activities, wellness, this_mon, today)
    tw_ago = _window_stats(activities, wellness, this_mon - shift, today - shift)
    tw_ago_full = _window_stats(activities, wellness, this_mon - shift, this_mon - shift + timedelta(days=6))
    l3 = [this_mon - timedelta(weeks=i) for i in (3, 2, 1)]
    now3 = [_window_stats(activities, wellness, w, w + timedelta(days=6)) for w in l3]
    ago3 = [_window_stats(activities, wellness, w - shift, w - shift + timedelta(days=6)) for w in l3]
    days_in = (today - this_mon).days + 1
    tn3, ta3 = _total(now3), _total(ago3)
    out = [_race(f"This week so far (day {days_in} of 7)",
                 [("Now", tw_now["load"], "#4fc3d9"), ("Then, same days", tw_ago["load"], "#e0a84f")],
                 finish=tw_ago_full["load"], sub=_lead(tw_now["load"], tw_ago["load"]) +
                 f" &middot; finish = last year&#8217;s full week ({tw_ago_full['load']:.0f})"),
           _race("Last 3 completed weeks",
                 [("Now", tn3["load"], "#4fc3d9"), ("Then", ta3["load"], "#e0a84f")],
                 sub=_lead(tn3["load"], ta3["load"])),
           f'<div class="cmp"><table>{_HEAD}'
           f'{_row(f"This week (day {days_in} of 7)", tw_now, "now")}'
           f'{_row(ago_label(this_mon - shift, today - shift), tw_ago, "ago")}'
           f'{_delta_row(tw_now, tw_ago)}'
           f'{_row("Then, full week Mon&#8211;Sun (" + (this_mon - shift).strftime("%d %b") + " &#8211; " + (this_mon - shift + timedelta(days=6)).strftime("%d %b %y") + ")", tw_ago_full, "ago")}'
           '</table></div>']
    rows = []
    for n, a in zip(now3, ago3):
        wk = date.fromisoformat(n["week"])
        rows.append(_row(f"w/c {wk.strftime('%d %b')}", n, "now"))
        rows.append(_row(ago_label(wk - shift, wk - shift + timedelta(days=6)), a, "ago"))
    tn, ta = _total(now3), _total(ago3)
    rows.append(_row("3-week total", tn, "now"))
    rows.append(_row("3-week total, then", ta, "ago"))
    rows.append(_delta_row(tn, ta))
    out.append(f'<div class="cmp"><table>{_HEAD}{"".join(rows)}</table></div>')
    out.append(_per_session_card(tn, ta))
    out.append(_by_sport_card(tn, ta))
    return "".join(out)


def render_panel(activities, wellness_entries, anchor=None, today=None):
    """HTML for the dashboard's 'Vs Last Year' tab. No network access."""
    today = today or date.today()
    anchor = monday(anchor or DEFAULT_ANCHOR)
    activities, unscored = prepare(activities)
    wellness = {w["id"]: w for w in wellness_entries if w.get("id")}
    this_mon = monday(today)
    anchored_shift = (this_mon - timedelta(weeks=3) - anchor).days

    def lbl(a, b):
        return f"Then ({a.strftime('%d %b')} – {b.strftime('%d %b %y')})"

    html = [_CSS, '<div class="oya">',
            "<h2>Same week last year</h2>",
            f'<div class="note">This week against the closest matching Monday&#8211;Sunday week a year ago '
            f"(w/c {(this_mon - timedelta(days=ALIGN_DAYS)).strftime('%d %b %Y')}), plus your last 3 completed weeks "
            "against the matching 3 weeks then. Led by time in each HR zone; totals are secondary.</div>",
            _view(activities, wellness, today, ALIGN_DAYS, lbl),
            "<h2>Aligned to last year&#8217;s restart</h2>",
            f'<div class="note">The same comparison, but lining your last 3 completed weeks up with the 3 weeks from '
            f"{anchor.strftime('%d %b %Y')}, when training properly restarted last year. Last year was still "
            "restarting in the same-calendar weeks above, so this is the fairer test of the two.</div>",
            _view(activities, wellness, today, anchored_shift, lbl),
            '<div class="legend"><b class="z1" style="background:#5b6b7a"></b>Z1'
            '<b class="z2" style="background:#4fc3d9"></b>Z2'
            '<b class="z3" style="background:#e0a84f"></b>Z3+</div>',
            '<div class="note">Walks are excluded. Zone times use each sport\'s own HR zones, unchanged since last year. '
            "Last year\'s &#8216;Other&#8217; sessions (rowing) were re-scored to your rowing zones. "
            + (f"{unscored} could not be re-scored yet (run one_year_ago.py --warm); those use the stored run-zone "
               "times and understate zone time. " if unscored else "")
            + "The sport mix differs between periods.</div>",
            "</div>"]
    return "".join(html)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=8)
    ap.add_argument("--json")
    ap.add_argument("--anchor", help="YYYY-MM-DD: compare to the 3 weeks starting here (a year-ago restart point) instead of the literal date match")
    ap.add_argument("--warm", action="store_true",
                    help="fetch HR streams for last year's Other (rowing) activities into the on-disk cache")
    ap.add_argument("--summary", action="store_true",
                    help="this week vs same week a year ago + last 3 completed weeks")
    args = ap.parse_args()
    if args.warm:
        acts = _client()("/activities", oldest="2025-09-01", newest=str(date.today()))
        _, left = prepare(acts, fetch=True)
        print("cache ready" if not left else f"{left} activities still not re-scored")
        raise SystemExit
    if args.summary:
        sm = summary(anchor=date.fromisoformat(args.anchor) if args.anchor else None)
        print_summary(sm)
        if args.json:
            json.dump(sm, open(args.json, "w"), indent=2)
        raise SystemExit
    data = build(args.weeks)
    print_table(data)
    if args.json:
        json.dump(data, open(args.json, "w"), indent=2)
        print("wrote", args.json)
