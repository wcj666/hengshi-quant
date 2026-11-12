"""为当前macOS用户安装工作日盘后量化扫描任务，不要求管理员权限。"""
from pathlib import Path
import argparse,plistlib,subprocess,sys,os

ROOT=Path(__file__).resolve().parent
LABEL='local.ashare.ab.phase2'
AGENTS=Path.home()/'Library'/'LaunchAgents'
PLIST=AGENTS/(LABEL+'.plist')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--hour',type=int,default=16)
    p.add_argument('--minute',type=int,default=45)
    p.add_argument('--top',type=int,default=60)
    p.add_argument('--uninstall',action='store_true')
    a=p.parse_args()
    domain=f'gui/{os.getuid()}'
    subprocess.run(['launchctl','bootout',domain,str(PLIST)],capture_output=True)
    if a.uninstall:
        PLIST.unlink(missing_ok=True);print('已移除定时任务');return
    if not(0<=a.hour<=23 and 0<=a.minute<=59):p.error('hour/minute 不合法')
    python=(ROOT/'.venv'/'bin'/'python')
    if not python.exists():p.error('未找到 .venv/bin/python，请先在该项目目录创建虚拟环境和安装依赖')
    AGENTS.mkdir(parents=True,exist_ok=True)
    log=ROOT/'output';log.mkdir(exist_ok=True)
    spec={
      'Label':LABEL,
      'ProgramArguments':[str(python),str(ROOT/'phase2.py'),'--mode','live','--top',str(a.top),'--notify'],
      'WorkingDirectory':str(ROOT),
      'StartCalendarInterval':[{'Weekday':i,'Hour':a.hour,'Minute':a.minute} for i in range(1,6)],
      'StandardOutPath':str(log/'schedule_stdout.log'),
      'StandardErrorPath':str(log/'schedule_stderr.log'),
      'RunAtLoad':False,
    }
    PLIST.write_bytes(plistlib.dumps(spec))
    r=subprocess.run(['launchctl','bootstrap',domain,str(PLIST)],capture_output=True,text=True)
    if r.returncode!=0:
        print('已写入任务但加载失败:',r.stderr, file=sys.stderr);sys.exit(1)
    print(f'安装成功：每周一至周五，Mac本地时间 {a.hour:02d}:{a.minute:02d} 运行。')
    print('注意：节假日不自动排除；Mac必须开机、可联网，休眠期间不保证准点运行。')
    print('检查日志:',log/'schedule_stdout.log')
    print('卸载: python install_schedule_mac.py --uninstall')

if __name__=='__main__':main()
