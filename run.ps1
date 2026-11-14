# 啟動 trade-coach 網頁介面：http://localhost:7870
# 用法：.\run.ps1                 正式資料庫 data\trades.db（前景執行，Ctrl+C 結束）
#       .\run.ps1 -Test           測試資料庫 data\test_trades.db
#       .\run.ps1 -Background     背景執行（不開視窗），輸出寫到 data\server.log / data\server.err.log
#       .\run.ps1 -NoReplace      如果 7870 已有 trade-coach 在跑，就不啟動（預設是先停掉舊的再啟動）
#       .\run.ps1 -Stop           只停掉 7870 上的 trade-coach，不啟動
param([switch]$Test, [int]$Port = 7870, [switch]$Background, [switch]$NoReplace, [switch]$Stop)
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
if ($Test) { $env:TRADE_COACH_DB = (Join-Path $PSScriptRoot "data\test_trades.db") } else { Remove-Item Env:TRADE_COACH_DB -ErrorAction SilentlyContinue }

function Get-TradeCoachProcs([int]$P) {
    # 找出佔用埠的程式；是 trade-coach（命令列有 app.py）就連同 venv 的啟動器（父程序）一起列出
    $found = @()
    $listen = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $listen) {
        $pr = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        if (-not $pr) { continue }
        $isTc = $pr.CommandLine -match 'app\.py'
        $found += [pscustomobject]@{ Id = $procId; TradeCoach = $isTc; Cmd = $pr.CommandLine }
        if ($isTc) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($pr.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($parent -and $parent.CommandLine -match 'app\.py') {
                $found += [pscustomobject]@{ Id = $parent.ProcessId; TradeCoach = $true; Cmd = $parent.CommandLine }
            }
        }
    }
    return $found
}

$existing = @(Get-TradeCoachProcs $Port)
if ($existing.Count -gt 0) {
    $other = @($existing | Where-Object { -not $_.TradeCoach })
    if ($other.Count -gt 0) {
        Write-Host "埠 $Port 被其他程式佔用（不是 trade-coach）：PID $($other.Id -join ', ')。不會動它，請換 -Port 或先關掉它。" -ForegroundColor Red
        exit 1
    }
    if ($NoReplace -and -not $Stop) {
        Write-Host "埠 $Port 已有 trade-coach 在跑（PID $($existing.Id -join ', ')），-NoReplace：不啟動新的。" -ForegroundColor Yellow
        exit 0
    }
    Write-Host "停掉埠 $Port 上舊的 trade-coach（PID $($existing.Id -join ', ')）..."
    $existing | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20 -and @(Get-TradeCoachProcs $Port).Count -gt 0; $i++) { Start-Sleep -Milliseconds 300 }
    if (@(Get-TradeCoachProcs $Port).Count -gt 0) { Write-Host "舊的 trade-coach 停不掉，請手動處理。" -ForegroundColor Red; exit 1 }
}
if ($Stop) { Write-Host "已停止（埠 $Port 沒有 trade-coach 在跑）。"; exit 0 }

# 確認 Ollama 在跑
try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 | Out-Null } catch { Write-Host "啟動 Ollama..."; Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 3 }

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($Background) {
    $p = Start-Process -FilePath $py -ArgumentList "app.py", "--port", "$Port" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $PSScriptRoot "data\server.log") -RedirectStandardError (Join-Path $PSScriptRoot "data\server.err.log") -PassThru
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2; $ok = $true; break } catch {}
        if ($p.HasExited) { break }
    }
    if ($ok) { Write-Host "trade-coach 已在背景啟動：http://localhost:$Port  (PID $($h.pid)，啟動器 $($p.Id)，db $($h.db))" }
    else { Write-Host "啟動失敗，請看 data\server.err.log" -ForegroundColor Red; exit 1 }
} else {
    & $py app.py --port $Port
}
