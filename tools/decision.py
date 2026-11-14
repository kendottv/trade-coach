"""決策輔助：「聯鈞適合抄底嗎」「該停損嗎」。

分工：
- decision_brief()：程式算好所有事實——你的持倉（成本、未實現損益、停損／目標距離）、技術面（均線與斜率、RSI14、
  20／60 日高低點、量比、52 週位置、ATR）、籌碼（法人連買連賣）、環境（同族群、費半／加權、新聞）、事件（財報日）。
  每條證據有 id、偏多／偏空標記和數字；價位只來自程式算的 levels。
- judge()：qwen2.5:14b 只拿 brief，輸出 JSON（傾向、信心、各 3 點理由引用 id、情境用 level id、規則檢查、觀察重點）。
  驗證：id 要存在、數字要在 brief 裡、情境價位只能是 levels；不合格的丟掉；JSON 壞掉就用程式的中性摘要。
- render()：程式產生最後的繁體中文回覆。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from tools._common import bar_label, iso_tpe, now_tpe
from tools import features_ext

JUDGE_MODEL = os.environ.get("TRADE_COACH_JUDGE_MODEL", "qwen2.5:14b")
JUDGE_KEEP_ALIVE = os.environ.get("TRADE_COACH_JUDGE_KEEP_ALIVE", "15m")   # P2-F：連續問決策題時不用每題重新載入 14B
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
FILLER = re.compile(r"需謹慎|謹慎看待|密切(關注|注意)|多方因素|多種因素|綜合考量|謹慎(評估|操作|考慮)|諮詢|投資有風險|自行判斷|僅供參考|不構成|視個人|因人而異|風險承受")


# ---------- 價格資料 ----------
LAST_OHLCV: dict[str, dict] = {}

def _ohlcv(yf_sym: str, period: str = "1y") -> dict:
    import yfinance as yf
    df = yf.download(yf_sym, period=period, interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"yfinance 沒有 {yf_sym} 的資料")
    if hasattr(df.columns, "levels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    return {"dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "open": [float(x) for x in df["Open"]], "high": [float(x) for x in df["High"]],
            "low": [float(x) for x in df["Low"]], "close": [float(x) for x in df["Close"]],
            "volume": [float(x or 0) for x in df["Volume"]]}


def _r(v, n=2):
    return None if v is None else round(float(v), n)


def slice_asof(ohlcv: dict, asof: str | None) -> dict:
    """只留 asof（含）以前的 K 棒：回測時不能偷看未來。asof=None 表示用全部資料。"""
    if asof is None:
        return ohlcv
    k = sum(1 for x in ohlcv["dates"] if x <= asof)   # dates 是遞增的 YYYY-MM-DD 字串
    if k == 0:
        raise ValueError(f"{asof} 以前沒有 K 棒")
    return {key: list(val[:k]) for key, val in ohlcv.items() if isinstance(val, list)}


def features(ohlcv: dict, asof: str | None = None, tw: bool = True) -> dict:
    """純函式：給日 K（dates/open/high/low/close/volume）和 asof 日期，算技術面特徵。
    只用 asof（含）以前的資料、不連網、不讀檔、不改輸入；同樣輸入永遠得到同樣輸出。
    即時判斷用 technicals(o, tw)（= features(o, 最後一天, tw)），回測可以對同一份 K 線逐日呼叫。"""
    o = slice_asof(ohlcv, asof)
    c, h, lo, v, d = o["close"], o["high"], o["low"], o["volume"], o["dates"]
    n = len(c)

    def ma(k, end=n):
        return sum(c[end - k:end]) / k if end >= k else None

    def slope(k, back=5):
        a, b = ma(k), ma(k, n - back)
        return _r((a - b) / b * 100) if a and b else None
    # RSI14（Wilder）
    gains = [max(c[i] - c[i - 1], 0) for i in range(1, n)]
    losses = [max(c[i - 1] - c[i], 0) for i in range(1, n)]
    rsi = None
    if n > 15:
        ag, al = sum(gains[:14]) / 14, sum(losses[:14]) / 14
        for g, l_ in zip(gains[14:], losses[14:]):
            ag, al = (ag * 13 + g) / 14, (al * 13 + l_) / 14
        rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    # ATR14（Wilder）
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, n)]
    atr = None
    if len(tr) >= 14:
        atr = sum(tr[:14]) / 14
        for x in tr[14:]:
            atr = (atr * 13 + x) / 14
    unit = 1000 if tw else 1
    vol = v[-1] / unit
    avg20 = sum(v[-21:-1]) / 20 / unit if n > 21 else None
    one_year = dt.date.fromisoformat(d[-1]) - dt.timedelta(days=365)
    idx = [i for i, x in enumerate(d) if dt.date.fromisoformat(x) >= one_year]
    hi52, lo52 = max(h[i] for i in idx), min(lo[i] for i in idx)
    from tools.history import analyze
    a = analyze(d, [round(x, 2) for x in c], recent=10)
    return {
        "last_date": d[-1], "close": _r(c[-1]), "change_1d_pct": a["recent"][-1]["change_pct"],
        "change_5d_pct": (a["change_5d"] or {}).get("change_pct"), "change_20d_pct": (a["change_20d"] or {}).get("change_pct"),
        "streak": {"direction": a["streak"]["direction"], "days": a["streak"]["days"], "cum_change_pct": a["streak"]["cum_change_pct"]},
        "ma": {f"ma{k}": _r(ma(k)) for k in (5, 10, 20, 60)},
        "ma_slope_5d_pct": {f"ma{k}": slope(k) for k in (5, 10, 20, 60)},
        "rsi14": _r(rsi, 1), "atr14": _r(atr), "atr_pct": _r(atr / c[-1] * 100) if atr else None,
        "high20": _r(max(h[-20:])), "low20": _r(min(lo[-20:])), "high60": _r(max(h[-60:])), "low60": _r(min(lo[-60:])),
        "high52": _r(hi52), "low52": _r(lo52),
        "pos52_pct": _r((c[-1] - lo52) / (hi52 - lo52) * 100, 1) if hi52 > lo52 else None,
        "volume": _r(vol, 0), "volume_avg20": _r(avg20, 0), "volume_ratio": _r(vol / avg20) if avg20 else None,
        "volume_unit": "張" if tw else "股",
        "ext": features_ext.ext(o),   # MACD、KD、布林、乖離、OBV／背離、K 棒型態、擺盪點、箱型、週 K（tools/features_ext.py）
    }


def technicals(o: dict, tw: bool) -> dict:
    """即時用：最後一根 K 棒當 asof（行為與重構前完全相同）。"""
    return features(o, None, tw)


# ---------- 你的持倉（交易日記）----------
def position(tg: dict) -> dict:
    from journal import db
    if tg["kind"] not in ("tw_stock", "us_stock"):
        return {"has_position": False, "note": "指數沒有持倉"}
    rows = db.list_trades(ticker=tg["symbol"], side="buy", status="open", include_test="test" in db.DB_PATH.name)
    rows = [r for r in rows if (r.get("remaining_shares") or 0) > 0]
    if not rows:
        return {"has_position": False}
    sh = sum(r["remaining_shares"] for r in rows)
    cost = sum(r["price"] * r["remaining_shares"] for r in rows) / sh
    last = sorted(rows, key=lambda r: (r["trade_date"], r["id"]))[-1]
    stop = last.get("stop_loss")
    targets = sorted({float(t) for r in rows for t in (r.get("target_prices") or [])})
    return {"has_position": True, "shares": sh, "lots": round(sh / 1000, 3), "avg_cost": _r(cost),
            "stop_loss": stop, "targets": targets, "trade_ids": [r["id"] for r in rows],
            "first_date": min(r["trade_date"] for r in rows)}


def earnings(tg: dict) -> dict:
    if tg["kind"] not in ("tw_stock", "us_stock"):
        return {"known": False, "note": "指數沒有財報日"}
    try:
        import yfinance as yf
        cal = yf.Ticker(tg["yf"]).calendar or {}
        ds = [x for x in (cal.get("Earnings Date") or []) if isinstance(x, dt.date)]
        today = now_tpe().date()
        fut = sorted(x for x in ds if x >= today)
        if fut:
            return {"known": True, "date": fut[0].isoformat(), "days_away": (fut[0] - today).days, "source": "yfinance calendar"}
        return {"known": False, "note": "yfinance 沒有下一次財報日"}
    except Exception as e:  # noqa: BLE001
        return {"known": False, "note": f"查不到（{type(e).__name__}）"}


# ---------- 證據與價位 ----------
def _p(v):
    return "—" if v is None else f"{v:+.2f}%"


def _n(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


def build_levels(t: dict, pos: dict) -> list[dict]:
    cand = [("MA5", t["ma"]["ma5"]), ("MA10", t["ma"]["ma10"]), ("MA20", t["ma"]["ma20"]), ("MA60", t["ma"]["ma60"]),
            ("20 日高點", t["high20"]), ("20 日低點", t["low20"]), ("60 日高點", t["high60"]), ("60 日低點", t["low60"]),
            ("52 週高點", t["high52"]), ("52 週低點", t["low52"])]
    sw = (t.get("ext") or {}).get("swing") or {}
    cand += [("前波高點", sw.get("high")), ("前波低點", sw.get("low"))]   # 擺盪點（左右各 3 根）
    if pos.get("has_position"):
        cand += [("你的成本", pos["avg_cost"]), ("你的停損", pos.get("stop_loss"))]
        cand += [(f"你的目標{i + 1}", x) for i, x in enumerate(pos.get("targets") or [])]
    out = []
    for name, v in cand:
        if v is None:
            continue
        v = _r(v)
        out.append({"name": name, "value": v, "dist_pct": _r((v - t["close"]) / t["close"] * 100),
                    "kind": "壓力" if v > t["close"] else "支撐" if v < t["close"] else "現價"})
    out.sort(key=lambda x: x["value"])
    for i, x in enumerate(out, 1):
        x["id"] = f"L{i}"
    return out


def build_items(b: dict) -> list[dict]:
    t, pos, items = b["tech"], b["position"], []
    lv = b["levels"]

    def add(cat, side, text):
        items.append({"cat": cat, "side": side, "text": text})
    c, ma, sl = t["close"], t["ma"], t["ma_slope_5d_pct"]
    px = "現價" if t.get("intraday") else "收盤"
    # 技術
    for k, lab in (("ma20", "MA20"), ("ma60", "MA60")):
        if ma[k] is None:
            continue
        above, up = c > ma[k], (sl[k] or 0) > 0
        side = "偏多" if above and up else "偏空" if not above and not up else "中性"
        add("技術", side, f"{px} {_n(c)} {'站上' if above else '低於'} {lab} {_n(ma[k])}（距離 {_p((c - ma[k]) / ma[k] * 100)}），"
                         f"{lab} 5 日斜率 {_p(sl[k])}（{'上升' if up else '下降'}）")
    if all(ma[k] for k in ("ma5", "ma10", "ma20", "ma60")):
        if ma["ma5"] > ma["ma10"] > ma["ma20"] > ma["ma60"]:
            add("技術", "偏多", f"均線多頭排列：MA5 {_n(ma['ma5'])} > MA10 {_n(ma['ma10'])} > MA20 {_n(ma['ma20'])} > MA60 {_n(ma['ma60'])}")
        elif ma["ma5"] < ma["ma10"] < ma["ma20"] < ma["ma60"]:
            add("技術", "偏空", f"均線空頭排列：MA5 {_n(ma['ma5'])} < MA10 {_n(ma['ma10'])} < MA20 {_n(ma['ma20'])} < MA60 {_n(ma['ma60'])}")
    if t["rsi14"] is not None:
        r = t["rsi14"]
        side, word = ("偏多", "超賣區，短線反彈機率較高") if r < 30 else ("偏空", "過熱區") if r > 70 else \
            ("偏空", "偏弱") if r < 45 else ("中性", "中性區")
        add("技術", side, f"RSI14 {r}（{word}）")
    st = t["streak"]
    if st["direction"] != "flat" and st["days"] >= 2:
        add("技術", "偏空" if st["direction"] == "down" else "偏多",
            f"連{'跌' if st['direction'] == 'down' else '漲'} {st['days']} 天，累計 {_p(st['cum_change_pct'])}；5 日 {_p(t['change_5d_pct'])}、20 日 {_p(t['change_20d_pct'])}")
    sup = [x for x in lv if x["kind"] == "支撐" and not x["name"].startswith("你的")]
    res = [x for x in lv if x["kind"] == "壓力" and not x["name"].startswith("你的")]
    if sup:
        s = sup[-1]
        add("技術", "偏多" if s["dist_pct"] > -3 else "中性", f"最近支撐 {s['name']} {_n(s['value'])}（距離 {_p(s['dist_pct'])}）")
    else:
        add("技術", "偏空", "下方沒有近期支撐（已在 52 週低點附近或以下）")
    if res:
        s = res[0]
        add("技術", "偏空" if s["dist_pct"] < 3 else "中性", f"最近壓力 {s['name']} {_n(s['value'])}（距離 {_p(s['dist_pct'])}）")
    if t["volume_ratio"] is not None:
        down = (t["change_1d_pct"] or 0) < 0
        vr = t["volume_ratio"]
        txt = f"成交量 {_n(t['volume'])} {t['volume_unit']}，是 20 日均量 {_n(t['volume_avg20'])} {t['volume_unit']} 的 {vr} 倍"
        if t.get("intraday"):   # 盤中累計量跟整天的均量比會誤判成量縮，不當多空證據
            add("技術", "中性", f"盤中累計成交量 {_n(t['volume'])} {t['volume_unit']}（還沒收盤，20 日均量 {_n(t['volume_avg20'])} {t['volume_unit']}，量能等收盤再判斷）")
        elif vr >= 1.5:
            add("技術", "偏空" if down else "偏多", txt + ("（放量下跌）" if down else "（放量上漲）"))
        elif vr <= 0.7:
            add("技術", "偏多" if down else "偏空", txt + ("（量縮下跌，賣壓減輕）" if down else "（量縮上漲，追價力道弱）"))
        else:
            add("技術", "中性", txt)
    if t["pos52_pct"] is not None:
        add("技術", "中性", f"在 52 週區間的 {t['pos52_pct']}% 位置（低 {_n(t['low52'])}、高 {_n(t['high52'])}）")
    if t["atr14"]:
        add("技術", "中性", f"波動：ATR14 {_n(t['atr14'])}（{t['atr_pct']}%），一天正常波動約 {_n(t['atr14'])}")
    # 技術（延伸）：MACD、KD、布林、乖離、背離、K 棒型態、擺盪點、箱型、週線與多週期一致性
    for cat, side, text, key in features_ext.ext_items(t):
        items.append({"cat": cat, "side": side, "text": text, "key": key})
    # 籌碼
    inst = b.get("institutional")
    if inst and inst.get("days"):
        for k, lab in (("foreign", "外資"), ("trust", "投信")):
            s_ = inst["streak"][k]
            tot = inst["sum"][k]
            if s_["days"] >= 2:
                add("籌碼", "偏空" if s_["side"] == "sell" else "偏多",
                    f"{lab}連{'賣' if s_['side'] == 'sell' else '買'} {s_['days']} 天，合計 {s_['sum']:+,} 張；近 {len(inst['days'])} 日合計 {tot:+,} 張")
            elif tot:
                add("籌碼", "偏空" if tot < 0 else "偏多", f"{lab}近 {len(inst['days'])} 日合計 {tot:+,} 張（最近一天 {inst['days'][-1][k]:+,} 張）")
    # 環境
    g = b.get("peers") or {}
    if g.get("count"):
        side = "偏空" if g["down"] >= max(2, g["count"] * 0.6) else "偏多" if g["up"] >= max(2, g["count"] * 0.6) else "中性"
        add("環境", side, f"同族群（{g['how']}）{g['count']} 檔：跌 {g['down']}、漲 {g['up']}，平均 {_p(g['avg_1d_pct'])}，5 日平均 {_p(g['avg_5d_pct'])}")
    for ix in b.get("index") or []:
        if ix.get("error"):
            continue
        s_ = ix["streak"]
        if s_["days"] >= 2 and s_["direction"] != "flat":
            add("環境", "偏空" if s_["direction"] == "down" else "偏多",
                f"{ix['name']}連{'跌' if s_['direction'] == 'down' else '漲'} {s_['days']} 天，累計 {_p(s_['cum_change_pct'])}")
        else:
            add("環境", "中性", f"{ix['name']} 最近一天 {_p(ix['change_1d_pct'])}，5 日 {_p(ix['change_5d_pct'])}")
    n = b.get("news") or {}
    if b.get("news_evidence"):
        pass   # 新聞改成 N1…N5（各自帶日期與情緒），在下面編號
    elif n.get("items"):
        add("新聞", "中性", f"近 {n['days']} 天 {n['count']} 則新聞標題提到，最新：「{n['items'][0]['title']}」")
    else:
        add("新聞", "中性", "近 7 天沒有找到標題提到它的新聞")
    e = b.get("earnings") or {}
    if e.get("known"):
        add("事件", "中性", f"下一次財報日 {e['date']}（{e['days_away']} 天後），前後波動可能放大")
    elif b["target"]["kind"] in ("tw_stock", "us_stock"):
        add("事件", "中性", "下一次財報日：查不到")
    # 持倉
    if pos.get("has_position"):
        pnl = (c - pos["avg_cost"]) / pos["avg_cost"] * 100
        add("持倉", "中性", f"你持有 {pos['shares']:,} 股，平均成本 {_n(pos['avg_cost'])}，現價 {_n(c)}，未實現 {_p(pnl)}")
        if pos.get("stop_loss"):
            sp = pos["stop_loss"]
            if c <= sp:
                add("持倉", "偏空", f"現價 {_n(c)} 已跌破你的停損 {_n(sp)}（{_p((c - sp) / sp * 100)}）")
            else:
                add("持倉", "中性", f"距離你的停損 {_n(sp)} 還有 {_p((sp - c) / c * 100)}")
        for i, x in enumerate(pos.get("targets") or [], 1):
            add("持倉", "中性", f"你的目標{i} {_n(x)}，距離 {_p((x - c) / c * 100)}")
    for x in items:
        x["key"] = rule_key(x)
    apply_calibration(items)
    for i, x in enumerate(items, 1):
        x["id"] = f"E{i}"
    # 新聞證據 N#：情緒來自 7B 分類（快取在新聞庫）或關鍵字規則；中性的只當背景
    side_of = {"利多": "偏多", "利空": "偏空"}
    for j, x in enumerate(b.get("news_evidence") or [], 1):
        from tools.news_store import basis_label
        who = basis_label(x)   # step12：模型讀內文／模型看標題／關鍵字
        extra = (f"；依據：「{x['quote'][:60]}」" if x.get("aspect") and x.get("quote")
                 else f"；內文：「{x['snippet'][:60]}」" if x.get("snippet") else "")
        if x.get("exaggerated"):
            extra += "；⚠ 標題比內文誇大"
        items.append({"cat": "新聞", "side": side_of.get(x["sentiment"], "中性"), "id": f"N{j}", "key": None,
                      "text": f"{x['published'][:10]} {x.get('publisher') or ''}：「{x['title']}」（{x['event']}，{x['sentiment']}，{who}{extra}）"})
    return items


# ---------- 證據規則代號與回測校準 ----------
_KEY_PATTERNS = [
    (r"(站上|低於) MA20", lambda m: "ma20_above" if m.group(1) == "站上" else "ma20_below"),
    (r"(站上|低於) MA60", lambda m: "ma60_above" if m.group(1) == "站上" else "ma60_below"),
    (r"^均線多頭排列", lambda m: "ma_bull_stack"), (r"^均線空頭排列", lambda m: "ma_bear_stack"),
    (r"^RSI14 .*（(超賣|過熱|偏弱|中性)", lambda m: {"超賣": "rsi_oversold", "過熱": "rsi_overbought", "偏弱": "rsi_weak", "中性": "rsi_mid"}[m.group(1)]),
    (r"^連(漲|跌) \d+ 天", lambda m: "streak_up" if m.group(1) == "漲" else "streak_down"),
    (r"^最近支撐", lambda m: "support"), (r"^下方沒有近期支撐", lambda m: "no_support"), (r"^最近壓力", lambda m: "resistance"),
    (r"（(放量上漲|放量下跌|量縮下跌|量縮上漲)", lambda m: {"放量上漲": "vol_up_big", "放量下跌": "vol_down_big",
                                                    "量縮下跌": "vol_down_shrink", "量縮上漲": "vol_up_shrink"}[m.group(1)]),
    (r"^在 52 週區間", lambda m: "pos52"), (r"^波動：ATR14", lambda m: "atr"),
]


def rule_key(x: dict) -> str | None:
    """技術面證據的規則代號（回測統計與校準用）。其他類別回傳 None。"""
    if x.get("key"):
        return x["key"]
    if x.get("cat") != "技術":
        return None
    for pat, f in _KEY_PATTERNS:
        m = re.search(pat, x["text"])
        if m:
            return f(m)
    return None


_CALIB = None


def calibration() -> dict:
    """data/evidence_calibration.json：{"overrides": {"rule|side": "中性"}}。回測顯示方向無效的證據改標中性（不當多空理由）。"""
    global _CALIB
    if _CALIB is None:
        from tools._common import DATA
        p = DATA / "evidence_calibration.json"
        try:
            _CALIB = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:  # noqa: BLE001
            _CALIB = {}
    return _CALIB


def apply_calibration(items: list[dict]) -> None:
    ov = (calibration().get("overrides") or {})
    for x in items:
        k = x.get("key")
        if k and f"{k}|{x['side']}" in ov:
            x["calibrated_from"] = x["side"]
            x["side"] = ov[f"{k}|{x['side']}"]


def decision_brief(target: str, market: str = "auto", question: str = "") -> dict:
    from tools import explain
    t0 = time.time()
    tg = explain._target(target, market)
    if not tg:
        return {"found": False, "query": target, "tried": ["證交所/櫃買公司資料", "美股指數名稱", "yfinance／新聞公司名稱解析"],
                "as_of": iso_tpe()}
    yf_sym = tg.get("yf") or {"TAIEX": "^TWII"}.get(tg["symbol"], tg["symbol"])
    tw = tg["kind"] in ("tw_stock", "tw_index")
    b = {"found": True, "query": target, "target": tg, "as_of": iso_tpe(), "errors": []}
    with ThreadPoolExecutor(6) as ex:
        f_px = ex.submit(_ohlcv, yf_sym)
        f_news = ex.submit(explain.recent_news, tg)
        f_idx = ex.submit(explain.index_context, tg)
        f_earn = ex.submit(earnings, tg)
        f_peer = ex.submit(explain.peers_us, tg) if tg["kind"] == "us_stock" else \
            ex.submit(explain.peers_us_index, tg["symbol"]) if tg["kind"] == "us_index" else None
        try:
            o = f_px.result()
            LAST_OHLCV[yf_sym] = o          # 給 K 線圖用（不放進 b，避免把整串價格塞進驗證用的工具輸出）
            b["tech"] = technicals(o, tw)
            # 交易時段內最後一根日 K 還沒收盤：文字一律寫「現價」，成交量是盤中累計
            b["tech"]["intraday"] = bar_label(b["tech"]["last_date"], us=not tw) == "盤中"
            dates = o["dates"][-10:]
        except Exception as e:  # noqa: BLE001
            return {"found": False, "query": target, "target": tg, "tried": [f"價格資料抓取失敗：{type(e).__name__}: {e}"], "as_of": iso_tpe()}
        f_inst = ex.submit(explain.stock_institutional, tg["symbol"], tg["listed"], 10, dates) \
            if tg["kind"] == "tw_stock" else None
        try:
            b["position"] = position(tg)
            P, c = b["position"], b["tech"]["close"]
            if P.get("has_position") and P.get("avg_cost"):
                P["market_value"] = round(P["shares"] * c)
                P["unrealized"] = round((c - P["avg_cost"]) * P["shares"])
                P["unrealized_pct"] = _r((c - P["avg_cost"]) / P["avg_cost"] * 100)
        except Exception as e:  # noqa: BLE001
            b["position"] = {"has_position": False, "error": f"{type(e).__name__}: {e}"}
        news = f_news.result()
        b["news"] = {k: v for k, v in news.items() if k != "all_titles"}
        try:   # P1-E：存進新聞庫並做時間衰減檢索，取前 5 則當 N1…N5 證據
            from tools import news_store
            b["news_evidence"] = news_store.news_evidence(tg, news)
        except Exception as e:  # noqa: BLE001
            b["news_evidence"] = []
            b["errors"].append(f"新聞庫：{type(e).__name__}: {e}")
        try:
            if tg["kind"] == "tw_stock":
                b["peers"] = explain.peers_tw(tg, news["all_titles"] + [x["title"] for x in explain._gnews_items(f"{tg['name']} 族群")])
            elif f_peer:
                b["peers"] = f_peer.result()
        except Exception as e:  # noqa: BLE001
            b["errors"].append(f"同族群：{type(e).__name__}: {e}")
        if f_inst:
            try:
                b["institutional"] = f_inst.result()
            except Exception as e:  # noqa: BLE001
                b["errors"].append(f"法人：{type(e).__name__}: {e}")
        b["index"] = f_idx.result()
        b["earnings"] = f_earn.result()
    b["levels"] = build_levels(b["tech"], b["position"])
    b["items"] = build_items(b)
    b["question_type"] = question_type(question)
    b["tech_focus"] = bool(TECH_FOCUS_RE.search(question or ""))
    b["elapsed_s"] = round(time.time() - t0, 1)
    return b


# 使用者明講要看 K 線／技術面：技術面的證據排前面（不足的由程式補上同方向的技術面證據）
TECH_FOCUS_RE = re.compile(r"[KkＫｋ]\s*線|線圖|線型|技術面|技術分析|型態|均線|圖形", re.I)


def question_type(q: str) -> str:
    if re.search(r"停損|停利|賣|出場|續抱|抱|減碼|獲利了結", q or ""):
        return "出場／續抱"
    if re.search(r"加碼|攤平", q or ""):
        return "加碼"
    if re.search(r"抄底|接|撿", q or ""):
        return "抄底"
    if re.search(r"買|進場|布局|佈局", q or ""):
        return "進場"
    return "操作"


# ---------- 判斷（14B）----------
def _brief_prompt(b: dict, question: str) -> str:
    tg = b["target"]
    pos = b["position"]
    lines = [f"使用者問：{question}", f"問題類型：{b['question_type']}",
             f"標的：{tg['name']}（{tg['symbol']}），現價 {_n(b['tech']['close'])}，資料日期 {b['tech']['last_date']}",
             "持倉：" + (f"有，{pos['shares']} 股，成本 {_n(pos['avg_cost'])}，停損 {pos.get('stop_loss')}，目標 {pos.get('targets')}"
                       if pos.get("has_position") else "沒有持倉紀錄"),
             "證據（id｜類別｜程式判斷｜內容）："]
    lines += [f"{x['id']}｜{x['cat']}｜{x['side']}｜{x['text']}" for x in b["items"]]
    lines.append("價位（id｜名稱｜價格｜支撐或壓力｜距離）：")
    lines += [f"{x['id']}｜{x['name']}｜{_n(x['value'])}｜{x['kind']}｜{_p(x['dist_pct'])}" for x in b["levels"]]
    return "\n".join(lines)


JUDGE_SYSTEM = """你是嚴謹的台股交易教練，幫使用者「做判斷」，不是只報資料。只能根據使用者給的證據和價位推論，不能用外部知識或自己編數字。
輸出 JSON（不要其他文字）：
{"lean":"偏多|中性|偏空","confidence":"低|中|高","summary":"一句話結論（20 字內，直接回答能不能做）",
 "bullish":[{"id":"E? 或 N?","point":"為什麼偏多（引用該證據的數字）"}],
 "bearish":[{"id":"E? 或 N?","point":"為什麼偏空（引用該證據的數字）"}],
 "scenarios":[{"level_id":"L?","trigger":"站回／跌破／突破／守住","then":"接下來怎麼做"}],
 "rules_check":["對照使用者持倉、停損、部位的檢查（沒有持倉就說沒有）"],
 "watch":["接下來要觀察什麼（例如法人轉買、費半止跌、財報日）"]}
