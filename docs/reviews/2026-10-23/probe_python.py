"""只读审查探针：识别、LLM、剪贴板与系统动作均使用模拟实现。"""

import copy
import http.client
import io
import json
import os
import runpy
import socket
import tempfile
import threading
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis_gateway.actions.injector import CursorInjector
from jarvis_gateway.audio.converter import pcm_to_wav_bytes, wav_bytes_to_pcm16
from jarvis_gateway.config import DEFAULT_CONFIG, ConfigError, load_config
from jarvis_gateway.server.server import GatewayHTTPServer, UnifiedGatewayServer
from jarvis_gateway.services.token_service import TokenService

RESULTS = []
REPO = Path(os.environ.get("REVIEW_REPO", Path(__file__).resolve().parents[3]))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def record(name, **values):
    RESULTS.append({"probe": name, **values})


def safe_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update(host="127.0.0.1", port=free_port(), auth_token="")
    cfg["data_service"]["opentoken_bin"] = "/nonexistent/review-opentoken"
    return cfg


@contextmanager
def fake_gateway():
    cfg = safe_config()
    with patch.dict(os.environ):
        os.environ.pop("JARVIS_AUTH_TOKEN", None)
        gateway = UnifiedGatewayServer(cfg, project_root="/tmp/jarvis-review")
    gateway.asr_manager = SimpleNamespace(transcribe=lambda p: ("普通听写文本", 0.0, "fake"))
    gateway.tts = SimpleNamespace(synthesize=lambda text: bytes(3200))
    gateway.polish_calls = []

    def polish(text):
        gateway.polish_calls.append(text)
        return "模拟润色：" + text

    gateway.llm_manager = SimpleNamespace(
        polish_text=polish, chat=lambda text: ("模拟回答", [])
    )
    httpd = GatewayHTTPServer(("127.0.0.1", 0), gateway.create_handler_class())
    gateway.port = httpd.server_port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def request(gateway, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=3)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    status = res.status
    conn.close()
    # record_turn 在响应写出后才执行，短暂等待后读取记录。
    time.sleep(0.01)
    return status, data


def metadata(data):
    size = int.from_bytes(data[:4], "big")
    return json.loads(data[4 : 4 + size])


