"""決策紀錄（decision_log）：助理每給一次個股／族群判斷，就記一筆，之後回填 T+1/T+5/T+20 實際報酬與價位是否觸及。

- 存在獨立的 SQLite：data/decision_log.db（不是 trades.db）。
- 測試／eval：TRADE_COACH_DB 指到 test_*.db 時自動改用 data/test_decision_log.db；
  也可用環境變數 TRADE_COACH_DECISION_LOG 直接指定路徑。
- 只記錄、不影響回覆：寫入失敗只回傳 None，不會讓對話出錯。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from tools._common import DATA, iso_tpe

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,          -- 回覆時間（台北時間 ISO）
    session_id    TEXT,
    question      TEXT,
    kind          TEXT NOT NULL,          -- stock / sector
    symbol        TEXT NOT NULL,          -- 個股代號（3450、NVDA）或族群名稱
    yf_symbol     TEXT,                   -- 回填用的 yfinance 代號（個股）
    market        TEXT,                   -- tw / us
    name          TEXT,
    question_type TEXT,
    verdict       TEXT,                   -- 偏多 / 中性 / 偏空
    confidence    TEXT,
    judged_by     TEXT,                   -- model / code_fallback / code
    model         TEXT,
    evidence_ids  TEXT,                   -- JSON：判斷引用的證據 id（E#）
    evidence      TEXT,                   -- JSON：全部證據 [{id,cat,side,text}]
    asof_date     TEXT,                   -- 判斷所根據的最後一根日 K 日期
    intraday      INTEGER,                -- 最後一根 K 棒是否盤中
    price         REAL,                   -- 判斷當下的價格（個股現價／收盤）
    support       REAL,                   -- 最近支撐（不含你的成本/停損）
    support_name  TEXT,
    resistance    REAL,                   -- 最近壓力
    resistance_name TEXT,
    levels        TEXT,                   -- JSON：全部 L# 價位
    members       TEXT,                   -- JSON：族群成員 [{symbol, close, market}]
    -- 以下由 backfill 回填
    base_close    REAL,                   -- asof_date 當天的最終收盤（回填時取得）
    ret_1d REAL, ret_5d REAL, ret_20d REAL,   -- %
    hit_support   INTEGER, hit_resistance INTEGER, first_hit TEXT,   -- 20 個交易日內
    mfe_20d REAL, mae_20d REAL,           -- 20 日內最大有利／不利幅度（%，以價格方向計）
    bars_after    INTEGER,                -- 回填時 asof 之後已有幾根 K
    filled_at     TEXT,
    fill_error    TEXT
);
CREATE INDEX IF NOT EXISTS ix_dl_symbol ON decision_log(symbol, asof_date);
"""


def log_path() -> Path:
    p = os.environ.get("TRADE_COACH_DECISION_LOG")
    if p:
        return Path(p)
    db = os.environ.get("TRADE_COACH_DB", "")
    if "test" in Path(db).name.lower():
        return DATA / "test_decision_log.db"
    return DATA / "decision_log.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def _cited(v: dict) -> list[str]:
    return [p.get("id") for k in ("bullish", "bearish") for p in (v.get(k) or []) if isinstance(p, dict) and p.get("id")]


def _model(v: dict) -> str:
    return (v.get("info") or {}).get("model") if v.get("by") == "model" else "code"


def _insert(row: dict) -> int | None:
    try:
        with connect() as con:
            cols = ",".join(row)
            cur = con.execute(f"INSERT INTO decision_log ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
            return cur.lastrowid
    except Exception:  # noqa: BLE001 — 紀錄失敗不能影響回覆
        return None


def log_stock(b: dict, v: dict, question: str = "", session_id: str | None = None) -> int | None:
    """個股判斷：b = decision.decision_brief(...)，v = decision.judge(...) 或 code_verdict(...)。"""
    tg, t, lv = b.get("target") or {}, b.get("tech") or {}, b.get("levels") or []
    sup = [x for x in lv if x["kind"] == "支撐" and not x["name"].startswith("你的")]
    res = [x for x in lv if x["kind"] == "壓力" and not x["name"].startswith("你的")]
    return _insert({
        "ts": iso_tpe(), "session_id": session_id, "question": question, "kind": "stock",
        "symbol": tg.get("symbol"), "yf_symbol": tg.get("yf") or {"TAIEX": "^TWII"}.get(tg.get("symbol"), tg.get("symbol")),
        "market": "us" if str(tg.get("kind", "")).startswith("us") else "tw", "name": tg.get("name"),
        "question_type": b.get("question_type"), "verdict": v.get("lean"), "confidence": v.get("confidence"),
        "judged_by": v.get("by") or "code", "model": _model(v), "evidence_ids": _j(_cited(v)),
        "evidence": _j([{k: x.get(k) for k in ("id", "cat", "side", "text")} for x in b.get("items") or []]),
        "asof_date": t.get("last_date"), "intraday": int(bool(t.get("intraday"))), "price": t.get("close"),
        "support": sup[-1]["value"] if sup else None, "support_name": sup[-1]["name"] if sup else None,
        "resistance": res[0]["value"] if res else None, "resistance_name": res[0]["name"] if res else None,
        "levels": _j(lv), "members": None})


def log_sector(b: dict, v: dict, question: str = "", session_id: str | None = None) -> int | None:
    """族群判斷：b = sector_decision.sector_brief(...)，v = judge_sector(...)。回填用成員等權平均報酬。"""
    g = b.get("group") or {}
    mk = b.get("market") or "tw"
    return _insert({
        "ts": iso_tpe(), "session_id": session_id, "question": question, "kind": "sector",
        "symbol": str(b.get("theme") or b.get("query")), "yf_symbol": None, "market": mk, "name": b.get("query"),
        "question_type": "族群", "verdict": v.get("lean"), "confidence": v.get("confidence"),
        "judged_by": v.get("by") or "code", "model": _model(v), "evidence_ids": _j(_cited(v)),
        "evidence": _j([{k: x.get(k) for k in ("id", "cat", "side", "text")} for x in b.get("items") or []]),
        "asof_date": g.get("last_date"), "intraday": int(bool(g.get("intraday"))), "price": None,   # 族群沒有單一價格；報酬用成員等權平均回填
        "support": None, "support_name": None, "resistance": None, "resistance_name": None, "levels": None,
        "members": _j([{"symbol": s.get("symbol"), "close": s.get("close"), "market": mk} for s in b.get("stocks") or []])})
