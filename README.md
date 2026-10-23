# intervals-icu-tools

A GTK training dashboard on top of [intervals.icu](https://intervals.icu), plus a terminal CLI (`icu`) and an older food logger (`food`). Linux only.

## Quick start

```bash
git clone https://github.com/alexdrane/intervals-icu-tools.git
cd intervals-icu-tools
./install.sh --autostart     # apt + pip deps, config files, app-menu launcher, Qwen models
$EDITOR ~/.config/intervals-icu/config.json   # athlete_id + api_key from intervals.icu
```

Then open **Training Dashboard** from the app menu (or `python3 training-brief.py`). Needs GTK3 + WebKit2 4.1 (Ubuntu 22.04+).

`install.sh` copies the example config, training plan and benchmarks into `~/.config/intervals-icu/`, symlinks `icu`, `food` and `training-brief.py` into `~/bin`, installs the icon and `.desktop` launcher, and pulls the Qwen models if Ollama is present.

### Things to change for your own use
- **Coach prompts** are written for a solo rower called Alex (`COACH_ROLE` and the nutrition prompt in `training-brief.py`). Edit them for your sport.
- **`oya_anchor`** in `config.json` (Vs Last Year tab) defaults to 2025-10-13; set it to the Monday your own training restarted.
- **`calorie_baseline` / `calorie_surplus_target`** in `config.json` set the daily calorie target.
- The "Apps" dropdown in the header links to personal apps (`~/app-hub`) and does nothing unless you have them.

---

## The dashboard (`training-brief.py`)

A 1440x860 GTK/WebKit window. Tabs:

| Tab | What it shows |
|---|---|
| **Today** | Form (TSB), HRV, resting HR, sleep; coach overview and tips; next 7 days of planned sessions; "Plan the week" box; food box |
| **Trends** | CTL/ATL, TSB, HRV+RHR, Z4+ load, calories vs target, body weight, sleep debt, with projections from your plan |
| **Nutrition** | Macro bars, calorie gap, AI nutrition coach, editable food log |
| **Performance** | Radar profile and test-metric cards from `benchmarks.json` (see below) |
| **Vs Last Year** | This year's training against last year's, led by time in HR zone (`one-year-ago/`) |
| **Focus** | Daily focus log |
| **Load** | How much load today is sustainable given your HRV/RHR state (`sustain*.py`); Student-t lag model with a step-up ceiling |

There is also an illness-tracking view, and a stretching streak tracker. The Life tab (calendar/todos) is hidden.

**AI runs locally** through [Ollama](https://ollama.com) (`qwen2.5:7b` for tips and food parsing, `qwen2.5:3b` for the nutrition coach). No cloud key is needed. The coach *overview* is composed in plain Python; only the tips are model-written and are checked against your data. Without Ollama the dashboard still runs, but tips, food logging and the Load explainer are blank. Food logging maps what you say to a nutrition table (`food_table.json`, built by `build_food_table.py`); the model only picks foods and amounts.

**Voice input** (the mic buttons for food and weekly planning) uses `faster-whisper` (`small.en`, CPU) and `sounddevice`, so it needs a microphone.

Data lives in `~/.config/intervals-icu/` (config, plan, benchmarks), `~/.local/share/training-brief/` (food, focus, sleep, notes logs) and `~/.cache/training-brief/`. Restart note: to restart the app, run `pkill -f '[b]in/training-brief.py'` as its own command.

---

## `icu` terminal CLI

```
icu                     # list 15 most recent activities
icu 30                  # list 30 most recent activities
icu health              # today's readiness: sleep, HRV, resting HR, form, hi-intensity load
icu fitness             # CTL/ATL/TSB chart over last 90 days (braille rendering)
icu fitness 30          # same, last 30 days
icu week                # this week's rowing zone breakdown (fetches per-second HR streams)
icu zones               # activity list with zone columns
icu --setup             # interactive setup wizard
```

## `food` CLI (legacy)

The dashboard now logs food itself with the local model. This CLI is the older route and still needs a cloud key: `anthropic_api_key` (Claude Haiku) or `gemini_api_key` in `config.json`. You can ignore it.

```
food add "scrambled eggs on toast with OJ"
food today          # show today's log and totals
food undo           # remove last entry
```

---

## Setup details

`install.sh` does all of this. By hand:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 python3-pip libportaudio2
pip install --user -r requirements.txt
mkdir -p ~/.config/intervals-icu
cp config.example.json ~/.config/intervals-icu/config.json
cp training-plan.example.json ~/.config/intervals-icu/training-plan.json
ollama pull qwen2.5:7b && ollama pull qwen2.5:3b
```

`./icu --setup` is an interactive alternative for the config.

| Key | Where to find it |
|---|---|
| `athlete_id` | URL when logged in: `intervals.icu/athletes/i123456` |
| `api_key` | intervals.icu -> Settings -> Developer Settings -> API Key |
| `calorie_baseline` | Your daily base calorie target (before training). Dashboard adds session burn on top. |
| `calorie_surplus_target` | Extra kcal/day above that for a gain goal (0 to maintain) |
| `gemini_api_key`, `anthropic_api_key` | Only for the legacy `food` CLI |

The config lives at `~/.config/intervals-icu/config.json` and is gitignored.

---

## Training plan

The training plan lives at `~/.config/intervals-icu/training-plan.json` (gitignored). Copy the example to get started:

```bash
cp training-plan.example.json ~/.config/intervals-icu/training-plan.json
```

You can also type or dictate a week into the **Plan the week** box on the Today tab; it parses the text into an editable grid, and Save writes the sessions into this file (an optional `kind` field records the session type, and an `illness` block pauses hard work).

### Format

```json
{
  "projection_weeks": 1,
  "sessions": [
    {
      "date": "2026-05-22",
      "name": "hard erg",
      "tss": 90,
      "z4_mins": 15
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `projection_weeks` | How many weeks ahead to extend the CTL/ATL/TSB and Z4+ load projections in the dashboard. Set to 1 for the current week only, 2–3 for a longer forecast. |
| `date` | ISO date of the session (`YYYY-MM-DD`) |
| `name` | Session name shown in the upcoming panel |
| `tss` | Training Stress Score — used to project future CTL/ATL/TSB. Rough guide: easy 60min erg ~40, steady state outing ~60–75, hard erg ~80–100. |
| `z4_mins` | Planned Z4+ minutes — used to project the high-intensity load curve. 0 for UT2/steady work, 8–15 for rate work or threshold pieces, 15–20+ for hard ergs. |

The dot colour on the TSB chart indicates session intensity: red = Z4+ ≥10min, amber = Z4+ ≥3min, grey = low intensity.

### Updating the plan with Claude Code

You can ask Claude Code to update the plan directly. For example:

> "Add next week's sessions: Monday weights + 17km outing, Tuesday hard erg 4×2k, Wednesday paddle, Saturday race."

Claude will write the correct JSON into `~/.config/intervals-icu/training-plan.json` with sensible TSS and z4_mins estimates based on session type. You can then adjust individual values if needed.

---

## Benchmarks (Performance tab)

The "Performance" tab in `training-brief.py` — the radar profile + test-metric cards — is driven entirely by `~/.config/intervals-icu/benchmarks.json` (gitignored). The tracked lifts/tests are per-athlete, so nothing is hardcoded in the script; the file is seeded with a small generic placeholder on first run. Copy the example to customise it:

```bash
cp benchmarks.example.json ~/.config/intervals-icu/benchmarks.json
```

### Format

```json
{
  "metrics": {
    "back_squat": {"label": "Back squat (working set)", "target": 100, "unit": "kg", "color": "#a78bfa"},
    "row_2k":     {"label": "2k row", "target": 420, "unit": "s", "color": "#f59e0b", "lower_is_better": true}
  },
  "radar_axes": [
    {"label": "Strength",  "keys": ["back_squat"]},
    {"label": "Endurance", "keys": ["row_2k"]}
  ]
}
```

| Field | Meaning |
|---|---|
| `metrics` key | The value you log against from the "Performance" tab's form (any string) |
| `label` | Display name on the metric card and log-form dropdown |
| `target` | Goal value for the progress bar |
| `unit` | Appended to displayed values (`kg`, `W`, `s`, `/10`, ...) |
| `lower_is_better` | Set `true` for time-based tests (e.g. a 2k row) where a smaller number is progress |
| `baseline` | Optional. Overrides the auto-detected starting point (normally your first logged entry) — useful after resetting a target so the progress bar doesn't measure from years-old history |
| `color` | Hex colour for the card accent and sparkline |
| `radar_axes` | Groups one or more metric keys into a labelled spoke on the radar chart. The polygon always has as many sides as there are axes here. |

`bodyweight` is a special key: if present, its history is auto-populated from your logged weigh-ins (the same data behind the Body Weight chart) instead of requiring manual entries, so it stays current without any action from the Performance tab's form.

---

## Keeping your copy up to date

`install.sh` symlinks the scripts from the clone into `~/bin`, so editing the repo file edits the live app. Pull updates with `git pull` and restart the dashboard.
