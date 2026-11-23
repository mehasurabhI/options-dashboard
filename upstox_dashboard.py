"""
Upstox Intraday Options Dashboard (read-only decision support, places NO orders)

Deploy: see README.md (hosted on Render, opened from Safari).
Required env vars: DASH_PASSWORD (your login password), SECRET_KEY (random).
Each morning: log in, paste today's Upstox access token in the box at the top.
Upstox access tokens expire daily (~3:30 AM IST).
"""
import os, time, hmac, secrets, datetime as dt
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


def api(path, params=None):
    r = requests.get(
        BASE + path,
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
        "chain": near,
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
<div class="top"><b>Options Dashboard</b>
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
 calc()}
async function saveTok(){const t=$('tok').value.trim();if(!t)return;
 try{await fetch('/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t})});$('tok').value='';load()}catch(e){$('err').textContent=String(e)}}
async function load(){try{const r=await fetch('/api/data?index='+$('idx').value);
 if(r.status===401){location='/login';return}render(await r.json())}catch(e){$('err').textContent=String(e)}}
$('idx').addEventListener('change',load);load();setInterval(load,10000);
</script></body></html>"""


@app.route("/")
def home():
    return Response(HTML, mimetype="text/html")


if __name__ == "__main__":
    hosted = bool(os.environ.get("PORT"))
    app.run(host="0.0.0.0" if hosted else "127.0.0.1", port=int(os.environ.get("PORT", 5000)))
