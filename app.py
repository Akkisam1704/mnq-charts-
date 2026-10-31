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


def log_api(path, b):
    if path == "/api/log_save":
        kv_set("log:" + clean(b["name"]), b["csv"])
        return {"ok": True}
    if path == "/api/log_list":
        return {"names": [k[4:] for k in kv_keys("log:")]}
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


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>MNQ Trade Charts v13</title>
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
<label>Saved online<select id="slog"><option value="">Choose a saved log...</option></select></label>
<label>Trade log CSV<input id="f" type="file" accept=".csv"></label>
<a id="lo" href="/logout" style="display:none;color:#9aa3b8;font-size:12px;align-self:center;margin-left:auto">Log out</a><div id="ver">v13</div></header>
<main><div id="left"><div id="flt"></div><div id="list"><div class="it">Enter login, then choose your CSV.</div></div></div>
<div id="right"><div id="bar"></div><div id="st"></div><div id="chart"></div><div id="det"></div><div id="note"></div><div id="sum" style="display:none"></div></div></main>
<script>
const CLOUD=__CLOUD__;
const $=id=>document.getElementById(id);
const TFS={'1m':1,'5m':5,'15m':15,'30m':30,'1h':60},PAD={'1m':[90,30],'5m':[80,25],'15m':[70,20],'30m':[60,15],'1h':[50,12]};
let notes={},pn=true,sumOn=false,flt={res:'all',side:'all',day:'all',time:'all',sort:'t'},cs,csvName='trades',nS=0,nR=0,lastDir='',lastFile='',days={},tznote='',trades=[],sel=-1,tf='5m',chart,cache={};
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
 tznote='CSV timezone: '+best.tz+(best.n?` (WARNING: ${best.n} trades fall in market-closed hours)`:'');return best}

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
 $('bar').innerHTML='<button onclick="showSummary()">Summary</button>'+Object.keys(TFS).map(k=>`<button class="${k==tf?'on':''}" onclick="show(sel,'${k}')">${k}</button>`).join('')+'<button onclick="dl1()">Save this PNG</button><button onclick="dl5(sel)">Save all 5 timeframes</button><button onclick="dlAll()">Save ALL trades</button>'+(CLOUD?'':'<button onclick="openF()">Open snapshot folder</button>')}

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

