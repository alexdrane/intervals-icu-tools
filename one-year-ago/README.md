# one-year-ago tracker

Compares recent training with last year's, led by time in HR zone.

- `one_year_ago.py --summary [--anchor YYYY-MM-DD]`: terminal view (this week-to-date + last 3 completed weeks vs last year).
- Dashboard tab "Vs Last Year" in `training-brief.py`: renders `one_year_ago.render_panel()` from the activity/wellness lists the dashboard already fetched (no extra API calls).
- Anchor = Monday of the week last year's training properly restarted (default 2025-10-13). Override with `"oya_anchor": "YYYY-MM-DD"` in `~/.config/intervals-icu/config.json`.
- Walks are excluded. HR zones are per sport and unchanged since last year, so zone time is like-for-like within sport; the sport mix differs between periods.
- Backup of the dashboard before the tab was added: `training-brief.py.bak-before-oya`.
- Not built yet: longest unbroken stretch in a zone (needs HR streams).

## Garmin 'Other' = rowing (confirmed by Alex, 2026-10-08)
Last year's Garmin 'Other' activities were rowing. `prepare()` retypes them as Rowing and re-scores zone time from
the HR stream against the rowing zones (intervals.icu had scored them with run zones, understating zone time).
Results are cached in `~/.cache/one-year-ago/`; run `python3 one_year_ago.py --warm` to refresh. The dashboard only
reads the cache. 'Other' only appears Jan-Nov 2025; none in 2026.

## Boat race
Each view in the tab opens with two little boat races on training load (`icu_training_load`, walks excluded):
this week so far vs the same days last year (finish line = last year's full Mon-Sun week), and the last 3 completed
weeks vs the matching 3 weeks then. Boats animate on load; the leader's lane is highlighted.
Primary view = closest same Monday-Sunday week last year (364 days back); the restart-aligned view sits below it.
