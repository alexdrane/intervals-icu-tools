# intervals-icu-tools

Three tools for [intervals.icu](https://intervals.icu) — a terminal CLI, a GTK morning dashboard, and a food logger. Linux only.

---

## Tools

### `icu` — terminal CLI

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

### `training-brief.py` — GTK morning dashboard

A 1440×860 GTK/WebKit window showing:

- Form (TSB), HRV, resting HR, sleep score with hover tooltips
- 7 charts: CTL/ATL, TSB (zone-coloured), HRV+RHR, Z4+ high-intensity load, calorie intake vs target, body weight, sleep debt
- Projected CTL/ATL/TSB and Z4+ load based on your training plan
- AI coach brief (Gemini 2.5 Flash) — overview and training tips, cached until data changes
- Nutrition section with macro bars and AI nutrition coach
- Training plan upcoming sessions panel with intensity indicators
- Stretch streak tracker

```bash
python3 training-brief.py
```

### `food` — nutrition logger

Log meals by describing them in plain English. Uses Gemini (or Claude Haiku if you have an Anthropic key) to parse macros.

```
food add "scrambled eggs on toast with OJ"
food add "lunch was a chicken wrap and an apple"
food today          # show today's log and totals
food undo           # remove last entry
food undo 2         # remove last 2 entries
food clear          # clear today's log
```

---

## Setup

### 1. Install dependencies

```bash
pip install requests rich google-genai anthropic
# For training-brief.py (GTK window):
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

### 2. Configure

```bash
# Option A: interactive wizard
./icu --setup

# Option B: manually
mkdir -p ~/.config/intervals-icu
cp config.example.json ~/.config/intervals-icu/config.json
# edit ~/.config/intervals-icu/config.json with your keys
```

**Getting your credentials:**

| Key | Where to find it |
|---|---|
| `athlete_id` | URL when logged in: `intervals.icu/athletes/i123456` |
| `api_key` | intervals.icu → Settings → Developer Settings → API Key |
| `gemini_api_key` | [Google AI Studio](https://aistudio.google.com/) — free tier works |
| `anthropic_api_key` | Optional. `food add` uses this for Claude Haiku if present, otherwise falls back to Gemini |
| `calorie_baseline` | Your daily base calorie target (before training). Dashboard adds session burn on top. |

The config lives at `~/.config/intervals-icu/config.json` and is gitignored — it never gets committed.

### 3. Put tools on your PATH

```bash
# symlink from the repo so edits are reflected immediately
ln -s ~/intervals-icu-tools/icu ~/bin/icu
ln -s ~/intervals-icu-tools/food ~/bin/food
ln -s ~/intervals-icu-tools/training-brief.py ~/bin/training-brief.py
```

Or copy them to `/usr/local/bin/` if you prefer.

---

## Training plan

The training plan lives at `~/.config/intervals-icu/training-plan.json` (gitignored). Copy the example to get started:

```bash
cp training-plan.example.json ~/.config/intervals-icu/training-plan.json
```

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

## Keeping the repo up to date

If you edit the scripts directly in `~/intervals-icu-tools/` (which you should if you used symlinks above), push changes with:

```bash
cd ~/intervals-icu-tools
git add -p          # review changes interactively
git commit -m "describe what changed"
git push
```

If you edited scripts in `~/bin/` directly, copy them back first:

```bash
cp ~/bin/icu ~/intervals-icu-tools/icu
cp ~/bin/training-brief.py ~/intervals-icu-tools/training-brief.py
cp ~/bin/food ~/intervals-icu-tools/food
cd ~/intervals-icu-tools && git add . && git commit -m "..." && git push
```

The easiest long-term setup is to use the symlinks above — then editing the repo file IS editing the live script.