def main():
    # 验证缺失的鉴权环境变量是否被静默展开为空。
    with tempfile.TemporaryDirectory(prefix="jarvis-review-config-") as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps({
            "gateway": {
                "host": "0.0.0.0",
                "auth_token": "${JARVIS_REVIEW_MISSING_TOKEN}",
            },
        }))
        with patch.dict(os.environ):
            os.environ.pop("JARVIS_REVIEW_MISSING_TOKEN", None)
            os.environ.pop("JARVIS_AUTH_TOKEN", None)
            try:
                load_config(path)
            except ConfigError as exc:
                record("missing_env_fails_closed", rejected=True,
                       error_type=type(exc).__name__)
            else:
                record("missing_env_fails_closed", rejected=False)

    # 只使用 172 字节合法 WAV，验证解码膨胀，不构造大音频。
    raw = bytes(128)
    wav = pcm_to_wav_bytes(raw, sample_rate=1)
    try:
        decoded = wav_bytes_to_pcm16(wav)
    except ValueError as exc:
        record("wav_amplification_rejected", input_bytes=len(wav),
               rejected=True, error_type=type(exc).__name__)
    else:
        record("wav_amplification_rejected", input_bytes=len(wav),
               rejected=False, decoded_bytes=len(decoded))
        del decoded

    with fake_gateway() as gateway, patch.object(CursorInjector, "inject_text", return_value=True):
        gateway.state.set_mode("raw")
        for heard in (
            "打字速度比以前快了",
            "整理一下今天的会议纪要",
            "谢谢大家的帮助，我明天继续整理文档",
            "好",
        ):
            gateway.asr_manager.transcribe = lambda p, h=heard: (h, 0, "fake")
            gateway.polish_calls.clear()
            status, data = request(gateway, "POST", "/chat?mode=raw", bytes(32000))
            record("raw_router_" + heard, status=status,
                   metadata=metadata(data), polish_calls=list(gateway.polish_calls))

        status, data = request(gateway, "POST", "/toggle-mode", b"")
        record("documented_post_toggle", status=status, body=data.decode())

        status, data = request(gateway, "POST", "/mode?set=dialog", b"", headers={
            "Origin": "https://review-attacker.invalid",
        })
        record("origin_only_mutation", status=status, mode=gateway.state.mode)

        status, data = request(gateway, "POST", "/mode?set=raw", b"", headers={
            "Origin": "http://127.0.0.1:12345",
            "Sec-Fetch-Site": "same-site",
        })
        record("same_site_different_origin_mutation", status=status, mode=gateway.state.mode)
        status, data = request(gateway, "POST", "/mode", b"mode=dialog", headers={
            "Origin": "https://review-attacker.invalid",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        record("cross_origin_form_without_fetch_metadata", status=status, mode=gateway.state.mode)

        gateway.asr_manager.transcribe = lambda p: ("普通听写文本", 0, "fake")
        with patch.object(CursorInjector, "inject_text", return_value=False):
            status, data = request(gateway, "POST", "/chat?mode=raw", bytes(32000))
        record("injection_failure_is_reported_as_error", status=status,
               metadata=metadata(data), recorded=gateway.state._recent_turns[0])

    # 验证较慢的第一段润色请求是否被第二段短录音超车。
    with fake_gateway() as gateway:
        count = iter(("第一段听写内容", "第二段听写内容"))
        gateway.asr_manager.transcribe = lambda p: (next(count), 0, "fake")
        entered = threading.Event()
        release = threading.Event()
        injected = []
        first_results = []

        def delayed_polish(text):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("review synchronization timeout")
            return text

        gateway.llm_manager.polish_text = delayed_polish
        with patch.object(CursorInjector, "inject_text",
                          side_effect=lambda text: injected.append(text) or True):
            worker = threading.Thread(target=lambda: first_results.append(
                request(gateway, "POST", "/chat?mode=auto", bytes(96000))
            ))
            worker.start()
            assert entered.wait(2)
            second_status, _ = request(gateway, "POST", "/chat?mode=auto", bytes(32000))
            release.set()
            worker.join(timeout=3)
            assert not worker.is_alive()
        record("parallel_requests_are_rejected", second_status=second_status, injected_order=injected)

    # 剪贴板操作完全模拟，不调用原生系统命令。
    clipboard = {"value": b"original"}
    saved = (
        CursorInjector._original_clipboard,
        CursorInjector._restore_timer,
    )
    try:
        CursorInjector._original_clipboard = None
        CursorInjector._restore_timer = None
        CursorInjector._injected_clipboard = None

        def write(data):
            clipboard["value"] = data
            return True

        with patch.object(CursorInjector, "_clipboard_has_text", return_value=True), \
             patch.object(CursorInjector, "_read_clipboard", side_effect=lambda: clipboard["value"]), \
             patch.object(CursorInjector, "_write_clipboard", side_effect=write), \
             patch.object(CursorInjector, "_press_paste", return_value=True), \
             patch("jarvis_gateway.actions.injector.subprocess.Popen"):
            CursorInjector.inject_text("review fake text", restore_delay_sec=10)
            CursorInjector._restore_timer.cancel()
            clipboard["value"] = b"user copied something new"
            CursorInjector._do_restore()
        record("clipboard_restore_preserves_new_copy", actual=clipboard["value"].decode())
    finally:
        if CursorInjector._restore_timer:
            CursorInjector._restore_timer.cancel()
        CursorInjector._original_clipboard, CursorInjector._restore_timer = saved
        CursorInjector._injected_clipboard = None

    # 模拟已安装但失效的 OpenToken，检查本地统计是否仍可降级。
    with patch("jarvis_gateway.services.token_service.shutil.which", return_value=None):
        token_service = TokenService(opentoken_bin="/nonexistent/review-opentoken")
    token_service.opentoken_bin = "/fake/installed-but-failing-opentoken"
    token_service.add_local_tokens(100)
    record("opentoken_falls_back_to_local_estimate", total=token_service.get_snapshot()["total"])

    # 启动器只使用模拟进程与临时 PID 目录，不启动或停止真实服务。
    launcher = runpy.run_path(str(REPO / "jarvis"))
    start = launcher["cmd_start"]
    stop = launcher["stop_port"]
    with tempfile.TemporaryDirectory(prefix="jarvis-review-launcher-") as temp, \
         patch("os.killpg") as killpg:
        root = Path(temp)
        for name in ("server", "model"):
            (root / name).touch()
        cfg = safe_config()
        cfg["gateway"]["port"] = 39001
        cfg["whisper"].update(server_bin=str(root / "server"), model=str(root / "model"))
        fake_proc = SimpleNamespace(pid=123456, poll=lambda: None, wait=lambda **kwargs: 1)
        # 所有启动器探针共享隔离目录；不能写真实 run/ 或发出真实信号。
        isolated = {
            "RUN_DIR": root / "run",
            "port_alive": lambda *a: False,
            "pid_started": lambda *a: "fake-start-time",
        }
        with patch.dict(start.__globals__, {**isolated,
            "http_json": lambda *a: None,
            "stop_port": lambda *a: False,
            "spawn": lambda *a, **kw: (fake_proc, root / "fake.log"),
            "RUN_DIR": root / "run",
            "wait_for_port": lambda port, *a, **kw: 0.1 if port == 39001 else None,
        }):
            rc = start(cfg, wait=True)
        record("launcher_whisper_failure_exit", returncode=rc)

        killpg.reset_mock()
        with patch.dict(start.__globals__, {**isolated,
            "http_json": lambda *a: None,
            "stop_port": lambda *a: False,
            "spawn": lambda *a, **kw: (
                SimpleNamespace(
                    pid=123456, poll=lambda: None, wait=lambda **kwargs: 1,
                ),
                root / "fake.log",
            ),
            "RUN_DIR": root / "run",
            "wait_for_port": lambda *a, **kw: None,
        }):
            rc = start(cfg, wait=True)
        record("launcher_gateway_failure_rolls_back", returncode=rc,
               terminate_calls=killpg.call_count,
               remaining_pid_files=len(list((root / "run").glob("*.pid"))))

        spawned = []
        with patch.dict(start.__globals__, {**isolated,
            "http_json": lambda *a: {"root": str(start.__globals__["ROOT"])},
            "port_alive": lambda *a: False,
            "spawn": lambda *a, **kw: spawned.append(True) or (fake_proc, root / "fake.log"),
            "wait_for_port": lambda *a, **kw: 0.1,
        }):
            rc = start(cfg, wait=True)
        record("launcher_existing_gateway_missing_whisper", returncode=rc, spawns=len(spawned))

    kill_calls = []
    pid_snapshots = iter(([123456], []))
    with patch.dict(stop.__globals__, {
        "RUN_DIR": Path("/nonexistent/jarvis-review-run"),
        "port_pids": lambda p: next(pid_snapshots, []),
        "pid_command": lambda p: "",
        "port_alive": lambda p: True,
    }), patch("os.kill", side_effect=lambda p, sig: kill_calls.append((p, int(sig)))), \
         patch("os.killpg", side_effect=lambda p, sig: kill_calls.append((p, int(sig)))):
        stop(39001, "fake unrelated listener")
    record("launcher_preserves_unidentified_listener", kill_signals=kill_calls)

    # 仅对探针自己的随机端口验证 shutdown 是否释放监听套接字。
    gateway = UnifiedGatewayServer(safe_config())
    gateway.start_services = lambda: None
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    for _ in range(200):
        if gateway.main_httpd:
            break
        time.sleep(0.01)
    port = gateway.main_httpd.server_port
    gateway.stop_services()
    thread.join(timeout=2)
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        port_reusable = True
    except OSError:
        port_reusable = False
    record("shutdown_releases_listener", thread_stopped=not thread.is_alive(),
           port_reusable=port_reusable)
    gateway.main_httpd.server_close()
    assert not thread.is_alive()


def verify_results():
    by_name = {result["probe"]: result for result in RESULTS}
    assert by_name["missing_env_fails_closed"]["rejected"]
    assert by_name["wav_amplification_rejected"]["rejected"]
    for name, result in by_name.items():
        if name.startswith("raw_router_"):
            assert result["status"] == 200
            assert result["metadata"]["typed"] == name.removeprefix("raw_router_")
            assert result["polish_calls"] == []
    assert by_name["documented_post_toggle"]["status"] == 200
    for name in ("origin_only_mutation", "same_site_different_origin_mutation",
                 "cross_origin_form_without_fetch_metadata"):
        assert by_name[name]["status"] == 403
    failure = by_name["injection_failure_is_reported_as_error"]
    assert failure["metadata"]["typed"] == ""
    assert failure["metadata"]["kind"] == failure["recorded"]["kind"] == "error"
    parallel = by_name["parallel_requests_are_rejected"]
    assert parallel["second_status"] == 429
    assert parallel["injected_order"] == ["第一段听写内容"]
    assert by_name["clipboard_restore_preserves_new_copy"]["actual"] == "user copied something new"
    assert by_name["opentoken_falls_back_to_local_estimate"]["total"] == 100
    assert by_name["launcher_whisper_failure_exit"]["returncode"] == 1
    rollback = by_name["launcher_gateway_failure_rolls_back"]
    assert rollback["returncode"] == 1 and rollback["terminate_calls"] == 2
    assert rollback["remaining_pid_files"] == 0
    restart = by_name["launcher_existing_gateway_missing_whisper"]
    assert restart["returncode"] == 0 and restart["spawns"] == 1
    assert by_name["launcher_preserves_unidentified_listener"]["kill_signals"] == []
    shutdown = by_name["shutdown_releases_listener"]
    assert shutdown["thread_stopped"] and shutdown["port_reusable"]


if __name__ == "__main__":
    # 隐藏模拟样本文本的过程日志，只输出结构化证据。
    with redirect_stdout(io.StringIO()):
        main()
        verify_results()
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
