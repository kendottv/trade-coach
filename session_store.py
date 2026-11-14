"""對話 session 存檔：伺服器重啟後對話（history、待確認草稿、追問主題）還在。

存在 SQLite：data/sessions.db（測試資料庫時用 data/sessions_test.db）。
只存對話文字與草稿，不存工具原始結果、不存任何金鑰。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from tools._common import now_tpe

ROOT = Path(__file__).resolve().parent
MAX_HISTORY = 200   # 每個 session 最多存幾則訊息
_LOCK = threading.Lock()

SCHEMA = """CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    turns      INTEGER NOT NULL DEFAULT 0,
    history    TEXT NOT NULL,     -- JSON list of {role, content}
    pending    TEXT,              -- JSON 待確認交易草稿 或 NULL
    topic      TEXT               -- JSON 追問主題 {entity, intent, focus}
)"""


def store_path(db_mode: str) -> Path:
    return ROOT / "data" / ("sessions_test.db" if db_mode == "test" else "sessions.db")


def _con(db_mode: str) -> sqlite3.Connection:
    p = store_path(db_mode)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute(SCHEMA)
    return con


def save(session, db_mode: str) -> None:
    now = now_tpe().isoformat(timespec="seconds")
    hist = session.history[-MAX_HISTORY:]
    with _LOCK, _con(db_mode) as con:
        con.execute(
            """INSERT INTO sessions(id, created_at, updated_at, turns, history, pending, topic)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, turns=excluded.turns,
                 history=excluded.history, pending=excluded.pending, topic=excluded.topic""",
            (session.id, getattr(session, "created_at", now), now, getattr(session, "turns", 0),
             json.dumps(hist, ensure_ascii=False),
             json.dumps(session.pending, ensure_ascii=False, default=str) if session.pending else None,
             json.dumps(session.topic or {}, ensure_ascii=False)))


def load_into(session, db_mode: str) -> bool:
    """把存檔內容放回 session；找不到回傳 False。"""
    with _LOCK, _con(db_mode) as con:
        row = con.execute("SELECT created_at, turns, history, pending, topic FROM sessions WHERE id=?",
                          (session.id,)).fetchone()
    if not row:
        return False
    session.created_at, session.turns = row[0], row[1] or 0
    session.history = json.loads(row[2] or "[]")
    session.pending = json.loads(row[3]) if row[3] else None
    topic = json.loads(row[4] or "{}")
    if isinstance(topic.get("entity"), list):
        topic["entity"] = tuple(topic["entity"])
    session.topic = topic
    return True


def delete(sid: str, db_mode: str) -> None:
    with _LOCK, _con(db_mode) as con:
        con.execute("DELETE FROM sessions WHERE id=?", (sid,))


def count(db_mode: str) -> int:
    with _LOCK, _con(db_mode) as con:
        return con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
