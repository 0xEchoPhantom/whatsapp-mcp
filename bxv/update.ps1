# bxv/update.ps1 — cập nhật WhatsApp MCP lên bản mới nhất trên Windows. Một lệnh, không hỏi gì.
#   git pull --ff-only → install.ps1 -NoLogin (uv sync, build bridge, nạp lại task) → dừng client
#   MCP Python chạy code cũ → in trạng thái. Phiên WhatsApp + lịch sử giữ nguyên.
# CHƯA CHẠY THỬ trên máy Windows thật (28/10/2026).
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
New-Item -ItemType Directory -Force -Path (Join-Path $Root "bxv\state") | Out-Null

Write-Host "== kéo code =="
& git pull --ff-only *> (Join-Path $Root "bxv\state\update.log")
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL git pull (có sửa tay trong repo? xem git status / bxv\state\update.log)"; exit 1 }
Write-Host "OK   $(& git log -1 --format='%h %s')"

$buildTs = Get-Date
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install.ps1") -NoLogin
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== dừng client MCP chạy code cũ =="
$serverDir = Join-Path $Root "whatsapp-mcp-server"
$killed = 0
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -like "*$serverDir*" -and $_.CreationDate -lt $buildTs -and $_.Name -match "python|uv"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++ }
if ($killed -gt 0) { Write-Host "OK   dừng $killed client cũ (client tự mở lại khi dùng)" } else { Write-Host "OK   không có client cũ" }

Write-Host "== trạng thái =="
$status = & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install.ps1") -Status
Write-Host $status
if (($status | ConvertFrom-Json).bridge_health) { Write-Host "OK   bridge mới đang chạy" }
else { Write-Host "FAIL bridge không trả lời sau update — xem bxv\state\bridge-stderr.log"; exit 1 }
Write-Host "INFO /mcp reconnect whatsapp ở các phiên Claude đang mở."
