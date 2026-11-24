"""
Upstox Intraday Options Dashboard (read-only decision support, places NO orders)

Deploy: see README.md (hosted on Render, opened from Safari).
Required env vars: DASH_PASSWORD (your login password), SECRET_KEY (random).
Each morning: log in, paste today's Upstox access token in the box at the top.
Upstox access tokens expire daily (~3:30 AM IST).
"""
import os, time, hmac, secrets, json, re, zlib, codecs, threading, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
import requests
from flask import Flask, jsonify, request, Response, session, redirect

TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
BASE = "https://api.upstox.com/v2"
INDEXES = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
VIX_KEY = "NSE_INDEX|India VIX"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("PORT")),  # https when hosted
)
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
_expiry_cache = {}
_fails = {}
_hist = {}  # per index: today's OI/spot readings for the chart (memory only)

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login</title><style>body{background:#0f1318;color:#e6e9ee;font:16px system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#181e26;padding:24px;border-radius:12px;display:flex;flex-direction:column;gap:12px;width:280px}
input,button{font-size:16px;padding:10px;border-radius:8px;border:1px solid #2b3440;background:#0f1318;color:#e6e9ee}
button{background:#3b82f6;border:0;font-weight:600}.m{color:#eab308;min-height:18px;font-size:13px}</style></head>
<body><form method="post"><b>Options Dashboard</b><input type="password" name="password" placeholder="Password" autofocus>
<button>Log in</button><div class="m">__MSG__</div></form></body></html>"""


@app.before_request
def guard():
    if request.endpoint in ("login", "static"):
        return None
    if not DASH_PASSWORD:
        return Response("Set the DASH_PASSWORD environment variable first.", 500)
    if not session.get("ok"):
        if request.path.startswith("/api/") or request.path == "/token":
            return jsonify({"errors": ["Not logged in"]}), 401
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
    msg = ""
    if request.method == "POST":
        pw = request.form.get("password", "").encode()
        if len(_fails[ip]) >= 5:
            msg = "Too many attempts. Try again in 10 minutes."
        elif DASH_PASSWORD and hmac.compare_digest(pw, DASH_PASSWORD.encode()):
            session["ok"] = True
            return redirect("/")
        else:
            _fails[ip].append(now)
            msg = "Wrong password."
    return Response(LOGIN_HTML.replace("__MSG__", msg), mimetype="text/html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/token", methods=["POST"])
def set_token():
    global TOKEN
    TOKEN = ((request.get_json(silent=True) or {}).get("token") or "").strip()
    _expiry_cache.clear()
    return jsonify({"ok": bool(TOKEN)})


def api(path, params=None, base=BASE):
    r = requests.get(
        base + path,
        params=params,
        headers={"Accept": "application/json", "Authorization": "Bearer " + TOKEN},
        timeout=10,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("data")


def ema(v, n):
    k, e, out = 2 / (n + 1), v[0], [v[0]]
    for x in v[1:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag, al = (ag * (n - 1) + g[i]) / n, (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def nearest_expiry(key):
    today = dt.datetime.now(IST).date().isoformat()
    if _expiry_cache.get(key, (None, None))[0] != today:
        data = api("/option/contract", {"instrument_key": key})
        exps = sorted({str(c["expiry"])[:10] for c in data})
        _expiry_cache[key] = (today, exps)
    exps = _expiry_cache[key][1]
    upcoming = [e for e in exps if e >= today]
    return upcoming[0] if upcoming else None, upcoming[:4]


def candles_5m(key):
    data = api(f"/historical-candle/intraday/{quote(key, safe='')}/1minute")
    c1 = list(reversed(data.get("candles", [])))  # chronological
    out = []
    for i in range(0, len(c1), 5):
        ch = c1[i:i + 5]
        out.append({
            "o": ch[0][1], "h": max(x[2] for x in ch),
            "l": min(x[3] for x in ch), "c": ch[-1][4],
        })
    return out


def prev_day_levels(key):
    today = dt.datetime.now(IST).date()
    frm = today - dt.timedelta(days=10)
    data = api(f"/historical-candle/{quote(key, safe='')}/day/{today}/{frm}")
    for c in data.get("candles", []):  # newest first
        if c[0][:10] < today.isoformat():
            return {"pdh": c[2], "pdl": c[3], "pdc": c[4]}
    return None


def max_pain(rows):
    best, best_v = None, None
    for k in rows:
        K = k["strike"]
        pain = sum(r["ce_oi"] * max(0, K - r["strike"]) + r["pe_oi"] * max(0, r["strike"] - K) for r in rows)
        if best_v is None or pain < best_v:
            best, best_v = K, pain
    return best


@app.route("/api/data")
def data():
    index = request.args.get("index", "NIFTY")
    key = INDEXES.get(index)
    out = {"errors": [], "time": dt.datetime.now(IST).strftime("%H:%M:%S")}
    if not TOKEN:
        out["errors"].append("Paste today's Upstox access token in the box at the top and tap Save")
        return jsonify(out)

    # option chain
    rows, spot, expiry, expiries = [], None, None, []
    try:
        expiry, expiries = nearest_expiry(key)
        chain = api("/option/chain", {"instrument_key": key, "expiry_date": expiry})
        for x in chain:
            ce, pe = x["call_options"], x["put_options"]
            spot = x.get("underlying_spot_price", spot)
            rows.append({
                "strike": x["strike_price"],
                "ce_ltp": ce["market_data"].get("ltp"), "pe_ltp": pe["market_data"].get("ltp"),
                "ce_oi": ce["market_data"].get("oi") or 0, "pe_oi": pe["market_data"].get("oi") or 0,
                "ce_poi": ce["market_data"].get("prev_oi") or 0, "pe_poi": pe["market_data"].get("prev_oi") or 0,
                "ce_iv": (ce.get("option_greeks") or {}).get("iv"), "pe_iv": (pe.get("option_greeks") or {}).get("iv"),
                "ce_delta": (ce.get("option_greeks") or {}).get("delta"),
                "pe_delta": (pe.get("option_greeks") or {}).get("delta"),
            })
        rows.sort(key=lambda r: r["strike"])
    except Exception as e:
        out["errors"].append(str(e))

    # VIX
    try:
        q = api("/market-quote/quotes", {"instrument_key": VIX_KEY})
        v = list(q.values())[0]
        out["vix"] = {"ltp": v.get("last_price"), "chg": v.get("net_change")}
    except Exception as e:
        out["errors"].append("VIX: " + str(e))

    # candles + previous day
    c5, prev = [], None
    try:
        c5 = candles_5m(key)
    except Exception as e:
        out["errors"].append("Candles: " + str(e))
    try:
        prev = prev_day_levels(key)
    except Exception as e:
        out["errors"].append("Prev day: " + str(e))

    if not rows or spot is None:
        return jsonify(out)

    tot_ce, tot_pe = sum(r["ce_oi"] for r in rows), sum(r["pe_oi"] for r in rows)
    pcr = round(tot_pe / tot_ce, 2) if tot_ce else None

    # record a reading for the OI vs index chart (resets each day / new expiry)
    today_s = dt.datetime.now(IST).date().isoformat()
    h = _hist.get(index)
    if not h or h["date"] != today_s or h.get("expiry") != expiry:
        h = _hist[index] = {"date": today_s, "expiry": expiry, "pts": []}
    now_ts = time.time()
    last = h["pts"][-1] if h["pts"] else None
    changed = not last or (last["ce"], last["pe"], last["spot"]) != (tot_ce, tot_pe, spot)
    if changed and (not last or now_ts - last["ts"] >= 25):
        h["pts"].append({"ts": now_ts, "t": dt.datetime.now(IST).strftime("%H:%M"),
                         "ce": tot_ce, "pe": tot_pe, "spot": spot})
        del h["pts"][:-1500]
    atm = min(rows, key=lambda r: abs(r["strike"] - spot))
    res_row = max((r for r in rows if r["strike"] >= spot), key=lambda r: r["ce_oi"], default=None)
    sup_row = max((r for r in rows if r["strike"] <= spot), key=lambda r: r["pe_oi"], default=None)
    i = rows.index(atm)
    near = rows[max(0, i - 5): i + 6]
    d_pe = sum(r["pe_oi"] - r["pe_poi"] for r in near)
    d_ce = sum(r["ce_oi"] - r["ce_poi"] for r in near)

    # bias score
    score, why = 0.0, []

    def add(pts, text):
        nonlocal score
        score += pts
        why.append({"pts": pts, "text": text})

    e9 = e21 = r14 = None
    if len(c5) >= 22:
        closes = [c["c"] for c in c5]
        e9, e21, r14 = ema(closes, 9)[-1], ema(closes, 21)[-1], rsi(closes)
        if closes[-1] > e21 and e9 > e21: add(1, "Price above 5m EMA21 and EMA9 > EMA21 (uptrend)")
        elif closes[-1] < e21 and e9 < e21: add(-1, "Price below 5m EMA21 and EMA9 < EMA21 (downtrend)")
        else: add(0, "5m EMAs mixed (no clear trend)")
        if r14 is not None:
            if r14 > 58: add(1, f"5m RSI {r14:.0f} (bullish momentum)")
            elif r14 < 42: add(-1, f"5m RSI {r14:.0f} (bearish momentum)")
            else: add(0, f"5m RSI {r14:.0f} (neutral)")
    else:
        why.append({"pts": 0, "text": "Not enough 5m candles yet for EMA/RSI (need ~22)"})
    if prev:
        if spot > prev["pdh"]: add(1, "Spot above previous day high")
        elif spot < prev["pdl"]: add(-1, "Spot below previous day low")
        else: add(0, "Spot inside previous day range")
    if pcr is not None:
        if pcr > 1.2: add(1, f"PCR {pcr} (put-heavy, supportive)")
        elif pcr < 0.8: add(-1, f"PCR {pcr} (call-heavy, capping)")
        else: add(0, f"PCR {pcr} (balanced)")
    if d_pe > d_ce * 1.3 and d_pe > 0: add(1, "Fresh put OI > call OI near ATM (put writing)")
    elif d_ce > d_pe * 1.3 and d_ce > 0: add(-1, "Fresh call OI > put OI near ATM (call writing)")
    else: add(0, "OI change near ATM balanced")
    if res_row and (res_row["strike"] - spot) / spot < 0.003: add(-1, f"Spot within 0.3% of max call OI wall {res_row['strike']}")
    if sup_row and (spot - sup_row["strike"]) / spot < 0.003: add(1, f"Spot within 0.3% of max put OI support {sup_row['strike']}")

    bias = "BULLISH (CE side)" if score >= 3 else "BEARISH (PE side)" if score <= -3 else "NEUTRAL / NO TRADE"
    out.update({
        "index": index, "spot": spot, "expiry": expiry, "expiries": expiries,
        "pcr": pcr, "max_pain": max_pain(rows),
        "resistance": res_row["strike"] if res_row else None,
        "support": sup_row["strike"] if sup_row else None,
        "prev": prev, "ema9": e9, "ema21": e21, "rsi": r14,
        "day_high": max((c["h"] for c in c5), default=None),
        "day_low": min((c["l"] for c in c5), default=None),
        "score": score, "bias": bias, "why": why,
        "atm": {"strike": atm["strike"], "ce_ltp": atm["ce_ltp"], "pe_ltp": atm["pe_ltp"],
                "ce_delta": atm["ce_delta"], "pe_delta": atm["pe_delta"],
                "ce_iv": atm["ce_iv"], "pe_iv": atm["pe_iv"]},
        "chain": near, "hist": h["pts"],
    })

    # trade idea
    if len(c5) >= 6 and score >= 3:
        sl = min(c["l"] for c in c5[-6:])
        tgt = res_row["strike"] if res_row and res_row["strike"] > spot else None
        out["idea"] = {"side": "CE", "entry": f"5m close above {max(c['h'] for c in c5[-3:]):.0f}",
                       "sl": sl, "sl_pts": max(spot - sl, 10), "target": tgt}
    elif len(c5) >= 6 and score <= -3:
        sl = max(c["h"] for c in c5[-6:])
        tgt = sup_row["strike"] if sup_row and sup_row["strike"] < spot else None
        out["idea"] = {"side": "PE", "entry": f"5m close below {min(c['l'] for c in c5[-3:]):.0f}",
                       "sl": sl, "sl_pts": max(sl - spot, 10), "target": tgt}
    return jsonify(out)


HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upstox Options Dashboard</title><style>
:root{--bg:#0f1318;--card:#181e26;--tx:#e6e9ee;--mut:#8b95a3;--g:#22c55e;--r:#ef4444;--y:#eab308;--b:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif;padding:12px}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
select,input{background:#0f1318;color:var(--tx);border:1px solid #2b3440;border-radius:6px;padding:6px 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}
.card{background:var(--card);border-radius:10px;padding:12px}.card h3{margin:0 0 8px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.big{font-size:26px;font-weight:700}.row{display:flex;justify-content:space-between;padding:3px 0}.mut{color:var(--mut)}
.g{color:var(--g)}.r{color:var(--r)}.y{color:var(--y)}table{width:100%;border-collapse:collapse;font-size:12px}
td,th{padding:3px 4px;text-align:right}th{color:var(--mut)}tr.atm{background:#232c38}
.bar{display:inline-block;height:8px;border-radius:2px}.warn{background:#3a1d1d;border:1px solid var(--r);padding:10px;border-radius:8px;margin-bottom:10px;display:none;font-weight:600}
.err{color:var(--y);font-size:12px}
</style></head><body>
<div class="top"><b>Options Dashboard</b> <a href="/scanner" style="color:#60a5fa">Stock scanner</a>
<select id="idx"><option>NIFTY</option><option>BANKNIFTY</option><option>SENSEX</option></select>
<input id="tok" type="password" placeholder="Today's Upstox token" style="width:170px"><button id="sv" onclick="saveTok()" style="background:#3b82f6;color:#fff;border:0;border-radius:6px;padding:7px 12px">Save</button>
<span class="mut" id="stamp"></span><a href="/logout" class="mut" style="margin-left:auto">Log out</a></div>
<div class="warn" id="halt">DAILY LOSS LIMIT HIT. Stop trading for today.</div>
<div class="err" id="err"></div>
<div class="grid">
<div class="card"><h3>Bias (rule-based)</h3><div class="big" id="bias">...</div><div class="mut" id="score"></div><div id="why" style="margin-top:8px"></div></div>
<div class="card"><h3>Market</h3><div id="mkt"></div></div>
<div class="card"><h3>Trade idea</h3><div id="idea" class="mut">Waiting for a clear bias</div></div>
<div class="card"><h3>Position size and daily limit</h3>
<div class="row"><span>Capital</span><input id="cap" type="number" value="200000" style="width:110px"></div>
<div class="row"><span>Risk per trade %</span><input id="rp" type="number" step="0.1" value="1" style="width:110px"></div>
<div class="row"><span>Lot size (verify!)</span><input id="lot" type="number" value="75" style="width:110px"></div>
<div class="row"><span>SL (index pts)</span><input id="slp" type="number" value="30" style="width:110px"></div>
<div class="row"><span>Max daily loss</span><input id="mdl" type="number" value="5000" style="width:110px"></div>
<div class="row"><span>Today's P&amp;L</span><input id="pnl" type="number" value="0" style="width:110px"></div>
<div id="size" style="margin-top:6px"></div></div>
</div>
<div class="card" style="margin-top:10px"><h3>Option chain near ATM</h3><table id="chain"></table></div>
<div class="card" style="margin-top:10px"><h3>Total call OI vs put OI vs index (today)</h3>
<canvas id="cv" style="width:100%;height:260px;display:block"></canvas>
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:6px;font-size:12px">
<span><b style="color:#ef4444">&#9473;</b> Call OI (left)</span>
<span><b style="color:#22c55e">&#9548;&#9548;</b> Put OI (left)</span>
<span><b style="color:#60a5fa">&#9473;</b> Index (right)</span></div>
<div class="mut" style="font-size:11px;margin-top:4px">Built from readings taken while this server is running, so it starts blank each day or after a restart. Rising put OI with a rising index, or rising call OI with a falling index, is the usual confirming pattern.</div></div>
<p class="mut" style="font-size:11px">Decision support only. Not advice, no orders are placed. Verify lot size and signals yourself.</p>
<script>
const $=id=>document.getElementById(id);let D=null;
for(const k of ['cap','rp','lot','slp','mdl','pnl']){try{const v=localStorage.getItem('d_'+k);if(v!==null)$(k).value=v}catch(e){}
 $(k).addEventListener('input',()=>{try{localStorage.setItem('d_'+k,$(k).value)}catch(e){};calc()})}
function f(n,d=0){return n==null?'-':Number(n).toLocaleString('en-IN',{maximumFractionDigits:d})}
function cls(n){return n>0?'g':n<0?'r':''}
function calc(){
 const risk=$('cap').value*$('rp').value/100, lot=+$('lot').value, sl=+$('slp').value;
 const side=D&&D.idea?D.idea.side:null, a=D&&D.atm;
 const delta=a?Math.abs(side==='PE'?a.pe_delta:a.ce_delta)||0.5:0.5;
 const perLot=sl*delta*lot, lots=perLot>0?Math.floor(risk/perLot):0;
 $('size').innerHTML=`Risk amount: <b>${f(risk)}</b><br>Approx risk per lot (delta ${delta.toFixed(2)}): ${f(perLot)}<br>Suggested lots: <b>${lots}</b>`;
 $('halt').style.display=(+$('pnl').value<=-Math.abs($('mdl').value))?'block':'none';
}
function render(d){D=d;$('stamp').textContent='Updated '+d.time;
 $('err').textContent=(d.errors||[]).join(' | ');
 if(!d.spot)return;
 const c=d.score>=3?'g':d.score<=-3?'r':'y';
 $('bias').innerHTML=`<span class="${c}">${d.bias}</span>`;
 $('score').textContent='Score '+d.score+' (needs +3 / -3 to signal)';
 $('why').innerHTML=d.why.map(w=>`<div class="row"><span>${w.text}</span><b class="${cls(w.pts)}">${w.pts>0?'+':''}${w.pts}</b></div>`).join('');
 const v=d.vix,p=d.prev;
 $('mkt').innerHTML=`<div class="big">${f(d.spot,2)}</div>
 <div class="row"><span>Expiry</span><span>${d.expiry}</span></div>
 <div class="row"><span>India VIX</span><span>${v?f(v.ltp,2)+' ('+f(v.chg,2)+')':'-'}</span></div>
 <div class="row"><span>PCR</span><span>${d.pcr}</span></div>
 <div class="row"><span>Max pain</span><span>${f(d.max_pain)}</span></div>
 <div class="row"><span>Resistance (max call OI)</span><span class="r">${f(d.resistance)}</span></div>
 <div class="row"><span>Support (max put OI)</span><span class="g">${f(d.support)}</span></div>
 <div class="row"><span>Prev day H / L / C</span><span>${p?f(p.pdh)+' / '+f(p.pdl)+' / '+f(p.pdc):'-'}</span></div>
 <div class="row"><span>Day H / L</span><span>${f(d.day_high)} / ${f(d.day_low)}</span></div>
 <div class="row"><span>5m EMA9 / EMA21 / RSI</span><span>${f(d.ema9)} / ${f(d.ema21)} / ${f(d.rsi)}</span></div>`;
 if(d.idea){const i=d.idea,rr=i.target?((Math.abs(i.target-d.spot))/i.sl_pts).toFixed(1):'-';
  $('idea').innerHTML=`<div class="row"><span>Side</span><b class="${i.side==='CE'?'g':'r'}">${i.side} @ ATM ${d.atm.strike}</b></div>
  <div class="row"><span>Trigger</span><span>${i.entry}</span></div>
  <div class="row"><span>Index SL</span><span>${f(i.sl)} (${f(i.sl_pts)} pts)</span></div>
  <div class="row"><span>Target (OI wall)</span><span>${i.target?f(i.target):'-'}</span></div>
  <div class="row"><span>Reward : Risk</span><b>${rr}</b></div>
  <div class="row"><span>ATM ${i.side} LTP / IV</span><span>${f(i.side==='CE'?d.atm.ce_ltp:d.atm.pe_ltp,2)} / ${f(i.side==='CE'?d.atm.ce_iv:d.atm.pe_iv,1)}</span></div>
  <div class="mut">Skip if reward:risk is under 1.5 or VIX is spiking.</div>`;
  $('slp').value=Math.round(i.sl_pts)}
 else $('idea').innerHTML='<span class="mut">No clear bias. Staying out is a valid trade.</span>';
 const mx=Math.max(...d.chain.map(r=>Math.max(r.ce_oi,r.pe_oi)),1);
 $('chain').innerHTML='<tr><th>CE OI chg</th><th>CE OI</th><th>CE LTP</th><th>Strike</th><th>PE LTP</th><th>PE OI</th><th>PE OI chg</th></tr>'+
  d.chain.map(r=>`<tr class="${r.strike===d.atm.strike?'atm':''}">
  <td class="${cls(-(r.ce_oi-r.ce_poi))}">${f(r.ce_oi-r.ce_poi)}</td>
  <td><span class="bar" style="width:${r.ce_oi/mx*60}px;background:var(--r)"></span> ${f(r.ce_oi)}</td><td>${f(r.ce_ltp,2)}</td>
  <td><b>${r.strike}</b></td><td>${f(r.pe_ltp,2)}</td>
  <td>${f(r.pe_oi)} <span class="bar" style="width:${r.pe_oi/mx*60}px;background:var(--g)"></span></td>
  <td class="${cls(r.pe_oi-r.pe_poi)}">${f(r.pe_oi-r.pe_poi)}</td></tr>`).join('');
 drawChart(d.hist);calc()}
let lastHist=[];
function drawChart(pts){
 lastHist=pts||[];const cv=$('cv');if(!cv)return;
 const w=cv.clientWidth,h=cv.clientHeight,dpr=window.devicePixelRatio||1;
 cv.width=w*dpr;cv.height=h*dpr;const x=cv.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
 x.font='11px system-ui';
 if(lastHist.length<2){x.fillStyle='#8b95a3';x.fillText('Collecting readings... keep this page open (needs at least 2).',12,24);return}
 const P=lastHist,n=P.length,L=54,R=54,T=10,B=22,pw=w-L-R,ph=h-T-B;
 let a0=Math.min(...P.map(p=>Math.min(p.ce,p.pe))),a1=Math.max(...P.map(p=>Math.max(p.ce,p.pe)));
 let s0=Math.min(...P.map(p=>p.spot)),s1=Math.max(...P.map(p=>p.spot));
 const pad=(lo,hi)=>{const d=(hi-lo)||Math.max(Math.abs(hi)*0.002,1);return[lo-d*0.08,hi+d*0.08]};
 [a0,a1]=pad(a0,a1);[s0,s1]=pad(s0,s1);
 const X=i=>L+pw*i/(n-1),Yo=v=>T+ph*(1-(v-a0)/(a1-a0)),Ys=v=>T+ph*(1-(v-s0)/(s1-s0));
 x.strokeStyle='#2b3440';x.fillStyle='#8b95a3';x.lineWidth=1;
 for(let g=0;g<=4;g++){const y=T+ph*g/4;x.beginPath();x.moveTo(L,y);x.lineTo(L+pw,y);x.stroke();
  x.textAlign='right';x.fillText((a1-(a1-a0)*g/4)/1e5>=100?((a1-(a1-a0)*g/4)/1e7).toFixed(2)+'Cr':((a1-(a1-a0)*g/4)/1e5).toFixed(1)+'L',L-6,y+4);
  x.textAlign='left';x.fillText(f(s1-(s1-s0)*g/4),L+pw+6,y+4)}
 x.textAlign='center';[0,Math.floor((n-1)/2),n-1].forEach(i=>x.fillText(P[i].t,X(i),h-6));
 const line=(key,Y,col,wd,dash)=>{x.beginPath();x.strokeStyle=col;x.lineWidth=wd;x.setLineDash(dash);
  P.forEach((p,i)=>{const px=X(i),py=Y(p[key]);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();x.setLineDash([])};
 line('ce',Yo,'#ef4444',2,[]);line('pe',Yo,'#22c55e',2,[6,4]);line('spot',Ys,'#60a5fa',2.5,[]);
}
window.addEventListener('resize',()=>drawChart(lastHist));
async function saveTok(){const t=$('tok').value.trim();if(!t)return;
 try{await fetch('/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t})});$('tok').value='';load()}catch(e){$('err').textContent=String(e)}}
async function load(){try{const r=await fetch('/api/data?index='+$('idx').value);
 if(r.status===401){location='/login';return}render(await r.json())}catch(e){$('err').textContent=String(e)}}
$('idx').addEventListener('change',load);load();setInterval(load,10000);
</script></body></html>"""


# ======================= Nifty 50 scanner =======================
BASE3 = "https://api.upstox.com/v3"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
# Nifty 50 after the 30-Sep-2026 rebalance (BSE in, Wipro out). The index changes every
# March/September: set env var SCAN_SYMBOLS=SYM1,SYM2,... on Render to override without editing code.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC", "POWERGRID", "RELIANCE",
    "SBILIFE", "SHRIRAMFIN", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM", "TMPV", "TATASTEEL",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO",
]
SYMBOLS = [s.strip() for s in os.environ.get("SCAN_SYMBOLS", "").split(",") if s.strip()] or NIFTY50
SCAN_TTL = 90  # seconds between full rescans (keeps well inside Upstox rate limits)
_uni = {"date": None, "map": {}, "missing": []}
_hist5 = {"date": None, "c": {}}
_scan = {"ts": 0.0, "data": None, "running": False, "progress": "", "err": None}
_scan_lock = threading.Lock()


def norm(s):
    return re.sub(r"[^A-Z0-9&]", "", s.upper())


def _open_instruments():
    return requests.get(INSTR_URL, stream=True, timeout=120)


def _iter_json_array_gz(resp):
    """Stream objects out of a big gzipped JSON array without loading it all in memory."""
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    txt = codecs.getincrementaldecoder("utf-8")("ignore")
    jd = json.JSONDecoder()
    buf = ""
    for chunk in resp.raw.stream(1 << 16, decode_content=False):
        buf += txt.decode(dec.decompress(chunk))
        i, n = 0, len(buf)
        while True:
            while i < n and buf[i] in " \r\n\t,[":
                i += 1
            if i >= n or buf[i] == "]":
                break
            try:
                obj, i = jd.raw_decode(buf, i)
            except ValueError:
                break  # object continues in the next chunk
            yield obj
        buf = buf[i:]


def load_universe():
    today = dt.datetime.now(IST).date().isoformat()
    if _uni["date"] == today and _uni["map"]:
        return
    want = {norm(s): s for s in SYMBOLS}
    found = {}
    r = _open_instruments()
    r.raise_for_status()
    try:
        for o in _iter_json_array_gz(r):
            if o.get("segment") != "NSE_EQ":
                continue
            ts = norm(o.get("trading_symbol", ""))
            if ts in want and ts not in found and o.get("instrument_key"):
                found[ts] = o["instrument_key"]
                if len(found) == len(want):
                    break
    finally:
        r.close()
    _uni.update(date=today, map={want[k]: v for k, v in found.items()},
                missing=[want[k] for k in want if k not in found])


def parse_c(raw):
    out = [[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] or 0)] for x in raw]
    out.reverse()  # API returns newest first
    return out


