"""Private shared MCP backend and dependency-free stdio relay.

The local protocol wraps backend messages to deliver audit markers to the caller's
stderr. MCP itself is never multiplexed: each socket owns a complete MCP session.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

START_TIMEOUT = 15.0
IDLE_TIMEOUT = 20.0
MAX_LINE = 16 * 1024 * 1024
BUFFER_LIMIT = 2 * MAX_LINE


def runtime_directory() -> Path:
    """Share only clients using the same source and WhatsApp configuration."""
    source = str(Path(__file__).resolve().parent)
    default_db = Path(source).parent / "whatsapp-bridge" / "store" / "messages.db"
    database = str(Path(os.getenv("WHATSAPP_DB_PATH", str(default_db))).resolve())
    identity = json.dumps([source, database, os.getenv("WHATSAPP_API_URL", "http://localhost:8080/api")])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    default = f"/tmp/wa-mcp-{os.getuid()}-{digest}"
    directory = Path(os.getenv("WHATSAPP_MCP_RUNTIME_DIR", default))
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("Runtime directory must be an owned directory with mode 0700")
    if len(os.fsencode(directory / "server.sock")) >= 104:
        raise RuntimeError("Runtime directory path is too long for a Unix socket")
    return directory


def open_lock(path: Path) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(descriptor)
        raise RuntimeError("Runtime lock must be an owned file with mode 0600")
    return descriptor


def connect_backend(directory: Path) -> socket.socket | None:
    path = directory / "server.sock"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("Backend socket has unsafe ownership, type, or permissions")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.25)
    try:
        client.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
        client.close()
        return None
    except BaseException:
        client.close()
        raise
    client.setblocking(False)
    return client


def ensure_backend(directory: Path, parent: int, cancelled=lambda: False) -> socket.socket:
    connected = connect_backend(directory)
    if connected is not None:
        return connected
    deadline = time.monotonic() + START_TIMEOUT
    lock = open_lock(directory / "startup.lock")
    child = None
    try:
        while True:
            if cancelled() or os.getppid() != parent:
                raise RuntimeError("Client parent exited during startup")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for backend startup")
                time.sleep(0.05)
        connected = connect_backend(directory)
        if connected is not None:
            return connected
        # An owner may already be warming up (socket bound but not listening).
        # Await that instance instead of spawning a duplicate that immediately exits.
        owner = open_lock(directory / "owner.lock")
        existing_owner = False
        try:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                existing_owner = True
        finally:
            os.close(owner)
        if not existing_owner:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--backend"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        while time.monotonic() < deadline:
            if cancelled() or os.getppid() != parent:
                raise RuntimeError("Client parent exited during startup")
            connected = connect_backend(directory)
            if connected is not None:
                return connected
            if child is not None and child.poll() is not None:
                raise RuntimeError("Shared backend exited during startup")
            time.sleep(0.05)
        raise RuntimeError("Shared backend was not ready within the startup timeout")
    except BaseException:
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        os.close(lock)


def relay_main() -> int:
    """Forward stdio and audit envelopes; EOF or parent death closes the session."""
    parent = os.getppid()
    client = None
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    previous_handlers = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    previous_blocking = {}
    try:
        if parent == 1:
            return 0
        # Do not start a backend for an already-closed or silent client. Polling
        # still notices parent death/signals while awaiting its first MCP bytes.
        # macOS kqueue rejects /dev/null and regular-file stdin; select supports
        # both those EOF probes and the usual MCP pipes.
        with selectors.SelectSelector() as preflight:
            preflight.register(0, selectors.EVENT_READ)
            while not stopped and os.getppid() == parent:
                if preflight.select(timeout=0.1):
                    first_input = os.read(0, 65536)
                    if not first_input:
                        return 0
                    break
            else:
                return 0
        client = ensure_backend(runtime_directory(), parent, lambda: stopped)
        to_backend = bytearray(
            (json.dumps({"version": 1, "read_only": os.getenv("MCP_READ_ONLY") == "1"}) + "\n").encode()
        )
        to_backend.extend(first_input)
        from_backend = bytearray()
        to_stdout = bytearray()
        to_stderr = bytearray()
        pending_audits = []
        for descriptor in (0, 1, 2):
            previous_blocking[descriptor] = os.get_blocking(descriptor)
            os.set_blocking(descriptor, False)
        with selectors.SelectSelector() as selector:
            selector.register(0, selectors.EVENT_READ)
            selector.register(client, selectors.EVENT_READ | selectors.EVENT_WRITE)
            while not stopped and os.getppid() == parent:
                # Bound queues even when a caller stops consuming stdout/stderr.
                socket_events = 0
                if len(to_stdout) + len(to_stderr) < BUFFER_LIMIT:
                    socket_events |= selectors.EVENT_READ
                if to_backend:
                    socket_events |= selectors.EVENT_WRITE
                if socket_events:
                    if client.fileno() in selector.get_map():
                        selector.modify(client, socket_events)
                    else:
                        selector.register(client, socket_events)
                elif client.fileno() in selector.get_map():
                    selector.unregister(client)
                if len(to_backend) < BUFFER_LIMIT:
                    if 0 not in selector.get_map():
                        selector.register(0, selectors.EVENT_READ)
                elif 0 in selector.get_map():
                    selector.unregister(0)
                for key, events in selector.select(timeout=0.1):
                    if key.fd == 0:
                        data = os.read(0, 65536)
                        if not data:
                            return 0
                        to_backend.extend(data)
                    else:
                        if events & selectors.EVENT_WRITE and to_backend:
                            try:
                                sent = client.send(to_backend)
                                del to_backend[:sent]
                            except BlockingIOError:
                                pass
                        if events & selectors.EVENT_READ:
                            try:
                                data = client.recv(65536)
                            except BlockingIOError:
                                continue
                            if not data:
                                return 0
                            from_backend.extend(data)
                            while b"\n" in from_backend:
                                line, _, remainder = from_backend.partition(b"\n")
                                from_backend = bytearray(remainder)
                                frame = json.loads(line)
                                if "message" in frame:
                                    to_stdout.extend(
                                        (json.dumps(frame["message"], separators=(",", ":")) + "\n").encode()
                                    )
                                elif "audit" in frame:
                                    name = frame["audit"]
                                    if not isinstance(name, str) or not name.isidentifier():
                                        raise RuntimeError("Invalid audit envelope")
                                    to_stderr.extend(f"[whatsapp-mcp:audit] MCP_TOOL_CALL tool={name}\n".encode())
                                    token = frame.get("token")
                                    if not isinstance(token, int):
                                        raise RuntimeError("Invalid audit token")
                                    pending_audits.append(token)
                                else:
                                    raise RuntimeError("Invalid backend envelope")
                            if len(from_backend) > MAX_LINE:
                                raise RuntimeError("Backend message exceeds transport limit")
                # Audit evidence must be delivered before the associated result.
                for descriptor, buffer in ((2, to_stderr), (1, to_stdout)):
                    if buffer and (descriptor == 2 or not to_stderr):
                        try:
                            count = os.write(descriptor, buffer)
                            del buffer[:count]
                        except BlockingIOError:
                            pass
                if not to_stderr and pending_audits:
                    for token in pending_audits:
                        to_backend.extend((json.dumps({"audit_ack": token}) + "\n").encode())
                    pending_audits.clear()
        return 0
    except (BrokenPipeError, ConnectionResetError):
        return 0
    except Exception as exc:
        # Fixed diagnostics never include MCP payloads or WhatsApp configuration.
        try:
            os.write(2, f"[whatsapp-mcp] relay failed ({type(exc).__name__})\n".encode())
        except OSError:
            pass
        return 0 if stopped else 1
    finally:
        if client is not None:
            client.close()
        for descriptor, blocking in previous_blocking.items():
            with contextlib.suppress(OSError):
                os.set_blocking(descriptor, blocking)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


async def serve_backend(directory: Path, socket_ready=None) -> None:
    # Heavy imports belong exclusively to the singleton process.
    import anyio
    from mcp import types

    # Policy is per connection, never inherited from the first launching client.
    os.environ.pop("MCP_READ_ONLY", None)
    import main

    # AnyIO binds and chmods in separate worker calls. The socket must already be
    # 0600 at bind time, so relays never observe a permissive intermediate mode.
    previous_umask = os.umask(0o177)
    try:
        listener = await anyio.create_unix_listener(str(directory / "server.sock"), mode=0o600)
    finally:
        os.umask(previous_umask)
    if socket_ready is not None:
        socket_ready((directory / "server.sock").lstat().st_ino)
    active = 0
    last_disconnected = time.monotonic()

    async def connection(stream):
        nonlocal active, last_disconnected
        active += 1
        token = None
        try:
            async with stream, anyio.create_task_group() as group:
                incoming_send, incoming_receive = anyio.create_memory_object_stream(0)
                result_send, result_receive = anyio.create_memory_object_stream(0)
                output_send, output_receive = anyio.create_memory_object_stream(float("inf"))

                async def lines():
                    buffer = bytearray()
                    while True:
                        try:
                            data = await stream.receive(65536)
                        except anyio.EndOfStream:
                            return
                        buffer.extend(data)
                        while b"\n" in buffer:
                            line, _, remainder = buffer.partition(b"\n")
                            buffer = bytearray(remainder)
                            if len(line) > MAX_LINE:
                                raise ValueError("Transport message too large")
                            yield line
                        if len(buffer) > MAX_LINE:
                            raise ValueError("Transport message too large")

                iterator = lines()
                with anyio.fail_after(START_TIMEOUT):
                    handshake = json.loads(await anext(iterator))
                if (
                    handshake.get("version") != 1
                    or not isinstance(handshake.get("read_only"), bool)
                    or set(handshake) != {"version", "read_only"}
                ):
                    return
                server = main.read_only_mcp if handshake["read_only"] else main.mcp
                pending_audits = {}
                next_audit = 0

                async def audit(name):
                    nonlocal next_audit
                    next_audit += 1
                    audit_id = next_audit
                    acknowledged = anyio.Event()
                    pending_audits[audit_id] = acknowledged
                    try:
                        await output_send.send({"audit": name, "token": audit_id})
                        await acknowledged.wait()
                    finally:
                        pending_audits.pop(audit_id, None)

                token = main.audit_callback.set(audit)

                async def read_messages():
                    try:
                        async for line in iterator:
                            try:
                                frame = json.loads(line)
                            except ValueError:
                                await incoming_send.send(ValueError("Invalid JSON-RPC message"))
                                continue
                            if isinstance(frame, dict) and set(frame) == {"audit_ack"}:
                                acknowledged = pending_audits.get(frame["audit_ack"])
                                if acknowledged is not None:
                                    acknowledged.set()
                                continue
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                            except ValueError:
                                # Match stdio transport: pass parse failures to the session.
                                await incoming_send.send(ValueError("Invalid JSON-RPC message"))
                                continue
                            await incoming_send.send(message)
                    finally:
                        # MCP 1.6 leaves incoming_messages open on EOF. Cancel its
                        # entire run scope, including handlers, instead of waiting.
                        group.cancel_scope.cancel()

                async def forward_results():
                    async for message in result_receive:
                        await output_send.send(
                            {"message": message.model_dump(mode="json", by_alias=True, exclude_none=True)}
                        )

                async def write_output():
                    async for frame in output_receive:
                        await stream.send((json.dumps(frame, separators=(",", ":")) + "\n").encode())

                group.start_soon(read_messages)
                group.start_soon(forward_results)
                group.start_soon(write_output)
                group.start_soon(
                    server._mcp_server.run,
                    incoming_receive,
                    result_send,
                    server._mcp_server.create_initialization_options(),
                )
        except Exception:
            # Peer errors affect only that peer and never expose message contents.
            pass
        finally:
            if token is not None:
                main.audit_callback.reset(token)
            active -= 1
            last_disconnected = time.monotonic()

    async with listener, anyio.create_task_group() as group:

        async def idle_shutdown():
            while True:
                await anyio.sleep(0.25)
                if active == 0 and time.monotonic() - last_disconnected >= IDLE_TIMEOUT:
                    group.cancel_scope.cancel()
                    return

        async def shutdown_signal():
            with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                async for _ in signals:
                    group.cancel_scope.cancel()
                    return

        group.start_soon(idle_shutdown)
        group.start_soon(shutdown_signal)
        await listener.serve(connection)


def backend_main() -> int:
    directory = runtime_directory()
    lock = open_lock(directory / "owner.lock")
    owned_socket = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Only the exclusive owner may remove a stale socket. Never remove files.
        path = directory / "server.sock"
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Unsafe stale socket")
            path.unlink()
        pid_path = directory / "backend.pid"
        descriptor = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as pid_file:
            pid_file.write(str(os.getpid()) + "\n")
        import anyio

        async def run():
            nonlocal owned_socket

            def socket_ready(inode):
                nonlocal owned_socket
                owned_socket = inode

            await serve_backend(directory, socket_ready)

        anyio.run(run)
        return 0
    finally:
        path = directory / "server.sock"
        if owned_socket is not None:
            with contextlib.suppress(FileNotFoundError):
                if path.lstat().st_ino == owned_socket:
                    path.unlink()
        pid_path = directory / "backend.pid"
        with contextlib.suppress(FileNotFoundError):
            if pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(backend_main() if sys.argv[1:] == ["--backend"] else relay_main())
