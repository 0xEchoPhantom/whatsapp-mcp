"""Exercise real MCP sessions without reading chats or sending WhatsApp messages."""

import asyncio
import contextlib
import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import main
import shared_runtime

SOURCE = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "Condition did not become true before deadline"


class Client:
    def __init__(self, directory, read_only=False):
        env = os.environ.copy()
        env["WHATSAPP_MCP_RUNTIME_DIR"] = str(directory)
        env["MCP_READ_ONLY"] = "1" if read_only else "0"
        self.process = subprocess.Popen(
            [sys.executable, str(SOURCE / "main.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=SOURCE,
        )
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def line(self, channel="stdout", timeout=10):
        descriptor = getattr(self.process, channel).fileno()
        buffer = self.buffers[channel]
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while b"\n" not in buffer:
                assert time.monotonic() < deadline, f"No {channel} response"
                if selector.select(timeout=0.1):
                    data = os.read(descriptor, 65536)
                    assert data, f"{channel} closed unexpectedly"
                    buffer.extend(data)
        line, _, remainder = buffer.partition(b"\n")
        self.buffers[channel] = bytearray(remainder)
        return line.decode()

    def request(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        result = json.loads(self.line())
        assert result["id"] == request_id
        return result

    def initialize(self, request_id=1):
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "runtime-test", "version": "1"},
            },
            request_id,
        )
        assert response["result"]["serverInfo"]["name"] == "whatsapp"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()


@pytest.fixture
def runtime():
    # macOS Unix socket paths cannot use pytest's long temporary directory names.
    directory = Path(tempfile.mkdtemp(prefix="wa-test-", dir="/tmp"))
    clients = []

    def create(read_only=False):
        client = Client(directory, read_only)
        clients.append(client)
        return client

    yield directory, create
    for client in clients:
        client.close()
    pid_path = directory / "backend.pid"
    if pid_path.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid_path.read_text()), signal.SIGTERM)
        wait_for(lambda: not (directory / "server.sock").exists())
    shutil.rmtree(directory)


def test_five_simultaneous_clients_share_backend_and_keep_ids_isolated(runtime):
    directory, create = runtime
    # First client is read-only; it must not restrict subsequent writable clients.
    clients = [create(read_only=index % 2 == 0) for index in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda entry: entry[1].initialize(request_id=entry[0] + 10), enumerate(clients)))
    pid = int((directory / "backend.pid").read_text())
    assert (directory / "server.sock").stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    # All clients deliberately reuse id=1: responses remain session-local.
    for client in clients:
        client.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    for index, client in enumerate(clients):
        response = json.loads(client.line())
        assert response["id"] == 1
        names = {tool["name"] for tool in response["result"]["tools"]}
        assert ("send_message" in names) == (index % 2 == 1)
        assert int((directory / "backend.pid").read_text()) == pid
    # An exclusive owner lock proves no second backend can be running.
    duplicate = subprocess.run(
        [sys.executable, str(SOURCE / "shared_runtime.py"), "--backend"],
        env={**os.environ, "WHATSAPP_MCP_RUNTIME_DIR": str(directory)},
        timeout=3,
        capture_output=True,
    )
    assert duplicate.returncode == 0
    assert int((directory / "backend.pid").read_text()) == pid
    clients[0].close()
    assert "result" in clients[1].request("tools/list")


def test_read_only_call_is_denied_before_whatsapp_function(monkeypatch):
    called = []
    monkeypatch.setattr(main, "whatsapp_send_message", lambda *args: called.append(args))
    with pytest.raises(Exception, match="Unknown tool"):
        asyncio.run(main.read_only_mcp.call_tool("send_message", {"recipient": "unused", "message": "unused"}))
    assert called == []


def test_wire_readonly_rejects_hidden_send_without_audit(runtime):
    _, create = runtime
    client = create(read_only=True)
    client.initialize()
    response = client.request("tools/call", {"name": "send_message", "arguments": {}})
    assert response["result"]["isError"] is True
    assert "Unknown tool" in response["result"]["content"][0]["text"]
    with selectors.DefaultSelector() as selector:
        selector.register(client.process.stderr, selectors.EVENT_READ)
        assert selector.select(timeout=0.2) == []


def test_audit_only_reaches_invoking_client_before_result(runtime):
    _, create = runtime
    first, second = create(), create(read_only=True)
    first.initialize()
    second.initialize()
    # get_contact's missing-identifier guard raises before DB/network access.
    result = first.request("tools/call", {"name": "get_contact", "arguments": {}})
    assert result["result"]["isError"] is True
    assert "Missing required argument" in result["result"]["content"][0]["text"]
    assert first.line("stderr") == "[whatsapp-mcp:audit] MCP_TOOL_CALL tool=get_contact"
    with selectors.DefaultSelector() as selector:
        selector.register(second.process.stderr, selectors.EVENT_READ)
        assert selector.select(timeout=0.2) == []