def fetch_candles(key):
    k = quote(key, safe="")
    today = dt.datetime.now(IST).date()
    hc = _hist5["c"].get(key)
    if hc is None:  # past sessions: fetched once per day
        to_d = (today - dt.timedelta(days=1)).isoformat()
        from_d = (today - dt.timedelta(days=8)).isoformat()
        d = api(f"/historical-candle/{k}/minutes/5/{to_d}/{from_d}", base=BASE3)
        hc = parse_c((d or {}).get("candles", []))
        _hist5["c"][key] = hc
    d = api(f"/historical-candle/intraday/{k}/minutes/5", base=BASE3)
    ic = parse_c((d or {}).get("candles", []))
    merged = {c[0]: c for c in hc + ic}
    return [merged[t] for t in sorted(merged)]


def supertrend(h, l, c, period=10, mult=3.0):
    n = len(c)
    if n < period + 2:
        return None, None
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = [None] * n
    a = sum(tr[:period]) / period
    atr[period - 1] = a
    for i in range(period, n):
        a = (a * (period - 1) + tr[i]) / period
        atr[i] = a
    dirs, st = [0] * n, [None] * n
    fu = fl = None
    d = 1
    for i in range(period - 1, n):
        hl2 = (h[i] + l[i]) / 2
        bu, bl = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if fu is None:
            fu, fl = bu, bl
            d = 1 if c[i] >= hl2 else -1
        else:
            fu = bu if (bu < fu or c[i - 1] > fu) else fu
            fl = bl if (bl > fl or c[i - 1] < fl) else fl
            if d == -1 and c[i] > fu:
                d = 1
            elif d == 1 and c[i] < fl:
                d = -1
        dirs[i] = d
        st[i] = fl if d == 1 else fu
    return dirs, st


