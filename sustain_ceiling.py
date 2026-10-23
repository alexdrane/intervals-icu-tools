"""Rested ceiling (HRV) / floor (RHR) with one-sided deficits.

    y'_t = C_t - a F_t - D_t + eps_t
      y' = ln HRV   for HRV;   y' = -RHR   for RHR  (so C' = -floor)
      C_t   slowly drifting ceiling (random walk, sd q per day), inferred with an exact grid filter
      F_t   decayed load through yesterday: F_t = lam F_{t-1} + u_{t-1}   (u = load / 40)
      D_t   >= 0 deficit, Exp with mean exp(-lt) * exp(-k (h_t - 7.5)):  shorter nights give bigger deficits
            (h = hours slept, clipped to 4..9.5; hours rather than Garmin's score, which is partly built from HRV)
      eps   small symmetric measurement noise, N(0, s^2)

Because every unexplained deviation can only push a morning BELOW the ceiling, the ceiling is pulled
to the top edge of the readings instead of the average.  Parameters are fitted rarely (cached in
~/.cache/training-brief/sustain-ceiling.json, refit in a background thread when older than REFIT_DAYS);
the daily filter itself is a few milliseconds.
"""
import json
import math
import os
import threading
import time

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.optimize import minimize
from scipy.special import log_ndtr

CACHE = os.path.expanduser("~/.cache/training-brief/sustain-ceiling.json")
REFIT_DAYS = 14
NGRID = 500
N_MC = 300
RECENT = 60
SCALE = 40.0

# theta vector = [a, lam, log_theta, log_s, log_q]; replaced by the cache when present.
DEFAULTS = {"hrv": [0.0245, 0.05, 2.797, -2.292, -4.234, 0.44], "rhr": [0.588, 0.05, -0.317, 0.521, -1.02, 0.29]}
DEFAULTS["hrv_best"] = [0.0309, 0.05, 2.31, -3.22, -2.84, 0.29]
DEFAULTS["rhr_best"] = [0.55, 0.05, -0.1, -0.22, -1.2, 0.29]
S_CAP = {"hrv_best": 0.04, "rhr_best": 0.8}      # symmetric-noise caps for the "best nights count" ceiling
HREF = 7.5          # hours at which the deficit rate equals exp(lt)
NPAR = 6

_lock = threading.Lock()
_refitting = False


def _decayed(u, lam):
    f = np.zeros(len(u))
    for i in range(1, len(u)):
        f[i] = lam * f[i - 1] + u[i - 1]
    return f


def _grid(y):
    sd = np.nanstd(y)
    g = np.linspace(np.nanmin(y) - 0.5 * sd, np.nanmax(y) + 1.5 * sd, NGRID)
    return g, g[1] - g[0]


def _filter(th, y, u, hrs, g, dg, upto, ret=False):
    a, lam, lt, ls, lq, k = th
    s, q = math.exp(ls), math.exp(lq)
    hc = np.where(np.isnan(hrs), HREF, np.clip(hrs, 4.0, 9.5))
    F = _decayed(u, lam)
    post = np.ones_like(g) / len(g)
    ll = 0.0
    mean = np.full(len(y), np.nan)
    lo = np.full(len(y), np.nan)
    hi = np.full(len(y), np.nan)
    for t in range(upto):
        if t > 0:
            post = gaussian_filter1d(post, q / dg, mode="nearest")
            post /= post.sum()
        if not np.isnan(y[t]):
            theta = math.exp(lt + k * (hc[t] - HREF))      # more sleep -> smaller deficits
            x = g - a * F[t] - y[t]            # >= 0: reading sits below the fatigue-adjusted ceiling
            lf = math.log(theta) - theta * x + 0.5 * theta ** 2 * s ** 2 + log_ndtr((x - theta * s ** 2) / s)
            m = lf.max()
            if not np.isfinite(m):
                ll -= 1e6                       # impossible reading under these parameters
            else:
                w = post * np.exp(lf - m)
                z = w.sum()
                if not z > 0:
                    ll -= 1e6
                else:
                    ll += math.log(z) + m
                    post = w / z
        c = np.cumsum(post)
        mean[t] = float((post * g).sum())
        lo[t] = g[min(np.searchsorted(c, 0.1), len(g) - 1)]
        hi[t] = g[min(np.searchsorted(c, 0.9), len(g) - 1)]
    return (ll, mean, lo, hi, post, F) if ret else -ll


def fit_metric(y, u, hrs, upto=None, free_k=True, lam=None, s_cap=None):
    """ML fit of [a, lam, log_theta, log_s, log_q]."""
    upto = len(y) if upto is None else upto
    g, dg = _grid(y)
    sd = float(np.nanstd(y))
    best = None
    for a0, lam0, th0 in ((0.02, 0.5, 15), (0.05, 0.8, 25), (0.01, 0.3, 8)):
        x0 = [a0 * sd, lam0, math.log(th0 / sd), math.log(0.25 * sd), math.log(0.01 * sd), 0.3 if free_k else 0.0]
        bnds = [(0, 2 * sd), (0.05, 0.97) if lam is None else (lam, lam), (-1, 8),
                (-7, 3) if s_cap is None else (-7, math.log(s_cap * (1 if sd < 1 else 1))), (-9, -1), (0, 1.5) if free_k else (0, 0)]
        if lam is not None:
            x0[1] = lam
        if s_cap is not None:
            x0[3] = min(x0[3], math.log(s_cap))
        r = minimize(lambda th: _filter(th, y, u, hrs, g, dg, upto), x0, method="L-BFGS-B", bounds=bnds)
        if best is None or r.fun < best.fun:
            best = r
    return [float(v) for v in best.x], float(-best.fun)


