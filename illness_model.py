"""
illness_model.py — GP + HMM illness detection for the training brief.

Entry point:
    from illness_model import get_illness_data
    result = get_illness_data(wellness_entries)

wellness_entries: raw list of dicts from the intervals.icu wellness API,
    each with keys "id" (YYYY-MM-DD), "restingHR", "atl".

Returns an IllnessResult namedtuple:
    .by_date        {date_str: p_sick}  — full history
    .today          float               — P(sick) for today
    .yesterday      float               — P(sick) for yesterday
    .bands          [{start, end}]      — episode list for chart shading
    .fresh          bool                — True if model was re-run (not pure cache hit)

Cache: ~/.cache/training-brief/illness-cache.json
    Warm-start (daily): L-BFGS-B from cached GP params + 2 outer EM iters → ~5s
    Full run (weekly):  DE + full outer EM → ~30s; triggered when last_full_run > 7d ago
"""

import json
import os
from datetime import datetime, timedelta

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import differential_evolution, minimize
from scipy.special import logsumexp
from collections import namedtuple

CACHE_FILE   = os.path.expanduser("~/.cache/training-brief/illness-cache.json")
TRAIN_DAYS   = 900   # ~Dec 2023; gives March 2024 clear of warmup
WARMUP_DAYS  = 60    # 3 correlation lengths at l≈18d; avoids GP boundary artefacts
OUTER_ITERS  = 6      # full run
WARM_ITERS   = 2      # warm-start outer EM iterations
BW_ITERS     = 100
SICK_THRESH  = 0.4
MIN_DELTA    = 1.5
MAX_P_HS     = 0.08
FULL_RERUN_DAYS = 7   # re-run DE if last full run was this many days ago

IllnessResult = namedtuple("IllnessResult", ["by_date", "today", "yesterday", "bands", "fresh"])


# ── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


# ── Data extraction ───────────────────────────────────────────────────────────

def _extract_arrays(wellness_entries):
    """Extract (dates, t, y, atl_norm, atl_stats) from raw wellness API list."""
    cutoff = datetime.now() - timedelta(days=TRAIN_DAYS)
    rows = []
    for e in sorted(wellness_entries, key=lambda x: x.get("id", "")):
        hr = e.get("restingHR")
        if not (hr and hr > 0):
            continue
        try:
            d = datetime.strptime(e["id"][:10], "%Y-%m-%d")
        except (ValueError, KeyError):
            continue
        if d < cutoff:
            continue
        rows.append((d, float(hr), e.get("atl")))

    if not rows:
        return None

    dates = [r[0] for r in rows]
    rhr   = np.array([r[1] for r in rows])
    atl   = np.array([r[2] if r[2] is not None else np.nan for r in rows])

    # Interpolate missing ATL
    nan_mask = np.isnan(atl)
    if nan_mask.any() and (~nan_mask).sum() >= 2:
        idx = np.arange(len(atl))
        atl[nan_mask] = np.interp(idx[nan_mask], idx[~nan_mask], atl[~nan_mask])
    elif nan_mask.all():
        atl[:] = 0.0

    t0       = dates[0]
    t        = np.array([(d - t0).days for d in dates], dtype=float)
    atl_mean = float(np.nanmean(atl))
    atl_std  = float(np.nanstd(atl)) or 1.0
    atl_norm = (atl - atl_mean) / atl_std

    return dates, t, rhr, atl_norm, (atl_mean, atl_std)


# ── SE kernel and GP ──────────────────────────────────────────────────────────

def _se_kernel(t1, t2, A, l):
    d2 = (t1[:, None] - t2[None, :]) ** 2
    return A**2 * np.exp(-0.5 * d2 / l**2)


def _neg_log_ml(p, t, y, atl_norm):
    log_A, log_l, log_sn, mu0, beta = p
    A, l, sn = np.exp(log_A), np.exp(log_l), np.exp(log_sn)
    N = len(y)
    mean_vec = mu0 + beta * atl_norm
    C = _se_kernel(t, t, A, l) + sn**2 * np.eye(N)
    try:
        L, lo = cho_factor(C, lower=True, check_finite=False)
    except np.linalg.LinAlgError:
        return 1e10
    r = y - mean_vec
    return 0.5 * (r @ cho_solve((L, lo), r) +
                  2*np.sum(np.log(np.diag(L))) + N*np.log(2*np.pi))


