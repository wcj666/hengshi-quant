#!/bin/bash
# Read-only local diagnostics. Never connects to the broker or market provider.
set -u
APP="$HOME/Library/Application Support/AshareAB/app"
if [[ ! -f "$APP/VERSION.txt" ]]; then
  APP="$(cd "$(dirname "$0")" && pwd)"
fi
PY="$APP/.venv/bin/python"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
printf '\n%s\n' '=== 衡势 V3.6.1 行情诊断（仅读取本机文件） ==='
"$PY" - "$APP" <<'PY'
import json,sys
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
app=Path(sys.argv[1]);output=app/'output'
print('检查目录:',app)
ver=app/'VERSION.txt'
print('已安装版本:',ver.read_text(encoding='utf-8').strip() if ver.exists() else '未知')
status=output/'intraday_status.json'
if not status.exists():
 print('本机监控状态: 尚无 intraday_status.json；检查是否安装/启动盘中服务。')
else:
 try:
  data=json.loads(status.read_text(encoding='utf-8'))
  print('监控模式:',data.get('mode','未知'))
  print('行情状态:',data.get('status','未知'))
  print('市场时间:',data.get('market_time','—'))
  print('上轮采集完成:',data.get('fetch_finished_at','—'))
  if data.get('fetch_finished_at'):
   t=datetime.fromisoformat(data['fetch_finished_at'])
   if t.tzinfo:
    age=(datetime.now(timezone.utc)-t.astimezone(timezone.utc)).total_seconds()
    print('距离采集完成:',round(age,1),'秒；此值不是行情源延迟')
  print('目标采集间隔:',data.get('interval_seconds','—'),'秒')
  print('实际轮间隔:',data.get('actual_cycle_gap_seconds','—'),'秒')
  print('采集处理耗时:',data.get('fetch_duration_seconds','—'),'秒')
  print('交易日验证:',data.get('calendar_ok','未知'))
  quality=data.get('quote_health') or {}
  print('持仓源时间已核验:',quality.get('verified_count',0),'/',quality.get('held_count','—'))
  print('暂停新交易信号:',quality.get('block_trade_signals','—'))
  for k in ('missing','invalid_jump','suspected_freeze','stale_source','unverified_source'):
   print('异常:',k,', '.join(quality.get(k,[]) or []) or '无')
  for e in (data.get('errors') or [])[:8]:print('接口错误:',str(e)[:250])
 except Exception as exc:print('状态文件读取失败:',type(exc).__name__,exc)
for name in ('local.ashare.ab.intraday.err.log','local.ashare.ab.web.err.log'):
 path=output/name
 if path.exists():
  print('\n最近日志:',name)
  try:
   for line in path.read_text(encoding='utf-8',errors='replace').splitlines()[-7:]:print(line[:300])
  except Exception as exc:print('日志读取失败:',exc)
print('\n此诊断不访问网络，也不修改任何持仓/交易记录。')
PY
read -r -p '按回车关闭窗口... ' _
