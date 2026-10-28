# bxv/install.ps1 — cài (hoặc cài lại) WhatsApp MCP (bản upstream) trên Windows.
#
# VIẾT CHO AGENT CHẠY (Claude Code / Codex cài hộ người dùng): không hỏi tương tác, in từng
# dòng OK/FAIL/WARN/SKIP/CHO_NGUOI/INFO, exit code nói thật.
#
#   powershell -ExecutionPolicy Bypass -File bxv\install.ps1 -Owner "Thư"
#   powershell -ExecutionPolicy Bypass -File bxv\install.ps1 -NoLogin     # update.ps1 dùng
#   powershell -ExecutionPolicy Bypass -File bxv\install.ps1 -Status      # JSON trạng thái
#
# Việc DUY NHẤT máy không làm thay được: quét QR bằng app WhatsApp trên điện thoại chủ tài
# khoản. Script in "CHO_NGUOI: ..." — agent chuyển lời, rồi chạy -Status tới khi connected=true.
#
# Yêu cầu: go (winget install GoLang.Go), uv (winget install astral-sh.uv), git.
# Bridge chạy nền bằng Task Scheduler "BXV-WhatsAppBridge" lúc đăng nhập, cửa sổ ẩn, tự chạy lại.
# CHƯA CHẠY THỬ trên máy Windows thật (28/10/2026) — lần đầu nhờ agent soát log.
param(
  [string]$Owner = "",
  [switch]$NoLogin,
  [switch]$Status
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$BridgeDir = Join-Path $Root "whatsapp-bridge"
$ServerDir = Join-Path $Root "whatsapp-mcp-server"
$StateDir = Join-Path $Root "bxv\state"; New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$Port = if ($env:WHATSAPP_BRIDGE_PORT) { $env:WHATSAPP_BRIDGE_PORT } else { "8080" }
$Api = "http://127.0.0.1:$Port/api"
$TokenFile = Join-Path $BridgeDir "store\.bridge-token"
$TaskName = "BXV-WhatsAppBridge"
$Binary = Join-Path $BridgeDir "whatsapp-bridge.exe"

function Ok($m)   { Write-Host "OK   $m" }
function Fail($m) { Write-Host "FAIL $m"; exit 1 }
function Token()  { if (Test-Path $TokenFile) { (Get-Content $TokenFile -Raw).Trim() } else { "" } }
function Health() {
  $t = Token; if (-not $t) { return $null }
  try { Invoke-RestMethod -Uri "$Api/health" -Headers @{ Authorization = "Bearer $t" } -TimeoutSec 3 }
  catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 503) { try { $_.ErrorDetails.Message | ConvertFrom-Json } catch { $null } } else { $null } }
}
function Print-Status {
  $h = Health
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  [ordered]@{
    bridge_loaded = [bool]$task
    bridge_health = [bool]$h
    connected     = [bool]($h -and $h.connected)
    paired        = [bool]($h -and $h.paired)
    state         = $(if ($h) { $h.state } else { "" })
  } | ConvertTo-Json -Compress
}
if ($Status) { Print-Status; exit 0 }

Write-Host "== 1/6 Công cụ =="
if (-not (Get-Command go -ErrorAction SilentlyContinue)) { Fail "thiếu go — winget install GoLang.Go" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "thiếu uv — winget install astral-sh.uv" }
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Ok "ffmpeg có" } else { Write-Host "WARN không có ffmpeg — tin thoại không chuyển mã" }
Ok "go $((& go version) -replace '^go version ','') · uv $(& uv --version)"

Write-Host "== 2/6 MCP server (Python) =="
Push-Location $ServerDir
& uv sync *> (Join-Path $StateDir "uv-sync.log"); $rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Fail "uv sync lỗi — xem bxv\state\uv-sync.log" }
Ok "uv sync xong"

