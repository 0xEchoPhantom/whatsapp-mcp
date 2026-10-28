#!/bin/bash
# bxv/update.sh — cập nhật WhatsApp MCP lên bản mới nhất trên macOS. Một lệnh, không hỏi gì.
#   git pull --ff-only → install.sh --no-login (uv sync, build bridge, nạp lại launchd) → dừng
#   client MCP Python đang chạy code cũ → in trạng thái.
# Phiên WhatsApp + lịch sử tin giữ nguyên (upstream: update không cần ghép nối lại).
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"; mkdir -p bxv/state
echo "== kéo code =="
git pull --ff-only >bxv/state/update.log 2>&1 || { echo "FAIL git pull (có sửa tay trong repo? xem git status / bxv/state/update.log)"; exit 1; }
echo "OK   $(git log -1 --format='%h %s' | cut -c1-80)"

BUILD_TS=$(date +%s)
bash bxv/install.sh --no-login || exit 1

echo "== dừng client MCP chạy code cũ =="
# MCP stdio = mỗi client Claude/Codex giữ một tiến trình Python riêng với code cũ trong RAM.
KILLED=0
export LC_ALL=C
for pid in $(pgrep -f "$ROOT/whatsapp-mcp-server" 2>/dev/null); do
  started=$(ps -o lstart= -p "$pid" 2>/dev/null) || continue
  started_ts=$(date -j -f "%a %b %e %T %Y" "$(echo "$started" | xargs)" +%s 2>/dev/null || echo 0)
  [ "$started_ts" -lt "$BUILD_TS" ] && kill "$pid" 2>/dev/null && KILLED=$((KILLED+1))
done
[ "$KILLED" -gt 0 ] && echo "OK   dừng $KILLED client cũ (client tự mở lại khi dùng)" || echo "OK   không có client cũ"

echo "== trạng thái =="
STATUS="$(bash bxv/install.sh --status)"; echo "$STATUS"
echo "$STATUS" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["bridge_health"] else 1)' \
  && echo "OK   bridge mới đang chạy" || { echo "FAIL bridge không trả lời sau update — xem ~/Library/Logs/whatsapp-mcp/bridge.err.log"; exit 1; }
echo "INFO /mcp reconnect whatsapp ở các phiên Claude đang mở."
