"""歷史走勢：近期每日收盤＋程式計算的連漲/連跌、N 日漲跌、52 週高低、均線位置。

- 美股指數：yfinance 日 K（1 年）
- 加權指數：證交所 FMTQIK（每月一個請求；過去的月份快取在 data/hist_cache/）
- 個股：yfinance「代號.TW」，找不到再試「.TWO」（上櫃）；公司清單上是上櫃的直接先試 .TWO
所有數字都由這裡算好，模型只引用；fact_lines() 直接產生回答用的句子。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from tools._common import DATA, iso_tpe, now_tpe, to_num

CACHE = DATA / "hist_cache"
CACHE.mkdir(exist_ok=True)

US_ALIASES = {
    "^SOX": ["費半", "費城半導體", "費城", "SOX", "半導體指數"],
    "^IXIC": ["那斯達克", "納斯達克", "那指", "NASDAQ", "Nasdaq", "IXIC"],
    "^DJI": ["道瓊", "道指", "DJI", "Dow"],
    "^GSPC": ["標普", "S&P", "SP500", "S&P500", "GSPC", "標準普爾"],
}
US_NAMES = {"^SOX": "費城半導體", "^IXIC": "那斯達克", "^DJI": "道瓊", "^GSPC": "S&P 500"}


def resolve_us(text: str) -> str | None:
    for sym, words in US_ALIASES.items():
        if any(w.lower() in text.lower() for w in words):
            return sym
    return None


# ---------- 計算 ----------
def analyze(dates: list[str], closes: list[float], recent: int = 10) -> dict:
    """dates/closes 由舊到新。"""
    n = len(closes)
    if n < 2:
        raise ValueError("資料不足")
    last, last_date = closes[-1], dates[-1]
    # 連漲/連跌：從最後一天往回，看每日收盤漲跌方向
    diffs = [closes[i] - closes[i - 1] for i in range(1, n)]
    d0 = diffs[-1]
    direction = "up" if d0 > 0 else "down" if d0 < 0 else "flat"
    days = 0
    for d in reversed(diffs):
        if (direction == "up" and d > 0) or (direction == "down" and d < 0) or (direction == "flat" and d == 0):
            days += 1
        else:
            break
    base = closes[-1 - days]
    streak = {"direction": direction, "days": days,
              "start_date": dates[-days] if days else last_date, "end_date": last_date,
              "base_close": base, "base_date": dates[-1 - days],
              "cum_change": round(last - base, 2), "cum_change_pct": round((last - base) / base * 100, 2)}

    def chg(k):
        if n <= k:
            return None
        b = closes[-1 - k]
        return {"days": k, "from_date": dates[-1 - k], "from_close": b,
                "change": round(last - b, 2), "change_pct": round((last - b) / b * 100, 2)}

    def ma(k):
        return round(sum(closes[-k:]) / k, 2) if n >= k else None

    one_year = dt.date.fromisoformat(last_date) - dt.timedelta(days=365)
    idx = [i for i, d in enumerate(dates) if dt.date.fromisoformat(d) >= one_year]
    hi_i = max(idx, key=lambda i: closes[i])
    lo_i = min(idx, key=lambda i: closes[i])
    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    # 本週（同一個 ISO 週）表現：對上週最後一個交易日收盤
    wk = dt.date.fromisoformat(last_date).isocalendar()[:2]
    prev_week = [i for i, d in enumerate(dates) if dt.date.fromisoformat(d).isocalendar()[:2] < wk]
    week = None
    if prev_week:
        b = closes[prev_week[-1]]
        week = {"from_date": dates[prev_week[-1]], "from_close": b, "change": round(last - b, 2),
                "change_pct": round((last - b) / b * 100, 2),
                "days_this_week": n - 1 - prev_week[-1]}
    return {
        "last_date": last_date, "last_close": last,
        "streak": streak,
        "change_5d": chg(5), "change_20d": chg(20), "this_week": week,
        "high_52w": {"close": closes[hi_i], "date": dates[hi_i],
                     "from_high_pct": round((last - closes[hi_i]) / closes[hi_i] * 100, 2)},
        "low_52w": {"close": closes[lo_i], "date": dates[lo_i],
                    "from_low_pct": round((last - closes[lo_i]) / closes[lo_i] * 100, 2)},
        "ma": {"ma5": ma5, "ma20": ma20, "ma60": ma60,
               "above_ma5": None if ma5 is None else last > ma5,
               "above_ma20": None if ma20 is None else last > ma20,
               "above_ma60": None if ma60 is None else last > ma60},
        "recent": [{"date": d, "close": c, "change_pct": (round((c - closes[i - 1]) / closes[i - 1] * 100, 2)
                                                          if i > 0 else None)}
                   for i, (d, c) in list(enumerate(zip(dates, closes)))[-recent:]],
    }


# ---------- 資料來源 ----------
def _yf_closes(symbol: str, period: str = "1y") -> tuple[list[str], list[float]]:
    import yfinance as yf
    df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"yfinance 沒有 {symbol} 的資料")
    c = df["Close"]
    if hasattr(c, "columns"):  # 新版 yfinance 會回多層欄位
        c = c.iloc[:, 0]
    c = c.dropna()
    return [d.strftime("%Y-%m-%d") for d in c.index], [round(float(x), 2) for x in c.values]


def us_index_history(symbol: str = "^SOX", days: int = 10) -> dict:
    """美股指數歷史＋連漲連跌等。symbol 可給 ^SOX / 費半 / 那斯達克 等。"""
    sym = symbol if symbol in US_NAMES else (resolve_us(symbol) or symbol)
    dates, closes = _yf_closes(sym)
    a = analyze(dates, closes, recent=days)
    return {"symbol": sym, "name": US_NAMES.get(sym, sym), "source": "yfinance_daily",
            "as_of": iso_tpe(), "data_type": "每日收盤（美東交易日）", **a}


def _fmtqik_month(ym: str) -> list[tuple[str, float]]:
    """ym=YYYYMM。過去月份快取，當月每次重抓。"""
    f = CACHE / f"FMTQIK_{ym}.json"
    current = ym == now_tpe().strftime("%Y%m")
    if f.exists() and not current:
        return [tuple(x) for x in json.loads(f.read_text(encoding="utf-8"))]
    r = requests.get("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
                     params={"date": ym + "01", "response": "json"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    rows = r.json().get("data") or []
    out = []
    for row in rows:
        y, m, d = row[0].split("/")
        out.append((f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}", to_num(row[4])))
    if out and not current:
        f.write_text(json.dumps(out), encoding="utf-8")
    time.sleep(0.2 if current or not f.exists() else 0)
    return out


def _taiex_closes(months: int = 13) -> tuple[list[str], list[float]]:
    d = now_tpe().date().replace(day=1)
    yms = []
    for _ in range(months):
        yms.append(d.strftime("%Y%m"))
        d = (d - dt.timedelta(days=1)).replace(day=1)
    rows = []
    for ym in reversed(yms):
        rows += _fmtqik_month(ym)
    rows = [r for r in rows if r[1] is not None]
    return [r[0] for r in rows], [r[1] for r in rows]


def tw_history(code: str = "TAIEX", days: int = 10) -> dict:
    """台股歷史：code='TAIEX'（加權指數，證交所）或個股代號（yfinance .TW/.TWO）。"""
    c = str(code).upper().strip()
    if c in ("TAIEX", "加權", "加權指數", "台股", "大盤", "^TWII", "IX0001"):
        dates, closes = _taiex_closes()
        a = analyze(dates, closes, recent=days)
        return {"symbol": "TAIEX", "name": "加權指數", "source": "twse_daily", "as_of": iso_tpe(),
                "data_type": "每日收盤", **a}
    from tools.news import load_stock_list
    lst = load_stock_list()
    if c not in lst:
        rev = {v: k for k, v in lst.items()}
        c = rev.get(code, c)
    err = None
    order = (".TW", ".TWO")
    try:   # 已知上櫃股直接用 .TWO，不用先白試一次 .TW（每次多 2–4 秒）
        from tools.resolve import tw_companies
        if (tw_companies().get(c) or {}).get("suffix") == ".TWO":
            order = (".TWO", ".TW")
    except Exception:  # noqa: BLE001
        pass
    for suffix in order:
        try:
            dates, closes = _yf_closes(c + suffix)
            a = analyze(dates, closes, recent=days)
            nm = lst.get(c)
            if not nm:
                try:
                    from tools.resolve import tw_companies
                    nm = (tw_companies().get(c) or {}).get("name")
                except Exception:  # noqa: BLE001
                    nm = None
            return {"symbol": c, "name": nm, "source": "yfinance_daily", "as_of": iso_tpe(),
                    "data_type": "每日收盤（未還原權息）", **a}
        except Exception as e:
            err = e
    raise ValueError(f"找不到 {code} 的歷史資料：{err}")


def us_streaks() -> list[dict]:
    """美股四大指數的連漲/連跌（給 market_overview 用）。"""
    import yfinance as yf
    syms = list(US_NAMES)
    df = yf.download(syms, period="3mo", interval="1d", progress=False, auto_adjust=False, group_by="ticker")
    out = []
    for s in syms:
        c = df[s]["Close"].dropna()
        a = analyze([d.strftime("%Y-%m-%d") for d in c.index], [round(float(x), 2) for x in c.values])
        out.append({"symbol": s, "name": US_NAMES[s], "date": a["last_date"], **_streak_brief(a)})
    return out


def _streak_brief(a: dict) -> dict:
    s = a["streak"]
    return {"streak_direction": s["direction"], "streak_days": s["days"], "streak_cum_pct": s["cum_change_pct"]}


# ---------- 回答句（程式產生） ----------
def _p(v):
    return f"{v:+.2f}%"


def _md(d):
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _num(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def fact_lines(h: dict, focus: str = "streak") -> list[str]:
    """focus: streak / week / high / ma / trend。第一句直接回答問題。"""
    name, s = h["name"], h["streak"]
    from tools._common import bar_label
    lab = bar_label(h["last_date"], us=str(h.get("symbol", "")).startswith("^") or "美東" in str(h.get("data_type", "")))
    lab0 = lab
    tag = f"（截至 {_md(h['last_date'])} {lab}，{h['data_type'] if lab == '收盤' else '當日 K 線還沒收盤、可能延遲約 20 分鐘'}，來源 {h['source']}）"
    word = {"up": "連漲", "down": "連跌", "flat": "持平"}[s["direction"]]
    if s["direction"] == "flat":
        streak = f"{name}最近一個交易日 {_md(h['last_date'])} 收平盤 {_num(h['last_close'])}"
    elif s["days"] == 1:
        prev = "漲" if s["direction"] == "down" else "跌"
        streak = (f"{name}只{'跌' if s['direction'] == 'down' else '漲'}了 1 天（{_md(s['end_date'])}，{_p(s['cum_change_pct'])}），"
                  f"前一個交易日是{prev}的，所以不算連{'跌' if s['direction'] == 'down' else '漲'}；收 {_num(h['last_close'])}")
    else:
        streak = (f"{name}已{word} {s['days']} 天（{_md(s['start_date'])}–{_md(s['end_date'])}），"
                  f"累計 {_p(s['cum_change_pct'])}（{_num(s['base_close'])} → {_num(h['last_close'])}）")
    hi, lo, ma = h["high_52w"], h["low_52w"], h["ma"]
    l_high = f"距 52 週高點 {_num(hi['close'])}（{_md(hi['date'])}）{_p(hi['from_high_pct'])}，距 52 週低點 {_num(lo['close'])}（{_md(lo['date'])}）{_p(lo['from_low_pct'])}"
    ma_parts = []
    for k, lab in (("ma5", "5 日均線"), ("ma20", "20 日均線"), ("ma60", "60 日均線")):
        if ma.get(k) is not None:
            ma_parts.append(f"{lab} {_num(ma[k])}（{'站上' if ma['above_' + k] else '跌破'}）")
    l_ma = ("最新價 " if lab0 == "盤中" else "收盤 ") + _num(h["last_close"]) + "，" + "、".join(ma_parts) if ma_parts else ""
    l_chg = "、".join(f"{x['days']} 日 {_p(x['change_pct'])}" for x in (h.get("change_5d"), h.get("change_20d")) if x)
    w = h.get("this_week")
    l_week = (f"本週（{w['days_this_week']} 個交易日）{_p(w['change_pct'])}，對上週收盤 {_num(w['from_close'])}（{_md(w['from_date'])}）"
              if w else "")
    recent = "、".join(f"{_md(r['date'])} {_p(r['change_pct'])}" for r in h["recent"][-5:] if r["change_pct"] is not None)
    l_recent = f"最近 5 個交易日：{recent}"

    order = {"streak": [streak, l_recent, l_chg, l_ma],
             "week": [l_week or streak, l_recent, streak, l_ma],
             "high": [l_high, streak, l_chg],
             "ma": [l_ma, streak, l_chg],
             "trend": [streak, l_week, l_chg, l_ma, l_high]}[focus]
    lines, seen = [], set()
    for x in order:
        if x and x not in seen:
            seen.add(x)
            lines.append(x)
    lines[0] = lines[0] + tag
    return lines


if __name__ == "__main__":
    from tools._common import dump_selftest
    res = {}
    for k, fn in (("sox", lambda: us_index_history("^SOX")), ("ixic", lambda: us_index_history("那斯達克")),
                  ("taiex", lambda: tw_history("TAIEX")), ("2330", lambda: tw_history("2330")),
                  ("us_streaks", us_streaks)):
        try:
            res[k] = fn()
        except Exception as e:
            res[k] = {"error": f"{type(e).__name__}: {e}"}
    for k in ("sox", "ixic", "taiex", "2330"):
        if "error" not in res[k]:
            res[k + "_lines"] = {f: fact_lines(res[k], f) for f in ("streak", "week", "high", "ma")}
    dump_selftest("history", res)
