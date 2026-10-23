#!/bin/bash
# 双击启动 Jarvis（会打开一个终端窗口显示进度）
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis start --open
status=$?
if [ $status -ne 0 ]; then
  echo
  read -n 1 -s -r -p "启动失败，按任意键关闭窗口..."
fi
