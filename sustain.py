"""Sustainable load: how much can I train today without denting tomorrow's HRV/RHR state?

State (one number per morning, in units of Alex's own day-to-day SD; + = fresher):
    s_t = (z[ln HRV_t] - z[RHR_t]) / 2,    z = (x - trailing 42d mean) / trailing 42d SD

Model (fit on the days where all of it is observed):
    s_{t+1} = a + b s_t + c0 u_t + c1 u_{t-1} + c2 u_{t-2} + c3 u_{t-3} + eps,   u = icu_training_load / 40

Fit is Student-t (nu=4, IRLS) with exponentially decaying time weights (half-life 120 d), so recent
behaviour dominates and the fatigue/rebound terms can drift with fitness.  Posterior draws of the
coefficients are shipped to the page, which does the load-vs-risk arithmetic live (sliders).

Nothing here is written back to intervals.icu.
"""
import math
from datetime import date, datetime, timedelta

import numpy as np

try:
    from sustain_ceiling import ceiling_payload as _ceiling_payload
except Exception:      # the ceiling model is optional
    _ceiling_payload = None

K = 4               # lags of load in the model
BASE_WIN = 42       # days in the trailing baseline for the z-scores
BASE_MIN = 20
HALF_LIFE = 120.0   # days; recency weighting of the fit
NU = 4.0            # Student-t degrees of freedom
N_DRAWS = 300
MIN_ROWS = 60
SCALE = 40.0        # load units per u
RECENT_DAYS = 42


def _daily(wellness, activities):
    """Date-indexed arrays: ln HRV, RHR, sleep score, load."""
    rows = {w["id"]: w for w in wellness if w.get("id")}
    hrv_days = sorted(d for d, w in rows.items() if w.get("hrv"))
    if not hrv_days:
        return None
    d0 = datetime.strptime(hrv_days[0], "%Y-%m-%d").date()
    d1 = max(datetime.strptime(max(rows), "%Y-%m-%d").date(), date.today())
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    n = len(days)
    lnhrv = np.full(n, np.nan)
    rhr = np.full(n, np.nan)
    sleep = np.full(n, np.nan)
    hours = np.full(n, np.nan)
    for i, d in enumerate(days):
        w = rows.get(d.isoformat())
        if not w:
            continue
        if w.get("hrv"):
            lnhrv[i] = math.log(w["hrv"])
        if w.get("restingHR"):
            rhr[i] = w["restingHR"]
        if w.get("sleepScore"):
            sleep[i] = w["sleepScore"]
        if w.get("sleepSecs"):
            hours[i] = w["sleepSecs"] / 3600.0
    idx = {d.isoformat(): i for i, d in enumerate(days)}
    load = np.zeros(n)
    for a in activities:
        ds = (a.get("start_date_local") or "")[:10]
        if ds in idx:
            load[idx[ds]] += a.get("icu_training_load") or 0
    return days, lnhrv, rhr, sleep, load, hours


def _trailing_z(x):
    """z-score of x[i] against the mean/SD of the previous BASE_WIN days (today excluded)."""
    z = np.full(len(x), np.nan)
    for i in range(len(x)):
        win = x[max(0, i - BASE_WIN):i]
        win = win[~np.isnan(win)]
        if len(win) >= BASE_MIN and not np.isnan(x[i]):
            sd = win.std(ddof=1)
            if sd > 0:
                z[i] = (x[i] - win.mean()) / sd
    return z


def _trailing_stats(x):
    """Like _trailing_z but also returns the trailing mean and SD themselves."""
    z = np.full(len(x), np.nan)
    mu = np.full(len(x), np.nan)
    sd = np.full(len(x), np.nan)
    for i in range(len(x)):
        win = x[max(0, i - BASE_WIN):i]
        win = win[~np.isnan(win)]
        if len(win) >= BASE_MIN:
            m, s_ = win.mean(), win.std(ddof=1)
            mu[i], sd[i] = m, s_
            if s_ > 0 and not np.isnan(x[i]):
                z[i] = (x[i] - m) / s_
    return z, mu, sd


