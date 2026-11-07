"""MNQ Trade Charts - local web app. Run: py app.py  (opens http://127.0.0.1:8765)
Needs only: pip install requests"""
import base64, hashlib, hmac, os, json, re, time, threading, webbrowser, requests
from datetime import datetime, timezone
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CLOUD = bool(APP_PASSWORD)
DB_URL = os.environ.get("DATABASE_URL", "")
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
SECRET = hashlib.sha256(("mnq|" + APP_PASSWORD + "|" + os.environ.get("SESSION_SECRET", "")).encode()).hexdigest()
FAILS = {}
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kv.json")
LOCK = threading.Lock()
LOGIN = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Log in</title>
<body style="margin:0;background:#0b0d11;color:#dde1ea;font:16px system-ui;display:flex;min-height:100vh;align-items:center;justify-content:center">
<form method="post" action="/login" style="background:#141821;border:1px solid #232938;border-radius:10px;padding:28px;display:flex;flex-direction:column;gap:12px;width:280px">
<b style="font-size:20px">MNQ Trade Charts</b><label for="pw" style="font-size:13px;color:#9aa3b8">Password</label>
<input id="pw" name="pw" type="password" autofocus style="padding:10px;border-radius:6px;border:1px solid #2a3142;background:#0f1218;color:#dde1ea;font-size:16px">
<button style="padding:10px;border:0;border-radius:6px;background:#2456c8;color:#fff;font-size:16px;cursor:pointer">Log in</button>
<div style="color:#ef5350;font-size:13px">__ERR__</div></form>'''


def session_token():
    return hmac.new(SECRET.encode(), b"ok", hashlib.sha256).hexdigest()


def pg(sql, args=()):
    import psycopg2
    c = psycopg2.connect(DB_URL, connect_timeout=20)
    try:
        with c, c.cursor() as cur:
            cur.execute("create table if not exists kv(k text primary key, v text not null)")
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


def _file_kv():
    try:
        return json.load(open(DATA, encoding="utf-8"))
    except Exception:
        return {}


def kv_get(k):
    if DB_URL:
        r = pg("select v from kv where k=%s", (k,))
        return json.loads(r[0][0]) if r else None
    return _file_kv().get(k)


def kv_set(k, v):
    if DB_URL:
        pg("insert into kv(k,v) values(%s,%s) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))
        return
    with LOCK:
        d = _file_kv()
        d[k] = v
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        json.dump(d, open(DATA, "w", encoding="utf-8"))


def kv_del(k):
    if DB_URL:
        pg("delete from kv where k=%s", (k,))
        return
    with LOCK:
        d = _file_kv()
        d.pop(k, None)
        json.dump(d, open(DATA, "w", encoding="utf-8"))


def kv_keys(prefix):
    if DB_URL:
        return [r[0] for r in pg("select k from kv where k like %s order by k", (prefix + "%",))]
    return sorted(k for k in _file_kv() if k.startswith(prefix))


BASE = "https://api.topstepx.com"
S = requests.Session()
AUTH, CONTRACTS = {}, []


def login(u, k):
    c = AUTH.get(u)
    if c and c[2] == k and time.time() - c[1] < 1200:
        return c[0]
    d = S.post(BASE + "/api/Auth/loginKey", json={"userName": u, "apiKey": k}, timeout=20).json()
    if not d.get("success"):
        raise Exception("Login failed: " + str(d.get("errorMessage") or d))
    AUTH[u] = (d["token"], time.time(), k)
    return d["token"]


def api(path, tok, body):
    r = S.post(BASE + path, json=body, timeout=60,
               headers={"Authorization": "Bearer " + tok, "accept": "text/plain"})
    r.raise_for_status()
    return r.json()


def candidates(tok, symbol, when):
    """Contract ids to try, best guess first. Expired contracts are not in the
    search list, so also build their ids directly (CON.F.US.MNQ.U26)."""
    global CONTRACTS
    if not CONTRACTS:
        d = api("/api/Contract/search", tok, {"live": False, "searchText": "MNQ"})
        CONTRACTS = [c for c in d.get("contracts", []) if c.get("symbolId") == "F.US.MNQ"]
        CONTRACTS.sort(key=lambda c: not c.get("activeContract"))
    out = []
    def add(cid, name):
        if cid and cid not in [x[0] for x in out]:
            out.append((cid, name))
    m = re.search(r"([HMUZ])(\d{1,2})$", (symbol or "").upper())
    if m:
        yy = m.group(2) if len(m.group(2)) == 2 else str(when.year)[2:3] + m.group(2)
        add(f"CON.F.US.MNQ.{m.group(1)}{yy}", "MNQ" + m.group(1) + m.group(2))
    mo, y = ((when.month - 1) // 3 + 1) * 3, when.year
    if when.month == mo and when.day > 14:
        mo += 3
    if mo > 12:
        mo, y = 3, y + 1
    code = "HMUZ"[mo // 3 - 1]
    add(f"CON.F.US.MNQ.{code}{str(y)[2:]}", f"MNQ{code}{str(y)[-1]}")
    for c in CONTRACTS:
        add(c.get("id"), c.get("name"))
    return out


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades snapshot")


def clean(n):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(n)).strip(" .")[:80] or "trades"


def save_png(b):
    folder = os.path.join(ROOT, clean(b["folder"]))
    os.makedirs(folder, exist_ok=True)
    fn = clean(b["name"])
    if not fn.lower().endswith((".png", ".csv")):
        fn += ".png"
    path = os.path.join(folder, fn)
    existed = os.path.exists(path)
    with open(path, "wb") as f:  # same name = old picture is replaced
        f.write(base64.b64decode(b["data"].split(",", 1)[1]))
    return {"path": path, "dir": folder, "replaced": existed}


def notes_file(b):
    return os.path.join(ROOT, clean(b["folder"]), "notes.json")


def notes_get(b):
    if DB_URL:
        return kv_get("notes:" + clean(b["folder"])) or {}
    p = notes_file(b)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def notes_set(b):
    if DB_URL:
        d, n = notes_get(b), b["note"]
        if n.get("text") or n.get("tags"):
            d[b["key"]] = n
        else:
            d.pop(b["key"], None)
        kv_set("notes:" + clean(b["folder"]), d)
        return {"ok": True}
    p = notes_file(b)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d, n = notes_get(b), b["note"]
    if n.get("text") or n.get("tags"):
        d[b["key"]] = n
    else:
        d.pop(b["key"], None)
    json.dump(d, open(p, "w", encoding="utf-8"), indent=1)
    return {"ok": True}


def kv_items(prefix):
    if DB_URL:
        return {k: json.loads(v) for k, v in pg("select k,v from kv where k like %s", (prefix + "%",))}
    return {k: v for k, v in _file_kv().items() if k.startswith(prefix)}


AI_SYSTEM = (
    "You are a trading-performance coach reviewing ONE day of MNQ futures trades for the trader who made them. "
    "Use only the data provided. All statistics and discipline checks were computed for you: quote them exactly and do not recompute them differently. "
    "Be specific, honest and constructive, not flattering. Refer to individual trades by number and time (New York time) when useful. "
    "Use these plain-text headings: What went well / What hurt most / Behaviour patterns / Rules for tomorrow (at most 3, about process and risk, never market calls). "
    "Do not give trade signals, price predictions or financial advice. Mention if the sample is too small to conclude anything. Maximum 350 words. No markdown tables."
)


def ai_review(b):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"error": "AI review is not set up yet. Add ANTHROPIC_API_KEY in your Render Environment settings, then redeploy."}
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    r = S.post("https://api.anthropic.com/v1/messages", timeout=90,
               headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
               json={"model": model, "max_tokens": 1500, "system": AI_SYSTEM,
                     "messages": [{"role": "user", "content": "Here is the day's data as JSON:\n" + json.dumps(b["data"])}]})
    d = r.json()
    if r.status_code != 200:
        return {"error": "AI request failed: " + str((d.get("error") or {}).get("message", d))[:300]}
    return {"text": "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text").strip(), "model": model}


def rev_api(path, b):
    pre = "review:" + clean(b.get("csv", "")) + ":"
    if path == "/api/review_all":
        return {"reviews": {k[len(pre):]: v for k, v in kv_items(pre).items() if k.startswith(pre)}}
    if path == "/api/review_save":
        kv_set(pre + clean(b["day"]), b["review"])
        return {"ok": True}
    if path == "/api/review_del":
        kv_del(pre + clean(b["day"]))
        return {"ok": True}
    if path == "/api/ai_review":
        return ai_review(b)
    return {"error": "unknown"}


def log_api(path, b):
    if path == "/api/log_save":
        kv_set("log:" + clean(b["name"]), b["csv"])
        return {"ok": True}
    if path == "/api/log_list":
        return {"names": [k[4:] for k in kv_keys("log:")]}
    if path == "/api/log_del":
        kv_del("log:" + clean(b["name"]))
        if b.get("notes"):
            if DB_URL:
                kv_del("notes:" + clean(b["name"]))
            elif os.path.exists(notes_file({"folder": b["name"]})):
                os.remove(notes_file({"folder": b["name"]}))
        return {"ok": True}
    if path == "/api/log_get":
        return {"csv": kv_get("log:" + clean(b["name"]))}
    return {"error": "unknown"}


def open_dir(b):
    d = os.path.join(ROOT, clean(b.get("folder", "")))
    if not os.path.isdir(d):
        os.makedirs(ROOT, exist_ok=True)
        d = ROOT
    try:
        os.startfile(d)
    except AttributeError:
        webbrowser.open(d)
    return {"ok": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if not CLOUD:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "s" and hmac.compare_digest(v, session_token()):
                return True
        return False

    def login(self):
        ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
        n, t0 = FAILS.get(ip, (0, time.time()))
        if time.time() - t0 > 900:
            n, t0 = 0, time.time()
        if n >= 8:
            return self.send(429, b"Too many attempts. Try again in 15 minutes.", "text/plain")
        pw = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()).get("pw", [""])[0]
        if hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            FAILS.pop(ip, None)
            sec = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self.send(303, b"", "text/plain", {"Location": "/", "Set-Cookie":
                             f"s={session_token()}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{sec}"})
        FAILS[ip] = (n + 1, t0)
        time.sleep(1.5)
        return self.send(401, LOGIN.replace("__ERR__", "Wrong password").encode(), "text/html; charset=utf-8")

    def do_GET(self):
        if self.path == "/logout":
            return self.send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": "s=; Path=/; Max-Age=0"})
        if not self.authed():
            return self.send(200, LOGIN.replace("__ERR__", "").encode(), "text/html; charset=utf-8")
        page = PAGE.replace("__CLOUD__", "true" if CLOUD else "false")
        self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path == "/login":
            return self.login()
        if not self.authed():
            return self.send(401, b'{"error":"Not logged in - reload the page"}', "application/json")
        try:
            b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/save":
                return self.send(200, json.dumps(save_png(b)).encode(), "application/json")
            if self.path.startswith("/api/review_") or self.path == "/api/ai_review":
                return self.send(200, json.dumps(rev_api(self.path, b)).encode(), "application/json")
            if self.path.startswith("/api/log_"):
                return self.send(200, json.dumps(log_api(self.path, b)).encode(), "application/json")
            if self.path == "/api/notes_get":
                return self.send(200, json.dumps(notes_get(b)).encode(), "application/json")
            if self.path == "/api/notes_set":
                return self.send(200, json.dumps(notes_set(b)).encode(), "application/json")
            if self.path == "/api/open":
                return self.send(200, json.dumps(open_dir(b)).encode(), "application/json")
            tok = login(os.environ.get("TOPSTEPX_USERNAME") or b.get("username", ""), os.environ.get("TOPSTEPX_API_KEY") or b.get("apiKey", ""))
            st = datetime.fromtimestamp(b["start"], timezone.utc)
            en = datetime.fromtimestamp(b["end"], timezone.utc)
            tried, d = [], {}
            for cid, cname in candidates(tok, b.get("symbol"), st):
                tried.append(cname or cid)
                d = api("/api/History/retrieveBars", tok, {
                    "contractId": cid, "live": False, "startTime": st.isoformat(),
                    "endTime": en.isoformat(), "unit": 2, "unitNumber": 1,
                    "limit": 20000, "includePartialBar": False})
                if d.get("bars"):
                    c = {"name": cname or cid}
                    break
            else:
                raise Exception("No data from any contract tried: " + ", ".join(tried))
            bars = [{"time": int(datetime.fromisoformat(x["t"].replace("Z", "+00:00")).timestamp()),
                     "open": x["o"], "high": x["h"], "low": x["l"], "close": x["c"]}
                    for x in (d.get("bars") or [])]
            bars.sort(key=lambda x: x["time"])
            out, code = {"contract": c.get("name"), "bars": bars}, 200
        except Exception as e:
            out, code = {"error": str(e)}, 400
        self.send(code, json.dumps(out).encode(), "application/json")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>MNQ Trade Charts v20</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
body{margin:0;font:14px system-ui,sans-serif;background:#0b0d11;color:#dde1ea}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:end;padding:12px 16px;background:#141821;border-bottom:1px solid #232938}
label{display:flex;flex-direction:column;font-size:11px;color:#8b93a7;gap:3px}
input,select,button{background:#0f1218;color:#dde1ea;border:1px solid #2a3142;border-radius:6px;padding:7px 9px;font:inherit}
button{cursor:pointer}button:hover{border-color:#4c8dff}button.on{background:#2456c8;border-color:#2456c8}
main{display:flex;height:calc(100vh - 78px)}
#left{width:300px;display:flex;flex-direction:column;border-right:1px solid #232938}#list{flex:1;overflow:auto}
.it{padding:9px 12px;border-bottom:1px solid #181d28;cursor:pointer;font-size:13px}
.it:hover{background:#151a25}.it.sel{background:#1b2740}
.w{color:#26a69a}.l{color:#ef5350}
#right{flex:1;padding:12px 16px;overflow:auto}
#bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
#list .dh{padding:8px 12px;background:#10151f;color:#aab2c5;font-size:12px;border-bottom:1px solid #232938;position:sticky;top:0}
#chart{overflow:hidden;width:100%}
details.rv{background:#141821;border:1px solid #232938;border-radius:8px;margin-bottom:10px}details.rv>summary{cursor:pointer;padding:12px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}details.rv .rb{padding:0 14px 14px}.ck{display:flex;gap:10px;padding:7px 0;border-top:1px solid #1c2230;font-size:13px}.ck b{width:60px}
#scbox svg{display:block;border-radius:6px;max-width:100%;height:auto}
.cal{display:grid;grid-template-columns:repeat(7,minmax(92px,1fr)) 110px;gap:4px;min-width:760px}.ch{color:#9aa3b8;font-size:12px;text-align:center;padding:4px}.cd{min-height:70px;border-radius:6px;padding:6px 8px;font-size:11px;background:#10141c;border:1px solid #1c2230}.cd.has{cursor:pointer}.cd.has:hover{outline:1px solid #4c8dff}.cd .dn{color:#c3cada;font-size:11px}.cd b{display:block;font-size:14px;margin:2px 0}
table.tb{width:100%;border-collapse:collapse;font-size:13px}.tb th{color:#9aa3b8;font-weight:normal;text-align:right;padding:8px 10px;border-bottom:1px solid #1c2230;white-space:nowrap}.tb td{padding:8px 10px;text-align:right;border-top:1px solid #1c2230;white-space:nowrap}.tb th:first-child,.tb td:first-child{text-align:left}
#toast{display:none;position:fixed;right:16px;bottom:16px;max-width:430px;background:#1b2740;border:1px solid #2456c8;color:#dde1ea;padding:12px 14px;border-radius:8px;font-size:13px;z-index:50}
.lr{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-bottom:1px solid #1c2230;font-size:13px;color:#dde1ea}.ln{cursor:pointer;flex:1}.ln:hover{color:#4c8dff}.lx{cursor:pointer;color:#9aa3b8;padding:2px 8px;border-radius:4px}.lx:hover{background:#ef5350;color:#fff}
@media(max-width:800px){main{flex-direction:column;height:auto}#left{width:auto;max-height:45vh}#right{padding:10px}#det{grid-template-columns:1fr}.cg{grid-template-columns:repeat(2,1fr)}header{padding:8px}#ver{display:none}.sr span:first-child{width:90px!important}}
.mu{color:#9aa3b8;font-size:12px}.chip{padding:4px 10px;border:1px solid #2a3142;border-radius:14px;font-size:12px;cursor:pointer;background:#0f1218}.chip.on{background:#2456c8;border-color:#2456c8}
#flt{padding:10px;border-bottom:1px solid #232938;display:flex;flex-direction:column;gap:8px}#flt .fg{display:grid;grid-template-columns:1fr 1fr;gap:6px}#flt select,#flt button{font-size:12px;padding:5px}
.c2{background:#141821;border:1px solid #232938;border-radius:8px;padding:12px 14px}.cg{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}.cg .c2{display:flex;flex-direction:column;gap:4px}
.sr{display:flex;gap:12px;align-items:center;padding:9px 12px;border-top:1px solid #1c2230;cursor:pointer}
#note{margin-top:12px;padding:12px 14px;background:#121620;border:1px solid #232938;border-radius:8px;display:flex;flex-direction:column;gap:8px}#note textarea{height:70px;resize:vertical}#tg{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=color]{padding:2px;width:46px;height:34px;cursor:pointer}
#ver{margin-left:auto;align-self:center;color:#5d667a;font-size:12px}
#det{display:grid;grid-template-columns:repeat(3,1fr);gap:6px 18px;margin-top:12px;padding:12px 14px;background:#121620;border:1px solid #232938;border-radius:8px}#det div{font-size:13px}#det span{color:#8b93a7;margin-right:6px}#det span:after{content:':'}
#st{margin:8px 0;color:#8b93a7}#st.err{color:#ef5350}
</style></head><body>
<header>
<label>TopstepX username<input id="u" autocomplete="off"></label>
<label>API key<input id="k" type="password" autocomplete="off"></label>
<label>CSV times without offset are in<select id="logtz"><option>America/New_York</option><option>America/Chicago</option><option>UTC</option><option>Asia/Kolkata</option></select></label>
<label>Show chart time in<select id="disptz"><option>Asia/Kolkata</option><option>America/New_York</option><option>UTC</option></select></label>
<label>Candle colours<select id="cpre"></select></label>
<label>Up<input id="cup" type="color"></label>
<label>Down<input id="cdn" type="color"></label>
<label>PNG size<select id="psz"><option value="1600x900">Large 1600x900</option><option value="1920x1080" selected>Extra large 1920x1080</option><option value="2560x1440">Huge 2560x1440</option></select></label>
<div style="display:flex;flex-direction:column;font-size:11px;color:#8b93a7;gap:3px;position:relative">Saved online<button id="slogb" type="button" style="min-width:220px;text-align:left">Choose a saved log &#9662;</button><div id="slogm" style="display:none;position:absolute;z-index:30;top:100%;left:0;min-width:320px;max-height:320px;overflow:auto;background:#141821;border:1px solid #2a3142;border-radius:6px"></div></div>
<label>Trade log CSV<input id="f" type="file" accept=".csv"></label>
<a id="lo" href="/logout" style="display:none;color:#9aa3b8;font-size:12px;align-self:center;margin-left:auto">Log out</a><div id="ver">v20</div></header>
<main><div id="left"><div id="flt"></div><div id="list"><div class="it">Enter login, then choose your CSV.</div></div></div>
<div id="right"><div id="bar"></div><div id="st"></div><div id="chart"></div><div id="det"></div><div id="note"></div><div id="sum" style="display:none"></div><div id="jr" style="display:none"></div></div></main><div id="toast"></div>
<script>
const CLOUD=__CLOUD__;
const $=id=>document.getElementById(id);
const TFS={'1m':1,'5m':5,'15m':15,'30m':30,'1h':60},PAD={'1m':[90,30],'5m':[80,25],'15m':[70,20],'30m':[60,15],'1h':[50,12]};
let notes={},pn=true,sumOn=false,flt={res:'all',side:'all',day:'all',time:'all',size:'all',sort:'t'},cs,csvName='trades',nS=0,nR=0,lastDir='',lastFile='',days={},tznote='',trades=[],sel=-1,tf='5m',chart,cache={};
const st=(m,e)=>{$('st').textContent=m;$('st').className=e?'err':''};

function csv(t){t=t.replace(/^\uFEFF/,'');const fl=t.split(/\r?\n/)[0]||'',dl=[',',';','\t'].map(d=>[fl.split(d).length,d]).sort((a,b)=>b[0]-a[0])[0][1];const o=[];let r=[],f='',q=false;
 for(let i=0;i<t.length;i++){const c=t[i];
  if(q){if(c=='"'){if(t[i+1]=='"'){f+='"';i++}else q=false}else f+=c}
  else if(c=='"')q=true;else if(c==dl){r.push(f);f=''}
  else if(c=='\n'||c=='\r'){if(c=='\r'&&t[i+1]=='\n')i++;r.push(f);f='';o.push(r);r=[]}
  else f+=c}
 if(f||r.length){r.push(f);o.push(r)}return o.filter(x=>x.length>1)}

function off(tz,ms){const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:tz,hourCycle:'h23',year:'numeric',month:'numeric',day:'numeric',hour:'numeric',minute:'numeric',second:'numeric'}).formatToParts(new Date(ms)).map(x=>[x.type,x.value]));
 return (Date.UTC(p.year,p.month-1,p.day,p.hour,p.minute,p.second)-Math.floor(ms/1000)*1000)/1000}

function parseTs(s,tz){s=s.trim();let Y,M,D,h,mi,sec,ap,z,m;
 if(m=s.match(/^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$/)){[,Y,M,D,h,mi,sec,z]=m}
 else if(m=s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4}),?\s+(\d{1,2}):(\d\d)(?::(\d\d))?\s*(AM|PM)?\s*(Z|[+-]\d\d:?\d\d)?$/i)){[,M,D,Y,h,mi,sec,ap,z]=m}
 else return NaN;
 h=+h;if(ap){ap=ap.toUpperCase();if(ap=='PM'&&h<12)h+=12;if(ap=='AM'&&h==12)h=0}
 const g=Date.UTC(+Y,M-1,+D,h,+mi,+(sec||0))/1000;
 if(z){if(z=='Z')return g;const sg=z[0]=='-'?-1:1,d=z.slice(1).replace(':','');return g-sg*(+d.slice(0,2)*3600+ +d.slice(2)*60)}
 let o=off(tz,g*1000);o=off(tz,(g-o)*1000);return g-o}

function closedET(u){const o=off('America/New_York',u*1000),d=new Date((u+o)*1000),wd=d.getUTCDay(),h=d.getUTCHours();
 return wd==6||(wd==5&&h>=17)||(wd==0&&h<18)||(h==17&&wd>=1&&wd<=4)}
function dayKey(u){const o=off('America/New_York',u*1000);let d=new Date((u+o)*1000);if(d.getUTCHours()>=18)d=new Date(d.getTime()+864e5);return d.toISOString().slice(0,10)}
function load(text){const r=csv(text);if(!r.length)throw new Error('This file looks empty or has no header row. Export the trade log again as a CSV and try once more.');const H=r[0].map(x=>x.trim().toLowerCase()),ix=n=>H.indexOf(n),g=(w,n)=>ix(n)>=0?w[ix(n)]:'',nu=v=>v===''?NaN:+v;let mk;
 if(['enteredat','exitedat','entryprice','exitprice','type'].every(n=>ix(n)>=0))
  mk=tz=>r.slice(1).map(w=>({long:/long|buy/i.test(g(w,'type')),et:parseTs(g(w,'enteredat'),tz),xt:parseTs(g(w,'exitedat'),tz),ep:+g(w,'entryprice'),xp:+g(w,'exitprice'),pnl:nu(g(w,'pnl')),day:(/^\d{4}-\d\d-\d\d/.exec(g(w,'tradeday'))||[''])[0],sym:g(w,'contractname').trim(),size:g(w,'size'),fees:nu(g(w,'fees')),comm:nu(g(w,'commissions'))}));
 else if(['buyprice','sellprice','boughttimestamp','soldtimestamp'].every(n=>ix(n)>=0))
  mk=tz=>r.slice(1).map(w=>{const bp=+g(w,'buyprice'),sp=+g(w,'sellprice'),bt=parseTs(g(w,'boughttimestamp'),tz),sd=parseTs(g(w,'soldtimestamp'),tz),L=bt<=sd;
   return {long:L,et:L?bt:sd,xt:L?sd:bt,ep:L?bp:sp,xp:L?sp:bp,pnl:nu(g(w,'pnl')),sym:g(w,'symbol').trim(),size:g(w,'qty'),fees:NaN,comm:NaN}});
 else throw new Error('Unrecognised CSV format. Found columns: '+r[0].join(', '));
 let best={tz:$('logtz').value,n:1e9,a:[]};
 for(const tz of [$('logtz').value,'Asia/Kolkata','America/New_York','America/Chicago','UTC']){
  const a=mk(tz).filter(t=>![t.et,t.xt,t.ep,t.xp].some(isNaN)),n=a.filter(t=>closedET(t.et)).length;if(n<best.n)best={tz,n,a}}
 $('logtz').value=best.tz;trades=best.a.sort((a,b)=>a.et-b.et);cache={};days={};
 for(const t of trades){t.day=t.day||dayKey(t.et);const D=days[t.day]??={n:0,sum:0};D.n++;D.sum+=net(t);t.run=D.sum}
 tznote='CSV timezone: '+best.tz+(best.n?` (WARNING: ${best.n} trades fall in market-closed hours)`:'')+(r.length-1>best.a.length?` | ${r.length-1-best.a.length} rows skipped (dates or prices could not be read)`:'');return best}

const fmt=s=>new Intl.DateTimeFormat('en-GB',{timeZone:$('disptz').value,day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(s*1000));
const win=t=>(t.xp-t.ep)*(t.long?1:-1)>0;
const usd=v=>isNaN(v)?'-':(v<0?'-':v>0?'+':'')+'$'+Math.abs(v).toFixed(2);
const gross=t=>(t.xp-t.ep)*(t.long?1:-1)*2*(+t.size||1);
const net=t=>isNaN(t.pnl)?gross(t):t.pnl;

const dlab=d=>new Intl.DateTimeFormat('en-GB',{timeZone:'UTC',weekday:'short',day:'2-digit',month:'short',year:'numeric'}).format(new Date(d+'T00:00:00Z'));
function renderList(){const v=view(),dv={};v.forEach(i=>{const t=trades[i];(dv[t.day]??={n:0,s:0});dv[t.day].n++;dv[t.day].s+=net(t)});let last='';
 $('list').innerHTML=v.map(i=>{const t=trades[i];let h='';if(flt.sort=='t'&&t.day!=last){last=t.day;const D=dv[t.day];h=`<div class="dh">${dlab(t.day)} | ${D.n} trade${D.n>1?'s':''} | Day net <b class="${D.s>=0?'w':'l'}">${usd(D.s)}</b></div>`}
  return h+`<div class="it ${i==sel?'sel':''}" onclick="show(${i})">#${i+1} ${t.long?'LONG':'SHORT'} <span class="${net(t)>=0?'w':'l'}">${win(t)?'WIN':'LOSS'} ${usd(net(t))}</span>${hasN(t)?' <small>[note]</small>':''}<br><small>${fmt(t.et)}</small></div>`}).join('')||'<div class="it">No trades match.</div>';
 renderFlt();
 $('bar').innerHTML='<button onclick="showSummary()">Summary</button><button onclick="showJournal()">Daily log</button>'+(flt.day!='all'?'<button onclick="backToSummary()" style="border-color:#4c8dff">&larr; Back to all days</button><span class="mu" style="align-self:center">Showing only '+dlab(flt.day)+'</span>':'')+Object.keys(TFS).map(k=>`<button class="${k==tf?'on':''}" onclick="show(sel,'${k}')">${k}</button>`).join('')+'<button onclick="dl1()">Save this PNG</button><button onclick="dl5(sel)">Save all 5 timeframes</button><button onclick="dlAll()">Save ALL trades</button>'+(CLOUD?'':'<button onclick="openF()">Open snapshot folder</button>')}

async function bars(t){const k=t.et+'|'+t.xt;if(cache[k])return cache[k];
 const r=await fetch('/api/bars',{method:'POST',body:JSON.stringify({username:$('u').value.trim(),apiKey:$('k').value.trim(),symbol:t.sym,start:t.et-6*86400,end:t.xt+86400})});
 const d=await r.json();if(d.error)throw new Error(d.error);if(!d.bars.length)throw new Error('No data returned for this trade (contract '+d.contract+')');return cache[k]=d}

function agg(b,m){const s=m*60,o=[],dz=$('disptz').value,oc={};
 for(const x of b){const u=Math.floor(x.time/s)*s,L=o[o.length-1];
  if(L&&L.u==u){L.high=Math.max(L.high,x.high);L.low=Math.min(L.low,x.low);L.close=x.close}
  else o.push({u,open:x.open,high:x.high,low:x.low,close:x.close})}
 for(const x of o){const h=Math.floor(x.u/3600);oc[h]??=off(dz,h*3600000);x.time=x.u+oc[h]}return o}

const chartW=()=>Math.max(600,$('right').clientWidth-34);
const chartH=()=>innerWidth<800?380:Math.max(700,innerHeight-230);
new ResizeObserver(()=>{if(chart){$('chart').style.height=chartH()+'px';chart.resize(chartW(),chartH(),true)}}).observe($('right'));
const fmtF=()=>new Intl.DateTimeFormat('en-GB',{timeZone:$('disptz').value,day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function rows(t,i){const f=x=>fmtF().format(new Date(x*1000)),p=(t.xp-t.ep)*(t.long?1:-1),du=Math.round(t.xt-t.et),n=v=>isNaN(v)?'-':v;
 return [['Trade','#'+(i+1)+' of '+trades.length],['Side',t.long?'LONG':'SHORT'],['Result',win(t)?'WIN':'LOSS'],['Contract',(t.sym||'-')+' x '+(t.size||1)],['Points',(p>0?'+':'')+p.toFixed(2)],['Gross $ (pts x $2)',usd(gross(t))],
 ['Entry',t.ep+' @ '+f(t.et)],['Exit',t.xp+' @ '+f(t.xt)],['Net PnL',usd(net(t))],['Fees / Comm',n(t.fees)+' / '+n(t.comm)],['Duration',Math.floor(du/60)+'m '+du%60+'s'],['Day running',usd(t.run)],['Day end net ('+t.day+')',usd(days[t.day].sum)+' | '+days[t.day].n+' trades'],['Time zone',$('disptz').value]]}
function draw(t,d){const c=agg(d.bars,TFS[tf]);
 if(t.et<c[0].u||t.xt>c[c.length-1].u+TFS[tf]*60)throw new Error('Trade time ('+fmt(t.et)+' '+$('disptz').value+') is outside the data returned - the CSV timezone may be wrong. '+tznote);
 if(chart)chart.remove();
 $('chart').style.height=chartH()+'px';chart=LightweightCharts.createChart($('chart'),{width:chartW(),height:chartH(),layout:{background:{color:'#0f1115'},textColor:'#cfd3dc',fontSize:13},grid:{vertLines:{color:'#1c2029'},horzLines:{color:'#1c2029'}},timeScale:{timeVisible:true,secondsVisible:false}});
 const se=cs=chart.addCandlestickSeries({upColor:$('cup').value,downColor:$('cdn').value,wickUpColor:$('cup').value,wickDownColor:$('cdn').value,borderUpColor:$('cup').value,borderDownColor:$('cdn').value,borderVisible:false});se.setData(c);
 const bar=u=>c.findLast(x=>x.u<=u)||c[0],eb=bar(t.et),xb=bar(t.xt);
 se.setMarkers([{time:eb.time,position:t.long?'belowBar':'aboveBar',color:'#2196f3',shape:t.long?'arrowUp':'arrowDown',text:'Entry '+t.ep},{time:xb.time,position:t.long?'aboveBar':'belowBar',color:'#ff9800',shape:t.long?'arrowDown':'arrowUp',text:'Exit '+t.xp}].sort((a,b)=>a.time-b.time));
 se.createPriceLine({price:t.ep,color:'#2196f3',lineStyle:2,lineWidth:1,title:'Entry'});
 se.createPriceLine({price:t.xp,color:'#ff9800',lineStyle:2,lineWidth:1,title:'Exit'});
 if(eb.time!=xb.time){const ln=chart.addLineSeries({color:win(t)?'#26a69a':'#ef5350',lineWidth:2,lastValueVisible:false,priceLineVisible:false});ln.setData([{time:eb.time,value:t.ep},{time:xb.time,value:t.xp}])}
 chart.timeScale().setVisibleLogicalRange({from:c.indexOf(eb)-PAD[tf][0],to:c.indexOf(xb)+PAD[tf][1]});
 $('det').innerHTML=rows(t,sel).map(x=>`<div><span>${x[0]}</span>${x[0]=='Net PnL'||x[0]=='Result'?`<b class="${net(t)>=0?'w':'l'}">${x[1]}</b>`:x[0]=='Day running'?`<b class="${t.run>=0?'w':'l'}">${x[1]}</b>`:x[0].startsWith('Day end')?`<b class="${days[t.day].sum>=0?'w':'l'}">${x[1]}</b>`:x[1]}</div>`).join('');
 st(`${d.contract} | ${t.long?'LONG':'SHORT'} ${t.ep} → ${t.xp} | ${tf} | ${tznote}`)}

async function show(i,k){if(i<0)return;syncNote();sumOn=false;$('sum').style.display='none';$('jr').style.display='none';$('chart').style.display='block';$('det').style.display='grid';$('note').style.display='flex';sel=i;if(k)tf=k;renderList();st('Loading data…');if(chart){chart.remove();chart=null;cs=null}$('det').innerHTML='';
 try{draw(trades[i],await bars(trades[i]));renderNote(trades[i])}catch(e){st(e.message,1)}}

function shot(){const t=trades[sel],[W,Hh]=$('psz').value.split('x').map(Number),ts=chart.timeScale(),rg=ts.getVisibleLogicalRange(),ow=chartW(),oh=chartH();
 chart.applyOptions({layout:{fontSize:17}});chart.resize(W,Hh,true);if(rg)ts.setVisibleLogicalRange(rg);
 const cv=chart.takeScreenshot();
 chart.resize(ow,oh,true);chart.applyOptions({layout:{fontSize:13}});if(rg)ts.setVisibleLogicalRange(rg);
 const k=cv.width/1280,R=rows(t,sel),o=document.createElement('canvas'),hh=46*k,fh=152*k,n=notes[nk(t)],hn=pn&&n&&(n.text||n.tags.length)?64*k:0;
 o.width=cv.width;o.height=cv.height+hh+fh+hn;
 const g=o.getContext('2d');g.fillStyle='#0f1115';g.fillRect(0,0,o.width,o.height);g.fillStyle='#fff';g.font=`bold ${20*k}px sans-serif`;
 g.fillText(`MNQ ${tf} | ${t.long?'LONG':'SHORT'} ${t.ep} -> ${t.xp} (${win(t)?'WIN':'LOSS'} ${usd(net(t))})`,14*k,31*k);
 g.drawImage(cv,0,hh);g.font=`${15*k}px sans-serif`;
 R.forEach((x,i)=>{const cx=14*k+(i%3)*(o.width/3),cy=hh+cv.height+28*k+Math.floor(i/3)*28*k;g.fillStyle='#8b93a7';g.fillText(x[0]+': ',cx,cy);g.fillStyle=(x[0]=='Result'||x[0]=='Net PnL')?(net(t)>=0?'#26a69a':'#ef5350'):x[0]=='Day running'?(t.run>=0?'#26a69a':'#ef5350'):x[0].startsWith('Day end')?(days[t.day].sum>=0?'#26a69a':'#ef5350'):'#fff';g.fillText(x[1],cx+g.measureText(x[0]+': ').width,cy)});
 if(hn){const y=hh+cv.height+fh;g.font=`${15*k}px sans-serif`;g.fillStyle='#9aa3b8';g.fillText('Tags: '+(n.tags.join(', ')||'-'),14*k,y+18*k);g.fillStyle='#fff';
  let tx='Note: '+n.text.replace(/\s+/g,' ');const mw=o.width-28*k;if(g.measureText(tx).width>mw){while(g.measureText(tx+'...').width>mw&&tx.length>8)tx=tx.slice(0,-1);tx+='...'}g.fillText(tx,14*k,y+44*k)}
 return o}
async function save(name,data){const u=data||shot().toDataURL('image/png');if(CLOUD){const a=document.createElement('a');a.href=u;a.download=csvName+'_'+name;a.click();nS++;lastDir='your browser Downloads folder';lastFile=a.download;return}const r=await fetch('/api/save',{method:'POST',body:JSON.stringify({folder:csvName,name,data:u})}),d=await r.json();
 if(d.error)throw new Error('Save failed: '+d.error);nS++;if(d.replaced)nR++;lastDir=d.dir;lastFile=d.path}
async function openF(){await fetch('/api/open',{method:'POST',body:JSON.stringify({folder:csvName})})}
const nm=i=>`trade_${String(i+1).padStart(3,'0')}_${tf}.png`,sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function dl1(){if(!(chart&&sel>=0))return;nS=nR=0;try{await save(nm(sel));st(`Saved${nR?' (replaced the old file)':''}: ${lastFile}`)}catch(e){st(e.message,1)}}
async function dl5(i,quiet){if(i<0)return;if(!quiet)nS=nR=0;const keep=tf;
 for(const k of Object.keys(TFS)){tf=k;await show(i);if(chart&&!$('st').className){try{await save(nm(i))}catch(e){st(e.message,1);return}}}
 tf=keep;await show(i);if(!quiet)st(`Saved ${nS} images (${nR} replaced old files) in: ${lastDir}`)}
async function dlAll(){const v=view();if(!v.length||!confirm('Save 5 PNGs for each of the '+v.length+' trades shown'+(CLOUD?' (they download to your browser)?':'? Pictures with the same name in the snapshot folder will be replaced.')))return;nS=nR=0;for(const i of v)await dl5(i,true);st(`Saved ${nS} images`+(nR?` (${nR} replaced old files)`:'')+` in: ${lastDir}`)}

const TB=['NY open','Midday','NY afternoon','Overnight / Asia / London'],TAGS=['Early entry','Late entry','FOMO','Good setup','Broke my rules','Good exit'],esc=x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;'),nk=t=>t.et+'|'+t.xt,hasN=t=>{const n=notes[nk(t)];return n&&(n.text||n.tags.length)};
function bucket(t){const o=off('America/New_York',t.et*1000),d=new Date((t.et+o)*1000),m=d.getUTCHours()*60+d.getUTCMinutes();return m>=570&&m<660?TB[0]:m>=660&&m<840?TB[1]:m>=840&&m<1020?TB[2]:TB[3]}
function view(){const o=[];trades.forEach((t,i)=>{if(flt.res=='w'&&!win(t)||flt.res=='l'&&win(t)||flt.side!='all'&&(flt.side=='L')!=t.long||flt.day!='all'&&t.day!=flt.day||flt.time!='all'&&bucket(t)!=flt.time||flt.size!='all'&&(+t.size||1)!=+flt.size)return;o.push(i)});
 return o.sort({t:(a,b)=>a-b,l:(a,b)=>net(trades[a])-net(trades[b]),w:(a,b)=>net(trades[b])-net(trades[a])}[flt.sort])}
function renderFlt(){const v=view(),sm=v.reduce((a,i)=>a+net(trades[i]),0),mk=(id,op,cur)=>`<select onchange="setF('${id}',this.value)">${op.map(o=>`<option value="${o[0]}" ${o[0]==cur?'selected':''}>${o[1]}</option>`).join('')}</select>`;
 $('flt').innerHTML=`<div style="display:flex;gap:6px">${[['all','All'],['w','Winners'],['l','Losers']].map(o=>`<span class="chip ${flt.res==o[0]?'on':''}" onclick="setF('res','${o[0]}')">${o[1]}</span>`).join('')}</div><div class="fg">${mk('side',[['all','Any side'],['L','Long'],['S','Short']],flt.side)}${mk('day',[['all','All days'],...Object.keys(days).sort().map(d=>[d,dlab(d)])],flt.day)}${mk('time',[['all','Any time (NY)'],...TB.map(x=>[x,x])],flt.time)}${mk('size',[['all','Any size'],...[...new Set(trades.map(t=>+t.size||1))].sort((a,b)=>a-b).map(z=>[z,z+(z>1?' contracts':' contract')])],flt.size)}${mk('sort',[['t','By time'],['l','Biggest loss'],['w','Biggest win']],flt.sort)}</div><div class="mu">Showing ${v.length} of ${trades.length} | net <b class="${sm>=0?'w':'l'}">${usd(sm)}</b></div><button onclick="dlAll()">Save these ${v.length} as PNGs</button>`}
function setF(k,v){flt[k]=v;renderList();if(sumOn)showSummary()}
function backToSummary(){flt.day='all';renderList();showSummary()}
function gotoDay(d){flt.day=d;renderList();const v=view();if(v.length)show(v[0])}
function stats(v){const a=v.map(i=>trades[i]),nt=a.map(net),W=nt.filter(x=>x>0),L=nt.filter(x=>x<0),sm=x=>x.reduce((p,q)=>p+q,0),bd={},bt={},bs={},bh={},nyH=t=>{const o=off('America/New_York',t.et*1000);return new Date((t.et+o)*1000).getUTCHours()};
 a.forEach(t=>{const z=+t.size||1,hr=nyH(t);for(const [M,k] of [[bd,t.day],[bt,bucket(t)],[bs,z],[bh,hr+'|'+z]]){const o=M[k]??={n:0,w:0,s:0};o.n++;o.s+=net(t);if(net(t)>0){o.w++;o.sw=(o.sw||0)+net(t)}else if(net(t)<0){o.l=(o.l||0)+1;o.sl=(o.sl||0)+net(t)}}});
 return {n:a.length,net:sm(nt),wr:a.length?W.length/a.length:0,aw:W.length?sm(W)/W.length:0,al:L.length?sm(L)/L.length:0,pf:L.length?sm(W)/Math.abs(sm(L)):NaN,byDay:bd,byT:bt,bySize:bs,byHS:bh}}
let reviews={},jrInit=false;const jrOpen=new Set(),med=a=>{if(!a.length)return 0;const q=[...a].sort((x,y)=>x-y),m=q.length>>1;return q.length%2?q[m]:(q[m-1]+q[m])/2},dayTrades=d=>trades.filter(t=>t.day==d).sort((a,b)=>a.et-b.et);
function nyStr(sec){const o=off('America/New_York',sec*1000),d=new Date((sec+o)*1000);return String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0')}
function analyzeDay(d){const T=dayTrades(d),n=T.length;if(!n)return null;
 const P=T.map(net),sz=T.map(t=>+t.size||1),sm=x=>x.reduce((p,q)=>p+q,0),avg=x=>x.length?sm(x)/x.length:0,W=P.filter(x=>x>0),Lo=P.filter(x=>x<0),gw=sm(W),gl=sm(Lo),aW=avg(W),aL=avg(Lo),
  hw=T.filter(t=>net(t)>0).map(t=>(t.xt-t.et)/60),hl=T.filter(t=>net(t)<0).map(t=>(t.xt-t.et)/60),medAll=med(trades.map(t=>+t.size||1)),
  others=[...new Set(trades.map(t=>t.day))].filter(x=>x!=d).map(x=>trades.filter(t=>t.day==x).length),medN=others.length>=2?med(others):null;
 let c=0,pk=0,dd=0,run=0,mr=0,en=0,ep=0,rn=0,rp=0;P.forEach(v=>{c+=v;pk=Math.max(pk,c);dd=Math.max(dd,pk-c);if(v<0){run++;mr=Math.max(mr,run)}else run=0});
 for(let i=1;i<n;i++)if(P[i-1]<0){if(sz[i]>sz[i-1]){en++;ep+=P[i]}if((T[i].et-T[i-1].xt)/60<3){rn++;rp+=P[i]}}
 const ck=[],add=(label,ok,detail)=>ck.push({label,ok,detail}),mxs=Math.max(...sz);
 add('No bigger size right after a loss',en==0,en?`${en} trade(s) were larger than the losing trade just before them; those trades netted ${usd(ep)}`:'Size never went up straight after a loss');
 add('No re-entry within 3 minutes of a loss',rn==0,rn?`${rn} quick re-entr${rn>1?'ies':'y'} after a loss, net ${usd(rp)}`:'No rapid re-entries after a loss');
 add('Average loss within 1.5x the average win',!Lo.length||(aW>0&&-aL<=1.5*aW),Lo.length?`Average win ${usd(aW)} vs average loss ${usd(aL)}`:'No losing trades');
 add('Largest size within 2x your typical size',mxs<=2*medAll,`Largest ${mxs} contracts vs typical (median) ${medAll}`);
 add('No more than 3 losses in a row',mr<=3,`Longest losing streak: ${mr}`);
 add('Did not give back more than half of the peak profit',pk<=0||c>=pk*.5,pk>0?`Peak ${usd(pk)}, finished ${usd(c)}`:'Never in profit during the day');
 if(hw.length&&hl.length)add('Losers not held much longer than winners',avg(hl)<=1.5*avg(hw),`Average hold: winners ${avg(hw).toFixed(1)} min, losers ${avg(hl).toFixed(1)} min`);
 if(medN)add('Trade count close to your usual',n<=1.5*medN,`${n} trades vs about ${Math.round(medN)} on a usual day`);
 const S_=sessData(T),ins=[],ss=TB.filter(b=>S_[b].n>0).sort((a,b)=>S_[b].s-S_[a].s),big=T.filter((t,i)=>medAll>0&&sz[i]>=1.5*medAll),small=T.filter((t,i)=>sz[i]<1.5*medAll),wst=Math.min(...P);
 if(ss.length>1)ins.push(`Best session: ${ss[0]} (${usd(S_[ss[0]].s)}, ${S_[ss[0]].n} trades). Weakest: ${ss[ss.length-1]} (${usd(S_[ss[ss.length-1]].s)}, ${S_[ss[ss.length-1]].n} trades).`);
 if(big.length>=2&&small.length>=2)ins.push(`Larger trades (1.5x typical size or more) averaged ${usd(avg(big.map(net)))} per trade, normal-size trades ${usd(avg(small.map(net)))}.`);
 if(gl<0&&Lo.length>1&&wst/gl>.5)ins.push(`One trade made up ${(wst/gl*100).toFixed(0)}% of all the losses that day (${usd(wst)}).`);
 const tg={};T.forEach(t=>((notes[nk(t)]||{}).tags||[]).forEach(g=>{(tg[g]??={n:0,s:0}).n++;tg[g].s+=net(t)}));Object.entries(tg).forEach(([g,o])=>ins.push(`Tagged "${g}": ${o.n} trade(s), net ${usd(o.s)}.`));
 return {day:d,fp:n+'|'+sm(P).toFixed(2),n,w:W.length,l:Lo.length,wr:W.length/n,net:sm(P),pf:Lo.length?gw/-gl:null,aW,aL,best:Math.max(...P),worst:wst,dd,mr,peak:pk,avgSz:sm(sz)/n,maxSz:mxs,avgHold:avg(T.map(t=>(t.xt-t.et)/60)),sess:S_,checks:ck,ins}}
async function syncReviews(){try{const r=await(await fetch('/api/review_all',{method:'POST',body:JSON.stringify({csv:csvName})})).json();if(r.error)return;reviews=r.reviews||{};
 const core=o=>JSON.stringify({...o,mine:0,ai:0,aiFp:0,aiAt:0,aiModel:0,gen:0});
 for(const d of [...new Set(trades.map(t=>t.day))]){const a=analyzeDay(d),o=reviews[d]||{},nw={...a,mine:o.mine||'',ai:o.ai||'',aiFp:o.aiFp||'',aiAt:o.aiAt||0,aiModel:o.aiModel||'',gen:o.gen||0};
  if(!reviews[d]||core(o)!=core(nw)){nw.gen=Date.now();reviews[d]=nw;await fetch('/api/review_save',{method:'POST',body:JSON.stringify({csv:csvName,day:d,review:nw})})}}
 if($('jr').style.display=='block')renderJournal()}catch(e){}}
const saveRev=d=>fetch('/api/review_save',{method:'POST',body:JSON.stringify({csv:csvName,day:d,review:reviews[d]})});
function toggleJ(d,o){o?jrOpen.add(d):jrOpen.delete(d)}
async function saveMine(d){const e=$('jm-'+d);if(!e||!reviews[d])return;reviews[d].mine=e.value;await saveRev(d)}
async function resetRev(d){if(!confirm('Reset the log for '+dlab(d)+'? This clears its AI review and your reflection, then rebuilds the numbers from your trades.'))return;await fetch('/api/review_del',{method:'POST',body:JSON.stringify({csv:csvName,day:d})});delete reviews[d];await syncReviews();renderJournal()}
function aiPayload(d){const r=reviews[d],T=dayTrades(d),nt=t=>notes[nk(t)]||{};
 return {day:d,csv:csvName,contract:(T[0]||{}).sym||'MNQ',times:'New York time',
  trades:T.map((t,i)=>({n:i+1,side:t.long?'long':'short',size:+t.size||1,entry_time:nyStr(t.et),exit_time:nyStr(t.xt),held_min:+((t.xt-t.et)/60).toFixed(1),entry_price:t.ep,exit_price:t.xp,pnl_usd:+net(t).toFixed(2),session:bucket(t),tags:nt(t).tags||[],note:(nt(t).text||'').slice(0,200)})),
  stats:{trades:r.n,wins:r.w,losses:r.l,win_rate_pct:+(r.wr*100).toFixed(1),net_usd:+r.net.toFixed(2),profit_factor:r.pf==null?null:+r.pf.toFixed(2),avg_win:+r.aW.toFixed(2),avg_loss:+r.aL.toFixed(2),best_trade:+r.best.toFixed(2),worst_trade:+r.worst.toFixed(2),max_intraday_drawdown:+r.dd.toFixed(2),longest_losing_streak:r.mr,avg_size:+r.avgSz.toFixed(1),max_size:r.maxSz,avg_hold_min:+r.avgHold.toFixed(1)},
  sessions:Object.fromEntries(TB.filter(b=>r.sess[b].n).map(b=>[b,{trades:r.sess[b].n,win_rate_pct:+(r.sess[b].w/r.sess[b].n*100).toFixed(0),profit_factor:pfS(r.sess[b]),net_usd:+r.sess[b].s.toFixed(2)}])),
  discipline_checks:r.checks.map(c=>({check:c.label,passed:c.ok,detail:c.detail})),observations:r.ins,trader_reflection:(r.mine||'').slice(0,600)}}
async function aiRev(d){const r=reviews[d];if(!r)return;st('Writing the AI review for '+dlab(d)+' - this can take up to a minute...');
 try{const res=await(await fetch('/api/ai_review',{method:'POST',body:JSON.stringify({data:aiPayload(d)})})).json();if(res.error)throw new Error(res.error);
  r.ai=res.text;r.aiFp=r.fp;r.aiAt=Date.now();r.aiModel=res.model;await saveRev(d);jrOpen.add(d);renderJournal();st('AI review saved for '+dlab(d))}catch(e){st(e.message,1)}}
function jrBody(d,r){const g=(a,b,c)=>`<div class="c2"><span class="mu">${a}</span><b class="${c||''}" style="font-size:17px">${b}</b></div>`,pf=r.pf==null?'-':r.pf.toFixed(2),stale=r.ai&&r.aiFp!=r.fp,pc=r.checks.filter(c=>c.ok).length;
 return `<div class="cg">${g('Net',usd(r.net),r.net>=0?'w':'l')}${g('Win rate',(r.wr*100).toFixed(0)+'%')}${g('Profit factor',pf)}${g('Avg win / avg loss',usd(r.aW)+' / '+usd(r.aL))}${g('Best / worst trade',usd(r.best)+' / '+usd(r.worst))}${g('Max drawdown in the day',usd(-r.dd),'l')}${g('Longest losing streak',r.mr)}${g('Size avg / max',r.avgSz.toFixed(1)+' / '+r.maxSz)}</div>
 <div class="mu" style="margin:6px 0">Discipline checks: ${pc} of ${r.checks.length} passed</div>${r.checks.map(c=>`<div class="ck"><b class="${c.ok?'w':'l'}">${c.ok?'PASS':'CHECK'}</b><span style="flex:1">${esc(c.label)}<br><span class="mu">${esc(c.detail)}</span></span></div>`).join('')}
 ${r.ins.length?`<div class="mu" style="margin:12px 0 4px">Observations</div><ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.6">${r.ins.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}
 <div class="c2" style="padding:0;margin-top:12px;overflow-x:auto"><table class="tb"><tr><th>Session (NY)</th><th>Trades</th><th>Win rate</th><th>Profit factor</th><th>Net</th></tr>${TB.filter(b=>r.sess[b].n).map(b=>{const q=r.sess[b];return `<tr><td>${b}</td><td>${q.n}</td><td>${(q.w/q.n*100).toFixed(0)}%</td><td>${pfS(q)}</td><td class="${q.s>=0?'w':'l'}">${usd(q.s)}</td></tr>`}).join('')}</table></div>
 <div class="mu" style="margin:14px 0 4px">My reflection (saved with this day)</div><textarea id="jm-${d}" style="width:100%;height:70px;box-sizing:border-box" onblur="saveMine('${d}')">${esc(r.mine||'')}</textarea>
 <div class="mu" style="margin:14px 0 4px">AI deep review${stale?' (outdated: your trades for this day changed after it was written)':''}</div>${r.ai?`<div style="white-space:pre-wrap;font-size:13px;line-height:1.55">${esc(r.ai)}</div><div class="mu" style="margin-top:6px">Written ${new Date(r.aiAt).toLocaleString()} | ${esc(r.aiModel||'')}</div>`:'<div class="mu">Not written yet. Click the button below.</div>'}
 <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap"><button onclick="aiRev('${d}')">${r.ai?'Regenerate':'Generate'} AI deep review</button><button onclick="gotoDay('${d}')">Open this day on the chart</button><button onclick="resetRev('${d}')">Reset this log</button></div>`}
function renderJournal(){const ds=Object.keys(reviews).sort().reverse();if(!ds.length){$('jr').innerHTML='<div class="mu" style="padding:12px">Load a trade CSV first. A log is created automatically for each trading day.</div>';return}
 if(!jrInit){jrOpen.add(ds[0]);jrInit=true}
 $('jr').innerHTML=`<h2 style="margin:0">Daily log</h2><div class="mu" style="margin:4px 0 12px">${ds.length} trading days | total net ${usd(ds.reduce((a,d)=>a+reviews[d].net,0))} | created automatically from your uploads and saved online</div>`+
 ds.map(d=>{const r=reviews[d],pc=r.checks.filter(c=>c.ok).length;return `<details class="rv" ${jrOpen.has(d)?'open':''} ontoggle="toggleJ('${d}',this.open)"><summary><b style="min-width:150px">${dlab(d)}</b><b class="${r.net>=0?'w':'l'}" style="min-width:90px">${usd(r.net)}</b><span class="mu">${r.n} trades | win ${(r.wr*100).toFixed(0)}% | PF ${r.pf==null?'-':r.pf.toFixed(2)} | checks ${pc}/${r.checks.length}</span>${r.ai?'<span class="chip on">AI review</span>':''}${r.mine?'<span class="chip">my note</span>':''}</summary><div class="rb">${jrBody(d,r)}</div></details>`}).join('')}
async function showJournal(){syncNote();sumOn=false;['chart','det','note','sum'].forEach(x=>$(x).style.display='none');$('jr').style.display='block';$('jr').innerHTML='<div class="mu" style="padding:12px">Building the daily log...</div>';await syncReviews();renderJournal();st('Daily log | one entry per trading day, saved online')}
let scSel={d:'all',r:'auto',l:'stack',dot:true};
const SBD=[['Overnight / Asia / London',0,930],['NY open',930,1020],['Midday',1020,1200],['NY afternoon',1200,1380]];
function sessData(a){const o={};TB.forEach(b=>o[b]={n:0,w:0,sw:0,sl:0,s:0,z:0});a.forEach(t=>{const x=o[bucket(t)],v=net(t);x.n++;x.s+=v;x.z+=(+t.size||1);if(v>0){x.w++;x.sw+=v}else if(v<0)x.sl+=v});return o}
const pfS=x=>x.sl<0?(x.sw/-x.sl).toFixed(2):(x.sw>0?'inf':'-');
function etPos(sec){const o=off('America/New_York',sec*1000),d=new Date((sec+o)*1000),m=d.getUTCHours()*60+d.getUTCMinutes()+d.getUTCSeconds()/60;return m>=1080?m-1080:m>=1020?1380:m+360}
function renderSession(){const box=$('scbox');if(!box)return;const dsel=$('scd').value||'all',rg=$('scr').value||'auto',lay=$('scl').value||'stack',dots=$('scdot').checked!==false;scSel={d:dsel,r:rg,l:lay,dot:dots};
 let a=view().map(i=>trades[i]);if(dsel!='all')a=a.filter(t=>t.day==dsel);
 if(!a.length){box.innerHTML='<div class="mu" style="padding:12px">No trades for this selection.</div>';return}
 const dsp=$('disptz').value,tzn=dsp=='Asia/Kolkata'?'IST':dsp.split('/').pop(),rowsT=[...new Map(a.map(t=>{const ms=t.et*1000,no=off('America/New_York',ms);return [no,Math.round((off(dsp,ms)-no)/60)]})).entries()].sort((p,q)=>q[0]-p[0]),xr=16*(rowsT.length-1);
 const nd=new Set(a.map(t=>t.day)).size,ps=a.flatMap(t=>[etPos(t.et),etPos(t.xt)]);let T0=0,T1=1380;
 if(rg=='us')T0=930;else if(rg=='auto'){T0=Math.max(0,Math.floor((Math.min(...ps)-20)/30)*30);T1=Math.min(1380,Math.ceil((Math.max(...ps)+20)/30)*30);if(T1-T0<120){T1=Math.min(1380,T0+120);T0=Math.max(0,T1-120)}}
 const stk=lay=='stack',W=1100,L=72,R=stk?24:64,PW=W-L-R,TP=56,PH=stk?240:300,SH=stk?110:PH,py0=TP,sy0=stk?TP+PH+34:TP,bot=sy0+SH,H=bot+150+xr,f=n=>n.toFixed(1),X=p=>L+(Math.min(Math.max(p,T0),T1)-T0)/(T1-T0)*PW,ev=[],sev=[];
 a.forEach(t=>{const z=(+t.size||1)/nd;ev.push([etPos(t.xt),net(t),t]);sev.push([etPos(t.et),z],[etPos(t.xt),-z])});ev.sort((p,q)=>p[0]-q[0]);sev.sort((p,q)=>p[0]-q[0]);
 const steps=E=>{let v=0;const pts=[];for(const e of E){v+=e[1];if(e[0]<T0){pts.length=0;pts.push([T0,v])}else{if(!pts.length)pts.push([T0,0]);pts.push([e[0],v])}}if(!pts.length)pts.push([T0,0]);return pts},
  cp=steps(ev),sp=steps(sev),vals=cp.map(p=>p[1]).concat(0),mn0=Math.min(...vals),mx0=Math.max(...vals),rg_=(mx0-mn0||1),mn=mn0-rg_*.08,mx=mx0+rg_*.28,
  Y=v=>py0+PH-(v-mn)/(mx-mn)*PH,mz=Math.max(1,...sp.map(p=>p[1]))*1.12,Z=v=>sy0+SH-v/mz*SH,
  path=(pts,F)=>{let d=`M${f(X(T0))},${f(F(pts[0][1]))}`;for(let i=1;i<pts.length;i++)d+=`H${f(X(pts[i][0]))}V${f(F(pts[i][1]))}`;return d+`H${f(X(T1))}`},
  D=sessData(a),col=x=>x.n?(x.s>0?'38,166,154':'239,83,80'):'120,130,150',m0=v=>(v<0?'-':'')+'$'+Math.abs(Math.round(v)),
  tx=(x,y,t,o='')=>`<text x="${f(x)}" y="${f(y)}" ${/font-size=/.test(o)?'':'font-size="12"'} ${/fill=/.test(o)?'':'fill="#c3cada"'} ${o}>${t}</text>`,hl=(y,c)=>`<line x1="${L}" x2="${L+PW}" y1="${f(y)}" y2="${f(y)}" stroke="${c}"/>`;
 let g=`<rect width="${W}" height="${H}" fill="#0f1115"/>`;
 SBD.forEach(([nm,s0,e0])=>{if(e0<=T0||s0>=T1)return;const x0=X(s0),x1=X(e0),w=x1-x0,q=D[nm],wr=q.n?(q.w/q.n*100).toFixed(0)+'%':'-',lines=[];
  if(w>=60)lines.push(nm.length*6.6>w-8?nm.split(' ')[0]:nm);if(w>=150)lines.push(`WR ${wr} | PF ${pfS(q)}`);
  g+=`<rect x="${f(x0)}" y="${py0}" width="${f(w)}" height="${bot-py0}" fill="rgba(${col(q)},${q.n?.07:.03})"><title>${nm}: ${q.n} trades | win rate ${wr} | profit factor ${pfS(q)} | net ${usd(q.s)}</title></rect><rect x="${f(x0)}" y="${py0}" width="${f(w)}" height="4" fill="rgba(${col(q)},.9)"/><line x1="${f(x0)}" x2="${f(x0)}" y1="${py0}" y2="${bot}" stroke="#2a3142" stroke-dasharray="2 4"/>`+lines.map((t,i)=>tx(x0+6,py0+20+i*15,t,'fill="#e6e9f2"')).join('')});
 {const raw=(mx-mn)/5,mag=10**Math.floor(Math.log10(raw)),stp=[1,2,5,10].map(q=>q*mag).find(q=>q>=raw);for(let v=Math.ceil(mn/stp)*stp;v<=mx+1e-9;v+=stp)g+=hl(Y(v),'#1c2230')+tx(L-8,Y(v)+4,m0(v),'text-anchor="end"')}
 const zs=Math.max(1,Math.ceil(mz/(stk?3:5)));for(let z=0;z<=mz;z+=zs)g+=(stk?hl(Z(z),'#1c2230'):'')+tx(stk?L-8:L+PW+8,Z(z)+4,z,stk?'text-anchor="end" fill="#ffb74d"':'fill="#ffb74d"');
 if(mn<0&&mx>0)g+=`<line x1="${L}" x2="${L+PW}" y1="${f(Y(0))}" y2="${f(Y(0))}" stroke="#6b7389" stroke-dasharray="4 4"/>`;
 if(stk)g+=hl(py0+PH,'#2a3142')+hl(sy0+SH,'#2a3142');
 g+=`<path d="${path(sp,Z)}V${f(Z(0))}H${f(X(T0))}Z" fill="rgba(255,183,77,.2)" stroke="none"/><path d="${path(sp,Z)}" fill="none" stroke="#ffb74d" stroke-width="1.8"/><path d="${path(cp,Y)}" fill="none" stroke="#e6e9f2" stroke-width="2.4"/>`;
 if(dots){let cum=0;const r=a.length>40?2.6:3.4;for(const e of ev){cum+=e[1];if(e[0]<T0)continue;g+=`<circle cx="${f(X(e[0]))}" cy="${f(Y(cum))}" r="${r}" fill="${e[1]>0?'#26a69a':'#ef5350'}" stroke="#0f1115" stroke-width=".8"><title>${fmt(e[2].xt)} | ${e[2].long?'LONG':'SHORT'} x${e[2].size||1} | ${usd(e[1])}</title></circle>`}}
 const hh=m=>{m=((m%1440)+1440)%1440;return String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0')},
  sp_=T1-T0,tstep=sp_<=240?30:sp_<=600?60:120;
 for(let p=Math.ceil(T0/tstep)*tstep;p<=T1;p+=tstep){const nyMin=(1080+p)%1440;g+=`<line x1="${f(X(p))}" x2="${f(X(p))}" y1="${bot}" y2="${bot+5}" stroke="#6b7389"/>`+tx(X(p),bot+20,hh(nyMin),'text-anchor="middle"')+rowsT.map(([no,dm],i)=>tx(X(p),bot+36+16*i,hh(nyMin+dm),'text-anchor="middle" fill="#8b93a7"')).join('')}
 g+=tx(6,bot+20,'NY')+rowsT.map(([no],i)=>tx(6,bot+36+16*i,`${tzn} (${no==-14400?'EDT':no==-18000?'EST':'NY'})`,'fill="#8b93a7" font-size="10"')).join('');
 g+=tx(L,22,`<tspan font-weight="bold" font-size="15" fill="#fff">Cumulative P&amp;L and position size by time of day</tspan>  ${dsel=='all'?nd+' day'+(nd>1?'s':'')+' (P&amp;L summed, size averaged per day)':dlab(dsel)}`);
 if(stk)g+=tx(L,py0-8,'Cumulative P&amp;L ($)','fill="#aab2c5" font-size="11"')+tx(L,sy0-8,'Position size (contracts)','fill="#ffb74d" font-size="11"');
 else g+=`<line x1="${L}" x2="${L+24}" y1="38" y2="38" stroke="#e6e9f2" stroke-width="2.4"/>`+tx(L+30,42,'P&amp;L ($, left axis)')+`<line x1="${L+170}" x2="${L+194}" y1="38" y2="38" stroke="#ffb74d" stroke-width="2"/>`+tx(L+200,42,'Position size (contracts, right axis)','fill="#ffb74d"');
 const cw=(W-40)/4,ly=bot+62+xr;
 SBD.slice().sort((p,q)=>p[1]-q[1]).forEach(([nm],i)=>{const q=D[nm],x0=20+i*cw,wr=q.n?(q.w/q.n*100).toFixed(0)+'%':'-';
  g+=`<rect x="${f(x0)}" y="${ly}" width="12" height="12" fill="rgba(${col(q)},.75)"/>`+tx(x0+18,ly+11,nm,'font-weight="bold" fill="#fff"')+tx(x0,ly+34,`Win rate ${wr} | Profit factor ${pfS(q)}`)+tx(x0,ly+52,`Net ${usd(q.s)} | ${q.n} trade${q.n==1?'':'s'}`)+tx(x0,ly+70,`Avg size ${q.n?(q.z/q.n).toFixed(1):'-'} contracts`,'fill="#8b93a7"')});
 box.innerHTML=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="100%" font-family="Segoe UI,Arial,sans-serif" data-w="${W}" data-h="${H}">${g}</svg>`}
function scPng(){const svg=$('scbox').querySelector('svg');if(!svg)return;const w=+svg.dataset.w*2,h=+svg.dataset.h*2,cl=svg.cloneNode(true);cl.setAttribute('width',w);cl.setAttribute('height',h);
 const img=new Image();img.onload=()=>{const c=document.createElement('canvas');c.width=w;c.height=h;c.getContext('2d').drawImage(img,0,0,w,h);nS=nR=0;save('session_chart.png',c.toDataURL('image/png')).then(()=>st('Saved'+(nR?' (replaced old file)':'')+': '+lastFile)).catch(e=>st(e.message,1))};
 img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(cl))}
function calHtml(bd){const ks=Object.keys(bd).sort();if(!ks.length)return'';const mx=Math.max(1,...ks.map(k=>Math.abs(bd[k].s))),p2=n=>String(n).padStart(2,'0'),mon=[...new Set(ks.map(k=>k.slice(0,7)))],MN=['January','February','March','April','May','June','July','August','September','October','November','December'];
 return '<div class="c2" style="margin-bottom:12px"><div class="mu">Calendar (click a day to open its trades)</div>'+mon.map(m=>{const [Y,M]=m.split('-').map(Number),dim=new Date(Date.UTC(Y,M,0)).getUTCDate(),lead=(new Date(Date.UTC(Y,M-1,1)).getUTCDay()+6)%7,cells=Array(lead).fill(null);
  for(let d=1;d<=dim;d++)cells.push(d);while(cells.length%7)cells.push(null);let mt=0;ks.filter(k=>k.startsWith(m)).forEach(k=>mt+=bd[k].s);
  let h=`<div style="margin:12px 0 6px"><b>${MN[M-1]} ${Y}</b> <span class="${mt>=0?'w':'l'}" style="margin-left:10px">${usd(mt)}</span></div><div style="overflow-x:auto"><div class="cal">`+['Mon','Tue','Wed','Thu','Fri','Sat','Sun','Week'].map(x=>`<div class="ch">${x}</div>`).join('');
  for(let i=0;i<cells.length;i+=7){let wt=0,wn=0;
   for(let j=0;j<7;j++){const d=cells[i+j];if(!d){h+='<div class="cd" style="opacity:.35"></div>';continue}const k=m+'-'+p2(d),o=bd[k];
    if(o){wt+=o.s;wn+=o.n;const a=(.18+.5*Math.abs(o.s)/mx).toFixed(2);h+=`<div class="cd has" onclick="gotoDay('${k}')" style="background:${o.s>=0?`rgba(38,166,154,${a})`:`rgba(239,83,80,${a})`}"><span class="dn">${d}</span><b>${usd(o.s)}</b>${o.n} trade${o.n>1?'s':''} | ${(o.w/o.n*100).toFixed(0)}% win</div>`}
    else h+=`<div class="cd"><span class="dn">${d}</span></div>`}
   h+=`<div class="cd" style="background:#141a26">${wn?`<b class="${wt>=0?'w':'l'}">${usd(wt)}</b>${wn} trades`:''}</div>`}
  return h+'</div></div>'}).join('')+'</div>'}
function sizeHtml(S){const sz=Object.keys(S.bySize).map(Number).sort((a,b)=>a-b);if(!sz.length)return'';const pc=x=>(x*100).toFixed(0)+'%',mxS=Math.max(1,...Object.values(S.byHS).map(o=>Math.abs(o.s))),
 tint=s=>{const a=(.15+.5*Math.min(1,Math.abs(s)/mxS)).toFixed(2);return s>=0?`rgba(38,166,154,${a})`:`rgba(239,83,80,${a})`},
 rows=sz.map(z=>{const o=S.bySize[z];return `<tr><td>${z} contract${z>1?'s':''}</td><td>${o.n}</td><td>${pc(o.w/o.n)}</td><td class="w">${usd(o.w?o.sw/o.w:0)}</td><td class="l">${usd(o.l?o.sl/o.l:0)}</td><td>${usd(o.s/o.n)}</td><td>${usd(o.s/o.n/z)}</td><td><b class="${o.s>=0?'w':'l'}">${usd(o.s)}</b></td></tr>`}).join(''),
 hrs=[...new Set(Object.keys(S.byHS).map(k=>+k.split('|')[0]))].sort((a,b)=>a-b),
 mat=hrs.map(h=>{let tn=0,ts=0;const c=sz.map(z=>{const o=S.byHS[h+'|'+z];if(!o)return'<td></td>';tn+=o.n;ts+=o.s;return `<td style="background:${tint(o.s)}"><b>${usd(o.s)}</b><br><span class="mu">${o.n} trade${o.n>1?'s':''}</span></td>`}).join('');return `<tr><td>${String(h).padStart(2,'0')}:00</td>${c}<td><b class="${ts>=0?'w':'l'}">${usd(ts)}</b><br><span class="mu">${tn} trades</span></td></tr>`}).join('');
 return `<div class="c2" style="padding:0;margin-top:12px;overflow-x:auto"><div class="mu" style="padding:10px 12px">Position size vs profit (use the Size filter on the left to look at one size)</div><table class="tb"><tr><th>Size</th><th>Trades</th><th>Win rate</th><th>Avg win</th><th>Avg loss</th><th>Avg $/trade</th><th>$/trade per contract</th><th>Net</th></tr>${rows}</table></div>
 <div class="c2" style="padding:0;margin-top:12px;overflow-x:auto"><div class="mu" style="padding:10px 12px">Entry hour (New York time) by position size: net profit and number of trades</div><table class="tb"><tr><th>Hour</th>${sz.map(z=>`<th>${z} contract${z>1?'s':''}</th>`).join('')}<th>Total</th></tr>${mat}</table></div>`}
function showSummary(){sumOn=true;syncNote();const v=view(),S=stats(v),pc=x=>(x*100).toFixed(0)+'%',D=Object.entries(S.byDay).sort(),bs=D.slice().sort((a,b)=>b[1].s-a[1].s),
 mx=Math.max(1,...D.map(x=>Math.abs(x[1].s)),...Object.values(S.byT).map(x=>Math.abs(x.s))),card=(a,b,c)=>`<div class="c2"><span class="mu">${a}</span><b class="${c||''}" style="font-size:22px">${b}</b></div>`,
 row=(lab,o,ck)=>`<div class="sr" ${ck?`onclick="${ck}"`:''}><span style="width:150px">${lab}</span><span class="mu" style="width:80px">${o.n} trades</span><span class="mu" style="width:80px">Win ${pc(o.w/o.n)}</span><span style="flex:1"><span style="display:block;height:12px;width:${Math.abs(o.s)/mx*100}%;background:${o.s>=0?'#26a69a':'#ef5350'};border-radius:3px"></span></span><b class="${o.s>=0?'w':'l'}" style="width:100px;text-align:right">${usd(o.s)}</b></div>`;
 ['chart','det','note'].forEach(x=>$(x).style.display='none');$('jr').style.display='none';$('sum').style.display='block';
 $('sum').innerHTML=`<h2 style="margin:0">Summary</h2><div class="mu">${S.n} trades currently shown in the list (use the filters on the left to change this)${flt.day!='all'?` | only ${dlab(flt.day)} <button onclick="backToSummary()" style="margin-left:8px">Show all days</button>`:''}</div><div class="cg">${card('Net',usd(S.net),S.net>=0?'w':'l')}${card('Win rate',pc(S.wr))}${card('Trades',S.n)}${card('Avg win',usd(S.aw),'w')}${card('Avg loss',usd(S.al),'l')}${card('Profit factor',isFinite(S.pf)?S.pf.toFixed(2):'-')}${card('Best day',bs.length?usd(bs[0][1].s):'-','w')}${card('Worst day',bs.length?usd(bs[bs.length-1][1].s):'-','l')}</div>
 <div class="c2" style="margin-bottom:12px"><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px"><b>Session chart</b><select id="scd" onchange="renderSession()"><option value="all">All days</option>${D.map(([d])=>`<option value="${d}">${dlab(d)}</option>`).join('')}</select><select id="scr" onchange="renderSession()"><option value="auto">Zoom to my trades</option><option value="full">Full trading day (18:00-17:00 NY)</option><option value="us">US hours (09:30-17:00 NY)</option></select><select id="scl" onchange="renderSession()"><option value="stack">Two panels (cleaner)</option><option value="overlay">One panel (overlay)</option></select><label class="mu" style="flex-direction:row;align-items:center;gap:4px"><input id="scdot" type="checkbox" checked onchange="renderSession()"> Trade dots</label><button onclick="scPng()">Save chart as PNG</button></div><div id="scbox"></div></div>${calHtml(S.byDay)}<div class="c2" style="padding:0"><div class="mu" style="padding:10px 12px">Day-end net (click a day to open its trades)</div>${D.map(([d,o])=>row(dlab(d),o,`gotoDay('${d}')`)).join('')}</div>
 <div class="c2" style="padding:0;margin-top:12px"><div class="mu" style="padding:10px 12px">By time of day (New York time)</div>${TB.filter(b=>S.byT[b]).map(b=>row(b,S.byT[b])).join('')}</div>
 ${sizeHtml(S)}<div style="margin:12px 0;display:flex;gap:8px"><button onclick="sumPng()">Save summary as PNG</button><button onclick="sumCsv()">Export day table (CSV)</button></div>`;if([...(($('scd').options)||[])].some(o=>o.value==scSel.d))$('scd').value=scSel.d;$('scr').value=scSel.r;$('scl').value=scSel.l;$('scdot').checked=scSel.dot;renderSession();st('Summary view | click any trade or a day to go back to the chart')}
function sumPng(){const S=stats(view()),D=Object.entries(S.byDay).sort(),W=1280,H=190+D.length*34+40,c=document.createElement('canvas');c.width=W;c.height=H;const g=c.getContext('2d');
 g.fillStyle='#0b0d11';g.fillRect(0,0,W,H);g.fillStyle='#fff';g.font='bold 28px sans-serif';g.fillText('MNQ summary | '+csvName,24,44);
 [['Net',usd(S.net)],['Win rate',(S.wr*100).toFixed(0)+'%'],['Trades',S.n],['Avg win',usd(S.aw)],['Avg loss',usd(S.al)],['Profit factor',isFinite(S.pf)?S.pf.toFixed(2):'-']].forEach((x,i)=>{g.font='15px sans-serif';g.fillStyle='#9aa3b8';g.fillText(x[0],24+i*205,92);g.font='bold 26px sans-serif';g.fillStyle=['Net','Avg win','Avg loss'].includes(x[0])?(String(x[1])[0]=='-'?'#ef5350':'#26a69a'):'#fff';g.fillText(x[1],24+i*205,126)});
 const mx=Math.max(1,...D.map(x=>Math.abs(x[1].s)));D.forEach(([d,o],i)=>{const y=180+i*34;g.font='16px sans-serif';g.fillStyle='#fff';g.fillText(dlab(d),24,y);g.fillStyle='#9aa3b8';g.fillText(`${o.n} trades | Win ${(o.w/o.n*100).toFixed(0)}%`,220,y);g.fillStyle=o.s>=0?'#26a69a':'#ef5350';g.fillRect(480,y-14,Math.abs(o.s)/mx*560,16);g.textAlign='right';g.fillText(usd(o.s),W-24,y);g.textAlign='left'});
 nS=nR=0;save('summary.png',c.toDataURL('image/png')).then(()=>st('Saved'+(nR?' (replaced old file)':'')+': '+lastFile)).catch(e=>st(e.message,1))}
function sumCsv(){const S=stats(view()),t='day,trades,wins,win_rate,net\n'+Object.entries(S.byDay).sort().map(([d,o])=>`${d},${o.n},${o.w},${(o.w/o.n*100).toFixed(1)},${o.s.toFixed(2)}`).join('\n');
 nS=nR=0;save('day_table.csv','data:text/csv;base64,'+btoa(t)).then(()=>st('Saved'+(nR?' (replaced old file)':'')+': '+lastFile)).catch(e=>st(e.message,1))}
function syncNote(){const e=$('nt'),t=trades[sel];if(!e||!t)return;const k=nk(t);if(e.value||notes[k])(notes[k]??={text:'',tags:[]}).text=e.value}
function renderNote(t){const n=notes[nk(t)]||{text:'',tags:[]};$('note').innerHTML=`<label for="nt" class="mu">Note for this trade (saved in the snapshot folder)</label><textarea id="nt" onblur="saveNote()">${esc(n.text)}</textarea><div id="tg">${TAGS.map(g=>`<span class="chip ${n.tags.includes(g)?'on':''}" onclick="tagT('${g}')">${g}</span>`).join('')}<span style="flex:1"></span><label class="mu"><input type="checkbox" ${pn?'checked':''} onchange="pn=this.checked"> Print note in PNG footer</label><button onclick="saveNote()">Save note</button></div>`}
function tagT(g){const t=trades[sel],n=notes[nk(t)]??={text:'',tags:[]};n.text=$('nt').value;n.tags=n.tags.includes(g)?n.tags.filter(x=>x!=g):[...n.tags,g];renderNote(t);saveNote()}
async function saveNote(){const t=trades[sel];if(!t)return;syncNote();const k=nk(t),n=notes[k]||{text:'',tags:[]};if(!n.text&&!n.tags.length)delete notes[k];
 try{await fetch('/api/notes_set',{method:'POST',body:JSON.stringify({folder:csvName,key:k,note:n})});st('Note saved');const L=$('list'),y=L.scrollTop;renderList();L.scrollTop=y}catch(e){st(e.message,1)}}
const CP={'Green / Red':['#26a69a','#ef5350'],'Bright green / Red':['#00c853','#ff1744'],'Blue / Orange':['#2196f3','#ff9800'],'White / Blue':['#ffffff','#2962ff'],'Yellow / Purple':['#ffd54f','#ab47bc']};
let rq=0;
function applyColors(){try{localStorage.cc=$('cup').value+','+$('cdn').value}catch(e){}
 if(rq||!chart||sel<0)return;
 rq=requestAnimationFrame(()=>{rq=0;const t=trades[sel],d=t&&cache[t.et+'|'+t.xt];if(!d||!chart)return;
  const rg=chart.timeScale().getVisibleLogicalRange();
  try{draw(t,d);if(rg)chart.timeScale().setVisibleLogicalRange(rg)}catch(e){st('Colour change failed: '+e.message,1)}})}
$('cpre').innerHTML='<option value="">Custom</option>'+Object.keys(CP).map(k=>`<option>${k}</option>`).join('');
$('cpre').onchange=e=>{const p=CP[e.target.value];if(p){$('cup').value=p[0];$('cdn').value=p[1];applyColors()}};
$('cup').oninput=$('cdn').oninput=()=>{$('cpre').value='';applyColors()};
let sv=['#26a69a','#ef5350'];try{const x=(localStorage.cc||'').split(',');if(x.length==2)sv=x}catch(e){}
$('cup').value=sv[0];$('cdn').value=sv[1];
$('cpre').value=Object.keys(CP).find(k=>CP[k][0]==sv[0]&&CP[k][1]==sv[1])||'';
async function openCsv(x,name){csvName=name;try{load(x);notes=await(await fetch('/api/notes_get',{method:'POST',body:JSON.stringify({folder:csvName})})).json();if(notes.error)notes={};flt={res:'all',side:'all',day:'all',time:'all',size:'all',sort:'t'};sel=-1;renderList();st(trades.length+' trades loaded | Total net '+usd(trades.reduce((a,t)=>a+net(t),0))+' | '+tznote);syncReviews();if(trades.length)show(0);return true}catch(er){st(er.message,1);return false}}
let logNames=[];
function renderLogs(){$('slogm').innerHTML=logNames.length?logNames.map((n,i)=>`<div class="lr"><span class="ln" onclick="pickLog(${i})">${esc(n)}</span><span class="lx" title="Delete this saved file" onclick="delLog(${i})">&#10005;</span></div>`).join(''):'<div class="lr mu">Nothing saved yet</div>'}
async function loadLogs(){try{const r=await(await fetch('/api/log_list',{method:'POST',body:'{}'})).json();logNames=r.names||[];renderLogs()}catch(e){}}
$('slogb').onclick=e=>{e.stopPropagation();const m=$('slogm');m.style.display=m.style.display=='block'?'none':'block'};
$('slogm').onclick=e=>e.stopPropagation();document.addEventListener('click',()=>{$('slogm').style.display='none'});
async function pickLog(i){$('slogm').style.display='none';const n=logNames[i];if(needLogin()){st('Enter username and API key first.',1);return}
 const r=await(await fetch('/api/log_get',{method:'POST',body:JSON.stringify({name:n})})).json();if(r.csv)openCsv(r.csv,n);else st('Could not load that log',1)}
async function delLog(i){const n=logNames[i];if(!confirm('Delete "'+n+'" from Saved online?'))return;const nt=confirm('Also delete the notes saved for "'+n+'"?\nOK = delete the notes too, Cancel = keep the notes.');
 try{const r=await(await fetch('/api/log_del',{method:'POST',body:JSON.stringify({name:n,notes:nt})})).json();if(r.error)throw new Error(r.error);st('Deleted "'+n+'" from Saved online'+(nt?' (notes deleted too)':''));await loadLogs();$('slogm').style.display='block'}catch(e){st(e.message,1)}}
let toastT;function toast(m){const e=$('toast');e.textContent=m;e.style.display='block';clearTimeout(toastT);toastT=setTimeout(()=>e.style.display='none',12000)}
function tkeys(text){const r=csv(text);if(!r.length)return{hdr:[],rows:[],keys:[]};const H=r[0].map(x=>x.trim().toLowerCase()),
 f=[['enteredat','exitedat','entryprice','exitprice'],['boughttimestamp','soldtimestamp','buyprice','sellprice']].find(a=>a.every(n=>H.includes(n)));
 if(!f)return{hdr:r[0],rows:r.slice(1),keys:[]};const ix=f.map(n=>H.indexOf(n));
 return{hdr:r[0],rows:r.slice(1),keys:r.slice(1).map(w=>ix.map(i=>(w[i]||'').trim()).join('|'))}}
function toCsv(h,rows){const q=v=>/[",\n\r]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;return [h,...rows].map(r=>r.map(q).join(',')).join('\n')+'\n'}
async function smartOpen(x,name){const nw=tkeys(x);let best=null,text=x,target=name,msg='';
 if(nw.keys.length){try{await loadLogs();
  const olds=await Promise.all(logNames.map(async n=>({n,csv:(await(await fetch('/api/log_get',{method:'POST',body:JSON.stringify({name:n})})).json()).csv||''})));
  for(const o of olds){const t=tkeys(o.csv),set=new Set(t.keys),ov=nw.keys.filter(k=>set.has(k)).length;if(ov>0&&(!best||ov>best.ov||ov==best.ov&&o.n==name))best={n:o.n,ov,t}}
  if(!best){const sm=olds.find(o=>o.n==name);if(sm)best={n:name,ov:0,t:tkeys(sm.csv)}}}catch(e){}
  if(best){target=best.n;const lo=h=>JSON.stringify(h.map(v=>v.trim().toLowerCase()));
   if(best.t.keys.length&&lo(nw.hdr)==lo(best.t.hdr)){const have=new Set(nw.keys),keep=best.t.rows.filter((w,i)=>!have.has(best.t.keys[i]));text=toCsv(nw.hdr,[...keep,...nw.rows]);
    msg=`Updated saved log "${target}": ${nw.rows.length} trades from this file + ${keep.length} older trades kept.`}
   else msg=`Updated saved log "${target}" with this file.`;
   if(target!=name)msg=`This file has ${best.ov} trades already in "${target}", so I updated that one instead of adding a new file. `+msg}}
 const ok=await openCsv(text,target);
 if(ok&&trades.length){await fetch('/api/log_save',{method:'POST',body:JSON.stringify({name:target,csv:text})});await loadLogs();if(msg)toast(msg)}}
const needLogin=()=>!CLOUD&&(!$('u').value||!$('k').value);
if(CLOUD){$('u').parentNode.style.display='none';$('k').parentNode.style.display='none';$('lo').style.display='block'}
$('f').onchange=e=>{const f=e.target.files[0];if(!f)return;
 if(needLogin()){st('Enter username and API key first, then pick the file again.',1);e.target.value='';return}
 f.text().then(async x=>{await smartOpen(x,f.name.replace(/\.csv$/i,'')||'trades')})};
loadLogs();
</script></body></html>"""

if __name__ == "__main__":
    srv, port = None, 8765
    if os.environ.get("PORT") and not APP_PASSWORD:
        raise SystemExit("Set APP_PASSWORD before running online.")
    if CLOUD and not DB_URL:
        print("WARNING: no DATABASE_URL set - saved data will be lost when the server restarts.")
    for port in ([int(os.environ["PORT"])] if os.environ.get("PORT") else range(8765, 8776)):
        try:
            srv = ThreadingHTTPServer((HOST, port), H)
            break
        except OSError:
            print(f"Port {port} is busy (an older copy may still be running) - trying the next one...")
    if srv is None:
        raise SystemExit("No free port found. Close all black windows and try again.")
    url = f"http://{HOST}:{port}"
    print(f"MNQ Trade Charts v20 running at {url}  (close this window to stop)")
    print("Pictures are saved in:", ROOT)
    if not os.environ.get("PORT"):
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    srv.serve_forever()
