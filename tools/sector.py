"""族群／主題表現：resolve（動態找股票）→ 抓日線 → 程式算每檔 1/5/20 日漲跌、連漲連跌，以及等權平均。

sector_performance(text, market) 也處理「單一公司」：resolve 判斷是公司時，回傳那一檔的歷史分析。
找不到就回 found=False 和 tried（試過哪些來源），呼叫端要明講「查不到」，不能拿別的資料頂替。
"""
from __future__ import annotations

import re
import time
from collections import Counter

from tools._common import iso_tpe
from tools.history import analyze
from tools.resolve import resolve, is_greeting_word


def _closes_batch(symbols: list[str], period: str = "6mo") -> dict[str, tuple[list[str], list[float]]]:
    import yfinance as yf
    out: dict[str, tuple[list[str], list[float]]] = {}
    if not symbols:
        return out
    df = yf.download(symbols, period=period, interval="1d", progress=False, auto_adjust=False,
                     group_by="ticker", threads=True)
    for s in symbols:
        try:
            sub = df[s] if len(symbols) > 1 or s in df.columns.get_level_values(0) else df
            c = sub["Close"]
            if hasattr(c, "columns"):
                c = c.iloc[:, 0]
            c = c.dropna()
            if len(c) >= 2:
                out[s] = ([d.strftime("%Y-%m-%d") for d in c.index], [round(float(x), 2) for x in c.values])
        except Exception:  # noqa: BLE001
            continue
    return out


def _mentions(found_by: list[str]) -> dict:
    """從 found_by 數出被幾則新聞提到（放進工具結果，回答裡引用的次數才有來源）。"""
    yf_news = sum(int(m) for f in found_by for m in re.findall(r"^yfinance 新聞.*×(\d+)", f))
    g = sum(int(m) for f in found_by for m in re.findall(r"^Google 新聞.*×(\d+)", f)) + \
        sum(1 for f in found_by if f.startswith("Google 新聞") and "×" not in f)
    return {"yf_news": yf_news, "google_news": g}


def _pct(a: dict | None):
    return a["change_pct"] if a else None


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def sector_performance(text: str, market: str = "auto", top: int = 8) -> dict:
    t0 = time.time()
    if is_greeting_word(text):  # 招呼語／填充詞：不翻譯、不搜新聞
        return {"query": text, "market": market, "kind": "none", "found": False, "greeting": True,
                "tried": ["招呼語／口頭禪，不當成股票或主題查"], "as_of": iso_tpe(), "elapsed_s": 0.0}
    r = resolve(text, market, top=top)
    base = {"query": text, "market": r.get("market"), "kind": r.get("kind"), "theme_key": r.get("theme_key"),
            "market_defaulted": r.get("market_defaulted", False), "tried": r.get("tried", []),
            "resolve_s": r.get("elapsed_s"), "resolve_cached": r.get("cached", False),
            "source": "yfinance_daily", "as_of": iso_tpe(), "data_type": "每日收盤",
            "excluded": r.get("excluded") or [],
            "company_like": r.get("company_like", False), "weak_candidates": r.get("weak_candidates") or []}
    inst = r.get("instruments") or []
    if not inst:
        return {**base, "found": False, "elapsed_s": round(time.time() - t0, 1)}
    closes = _closes_batch([i["yf"] for i in inst], "1y" if r.get("kind") == "company" else "6mo")
    rows, missing = [], []
    for i in inst:
        dc = closes.get(i["yf"])
        if not dc:
            missing.append({"symbol": i["symbol"], "name": i["name"]})
            continue
        a = analyze(*dc, recent=6)
        rows.append({"symbol": i["symbol"], "name": i.get("zh_name") or i["name"], "industry": i.get("industry"),
                     "found_by": i["found_by"], "mentions": _mentions(i["found_by"]),
                     **({"relevance": i["relevance"]} if i.get("relevance") else {}), "last_date": a["last_date"], "close": a["last_close"],
                     "change_1d_pct": a["recent"][-1]["change_pct"], "change_5d_pct": _pct(a["change_5d"]),
                     "change_20d_pct": _pct(a["change_20d"]), "streak": a["streak"],
                     **({"analysis": a} if r.get("kind") == "company" else {})})
    if not rows:
        return {**base, "found": False, "no_price_data": missing, "elapsed_s": round(time.time() - t0, 1)}
    rows.sort(key=lambda x: (x["change_1d_pct"] if x["change_1d_pct"] is not None else 0))
    dates = Counter(x["last_date"] for x in rows)
    last_date = max(dates)
    d1 = [x["change_1d_pct"] for x in rows]
    group = {"count": len(rows), "last_date": last_date,
             "avg_1d_pct": _avg(d1), "avg_5d_pct": _avg([x["change_5d_pct"] for x in rows]),
             "avg_20d_pct": _avg([x["change_20d_pct"] for x in rows]),
             "up": sum(1 for x in d1 if x is not None and x > 0), "down": sum(1 for x in d1 if x is not None and x < 0),
             "flat": sum(1 for x in d1 if x == 0),
             "stale": [x["symbol"] for x in rows if x["last_date"] != last_date], "weighting": "等權平均"}
    # 交易時段內 yfinance 的日 K 最後一根是「今天還沒收盤」的：要標成盤中，不能說收盤
    from tools._common import now_tpe
    if r.get("market") == "us":
        from tools.us_market import NY, us_session
        import datetime as _dt
        intraday = us_session() == "open" and last_date == _dt.datetime.now(NY).strftime("%Y-%m-%d")
    else:
        n = now_tpe()
        intraday = last_date == n.strftime("%Y-%m-%d") and n.hour * 100 + n.minute < 1430
    group["intraday"] = intraday
    dtype = ("當日 K 線還沒收盤、可能延遲約 20 分鐘" if intraday else
             "每日收盤（美東交易日）" if r.get("market") == "us" else "每日收盤（未還原權息）")
    return {**base, "found": True, "group": group, "stocks": rows, "missing": missing,
            "data_type": dtype,
            "elapsed_s": round(time.time() - t0, 1)}
