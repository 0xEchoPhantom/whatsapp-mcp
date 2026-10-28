#!/bin/bash
# bxv/install.sh — cài (hoặc cài lại) WhatsApp MCP (bản upstream verygoodplugins) trên macOS.
#
# VIẾT CHO AGENT CHẠY (Claude Code / Codex cài hộ người dùng): không hỏi tương tác, in từng
# dòng OK/FAIL/WARN/SKIP/CHO_NGUOI/INFO, exit code nói thật. Là lớp bọc mỏng quanh bộ cài
# launchd có sẵn của upstream (scripts/install-launchd-macos.sh) + đăng ký MCP + ghép nối QR
# khi bridge chạy nền.
#
#   bash bxv/install.sh --owner "Thư"   # cài đầy đủ
#   bash bxv/install.sh --no-login      # (update.sh dùng) không chờ quét QR
#   bash bxv/install.sh --status        # in JSON trạng thái rồi thoát
#   --no-mcp: không đụng cấu hình MCP của Claude/Codex (máy đã có cấu hình riêng, vd máy Vũ)
#
# Việc DUY NHẤT máy không làm thay được: quét QR bằng app WhatsApp trên điện thoại chủ tài
# khoản (Thiết bị đã liên kết → Liên kết thiết bị). Script in "CHO_NGUOI: ..." — agent dừng,
# chuyển lời, rồi chạy --status tới khi connected=true.
#
# Yêu cầu: go (brew install go), uv (brew install uv), git. Tuỳ chọn: ffmpeg (tin thoại).
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BRIDGE_DIR="$ROOT/whatsapp-bridge"
SERVER_DIR="$ROOT/whatsapp-mcp-server"
STATE_DIR="$ROOT/bxv/state"; mkdir -p "$STATE_DIR"
PORT="${WHATSAPP_BRIDGE_PORT:-8080}"
API="http://127.0.0.1:$PORT/api"
TOKEN_FILE="$BRIDGE_DIR/store/.bridge-token"
LABEL="com.whatsapp-mcp.bridge"

OWNER=""; NO_LOGIN=0; STATUS_ONLY=0; NO_MCP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --owner) OWNER="$2"; shift 2 ;;
    --no-login) NO_LOGIN=1; shift ;;
    --status) STATUS_ONLY=1; shift ;;
    --no-mcp) NO_MCP=1; shift ;;
    *) echo "FAIL tham số lạ: $1"; exit 2 ;;
  esac
done

