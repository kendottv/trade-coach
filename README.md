# trade-coach

個人交易教練：用對話記錄每天的買賣，結合台股/美股盤勢與新聞熱度，檢討交易策略。
（跟 `D:\trade-rag` 完全分開。）

## 目前進度：Step 1 行情工具

| 檔案 | 內容 | 來源 |
|---|---|---|
| `tools/tw_market.py` | 加權指數、個股快照（價格、漲跌、成交量） | 永豐 Shioaji（simulation，只看報價不下單）；指數失敗時改用證交所 FMTQIK |
| `tools/tw_futures.py` | 台指期近月：一般盤、盤後（夜盤）收盤與漲跌、成交量；臺股期貨三大法人未平倉 | 盤中永豐 Shioaji 即時（TXFR1）；其他時間期交所每日行情 |
| `tools/twse_inst.py` | 三大法人買賣超、外資個股買賣超排行 | 證交所 BFI82U / TWT38U |
| `tools/us_market.py` | 道瓊、那斯達克、S&P 500、費半 | yfinance |
| `tools/news.py` | 今日財經新聞、熱門關鍵字與個股、持股被提到的新聞 | Yahoo 股市 RSS、Google News RSS、鉅亨網 |

每個函式都回傳 dict，之後直接當 LLM 工具呼叫。每個結果都有：
- `source`：`shioaji_live`（盤中即時）、`twse_daily` / `taifex_daily`（每日公開資料）、`yfinance_live` / `yfinance_daily`、`rss_live`
- `as_of`：抓取時間（台北時間）；即時報價另有 `quote_time`

即時/每日自動切換：
- 台股（09:00–13:30）、台指期日盤（08:45–13:45）、夜盤（15:00–05:00）用 Shioaji 即時，其他時間用公開每日資料。可用 `force="live"` / `force="daily"` 強制。
- 國定假日不判斷，看 `quote_time` 就知道是不是舊資料。
- 期交所把夜盤記在「下一個交易日」：10/16（五）15:00 ~ 10/17（六）05:00 的夜盤，資料日期是 10/19。`tw_futures` 會把夜盤對回它接著的一般盤（`follows_regular_session`）。
- 新聞每次呼叫都重新抓，不快取；只有上市櫃名單快取 7 天。

## 安裝與測試

```powershell
cd D:\trade-coach
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

.\.venv\Scripts\python.exe tools\tw_market.py
.\.venv\Scripts\python.exe tools\tw_futures.py
.\.venv\Scripts\python.exe tools\twse_inst.py
.\.venv\Scripts\python.exe tools\us_market.py
.\.venv\Scripts\python.exe tools\news.py
```

## 金鑰（自己填）

永豐 Shioaji 金鑰放在本專案的 `.env`（已在 `.gitignore`，不會進 git）：

1. 打開 `D:\trade-coach\.env`（範本見 `.env.example`）
2. 填入 `SHIOAJI_API_KEY=` 和 `SHIOAJI_SECRET_KEY=`（等號後面直接貼，不用引號）
3. 存檔後重新啟動 `run.ps1`

- 程式只在執行時讀 `.env`，不印出、不寫 log；強制 `simulation=True`，沒有下單功能。
- 沒填也能用：盤中即時報價會改用證交所/櫃買/期交所的每日公開資料，結果帶 `warning: "shioaji_not_configured"`。

## Step 2：交易日記

| 檔案 | 內容 |
|---|---|
| `journal/db.py` | SQLite `data/trades.db`：`trades`、`market_daily`、`notes`；新增/查詢/修改/刪除；`snapshot_market()` 呼叫行情工具存當天盤勢 |
| `journal/parse.py` | `parse_trade(text)`：Ollama `qwen2.5:7b`（JSON 模式）把一句話拆成草稿；`confirm_and_save(draft, confirmed=True)` 確認後才存；`add_note(text)` |
| `journal/index.py` | FAISS 檢索日記和交易理由（`paraphrase-multilingual-MiniLM-L12-v2`），`sync()` 增量、`search(query, k)` |
| `tools/stats.py` | `compute_stats()`：勝率、平均賺賠、profit factor、已實現/未實現損益、停損紀律、持有天數、各策略表現 |

規則：
- 所有數字（損益、手續費、勝率）都是程式算，模型只負責解釋。
- 手續費 0.1425%（可在 `journal/db.py` 設折扣）、證交稅 0.3%；整股最低 20 元、零股最低 1 元。
- 賣出沒指定買單時用 FIFO 對應，不支援放空。
- `parse_trade` 會檢查每個數字是否真的出現在原句（含「一張」「兩張」這種中文數量），模型多填的會清掉並警告；股票代號用上市櫃名單對照，不信任模型給的代號。
- 沒講停損/目標價會列在 `missing`，不會補值；缺代號/買賣/價格/股數（`blocking`）不能存。

自測（用 `data/test_trades.db` 假資料，不碰正式庫）：

```powershell
.\.venv\Scripts\python.exe eval\test_journal.py
```

需要 Ollama 與模型：`ollama pull qwen2.5:7b`。

## Step 3/4：對話教練＋網頁介面

