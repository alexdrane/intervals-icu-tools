#!/usr/bin/env python3
"""Focus: three-a-day check-in popup + "stimulation balance" bar.

Data lives in ~/.local/share/training-brief/focus-log.json (list of entries) and
focus-config.json (tags, slot times).  The dashboard's Focus tab imports
`focus_payload()`; the popup is `python3 focus.py popup [--slot morning|midday|evening]`.

The bar is NOT a dopamine measurement. It is a decaying tally of the activity
tags Alex ticks himself: restorative/effortful tags push it up, cheap-stimulation
tags push it down.
"""
import json
import math
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta

DATA_DIR = os.path.expanduser("~/.local/share/training-brief")
LOG_FILE = os.path.join(DATA_DIR, "focus-log.json")
CONFIG_FILE = os.path.join(DATA_DIR, "focus-config.json")

DEFAULT_CONFIG = {
    "slots": {"morning": "09:30", "midday": "13:30", "evening": "21:00"},
    "bar_half_life_h": 6,
    "tags": {
        "restorative": ["exercise", "dog walk", "tidying", "cooking", "reading",
                        "deep work", "creative", "social", "outside"],
        "cheap": ["scrolling", "short video", "gaming", "news", "snacking"],
    },
}

SLOT_PROMPTS = {
    "morning": "What's the one small thing you'll do first?",
    "midday": "Any urge right now to reach for something (phone, snack, game)? What is it?",
    "evening": "One line: what worked today, and what would you change?",
}

STORER_STEPS = [
    "Stop digging (put the cheap thing down)",
    "Tune in to the body (movement, water, breath)",
    "One small win (make the bed, wash a plate)",
    "Regulate 10-15 min (walk, tea, stretch)",
    "Engage one meaningful target",
    "Reflect on the signal",
]


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        for k in ("slots", "tags"):
            cfg[k].update(user.get(k, {}))
        cfg["bar_half_life_h"] = user.get("bar_half_life_h", cfg["bar_half_life_h"])
    except (OSError, ValueError):
        pass
    return cfg


def load_log():
    try:
        with open(LOG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def add_entry(entry):
    os.makedirs(DATA_DIR, exist_ok=True)
    log = load_log()
    log.append(entry)
    tmp = LOG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=1)
    os.replace(tmp, LOG_FILE)


def slot_for_hour(hour):
    return "morning" if hour < 11 else "midday" if hour < 17 else "evening"


def slot_done_today(slot, now=None):
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    return any(e.get("date") == today and e.get("slot") == slot for e in load_log())


def bar_value(log, cfg, now=None):
    """0-100, 50 = neutral. Each tag counts +1 (restorative) / -1 (cheap),
    halving every `bar_half_life_h` hours."""
    now = now or datetime.now()
    rest = set(cfg["tags"]["restorative"])
    cheap = set(cfg["tags"]["cheap"])
    hl = cfg["bar_half_life_h"] * 3600
    total = 0.0
    for e in log:
        try:
            age = (now - datetime.fromisoformat(e["ts"])).total_seconds()
        except (KeyError, ValueError):
            continue
        if age < 0 or age > 6 * hl:
            continue
        w = 0.5 ** (age / hl)
        total += w * sum((t in rest) - (t in cheap) for t in e.get("tags", []))
    return max(0.0, min(100.0, 50 + 6 * total))


def focus_payload(now=None):
    """Everything the dashboard tab needs, JSON-serialisable."""
    now = now or datetime.now()
    cfg = load_config()
    log = load_log()
    cutoff = (now - timedelta(days=30)).isoformat()
    recent = [e for e in log if e.get("ts", "") >= cutoff]
    today = now.strftime("%Y-%m-%d")
    scored = [e for e in log if e.get("mood") or e.get("energy")]
    return {
        "latest": ({k: scored[-1].get(k) for k in ("mood", "energy", "slot", "date")}
                   if scored else None),
        "bar": round(bar_value(log, cfg, now), 1),
        "entries": recent,
        "today_slots": sorted({e["slot"] for e in recent if e.get("date") == today}),
        "tags": cfg["tags"],
        "slots": cfg["slots"],
        "storer": STORER_STEPS,
    }


