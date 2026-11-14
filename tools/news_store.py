"""新聞庫（P1-D）＋時間衰減檢索與新聞證據（P1-E）。

ingest()：抓現有來源（Yahoo RSS、鉅亨、Google News；外加決策紀錄裡最近問過的股票）→ 正規化 → 去重 → 標註股票 → 存 SQLite
         → （有 sentence-transformers 時）算 MiniLM embedding 存進同一列，並重建 FAISS 索引 data/news.faiss。
去重三層：1) 網址（去掉追蹤參數）hash；2) 完整標題正規化 hash（不是只取前 40 字）；
         3) 近似標題：字元 3-gram Jaccard ≥ 0.8（同一事件不同媒體的標題），有 embedding 時再加 cosine ≥ 0.9。
classify_pending()：用 qwen2.5:7b（format=json）把還沒分類的標題批次標成 利多／利空／中性 × 事件類型，結果存回資料庫（快取，不重算）。
                   對話時不呼叫模型：沒分類到的用關鍵字規則（rule_sentiment）。
retrieve(tg)：score = sim × exp(-Δ小時/48) × 股票命中權重（標註命中 1.0、只有標題含名稱 0.6），取前 5 則 → N1…N5 證據。

用法：python tools/news_store.py ingest [--classify] [--hours 72]
      python tools/news_store.py search 聯鈞
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._common import DATA, TPE, iso_tpe, now_tpe  # noqa: E402

TAU_H = 48.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT, url_hash TEXT UNIQUE,
    title       TEXT NOT NULL, title_norm TEXT, title_hash TEXT,
    published   TEXT,              -- 台北時間 ISO；沒有時間的用抓取時間並標 time_guessed=1
    time_guessed INTEGER DEFAULT 0,
    fetched     TEXT,
    feed        TEXT, publisher TEXT, summary TEXT,
    tickers     TEXT,              -- JSON：["3450", "^SOX", "NVDA"]
    dup_of      INTEGER,           -- 近似重複時指向第一則
    sentiment   TEXT, event TEXT, sent_by TEXT,   -- 利多/利空/中性、事件類型、qwen2.5:7b 或 rule
    emb         BLOB               -- float32 × 384（MiniLM，已正規化）
);
CREATE INDEX IF NOT EXISTS ix_news_pub ON news(published);
CREATE INDEX IF NOT EXISTS ix_news_th ON news(title_hash);
"""


def db_path() -> Path:
    p = os.environ.get("TRADE_COACH_NEWS_DB")
    if p:
        return Path(p)
    if "test" in Path(os.environ.get("TRADE_COACH_DB", "")).name.lower():
        return DATA / "test_news.db"
    return DATA / "news.db"


def connect(path=None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)   # step12：內文欄位與 chunks 表
    return con


# ---------- 正規化與去重 ----------
_TRACK = re.compile(r"^(utm_|fbclid|gclid|ref|from|cid|oc)$", re.I)


def norm_url(u: str | None) -> str:
    if not u:
        return ""
    p = urlparse(u.strip())
    if "news.google.com" in p.netloc:   # Google News 轉址網址本身就是唯一 id
        return urlunparse(p._replace(query="", fragment=""))
    q = [(k, v) for k, v in parse_qsl(p.query) if not _TRACK.match(k)]
    return urlunparse(p._replace(query=urlencode(q), fragment="", netloc=p.netloc.lower()))