ok()   { echo "OK   $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }
token() { [ -r "$TOKEN_FILE" ] && tr -d '[:space:]' < "$TOKEN_FILE" || echo ""; }
health_json() { local t; t="$(token)"; [ -n "$t" ] || { echo '{}'; return; }; curl -s --max-time 3 -H "Authorization: Bearer $t" "$API/health" 2>/dev/null || echo '{}'; }
health_field() { health_json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null; }

print_status() {
  local h; h="$(health_json)"
  python3 - "$h" "$LABEL" "$BRIDGE_DIR/store/messages.db" <<'PY'
import json, sys, subprocess, sqlite3, os
h = json.loads(sys.argv[1] or "{}")
loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout.find(sys.argv[2]) >= 0
msgs = 0
try:
    if os.path.exists(sys.argv[3]):
        msgs = sqlite3.connect(f"file:{sys.argv[3]}?mode=ro", uri=True).execute("select count(*) from messages").fetchone()[0]
except Exception:
    pass
print(json.dumps({
  "bridge_loaded": loaded,
  "bridge_health": bool(h),
  "connected": bool(h.get("connected")),
  "paired": bool(h.get("paired")),
  "state": h.get("state", ""),
  "messages_cached": msgs,
}, ensure_ascii=False))
PY
}

if [ "$STATUS_ONLY" = "1" ]; then print_status; exit 0; fi

echo "== 1/6 Công cụ =="
command -v go >/dev/null 2>&1 || fail "thiếu go — brew install go"
command -v uv >/dev/null 2>&1 || fail "thiếu uv — brew install uv"
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg có (gửi tin thoại được)" || echo "WARN không có ffmpeg — gửi tin thoại sẽ không chuyển mã (brew install ffmpeg nếu cần)"
ok "go $(go version | awk '{print $3}') · uv $(uv --version | awk '{print $2}')"

echo "== 2/6 MCP server (Python) =="
(cd "$SERVER_DIR" && uv sync >"$STATE_DIR/uv-sync.log" 2>&1) || fail "uv sync lỗi — xem bxv/state/uv-sync.log (Intel Mac: xem README mục cryptography)"
ok "uv sync xong"

echo "== 3/6 Bridge chạy nền (launchd $LABEL, port $PORT) =="
# Tên thiết bị hiện trong WhatsApp > Thiết bị đã liên kết — để người dùng nhận ra agent của mình.
[ -n "$OWNER" ] && export WHATSAPP_DEVICE_NAME="BXV Agent ($OWNER)"
export WHATSAPP_BRIDGE_PORT="$PORT"
zsh scripts/install-launchd-macos.sh >"$STATE_DIR/launchd-install.log" 2>&1 || fail "bộ cài launchd của upstream lỗi — xem bxv/state/launchd-install.log"
for _ in $(seq 1 30); do [ -n "$(token)" ] && [ -n "$(health_field state)" ] && break; sleep 1; done
[ -n "$(health_field state)" ] || fail "bridge không trả /api/health sau 30s — xem ~/Library/Logs/whatsapp-mcp/bridge.err.log"
ok "bridge chạy, state=$(health_field state)"

echo "== 4/6 Đăng ký MCP =="
if [ "$NO_MCP" = "1" ]; then echo "SKIP --no-mcp"
elif command -v claude >/dev/null 2>&1; then
  claude mcp remove -s user whatsapp >/dev/null 2>&1 || true
  claude mcp add -s user whatsapp -- uv --directory "$SERVER_DIR" run main.py >/dev/null 2>&1 && ok "Claude Code user scope" || echo "WARN claude mcp add lỗi"
else echo "SKIP không có lệnh claude"; fi
if [ "$NO_MCP" = "1" ]; then :
elif command -v codex >/dev/null 2>&1; then
  codex mcp remove whatsapp >/dev/null 2>&1 || true
  codex mcp add whatsapp -- uv --directory "$SERVER_DIR" run main.py >/dev/null 2>&1 && ok "Codex" || echo "WARN codex mcp add lỗi — thêm tay vào ~/.codex/config.toml"
else echo "SKIP không có lệnh codex"; fi
echo "INFO Claude Desktop: \"whatsapp\": {\"command\": \"uv\", \"args\": [\"--directory\", \"$SERVER_DIR\", \"run\", \"main.py\"]}"

echo "== 5/6 Ghép nối WhatsApp =="
if [ "$(health_field connected)" = "True" ]; then
  ok "đã liên kết sẵn"
elif [ "$NO_LOGIN" = "1" ]; then
  echo "WARN chưa liên kết — chạy bxv/install.sh (không --no-login) để quét QR"
else
  QR="$STATE_DIR/pair-qr.png"
  GOT=0
  for _ in $(seq 1 20); do
    BRIDGE_TOKEN_FILE="$TOKEN_FILE" WHATSAPP_API_URL="$API" uv run --quiet --with "qrcode[pil]" bxv/qr-png.py "$QR" >"$STATE_DIR/qr.log" 2>&1
    rc=$?
    [ "$rc" = "0" ] && { GOT=1; break; }
    [ "$rc" = "3" ] && { GOT=2; break; }
    sleep 2
  done
  if [ "$GOT" = "2" ]; then ok "đã liên kết sẵn"
  elif [ "$GOT" = "1" ]; then
    open "$QR" 2>/dev/null || true
    echo "CHO_NGUOI: Mở WhatsApp trên điện thoại → Cài đặt → Thiết bị đã liên kết → Liên kết thiết bị → quét ảnh $QR (vừa bật lên). QR hết hạn ~1 phút thì script tự vẽ lại. Chờ tối đa 3 phút."
    for i in $(seq 1 90); do
      [ "$(health_field connected)" = "True" ] && break
      # QR đổi mã theo chu kỳ — vẽ lại mỗi 30s để ảnh đang mở luôn quét được.
      if [ $((i % 15)) = 0 ]; then BRIDGE_TOKEN_FILE="$TOKEN_FILE" WHATSAPP_API_URL="$API" uv run --quiet --with "qrcode[pil]" bxv/qr-png.py "$QR" >/dev/null 2>&1 && open "$QR" 2>/dev/null; fi
      sleep 2
    done
    [ "$(health_field connected)" = "True" ] && ok "liên kết xong" || echo "WARN chưa thấy liên kết — chạy lại bxv/install.sh để lấy QR mới; kiểm: bash bxv/install.sh --status"
  else
    echo "WARN bridge chưa đưa QR (state=$(health_field state)) — xem bxv/state/qr.log, chạy lại sau 10s"
  fi
fi

echo "== 6/6 Trạng thái =="
print_status
echo "INFO tiếp theo: /mcp reconnect whatsapp ở phiên Claude đang mở · update: bash bxv/update.sh · log: ~/Library/Logs/whatsapp-mcp/"
echo "INFO bản upstream KHÔNG có whitelist người nhận — agent gửi được cho bất kỳ ai; chỉ gửi khi người dùng nêu rõ tên/nhóm."
