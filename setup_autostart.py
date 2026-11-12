"""One-time per-user Mac setup: persistent app folder + user LaunchAgents.

Usage: double click 安装_开机自动启动.command; no administrator privileges needed.
It does NOT connect to a brokerage account or submit trades.
"""
from __future__ import annotations
import argparse
import os
import signal
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parent
HOME = Path.home()
BASE = HOME / 'Library' / 'Application Support' / 'AshareAB'
APP = BASE / 'app'
AGENTS = HOME / 'Library' / 'LaunchAgents'
OUTPUT = APP / 'output'
URL = 'http://127.0.0.1:8765/'
EXPECTED_VERSION = '3.6.1'
LABELS = {
    'web': 'local.ashare.ab.web',
    'intraday': 'local.ashare.ab.intraday',
    'phase2': 'local.ashare.ab.phase2',
}


def cmd(args, *, check=False, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=check, **kwargs)


def domain():
    return f'gui/{os.getuid()}'


def plist_path(label):
    return AGENTS / (label + '.plist')


def stop_agent(label, remove=False):
    path = plist_path(label)
    # The first form handles an already registered label even if the plist path changed.
    cmd(['launchctl', 'bootout', domain() + '/' + label])
    if path.exists():
        cmd(['launchctl', 'bootout', domain(), str(path)])
        if remove:
            path.unlink(missing_ok=True)


def network_port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(('127.0.0.1', 8765)) == 0


def listeners():
    """List active 8765 listeners; never modify an unrelated process."""
    result = cmd(['/usr/sbin/lsof', '-nP', '-tiTCP:8765', '-sTCP:LISTEN'])
    rows = []
    for value in result.stdout.splitlines():
        if not value.isdigit():
            continue
        pid = int(value)
        ps = cmd(['/bin/ps', '-p', str(pid), '-o', 'command='])
        rows.append((pid, ps.stdout.strip()))
    return rows


def release_owned_listener():
    """Only terminate a clearly identified earlier Hengshi web server, never another app."""
    if not network_port_open():
        return
    for pid, command in listeners():
        if (('AshareAB/app/' in command or 'a_share_ab_web_v3_' in command)
                and ('serve_web.py' in command or 'web_dashboard.py' in command)):
            print(f'检测到旧衡势网页进程 {pid}，正在停止以完成升级……', flush=True)
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError) as e:
                print(f'无法停止旧进程 {pid}: {e}', flush=True)
    for _ in range(30):
        if not network_port_open():
            return
        time.sleep(.25)
    details = '; '.join(f'PID {p}: {c[:130]}' for p, c in listeners()) or '未知程序'
    raise RuntimeError('8765 端口仍被占用，无法启动新版。请关闭之前手动打开的旧版网页终端或进程后重试。监听进程：'+details)


def copy_sources():
    APP.mkdir(parents=True, exist_ok=True)
    files = 0
    if SOURCE.resolve() == APP.resolve():
        return files
    for p in SOURCE.rglob('*'):
        rel = p.relative_to(SOURCE)
        if any(part in ('.venv', 'output', '__pycache__', '.git', '.DS_Store') for part in rel.parts):
            continue
        dest = APP / rel
        if p.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif p.is_file():
            if rel == Path('config.json') and dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            files += 1
    return files


def import_previous_config():
    # Never overwrite an installed, possibly user-edited configuration.
    cfg = APP / 'config.json'
    if cfg.exists():
        return '保留已安装版本的持仓和自选配置'
    folders = ['a_share_ab_web_v3_5_9', 'a_share_ab_web_v3_5_8', 'a_share_ab_web_v3_5_7', 'a_share_ab_web_v3_5_6_1', 'a_share_ab_web_v3_5_6', 'a_share_ab_web_v3_5_5', 'a_share_ab_web_v3_5_4', 'a_share_ab_web_v3_5_3', 'a_share_ab_web_v3_5_2', 'a_share_ab_web_v3_5_1', 'a_share_ab_web_v3_5', 'a_share_ab_web_v3_4_1', 'a_share_ab_web_v3_4', 'a_share_ab_web_v3_3', 'a_share_ab_web_v3_2', 'a_share_ab_web_v3_1', 'a_share_ab_web_v3']
    candidates = [base / name / 'config.json' for base in
                  (HOME / 'Downloads', HOME / 'Documents', HOME / 'Desktop') for name in folders]
    candidates = [p for p in candidates if p.is_file()]
    if candidates:
        best = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            import json
            data = json.loads(best.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('positions'), list) and 'baseline_equity' in data:
                shutil.copy2(best, cfg)
                return f'已导入旧版配置：{best}；请在网页检查持仓/现金是否最新'
        except (OSError, ValueError):
            pass
    shutil.copy2(SOURCE / 'config.json', cfg)
    return '采用随包初始配置（仅为历史示例，请核对）'


