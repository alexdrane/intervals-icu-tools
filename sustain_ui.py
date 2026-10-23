"""Markup, CSS and JS for the Load tab (model in sustain.py / sustain_ceiling.py).

Inserted into training-brief.py's f-string as plain values, so the braces here are NOT doubled.
Keep backslash-quote sequences out of the JS (see the f-string gotcha in the project notes).

Layout (glance + drill-down): status strip, one plan row (day, load, sleep), two stacked HRV / RHR
charts with hover, then two fold-outs.  Everything technical lives in the second fold-out.
"""

CSS = """
#sus-root{padding:18px 26px 28px;max-width:940px;font-size:14px;color:#d4d6d8}
.sus-hero{background:#1f2123;border:1px solid #2a2c2f;border-radius:9px;padding:14px 20px}
.sus-hero-top{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.sus-chip{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.1em;padding:3px 10px;border-radius:20px}
.sus-chip.go{background:rgba(111,207,151,.14);color:#6fcf97}
.sus-chip.hold{background:rgba(224,177,90,.14);color:#e0b15a}
.sus-chip.easy{background:rgba(224,118,107,.14);color:#e0766b}
.sus-hero .big{font-size:26px;font-weight:600;color:#e8e9ea;font-variant-numeric:tabular-nums}
.sus-hero .sub{color:#a3a6a9;font-size:13px;line-height:1.5;margin-top:6px}
.sus-hero .next{color:#d4d6d8;font-size:13.5px;margin-top:6px}
.sus-hero .next b{color:#e8e9ea}
.sus-hero .why{margin-top:6px;font-size:12.5px;color:#e0b15a}
.sus-card{background:#1f2123;border:1px solid #2a2c2f;border-radius:9px;padding:14px 20px;margin-top:12px}
.sus-card h3{margin:0 0 4px;font-size:14px;font-weight:600;color:#e8e9ea}
.sus-card .note{color:#75777a;font-size:12.5px;margin-bottom:10px;line-height:1.5}
.sus-plan-row{display:grid;grid-template-columns:auto 1fr 1fr;gap:22px;align-items:end}
.sus-tog{display:inline-flex;border:1px solid #2a2c2f;border-radius:8px;overflow:hidden}
.sus-tog button{background:none;border:none;color:#a3a6a9;padding:6px 12px;font-size:12.5px;cursor:pointer;white-space:nowrap}
.sus-tog button.on{background:rgba(136,220,253,.16);color:#e8e9ea}
.sus-sl{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:#a3a6a9;min-width:0}
.sus-sl b{color:#e8e9ea;font-weight:600;font-variant-numeric:tabular-nums}
.sus-sl input[type=range]{width:100%;accent-color:#88dcfd}
.sus-verdict{margin-top:10px;padding:8px 12px;border-radius:8px;background:rgba(159,165,173,.07);font-size:13px;line-height:1.5}
.sus-verdict .t{font-weight:600;color:#e8e9ea}
.sus-verdict.over{background:rgba(224,177,90,.10)}
.sus-logged{font-size:12px;color:#75777a;margin-top:8px}
.sus-logged b{color:#a3a6a9}
.sus-chartbox{position:relative}
.sus-mhead{display:flex;align-items:center;gap:12px;font-size:12.5px;color:#a3a6a9;margin-bottom:6px}
.sus-svg{width:100%;height:auto;display:block}
.sus-svg text{fill:#75777a;font-size:10px;font-family:inherit}
.sus-tip{display:none;position:absolute;top:8px;left:0;z-index:5;background:#15171a;border:1px solid #3a3d40;border-radius:8px;padding:7px 10px;font-size:11.5px;line-height:1.5;color:#d4d6d8;pointer-events:none;white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.4)}
.sus-tip .d{color:#e8e9ea;font-weight:600;margin-bottom:2px}
.sus-tip i{display:inline-block;width:9px;height:3px;margin-right:6px;vertical-align:middle;border-radius:2px}
.sus-ctitle{display:flex;align-items:baseline;gap:10px;margin:2px 0 2px;font-size:13px;color:#e8e9ea}
.sus-ctitle span{color:#75777a;font-size:12px}
.sus-ctitle .now{margin-left:auto;color:#a3a6a9;font-size:12.5px}
.sus-ctitle .now b{color:#e8e9ea}
.sus-leg{display:flex;gap:6px 16px;flex-wrap:wrap;font-size:11.5px;color:#a3a6a9;margin-top:6px}
.sus-leg i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:5px;border-radius:2px}
.sus-exp .txt{color:#d4d6d8;font-size:14px;line-height:1.65;white-space:pre-wrap}
.sus-exp .row{display:flex;align-items:center;gap:12px;margin-top:10px}
.sus-exp button{background:rgba(136,220,253,.14);color:#88dcfd;border:1px solid rgba(136,220,253,.35);border-radius:8px;padding:6px 14px;font-size:12.5px;cursor:pointer}
.sus-exp button:hover{background:rgba(136,220,253,.22)}
.sus-exp button[disabled]{opacity:.5;cursor:default}
.sus-exp .st{font-size:11.5px;color:#75777a}
.sus-adv{margin-top:10px}
.sus-adv summary{cursor:pointer;color:#a3a6a9;font-size:13px;padding:8px 0}
.sus-adv summary:hover{color:#e8e9ea}
.sus-ctl label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:#a3a6a9;margin:8px 0}
.sus-ctl b{color:#e8e9ea;font-weight:600}
.sus-ctl input[type=range]{width:100%;accent-color:#88dcfd}
.sus-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:12px}
.sus-tile{background:#1f2123;border:1px solid #2a2c2f;border-radius:9px;padding:10px 14px}
.sus-tile .v{font-size:18px;font-weight:600;color:#e8e9ea;font-variant-numeric:tabular-nums}
.sus-tile .l{font-size:10px;letter-spacing:.08em;color:#75777a;text-transform:uppercase;margin-bottom:2px}
.sus-tile .d{font-size:11.5px;color:#75777a;margin-top:2px}
.sus-limits{color:#a3a6a9;font-size:12.5px;line-height:1.6;margin:0;padding-left:18px}
.sus-limits li{margin-bottom:3px}
.sus-banner{margin-top:10px;padding:8px 14px;border-radius:8px;background:rgba(224,177,90,.10);border:1px solid rgba(224,177,90,.35);color:#e0b15a;font-size:12.5px}
@media (max-width:760px){.sus-plan-row{grid-template-columns:1fr}}
"""