def boll_at(closes, end, n=20, k=2.0):
    w = closes[end - n:end]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return m, m + k * sd, m - k * sd


def analyze(sym, cs):
    now = dt.datetime.now(IST)
    if cs and cs[-1][0][:10] == now.date().isoformat():
        try:
            if dt.datetime.fromisoformat(cs[-1][0]) + dt.timedelta(minutes=5) > now:
                cs = cs[:-1]  # drop the still-forming candle
        except ValueError:
            pass
    if len(cs) < 40:
        return None
    sess = cs[-1][0][:10]
    i0 = next(i for i, c in enumerate(cs) if c[0][:10] == sess)
    today_c, prior = cs[i0:], cs[:i0]
    if not prior:
        return None
    o_, h_, l_, c_, v_ = ([x[j] for x in cs] for j in (1, 2, 3, 4, 5))
    last = c_[-1]
    prev_day = prior[-1][0][:10]
    pday = [x for x in prior if x[0][:10] == prev_day]
    pdh, pdl, pclose = max(x[2] for x in pday), min(x[3] for x in pday), pday[-1][4]
    chg = (last / pclose - 1) * 100 if pclose else 0

    e9, e21 = ema(c_, 9)[-1], ema(c_, 21)[-1]
    r14 = rsi(c_) or 50
    pv = vv = 0.0
    for x in today_c:
        pv += (x[2] + x[3] + x[4]) / 3 * x[5]
        vv += x[5]
    vwap = pv / vv if vv > 0 else last
    dirs, stv = supertrend(h_, l_, c_)
    sdir = dirs[-1] if dirs else 0
    flip = bool(dirs) and any(dirs[i] != dirs[i - 1] for i in range(len(dirs) - 3, len(dirs)))
    mid, up, lo = boll_at(c_, len(c_))
    pctb = (last - lo) / (up - lo) if up > lo else 0.5
    bws = []
    for j in range(max(20, len(c_) - 100), len(c_) + 1):
        m, u2, l2 = boll_at(c_, j)
        bws.append((u2 - l2) / m if m else 0)
    squeeze = len(bws) >= 30 and bws[-1] <= sorted(bws)[int(0.2 * len(bws))]

    # volume vs the same time-of-day slot on earlier sessions
    pdates = sorted({x[0][:10] for x in prior})[-5:]
    slots = {}
    for x in prior:
        if x[0][:10] in pdates:
            slots.setdefault(x[0][11:16], []).append(x[5])
    ratios = []
    for x in today_c[-2:]:
        s = slots.get(x[0][11:16])
        if s and sum(s) > 0:
            ratios.append(x[5] / (sum(s) / len(s)))
    rvol = sum(ratios) / len(ratios) if ratios else None

    sc, why = 0, []

    def add(p, t):
        nonlocal sc
        sc += p
        why.append([p, t])

    if last > e21 and e9 > e21: add(1, "Price above EMA21 and EMA9 > EMA21 (uptrend)")
    elif last < e21 and e9 < e21: add(-1, "Price below EMA21 and EMA9 < EMA21 (downtrend)")
    else: add(0, "EMAs mixed")
    if r14 > 58: add(1, f"RSI {r14:.0f} (bullish momentum)")
    elif r14 < 42: add(-1, f"RSI {r14:.0f} (bearish momentum)")
    else: add(0, f"RSI {r14:.0f} (neutral)")
    if last > vwap: add(1, f"Above VWAP {vwap:.2f}")
    elif last < vwap: add(-1, f"Below VWAP {vwap:.2f}")
    else: add(0, "At VWAP")
    if sdir > 0: add(1, "Supertrend(10,3) up" + (" - flipped recently" if flip else ""))
    elif sdir < 0: add(-1, "Supertrend(10,3) down" + (" - flipped recently" if flip else ""))
    else: add(0, "Supertrend n/a")
    if pctb >= 0.8: add(1, f"Bollinger %B {pctb*100:.0f}% (riding upper band)" + (", squeeze" if squeeze else ""))
    elif pctb <= 0.2: add(-1, f"Bollinger %B {pctb*100:.0f}% (riding lower band)" + (", squeeze" if squeeze else ""))
    else: add(0, f"Bollinger %B {pctb*100:.0f}% (mid-range)" + (", squeeze" if squeeze else ""))
    if last > pdh: add(1, f"Above previous day high {pdh:.2f}")
    elif last < pdl: add(-1, f"Below previous day low {pdl:.2f}")
    else: add(0, "Inside previous day range")
    move = today_c[-1][4] - (today_c[-2][1] if len(today_c) >= 2 else today_c[-1][1])
    if rvol is not None and rvol >= 1.5 and move > 0: add(1, f"Volume {rvol:.1f}x normal on rising price")
    elif rvol is not None and rvol >= 1.5 and move < 0: add(-1, f"Volume {rvol:.1f}x normal on falling price")
    else: add(0, "Volume not unusual" if rvol is not None else "Volume baseline n/a")

    sig = "STRONG BULL" if sc >= 6 else "BULL" if sc >= 4 else "STRONG BEAR" if sc <= -6 else "BEAR" if sc <= -4 else "NEUTRAL"
    r2 = lambda x, n=2: None if x is None else round(x, n)
    return {
        "sym": sym, "ltp": r2(last), "chg": r2(chg), "score": sc, "sig": sig,
        "vwap": r2(vwap), "vwap_pct": r2((last / vwap - 1) * 100), "vside": 1 if last > vwap else -1 if last < vwap else 0,
        "st": sdir, "st_val": r2(stv[-1]) if stv else None, "flip": flip,
        "pctb": r2(pctb, 3), "bb": [r2(lo), r2(mid), r2(up)], "squeeze": squeeze,
        "rvol": r2(rvol, 1), "rsi": r2(r14, 1), "e9": r2(e9), "e21": r2(e21),
        "pdh": r2(pdh), "pdl": r2(pdl), "why": why, "t": cs[-1][0][11:16], "session": sess,
    }


