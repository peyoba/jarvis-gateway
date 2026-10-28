"""共享配置、摘要状态与身份校验回归；不启动或停止真实进程。"""

import copy
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

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


@pytest.mark.parametrize("invalid_config", [
    '{"gateway":{"auth_token":"${JARVIS_TEST_REMOVED_SECRET}"}}',
    '{"gateway":{"port":0}}',
    '{"invalid":',
    None,
])
def test_stop_remains_available_with_invalid_or_removed_config(tmp_path, monkeypatch, invalid_config):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    main = launcher["main"]
    config = tmp_path / "config.json"
    if invalid_config is not None:
        config.write_text(invalid_config)
    monkeypatch.delenv("JARVIS_TEST_REMOVED_SECRET", raising=False)
    record = {"pid": 987654, "started": "test-start", "port": 28777}
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text(json.dumps(record))
    active = {"gateway": record}
    monkeypatch.setitem(main.__globals__, "RUN_DIR", tmp_path)
    monkeypatch.setitem(main.__globals__, "_managed_record", lambda name: active.get(name))
    monkeypatch.setitem(main.__globals__, "port_alive", lambda port: bool(active) and port == 28777)
    signals = []

    def terminate(pid, sig):
        signals.append(pid)
        active.clear()

    monkeypatch.setattr("os.killpg", terminate)
    assert main(["--config", str(config), "stop"]) == 0
    assert signals == [987654]
    assert not pid_file.exists()


