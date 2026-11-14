# trade-coach 修改紀錄：Step 1（決策紀錄）＋ Step 2（features 純函式）
日期：2026-10-22（台北時間）。依據：`trade-coach-gap.md` 的 P0-A、P0-B。
每個被修改的舊檔都有備份：`*.bak_step1`（Step 1 前）、`*.bak_step2`（Step 2 前）。新增的檔案沒有備份，要還原時刪掉即可。

---
## Step 1（P0-A）：決策紀錄 decision_log

### 為什麼要做
助理現在會說「偏多／偏空」，但**沒有人知道它說得準不準**。chat_logs 雖然有記，但沒有回頭對照之後的股價。
先把每次判斷存下來，再回填之後的報酬，才有辦法衡量之後每一步改動（補指標、加新聞、換模型）是變好還是變壞。

### 1-1. 新檔 `tools/decision_log.py`：存判斷
**做什麼**：建立獨立的 SQLite `data/decision_log.db`（**不是 trades.db**），一個判斷一列。

**存哪些欄位**：
| 欄位 | 內容 |
|---|---|
| `ts`、`session_id`、`question` | 時間（台北 ISO）、對話、原始問題 |
| `kind`、`symbol`、`yf_symbol`、`market`、`name` | stock／sector、代號或族群名、回填用的 yfinance 代號 |
| `verdict`、`confidence`、`judged_by`、`model` | 偏多／中性／偏空、信心、model 或 code_fallback、`qwen2.5:14b` 或 `code` |
| `evidence_ids`、`evidence` | 判斷引用的 E# 清單，以及全部證據原文（JSON） |
| `asof_date`、`intraday`、`price` | 判斷依據的最後一根日 K、是否盤中、當下價格 |
| `support`／`resistance`（＋`_name`）、`levels` | 最近支撐／壓力（排除「你的成本／停損」），以及全部 L# |
| `members` | 族群成員（族群判斷用來算等權報酬） |
| `ret_1d/5d/20d`、`hit_support`、`hit_resistance`、`first_hit`、`mfe_20d`、`mae_20d`、`bars_after`… | 由回填腳本填入 |

**怎麼決定寫到哪個檔**（eval 不會汙染正式紀錄）：
```python
def log_path() -> Path:
    p = os.environ.get("TRADE_COACH_DECISION_LOG")      # 1) 明確指定
    if p:
        return Path(p)
    db = os.environ.get("TRADE_COACH_DB", "")
    if "test" in Path(db).name.lower():                 # 2) 用測試交易庫時 → 測試決策紀錄
        return DATA / "test_decision_log.db"
    return DATA / "decision_log.db"                     # 3) 正式
```
**寫入失敗不影響回覆**：`_insert` 包在 try 裡，失敗只回 None。

函式：`log_stock(b, v, question, session_id)`（個股，b = decision_brief，v = judge 結果）、`log_sector(...)`（族群）。

### 1-2. `agent.py`：在回覆前記錄
**位置**：`agent.py:decision_answer`、`agent.py:sector_answer`，加一個共用的 `_log_decision`。
```python
def _log_decision(kind, b, v, user_text, session, meta):
    try:
        from tools import decision_log
        fn = decision_log.log_stock if kind == "stock" else decision_log.log_sector
        meta["decision_log_id"] = fn(b, v, user_text, getattr(session, "id", None))
    except Exception as e:
        meta["warnings"].append({"decision_log_failed": f"{type(e).__name__}: {e}"})

# decision_answer 裡，render 之前：
    _log_decision("stock", b, v, user_text, session, meta)
    return decision.render(b, v)
```
**為什麼放在這裡**：這是「14B 判斷 → 程式驗證 → 排版」之後唯一的出口，b（證據、價位）和 v（傾向、引用 id）都在手上。
族群候選股（`sector_decision._candidates` 裡的 code_verdict）只是附帶資訊，**不另外記**，免得一題記 5 筆。

### 1-3. 新檔 `tools/decision_backfill.py`：回填結果
**做什麼**：對每一筆，用 yfinance 日 K 從 `asof_date` 往後算：
- T+1／T+5／T+20 報酬（%）
- 20 個交易日內是否碰到支撐（low ≤ support）、壓力（high ≥ resistance），以及哪個先碰到
- 20 日內 MFE／MAE（最大漲幅／最大跌幅）
- 族群：各成員分別算，再取等權平均（全部成員都有資料才算）

**核心是純函式 `path_stats(o, asof, support, resistance)`**，方便測試：
```python
i = o["dates"].index(asof); base = o["close"][i]
for h in (1, 5, 20):
    out[f"ret_{h}d"] = (o["close"][i+h] - base) / base * 100 if after >= h else None
```
**基準價**：用 asof 那天的「最終收盤」，不用判斷當下的盤中價（盤中價另存在 `price`），這樣各筆比較才公平。

**冪等**：每次都從原始 K 線重算同一組欄位並覆寫，不會累加。`bars_after >= 20` 的列視為完成、跳過（`--all` 強制重算）。
台股代號會依序試 `.TW` 和 `.TWO`。

### 1-4. 新檔 `tools/decision_report.py` ＋ 對話指令「命中率」
**做什麼**：依傾向分組，列筆數、T+1／T+5／T+20 平均報酬與方向命中率、先碰支撐或壓力的次數。
命中定義：偏多看漲（>0）、偏空看跌（<0）、中性看 ±2% 以內。

**對話指令**：`agent.py` 加 `HITRATE_RE`，在招呼語判斷之後、模型路由之前處理，純程式、不呼叫模型：
```python
HITRATE_RE = re.compile(r"^\s*(?:/hitrate|/命中率|(?:決策|建議|判斷)?命中率(?:報表|統計)?|(?:你的)?(?:建議|判斷)(?:準不準|準嗎|命中率))\s*[?？]?\s*$", re.I)
if HITRATE_RE.match(user_text or ""):
    meta["route"] = {"mode": "code_hitrate", "intent": "decision_hitrate", "tool": "none"}
    return _finish(decision_report.text())
```
整句比對（^…$），所以「聯鈞命中率高嗎」不會誤觸。

### 1-5. `eval/run_eval.py`：eval 只寫測試紀錄
```python
os.environ["TRADE_COACH_DECISION_LOG"] = str(ROOT / "data" / "test_decision_log.db")
...
assert _dl.log_path().name == "test_decision_log.db"
```
雙保險：就算環境變數被改掉，assert 也會擋住。

