"""Unit tests for configuration loading and deep merging."""

from jarvis_gateway.config import DEFAULT_CONFIG, deep_merge


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