def backup_current_config():
    """Backup existing ledger before a version upgrade (never alters original)."""
    cfg=APP/'config.json'
    if not cfg.is_file():return None
    import json
    # Refuse to overwrite a damaged/partially-written configuration silently.
    try:
        data=json.loads(cfg.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('positions'),list):
            raise ValueError('配置字段不完整')
    except (ValueError,OSError) as exc:
        raise RuntimeError(f'原配置无法解析，升级已停止以免数据丢失：{exc}') from exc
    dest=BASE/'backups'/('before_v3.6.1_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    dest.mkdir(parents=True,exist_ok=False,mode=0o700)
    shutil.copy2(cfg,dest/'config.json')
    for name in ('manual_trade_log.jsonl','equity_observations.jsonl'):
        source=OUTPUT/name
        if source.is_file():shutil.copy2(source,dest/name)
    print('已备份原持仓配置及交易记录到：',dest,flush=True)
    return dest


def build_env():
    python = APP / '.venv' / 'bin' / 'python'
    if not python.is_file():
        print('首次创建独立 Python 环境（无需管理员权限）……', flush=True)
        cmd([sys.executable, '-m', 'venv', str(APP / '.venv')], check=True)
    print('安装/核对依赖，可能需要联网……', flush=True)
    install = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(APP / 'requirements.txt')], cwd=str(APP))
    if install.returncode:
        raise RuntimeError('依赖安装失败：请检查网络、Python版本及上方错误')
    test = cmd([str(python), '-c', 'import flask, akshare, pandas, waitress; import serve_web'], cwd=str(APP))
    if test.returncode:
        raise RuntimeError('Python依赖导入失败：' + test.stderr[-700:])
    return python


def schedule_local_time():
    # All previous installers use Mac local time. Convert 15:45 Beijing time to Mac local.
    cn = datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=15, minute=45, second=0, microsecond=0)
    local = cn.astimezone()
    return local.hour, local.minute


def write_agent(label, args, *, always_running=False, at=None):
    OUTPUT.mkdir(exist_ok=True, parents=True)
    spec = {
        'Label': label,
        'ProgramArguments': [str(x) for x in args],
        'WorkingDirectory': str(APP),
        'StandardOutPath': str(OUTPUT / f'{label}.out.log'),
        'StandardErrorPath': str(OUTPUT / f'{label}.err.log'),
        'ProcessType': 'Background',
    }
    if always_running:
        spec.update(RunAtLoad=True, KeepAlive=True, ThrottleInterval=60)
    else:
        hour, minute = at
        spec['RunAtLoad'] = False
        spec['StartCalendarInterval'] = [{'Weekday': day, 'Hour': hour, 'Minute': minute}
                                         for day in range(1, 6)]
    path = plist_path(label)
    path.write_bytes(plistlib.dumps(spec, sort_keys=True))
    result = cmd(['launchctl', 'bootstrap', domain(), str(path)])
    if result.returncode:
        raise RuntimeError(f'登录自启任务加载失败 {label}: {result.stderr.strip() or result.stdout.strip()}')


def create_shortcut():
    desktop = HOME / 'Desktop'
    if desktop.is_dir():
        link = desktop / '衡势A股量化助手.webloc'
        link.write_bytes(plistlib.dumps({'URL': URL}))
        return str(link)
    return None


def webpage_html():
    try:
        req = urllib.request.Request(URL + '?version_check=1', headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=2.0) as r:
            if r.status == 200 and 'html' in r.headers.get('Content-Type', '').lower():
                return r.read(200000).decode('utf-8', errors='replace')
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return ''


def webpage_responding():
    return bool(webpage_html())


def served_version():
    import re
    html = webpage_html()
    match = re.search(r'LOCAL RESEARCH / V(\d+(?:\.\d+)+)', html)
    if not match:
        match = re.search(r'class="version">v(\d+(?:\.\d+)+)', html)
    return match.group(1) if match else ('无法识别' if html else '服务未响应')


