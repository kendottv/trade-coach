"""別名表（P2-G）：讀 data/aliases.json。指數／美股代號／族群英文名稱都從這裡查，不交給模型。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tools._common import DATA

PATH = DATA / "aliases.json"
SUGGEST = DATA / "alias_suggestions.json"
_T = None


def table() -> dict:
    global _T
    if _T is None:
        try:
            _T = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _T = {}
    return _T


def index_symbol(text: str) -> str | None:
    t = (text or "").lower()
    for sym, v in (table().get("indices") or {}).items():
        if any(a.lower() in t for a in v["aliases"]):
            return sym
    return None


def symbols_in(text: str) -> set[str]:
    """新聞標題裡提到的指數與美股代號（中文別名、英文名、或大寫代號整字）。"""
    out = set()
    for sym, v in (table().get("indices") or {}).items():
        if any((a in text) if re.search(r"[\u4e00-\u9fff]", a) else re.search(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", text)
               for a in v["aliases"] if len(a) >= 2):
            out.add(sym)
    for sym, names in (table().get("us_tickers") or {}).items():
        if re.search(rf"(?<![A-Za-z]){sym}(?![A-Za-z])", text) or any(n in text for n in names if len(n) >= 2):
            out.add(sym)
    return out


def theme_en(term: str) -> list[str] | None:
    return (table().get("theme_en") or {}).get((term or "").strip())


def fix_names(text: str) -> tuple[str, list[str]]:
    fixed = []
    for bad, good in (table().get("name_fixes") or {}).items():
        if bad.startswith("_") or bad not in text:
            continue
        text = text.replace(bad, good)
        fixed.append(f"{bad}→{good}")
    return text, fixed


def suggest(term: str, en: list[str]) -> None:
    """7B 翻譯的結果先記下來等人工審核（不會自動進表）。"""
    try:
        d = json.loads(SUGGEST.read_text(encoding="utf-8")) if SUGGEST.exists() else {}
        if d.get(term) != en:
            d[term] = en
            SUGGEST.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
