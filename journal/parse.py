"""用對話記交易：Ollama（qwen2.5:7b，JSON 模式）把一句話拆成交易草稿。

原則：
- 模型只負責「抽欄位」，不算任何數字。
- 程式檢查：每個數字都必須真的出現在原句裡，否則清成 null 並警告（防止模型亂填）。
- 股票代號/名稱用上市櫃清單對照，不信任模型自己給的代號。
- 沒講的欄位（例如停損）標在 missing，不會補值。
- 草稿 confirmed=False，一定要 confirm_and_save(draft, confirmed=True) 才會寫入資料庫。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from journal import db  # noqa: E402
from tools._common import now_tpe  # noqa: E402

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"

SYSTEM = """你是交易日記的欄位抽取器。把使用者的一句話轉成 JSON，只抽取句子裡明確寫到的資訊。
規則：
- 句子沒提到的欄位一律填 null，絕對不要猜、不要補、不要自己算。
- side：買/買進/加碼/進場 = "buy"；賣/賣出/停損出場/停利/出場 = "sell"。
- stock：句子裡的股票名稱或代號原文（例如 "聯鈞"、"2330"）。
- price：成交價（數字）。注意「531買」「在531買」「531元」的 531 是價格，不是股票代號。
- quantity 與 unit：數量與單位，unit 只能是 "股" 或 "張"。
- stop_loss：停損價；target_prices：目標價/停利價的數字陣列，沒有就 []。
- reason：買賣理由原文（簡短）；strategy_tag：若有明確策略名稱（例如 突破、拉回、均線）才填，否則 null。
- date_text：句子裡的日期詞原文（例如 "今天"、"昨天"、"10/16"），沒有就 null。
只輸出 JSON，欄位：side, stock, price, quantity, unit, stop_loss, target_prices, reason, strategy_tag, date_text"""

REQUIRED = ("ticker", "side", "price", "shares")


def _stock_list() -> dict[str, str]:
    from tools.news import load_stock_list
    return load_stock_list()


def _find_stock(text: str, hint: str | None, stocks: dict[str, str]) -> tuple[str | None, str | None]:
    """先用原句比對最長的股票簡稱，再用模型給的 hint，最後找明確的 4 碼代號。"""
    names = {v: k for k, v in stocks.items() if len(k) == 4 and len(v) >= 2}
    try:   # 正規化別名：共信-KY 也可以講「共信」
        from tools.resolve import tw_name_index
        for a_, c in tw_name_index().items():
            names.setdefault(a_, c)
    except Exception:  # noqa: BLE001
        pass
    import unicodedata
    text = unicodedata.normalize("NFKC", text)
    hits = [n for n in names if n in text]
    if hits:
        n = max(hits, key=len)
        return names[n], stocks.get(names[n], n)
    if hint:
        h = str(hint).strip()
        if h in stocks:
            return h, stocks[h]
        if h in names:
            return names[h], h
    for m in re.finditer(r"(?<!\d)(\d{4,6})(?!\d)", text):
        c = m.group(1)
        # 後面接 股/張/元 的數字是數量或價格，不是代號
        tail = text[m.end():m.end() + 1]
        if c in stocks and tail not in ("股", "張", "元", "塊"):
            return c, stocks[c]
    return None, None


_CN = {"零": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str) -> int | None:
    """一～九十九的中文數字，例如 兩、十、十五、二十三。"""
    if not s:
        return None
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN.get(a, 1) if a else 1
        ones = _CN.get(b, 0) if b else 0
        return tens * 10 + ones
    return _CN.get(s) if len(s) == 1 else None


def _nums_in(text: str) -> set[float]:
    nums = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))}
    # 中文數量：「一張」「兩張」「十五股」
    for m in re.finditer(r"([一二兩两三四五六七八九十]{1,3})\s*(?:張|股)", text):
        v = _cn_to_int(m.group(1))
        if v is not None:
            nums.add(float(v))
    return nums


def _date_from(text: str, date_text: str | None) -> str:
    today = now_tpe().date()
    s = f"{date_text or ''} {text}"
    if "前天" in s:
        d = today - dt.timedelta(days=2)
    elif "昨天" in s or "昨日" in s:
        d = today - dt.timedelta(days=1)
    else:
        m = re.search(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})", date_text or "")
        if m:
            y = int(m.group(1) or today.year)
            d = dt.date(y, int(m.group(2)), int(m.group(3)))
        else:
            d = today
    return d.strftime("%Y-%m-%d")


def _ask_model(text: str) -> dict:
    from tools._common import ollama_chat
    j = ollama_chat({
        "model": MODEL, "format": "json", "stream": False,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": text}],
    }, "解析交易", timeout=180)
    return json.loads(j["message"]["content"])


def _price_from_text(text: str) -> float | None:
    """模型漏抓價格時的備援：「531買了10股」「買在531」「價格310」「@70」。
    排除停損/目標後面的數字和數量（後面接股/張）。"""
    clean = re.sub(r"(停損|停利|目標價?|stop|target)\s*[:：]?\s*\d+(?:\.\d+)?", " ", text, flags=re.I)
    for pat in _PRICE_PATTERNS:
        for m in re.finditer(pat, clean):
            v = float(m.group(1))
            tail = clean[m.end(1):m.end(1) + 2]
            if re.match(r"\s*(股|張)", tail) or v <= 0:
                continue
            return v
    return None


def parse_trade(text: str) -> dict:
    """一句話 -> 交易草稿（尚未存檔）。"""
    raw = _ask_model(text)
    stocks = _stock_list()
    nums = _nums_in(text)
    warnings = []

    def num(key, v):
        if v in (None, "", "null"):
            return None
        try:
            f = float(str(v).replace(",", ""))
        except ValueError:
            warnings.append(f"{key}={v!r} 不是數字，已清除")
            return None
        if f not in nums:
            warnings.append(f"{key}={f:g} 不在原句裡，已清除（不讓模型自己補）")
            return None
        return f

    ticker, name = _find_stock(text, raw.get("stock"), stocks)
    if raw.get("stock") and not ticker:
        warnings.append(f"找不到股票「{raw.get('stock')}」")

    side = raw.get("side") if raw.get("side") in ("buy", "sell") else None
    price = num("price", raw.get("price"))
    if price is None:
        rp = _price_from_text(text)
        if rp is not None:
            price = rp
            warnings.append(f"價格 {rp:g} 由規則從原句補上（模型沒抓到），請確認")
    qty = num("quantity", raw.get("quantity"))
    unit = raw.get("unit") if raw.get("unit") in ("股", "張") else None
    if qty is not None and unit is None:
        # 原句裡數字後面接的單位
        m = re.search(rf"(?:{qty:g}|[一二兩两三四五六七八九十]{{1,3}})\s*(股|張)", text)
        unit = m.group(1) if m else None
        if unit is None:
            warnings.append("數量沒有單位（股/張），請確認")
    shares = int(qty * 1000) if (qty is not None and unit == "張") else (int(qty) if qty is not None and unit == "股" else None)
    if price is not None and ticker and price == float(ticker):
        warnings.append("價格跟股票代號一樣，請確認")

    targets = []
    for t in raw.get("target_prices") or []:
        v = num("target_price", t)
        if v is not None:
            targets.append(v)

    draft = {
        "confirmed": False,
        "trade_date": _date_from(text, raw.get("date_text")),
        "ticker": ticker,
        "name": name,
        "side": side,
        "price": price,
        "shares": shares,
        "odd_lot": (shares % 1000 != 0) if shares else None,
        "stop_loss": num("stop_loss", raw.get("stop_loss")) if side != "sell" else None,
        "target_prices": targets,
        "reason": (raw.get("reason") or None),
        "strategy_tag": (raw.get("strategy_tag") or None),
        "source_text": text,
    }
    missing = [k for k in REQUIRED if draft.get(k) in (None, "")]
    if side == "buy":
        if draft["stop_loss"] is None:
            missing.append("stop_loss")
        if not targets:
            missing.append("target_prices")
    if not draft["reason"]:
        missing.append("reason")
    if draft["stop_loss"] and price and side == "buy" and draft["stop_loss"] >= price:
        warnings.append("停損價高於或等於買價，請確認")
    draft["missing"] = missing
    draft["blocking"] = [k for k in missing if k in REQUIRED]  # 這些沒補齊不能存
    draft["warnings"] = warnings
    draft["model_raw"] = raw
    return draft


POSITION_RE = re.compile(r"(持有|持股|庫存|手上有|手中有|目前有|現在有|我有)")


def parse_position(text: str) -> dict:
    """「我持有聯鈞 10股 成本531 停損483 目標570、640」→ 既有持倉草稿（程式解析，不經模型；存成一筆買單）。"""
    stocks = _stock_list()
    ticker, name = _find_stock(text, None, stocks)
    warnings = []
    m = re.search(r"(\d+(?:\.\d+)?)\s*(股|張)", text)
    shares = None
    if m:
        q = float(m.group(1))
        shares = int(q * 1000) if m.group(2) == "張" else int(q)
    else:
        cn = re.search(r"([一二兩两三四五六七八九十百]+)\s*(股|張)", text)
        if cn and _cn_to_int(cn.group(1)):
            shares = _cn_to_int(cn.group(1)) * (1000 if cn.group(2) == "張" else 1)

    def num(pat):
        mm = re.search(pat, text)
        return float(mm.group(1)) if mm else None
    cost = num(r"(?:平均成本|成本價|成本|均價|買在|買價)\s*[:：@]?\s*(\d+(?:\.\d+)?)")
    stop = num(r"停損\s*[:：]?\s*(?:設在|在)?\s*(\d+(?:\.\d+)?)")
    targets = []
    mt = re.search(r"目標(?:價)?\s*[:：]?\s*(?:是|在)?\s*([\d.\s、,，/和跟及]+)", text)
    if mt:
        targets = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", mt.group(1))]
    md = re.search(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})\s*(?:買|進)", text)
    if md:
        today = now_tpe().date()
        trade_date = dt.date(int(md.group(1) or today.year), int(md.group(2)), int(md.group(3))).strftime("%Y-%m-%d")
    else:
        trade_date = now_tpe().strftime("%Y-%m-%d")
        warnings.append("沒有買進日期，日期先用今天（記錄日）；持有天數會從今天算")
    reason = None
    mr = re.search(r"(?:因為|理由)[:：]?\s*(.+?)(?:\s+停損|\s+目標|$)", text)
    if mr:
        reason = mr.group(1).strip()
    draft = {"confirmed": False, "position_entry": True, "trade_date": trade_date, "ticker": ticker, "name": name,
             "side": "buy", "price": cost, "shares": shares, "odd_lot": (shares % 1000 != 0) if shares else None,
             "stop_loss": stop, "target_prices": targets, "reason": reason or "記錄既有持倉",
             "strategy_tag": "既有持倉", "source_text": text}
    missing = [k for k in REQUIRED if draft.get(k) in (None, "")]
    if stop is None:
        missing.append("stop_loss")
    if not targets:
        missing.append("target_prices")
    if stop and cost and stop >= cost:
        warnings.append("停損價高於或等於成本，請確認（如果是移動停損可以忽略）")
    if not ticker:
        warnings.append("找不到股票名稱或代號")
    draft["missing"] = missing
    draft["blocking"] = [k for k in missing if k in REQUIRED]
    draft["warnings"] = warnings
    return draft


def confirm_and_save(draft: dict, confirmed: bool = False, is_test: bool = False, **overrides) -> dict:
    """使用者確認後才存。overrides 可補欄位，例如 stop_loss=483。"""
    if not confirmed:
        return {"saved": False, "reason": "尚未確認，沒有存檔", "draft": draft}
    d = {**draft, **overrides}
    missing = [k for k in REQUIRED if d.get(k) in (None, "")]
    if missing:
        return {"saved": False, "reason": f"缺必要欄位：{missing}", "draft": d}
    if d["side"] == "buy":
        tid = db.add_buy(d["trade_date"], d["ticker"], d["price"], int(d["shares"]), name=d.get("name"),
                         reason=d.get("reason"), stop_loss=d.get("stop_loss"),
                         target_prices=d.get("target_prices"), strategy_tag=d.get("strategy_tag"),
                         is_test=is_test)
        ids = [tid]
    else:
        ids = db.add_sell(d["trade_date"], d["ticker"], d["price"], int(d["shares"]), name=d.get("name"),
                          reason=d.get("reason"), strategy_tag=d.get("strategy_tag"), is_test=is_test)
    try:
        from journal import index
        index.sync()
    except Exception as e:  # 索引失敗不影響交易紀錄
        return {"saved": True, "trade_ids": ids, "index_error": f"{type(e).__name__}: {e}"}
    return {"saved": True, "trade_ids": ids, "trades": [db.get_trade(i) for i in ids]}


def add_note(text: str, date: str | None = None, related_trade_ids: list[int] | None = None,
             is_test: bool = False) -> dict:
    """心得/檢討原文直接存（使用者自己的話，不經模型改寫），並加入索引。"""
    nid = db.add_note_row(text, date=date, related_trade_ids=related_trade_ids, is_test=is_test)
    out = {"saved": True, "note_id": nid}
    try:
        from journal import index
        index.sync()
    except Exception as e:
        out["index_error"] = f"{type(e).__name__}: {e}"
    return out