def _fit(X, y, ages):
    """Student-t IRLS with time weights.  Returns beta, cov, sigma, effective weights."""
    wt = 0.5 ** (ages / HALF_LIFE)
    w = np.ones(len(y))
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    sigma2 = np.var(y - X @ beta)
    for _ in range(30):
        W = wt * w
        XtW = X.T * W
        beta = np.linalg.solve(XtW @ X, XtW @ y)
        r = y - X @ beta
        w = (NU + 1.0) / (NU + r ** 2 / sigma2)
        sigma2 = float(np.sum(wt * w * r ** 2) / np.sum(wt))
    W = wt * w
    XtW = X.T * W
    cov = sigma2 * np.linalg.inv(XtW @ X)
    return beta, cov, math.sqrt(sigma2)


def _metric_model(x, sign, u, n, days, ln, label, unit, rng):
    """Same lag model fitted to one metric on its own (state = sign*z, so + is always 'fresher'),
    so HRV and resting HR can each be projected in their own units."""
    z, mu, sd = _trailing_stats(x)
    st = sign * z
    rows, ys, ages = [], [], []
    for t in range(K - 1, n - 1):
        if np.isnan(st[t]) or np.isnan(st[t + 1]):
            continue
        rows.append([1.0, st[t]] + [u[t - k] for k in range(K)])
        ys.append(st[t + 1])
        ages.append(n - 1 - t)
    if len(rows) < MIN_ROWS:
        return None
    beta, cov, sigma = _fit(np.array(rows), np.array(ys), np.array(ages, float))
    draws = rng.multivariate_normal(beta, cov, 150)
    last = max((i for i in range(n) if not np.isnan(st[i])), default=None)
    if last is None or np.isnan(mu[last]):
        return None

    def raw(v):
        return None if np.isnan(v) else (math.exp(v) if ln else float(v))
    recent = []
    for i in range(max(0, n - 60), n):
        recent.append({"d": days[i].isoformat(), "v": _nz(raw(x[i]), 1) if not np.isnan(x[i]) else None,
                       "mu": _nz(mu[i], 4), "sd": _nz(sd[i], 4)})
    return {"label": label, "unit": unit, "ln": ln, "sign": sign,
            "s_now": round(float(st[last]), 3), "mu": round(float(mu[last]), 4), "sd": round(float(sd[last]), 4),
            "sigma": round(sigma, 3), "beta": [round(float(b), 4) for b in beta],
            "draws": [[round(float(v), 4) for v in d] for d in draws], "recent": recent}


def _nz(x, nd=3):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def sustain_payload(wellness, activities, illness=None, illness_paused=False):
    """Everything the Load tab needs, or {'ok': False, 'why': ...}."""
    try:
        return _payload(wellness, activities, illness, illness_paused)
    except Exception as e:  # never take the dashboard down
        return {"ok": False, "why": f"model failed: {e}"}