def _load_cache():
    try:
        with open(CACHE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) and all(k in d for k in ("hrv", "rhr", "hrv_best", "rhr_best")) else None
    except (OSError, ValueError):
        return None


def _refit_worker(y_h, y_r, u, hrs):
    global _refitting
    try:
        try:                      # keep the CPU-heavy fit from starving the UI threads
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
        except (AttributeError, OSError):
            pass
        th = {"hrv": fit_metric(y_h, u, hrs)[0], "rhr": fit_metric(y_r, u, hrs)[0],
              "hrv_best": fit_metric(y_h, u, hrs, s_cap=S_CAP["hrv_best"])[0],
              "rhr_best": fit_metric(y_r, u, hrs, s_cap=S_CAP["rhr_best"])[0],
              "fitted": time.time(), "n": int(len(u))}
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(th, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass
    finally:
        _refitting = False


def _params(y_h, y_r, u, hrs):
    """Cached parameters if present; schedule a background refit when missing or stale."""
    global _refitting
    c = _load_cache()
    if c is not None and any(len(c[k]) != NPAR for k in ("hrv", "rhr", "hrv_best", "rhr_best")):
        c = None                    # cache from an older model version
    stale = c is None or (time.time() - c.get("fitted", 0)) > REFIT_DAYS * 86400
    if stale:
        with _lock:
            if not _refitting:
                _refitting = True
                threading.Thread(target=_refit_worker, args=(y_h.copy(), y_r.copy(), u.copy(), hrs.copy()), daemon=True).start()
    src = c if c else DEFAULTS
    return {k: src[k] for k in ("hrv", "rhr", "hrv_best", "rhr_best")}


def _block(th, y, u, hrs, last, days, ln, seed):
    """One ceiling model's full state: parameters, per-day ceiling posterior, and Monte Carlo draws."""
    g, dg = _grid(y)
    ll, mean, lo, hi, post, F = _filter(th, y, u, hrs, g, dg, upto=last + 1, ret=True)
    a, lam, lt, ls, lq, k = th
    theta, s, q = math.exp(lt), math.exp(ls), math.exp(lq)
    conv = (lambda v: math.exp(v)) if ln else (lambda v: -v)
    recent = []
    for i in range(max(0, last - RECENT + 1), last + 1):
        c1, c2 = sorted([conv(lo[i]), conv(hi[i])])
        recent.append({"d": days[i].isoformat(), "v": None if np.isnan(y[i]) else round(conv(y[i]), 1),
                       "c": round(conv(mean[i]), 2), "lo": round(c1, 2), "hi": round(c2, 2)})
    cdf = np.cumsum(post)
    pts = (np.arange(N_MC) + 0.5) / N_MC
    Cs = g[np.minimum(np.searchsorted(cdf, pts), len(g) - 1)]
    rng = np.random.default_rng(seed)
    return {"a": round(a, 5), "lam": round(lam, 4), "theta": round(theta, 4), "s": round(s, 5), "q": round(q, 5),
            "k": round(k, 4),
            "F_anchor": round(float(F[last]), 4),
            "c_now": recent[-1]["c"], "c_lo": recent[-1]["lo"], "c_hi": recent[-1]["hi"],
            "mean_deficit": round(1.0 / theta, 4),
            "recent": recent,
            "mc": {"C": [round(float(v), 4) for v in Cs],
                   "e": [round(float(v), 3) for v in rng.standard_normal(N_MC)],
                   "d": [round(float(v), 3) for v in rng.exponential(1.0, N_MC)],
                   "n": [round(float(v), 3) for v in rng.standard_normal(N_MC)]}}


def _one(th, th_best, y, u, hrs, last, days, ln, label, unit, sign_out):
    """Both ceiling models for one metric: 'data' (noise fitted freely) and 'best' (noise capped)."""
    return {"label": label, "unit": unit, "ln": ln, "sign": sign_out, "hrs_ref": HREF,
            "models": {"data": _block(th, y, u, hrs, last, days, ln, 7),
                       "best": _block(th_best, y, u, hrs, last, days, ln, 8)}}


def ceiling_payload(lnhrv, rhr, load, days, last, hours=None):
    """{'hrv': ..., 'rhr': ...} for the Load tab, or None."""
    try:
        u = np.asarray(load, float) / SCALE
        y_h = np.asarray(lnhrv, float)
        y_r = -np.asarray(rhr, float)
        hrs = np.full(len(u), np.nan) if hours is None else np.asarray(hours, float)
        P = _params(y_h, y_r, u, hrs)
        if np.isnan(y_h[: last + 1]).all():
            return None
        rec = hrs[max(0, last - 13):last + 1]
        rec = rec[~np.isnan(rec)]
        return {"sleep_default": round(float(rec.mean()), 1) if len(rec) else HREF,
                "hrv": _one(P["hrv"], P["hrv_best"], y_h, u, hrs, last, days, True, "HRV", "ms", 1),
                "rhr": _one(P["rhr"], P["rhr_best"], y_r, u, hrs, last, days, False, "Resting heart rate", "bpm", -1)}
    except Exception:
        return None
