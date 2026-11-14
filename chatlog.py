"""對話紀錄（給之後調校用）。

每一輪寫一行 JSON 到 data/chat_logs/YYYY-MM-DD.jsonl（台北日期，UTF-8，ensure_ascii=False）。
測試資料庫（db_mode=test，例如 eval）寫到 data/chat_logs/test/，不會混進正式紀錄。
關掉：環境變數 TRADE_COACH_CHATLOG=0。

安全：不寫任何金鑰。寫入前會遞迴遮蔽「名稱像金鑰」的欄位（key/secret/token/password…），
字串裡像 `SHIOAJI_xxx=...`、`api_key: ...`、長串 hex/base64 也會遮掉。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from tools._common import now_tpe

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "data" / "chat_logs"
MAX_RESULT_CHARS = 4000      # 單一工具結果超過就截斷（另外保留 source / as_of / warnings 摘要）
REDACTED = "[REDACTED]"
_LOCK = threading.Lock()

_KEY_NAME = re.compile(r"(api[_-]?key|secret|token|passw(or)?d|passwd|credential|private[_-]?key|(?<![a-z])auth(?![a-z])|authorization|bearer|"
                       r"cookie|session[_-]?key|person[_-]?id|ca[_-]?(path|passwd)|cert)", re.I)
_KV_IN_TEXT = re.compile(r"((?:SHIOAJI|API|SECRET|TOKEN|PASSWORD|PASSWD|KEY)[A-Z_]*\s*[=:]\s*)(\S+)", re.I)
_LONG_BLOB = re.compile(r"(?<![\w/.])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}(?![\w/.])")


def enabled() -> bool:
    return os.environ.get("TRADE_COACH_CHATLOG", "1") not in ("0", "false", "no")


def _scrub_str(s: str) -> str:
    s = _KV_IN_TEXT.sub(lambda m: m.group(1) + REDACTED, s)
    return _LONG_BLOB.sub(REDACTED, s)


def scrub(obj, _depth=0):
    """遞迴遮蔽像金鑰的欄位與字串。"""
    if _depth > 30:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k)
            if _KEY_NAME.search(ks) and not isinstance(v, (dict, list, bool)) and v not in (None, ""):
                out[ks] = REDACTED
            else:
                out[ks] = scrub(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


def provenance(out, limit: int = 12) -> dict:
    """從工具結果裡撈 source / as_of / date / warnings（截斷後還看得到資料來源）。"""
    found = {"source": [], "as_of": [], "date": [], "warnings": []}

    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("source", "as_of", "date", "data_date") and isinstance(v, (str, int, float)):
                    key = "date" if k == "data_date" else k
                    if v not in found[key] and len(found[key]) < limit:
                        found[key].append(v)
                elif k in ("warning", "warnings"):
                    for w in (v if isinstance(v, list) else [v]):
                        if isinstance(w, str) and w not in found["warnings"]:
                            found["warnings"].append(w)
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o[:50]:
                walk(v, depth + 1)

    walk(out)
    return {k: v for k, v in found.items() if v}


def slim_result(out):
    txt = json.dumps(out, ensure_ascii=False, default=str)
    if len(txt) <= MAX_RESULT_CHARS:
        return out
    return {"_truncated": True, "_chars": len(txt), "preview": txt[:MAX_RESULT_CHARS] + "…"}


def log_path(db_mode: str = "real", day: str | None = None) -> Path:
    d = LOG_DIR / "test" if db_mode == "test" else LOG_DIR
    return d / f"{day or now_tpe().strftime('%Y-%m-%d')}.jsonl"


def log_turn(record: dict) -> Path | None:
    if not enabled():
        return None
    rec = scrub(record)
    p = log_path(rec.get("db_mode", "real"))
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return p
