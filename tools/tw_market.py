r"""台股行情：永豐 Shioaji（simulation，只看報價，不下單）。

金鑰由使用者自己填在 D:\trade-coach\.env（SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY），
執行時讀取，不印出、不寫 log。沒填時自動改用證交所/櫃買公開每日資料，結果加上
warning = "shioaji_not_configured"。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from tools._common import DATA, data_time, iso_tpe, now_tpe, roc_to_iso, to_num, tw_stock_session

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
NOT_CONFIGURED = "shioaji_not_configured"

# Shioaji 的 log 放到本專案 data/（已 gitignore），不要寫進目前工作目錄
os.environ.setdefault("SJ_LOG_PATH", str(DATA / "shioaji.log"))

_api = None


class ShioajiNotConfigured(RuntimeError):
    """.env 沒有填永豐金鑰。"""


def _load_creds() -> tuple[str, str]:
    from dotenv import dotenv_values

    env = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    key = (env.get("SHIOAJI_API_KEY") or "").strip()
    secret = (env.get("SHIOAJI_SECRET_KEY") or "").strip()
    if not key or not secret:
        raise ShioajiNotConfigured(f"{NOT_CONFIGURED}：請在 {ENV_PATH} 填 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY")
    return key, secret


def is_configured() -> bool:
    try:
        _load_creds()
        return True
    except ShioajiNotConfigured:
        return False


def _err(e: Exception) -> str:
    return NOT_CONFIGURED if isinstance(e, ShioajiNotConfigured) else f"{type(e).__name__}: {e}"


def _get_api():
    """登入 Shioaji simulation（只取報價）。金鑰用完即丟，不留在模組變數。"""
    global _api
    if _api is not None:
        return _api
    import shioaji as sj

    key, secret = _load_creds()
    api = sj.Shioaji(simulation=True)  # 強制模擬環境，絕不下單
    try:
        # shioaji 1.7.x：login 不再接受 fetch_contract；不訂閱成交回報
        api.login(api_key=key, secret_key=secret, subscribe_trade=False)
    finally:
        del key, secret
    _api = api
    return api


def logout():
    global _api
    if _api is not None:
        try:
            _api.logout()
        except Exception:
            pass
        _api = None


def _contract(api, code: str):
    """shioaji 1.7.x 用 api.contracts.get(code)（指數 IX0001）；舊版用 api.Contracts。"""
    getter = getattr(getattr(api, "contracts", None), "get", None)
    if callable(getter):
        try:
            c = getter(code)
            if c is not None:
                return c
        except Exception:
            pass
        try:  # 合約尚未下載時補抓一次
            api.fetch_contracts(contract_download=True)
            c = getter(code)
            if c is not None:
                return c
        except Exception:
            pass
    try:
        if code.startswith("IX"):
            return api.Contracts.Indexs.TSE["001"]
        return api.Contracts.Stocks[code]
    except Exception:
        return None


def _snap_to_dict(s) -> dict:
    import datetime as dt

    ts = getattr(s, "ts", None)
    t = None
    if ts:
        # Shioaji ts 為奈秒，已是台北當地時間
        t = dt.datetime.fromtimestamp(ts / 1e9, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "code": getattr(s, "code", None),
        "close": getattr(s, "close", None),
        "open": getattr(s, "open", None),
        "high": getattr(s, "high", None),
        "low": getattr(s, "low", None),
        "change": getattr(s, "change_price", None),
        "change_pct": getattr(s, "change_rate", None),
        "volume": getattr(s, "total_volume", None),  # 個股：張
        "amount": getattr(s, "total_amount", None),
        "time": t,
    }


def _taiex_twse_fallback() -> dict:
    """證交所 FMTQIK：每日加權指數收盤與漲跌點數。"""
    d = now_tpe()
    for _ in range(2):  # 本月、上月
        ym = d.strftime("%Y%m01")
        r = requests.get("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK",
                         params={"date": ym, "response": "json"}, timeout=15)
        j = r.json()
        rows = j.get("data") or []
        if rows:
            last = rows[-1]
            close, chg = to_num(last[4]), to_num(last[5])
            prev = close - chg if close is not None and chg is not None else None
            return {
                "code": "TAIEX",
                "date": last[0],  # 民國日期
                "close": close,
                "change": chg,
                "change_pct": round(chg / prev * 100, 2) if prev else None,
                "volume_shares": to_num(last[1]),
                "amount_twd": to_num(last[2]),
                "source": "twse_daily",
                "dataset": "TWSE FMTQIK",
            }
        d = d.replace(day=1) - __import__("datetime").timedelta(days=1)
    raise RuntimeError("TWSE FMTQIK 沒有資料")


def _shioaji_taiex() -> dict:
    api = _get_api()
    c = _contract(api, "IX0001")
    if c is None:
        raise RuntimeError("找不到加權指數合約 IX0001")
    snaps = api.snapshots([c])
    if not snaps:
        raise RuntimeError("snapshots 回傳空的")
    out = _snap_to_dict(snaps[0])
    out["name"] = "加權指數"
    return out


def get_taiex(force: str | None = None) -> dict:
    """加權指數。盤中（09:00–13:30）用 Shioaji 即時；其他時間用證交所每日資料。

    force='live' / 'daily' 可強制來源。結果有 source（shioaji_live / twse_daily）與 as_of。
    """
    live = force == "live" or (force is None and tw_stock_session() == "day")
    err = None
    if live:
        try:
            out = _shioaji_taiex()
            qt = out.pop("time")
            out.update({"source": "shioaji_live", "quote_time": qt, **data_time("shioaji_live", quote_time=qt)})
            return out
        except Exception as e:
            err = _err(e)
    try:
        out = _taiex_twse_fallback()
        out.update(data_time("twse_daily", roc_to_iso(out.get("date"))))   # as_of = 那天收盤，不是查詢時間
        if err == NOT_CONFIGURED:
            out["warning"] = NOT_CONFIGURED
        elif err:
            out["shioaji_error"] = err
        return out
    except Exception as e2:  # 證交所掛了，退回 Shioaji 最後一筆
        out = _shioaji_taiex()
        qt = out.pop("time")
        out.update({"source": "shioaji_snapshot", "quote_time": qt, **data_time("shioaji_snapshot", quote_time=qt),
                    "twse_error": f"{type(e2).__name__}: {e2}"})
        return out


def _stock_names() -> dict:
    cache = DATA / "stock_list.json"
    return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}


def _stocks_daily(codes: list[str]) -> dict:
    """證交所 STOCK_DAY_ALL（上市）＋櫃買中心收盤行情（上櫃），最近一個交易日。"""
    want, items = set(codes), {}
    r = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=20)
    for x in r.json():
        if x.get("Code") in want:
            close, chg = to_num(x["ClosingPrice"]), to_num(x["Change"])
            prev = close - chg if close is not None and chg is not None else None
            items[x["Code"]] = {
                "code": x["Code"], "name": x["Name"], "date": x.get("Date"),
                "close": close, "open": to_num(x["OpeningPrice"]),
                "high": to_num(x["HighestPrice"]), "low": to_num(x["LowestPrice"]),
                "change": chg, "change_pct": round(chg / prev * 100, 2) if prev else None,
                "volume": round((to_num(x["TradeVolume"]) or 0) / 1000), "market": "上市",
                "data_date": roc_to_iso(x.get("Date"))}
    left = want - set(items)
    if left:
        r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=20)
        for x in r.json():
            c = x.get("SecuritiesCompanyCode")
            if c in left:
                close, chg = to_num(x["Close"]), to_num(x["Change"])
                prev = close - chg if close is not None and chg is not None else None
                items[c] = {
                    "code": c, "name": x["CompanyName"], "date": x.get("Date"),
                    "close": close, "open": to_num(x["Open"]), "high": to_num(x["High"]),
                    "low": to_num(x["Low"]), "change": chg,
                    "change_pct": round(chg / prev * 100, 2) if prev else None,
                    "volume": round((to_num(x["TradingShares"]) or 0) / 1000), "market": "上櫃",
                    "data_date": roc_to_iso(x.get("Date"))}
    return items


def _daily_out(codes: list[str], got: dict) -> dict:
    """每日收盤：as_of 用資料本身的日期（例如 2026-10-16 收盤），不是查詢時間。"""
    dates = sorted({got[c].get("data_date") for c in codes if c in got and got[c].get("data_date")})
    return {"source": "twse_daily", **data_time("twse_daily", dates[-1] if dates else None),
            "stocks": [got[c] for c in codes if c in got],
            "not_found": [c for c in codes if c not in got]}


def _esb_codes(codes: list[str]) -> set[str]:
    try:
        from tools.resolve import tw_esb_companies
        esb = tw_esb_companies()
    except Exception:  # noqa: BLE001
        return set()
    return {c for c in codes if c in esb}


def _esb_quotes(codes: list[str]) -> dict:
    """興櫃股：櫃買中心 OpenAPI「興櫃行情」tpex_esb_latest_statistics（最近成交、前一日均價、成交股數）。
    興櫃沒有漲跌幅限制，參考價是前一日的成交均價，所以漲跌是「最近成交 − 前一日均價」。"""
    want = set(codes)
    r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics", timeout=20)
    items = {}
    for x in r.json():
        c = (x.get("SecuritiesCompanyCode") or "").strip()
        if c not in want:
            continue
        last, prev = to_num(x.get("LatestPrice")), to_num(x.get("PreviousAveragePrice"))
        chg = round(last - prev, 2) if last is not None and prev is not None else None
        d = roc_to_iso(x.get("Date"))
        tm = str(x.get("Time") or "").zfill(6)
        items[c] = {"code": c, "name": (x.get("CompanyName") or "").strip(), "market": "興櫃",
                    "close": last, "change": chg, "change_pct": round(chg / prev * 100, 2) if chg is not None and prev else None,
                    "prev_avg": prev, "avg": to_num(x.get("Average")), "high": to_num(x.get("Highest")),
                    "low": to_num(x.get("Lowest")), "volume": round((to_num(x.get("TransactionVolume")) or 0) / 1000),
                    "data_date": d, "quote_time": f"{d} {tm[:2]}:{tm[2:4]}:{tm[4:]}" if d else None}
    qts = sorted(v["quote_time"] for v in items.values() if v.get("quote_time"))
    return {"source": "tpex_esb", "as_of": qts[-1] if qts else None,
            "data_type": "興櫃行情（櫃買中心，最近成交，非即時）", "is_live": False, "fetched_at": iso_tpe(),
            "stocks": [items[c] for c in codes if c in items], "not_found": [c for c in codes if c not in items],
            "esb_note": "興櫃沒有漲跌幅限制；漲跌是最近成交跟前一日成交均價比"}


def get_stock_snapshot(codes: list[str] | str, force: str | None = None) -> dict:
    """個股：價格、漲跌、成交量（張）。盤中 Shioaji 即時，其他時間證交所/櫃買每日收盤；興櫃股用櫃買中心興櫃行情。"""
    if isinstance(codes, str):
        codes = [codes]
    esb = _esb_codes(codes)
    if not esb:
        return _main_snapshot(codes, force)
    eq = _esb_quotes([c for c in codes if c in esb])
    rest = [c for c in codes if c not in esb]
    if not rest:
        return eq
    out = _main_snapshot(rest, force)
    out["stocks"] = list(out.get("stocks") or []) + eq["stocks"]
    out["not_found"] = list(out.get("not_found") or []) + eq["not_found"]
    out["esb"] = {k: eq[k] for k in ("source", "as_of", "data_type", "esb_note")}
    return out


def _main_snapshot(codes: list[str], force: str | None = None) -> dict:
    """上市／上櫃：盤中 Shioaji 即時，其他時間證交所/櫃買每日收盤（收盤後當天資料還沒公布時用永豐快照）。"""
    live = force == "live" or (force is None and tw_stock_session() == "day")
    err = None
    daily = None
    if not live:
        try:
            got = _stocks_daily(codes)
            daily = _daily_out(codes, got)
            # 平日收盤後，證交所／櫃買當天的收盤資料要晚一點才公布；這段時間還拿得到的是上一個交易日。
            # 這時改用永豐快照（今天的最後成交），失敗才退回上一個交易日的收盤。
            t = now_tpe()
            stale = t.weekday() < 5 and t.strftime("%H%M") >= "1335" and any(
                (x.get("data_date") or "") < t.strftime("%Y-%m-%d") for x in daily["stocks"])
            if not stale:
                return daily
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    try:
        api = _get_api()
    except Exception as e:
        if daily is not None:
            return {**daily, "warning": _err(e)}
        got = _stocks_daily(codes)
        return {**_daily_out(codes, got), "warning": _err(e)}
    contracts, missing = [], []
    for code in codes:
        c = _contract(api, code)
        (contracts.append(c) if c is not None else missing.append(code))
    names = _stock_names()
    try:
        snaps = api.snapshots(contracts) if contracts else []
    except Exception as e:
        if daily is not None:
            return {**daily, "warning": _err(e)}
        raise
    items = []
    for s in snaps:
        d = _snap_to_dict(s)
        d["name"] = names.get(d["code"])
        d["quote_time"] = d.pop("time")
        items.append(d)
    src = "shioaji_live" if live else "shioaji_snapshot"
    qts = sorted(x["quote_time"] for x in items if x.get("quote_time"))
    out = {"source": src, **data_time(src, quote_time=qts[-1] if qts else None),
           "stocks": items, "not_found": missing}
    if err:
        out["daily_error"] = err
    return out


if __name__ == "__main__":
    from tools._common import dump_selftest
    res = {}
    try:
        res["taiex_auto"] = get_taiex()
        res["taiex_live"] = get_taiex(force="live")
        for mode in (None, "live"):
            try:
                res[f"stocks_{mode or 'auto'}"] = get_stock_snapshot(["2330", "2317", "3714", "6187"], force=mode)
            except Exception as e:
                res[f"stocks_{mode or 'auto'}_error"] = f"{type(e).__name__}: {e}"
    finally:
        logout()
    dump_selftest("tw_market", res)