### 怎麼驗證 Step 1
```powershell
cd D:\trade-coach
# 1. 問一題決策，看紀錄
#    UI 問「聯鈞現在適合抄底嗎」，然後：
python -c "import sqlite3;print(sqlite3.connect('data/decision_log.db').execute('select ts,symbol,verdict,confidence,model,price,support,resistance from decision_log order by id desc limit 3').fetchall())"
# 2. 回填（可以重複跑，結果一樣）
python tools\decision_backfill.py
# 3. 報表
python tools\decision_report.py        # 或在 UI 打「命中率」
# eval 用的是 --test：
python tools\decision_report.py --test
```

---
## Step 2（P0-B，只重構）：`features(ohlcv, asof)` 純函式

### 為什麼要做
回測時要問「2026-09-15 那天程式會怎麼判斷」，就得只用那天以前的 K 線。
原本的 `technicals(o, tw)` 一律拿整份資料的最後一根當今天，沒辦法指定日期；而且它和 `_ohlcv`（連網抓資料）分開得不夠清楚。
這一步**只改結構、不改任何數值**，新指標留到下一步。

### 2-1. `tools/decision.py:slice_asof`、`features`、`technicals`
```python
def slice_asof(ohlcv, asof):
    if asof is None:
        return ohlcv
    k = sum(1 for x in ohlcv["dates"] if x <= asof)     # 只留 asof（含）以前
    return {key: list(val[:k]) for key, val in ohlcv.items() if isinstance(val, list)}

def features(ohlcv, asof=None, tw=True) -> dict:
    o = slice_asof(ohlcv, asof)
    ...（原本 technicals 的計算，一行都沒改）

def technicals(o, tw):            # 即時用的舊介面，保留給 decision_brief
    return features(o, None, tw)
```
**純函式的意思**：不連網、不讀檔、不改輸入；同樣輸入永遠得到同樣輸出。所以回測可以抓一次 K 線，再逐日呼叫 `features(o, 某天)`。

**沒有動的部分**：`build_items`、`build_levels` 依賴持倉、籌碼、新聞等非 K 線資料，這次不搬，介面也不變。

### 2-2. 新檔 `eval/test_features.py`：新舊對照＋防偷看
- 從 `tools/decision.py.bak_step2`（重構前備份）載入舊版 `technicals`。
- 5 檔（3450.TW、2330.TW、6617.TWO、NVDA、^SOX）× 5 個日期（倒數第 0/1/5/20/60 根）：
  `舊版(截斷資料) == features(完整資料, asof) == 新版 technicals(截斷資料)`，dict 要完全相等。
- 防偷看未來：把 asof 之後的價格全部乘 3，`features` 結果不能變。
- 也檢查 `features` 沒有改到輸入。

### 怎麼驗證 Step 2
```powershell
python eval\test_features.py      # 應顯示 26/26 passed（需要網路）
python eval\run_eval.py           # 完整 eval
```

---
## 還原方式
```powershell
Copy-Item agent.py.bak_step1 agent.py; Copy-Item eval\run_eval.py.bak_step1 eval\run_eval.py   # 撤銷 Step 1 的掛鉤
Copy-Item tools\decision.py.bak_step2 tools\decision.py                                         # 撤銷 Step 2
# 新檔：tools\decision_log.py、decision_backfill.py、decision_report.py、eval\test_features.py 直接刪除
```

---
## 驗證結果（2026-10-22 台北時間）
| 時間點 | eval | 失敗案例 |
|---|---|---|
| 修改前（上次） | 76/83 | 4、14、24、42、51、54、58 |
| Step 1 後 | 76/83 | 4、11、14、24、42、51、54 |
| Step 2 後 | 77/83 | 14、24、42、44、51、54 |
- 新出現的 11（停損紀律：7B 自己算出 30%）、44（輝達為什麼跌：缺「可能原因」，輝達昨天其實漲了 2.41%）都走 7B 自由回答或新聞路徑，跟決策紀錄、features 無關。
- 一直失敗的 24、42 是因為費半 10/21 上漲，題目預期的「連跌」不成立。
- `eval\test_features.py`：26/26 通過（新舊輸出完全相同，也確認不偷看未來）。
- eval 只寫入 `data\test_decision_log.db`；正式的 `data\decision_log.db` 由 7870 冒煙測試產生第 1 筆（聯鈞，偏空／中，qwen2.5:14b，價格 430.5，支撐 426.5（20 日低點），壓力 459.8（MA5））。

---
# 後續步驟 C、B-ext、D、E、F、G、H、I 與排程（2026-10-22 台北時間）
eval 都只用 test_trades.db；每一步都跑完整 eval，與上一步比較。每個被改的舊檔都有 `*.bak_stepN`（N：3=C、4=B-ext、6=E、7=F、8=G、9=H、10=I）。

| 步驟 | eval | 失敗案例 | 說明 |
|---|---|---|---|
| 起點（Step 2 後） | 77/83 | 14、24、42、44、51、54 | |
| C 回測＋校準 | 77/83 | 14、24、42、44、51、54 | 完全相同 |
| B-ext 新指標 | 76/83 | 4、14、24、42、44、51、54 | 4 是 7B 自己算數字，之前也時好時壞 |
| D 新聞庫 | —（D 不接對話路徑，和 E 一起測） | | |
| E 新聞證據 N# | 76/83 | 同 B-ext | |
| F 只准引用 id、程式組數字 | **78/83** | 24、42、44、51、54 | 4、14 修好 |
| G 別名表 | 78/83 | 同 F | |
| H eval 改用固定資料＋延遲預算 | **80/83** | 44、51、54 | 24、42 修好 |

