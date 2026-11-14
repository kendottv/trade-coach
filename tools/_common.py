"""共用小工具。"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TPE = dt.timezone(dt.timedelta(hours=8))
# 所有 Ollama 請求共用一把鎖：7B（對話）和 14B（判斷）放不進同一張 12GB 顯卡，不能同時跑
OLLAMA_LOCK = threading.RLock()
OLLAMA_HOST = __import__("os").environ.get("OLLAMA_HOST", "http://localhost:11434")

# 目前的模型請求狀態（給網頁「模型狀態」看）：idle / waiting（排隊等鎖）/ running（送出、等模型回答，含載入）
MODEL_STATUS = {"state": "idle", "model": None, "purpose": None, "since": None}
# 最近的模型呼叫紀錄（每一輪結束時 agent.chat 依時間撈出這一輪用了哪些模型）
MODEL_CALLS: list[dict] = []
_CALLS_LOCK = threading.Lock()


def ollama_chat(body: dict, purpose: str, timeout: int = 300) -> dict:
    """所有 Ollama /api/chat 請求都走這裡：共用鎖（7B／14B 不同時跑）、更新狀態、記錄用了哪個模型和花多久。"""
    import time

    import requests

    model = body.get("model")
    t0 = time.time()
    MODEL_STATUS.update(state="waiting", model=model, purpose=purpose, since=t0)
    rec = {"model": model, "purpose": purpose, "t0": t0, "thread": threading.get_ident()}
    try:
        with OLLAMA_LOCK:
            rec["wait_s"] = round(time.time() - t0, 1)
            MODEL_STATUS.update(state="running", model=model, purpose=purpose, since=time.time())
            if "keep_alive" not in body:   # step11：7B 預設常駐 30 分（14B judge 自己帶 15m）
                import os as _os
                body = {**body, "keep_alive": _os.environ.get("TRADE_COACH_KEEP_ALIVE", "30m")}
            r = requests.post(f"{OLLAMA_HOST}/api/chat", json=body, timeout=timeout)
            r.raise_for_status()
            j = r.json()
        rec.update(load_s=round(j.get("load_duration", 0) / 1e9, 1), eval_s=round(j.get("eval_duration", 0) / 1e9, 1),
                   total_s=round(time.time() - t0, 1), tokens=j.get("eval_count"), ok=True)
        return j
    except Exception as e:
        rec.update(total_s=round(time.time() - t0, 1), ok=False, error=f"{type(e).__name__}: {e}"[:200])
        raise
    finally:
        MODEL_STATUS.update(state="idle", model=None, purpose=None, since=None, last=dict(rec))
        with _CALLS_LOCK:
            MODEL_CALLS.append(rec)
            del MODEL_CALLS[:-200]


def model_calls_since(t0: float) -> list[dict]:
    with _CALLS_LOCK:
        return [{k: v for k, v in c.items() if k not in ("t0", "thread")} for c in MODEL_CALLS if c["t0"] >= t0]


def ollama_ps() -> dict:
    """Ollama 目前載入的模型（/api/ps）：名稱、顯示卡用量、什麼時候自動卸載。"""
    import requests

    try:
        j = requests.get(f"{OLLAMA_HOST}/api/ps", timeout=3).json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "models": []}
    import re as _re
    out = []
    for m in j.get("models") or []:
        until = None
        exp = m.get("expires_at") or ""
        try:  # 例：2026-10-19T01:20:12.123456789+08:00（奈秒要截成微秒才能解析）
            e = _re.sub(r"(\.\d{6})\d+", r"\1", exp).replace("Z", "+00:00")
            until = int((dt.datetime.fromisoformat(e) - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except Exception:  # noqa: BLE001
            pass
        out.append({"name": m.get("name") or m.get("model"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                    "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1), "expires_at": exp,
                    "unload_in_s": until, "context": m.get("context_length")})
    return {"ok": True, "models": out}


def now_tpe() -> dt.datetime:
    return dt.datetime.now(TPE)


def to_num(s):
    """'1,234.5' / '-12' / '' -> float|None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def dump_selftest(name: str, obj) -> None:
    """自測結果：印出並存成 data/selftest_<name>.json（UTF-8，方便看中文）。"""
    import json
    txt = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    (DATA / f"selftest_{name}.json").write_text(txt, encoding="utf-8")
    print(txt)


