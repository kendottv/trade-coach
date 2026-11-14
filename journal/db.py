"""SQLite 交易日記：trades / market_daily / notes。

金額、損益、手續費全部由程式計算，模型不算任何數字。
預設資料庫 data/trades.db；測試用 set_db(DATA / "test_trades.db")。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools._common import DATA, now_tpe  # noqa: E402

DB_PATH = DATA / "trades.db"

# 台股費用（可依券商折扣調整）
COMMISSION_RATE = 0.001425
COMMISSION_DISCOUNT = 1.0      # 例如 0.28 = 2.8 折
COMMISSION_MIN_BOARD = 20      # 整股最低手續費
COMMISSION_MIN_ODD = 1         # 零股最低手續費
SELL_TAX_RATE = 0.003          # 證交稅（現股賣出）

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date       TEXT NOT NULL,              -- YYYY-MM-DD
    ticker           TEXT NOT NULL,
    name             TEXT,
    side             TEXT NOT NULL CHECK (side IN ('buy','sell')),
    price            REAL NOT NULL CHECK (price > 0),
    shares           INTEGER NOT NULL CHECK (shares > 0),   -- 股數（1 張 = 1000 股）
    odd_lot          INTEGER NOT NULL DEFAULT 0,            -- 1 = 零股
    reason           TEXT,
    stop_loss        REAL,
    target_prices    TEXT,                                  -- JSON list
    strategy_tag     TEXT,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    remaining_shares INTEGER,                               -- 買單尚未賣出的股數
    linked_buy_id    INTEGER REFERENCES trades(id),         -- 賣單對應的買單
    fee              REAL,                                  -- 手續費＋稅（程式算）
    realized_pnl     REAL,                                  -- 賣單已實現損益（程式算，含雙邊費用）
    is_test          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker, side, status);

CREATE TABLE IF NOT EXISTS market_daily (
    date              TEXT PRIMARY KEY,      -- 台股交易日 YYYY-MM-DD
    taiex_close       REAL, taiex_change REAL, taiex_change_pct REAL,
    inst_foreign_net  REAL, inst_trust_net REAL, inst_dealer_net REAL, inst_total_net REAL,  -- 元
    foreign_top_buy   TEXT, foreign_top_sell TEXT,            -- JSON
    tx_contract       TEXT,
    tx_day_close      REAL, tx_day_change REAL, tx_day_volume REAL,
    tx_night_close    REAL, tx_night_change REAL, tx_night_vs_day REAL, tx_night_volume REAL,
    fut_foreign_oi_net REAL, fut_trust_oi_net REAL, fut_dealer_oi_net REAL,               -- 口
    us_date           TEXT,
    us_dji REAL, us_dji_pct REAL, us_ixic REAL, us_ixic_pct REAL,
    us_gspc REAL, us_gspc_pct REAL, us_sox REAL, us_sox_pct REAL,
    top_keywords      TEXT, top_stocks TEXT,                  -- JSON
    raw_json          TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    text              TEXT NOT NULL,
    related_trade_ids TEXT,                  -- JSON list
    is_test           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
"""


