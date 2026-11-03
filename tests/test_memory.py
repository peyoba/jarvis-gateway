"""长期记忆：事实库、对话历史、自我进化整理与 HTTP 端点。"""

import copy
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from jarvis_gateway.config import DEFAULT_CONFIG
from jarvis_gateway.llm.client import LLMClientManager
from jarvis_gateway.memory import Memory, parse_consolidation
from jarvis_gateway.server.server import UnifiedGatewayServer


def make_memory(tmp_path, **kwargs):
    kwargs.setdefault("enabled", True)
    return Memory(str(tmp_path / "mem"), **kwargs)


def test_facts_recall_and_dedupe(tmp_path):
    memory = make_memory(tmp_path)
    fact, created = memory.add_fact("用户正在做 ESP32 语音助手", importance=5)
    assert created and fact.id == 1
    again, created_again = memory.add_fact("用户正在做 ESP32 语音助手", importance=3)
    assert not created_again and again.id == fact.id
    memory.add_fact("用户喜欢简短的语音回答", importance=4)

    hits = memory.recall("ESP32 项目进展")
    assert [f.text for f in hits][0] == "用户正在做 ESP32 语音助手"
    assert memory.recall("完全不相关的天气") == []
    assert "长期记忆" in memory.context_block("ESP32 语音助手")
    assert memory.context_block("毫不相干") == ""


def test_forget_list_and_commands(tmp_path):
    memory = make_memory(tmp_path)
    memory.add_fact("用户的猫叫豆豆", importance=4)
    memory.add_fact("用户在北京工作", importance=3)

    reply, trigger = memory.handle_command("记住：我每天早上七点起床")
    assert "记下了" in reply and trigger is False
    assert any("七点" in f.text for f in memory.facts())

    reply, _ = memory.handle_command("忘掉豆豆")
    assert "已忘掉" in reply
    assert not any("豆豆" in f.text for f in memory.facts())

    reply, _ = memory.handle_command("你记得什么")
    assert "我记住了" in reply

    reply, trigger = memory.handle_command("整理记忆")
    assert trigger is True and "整理" in reply

    assert memory.handle_command("今天天气怎么样") is None
    assert memory.handle_command("") is None


def test_commands_need_explicit_form(tmp_path):
    memory = make_memory(tmp_path)
    # 普通口述里的「记住」不能误触发，避免把正文写进记忆库。
    assert memory.handle_command("请记住这句话要原样打出来吗") is not None
    assert memory.handle_command("我记住了这个 bug 的修法") is None


def test_history_window_feeds_chat_messages(tmp_path):
    memory = make_memory(tmp_path, history_turns=2)
    memory.record_exchange("第一轮问题", "第一轮回答")
    memory.record_exchange("第二轮问题", "第二轮回答")
    memory.record_exchange("第三轮问题", "第三轮回答")

    history = memory.chat_history()
    assert history[0] == {"role": "user", "content": "第二轮问题"}
    assert history[-1] == {"role": "assistant", "content": "第三轮回答"}
    assert all("第一轮" not in item["content"] for item in history)
    assert memory.summary()["turns_total"] == 3


def test_consolidation_applies_json_and_persona(tmp_path):
    calls = []

    def complete(messages):
        calls.append(messages)
        return (
            '{"facts":[{"text":"用户偏好简洁回答","importance":4,"tags":["偏好"]}],'
            '"forget":[1],'
            '"persona":"先给结论，再补充一句原因。"}'
        )

    memory = make_memory(tmp_path, complete=complete, every_turns=1, min_interval_sec=0)
    old, _ = memory.add_fact("过时的事实", importance=2)
    memory.record_exchange("以后请简短一点", "好的")
    assert memory.due()
    assert memory.consolidate() is True

    facts = [f.text for f in memory.facts()]
    assert "用户偏好简洁回答" in facts
    assert old.text not in facts
    assert memory.persona().startswith("先给结论")
    assert memory.summary()["consolidations"] == 1
    assert memory.summary()["turns_pending"] == 0
    assert "[自我进化笔记]" in memory.context_block("回答风格")
    assert calls and calls[0][0]["role"] == "system"


def test_consolidation_drops_garbage_and_survives_errors(tmp_path):
    memory = make_memory(tmp_path, complete=lambda messages: "这不是 JSON", every_turns=1,
                         min_interval_sec=0)
    memory.record_exchange("你好", "我在")
    assert memory.consolidate() is True or memory.consolidate(force=True) is False
    assert memory.summary()["consolidations"] == 0

    def boom(messages):
        raise RuntimeError("network down")

    broken = make_memory(tmp_path / "broken", complete=boom, every_turns=1, min_interval_sec=0)
    broken.record_exchange("你好", "我在")
    assert broken.consolidate(force=True) is False
    assert broken.summary()["facts"] == 0