規則：
1. 一定要給傾向，不要用「需考慮多方因素」「請謹慎評估」這類空話。證據互相矛盾時選中性並說明關鍵分歧。
2. bullish、bearish 各最多 3 點，每點的 id 必須是上面證據的 id（技術／籌碼／環境是 E#，新聞是 N#），數字只能抄證據裡的。summary、watch、rules_check 裡如果提到 id，只能是 E#／N#／L#，不要自己編 id。
3. scenarios 2 到 3 個，level_id 必須是上面價位的 id；trigger 和 then 不要寫價格數字（程式會填）。價位標「支撐」（在現價下方）的 trigger 只能用 跌破／守住；標「壓力」（在現價上方）的只能用 站回／突破。then 要寫具體動作（進場、不接、停損、減碼、續抱、等待），不要寫「需謹慎」「密切關注」。
4. 問抄底／進場／加碼時：已跌破停損、空頭排列、法人連賣、族群一起跌都是不利證據；問停損時，以使用者自己的停損為最高原則。
5. 不要給保證，不要預測確定的漲跌。用台灣繁體中文。"""


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")


def _allowed(b: dict) -> set[float]:
    blob = json.dumps({k: b.get(k) for k in ("items", "levels", "tech", "position", "earnings")}, ensure_ascii=False, default=str)
    blob += " " + " ".join(x["text"] for x in b["items"])
    vals = set()
    for s in _numbers(blob):
        try:
            v = float(s.replace(",", ""))
        except ValueError:
            continue
        vals |= {round(v, 2), round(v, 1), round(v)}
    return vals


def _nums_ok(text: str, allowed: set[float]) -> bool:
    for s in _numbers(text):
        v = float(s.replace(",", ""))
        if v <= 3 and v == int(v):
            continue
        if not any(abs(v - a) < 0.006 for a in allowed):
            return False
    return True


def _call_judge(prompt: str, system: str | None = None, purpose: str = "決策判斷") -> tuple[dict | None, dict]:
    from tools._common import ollama_chat
    info = {"model": JUDGE_MODEL}
    t0 = time.time()
    try:
        # 7B 和 14B 放不進同一張 12GB 顯卡：ollama_chat 用共用鎖一次只跑一個請求，讓 Ollama 依序換模型
        j = ollama_chat({
            "model": JUDGE_MODEL, "stream": False, "format": "json", "keep_alive": JUDGE_KEEP_ALIVE,
            "options": {"temperature": 0.1, "num_ctx": 6144},
            "messages": [{"role": "system", "content": system or JUDGE_SYSTEM}, {"role": "user", "content": prompt}]},
            purpose, timeout=300)
        info.update({"load_s": round(j.get("load_duration", 0) / 1e9, 1), "eval_s": round(j.get("eval_duration", 0) / 1e9, 1),
                     "prompt_s": round(j.get("prompt_eval_duration", 0) / 1e9, 1), "total_s": round(time.time() - t0, 1),
                     "tokens": j.get("eval_count")})
        return json.loads(j["message"]["content"]), info
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
        info["total_s"] = round(time.time() - t0, 1)
        return None, info


def _to_tw(s: str) -> str:
    try:
        import agent
        return agent.to_tw(s)
    except Exception:  # noqa: BLE001
        return s


def judge(b: dict, question: str) -> dict:
    raw, info = _call_judge(_brief_prompt(b, question))
    allowed = _allowed(b)
    ids = {x["id"]: x for x in b["items"]}
    lv = {x["id"]: x for x in b["levels"]}
    out = {"by": "model", "info": info, "dropped": []}
    if not isinstance(raw, dict):
        out.update(code_verdict(b))
        out["by"] = "code_fallback"
        return out

    def clean(s):
        return _to_tw(str(s or "").strip())

    known_ids = set(ids) | set(lv)

    def ok_text(s):
        # P2-F：文字裡出現的證據代號（E3、N2、L4）必須真的存在
        bad_id = any(m not in known_ids for m in re.findall(r"(?<![A-Za-z])[ENL]\d{1,2}(?!\d)", s or ""))
        return s and not FILLER.search(s) and _nums_ok(s, allowed) and not bad_id
    out["lean"] = raw.get("lean") if raw.get("lean") in ("偏多", "中性", "偏空") else None
    out["confidence"] = raw.get("confidence") if raw.get("confidence") in ("低", "中", "高") else "低"
    sm = clean(raw.get("summary"))
    out["summary"] = sm if ok_text(sm) else None
    for key, sides in (("bullish", ("偏多", "中性")), ("bearish", ("偏空", "中性"))):
        keep = []
        for p in raw.get(key) or []:
            if not isinstance(p, dict):
                continue
            pid, pt = str(p.get("id", "")).strip(), clean(p.get("point"))
            neutral_pos = pid in ids and ids[pid]["side"] == "中性" and ids[pid]["cat"] == "持倉"  # 目標距離、持股資訊不算多空理由
            if pid in ids and ids[pid]["side"] in sides and not neutral_pos and ok_text(pt) and pid not in [k["id"] for k in keep]:
                keep.append({"id": pid, "point": pt})
            else:
                out["dropped"].append({key: p})
        out[key] = keep[:3]
    sc = []
    for s in raw.get("scenarios") or []:
        if not isinstance(s, dict):
            continue
        lid, trig, then = str(s.get("level_id", "")).strip(), clean(s.get("trigger")), clean(s.get("then"))
        # 情境的價位只能來自程式算的 levels：trigger/then 裡如果出現數字，必須等於某個 level
        lvl_vals = {x["value"] for x in b["levels"]}
        bare = trig + " " + then
        for x in b["levels"]:   # 「20 日低點」「MA60」這類名稱裡的數字不是價位
            bare = bare.replace(x["name"], " ")
        bare = re.sub(r"MA\d+|\d+\s*(日|天|週|周)", " ", bare)
        nums_bad = [n for n in _numbers(bare)
                    if float(n.replace(",", "")) > 3 and not any(abs(float(n.replace(",", "")) - v) < 0.006 for v in lvl_vals)]
        # 觸發方向要對：價位在現價下方只能「跌破／守住／回測」，在上方只能「站回／突破／站上／反彈」
        below = lid in lv and lv[lid]["value"] < b["tech"]["close"]
        dir_ok = lid in lv and (re.search(r"跌破|守住|回測|守穩|跌到|跌至|失守", trig) if below
                                else re.search(r"站回|突破|站上|反彈|漲到|漲至|收復|觸及|過", trig))
        # 動作要和觸發、持倉一致：跌破後不能說進場／加碼，突破後不能說不接／停損；沒有持倉不談續抱／減碼；
        # 已跌破自己的停損就不能建議續抱／加碼／抄底（使用者自己的規則優先）
        P = b.get("position") or {}
        breached = P.get("has_position") and P.get("stop_loss") and b["tech"]["close"] < P["stop_loss"]
        act_bad = (re.search(r"跌破|失守", trig) and re.search(r"進場|加碼|抄底|買進|續抱|轉強", then)) or \
            (re.search(r"站回|突破|站上|收復", trig) and re.search(r"不接|停損|減碼|出場|賣出|轉弱", then)) or \
            (not P.get("has_position") and (re.search(r"續抱|減碼|出場", then) or ("停損" in then and "持有" not in then))) or \
            (breached and re.search(r"續抱|加碼|抄底|攤平", then))
        if lid in lv and trig and then and not nums_bad and not FILLER.search(then) and dir_ok and not act_bad:
            sc.append({"level_id": lid, "trigger": trig, "then": then})
        else:
            out["dropped"].append({"scenario": s})
    out["scenarios"] = sc[:3]
    out["rules_check"] = [x for x in (clean(r) for r in raw.get("rules_check") or []) if ok_text(x)][:2]
    out["watch"] = [x for x in (clean(w) for w in raw.get("watch") or []) if ok_text(x)][:3]
    cv = code_verdict(b)
    if not out["lean"]:
        out["lean"], out["lean_by"] = cv["lean"], "code"
    for k in ("bullish", "bearish", "scenarios", "watch"):
        if not out[k]:
            out[k] = cv[k]
            out.setdefault("filled_by_code", []).append(k)
    if len(out["scenarios"]) < 2:   # 情境至少兩個（一上一下），不足用程式的補
        have = {x["level_id"] for x in out["scenarios"]}
        add = [x for x in cv["scenarios"] if x["level_id"] not in have][:2 - len(out["scenarios"])]
        if add:
            out["scenarios"] += add
            out.setdefault("filled_by_code", []).append("scenarios")
    return out


def code_verdict(b: dict) -> dict:
    """模型失敗或缺項時的程式版本：依證據偏多／偏空數量給中性為主的傾向。"""
    items = b["items"]
    bull = [x for x in items if x["side"] == "偏多"]
    bear = [x for x in items if x["side"] == "偏空"]
    score = len(bull) - len(bear) - (2 if any("已跌破你的停損" in x["text"] for x in bear) else 0)
    lean = "偏多" if score >= 3 else "偏空" if score <= -3 else "中性"
    lv = b["levels"]
    sup = [x for x in lv if x["kind"] == "支撐"]
    res = [x for x in lv if x["kind"] == "壓力"]
    sc = []
    if res:
        sc.append({"level_id": res[0]["id"], "trigger": "站回", "then": "短線轉強，再評估是否進場"})
    if sup:
        sc.append({"level_id": sup[-1]["id"], "trigger": "跌破", "then": "弱勢延續，先不要接；持有者照停損"})
    watch = []
    if any(x["cat"] == "籌碼" and x["side"] == "偏空" for x in items):
        watch.append("法人是否由賣轉買")
    if any("費城半導體連跌" in x["text"] for x in items):
        watch.append("費半是否止跌")
    if (b.get("earnings") or {}).get("known"):
        watch.append(f"財報日 {b['earnings']['date']}")
    return {"lean": lean, "confidence": "低", "summary": None,
            "bullish": [{"id": x["id"], "point": x["text"]} for x in bull[:3]],
            "bearish": [{"id": x["id"], "point": x["text"]} for x in bear[:3]],
            "scenarios": sc, "rules_check": [], "watch": watch or ["等站回最近壓力或守住最近支撐再決定"]}


# ---------- 規則檢查（程式）----------
def rules_check(b: dict) -> list[str]:
    tg, t, pos = b["target"], b["tech"], b["position"]
    qt = b["question_type"]
    out = []
    if tg["kind"] not in ("tw_stock", "us_stock"):
        return ["指數沒有持倉檢查。"]
    if not pos.get("has_position"):
        nm = tg["symbol"] if tg["kind"].startswith("us") else tg["name"]
        out.append(f"日記裡沒有 {nm} 的持倉紀錄。要記錄現有部位可以說「我持有{nm} ○股 成本○ 停損○ 目標○、○」，確認後才會存。")
        if qt in ("抄底", "進場", "加碼"):
            sup = [x for x in b["levels"] if x["kind"] == "支撐"]
            if sup and t.get("atr14"):
                out.append(f"如果要進場，先決定停損：最近支撐 {sup[-1]['name']} {_n(sup[-1]['value'])}，"
                           f"一天正常波動（ATR14）約 {_n(t['atr14'])}，停損放太近容易被洗掉。")
        return out
    c = t["close"]
    out.append(f"持有 {pos['shares']:,} 股，平均成本 {_n(pos['avg_cost'])}，市值約 {_n(round(pos['shares'] * c))} 元，未實現 {_p((c - pos['avg_cost']) / pos['avg_cost'] * 100)}。")
    sp = pos.get("stop_loss")
    if not sp:
        out.append("你沒有設停損，先補上停損再談加碼或抄底。")
    elif c <= sp:
        out.append(f"現價 {_n(c)} 已跌破你的停損 {_n(sp)}：依你自己的規則應該出場，不是{qt if qt in ('加碼', '抄底', '進場') else '續抱'}。")
    else:
        out.append(f"還沒跌破你的停損 {_n(sp)}（距離 {_p((sp - c) / c * 100)}）。")
    if qt in ("抄底", "加碼", "進場") and c < pos["avg_cost"]:
        out.append("你已經是虧損部位，這時再買＝攤平，會讓虧損部位變大；你的停損規則優先。")
    return out


# ---------- 回覆（程式產生）----------
def intraday_words(x, on: bool):
    """盤中時，模型寫的「收盤／收盤價」改成「現價」（最後一根日 K 還沒收盤）。"""
    if not on:
        return x
    if isinstance(x, str):
        return re.sub(r"收盤價|收盤", "現價", x)
    if isinstance(x, list):
        return [intraday_words(y, on) for y in x]
    if isinstance(x, dict):
        return {k: intraday_words(y, on) for k, y in x.items()}
    return x


def render(b: dict, v: dict) -> str:
    from tools.explain import label_of
    tg, t = b["target"], b["tech"]
    v = {**v, **{k: intraday_words(v.get(k), bool(t.get("intraday"))) for k in ("summary", "watch", "scenarios") if k in v}}
    ids = {x["id"]: x for x in b["items"]}
    lv = {x["id"]: x for x in b["levels"]}
    md = f"{int(t['last_date'][5:7])}/{int(t['last_date'][8:10])}"
    head = f"結論傾向：{v['lean']}（信心：{v['confidence']}）"
    if v.get("summary"):
        head += f"｜{v['summary']}"
    lines = [head, f"（{label_of(tg)} 現價 {_n(t['close'])}，截至 {md} {bar_label(t['last_date'], us=tg['kind'].startswith('us'))}；你問的是：{b['question_type']}）"]
    if b.get("tech_focus"):
        lines.append("（你問的是 K 線／技術面：技術面的理由排在前面；籌碼、新聞、大盤放後面當參考）")
    for key, lab in (("bullish", "偏多理由"), ("bearish", "偏空理由")):
        pts = list(v.get(key) or [])
        if b.get("tech_focus"):
            side = "偏多" if key == "bullish" else "偏空"
            have = {p["id"] for p in pts}
            extra = [{"id": x["id"]} for x in b["items"] if x["cat"] == "技術" and x["side"] == side and x["id"] not in have]
            n_tech = sum(1 for p in pts if ids.get(p["id"], {}).get("cat") == "技術")
            pts += extra[:max(0, 2 - n_tech)]
            pts.sort(key=lambda p: 0 if ids.get(p["id"], {}).get("cat") == "技術" else 1)
        lines.append(f"\n{lab}：" if pts else f"\n{lab}：沒有明顯的{lab[:2]}證據")
        # 模型只負責挑選與排序；顯示的文字一律用程式寫的證據原文（避免模型把數字配錯欄位，例如把距離說成斜率）
        lines += [f"- {ids[p['id']]['text']}［{ids[p['id']]['cat']}］" for p in pts]
    if v.get("scenarios"):
        lines.append("\n情境（價位都是程式算出來的支撐／壓力／你的停損目標）：")
        for s in v["scenarios"]:
            L = lv[s["level_id"]]
            lines.append(f"- {s['trigger']} {L['name']} {_n(L['value'])}（{_p(L['dist_pct'])}）→ {s['then']}")
    lines.append("\n你的規則檢查：")
    rc = rules_check(b)
    # 程式已經寫了持倉／停損／攤平的檢查，模型重複的就不再列
    extra = [x for x in (v.get("rules_check") or [])
             if not re.search(r"持倉|持股|停損|攤平|部位", x) and x not in rc][:1]
    lines += [f"- {x}" for x in rc + extra]
    lines.append("\n觀察重點：")
    watch = list(dict.fromkeys(v.get("watch") or []))
    norm = lambda z: re.sub(r"外資|投信|自營商?", "法人", re.sub(r"費城半導體|費城", "費半", z))  # noqa: E731
    joined = norm("".join(watch))
    for x in code_verdict(b).get("watch") or []:   # 程式的觀察點只補模型沒提到的（例如財報日）
        kw = re.findall(r"法人|費半|財報|族群|量|均線|MA\d+|停損|支撐|壓力", norm(x))
        if x not in watch and not (kw and any(k in joined for k in kw)):
            watch.append(x)
    lines += [f"- {x}" for x in watch[:4]]
    sup = [x for x in b["levels"] if x["kind"] == "支撐"][-3:][::-1]
    res = [x for x in b["levels"] if x["kind"] == "壓力"][:3]
    fmt = lambda xs: "、".join(f"{x['name']} {_n(x['value'])}（{_p(x['dist_pct'])}）" for x in xs) or "—"  # noqa: E731
    lines.append(f"\n關鍵價位：支撐 {fmt(sup)}；壓力 {fmt(res)}")
    lines.append(f"技術指標：RSI14 {t['rsi14']}、ATR14 {_n(t['atr14']) if t['atr14'] else '—'}、量比 {t['volume_ratio']}、"
                 f"52 週位置 {t['pos52_pct']}%（" + ("含今天盤中 K 線、量比是盤中累計，可能延遲約 20 分鐘" if t.get("intraday") else "每日收盤資料")
                 + "，來源 yfinance_daily）")
    if b.get("errors"):
        lines.append("（部分資料抓取失敗：" + "；".join(b["errors"]) + "）")
    lines.append("\n（以上是決策輔助，不是保證；進出場和風險由你自己決定。）")
    return "\n".join(lines)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "聯鈞"
    b = decision_brief(q, question="適合抄底嗎")
    print(json.dumps({k: b.get(k) for k in ("tech", "position", "levels", "items", "earnings")}, ensure_ascii=False, indent=1))
    if b.get("found"):
        v = judge(b, f"{q}適合抄底嗎") if "--judge" in sys.argv else code_verdict(b)
        print(render(b, v))
        print(json.dumps(v, ensure_ascii=False, indent=1)[:3000])
