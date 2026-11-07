#!/bin/bash
# 双击运行：全量导出 iMessage 到桌面的「iMessage导出_日期」文件夹
cd "$(dirname "$0")" || exit 1

echo "========================================"
echo "  iMessage / 信息  全量导出"
echo "========================================"
echo

if ! python3 -c "open('$HOME/Library/Messages/chat.db','rb').read(1)" 2>/dev/null; then
  echo "✗ 当前程序没有「完全磁盘访问权限」，无法读取消息数据库。"
  echo
  echo "  解决：系统设置 → 隐私与安全性 → 完全磁盘访问权限"
  echo "        点 + 添加「终端」(Terminal)，打开开关"
  echo "        然后 ⌘Q 完全退出终端，重新打开本文件"
  echo
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi

python3 export_imessage.py "$@"
echo
read -n 1 -s -r -p "按任意键关闭窗口…"
