"""Tests for the molecule-a2a hermes adapter.

These tests stand up a fake MCP server (a tiny Python script that
speaks JSON-RPC over stdio) so the adapter exercises its real
subprocess+pipe code path. Mocking the subprocess directly would
mask the bug-class this adapter is most likely to hit (JSON framing,
demux, handshake order).

Run via: ``pytest -q tests/test_adapter.py`` from the plugin root.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

# Resolve the adapter file relative to the package, regardless of where
# pytest is invoked from. The tests stub gateway.platforms.base before
# loading adapter.py, so we sidestep `import hermes_channel_molecule`
# (which would trigger the real base-class import) and load via importlib.
import importlib.util

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADAPTER_PATH = _REPO_ROOT / "hermes_channel_molecule" / "adapter.py"


def _load_adapter_module():
    """Import adapter.py without going through gateway/* + hermes_cli/*.

    The real plugin loader pulls in gateway.config + gateway.platforms.base;
    in tests we stub them so the import doesn't require the whole
    hermes-agent tree.
    """
    # Stub gateway.config.Platform — the adapter constructs Platform("molecule")
    # in __init__ to identify itself to the upstream platform_registry.
    fake_gateway = type(sys)("gateway")
    fake_config = type(sys)("gateway.config")

    from dataclasses import dataclass, field
    from enum import Enum
    from typing import Optional, List, Any as TAny
    from datetime import datetime

    class Platform(Enum):
        # Open enum (per upstream #17751): Platform("molecule") creates a
        # pseudo-member at runtime when not in the in-tree set. Empty
        # enums can't be created in Python — seed with a sentinel that
        # the adapter never references.
        _SENTINEL = "__test_sentinel__"

        @classmethod
        def _missing_(cls, value):
            if not isinstance(value, str) or not value.strip():
                return None
            value = value.strip().lower()
            if value in cls._value2member_map_:
                return cls._value2member_map_[value]
            pseudo = object.__new__(cls)
            pseudo._value_ = value
            pseudo._name_ = value.upper().replace("-", "_")
            cls._value2member_map_[value] = pseudo
            cls._member_map_[pseudo._name_] = pseudo
            return pseudo

    fake_config.Platform = Platform
    fake_gateway.config = fake_config
    sys.modules["gateway"] = fake_gateway
    sys.modules["gateway.config"] = fake_config

    # Stub gateway.platforms.base — the adapter only uses
    # BasePlatformAdapter, MessageEvent, MessageType, SendResult.
    fake_platforms = type(sys)("gateway.platforms")
    fake_base = type(sys)("gateway.platforms.base")

    class MessageType(Enum):
        TEXT = "text"

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: TAny = None
        retryable: bool = False

    @dataclass
    class MessageEvent:
        text: str
        message_type: TAny = MessageType.TEXT
        source: TAny = None
        raw_message: TAny = None
        message_id: Optional[str] = None
        media_urls: List[str] = field(default_factory=list)
        media_types: List[str] = field(default_factory=list)
        reply_to_message_id: Optional[str] = None
        reply_to_text: Optional[str] = None
        timestamp: TAny = field(default_factory=datetime.now)

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self._running = False
            self.handled: list = []
            self._fatal_error_message = None
            self._fatal_error_code = None
            self._fatal_error_retryable = True

        def _mark_connected(self) -> None:
            self._running = True

        def _mark_disconnected(self) -> None:
            self._running = False

        @property
        def is_connected(self) -> bool:
            return self._running

        def _set_fatal_error(self, code, message, *, retryable):
            self._fatal_error_code = code
            self._fatal_error_message = message
            self._fatal_error_retryable = retryable

        def build_source(self, **kw):
            return kw

        async def handle_message(self, event):
            self.handled.append(event)

    fake_base.BasePlatformAdapter = BasePlatformAdapter
    fake_base.MessageEvent = MessageEvent
    fake_base.MessageType = MessageType
    fake_base.SendResult = SendResult
    fake_platforms.base = fake_base
    fake_gateway.platforms = fake_platforms
    sys.modules["gateway"] = fake_gateway
    sys.modules["gateway.platforms"] = fake_platforms
    sys.modules["gateway.platforms.base"] = fake_base

    spec = importlib.util.spec_from_file_location("molecule_a2a_adapter", _ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adapter_mod = _load_adapter_module()


# ----------------------------------------------------------------------
# Fake MCP server — a tiny script the adapter spawns instead of the real
# molecule_runtime.a2a_mcp_server. Behavior is driven by env vars so each
# test can configure responses without rewriting the script.
# ----------------------------------------------------------------------

_FAKE_MCP_SERVER = textwrap.dedent("""
    import json, os, sys, time

    # Optional: emit a fixed sequence of JSON responses for tools/call by
    # name, encoded as MCP `content` text. Configure via env:
    #   FAKE_TOOL_<NAME> = JSON-string returned as the tool's text payload
    #   FAKE_TOOL_<NAME>_RAW = if set, return as-is (no extra wrapping)
    # wait_for_message gets special handling: if FAKE_INBOX is set to a
    # newline-delimited list of JSON dicts, each call pops the next one;
    # exhaustion returns the timeout sentinel.

    inbox_iter = iter(
        [json.loads(line) for line in os.environ.get("FAKE_INBOX", "").splitlines() if line.strip()]
    )

    # If FAKE_ENV_DUMP is set, dump the env once at startup so tests
    # can assert what was actually passed through to the subprocess.
    dump_path = os.environ.get("FAKE_ENV_DUMP", "")
    if dump_path:
        with open(dump_path, "w") as f:
            json.dump(dict(os.environ), f)

    def respond(req_id, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-a2a", "version": "0.0.1"},
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/call":
            name = params.get("name", "")
            if name == "wait_for_message":
                try:
                    payload = next(inbox_iter)
                except StopIteration:
                    payload = {"timeout": True, "timeout_secs": 0}
                respond(req_id, {"content": [{"type": "text", "text": json.dumps(payload)}]})
            else:
                key = f"FAKE_TOOL_{name.upper()}"
                if key in os.environ:
                    text = os.environ[key]
                else:
                    text = json.dumps({"ok": True, "tool": name, "args": params.get("arguments", {})})
                respond(req_id, {"content": [{"type": "text", "text": text}]})
        else:
            if req_id is not None:
                respond(req_id, {})
""")


@pytest.fixture
def fake_mcp_script(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_MCP_SERVER)
    return script


def _make_adapter(monkeypatch, fake_mcp_script: Path, *, env_extra: dict | None = None):
    """Build a MoleculeAdapter that spawns the fake MCP script."""
    # Force _resolve_python to return THIS python — guarantees the fake
    # script is interpretable in the test environment.
    monkeypatch.setattr(adapter_mod, "_resolve_python", lambda: sys.executable)

    monkeypatch.setenv("MOLECULE_WORKSPACE_ID", "ws-test-1234")
    monkeypatch.setenv("MOLECULE_PLATFORM_URL", "http://platform:8080")
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-test")
    if env_extra:
        for k, v in env_extra.items():
            monkeypatch.setenv(k, v)

    cfg = type("Cfg", (), {"extra": {}, "enabled": True})()
    a = adapter_mod.MoleculeAdapter(cfg)

    # Override the spawn target to point at our fake script instead of
    # the `-m molecule_runtime.mcp_cli` module.
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def fake_spawn(*args, **kwargs):
        # Replace the trailing `-m molecule_runtime.mcp_cli` args
        # with the path to the fake script.
        new_args = (args[0], str(fake_mcp_script))
        return await real_create_subprocess_exec(*new_args, **kwargs)

    monkeypatch.setattr(adapter_mod.asyncio, "create_subprocess_exec", fake_spawn)
    return a


@pytest.fixture
def fake_platform_http():
    """Local platform boundary used by the real-runtime startup test."""

    class State:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.calls: list[dict[str, Any]] = []
            self.message_sent = False

        def record(self, handler, method: str) -> None:
            parsed = urlparse(handler.path)
            with self.lock:
                self.calls.append(
                    {
                        "method": method,
                        "path": parsed.path,
                        "query": parse_qs(parsed.query),
                        # Keep credentials out of assertion output. These
                        # booleans still prove the real runtime authenticated
                        # every platform lifecycle request.
                        "auth_ok": handler.headers.get("Authorization")
                        == "Bearer test-workspace-credential",
                        "origin_ok": handler.headers.get("Origin")
                        == base_url,
                    }
                )

        def snapshot(self) -> list[dict[str, Any]]:
            with self.lock:
                return list(self.calls)

    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args) -> None:
            return

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            state.record(self, "POST")
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            self._json({"ok": True, "platform_inbound_secret": "test-inbound"})

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            state.record(self, "GET")
            parsed = urlparse(self.path)
            if parsed.path == "/workspaces/ws-real-runtime/activity":
                with state.lock:
                    first = not state.message_sent
                    state.message_sent = True
                if first:
                    self._json(
                        [
                            {
                                "id": "activity-real-1",
                                "source_id": "",
                                "method": "message/send",
                                "created_at": "2026-07-14T00:00:00Z",
                                "request_body": {
                                    "params": {
                                        "message": {
                                            "parts": [
                                                {
                                                    "kind": "text",
                                                    "text": "real runtime inbound",
                                                }
                                            ]
                                        }
                                    }
                                },
                            }
                        ]
                    )
                    return
                self._json([])
                return
            self._json({"error": "not found"}, status=404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_completes_handshake_and_starts_loops(monkeypatch, fake_mcp_script):
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        ok = await a.connect()
        assert ok is True
        assert a.is_connected is True
        assert a._poll_task is not None
        assert a._reader_task is not None
        assert a._fatal_error_message is None
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_real_runtime_entrypoint_starts_external_lifecycle_and_inbox(
    monkeypatch, tmp_path, fake_platform_http, caplog
):
    """The channel must launch the standalone runtime, not its bare MCP loop.

    Runtime 0.4.4+'s mcp_cli path registers the external workspace, starts a
    heartbeat, starts the activity poller, then serves MCP over stdio. A fake
    JSON-RPC subprocess cannot prove any of those platform-boundary effects.
    """
    assert tuple(int(part) for part in version("molecules-workspace-runtime").split(".")[:3]) >= (
        0,
        4,
        4,
    )

    base_url, state = fake_platform_http
    home = tmp_path / "home"
    mailbox = tmp_path / "mailbox"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MOLECULE_MAILBOX_DIR", str(mailbox))
    monkeypatch.delenv("CONFIGS_DIR", raising=False)
    monkeypatch.delenv("MOLECULE_CONFIGS_DIR", raising=False)
    monkeypatch.setenv("MOLECULE_WORKSPACE_ID", "ws-real-runtime")
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "test-workspace-credential")
    monkeypatch.setenv("MOLECULE_PLATFORM_URL", base_url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("MOLECULE_MCP_DISABLE_HEARTBEAT", raising=False)
    monkeypatch.delenv("MOLECULE_MCP_DISABLE_INBOX", raising=False)
    monkeypatch.setattr(adapter_mod, "_resolve_python", lambda: sys.executable)
    caplog.set_level("INFO", logger=adapter_mod.logger.name)

    cfg = type("Cfg", (), {"extra": {}, "enabled": True})()
    adapter = adapter_mod.MoleculeAdapter(cfg)
    try:
        assert await adapter.connect()
        for _ in range(200):
            calls = state.snapshot()
            paths = {(call["method"], call["path"]) for call in calls}
            if (
                ("POST", "/registry/register") in paths
                and ("POST", "/registry/heartbeat") in paths
                and ("GET", "/workspaces/ws-real-runtime/activity") in paths
                and adapter.handled
            ):
                break
            await asyncio.sleep(0.05)

        calls = state.snapshot()
        paths = {(call["method"], call["path"]) for call in calls}
        assert ("POST", "/registry/register") in paths
        assert ("POST", "/registry/heartbeat") in paths
        assert ("GET", "/workspaces/ws-real-runtime/activity") in paths
        lifecycle_calls = [
            call
            for call in calls
            if call["path"]
            in {
                "/registry/register",
                "/registry/heartbeat",
                "/workspaces/ws-real-runtime/activity",
            }
        ]
        assert lifecycle_calls
        assert all(call["auth_ok"] and call["origin_ok"] for call in lifecycle_calls)

        poll = next(
            call
            for call in lifecycle_calls
            if call["path"] == "/workspaces/ws-real-runtime/activity"
        )
        assert poll["query"]["type"] == ["a2a_receive"]
        assert poll["query"]["include"] == ["peer_info"]
        assert len(adapter.handled) == 1
        assert adapter.handled[0].text == "real runtime inbound"
        assert "MCP initialize ok (server=molecule)" in caplog.text
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_subprocess_env_includes_workspace_token(monkeypatch, fake_mcp_script, tmp_path):
    """Regression: MOLECULE_WORKSPACE_TOKEN must reach the spawned MCP
    subprocess. molecule_runtime.platform_auth.get_token reads it for
    external runtimes (no /configs/.auth_token file). Without this
    passthrough the runtime sends every outbound platform call
    unauthenticated and the platform 401s.
    """
    env_dump = tmp_path / "env-dump.json"
    a = _make_adapter(
        monkeypatch,
        fake_mcp_script,
        env_extra={
            "MOLECULE_WORKSPACE_TOKEN": "test-only",
            "FAKE_ENV_DUMP": str(env_dump),
        },
    )
    try:
        assert await a.connect()
        assert env_dump.exists(), "fake server should have dumped env"
        captured = json.loads(env_dump.read_text())
        # Exact-equality on the token. Substring-in-repr would pass
        # whether the var was passed through OR whether something else
        # leaked the literal text.
        assert captured.get("MOLECULE_WORKSPACE_TOKEN") == "test-only"
        # And confirm the canonical runtime env vars are also present.
        assert captured.get("WORKSPACE_ID") == "ws-test-1234"
        assert captured.get("PLATFORM_URL") == "http://platform:8080"
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_subprocess_env_omits_workspace_token_when_unset(monkeypatch, fake_mcp_script, tmp_path):
    """Low-level env construction never fabricates a workspace credential.

    The supported gateway path rejects this configuration in
    check_molecule_requirements before adapter creation. This direct adapter
    test keeps the lower-level invariant explicit as defense in depth.
    """
    monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)
    env_dump = tmp_path / "env-dump.json"
    a = _make_adapter(
        monkeypatch,
        fake_mcp_script,
        env_extra={"FAKE_ENV_DUMP": str(env_dump)},
    )
    try:
        assert await a.connect()
        captured = json.loads(env_dump.read_text())
        assert "MOLECULE_WORKSPACE_TOKEN" not in captured
    finally:
        await a.disconnect()
    assert a.is_connected is False


@pytest.mark.asyncio
async def test_send_canvas_routes_to_send_message_to_user(monkeypatch, fake_mcp_script):
    monkeypatch.setenv("FAKE_TOOL_SEND_MESSAGE_TO_USER", json.dumps({"sent": True, "via": "canvas"}))
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        result = await a.send("canvas:ws-test-1234", "hello canvas")
        assert result.success is True
        # Exact-shape assertion — the fake echoes back what tool was hit.
        assert result.raw_response == {"sent": True, "via": "canvas"}
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_send_canvas_forwards_attachment_metadata(monkeypatch, fake_mcp_script):
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        result = await a.send(
            "canvas:ws-test-1234",
            "see attached",
            metadata={
                "attachments": [
                    {
                        "uri": "workspace:/workspace/org_chart_v2.png",
                        "name": "org_chart_v2.png",
                        "mimeType": "image/png",
                        "size": 12345,
                    }
                ]
            },
        )
        assert result.success is True
        assert result.raw_response["args"] == {
            "message": "see attached",
            "attachments": [
                {
                    "uri": "workspace:/workspace/org_chart_v2.png",
                    "name": "org_chart_v2.png",
                    "mimeType": "image/png",
                    "size": 12345,
                }
            ],
        }
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_send_canvas_forwards_local_attachment_paths(monkeypatch, fake_mcp_script):
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        result = await a.send(
            "canvas:ws-test-1234",
            "see attached",
            metadata={"attachments": ["/workspace/org_chart_v2.png"]},
        )
        assert result.success is True
        assert result.raw_response["args"] == {
            "message": "see attached",
            "attachments": [{"path": "/workspace/org_chart_v2.png"}],
        }
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_send_peer_routes_to_delegate_task(monkeypatch, fake_mcp_script):
    monkeypatch.setenv(
        "FAKE_TOOL_DELEGATE_TASK",
        json.dumps({"delegated_to": "peer-uuid-7777", "echo_task": "do the thing"}),
    )
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        result = await a.send("peer:peer-uuid-7777", "do the thing")
        assert result.success is True
        assert result.raw_response == {
            "delegated_to": "peer-uuid-7777",
            "echo_task": "do the thing",
        }
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_send_unknown_chat_id_prefix_fails(monkeypatch, fake_mcp_script):
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        result = await a.send("telegram:123", "nope")
        assert result.success is False
        assert "unknown chat_id prefix" in (result.error or "")
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_long_poll_dispatches_canvas_message_and_acks(monkeypatch, fake_mcp_script):
    inbox_msg = {
        "activity_id": "act-aaa-111",
        "text": "hi from canvas",
        "peer_id": "",
        "kind": "canvas_user",
        "method": "message/send",
        "created_at": "2026-05-03T00:00:00Z",
    }
    monkeypatch.setenv("FAKE_INBOX", json.dumps(inbox_msg))
    monkeypatch.setenv(
        "FAKE_TOOL_INBOX_POP",
        json.dumps({"acked": "act-aaa-111"}),
    )
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        # Wait briefly for the long-poll loop to see the message.
        for _ in range(50):
            if a.handled:
                break
            await asyncio.sleep(0.05)
        assert len(a.handled) == 1, "expected exactly one MessageEvent dispatched"
        ev = a.handled[0]
        # Exact-equality on what the gateway sees.
        assert ev.text == "hi from canvas"
        assert ev.message_id == "act-aaa-111"
        assert ev.source["chat_id"] == "canvas:ws-test-1234"
        assert ev.source["user_id"] == "canvas"
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_long_poll_dispatches_peer_message(monkeypatch, fake_mcp_script):
    inbox_msg = {
        "activity_id": "act-bbb-222",
        "text": "ping from peer",
        "peer_id": "ws-peer-9999",
        "kind": "peer_agent",
        "method": "tasks/send",
        "created_at": "2026-05-03T00:00:01Z",
    }
    monkeypatch.setenv("FAKE_INBOX", json.dumps(inbox_msg))
    a = _make_adapter(monkeypatch, fake_mcp_script)
    try:
        assert await a.connect()
        for _ in range(50):
            if a.handled:
                break
            await asyncio.sleep(0.05)
        assert len(a.handled) == 1
        ev = a.handled[0]
        assert ev.text == "ping from peer"
        # Peer messages encode peer_id into chat_id so the reply path
        # can route back to delegate_task with the right workspace_id.
        assert ev.source["chat_id"] == "peer:ws-peer-9999"
        assert ev.source["user_id"] == "ws-peer-9999"
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_disconnect_terminates_subprocess(monkeypatch, fake_mcp_script):
    a = _make_adapter(monkeypatch, fake_mcp_script)
    assert await a.connect()
    proc = a._proc
    assert proc is not None and proc.returncode is None
    await a.disconnect()
    # Either reaped (returncode set) or already collected (proc=None).
    if a._proc is None:
        # Fully torn down — that's the success shape.
        return
    assert a._proc.returncode is not None


def test_decode_chat_id_canvas():
    assert adapter_mod.MoleculeAdapter._decode_chat_id("canvas:ws-1") == ("canvas", "ws-1")


def test_decode_chat_id_peer():
    assert adapter_mod.MoleculeAdapter._decode_chat_id("peer:peer-uuid") == ("peer", "peer-uuid")


def test_decode_chat_id_unknown_prefix():
    # Unknown but well-formed: prefix returned as kind, rest as target.
    assert adapter_mod.MoleculeAdapter._decode_chat_id("telegram:123") == ("telegram", "123")


def test_decode_chat_id_no_prefix():
    assert adapter_mod.MoleculeAdapter._decode_chat_id("bare") == ("", "bare")


def test_extract_inbox_message_timeout_returns_none():
    assert adapter_mod.MoleculeAdapter._extract_inbox_message(
        {"timeout": True, "timeout_secs": 20}
    ) is None


def test_extract_inbox_message_real_message_returns_dict():
    msg = {"activity_id": "x", "text": "hi", "peer_id": "", "kind": "canvas_user"}
    assert adapter_mod.MoleculeAdapter._extract_inbox_message(msg) == msg


def test_extract_inbox_message_garbage_returns_none():
    assert adapter_mod.MoleculeAdapter._extract_inbox_message("not a dict") is None
    assert adapter_mod.MoleculeAdapter._extract_inbox_message({}) is None


def test_extract_tool_payload_parses_json_text():
    raw = {"content": [{"type": "text", "text": '{"k": "v"}'}]}
    assert adapter_mod.MoleculeAdapter._extract_tool_payload(raw) == {"k": "v"}


def test_extract_tool_payload_falls_back_to_text():
    raw = {"content": [{"type": "text", "text": "ok"}]}
    assert adapter_mod.MoleculeAdapter._extract_tool_payload(raw) == "ok"


def test_check_molecule_requirements_missing_workspace_id(monkeypatch):
    monkeypatch.delenv("MOLECULE_WORKSPACE_ID", raising=False)
    assert adapter_mod.check_molecule_requirements() is False


def test_check_molecule_requirements_missing_workspace_token(monkeypatch):
    monkeypatch.setenv("MOLECULE_WORKSPACE_ID", "ws-test")
    monkeypatch.setenv("MOLECULE_PLATFORM_URL", "https://tenant.example")
    monkeypatch.delenv("MOLECULE_WORKSPACE_TOKEN", raising=False)
    assert adapter_mod.check_molecule_requirements() is False


def test_check_molecule_requirements_missing_platform_url(monkeypatch):
    monkeypatch.setenv("MOLECULE_WORKSPACE_ID", "ws-test")
    monkeypatch.setenv("MOLECULE_WORKSPACE_TOKEN", "test-workspace-credential")
    monkeypatch.delenv("MOLECULE_PLATFORM_URL", raising=False)
    assert adapter_mod.check_molecule_requirements() is False


def test_register_platform_install_hint_uses_canonical_runtime_distribution():
    """The gateway's actionable failure hint must not name the retired dist."""
    import hermes_channel_molecule

    captured = {}

    class Context:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    hermes_channel_molecule.register(Context())

    assert captured["required_env"] == [
        "MOLECULE_WORKSPACE_ID",
        "MOLECULE_WORKSPACE_TOKEN",
        "MOLECULE_PLATFORM_URL",
    ]
    assert "molecules-workspace-runtime" in captured["install_hint"]
    assert "molecule-ai-workspace-runtime" not in captured["install_hint"]
