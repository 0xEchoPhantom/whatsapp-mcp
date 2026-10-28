# Cài WhatsApp MCP cho một người trong BXV — hướng dẫn viết cho AGENT đọc

*28/10/2026. Người cài thường là Claude Code hoặc Codex chạy trên máy của người dùng. Người dùng chỉ phải làm
MỘT việc: quét QR bằng app WhatsApp. Bản này là **upstream verygoodplugins/whatsapp-mcp** nguyên vẹn + thư mục
`bxv/` (bộ cài cho agent). Repo public, không cần tài khoản GitHub để clone.*

## Kết quả cần đạt

Trên máy người dùng: bridge Go chạy nền tự bật khi đăng nhập máy (mac: launchd, Windows: Task Scheduler), MCP
`whatsapp` đăng ký trong Claude Code (và Codex nếu có), WhatsApp của CHÍNH người dùng đã liên kết như một
"thiết bị đã liên kết", agent đọc/tìm tin nhắn được.

## Điều kiện

| Cần | mac | Windows |
|---|---|---|
| Go | `brew install go` | `winget install GoLang.Go` |
| uv (Python) | `brew install uv` | `winget install astral-sh.uv` |
| git | có sẵn | `winget install Git.Git` |
| ffmpeg (tuỳ chọn, tin thoại) | `brew install ffmpeg` | `winget install Gyan.FFmpeg` |

Thiếu công cụ nào thì agent cài công cụ đó trước, rồi chạy lại. Script tự báo `FAIL thiếu go/uv`.

## Các bước (agent chạy, không hỏi người trừ bước 2 và 4)

1. Clone: mac `~/mcp-servers/whatsapp-mcp`, Windows `%USERPROFILE%\mcp-servers\whatsapp-mcp`, từ
   `https://github.com/0xEchoPhantom/whatsapp-mcp` nhánh `main`. Đã có thì `git pull --ff-only`.
   **Không** clone vào thư mục đồng bộ cloud (iCloud/Drive/OneDrive) — phiên WhatsApp sẽ bị sync đi.
2. Hỏi người dùng **một câu**: "Em/anh tên gì?" → `--owner`. Tên này hiện trong WhatsApp → Thiết bị đã liên kết
   là "BXV Agent (<tên>)" để người dùng nhận ra.
3. Chạy một lệnh:
   - mac: `bash bxv/install.sh --owner "<tên>"`
   - Windows: `powershell -ExecutionPolicy Bypass -File bxv\install.ps1 -Owner "<tên>"`
4. Khi thấy dòng `CHO_NGUOI:` → **dừng, chuyển nguyên văn cho người dùng**. Ảnh QR tự bật lên và nằm ở
   `bxv/state/pair-qr.png`; script tự vẽ lại ảnh mỗi 30 giây vì QR WhatsApp hết hạn nhanh.
5. Chạy `bxv/install.sh --status` (Windows `install.ps1 -Status`) tới khi `"connected": true`. Mỗi 20 giây,
   tối đa 3 phút; quá thì chạy lại lệnh cài để lấy QR mới.
6. Bảo người dùng mở phiên Claude mới hoặc gõ `/mcp reconnect whatsapp`. Thử tool `list_chats` — phải trả về
   danh sách chat. Lịch sử đồng bộ dần trong vài phút đầu (WhatsApp gửi ~3 tháng gần nhất).

## Đọc output của script

| Từ khoá | Nghĩa | Agent làm gì |
|---|---|---|
| `OK` | bước xong | đi tiếp |
| `FAIL` | dừng, exit 1 | đọc lý do trên dòng đó, sửa đúng cái đó, chạy lại |
| `WARN` | chưa xong nhưng không chặn | ghi nhận, báo người ở cuối |
| `SKIP` | không áp dụng trên máy này | bỏ qua |
| `CHO_NGUOI` | cần người thật | chuyển lời, chờ |
| `INFO` | gợi ý bước sau | đưa vào báo cáo cuối |

`--status` in một dòng JSON: `bridge_health`, `connected`, `paired`, `state`. Dùng nó để kiểm, đừng đoán từ log.
`state` có thể là `connected · pairing · logged_out · disconnected · outdated · connecting`. `outdated` = phải
update (quét QR không giải quyết được).

## Update sau này

Một lệnh: mac `bash bxv/update.sh`, Windows `bxv\update.ps1`. Kéo code, build lại bridge, nạp lại daemon, dừng
client cũ. **Không** phải quét QR lại, lịch sử giữ nguyên. Exit 0 = xong; exit 1 = đọc dòng FAIL.

## Những thứ agent KHÔNG được làm

- **Bản upstream không có whitelist người nhận**: tool `send_message` gửi được cho bất kỳ ai trong danh bạ.
  Chỉ gửi khi người dùng nêu rõ tên người/nhóm trong câu lệnh, và nhắc lại tên trước khi gửi. Không gửi hàng loạt.
- Không copy thư mục `whatsapp-bridge/store/` từ máy khác (đó là phiên đăng nhập + kho tin của người khác).
- Không đổi port 8080 hay token trong `store/.bridge-token` để "cho chạy được" — báo lỗi về cho Vũ.
- Không xoá `store/whatsapp.db` trừ khi người dùng muốn liên kết lại từ đầu (mất phiên, phải quét QR lại).

## Khác biệt theo hệ

- mac: dùng bộ cài launchd có sẵn của upstream — job `com.whatsapp-mcp.bridge` + monitor 60s; biến môi trường
  ở `~/Library/Application Support/whatsapp-mcp/launchd.env`; log `~/Library/Logs/whatsapp-mcp/`.
- Windows: Task Scheduler `BXV-WhatsAppBridge`, log `bxv/state/`. **Chưa chạy thử trên máy thật (28/10/2026)** —
  lần đầu cài, agent đọc kỹ `bxv/state/bridge-stderr.log` và báo lại.
- Dữ liệu (`store/`) **không mã hoá**: ai đọc được `whatsapp.db` là có phiên WhatsApp. Máy phải có mật khẩu/FileVault.

## Khi có lỗi

- `FAIL uv sync` trên Mac Intel: thiếu toolchain cho `cryptography`, xem README upstream mục Intel macOS.
- Bridge lên nhưng `state=outdated`: chạy `bxv/update.sh` (WhatsApp bắt client mới).
- `connected` mãi false sau khi quét: điện thoại phải có mạng khi quét; thử lại với QR mới.
- Tool trả "Unauthorized": client Python và bridge không cùng token — restart phiên Claude; nếu vẫn, chạy lại
  lệnh cài (bộ cài upstream ghi lại token vào `launchd.env`).