---
## C. 歷史回測與門檻校準
**檔案**：新增 `eval/backtest.py`、`data/evidence_calibration.json`；改 `tools/decision.py:build_items`（加 `rule_key`、`calibration`、`apply_calibration`）。
**做什麼**：49 檔（台股 40＋美股 10，其中 6442 抓不到資料）、3 年、每 2 個交易日取一次樣，共 16,997 個樣本。每個樣本用 `features(o, asof)`，只看當天以前的 K 線，重算技術證據和 `code_verdict`，再對照之後 T+5／T+20 的報酬。
**怎麼做**：每條證據依文字比對出一個規則代號，例如 `rsi_overbought|偏空`。
```python
for x in items:
    x["key"] = rule_key(x)     # 依文字樣式判斷規則代號
apply_calibration(items)       # data/evidence_calibration.json 的 overrides：{"rule|side": "中性"}
```
判斷規則：一條證據在 T+5 和 T+20 的超額報酬，方向都跟它標的多空相反，就**改標中性**。文字保留，但不當多空理由。
**結果**（詳見 `docs/backtest_C.md`）：

| 階段 | 偏多 T+20 | 偏空 T+20 | 差距 |
|---|---|---|---|
| 原始 | +4.16% | +2.94% | 1.22 |
| 校準後 | +5.64% | +2.70% | **2.94** |

- 被改成中性的：RSI>70 過熱（之後反而續漲 +6.4%）、連跌、靠近支撐。
- 門檻掃描：RSI 超賣用 30 合理（25 更強但樣本只有 152）；量比 1.5／0.7 保留。
- 限制：只能測技術面（籌碼、新聞沒有歷史資料）；樣本是現存公司（倖存者偏差）；價格沒有還原權息；這三年整體是多頭。
**為什麼**：原本的門檻都是寫死的，沒有用資料驗證過。
**驗證**：`python eval\backtest.py --years 3 --step 2`（約 75 秒）。
**還原**：`tools\decision.py.bak_step3`，並刪除 `data\evidence_calibration.json`。

## B-ext. 新技術指標與 K 棒型態
**檔案**：新增 `tools/features_ext.py`、`eval/test_features_ext.py`；改 `tools/decision.py`（`features()` 多一個 `ext` 欄位、`build_items` 加入延伸證據、`build_levels` 加前波高低點）；`eval/test_features.py` 改成只比對舊欄位。
**內容**（全部是純函式，只吃已經截到 asof 的 K 線）：
- MACD(12,26,9)、KD(9,3,3)、布林(20,2)、乖離率 5／20／60、OBV 與 RSI 背離
- K 棒型態：十字、錘子、流星、多空吞噬、跳空缺口、長上下影線（盤中不判斷）
- 擺盪點（左右各 3 根）、20 日箱型、週 K（依 ISO 週合併）
```python
"ext": features_ext.ext(o),          # 在 features() 裡
for cat, side, text, key in features_ext.ext_items(t):
    items.append({"cat": cat, "side": side, "text": text, "key": key})
```
**回測與校準**：
- 有效：負乖離 >10%、向上跳空、箱型跌破、擺盪點結構、日週同向偏多。
- 無效、改標中性：MACD 交叉與柱狀、KD 一般交叉、OBV／RSI 背離、吞噬、錘子、流星、向下跳空、正乖離過大、箱型突破、週線單獨方向。
- 校準後偏多和偏空的 T+20 差距是 1.69%（偏多 +4.79%、偏空 +3.10%）。比只用原本證據時的 2.94% 小，因為證據變多後 `code_verdict` 的 ±3 門檻比較容易被觸發。之後可以考慮調高門檻或改成加權。
- 已修一個 bug：擺盪低點墊高要求現價沒跌破最近的低點。
**驗證**：
- `python eval\test_features_ext.py`：16/16（含不偷看未來）
- `python eval\test_features.py`：26/26（舊欄位不變）
**還原**：`tools\decision.py.bak_step4`、`eval\test_features.py.bak_step4`、`data\evidence_calibration.json.bak_step4`。

## D. 新聞庫
**檔案**：新增 `tools/news_store.py`；資料存在 `data/news.db`、`data/news.faiss`。eval 時寫 `data/test_news.db`。
**做什麼**：
- 來源：原有的 Yahoo RSS、鉅亨、Google News，加上最近 30 天決策紀錄裡問過的股票。
- 三層去重：
  1. 網址去掉追蹤參數後的 hash
  2. **完整**標題正規化後的 hash（原本只取前 40 字）
  3. 字元 3-gram Jaccard ≥0.8，有 embedding 時再加 cosine ≥0.9
- 標註股票：台股用 `resolve._tw_codes_in`，指數和美股用別名表。
- MiniLM embedding 存在 BLOB 欄位，另建 FAISS 索引。
- 情緒和事件：先用關鍵字規則標，再由 `classify_pending()` 用 7B（`format=json`）批次分類，結果存回資料庫，不重算。
**實測**：
- 第一次抓 334 則：URL 重複 1、標題重複 7、近似重複 5，embedding 334 筆。
- 第二次跑新增 0 則，證明可重複執行。
- 7B 分類 40 則花 38 秒（只在排程跑，對話時不叫模型）。
**驗證**：
```
.venv\Scripts\python.exe tools\news_store.py ingest --classify
.venv\Scripts\python.exe tools\news_store.py stats
.venv\Scripts\python.exe tools\news_store.py search 台積電
```

## E. 時間衰減檢索與新聞證據 N#
**檔案**：`tools/news_store.py:retrieve / news_evidence`、`tools/decision.py:decision_brief / build_items`。
**做法**：
```python
score = sim * exp(-Δh/48) * (1.0 if 標註命中 else 0.6)    # sim 有 embedding 且模型已載入才算，否則 1
```
- 取前 5 則做成 N1…N5，每則有日期、來源、事件類型、情緒。
- 利多算偏多、利空算偏空，取代原本一律「中性」的那一條新聞證據。
- 對話時會先把這次即時抓到的新聞存進庫（不算 embedding，避免拖慢）。
**為什麼**：原本 14B 只看得到一則標題，而且一律標中性，新聞幾乎沒有進入判斷。
**驗證**：`decision_brief('台積電')` 的新聞證據顯示為 N1–N5。
**還原**：`tools\decision.py.bak_step6`。