def norm_title(t: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{1,30}$", "", t or "")          # 「 - 媒體名」後綴
    t = re.sub(r"[【\[].{0,12}?[】\]]", "", t)                 # 【焦點】［獨家］之類的標籤
    return re.sub(r"[\W_]+", "", t).lower()


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _grams(s: str, n: int = 3) -> set[str]:
    return {s[i:i + n] for i in range(max(1, len(s) - n + 1))}


def jaccard(a: str, b: str) -> float:
    A, B = _grams(a), _grams(b)
    return len(A & B) / len(A | B) if A and B else 0.0


# ---------- 標註股票 ----------
_US_TICKER = re.compile(r"(?<![A-Za-z])\$?([A-Z]{2,5})(?![A-Za-z])")


def tag_tickers(text: str) -> list[str]:
    out = set()
    try:
        from tools.resolve import _tw_codes_in
        out |= _tw_codes_in(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        from tools import aliases
        out |= aliases.symbols_in(text)
    except Exception:  # noqa: BLE001
        from tools.history import US_ALIASES
        out |= {s for s, ws in US_ALIASES.items() if any(w in text for w in ws if len(w) >= 2)}
    return sorted(out)


# ---------- 情緒／事件 ----------
_POS = re.compile(r"漲停|大漲|飆|噴出|創(新|歷史)?高|獲利(成長|大增|創高)|營收(創|年增|月增|成長)|接單|大單|訂單(湧|滿|增)|上修|調升|"
                  r"買進評等|目標價(上調|調升)|擴產|受惠|看好|轉盈|轉虧為盈|超預期|優於預期|加碼|回購|配息(增|創)")
_NEG = re.compile(r"跌停|大跌|重挫|暴跌|崩|下修|調降|虧損|衰退|減產|裁員|利空|砍單|降評|警示|處置|罰|訴訟|不如預期|低於預期|"
                  r"目標價(下調|調降)|賣超|出脫|減碼|違約|停工|跌破|工安|死亡|\d+死|火災|爆炸|斷鏈|召回|調查|制裁|禁令|下市")
_EVENTS = [("財報", r"財報|EPS|季報|年報|獲利|毛利"), ("營收", r"營收"), ("法說", r"法說|股東會"), ("訂單", r"訂單|接單|大單|砍單"),
           ("評等", r"評等|目標價|升評|降評"), ("股利", r"股利|配息|除息|除權"), ("併購", r"併購|收購|合併"),
           ("產能", r"擴產|產能|新廠|減產"), ("籌碼", r"外資|投信|自營|買超|賣超"), ("價格", r"漲停|跌停|大漲|大跌|重挫|創高|飆")]


def rule_sentiment(title: str) -> tuple[str, str]:
    p, n = bool(_POS.search(title)), bool(_NEG.search(title))
    s = "利多" if p and not n else "利空" if n and not p else "中性"
    ev = next((k for k, pat in _EVENTS if re.search(pat, title)), "其他")
    return s, ev


# ---------- embedding（可選）----------
_ST = None


def _embed(texts: list[str]):
    """step12：改用共用的 tools/embed.py（整個程式只載入一份模型）。"""
    from tools import embed
    return embed.encode_safe(texts)


def _has_st() -> bool:
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


# ---------- 寫入 ----------
def _to_iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        return (t if t.tzinfo else t.replace(tzinfo=TPE)).astimezone(TPE).isoformat(timespec="minutes")
    try:
        return dt.datetime.fromisoformat(str(t)).astimezone(TPE).isoformat(timespec="minutes")
    except Exception:  # noqa: BLE001
        return None


def store(items: list[dict], embed: bool = False, con=None) -> dict:
    """items：[{title, link, published|time, publisher, feed, summary}]。回傳 {new, dup_url, dup_title, near_dup}。"""
    own = con is None
    con = con or connect()
    st = {"new": 0, "dup_url": 0, "dup_title": 0, "near_dup": 0}
    since = (now_tpe() - dt.timedelta(days=3)).isoformat(timespec="minutes")
    recent = [(r["id"], r["title_norm"]) for r in con.execute(
        "SELECT id, title_norm FROM news WHERE dup_of IS NULL AND published >= ?", (since,))]
    new_ids = []
    for it in items:
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()
        if not title:
            continue
        url = norm_url(it.get("link"))
        uh = _h(url) if url else _h("title:" + title)
        tn = norm_title(title)
        th = _h(tn)
        if con.execute("SELECT 1 FROM news WHERE url_hash=?", (uh,)).fetchone():
            st["dup_url"] += 1
            continue
        if con.execute("SELECT 1 FROM news WHERE title_hash=?", (th,)).fetchone():
            st["dup_title"] += 1
            continue
        dup_of = next((i for i, t in recent if jaccard(tn, t) >= 0.8), None)
        pub = _to_iso(it.get("published") or it.get("time"))
        s, ev = rule_sentiment(title)
        cur = con.execute(
            "INSERT INTO news(url,url_hash,title,title_norm,title_hash,published,time_guessed,fetched,feed,publisher,summary,"
            "tickers,dup_of,sentiment,event,sent_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, uh, title, tn, th, pub or iso_tpe(), int(pub is None), iso_tpe(), it.get("feed"), it.get("publisher"),
             (it.get("summary") or "")[:300], json.dumps(tag_tickers(title + " " + (it.get("summary") or "")[:200])),
             dup_of, s, ev, "rule"))
        if dup_of:
            st["near_dup"] += 1
        else:
            recent.append((cur.lastrowid, tn))
            st["new"] += 1
        new_ids.append(cur.lastrowid)
    con.commit()
    if embed and new_ids:
        _embed_rows(con, new_ids)
    if own:
        con.close()
    return st


def _embed_rows(con, ids: list[int]) -> int:
    rows = con.execute(f"SELECT id, title FROM news WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    v = _embed([r["title"] for r in rows])
    if v is None:
        return 0
    import numpy as np
    n = 0
    for r, e in zip(rows, v):
        # 語意近似去重：和最近 3 天 cosine ≥ 0.9 的標成重複
        con.execute("UPDATE news SET emb=? WHERE id=?", (e.astype("float32").tobytes(), r["id"]))
        n += 1
    since = (now_tpe() - dt.timedelta(days=3)).isoformat(timespec="minutes")
    old = con.execute("SELECT id, emb FROM news WHERE emb IS NOT NULL AND dup_of IS NULL AND published >= ? AND id NOT IN (%s)"
                      % ",".join("?" * len(ids)), [since, *ids]).fetchall()
    if old:
        M = np.stack([np.frombuffer(o["emb"], dtype="float32") for o in old])
        for r, e in zip(rows, v):
            sims = M @ e
            j = int(sims.argmax())
            if sims[j] >= 0.9:
                con.execute("UPDATE news SET dup_of=COALESCE(dup_of, ?) WHERE id=?", (old[j]["id"], r["id"]))
    con.commit()
    return n


def build_faiss(con=None) -> int:
    """把所有有 embedding 的新聞建成 FAISS（IndexFlatIP，向量已正規化 = cosine）。"""
    try:
        import faiss
        import numpy as np
    except Exception:  # noqa: BLE001
        return 0
    con = con or connect()
    rows = con.execute("SELECT id, emb FROM news WHERE emb IS NOT NULL").fetchall()
    if not rows:
        return 0
    idx = faiss.IndexIDMap(faiss.IndexFlatIP(384))
    idx.add_with_ids(np.stack([np.frombuffer(r["emb"], dtype="float32") for r in rows]),
                     np.array([r["id"] for r in rows], dtype="int64"))
    faiss.write_index(idx, str(db_path().with_suffix(".faiss")))
    return len(rows)


def _ingest(hours: int = 72, classify: bool = False, extra_queries: list[str] | None = None,
            bodies: bool = True, body_limit: int = 400, classify_limit: int = 200) -> dict:
    from tools import news
    from tools.explain import _gnews_items
    res = news.fetch_news(hours=hours)
    items = list(res["items"])
    qs = list(extra_queries or [])
    try:   # 最近 30 天問過的股票也抓一下個股新聞
        from tools import decision_log
        c = decision_log.connect()
        since = (now_tpe() - dt.timedelta(days=30)).isoformat()
        qs += [r["name"] for r in c.execute("SELECT DISTINCT name FROM decision_log WHERE kind='stock' AND ts >= ? AND name IS NOT NULL", (since,))]
    except Exception:  # noqa: BLE001
        pass
    for q in dict.fromkeys(qs):
        for it in _gnews_items(f"{q} when:3d"):
            items.append({**it, "feed": f"gnews:{q}"})
    con = connect()
    st = store(items, embed=_has_st(), con=con)
    st["faiss"] = build_faiss(con)
    st["status"] = res.get("status")
    if bodies:     # step12：抓內文
        st["bodies"] = fetch_bodies(limit=body_limit, con=con)
    if _has_st():  # step12：切塊 → embed → 重建 chunk FAISS
        st["chunks"] = embed_chunks(con=con)
        st["chunk_faiss"] = build_chunk_faiss(con)
    if classify:
        st["classified"] = classify_pending(limit=classify_limit, con=con)
    st["total_rows"] = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    return st


def ingest(*a, **kw) -> dict:
    """排程入口：同一時間只跑一個 ingest（data/news_ingest.lock，2 小時內有效），記總耗時。"""
    import time
    lock = DATA / "news_ingest.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 7200:
        return {"skipped": "另一個 ingest 正在跑（data/news_ingest.lock）"}
    lock.write_text(str(os.getpid()), encoding="utf-8")
    t0 = time.time()
    try:
        st = _ingest(*a, **kw)
    finally:
        lock.unlink(missing_ok=True)
    st["elapsed_s"] = round(time.time() - t0, 1)
    return st


def classify_titles(limit: int = 200, batch: int = 20, con=None) -> int:
    """7B 分類：只處理還是 rule 標的，結果存回（快取）。失敗的保留規則結果。"""
    from tools._common import ollama_chat
    con = con or connect()
    rows = con.execute("SELECT id, title FROM news WHERE sent_by='rule' AND dup_of IS NULL AND (body_status IS NULL OR body_status!='ok') "
                       "ORDER BY published DESC LIMIT ?",
                       (limit,)).fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        lines = "\n".join(f"{k}. {r['title']}" for k, r in enumerate(part, 1))
        prompt = ("判斷每則財經新聞標題對「標題裡的公司或市場」是利多、利空還是中性，並分類事件。\n"
                  "事件只能是：財報、營收、法說、訂單、評等、股利、併購、產能、籌碼、價格、政策、其他。\n"
                  '輸出 JSON：{"items":[{"n":1,"sentiment":"利多|利空|中性","event":"..."}]}。不確定就給中性。\n' + lines)
        try:
            r = ollama_chat({"model": os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b"), "stream": False, "format": "json",
                             "options": {"temperature": 0, "num_ctx": 4096},
                             "messages": [{"role": "user", "content": prompt}]}, "新聞分類", timeout=180)
            got = json.loads(r["message"]["content"]).get("items") or []
        except Exception:  # noqa: BLE001
            continue
        for g in got:
            try:
                row = part[int(g.get("n")) - 1]
            except Exception:  # noqa: BLE001
                continue
            s = g.get("sentiment") if g.get("sentiment") in ("利多", "利空", "中性") else None
            if s:
                con.execute("UPDATE news SET sentiment=?, event=?, sent_by=? WHERE id=?",
                            (s, str(g.get("event") or "其他")[:8], "qwen2.5:7b", row["id"]))
                done += 1
        con.commit()
    return done


# ---------- 內文抓取（step12）：標題常常聳動，判讀要看內文 ----------
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
_PAYWALL = re.compile(r"訂閱(會員|方案|後|即可)|付費(會員|文章|閱讀|解鎖)|登入(後|會員)?(閱讀|觀看)|加入會員|subscribe to (read|continue)|paywall", re.I)
_NEW_COLS = {"body": "TEXT", "body_status": "TEXT", "body_url": "TEXT", "body_fetched": "TEXT", "body_tickers": "TEXT",
             "magnitude": "INTEGER", "exaggerated": "INTEGER", "sent_reason": "TEXT", "sent_quote": "TEXT"}
CHUNK_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id INTEGER NOT NULL,
    idx INTEGER, kind TEXT,          -- kind：body（內文切塊）／title（抓不到內文時用標題＋摘要）
    text TEXT, emb BLOB              -- float32 × 384，已正規化
);
CREATE INDEX IF NOT EXISTS ix_chunks_news ON chunks(news_id);
"""


def _migrate(con) -> None:
    con.executescript(CHUNK_SCHEMA)
    con.executescript(TICKER_SCHEMA)   # step13
    have = {r[1] for r in con.execute("PRAGMA table_info(news)")}
    for c, t in _NEW_COLS.items():
        if c not in have:
            con.execute(f"ALTER TABLE news ADD COLUMN {c} {t}")
    con.commit()


def chunk_faiss_path() -> Path:
    return db_path().with_suffix(".chunks.faiss")


def _host(url: str | None) -> str:
    return urlparse(url or "").netloc.lower()


def resolve_url(url: str) -> tuple[str | None, str]:
    """Google News 的 /rss/articles/… 是加密過的轉址，要先解回原始網址才抓得到內文。"""
    if "news.google.com" in _host(url):
        from googlenewsdecoder import gnewsdecoder
        d = gnewsdecoder(url)
        if d.get("success") or d.get("status"):
            return d.get("decoded_url"), "gnews_decoded"
        return None, "gnews_decode_failed"
    return url, "direct"


def fetch_body(url: str, timeout=(5, 12)) -> tuple[str, str | None, str]:
    """抓一篇內文。回傳 (status, 實際網址, 內文)。status：ok／paywall／short／http_4xx／error:…／gnews_decode_failed"""
    import requests
    import trafilatura
    try:
        real, how = resolve_url(url)
    except Exception as e:  # noqa: BLE001
        return f"error:decode_{type(e).__name__}", None, ""
    if not real:
        return how, None, ""
    try:
        r = requests.get(real, timeout=timeout, headers=UA, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return f"error:{type(e).__name__}", real, ""
    if r.status_code >= 400:
        return f"http_{r.status_code}", real, ""
    try:
        body = trafilatura.extract(r.content, include_comments=False, include_tables=False, favor_precision=True) or ""
    except Exception as e:  # noqa: BLE001
        return f"error:extract_{type(e).__name__}", r.url, ""
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) < 150:     # 抽不到正文：多半是付費牆或 JS 才載入的頁面
        return ("paywall" if _PAYWALL.search(r.text[:300000]) else "short"), r.url, body
    if len(body) < 600 and _PAYWALL.search(body[-300:]):
        return "paywall", r.url, body
    return "ok", r.url, body[:12000]


def fetch_bodies(limit: int = 400, con=None, min_interval: float = 1.0, workers: int = 6) -> dict:
    """還沒抓過內文的新聞逐篇抓。同一個來源網站一次只開一條線、兩次請求至少隔 min_interval 秒（禮貌限速）；不同網站平行。
    失敗不重試（body_status 記原因，判讀退回看標題／關鍵字）。"""
    import threading
    import time
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    con = con or connect()
    rows = [dict(r) for r in con.execute(
        "SELECT id, url FROM news WHERE body_status IS NULL AND dup_of IS NULL AND url IS NOT NULL AND url != '' "
        "ORDER BY published DESC LIMIT ?", (limit,))]
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(_host(r["url"]), []).append(r)
    results, lock, t0 = [], threading.Lock(), time.time()

    def work(host, items):
        last, s = 0.0, {"ok": 0, "failed": 0, "reasons": Counter(), "secs": 0.0, "final_hosts": Counter()}
        for it in items:
            w = min_interval - (time.time() - last)
            if w > 0:
                time.sleep(w)
            last = a = time.time()
            st, real, body = fetch_body(it["url"])
            s["secs"] += time.time() - a
            s["ok" if st == "ok" else "failed"] += 1
            if st != "ok":
                s["reasons"][st] += 1
            if real:
                s["final_hosts"][_host(real)] += 1
            with lock:
                results.append((it["id"], st, real, body))
        return host, s

    with ThreadPoolExecutor(max(1, min(workers, len(groups) or 1))) as ex:
        per = dict(ex.map(lambda kv: work(*kv), groups.items()))
    for nid, st, real, body in results:
        bt = json.dumps(tag_tickers(body[:3000])) if st == "ok" else None
        con.execute("UPDATE news SET body=?, body_status=?, body_url=?, body_fetched=?, body_tickers=? WHERE id=?",
                    (body or None, st, real, iso_tpe(), bt, nid))
    con.commit()
    by_source = {h: {"ok": s["ok"], "failed": s["failed"], "fail_reasons": dict(s["reasons"]),
                     "avg_s": round(s["secs"] / max(1, s["ok"] + s["failed"]), 2),
                     "top_final_hosts": dict(s["final_hosts"].most_common(5))} for h, s in per.items()}
    return {"tried": len(rows), "ok": sum(1 for r in results if r[1] == "ok"),
            "failed": sum(1 for r in results if r[1] != "ok"), "elapsed_s": round(time.time() - t0, 1), "by_source": by_source}


# ---------- 切塊（chunking）----------
CHUNK_CHARS, CHUNK_OVERLAP, CHUNK_MAX_TOKENS = 300, 60, 384


_BOILER = re.compile(r"投資一定有風險|基金投資有賺有賠|申購前應詳閱|延伸閱讀|免責聲明|版權所有|未經(授權|同意)[^。]{0,6}不得|"
                     r"更多[^。．\n]{0,12}(新聞|報導)|本文(僅供參考|不代表)|責任編輯")


def strip_boilerplate(body: str) -> str:
    """trafilatura 偶爾會把頁尾（投資警語、「更多 XX 報導」、延伸閱讀）留在正文。從後半段第一個頁尾標記處截斷，
    否則這些和任何財經問題都「很像」的段落會在向量檢索裡排第一。"""
    cut = [m.start() for m in _BOILER.finditer(body or "") if m.start() >= len(body) * 0.4]
    return (body[:min(cut)] if cut else body or "").strip()


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """內文切成約 size 字的塊：先照句號／換行斷句再湊滿，避免把一句話切兩半；
    相鄰兩塊重疊 overlap 字（上一塊的結尾接在下一塊開頭），跨塊的事實（「營收…」在上一塊、「年增 35%」在下一塊）才不會被切斷。"""
    text = re.sub(r"[ \t\u3000]+", " ", text or "").strip()
    if not text:
        return []
    sents = [s.strip() for s in re.split(r"(?<=[。！？!?；])|\n+", text) if s and s.strip()]
    parts = []
    for s in sents:              # 單句比 size 還長就硬切（也保留重疊）
        while len(s) > size:
            parts.append(s[:size])
            s = s[size - overlap:]
        parts.append(s)
    chunks, cur = [], ""
    for s in parts:
        if cur and len(cur) + len(s) > size:
            chunks.append(cur)
            cur = cur[-overlap:] + s if overlap else s
        else:
            cur += s
    if cur:
        chunks.append(cur)
    return chunks


def embed_chunks(con=None, limit: int = 2000) -> dict:
    """內文抓到的：切塊後 embed；抓不到的：用「標題＋摘要」當一塊（標記 kind=title）。內文後來才抓到的會把 title 塊換掉。"""
    from tools import embed
    con = con or connect()
    need_body = [dict(r) for r in con.execute(
        "SELECT id, title, body FROM news n WHERE body_status='ok' AND dup_of IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.news_id=n.id AND c.kind='body') ORDER BY published DESC LIMIT ?", (limit,))]
    need_title = [dict(r) for r in con.execute(
        "SELECT id, title, summary FROM news n WHERE (body_status IS NULL OR body_status!='ok') AND dup_of IS NULL AND NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.news_id=n.id) ORDER BY published DESC LIMIT ?", (limit,))]
    todo = []
    for r in need_body:
        con.execute("DELETE FROM chunks WHERE news_id=?", (r["id"],))
        todo += [(r["id"], i, "body", c) for i, c in enumerate(chunk_text(strip_boilerplate(r["body"])))]
    for r in need_title:
        todo.append((r["id"], 0, "title", (r["title"] + " " + (r.get("summary") or "")[:200]).strip()))
    if not todo:
        return {"body_articles": 0, "title_articles": 0, "chunks": 0}
    v = embed.encode([t[3] for t in todo], max_len=CHUNK_MAX_TOKENS)
    con.executemany("INSERT INTO chunks(news_id, idx, kind, text, emb) VALUES(?,?,?,?,?)",
                    [(a, b, c, d, e.tobytes()) for (a, b, c, d), e in zip(todo, v)])
    con.commit()
    return {"body_articles": len(need_body), "title_articles": len(need_title), "chunks": len(todo)}


def rechunk_all(con=None) -> dict:
    """切塊規則改了以後：刪掉全部 chunk、重新切塊 embed、重建索引（向量重算，約 1 分鐘）。"""
    con = con or connect()
    con.execute("DELETE FROM chunks")
    con.commit()
    st = embed_chunks(con=con, limit=100000)
    st["chunk_faiss"] = build_chunk_faiss(con)
    return st


def build_chunk_faiss(con=None) -> int:
    """所有 chunk 建一個 FAISS（IndexIDMap(IndexFlatIP)，id = chunks.id）。每次 ingest 從 SQLite 全量重建（向量已存庫，不用重算）。"""
    import faiss
    import numpy as np
    con = con or connect()
    rows = con.execute("SELECT c.id, c.emb FROM chunks c JOIN news n ON n.id=c.news_id WHERE c.emb IS NOT NULL AND n.dup_of IS NULL").fetchall()
    if not rows:
        return 0
    idx = faiss.IndexIDMap(faiss.IndexFlatIP(384))
    idx.add_with_ids(np.stack([np.frombuffer(r["emb"], dtype="float32") for r in rows]),
                     np.array([r["id"] for r in rows], dtype="int64"))
    faiss.write_index(idx, str(chunk_faiss_path()))
    return len(rows)


# ---------- 內文判讀（7B，排程跑，對話時不跑）----------
_EVENT_SET = ("財報", "營收", "法說", "訂單", "評等", "股利", "併購", "產能", "籌碼", "價格", "政策", "其他")
BODY_JUDGE_PROMPT = """你是財經新聞判讀員。只根據「內文」的事實判斷這則新聞對文中主要公司或市場是利多、利空還是中性；標題只拿來比對有沒有誇大。
規則：
- sentiment 只能是：利多、利空、中性。內文沒有實質影響（排行榜、單純描述股價、一般活動報導）給中性。
- event 只能是：財報、營收、法說、訂單、評等、股利、併購、產能、籌碼、價格、政策、其他。
- magnitude：1＝輕微、2＝中等、3＝重大。依內文的實際數字和事實判斷，不看標題用詞。
- title_exaggerated：標題用詞（暴跌、崩盤、噴出、慘、狂、爆…）明顯比內文事實誇張時為 true，否則 false。
- reason：一句 30 字內的中文，說明判斷根據。
- quote：從內文「原封不動」複製 10～40 字支持判斷的句子。
只輸出 JSON：{{"sentiment":"…","event":"…","magnitude":1,"title_exaggerated":false,"reason":"…","quote":"…"}}

標題：{title}
內文：
{body}"""


def _nows(s: str) -> str:
    return re.sub(r"[\s「」『』\"'“”]+", "", s or "")


def validate_judgment(j: dict, body: str) -> tuple[dict | None, str]:
    """檢查 7B 的 JSON：欄位合法、quote 真的出現在內文（有根據）。回傳 (整理後的結果, 狀態)。"""
    if not isinstance(j, dict) or j.get("sentiment") not in ("利多", "利空", "中性"):
        return None, "bad_json"
    ev = j.get("event") if j.get("event") in _EVENT_SET else "其他"
    try:
        mag = min(3, max(1, int(j.get("magnitude"))))
    except Exception:  # noqa: BLE001
        mag = 1
    ex = j.get("title_exaggerated")
    ex = ex if isinstance(ex, bool) else str(ex).strip().lower() in ("true", "1", "是", "yes")
    q, b = _nows(str(j.get("quote") or "")), _nows(body)
    grounded = len(q) >= 6 and (q in b or (len(q) >= 10 and any(q[i:i + 10] in b for i in range(0, len(q) - 9))))
    out = {"sentiment": j["sentiment"], "event": ev, "magnitude": mag, "exaggerated": bool(ex),
           "reason": str(j.get("reason") or "").strip()[:60], "quote": str(j.get("quote") or "").strip()[:80]}
    return out, ("ok" if grounded else "ungrounded")


def judge_body(title: str, body: str) -> tuple[dict | None, str]:
    from tools._common import ollama_chat
    msgs = [{"role": "user", "content": BODY_JUDGE_PROMPT.format(title=title, body=body[:1800])}]
    st = "error"
    for attempt in range(2):
        try:
            r = ollama_chat({"model": os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b"), "stream": False, "format": "json",
                             "options": {"temperature": 0, "num_ctx": 4096}, "messages": msgs}, "新聞內文判讀", timeout=180)
            res, st = validate_judgment(json.loads(r["message"]["content"]), body)
        except Exception as e:  # noqa: BLE001
            res, st = None, f"error:{type(e).__name__}"
        if st == "ok":
            return res, st
        msgs = msgs + [{"role": "user", "content": "quote 必須是內文裡一字不差的句子，sentiment／event 要用規定的值。請重新輸出 JSON。"}]
    return res, st


def classify_bodies(limit: int = 150, con=None) -> dict:
    """有內文、還沒被 7B 讀過內文的新聞逐篇判讀，結果存回（快取）。找不到內文依據（ungrounded）的不採用，保留關鍵字結果並標記。"""
    con = con or connect()
    rows = con.execute("SELECT id, title, body FROM news WHERE body_status='ok' AND dup_of IS NULL AND "
                       "(sent_by IS NULL OR sent_by NOT LIKE '%/body%') ORDER BY published DESC LIMIT ?", (limit,)).fetchall()
    st = {"tried": len(rows), "ok": 0, "ungrounded": 0, "error": 0, "exaggerated": 0}
    model = os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b")
    for r in rows:
        res, s = judge_body(r["title"], r["body"])
        if s == "ok":
            con.execute("UPDATE news SET sentiment=?, event=?, magnitude=?, exaggerated=?, sent_reason=?, sent_quote=?, sent_by=? WHERE id=?",
                        (res["sentiment"], res["event"], res["magnitude"], int(res["exaggerated"]), res["reason"], res["quote"],
                         f"{model}/body", r["id"]))
            st["ok"] += 1
            st["exaggerated"] += int(res["exaggerated"])
        elif s == "ungrounded":
            sr, ev = rule_sentiment(r["title"])
            con.execute("UPDATE news SET sentiment=?, event=?, sent_by='rule/body_ungrounded', sent_reason=? WHERE id=?",
                        (sr, ev, "7B 判讀找不到內文依據，未採用", r["id"]))
            st["ungrounded"] += 1
        else:
            st["error"] += 1   # 下次排程再試
        con.commit()
    return st


# ---------- 針對個股的情緒（aspect-based sentiment，step13）----------
# 一篇新聞常常同時講好幾檔：「聯鈞接大單、華通遭砍單」。整篇文章只給一個情緒，對其中某一檔就會錯。
# 所以對每一個 (文章, 股票) 配對，只把「有提到這檔的段落」給 7B，問它「對這一檔」是利多還是利空。
TICKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_sent (
    news_id INTEGER NOT NULL, ticker TEXT NOT NULL, name TEXT,
    sentiment TEXT, event TEXT, magnitude INTEGER, exaggerated INTEGER, reason TEXT, quote TEXT,
    status TEXT,          -- ok／ungrounded／no_mention／error:…
    judged_by TEXT, judged_at TEXT, n_chunks INTEGER,
    PRIMARY KEY (news_id, ticker)
);
CREATE INDEX IF NOT EXISTS ix_ts_ticker ON ticker_sent(ticker);
"""
TICKER_PROMPT = """你是財經新聞判讀員。下面是一篇新聞裡「有提到 {name}（{code}）的段落」。
只根據這些段落，判斷這則新聞對「{name}」這一檔本身是利多、利空還是中性。文中其他公司的好壞消息不算在 {name} 身上。
判斷優先順序（很重要）：
1. 先找「實質事實」：外資／投信／自營商買超或賣超（張數）、接單／訂單／砍單、營收（月增、年增、衰退）、財報、法說、產能、評等調整。
2. 只出現在名單、排行榜、表格裡的數字（例如「EPS 前十名」「供應鏈名單」「概念股有某某」）是順帶一提，不是實質消息。
3. 段落裡同時有名單和實質事實時，一律依實質事實判斷，quote 也要引用實質事實那句。
4. 外資或投信大幅賣超、被砍單、營收衰退 → 利空；大幅買超、接到新訂單、營收創新高 → 利多。
規則：
- sentiment 只能是：利多、利空、中性。{name} 只是被列在名單裡、沒有實質消息時給中性。
- event 只能是：財報、營收、法說、訂單、評等、股利、併購、產能、籌碼、價格、政策、其他。
- magnitude：1＝輕微、2＝中等、3＝重大（對 {name} 的影響，依段落裡的數字和事實）。
- title_exaggerated：標題對 {name} 的描述明顯比段落事實誇張時為 true，否則 false。
- reason：一句 30 字內的中文，說明對 {name} 的判斷根據。
- quote：從段落「原封不動」複製 10～40 字、跟 {name} 有關的句子。
只輸出 JSON：{{"ticker":"{code}","sentiment":"…","event":"…","magnitude":1,"title_exaggerated":false,"reason":"…","quote":"…"}}

標題：{title}
段落：
{chunks}"""


def ticker_name(sym: str) -> str:
    try:
        if re.fullmatch(r"\d{4,6}[A-Z]?", sym):
            from tools.resolve import tw_companies
            return (tw_companies().get(sym) or {}).get("name") or sym
        from tools import aliases
        t = aliases.table()
        cjk = lambda ws: next((w for w in ws if re.search(r"[\u4e00-\u9fff]", w)), None)  # noqa: E731
        if sym in (t.get("indices") or {}):
            return t["indices"][sym].get("name") or sym
        hit = cjk((t.get("us_tickers") or {}).get(sym, []))
        if hit:
            return hit
        from tools.history import US_ALIASES
        return cjk(US_ALIASES.get(sym, [])) or sym
    except Exception:  # noqa: BLE001
        return sym


def watch_tickers(days: int = 30) -> set[str]:
    """最近 days 天在 decision_log 被問過的股票：這些就算只在內文被提到，也值得逐檔判讀。"""
    try:
        from tools import decision_log
        c = decision_log.connect()
        since = (now_tpe() - dt.timedelta(days=days)).isoformat()
        return {r[0] for r in c.execute("SELECT DISTINCT symbol FROM decision_log WHERE ts >= ? AND symbol IS NOT NULL", (since,))}
    except Exception:  # noqa: BLE001
        return set()


def ticker_pairs(con, days: int = 7, watch: set[str] | None = None, body_only: bool = False) -> list[tuple]:
    """要判讀的 (news_id, ticker) 配對，優先順序：關注清單 > 標題標註 > （body_only=True 時）只在內文出現的。已判過的跳過。"""
    watch = watch_tickers() if watch is None else watch
    since = (now_tpe() - dt.timedelta(days=days)).isoformat(timespec="minutes")
    done = {(r[0], r[1]) for r in con.execute("SELECT news_id, ticker FROM ticker_sent")}
    hi, mid, lo = [], [], []
    for r in con.execute("SELECT id, tickers, body_tickers, published FROM news WHERE body_status='ok' AND dup_of IS NULL "
                         "AND published >= ? ORDER BY published DESC", (since,)):
        tt = set(json.loads(r["tickers"] or "[]"))
        bt = set(json.loads(r["body_tickers"] or "[]"))
        for t in sorted(tt | bt):
            if t.startswith("^") or (r["id"], t) in done:
                continue
            (hi if t in watch else mid if t in tt else lo).append((r["id"], t))
    return hi + mid + (lo if body_only else [])


def ticker_aliases(code: str, name: str | None = None) -> list[str]:
    """一檔股票在內文可能出現的所有寫法：代號、正式名、去掉 * / -KY 的名、aliases.json 的別名與簡稱。"""
    out = [code, name or ""]
    if name:
        out.append(re.sub(r"[*＊]|-(KY|創)$", "", name))
    try:
        from tools import aliases
        t = aliases.table()
        out += (t.get("us_tickers") or {}).get(code, [])
        out += ((t.get("indices") or {}).get(code) or {}).get("aliases", [])
        out += [w for w in ((t.get("tw_short_names") or {}).get(code) or []) if isinstance(w, str)]
    except Exception:  # noqa: BLE001
        pass
    return [k for k in dict.fromkeys(out) if k and len(k) >= 2]


def mentions(text: str, keys: list[str]) -> bool:
    """中文別名用子字串；英文別名要整字（避免 Meta 命中 Metal、AMD 命中 AMDOCS）。"""
    for k in keys:
        if re.search(r"[\u4e00-\u9fff]", k):
            if k in text:
                return True
        elif re.search(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])", text):
            return True
    return False


