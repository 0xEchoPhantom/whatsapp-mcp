#!/usr/bin/env python3
"""qr-png.py — lấy mã QR ghép nối từ bridge đang chạy NỀN và vẽ ra ảnh PNG.

Bridge upstream chỉ in QR ra terminal khi chạy foreground; chạy nền (launchd / Task
Scheduler) thì QR chỉ có qua GET /api/connection (trường qrCode). Script này gọi
endpoint đó, vẽ PNG để người dùng quét bằng điện thoại.

Chạy qua uv để khỏi cài gì thêm:  uv run --with "qrcode[pil]" bxv/qr-png.py <out.png>
Env: WHATSAPP_API_URL (mặc định http://127.0.0.1:8080/api), WHATSAPP_BRIDGE_TOKEN hoặc
     đọc <bridge-dir>/store/.bridge-token qua BRIDGE_TOKEN_FILE.
Exit: 0 = đã ghi PNG · 3 = đã kết nối, không cần QR · 4 = bridge chưa có QR (thử lại) · 1 = lỗi.
"""
import json
import os
import sys
import urllib.request

out = sys.argv[1] if len(sys.argv) > 1 else "pair-qr.png"
api = os.environ.get("WHATSAPP_API_URL", "http://127.0.0.1:8080/api").rstrip("/")
token = os.environ.get("WHATSAPP_BRIDGE_TOKEN", "").strip()
if not token:
    tf = os.environ.get("BRIDGE_TOKEN_FILE", "")
    if tf and os.path.exists(tf):
        token = open(tf, encoding="utf-8").read().strip()
if not token:
    print("FAIL không có token bridge (WHATSAPP_BRIDGE_TOKEN hoặc store/.bridge-token)", file=sys.stderr)
    sys.exit(1)

req = urllib.request.Request(f"{api}/connection", headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        d = json.load(r)
except Exception as e:  # noqa: BLE001
    print(f"FAIL không gọi được {api}/connection: {e}", file=sys.stderr)
    sys.exit(1)

if d.get("connected"):
    print("CONNECTED")
    sys.exit(3)
code = d.get("qrCode")
if not code:
    print(f"NO_QR state={d.get('state')} message={d.get('message')}")
    sys.exit(4)

import qrcode  # noqa: E402  (có nhờ `uv run --with qrcode[pil]`)

qrcode.make(code).save(out)
print(f"QR_WRITTEN {out} expires={d.get('qrExpiresAt')}")
