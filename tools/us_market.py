"""美股四大指數（yfinance）：道瓊、那斯達克、S&P 500、費半。

美股盤中（美東 09:30–16:00）另抓 1 分 K，取最新一筆價格和時間（source=yfinance_live）；
其他時間用日 K 收盤（source=yfinance_daily）。Yahoo 的指數報價可能有延遲，
請看 quote_time（台北時間）判斷新鮮度。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf

from tools._common import TPE, data_time, iso_tpe

NY = ZoneInfo("America/New_York")
INDICES = {
    "^DJI": "道瓊",
    "^IXIC": "那斯達克",
    "^GSPC": "S&P 500",
    "^SOX": "費城半導體",
}


def us_session(t: dt.datetime | None = None) -> str:
    t = (t or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
    hm = t.hour * 100 + t.minute
    return "open" if t.weekday() < 5 and 930 <= hm < 1600 else "closed"


def get_us_indices(symbols: list[str] | None = None) -> dict:
    symbols = symbols or list(INDICES)
    session = us_session()
    out, errors = [], {}
    daily = yf.download(symbols, period="10d", interval="1d", progress=False,
                        auto_adjust=False, group_by="ticker", threads=True)
    intraday = None
    if session == "open":
        try:
            intraday = yf.download(symbols, period="1d", interval="1m", progress=False,
                                   auto_adjust=False, group_by="ticker", threads=True)
        except Exception as e:
            errors["intraday"] = f"{type(e).__name__}: {e}"
    today_ny = dt.datetime.now(NY).date()
    for sym in symbols:
        try:
            closes = daily[sym]["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("資料不足")
            # 前一日收盤：日 K 最後一筆若是今天（盤中未完成），用倒數第二筆
            if closes.index[-1].date() == today_ny and session == "open":
                prev = float(closes.iloc[-2])
            else:
                prev = float(closes.iloc[-2]) if session == "closed" else float(closes.iloc[-1])
            last, qtime, src = float(closes.iloc[-1]), None, "yfinance_daily"
            if session == "closed":
                prev = float(closes.iloc[-2])
            if intraday is not None:
                m = intraday[sym]["Close"].dropna()
                if len(m):
                    last = float(m.iloc[-1])
                    qtime = m.index[-1].to_pydatetime().astimezone(TPE).strftime("%Y-%m-%d %H:%M")
                    src = "yfinance_live"
            out.append({
                "symbol": sym,
                "name": INDICES.get(sym, sym),
                "date": closes.index[-1].strftime("%Y-%m-%d"),  # 美東交易日
                "close": round(last, 2),
                "change": round(last - prev, 2),
                "change_pct": round((last - prev) / prev * 100, 2),
                "source": src,
                "quote_time": qtime,
            })
        except Exception as e:
            errors[sym] = f"{type(e).__name__}: {e}"
    src = "yfinance_live" if intraday is not None else "yfinance_daily"
    if src == "yfinance_live":
        qts = sorted(x["quote_time"] for x in out if x.get("quote_time"))
        dt_ = data_time(src, quote_time=(qts[-1] + "（台北時間）") if qts else None)
    else:  # as_of = 美東那天收盤（台北時間隔天清晨），不是查詢時間
        ds = sorted({x["date"] for x in out})
        dt_ = data_time(src, ds[-1] if ds else None, label="收盤（美東）")
    return {"source": src, **dt_, "us_session": session, "indices": out, "errors": errors}


if __name__ == "__main__":
    from tools._common import dump_selftest
    dump_selftest("us_market", get_us_indices())
