"""共享配置、摘要状态与身份校验回归；不启动或停止真实进程。"""

import copy
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

from jarvis_gateway.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_loads_shared_config_and_environment(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    loader = launcher["load_config"]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"gateway": {
        "port": 28888, "auth_token": "${JARVIS_TEST_LAUNCHER}",
    }}), encoding="utf-8")
    monkeypatch.setitem(loader.__globals__, "CONFIG_PATH", config)
    monkeypatch.setenv("JARVIS_TEST_LAUNCHER", "test-shared-token")
    original_path = sys.path[:]
    try:
        cfg = loader()
    finally:
        sys.path[:] = original_path
    assert cfg["gateway"]["port"] == 28888
    assert cfg["gateway"]["auth_token"] == "test-shared-token"


def test_launcher_status_uses_custom_port_and_summary_auth(monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    status = launcher["cmd_status"]
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update(port=28888, auth_token="test-only")
    requester = Mock(side_effect=[
        {"status": "ready"},
        {"gateway": {"pid": 1, "uptime": 1, "mode": "raw"},
         "data": {}, "stats": {"turns": 0}},
    ])
    monkeypatch.setitem(status.__globals__, "http_json", requester)
    monkeypatch.setitem(status.__globals__, "port_pids", lambda *args: [])
    assert status(cfg) == 0
    assert requester.call_args_list[0].args == ("http://127.0.0.1:28888/ping",)
    assert requester.call_args_list[1].args == ("http://127.0.0.1:28888/status?summary=1",)
    assert requester.call_args_list[1].kwargs["token"] == "test-only"


def test_pid_reuse_prevents_stop(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    stop = launcher["stop_port"]
    (tmp_path / "gateway.pid").write_text(json.dumps({
        "pid": 123456, "started": "old-start", "port": 28888,
    }))
    monkeypatch.setitem(stop.__globals__, "RUN_DIR", tmp_path)
    monkeypatch.setitem(stop.__globals__, "pid_started", lambda pid: "new-start")
    monkeypatch.setitem(stop.__globals__, "pid_command", lambda pid: "unrelated")
    monkeypatch.setitem(stop.__globals__, "port_alive", lambda port: True)
    kill = Mock()
    monkeypatch.setattr("os.killpg", kill)
    assert not stop(28888, "测试监听者")
    kill.assert_not_called()
