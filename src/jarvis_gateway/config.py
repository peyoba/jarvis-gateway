"""
Configuration management for Jarvis Gateway.
Loads JSON configuration with deep-merge support and environment fallback.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict

# Detect default whisper-server binary
_WHISPER_SERVER_DEFAULT = shutil.which("whisper-server") or "/opt/homebrew/bin/whisper-server"
_WHISPER_CLI_DEFAULT = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
_OPENTOKEN_DEFAULT = shutil.which("opentoken") or "~/.local/bin/opentoken"

DEFAULT_CONFIG: Dict[str, Any] = {
    "gateway": {
        # Loopback by default: set "0.0.0.0" to expose the gateway to your LAN.
        "host": "127.0.0.1",
        "port": 18888,
        "auth_token": "",  # Optional Bearer / X-Jarvis-Token for LAN authentication
        # Extra Host header values accepted by the DNS-rebinding guard (e.g. "jarvis.lan").
        "allowed_hosts": [],
        "enable_legacy_data_port": False,  # Deprecated alias of data_service.port (8080)
    },
    "whisper": {
        "cli_bin": _WHISPER_CLI_DEFAULT,
        "server_bin": _WHISPER_SERVER_DEFAULT,
        "model": "~/.cache/whisper/ggml-small-q5_1.bin",
        "port": 18889,
        "language": "zh",
        "prompt": "以下是普通话简体中文语音输入。",
        "beam_size": 2,
    },
    "tts": {
        "voice": "Tingting",
        "gain": 1.3,
    },
    "typing": {
        "polish_threshold_sec": 2.8,
        "speak_typed": False,
        "default_mode": "auto",  # auto | raw | polish | dialog
    },
    "llm": {
        "primary": {
            "name": "Primary",
            "api_key": "",
            "base_url": "",
            "model": "",
            "timeout": 8.0,
        },
        "fallback": {
            "name": "Fallback",
            "api_key": "",
            "base_url": "",
            "model": "",
            "timeout": 8.0,
        },
    },
    "data_service": {
        "port": 8080,
        "opentoken_bin": _OPENTOKEN_DEFAULT,
        "weather_city": "Beijing",
        "weather_label": "北京",
    },
}


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: Any) -> Any:
    """Expand ${VAR} reference in strings so API keys can stay in the environment."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def deep_merge(base: Dict[str, Any], override: Dict[str, Any] | None) -> Dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    out = copy.deepcopy(base)
    if not override:
        return out
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_config_file(explicit_path: str | Path | None = None) -> Path | None:
    """Find configuration file from explicit path, env var, or standard locations."""
    if explicit_path:
        p = Path(explicit_path).expanduser().resolve()
        if p.is_file():
            return p
        raise FileNotFoundError(f"config file not found: {p}")

    env_path = os.getenv("JARVIS_CONFIG")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file():
            return p

    # Standard search paths
    search_dirs = [
        Path.cwd(),
        Path(__file__).resolve().parent.parent.parent,  # Jarvis-Gateway root
        Path.home() / ".config" / "jarvis-gateway",
    ]
    for d in search_dirs:
        cfg = d / "config.json"
        if cfg.is_file():
            return cfg
    return None


def load_config(config_path: str | Path | None = None) -> Dict[str, Any]:
    """
    Load configuration with deep merge over DEFAULT_CONFIG.
    Expands user home ~ paths automatically.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    target = find_config_file(config_path)

    if target and target.is_file():
        try:
            with open(target, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg = deep_merge(cfg, expand_env(user_cfg))
        except Exception as e:
            print(f"[!] 读取配置文件失败 ({target}): {e}，使用默认配置", flush=True)
    else:
        # Check if config.example.json exists
        example = Path.cwd() / "config.example.json"
        if example.is_file() and not (Path.cwd() / "config.json").is_file():
            print("[i] 未找到 config.json，正在以默认配置启动。可参考 config.example.json 自定义。", flush=True)

    # Normalize expanded paths
    if "whisper" in cfg:
        for k in ("cli_bin", "server_bin", "model"):
            if k in cfg["whisper"] and isinstance(cfg["whisper"][k], str):
                cfg["whisper"][k] = os.path.expanduser(cfg["whisper"][k])

    if "data_service" in cfg and "opentoken_bin" in cfg["data_service"]:
        cfg["data_service"]["opentoken_bin"] = os.path.expanduser(
            str(cfg["data_service"]["opentoken_bin"])
        )

    return cfg
