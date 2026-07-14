"""Web monitor — the TUI monitor as a lightweight web page (stdlib only).

Serves on MONITOR_PORT (default 8899, bind 0.0.0.0 — publish it via compose):
    /            the dashboard (auto-refreshing single page)
    /api/state   the JSON snapshot it renders

Same read-only telemetry the TUI reads (/workspace files) — no hooks into the
bot. Optional access gate: set MONITOR_TOKEN in the workspace .env, then open
/?token=<value> once (stored in a cookie).
"""
import glob
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import monitor as m  # reuse the TUI's collectors + name cache

PORT = int(os.environ.get("MONITOR_PORT", "8899"))
TOKEN = os.environ.get("MONITOR_TOKEN", "").strip()
DONE_KEEP = int(os.environ.get("WORKER_DONE_RETENTION", str(24 * 3600)))
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
_SLACK_UID = re.compile(r"^[UW][A-Z0-9]{6,}$")


def _slack_api(method, **params):
    # Form-encoded, NOT json: Slack's read methods silently ignore JSON bodies.
    r = httpx.post(f"https://slack.com/api/{method}",
                   headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                   data=params, timeout=6)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("error"))
    return d


def slack_user(uid):
    """Slack uid → display name, cached in the shared name cache."""
    k = "su:" + uid
    if k not in m._names:
        try:
            u = _slack_api("users.info", user=uid)["user"]
            m._names[k] = (u.get("profile", {}).get("display_name")
                           or u.get("real_name") or u.get("name") or uid)
        except Exception:
            return uid  # don't cache failures (scope may get fixed later)
        m._save_names()
    return m._names[k]


def slack_channel(cid):
    """Slack channel id → human label, cached."""
    k = "sc:" + cid
    if k not in m._names:
        try:
            c = _slack_api("conversations.info", channel=cid)["channel"]
            if c.get("is_im"):
                m._names[k] = "Slack DM · " + slack_user(c.get("user") or "")
            else:
                m._names[k] = "Slack · #" + (c.get("name") or cid)
        except Exception:
            return "Slack · " + cid[:9]
        m._save_names()
    return m._names[k]


def _loc(channel, server=None):
    ch = str(channel or "")
    if ch.startswith("slack:"):
        ch = ch[6:]
    if ch and not ch.isdigit():  # Slack id (C…/D…/G…)
        return slack_channel(ch)
    cname, gid = m.channel_info(ch)
    g = server or gid
    return f"{m.guild_name(g)} · #{cname}" if g else f"#{cname}"


def _who(name):
    s = str(name or "")
    return slack_user(s) if _SLACK_UID.match(s) else s


def _heartbeat_series(cols=64, span=900, win=75):
    rows = m._usage()
    now = time.time()
    step = span / cols
    series = [0.0] * cols
    tot_tok = tot_cost = 0.0
    for r in rows:
        ts = r.get("ts") or 0
        if ts < now - span - win or ts > now + 1:
            continue
        tok = (r.get("in_tokens") or 0) + (r.get("out_tokens") or 0)
        cost = r.get("cost_usd") or 0.0
        if now - ts < span:
            tot_tok += tok
            tot_cost += cost
        rate = (tok if tok else cost * 5000) / (win / 60.0)
        half = win / 2.0
        lo, hi = int((ts - half - (now - span)) / step), int((ts + half - (now - span)) / step)
        for j in range(max(0, lo), min(cols, hi + 1)):
            w = 1.0 - abs((now - span) + (j + 0.5) * step - ts) / half
            if w > 0:
                series[j] += rate * w
    return series, tot_tok, tot_cost


