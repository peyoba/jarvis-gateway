"""
Configuration management for Jarvis Gateway.
Loads JSON configuration with deep-merge support and environment fallback.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
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
        "total_timeout": 16.0,
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


class ConfigError(ValueError):
    """Raised when configuration would be unsafe or internally inconsistent."""


def expand_env(value: Any, *, strict: bool = False, path: str = "") -> Any:
    """Expand ``${VAR}`` references without ever printing their values.

    ``strict`` is used for security-sensitive fields such as the gateway token.
    Optional provider credentials may continue to use the historical empty-value
    fallback, but an explicitly requested security variable must never silently
    disable protection.
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in os.environ and (os.environ[name].strip() or not strict):
                return os.environ[name]
            if strict:
                field = path or "configuration"
                raise ConfigError(f"missing environment variable for {field}: {name}")
            return ""

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {
            k: expand_env(v, strict=strict, path=f"{path}.{k}" if path else str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            expand_env(v, strict=strict, path=f"{path}[{i}]")
            for i, v in enumerate(value)
        ]
    return value


def validate_config(cfg: Dict[str, Any]) -> None:
    """Fail closed before binding a non-loopback gateway socket."""
    for section in DEFAULT_CONFIG:
        if not isinstance(cfg.get(section), dict):
            raise ConfigError(f"{section} must be an object")
    gateway = cfg.get("gateway", {})
    host = gateway.get("host", "127.0.0.1")
    token = gateway.get("auth_token", "")
    if not isinstance(host, str) or not host or host != host.strip() or any(c.isspace() for c in host):
        raise ConfigError("gateway.host must be a nonempty string")
    if not isinstance(token, str) or (token and not re.fullmatch(r"[\x21-\x7e]+", token)):
        raise ConfigError("gateway.auth_token must contain only visible ASCII characters without whitespace")
    host = host.strip().lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback and not token:
        raise ConfigError(
            "gateway.auth_token is required when gateway.host is not loopback; "
            "refusing to expose an unauthenticated gateway"
        )

    if _ENV_PATTERN.search(token):
        raise ConfigError(
            "gateway.auth_token still contains an unresolved environment placeholder"
        )
    for section in ("gateway", "whisper", "data_service"):
        port = cfg[section].get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{section}.port must be an integer between 1 and 65535")
    if cfg["gateway"]["port"] == cfg["whisper"]["port"]:
        raise ConfigError("gateway.port and whisper.port must be different")
    if gateway.get("enable_legacy_data_port") and cfg["data_service"]["port"] in (
        gateway["port"], cfg["whisper"]["port"]
    ):
        raise ConfigError("enabled data_service.port must differ from gateway.port and whisper.port")
    for section, key in (("gateway", "enable_legacy_data_port"), ("typing", "speak_typed")):
        if not isinstance(cfg[section].get(key), bool):
            raise ConfigError(f"{section}.{key} must be a boolean")
    allowed = gateway.get("allowed_hosts", [])
    if not isinstance(allowed, list) or not all(isinstance(h, str) for h in allowed):
        raise ConfigError("gateway.allowed_hosts must be a list of strings")
    if cfg["typing"].get("default_mode") not in ("auto", "raw", "polish", "dialog"):
        raise ConfigError("typing.default_mode must be auto, raw, polish or dialog")
    for section, keys in (
        ("whisper", ("cli_bin", "server_bin", "model", "language", "prompt")),
        ("tts", ("voice",)),
        ("data_service", ("opentoken_bin", "weather_city", "weather_label")),
    ):
        for key in keys:
            if not isinstance(cfg[section].get(key), str):
                raise ConfigError(f"{section}.{key} must be a string")
    beam = cfg["whisper"].get("beam_size")
    if isinstance(beam, bool) or not isinstance(beam, int) or not 0 <= beam <= 128:
        raise ConfigError("whisper.beam_size must be an integer between 0 and 128")

    def number(value: Any, field: str, maximum: float, *, allow_zero: bool = False):
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or value > maximum or not math.isfinite(value)
            or (value < 0 if allow_zero else value <= 0)
        ):
            raise ConfigError(f"{field} must be a finite number in the supported range")

    number(cfg["typing"].get("polish_threshold_sec"), "typing.polish_threshold_sec", 120)
    number(cfg["tts"].get("gain"), "tts.gain", 10, allow_zero=True)
    number(cfg["llm"].get("total_timeout", 16), "llm.total_timeout", 300)
    for channel in ("primary", "fallback"):
        if not isinstance(cfg["llm"].get(channel), dict):
            raise ConfigError(f"llm.{channel} must be an object")
        for key in ("api_key", "base_url", "model", "name"):
            if not isinstance(cfg["llm"][channel].get(key), str):
                raise ConfigError(f"llm.{channel}.{key} must be a string")
        number(cfg["llm"][channel].get("timeout"), f"llm.{channel}.timeout", 300)


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
        raise FileNotFoundError(f"config file not found: {p}")

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
            if not isinstance(user_cfg, dict):
                raise ConfigError("configuration root must be an object")
            # A missing token variable is a configuration error even when the
            # host is loopback: the operator explicitly requested a secret.
            gateway_cfg = user_cfg.get("gateway", {}) if isinstance(user_cfg, dict) else {}
            raw_token = gateway_cfg.get("auth_token") if isinstance(gateway_cfg, dict) else None
            expanded = expand_env(user_cfg)
            if isinstance(raw_token, str) and _ENV_PATTERN.search(raw_token):
                expanded["gateway"]["auth_token"] = expand_env(
                    raw_token, strict=True, path="gateway.auth_token"
                )
            cfg = deep_merge(cfg, expanded)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            # JSON errors may contain private source text; never echo the parser
            # exception or silently replace an explicitly supplied configuration.
            raise ConfigError("cannot read configuration: invalid JSON or unreadable file") from exc
    else:
        # Check if config.example.json exists
        example = Path.cwd() / "config.example.json"
        if example.is_file() and not (Path.cwd() / "config.json").is_file():
            print("[i] 未找到 config.json，正在以默认配置启动。可参考 config.example.json 自定义。", flush=True)

    if os.environ.get("JARVIS_AUTH_TOKEN"):
        cfg["gateway"]["auth_token"] = os.environ["JARVIS_AUTH_TOKEN"]
    validate_config(cfg)

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
