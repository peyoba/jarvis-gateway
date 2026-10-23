#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jarvis Gateway Entry Point (v2.3)
Seamless bridge to the modular jarvis_gateway engine.
"""

import sys
from pathlib import Path

# Add src/ to Python path if running uninstalled
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from jarvis_gateway.config import load_config
from jarvis_gateway.server.server import UnifiedGatewayServer


def main():
    cfg = load_config()
    server = UnifiedGatewayServer(cfg, project_root=str(ROOT_DIR))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] 收到退出信号，Jarvis 网关已停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
