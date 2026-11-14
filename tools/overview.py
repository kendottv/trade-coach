"""今日盤勢：一次抓台股、法人、台指期（日盤/夜盤/未平倉）、美股、新聞熱度，
整理成結構化摘要。所有換算（億、口數變化）都在這裡用程式算好，模型只引用。"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._common import iso_tpe


def _yi(v):
    return None if v is None else round(v / 1e8, 2)


def market_overview(held: list[str] | None = None, with_news: bool = True) -> dict:
    from tools import news, tw_futures, tw_market, twse_inst, us_market

    jobs = {
        "taiex": tw_market.get_taiex,
        "inst": twse_inst.get_institutional,
        "foreign_top": lambda: twse_inst.get_foreign_top(top=5),
        "tx": tw_futures.get_tx_futures,
        "fut_inst": tw_futures.get_futures_institutional,
        "us": us_market.get_us_indices,
        "us_streaks": lambda: __import__("tools.history", fromlist=["x"]).us_streaks(),
        "taiex_hist": lambda: __import__("tools.history", fromlist=["x"]).tw_history("TAIEX", 5),
    }
    if with_news:
        jobs["news"] = lambda: news.get_news_heat(held=held, max_headlines=5)
    raw, errors = {}, {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        for k, f in futs.items():
            try:
                raw[k] = f.result(timeout=90)
            except Exception as e:
                errors[k] = f"{type(e).__name__}: {e}"

    out: dict = {"as_of": iso_tpe(), "errors": errors}
    warnings = set()

    t = raw.get("taiex")
    if t:
        out["加權指數"] = {"close": t.get("close"), "change": t.get("change"), "change_pct": t.get("change_pct"),
                       "date": t.get("date") or t.get("quote_time"), "source": t.get("source"),
                       "as_of": t.get("as_of"), "data_type": t.get("data_type")}
        if t.get("warning"):
            warnings.add(t["warning"])
    th = raw.get("taiex_hist")
    if th and "加權指數" in out:
        st = th["streak"]
        out["加權指數"].update({"streak_direction": st["direction"], "streak_days": st["days"],
                             "streak_cum_pct": st["cum_change_pct"], "streak_as_of": th["last_date"],
                             "change_5d_pct": (th.get("change_5d") or {}).get("change_pct"),
                             "above_ma20": th["ma"]["above_ma20"]})

    i = raw.get("inst")
    if i:
        out["三大法人現貨"] = {"date": i["date"], "unit": "億元",
                         "外資": _yi(i["foreign_net"]), "投信": _yi(i["trust_net"]),
                         "自營商": _yi(i["dealer_net"]), "合計": _yi(i["total_net"]),
                         "source": "twse_daily"}
    ft = raw.get("foreign_top")
    if ft:
        out["外資買賣超個股"] = {"date": ft["date"], "unit": "張",
                          "買超": [f"{x['name']}({x['code']}) {x['net_lots']}" for x in ft["top_buy"]],
                          "賣超": [f"{x['name']}({x['code']}) {x['net_lots']}" for x in ft["top_sell"]],
                          "source": "twse_daily"}

    tx = raw.get("tx")
    if tx:
        if tx.get("warning"):
            warnings.add(tx["warning"])
        d = tx.get("daily") or {}
        reg, night, last_night = d.get("regular") or {}, d.get("night"), d.get("last_night")
        fut = {"contract": d.get("contract"), "session_now": tx.get("session_now"), "source": tx.get("source"),
               "as_of": tx.get("as_of"), "data_type": tx.get("data_type"),
               "日盤": {"date": reg.get("trade_date"), "close": reg.get("close"), "change": reg.get("change"),
                       "change_pct": reg.get("change_pct"), "volume": reg.get("volume"),
                       "open_interest": reg.get("open_interest"),
                       "data_type": d.get("data_type"), "source": d.get("source")}}
        n = night or last_night
        if n:
            fut["夜盤"] = {"label": "夜盤" if night else "最近一個已收盤的夜盤",
                         "接在日盤": n.get("follows_regular_session"), "session_end": n.get("session_end"),
                         "close": n.get("close"),
                         "change_vs_settlement": n.get("change"), "change_pct": n.get("change_pct"),
                         "vs_日盤收盤": n.get("change_vs_regular_close"), "volume": n.get("volume")}
        fut["夜盤狀態"] = tx.get("night_status") or d.get("night_status")
        live = tx.get("live")
        if live:
            fut["即時"] = {"label": tx.get("live_label") or "即時", "price": live.get("close"),
                         "change": live.get("change"), "change_pct": live.get("change_pct"),
                         "vs_日盤收盤": live.get("change_vs_regular_close"), "volume": live.get("volume"),
                         "quote_time": live.get("time"), "trading_day": tx.get("trading_day")}
        out["台指期"] = fut

    fi = raw.get("fut_inst")
    if fi:
        out["期貨三大法人未平倉"] = {"date": fi["date"], "unit": "口", "source": "taifex_daily",
                             **{k: {"未平倉淨額": v.get("oi_net_lots"), "增減": v.get("oi_net_change"),
                                    "當日交易淨額": v.get("trade_net_lots")}
                                for k, v in fi["by_investor"].items()}}

    us = raw.get("us")
    if us:
        out["美股"] = {"source": us["source"], "as_of": us["as_of"], "data_type": us.get("data_type"),
                     "us_session": us.get("us_session"),
                     "指數": [{"name": x["name"], "date": x["date"], "close": x["close"], "change": x["change"],
                             "change_pct": x["change_pct"]} for x in us["indices"]]}
        streaks = {x["symbol"]: x for x in raw.get("us_streaks") or []}
        for x, src in zip(out["美股"]["指數"], us["indices"]):
            st = streaks.get(src["symbol"])
            if st and st["date"] == x["date"]:
                x.update({k: st[k] for k in ("streak_direction", "streak_days", "streak_cum_pct")})

    n = raw.get("news")
    if n:
        out["新聞熱度"] = {"as_of": n["as_of"], "source": n["source"], "news_count": n["news_count"],
                       "熱門關鍵字": [f"{k['keyword']}({k['count']})" for k in n["hot_keywords"][:8]],
                       "熱門個股": [f"{s['name']}({s['code']}) {s['count']}則" for s in n["hot_stocks"][:8]],
                       "持股相關": n.get("held_mentions", [])[:5],
                       "頭條": [{"title": h["title"], "link": h["link"], "time": h["time"]} for h in n["headlines"][:5]]}
    out["warnings"] = sorted(warnings)
    out["summary_lines"] = _lines(out)
    return out


def _fmt(v, nd=2, sign=True):
    if v is None:
        return "—"
    s = f"{v:+,.{nd}f}" if sign else f"{v:,.{nd}f}"
    return s


def _streak_txt(d: dict) -> str:
    if not d.get("streak_days"):
        return ""
    w = {"up": "連漲", "down": "連跌"}.get(d["streak_direction"])
    if not w:
        return ""
    if d["streak_days"] == 1:
        return f"，{'漲' if d['streak_direction'] == 'up' else '跌'} 1 天（前一日相反）"
    return f"，{w} {d['streak_days']} 天累計 {_fmt(d['streak_cum_pct'])}%"


def _lines(o: dict) -> list[str]:
    """程式產生的摘要句（數字直接來自資料）。"""
    L = []
    t = o.get("加權指數")
    if t:
        L.append(f"加權指數 {_fmt(t['close'], 2, False)}（{_fmt(t['change'])} 點，{_fmt(t['change_pct'])}%）"
                 f"{_streak_txt(t)}（{t.get('data_type') or ''}，截至 {t.get('as_of')}，來源 {t['source']}）")
    i = o.get("三大法人現貨")
    if i:
        L.append(f"三大法人（{i['date']}）：外資 {_fmt(i['外資'])} 億、投信 {_fmt(i['投信'])} 億、"
                 f"自營商 {_fmt(i['自營商'])} 億、合計 {_fmt(i['合計'])} 億（證交所）")
    f = o.get("台指期")
    if f:
        r = f["日盤"]
        L.append(f"台指期 {f['contract']} 日盤（{r['date']}）收 {_fmt(r['close'], 0, False)}（{_fmt(r['change'], 0)}）"
                 f"（{r.get('data_type') or ''}，來源 {r.get('source')}）")
        if isinstance(f.get("即時"), dict):
            lv = f["即時"]
            L.append(f"台指期{lv['label']} {_fmt(lv['price'], 0, False)}（比日盤收盤 {_fmt(lv.get('vs_日盤收盤'), 0)} 點，"
                     f"對結算價 {_fmt(lv['change'], 0)}，報價時間 {lv['quote_time']}，永豐即時）")
        nt = f.get("夜盤")
        if isinstance(nt, dict):
            L.append(f"{nt['label']}（接 {nt['接在日盤']} 日盤，{nt.get('session_end') or ''} 收盤）收 {_fmt(nt['close'], 0, False)}，"
                     f"比日盤收盤 {_fmt(nt['vs_日盤收盤'], 0)} 點（對結算價 {_fmt(nt['change_vs_settlement'], 0)}，{nt['change_pct']}）")
        if f.get("session_now") == "night" and not isinstance(f.get("即時"), dict) and f.get("夜盤狀態"):
            L.append(f"（{f['夜盤狀態']}）")
    fi = o.get("期貨三大法人未平倉")
    if fi and isinstance(fi.get("外資及陸資"), dict):
        x = fi["外資及陸資"]
        L.append(f"外資台指期未平倉淨額（{fi['date']}）{_fmt(x['未平倉淨額'], 0)} 口（增減 {_fmt(x['增減'], 0)}）")
    us = o.get("美股")
    if us:
        L.append("美股（" + (us.get("as_of") or us["指數"][0]["date"]) + "，" + (us.get("data_type") or "") + "）：" + "、".join(
            f"{x['name']} {_fmt(x['change_pct'])}%{_streak_txt(x)}" for x in us["指數"]))
    n = o.get("新聞熱度")
    if n:
        L.append("新聞熱門關鍵字：" + "、".join(n["熱門關鍵字"][:5]))
    return L


if __name__ == "__main__":
    from tools._common import dump_selftest
    dump_selftest("overview", market_overview())