def _fit_gp_warm(t, y, atl_norm, x0):
    """L-BFGS-B only, starting from cached x0 = [log_A, log_l, log_sn, mu0, beta]."""
    t_range = max(t[-1] - t[0], 1.0)
    bounds = [
        (np.log(0.5), np.log(15)),
        (np.log(10),  np.log(t_range)),
        (np.log(0.3), np.log(8)),
        (35.0, 70.0),
        (-5.0, 5.0),
    ]
    res = minimize(_neg_log_ml, x0, args=(t, y, atl_norm),
                   method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 500, "ftol": 1e-11})
    x = res.x
    return np.exp(x[0]), np.exp(x[1]), np.exp(x[2]), x[3], x[4]


def _fit_gp_full(t, y, atl_norm):
    """DE global search + L-BFGS-B polish."""
    t_range = max(t[-1] - t[0], 1.0)
    bounds = [
        (np.log(0.5), np.log(15)),
        (np.log(10),  np.log(t_range)),
        (np.log(0.3), np.log(8)),
        (35.0, 70.0),
        (-5.0, 5.0),
    ]
    de = differential_evolution(_neg_log_ml, bounds, args=(t, y, atl_norm),
                                maxiter=300, popsize=10, seed=42,
                                polish=False, workers=1)
    res = minimize(_neg_log_ml, de.x, args=(t, y, atl_norm),
                   method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 1000, "ftol": 1e-12})
    x = res.x if res.fun < de.fun else de.x
    return np.exp(x[0]), np.exp(x[1]), np.exp(x[2]), x[3], x[4]


def _gp_posterior_mean(t_tr, y_tr, atl_tr, t_star, atl_star, A, l, sn, mu0, beta):
    N = len(t_tr)
    C  = _se_kernel(t_tr, t_tr, A, l) + sn**2 * np.eye(N)
    Ks = _se_kernel(t_star, t_tr, A, l)
    L, lo = cho_factor(C, lower=True, check_finite=False)
    alpha = cho_solve((L, lo), y_tr - (mu0 + beta * atl_tr))
    return (mu0 + beta * atl_star) + Ks @ alpha


# ── 2-state HMM (Baum-Welch) ─────────────────────────────────────────────────

def _log_gauss(x, mu, sigma):
    return -0.5*np.log(2*np.pi*sigma**2) - 0.5*(x-mu)**2/sigma**2


def _forward_backward(log_emit, log_A, log_pi0):
    N, K = log_emit.shape
    log_alpha = np.empty((N, K))
    log_alpha[0] = log_pi0 + log_emit[0]
    for i in range(1, N):
        log_alpha[i] = log_emit[i] + logsumexp(log_alpha[i-1, :, None] + log_A, axis=0)
    log_Z = logsumexp(log_alpha[-1])
    log_beta = np.zeros((N, K))
    for i in range(N-2, -1, -1):
        log_beta[i] = logsumexp(log_A + log_emit[i+1] + log_beta[i+1], axis=1)
    log_gamma = log_alpha + log_beta - log_Z
    log_xi = (log_alpha[:-1, :, None] + log_A[None, :, :] +
              log_emit[1:, None, :] + log_beta[1:, None, :] - log_Z)
    return log_gamma, log_xi, log_Z


