"""网关地址同步（ip_sync）的离线回归：不碰真实串口和网络。"""

import json
import subprocess
import sys
import types

import pytest

from jarvis_gateway import ip_sync


class FakeSerial:
    """最小串口替身：写入什么就回显什么，用于验证命令格式与判定逻辑。"""

    instances = []
    reply = b"cmd hatch.host=192.168.0.102 -> ok\r\n"
    raise_on_kwargs = False

    def __init__(self, port, baud, timeout=0.2, dtr=True, rts=True):
        if FakeSerial.raise_on_kwargs and (dtr is not True or rts is not True):
            raise TypeError("unexpected keyword arguments")
        self.port = port
        self.baud = baud
        self.written = b""
        self.closed = False
        self.dtr = dtr
        self.rts = rts
        self._buffer = b""
        FakeSerial.instances.append(self)

    def reset_input_buffer(self):
        self._buffer = b""

    def write(self, data):
        self.written += data
        self._buffer = FakeSerial.reply

    def flush(self):
        pass

    def read(self, size):
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    FakeSerial.instances = []
    FakeSerial.reply = b"cmd hatch.host=192.168.0.102 -> ok\r\n"
    FakeSerial.raise_on_kwargs = False
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakeSerial))
    monkeypatch.setattr(ip_sync, "find_serial_port", lambda: "/dev/cu.usbserial-test")
    return FakeSerial


def test_push_host_reports_success_and_command(fake_serial):
    ok, detail, port = ip_sync.push_host("192.168.0.102", wait=0.5)

    assert ok is True
    assert port == "/dev/cu.usbserial-test"
    assert "hatch.host" in detail
    assert fake_serial.instances[0].written == b">hatch.host=192.168.0.102\n"
    assert fake_serial.instances[0].closed is True


def test_push_host_falls_back_when_kwargs_unsupported(fake_serial):
    fake_serial.raise_on_kwargs = True

    ok, _detail, _port = ip_sync.push_host("192.168.0.102", wait=0.5)

    assert ok is True
    opened = fake_serial.instances[0]
    assert (opened.dtr, opened.rts) == (False, False)


def test_push_host_reports_failure_without_echo(fake_serial):
    fake_serial.reply = b"ERR unknown command\r\n"

    ok, detail, _port = ip_sync.push_host("192.168.0.102", wait=0.5)

    assert ok is False
    assert "ERR" in detail


def test_push_host_without_board(monkeypatch):
    monkeypatch.setattr(ip_sync, "find_serial_port", lambda: None)

    ok, detail, port = ip_sync.push_host("192.168.0.102")

    assert (ok, port) == (False, None)
    assert "串口" in detail


def test_push_host_reports_missing_pyserial(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)
    monkeypatch.setattr(ip_sync, "find_serial_port", lambda: "/dev/cu.usbserial-test")

    ok, detail, port = ip_sync.push_host("192.168.0.102")

    assert ok is False
    assert "pyserial" in detail
    assert port == "/dev/cu.usbserial-test"


def test_lan_ip_prefers_ipconfig(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        out = "192.168.0.102\n" if cmd[-1] == "en0" else ""
        return types.SimpleNamespace(stdout=out)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ip_sync.lan_ip() == "192.168.0.102"
    assert calls[0][:2] == ["ipconfig", "getifaddr"]


def test_lan_ip_falls_back_to_route_probe(monkeypatch):
    class FakeSocket:
        def connect(self, address):
            self.address = address

        def getsockname(self):
            return ("10.0.0.7", 51234)

        def close(self):
            self.closed = True

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=""))
    monkeypatch.setattr(ip_sync.socket, "socket", lambda *a, **k: FakeSocket())

    assert ip_sync.lan_ip() == "10.0.0.7"


def test_state_roundtrip_and_needs_push(tmp_path):
    run_dir = tmp_path / "run"

    assert ip_sync.read_applied(run_dir) == ""
    assert ip_sync.needs_push("192.168.0.102", run_dir) is True

    ip_sync.write_applied("192.168.0.102", run_dir)

    assert ip_sync.read_applied(run_dir) == "192.168.0.102"
    assert ip_sync.needs_push("192.168.0.102", run_dir) is False
    assert ip_sync.needs_push("192.168.0.117", run_dir) is True


def test_cli_skips_when_unchanged(tmp_path, monkeypatch, capsys):
    ip_sync.write_applied("192.168.0.102", tmp_path / "run")
    monkeypatch.setattr(ip_sync, "ROOT", tmp_path)
    monkeypatch.setattr(ip_sync, "lan_ip", lambda: "192.168.0.102")

    def boom(*args, **kwargs):
        raise AssertionError("地址没变时不应该打开串口")

    monkeypatch.setattr(ip_sync, "push_host", boom)

    assert ip_sync.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["port"] is None


def test_cli_force_pushes(monkeypatch, capsys):
    monkeypatch.setattr(ip_sync, "needs_push", lambda ip, run_dir=None: False)
    monkeypatch.setattr(ip_sync, "lan_ip", lambda: "192.168.0.102")
    monkeypatch.setattr(ip_sync, "push_host", lambda ip, port=None: (True, "cmd hatch.host=ok", port))
    monkeypatch.setattr(ip_sync, "write_applied", lambda ip, run_dir=None: None)

    assert ip_sync.main(["--force", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
