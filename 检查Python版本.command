#!/bin/bash
cd "$(dirname "$0")" || exit 1
source ./python_scan.sh
REPORT="$HOME/Desktop/衡势_Python检查结果.txt"
if [[ ! -d "$HOME/Desktop" ]]; then REPORT="$PWD/衡势_Python检查结果.txt"; fi
print_python_diagnostics | tee "$REPORT"
printf '\n诊断报告已保存：%s\n' "$REPORT"
printf '%s\n' '将报告中的 Python 版本与路径发来即可；不需要提供密码或其他个人信息。'
read -r -p '按回车关闭窗口... ' _