def _baum_welch(r):
    N = len(r)
    pos = r[r > 0]
    thresh = np.percentile(pos, 75) if len(pos) > 5 else np.percentile(r, 80)
    sick_i = (r > thresh).astype(float)
    mu_H  = float(np.mean(r[sick_i < 0.5])) if (sick_i < 0.5).sum() > 1 else 0.0
    mu_S  = max(float(np.mean(r[sick_i > 0.5])) if (sick_i > 0.5).sum() > 1 else mu_H + 3.0,
                mu_H + MIN_DELTA)
    sig_H = max(float(np.std(r[sick_i < 0.5])) if (sick_i < 0.5).sum() > 1 else 1.5, 0.5)
    sig_S = max(float(np.std(r[sick_i > 0.5])) if (sick_i > 0.5).sum() > 1 else sig_H, 0.7)
    p_HS, p_SH = 0.025, 0.12

    log_A   = np.log([[1-p_HS, p_HS], [p_SH, 1-p_SH]])
    log_pi0 = np.log([0.95, 0.05])
    prev_ll = -np.inf
    A_new   = np.array([[1-p_HS, p_HS], [p_SH, 1-p_SH]])

    for _ in range(BW_ITERS):
        log_emit = np.column_stack([_log_gauss(r, mu_H, sig_H),
                                    _log_gauss(r, mu_S, sig_S)])
        log_gamma, log_xi, log_Z = _forward_backward(log_emit, log_A, log_pi0)
        gamma = np.exp(log_gamma)
        xi    = np.exp(log_xi)
        ll    = log_Z / N
        if abs(ll - prev_ll) < 1e-7:
            break
        prev_ll = ll

        xi_sum = xi.sum(axis=0)
        A_new  = xi_sum / xi_sum.sum(axis=1, keepdims=True)
        A_new  = np.clip(A_new, 1e-6, 1-1e-6)
        A_new /= A_new.sum(axis=1, keepdims=True)
        A_new[0, 1] = min(A_new[0, 1], MAX_P_HS)
        A_new[0, 0] = 1.0 - A_new[0, 1]

        w       = gamma.sum(axis=0)
        mu_new  = (gamma * r[:, None]).sum(axis=0) / w
        sig_new = np.sqrt((gamma * (r[:, None] - mu_new)**2).sum(axis=0) / w)
        sig_new = np.maximum(sig_new, 0.4)

        if mu_new[0] > mu_new[1]:
            mu_new  = mu_new[[1, 0]]
            sig_new = sig_new[[1, 0]]
            gamma   = gamma[:, [1, 0]]
            A_new   = A_new[[1, 0], :][:, [1, 0]]

        if mu_new[1] - mu_new[0] < MIN_DELTA:
            mu_new[1] = mu_new[0] + MIN_DELTA

        mu_H, mu_S   = mu_new
        sig_H, sig_S = sig_new
        log_A   = np.log(A_new)
        log_pi0 = log_gamma[0] - logsumexp(log_gamma[0])

    return gamma[:, 1]


# ── Outer EM ──────────────────────────────────────────────────────────────────

def _outer_em(t, y, atl_norm, n_iters, fit_fn):
    """
    fit_fn: callable(t, y, atl_norm) -> (A, l, sn, mu0, beta)
    Returns (gp_params, p_sick_full, f_hat)
    """
    N = len(t)
    warmup_idx    = int(np.searchsorted(t, WARMUP_DAYS))
    healthy_mask  = np.ones(N, dtype=bool)
    gp_params     = None
    p_sick        = np.zeros(N)
    f_hat         = np.full(N, float(np.nanmean(y)))   # fallback if loop exits early
    prev_sick_frac = 1.0

    for _ in range(n_iters):
        mask = healthy_mask
        t_h, y_h, a_h = t[mask], y[mask], atl_norm[mask]
        if len(t_h) < 20:
            break

        gp_params = fit_fn(t_h, y_h, a_h)
        A, l, sn, mu0, beta = gp_params

        f_hat     = _gp_posterior_mean(t_h, y_h, a_h, t, atl_norm, A, l, sn, mu0, beta)
        residuals = y - f_hat

        r_hmm   = residuals[warmup_idx:]
        p_hmm   = _baum_welch(r_hmm)
        p_sick  = np.concatenate([np.zeros(warmup_idx), p_hmm])

        new_mask   = p_sick < SICK_THRESH
        sick_frac  = (~new_mask).sum() / N
        converged  = (np.array_equal(new_mask, healthy_mask) or
                      abs(sick_frac - prev_sick_frac) < 0.02)
        healthy_mask   = new_mask
        prev_sick_frac = sick_frac
        if converged:
            break

    return gp_params, p_sick, f_hat


# ── Episode extraction ────────────────────────────────────────────────────────

def _episodes(dates, p_sick):
    bands, in_ep = [], False
    for i, (d, ps) in enumerate(zip(dates, p_sick)):
        d_str = d.strftime("%Y-%m-%d")
        if ps >= SICK_THRESH and not in_ep:
            ep_start = d_str; in_ep = True
        elif ps < SICK_THRESH and in_ep:
            bands.append({"start": ep_start, "end": dates[i-1].strftime("%Y-%m-%d")})
            in_ep = False
    if in_ep:
        bands.append({"start": ep_start, "end": dates[-1].strftime("%Y-%m-%d")})
    return bands