## F. 只准引用 id、數字由程式組、降低延遲
**檔案**：`tools/decision.py`（JUDGE_SYSTEM、`judge.ok_text`、`JUDGE_KEEP_ALIVE`）、`agent.py`（`market_answer` 加 `get_us_indices`、新增 `drop_unverified_sentences`）。
- judge 的 prompt 改成 bullish／bearish 只能用 E#／N#。summary、watch、rules_check 裡如果出現不存在的 E／N／L 代號，整段丟掉。
- 「美股今天如何」改由程式排版：名稱來自別名，數字直接抄工具，不再經過 7B，「貴銅指數」這類錯誤不會再出現。
- 自由回答裡含未驗證數字的句子**整句拿掉**，並註明拿掉了幾句。全部都被拿掉時才退回原本的加註。
```python
kept, dropped = drop_unverified_sentences(reply, bad)
```
- 14B 的 `keep_alive` 從 2 分改成 15 分。7B 和 14B 擠不進同一張 12GB 顯卡，這個改動只能讓**連續問決策題**時不用每題重新載入。換模型本身的載入時間還在（實測見文末）。單一模型的方案要靠回測加 eval 比較品質，這次沒做。
**結果**：案例 4、14 通過（eval 78/83）。
**還原**：`tools\decision.py.bak_step7`、`agent.py.bak_step7`。

## G. 別名表資料化
**檔案**：新增 `data/aliases.json`、`tools/aliases.py`；改 `tools/resolve.py:llm_translate`、`agent.py:_finish`。
- 指數、美股代號、族群英文名、常見錯名修正，都集中在 JSON。
- `llm_translate` 先查表，查不到才叫 7B。翻譯是空的就不快取（原本會快取 30 天）。有結果的寫進 `data/alias_suggestions.json` 等人工審核，不會自動進表。
- 清除有問題的翻譯快取：已經在表裡的 5 個詞（無人機、機器人、航運、生技、重電）移到 `data/resolve_cache_purged_step8/`，可以搬回去。
- 模型回覆送出前，照 `name_fixes` 修正錯名（例如 貴銅指數→費城半導體指數）。
**還原**：`tools\resolve.py.bak_step8`、`agent.py.bak_step8`，並刪除 `tools\aliases.py`。

## H. eval 擴充
**檔案**：`eval/run_eval.py`、新增 `eval/test_decision_quality.py`、`data/decision_golden.json`。
- 案例 24、42、44 改用 `mock.closes`：固定的「最後 4 天連跌」序列（同時 patch `history._yf_closes` 和 `sector._closes_batch`），題目前提不再依賴當天行情。
- 延遲預算 `latency_ok`：招呼語 <1 秒，決策 <30 秒。
- 判斷品質離線評測：10 檔、2 年、每 5 日取樣。要求偏多的 T+20 平均報酬高於偏空：實測偏多 5.94%、偏空 4.48%，PASS。另外記錄 24 個固定歷史日期的傾向快照，之後傾向有變動會列出來。
**結果**：80/83。
- 剩下的 51（輝達的情境價位不在 levels 裡）、54（光通訊成員混進大立光／穩懋，是名稱解析的問題）都是原本就有的失敗。
- 44 這次的 `content_ok` 沒過，是 7B 沒寫出「可能原因」那一段；用 mock 單獨測時連跌前提是對的。
**還原**：`eval\run_eval.py.bak_step9`。