def state():
    now = time.time()
    runs = m._runs()
    usage = m._usage()
    today = [r for r in usage if now - (r.get("ts") or 0) < 86400]
    try:
        lim = float(open(os.path.join(m.WORKSPACE, ".limited_until")).read().strip())
    except Exception:
        lim = 0.0
    deferred = sum(len(glob.glob(os.path.join(m.WORKSPACE, d, "*.json")))
                   for d in (".deferred", ".deferred_slack"))
    reports = len(glob.glob(os.path.join(m.WORKSPACE, ".worker_reports", "*.json")))

    agents = [{
        "pid": d.get("pid"), "phase": d.get("phase"), "user": _who(d.get("user")),
        "where": _loc(d.get("channel"), d.get("server")),
        "elapsed": int(now - (d.get("start") or now)),
        "thinking": (d.get("thinking") or "")[-500:],
    } for d in runs]

    tasks = []
    for s in m.subagents():
        if not s["running"]:
            started = s.get("started") or 0
            if now - started > DONE_KEEP:
                continue
        ch = s.get("channel")
        tasks.append({
            "name": s["name"], "status": s["status"], "running": s["running"],
            "where": _loc(ch) if ch else "—",
            "age": int(now - (s.get("started") or now)),
            "note": s.get("note") or "",
        })

    recent = [{
        "where": _loc(r.get("channel")), "who": _who(r.get("author")),
        "cost": round(r.get("cost_usd") or 0, 2), "turns": r.get("turns"),
        "dur": (r.get("duration_ms") or 0) // 1000,
        "ago": int(now - (r.get("ts") or now)),
    } for r in list(reversed(usage))[:10]]

    series, tok15, cost15 = _heartbeat_series()
    return {
        "ts": now,
        "workers": {"busy": len(runs), "total": m.WORKERS},
        "sessions": len(m._json(m.SESSIONS_FILE, {})),
        "limited_until": lim if lim > now else 0,
        "deferred": deferred, "pending_reports": reports,
        "today": {"runs": len(today), "cost": round(sum(r.get("cost_usd") or 0 for r in today), 2)},
        "agents": agents, "tasks": tasks, "recent": recent,
        "heartbeat": {"series": [round(v) for v in series], "tok15": int(tok15),
                      "cost15": round(cost15, 2)},
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mochi monitor</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0}
body{background:#151a24;color:#d6dbe5;font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;padding:14px}
h1{font-size:15px;color:#7fd4c1;margin-bottom:10px;display:flex;justify-content:space-between;align-items:baseline}
h1 small{color:#5a6273;font-weight:normal}
.grid{display:grid;gap:10px;grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:820px){.grid{grid-template-columns:minmax(0,1fr)}.wide{grid-column:auto !important}}
.wide{grid-column:1/-1}
.card{background:#1b2230;border:1px solid #2a3347;border-radius:8px;padding:10px 12px;min-width:0}
.card h2{font-size:12px;color:#8b93a7;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.kv{display:flex;flex-wrap:wrap;gap:6px 18px}
.kv b{color:#fff;font-weight:600}
.ok{color:#69d58c}.warn{color:#e8b34b}.bad{color:#ef6a6a}.dim{color:#5a6273}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td,th{padding:3px 6px;text-align:left;vertical-align:top;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
th{color:#5a6273;font-weight:normal;font-size:12px}
tr+tr td{border-top:1px solid #232c3e}
td.wrap{white-space:normal}
.right{text-align:right}
.agent{border-left:3px solid #e8b34b;padding:6px 10px;margin:6px 0;background:#1f2735;border-radius:4px}
.agent .meta{color:#7fa3d4;font-size:12px}
.agent .think{color:#707a8e;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:70px;overflow:hidden}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
svg{display:block;width:100%;height:70px}
.footer{margin-top:10px;color:#3f4757;font-size:11px}
</style></head><body>
<h1>🍡 Mochi monitor <small id="clock"></small></h1>
<div class="grid">
 <div class="card wide"><h2>overview</h2><div class="kv" id="ov"></div></div>
 <div class="card wide" id="agentsCard"><h2>active agents</h2><div id="agents"></div></div>
 <div class="card"><h2>tasks (workers)</h2><table id="tasks"></table></div>
 <div class="card"><h2>recent runs</h2><table id="recent"></table></div>
 <div class="card wide"><h2>token usage — last 15 min <span class="dim" id="hbsum"></span></h2>
   <svg id="hb" viewBox="0 0 640 70" preserveAspectRatio="none"></svg></div>
</div>
<div class="footer">read-only · refreshes every 3s · /api/state</div>
<script>
const qs=new URLSearchParams(location.search);
if(qs.get('token')){document.cookie='mtok='+qs.get('token')+';path=/;max-age=31536000';history.replaceState(null,'',location.pathname)}
function fmtAge(s){if(s<90)return s+'s';if(s<5400)return Math.round(s/60)+'m';return (s/3600).toFixed(1)+'h'}
async function tick(){
 let r;try{r=await fetch('/api/state');if(!r.ok)throw 0}catch(e){document.getElementById('clock').textContent='disconnected';return}
 const s=await r.json();
 document.getElementById('clock').textContent=new Date(s.ts*1000).toLocaleTimeString();
 const lim=s.limited_until?`<span class="bad">limited → ${new Date(s.limited_until*1000).toLocaleTimeString()}</span>`:'<span class="ok">ok</span>';
 document.getElementById('ov').innerHTML=
  `<span>workers <b>${s.workers.busy}/${s.workers.total}</b></span>`+
  `<span>rate-limit ${lim}</span>`+
  `<span>sessions <b>${s.sessions}</b></span>`+
  `<span>deferred <b class="${s.deferred?'warn':''}">${s.deferred}</b></span>`+
  `<span>pending reports <b class="${s.pending_reports?'warn':''}">${s.pending_reports}</b></span>`+
  `<span>today <b>${s.today.runs}</b> runs · <b>$${s.today.cost}</b></span>`;
 document.getElementById('agents').innerHTML=s.agents.length?s.agents.map(a=>
  `<div class="agent"><div><b>${a.phase||'?'}</b> <span class="dim">${fmtAge(a.elapsed)}</span>
   <div class="meta">${a.where} · ${a.user||''} · pid ${a.pid}</div>
   <div class="think">${(a.thinking||'…').replace(/</g,'&lt;')}</div></div></div>`).join('')
  :'<span class="dim">idle — no agents running</span>';
 document.getElementById('tasks').innerHTML='<colgroup><col style="width:16px"><col><col style="width:34%"><col style="width:52px"></colgroup>'+
  '<tr><th></th><th>task</th><th>state</th><th class="right">age</th></tr>'+
  (s.tasks.length?s.tasks.map(t=>{
   const c=t.running?'#69d58c':(t.status.includes('exit 0')||t.status==='done'?'#5a6273':'#ef6a6a');
   return `<tr><td><span class="dot" style="background:${c}"></span></td>
    <td class="wrap"><b>${t.name}</b><br><span class="dim">${t.note.slice(0,80)||''}</span></td>
    <td class="wrap">${t.status}<br><span class="dim">${t.where}</span></td>
    <td class="right">${fmtAge(t.age)}</td></tr>`}).join('')
   :'<tr><td></td><td class="dim">(none)</td></tr>');
 document.getElementById('recent').innerHTML='<colgroup><col style="width:44%"><col><col style="width:56px"><col style="width:52px"><col style="width:52px"></colgroup>'+
  '<tr><th>where</th><th>who</th><th class="right">$</th><th class="right">dur</th><th class="right">ago</th></tr>'+
  s.recent.map(r=>`<tr><td title="${r.where}">${r.where}</td><td title="${r.who||''}">${r.who||''}</td><td class="right">${r.cost}</td>
   <td class="right">${r.dur}s</td><td class="right">${fmtAge(r.ago)}</td></tr>`).join('');
 const v=s.heartbeat.series,mx=Math.max(...v,1),W=640,H=70,n=v.length;
 let pts=v.map((y,i)=>`${(i/(n-1)*W).toFixed(1)},${(H-2-(y/mx)*(H-6)).toFixed(1)}`).join(' ');
 document.getElementById('hb').innerHTML=
  `<polygon points="0,${H} ${pts} ${W},${H}" fill="#7fd4c122"/><polyline points="${pts}" fill="none" stroke="#7fd4c1" stroke-width="1.5"/>`;
 document.getElementById('hbsum').textContent=` · Σ${s.heartbeat.tok15.toLocaleString()} tok · $${s.heartbeat.cost15} · peak ${Math.round(mx).toLocaleString()}/min`;
}
tick();setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        if not TOKEN:
            return True
        if f"token={TOKEN}" in (self.path.split("?", 1) + [""])[1]:
            return True
        return f"mtok={TOKEN}" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden (set ?token=...)")
            return
        if path == "/api/state":
            try:
                body = json.dumps(state()).encode()
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass  # quiet


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[webmon] serving on :{PORT}" + (" (token required)" if TOKEN else ""), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
