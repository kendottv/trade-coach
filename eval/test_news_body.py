"""step12 測試（用 .venv 跑，需要 sentence-transformers、faiss 與本機 Ollama 7B）：
1) chunk_text：長度、重疊、覆蓋全文
2) 共用 embedding：journal 與新聞庫只載入一份模型
3) validate_judgment：quote 必須出自內文
4) 7B 讀內文：聳動標題＋溫和內文 → 不判利空、標出誇大；真正的利空內文 → 利空
5) FAISS chunk 檢索：問「營收」時取到講營收的那一塊
用法：.venv\\Scripts\\python.exe eval\\test_news_body.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = Path(tempfile.mkdtemp()) / "news_fixture.db"
os.environ["TRADE_COACH_NEWS_DB"] = str(tmp)

from tools import embed, news_store  # noqa: E402
from tools._common import now_tpe  # noqa: E402

ok = fail = 0


def check(name, cond, info=""):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(("PASS " if cond else "FAIL ") + name, info if not cond else "")


# 1) chunking
body = "".join(f"第{i}句：聯鈞第三季營收成長，主要受惠於光通訊模組出貨增加。" for i in range(40))
ch = news_store.chunk_text(body, 300, 60)
check("chunk 數量 >1", len(ch) > 1, len(ch))
check("chunk 長度 ≤ size+overlap", all(len(c) <= 360 for c in ch), [len(c) for c in ch])
check("相鄰 chunk 有重疊", all(a[-60:] == b[:60] for a, b in zip(ch, ch[1:])))
check("每一句都在某塊裡", all(any(f"第{i}句" in c for c in ch) for i in range(40)))
check("短文一塊", news_store.chunk_text("短新聞。只有兩句。") == ["短新聞。只有兩句。"])

# 2) 共用 embedding
from journal import index as jidx  # noqa: E402
v1 = jidx._embed(["聯鈞營收"])
v2 = news_store._embed(["聯鈞營收"])
check("journal 與新聞庫同一個模型、只載入一次", embed.LOADS == 1 and abs(float(v1[0] @ v2[0]) - 1) < 1e-4, embed.LOADS)
check("max_seq_length 用完還原 128", embed.get_model().max_seq_length == 128)
tok = embed.get_model().tokenizer
check("300 字中文 chunk 在 384 token 內", len(tok(ch[0])["input_ids"]) <= 384, len(tok(ch[0])["input_ids"]))

# 3) validate_judgment
mild = ("聯鈞（3450）今日股價小跌1.2%，終場收在443.5元。法人指出，近期光通訊族群漲多後出現獲利了結，屬正常整理，"
        "公司基本面與訂單能見度並未改變，第四季營收仍可望維持高檔。")
r, s = news_store.validate_judgment({"sentiment": "中性", "event": "價格", "magnitude": 1, "title_exaggerated": True,
                                     "reason": "x", "quote": "屬正常整理，公司基本面與訂單能見度並未改變"}, mild)
check("quote 出自內文 → ok", s == "ok" and r["exaggerated"] is True, s)
r, s = news_store.validate_judgment({"sentiment": "利空", "event": "價格", "magnitude": 3, "title_exaggerated": False,
                                     "reason": "x", "quote": "公司宣布裁員一半並下修全年財測"}, mild)
check("quote 不在內文 → ungrounded", s == "ungrounded", s)
r, s = news_store.validate_judgment({"sentiment": "大好", "quote": "屬正常整理"}, mild)
check("sentiment 不合法 → bad_json", s == "bad_json", s)

# 4) 7B 讀內文（真的叫模型）
con = news_store.connect()
now = now_tpe()
neg = ("聯鈞（3450）今日公告，桃園廠昨晚發生火災，部分產線停工，公司預估影響第四季營收約15%，"
       "復工時間尚無法確定，客戶訂單可能轉單至競爭對手。")
pos = ("聯鈞（3450）公布9月合併營收，年增35%，創同期新高，主要受惠於資料中心光通訊模組出貨增加。"
       "公司表示第四季訂單能見度佳，產能已滿載到明年第一季。")
for i, (t, b) in enumerate([("聯鈞暴跌崩盤！法人狂倒貨 散戶哭慘", mild), ("聯鈞廠區火災停工 估影響營收15%", neg),
                            ("聯鈞9月營收年增35% 創同期新高", pos)], 1):
    con.execute("INSERT INTO news(url,url_hash,title,title_norm,title_hash,published,fetched,feed,publisher,tickers,sentiment,event,"
                "sent_by,body,body_status,body_tickers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"https://example.com/{i}", f"h{i}", t, t, f"t{i}", (now - dt.timedelta(hours=i)).isoformat(timespec="minutes"),
                 now.isoformat(timespec="minutes"), "test", "測試社", json.dumps(["3450"]), "利空" if i < 3 else "中性", "價格",
                 "rule", b, "ok", json.dumps(["3450"])))
con.commit()
st = news_store.classify_bodies(con=con)
print("classify_bodies:", st)
rows = {r["id"]: dict(r) for r in con.execute("SELECT id, title, sentiment, exaggerated, sent_by, sent_reason, sent_quote FROM news")}
for r in rows.values():
    print("  ", r["title"], "→", r["sentiment"], "誇大" if r["exaggerated"] else "", r["sent_by"], r["sent_reason"], "|", r["sent_quote"])
check("聳動標題＋溫和內文：不判利空", rows[1]["sentiment"] != "利空" and rows[1]["sent_by"].endswith("/body"), rows[1])
check("聳動標題＋溫和內文：標出誇大", bool(rows[1]["exaggerated"]), rows[1])
check("火災停工內文 → 利空", rows[2]["sentiment"] == "利空", rows[2])
check("營收創高內文 → 利多", rows[3]["sentiment"] == "利多", rows[3])

# 5) FAISS chunk 檢索
print("embed_chunks:", news_store.embed_chunks(con=con), "faiss:", news_store.build_chunk_faiss(con))
tg = {"symbol": "3450", "name": "聯鈞", "kind": "tw_stock"}
res = news_store.retrieve(tg, k=3, query="聯鈞營收成長", con=con)
for x in res:
    print("  ", x["score"], x["sim"], x["retrieval"], x["title"], "|", x["snippet"][:50])
check("用了 FAISS chunk 檢索", res and res[0]["retrieval"].startswith("faiss_chunks"), res and res[0]["retrieval"])
top = max(res, key=lambda x: x["sim"]) if res else None
check("問營收成長 → 語意最像的是營收創高那篇", top and "年增" in top["snippet"], top and top["title"])

# 6) step13：個股（aspect）判讀：同一篇對 A 好、對 B 壞
multi = ("PCB 與光通訊供應鏈本週消息分歧。聯鈞（3450）宣布接獲美系雲端大廠800G光模組大單，預估明年營收倍增，產能已滿載。"
         "另一方面，華通（2313）則因主要客戶砍單，公司下修第四季營收預估約兩成，並暫停新廠擴產計畫。")
con.execute("INSERT INTO news(url,url_hash,title,title_norm,title_hash,published,fetched,feed,publisher,tickers,sentiment,event,"
            "sent_by,body,body_status,body_tickers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("https://example.com/m", "hm", "供應鏈消息分歧 PCB廠遭砍單", "x", "tm", (now - dt.timedelta(hours=2)).isoformat(timespec="minutes"),
             now.isoformat(timespec="minutes"), "test", "測試社", json.dumps(["3450", "2313"]), "利空", "訂單", "qwen2.5:7b/body", multi, "ok",
             json.dumps(["3450", "2313"])))
con.commit()
news_store.embed_chunks(con=con)
news_store.build_chunk_faiss(con)
st = news_store.classify_tickers(con=con, watch=set())
print("classify_tickers:", st)
ts = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM ticker_sent WHERE news_id=(SELECT id FROM news WHERE url_hash='hm')")}
for t, r in ts.items():
    print("  ", t, r["name"], r["sentiment"], r["status"], r["reason"], "|", r["quote"])
check("多檔文章：聯鈞 → 利多", ts.get("3450", {}).get("sentiment") == "利多", ts.get("3450"))
check("多檔文章：華通 → 利空", ts.get("2313", {}).get("sentiment") == "利空", ts.get("2313"))
check("quote 出自提到該股的段落", all(r["status"] == "ok" for r in ts.values()), ts)
r1 = [x for x in news_store.retrieve({"symbol": "3450", "name": "聯鈞", "kind": "tw_stock"}, k=10, query="聯鈞", con=con) if x["title"].startswith("供應鏈")]
r2 = [x for x in news_store.retrieve({"symbol": "2313", "name": "華通", "kind": "tw_stock"}, k=10, query="華通", con=con) if x["title"].startswith("供應鏈")]
check("retrieve(聯鈞) 用個股判讀 利多", r1 and r1[0]["aspect"] and r1[0]["sentiment"] == "利多", r1)
check("retrieve(華通) 用個股判讀 利空", r2 and r2[0]["aspect"] and r2[0]["sentiment"] == "利空", r2)
check("basis_label 標成針對本股", r1 and news_store.basis_label(r1[0]) == "模型針對本股讀內文判讀")

print(f"\n{ok}/{ok + fail} PASS")
sys.exit(1 if fail else 0)
