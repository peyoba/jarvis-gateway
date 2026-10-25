"""
CLI entry point when executed as `python -m jarvis_gateway`.
"""

from __future__ import annotations

import argparse
import os
import sys

from jarvis_gateway import __version__
from jarvis_gateway.config import ConfigError, load_config, validate_config
from jarvis_gateway.server.server import UnifiedGatewayServer


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis-gateway",
        description="Local voice-typing gateway for hardware companions and browsers.",
    )
    parser.add_argument("-c", "--config", metavar="PATH", help="path to config.json")
    parser.add_argument("--host", help="override gateway.host for this run")
    parser.add_argument("--port", type=_port, help="override gateway.port for this run (1-65535)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ConfigError) as exc:
        parser.error(str(exc))
    if args.host:
        cfg.setdefault("gateway", {})["host"] = args.host
    if args.port:
        cfg.setdefault("gateway", {})["port"] = args.port

    try:
        validate_config(cfg)
        server = UnifiedGatewayServer(cfg, project_root=os.getcwd())
    except ConfigError as exc:
        parser.error(str(exc))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] 收到退出信号，Jarvis 网关已停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