def _payload(wellness, activities, illness, illness_paused):
    dd = _daily(wellness, activities)
    if dd is None:
        return {"ok": False, "why": "no HRV data yet"}
    days, lnhrv, rhr, sleep, load, hours = dd
    n = len(days)
    zh, zr = _trailing_z(lnhrv), _trailing_z(rhr)
    s = (zh - zr) / 2.0                      # NaN unless both observed
    u = load / SCALE

    # Design: row t predicts s[t+1] from s[t], u[t..t-K+1]
    rows, ys, ages = [], [], []
    for t in range(K - 1, n - 1):
        if np.isnan(s[t]) or np.isnan(s[t + 1]):
            continue
        rows.append([1.0, s[t]] + [u[t - k] for k in range(K)])
        ys.append(s[t + 1])
        ages.append(n - 1 - t)
    if len(rows) < MIN_ROWS:
        return {"ok": False, "why": f"only {len(rows)} usable days (need {MIN_ROWS})"}
    X, y, ages = np.array(rows), np.array(ys), np.array(ages, float)
    beta, cov, sigma = _fit(X, y, ages)
    rng = np.random.default_rng(0)
    draws = rng.multivariate_normal(beta, cov, N_DRAWS)
    se = np.sqrt(np.diag(cov))

    # Residuals for every day with a prediction (what the model said about this morning)
    pred = np.full(n, np.nan)
    for t in range(K - 1, n - 1):
        if not np.isnan(s[t]):
            pred[t + 1] = beta[0] + beta[1] * s[t] + sum(beta[2 + k] * u[t - k] for k in range(K))
    res = s - pred
    zres = res / sigma
    # How much of the residual does the same night's sleep score explain?
    sz = (sleep - np.nanmean(sleep)) / np.nanstd(sleep)
    ok = ~np.isnan(res) & ~np.isnan(sz)
    slope = float(np.polyfit(sz[ok], res[ok], 1)[0]) if ok.sum() > 30 else 0.0
    r2 = float(np.corrcoef(sz[ok], res[ok])[0, 1] ** 2) if ok.sum() > 30 else 0.0

    # Latest morning with a state
    last = max((i for i in range(n) if not np.isnan(s[i])), default=None)
    if last is None:
        return {"ok": False, "why": "no current state"}
    today_i = n - 1
    stale_days = today_i - last

    recent = []
    for i in range(max(0, n - RECENT_DAYS), n):
        recent.append({"d": days[i].isoformat(), "s": _nz(s[i], 2), "pred": _nz(pred[i], 2),
                       "z": _nz(zres[i], 2), "load": round(float(load[i])),
                       "sleep": _nz(sleep[i], 0)})

    # Regime: unwell if the detector says so, the plan is paused, or 3 nights in a row well under prediction
    reasons = []
    p_sick = getattr(illness, "today", None) if illness is not None else None
    if p_sick is not None and p_sick >= 0.4:
        reasons.append(f"illness detector P(sick) = {p_sick:.0%}")
    if illness_paused:
        reasons.append("illness pause active in the training plan")
    tail = [zres[i] for i in range(last - 2, last + 1) if not np.isnan(zres[i])]
    if len(tail) == 3 and all(z < -1.0 for z in tail):
        reasons.append("3 nights running well below prediction")

    uref = float(load[max(0, last - 27):last + 1].mean())     # complete days only
    # Default-slider ceiling (delta 0.25 SD, 80%) so the plain-words fallback needs no browser.
    c0 = draws[:, 2]
    ti = np.where(c0 < 0, uref + SCALE * 0.25 / np.maximum(-c0, 1e-9), np.inf)
    ceil0 = float(min(max(load.max(), 100), max(uref, np.quantile(ti, 0.2))))

    # Slow-drift brake: the trailing-42d baseline follows a slow decline down, so also compare the
    # last 7 days with the preceding 90 (in SDs of the daily value).  Negative = worse than usual.
    def _drift(x, sign):
        w7, w90 = x[max(0, n - 7):], x[max(0, n - 97):max(0, n - 7)]
        w7, w90 = w7[~np.isnan(w7)], w90[~np.isnan(w90)]
        if len(w7) < 4 or len(w90) < 30 or w90.std(ddof=1) == 0:
            return None
        return float(sign * (w7.mean() - w90.mean()) / w90.std(ddof=1))
    dh, dr = _drift(lnhrv, 1.0), _drift(rhr, -1.0)
    ds = [v for v in (dh, dr) if v is not None]
    drift = float(np.mean(ds)) if ds else None
    if drift is not None and drift < -0.5:
        reasons.append(f"7-day HRV/RHR sit {abs(drift):.1f} SD below their 90-day norm")
    # Tomorrow morning's expected state given what is already logged today (mean parameters).
    tmr = float(beta[0] + beta[1] * s[last] + sum(beta[2 + k] * u[last - k] for k in range(K)))
    mode0 = "ok"
    if reasons and (illness_paused or (p_sick is not None and p_sick >= 0.4)):
        mode0 = "unwell"
    elif drift is not None and drift < -1.0:
        mode0 = "unwell"
    elif reasons and any("below their 90-day" in r for r in reasons):
        mode0 = "hold"
    rng2 = np.random.default_rng(1)
    metrics = {}
    for key, (arr, sg, ln, label, unit) in {"hrv": (lnhrv, 1.0, True, "HRV", "ms"),
                                            "rhr": (rhr, -1.0, False, "Resting heart rate", "bpm")}.items():
        m = _metric_model(arr, sg, u, n, days, ln, label, unit, rng2)
        if m:
            metrics[key] = m
    out = {
        "ok": True,
        "metrics": metrics,
        "ceil": (_ceiling_payload(lnhrv, rhr, load, days, last, hours) if _ceiling_payload else None),
        "asof": days[last].isoformat(),
        "stale_days": stale_days,
        "s_now": round(float(s[last]), 3),
        "z_resid_now": _nz(zres[last], 2),
        "sleep_now": _nz(sleep[last], 0),
        "sleep_slope": round(slope, 3),
        "sleep_r2": round(r2, 3),
        # loads on the day of the last reading, the day before, ... (u_t in the model); then any days since
        "load_recent": [round(float(load[last - k])) for k in range(K)],
        "loads_after": [round(float(load[i])) for i in range(last + 1, n)],
        "u_ref": round(uref, 1),
        "max_load": int(max(load.max(), 100)),
        "scale": SCALE,
        "names": ["a", "b"] + [f"c{k}" for k in range(K)],
        "beta": [round(float(b), 4) for b in beta],
        "se": [round(float(e), 4) for e in se],
        "draws": [[round(float(v), 4) for v in d] for d in draws],
        "sigma": round(sigma, 3),
        "sd_s": round(float(np.nanstd(s)), 3),
        "n": int(len(y)),
        "half_life": HALF_LIFE,
        "recent": recent,
        "regime": {"unwell": bool(reasons), "reasons": reasons},
        "drift": _nz(drift, 2), "drift_hrv": _nz(dh, 2), "drift_rhr": _nz(dr, 2),
        "p_sick": _nz(p_sick, 3) if p_sick is not None else None,
        "ceil0": round(ceil0), "mode0": mode0,
        "today": date.today().isoformat(), "tomorrow_fc": round(tmr, 2),
    }
    out["explain_fallback"] = explain_fallback(out)
    return out


