"""台指期（TX）近月：一般盤＋盤後（夜盤），三大法人未平倉。

來源：
- 盤中（日盤 08:45–13:45、夜盤 15:00–05:00）：永豐 Shioaji 即時快照（simulation，只看報價）
- 其他時間：期交所每日行情（futDataDown CSV，含「一般」「盤後」交易時段）

注意期交所的日期規則：夜盤算「下一個交易日」。例如 10/16（五）15:00 ~ 10/17（六）05:00
的夜盤，在期交所資料裡日期是 10/19（一）。本模組會把它對回它接在後面的那個一般盤。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from tools import _common
from tools._common import data_time, iso_tpe, roc_to_iso, to_num, tw_futures_session


def now_tpe() -> dt.datetime:
    """經過 _common.now_tpe（評測可以 mock 時間）。"""
    return _common.now_tpe()


def futures_trading_day(t: dt.datetime | None = None) -> dt.date:
    """期交所的「交易日」：15:00 以後的夜盤算下一個交易日（週五夜盤→下週一）；凌晨 00:00–05:00 算當天
    （週六凌晨→下週一）。不判斷國定假日。"""
    t = t or now_tpe()
    d, hm = t.date(), t.hour * 100 + t.minute
    if hm >= 1500:
        d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


LIVE_STALE_MIN = 15  # 盤中報價時間比現在舊超過這麼多分鐘 → 標示「報價可能停住」

HDR = {"User-Agent": "Mozilla/5.0"}
TAIFEX = "https://www.taifex.com.tw/cht/3"


def _taifex_csv(path: str, data: dict) -> list[dict]:
    r = requests.post(f"{TAIFEX}/{path}", data=data, headers=HDR, timeout=20)
    r.raise_for_status()
    text = r.content.decode("big5", errors="replace")
    if not text.lstrip().startswith(("交易日期", "日期")):
        raise RuntimeError(f"期交所回傳非 CSV（{path}）")
    rows = list(csv.reader(io.StringIO(text)))
    head = [h.strip() for h in rows[0]]
    return [dict(zip(head, [c.strip() for c in r])) for r in rows[1:] if len(r) >= len(head) - 1]


def _row(r: dict) -> dict:
    return {
        "trade_date": r["交易日期"],
        "contract_month": r["到期月份(週別)"],
        "open": to_num(r["開盤價"]), "high": to_num(r["最高價"]),
        "low": to_num(r["最低價"]), "close": to_num(r["收盤價"]),
        "change": to_num(r["漲跌價"]), "change_pct": r["漲跌%"],
        "volume": to_num(r["成交量"]),
        "settlement": to_num(r["結算價"]),
        "open_interest": to_num(r["未沖銷契約數"]),
    }


def get_tx_daily(days_back: int = 10) -> dict:
    """期交所每日行情：最近一個一般盤，以及接在它之後的夜盤（若已收盤）。"""
    today = now_tpe().date()
    start = (today - dt.timedelta(days=days_back)).strftime("%Y/%m/%d")
    end = (today + dt.timedelta(days=7)).strftime("%Y/%m/%d")  # 夜盤日期會是下一個交易日
    rows = _taifex_csv("futDataDown", {"down_type": "1", "commodity_id": "TX",
                                       "commodity_id2": "", "queryStartDate": start,
                                       "queryEndDate": end})
    # 只要單一月份（排除價差 '202610/202611' 和週選 W）
    rows = [r for r in rows if r.get("契約") == "TX" and r["到期月份(週別)"].isdigit()]
    reg = [r for r in rows if r["交易時段"] == "一般"]
    if not reg:
        raise RuntimeError("期交所沒有一般盤資料")
    last_date = max(r["交易日期"] for r in reg)
    reg_day = [r for r in reg if r["交易日期"] == last_date]
    near = min(reg_day, key=lambda r: r["到期月份(週別)"])  # 近月 = 到期月份最小
    regular = _row(near)

    month = near["到期月份(週別)"]
    reg_m = {r["交易日期"]: r for r in reg if r["到期月份(週別)"] == month}
    reg_dates = sorted({r["交易日期"] for r in reg})

    def _night(n: dict) -> dict:
        """期交所把夜盤記在「下一個交易日」：對回它接在後面的那個一般盤。"""
        x = _row(n)
        x["taifex_date"] = x.pop("trade_date")
        prev = [d for d in reg_dates if d < x["taifex_date"]]
        fol = prev[-1] if prev else None
        x["follows_regular_session"] = fol
        if fol:
            fd = dt.date.fromisoformat(roc_to_iso(fol))
            x["session_start"] = f"{fd.isoformat()} 15:00"
            x["session_end"] = f"{(fd + dt.timedelta(days=1)).isoformat()} 05:00"
        pr = reg_m.get(fol) if fol else None
        x["regular_close_before"] = pr and to_num(pr["收盤價"])
        x["change_vs_regular_close"] = (round(x["close"] - x["regular_close_before"], 2)
                                        if x["close"] and x["regular_close_before"] else None)
        x["note"] = "change 是對前一個一般盤結算價"
        return x

    nights_all = sorted((r for r in rows if r["交易時段"] == "盤後" and r["到期月份(週別)"] == month),
                        key=lambda r: r["交易日期"])
    # 接在最近一個一般盤之後的夜盤（已收盤、已公布）
    after = [r for r in nights_all if r["交易日期"] > last_date]
    night = _night(after[0]) if after else None
    # 最近一個「已收盤」的夜盤：有上面那個就是它；沒有（今晚夜盤進行中、或期交所還沒公布）就是更早那一盤
    done = [r for r in nights_all if r["交易日期"] <= last_date]
    last_night = night or (_night(done[-1]) if done else None)
    return {
        "source": "taifex_daily",
        # as_of = 最新一筆資料的收盤（有夜盤就是夜盤收盤，否則是日盤收盤），不是查詢時間
        **(data_time("taifex_daily", roc_to_iso(night["taifex_date"]), label="05:00 夜盤收盤（接在 "
                     + (roc_to_iso(last_date) or last_date) + " 日盤之後）") if night else
           data_time("taifex_daily", roc_to_iso(last_date), label="13:45 日盤收盤")),
        "regular_close_date": roc_to_iso(last_date),
        "contract": f"TX{month}",
        "regular": regular,
        "night": night,
        "last_night": last_night,
        "night_status": "closed" if night else "接在這個日盤之後的夜盤，期交所還沒有資料（夜盤進行中，或 05:00 收盤後尚未公布）",
    }


def _shioaji_tx_live() -> dict:
    """Shioaji 即時快照：TXF 近月（TXFR1）。"""
    from tools import tw_market

    api = tw_market._get_api()
    c = None
    for code in ("TXFR1", "TXF"):
        c = tw_market._contract(api, code)
        if c is not None:
            break
    if c is None:
        raise RuntimeError("找不到 TXFR1 合約")
    snaps = api.snapshots([c])
    if not snaps:
        raise RuntimeError("TXF snapshots 回傳空的")
    d = tw_market._snap_to_dict(snaps[0])
    d["contract"] = getattr(c, "code", None) or getattr(c, "symbol", None)
    d["contract_name"] = getattr(c, "name", None)
    return d


def get_tx_futures(force: str | None = None) -> dict:
    """台指期近月。盤中（日盤、夜盤）用 Shioaji 即時；其他時間用期交所每日行情。

    force='live' 或 'daily' 可強制來源。回傳都有 source 和 as_of（台北時間）。
    夜盤（15:00–隔日 05:00，跨午夜）：即時報價標「夜盤即時」，算在下一個交易日；即時失敗時
    回最近一個已收盤的夜盤（last_night），並寫明原因（night_status）。
    """
    now = now_tpe()
    session = tw_futures_session(now)
    out = {"session_now": session, "now": iso_tpe(now)}
    want_live = force == "live" or (force is None and session in ("day", "night"))
    if want_live:
        try:
            live = _shioaji_tx_live()
            live["session"] = session
            out.update({"source": "shioaji_live", "quote_time": live.get("time"), "live": live,
                        "live_label": {"night": "夜盤即時", "day": "日盤即時"}.get(session, "即時（非交易時段，最後成交）"),
                        **data_time("shioaji_live", quote_time=live.get("time"))})
            if session == "night":
                out["trading_day"] = futures_trading_day(now).isoformat()
            try:
                qt = dt.datetime.strptime(str(live.get("time"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
                age = (now - qt).total_seconds() / 60
                if session in ("day", "night") and age > LIVE_STALE_MIN:
                    out["live_stale_min"] = round(age)
            except (TypeError, ValueError):
                pass
        except Exception as e:
            from tools.tw_market import NOT_CONFIGURED, ShioajiNotConfigured
            if isinstance(e, ShioajiNotConfigured):
                out["warning"] = NOT_CONFIGURED
            else:
                out["live_error"] = f"{type(e).__name__}: {e}"
    try:
        daily = get_tx_daily()
        out.setdefault("source", daily["source"])
        for k in ("as_of", "data_type", "is_live", "fetched_at"):
            out.setdefault(k, daily.get(k))
        out["daily"] = daily
        lv, reg = out.get("live"), daily.get("regular") or {}
        if lv and lv.get("close") is not None and reg.get("close"):
            lv["regular_close"] = reg["close"]
            lv["regular_close_date"] = daily.get("regular_close_date")
            lv["change_vs_regular_close"] = round(lv["close"] - reg["close"], 2)
    except Exception as e:
        out["daily_error"] = f"{type(e).__name__}: {e}"
    # 夜盤狀態（給回覆和側邊欄用）
    has_live = bool(out.get("live"))
    if session == "night":
        out["night_status"] = ("夜盤交易中（上面是夜盤即時報價）" if has_live else
                               "今晚夜盤交易中，但即時報價取得失敗"
                               + ("（沒有設定永豐金鑰）" if out.get("warning") else
                                  f"（{out.get('live_error')}）" if out.get("live_error") else "")
                               + "；期交所要 05:00 收盤後才公布今晚這盤，下面是日盤收盤和最近一個已收盤的夜盤")
    elif (out.get("daily") or {}).get("night"):
        out["night_status"] = "最近一個夜盤已收盤"
    else:
        out["night_status"] = "最近一個日盤之後的夜盤，期交所還沒公布；下面是再前一個已收盤的夜盤"
    return out


def get_futures_institutional(date: str | None = None, days_back: int = 10) -> dict:
    """臺股期貨三大法人：交易口數淨額、未平倉口數淨額（最近一個交易日）。"""
    end = dt.datetime.strptime(date, "%Y%m%d").date() if date else now_tpe().date()
    rows, err = [], None
    # 這個端點的結束日必須是有資料的交易日，否則回 HTML；週末/假日往回退
    for _ in range(10):
        if end.weekday() < 5:
            start = end - dt.timedelta(days=days_back)
            try:
                rows = _taifex_csv("futContractsDateDown", {
                    "queryStartDate": start.strftime("%Y/%m/%d"),
                    "queryEndDate": end.strftime("%Y/%m/%d"), "commodityId": "TXF"})
                if rows:
                    break
            except RuntimeError as e:
                err = e
        end -= dt.timedelta(days=1)
    if not rows:
        raise RuntimeError(f"期交所沒有三大法人期貨資料（{err}）")
    last = max(r["日期"] for r in rows)
    prev_dates = sorted({r["日期"] for r in rows if r["日期"] < last})
    prev = prev_dates[-1] if prev_dates else None
    out = {}
    for r in rows:
        if r["日期"] not in (last, prev):
            continue
        k = r["身份別"]
        out.setdefault(k, {})
        if r["日期"] == last:
            out[k].update({
                "trade_net_lots": to_num(r["多空交易口數淨額"]),
                "oi_long": to_num(r["多方未平倉口數"]),
                "oi_short": to_num(r["空方未平倉口數"]),
                "oi_net_lots": to_num(r["多空未平倉口數淨額"]),
                "oi_net_amount_k": to_num(r["多空未平倉契約金額淨額(千元)"]),
            })
        else:
            out[k]["prev_oi_net_lots"] = to_num(r["多空未平倉口數淨額"])
    for v in out.values():
        if v.get("oi_net_lots") is not None and v.get("prev_oi_net_lots") is not None:
            v["oi_net_change"] = v["oi_net_lots"] - v["prev_oi_net_lots"]
    return {"source": "taifex_daily", **data_time("taifex_daily", roc_to_iso(last)), "date": last,
            "product": "臺股期貨(TXF)", "by_investor": out}


if __name__ == "__main__":
    from tools import tw_market
    from tools._common import dump_selftest

    res = {}
    try:
        for key, fn in (("auto", get_tx_futures),
                        ("force_live", lambda: get_tx_futures(force="live")),
                        ("institutional", get_futures_institutional)):
            try:
                res[key] = fn()
            except Exception as e:
                res[key + "_error"] = f"{type(e).__name__}: {e}"
        if "force_live" in res:  # 只留即時部分，避免重複
            fl = res["force_live"]
            res["force_live"] = {k: fl.get(k) for k in ("source", "as_of", "quote_time", "live", "live_error")}
    finally:
        tw_market.logout()
    dump_selftest("tw_futures", res)