def set_db(path) -> None:
    global DB_PATH
    DB_PATH = Path(path)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return now_tpe().strftime("%Y-%m-%d %H:%M:%S")


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("target_prices", "related_trade_ids", "top_keywords", "top_stocks",
              "foreign_top_buy", "foreign_top_sell"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


# ---------- 費用 ----------
def calc_fee(price: float, shares: int, side: str) -> float:
    amount = price * shares
    odd = shares % 1000 != 0
    comm = math.floor(amount * COMMISSION_RATE * COMMISSION_DISCOUNT)
    comm = max(comm, COMMISSION_MIN_ODD if odd else COMMISSION_MIN_BOARD)
    tax = math.floor(amount * SELL_TAX_RATE) if side == "sell" else 0
    return float(comm + tax)


# ---------- trades ----------
def add_buy(trade_date: str, ticker: str, price: float, shares: int, name: str | None = None,
            reason: str | None = None, stop_loss: float | None = None,
            target_prices: list[float] | None = None, strategy_tag: str | None = None,
            is_test: bool = False) -> int:
    with connect() as con:
        cur = con.execute(
            """INSERT INTO trades (trade_date,ticker,name,side,price,shares,odd_lot,reason,stop_loss,
               target_prices,strategy_tag,status,remaining_shares,fee,is_test,created_at)
               VALUES (?,?,?,'buy',?,?,?,?,?,?,?,'open',?,?,?,?)""",
            (trade_date, ticker, name, price, shares, int(shares % 1000 != 0), reason, stop_loss,
             json.dumps(target_prices or []), strategy_tag, shares,
             calc_fee(price, shares, "buy"), int(is_test), _now()))
        return cur.lastrowid


def add_sell(trade_date: str, ticker: str, price: float, shares: int, name: str | None = None,
             reason: str | None = None, linked_buy_id: int | None = None,
             strategy_tag: str | None = None, is_test: bool = False) -> list[int]:
    """賣出。沒指定 linked_buy_id 就用 FIFO 對應最早的未平倉買單，必要時拆成多筆。

    已實現損益 = (賣價 - 買價) × 股數 - 賣方費用 - 對應比例的買方手續費。
    """
    ids = []
    with connect() as con:
        if linked_buy_id:
            buys = [con.execute("SELECT * FROM trades WHERE id=? AND side='buy'", (linked_buy_id,)).fetchone()]
            if buys[0] is None:
                raise ValueError(f"找不到買單 id={linked_buy_id}")
        else:
            buys = con.execute("""SELECT * FROM trades WHERE side='buy' AND ticker=? AND status='open'
                                  AND is_test=? ORDER BY trade_date, id""", (ticker, int(is_test))).fetchall()
        avail = sum(b["remaining_shares"] for b in buys)
        if avail < shares:
            raise ValueError(f"{ticker} 可賣股數 {avail} 小於要賣的 {shares}（不支援放空）")
        left = shares
        for b in buys:
            if left <= 0:
                break
            q = min(left, b["remaining_shares"])
            if q <= 0:
                continue
            sell_fee = calc_fee(price, q, "sell")
            buy_fee_part = (b["fee"] or 0) * q / b["shares"]
            pnl = round((price - b["price"]) * q - sell_fee - buy_fee_part, 2)
            cur = con.execute(
                """INSERT INTO trades (trade_date,ticker,name,side,price,shares,odd_lot,reason,stop_loss,
                   target_prices,strategy_tag,status,linked_buy_id,fee,realized_pnl,is_test,created_at)
                   VALUES (?,?,?,'sell',?,?,?,?,NULL,'[]',?,'closed',?,?,?,?,?)""",
                (trade_date, ticker, name or b["name"], price, q, int(q % 1000 != 0), reason,
                 strategy_tag or b["strategy_tag"], b["id"], sell_fee, pnl, int(is_test), _now()))
            ids.append(cur.lastrowid)
            rem = b["remaining_shares"] - q
            con.execute("UPDATE trades SET remaining_shares=?, status=? WHERE id=?",
                        (rem, "closed" if rem == 0 else "open", b["id"]))
            left -= q
    return ids


def get_trade(trade_id: int) -> dict | None:
    with connect() as con:
        return _row(con.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())


def list_trades(ticker: str | None = None, side: str | None = None, status: str | None = None,
                since: str | None = None, until: str | None = None, include_test: bool = False,
                limit: int = 200) -> list[dict]:
    q, a = "SELECT * FROM trades WHERE 1=1", []
    for col, val in (("ticker", ticker), ("side", side), ("status", status)):
        if val:
            q += f" AND {col}=?"
            a.append(val)
    if since:
        q += " AND trade_date>=?"
        a.append(since)
    if until:
        q += " AND trade_date<=?"
        a.append(until)
    if not include_test:
        q += " AND is_test=0"
    q += " ORDER BY trade_date DESC, id DESC LIMIT ?"
    a.append(limit)
    with connect() as con:
        return [_row(r) for r in con.execute(q, a).fetchall()]


_EDITABLE = {"reason", "stop_loss", "target_prices", "strategy_tag", "name", "trade_date"}


def update_trade(trade_id: int, **fields) -> dict:
    """只允許改說明類欄位；價格/股數改錯請刪掉重記（避免損益錯亂）。"""
    bad = set(fields) - _EDITABLE
    if bad:
        raise ValueError(f"不能改這些欄位：{bad}")
    if "target_prices" in fields:
        fields["target_prices"] = json.dumps(fields["target_prices"] or [])
    with connect() as con:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))
    return get_trade(trade_id)