def _chunks_mentioning(con, news_id: int, code: str, name: str, max_chars: int = 1500) -> tuple[str, int]:
    rows = con.execute("SELECT text FROM chunks WHERE news_id=? AND kind='body' ORDER BY idx", (news_id,)).fetchall()
    keys = ticker_aliases(code, name)
    hit = [r["text"] for r in rows if mentions(r["text"], keys)]
    if not hit:   # 還沒切塊（或切塊後剛好被頁尾規則截掉）：直接看內文
        b = (con.execute("SELECT body FROM news WHERE id=?", (news_id,)).fetchone() or [""])[0] or ""
        hit = [c for c in chunk_text(strip_boilerplate(b)) if mentions(c, keys)]
    return "\n…\n".join(hit)[:max_chars], len(hit)


def judge_ticker(title: str, code: str, name: str, text: str) -> tuple[dict | None, str]:
    from tools._common import ollama_chat
    msgs = [{"role": "user", "content": TICKER_PROMPT.format(name=name, code=code, title=title, chunks=text)}]
    res, st = None, "error"
    for _ in range(2):
        try:
            r = ollama_chat({"model": os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b"), "stream": False, "format": "json",
                             "options": {"temperature": 0, "num_ctx": 4096}, "messages": msgs}, "新聞個股判讀", timeout=180)
            j = json.loads(r["message"]["content"])
            res, st = validate_judgment(j, text)          # quote 必須出自「提到這檔的段落」
            if res is not None and str(j.get("ticker") or code).strip().upper() not in (code.upper(), name.upper()):
                res, st = None, "wrong_ticker"           # 模型判到別檔去了
        except Exception as e:  # noqa: BLE001
            res, st = None, f"error:{type(e).__name__}"
        if st == "ok":
            return res, st
        msgs = msgs + [{"role": "user", "content": f"ticker 必須是 {code}，quote 必須是段落裡一字不差的句子。請重新輸出 JSON。"}]
    return res, st


