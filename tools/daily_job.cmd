@echo off
rem ?????Windows ???????????????????????daily_job.cmd news ^| backfill
cd /d D:\trade-coach
set PYTHONIOENCODING=utf-8
if "%1"=="news" (
  echo ==== %date% %time% news ingest >> data\logs\news_ingest.log
  .venv\Scripts\python.exe tools\news_store.py ingest --classify --hours 48 >> data\logs\news_ingest.log 2>&1
)
if "%1"=="backfill" (
  echo ==== %date% %time% decision backfill >> data\logs\decision_backfill.log
  .venv\Scripts\python.exe tools\decision_backfill.py >> data\logs\decision_backfill.log 2>&1
  .venv\Scripts\python.exe tools\decision_report.py >> data\logs\decision_backfill.log 2>&1
)
