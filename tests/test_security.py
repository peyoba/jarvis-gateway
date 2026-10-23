"""Security and protocol hardening tests (no audio is ever injected)."""

import copy
import http.client
import io
import json
import struct
import threading
import time
import wave

import pytest

from jarvis_gateway.asr.manager import is_hallucination
from jarvis_gateway.audio.converter import wav_bytes_to_pcm16
from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.server.server import UnifiedGatewayServer

TOKEN = "secret123"


@pytest.fixture(scope="module")
def gateway():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update({
        "host": "127.0.0.1",
        "port": 19996,
        "auth_token": TOKEN,
        "enable_legacy_data_port": False,
    })
    server = UnifiedGatewayServer(cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.4)
    yield server
    server.stop_services()


def request(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", 19996, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def test_status_is_not_readable_cross_origin(gateway):
    conn = http.client.HTTPConnection("127.0.0.1", 19996, timeout=5)
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
    conn = http.client.HTTPConnection("127.0.0.1", 19996, timeout=5)
    conn.putrequest("GET", "/status", skip_host=True)
    conn.putheader("Host", "evil.example.com")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 403
    resp.read()
    conn.close()


def test_cross_site_mutation_is_rejected(gateway):
    status, _ = request(
        "GET", "/mode?set=raw",
        headers={"X-Jarvis-Token": TOKEN, "Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert gateway.state.mode != "raw"


def test_invalid_mode_is_rejected(gateway):
    status, body = request(
        "POST", "/mode?set=banana", b"", {"X-Jarvis-Token": TOKEN}
    )
    assert status == 400
    assert json.loads(body)["options"] == ["auto", "raw", "polish", "dialog"]


def test_missing_content_length_is_411(gateway):
    conn = http.client.HTTPConnection("127.0.0.1", 19996, timeout=5)
    conn.putrequest("POST", "/chat", skip_host=False)
    conn.putheader("Host", "127.0.0.1:19996")
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
    assert not is_hallucination("今天天气不错", 2.0)