def _scan_worker():
    try:
        _scan["err"] = None
        _scan["progress"] = "Loading instrument list..."
        load_universe()
        if not _uni["map"]:
            raise RuntimeError("None of the Nifty 50 symbols were found in the Upstox instrument list")
        today = dt.datetime.now(IST).date().isoformat()
        if _hist5["date"] != today:
            _hist5.update(date=today, c={})
        items = list(_uni["map"].items())
        res, errs, done = [], [], [0]

        def work(it):
            sym, key = it
            try:
                a = analyze(sym, fetch_candles(key))
                if a: res.append(a)
                else: errs.append(f"{sym}: not enough candles")
            except Exception as e:
                errs.append(f"{sym}: {str(e)[:90]}")
            done[0] += 1
            _scan["progress"] = f"Scanning {done[0]}/{len(items)}"

        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(work, items))
        if not res:
            raise RuntimeError(errs[0] if errs else "No data returned")
        res.sort(key=lambda r: -r["score"])
        n = len(res)
        bull = sum(1 for r in res if r["score"] >= 4)
        bear = sum(1 for r in res if r["score"] <= -4)
        pct = lambda fn: round(100 * sum(1 for r in res if fn(r)) / n)
        sm = {"n": n, "bull": bull, "bear": bear, "neutral": n - bull - bear,
              "above_vwap": pct(lambda r: r["vside"] > 0), "st_up": pct(lambda r: r["st"] > 0),
              "adv": sum(1 for r in res if r["chg"] > 0), "dec": sum(1 for r in res if r["chg"] < 0)}
        if bull >= 2 * max(bear, 1) and sm["above_vwap"] >= 65:
            sm["read"] = "Breadth leans bullish: supports CE-side index setups. Confirm on the index itself."
        elif bear >= 2 * max(bull, 1) and sm["above_vwap"] <= 35:
            sm["read"] = "Breadth leans bearish: supports PE-side index setups. Confirm on the index itself."
        else:
            sm["read"] = "Mixed breadth: no broad edge from the stocks right now."
        now = dt.datetime.now(IST)
        _scan["data"] = {"rows": res, "summary": sm, "missing": _uni["missing"],
                         "asof": now.strftime("%H:%M:%S"), "session": max(r["session"] for r in res),
                         "last_candle": max(r["t"] for r in res), "failed": errs[:5]}
        _scan["ts"] = time.time()
        if errs:
            _scan["err"] = f"{len(errs)} stocks failed, e.g. {errs[0]}"
    except Exception as e:
        _scan["err"] = str(e)[:200]
        _scan["ts"] = time.time() - SCAN_TTL + 30  # retry in ~30s
    finally:
        _scan["running"] = False


