#!/bin/bash
# 首次配置：双击运行（macOS）
# 等同于 python3 clash_opt.py check && python3 clash_opt.py fix
cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo "  clash-opt 首次配置"
echo "=============================================="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3。请先安装：brew install python3"
  read -r -p "按回车退出"
  exit 1
fi

echo "[1/2] 自检"
python3 clash_opt.py check
echo
echo "[2/2] 修复"
python3 clash_opt.py fix
echo

echo "=============================================="
echo "  还差最后一步：重启 Clash Verge"
echo "  重启后运行下面这条确认："
echo "      python3 clash_opt.py status"
echo "  然后启动监控："
echo "      python3 clash_opt.py serve"
echo "=============================================="
read -r -p "按回车退出"
