"""Integration tests for HTTP server routes."""

import copy
import json
import socket
import threading
import time
import urllib.parse
import urllib.request

from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server.server import UnifiedGatewayServer


def free_port():
    """Ask the OS for an unused loopback port (CI runners may reserve fixed ports)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port, timeout=10.0, startup_errors=None):
    """Wait until the in-process server is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if startup_errors:
            raise AssertionError(f"gateway failed to start: {startup_errors[0]!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"gateway did not start listening on 127.0.0.1:{port} within {timeout}s")


def test_server_routes():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    # Use distinct test ports
    main_port = free_port()
    legacy_port = free_port()
    cfg["gateway"]["port"] = main_port
    cfg["gateway"]["host"] = "127.0.0.1"
    cfg["gateway"]["enable_legacy_data_port"] = True
    cfg["data_service"]["port"] = legacy_port
    cfg["gateway"]["auth_token"] = "secret123"

    server = UnifiedGatewayServer(cfg, project_root="/tmp/jarvis-test-root")
    startup_errors = []

    def run():
        try:
            server.serve_forever()
        except Exception as exc:  # pragma: no cover - only surfaced on failure
            startup_errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Wait for server to bind
    wait_for_port(main_port, startup_errors=startup_errors)
    base_url = f"http://127.0.0.1:{main_port}"

    try:
        # 1. Ping endpoint
        with urllib.request.urlopen(f"{base_url}/ping") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "ready"
            assert data["version"] == "2.3.0"
            assert data["root"] == "/tmp/jarvis-test-root"

        # 2. Status endpoint (requires auth because it may contain dictation text)
        status_req = urllib.request.Request(
            f"{base_url}/status", headers={"Authorization": "Bearer secret123"}
        )
        with urllib.request.urlopen(status_req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert "gateway" in data
            assert data["gateway"]["online"] is True
            assert data["data"]["legacy_enabled"] is True
            assert data["data"]["port"] == legacy_port

        # 3. Auth protected endpoint without token -> 401
        try:
            req = urllib.request.Request(f"{base_url}/mode?set=raw")
            urllib.request.urlopen(req)
            assert False, "Should have raised 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401

        # 4. Auth protected endpoint with Bearer token -> 200
        req = urllib.request.Request(
            f"{base_url}/mode?set=raw",
            headers={"Authorization": "Bearer secret123"},
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["current_mode"] == "raw"

        # 5. Token endpoints (available directly on the main port)
        with urllib.request.urlopen(f"{base_url}/cursor-status") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["cursor_running"] is False
            assert "today_tokens" in data

        with urllib.request.urlopen(f"{base_url}/deepseek-balance") as resp:
            data = json.loads(resp.read().decode())
            assert data["is_available"] is False
            assert data["balance_infos"] == []
            assert data["balance_str"] == "--"

        # 6. Legacy port serving the exact same cursor-status
        with urllib.request.urlopen(f"http://127.0.0.1:{legacy_port}/cursor-status") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["cursor_running"] is False

    finally:
        server.stop_services()
