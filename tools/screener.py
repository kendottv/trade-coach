"""開放式選股（Step 15）：「有哪檔股票推薦買入」沒有指定個股或族群時，用程式排序觀察清單，不叫 7B、不寫 decision_log、不下單。

觀察清單（universe）＝ data/watchlist.json（使用者自己列的代號）∪ 日記裡還沒賣完的持股 ∪ 最近 30 天問過決策的股票。
排序分數＝ code_verdict 同一套證據（技術 E#、籌碼、個股新聞 N#）的「偏多數 − 偏空數」，跌破停損再扣 2。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from tools._common import DATA

WATCH = DATA / "watchlist.json"
MAX_UNIVERSE = 10
DISCLAIMER = ("⚠ 以上只是把你的觀察清單用同一套證據規則排序，僅供參考，不是買進建議，也不會幫你下單；"
              "要細看某一檔可以問「○○適合買嗎」，或指定族群，例如「半導體有哪檔可以買」。")
EMPTY = ("你的觀察清單是空的（data\\watchlist.json 沒有股票、日記裡也沒有持股或最近問過的股票），所以沒有東西可以排序。\n"
         "可以：1) 在 data\\watchlist.json 列幾檔代號，例如 [\"2330\", \"3450\"]；2) 先問個股，例如「聯鈞適合買嗎」；"
         "3) 指定族群，例如「半導體有哪檔可以買」「CPO 推薦哪幾檔」。")


def universe() -> list[str]:
    out: list[str] = []
    try:
        w = json.loads(WATCH.read_text(encoding="utf-8"))
        out += [str(x).strip().upper() for x in (w.get("symbols", []) if isinstance(w, dict) else w)]
    except Exception:  # noqa: BLE001
        pass
    try:
        from journal import db
        out += [r["ticker"] for r in db.list_trades(side="buy", status="open", include_test="test" in db.DB_PATH.name)
                if (r.get("remaining_shares") or 0) > 0]
    except Exception:  # noqa: BLE001
        pass
    try:
        from tools.news_store import watch_tickers
        out += sorted(watch_tickers(30))
    except Exception:  # noqa: BLE001
        pass
    return [s for s in dict.fromkeys(out) if s and not s.startswith("^") and s not in ("TAIEX", "TXF")][:MAX_UNIVERSE]


def score(b: dict) -> dict:
    items = b.get("items") or []
    bull = [x for x in items if x["side"] == "偏多"]
    bear = [x for x in items if x["side"] == "偏空"]
    s = len(bull) - len(bear) - (2 if any("已跌破你的停損" in x["text"] for x in bear) else 0)
    news_s = sum(1 if x["side"] == "偏多" else -1 for x in bull + bear if x["id"].startswith("N"))
    return {"score": s, "news": news_s, "bull": bull, "bear": bear,
            "lean": "偏多" if s >= 3 else "偏空" if s <= -3 else "中性"}


def _brief(sym: str) -> dict:
    from tools.decision import decision_brief
    try:
        return decision_brief(sym, "auto", "")
    except Exception as e:  # noqa: BLE001
        return {"found": False, "query": sym, "error": f"{type(e).__name__}: {e}"}


def screen(top: int = 3, symbols: list[str] | None = None, brief=_brief) -> dict:
    t0 = time.time()
    uni = universe() if symbols is None else symbols
    if not uni:
        return {"empty": True, "universe": [], "ranked": [], "text": EMPTY, "elapsed_s": round(time.time() - t0, 1)}
    with ThreadPoolExecutor(4) as ex:
        briefs = list(ex.map(brief, uni))
    ranked, failed = [], []
    for sym, b in zip(uni, briefs):
        if not b.get("found") or not b.get("items"):
            failed.append(sym)
            continue
        sc = score(b)
        tg, t = b["target"], b.get("tech") or {}
        try:
            from tools.news_store import ticker_name
            nm = ticker_name(tg["symbol"])
        except Exception:  # noqa: BLE001
            nm = tg["symbol"]
        if nm == tg["symbol"]:
            nm = tg.get("name") or nm
        ranked.append({"symbol": tg["symbol"], "name": nm, "close": t.get("close"), **sc})
    ranked.sort(key=lambda r: (r["score"], r["news"]), reverse=True)
    return {"empty": False, "universe": uni, "failed": failed, "ranked": ranked, "top": ranked[:top],
            "text": render(ranked[:top], len(uni), failed), "elapsed_s": round(time.time() - t0, 1)}


def render(top: list[dict], n: int, failed: list[str]) -> str:
    if not top:
        return "觀察清單裡的股票這次都抓不到價格資料，沒辦法排序，稍後再試。\n" + DISCLAIMER
    lines = [f"從你的觀察清單 {n} 檔裡，用證據規則（技術 E#、籌碼、個股新聞 N#）排序，分數較高的前 {len(top)} 檔："]
    for i, r in enumerate(top, 1):
        px = f"，收盤 {r['close']:g}" if isinstance(r.get("close"), (int, float)) else ""
        lines.append(f"{i}. {r['name']}（{r['symbol']}）{px}：偏多 {len(r['bull'])} 項／偏空 {len(r['bear'])} 項 → 分數 {r['score']:+d}，整體{r['lean']}")
        for x in r["bull"][:2]:
            lines.append(f"   ＋ {x['id']} {x['text'][:70]}")
        for x in r["bear"][:1]:
            lines.append(f"   － {x['id']} {x['text'][:70]}")
    if failed:
        lines.append(f"（{', '.join(failed)} 抓不到資料，沒有列入排序）")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
