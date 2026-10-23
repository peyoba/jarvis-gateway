#!/bin/bash
# 双击跟踪网关日志（Ctrl+C 退出）
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis logs gateway -f