PANEL = '<section class="panel" data-panel="sustain"><div id="sus-root"></div></section>'

JS = r"""
var SUSB = false, SUS = {delta: 0.25, cred: 0.8, plan: null, day: undefined, sleep: null, model: 'best', nxt: {}}, SUSD = null;
function susQ(arr, q) {
  var a = arr.slice().sort(function(x, y) { return x - y; });
  var i = Math.min(a.length - 1, Math.max(0, Math.round(q * (a.length - 1))));
  return a[i];
}
function susIrf(p, H) {
  var r = [0];
  for (var h = 1; h <= H; h++) r.push((h - 1 < 4 ? p[2 + h - 1] : 0) + p[1] * r[h - 1]);
  return r;
}
function susPath(p, s0, lr, loads, H) {
  // loads[j] = load on day j (0 = day of the last reading), history lr = [that day, day before, ...]
  var seq = [lr[3], lr[2], lr[1]].concat(loads), out = [s0], s = s0;
  for (var j = 0; j < H; j++) {
    var i = 3 + j, v = p[0] + p[1] * s;
    for (var k = 0; k < 4; k++) v += p[2 + k] * seq[i - k] / SUSD.scale;
    out.push(v); s = v;
  }
  return out;
}
function susPlanLoads(H) {
  var by = {}, out = [];
  (DATA.planSessions || []).forEach(function(s) { by[s.date] = (by[s.date] || 0) + (s.tss || 0); });
  var t0 = new Date(SUSD.asof + 'T12:00:00');
  for (var j = 0; j < H; j++) {
    var key = new Date(t0.getTime() + j * 86400000).toISOString().slice(0, 10);
    out.push(by[key] !== undefined ? by[key] : null);
  }
  return out;
}
function susF(x, d) { return (x >= 0 ? '+' : '-') + Math.abs(x).toFixed(d === undefined ? 2 : d); }
function susRecWord(s) {
  return s > 0.75 ? 'well above your normal' : s > 0.25 ? 'a bit above your normal' : s > -0.25 ? 'about your normal' :
    s > -0.75 ? 'a bit below your normal' : 'well below your normal';
}
function susDipWord(d) {   // d = size of the dip (positive number)
  return d < 0.08 ? 'barely noticeable' : d < 0.2 ? 'slightly lower' : d < 0.4 ? 'noticeably lower' : 'much lower';
}
function susDateLabel(offset) {
  var d = new Date(new Date(SUSD.asof + 'T12:00:00').getTime() + offset * 86400000);
  return d.toLocaleDateString('en-GB', {weekday: 'short', day: 'numeric'});
}
function susDateOf(iso) {
  return new Date(iso + 'T12:00:00').toLocaleDateString('en-GB', {weekday: 'short', day: 'numeric', month: 'short'});
}
function susKnownLoad(j) {   // load already logged on day j after the last reading (j=0 is that day)
  return j === 0 ? SUSD.load_recent[0] : (SUSD.loads_after[j - 1] !== undefined ? SUSD.loads_after[j - 1] : 0);
}
function susDefaultPlan(day) {
  var pl = susPlanLoads(SUSD.stale_days + 3)[SUSD.stale_days + day];
  if (pl !== null && pl !== undefined) return Math.round(pl / 5) * 5;
  return Math.round(SUSD.u_ref / 5) * 5;
}
function susModel(m) { SUS.model = m; susUpdate(); }
function susDay(d) {
  SUS.day = d; SUS.plan = susDefaultPlan(d);
  document.getElementById('sus-plan').value = SUS.plan;
  susUpdate();
}
function susHover(svg, W, L, R, nx, info) {
  var tip = svg.parentNode.querySelector('.sus-tip'), hl = svg.querySelector('.sus-hl');
  svg.onmousemove = function(ev) {
    var r = svg.getBoundingClientRect(), xv = (ev.clientX - r.left) / r.width * W;
    var i = Math.round((xv - L) / (W - L - R) * (nx - 1));
    i = Math.max(0, Math.min(nx - 1, i));
    var x = L + (W - L - R) * i / (nx - 1);
    hl.setAttribute('x1', x); hl.setAttribute('x2', x); hl.style.display = '';
    tip.innerHTML = info(i); tip.style.display = 'block';
    var px = x / W * r.width, tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(px + 14, r.width - tw)) + 'px';
  };
  svg.onmouseleave = function() { hl.style.display = 'none'; tip.style.display = 'none'; };
}
function susBand(X, idx, lo, hi, fill) {
  // closed polygon: along hi from left to right, back along lo from right to left
  var d = '', k;
  for (k = 0; k < idx.length; k++) d += (k ? 'L' : 'M') + X(idx[k]) + ',' + hi[k];
  for (k = idx.length - 1; k >= 0; k--) d += 'L' + X(idx[k]) + ',' + lo[k];
  return '<path d="' + d + 'Z" fill="' + fill + '"/>';
}
function susPoly(X, idx, vals, col, w, dash) {
  var d = '';
  for (var k = 0; k < idx.length; k++) if (vals[k] !== null) d += (d ? 'L' : 'M') + X(idx[k]) + ',' + vals[k];
  return d ? '<path d="' + d + '" fill="none" stroke="' + col + '" stroke-width="' + w + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : '') + ' stroke-linejoin="round"/>' : '';
}
function renderSustain() {
  var root = document.getElementById('sus-root');
  if (!root) return;
  SUSD = DATA.sustain;
  if (!SUSD || !SUSD.ok) {
    root.innerHTML = '<div class="sus-card"><h3>Sustainable load</h3><div class="note">Model unavailable: ' +
      ((SUSD && SUSD.why) || 'no data') + '</div></div>';
    SUSB = false; return;
  }
  if (!SUSB) {
    if (SUS.day === undefined) SUS.day = susKnownLoad(SUSD.stale_days) > 0 ? 1 : 0;
    SUS.plan = susDefaultPlan(SUS.day);
    var sd = SUSD.ceil && SUSD.ceil.sleep_default ? SUSD.ceil.sleep_default : 7.5;
    SUS.sleep = Math.round(sd * 2) / 2;
    root.innerHTML =
      '<div class="sus-hero" id="sus-hero"></div><div id="sus-banner"></div>' +
      '<div class="sus-card"><div class="sus-plan-row">' +
      '<div class="sus-tog"><button id="sus-d0" onclick="susDay(0)"></button><button id="sus-d1" onclick="susDay(1)"></button></div>' +
      '<label class="sus-sl"><span>Training load <b id="sus-plan-v"></b></span><input type="range" id="sus-plan" min="0" max="' + SUSD.max_load + '" step="5"></label>' +
      '<label class="sus-sl"><span>Sleep each night <b id="sus-sleep-v"></b></span><input type="range" id="sus-sleep" min="5" max="10" step="0.5"></label>' +
      '</div><div class="sus-verdict" id="sus-verdict"></div><div class="sus-logged" id="sus-logged"></div></div>' +
      '<div class="sus-card"><div class="sus-mhead"><span>Forecast based on</span><div class="sus-tog"><button id="sus-mb" onclick="susModel(\'best\')">Best nights count</button><button id="sus-md" onclick="susModel(\'data\')">Data-driven</button></div></div><div id="sus-m-hrv"></div><div id="sus-m-rhr" style="margin-top:6px"></div><div class="sus-leg" id="sus-m-leg"></div></div>' +
      '<details class="sus-adv"><summary>In plain words</summary><div class="sus-card sus-exp" style="margin-top:4px"><div class="txt" id="sus-exp-txt"></div>' +
      '<div class="row"><button id="sus-exp-btn" onclick="susExplain()">Reword with Qwen</button><span class="st" id="sus-exp-st"></span></div></div></details>' +
      '<details class="sus-adv"><summary>How it works and how far to trust it</summary>' +
      '<div class="sus-card sus-ctl"><h3>Tuning</h3>' +
      '<label>Biggest next-day dip I will accept: <b id="sus-delta-v"></b><input type="range" id="sus-delta" min="0.1" max="0.6" step="0.05"></label>' +
      '<label>How sure the model must be: <b id="sus-cred-v"></b><input type="range" id="sus-cred" min="60" max="95" step="5"></label></div>' +
      '<div class="sus-tiles" id="sus-tiles"></div>' +
      '<div class="sus-card"><h3>Recovery score, past and forecast</h3>' +
      '<div class="note">One combined number from HRV and resting heart rate. 0 is your average for the last six weeks; higher is fresher.</div>' +
      '<div class="sus-chartbox" id="sus-chart"></div><div class="sus-leg" id="sus-leg"></div></div>' +
      '<div class="sus-card"><h3>What one training day does</h3>' +
      '<div class="note">Effect of an extra 40 load on your recovery score on each later morning (bar = best estimate, line = 90% range). A hard day is a loan: lower tomorrow, paid back soon after.</div>' +
      '<div id="sus-irf"></div></div>' +
      '<div class="sus-card"><h3>How far to trust it</h3><ul class="sus-limits" id="sus-limits"></ul></div></details>';
    var pl = document.getElementById('sus-plan'), sl = document.getElementById('sus-sleep');
    var de = document.getElementById('sus-delta'), cr = document.getElementById('sus-cred');
    pl.value = SUS.plan; sl.value = SUS.sleep; de.value = SUS.delta; cr.value = Math.round(SUS.cred * 100);
    pl.oninput = function() { SUS.plan = +pl.value; susUpdate(); };
    sl.oninput = function() { SUS.sleep = +sl.value; susUpdate(); };
    de.oninput = function() { SUS.delta = +de.value; susUpdate(); };
    cr.oninput = function() { SUS.cred = +cr.value / 100; susUpdate(); };
    document.getElementById('sus-exp-txt').textContent = SUSD.explain_fallback || '';
    document.getElementById('sus-exp-st').textContent = 'Written by the model code, not Qwen';
    SUSB = true;
  }
  susUpdate();
}
function susUpdate() {
  var D = SUSD, P = D.draws, H = 7, uref = D.u_ref, lr = D.load_recent, sc = D.scale, off = D.stale_days, dayIdx = off + SUS.day;
  var dayLbl = susDateLabel(dayIdx);
  var b0 = document.getElementById('sus-d0'), b1 = document.getElementById('sus-d1');
  b0.textContent = susDateLabel(off) + ' (today)'; b1.textContent = susDateLabel(off + 1) + ' (tomorrow)';
  b0.className = SUS.day === 0 ? 'on' : ''; b1.className = SUS.day === 1 ? 'on' : '';
  document.getElementById('sus-mb').className = SUS.model === 'best' ? 'on' : '';
  document.getElementById('sus-md').className = SUS.model === 'data' ? 'on' : '';
  document.getElementById('sus-plan-v').textContent = SUS.plan + ' on ' + dayLbl;
  document.getElementById('sus-sleep-v').textContent = SUS.sleep.toFixed(1) + ' h';
  document.getElementById('sus-delta-v').textContent = SUS.delta.toFixed(2) + ' (' + susDipWord(SUS.delta) + ')';
  document.getElementById('sus-cred-v').textContent = Math.round(SUS.cred * 100) + '%';

  // Ceiling on load: largest load whose next-day dip stays within the accepted size in `cred` of draws.
  var Ti = P.map(function(p) { return p[2] < 0 ? uref + sc * SUS.delta / (-p[2]) : Infinity; });
  var ceil = Math.min(D.max_load, Math.max(uref, susQ(Ti, 1 - SUS.cred)));
  var mode = 'ok';
  if (D.regime.unwell) mode = 'unwell';
  else if (D.drift !== null && D.drift < -1.0) mode = 'unwell';
  else if (D.drift !== null && D.drift < -0.5) mode = 'hold';

  // Loads for days 0..H-1: logged so far, then your choice, then the training plan, else a usual day.
  var plan = susPlanLoads(H), T = SUS.plan;
  var loadsFor = function(target) {
    var a = [];
    for (var j = 0; j < H; j++) {
      if (j === dayIdx) a.push(target);
      else if (j <= off) a.push(susKnownLoad(j));
      else a.push(plan[j] !== null ? plan[j] : uref);
    }
    return a;
  };
  var LT = loadsFor(T), LR = loadsFor(uref);

  // Charts first (they record tomorrow's typical numbers for the status strip)
  susMetricChart(D, 'hrv', LT, LR, H, 'sus-m-hrv', dayLbl);
  susMetricChart(D, 'rhr', LT, LR, H, 'sus-m-rhr', dayLbl);
  document.getElementById('sus-m-leg').innerHTML =
    '<span><i style="background:#e8e9ea"></i>your mornings</span>' +
    '<span><i style="background:#a3a6a9"></i>ceiling / floor (level your best mornings reach), data-driven</span>' +
    '<span><i style="background:#d9c27a"></i>same, best-nights model</span>' +
    '<span><i style="background:#88dcfd"></i>typical morning with this plan</span>' +
    '<span><i style="background:#8b8e91"></i>with usual days</span>' +
    '<span><i style="background:#6fcf97"></i>great morning (best 1 in 10): no training, 9.5 h sleep</span>';

  // Status strip
  var chip, big, sub, why = D.regime.reasons.length ? '<div class="why">Why: ' + D.regime.reasons.join('; ') + '</div>' : '';
  if (mode === 'unwell') {
    chip = '<span class="sus-chip easy">EASY</span>'; big = 'Easy day only';
    sub = 'Keep it light: easy aerobic work or rest. The usual limit would be about ' + Math.round(ceil) + ' load, but the signals say the usual rules do not apply right now.';
  } else if (mode === 'hold') {
    chip = '<span class="sus-chip hold">HOLD</span>'; big = 'Stay around ' + Math.round(uref) + ' load';
    sub = 'No step up. Your last week of HRV and resting heart rate has drifted below your three-month norm.';
  } else {
    chip = '<span class="sus-chip go">GO</span>'; big = 'Up to about ' + Math.round(ceil) + ' load on ' + dayLbl;
    sub = 'A normal day for you is about ' + Math.round(uref) + '. Stay under ' + Math.round(ceil) + ' and the next morning should be at most ' + susDipWord(SUS.delta) + '.';
  }
  var nx = SUS.nxt, nextTxt = (nx.hrv !== undefined && nx.rhr !== undefined)
    ? '<div class="next">' + susDateLabel(1) + ' morning' + (off > 0 ? ' (not in yet)' : '') + ': typically <b>' + Math.round(nx.hrv) + ' ms</b> HRV and <b>' + Math.round(nx.rhr) + ' bpm</b> resting HR.</div>' : '';
  document.getElementById('sus-hero').innerHTML = '<div class="sus-hero-top">' + chip + '<span class="big">' + big + '</span></div><div class="sub">' + sub + '</div>' + nextTxt + why;
  document.getElementById('sus-banner').innerHTML = off > 0
    ? '<div class="sus-banner">No HRV reading for ' + susDateLabel(off) + ' yet (it arrives after you sleep). The mornings below are forecasts from ' + susDateLabel(0) + '\'s reading.</div>' : '';

  // What-if verdict and logged loads
  var extra = (T - uref) / sc;
  var eff1 = P.map(function(p) { return p[2] * extra; });
  var cum3 = P.map(function(p) { var r = susIrf(p, 3); return (r[1] + r[2] + r[3]) * extra; });
  var pay = cum3.filter(function(v) { return v >= 0; }).length / cum3.length;
  var e1m = eff1.reduce(function(a, b) { return a + b; }, 0) / eff1.length;
  var v = document.getElementById('sus-verdict'), within = T <= ceil + 0.5;
  if (T <= uref + 5) {
    v.className = 'sus-verdict';
    v.innerHTML = '<span class="t">No extra cost expected.</span> At or below your usual day, so the next morning should look the same as normal.';
  } else {
    var dip = Math.max(0, -e1m);
    v.className = 'sus-verdict' + (within ? '' : ' over');
    v.innerHTML = '<span class="t">' + (within ? 'Within the limit.' : 'Above the limit.') + '</span> The morning after ' + dayLbl + ' is likely <b>' + susDipWord(dip) +
      '</b> than otherwise (' + susF(e1m) + ' on the recovery scale). ' +
      (dip < 0.08 ? '' : pay >= 0.6 ? 'Usually made up within about three days.' : pay >= 0.3 ? 'May take longer than three days to come back.' : 'Will probably take more than three days to come back.');
  }
  var lg = [];
  for (var q0 = Math.max(0, off - 1); q0 <= off; q0++) lg.push(susDateLabel(q0) + ' <b>' + susKnownLoad(q0) + '</b>');
  document.getElementById('sus-logged').innerHTML = 'Logged load: ' + lg.join(' &middot; ');
  SUS.last = {plan: T, delta: SUS.delta, mode: mode, ceil: Math.round(ceil), dip: e1m, repaid: Math.round(pay * 100), tomorrow: undefined};

  // Advanced: recovery chart, tiles, impulse response, limits
  var paths = P.map(function(p) { return susPath(p, D.s_now, lr, LT, H); });
  var refs = P.map(function(p) { return susPath(p, D.s_now, lr, LR, H); });
  var mean = function(arr, j) { return arr.reduce(function(a, p) { return a + p[j]; }, 0) / arr.length; };
  var pm = [], rm = [], lo = [], hi = [];
  for (var j = 0; j <= H; j++) {
    var col = paths.map(function(p) { return p[j]; });
    pm.push(mean(paths, j)); rm.push(mean(refs, j)); lo.push(susQ(col, 0.1)); hi.push(susQ(col, 0.9));
  }
  SUS.last.tomorrow = pm[1];
  susChart(D, {pm: pm, rm: rm, lo: lo, hi: hi}, LT, T, uref, H, dayIdx, plan, dayLbl);
  var tiles = '';
  tiles += '<div class="sus-tile"><div class="l">Recovery ' + (off > 0 ? 'on ' + D.asof : 'this morning') + '</div><div class="v">' + susF(D.s_now) + '</div><div class="d">' + susRecWord(D.s_now) + '</div></div>';
  tiles += '<div class="sus-tile"><div class="l">Vs what was expected</div><div class="v">' + (D.z_resid_now === null ? 'n/a' : susF(D.z_resid_now, 1)) + '</div><div class="d">' +
    (D.sleep_now !== null ? 'sleep score ' + D.sleep_now : 'no sleep score') + ' (0 = as predicted)</div></div>';
  tiles += '<div class="sus-tile"><div class="l">7-day vs 3-month norm</div><div class="v">' + (D.drift === null ? 'n/a' : susF(D.drift, 1)) + '</div><div class="d">below -0.5 triggers HOLD</div></div>';
  tiles += '<div class="sus-tile"><div class="l">Chance repaid by day 3</div><div class="v">' + (T <= uref ? 'n/a' : Math.round(pay * 100) + '%') + '</div><div class="d">at ' + T + ' load on ' + dayLbl + '</div></div>';
  document.getElementById('sus-tiles').innerHTML = tiles;
  susIrfChart(D, P);
  var lim = [];
  lim.push('Fit on <b>' + D.n + '</b> days, recent ones weighted more (half-life ' + D.half_life + ' days), so single bad nights do not drive it.');
  lim.push('It explains only part of the night-to-night variation (typical miss ' + D.sigma.toFixed(2) + ' against a normal spread of ' + D.sd_s.toFixed(2) + '). Trust the shape, not any one day.');
  lim.push('The ceiling / floor is the level your best mornings reach (the top 1 in 10) when nothing drags you down; unexplained bad nights can only push a morning below it. Two versions are drawn (the toggle picks which one drives the forecast): the data-driven one lets about 10% of nightly noise swing either way, the best-nights one caps that at 4% so your highest nights count as real. Truth is probably between them.');
  lim.push('Sleep hours matter a lot: each extra hour cuts the typical shortfall below the ceiling by roughly a third. Training itself costs little: about 1 to 2% of HRV the next morning per 40 load, mostly gone within a day.');
  lim.push('Rest only lifts you toward the ceiling if you also sleep well. The green line is a great morning, the best 1 in 10 under the selected model with no training and 9.5 h sleep, so it sits at or just under the ceiling. The ceiling is held steady over the week because it only moves over weeks.');
  lim.push('Your dog walk is not in intervals.icu, so it is missing from load here. Garmin\'s sleep score shares data with HRV, so the model uses sleep hours instead.');
  document.getElementById('sus-limits').innerHTML = lim.map(function(t) { return '<li>' + t + '</li>'; }).join('');
}
function susMetricChart(D, key, LT, LR, H, elId, dayLbl) {
  var Mt = D.ceil && D.ceil[key], el = document.getElementById(elId);
  if (!Mt) { el.innerHTML = '<div class="note">Ceiling model unavailable.</div>'; return; }
  var hrv = Mt.ln, MD = Mt.models.data, MB = Mt.models.best, MS = Mt.models[SUS.model];
  var mc = MS.mc, N = mc.C.length, Z = 1.2816, Cmed = susQ(mc.C, 0.5);
  var conv = hrv ? function(v) { return Math.exp(v); } : function(v) { return -v; };
  var dp = function(Mx, v) { return hrv ? v * Math.exp(Z * Mx.s) : v - Z * Mx.s; };   // level the best 1 in 10 mornings reach
  var hist = MS.recent.filter(function(r) { return r.d <= D.asof; }).slice(-30), nh = hist.length, nx = nh + H, x0 = nh - 1;
  var lastObs = hist[x0].v;
  var proj = function(loads, sleepH) {
    var F = MS.F_anchor, hc = Math.max(4, Math.min(9.5, sleepH));
    var thS = MS.theta * Math.exp(MS.k * (hc - Mt.hrs_ref));
    var o = {typ: [], lo: [], hi: [], good: [], cl: [], cm: [], ch: []}, qEff = Math.min(MS.q, 0.01);
    for (var j = 0; j <= H; j++) {
      if (j > 0) F = MS.lam * F + loads[j - 1] / D.scale;
      var yv = [], cv = [], gv = [];
      for (var i = 0; i < N; i++) {
        var C = mc.C[i] + (j > 0 ? qEff * Math.sqrt(j) * mc.n[i] : 0);
        cv.push(conv(C));
        yv.push(conv(C - MS.a * F - mc.d[i] / thS + MS.s * mc.e[i]));
        gv.push(conv(Cmed - MS.a * F - mc.d[i] / thS + MS.s * mc.e[i]));
      }
      o.typ.push(susQ(yv, 0.5)); o.lo.push(susQ(yv, 0.1)); o.hi.push(susQ(yv, 0.9));
      o.good.push(hrv ? susQ(gv, 0.9) : susQ(gv, 0.1));          // best 1 in 10 mornings at the median ceiling
      o.cl.push(susQ(cv, 0.1)); o.cm.push(susQ(cv, 0.5)); o.ch.push(susQ(cv, 0.9));
    }
    if (lastObs !== null) { o.typ[0] = lastObs; o.lo[0] = lastObs; o.hi[0] = lastObs; o.good[0] = lastObs; }
    return o;
  };
  var zero = []; for (var z = 0; z < H; z++) zero.push(0);
  var PT = proj(LT, SUS.sleep), PR = proj(LR, SUS.sleep), PB = proj(zero, 9.5);
  SUS.nxt[key] = PT.typ[1];
  var W = 820, Ht = 210, L = 44, R = 14, top = 12, bot = 22;
  var X = function(i) { return L + (W - L - R) * i / (nx - 1); };
  var tailD = MD.recent.slice(-nh), tailB = MB.recent.slice(-nh), k;
  var vals = [];
  hist.forEach(function(r, i) { if (r.v !== null) vals.push(r.v); vals.push(dp(MS, r.lo), dp(MS, r.hi), dp(MD, tailD[i].c), dp(MB, tailB[i].c)); });
  PT.lo.concat(PT.hi, PT.good).forEach(function(v) { vals.push(v); });
  PT.cl.concat(PT.ch).forEach(function(v) { vals.push(dp(MS, v)); });
  vals.push(dp(MD, MD.c_now), dp(MB, MB.c_now));
  var ymin = Math.min.apply(null, vals), ymax = Math.max.apply(null, vals), pad = (ymax - ymin) * 0.06;
  ymin -= pad; ymax += pad;
  var Y = function(v) { return top + (Ht - top - bot) * (ymax - v) / (ymax - ymin); };
  var all = []; for (k = 0; k < nx; k++) all.push(k);
  // ceiling band: only for the selected model (history posterior, then projected)
  var cLo = [], cHi = [];
  for (k = 0; k < nh; k++) { cLo.push(Y(dp(MS, hist[k].lo))); cHi.push(Y(dp(MS, hist[k].hi))); }
  for (k = 1; k <= H; k++) { cLo.push(Y(dp(MS, PT.cl[k]))); cHi.push(Y(dp(MS, PT.ch[k]))); }
  // each model's ceiling line; the selected one is projected, the other is held flat
  var lineOf = function(Mx, tail) {
    var out = [];
    for (var q = 0; q < nh; q++) out.push(Y(dp(Mx, tail[q].c)));
    for (var q2 = 1; q2 <= H; q2++) out.push(Y(dp(Mx, Mx === MS ? PT.cm[q2] : Mx.c_now)));
    return out;
  };
  var fut = []; for (k = x0; k < nx; k++) fut.push(k);
  var fLo = [], fHi = [], tT = [], tR = [], tG = [];
  for (k = 0; k <= H; k++) { fLo.push(Y(PT.lo[k])); fHi.push(Y(PT.hi[k])); tT.push(Y(PT.typ[k])); tR.push(Y(PR.typ[k])); tG.push(Y(PB.good[k])); }
  var hidx = [], hobs = [];
  hist.forEach(function(r, i) { hidx.push(i); hobs.push(r.v === null ? null : Y(r.v)); });

  var svg = '<svg class="sus-svg" viewBox="0 0 ' + W + ' ' + Ht + '" role="img" aria-label="' + Mt.label + ' history, ceiling and projection">';
  var step = (ymax - ymin) > 30 ? 10 : (ymax - ymin) > 12 ? 5 : 2;
  for (var g = Math.ceil(ymin / step) * step; g <= ymax; g += step)
    svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(g) + '" y2="' + Y(g) + '" stroke="#26292b"/><text x="' + (L - 6) + '" y="' + (Y(g) + 3) + '" text-anchor="end">' + g + '</text>';
  svg += susBand(X, all, cLo, cHi, SUS.model === 'best' ? 'rgba(217,194,122,.14)' : 'rgba(159,165,173,.22)');
  svg += susPoly(X, all, lineOf(MD, tailD), '#a3a6a9', 1.4);
  svg += susPoly(X, all, lineOf(MB, tailB), '#d9c27a', 1.4, '5 4');
  svg += susBand(X, fut, fLo, fHi, 'rgba(136,220,253,.13)');
  svg += susPoly(X, fut, tG, '#6fcf97', 1.5, '2 4');
  svg += susPoly(X, fut, tR, '#8b8e91', 1.4, '4 4');
  svg += susPoly(X, fut, tT, '#88dcfd', 2.2);
  svg += susPoly(X, hidx, hobs, 'rgba(232,233,234,.5)', 1.2);
  hist.forEach(function(r, i) { if (r.v !== null) svg += '<circle cx="' + X(i) + '" cy="' + Y(r.v) + '" r="' + (i === x0 ? 3.6 : 2.2) + '" fill="#e8e9ea"/>'; });
  svg += '<line x1="' + X(x0) + '" x2="' + X(x0) + '" y1="' + top + '" y2="' + (Ht - bot) + '" stroke="#3a3d40" stroke-dasharray="2 3"/>';
  svg += '<line class="sus-hl" y1="' + top + '" y2="' + (Ht - bot) + '" stroke="#6c6f72" style="display:none"/>';
  svg += '<text x="' + X(0) + '" y="' + (Ht - 6) + '">' + hist[0].d.slice(5) + '</text><text x="' + X(x0) + '" y="' + (Ht - 6) + '" text-anchor="middle">' + (D.stale_days > 0 ? 'last reading' : 'today') + '</text><text x="' + X(nx - 1) + '" y="' + (Ht - 6) + '" text-anchor="end">+' + H + ' days</text>';
  svg += '</svg>';

  var f = function(v) { return v.toFixed(0); }, u = Mt.unit, cw = hrv ? 'ceiling' : 'floor';
  var info = function(i) {
    if (i <= x0) {
      var r = hist[i];
      return '<div class="d">' + susDateOf(r.d) + '</div>' +
        (r.v !== null ? '<div><i style="background:#e8e9ea"></i>reading <b>' + f(r.v) + ' ' + u + '</b></div>' : '<div>no reading</div>') +
        '<div><i style="background:#a3a6a9"></i>' + cw + ' (data) ' + f(dp(MD, tailD[i].c)) + ' (' + f(dp(MD, tailD[i].lo)) + ' to ' + f(dp(MD, tailD[i].hi)) + ')</div>' +
        '<div><i style="background:#d9c27a"></i>' + cw + ' (best nights) ' + f(dp(MB, tailB[i].c)) + ' (' + f(dp(MB, tailB[i].lo)) + ' to ' + f(dp(MB, tailB[i].hi)) + ')</div>';
    }
    var j = i - x0, lo2 = Math.min(PT.lo[j], PT.hi[j]), hi2 = Math.max(PT.lo[j], PT.hi[j]);
    return '<div class="d">' + susDateOf(new Date(new Date(D.asof + 'T12:00:00').getTime() + j * 86400000).toISOString().slice(0, 10)) + ' (forecast, ' + (SUS.model === 'best' ? 'best nights' : 'data-driven') + ')</div>' +
      '<div><i style="background:#88dcfd"></i>typical <b>' + f(PT.typ[j]) + ' ' + u + '</b> (single morning ' + f(lo2) + ' to ' + f(hi2) + ')</div>' +
      '<div><i style="background:#8b8e91"></i>with usual days ' + f(PR.typ[j]) + '</div>' +
      '<div><i style="background:#6fcf97"></i>great morning ' + f(PB.good[j]) + '</div>' +
      '<div><i style="background:#a3a6a9"></i>' + cw + ' ' + f(dp(MS, PT.cm[j])) + ' (' + f(dp(MS, PT.cl[j])) + ' to ' + f(dp(MS, PT.ch[j])) + ')</div>';
  };
  el.innerHTML = '<div class="sus-ctitle">' + Mt.label + ' <span>' + u + '</span><span class="now">' + cw + ' <b>' + (f(Math.min(dp(MD, MD.c_now), dp(MB, MB.c_now))) === f(Math.max(dp(MD, MD.c_now), dp(MB, MB.c_now))) ? f(dp(MS, MS.c_now)) : f(Math.min(dp(MD, MD.c_now), dp(MB, MB.c_now))) + '</b> to <b>' + f(Math.max(dp(MD, MD.c_now), dp(MB, MB.c_now)))) + '</b></span></div>' +
    '<div class="sus-chartbox">' + svg + '<div class="sus-tip"></div></div>';
  susHover(el.querySelector('svg'), W, L, R, nx, info);
}
function susChart(D, F, LT, T, uref, H, dayIdx, plan, dayLbl) {
  var W = 820, Ht = 230, L = 38, R = 14, top = 12, plotB = 160, barB = 216, hist = D.recent.filter(function(r) { return r.d <= D.asof; }).slice(-14);
  var nh = hist.length, nx = nh + H, pm = F.pm, rm = F.rm, lo = F.lo, hi = F.hi, x0 = nh - 1;
  var X = function(i) { return L + (W - L - R) * i / (nx - 1); };
  var ys = [0];
  hist.forEach(function(r) { if (r.s !== null) ys.push(r.s); });
  lo.concat(hi).forEach(function(v) { ys.push(v); });
  var ymin = Math.min.apply(null, ys) - 0.2, ymax = Math.max.apply(null, ys) + 0.2;
  var Y = function(v) { return top + (plotB - top) * (ymax - v) / (ymax - ymin); };
  var svg = '<svg class="sus-svg" viewBox="0 0 ' + W + ' ' + Ht + '" role="img" aria-label="Recovery score history and forecast">';
  for (var g = Math.ceil(ymin); g <= Math.floor(ymax); g++)
    svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(g) + '" y2="' + Y(g) + '" stroke="' + (g === 0 ? '#4a4d50' : '#26292b') + '"/>' +
           '<text x="' + (L - 6) + '" y="' + (Y(g) + 3) + '" text-anchor="end">' + (g === 0 ? 'avg' : g) + '</text>';
  var fut = [], fLo = [], fHi = [], fP = [], fR = [], k;
  for (k = 0; k <= H; k++) { fut.push(x0 + k); fLo.push(Y(lo[k])); fHi.push(Y(hi[k])); fP.push(Y(pm[k])); fR.push(Y(rm[k])); }
  svg += susBand(X, fut, fLo, fHi, 'rgba(136,220,253,.22)');
  svg += susPoly(X, fut, fR, '#8b8e91', 1.4, '4 4') + susPoly(X, fut, fP, '#88dcfd', 2.2);
  var hidx = [], hv = [];
  hist.forEach(function(r, i) { hidx.push(i); hv.push(r.s === null ? null : Y(r.s)); });
  svg += susPoly(X, hidx, hv, '#e8e9ea', 1.8);
  hist.forEach(function(r, i) { if (r.s !== null && r.z !== null && r.z < -2) svg += '<circle cx="' + X(i) + '" cy="' + Y(r.s) + '" r="4" fill="#e0766b"/>'; });
  var maxL = Math.max(D.max_load * 0.5, T, uref, 10);
  for (k = 0; k < H; k++) if (LT[k] > maxL) maxL = LT[k];
  var BH = function(val) { return (barB - plotB - 22) * Math.min(val, maxL) / maxL; };
  hist.forEach(function(r, i) {
    if (i === x0) return;
    svg += '<rect x="' + (X(i) - 4) + '" y="' + (barB - BH(r.load)) + '" width="8" height="' + BH(r.load) + '" fill="#4a4d50" rx="1.5"/>';
  });
  for (var b = 0; b < H; b++) {
    var col = b === dayIdx ? '#88dcfd' : (b <= D.stale_days ? '#8b8e91' : (plan[b] !== null ? '#4f7f93' : '#2f3235'));
    svg += '<rect x="' + (X(x0 + b) - 4) + '" y="' + (barB - BH(LT[b])) + '" width="8" height="' + BH(LT[b]) + '" fill="' + col + '" rx="1.5"/>';
  }
  svg += '<text x="' + L + '" y="' + (plotB + 16) + '">training load per day</text>';
  svg += '<text x="' + X(0) + '" y="' + (barB + 11) + '">' + hist[0].d.slice(5) + '</text>';
  svg += '<text x="' + X(x0) + '" y="' + (barB + 11) + '" text-anchor="middle">' + (D.stale_days > 0 ? 'last reading' : 'today') + '</text>';
  svg += '<text x="' + X(nx - 1) + '" y="' + (barB + 11) + '" text-anchor="end">+' + H + ' days</text>';
  svg += '<line x1="' + X(x0) + '" x2="' + X(x0) + '" y1="' + top + '" y2="' + plotB + '" stroke="#3a3d40" stroke-dasharray="2 3"/>';
  svg += '<line class="sus-hl" y1="' + top + '" y2="' + barB + '" stroke="#6c6f72" style="display:none"/></svg>';
  var info = function(i) {
    if (i <= x0) {
      var r = hist[i];
      return '<div class="d">' + susDateOf(r.d) + '</div><div>recovery ' + (r.s === null ? 'n/a' : '<b>' + susF(r.s) + '</b> (' + susRecWord(r.s) + ')') + '</div><div>load that day ' + r.load + '</div>';
    }
    var j = i - x0, ds = new Date(new Date(D.asof + 'T12:00:00').getTime() + j * 86400000).toISOString().slice(0, 10);
    return '<div class="d">' + susDateOf(ds) + ' (forecast)</div><div>recovery <b>' + susF(pm[j]) + '</b> (' + susRecWord(pm[j]) + ')</div><div>likely range ' + susF(Math.min(lo[j], hi[j])) + ' to ' + susF(Math.max(lo[j], hi[j])) + '</div><div>load that day ' + Math.round(LT[j] !== undefined ? LT[j] : 0) + '</div>';
  };
  document.getElementById('sus-chart').innerHTML = svg + '<div class="sus-tip"></div>';
  susHover(document.querySelector('#sus-chart svg'), W, L, R, nx, info);
  document.getElementById('sus-leg').innerHTML =
    '<span><i style="background:#e8e9ea"></i>your recovery (red dot = unusually bad night)</span>' +
    '<span><i style="background:#88dcfd"></i>forecast with ' + T + ' load on ' + dayLbl + '</span>' +
    '<span><i style="background:#8b8e91"></i>with a usual day (' + Math.round(uref) + ')</span>' +
    '<span><i style="background:#4f7f93"></i>planned sessions</span>';
}
function susIrfChart(D, P) {
  var H = 6, rows = [];
  for (var h = 1; h <= H; h++) {
    var v = P.map(function(p) { return susIrf(p, H)[h]; });
    rows.push({h: h, m: v.reduce(function(a, b) { return a + b; }, 0) / v.length, lo: susQ(v, 0.05), hi: susQ(v, 0.95)});
  }
  var W = 560, Ht = 170, L = 34, R = 8, top = 14, bot = 24;
  var mn = Math.min.apply(null, rows.map(function(r) { return r.lo; }).concat([-0.05])), mx = Math.max.apply(null, rows.map(function(r) { return r.hi; }).concat([0.05]));
  var Y = function(v) { return top + (Ht - top - bot) * (mx - v) / (mx - mn); };
  var bw = (W - L - R) / H, o = '<svg class="sus-svg" viewBox="0 0 ' + W + ' ' + Ht + '">';
  o += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(0) + '" y2="' + Y(0) + '" stroke="#3a3d40"/>';
  rows.forEach(function(r, i) {
    var cx = L + bw * (i + 0.5), y0 = Y(0), y1 = Y(r.m);
    o += '<g><title>+' + r.h + (r.h === 1 ? ' day' : ' days') + ': ' + susF(r.m) + ' (90% range ' + susF(r.lo) + ' to ' + susF(r.hi) + ')</title>' +
      '<rect x="' + (cx - bw * 0.3) + '" y="' + Math.min(y0, y1) + '" width="' + (bw * 0.6) + '" height="' + Math.max(1, Math.abs(y1 - y0)) + '" fill="' + (r.m < 0 ? '#e0766b' : '#88dcfd') + '" opacity=".75" rx="2"/>' +
      '<line x1="' + cx + '" x2="' + cx + '" y1="' + Y(r.lo) + '" y2="' + Y(r.hi) + '" stroke="#a3a6a9"/>' +
      '<rect x="' + (cx - bw / 2) + '" y="' + top + '" width="' + bw + '" height="' + (Ht - top - bot) + '" fill="transparent"/></g>';
    o += '<text x="' + cx + '" y="' + (Ht - 8) + '" text-anchor="middle">+' + r.h + (r.h === 1 ? ' day' : ' days') + '</text>';
    o += '<text x="' + cx + '" y="' + (r.m < 0 ? Y(r.lo) + 12 : Y(r.hi) - 4) + '" text-anchor="middle">' + susF(r.m) + '</text>';
  });
  o += '<text x="' + (L - 6) + '" y="' + (Y(0) + 3) + '" text-anchor="end">0</text></svg>';
  document.getElementById('sus-irf').innerHTML = o;
}
function susExplain() {
  var b = document.getElementById('sus-exp-btn'), st = document.getElementById('sus-exp-st');
  b.disabled = true; st.textContent = 'Asking Qwen (about a minute on CPU)...';
  document.title = '__susexplain__' + Date.now() + '|' + encodeURIComponent(JSON.stringify(SUS.last || {}));
}
function susExplainDone(text, src) {
  var b = document.getElementById('sus-exp-btn'), st = document.getElementById('sus-exp-st');
  if (text) document.getElementById('sus-exp-txt').textContent = text;
  if (b) b.disabled = false;
  if (st) st.textContent = src === 'qwen' ? 'Reworded by Qwen; numbers checked against the model' :
    'Qwen unavailable or unreliable, showing the model-code text';
}
"""
