"""Web 控制台的「开发板地址」面板：串口写入的排队、跳过与错误处理。"""

import copy
import http.client
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jarvis_gateway import ip_sync
from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server.board import BoardLink
from jarvis_gateway.server.server import UnifiedGatewayServer

TOKEN = "board-secret"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Recorder:
    """替身串口写入：记录被写入的地址，可配置成败与延迟。"""

    def __init__(self, ok=True, detail="cmd hatch.host -> ok", delay=0.0):
        self.ok, self.detail, self.delay = ok, detail, delay
        self.calls = []

    def __call__(self, ip, port=None, wait=4.0):
        self.calls.append(ip)
        if self.delay:
            time.sleep(self.delay)
        return self.ok, self.detail, "/dev/tty.usbmodem-test"


@pytest.fixture()
def run_dir():
    return Path(tempfile.mkdtemp(prefix="jarvis-board-"))


def test_snapshot_reports_state(run_dir):
    push = _Recorder()
    link = BoardLink(
        run_dir=run_dir, push=push, lan_ip=lambda: "192.168.0.102", find_port=lambda: "/dev/cu.usbmodem1"
    )
    ip_sync.write_applied("192.168.0.102", run_dir)

    snap = link.snapshot()
    assert snap["lan_ip"] == "192.168.0.102"
    assert snap["board_host"] == "192.168.0.102"
    assert snap["serial_port"] == "/dev/cu.usbmodem1"
    assert snap["busy"] is False
    assert snap["last"] == {}


def test_auto_sync_skips_serial_when_address_unchanged(run_dir):
    push = _Recorder()
    link = BoardLink(run_dir=run_dir, push=push, lan_ip=lambda: "10.0.0.7", find_port=lambda: "/dev/x")
    ip_sync.write_applied("10.0.0.7", run_dir)

    result = link.sync("")
    assert result["skipped"] is True
    assert push.calls == []  # 不碰串口就不会复位板子
    assert link.snapshot()["last"]["ok"] is True


def test_manual_host_is_pushed_and_persisted(run_dir):
    push = _Recorder()
    link = BoardLink(run_dir=run_dir, push=push, lan_ip=lambda: "10.0.0.7", find_port=lambda: "/dev/x")

    result = link.sync("192.168.1.50")
    assert result["started"] is True
    link.wait(2)

    snap = link.snapshot()
    assert push.calls == ["192.168.1.50"]
    assert snap["board_host"] == "192.168.1.50"
    assert snap["last"]["ok"] is True
    assert snap["busy"] is False


def test_invalid_host_is_rejected_before_touching_serial(run_dir):
    push = _Recorder()
    link = BoardLink(run_dir=run_dir, push=push, lan_ip=lambda: "10.0.0.7", find_port=lambda: "/dev/x")

    assert link.sync("not-an-ip")["error"] == "invalid_host"
    assert link.sync("192.168.0")["error"] == "invalid_host"
    assert push.calls == []


def test_failed_push_keeps_previous_state_file(run_dir):
    ip_sync.write_applied("10.0.0.7", run_dir)
    link = BoardLink(
        run_dir=run_dir, push=_Recorder(ok=False, detail="未找到串口：开发板没有插在这台 Mac 上"),
        lan_ip=lambda: "10.0.0.8", find_port=lambda: "",
    )

    assert link.sync("")["started"] is True
    link.wait(2)

    snap = link.snapshot()
    assert snap["last"]["ok"] is False
    assert "未找到串口" in snap["last"]["detail"]
    assert snap["board_host"] == "10.0.0.7"  # 没写成功就不该改状态


def test_busy_sync_is_refused(run_dir):
    slow = _Recorder(delay=0.3)
    link = BoardLink(run_dir=run_dir, push=slow, lan_ip=lambda: "10.0.0.9", find_port=lambda: "/dev/x")

    assert link.sync("")["started"] is True
    assert link.sync("")["error"] == "busy"
    link.wait(3)
    assert len(slow.calls) == 1


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #

PORT = _free_port()


@pytest.fixture(scope="module")
def gateway():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update({"host": "127.0.0.1", "port": PORT, "auth_token": TOKEN})
    server = UnifiedGatewayServer(cfg)
    server.board.run_dir = Path(tempfile.mkdtemp(prefix="jarvis-board-http-"))
    server.board._push = _Recorder()
    server.board._lan_ip = lambda: "192.168.0.102"
    server.board._find_port = lambda: "/dev/cu.usbmodem-test"

    errors = []

    def run():
        try:
            server.serve_forever()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if errors:
            pytest.fail(str(errors[0]))
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    yield server
    server.stop_services()
    thread.join(timeout=3)


def _call(method, path, body=None, token=TOKEN):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Jarvis-Token"] = token
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def test_board_endpoint_requires_token(gateway):
    status, _ = _call("GET", "/board", token="")
    assert status == 401


def test_board_snapshot_then_manual_write(gateway):
    status, payload = _call("GET", "/board")
    assert status == 200
    snap = json.loads(payload)
    assert snap["lan_ip"] == "192.168.0.102"
    assert snap["serial_port"] == "/dev/cu.usbmodem-test"

    status, payload = _call("POST", "/board", json.dumps({"host": "192.168.0.150"}))
    assert status == 200
    assert json.loads(payload)["started"] is True

    gateway.board.wait(3)
    status, payload = _call("GET", "/board")
    snap = json.loads(payload)
    assert snap["board_host"] == "192.168.0.150"
    assert snap["last"]["ok"] is True


def test_board_rejects_invalid_host_with_400(gateway):
    status, payload = _call("POST", "/board", json.dumps({"host": "192.168.0.999"}))
    assert status == 400
    assert json.loads(payload)["error"] == "invalid_host"


def test_board_rejects_broken_json(gateway):
    status, _ = _call("POST", "/board", "{not json")
    assert status == 400