def classify_tickers(limit: int = 300, days: int = 7, con=None, watch: set[str] | None = None, body_only: bool = False,
                     pairs: list[tuple] | None = None) -> dict:
    """排程用：逐對判讀並存進 ticker_sent（快取）。對話時只讀。"""
    import time
    con = con or connect()
    pairs = list(pairs) if pairs is not None else ticker_pairs(con, days=days, watch=watch, body_only=body_only)
    todo = pairs[:limit]
    st = {"pending": len(pairs), "tried": len(todo), "ok": 0, "ungrounded": 0, "no_mention": 0, "error": 0, "exaggerated": 0}
    model = os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b")
    t0 = time.time()
    titles = {}
    for nid, code in todo:
        if nid not in titles:
            titles[nid] = (con.execute("SELECT title FROM news WHERE id=?", (nid,)).fetchone() or [""])[0]
        name = ticker_name(code)
        text, n = _chunks_mentioning(con, nid, code, name)
        if not text:
            row, s = {}, "no_mention"
        else:
            row, s = judge_ticker(titles[nid], code, name, text)
            row = row or {}
        key = "ok" if s == "ok" else "ungrounded" if s == "ungrounded" else "no_mention" if s == "no_mention" else "error"
        st[key] += 1
        if s.startswith("error"):
            continue                       # 下次排程再試
        con.execute("INSERT OR REPLACE INTO ticker_sent(news_id, ticker, name, sentiment, event, magnitude, exaggerated, reason, quote, "
                    "status, judged_by, judged_at, n_chunks) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, code, name, row.get("sentiment"), row.get("event"), row.get("magnitude"), int(bool(row.get("exaggerated"))),
                     row.get("reason"), row.get("quote"), s, f"{model}/ticker", iso_tpe(), n))
        st["exaggerated"] += int(s == "ok" and bool(row.get("exaggerated")))
        con.commit()
    st["elapsed_s"] = round(time.time() - t0, 1)
    return st


