"""Security and protocol hardening tests (no audio is ever injected)."""

import copy
import http.client
import io
import json
import socket
import struct
import threading
import time
import wave
from types import SimpleNamespace

import pytest

from jarvis_gateway.actions.injector import CursorInjector
from jarvis_gateway.actions.system import SystemActionExecutor
from jarvis_gateway.asr.manager import is_hallucination
from jarvis_gateway.audio.converter import pcm_to_wav_bytes, wav_bytes_to_pcm16
from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server.server import UnifiedGatewayServer

TOKEN = "secret123"


def free_port():
    """Ask the OS for an unused loopback port (CI runners may reserve fixed ports)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()


def wait_for_port(port, timeout=10.0, startup_errors=None):
    """Wait until the in-process server is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if startup_errors:
            pytest.fail(f"gateway failed to start: {startup_errors[0]!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail(f"gateway did not start listening on 127.0.0.1:{port} within {timeout}s")


@pytest.fixture(scope="module")
def gateway():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update({
        "host": "127.0.0.1",
        "port": PORT,
        "auth_token": TOKEN,
        "enable_legacy_data_port": False,
    })
    server = UnifiedGatewayServer(cfg)
    startup_errors = []

    def run():
        try:
            server.serve_forever()
        except Exception as exc:  # pragma: no cover - only surfaced on failure
            startup_errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_for_port(PORT, startup_errors=startup_errors)
    yield server
    server.stop_services()
    thread.join(timeout=3)
    assert not thread.is_alive()


def request(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def test_status_is_not_readable_cross_origin(gateway):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("GET", "/status", headers={
        "Origin": "https://evil.example",
        "Authorization": f"Bearer {TOKEN}",
    })
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Access-Control-Allow-Origin") is None
    resp.read()
    conn.close()


def test_status_requires_auth_when_configured(gateway):
    status, _ = request("GET", "/status")
    assert status == 401


def test_dns_rebinding_host_is_rejected(gateway):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.putrequest("GET", "/status", skip_host=True)
    conn.putheader("Host", "evil.example.com")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 403
    resp.read()
    conn.close()


def test_cross_site_mutation_is_rejected(gateway):
    status, _ = request(
        "POST", "/mode?set=raw", b"",
        headers={"X-Jarvis-Token": TOKEN, "Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert gateway.state.mode != "raw"


def test_origin_only_and_same_site_different_origin_are_rejected(gateway):
    status, _ = request(
        "POST", "/mode?set=dialog", b"",
        headers={"X-Jarvis-Token": TOKEN, "Origin": "https://evil.example"},
    )
    assert status == 403
    status, _ = request(
        "POST", "/mode", b"mode=dialog",
        {"X-Jarvis-Token": TOKEN, "Origin": f"http://127.0.0.1:{PORT + 1}"},
    )
    assert status == 403


def test_post_toggle_mode_is_supported(gateway):
    status, body = request("POST", "/toggle-mode", b"", {"X-Jarvis-Token": TOKEN})
    assert status == 200
    assert json.loads(body)["mode"] in {"auto", "raw", "polish", "dialog"}


def test_summary_status_omits_recent_text(gateway):
    gateway.state.record_turn("测试私密正文", "测试回复", "测试键入", "raw", 0, "ok")
    status, body = request("GET", "/status?summary=1")
    assert status == 200
    payload = json.loads(body)
    assert set(payload) == {"gateway", "whisper", "data", "stats"}
    assert "root" not in payload["gateway"]
    assert "测试私密正文" not in body.decode()


def test_get_mutations_are_method_not_allowed(gateway):
    before = gateway.state.mode
    for path in ("/mode?set=dialog", "/toggle-mode"):
        status, _ = request("GET", path, headers={"X-Jarvis-Token": TOKEN})
        assert status == 405
        assert gateway.state.mode == before


@pytest.mark.parametrize("origin", ["null", "file://", "http://localhost:1234"])
def test_null_and_foreign_origins_are_rejected(gateway, origin):
    status, _ = request("POST", "/mode?set=auto", b"", {
        "X-Jarvis-Token": TOKEN, "Origin": origin,
    })
    assert status == 403


def test_same_origin_post_is_allowed(gateway):
    status, _ = request("POST", "/mode?set=auto", b"", {
        "X-Jarvis-Token": TOKEN, "Origin": f"http://127.0.0.1:{PORT}",
    })
    assert status == 200


def _wire_meta(body):
    size = struct.unpack("!I", body[:4])[0]
    return json.loads(body[4 : 4 + size])


def test_real_audio_route_preserves_raw_text_and_failure_metadata(gateway, monkeypatch):
    monkeypatch.setattr(
        gateway.asr_manager,
        "transcribe",
        lambda path: ("打字速度比以前快了", 0.01, "fake"),
    )
    monkeypatch.setattr(gateway.tts, "synthesize", lambda text: bytes(3200))
    monkeypatch.setattr(CursorInjector, "inject_text", lambda text: True)
    status, body = request(
        "POST", "/chat?mode=raw", bytes(32000), {"X-Jarvis-Token": TOKEN}
    )
    meta = _wire_meta(body)
    assert status == 200
    assert meta["typed"] == "打字速度比以前快了"
    assert meta["kind"] == "ok"

    monkeypatch.setattr(CursorInjector, "inject_text", lambda text: False)
    status, body = request(
        "POST", "/chat?mode=raw", bytes(32000), {"X-Jarvis-Token": TOKEN}
    )
    meta = _wire_meta(body)
    assert status == 200
    assert meta["typed"] == ""
    assert meta["kind"] == "error"
    assert gateway.state._recent_turns[0]["kind"] == "error"


@pytest.mark.parametrize(("mode", "heard", "seconds", "typed", "polish", "chat", "actions"), [
    ("auto", "普通口述", 1, "普通口述", False, False, []),
    ("auto", "较长口述", 3, "润色结果", True, False, []),
    ("polish", "需要润色的口述", 1, "润色结果", True, False, []),
    ("raw", "整理今天的会议纪要", 1, "整理今天的会议纪要", False, False, []),
    ("raw", "润色：需要整理的口述", 1, "润色结果", True, False, []),
    ("polish", "原字：保持原样", 1, "保持原样", False, False, []),
    ("dialog", "原字：保持原样", 1, "保持原样", False, False, []),
    ("dialog", "解释量子计算", 1, "", False, True, []),
    ("raw", "贾维斯，锁屏", 1, "", False, False, ["lock_screen"]),
])
def test_audio_intent_contracts(gateway, monkeypatch, mode, heard, seconds, typed, polish, chat, actions):
    calls = {"polish": [], "chat": [], "injected": [], "actions": []}
    monkeypatch.setattr(gateway.asr_manager, "transcribe", lambda path: (heard, 0, "fake"))
    monkeypatch.setattr(gateway.tts, "synthesize", lambda text: bytes(3200))
    monkeypatch.setattr(gateway.llm_manager, "polish_text",
                        lambda text: calls["polish"].append(text) or "润色结果")
    monkeypatch.setattr(gateway.llm_manager, "chat",
                        lambda text: (calls["chat"].append(text) or "模拟回答", []))
    monkeypatch.setattr(CursorInjector, "inject_text",
                        lambda text: calls["injected"].append(text) or True)
    monkeypatch.setattr(SystemActionExecutor, "execute_action",
                        lambda action: calls["actions"].append(action))
    status, body = request("POST", f"/chat?mode={mode}", bytes(seconds * 32000),
                           {"X-Jarvis-Token": TOKEN})
    meta = _wire_meta(body)
    assert status == 200
    assert meta["kind"] == "ok"
    assert meta["typed"] == typed
    assert bool(calls["polish"]) == polish
    assert bool(calls["chat"]) == chat
    assert calls["injected"] == ([typed] if typed else [])
    assert calls["actions"] == actions
    assert meta["speak"] == (not typed)


@pytest.mark.parametrize(("heard", "provider", "kind", "error"), [
    ("", "fake", "miss", "not_recognized"),
    ("", "none", "error", "asr_unavailable"),
])
def test_asr_miss_is_distinct_from_backend_failure(gateway, monkeypatch, heard, provider, kind, error):
    monkeypatch.setattr(gateway.asr_manager, "transcribe", lambda path: (heard, 0, provider))
    monkeypatch.setattr(gateway.tts, "synthesize", lambda text: bytes(3200))
    status, body = request("POST", "/chat", bytes(32000), {"X-Jarvis-Token": TOKEN})
    meta = _wire_meta(body)
    assert status == 200
    assert meta["typed"] == ""
    assert (meta["kind"], meta["error"]) == (kind, error)


def test_internal_errors_do_not_log_exception_text(gateway, monkeypatch, capsys):
    def failed_asr(path):
        raise RuntimeError("synthetic-sensitive-exception-body")

    monkeypatch.setattr(gateway.asr_manager, "transcribe", failed_asr)
    before = gateway.state._stats["errors"]
    status, body = request("POST", "/chat", bytes(32000), {"X-Jarvis-Token": TOKEN})
    assert status == 500
    assert b"synthetic-sensitive-exception-body" not in body
    assert "synthetic-sensitive-exception-body" not in capsys.readouterr().out
    assert gateway.state._stats["errors"] == before + 1


@pytest.mark.parametrize("sample_rate,channels", [(1, 1), (384000, 1), (16000, 3)])
def test_wav_samples_are_not_reinterpreted_as_nested_containers(
    gateway, monkeypatch, sample_rate, channels
):
    calls = []

    def inspect_asr_input(path):
        with wave.open(path, "rb") as wf:
            calls.append(wf.getparams()[:4])
        return "", 0, "fake"

    monkeypatch.setattr(gateway.asr_manager, "transcribe", inspect_asr_input)
    monkeypatch.setattr(gateway.tts, "synthesize", lambda text: bytes(3200))
    inner = pcm_to_wav_bytes(bytes(240), sample_rate=sample_rate, channels=channels)
    # 非法容器直接上传必须拒绝，不得进入 ASR。
    status, _ = request("POST", "/chat", inner, {"X-Jarvis-Token": TOKEN})
    assert status == 413
    assert calls == []
    # 合法外层的 data chunk 是普通样本，重新封装后仍必须是 16 kHz 单声道。
    for payload in (bytes(320), pcm_to_wav_bytes(bytes(320)), pcm_to_wav_bytes(inner)):
        status, _ = request("POST", "/chat", payload, {"X-Jarvis-Token": TOKEN})
        assert status == 200
    assert calls == [
        (1, 2, 16000, 160),
        (1, 2, 16000, 160),
        (1, 2, 16000, len(inner) // 2),
    ]


def test_unknown_model_actions_do_not_log_private_body(gateway, monkeypatch, capsys):
    private = "synthetic-private-transcript"
    monkeypatch.setattr(gateway.asr_manager, "transcribe", lambda path: ("解释问题", 0, "fake"))
    monkeypatch.setattr(gateway.tts, "synthesize", lambda text: bytes(3200))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=f"模拟回答[ACTION:{private}][ACTION:{private}:value]"
    ))])
    completions = SimpleNamespace(create=lambda **kwargs: response)
    monkeypatch.setattr(gateway.llm_manager, "primary_client",
                        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    monkeypatch.setattr(gateway.llm_manager, "primary_cfg", {"model": "test-model"})
    executed = []
    monkeypatch.setattr(SystemActionExecutor, "_run_safe", lambda args: executed.append(args))
    status, body = request("POST", "/chat?mode=dialog", bytes(32000), {"X-Jarvis-Token": TOKEN})
    assert status == 200
    assert _wire_meta(body)["kind"] == "ok"
    assert executed == []
    output = capsys.readouterr().out
    assert private not in output
    assert "unknown" in output


def test_invalid_mode_is_rejected(gateway):
    status, body = request(
        "POST", "/mode?set=banana", b"", {"X-Jarvis-Token": TOKEN}
    )
    assert status == 400
    assert json.loads(body)["options"] == ["auto", "raw", "polish", "dialog"]


def test_missing_content_length_is_411(gateway):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.putrequest("POST", "/chat", skip_host=False)
    conn.putheader("Host", f"127.0.0.1:{PORT}")
    conn.putheader("X-Jarvis-Token", TOKEN)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 411
    resp.read()
    conn.close()


def test_oversized_body_is_413(gateway):
    status, _ = request(
        "POST", "/chat", b"",
        {"X-Jarvis-Token": TOKEN, "Content-Length": str(9 * 1024 * 1024)},
    )
    assert status == 413


def test_webm_upload_is_rejected(gateway):
    body = b"\x1aE\xdf\xa3" + b"\x00" * 200
    status, payload = request(
        "POST", "/chat", body, {"X-Jarvis-Token": TOKEN}
    )
    assert status == 415
    assert b"PCM" in payload


def test_fake_balance_endpoint_is_gone(gateway):
    status, _ = request("GET", "/deepseek-account-balance")
    assert status == 410


def test_wav_with_junk_chunk_is_decoded():
    pcm = struct.pack("<4h", 100, -100, 200, -200)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    raw = bytearray(buf.getvalue())
    junk = b"JUNK" + struct.pack("<I", 4000) + b"\x00" * 4000
    raw[36:36] = junk
    struct.pack_into("<I", raw, 4, len(raw) - 8)
    assert wav_bytes_to_pcm16(bytes(raw)) == pcm


def test_hallucination_filter_keeps_real_speech():
    assert is_hallucination("谢谢大家", 2.0)
    assert is_hallucination("字幕由 Amara.org 社区提供", 1.5)
    assert not is_hallucination("谢谢大家", 9.0)
    assert not is_hallucination("再见", 2.0)
    assert not is_hallucination("好", 0.2)
    assert not is_hallucination("谢谢大家的帮助，我明天继续整理文档", 2.0)
    assert not is_hallucination("今天天气不错", 2.0)
