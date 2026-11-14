"""「為什麼 X 漲／跌」：把可能相關的事實一次收齊（程式計算），讓回答有根據。

explain_move(target) 平行抓：
- 走勢：連漲連跌、5／20 日、均線（history）
- 新聞：近 7 天標題有提到它的新聞（Google 新聞 RSS；美股另加 yfinance 新聞），附連結
- 法人：台股個股最近 5 個交易日的外資／投信／自營商買賣超（上市 TWSE T86、上櫃 TPEx dailyTrade）
- 同族群：台股用「新聞標題一起出現的股票」（不足再用同產業別）；美股用 yfinance 同產業前幾大
- 大盤與美股：加權指數；科技股另看費半、那斯達克
最後產生 signals（可能原因的候選，每條都帶事實數字）。原因只能是推論，回答會註明。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from tools._common import DATA, TPE, iso_tpe, now_tpe

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
INST_CACHE = DATA / "inst_cache"
TECH_TW = {"24", "25", "26", "27", "28", "29", "30", "31", "36"}
TECH_US_SECTORS = {"Technology", "Communication Services"}
INDEX_WORDS = re.compile(r"^(TAIEX|加權|加權指數|台股|大盤|台股大盤)$", re.I)


# ---------- 解析目標 ----------
def _target(text: str, market: str = "auto") -> dict | None:
    from tools.history import US_NAMES, resolve_us
    from tools.resolve import resolve_company, tw_companies
    t = (text or "").strip()
    if INDEX_WORDS.match(t):
        return {"kind": "tw_index", "symbol": "TAIEX", "name": "加權指數"}
    sym = resolve_us(t) if not re.fullmatch(r"\d{4}", t) else None
    if sym:
        return {"kind": "us_index", "symbol": sym, "name": US_NAMES[sym]}
    comp = tw_companies()
    if market in ("auto", "tw"):
        c = resolve_company(t, "tw")
        if c:
            v = comp[c["symbol"]]
            return {"kind": "tw_stock", "symbol": c["symbol"], "name": v["name"], "yf": c["yf"],
                    "industry": v["industry"], "industry_name": v["industry_name"], "listed": v["market"],
                    "found_by": c["found_by"]}
    if market in ("auto", "us"):
        c = resolve_company(t, "us")
        if c:
            return {"kind": "us_stock", "symbol": c["symbol"], "name": c["name"], "zh_name": c.get("zh_name"),
                    "yf": c["yf"], "industry": c.get("industry"), "found_by": c["found_by"]}
    return None


# ---------- 新聞（近 7 天，附連結）----------
def _gnews_items(q: str, lang: str = "zh") -> list[dict]:
    params = ({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"} if lang == "en"
              else {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    try:
        r = requests.get("https://news.google.com/rss/search", params=params, headers=HDR, timeout=15)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        def tag(n):
            m = re.search(rf"<{n}[^>]*>(.*?)</{n}>", item, re.S)
            return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else ""
        title, link, pub, src = tag("title"), tag("link"), tag("pubDate"), tag("source")
        try:
            t = parsedate_to_datetime(pub).astimezone(TPE)
        except Exception:  # noqa: BLE001
            t = None
        title = re.sub(r"\s+-\s+[^-]{1,30}$", "", title) if src else title
        out.append({"title": title, "publisher": src, "link": link, "time": t.isoformat(timespec="minutes") if t else None})
    return out


def _yf_news(sym: str) -> list[dict]:
    from tools.resolve import _yf_search
    out = []
    for n in _yf_search(sym, 1, 10)["news"]:
        if sym not in (n.get("relatedTickers") or []):
            continue
        ts = n.get("providerPublishTime")
        t = dt.datetime.fromtimestamp(ts, TPE).isoformat(timespec="minutes") if ts else None
        out.append({"title": n.get("title", ""), "publisher": n.get("publisher", ""), "link": n.get("link", ""), "time": t})
    return out


_BOILER = re.compile(r"股市爆料同學會|YouTube|個股概覽|謹供參考|即時報價|股價走勢圖|技術分析圖|K線圖|基本資料|Stock Price, News, Quote|Stock Quote")


def recent_news(tg: dict, days: int = 7, limit: int = 6) -> dict:
    names = [x for x in (tg.get("zh_name"), tg["name"] if tg["kind"] != "us_stock" else None) if x]
    if tg["kind"] == "tw_stock":
        names = [tg["name"]]
        qs = [(f"{tg['name']} when:{days}d", "zh"), (f"{tg['symbol']} {tg['name']} when:{days}d", "zh")]
    elif tg["kind"] == "us_index":
        from tools.history import US_ALIASES
        names = [w for w in US_ALIASES[tg["symbol"]] if re.search(r"[\u4e00-\u9fff]", w)]
        qs = [(f"{names[0]} when:{days}d", "zh")]
    elif tg["kind"] == "tw_index":
        names = ["台股", "加權", "大盤"]
        qs = [(f"台股 大盤 when:{min(days, 3)}d", "zh")]
    else:
        qs = ([(f"{tg['zh_name']} when:{days}d", "zh")] if tg.get("zh_name") else []) + \
             [(f"{tg['symbol']} stock when:{days}d", "en")]
        names = [x for x in (tg.get("zh_name"), tg["symbol"], tg["name"].split(",")[0].split(" Inc")[0]) if x]
    items, tried = [], []
    with ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(_gnews_items, q, lang) for q, lang in qs]
        yf_f = ex.submit(_yf_news, tg["symbol"]) if tg["kind"] == "us_stock" else None
        for (q, lang), f in zip(qs, futs):
            tried.append(f"Google 新聞「{q}」" if lang == "zh" else f"Google News「{q}」")
            items += f.result()
        if yf_f:
            tried.append(f"yfinance 新聞（{tg['symbol']}）")
            items += yf_f.result()
    cutoff = now_tpe() - dt.timedelta(days=days)
    seen, keep = set(), []
    for it in sorted(items, key=lambda x: x.get("time") or "", reverse=True):
        key = re.sub(r"\W", "", it["title"])[:40]
        if key in seen or not any(n.lower() in it["title"].lower() for n in names) or \
                _BOILER.search(it["title"] + " " + (it.get("publisher") or "")):
            continue
        if it.get("time") and dt.datetime.fromisoformat(it["time"]) < cutoff:
            continue
        seen.add(key)
        keep.append(it)
    return {"items": keep[:limit], "count": len(keep), "days": days, "match_names": names, "tried": tried,
            "all_titles": [it["title"] for it in items]}


# ---------- 法人（台股個股，最近 N 個交易日）----------
def _inst_day(ymd: str, listed: str) -> dict | None:
    """一天的個股三大法人買賣超（股）：{code: [外資, 投信, 自營商, 合計]}。有資料才快取。"""
    INST_CACHE.mkdir(parents=True, exist_ok=True)
    src = "twse" if listed == "上市" else "tpex"
    f = INST_CACHE / f"{src}_{ymd}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    num = lambda s: int(str(s).replace(",", "").strip() or 0)  # noqa: E731
    out = {}
    try:
        if src == "twse":
            j = requests.get("https://www.twse.com.tw/rwd/zh/fund/T86", headers=HDR, timeout=20,
                             params={"date": ymd, "selectType": "ALLBUT0999", "response": "json"}).json()
            if j.get("stat") != "OK" or not j.get("data"):
                return None
            for r in j["data"]:
                out[r[0].strip()] = [num(r[4]) + num(r[7]), num(r[10]), num(r[11]), num(r[18])]
        else:
            j = requests.get("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade", headers=HDR, timeout=20,
                             params={"type": "Daily", "sect": "EW", "date": f"{ymd[:4]}/{ymd[4:6]}/{ymd[6:]}",
                                     "response": "json"}).json()
            rows = (j.get("tables") or [{}])[0].get("data") or []
            if not rows:
                return None
            for r in rows:
                out[r[0].strip()] = [num(r[10]), num(r[13]), num(r[22]), num(r[23])]
    except Exception:  # noqa: BLE001
        return None
    if out:
        f.write_text(json.dumps(out), encoding="utf-8")
    return out or None


def stock_institutional(code: str, listed: str, days: int = 5, dates: list[str] | None = None) -> dict:
    src = "TWSE T86" if listed == "上市" else "TPEx 三大法人日報"
    cand = [d.replace("-", "") for d in (dates or [])][::-1]
    if not cand:
        d = now_tpe().date()
        cand = [(d - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(14)
                if (d - dt.timedelta(days=i)).weekday() < 5]
    rows = []
    for ymd in cand:
        if len(rows) >= days:
            break
        day = _inst_day(ymd, listed)
        if day is None:
            continue
        v = day.get(code)
        lots = [round(x / 1000) for x in v] if v else [0, 0, 0, 0]
        rows.append({"date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "foreign": lots[0], "trust": lots[1],
                     "dealer": lots[2], "total": lots[3]})
        time.sleep(0.25)
    rows.reverse()

    def streak(k):
        if not rows or rows[-1][k] == 0:
            return {"side": "flat", "days": 0, "sum": 0}
        side = "sell" if rows[-1][k] < 0 else "buy"
        n, s = 0, 0
        for r in reversed(rows):
            if (r[k] < 0) == (side == "sell") and r[k] != 0:
                n, s = n + 1, s + r[k]
            else:
                break
        return {"side": side, "days": n, "sum": s}
    return {"source": src, "unit": "張", "days": rows,
            "sum": {k: sum(r[k] for r in rows) for k in ("foreign", "trust", "dealer", "total")},
            "streak": {k: streak(k) for k in ("foreign", "trust", "dealer")}}


# ---------- 同族群 ----------
def _perf(yf_syms: dict[str, tuple[str, str]], period="3mo") -> list[dict]:
    from tools.history import analyze
    from tools.sector import _closes_batch
    data = _closes_batch(list(yf_syms), period=period)
    out = []
    for ys, (sym, name) in yf_syms.items():
        if ys not in data:
            continue
        a = analyze(*data[ys], recent=5)
        out.append({"symbol": sym, "name": name, "last_date": a["last_date"],
                    "change_1d_pct": a["recent"][-1]["change_pct"],
                    "change_5d_pct": (a["change_5d"] or {}).get("change_pct"),
                    "streak": {"direction": a["streak"]["direction"], "days": a["streak"]["days"]}})
    return out


def peers_tw(tg: dict, titles: list[str], top: int = 6) -> dict:
    from tools.resolve import _tw_codes_in, tw_companies
    comp = tw_companies()
    cnt = Counter()
    for t in set(titles):
        if tg["name"] in t or tg["symbol"] in t:
            for c in _tw_codes_in(t) - {tg["symbol"]}:
                cnt[c] += 1
    picked = [(c, f"新聞標題和{tg['name']}一起出現 {n} 次") for c, n in cnt.most_common(top) if n >= 2 and c in comp]
    how = "新聞標題一起出現"
    if len(picked) < 3:
        same = sorted((c for c, v in comp.items() if v["industry"] == tg["industry"] and c != tg["symbol"]
                       and len(c) == 4 and c not in dict(picked)), key=lambda c: -comp[c]["capital"])
        picked += [(c, f"同產業別 {tg['industry_name']}") for c in same[:top - len(picked)]]
        how = "新聞標題一起出現＋同產業別（資本額大）" if cnt else f"同產業別 {tg['industry_name']}（資本額大）"
    why = dict(picked)
    perf = _perf({c + comp[c]["suffix"]: (c, comp[c]["name"]) for c, _ in picked})
    for p in perf:
        p["why"] = why[p["symbol"]]
    return _group(perf, how)


def peers_us(tg: dict, top: int = 6) -> dict:
    from tools.resolve import _industry_top, yf_industry_catalog
    ind = tg.get("industry")
    key = next((k for k, v in yf_industry_catalog().items() if v.get("name") == ind), None) if ind else None
    if not key:
        return _group([], "找不到 yfinance 產業")
    rows = [(s, n) for s, n, _ in _industry_top(key) if s != tg["symbol"]][:top]
    perf = _perf({s: (s, n) for s, n in rows})
    for p in perf:
        p["why"] = f"yfinance 產業 {ind} 前幾大"
    return _group(perf, f"yfinance 產業 {ind} 前幾大公司")


def peers_us_index(sym: str) -> dict:
    from tools.history import US_NAMES
    perf = _perf({s: (s, n) for s, n in US_NAMES.items() if s != sym})
    for p in perf:
        p["why"] = "其他美股主要指數"
    return _group(perf, "其他美股主要指數")


def _group(perf: list[dict], how: str) -> dict:
    v1 = [p["change_1d_pct"] for p in perf if p["change_1d_pct"] is not None]
    v5 = [p["change_5d_pct"] for p in perf if p["change_5d_pct"] is not None]
    perf.sort(key=lambda p: p["change_1d_pct"] if p["change_1d_pct"] is not None else 0)
    return {"how": how, "stocks": perf, "count": len(perf),
            "avg_1d_pct": round(sum(v1) / len(v1), 2) if v1 else None,
            "avg_5d_pct": round(sum(v5) / len(v5), 2) if v5 else None,
            "down": sum(1 for v in v1 if v < 0), "up": sum(1 for v in v1 if v > 0)}


# ---------- 大盤／美股 ----------
def index_context(tg: dict) -> list[dict]:
    from tools import history
    want = []
    if tg["kind"] == "tw_stock":
        want = ["TAIEX"] + (["^SOX", "^IXIC"] if tg.get("industry") in TECH_TW else [])
    elif tg["kind"] == "tw_index":
        want = ["^SOX", "^IXIC"]
    elif tg["kind"] == "us_stock":
        want = ["^GSPC", "^IXIC"] + (["^SOX"] if re.search(r"Semiconductor", tg.get("industry") or "") else [])
    out = []
    for s in want:
        try:
            h = history.tw_history("TAIEX", 5) if s == "TAIEX" else history.us_index_history(s, 5)
            out.append({"symbol": h["symbol"], "name": h["name"], "last_date": h["last_date"],
                        "source": h["source"], "change_1d_pct": h["recent"][-1]["change_pct"],
                        "change_5d_pct": (h.get("change_5d") or {}).get("change_pct"),
                        "streak": {"direction": h["streak"]["direction"], "days": h["streak"]["days"],
                                   "cum_change_pct": h["streak"]["cum_change_pct"]}})
        except Exception as e:  # noqa: BLE001
            out.append({"symbol": s, "error": f"{type(e).__name__}: {e}"})
    return out


def _price(tg: dict) -> dict:
    from tools import history
    if tg["kind"] == "tw_index":
        return history.tw_history("TAIEX", 10)
    if tg["kind"] == "us_index":
        return history.us_index_history(tg["symbol"], 10)
    if tg["kind"] == "tw_stock":
        return history.tw_history(tg["symbol"], 10)
    dates, closes = history._yf_closes(tg["yf"])
    return {"symbol": tg["symbol"], "name": tg["name"], "source": "yfinance_daily", "as_of": iso_tpe(),
            "data_type": "每日收盤（美東交易日）", **history.analyze(dates, closes, recent=10)}


# ---------- 主函式 ----------
def explain_move(target: str, market: str = "auto") -> dict:
    t0 = time.time()
    tg = _target(target, market)
    if not tg:
        return {"found": False, "query": target, "tried": ["證交所/櫃買公司資料", "美股指數名稱", "yfinance／新聞公司名稱解析"],
                "source": "resolve", "as_of": iso_tpe()}
    out = {"found": True, "query": target, "target": tg, "as_of": iso_tpe(), "errors": []}
    with ThreadPoolExecutor(6) as ex:
        f_price = ex.submit(_price, tg)
        f_news = ex.submit(recent_news, tg)
        f_idx = ex.submit(index_context, tg)
        f_peer_us = ex.submit(peers_us, tg) if tg["kind"] == "us_stock" else \
            ex.submit(peers_us_index, tg["symbol"]) if tg["kind"] == "us_index" else None
        try:
            out["price"] = f_price.result()
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"走勢：{type(e).__name__}: {e}")
            out["price"] = None
        dates = [r["date"] for r in (out["price"] or {}).get("recent", [])]
        f_inst = ex.submit(stock_institutional, tg["symbol"], tg["listed"], 5, dates) if tg["kind"] == "tw_stock" else None
        news = f_news.result()
        out["news"] = {k: v for k, v in news.items() if k != "all_titles"}
        if tg["kind"] == "tw_stock":
            try:
                extra = _gnews_items(f"{tg['name']} 族群")
                out["peers"] = peers_tw(tg, news["all_titles"] + [x["title"] for x in extra])
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"同族群：{type(e).__name__}: {e}")
        elif f_peer_us:
            try:
                out["peers"] = f_peer_us.result()
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"同族群：{type(e).__name__}: {e}")
        if f_inst:
            try:
                out["institutional"] = f_inst.result()
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"法人：{type(e).__name__}: {e}")
        out["index"] = f_idx.result()
    out["signals"] = signals(out)
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out


# ---------- 可能原因的候選（程式產生，每條都有數字依據）----------
def _p(v):
    return "—" if v is None else f"{v:+.2f}%"


def _updown(p: dict) -> str:
    ch = [r["change_pct"] for r in p.get("recent", []) if r.get("change_pct") is not None]
    return f"最近 {len(ch)} 個交易日：跌 {sum(1 for c in ch if c < 0)} 天、漲 {sum(1 for c in ch if c > 0)} 天"


def signals(o: dict) -> list[dict]:
    sig = []
    p = o.get("price")
    if not p:
        return sig
    s = p["streak"]
    down = s["direction"] == "down"
    word = "跌" if down else "漲"
    if s["days"] >= 1 and s["direction"] != "flat":
        c5 = (p.get("change_5d") or {}).get("change_pct")
        sig.append({"tag": "走勢", "text": f"{o['target'].get('zh_name') or o['target']['name']}{'連' if s['days'] >= 2 else ''}{word} {s['days']} 天，累計 {_p(s['cum_change_pct'])}；"
                                           f"{_updown(p)}" + (f"，5 日 {_p(c5)}" if c5 is not None else "")})
    g = o.get("peers") or {}
    if g.get("count"):
        if (g["down"] if down else g["up"]) >= max(2, g["count"] * 0.6):
            sig.append({"tag": "同族群", "text": f"同族群一起{word}：{g['count']} 檔中 {g['down'] if down else g['up']} 檔{word}，"
                                                 f"平均 {_p(g['avg_1d_pct'])}（{g['how']}）"})
        else:
            sig.append({"tag": "同族群", "text": f"同族群沒有一起{word}：{g['count']} 檔中跌 {g['down']}、漲 {g['up']}，"
                                                 f"平均 {_p(g['avg_1d_pct'])}，比較像個股因素"})
    inst = o.get("institutional")
    if inst and inst.get("days"):
        for k, lab in (("foreign", "外資"), ("trust", "投信"), ("dealer", "自營商")):
            st = inst["streak"][k]
            want = "sell" if down else "buy"
            if st["side"] == want and st["days"] >= 2:
                sig.append({"tag": "法人", "text": f"{lab}連{'賣' if want == 'sell' else '買'} {st['days']} 天，合計 {st['sum']:+,} 張"})
            elif st["side"] == want and k != "dealer" and abs(inst["days"][-1][k]) >= 100:
                sig.append({"tag": "法人", "text": f"{lab}最近一天{'賣超' if want == 'sell' else '買超'} {abs(inst['days'][-1][k]):,} 張"})
        if not any(x["tag"] == "法人" for x in sig):
            sm = inst["sum"]
            sig.append({"tag": "法人", "text": f"法人沒有明顯{'賣' if down else '買'}：近 {len(inst['days'])} 日外資 {sm['foreign']:+,} 張、投信 {sm['trust']:+,} 張"})
    for ix in o.get("index") or []:
        if ix.get("error"):
            continue
        st = ix["streak"]
        if st["direction"] == s["direction"] and st["days"] >= 2:
            sig.append({"tag": "大盤" if ix["symbol"] == "TAIEX" else "美股",
                        "text": f"{ix['name']}也連{word} {st['days']} 天，累計 {_p(st['cum_change_pct'])}"})
        elif ix["change_1d_pct"] is not None and (ix["change_1d_pct"] < -1 if down else ix["change_1d_pct"] > 1):
            sig.append({"tag": "大盤" if ix["symbol"] == "TAIEX" else "美股",
                        "text": f"{ix['name']} {ix['last_date'][5:].replace('-', '/')} {_p(ix['change_1d_pct'])}"})
    n = o.get("news") or {}
    if n.get("items"):
        sig.append({"tag": "新聞", "text": f"近 {n['days']} 天有 {n['count']} 則新聞標題提到，最新：「{n['items'][0]['title']}」"})
    else:
        sig.append({"tag": "新聞", "text": f"近 {n.get('days', 7)} 天沒有找到標題提到它的新聞"})
    return sig


def label_of(tg: dict) -> str:
    if tg["kind"] == "us_stock":
        return f"{tg.get('zh_name') or tg['name']}（{tg['symbol']}）"
    return f"{tg['name']}（{tg['symbol']}）" if tg["kind"] == "tw_stock" else tg["name"]


def fact_text(o: dict) -> str:
    """事實段落（程式產生）。"""
    from tools import history
    tg = o["target"]
    label = label_of(tg)
    lines = []
    p = o.get("price")
    if p:
        h = {**p, "name": label}
        fl = history.fact_lines(h, "streak")
        lines.append(fl[0])
        lines.append("- " + _updown(p))
        lines += ["- " + x for x in fl[1:3]]
    else:
        lines.append(f"{label}：抓不到走勢資料。")
    inst = o.get("institutional")
    if inst and inst.get("days"):
        rows = "；".join(f"{history._md(r['date'])} 外資 {r['foreign']:+,}、投信 {r['trust']:+,}、自營 {r['dealer']:+,}"
                        for r in inst["days"])
        sm = inst["sum"]
        lines.append(f"\n法人買賣超（張，{inst['source']}）：{rows}")
        lines.append(f"- 近 {len(inst['days'])} 日合計：外資 {sm['foreign']:+,}、投信 {sm['trust']:+,}、自營 {sm['dealer']:+,}")
    g = o.get("peers")
    if g and g.get("count"):
        md = history._md(g["stocks"][0]["last_date"])
        lines.append(f"\n同族群（{g['how']}）{md} 平均 {_p(g['avg_1d_pct'])}，跌 {g['down']} 檔、漲 {g['up']} 檔；5 日平均 {_p(g['avg_5d_pct'])}")
        lines.append("- " + "、".join(f"{x['symbol']} {x['name'][:12]} {_p(x['change_1d_pct'])}" for x in g["stocks"]))
    idx = [x for x in o.get("index") or [] if not x.get("error")]
    if idx:
        def one(x):
            st = x["streak"]
            w = {"up": "連漲", "down": "連跌", "flat": "平盤"}[st["direction"]]
            tail = f"（{w} {st['days']} 天）" if st["days"] >= 2 else ""
            return f"{x['name']} {_p(x['change_1d_pct'])}{tail}"
        from tools._common import bar_label
        lines.append(f"\n大盤／美股（{history._md(idx[0]['last_date'])} {bar_label(idx[0]['last_date'])}）：" + "、".join(one(x) for x in idx))
    n = o.get("news") or {}
    if n.get("items"):
        lines.append(f"\n近 {n['days']} 天新聞（標題有提到，共 {n['count']} 則）：")
        for it in n["items"][:4]:
            tm = it["time"][5:10].replace("-", "/") if it.get("time") else ""
            lines.append(f"- {tm} {it['title']}（{it['publisher']}）{it['link']}")
    else:
        lines.append(f"\n近 {n.get('days', 7)} 天沒有找到標題提到「{tg['name']}」的新聞。")
    if o.get("errors"):
        lines.append("（部分資料抓取失敗：" + "；".join(o["errors"]) + "）")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys as _s
    r = explain_move(_s.argv[1] if len(_s.argv) > 1 else "聯鈞")
    print(json.dumps(r, ensure_ascii=False, indent=1, default=str)[:6000])
    if r.get("found"):
        print(fact_text(r))
        print(json.dumps(r["signals"], ensure_ascii=False, indent=1))
