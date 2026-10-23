"""Integration tests for HTTP server routes."""

import copy
import json
import threading
import time
import urllib.parse
import urllib.request

from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server.server import UnifiedGatewayServer


def test_server_routes():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    # Use distinct test ports
    cfg["gateway"]["port"] = 19998
    cfg["gateway"]["host"] = "127.0.0.1"
    cfg["gateway"]["enable_legacy_data_port"] = True
    cfg["data_service"]["port"] = 19999
    cfg["gateway"]["auth_token"] = "secret123"

    server = UnifiedGatewayServer(cfg, project_root="/tmp/jarvis-test-root")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Wait for server to bind
    time.sleep(0.5)
    base_url = "http://127.0.0.1:19998"

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
            assert data["data"]["port"] == 19999

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

        # 5. Token endpoints (available directly on 19998)
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

        # 6. Legacy port (19999) serving the exact same cursor-status
        with urllib.request.urlopen("http://127.0.0.1:19999/cursor-status") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["cursor_running"] is False

    finally:
        server.stop_services()