async function show(i,k){if(i<0)return;syncNote();sumOn=false;$('sum').style.display='none';$('chart').style.display='block';$('det').style.display='grid';$('note').style.display='flex';sel=i;if(k)tf=k;renderList();st('Loading data…');if(chart){chart.remove();chart=null;cs=null}$('det').innerHTML='';
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
function view(){const o=[];trades.forEach((t,i)=>{if(flt.res=='w'&&!win(t)||flt.res=='l'&&win(t)||flt.side!='all'&&(flt.side=='L')!=t.long||flt.day!='all'&&t.day!=flt.day||flt.time!='all'&&bucket(t)!=flt.time)return;o.push(i)});
 return o.sort({t:(a,b)=>a-b,l:(a,b)=>net(trades[a])-net(trades[b]),w:(a,b)=>net(trades[b])-net(trades[a])}[flt.sort])}
function renderFlt(){const v=view(),sm=v.reduce((a,i)=>a+net(trades[i]),0),mk=(id,op,cur)=>`<select onchange="setF('${id}',this.value)">${op.map(o=>`<option value="${o[0]}" ${o[0]==cur?'selected':''}>${o[1]}</option>`).join('')}</select>`;
 $('flt').innerHTML=`<div style="display:flex;gap:6px">${[['all','All'],['w','Winners'],['l','Losers']].map(o=>`<span class="chip ${flt.res==o[0]?'on':''}" onclick="setF('res','${o[0]}')">${o[1]}</span>`).join('')}</div><div class="fg">${mk('side',[['all','Any side'],['L','Long'],['S','Short']],flt.side)}${mk('day',[['all','All days'],...Object.keys(days).sort().map(d=>[d,dlab(d)])],flt.day)}${mk('time',[['all','Any time (NY)'],...TB.map(x=>[x,x])],flt.time)}${mk('sort',[['t','By time'],['l','Biggest loss'],['w','Biggest win']],flt.sort)}</div><div class="mu">Showing ${v.length} of ${trades.length} | net <b class="${sm>=0?'w':'l'}">${usd(sm)}</b></div><button onclick="dlAll()">Save these ${v.length} as PNGs</button>`}
function setF(k,v){flt[k]=v;renderList();if(sumOn)showSummary()}
function gotoDay(d){flt.day=d;renderList();const v=view();if(v.length)show(v[0])}
function stats(v){const a=v.map(i=>trades[i]),nt=a.map(net),W=nt.filter(x=>x>0),L=nt.filter(x=>x<0),sm=x=>x.reduce((p,q)=>p+q,0),bd={},bt={};
 a.forEach(t=>{for(const [M,k] of [[bd,t.day],[bt,bucket(t)]]){const o=M[k]??={n:0,w:0,s:0};o.n++;o.s+=net(t);if(net(t)>0)o.w++}});
 return {n:a.length,net:sm(nt),wr:a.length?W.length/a.length:0,aw:W.length?sm(W)/W.length:0,al:L.length?sm(L)/L.length:0,pf:L.length?sm(W)/Math.abs(sm(L)):NaN,byDay:bd,byT:bt}}
function showSummary(){sumOn=true;syncNote();const v=view(),S=stats(v),pc=x=>(x*100).toFixed(0)+'%',D=Object.entries(S.byDay).sort(),bs=D.slice().sort((a,b)=>b[1].s-a[1].s),
 mx=Math.max(1,...D.map(x=>Math.abs(x[1].s)),...Object.values(S.byT).map(x=>Math.abs(x.s))),card=(a,b,c)=>`<div class="c2"><span class="mu">${a}</span><b class="${c||''}" style="font-size:22px">${b}</b></div>`,
 row=(lab,o,ck)=>`<div class="sr" ${ck?`onclick="${ck}"`:''}><span style="width:150px">${lab}</span><span class="mu" style="width:80px">${o.n} trades</span><span class="mu" style="width:80px">Win ${pc(o.w/o.n)}</span><span style="flex:1"><span style="display:block;height:12px;width:${Math.abs(o.s)/mx*100}%;background:${o.s>=0?'#26a69a':'#ef5350'};border-radius:3px"></span></span><b class="${o.s>=0?'w':'l'}" style="width:100px;text-align:right">${usd(o.s)}</b></div>`;
 ['chart','det','note'].forEach(x=>$(x).style.display='none');$('sum').style.display='block';
 $('sum').innerHTML=`<h2 style="margin:0">Summary</h2><div class="mu">${S.n} trades currently shown in the list (use the filters on the left to change this)</div><div class="cg">${card('Net',usd(S.net),S.net>=0?'w':'l')}${card('Win rate',pc(S.wr))}${card('Trades',S.n)}${card('Avg win',usd(S.aw),'w')}${card('Avg loss',usd(S.al),'l')}${card('Profit factor',isFinite(S.pf)?S.pf.toFixed(2):'-')}${card('Best day',bs.length?usd(bs[0][1].s):'-','w')}${card('Worst day',bs.length?usd(bs[bs.length-1][1].s):'-','l')}</div>
 <div class="c2" style="padding:0"><div class="mu" style="padding:10px 12px">Day-end net (click a day to open its trades)</div>${D.map(([d,o])=>row(dlab(d),o,`gotoDay('${d}')`)).join('')}</div>
 <div class="c2" style="padding:0;margin-top:12px"><div class="mu" style="padding:10px 12px">By time of day (New York time)</div>${TB.filter(b=>S.byT[b]).map(b=>row(b,S.byT[b])).join('')}</div>
 <div style="margin:12px 0;display:flex;gap:8px"><button onclick="sumPng()">Save summary as PNG</button><button onclick="sumCsv()">Export day table (CSV)</button></div>`;st('Summary view | click any trade or a day to go back to the chart')}
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
async function openCsv(x,name){csvName=name;try{load(x);notes=await(await fetch('/api/notes_get',{method:'POST',body:JSON.stringify({folder:csvName})})).json();if(notes.error)notes={};flt={res:'all',side:'all',day:'all',time:'all',sort:'t'};sel=-1;renderList();st(trades.length+' trades loaded | Total net '+usd(trades.reduce((a,t)=>a+net(t),0))+' | '+tznote);if(trades.length)show(0)}catch(er){st(er.message,1)}}
async function loadLogs(){try{const r=await(await fetch('/api/log_list',{method:'POST',body:'{}'})).json();$('slog').innerHTML='<option value="">Choose a saved log...</option>'+(r.names||[]).map(n=>`<option>${esc(n)}</option>`).join('')}catch(e){}}
const needLogin=()=>!CLOUD&&(!$('u').value||!$('k').value);
if(CLOUD){$('u').parentNode.style.display='none';$('k').parentNode.style.display='none';$('lo').style.display='block'}
$('f').onchange=e=>{const f=e.target.files[0];if(!f)return;
 if(needLogin()){st('Enter username and API key first, then pick the file again.',1);e.target.value='';return}
 f.text().then(async x=>{await openCsv(x,f.name.replace(/\.csv$/i,'')||'trades');fetch('/api/log_save',{method:'POST',body:JSON.stringify({name:csvName,csv:x})}).then(loadLogs)})};
$('slog').onchange=async e=>{const n=e.target.value;if(!n)return;if(needLogin()){st('Enter username and API key first.',1);return}
 const r=await(await fetch('/api/log_get',{method:'POST',body:JSON.stringify({name:n})})).json();if(r.csv)openCsv(r.csv,n);else st('Could not load that log',1)};
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
    print(f"MNQ Trade Charts v13 running at {url}  (close this window to stop)")
    print("Pictures are saved in:", ROOT)
    if not os.environ.get("PORT"):
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    srv.serve_forever()
