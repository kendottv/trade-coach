"""日記檢索：FAISS（IndexFlatIP + 正規化 = cosine）索引 notes 和交易理由。

embedding 模型與 trade-rag 相同（只用模型名稱從 Hugging Face 載入，不讀 trade-rag 的檔案）。
索引檔跟著資料庫走：trades.db -> trades.faiss / trades.meta.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from journal import db  # noqa: E402

from tools.embed import EMBED_MODEL  # noqa: E402,F401  （step12：共用 tools/embed.py，整個程式只載入一份模型）


def _paths():
    base = db.DB_PATH.with_suffix("")
    return base.with_suffix(".faiss"), base.with_suffix(".meta.json")


def _embed(texts: list[str]) -> np.ndarray:
    from tools import embed
    return embed.encode(texts)   # 一樣是正規化的 float32、max_seq_length 128


def _load():
    import faiss
    fp, mp = _paths()
    if fp.exists() and mp.exists():
        return faiss.read_index(str(fp)), json.loads(mp.read_text(encoding="utf-8"))
    return faiss.IndexIDMap(faiss.IndexFlatIP(384)), {"next_id": 0, "items": {}, "keys": []}


def _save(index, meta):
    import faiss
    fp, mp = _paths()
    faiss.write_index(index, str(fp))
    mp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _docs(include_test: bool) -> list[dict]:
    docs = []
    for n in db.list_notes(include_test=include_test, limit=100000):
        docs.append({"key": f"note:{n['id']}", "kind": "note", "ref_id": n["id"], "date": n["date"],
                     "text": n["text"]})
    for t in db.list_trades(include_test=include_test, limit=100000):
        if not t.get("reason"):
            continue
        side = "買進" if t["side"] == "buy" else "賣出"
        txt = (f"{t['trade_date']} {side} {t['ticker']} {t.get('name') or ''} @{t['price']:g} "
               f"理由：{t['reason']}")
        if t.get("strategy_tag"):
            txt += f" 策略：{t['strategy_tag']}"
        docs.append({"key": f"trade:{t['id']}", "kind": "trade", "ref_id": t["id"],
                     "date": t["trade_date"], "text": txt})
    return docs


def sync(include_test: bool | None = None) -> dict:
    """把還沒進索引的 notes / 交易理由加進去（增量）。"""
    if include_test is None:
        include_test = "test" in db.DB_PATH.name
    index, meta = _load()
    have = set(meta["keys"])
    new = [d for d in _docs(include_test) if d["key"] not in have]
    if new:
        vecs = _embed([d["text"] for d in new])
        ids = np.arange(meta["next_id"], meta["next_id"] + len(new), dtype="int64")
        index.add_with_ids(vecs, ids)
        for i, d in zip(ids.tolist(), new):
            meta["items"][str(i)] = d
            meta["keys"].append(d["key"])
        meta["next_id"] += len(new)
        _save(index, meta)
    return {"added": len(new), "total": index.ntotal}


def rebuild() -> dict:
    """刪掉/修改紀錄後重建（索引只會增量加，不會自動刪）。"""
    for p in _paths():
        if p.exists():
            p.unlink()
    return sync()


def search(query: str, k: int = 5, kind: str | None = None) -> list[dict]:
    index, meta = _load()
    if index.ntotal == 0:
        return []
    scores, ids = index.search(_embed([query]), min(index.ntotal, k * 3 if kind else k))
    out = []
    for s, i in zip(scores[0].tolist(), ids[0].tolist()):
        if i < 0:
            continue
        d = meta["items"].get(str(i))
        if d and (kind is None or d["kind"] == kind):
            out.append({**d, "score": round(s, 3)})
    return out[:k]