@app.route("/api/scan")
def scan_api():
    if not TOKEN:
        return jsonify({"errors": ["Paste today's Upstox access token in the box at the top and tap Save"]})
    with _scan_lock:
        if not _scan["running"] and time.time() - _scan["ts"] > SCAN_TTL:
            _scan["running"] = True
            _scan["progress"] = "Starting..."
            threading.Thread(target=_scan_worker, daemon=True).start()
        running = _scan["running"]
    out = dict(_scan["data"] or {})
    out["running"] = running
    out["progress"] = _scan["progress"]
    out["errors"] = [_scan["err"]] if _scan["err"] else []
    return jsonify(out)


SCAN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nifty 50 Scanner</title><style>
:root{--bg:#0f1318;--card:#181e26;--tx:#e6e9ee;--mut:#8b95a3;--g:#22c55e;--r:#ef4444;--y:#eab308}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif;padding:12px}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
select,input{background:#0f1318;color:var(--tx);border:1px solid #2b3440;border-radius:6px;padding:6px 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}
.card{background:var(--card);border-radius:10px;padding:12px}.card h3{margin:0 0 8px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.big{font-size:24px;font-weight:700}.mut{color:var(--mut)}.g{color:var(--g)}.r{color:var(--r)}.y{color:var(--y)}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}td,th{padding:6px 6px;text-align:right;white-space:nowrap}th{color:var(--mut);font-weight:600}
tr.rw{cursor:pointer;border-top:1px solid #232c38}tr.rw:hover{background:#1d2530}tr.dt td{background:#141a21;white-space:normal;padding:8px 12px;font-size:12px}
.err{color:var(--y);font-size:12px;margin-bottom:6px}button{background:#3b82f6;color:#fff;border:0;border-radius:6px;padding:7px 12px}
</style></head><body>
<div class="top"><b>Nifty 50 Scanner</b><a href="/" style="color:#60a5fa">Options dashboard</a>
<input id="tok" type="password" placeholder="Today's Upstox token" style="width:170px"><button onclick="saveTok()">Save</button>
<select id="flt"><option value="all">All stocks</option><option value="bull">Bullish only</option><option value="bear">Bearish only</option></select>
<select id="srt"><option value="score_d">Score: high to low</option><option value="score_a">Score: low to high</option>
<option value="chg_d">Change: high to low</option><option value="chg_a">Change: low to high</option><option value="rvol">Volume spike first</option></select>
<span class="mut" id="stamp"></span><a href="/logout" class="mut" style="margin-left:auto">Log out</a></div>
<div class="err" id="err"></div>
<div class="grid" id="sum"></div>
<div class="card" style="margin-top:10px;overflow-x:auto"><table id="tb"></table></div>
<p class="mut" style="font-size:11px">Scores use completed 5-minute candles (7 checks: EMA trend, RSI, VWAP, Supertrend 10/3, Bollinger 20/2, previous day high/low, volume vs same time on earlier days). Tap a row for the reasons. Outside market hours it shows the last session. Decision support only, not advice.</p>
<script>
const $=id=>document.getElementById(id);let D=null,openS=new Set(),timer=null;
function f(n,d=0){return n==null?'-':Number(n).toLocaleString('en-IN',{maximumFractionDigits:d})}
function cls(n){return n>0?'g':n<0?'r':''}
function sg(r){const s=r.score;return s>=6?['STRONG BULL','g']:s>=4?['BULL','g']:s<=-6?['STRONG BEAR','r']:s<=-4?['BEAR','r']:['NEUTRAL','y']}
function render(){
 if(!D||!D.rows)return;const s=D.summary;
 $('sum').innerHTML=`<div class="card"><h3>Stocks scored</h3><div class="big">${s.n}</div><div class="mut">${D.missing&&D.missing.length?'Not found: '+D.missing.join(', '):'All 50 matched'}</div></div>
 <div class="card"><h3>Bull / Neutral / Bear</h3><div class="big"><span class="g">${s.bull}</span> / <span class="y">${s.neutral}</span> / <span class="r">${s.bear}</span></div><div class="mut">Bull = score +4 or more</div></div>
 <div class="card"><h3>Above VWAP</h3><div class="big">${s.above_vwap}%</div><div class="mut">Supertrend up: ${s.st_up}%</div></div>
 <div class="card"><h3>Advancing / declining</h3><div class="big"><span class="g">${s.adv}</span> / <span class="r">${s.dec}</span></div><div class="mut">vs previous close</div></div>
 <div class="card" style="grid-column:1/-1"><h3>Breadth read</h3>${s.read}</div>`;
 let rows=D.rows.slice();const fl=$('flt').value;
 if(fl==='bull')rows=rows.filter(r=>r.score>=4);else if(fl==='bear')rows=rows.filter(r=>r.score<=-4);
 const key={score_d:r=>-r.score,score_a:r=>r.score,chg_d:r=>-r.chg,chg_a:r=>r.chg,rvol:r=>-(r.rvol||0)}[$('srt').value];
 rows.sort((a,b)=>key(a)-key(b));
 let h='<tr><th style="text-align:left">Stock</th><th>LTP</th><th>Chg %</th><th>Score</th><th>Signal</th><th>VWAP</th><th>Supertrend</th><th>BB %B</th><th>Vol</th><th>RSI</th></tr>';
 for(const r of rows){const [t,c]=sg(r);
  h+=`<tr class="rw" data-s="${r.sym}"><td style="text-align:left"><b>${r.sym}</b></td><td>${f(r.ltp,2)}</td><td class="${cls(r.chg)}">${f(r.chg,2)}</td>
  <td><b class="${cls(r.score)}">${r.score>0?'+':''}${r.score}</b></td><td class="${c}">${t}</td>
  <td class="${cls(r.vside)}">${r.vside>0?'above':r.vside<0?'below':'at'} ${f(Math.abs(r.vwap_pct),2)}%</td>
  <td class="${cls(r.st)}">${r.st>0?'Up':'Down'}${r.flip?' (new)':''}</td>
  <td>${f(r.pctb*100)}%${r.squeeze?' SQZ':''}</td><td>${r.rvol==null?'-':f(r.rvol,1)+'x'}</td><td>${f(r.rsi)}</td></tr>`;
  if(openS.has(r.sym))h+=`<tr class="dt"><td colspan="10" style="text-align:left">`+
   r.why.map(w=>`<div><b class="${cls(w[0])}">${w[0]>0?'+':''}${w[0]}</b> ${w[1]}</div>`).join('')+
   `<div class="mut" style="margin-top:6px">VWAP ${f(r.vwap,2)} | EMA9 ${f(r.e9,2)} / EMA21 ${f(r.e21,2)} | Supertrend ${f(r.st_val,2)} | Bollinger ${f(r.bb[0],2)} / ${f(r.bb[1],2)} / ${f(r.bb[2],2)} | Prev day high ${f(r.pdh,2)} low ${f(r.pdl,2)}</div></td></tr>`;
 }
 $('tb').innerHTML=h}
$('tb').addEventListener('click',e=>{const tr=e.target.closest('tr.rw');if(!tr)return;const s=tr.dataset.s;openS.has(s)?openS.delete(s):openS.add(s);render()});
$('flt').addEventListener('change',render);$('srt').addEventListener('change',render);
async function saveTok(){const t=$('tok').value.trim();if(!t)return;
 try{await fetch('/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t})});$('tok').value='';load()}catch(e){$('err').textContent=String(e)}}
async function load(){clearTimeout(timer);
 try{const r=await fetch('/api/scan');if(r.status===401){location='/login';return}
  const d=await r.json();if(d.rows){D=d;render()}
  $('err').textContent=(d.errors||[]).join(' | ');
  $('stamp').textContent=d.running?(d.progress||'Scanning...'):(d.asof?'Updated '+d.asof+' | last candle '+d.last_candle+' | session '+d.session:'');
  timer=setTimeout(load,d.running?3000:(d.rows?30000:10000))
 }catch(e){$('err').textContent=String(e);timer=setTimeout(load,10000)}}
load();
</script></body></html>"""


@app.route("/scanner")
def scanner_page():
    return Response(SCAN_HTML, mimetype="text/html")


@app.route("/")
def home():
    return Response(HTML, mimetype="text/html")


if __name__ == "__main__":
    hosted = bool(os.environ.get("PORT"))
    app.run(host="0.0.0.0" if hosted else "127.0.0.1", port=int(os.environ.get("PORT", 5000)))
