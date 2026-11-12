"""Install a launchd agent running local intraday monitoring during market sessions."""
from __future__ import annotations
import argparse,os,plistlib,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
LABEL='local.ashare.ab.intraday'
AGENTS=Path.home()/'Library'/'LaunchAgents'
PLIST=AGENTS/(LABEL+'.plist')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--interval',type=int,default=10)
    p.add_argument('--uninstall',action='store_true')
    args=p.parse_args()
    if not 10<=args.interval<=1800:p.error('间隔必须为10—1800秒')
    domain=f'gui/{os.getuid()}'
    subprocess.run(['launchctl','bootout',domain,str(PLIST)],capture_output=True)
    if args.uninstall:
        PLIST.unlink(missing_ok=True)
        print('已停止并卸载盘中监控登录任务');return
    python=ROOT/'.venv/bin/python'
    if not python.exists():p.error('未找到.venv，请先安装虚拟环境和依赖')
    AGENTS.mkdir(parents=True,exist_ok=True)
    OUT=ROOT/'output';OUT.mkdir(exist_ok=True)
    spec={'Label':LABEL,'ProgramArguments':[str(python),str(ROOT/'intraday_monitor.py'),'--mode','live','--interval',str(args.interval),'--notify'],
          'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':60,
          'StandardOutPath':str(OUT/'intraday_stdout.log'),'StandardErrorPath':str(OUT/'intraday_stderr.log')}
    PLIST.write_bytes(plistlib.dumps(spec))
    r=subprocess.run(['launchctl','bootstrap',domain,str(PLIST)],capture_output=True,text=True)
    if r.returncode:
        print('创建成功但启动失败：',r.stderr,file=sys.stderr);sys.exit(1)
    print('盘中监控已安装并启动：用户登录后持续驻留，交易时间才抓取行情；间隔',args.interval,'秒。')
    print('必须保持Mac开机、登录、联网及未休眠；行情时戳未验证，提醒不保证送达。')
    print('查看日志:',OUT/'intraday_stdout.log')
    print('卸载：python install_intraday_mac.py --uninstall')
if __name__=='__main__':main()