## 排程（Windows 工作排程器）
- `tools\daily_job.cmd`：只讀市場資料，不下單，log 寫在 `data\logs\`。
- 兩個工作：
  - `trade-coach\news_ingest`：每天 14:30，新聞抓取加 7B 分類
  - `trade-coach\decision_backfill`：每天 14:45，回填加報表
- 查看：`schtasks /Query /TN "trade-coach\news_ingest"`
- 移除：`schtasks /Delete /TN "trade-coach\news_ingest" /F`

  - `trade-coach\news_ingest`：每天 14:30，新聞抓取加 7B 分類
  - `trade-coach\decision_backfill`：每天 14:45，回填加報表
- 查看：`schtasks /Query /TN "trade-coach\news_ingest"`
- 移除：`schtasks /Delete /TN "trade-coach\news_ingest" /F`

## Step I — 多時間框架證據 + K 線圖
- 做什麼：tools/chart.py（matplotlib K 線、MA20/60、L# 價位線，存 data/charts）；decision 快取 LAST_OHLCV；agent 決策回覆附 res["chart"]；app.py /api/chat 回傳 chart、GET /charts/<png>、前端顯示圖。多時框 mtf_agree/mtf_conflict 已在 B-ext 的 features_ext。
- 驗證：eval 81/83（僅 51 levels_ok、54 光通訊成員 為既有問題）；GET 圖檔 200。
- 回滾：agent.py / app.py / tools/decision.py 的 .bak_step10 蓋回，刪 tools/chart.py。

## 煙霧測試（7870 重啟後）
你好 0.3s｜聯鈞抄底 15.2s（偏空，附圖）｜光通訊 0.8s｜美股 1.6s（無貴銅）｜共信-KY 1.5s｜命中率 0.1s｜連續第二個決策題（輝達）11.9s（judge keep_alive 15m，免重載）


## Step 11 — 對話 log 問題修正（新聞利多利空路由、追問、能力問題、7B 常駐、localhost）
**起因**：11:42 的「聯鈞最近是利空還是利多」被 HIST_RE 的「最近」帶去 tw_history（只回走勢）；追問「有利空消息嗎」沒有主體，掉到 7B 空手回答（說錯「未平倉口數」，load_s 39.1、共 57.9s）；「你可以幫我做什麼」不在閒聊正則裡；用 localhost 連線會多等約 2 秒。

### 做了什麼／怎麼做
1. **agent.py route()**：走勢分支的排除字加上 `利空|利多|利好|好消息|壞消息`，不再被「最近」搶走。
2. **agent.py 新增 `news_lean_target()`、`news_lean()`，以及 chat() 的 0c 步驟**（在命中率之後、草稿確認之前）：
   - 觸發條件：句子有 `利空|利多|利好|好消息|壞消息|消息|新聞`，同時有股票（台股或美股個股／指數，不含大盤），而且不是決策題或「為什麼漲跌」。
   - 追問：沒寫股票、但是短句或追問語氣（≤8 字、「有…」「那…」開頭、「…呢」結尾），就沿用 `session.topic` 的上一題主體。
   - 回答：`news_store.retrieve()` 時間衰減取前 5 則，編號 N1…N5，每則附日期、媒體、事件、情緒、判讀者。新聞庫不足 3 則時補抓 Google 新聞存庫再查。
   - 整體傾向由程式計算：利多、利空分數加總，一方 ≥1.5 倍算偏利多或偏利空，否則算多空互見；全部中性就算中性。
   - 不叫模型，不寫 decision_log。route 記成 `{"mode":"code_news","intent":"news_lean","follow_up":…}`。
   ```python
   if pos >= 1.5 * neg: lean = "偏利多"
   elif neg >= 1.5 * pos: lean = "偏利空"
   else: lean = "多空互見"
   ```
3. **CHITCHAT_RE**：加上 `你(可以|能|會)(幫我|替我|幫忙)?(做|幹)?(什麼|啥|哪些事)`、`你有(什麼|哪些)功能`。
4. **tools/_common.py ollama_chat**：請求沒帶 keep_alive 時預設 `TRADE_COACH_KEEP_ALIVE=30m`（7B 用），judge 仍帶 15m。
   - **顯示卡記憶體實測**（12 GB 卡）：7B 單獨 5.7 GB；載入 14B（8K context）後 `ollama ps` 只剩 14B（10 GB），7B 被擠掉。兩個放不下。
   - **取捨**：keep_alive 只管「閒置多久卸載」，記憶體不夠時 Ollama 一定會換模型，所以換模型的成本只出現在決策題前後。日常對話主要用 7B，給它 30m，就不會像 11:43 那樣閒置後要重載 39 秒。
   - 14B 維持 15m，讓連續幾題決策不用重載。不改成只用單一模型。
5. **app.py**：預設 host 127.0.0.1 時，另開一條執行緒同時聽 `::1`（仍只有本機能連），localhost 不再先試 IPv6 失敗再退回。
6. **tools/resolve.py resolve_company**：中文公司名先查 aliases.json（例如輝達→NVDA），只有一個候選時用 yfinance 確認。
   - 原因：eval 第 31、44 題重現失敗，原本只靠當天 Google 新聞標題剛好有「輝達(NVDA)」，今天的標題沒有。
7. **eval**：新增 4 題（74：利空利多、追問；75：你可以幫我做什麼、你有哪些功能）。第 14 題「聯鈞最近有新聞嗎」的期望工具加上 news_lean。

### 驗證
- eval：**87/87**（加題前 81/83；第一次跑 84/87，第 4 題 7B 數字波動，第 31、44 題是輝達解析問題，修第 6 點後全過）。
- 7870 重啟：127.0.0.1 和 ::1 都在聽。同一個 session 的實測：

  | 問題 | 耗時 | 結果 |
  |---|---|---|
  | 聯鈞最近是利空還是利多 | 0.14s | code_news，「利多 1、利空 1、中性 3 → 多空互見」 |
  | 有利空消息嗎 | 0.08s | follow_up=true，沿用聯鈞 |
  | 你可以幫我做什麼 | 0.32s | 閒聊回覆 |
  | 你好 | 0.09s | 閒聊回覆 |
  | localhost 問你好 | 0.08s | 原本 2.2–2.6s |

  - 注意：API 的 session 欄位是 `session_id`。用錯欄位會每題開新 session，追問就接不上。

### 回滾
把 `agent.py`、`app.py`、`tools/_common.py`、`tools/resolve.py`、`eval/run_eval.py` 的 `.bak_step11` 蓋回，再執行 `.\run.ps1 -Background`。


## Step 12 — 新聞改看「內文」：抓全文、切塊、FAISS chunk 檢索、7B 讀內文判讀、標出誇大標題
**為什麼**：新聞標題常常是釣魚標題（「暴跌崩盤！」，內文其實只跌 1.2%、屬正常整理）。只看標題判讀利多利空，會被用詞帶著走。所以改成：抓內文 → 切塊 → 用問題去找最相關的段落 → 7B 讀內文判讀，並比對標題有沒有誇大。

### 1. 抓內文（tools/news_store.py：`resolve_url` / `fetch_body` / `fetch_bodies`）
- Google News 的 `news.google.com/rss/articles/…` 是加密轉址，直接 GET 拿不到文章。先用 `googlenewsdecoder` 解回原始網址，再抓。
- 正文用 `trafilatura.extract(r.content, favor_precision=True)` 抽，會去掉選單、廣告、留言。傳 bytes 讓它自己判斷編碼，避免亂碼。
- **禮貌限速**：同一個來源網站一條執行緒、兩次請求至少隔 1 秒；不同網站平行。逾時設 (連線 5s, 讀取 12s)。
- **失敗處理**：狀態寫進 `body_status`，判讀退回看標題或關鍵字，回覆裡標「關鍵字判讀（標題）」。
  - `ok`：抓到正文。
  - `paywall`：抽不到正文、頁面有「訂閱／付費會員」字樣。
  - `short`：抽不到正文（多半是 JS 載入）。
  - `http_403`、`error:Timeout`、`gnews_decode_failed`：連線或解碼失敗。
- 新欄位：`body, body_status, body_url, body_fetched, body_tickers, magnitude, exaggerated, sent_reason, sent_quote`。`connect()` 會自動 `ALTER TABLE` 補上，舊資料庫不用重建。
- 同一時間只讓一個 ingest 跑（`data/news_ingest.lock`），手動回補和 14:30 排程撞在一起時，後到的會直接跳過。

### 2. 切塊（chunking）：`chunk_text(text, size=300, overlap=60)`
```python
sents = re.split(r"(?<=[。！？!?；])|\n+", text)     # 先照句子斷
...
if cur and len(cur) + len(s) > size:
    chunks.append(cur)
    cur = cur[-overlap:] + s                          # 下一塊開頭接上一塊最後 60 字