# ── Plain-words explainer ────────────────────────────────────────────────────────────────────
# Python states the facts; a small local model may only reword them.  Anything it says with a number
# that is not in the facts (or that contradicts the regime) is dropped, and if nothing survives the
# deterministic text below is shown instead.

def _sd(x, nd=2):
    return f"{x:+.{nd}f}"


def explain_facts(p, ctx=None):
    """List of fact sentences in plain language.  ctx = what the page is showing (slider scenario)."""
    ctx = ctx or {}
    f = []
    when = "This morning" if p["stale_days"] == 0 else f"On {p['asof']} (latest reading)"
    f.append(f"{when} your recovery score is {_sd(p['s_now'])}. It combines overnight HRV and resting heart rate, "
             "where 0 is your normal for the last six weeks and higher is fresher.")
    z = p.get("z_resid_now")
    if z is not None and abs(z) >= 0.5:
        side = "higher" if z >= 0 else "lower"
        f.append(f"That is {abs(z):.1f} {side} than the model expected from yesterday's recovery and recent load.")
    if p.get("sleep_now") is not None and z is not None and z < -1:
        f.append(f"Sleep score was {p['sleep_now']:.0f}, which usually explains part of a low reading.")
    if p.get("drift") is not None:
        side = "above" if p["drift"] >= 0 else "below"
        f.append(f"Over the last 7 days your HRV and resting heart rate sit {abs(p['drift']):.1f} {side} their three-month norm.")
    b = dict(zip(p["names"], p["beta"]))
    w0 = "lowers" if b["c0"] < 0 else "raises"
    w1 = "raises" if b["c1"] > 0 else "lowers"
    f.append(f"In your data an extra 40 load {w0} the next morning's recovery by {abs(b['c0']):.2f} "
             f"and {w1} the morning after by {abs(b['c1']):.2f}, so a hard day is mostly paid back within two days.")
    f.append(f"Your average daily load over the last 28 days is {p['u_ref']:.0f}.")
    tm = ctx.get("tomorrow", p.get("tomorrow_fc"))
    if tm is not None:
        f.append(f"With {p['load_recent'][0]:.0f} load logged on {p['asof']}, the next morning's recovery is forecast at {_sd(tm)}.")
    mode = ctx.get("mode") or p["mode0"]
    ceil = ctx.get("ceil") if ctx.get("ceil") is not None else p["ceil0"]
    if mode == "unwell":
        f.append(f"Easy only today. The usual limit would be {ceil:.0f} load, but the signals say the usual rules do not apply.")
    elif mode == "hold":
        f.append(f"Hold at about {p['u_ref']:.0f} load with no step up, because your 7-day HRV and resting heart rate have slipped.")
    else:
        f.append(f"The model's limit is about {ceil:.0f} load before the next morning's recovery drops "
                 f"by more than {ctx.get('delta', 0.25):.2f}.")
    for r in p["regime"]["reasons"]:
        f.append(f"Flag: {r}.")
    if ctx.get("plan") is not None and ctx.get("dip") is not None and ctx["plan"] > p["u_ref"] + 5:
        word = "lower" if ctx["dip"] < 0 else "higher"
        f.append(f"If you do {ctx['plan']:.0f} load, the next morning's recovery is expected to be {abs(ctx['dip']):.2f} {word} "
                 f"than otherwise, and the chance it is made up within 3 days is {ctx.get('repaid', 0):.0f} percent.")
    return f


