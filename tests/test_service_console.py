"""Web 控制台的「服务控制」面板：启动/停止/重启、登录自启、日志尾巴。

这里不真的去动 launchd 或网关进程：spawn 与 launchctl 都被替换成替身，
断言的是「控制台发出去的请求」和「状态快照」。
"""

import copy
import http.client
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server import handler as handler_module
from jarvis_gateway.server.server import UnifiedGatewayServer
from jarvis_gateway.server.service import ServiceControl

TOKEN = "service-secret"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = _free_port()


class _Proc:
    """假的子进程：可以一直卡在 wait() 上，用来模拟「操作进行中」。"""

    def __init__(self, gate=None):
        self.gate = gate
        self.finished = False

    def wait(self, timeout=None):
        if self.gate is not None:
            self.gate.wait(timeout=5)
        self.finished = True
        return 0


class _Spawner:
    """记录被派发的启动器命令。"""

    def __init__(self, block=None):
        self.calls = []
        self.procs = []
        self.block = block

    def __call__(self, cmd, log_path):
        self.calls.append([str(part) for part in cmd])
        proc = _Proc(self.block)
        self.procs.append(proc)
        return True, "", proc


class _Launchctl:
    def __init__(self, loaded=False):
        self.loaded = loaded
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))

        class _Result:
            returncode = 0 if self.loaded else 113
            stdout = ""
            stderr = ""

        return _Result()


@pytest.fixture()
def control(tmp_path):
    spawner = _Spawner()
    ctl = ServiceControl(
        str(tmp_path),
        spawn=spawner,
        launchctl=_Launchctl(loaded=False),
        launcher=tmp_path / "jarvis",
        python=tmp_path / "venv" / "bin" / "python",
        plist=tmp_path / "ai.jarvis.gateway.plist",
    )
    ctl.spawner = spawner
    return ctl


def test_action_dispatches_launcher_verb(control):
    result = control.action("restart")

    assert result["started"] is True
    assert result["action"] == "restart"
    assert control.spawner.calls[0][1:] == [str(control.launcher), "restart"]


def test_action_rejects_unknown_verb(control):
    result = control.action("rm -rf /")

    assert result["error"] == "unknown_action"
    assert control.spawner.calls == []


def test_second_action_while_busy_returns_busy(control):
    blocking = _Spawner(block=threading.Event())
    control._spawn = blocking
    assert control.action("stop")["started"] is True

    assert control.action("start")["error"] == "busy"
    assert control.snapshot()["busy"] is True

    blocking.block.set()
    deadline = time.monotonic() + 3
    while control.snapshot()["busy"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert control.snapshot()["busy"] is False


def test_autostart_toggle_uses_autostart_subcommand(control):
    control.action("autostart_on")
    control.action("autostart_off")

    verbs = [call[1:] for call in control.spawner.calls]
    assert verbs[0][-2:] == ["autostart", "on"]
    assert verbs[1][-2:] == ["autostart", "off"]


def test_autostart_snapshot_follows_plist_and_launchctl(control):
    assert control.autostart()["enabled"] is False
    assert control.autostart()["loaded"] is False

    control.plist_path.write_text("<plist/>", encoding="utf-8")
    control._launchctl = _Launchctl(loaded=True)
    control._cache["data"] = None

    snap = control.autostart()
    assert snap["enabled"] is True
    assert snap["loaded"] is True
    assert snap["label"] == "ai.jarvis.gateway"


def test_log_tail_reads_last_lines(control):
    log_dir = control.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "control.log").write_text(
        "\n".join("line %d" % i for i in range(50)), encoding="utf-8"
    )

    payload = control.log_tail("control", 5)
    assert payload["lines"] == ["line %d" % i for i in range(45, 50)]


def test_log_tail_rejects_unknown_name(control):
    assert control.log_tail("../passwd", 10)["error"] == "unknown_log"


def test_log_tail_reports_missing_file(control):
    payload = control.log_tail("gateway", 10)
    assert payload["empty"] is True
    assert payload["lines"] == []


# --------------------------------------------------------------------------- #
# HTTP 层：令牌、快照、动作、日志
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def gateway():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update({"host": "127.0.0.1", "port": PORT, "auth_token": TOKEN})
    server = UnifiedGatewayServer(cfg)

    root = Path(tempfile.mkdtemp(prefix="jarvis-service-http-"))
    spawner = _Spawner()
    server.service = ServiceControl(
        str(root),
        spawn=spawner,
        launchctl=_Launchctl(loaded=False),
        launcher=root / "jarvis",
        python=root / "venv" / "bin" / "python",
        plist=root / "ai.jarvis.gateway.plist",
    )
    server.service.spawner = spawner

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


def test_service_requires_token(gateway):
    assert _call("GET", "/service", token="")[0] == 401
    assert _call("POST", "/service", json.dumps({"action": "restart"}), token="")[0] == 401


def test_service_snapshot_shape(gateway):
    status, payload = _call("GET", "/service")
    assert status == 200
    snap = json.loads(payload)
    assert snap["pid"] > 0
    assert snap["port"] == PORT
    assert snap["busy"] is False
    assert snap["last"] == {}
    assert snap["autostart"]["enabled"] is False


def test_service_restart_is_accepted(gateway):
    status, payload = _call("POST", "/service", json.dumps({"action": "restart"}))
    assert status == 202
    body = json.loads(payload)
    assert body["started"] is True
    assert body["action"] == "restart"
    assert gateway.service.spawner.calls[-1][-1] == "restart"


def test_service_rejects_unknown_action(gateway):
    status, payload = _call("POST", "/service", json.dumps({"action": "explode"}))
    assert status == 400
    assert json.loads(payload)["error"] == "unknown_action"


def test_service_rejects_broken_json(gateway):
    assert _call("POST", "/service", "{not json")[0] == 400
    assert _call("POST", "/service", json.dumps({}))[0] == 400


def test_service_autostart_toggle(gateway):
    status, payload = _call("POST", "/service", json.dumps({"autostart": True}))
    assert status == 202
    assert json.loads(payload)["action"] == "autostart_on"

    status, payload = _call("POST", "/service", json.dumps({"autostart": False}))
    assert status == 202
    assert json.loads(payload)["action"] == "autostart_off"


def test_logs_endpoint_returns_tail(gateway):
    log_dir = gateway.service.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "jarvis_gateway.log").write_text("hello\nworld\n", encoding="utf-8")

    status, payload = _call("GET", "/logs?name=gateway&lines=1")
    assert status == 200
    data = json.loads(payload)
    assert data["lines"] == ["world"]


def test_logs_endpoint_rejects_unknown_name(gateway):
    status, payload = _call("GET", "/logs?name=../../etc/passwd")
    assert status == 400
    assert json.loads(payload)["error"] == "unknown_log"


def test_logs_requires_token(gateway):
    assert _call("GET", "/logs?name=gateway", token="")[0] == 401


def test_open_targets_are_whitelisted(gateway, monkeypatch):
    calls = []

    class _Stub:
        @staticmethod
        def run(cmd, **kwargs):
            calls.append(cmd)

    monkeypatch.setattr(handler_module, "subprocess", _Stub)

    status, payload = _call("POST", "/service", json.dumps({"open": "logs"}))
    assert status == 200
    assert json.loads(payload)["opened"] == "logs"
    assert calls[0][0] == "open"

    status, payload = _call("POST", "/service", json.dumps({"open": "terminal"}))
    assert status == 400
    assert json.loads(payload)["error"] == "unknown_target"