def print_status():
    print('\n--- 本机服务状态 ---')
    for alias, label in LABELS.items():
        r = cmd(['launchctl', 'print', domain() + '/' + label])
        print(f'{alias}:', '已注册' if r.returncode == 0 else '未注册')
    versionfile = APP / 'VERSION.txt'
    installed = versionfile.read_text(encoding='utf-8').strip() if versionfile.is_file() else '未找到'
    print('已安装程序版本:', installed)
    print('网页实际显示版本:', served_version())
    print('网页:', '已响应 ' + URL if webpage_responding() else '尚未响应（检查 8765 端口及日志）')
    if OUTPUT.is_dir():
        print('日志目录:', OUTPUT)
    print('持仓配置:', APP / 'config.json')


def install():
    if sys.platform != 'darwin':
        raise RuntimeError('一键登录自启仅适用于 macOS，请在 Apple M 系列 Mac 上运行')
    if sys.version_info < (3, 11):
        raise RuntimeError('需要 Python 3.11 或更高版本（推荐 3.13）')
    AGENTS.mkdir(parents=True, exist_ok=True)
    print('正在配置当前 Mac 用户的登录自启服务；无需 sudo。')
    backup_current_config()
    # Stop older V3.x scheduled services (same labels) before replacing their paths.
    for label in LABELS.values():
        stop_agent(label, remove=True)
    release_owned_listener()
    # Copy code first, but do not overwrite the user's persistent config/output.
    old_config = APP / 'config.json'
    if not old_config.exists():
        # Keep initial config until we try to import an existing v3.x file.
        cfg_src = SOURCE / 'config.json'
        # Temporarily avoid copying default config so we can prefer a prior user-edited one.
        if cfg_src.exists():
            default = cfg_src.read_bytes()
            copy_sources()
            (APP / 'config.json').unlink(missing_ok=True)
            if not (APP / 'config.json').exists():
                message = import_previous_config()
            else:
                message = '保留现有持仓配置'
        else:
            raise RuntimeError('缺少原始 config.json')
    else:
        copy_sources()
        message = '已保留已安装版本的持仓、自选与止盈止损配置'
    print(message)
    python = build_env()
    hour, minute = schedule_local_time()
    write_agent(LABELS['web'], [python, APP/'serve_web.py'], always_running=True)
    write_agent(LABELS['intraday'], [python, APP/'intraday_monitor.py', '--mode', 'live', '--interval', '10', '--notify'], always_running=True)
    write_agent(LABELS['phase2'], [python, APP/'phase2.py', '--mode', 'live', '--top', '60', '--notify'], at=(hour, minute))
    short = create_shortcut()
    print('V3.6.1登录自启已注册：本地网页 + 盘中监控 + 周一至周五盘后扫描（包括每日精选观察）')
    print(f'盘后扫描：Mac本地时间 {hour:02d}:{minute:02d}（当前时区，北京时间约15:45）')
    print('等待本地网页启动……', flush=True)
    for _ in range(25):
        if webpage_responding():
            break
        time.sleep(.4)
    print_status()
    if short:
        print('已在桌面创建快捷方式:', short)
    actual = served_version()
    if actual != EXPECTED_VERSION:
        raise RuntimeError(f'升级未完成：网页目前显示 V{actual}，目标是 V{EXPECTED_VERSION}。请将上方检测结果发给我，别以旧网页作为新功能使用。')
    import webbrowser
    webbrowser.open(URL)
    print('\n以后每天：Mac登录后，直接双击桌面快捷方式或浏览器书签，不需要打开终端。')


def uninstall():
    for label in LABELS.values():
        stop_agent(label, remove=True)
    print('已关闭并移除本用户的三项开机自启任务。')
    print('保留安装程序、持仓配置和历史报告:', BASE)


def open_dashboard():
    if not webpage_responding():
        cmd(['launchctl', 'kickstart', '-k', domain() + '/' + LABELS['web']])
        for _ in range(20):
            if webpage_responding():
                break
            time.sleep(.35)
    if webpage_responding():
        import webbrowser
        webbrowser.open(URL)
        print('已打开：', URL)
    else:
        print('本机服务没有响应。请先双击安装程序或“检查运行状态.command”。')
        print_status()
        raise RuntimeError('无法打开本地网页')


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--install', action='store_true')
    g.add_argument('--uninstall', action='store_true')
    g.add_argument('--status', action='store_true')
    g.add_argument('--open', action='store_true')
    a = p.parse_args()
    if a.install: install()
    elif a.uninstall: uninstall()
    elif a.status: print_status()
    elif a.open: open_dashboard()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('\n安装/运行失败：', e, file=sys.stderr)
        sys.exit(1)
