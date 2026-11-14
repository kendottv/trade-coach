"""共用 embedding（step12）：整個程式只載入一份 MiniLM。journal/index.py 和 tools/news_store.py 都走這裡。

- 模型：paraphrase-multilingual-MiniLM-L12-v2（384 維，支援中文）。
- normalize_embeddings=True：向量長度 1，內積就是 cosine，FAISS 用 IndexFlatIP。
- max_seq_length：模型預設 128 token（中文約 1 字 1 token）。新聞內文 chunk 約 300 字，所以 chunk 用 max_len=384，
  其他呼叫（日記、標題）維持 128，跟以前行為一樣。
"""
from __future__ import annotations

import threading

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_MAX_LEN = 128
_MODEL = None
_LOCK = threading.Lock()
LOADS = 0   # 測試用：這個 process 實際載入模型幾次（應該 ≤1）


def get_model():
    global _MODEL, LOADS
    with _LOCK:
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer
            _MODEL = SentenceTransformer(EMBED_MODEL)
            _MODEL.max_seq_length = DEFAULT_MAX_LEN
            LOADS += 1
    return _MODEL


def loaded() -> bool:
    return _MODEL is not None


def encode(texts, max_len: int | None = None):
    """回傳 float32、已正規化的向量（n×384）。失敗會丟例外。"""
    import numpy as np
    m = get_model()
    with _LOCK:   # max_seq_length 是模型上的屬性，暫時改完要改回來；同時間只讓一個執行緒用
        m.max_seq_length = max_len or DEFAULT_MAX_LEN
        try:
            v = m.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False, batch_size=32)
        finally:
            m.max_seq_length = DEFAULT_MAX_LEN
    return np.asarray(v, dtype="float32")


def encode_safe(texts, max_len: int | None = None):
    """同 encode，但沒裝套件或出錯時回 None（新聞庫可以降級成不用語意相似度）。"""
    try:
        return encode(texts, max_len)
    except Exception:  # noqa: BLE001
        return None


def warmup_async() -> None:
    """伺服器啟動時在背景載入模型，第一題就不用等。"""
    def _w():
        try:
            get_model()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_w, daemon=True, name="embed-warmup").start()