```powershell
cd D:\trade-coach
.\run.ps1               # 正式資料庫，開 http://localhost:7870（前景執行）
.\run.ps1 -Test         # 用測試資料庫 data\test_trades.db
.\run.ps1 -Background   # 背景執行，輸出在 data\server.log / data\server.err.log
.\run.ps1 -Stop         # 停掉 7870 上的 trade-coach
```

`run.ps1` 啟動前會檢查 7870：已有 trade-coach 就先停掉舊的（含 venv 啟動器）再啟動，避免兩個伺服器搶同一個埠、回答還是舊程式；`-NoReplace` 改成已有就不啟動。如果是別的程式佔用，會拒絕並請你換 `-Port`。

- `agent.py`：Ollama `qwen2.5:7b` 原生工具呼叫（最多 5 輪），session 內記得對話；回覆經 OpenCC s2twp 轉台灣繁體。也可 `python agent.py` 在終端機聊天。
- `tools/overview.py`：`market_overview()`「今日盤勢」—加權指數、三大法人、台指期日盤/夜盤＋未平倉、美股、新聞熱度，並用程式產生 `summary_lines`。
- 記交易：模型呼叫 `parse_trade` 只產生草稿；按「確認儲存」或回「確認」才寫入（程式判斷，不靠模型）。
- 側欄：今日盤勢（5 分鐘快取，可按重新整理）和持股未實現損益。
- 族群／主題／公司：「美股光通訊跌多慘」「台股光通訊表現」「記憶體股今天怎樣」「CPO 概念股」「輝達」「Coherent」→ `tools\resolve.py` 在查詢當下動態找股票（yfinance 搜尋與產業榜、證交所／櫃買產業別、Google 新聞標題），`tools\sector.py` 由程式算每檔 1／5／20 日漲跌、連漲連跌和等權平均；每檔附上找到它的來源。沒有寫死的族群清單：不認得的主題（例如 重電、機器人、航運、生技、無人機）會在查詢當下用本機模型翻成英文主題詞，再查 yfinance 產業目錄、證交所產業別名稱和新聞標題；找不到就回「查不到」。
- 為什麼漲／跌：「為何聯鈞跌那麼多天」「台積電怎麼了」「為什麼費半一直跌」→ `tools\explain.py` 一次抓走勢、近 7 天新聞（附連結）、個股法人買賣超（證交所 T86／櫃買）、同族群、大盤與美股；事實由程式寫，模型只寫標明是推論的「可能原因」，每點都要引用事實。
- 一句話問好幾題（例如「費半跌了幾天  美股光通訊跌多慘」）會拆開，各自回答。
- 對話紀錄：每輪一行 JSON 寫到 `data\chat_logs\YYYY-MM-DD.jsonl`（台北日期），內容有問題、路由方式（規則／追問／模型／反問）、工具與參數、結果（太長會截斷，保留 source / as_of）、回答、警告、延遲、模型、正式/測試庫。不會寫入任何金鑰（另有遮蔽保險）。測試庫的紀錄在 `data\chat_logs\test\`。設 `TRADE_COACH_CHATLOG=0` 可關閉。
- 對話還原：session（對話、待確認草稿、追問主題）存在 `data\sessions.db`，伺服器重啟後重新整理網頁，先前的對話會回來；右上「新對話」開新的。
- 看紀錄：`.\.venv\Scripts\python.exe eval\review_logs.py`（今天）、`--date 2026-10-18`、`--test`、`--export`（把有警告的輪次輸出到 `eval\candidates\` 當候選評測案例）。在 PowerShell 用 `>` 轉存會亂碼，請改用 `--out 檔名`（寫成 UTF-8）。
- 評測：`.\.venv\Scripts\python.exe eval\run_eval.py`（用測試資料庫；檢查工具選擇、數字是否都來自工具輸出、繁體）。

## 接下來

1. ~~行情工具~~
2. ~~交易日記資料庫（SQLite）＋對話輸入~~
3. ~~統計（勝率、損益、停損紀律）＋日記 RAG 檢討~~
4. ~~UI 與評測集~~

## 決策輔助（適合買／抄底／停損嗎）

- 問「聯鈞適合抄底嗎」「台積電可以加碼嗎」「該停損嗎」「怎麼操作」時，程式先算持倉、均線、RSI、ATR、支撐壓力、量能（張）、法人、大盤／族群、新聞、財報日，再交給 `qwen2.5:14b` 權衡，回覆第一行是「結論傾向」。沒講哪一檔會沿用上一題，否則會反問。
- 第一次用要先下載判斷模型：`ollama pull qwen2.5:14b`（約 9GB）。可用環境變數 `TRADE_COACH_JUDGE_MODEL` 換模型；沒有或失敗時改用程式產生的中性結論。
- 12GB 顯卡放不下 7B＋14B，Ollama 會換模型：問完決策題後第一次換模型約多 30–50 秒；14B 用 `keep_alive=2m`，閒置後自動卸載。
- 登錄已經持有的部位：「我持有聯鈞 10股 成本531 停損483 目標570、640」→ 草稿，按「確認儲存」才會寫進日記。
- 細節見 `docs/HOW_IT_WORKS.md` 2.4。