def test_stop_uses_old_recorded_ports_after_config_change(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    stop = launcher["cmd_stop"]
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"]["port"] = 28888
    record = {"pid": 987654, "started": "test-start", "port": 28777}
    (tmp_path / "gateway.pid").write_text(json.dumps(record))
    active = {"gateway": record}
    monkeypatch.setitem(stop.__globals__, "RUN_DIR", tmp_path)
    monkeypatch.setitem(stop.__globals__, "_managed_record", lambda name: active.get(name))
    monkeypatch.setitem(stop.__globals__, "port_alive", lambda port: bool(active) and port == 28777)
    kill = Mock(side_effect=lambda *args: active.clear())
    monkeypatch.setattr("os.killpg", kill)
    assert stop(cfg) == 0
    assert kill.call_args.args[0] == record["pid"]


def test_stop_does_not_hide_an_unverified_old_listener(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    stop = launcher["cmd_stop"]
    (tmp_path / "gateway.pid").write_text(json.dumps({
        "pid": 987654, "started": "old-start", "port": 28777,
    }))
    monkeypatch.setitem(stop.__globals__, "RUN_DIR", tmp_path)
    monkeypatch.setitem(stop.__globals__, "pid_started", lambda pid: "reused-start")
    monkeypatch.setitem(stop.__globals__, "pid_command", lambda pid: "unrelated")
    monkeypatch.setitem(stop.__globals__, "port_alive", lambda port: port == 28777)
    kill = Mock()
    monkeypatch.setattr("os.killpg", kill)
    assert stop(copy.deepcopy(DEFAULT_CONFIG)) == 1
    kill.assert_not_called()
    assert (tmp_path / "gateway.pid").exists()


def test_stop_reports_signal_failure_and_preserves_record(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    stop = launcher["cmd_stop"]
    record = {"pid": 987654, "started": "test-start", "port": 28777}
    (tmp_path / "gateway.pid").write_text(json.dumps(record))
    monkeypatch.setitem(stop.__globals__, "RUN_DIR", tmp_path)
    monkeypatch.setitem(stop.__globals__, "_managed_record",
                        lambda name: record if name == "gateway" else None)
    monkeypatch.setitem(stop.__globals__, "port_alive", lambda port: True)
    monkeypatch.setattr("os.killpg", Mock(side_effect=PermissionError))
    assert stop(copy.deepcopy(DEFAULT_CONFIG)) == 1
    assert (tmp_path / "gateway.pid").exists()


@pytest.mark.parametrize("action", ["off", "status"])
def test_autostart_disable_and_status_do_not_require_startup_config(monkeypatch, action):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    main = launcher["main"]
    loader = Mock(side_effect=ValueError("configuration unavailable"))
    auto = Mock(return_value=0)
    monkeypatch.setitem(main.__globals__, "load_config", loader)
    monkeypatch.setitem(main.__globals__, "cmd_autostart", auto)
    assert main(["autostart", action]) == 0
    loader.assert_not_called()
    auto.assert_called_once_with(None, action)


def test_config_precedence_is_explicit_then_environment_then_project(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    loader = launcher["load_config"]
    configs = []
    for index in range(3):
        path = tmp_path / f"config-{index}.json"
        path.write_text(json.dumps({"gateway": {"port": 28770 + index}}))
        configs.append(path)
    project, environment, explicit = configs
    monkeypatch.setitem(loader.__globals__, "CONFIG_PATH", project)
    monkeypatch.setenv("JARVIS_CONFIG", str(environment))
    assert loader()["gateway"]["port"] == 28771
    assert loader(explicit)["gateway"]["port"] == 28772
    monkeypatch.delenv("JARVIS_CONFIG")
    assert loader()["gateway"]["port"] == 28770


def test_main_passes_selected_config_to_start(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    main = launcher["main"]
    explicit = tmp_path / "chosen.json"
    explicit.write_text('{"gateway":{"port":28777}}')
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "missing-env.json"))
    start = Mock(return_value=0)
    monkeypatch.setitem(main.__globals__, "cmd_start", start)
    assert main(["--config", str(explicit), "start"]) == 0
    assert start.call_args.args[0]["gateway"]["port"] == 28777
    assert start.call_args.kwargs["config_path"] == explicit
    assert main(["--config", str(tmp_path / "missing-explicit.json"), "start"]) == 1
    assert start.call_count == 1


def test_start_pins_config_in_gateway_child_environment(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    start = launcher["cmd_start"]
    config = tmp_path / "chosen.json"
    config.write_text('{"gateway":{"port":28777}}')
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"]["port"] = 28777
    for key in ("server_bin", "model"):
        path = tmp_path / key
        path.touch()
        cfg["whisper"][key] = str(path)
    spawned = []
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setitem(start.__globals__, "http_json", lambda *args: None)
    monkeypatch.setitem(start.__globals__, "port_alive", lambda *args: False)
    monkeypatch.setitem(start.__globals__, "wait_for_port", lambda *args: 0.01)
    monkeypatch.setitem(start.__globals__, "_record_process", Mock())
    monkeypatch.setitem(start.__globals__, "spawn",
                        lambda *args, **kwargs: spawned.append((args, kwargs)) or (process, tmp_path / "log"))
    monkeypatch.setenv("JARVIS_CONFIG", str(tmp_path / "other.json"))
    assert start(cfg, config_path=config) == 0
    assert spawned[1][1]["env"]["JARVIS_CONFIG"] == str(config)


def test_start_does_not_overwrite_a_managed_record_after_port_change(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(ROOT / "jarvis"))
    start = launcher["cmd_start"]
    config = tmp_path / "chosen.json"
    config.write_text("{}")
    monkeypatch.setitem(start.__globals__, "_managed_record",
                        lambda name: {"pid": 987654, "port": 28777} if name == "gateway" else None)
    spawn = Mock()
    monkeypatch.setitem(start.__globals__, "spawn", spawn)
    assert start(copy.deepcopy(DEFAULT_CONFIG), config_path=config) == 1
    spawn.assert_not_called()


def test_autostart_uses_selected_config_without_copying_secrets(tmp_path, monkeypatch):
    import plistlib

    launcher = runpy.run_path(str(ROOT / "jarvis"))
    auto = launcher["cmd_autostart"]
    config = tmp_path / "chosen.json"
    config.write_text('{"gateway":{"auth_token":"${JARVIS_TEST_SELECTED_SECRET}"}}')
    monkeypatch.setitem(auto.__globals__, "CONFIG_PATH", tmp_path / "unused.json")
    monkeypatch.setitem(auto.__globals__, "PLIST_PATH", tmp_path / "test.plist")
    monkeypatch.setitem(auto.__globals__, "LOG_DIR", tmp_path / "logs")
    run = Mock(return_value=Mock(returncode=0, stdout="synthetic-test-secret", stderr=""))
    monkeypatch.setattr("subprocess.run", run)
    assert auto(copy.deepcopy(DEFAULT_CONFIG), "on", config_path=config) == 0
    assert run.call_args_list[0].args[0] == ["launchctl", "getenv", "JARVIS_TEST_SELECTED_SECRET"]
    data = (tmp_path / "test.plist").read_bytes()
    assert b"synthetic-test-secret" not in data
    assert plistlib.loads(data)["EnvironmentVariables"]["JARVIS_CONFIG"] == str(config)
