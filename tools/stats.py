"""交易統計：全部用程式算，模型只負責解釋結果。

一筆「回合」= 一張買單＋它所有的賣單。買單全部賣完才算平倉回合（勝率用這個算）。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db
from tools._common import iso_tpe


def _days(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def compute_stats(since: str | None = None, until: str | None = None, with_quotes: bool = True,
                  include_test: bool | None = None) -> dict:
    """since/until：YYYY-MM-DD，以買進日篩選。"""
    if include_test is None:
        include_test = "test" in db.DB_PATH.name
    buys = db.list_trades(side="buy", since=since, until=until, include_test=include_test, limit=100000)
    sells_all = db.list_trades(side="sell", include_test=include_test, limit=100000)
    sells_by_buy: dict[int, list] = {}
    for s in sells_all:
        sells_by_buy.setdefault(s["linked_buy_id"], []).append(s)

    rounds, open_pos = [], []
    for b in buys:
        sells = sorted(sells_by_buy.get(b["id"], []), key=lambda s: (s["trade_date"], s["id"]))
        realized = round(sum(s["realized_pnl"] or 0 for s in sells), 2)
        sold = sum(s["shares"] for s in sells)
        r = {"buy_id": b["id"], "ticker": b["ticker"], "name": b["name"], "buy_date": b["trade_date"],
             "buy_price": b["price"], "shares": b["shares"], "stop_loss": b["stop_loss"],
             "strategy_tag": b["strategy_tag"], "realized_pnl": realized, "sold_shares": sold,
             "sell_ids": [s["id"] for s in sells]}
        if sells:
            r["avg_sell_price"] = round(sum(s["price"] * s["shares"] for s in sells) / sold, 2)
            r["last_sell_date"] = sells[-1]["trade_date"]
            r["holding_days"] = _days(b["trade_date"], sells[-1]["trade_date"])
            if b["stop_loss"]:
                below = [s for s in sells if s["price"] < b["stop_loss"]]
                r["sold_below_stop"] = bool(below)
                r["worst_below_stop_pct"] = (round(min((s["price"] - b["stop_loss"]) / b["stop_loss"] * 100
                                                       for s in below), 2) if below else 0.0)
        if b["status"] == "closed":
            r["return_pct"] = round(realized / (b["price"] * b["shares"]) * 100, 2)
            rounds.append(r)
        else:
            r["remaining_shares"] = b["remaining_shares"]
            open_pos.append(r)

    # 未平倉：用 tw_market 報價算未實現損益（費用估算：賣方手續費＋證交稅）
    quote_info = None
    if with_quotes and open_pos:
        try:
            from tools import tw_market
            q = tw_market.get_stock_snapshot(sorted({p["ticker"] for p in open_pos}))
            px = {s["code"]: s for s in q["stocks"]}
            quote_info = {"source": q["source"], "as_of": q["as_of"]}
            for p in open_pos:
                s = px.get(p["ticker"])
                if not s or s.get("close") is None:
                    p["unrealized_error"] = "沒有報價"
                    continue
                last, rem = s["close"], p["remaining_shares"]
                buy_fee_part = (db.calc_fee(p["buy_price"], p["shares"], "buy")) * rem / p["shares"]
                est = (last - p["buy_price"]) * rem - db.calc_fee(last, rem, "sell") - buy_fee_part
                p.update({"last_price": last, "quote_date": s.get("date") or s.get("quote_time"),
                          "unrealized_pnl": round(est, 2),
                          "unrealized_return_pct": round((last - p["buy_price"]) / p["buy_price"] * 100, 2)})
                if p["stop_loss"]:
                    p["below_stop_now"] = last < p["stop_loss"]
        except Exception as e:
            quote_info = {"error": f"{type(e).__name__}: {e}"}

    wins = [r for r in rounds if r["realized_pnl"] > 0]
    losses = [r for r in rounds if r["realized_pnl"] <= 0]
    gw = sum(r["realized_pnl"] for r in wins)
    gl = -sum(r["realized_pnl"] for r in losses)
    realized_total = round(sum(r["realized_pnl"] for r in rounds) +
                           sum(p["realized_pnl"] for p in open_pos), 2)  # 含部分賣出

    with_stop = [b for b in buys if b["stop_loss"]]
    loss_with_stop = [r for r in losses if r["stop_loss"]]
    discipline = {
        "buys_total": len(buys),
        "buys_with_stop": len(with_stop),
        "stop_set_rate": round(len(with_stop) / len(buys) * 100, 1) if buys else None,
        "losing_rounds_with_stop": len(loss_with_stop),
        "sold_below_stop": [{"buy_id": r["buy_id"], "ticker": r["ticker"], "stop": r["stop_loss"],
                             "avg_sell": r["avg_sell_price"], "pct_below_stop": r["worst_below_stop_pct"]}
                            for r in rounds if r.get("sold_below_stop")],
        "held_past_stop_now": [{"buy_id": p["buy_id"], "ticker": p["ticker"], "stop": p["stop_loss"],
                                "last": p.get("last_price")} for p in open_pos if p.get("below_stop_now")],
        "losing_rounds_without_stop": [r["buy_id"] for r in losses if not r["stop_loss"]],
        "note": "sold_below_stop = 賣價低於停損價（停損執行太慢）；held_past_stop_now = 目前價格已跌破停損但還沒賣。"
                "已平倉回合期間是否曾跌破停損需要歷史價，目前沒算。",
    }

    hold = [r["holding_days"] for r in rounds if "holding_days" in r]
    by_tag: dict[str, dict] = {}
    for r in rounds:
        t = by_tag.setdefault(r["strategy_tag"] or "(未標)", {"rounds": 0, "wins": 0, "pnl": 0.0})
        t["rounds"] += 1
        t["wins"] += int(r["realized_pnl"] > 0)
        t["pnl"] = round(t["pnl"] + r["realized_pnl"], 2)
    for t in by_tag.values():
        t["win_rate"] = round(t["wins"] / t["rounds"] * 100, 1)

    return {
        "as_of": iso_tpe(),
        "period": {"since": since, "until": until},
        "closed_rounds": len(rounds),
        "win_rate": round(len(wins) / len(rounds) * 100, 1) if rounds else None,
        "avg_win": round(gw / len(wins), 2) if wins else None,
        "avg_loss": round(-gl / len(losses), 2) if losses else None,
        "profit_factor": round(gw / gl, 2) if gl > 0 else (None if not wins else float("inf")),
        "realized_pnl_total": realized_total,
        "avg_holding_days": round(sum(hold) / len(hold), 1) if hold else None,
        "avg_holding_days_win": (round(sum(r["holding_days"] for r in wins) / len(wins), 1) if wins else None),
        "avg_holding_days_loss": (round(sum(r["holding_days"] for r in losses) / len(losses), 1) if losses else None),
        "by_strategy": by_tag,
        "stop_discipline": discipline,
        "open_positions": open_pos,
        "unrealized_pnl_total": round(sum(p.get("unrealized_pnl") or 0 for p in open_pos), 2),
        "quotes": quote_info,
        "rounds": rounds,
        "units": "TWD；損益已扣手續費（費率見 journal/db.py）與證交稅",
    }
