#!/bin/bash
# macOS 启动脚本（双击运行；首次需 chmod +x）
cd "$(dirname "$0")" || exit 1

if lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "监控已在运行，正在打开看板..."
  open "http://127.0.0.1:8787"
  exit 0
fi

echo "============================================"
echo "  clash-opt  代理链路监控 / 自动优选"
echo "============================================"
echo "  看板: http://127.0.0.1:8787"
echo "  按 Ctrl+C 停止"
echo

open "http://127.0.0.1:8787" 2>/dev/null
exec python3 clash_opt.py serve