# ---------- 交易時段 ----------
def _hm(t: dt.datetime) -> int:
    return t.hour * 100 + t.minute


def tw_stock_session(t: dt.datetime | None = None) -> str:
    """'day'（09:00–13:30，含收盤撮合到 13:35）或 'closed'。不含國定假日判斷。"""
    t = t or now_tpe()
    if t.weekday() < 5 and 900 <= _hm(t) <= 1335:
        return "day"
    return "closed"


def tw_futures_session(t: dt.datetime | None = None) -> str:
    """台指期：'day'（08:45–13:45）、'night'（15:00–隔日 05:00）或 'closed'。

    夜盤從週一到週五的 15:00 開始，所以週六 00:00–05:00 也算夜盤；週一 00:00–05:00 不是。
    國定假日不判斷（假日時 Shioaji 會回最後成交，結果的 as_of 會顯示時間）。
    """
    t = t or now_tpe()
    wd, hm = t.weekday(), _hm(t)
    if wd < 5 and 845 <= hm <= 1345:
        return "day"
    if wd < 5 and hm >= 1500:
        return "night"
    if 1 <= wd <= 5 and hm < 500:  # 週二～週六凌晨
        return "night"
    return "closed"


def roc_to_iso(s) -> str | None:
    """民國日期 '1151016' / '115/10/16' → '2026-10-16'；西元 '2026/10/16'、'20261016' 也接受。"""
    import re as _re
    if not s:
        return None
    s = str(s).strip()
    m = _re.fullmatch(r"(\d{2,3})/?(\d{2})/?(\d{2})", s.replace("/", "")) if len(s.replace("/", "")) in (6, 7) else None
    if m:
        return f"{int(m.group(1)) + 1911:04d}-{m.group(2)}-{m.group(3)}"
    m = _re.fullmatch(r"(\d{4})[/-]?(\d{1,2})[/-]?(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def data_time(source: str, date_iso: str | None = None, quote_time: str | None = None, label: str = "收盤") -> dict:
    """行情資料的「資料時間」：as_of = 這筆資料真正的時間（每日資料 = 那天收盤；即時 = 報價時間），
    fetched_at = 查詢時間。data_type 寫清楚是「每日收盤資料（非即時）」還是「即時」。"""
    live = source.endswith("_live")
    if live:
        return {"as_of": quote_time or iso_tpe(), "data_type": "即時" if source.startswith("shioaji") else
                "盤中（yfinance，可能延遲數分鐘）", "is_live": True, "fetched_at": iso_tpe()}
    if source == "shioaji_snapshot":
        return {"as_of": quote_time or iso_tpe(), "data_type": "永豐快照（非盤中，為最後成交）", "is_live": False,
                "fetched_at": iso_tpe()}
    return {"as_of": f"{date_iso} {label}" if date_iso else None, "data_date": date_iso,
            "data_type": "每日收盤資料（非即時）", "is_live": False, "fetched_at": iso_tpe()}


def bar_label(last_date: str | None, us: bool = False) -> str:
    """日 K 最後一根是不是「今天、還沒收盤」：是就回「盤中」，否則「收盤」。"""
    if not last_date:
        return "收盤"
    if us:
        from zoneinfo import ZoneInfo
        ny = dt.datetime.now(ZoneInfo("America/New_York"))
        open_ = ny.weekday() < 5 and 930 <= ny.hour * 100 + ny.minute < 1600
        return "盤中" if open_ and last_date[:10] == ny.strftime("%Y-%m-%d") else "收盤"
    n = now_tpe()
    return "盤中" if last_date[:10] == n.strftime("%Y-%m-%d") and n.hour * 100 + n.minute < 1430 else "收盤"


def iso_tpe(t: dt.datetime | None = None) -> str:
    return (t or now_tpe()).strftime("%Y-%m-%d %H:%M:%S+08:00")