# ── Popup ────────────────────────────────────────────────────────────────────
POPUP_HTML = r"""<!doctype html><meta charset="utf-8"><style>
body{margin:0;padding:18px 20px;background:#0f172a;color:#e2e8f0;font:14px system-ui,sans-serif}
h1{font-size:16px;margin:0 0 2px;color:#a78bfa}.sub{color:#64748b;margin-bottom:14px;font-size:12px}
.row{margin:12px 0 4px;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid #334155;background:#1e293b;color:#cbd5e1;border-radius:6px;padding:6px 11px;cursor:pointer;font-size:13px}
.chip:hover{border-color:#64748b}.chip.on{background:#1e1b4b;border-color:#a78bfa;color:#e9d5ff}
.chip.cheap.on{background:#431407;border-color:#fb923c;color:#fed7aa}
.chip.rest.on{background:#052e16;border-color:#4ade80;color:#bbf7d0}
input[type=text]{width:100%;box-sizing:border-box;background:#1e293b;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:8px;font-size:13px}
.btns{display:flex;gap:8px;margin-top:16px}button.act{border:0;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:13px}
.save{background:#7c3aed;color:#fff}.later{background:#1e293b;color:#94a3b8}.stuck{background:#1e293b;color:#fbbf24;margin-left:auto}
#wl{display:none}#storer{display:none;margin-top:10px;padding:10px;background:#1e293b;border-radius:6px}
#storer label{display:block;margin:4px 0;cursor:pointer}
</style>
<h1 id="title"></h1><div class="sub" id="sub"></div>
<div class="row">Mood</div><div class="chips" id="mood"></div>
<div class="row">Energy</div><div class="chips" id="energy"></div>
<div class="row">What have you been doing?</div><div class="chips" id="tags"></div>
<div id="wl"><div class="row">Cheap-stimulation block: wanted it?</div><div class="chips" id="want"></div>
<div class="row">...and enjoyed it?</div><div class="chips" id="like"></div></div>
<div class="row" id="prompt"></div><input type="text" id="note" autofocus>
<div id="storer"><b>STORER</b></div>
<div class="btns"><button class="act save" onclick="save()">Save</button>
<button class="act later" onclick="later()">Later (20 min)</button>
<button class="act stuck" onclick="toggleStorer()">I'm stuck &rarr; STORER</button></div>
<script>
var CFG = __CFG__, S = {mood:null, energy:null, tags:new Set(), want:null, like:null, storer:new Set()};
document.getElementById('title').textContent = CFG.slot[0].toUpperCase()+CFG.slot.slice(1)+' check-in';
document.getElementById('sub').textContent = 'A few taps. No right answer.';
document.getElementById('prompt').textContent = CFG.prompt;
function scale(id, key){var el=document.getElementById(id);for(var i=1;i<=5;i++){(function(v){
 var b=document.createElement('div');b.className='chip';b.textContent=v;b.onclick=function(){S[key]=v;
 Array.from(el.children).forEach(function(c){c.classList.toggle('on',c===b)})};el.appendChild(b)})(i)}}
scale('mood','mood');scale('energy','energy');scale('want','want');scale('like','like');
function tagChip(t,cls){var b=document.createElement('div');b.className='chip '+cls;b.textContent=t;
 b.onclick=function(){if(S.tags.has(t))S.tags.delete(t);else S.tags.add(t);b.classList.toggle('on');
 document.getElementById('wl').style.display=CFG.cheap.some(function(c){return S.tags.has(c)})?'block':'none'};
 document.getElementById('tags').appendChild(b)}
CFG.restorative.forEach(function(t){tagChip(t,'rest')});CFG.cheap.forEach(function(t){tagChip(t,'cheap')});
CFG.storer.forEach(function(s,i){var l=document.createElement('label');var c=document.createElement('input');
 c.type='checkbox';c.onchange=function(){if(c.checked)S.storer.add(i);else S.storer.delete(i)};
 l.appendChild(c);l.appendChild(document.createTextNode(' '+s));document.getElementById('storer').appendChild(l)});
function toggleStorer(){var s=document.getElementById('storer');s.style.display=s.style.display==='block'?'none':'block'}
function send(cmd,obj){document.title='__'+cmd+'__'+Date.now()+'|'+encodeURIComponent(JSON.stringify(obj))}
function save(){send('save',{mood:S.mood,energy:S.energy,tags:Array.from(S.tags),want:S.want,like:S.like,
 note:document.getElementById('note').value.trim(),stuck:S.storer.size>0,storer:Array.from(S.storer)})}
function later(){send('later',{})}
document.addEventListener('keydown',function(e){if(e.key==='Enter')save()});
</script>"""


def run_popup(slot=None, force=False):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2

    now = datetime.now()
    slot = slot or slot_for_hour(now.hour)
    if not force and slot_done_today(slot, now):
        return
    cfg = load_config()
    page = POPUP_HTML.replace("__CFG__", json.dumps({
        "slot": slot, "prompt": SLOT_PROMPTS[slot],
        "restorative": cfg["tags"]["restorative"], "cheap": cfg["tags"]["cheap"],
        "storer": STORER_STEPS}))

    win = Gtk.Window(title="Focus check-in")
    win.set_default_size(470, 640)
    win.set_position(Gtk.WindowPosition.CENTER)
    win.set_keep_above(True)
    win.connect("destroy", Gtk.main_quit)
    wv = WebKit2.WebView()
    win.add(wv)

    def on_title(w, _):
        t = w.get_title() or ""
        if t.startswith("__save__"):
            d = json.loads(urllib.parse.unquote(t.partition("|")[2]))
            d.update(ts=datetime.now().isoformat(timespec="seconds"),
                     date=datetime.now().strftime("%Y-%m-%d"), slot=slot)
            add_entry(d)
            Gtk.main_quit()
        elif t.startswith("__later__"):
            subprocess.Popen(
                ["systemd-run", "--user", "--on-active=20m", "--quiet",
                 sys.executable, os.path.abspath(__file__), "popup", "--slot", slot])
            Gtk.main_quit()

    wv.connect("notify::title", on_title)
    wv.load_html(page, "file:///")
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["popup"]:
        slot = args[args.index("--slot") + 1] if "--slot" in args else None
        if slot == "auto":
            slot = None
        run_popup(slot, force="--force" in args)
    elif args[:1] == ["bar"]:
        print(json.dumps(focus_payload()["bar"]))
    else:
        print(__doc__)