def test_blocked_tool_does_not_block_other_sessions_and_audits_before_work(runtime):
    directory, create = runtime
    release = directory / "release"
    started = directory / "started"
    code = (
        "import main,shared_runtime,time,pathlib\n"
        f"release=pathlib.Path({str(release)!r})\n"
        f"started=pathlib.Path({str(started)!r})\n"
        "def blocked(query):\n"
        " started.touch()\n"
        " while not release.exists(): time.sleep(.05)\n"
        " return []\n"
        "main.whatsapp_search_contacts=blocked\n"
        "shared_runtime.IDLE_TIMEOUT=.25\n"
        "raise SystemExit(shared_runtime.backend_main())\n"
    )
    backend = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=SOURCE,
        env={**os.environ, "MCP_READ_ONLY": "0", "WHATSAPP_MCP_RUNTIME_DIR": str(directory)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: (directory / "server.sock").exists())
        first = create()
        first.initialize()
        first.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_contacts", "arguments": {"query": "test-only"}},
            }
        )
        assert first.line("stderr") == "[whatsapp-mcp:audit] MCP_TOOL_CALL tool=search_contacts"
        wait_for(started.exists)
        # Real initialize/list on another connection while the substituted tool blocks.
        second = create(read_only=True)
        second.initialize()
        assert "result" in second.request("tools/list")
        # Disconnect both clients: already-started work must remain tracked and
        # keep this backend alive beyond its shortened test-only idle timeout.
        first.close()
        second.close()
        time.sleep(0.6)
        assert backend.poll() is None
        third = create(read_only=True)
        third.initialize()
        assert "result" in third.request("tools/list")
        release.touch()
        third.close()
        assert backend.wait(timeout=5) == 0
    finally:
        release.touch()
        backend.terminate()
        backend.wait(timeout=5)


@pytest.mark.parametrize("initialize", [False, True])
def test_stdin_eof_exits_relay_promptly(runtime, initialize):
    _, create = runtime
    client = create()
    if initialize:
        client.initialize()
    client.process.stdin.close()
    assert client.process.wait(timeout=3) == 0


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_relay_signals_release_its_session(runtime, signum):
    _, create = runtime
    client = create()
    client.initialize()
    client.process.send_signal(signum)
    assert client.process.wait(timeout=3) == 0


def test_parent_death_exits_even_with_stdin_still_open(runtime):
    directory, _ = runtime
    # The grandparent holds stdin open. Parent death, rather than EOF, must exit.
    read_descriptor, write_descriptor = os.pipe()
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,sys.argv[1]],stdin=int(sys.argv[2]),"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(p.pid,flush=True); time.sleep(30)",
            str(SOURCE / "main.py"),
            str(read_descriptor),
        ],
        stdout=subprocess.PIPE,
        pass_fds=(read_descriptor,),
        env={**os.environ, "WHATSAPP_MCP_RUNTIME_DIR": str(directory)},
    )
    relay_pid = int(parent.stdout.readline())
    try:
        time.sleep(0.3)
        assert not (directory / "server.sock").exists()
        parent.terminate()
        parent.wait(timeout=3)

        def gone():
            result = subprocess.run(["ps", "-p", str(relay_pid), "-o", "stat="], capture_output=True, text=True)
            return not result.stdout.strip() or result.stdout.strip().startswith("Z")

        wait_for(gone, timeout=3)
    finally:
        parent.kill() if parent.poll() is None else None
        parent.wait()
        parent.stdout.close()
        os.close(read_descriptor)
        os.close(write_descriptor)
        with contextlib.suppress(ProcessLookupError):
            os.kill(relay_pid, signal.SIGTERM)


def test_backend_signal_removes_owned_socket(runtime):
    directory, create = runtime
    client = create()
    client.initialize()
    pid = int((directory / "backend.pid").read_text())
    os.kill(pid, signal.SIGTERM)
    wait_for(lambda: not (directory / "server.sock").exists())
    assert client.process.wait(timeout=3) == 0
    assert not (directory / "backend.pid").exists()


def test_stale_socket_is_replaced_only_by_new_owner(runtime):
    directory, create = runtime
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(directory / "server.sock"))
    os.chmod(directory / "server.sock", 0o600)
    stale.close()
    client = create()
    client.initialize()
    assert "result" in client.request("tools/list")


def test_socket_is_private_even_while_anyio_chmod_is_delayed(runtime):
    directory, create = runtime
    code = (
        "import time,anyio._core._sockets as sockets,shared_runtime\n"
        "original=sockets.chmod\n"
        "def delayed(path,mode):\n"
        " time.sleep(.5)\n"
        " original(path,mode)\n"
        "sockets.chmod=delayed\n"
        "raise SystemExit(shared_runtime.backend_main())\n"
    )
    backend = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=SOURCE,
        env={**os.environ, "WHATSAPP_MCP_RUNTIME_DIR": str(directory)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: (directory / "server.sock").exists())
        assert (directory / "server.sock").stat().st_mode & 0o777 == 0o600
        client = create(read_only=True)
        client.initialize()
    finally:
        backend.terminate()
        backend.wait(timeout=5)


def test_backend_idle_exit_proves_eof_sessions_do_not_leak(runtime):
    directory, create = runtime
    clients = [create() for _ in range(5)]
    for client in clients:
        client.initialize()
        client.close()
    wait_for(lambda: not (directory / "server.sock").exists(), timeout=shared_runtime.IDLE_TIMEOUT + 3)
    assert not (directory / "backend.pid").exists()


def test_unsafe_runtime_directory_is_rejected(monkeypatch, tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o755)
    monkeypatch.setenv("WHATSAPP_MCP_RUNTIME_DIR", str(directory))
    with pytest.raises(RuntimeError, match="0700"):
        shared_runtime.runtime_directory()


def test_devnull_eof_never_starts_backend(runtime):
    directory, _ = runtime
    process = subprocess.run(
        [sys.executable, str(SOURCE / "main.py")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={**os.environ, "WHATSAPP_MCP_RUNTIME_DIR": str(directory)},
        timeout=3,
    )
    assert process.returncode == 0
    assert process.stdout == process.stderr == b""
    assert not (directory / "backend.pid").exists()