def classify_pending(limit: int = 200, batch: int = 20, con=None) -> dict:
    """排程用：先讀內文判讀，再把抓不到內文的用標題批次判讀。"""
    con = con or connect()
    return {"body": classify_bodies(limit=limit, con=con), "ticker": classify_tickers(limit=max(300, limit), con=con),
            "title": classify_titles(limit=limit, batch=batch, con=con)}


def basis_label(x: dict) -> str:
    sb = x.get("sent_by") or "rule"
    if "/ticker" in sb:
        return "模型針對本股讀內文判讀"
    if sb.startswith("rule"):
        return "關鍵字判讀（標題）"
    if "/body" in sb:
        return "模型讀整篇內文判讀" + ("（非針對本股）" if x.get("multi") else "")
    return "模型看標題判讀"


# ---------- 檢索（P1-E）----------
def _names_for(tg: dict) -> list[str]:
    ns = [tg.get("name"), tg.get("zh_name")]
    if tg.get("kind") == "us_index":
        from tools.history import US_ALIASES
        ns += [w for w in US_ALIASES.get(tg.get("symbol"), []) if re.search(r"[\u4e00-\u9fff]", w)]
    return [n for n in dict.fromkeys(ns) if n and len(n) >= 2]


_CF: dict = {"path": None, "mtime": None, "index": None}


