"""整理某一天的對話紀錄（data/chat_logs/YYYY-MM-DD.jsonl），方便調校。

用法：
  .venv\\Scripts\\python.exe eval\\review_logs.py                 今天（台北時間）的正式紀錄
  .venv\\Scripts\\python.exe eval\\review_logs.py --date 2026-10-18
  .venv\\Scripts\\python.exe eval\\review_logs.py --test          測試庫紀錄（data/chat_logs/test/）
  .venv\\Scripts\\python.exe eval\\review_logs.py --export        另外把被標記的輪次輸出成候選 eval 案例
  .venv\\Scripts\\python.exe eval\\review_logs.py --json          摘要用 JSON 輸出
  .venv\\Scripts\\python.exe eval\\review_logs.py --out r.txt     摘要寫成 UTF-8 檔

摘要內容：輪數 / session 數、路由方式（規則、追問、模型、澄清、按鈕）、工具使用次數、
有警告的輪次（沒叫工具、未驗證數字、工具錯誤…）、最慢的幾輪。
候選 eval 案例會寫到 eval/candidates/YYYY-MM-DD.json：每筆有 session、上一句（追問用）、問題、
實際叫的工具、警告，以及空的 expect 欄位讓人工填期望工具後再搬進 run_eval.py 的 CASES。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatlog import log_path  # noqa: E402
from tools._common import now_tpe  # noqa: E402

SLOW_S = 20.0
# 這些警告代表「值得看一下」；filler_stripped 只是清掉客套話，不算
FLAG_KINDS = {"no_tool", "no_tool_numbers", "unverified_numbers", "tool_error", "retry_numbers_without_tool",
              "max_tool_rounds", "clarify_asked", "shioaji_not_configured"}


def warn_kind(w) -> str:
    return next(iter(w)) if isinstance(w, dict) else str(w)


def load(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                print(f"（第 {i} 行不是合法 JSON，略過）", file=sys.stderr)
    return rows


def route_label(r: dict) -> str:
    rt = r.get("route") or {}
    mode = rt.get("mode", "?")
    if mode == "router" and rt.get("follow_up"):
        return f"router+follow_up({rt['follow_up'].get('kind')})"
    if mode == "router":
        return f"router({rt.get('answer', '?')})"
    return mode


def flags(r: dict, slow_s: float) -> list[str]:
    f = [warn_kind(w) for w in r.get("warnings") or [] if warn_kind(w) in FLAG_KINDS]
    if (r.get("latency_s") or 0) >= slow_s:
        f.append("slow")
    return f


def summarize(rows: list[dict], top: int, slow_s: float) -> dict:
    tools = Counter(t["name"] for r in rows for t in r.get("tool_calls") or [])
    routes = Counter(route_label(r) for r in rows)
    warns = Counter(warn_kind(w) for r in rows for w in r.get("warnings") or [])
    flagged = [r for r in rows if flags(r, slow_s)]
    lat = sorted((r.get("latency_s") or 0 for r in rows))
    slowest = sorted(rows, key=lambda r: -(r.get("latency_s") or 0))[:top]
    return {
        "turns": len(rows), "sessions": len({r.get("session_id") for r in rows}),
        "models": dict(Counter(r.get("model") for r in rows)),
        "latency_s": {"median": lat[len(lat) // 2] if lat else None, "max": lat[-1] if lat else None},
        "routes": dict(routes.most_common()), "tools": dict(tools.most_common()),
        "warnings": dict(warns.most_common()),
        "flagged": [{"ts": r.get("ts"), "session_id": r.get("session_id"), "turn": r.get("turn"),
                     "user": r.get("user"), "flags": flags(r, slow_s),
                     "tools": [t["name"] for t in r.get("tool_calls") or []],
                     "reply": (r.get("reply") or "")[:160]} for r in flagged],
        "slowest": [{"latency_s": r.get("latency_s"), "user": r.get("user"), "route": route_label(r),
                     "tools": [t["name"] for t in r.get("tool_calls") or []]} for r in slowest],
    }


def candidates(rows: list[dict], slow_s: float) -> list[dict]:
    """被標記的輪次 → 候選 eval 案例（附上一句，追問才測得出來）。"""
    prev_by_sid: dict[str, str] = {}
    out = []
    for r in sorted(rows, key=lambda r: (r.get("session_id"), r.get("turn") or 0)):
        sid = r.get("session_id")
        f = flags(r, slow_s)
        if f and (r.get("route") or {}).get("mode") != "button":
            out.append({"session_id": sid, "turn": r.get("turn"), "previous_user": prev_by_sid.get(sid),
                        "question": r.get("user"), "called": [t["name"] for t in r.get("tool_calls") or []],
                        "flags": f, "reply": r.get("reply"), "expect": []})
        prev_by_sid[sid] = r.get("user")
    return out


def print_text(day: str, path: Path, s: dict) -> None:
    print(f"== {day}  {path}")
    print(f"輪數 {s['turns']}，session {s['sessions']}，模型 {s['models']}，"
          f"延遲中位數 {s['latency_s']['median']}s / 最大 {s['latency_s']['max']}s")
    print("路由：" + "、".join(f"{k} {v}" for k, v in s["routes"].items()))
    print("工具：" + ("、".join(f"{k} {v}" for k, v in s["tools"].items()) or "（無）"))
    print("警告：" + ("、".join(f"{k} {v}" for k, v in s["warnings"].items()) or "（無）"))
    print(f"\n-- 需要看的輪次（{len(s['flagged'])}）")
    for r in s["flagged"]:
        print(f"[{r['ts']}] {r['session_id']}#{r['turn']} {','.join(r['flags'])} | {r['user']}")
        print(f"    工具 {r['tools'] or '（無）'} → {r['reply'].replace(chr(10), ' / ')}")
    print("\n-- 最慢的輪次")
    for r in s["slowest"]:
        print(f"{r['latency_s']:>6}s  {r['route']:<34} {r['tools']}  {r['user']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="整理 trade-coach 對話紀錄")
    ap.add_argument("--date", default=now_tpe().strftime("%Y-%m-%d"))
    ap.add_argument("--test", action="store_true", help="看測試庫紀錄 data/chat_logs/test/")
    ap.add_argument("--file", help="直接指定 jsonl 檔")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--slow", type=float, default=SLOW_S, help="超過幾秒算慢（預設 20）")
    ap.add_argument("--export", action="store_true", help="輸出候選 eval 案例到 eval/candidates/")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="把摘要寫到 UTF-8 檔案（PowerShell 轉向 > 可能會亂碼時用）")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(a.file) if a.file else log_path("test" if a.test else "real", a.date)
    if not path.exists():
        print(f"找不到紀錄：{path}")
        return 1
    rows = load(path)
    s = summarize(rows, a.top, a.slow)
    if a.out:
        import contextlib
        with open(a.out, "w", encoding="utf-8") as f, contextlib.redirect_stdout(f):
            print(json.dumps(s, ensure_ascii=False, indent=1)) if a.json else print_text(a.date, path, s)
        print(f"摘要已寫到 {a.out}")
    elif a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print_text(a.date, path, s)
    if a.export:
        c = candidates(rows, a.slow)
        out = ROOT / "eval" / "candidates" / f"{path.stem}{'-test' if a.test else ''}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n候選 eval 案例 {len(c)} 筆 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
