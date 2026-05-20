# intervals-icu-tools

Two tools for [intervals.icu](https://intervals.icu) that give you a terminal CLI and a GTK morning dashboard for your training data for linux


---

## Tools

### `icu` — terminal CLI

Fetches live data from the intervals.icu API and displays it in the terminal using Rich.

```
icu                     # list 15 most recent activities
icu 30                  # list 30 most recent activities
icu health              # today's readiness: sleep, HRV, resting HR, form, hi-intensity load
icu fitness             # CTL/ATL/TSB chart over last 90 days (braille rendering)
icu fitness 30          # same, last 30 days
icu week                # this week's rowing zone breakdown (fetches HR streams)
icu zones               # activity list with zone columns
icu --setup             # interactive setup wizard
```

### `training-brief.py` — GTK morning dashboard

A 1440×860 GTK/WebKit window showing:

- Form (TSB), HRV, resting HR, sleep score with hover tooltips
- 7 charts: CTL/ATL, TSB (zone-coloured), HRV+RHR, Z4+ high-intensity load, calorie intake vs target, body weight, sleep debt
- Projected CTL/ATL/TSB and Z4+ load based on your training plan
- AI coach brief (Gemini 2.5 Flash) — overview and training tips
- Nutrition section with macro bars and AI nutrition coach
- Training plan upcoming sessions panel
- Stretch streak tracker

```bash
python3 training-brief.py
```

---

## Setup

### 1. Install dependencies

```bash
pip install requests rich google-genai
# For training-brief.py (GTK window) you also need system packages:
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

### 2. Configure

```bash
# Option A: interactive wizard
./icu --setup

# Option B: copy example and fill in manually
mkdir -p ~/.config/intervals-icu
cp config.example.json ~/.config/intervals-icu/config.json
# then edit ~/.config/intervals-icu/config.json
```

**Getting your credentials:**
- `athlete_id`: visible in the URL when logged in — `intervals.icu/athletes/i123456`
- `api_key`: Settings → Developer Settings → API Key
- `gemini_api_key`: optional — only needed for AI coach briefs in `training-brief.py`. Get one at [Google AI Studio](https://aistudio.google.com/)

Config file lives at `~/.config/intervals-icu/config.json` — never commit it.

### 3. (Optional) Training plan

Copy `training-plan.example.json` to `~/.config/intervals-icu/training-plan.json` and fill in your sessions. Each session needs:

```json
{
  "date": "2026-05-22",
  "name": "hard erg",
  "tss": 90,
  "z4_mins": 15
}
```

`z4_mins` is the planned Z4+ minutes — used to project future CTL/ATL/TSB and Z4+ load curves.

### 4. (Optional) Put `icu` on your PATH

```bash
cp icu ~/bin/icu          # if ~/bin is on your PATH
# or
sudo cp icu /usr/local/bin/icu
```

---

## Notes

- The `week` subcommand fetches per-second HR and velocity streams to compute active zone time, filtering out slow/Z1 traffic (e.g. waiting at a lock). This takes a few seconds.
- `training-brief.py` uses a lock file to prevent multiple instances. If it crashes, delete `/tmp/training-brief.lock`.
- Gemini responses are cached in `~/.cache/training-brief/` and only re-fetched when training data changes.
- Food logging (shown in the nutrition section) is written by a separate `food` CLI not included here — the dashboard handles the case where no food data is present.
