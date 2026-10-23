#!/bin/bash
# 双击做一次环境自检
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis doctor
echo
read -n 1 -s -r -p "按任意键关闭窗口..."
