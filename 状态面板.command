#!/bin/bash
# 双击查看状态并打开控制台
cd "$(dirname "$0")" || exit 1
./venv/bin/python jarvis status
echo
./venv/bin/python jarvis open
sleep 1