def explain_fallback(p, ctx=None):
    return " ".join(explain_facts(p, ctx))


def explain_prompt(facts):
    return (
        "You are explaining a training-recovery model's current reading to the athlete who owns the data. "
        "Rewrite the FACTS below as 4 to 6 short, plain sentences, second person. "
        "Use ONLY the numbers in the FACTS, exactly as written. Do not add advice, numbers, medical claims or "
        "diagnoses that are not in the FACTS. If a Flag is listed, mention it. Start with what state he is in, "
        "then what that means for today's training.\n\nFACTS:\n- " + "\n- ".join(facts))


import re as _re
_NUMBER = _re.compile(r"\d+(?:\.\d+)?")
_MEDICAL = _re.compile(r"\b(diagnos|doctor|infection|virus|disease|medication|see a )", _re.I)
_PLAN_TOMORROW = _re.compile(r"tomorrow\b[^.]*\b(planned|you have \d+)", _re.I)   # the load is today's
_SICK = _re.compile(r"\b(ill|sick|unwell|illness)\b", _re.I)


def _allowed_numbers(facts):
    ok = set()
    for m in _NUMBER.findall(" ".join(facts)):
        v = float(m)
        ok.update({m, f"{v:.0f}", f"{v:.1f}", f"{v:.2f}"})
    return ok


def verify_explanation(text, facts, mode):
    """Keep only sentences whose numbers are in the facts and that do not invent illness or medical claims."""
    allowed = _allowed_numbers(facts)
    flagged = any(s.startswith("Flag:") for s in facts)
    kept = []
    for sent in _re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sent:
            continue
        bad = [n for n in _NUMBER.findall(sent) if n not in allowed and not (n.isdigit() and int(n) <= 12)]
        if bad or _MEDICAL.search(sent) or _PLAN_TOMORROW.search(sent) or (_SICK.search(sent) and not flagged and mode == "ok"):
            continue
        kept.append(sent)
    return " ".join(kept)