def test_empty_consolidation_clears_pending(tmp_path):
    empty = '{"facts":[],"forget":[],"persona":""}'
    memory = make_memory(tmp_path, complete=lambda messages: empty, every_turns=1,
                         min_interval_sec=0)
    memory.record_exchange("今天先这样", "好的，随时叫我")
    assert memory.summary()["turns_pending"] == 1
    assert memory.consolidate(force=True) is False
    summary = memory.summary()
    assert summary["turns_pending"] == 0
    assert summary["consolidations"] == 0
    assert summary["last_consolidation"] > 0


def test_parse_consolidation_limits(tmp_path):
    assert parse_consolidation("not json") is None
    assert parse_consolidation('{"facts":[],"forget":[],"persona":""}') is None
    long_text = "x" * 400
    payload = {
        "facts": ["纯字符串事实", {"text": "带重要度", "importance": 99}, {"text": long_text}],
        "forget": ["3", True, {"bad": 1}],
        "persona": "  ",
    }
    parsed = parse_consolidation("前缀 " + json.dumps(payload, ensure_ascii=False))
    assert parsed is not None
    assert [f["text"] for f in parsed["facts"]] == ["纯字符串事实", "带重要度"]
    assert parsed["facts"][1]["importance"] == 5
    assert parsed["forget"] == [3]
    assert parsed["persona"] == ""


def test_caps_clear_and_permissions(tmp_path):
    memory = make_memory(tmp_path, max_facts=5)
    for index in range(9):
        memory.add_fact("事实" + str(index), importance=1 + index % 5)
    assert len(memory.facts()) == 5

    memory.set_persona("回答先给结论")
    mode = os.stat(str(tmp_path / "mem" / "facts.json")).st_mode & 0o777
    assert mode == 0o600

    memory.record_exchange("记录一轮", "好的")
    memory.clear()
    assert memory.facts() == [] and memory.persona() == ""
    assert memory.summary()["turns_total"] >= 1


def test_chat_messages_inject_memory_and_trim_history():
    history = []
    for index in range(10):
        history.append({"role": "user", "content": "问题" + str(index) + "x" * 400})
        history.append({"role": "assistant", "content": "回答" + str(index) + "x" * 400})
    messages = LLMClientManager._chat_messages("本轮", history, "[长期记忆]\n1. 用户在做 ESP32")
    assert messages[0]["role"] == "system"
    assert "[长期记忆]" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "本轮"}
    assert sum(len(m["content"]) for m in messages[1:-1]) <= 4000
    # 丢的是最旧的一轮，最新的对话必须保留在提示里。
    assert "问题9" in messages[-3]["content"]

    plain = LLMClientManager._chat_messages("你好", None, "")
    assert [m["role"] for m in plain] == ["system", "user"]
    assert plain[-1]["content"] == "你好"


def test_memory_config_validation():
    from jarvis_gateway.config import ConfigError, validate_config

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    validate_config(cfg)

    bad = copy.deepcopy(DEFAULT_CONFIG)
    bad["memory"]["enabled"] = "yes"
    with pytest.raises(ConfigError):
        validate_config(bad)

    bad = copy.deepcopy(DEFAULT_CONFIG)
    bad["memory"]["history_turns"] = -1
    with pytest.raises(ConfigError):
        validate_config(bad)

    bad = copy.deepcopy(DEFAULT_CONFIG)
    bad["memory"]["dir"] = ""
    with pytest.raises(ConfigError):
        validate_config(bad)


def test_dashboard_shows_memory_card():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "src" / "jarvis_gateway" / "web" / "dashboard.html").read_text(encoding="utf-8")
    for marker in ('id="mem-facts"', 'id="mem-turns"', 'id="mem-evolve"',
                   'id="mem-consolidate"', '"/memory?summary=1"'):
        assert marker in html


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("gateway did not start listening")


def request(url, token="", data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Jarvis-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_memory_http_endpoints(tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    port = free_port()
    cfg["gateway"].update({"port": port, "host": "127.0.0.1", "auth_token": "secret123"})
    cfg["memory"]["dir"] = str(tmp_path / "mem")
    server = UnifiedGatewayServer(cfg, project_root=str(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_port(port)
    base = "http://127.0.0.1:" + str(port)
    try:
        status, payload = request(base + "/memory?summary=1")
        assert status == 200 and payload["summary"]["facts"] == 0

        status, _payload = request(base + "/memory")
        assert status == 401

        status, payload = request(base + "/memory", token="secret123")
        assert status == 200 and payload["summary"]["enabled"] is True

        status, payload = request(base + "/memory", token="secret123",
                                  data={"action": "add", "text": "用户在做 ESP32 语音助手"})
        assert status == 200 and payload["status"] == "added"

        status, payload = request(base + "/memory?summary=1")
        assert payload["summary"]["facts"] == 1

        status, _payload = request(base + "/memory", data={"action": "add", "text": "偷偷写入"})
        assert status == 401

        status, payload = request(base + "/memory", token="secret123", data={"action": "clear"})
        assert status == 200 and payload["status"] == "cleared"
        status, payload = request(base + "/memory?summary=1")
        assert payload["summary"]["facts"] == 0
    finally:
        server.stop_services()
        thread.join(timeout=5)