Write-Host "== 3/6 Build bridge + Task Scheduler $TaskName (port $Port) =="
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-Process -Name "whatsapp-bridge" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 1
Push-Location $BridgeDir
& go build -o $Binary . *> (Join-Path $StateDir "go-build.log"); $rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Fail "go build lỗi — xem bxv\state\go-build.log" }
$envPrefix = "set WHATSAPP_BRIDGE_PORT=$Port&& "
if ($Owner) { $envPrefix += "set WHATSAPP_DEVICE_NAME=BXV Agent ($Owner)&& " }
$cmd = "/c $envPrefix cd /d `"$BridgeDir`" && `"$Binary`" >> `"$(Join-Path $StateDir 'bridge-stdout.log')`" 2>> `"$(Join-Path $StateDir 'bridge-stderr.log')`""
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $BridgeDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -Hidden -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$h = $null
for ($i = 0; $i -lt 30 -and -not $h; $i++) { Start-Sleep 1; $h = Health }
if (-not $h) { Fail "bridge không trả /api/health sau 30s — xem bxv\state\bridge-stderr.log" }
Ok "bridge chạy, state=$($h.state)"

Write-Host "== 4/6 Đăng ký MCP =="
if (Get-Command claude -ErrorAction SilentlyContinue) {
  & claude mcp remove -s user whatsapp *> $null
  & claude mcp add -s user whatsapp -- uv --directory $ServerDir run main.py *> $null
  if ($LASTEXITCODE -eq 0) { Ok "Claude Code user scope" } else { Write-Host "WARN claude mcp add lỗi" }
} else { Write-Host "SKIP không có lệnh claude" }
if (Get-Command codex -ErrorAction SilentlyContinue) {
  & codex mcp remove whatsapp *> $null
  & codex mcp add whatsapp -- uv --directory $ServerDir run main.py *> $null
  if ($LASTEXITCODE -eq 0) { Ok "Codex" } else { Write-Host "WARN codex mcp add lỗi — thêm tay vào ~\.codex\config.toml" }
} else { Write-Host "SKIP không có lệnh codex" }
Write-Host "INFO Claude Desktop: `"whatsapp`": {`"command`": `"uv`", `"args`": [`"--directory`", `"$($ServerDir -replace '\\','\\')`", `"run`", `"main.py`"]}"

Write-Host "== 5/6 Ghép nối WhatsApp =="
$Qr = Join-Path $StateDir "pair-qr.png"
function Draw-Qr {
  $env:BRIDGE_TOKEN_FILE = $TokenFile; $env:WHATSAPP_API_URL = $Api
  & uv run --quiet --with "qrcode[pil]" (Join-Path $PSScriptRoot "qr-png.py") $Qr *> (Join-Path $StateDir "qr.log")
  return $LASTEXITCODE
}
if ($h.connected) { Ok "đã liên kết sẵn" }
elseif ($NoLogin) { Write-Host "WARN chưa liên kết — chạy install.ps1 (không -NoLogin) để quét QR" }
else {
  $got = 0
  for ($i = 0; $i -lt 20; $i++) { $rc = Draw-Qr; if ($rc -eq 0) { $got = 1; break }; if ($rc -eq 3) { $got = 2; break }; Start-Sleep 2 }
  if ($got -eq 2) { Ok "đã liên kết sẵn" }
  elseif ($got -eq 1) {
    Start-Process $Qr
    Write-Host "CHO_NGUOI: Mở WhatsApp trên điện thoại → Cài đặt → Thiết bị đã liên kết → Liên kết thiết bị → quét ảnh $Qr (vừa bật lên). QR hết hạn ~1 phút thì script tự vẽ lại. Chờ tối đa 3 phút."
    for ($i = 1; $i -le 90; $i++) {
      $h = Health; if ($h -and $h.connected) { break }
      if ($i % 15 -eq 0) { if ((Draw-Qr) -eq 0) { Start-Process $Qr } }
      Start-Sleep 2
    }
    if ($h -and $h.connected) { Ok "liên kết xong" } else { Write-Host "WARN chưa thấy liên kết — chạy lại install.ps1 để lấy QR mới; kiểm: install.ps1 -Status" }
  } else { Write-Host "WARN bridge chưa đưa QR (state=$($h.state)) — xem bxv\state\qr.log, chạy lại sau 10s" }
}

Write-Host "== 6/6 Trạng thái =="
Print-Status
Write-Host "INFO tiếp theo: /mcp reconnect whatsapp ở phiên Claude đang mở · update: bxv\update.ps1 · log: bxv\state\"
Write-Host "INFO bản upstream KHÔNG có whitelist người nhận — agent gửi được cho bất kỳ ai; chỉ gửi khi người dùng nêu rõ tên/nhóm."