def _chunk_index():
    p = chunk_faiss_path()
    if not p.exists():
        return None
    import faiss
    m = p.stat().st_mtime
    if _CF["path"] != str(p) or _CF["mtime"] != m:   # 排程重建後自動換新索引
        _CF.update(path=str(p), mtime=m, index=faiss.read_index(str(p)))
    return _CF["index"]


def _snip(text: str, names: list[str], n: int = 110) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    pos = min([i for i in (text.find(x) for x in names) if i >= 0] or [0])
    a = max(0, pos - 20)
    s = text[a:a + n]
    return ("…" if a > 0 else "") + s + ("…" if a + n < len(text) else "")


def retrieve(tg: dict, k: int = 5, days: int = 7, query: str | None = None, now: dt.datetime | None = None, con=None) -> list[dict]:
    """檢索（step12）：
    1) SQL 先篩候選文章：沒重複、近 days 天、而且標題標註／內文標註／標題名稱有提到這檔；
    2) 模型已載入時，用「問題＋股票名稱」當查詢，在 chunk 的 FAISS 上只搜這些候選文章的塊（IDSelector），每篇取最像的一塊；
    3) score = cosine × exp(-Δ小時/48) × 股票命中權重（標題標註 1.0、只有內文提到 0.8、只有標題名稱 0.6）。
    沒有模型（例如 eval 環境）時 sim=1，片段改用內文裡第一次提到名稱的位置。"""
    from tools import embed
    con = con or connect()
    now = now or now_tpe()
    since = (now - dt.timedelta(days=days)).isoformat(timespec="minutes")
    sym = tg.get("symbol")
    names = _names_for(tg)
    cond = " OR ".join(["tickers LIKE ?", "body_tickers LIKE ?"] + ["title LIKE ?"] * len(names))
    args = [f'%"{sym}"%', f'%"{sym}"%'] + [f"%{n}%" for n in names]
    rows = con.execute(f"SELECT * FROM news WHERE dup_of IS NULL AND published >= ? AND published <= ? AND ({cond})",
                       [since, now.isoformat(timespec="minutes"), *args]).fetchall()
    best: dict[int, tuple] = {}
    used = "none"
    if rows and embed.loaded():   # 對話時不為了一題去載模型（伺服器啟動時已在背景載入）
        idx = _chunk_index()
        qv = embed.encode_safe([((query or "") + " " + " ".join(names or [sym or ""])).strip()], max_len=CHUNK_MAX_TOKENS)
        if idx is not None and qv is not None:
            ids_in = [r["id"] for r in rows]
            crow = con.execute(f"SELECT id, news_id, kind, text FROM chunks WHERE news_id IN ({','.join('?' * len(ids_in))})", ids_in).fetchall()
            cmap = {c["id"]: c for c in crow}
            if cmap:
                import faiss
                import numpy as np
                sel_ids = np.array(sorted(cmap), dtype="int64")
                try:
                    params = faiss.SearchParameters(sel=faiss.IDSelectorBatch(len(sel_ids), faiss.swig_ptr(sel_ids)))
                    D, I = idx.search(qv, min(len(sel_ids), 500), params=params)
                    used = "faiss_chunks+selector"
                except Exception:  # noqa: BLE001
                    D, I = idx.search(qv, min(idx.ntotal, 5000))
                    used = "faiss_chunks"
                for s, i in zip(D[0].tolist(), I[0].tolist()):
                    c = cmap.get(i)
                    if c is None:
                        continue
                    # 同一篇裡，有提到這檔股票的段落優先當片段（避免挑到頁尾免責聲明、表格）；分數仍用原本的 cosine
                    key = (1 if any(n and n in c["text"] for n in names + [sym or ""]) else 0, s)   # 先看有沒有提到，再看 cosine
                    if c["news_id"] not in best or key > best[c["news_id"]][3]:
                        best[c["news_id"]] = (float(s), c["text"], c["kind"], key)
    tsent = {}
    if rows:   # step13：這檔股票在每篇文章的個股判讀（有就用，沒有退回整篇判讀並標明）
        ids_in = [r["id"] for r in rows]
        tsent = {t["news_id"]: t for t in con.execute(
            f"SELECT * FROM ticker_sent WHERE ticker=? AND status='ok' AND news_id IN ({','.join('?' * len(ids_in))})", [sym, *ids_in])}
    out = []
    for r in rows:
        try:
            dh = max(0.0, (now - dt.datetime.fromisoformat(r["published"])).total_seconds() / 3600)
        except Exception:  # noqa: BLE001
            continue
        tagged = sym in json.loads(r["tickers"] or "[]")
        in_body = sym in json.loads(r["body_tickers"] or "[]")
        w = 1.0 if tagged else 0.8 if in_body else 0.6
        if used != "none":
            sim, ctext, kind = best.get(r["id"], (0.2, "", "", (0, 0.0)))[:3]   # 沒有 chunk 的文章（還沒 embed）給低分，不直接丟掉
            sim = max(0.0, sim)
        else:
            sim, ctext, kind = 1.0, "", ""
        body_ok = r["body_status"] == "ok" and r["body"]
        if kind == "body" and ctext:
            snippet = _snip(ctext, names + [sym or ""])
        elif body_ok:
            snippet = _snip(r["body"], names + [sym or ""])
        else:
            snippet = ""
        score = sim * math.exp(-dh / TAU_H) * w
        ts = tsent.get(r["id"])
        alltk = set(json.loads(r["tickers"] or "[]")) | set(json.loads(r["body_tickers"] or "[]"))
        out.append({"id": r["id"], "title": r["title"], "published": r["published"], "publisher": r["publisher"] or r["feed"],
                    "url": r["body_url"] or r["url"], "sentiment": r["sentiment"] or "中性", "event": r["event"] or "其他",
                    "sent_by": r["sent_by"], "tagged": tagged, "age_h": round(dh, 1), "sim": round(sim, 3), "score": round(score, 4),
                    "snippet": snippet, "basis": "body" if body_ok else "title", "body_status": r["body_status"],
                    "exaggerated": bool(r["exaggerated"]), "magnitude": r["magnitude"], "reason": r["sent_reason"],
                    "retrieval": used, "aspect": False, "multi": len(alltk) > 1, "quote": r["sent_quote"]})
        if ts:   # 個股判讀覆蓋整篇判讀
            out[-1].update(sentiment=ts["sentiment"], event=ts["event"] or "其他", magnitude=ts["magnitude"], exaggerated=bool(ts["exaggerated"]),
                           reason=ts["reason"], quote=ts["quote"], sent_by=ts["judged_by"], aspect=True,
                           article_sentiment=r["sentiment"])
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:k]


