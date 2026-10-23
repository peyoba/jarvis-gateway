#!/bin/bash
# 双击停止 Jarvis
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis stop
echo
read -n 1 -s -r -p "按任意键关闭窗口..."
