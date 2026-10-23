#!/bin/bash
# 双击开启开机自启（下次登录 Mac 时自动启动 Jarvis）
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis autostart on
echo
read -n 1 -s -r -p "按任意键关闭窗口..."