def news_evidence(tg: dict, live: dict | None = None, k: int = 5, query: str | None = None) -> list[dict]:
    """決策用：先把這次即時抓到的新聞存進庫（不抓內文、不算 embedding、不叫模型），再做檢索。"""
    try:
        con = connect()
        if live and live.get("items"):
            store([{**it, "feed": "decision_live"} for it in live["items"]], embed=False, con=con)
        return retrieve(tg, k=k, con=con, query=query)
    except Exception:  # noqa: BLE001
        return []



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "classify", "search", "stats", "rechunk"])
    ap.add_argument("q", nargs="?")
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--classify", action="store_true")
    ap.add_argument("--no-bodies", action="store_true")
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()
    if a.cmd == "ingest":
        print(json.dumps(ingest(a.hours, a.classify, bodies=not a.no_bodies, body_limit=a.limit, classify_limit=a.limit),
                         ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "rechunk":
        print(json.dumps(rechunk_all(), ensure_ascii=False))
    elif a.cmd == "classify":
        print(classify_pending())
    elif a.cmd == "search":
        from tools.explain import _target
        tg = _target(a.q) or {"symbol": a.q, "name": a.q, "kind": "tw_stock"}
        from tools import embed
        embed.encode_safe(["warmup"])
        for x in retrieve(tg, k=10, query=a.q):
            print(x["score"], x["published"], x["sentiment"], x["event"], basis_label(x), x["title"], "|", x["snippet"][:60])
    else:
        c = connect()
        print(dict(c.execute("SELECT COUNT(*) n, SUM(dup_of IS NOT NULL) dups, SUM(emb IS NOT NULL) emb, SUM(sent_by!='rule') llm, SUM(body_status='ok') body_ok, SUM(body_status IS NOT NULL AND body_status!='ok') body_failed, "
                          "SUM(sent_by LIKE '%/body') llm_body, SUM(exaggerated) exaggerated, (SELECT COUNT(*) FROM chunks) chunks FROM news").fetchone()))
