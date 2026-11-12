#!/bin/bash
set -u
printf '%s\n' '=== 衡势网页版本检查（不修改任何配置） ==='
APP="$HOME/Library/Application Support/AshareAB/app"
printf '\n%s\n' '已安装程序版本：'
if [ -f "$APP/VERSION.txt" ]; then cat "$APP/VERSION.txt"; else echo '未找到'; fi
printf '\n%s\n' '网页运行版本：'
HTML=$(/usr/bin/curl --silent --max-time 5 -H 'Cache-Control: no-cache' 'http://127.0.0.1:8765/?version_check=1' || true)
if [ -n "$HTML" ]; then
  printf '%s' "$HTML" | /usr/bin/grep -oE 'LOCAL RESEARCH / V[0-9]+(\.[0-9]+)+' | /usr/bin/head -1 || echo '网页已响应但未识别版本'
else
  echo '网页未响应'
fi
printf '\n%s\n' '8765端口进程：'
/usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN || true
printf '\n%s\n' '请比较两个版本。如果已安装为V3.6.1但网页仍旧，说明后台还有旧网页进程或浏览器缓存。'
read -r -p '按回车关闭窗口... ' _
