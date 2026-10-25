"""Unit tests for configuration loading and deep merging."""

import copy
import json

import pytest

from jarvis_gateway.config import DEFAULT_CONFIG, ConfigError, deep_merge, load_config


def test_deep_merge():
    base = {"a": 1, "nested": {"x": 10, "y": 20}}
    override = {"a": 2, "nested": {"y": 99, "z": 100}, "new_key": "hello"}
    merged = deep_merge(base, override)

    assert merged["a"] == 2
    assert merged["nested"]["x"] == 10
    assert merged["nested"]["y"] == 99
    assert merged["nested"]["z"] == 100
    assert merged["new_key"] == "hello"


def test_default_config_structure():
    assert "gateway" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["gateway"]["port"] == 18888
    assert "whisper" in DEFAULT_CONFIG
    assert "llm" in DEFAULT_CONFIG
    assert "data_service" in DEFAULT_CONFIG


def test_explicit_missing_config_does_not_silently_fall_back():
    import pytest

    from jarvis_gateway.config import find_config_file

    with pytest.raises(FileNotFoundError):
        find_config_file("/definitely/not/a/jarvis/config.json")


def test_missing_auth_environment_variable_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        '{"gateway":{"host":"0.0.0.0","auth_token":"${JARVIS_TEST_MISSING}"}}',
        encoding="utf-8",
    )
    monkeypatch.delenv("JARVIS_TEST_MISSING", raising=False)
    import pytest

    with pytest.raises(ConfigError, match="JARVIS_TEST_MISSING"):
        load_config(path)


def test_non_loopback_without_token_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"gateway":{"host":"0.0.0.0"}}', encoding="utf-8")
    import pytest

    with pytest.raises(ConfigError, match="auth_token"):
        load_config(path)


def test_environment_token_overrides_direct_server_config(monkeypatch):
    import copy

    from jarvis_gateway.server.server import UnifiedGatewayServer

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"].update(host="0.0.0.0", auth_token="")
    monkeypatch.setenv("JARVIS_AUTH_TOKEN", "test-token")
    server = UnifiedGatewayServer(cfg)
    assert server.auth_token == "test-token"


@pytest.mark.parametrize(("section", "key", "value"), [
    ("gateway", "port", 0),
    ("gateway", "port", True),
    ("gateway", "port", "18888"),
    ("gateway", "auth_token", "token\nwith-control"),
    ("gateway", "auth_token", "中文不能作为 HTTP 令牌"),
    ("gateway", "enable_legacy_data_port", "false"),
    ("typing", "speak_typed", "false"),
    ("typing", "polish_threshold_sec", -1),
    ("whisper", "beam_size", 2.5),
    ("whisper", "model", None),
    ("tts", "gain", float("inf")),
    ("llm", "total_timeout", float("nan")),
])
def test_invalid_configuration_types_are_rejected(tmp_path, section, key, value):
    cfg = {section: {key: value}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_malformed_configuration_does_not_disclose_source(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"private":"synthetic-sensitive-value",oops}', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "synthetic-sensitive-value" not in str(error.value)


def test_legacy_port_must_be_distinct(tmp_path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["gateway"]["enable_legacy_data_port"] = True
    cfg["data_service"]["port"] = cfg["whisper"]["port"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ConfigError, match="data_service.port"):
        load_config(path)
