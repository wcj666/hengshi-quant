#!/bin/bash
cd "$(dirname "$0")" || exit 1
printf '%s\n' '=== 衡势 A股量化助手 V3.6.1：历史缠论结构核验升级 ==='
source ./python_scan.sh
if ! find_compatible_python; then
  REPORT="$HOME/Desktop/衡势_Python检查结果.txt"
  if [[ ! -d "$HOME/Desktop" ]]; then REPORT="$PWD/衡势_Python检查结果.txt"; fi
  print_python_diagnostics > "$REPORT" 2>&1
  printf '\n%s\n' '未找到可用的 Python 3.11 或更新版本。'
  printf '%s\n' '这可能是安装器无法识别安装位置，或电脑目前只有 Python 3.10及以下版本。'
  printf '已生成诊断报告：%s\n' "$REPORT"
  printf '%s\n' '请双击“检查Python版本.command”或将该报告中的 Python 版本及路径发给我。'
  printf '%s\n' '若确认确实没有3.11+，可安装官方 Python 3.13 macOS universal2：'
  printf '%s\n' 'https://www.python.org/downloads/macos/'
  read -r -p '按回车关闭窗口... ' _
  exit 1
fi
printf '已找到兼容 Python %s：%s\n' "$FOUND_VERSION" "$FOUND_PYTHON"
"$FOUND_PYTHON" setup_autostart.py --install
result=$?
if [[ "$result" -eq 0 ]]; then
  printf '\n%s\n' '安装完成，以后打开 http://127.0.0.1:8765/ 即可。'
else
  printf '\n%s\n' '安装尚未完成，请截取上方错误信息，以便判断是否是依赖安装或端口问题。'
fi
read -r -p '按回车关闭窗口... ' _
exit "$result"
