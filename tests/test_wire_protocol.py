"""Unit tests for the binary wire protocol."""

import json
import struct


def test_binary_wire_protocol_packing():
    meta = {"heard": "你好", "reply": "你好！", "typed": "你好！", "mode": "raw"}
    meta_json = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    pcm_out = bytes(3200)  # 100ms silence

    payload = struct.pack("!I", len(meta_json)) + meta_json + pcm_out

    # Unpack header
    json_len = struct.unpack("!I", payload[:4])[0]
    assert json_len == len(meta_json)

    # Unpack JSON
    unpacked_json = json.loads(payload[4 : 4 + json_len].decode("utf-8"))
    assert unpacked_json["heard"] == "你好"
    assert unpacked_json["reply"] == "你好！"

    # Unpack PCM
    unpacked_pcm = payload[4 + json_len :]
    assert len(unpacked_pcm) == 3200