```
- **為什麼要切**：一篇新聞 1000～3000 字，整篇壓成一個向量，重點會被稀釋，問「營收」時找不到講營收的那段。切成約 300 字的小段，每段一個向量，檢索才能指到「哪一段」。
- **為什麼先斷句再湊**：硬切 300 字會把「營收年增」和「35%」切到兩塊。先照句號湊滿，一句話盡量留在同一塊。
- **為什麼要重疊（overlap）**：跨句的事實（上一句講主體、下一句講數字）剛好落在邊界時，重疊的 60 字讓下一塊也帶著上一句的結尾，不會兩邊都只有半個事實。代價是多存約 20% 的塊。
- **長度和模型上限**：MiniLM 預設只看 128 token，中文約 1 字 1 token，300 字會被截掉後半。所以內文塊 embed 時用 `max_len=384`（測試確認 300 字塊在 384 token 內），其他呼叫維持 128。
- 抓不到內文的新聞用「標題＋摘要」當一塊，標記 `kind='title'`。以後抓到內文，會自動換成內文塊。

### 3. 共用 embedding 模組（新檔 tools/embed.py）
- `journal/index.py` 和 `news_store.py` 原本各自載入一份 MiniLM，現在都走 `embed.encode()`，整個程式只載入一份。測試裡用 `embed.LOADS == 1` 驗證。
- journal 行為不變：一樣是 normalize、float32、128 token。
- `app.py` 啟動時 `embed.warmup_async()` 在背景載入模型，新聞檢索第一題不用等。

### 4. 檢索：真的用 FAISS 搜 chunk（`retrieve`）
```python
cond = tickers LIKE ? OR body_tickers LIKE ? OR title LIKE 名稱     # 1) SQL 先篩候選文章（近 7 天、非重複）
params = faiss.SearchParameters(sel=faiss.IDSelectorBatch(...))       # 2) 只在候選文章的 chunk 裡搜
D, I = idx.search(embed(問題＋股票名稱), ..., params=params)
best[news_id] = 最像的那一塊                                            # 3) 每篇文章取最相關的一段
score = cos × exp(-Δ小時/48) × (標題標註 1.0／只有內文提到 0.8／只有標題名稱 0.6)
```
- 證據 N# 現在帶日期、來源、內文片段（最相關那塊裡提到股票的位置，約 110 字）。
- 沒有模型的環境（eval 用的 miniconda）：sim=1，片段取內文第一次提到名稱的位置。功能還在，只是沒有語意排序。

### 5. 7B 讀內文判讀（`classify_bodies`，只在排程跑，對話時不跑）
- 輸出 JSON：`{sentiment, event, magnitude(1-3), title_exaggerated, reason, quote}`。
- **驗證**：
  - sentiment、event 必須是規定的值；magnitude 限定在 1～3。
  - **quote 必須真的出現在內文裡**（去空白後比對，允許 10 字片段相符）。這樣就能擋掉模型自己編的理由。
  - 驗證失敗會重問一次。還是失敗就不採用，保留關鍵字結果並標記 `rule/body_ungrounded`，以後也不會一直重試。
- 結果快取在 SQLite（`sent_by='qwen2.5:7b/body'`）。抓不到內文的，仍用原本的標題批次判讀（`classify_titles`）。
- 排程：`daily_job.cmd news` 原本就是 `ingest --classify`，現在依序是 抓新聞 → 抓內文 → 切塊 embed → 重建 chunk FAISS → 讀內文判讀 → 標題判讀。

### 6. 回覆顯示
```
- N1 10/22 測試新聞社：「聯鈞暴跌崩盤！法人狂倒貨 散戶哭慘」（價格，中性，模型讀內文判讀）
  內文：「聯鈞（3450）今日股價小跌1.2%……屬正常整理，公司基本面與訂單能見度並未改變……」
  ⚠ 標題比內文誇大：內文只說小跌屬正常整理，基本面未變
