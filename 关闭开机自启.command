#!/bin/bash
# 双击关闭开机自启
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis autostart off
echo
read -n 1 -s -r -p "按任意键关闭窗口..."
