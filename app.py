"""trade-coach 本機網頁介面：http://localhost:7870

python app.py [--port 7870] ；資料庫可用環境變數 TRADE_COACH_DB 指定（預設 data/trades.db）。
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
import session_store
from journal import db

SESSIONS: dict[str, agent.Session] = {}
_LOCK = threading.Lock()
_SIDEBAR = {"t": 0, "data": None}
SIDEBAR_TTL = 300  # 秒
_SESSION_LOCKS: dict[str, threading.Lock] = {}


def _session_lock(sid: str) -> threading.Lock:
    """同一個 session 一次只處理一則訊息（避免連按兩次把 history 弄亂）。"""
    with _LOCK:
        return _SESSION_LOCKS.setdefault(sid, threading.Lock())


def session(sid: str | None) -> agent.Session:
    """記憶體裡沒有就從 data/sessions.db 還原（伺服器重啟後對話還在）。"""
    with _LOCK:
        if sid and sid in SESSIONS:
            return SESSIONS[sid]
        s = agent.Session(sid or None)
        if sid:
            try:
                s.restored = session_store.load_into(s, agent.db_mode())
            except Exception as e:  # noqa: BLE001
                print(f"[session] 還原失敗 {sid}: {e}", flush=True)
        SESSIONS[s.id] = s
        return s


def persist(s: agent.Session) -> None:
    try:
        session_store.save(s, agent.db_mode())
    except Exception as e:  # noqa: BLE001
        print(f"[session] 存檔失敗 {s.id}: {e}", flush=True)


def button_turn(s: agent.Session, label: str, reply: str, tool: str, out: dict) -> None:
    """按鈕（確認儲存 / 取消）也算一輪：寫進對話紀錄與 chat log。"""
    s.turns = getattr(s, "turns", 0) + 1
    s.history += [{"role": "user", "content": label}, {"role": "assistant", "content": reply}]
    res = {"reply": reply, "tool_calls": [{"name": tool, "args": {}, "routed": True}],
           "tool_outputs": [{"name": tool, "output": out}], "elapsed_s": 0.0}
    agent._log_turn(s, label, res, {"route": {"mode": "button", "intent": tool}, "warnings": []})


def sidebar(force: bool = False) -> dict:
    if not force and _SIDEBAR["data"] and time.time() - _SIDEBAR["t"] < SIDEBAR_TTL:
        return _SIDEBAR["data"]
    from tools import overview, stats
    try:
        ov = overview.market_overview(held=agent._held_tickers(), with_news=True)
    except Exception as e:
        ov = {"error": str(e)}
    try:
        st = stats.compute_stats()
        pos = st["open_positions"]
    except Exception as e:
        pos = [{"error": str(e)}]
    data = {"overview": ov, "positions": pos, "db": db.DB_PATH.name}
    _SIDEBAR.update(t=time.time(), data=data)
    return data


def model_status() -> dict:
    """網頁右上角／側欄的模型狀態：Ollama 目前載入哪些模型（/api/ps），以及這個程式正在等哪個模型。"""
    from tools import decision
    from tools._common import MODEL_STATUS, ollama_ps
    ps = ollama_ps()
    st = dict(MODEL_STATUS)
    loaded = {m["name"] for m in ps.get("models", [])}
    if st.get("since"):
        st["elapsed_s"] = round(time.time() - st["since"], 1)
    # 正在等的模型還沒出現在 /api/ps → Ollama 正在載入（或把另一個模型換下來）
    st["loading"] = st.get("state") == "running" and st.get("model") not in loaded
    last = st.pop("last", None)
    st.pop("since", None)
    return {"ollama": ps, "request": st, "last_call": last and {k: last.get(k) for k in
                                                                ("model", "purpose", "total_s", "load_s", "wait_s", "ok")},
            "chat_model": agent.MODEL, "judge_model": decision.JUDGE_MODEL}


EMBED_CSS = ("body.embed aside,body.embed #mstat,body.embed #dbname{display:none!important}"
             "body.embed header{padding:6px 10px}body.embed header b{font-size:14px}"
             "body.embed #log{padding:10px}body.embed form{padding:8px 10px}body.embed .msg{font-size:14px}"
             "body.embed textarea{height:44px}")
PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>trade-coach 交易教練</title>
<style>
:root{--bg:#0e1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--mut:#8b949e;--acc:#2f81f7;--up:#f85149;--dn:#3fb950}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 "Noto Sans TC","Microsoft JhengHei",sans-serif;display:flex;height:100vh}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center}
header b{font-size:17px}header span{color:var(--mut);font-size:13px}
#log{flex:1;overflow:auto;padding:18px}
.msg{max-width:860px;margin:0 0 14px;padding:10px 14px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.user{background:#1f6feb33;margin-left:auto;border:1px solid #1f6feb66}
.bot{background:var(--panel);border:1px solid var(--line)}
.tools{color:var(--mut);font-size:12px;margin-top:6px}
#pending{display:none;margin:0 18px 10px;padding:12px;border:1px solid #d29922;background:#d2992215;border-radius:10px}
#pending button{margin-right:8px}
form{display:flex;gap:8px;padding:12px 18px;border-top:1px solid var(--line)}
textarea{flex:1;resize:none;height:52px;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 16px;font:inherit;cursor:pointer}
button.sec{background:#30363d}button:disabled{opacity:.5}
aside{width:380px;border-left:1px solid var(--line);overflow:auto;padding:14px;background:#0d1117}
aside h3{margin:14px 0 6px;font-size:14px;color:var(--mut);font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:13px}
.card div{margin:2px 0}.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:3px 4px;border-bottom:1px solid var(--line);text-align:right}td:first-child,th:first-child{text-align:left}
#mstat{font-size:12px;color:var(--mut);display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
#mstat .pill{background:#21262d;border:1px solid var(--line);border-radius:12px;padding:1px 9px;white-space:nowrap}
#mstat .busy{border-color:#d29922;color:#e3b341}#mstat .load{border-color:#f85149;color:#ff7b72}#mstat .ok{border-color:#238636;color:#3fb950}
.mtag{display:inline-block;background:#21262d;border-radius:8px;padding:0 6px;margin-right:4px}
.chips span{display:inline-block;background:#21262d;border-radius:10px;padding:1px 8px;margin:2px;font-size:12px}
</style></head><body>
<div id="main">
<header><b>trade-coach 交易教練</b><span id="dbname"></span><span id="mstat" style="margin-left:auto"><span class="pill">模型狀態載入中…</span></span><button class="sec" onclick="newChat()" style="padding:4px 12px;font-size:13px">新對話</button></header>
<div id="log"></div>
<div id="pending"><div id="ptext"></div><div style="margin-top:8px"><button onclick="confirmTrade()">確認儲存</button><button class="sec" onclick="cancelTrade()">取消</button></div></div>
<form onsubmit="send(event)"><textarea id="q" placeholder="輸入訊息，Enter 送出（Shift+Enter 換行）"></textarea><button id="btn">送出</button></form>
</div>
<aside><b>模型狀態</b><div id="mcard" class="card" style="margin:6px 0 14px"><span class="mut">讀取中…</span></div>
<div style="display:flex;justify-content:space-between;align-items:center"><b>今日盤勢</b><button class="sec" onclick="loadSide(true)" style="padding:3px 10px;font-size:12px">重新整理</button></div>
<div id="side"><div class="mut">載入中…（第一次約 10–20 秒）</div></div></aside>
<script>
let sid=localStorage.getItem('tc_sid')||'';
const HELLO='嗨！可以問我「今天盤勢怎樣」「台指期夜盤」「費半連跌幾天了」，或直接說「今天531買了10股聯鈞 因為突破 停損483」記交易。';
async function restore(){$('log').innerHTML='';add('bot',HELLO);if(!sid)return;
 try{const r=await (await fetch('/api/session?session_id='+encodeURIComponent(sid))).json();
  if(!r.found)return;
  if(r.history.length)add('bot','（已還原先前的對話，共 '+r.turns+' 輪）');
  for(const m of r.history)add(m.role==='user'?'user':'bot',m.content);showPending(r.pending)}catch(e){}}
async function newChat(){const r=await post('/api/new',{});sid=r.session_id;localStorage.setItem('tc_sid',sid);$('log').innerHTML='';add('bot',HELLO);showPending(null)}
const $=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function linkify(h){return h.replace(/https?:\/\/[^\s<"']+/g,u=>`<a href="${u}" target="_blank" rel="noopener noreferrer">連結</a>`)}
function add(cls,text,extra){const d=document.createElement('div');d.className='msg '+cls;d.innerHTML=(cls==='bot'?linkify(esc(text)):esc(text))+(extra?'<div class="tools">'+esc(extra)+'</div>':'');$('log').appendChild(d);$('log').scrollTop=1e9;return d}
function showPending(p){if(!p){$('pending').style.display='none';return}
 const f=k=>p[k]==null||p[k]===''?'<span class="mut">（未填）</span>':esc(Array.isArray(p[k])?p[k].join(', '):p[k]);
 $('ptext').innerHTML=`<b>待確認交易</b>：${f('trade_date')} ${p.side==='buy'?'買進':p.side==='sell'?'賣出':f('side')} ${f('ticker')} ${f('name')} @ ${f('price')} × ${f('shares')} 股<br>停損 ${f('stop_loss')}｜目標 ${f('target_prices')}｜理由 ${f('reason')}`+
 (p.missing&&p.missing.length?`<br><span class="mut">缺：${esc(p.missing.join(', '))}</span>`:'')+(p.warnings&&p.warnings.length?`<br><span class="mut">注意：${esc(p.warnings.join('；'))}</span>`:'');
 $('pending').style.display='block'}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json()}
async function send(e){e&&e.preventDefault();const q=$('q').value.trim();if(!q)return;$('q').value='';add('user',q);
 const wait=add('bot','思考中…');$('btn').disabled=true;
 try{const r=await post('/api/chat',{session_id:sid,message:q});sid=r.session_id;localStorage.setItem('tc_sid',sid);
  wait.remove();const bm=add('bot',r.reply||r.error,footer(r));if(r.chart){const im=document.createElement('img');im.src=r.chart;im.style.cssText='max-width:100%;margin-top:8px;border-radius:6px';bm.appendChild(im);$('log').scrollTop=1e9}showPending(r.pending);pollModel();
  if(r.tools&&r.tools.some(t=>t==='confirm_trade'))loadSide(true)}
 catch(err){wait.remove();add('bot','錯誤：'+err)}finally{$('btn').disabled=false}}
function footer(r){const ms=(r.models||[]).map(c=>`${c.model}（${c.purpose||''} ${c.total_s??'?'} 秒${c.load_s>=1?'，含載入 '+c.load_s+' 秒':''}）`);
 return '模型：'+(ms.length?ms.join('、'):(r.model_used||'僅程式（未用模型）'))+'｜'+(r.tools&&r.tools.length?'工具：'+r.tools.join(', ')+'｜':'')+'共 '+(r.elapsed_s??0)+' 秒'}
function fmtS(s){if(s==null)return '';if(s>86400)return '常駐';if(s<0)return '即將卸載';return s>=60?Math.floor(s/60)+' 分 '+(s%60)+' 秒後卸載':s+' 秒後卸載'}
async function pollModel(){try{const r=await (await fetch('/api/model_status')).json();const q=r.request||{},ms=(r.ollama&&r.ollama.models)||[];
 let pills='';
 if(q.state==='waiting')pills+=`<span class="pill busy">⏳ 排隊等 ${esc(q.model)}（${esc(q.purpose)}）${q.elapsed_s??''} 秒</span>`;
 else if(q.state==='running')pills+=q.loading?`<span class="pill load">⇄ 載入／切換到 ${esc(q.model)}…${q.elapsed_s??''} 秒</span>`:`<span class="pill busy">⚙ ${esc(q.model)} 回答中（${esc(q.purpose)}）${q.elapsed_s??''} 秒</span>`;
 if(!r.ollama||!r.ollama.ok)pills+=`<span class="pill load">Ollama 連不上</span>`;
 else if(!ms.length)pills+=`<span class="pill">顯示卡上沒有載入模型（下次提問會先載入）</span>`;
 else pills+=ms.map(m=>`<span class="pill ok">● ${esc(m.name)} ${m.vram_gb} GB</span>`).join('');
 $('mstat').innerHTML=pills;
 let c=ms.length?ms.map(m=>`<div>● <b>${esc(m.name)}</b> 已載入｜顯示卡 ${m.vram_gb} GB${m.size_gb>m.vram_gb+0.1?'（部分在 CPU）':''}｜${fmtS(m.unload_in_s)}</div>`).join(''):'<div class="mut">目前沒有載入任何模型</div>';
 if(q.state&&q.state!=='idle')c+=`<div>${q.state==='waiting'?'⏳ 排隊中':q.loading?'⇄ 正在載入／切換模型':'⚙ 回答中'}：${esc(q.model)}（${esc(q.purpose)}）${q.elapsed_s??''} 秒</div>`;
 const l=r.last_call;if(l)c+=`<div class="mut">上一次：${esc(l.model)}（${esc(l.purpose)}）${l.total_s} 秒${l.load_s>=1?'，含載入 '+l.load_s+' 秒':''}${l.ok===false?'，失敗':''}</div>`;
 c+=`<div class="mut">對話／路由：${esc(r.chat_model)}；決策判斷：${esc(r.judge_model)}（12GB 顯卡一次只放得下一個，會自動切換）</div>`;
 $('mcard').innerHTML=c}catch(e){$('mstat').innerHTML='<span class="pill load">狀態讀取失敗</span>'}}
setInterval(pollModel,3000);
async function confirmTrade(){const r=await post('/api/confirm',{session_id:sid});add('bot',r.reply||(r.saved?`已儲存（id ${r.trade_ids}）`:'沒有存：'+(r.reason||'')));showPending(r.pending);if(r.saved)loadSide(true)}
async function cancelTrade(){const r=await post('/api/cancel',{session_id:sid});add('bot','已取消草稿');showPending(null)}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
function cls(v){return v>0?'up':v<0?'dn':''}
function sg(v,d=2){return v==null?'—':(v>0?'+':'')+Number(v).toLocaleString(undefined,{maximumFractionDigits:d})}
async function loadSide(force){$('side').innerHTML='<div class="mut">載入中…</div>';
 const r=await (await fetch('/api/sidebar'+(force?'?force=1':''))).json();$('dbname').textContent='資料庫：'+r.db;
 const o=r.overview||{};let h='';const t=o['加權指數'];
 if(t)h+=`<h3>加權指數</h3><div class="card"><div style="font-size:20px">${Number(t.close).toLocaleString()} <span class="${cls(t.change)}">${sg(t.change)}（${sg(t.change_pct)}%）</span></div><div class="mut">${t.data_type==='即時'?'🟢 即時':'⚪ 收盤資料（非即時）'}｜截至 ${esc(t.as_of)}｜${esc(t.source)}</div></div>`;
 const i=o['三大法人現貨'];if(i)h+=`<h3>三大法人（億）${esc(i.date)}</h3><div class="card">`+['外資','投信','自營商','合計'].map(k=>`<div>${k} <span class="${cls(i[k])}">${sg(i[k])}</span></div>`).join('')+'</div>';
 const f=o['台指期'];if(f){const n=f['夜盤'];h+=`<h3>台指期 ${esc(f.contract)}</h3><div class="card"><div>日盤 ${esc(f['日盤'].date)}：${Number(f['日盤'].close).toLocaleString()} <span class="${cls(f['日盤'].change)}">${sg(f['日盤'].change,0)}</span></div>`+
  (f['即時']?`<div>${esc(f['即時'].label)} ${Number(f['即時'].price).toLocaleString()} <span class="${cls(f['即時'].vs_日盤收盤)}">比日盤 ${sg(f['即時'].vs_日盤收盤,0)}</span><div class="mut">報價 ${esc(f['即時'].quote_time)}${f['即時'].trading_day?'｜算 '+esc(f['即時'].trading_day)+' 交易日':''}</div></div>`:'')+
  (n&&typeof n==='object'?`<div>${esc(n.label)}（${esc(n.session_end||'')} 收）：${Number(n.close).toLocaleString()} <span class="${cls(n['vs_日盤收盤'])}">比日盤 ${sg(n['vs_日盤收盤'],0)}</span></div>`:'')+
  (f['夜盤狀態']&&!(f['即時']&&f.session_now==='night')?`<div class="mut">${esc(f['夜盤狀態'])}</div>`:'')+`<div class="mut">${f.data_type==='即時'?'🟢 即時':'⚪ 收盤資料（非即時）'}｜${esc(f.source)}｜目前：${esc(f.session_now)}</div></div>`}
 const fi=o['期貨三大法人未平倉'];if(fi)h+=`<h3>期貨未平倉（口）${esc(fi.date)}</h3><div class="card">`+['外資及陸資','投信','自營商'].filter(k=>fi[k]).map(k=>`<div>${k} ${sg(fi[k]['未平倉淨額'],0)} <span class="mut">(${sg(fi[k]['增減'],0)})</span></div>`).join('')+'</div>';
 const u=o['美股'];if(u)h+=`<h3>美股 ${esc(u['指數'][0]&&u['指數'][0].date)}</h3><div class="card">`+u['指數'].map(x=>`<div>${esc(x.name)} ${Number(x.close).toLocaleString()} <span class="${cls(x.change)}">${sg(x.change_pct)}%</span></div>`).join('')+`<div class="mut">${u.source==='yfinance_live'?'🟡 盤中（Yahoo，可能延遲）':'⚪ 收盤資料（非即時）'}｜截至 ${esc(u.as_of)}｜${esc(u.source)}</div></div>`;
 const nw=o['新聞熱度'];if(nw)h+=`<h3>新聞熱度</h3><div class="card chips">`+nw['熱門關鍵字'].map(k=>`<span>${esc(k)}</span>`).join('')+'<div style="margin-top:6px">'+nw['熱門個股'].slice(0,6).map(k=>`<span>${esc(k)}</span>`).join('')+'</div></div>';
 if(o.warnings&&o.warnings.length)h+=`<div class="mut" style="margin-top:8px">⚠ ${esc(o.warnings.join(', '))}（未設定永豐金鑰時使用每日公開資料）</div>`;
 const p=r.positions||[];h+='<h3>持股</h3><div class="card">'+(p.length?'<table><tr><th>股票</th><th>成本</th><th>現價</th><th>損益</th></tr>'+p.map(x=>`<tr><td>${esc(x.ticker)} ${esc(x.name||'')}</td><td>${x.buy_price??''}</td><td>${x.last_price??'—'}</td><td class="${cls(x.unrealized_pnl)}">${sg(x.unrealized_pnl,0)}${x.below_stop_now?' ⚠破停損':''}</td></tr>`).join('')+'</table>':'<span class="mut">沒有持股</span>')+'</div>';
 h+=`<div class="mut" style="margin-top:10px">更新：${esc(o.as_of)}</div>`;$('side').innerHTML=h}
restore();loadSide(false);pollModel();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html") or self.path.startswith("/embed") or "embed=1" in self.path:
            page = PAGE
            if self.path.startswith("/embed") or "embed=1" in self.path:   # 嵌入用：只有訊息和輸入框
                page = PAGE.replace("<body>", '<body class="embed">', 1).replace("</style>", EMBED_CSS + "</style>", 1)
            b = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith("/api/sidebar"):
            self._json(sidebar(force="force=1" in self.path))
        elif self.path.startswith("/api/model_status"):
            self._json(model_status())
        elif self.path.startswith("/charts/"):   # P3-I：K 線圖（只給 data/charts 底下的 png）
            name = os.path.basename(urlparse(self.path).path)
            p = Path(__file__).resolve().parent / "data" / "charts" / name
            if not name.endswith(".png") or not p.is_file():
                self._json({"error": "not found"}, 404)
                return
            b = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/health":
            self._json({"ok": True, "db": db.DB_PATH.name, "db_mode": agent.db_mode(), "model": agent.MODEL,
                        "app": "trade-coach", "pid": os.getpid()})
        elif self.path.startswith("/api/session"):
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get("session_id") or [""])[0]
            if not sid:
                self._json({"found": False})
                return
            s = session(sid)
            found = bool(s.history) or bool(getattr(s, "restored", False))
            self._json({"found": found, "session_id": s.id, "turns": getattr(s, "turns", 0),
                        "history": s.history, "pending": s.pending})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/new":
                s = session(None)
                persist(s)
                self._json({"session_id": s.id})
                return
            s = session(body.get("session_id"))
            if self.path == "/api/chat":
                with _session_lock(s.id):
                    r = agent.chat(s, (body.get("message") or "").strip())
                    persist(s)
                self._json({"session_id": s.id, "reply": r["reply"], "pending": r["pending"],
                            "tools": [c["name"] for c in r["tool_calls"]], "elapsed_s": r["elapsed_s"],
                            "model_used": r.get("model_used"), "models": r.get("models") or [], "chart": r.get("chart")})
            elif self.path == "/api/confirm":
                with _session_lock(s.id):
                    r = agent.confirm_pending(s)
                    reply = f"已儲存，交易編號 {r['trade_ids']}。" if r.get("saved") else f"沒有存檔：{r.get('reason')}"
                    button_turn(s, "（按下「確認儲存」）", reply, "confirm_trade", r)
                    persist(s)
                self._json({**r, "reply": reply, "session_id": s.id, "pending": s.pending})
            elif self.path == "/api/cancel":
                with _session_lock(s.id):
                    had = bool(s.pending)
                    s.pending = None
                    if had:
                        button_turn(s, "（按下「取消」）", "已取消這筆草稿。", "cancel_trade", {"cancelled": True})
                    persist(s)
                self._json({"cancelled": True, "session_id": s.id})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 會讓兩個程式同時綁同一個埠（舊的那個繼續接請求）→ 關掉，讓第二個直接啟動失敗
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    try:
        srv = Server((a.host, a.port), H)
    except OSError as e:
        print(f"埠 {a.port} 已被佔用（可能已有 trade-coach 在跑）：{e}\n"
              f"請先關掉舊的，或用 .\\run.ps1（會自動停掉舊的 trade-coach）。", flush=True)
        raise SystemExit(1)
    if a.host == "127.0.0.1":   # step11：也聽 IPv6 迴環 ::1（只有本機），localhost 不再多等約 2 秒
        try:
            import socket
            import threading

            class Server6(Server):
                address_family = socket.AF_INET6
            srv6 = Server6(("::1", a.port), H)
            threading.Thread(target=srv6.serve_forever, daemon=True).start()
        except OSError as e:
            print(f"（::1 沒有綁成功，localhost 可能較慢：{e}）", flush=True)
    try:   # step12：背景先載入共用 embedding 模型，新聞檢索第一題不用等
        from tools import embed
        embed.warmup_async()
    except Exception:  # noqa: BLE001
        pass
    print(f"trade-coach: http://localhost:{a.port}  pid={os.getpid()}  (db: {db.DB_PATH}, "
          f"sessions: {session_store.store_path(agent.db_mode()).name}, logs: data/chat_logs)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