```
- 整體傾向改成用「分數 × 影響程度」加權。誇大標題的則數會寫在第二行。
- 決策題的 N# 證據也一起帶內文片段和「⚠ 標題比內文誇大」。

### 驗證
- `eval/test_news_body.py`（.venv，真的叫 7B 和 FAISS）：**17/17**。
  - 切塊：長度、重疊、覆蓋全文。
  - 共用模型只載入一次。
  - quote 驗證。
  - 7B 讀內文：
    - 聳動標題＋溫和內文 → **中性＋標出誇大**。
    - 火災停工 → 利空；營收創高 → 利多。
  - FAISS selector 檢索。
- `eval/run_eval.py`：新增第 76 題（固定新聞庫：釣魚標題＋溫和內文），要求出現「標題比內文誇大」「內文：「」「整體偏利多」。
  - 全部 88 題結果：**87/88**，唯一失敗是第 4 題 7B 寫錯外資金額，以前就時好時壞（B-ext、Step 11 都出現過），和這次改動無關。

### 回滾
- 程式：把 `tools/news_store.py`、`journal/index.py`、`agent.py`、`tools/decision.py`、`app.py`、`eval/run_eval.py` 的 `.bak_step12` 蓋回，刪掉 `tools/embed.py`、`eval/test_news_body.py`。
- 資料：把 `data/news.db.bak_step12` 蓋回，刪掉 `data/news.chunks.faiss`。新欄位留著也不影響舊程式。


## Step 13 — 針對個股的情緒判讀（aspect-based sentiment）
**為什麼**：Step 12 的 7B 判讀的是「整篇文章」。〈SpaceX財報紅火 帶旺台廠類股28強〉整篇是利多，但同一篇也寫「聯鈞遭外資賣超1萬1674張」；一篇講好幾檔的新聞，一個情緒套到每一檔一定會錯。文字探勘把這種問題叫 **aspect-based sentiment（面向情緒）**：情緒要綁在「對誰」上。

### 做了什麼
1. **新表 `ticker_sent`**（主鍵 news_id＋ticker）：存每一個（文章, 股票）配對的 sentiment、event、magnitude、title_exaggerated、reason、quote、status、judged_by。`_migrate` 自動建表。
2. **選配對 `ticker_pairs()`**，優先順序：
   - 關注清單：最近 30 天 decision_log 問過的股票。
   - 標題有標註的股票。
   - 只在內文出現的（預設不跑，只能退回整篇判讀）。
   - 原因：近 7 天 362 篇內文共有 3690 個配對，絕大多數是「28 強名單」這類只被點名的。全部跑約 3.5 小時，不划算。實際只跑前兩類（318 對）。
3. **只給「提到這檔的段落」**（`_chunks_mentioning`）：從 Step 12 的 chunk 裡挑出含名稱或代號的塊，把其他公司的消息擋在外面。這就是 aspect 判讀的關鍵：輸入就只有跟這檔有關的上下文。
4. **prompt `TICKER_PROMPT`**：明講「文中其他公司的好壞消息不算在 {name} 身上」「只被列在名單裡、沒有實質消息給中性」。
5. **驗證**：
   - quote 必須出自「提到這檔的段落」（沿用 `validate_judgment`）。
   - 回傳的 ticker 必須是這一檔（`wrong_ticker` 就重問一次）。
   - 沒有任何段落提到這檔的，標 `no_mention`，不再重試；呼叫出錯的下次排程再試。
6. **使用**：
   - `retrieve()` 會查 `ticker_sent`，有個股判讀就覆蓋整篇判讀（`aspect=True`，保留 `article_sentiment`）。
   - 沒有就退回整篇判讀，多檔文章標「模型讀整篇內文判讀（非針對本股）」。
   - `news_lean`：
     - 顯示「依據（針對聯鈞）：「引文」」。
     - 整篇和個股判讀不同時，註明「整篇文章判讀是利空，但對聯鈞本身是利多」。
     - 在第二行列出「影響整體判斷的：N#（利多，權重 +0.xxx）」，權重＝相似度×時間衰減×股票權重×影響程度，利空為負。
   - 決策的 N# 證據也改成帶個股依據引文。
7. **排程**：`classify_pending` 依序是 讀內文判讀 → **個股判讀（每次最多 300 對）** → 標題判讀，跟著 14:30 的 `daily_job.cmd news` 跑。

### 測試
- `eval/test_news_body.py` 新增多檔文章（同一段裡：「聯鈞接獲 800G 大單…」「華通遭客戶砍單、下修營收兩成」）：
  - 7B 個股判讀：聯鈞 → 利多、華通 → 利空。
  - quote 都出自段落。
  - `retrieve(聯鈞)`、`retrieve(華通)` 各自拿到自己的判讀。
  - 結果 **23/23**。
- `run_eval` 固定新聞庫加入同一篇（整篇判讀故意寫成利空），新增兩題：
  - 第 77 題「聯鈞」：要出現「依據（針對聯鈞）」「對聯鈞本身是利多」「整體偏利多」。
  - 第 78 題「華通」：要出現華通的引文和「整體偏利空」。

### 回滾
- `tools/news_store.py`、`agent.py`、`tools/decision.py`、`eval/run_eval.py`、`eval/test_news_body.py` 的 `.bak_step13` 蓋回。
- `data/news.db.bak_step13` 蓋回；或保留新表，舊程式不讀它，不受影響。


## Step 14 — 個股情緒 prompt 抓重點、標註邊界（長名優先）、段落別名比對

**備份**：`tools\news_store.py`、`tools\resolve.py`、`data\aliases.json`、`eval\run_eval.py`、`docs\CHANGELOG_rag.md`、`data\news.db` → `*.bak_step14`。新增 `eval\test_tagging.py`。

### 1. Prompt：實質事實優先（what / why）
Step 13 的 SpaceX 文章，聯鈞同時出現在「EPS 名單」和「外資賣超 1 萬 1674 張」兩段，7B 抓了名單那句判成中性／利多。
小模型會「抓最先看到的數字」，所以要在 prompt 裡明講優先順序：

```text
判斷優先順序（很重要）：
1. 先找「實質事實」：外資／投信／自營商買超或賣超（張數）、接單／訂單／砍單、營收…、財報、法說、產能、評等調整。
2. 只出現在名單、排行榜、表格裡的數字（例如「EPS 前十名」「供應鏈名單」）是順帶一提，不是實質消息。
3. 段落裡同時有名單和實質事實時，一律依實質事實判斷，quote 也要引用實質事實那句。
4. 外資或投信大幅賣超、被砍單、營收衰退 → 利空；大幅買超、接到新訂單、營收創新高 → 利多。
```
**驗證**：重判聯鈞（3450）2 對，SpaceX 文章 → `利空／籌碼／2`，quote「聯鈞遭外資賣超1萬1674張，及投信賣超4291張…」（逐字出自段落，validate 通過）；申報生效彙總那篇 → 中性（只在名單）。

### 2. 標註邊界：長名優先＋外國名封鎖（tools\resolve.py `_tw_name_re` / `_tw_codes_in`）
問題：台股名稱是用一個大 regex（長名在前）去 `finditer`，「諾基亞」裡沒有任何台股名從「諾」開始，regex 往右一格就命中「基亞」→ 誤標 3176。
作法：把**外國公司名**也放進同一個交替式（alternation），但不對應任何代號。regex 從左掃到「諾」時，最長的「諾基亞」先吃掉整串，「基亞」就沒機會出現；結果集合只收 `_NAME2CODE` 裡有的。

```python
_FOREIGN.update(_alias_list("foreign_names") - set(_NAME2CODE))
# 美股中文名（輝達、特斯拉…）也一起吃掉：「輝達發表」不會切出「達發」
names = sorted(set(_NAME2CODE) | _FOREIGN, key=len, reverse=True)
...
found = {_NAME2CODE[m.group(0)] for m in _tw_name_re().finditer(text) if m.group(0) in _NAME2CODE}
```
封鎖清單放資料檔 `data\aliases.json`：`foreign_names`（諾基亞、應用材料、聯合利華、三星電子…）和 `tag_stop_names`（「材料」太一般，不拿來標 4763 材料-KY；寫成「材料（4763）」仍可標）。實測還順便抓到「輝達發表」→ 達發 6526、「應用材料」→ 4763、「聯合利華」→ 利華 1423 三種誤標。
**資料修正**：重新標註 111 篇受影響新聞，刪掉 6 筆失效的 ticker_sent。
**測試**：`eval\test_tagging.py` 17/17（諾基亞不標基亞、單獨基亞仍標、同句兩者並存只標真的、輝達發表不標達發、Meta 不命中 Metal…）。

### 3. 段落比對用別名（tools\news_store.py `ticker_aliases` / `mentions` / `_chunks_mentioning`）
94 對 no_mention 的主因：`ticker_name("NVDA")` 回傳 "NVDA"（中文名沒查 aliases.json），內文寫「輝達」找不到；TAIEX 內文寫「台股／大盤」；名稱帶 `*`、`-KY`（國巨*、愛普*）。
```python
def ticker_aliases(code, name=None):
    out = [code, name, re.sub(r"[*＊]|-(KY|創)$", "", name)]
    out += us_tickers[code] + indices[code]["aliases"] + tw_short_names[code]   # 都讀 aliases.json