def delete_trade(trade_id: int) -> None:
    """刪除一筆。刪賣單會把股數還給買單；有賣單連著的買單不能刪。"""
    with connect() as con:
        t = con.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if t is None:
            return
        if t["side"] == "buy":
            n = con.execute("SELECT COUNT(*) FROM trades WHERE linked_buy_id=?", (trade_id,)).fetchone()[0]
            if n:
                raise ValueError("這筆買單已有賣出紀錄，請先刪掉對應的賣單")
        else:
            con.execute("""UPDATE trades SET remaining_shares=remaining_shares+?, status='open'
                           WHERE id=?""", (t["shares"], t["linked_buy_id"]))
        con.execute("DELETE FROM trades WHERE id=?", (trade_id,))


# ---------- notes ----------
def add_note_row(text: str, date: str | None = None, related_trade_ids: list[int] | None = None,
                 is_test: bool = False) -> int:
    with connect() as con:
        cur = con.execute("INSERT INTO notes (date,text,related_trade_ids,is_test,created_at) VALUES (?,?,?,?,?)",
                          (date or now_tpe().strftime("%Y-%m-%d"), text,
                           json.dumps(related_trade_ids or []), int(is_test), _now()))
        return cur.lastrowid


def list_notes(since: str | None = None, include_test: bool = False, limit: int = 100) -> list[dict]:
    q, a = "SELECT * FROM notes WHERE 1=1", []
    if since:
        q += " AND date>=?"
        a.append(since)
    if not include_test:
        q += " AND is_test=0"
    q += " ORDER BY date DESC, id DESC LIMIT ?"
    a.append(limit)
    with connect() as con:
        return [_row(r) for r in con.execute(q, a).fetchall()]


