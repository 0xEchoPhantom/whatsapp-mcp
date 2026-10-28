# Shared WhatsApp MCP runtime

Start clients with the existing commands: `uv run main.py` or `python main.py` in
the configured Python environment. No client configuration change is required.
Each stdio client runs a small standard-library relay. Relays using the same
source directory, resolved `WHATSAPP_DB_PATH`, and `WHATSAPP_API_URL` connect to
one backend through a private Unix socket; no network port is opened. Different
WhatsApp configurations use separate backends.

Every connection owns its MCP session, request IDs and initialization state.
`MCP_READ_ONLY=1` applies to that connection: `send_message`, `send_file` and
`send_audio_message` are absent and cannot be called. Other clients retain the
normal tool catalog. Tool names, arguments and results are unchanged.

The invoking client's stderr receives the existing
`[whatsapp-mcp:audit] MCP_TOOL_CALL tool=...` marker, without arguments or message
contents. Its relay acknowledges writing the marker before the backend starts
the tool. Blocking WhatsApp work runs in worker threads so other sessions remain
responsive. A disconnected client's already-started operation stays tracked
until it returns; it is not abandoned or replayed. Existing HTTP/audio operations
have no new timeout, so a permanently blocked operation can keep the single
backend alive. Process termination still cannot guarantee whether an already
started outward operation completed.

Closed stdin, broken output pipes, SIGINT, SIGTERM and parent death stop a relay
and close its session. A silent client or stdin already at EOF does not start a
backend. Without clients or in-flight work, the backend exits after 20 seconds.
Startup waits at most 15 seconds and respects cancellation. Startup and lifetime
locks prevent simultaneous backend creation; stale sockets are replaced only by
the lock owner. Existing processes keep their already-loaded code; this fix is
not retroactive. Reconnecting starts the final runtime, but an old server may
remain hung after its client disconnects. Preserve active clients. Before sending
TERM to a disconnected legacy server, verify its PID, start time, command and
file-descriptor peer state, and confirm no operation is still in flight.

Runtime files live in `/tmp/wa-mcp-<uid>-<configuration-hash>/`, with directory
mode `0700`, socket/lock mode `0600`, and a `backend.pid` file for diagnostics.
For isolated tests, set `WHATSAPP_MCP_RUNTIME_DIR` to an owned, mode-0700 directory
with a short absolute path; do not reuse that override for different WhatsApp
configurations. Never unlink a live socket or signal a PID without checking its
command and runtime ownership. Tool audit evidence goes to each caller's stderr.

Run checks without altering the installed MCP environment:

```sh
uv run --no-sync --with pytest --with pytest-asyncio python -m pytest -q
uv run --no-sync --with ruff ruff check main.py shared_runtime.py tests/test_shared_runtime.py
```

Tests use real initialization and tool listings. Safe error-path calls and
isolated test substitutions verify audit/security/concurrency; they never read
real chats or send WhatsApp messages.

To roll back this installation, restore the pre-change `main.py` and
`pyproject.toml` backups at
`/Users/vupham/Documents/Codex/2026-09-30/whatsapp-mcp-fix/`. Existing live
processes keep their loaded code. Allow the new backend to become idle before
removing its helper; the Go bridge and WhatsApp data need no changes.