def mentions(text, keys):   # 中文子字串；英文整字（Meta≠Metal）
```
`ticker_name` 也改成先查 aliases.json（NVDA→輝達、TAIEX→加權指數），所以 prompt 裡的名字也對了。`aliases.json` 新增 `tw_short_names`（台積/TSMC、鴻海/Foxconn、TAIEX→台股/大盤、TXF→台指…）。
`classify_tickers(..., pairs=...)` 新參數：可以指定只重判某些配對。
**重跑結果**：原 94 對 → 重新標註後剩 92 對 → 7B 重判 365 秒：ok 80、ungrounded 1、no_mention 11（其中 1 對是「輝達發表→達發」誤標，後來刪掉）→ **成功率 0% → 87.9%（80/91）**。剩下 10 對是內文確實沒提到（影音頁、內文只抓到摘要）。ticker_sent 目前：ok 301、no_mention 10、ungrounded 1。

### 回滾
`Copy-Item tools\news_store.py.bak_step14 tools\news_store.py`（resolve.py、aliases.json、news.db 同理），刪 `eval\test_tagging.py`。


### Eval / 重啟（Step 14）
- 單元測試：eval\test_tagging.py 17/17、eval\test_news_body.py 23/23。
- 完整 eval 沒跑完：跑到 52 題時 44 PASS／8 FAIL，8 題全是決策題 latency_ok=False（14B ReadTimeout 300s；同時 GPU 被 Forza Motorsport 占用，100%、11.7/12GB），內容檢查都通過。與本步改動無關，等 GPU 空出來要重跑。
- 7870 已重啟（run.ps1 -Background），HTTP 200；「聯鈞最近是利空還是利多」→ 整體偏利空，N3 SpaceX 文章：針對聯鈞為利空（外資賣超 1 萬 1674 張）。


## Step 15 — 開放式選股（「有哪檔股票推薦買入」）走程式排序，族群路徑秒拒垃圾字

**問題**：「有哪檔股票推薦買入」命中 `RECO_RE` 的「推薦」→ `_reco_plan` 剝完剩「有哪檔」→ 當成未知主題送 `sector_decision` → 7B 翻譯主題 30.6 秒（其中 28 秒在載模型）→「找不到『有哪檔』這個族群」。總共 33.3 秒，答非所問。
**備份**：`agent.py`、`tools\sector_decision.py`、`eval\run_eval.py`、`docs\CHANGELOG_rag.md` → `*.bak_step15`。新增 `tools\screener.py`、`eval\test_router_screen.py`。

### 1. 路由：先判斷「有沒有主體」（agent.py `SCREEN_RE` / `is_screen`）
```python
def is_screen(t):
    if not SCREEN_RE.search(t) or _DONE_TRADE.search(t) or _TRADE_RE.search(t) or _stocks_in(t):
        return False
    return len(_SCREEN_STRIP.sub("", t).strip()) < 2   # 剝掉選股字和虛字後沒剩主體
```
放在 `route()` 最前面（`is_reco` 之前），`chat()` 另有 0d 段直接處理。「半導體有哪檔可以買」剝完還剩「半導體」→ 不算選股，照舊走族群。
**為什麼用剝字而不是列舉問法**：問法無限多，但「主體」只有兩種（個股、族群）。判斷「剩下的字夠不夠當主體」比列舉所有問法穩。

### 2. 選股器（tools\screener.py）
- 觀察清單＝ `data\watchlist.json` ∪ 日記裡還沒賣完的持股 ∪ 最近 30 天問過決策的股票（最多 10 檔）。
- 每檔跑既有的 `decision_brief`（4 條執行緒並行，不叫模型），分數＝偏多證據數 − 偏空證據數（跌破停損再扣 2），和 `code_verdict` 同一套；同分時個股新聞 N# 淨值高的排前面。
- 回覆列前 3 檔、各附 2 條偏多（E#/N#）＋1 條偏空，最後加免責聲明「僅供參考，不是買進建議，也不會幫你下單」。
- **不寫 decision_log**（只有 `_log_decision` 會寫，選股器沒呼叫）、不下單。清單空的時候說明，並建議加股票或指定族群。

### 3. 族群路徑秒拒垃圾字（tools\sector_decision.py `is_junk_theme`）
`sector_brief` 一開頭先檢查：剝掉標點後 <2 字、或全部由疑問詞／選股字（有、哪、檔、推薦、可以、買、股票…）組成 → 直接回 `found: False, junk: True`，不叫 7B 翻譯。

### 驗證
- `eval\test_router_screen.py` 27/27：7 種選股問法 → `screen_watchlist`（每個 <2 秒）；半導體有哪檔可以買／CPO 推薦哪幾檔 → `sector_decision`；聯鈞適合買嗎 → `decision_brief`；垃圾字判斷；假資料排序 A>C>B；清單空的訊息。
- 7870 實測：「有哪檔股票推薦買入」**33.3 秒 → 5.8 秒**（僅程式），排序 NVDA（-3）、聯鈞（-5），附 E#/N# 與免責聲明。「半導體有哪檔可以買」46.8 秒（`sector_decision` + 14B 判斷，族群路徑照舊）。

### 回滾
把 4 個 `.bak_step15` 複製回去，刪掉 `tools\screener.py`、`eval\test_router_screen.py`。


### Step 15 追加：程式回覆不再做繁簡轉換＋eval 結果
- 完整 eval 第一輪 88/90，第 77 題沒過：fixture 原文是「宣布」，`_finish()` 的 `to_tw()`（OpenCC）把它改成「宣佈」，「消息」改成「訊息」。**引文被改字＝不再是原文**。
- 修正（agent.py `_finish`）：`meta["route"]["mode"]` 是 `code_news` 或 `code_screen` 時跳過 `to_tw`。這兩種回覆都是程式組的，內容是已經是繁體的原文引用。
- 第二輪 eval（test_trades.db，GPU 空閒）：**89/90**。唯一沒過的是第 48 題「光通訊現況」：今天的動態成員名單把穩懋列成「相關性中」，碰到這題的禁止條件。這是即時資料造成的，和本步驟無關（`sector_performance` 沒改）。
- `eval\test_router_screen.py` 27/27。7870 重啟後「有哪檔股票推薦買入」7.9 秒，名稱改用中文（輝達）。
- 注意：煙霧測試「半導體有哪檔可以買」是族群決策題，會照設計寫一筆到**正式**的 decision_log。