def delete_note(note_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM notes WHERE id=?", (note_id,))


# ---------- market_daily ----------
def _ymd(s: str | None) -> str | None:
    """'2026/10/16'、'20261016'、'115/10/16'、'1151016' -> '2026-10-16'"""
    if not s:
        return None
    s = s.replace("-", "/").strip()
    if "/" in s:
        y, m, d = s.split("/")
    elif len(s) == 8:
        y, m, d = s[:4], s[4:6], s[6:]
    elif len(s) == 7:
        y, m, d = s[:3], s[3:5], s[5:]
    else:
        return s
    y = int(y) + 1911 if int(y) < 1911 else int(y)
    return f"{y:04d}-{int(m):02d}-{int(d):02d}"


def snapshot_market(date: str | None = None, with_news: bool = True) -> dict:
    """呼叫現有工具，把最近一個交易日的盤勢存進 market_daily。

    date（YYYYMMDD）會傳給支援日期的工具（三大法人、期貨法人）；
    加權指數、台指期、美股取「最近一筆」，若日期對不上會記在 warnings。
    """
    from tools import news, tw_futures, tw_market, twse_inst, us_market

    raw, warnings = {}, []

    def call(key, fn):
        try:
            raw[key] = fn()
        except Exception as e:
            raw[key] = None
            warnings.append(f"{key}: {type(e).__name__}: {e}")

    call("institutional", lambda: twse_inst.get_institutional(date))
    call("foreign_top", lambda: twse_inst.get_foreign_top(date, top=5))
    call("taiex", lambda: tw_market.get_taiex(force="daily"))
    call("tx", lambda: tw_futures.get_tx_futures(force="daily"))
    call("fut_inst", lambda: tw_futures.get_futures_institutional(date))
    call("us", us_market.get_us_indices)
    if with_news:
        call("news", lambda: news.get_news_heat(max_headlines=10))

    inst = raw.get("institutional") or {}
    day = _ymd(inst.get("date")) or (_ymd(date) if date else now_tpe().strftime("%Y-%m-%d"))
    tx = ((raw.get("tx") or {}).get("daily")) or {}
    reg, night = tx.get("regular") or {}, tx.get("night") or {}
    fi = ((raw.get("fut_inst") or {}).get("by_investor")) or {}
    us = {i["symbol"]: i for i in (raw.get("us") or {}).get("indices", [])}
    taiex = raw.get("taiex") or {}
    ft = raw.get("foreign_top") or {}
    nw = raw.get("news") or {}

    for label, d in (("taiex", _ymd(taiex.get("date"))), ("tx_regular", _ymd(reg.get("trade_date"))),
                     ("fut_inst", _ymd((raw.get("fut_inst") or {}).get("date")))):
        if d and d != day:
            warnings.append(f"{label} 日期 {d} 與 {day} 不同")
    if night and _ymd(night.get("follows_regular_session")) != day:
        warnings.append("夜盤不是接在這個交易日之後")
        night = {}

    rec = {
        "date": day,
        "taiex_close": taiex.get("close"), "taiex_change": taiex.get("change"),
        "taiex_change_pct": taiex.get("change_pct"),
        "inst_foreign_net": inst.get("foreign_net"), "inst_trust_net": inst.get("trust_net"),
        "inst_dealer_net": inst.get("dealer_net"), "inst_total_net": inst.get("total_net"),
        "foreign_top_buy": json.dumps(ft.get("top_buy", []), ensure_ascii=False),
        "foreign_top_sell": json.dumps(ft.get("top_sell", []), ensure_ascii=False),
        "tx_contract": tx.get("contract"),
        "tx_day_close": reg.get("close"), "tx_day_change": reg.get("change"), "tx_day_volume": reg.get("volume"),
        "tx_night_close": night.get("close"), "tx_night_change": night.get("change"),
        "tx_night_vs_day": night.get("change_vs_regular_close"), "tx_night_volume": night.get("volume"),
        "fut_foreign_oi_net": (fi.get("外資及陸資") or {}).get("oi_net_lots"),
        "fut_trust_oi_net": (fi.get("投信") or {}).get("oi_net_lots"),
        "fut_dealer_oi_net": (fi.get("自營商") or {}).get("oi_net_lots"),
        "us_date": (us.get("^GSPC") or {}).get("date"),
        "top_keywords": json.dumps(nw.get("hot_keywords", [])[:10], ensure_ascii=False),
        "top_stocks": json.dumps(nw.get("hot_stocks", [])[:10], ensure_ascii=False),
        "raw_json": json.dumps({"data": raw, "warnings": warnings}, ensure_ascii=False, default=str),
        "created_at": _now(),
    }
    for sym, col in (("^DJI", "us_dji"), ("^IXIC", "us_ixic"), ("^GSPC", "us_gspc"), ("^SOX", "us_sox")):
        rec[col] = (us.get(sym) or {}).get("close")
        rec[col + "_pct"] = (us.get(sym) or {}).get("change_pct")

    with connect() as con:
        cols = ", ".join(rec)
        con.execute(f"INSERT OR REPLACE INTO market_daily ({cols}) VALUES ({', '.join('?' * len(rec))})",
                    tuple(rec.values()))
    out = get_market_day(day)
    out.pop("raw_json", None)
    out["warnings"] = warnings
    return out


def get_market_day(date: str) -> dict | None:
    with connect() as con:
        return _row(con.execute("SELECT * FROM market_daily WHERE date=?", (_ymd(date),)).fetchone())


def list_market_days(limit: int = 30) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM market_daily ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d.pop("raw_json", None)
        out.append(d)
    return out