# ── Public entry point ────────────────────────────────────────────────────────

def get_illness_data(wellness_entries, force_full=False):
    """
    Main entry point. Returns IllnessResult.
    force_full=True forces a full DE re-run regardless of cache age.
    """
    today_str     = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    cache = _load_cache()
    last_full = cache.get("last_full_run", "")
    last_warm = cache.get("last_warm_run", "")

    # Pure cache hit: already ran today AND that run produced a value for today.
    # A run started just after midnight sees no restingHR for today yet — intervals.icu
    # has not synced the night — so _extract_arrays drops today's row and no posterior
    # is stored. Keying the guard on the run date alone would then lock the model out
    # for the rest of the day, leaving the stat stuck on "awaiting today's data".
    if (last_warm == today_str and not force_full
            and cache.get("illness", {}).get(today_str) is not None):
        by_date = cache["illness"]
        bands   = _bands_from_dict(by_date)
        return IllnessResult(
            by_date   = by_date,
            today     = by_date.get(today_str),
            yesterday = by_date.get(yesterday_str, 0.0),
            bands     = bands,
            fresh     = False,
        )

    extracted = _extract_arrays(wellness_entries)
    if extracted is None:
        # No usable data — return empty but don't crash
        return IllnessResult(by_date={}, today=0.0, yesterday=0.0, bands=[], fresh=False)

    dates, t, y, atl_norm, atl_stats = extracted

    # Decide whether to do a full run or warm-start
    days_since_full = (datetime.now() - datetime.strptime(last_full, "%Y-%m-%d")).days \
        if last_full else 999
    do_full = force_full or days_since_full >= FULL_RERUN_DAYS or not cache.get("gp_params")

    if do_full:
        gp_params, p_sick, _ = _outer_em(t, y, atl_norm, OUTER_ITERS, _fit_gp_full)
        cache["last_full_run"] = today_str
        # Store as log-space for warm-starting next time
        A, l, sn, mu0, beta = gp_params
        cache["gp_params"] = [float(np.log(A)), float(np.log(l)),
                               float(np.log(sn)), float(mu0), float(beta)]
    else:
        x0 = cache["gp_params"]
        def warm_fit(t_, y_, a_):
            return _fit_gp_warm(t_, y_, a_, x0)
        gp_params, p_sick, _ = _outer_em(t, y, atl_norm, WARM_ITERS, warm_fit)
        A, l, sn, mu0, beta = gp_params
        cache["gp_params"] = [float(np.log(A)), float(np.log(l)),
                               float(np.log(sn)), float(mu0), float(beta)]

    cache["last_warm_run"] = today_str
    cache["atl_stats"]     = list(atl_stats)

    # Merge updated p_sick into the stored dict (keeps historical values on warm-start)
    existing = cache.get("illness", {})
    for d, ps in zip(dates, p_sick):
        existing[d.strftime("%Y-%m-%d")] = round(float(ps), 4)
    cache["illness"] = existing
    _save_cache(cache)

    bands = _episodes(dates, p_sick)
    return IllnessResult(
        by_date   = existing,
        today     = existing.get(today_str),
        yesterday = existing.get(yesterday_str, 0.0),
        bands     = bands,
        fresh     = True,
    )


def _bands_from_dict(by_date):
    """Reconstruct episode bands from a {date_str: p_sick} dict."""
    sorted_dates = sorted(by_date.keys())
    bands, in_ep, ep_start = [], False, None
    for i, d in enumerate(sorted_dates):
        ps = by_date[d]
        if ps >= SICK_THRESH and not in_ep:
            ep_start = d; in_ep = True
        elif ps < SICK_THRESH and in_ep:
            bands.append({"start": ep_start, "end": sorted_dates[i-1]})
            in_ep = False
    if in_ep and ep_start:
        bands.append({"start": ep_start, "end": sorted_dates[-1]})
    return bands


def get_cached_result():
    """Return the last cached result without running the model. Safe to call at startup."""
    cache = _load_cache()
    if not cache.get("illness"):
        return None
    today_str     = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    by_date = cache["illness"]
    return IllnessResult(
        by_date   = by_date,
        today     = by_date.get(today_str),
        yesterday = by_date.get(yesterday_str, 0.0),
        bands     = _bands_from_dict(by_date),
        fresh     = False,
    )
