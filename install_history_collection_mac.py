"""Install this user's after-close Tencent archive job on macOS."""
import argparse
from pathlib import Path
import os
import plistlib
import subprocess
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parent
LABEL = 'local.ashare.ab.history'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def job_spec(root):
    return {
        'Label': LABEL,
        'ProgramArguments': [str(root / '.venv/bin/python'), str(root / 'history_collection.py')],
        'WorkingDirectory': str(root),
        'RunAtLoad': True,
        'StartCalendarInterval': [
            {'Weekday': weekday, 'Hour': hour, 'Minute': minute}
            for weekday in range(1, 6) for hour, minute in ((15, 20), (16, 10))
        ],
        'StandardOutPath': str(root / 'output' / 'history_collection.log'),
        'StandardErrorPath': str(root / 'output' / 'history_collection_error.log'),
        'ProcessType': 'Background',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uninstall', action='store_true')
    args = parser.parse_args()
    domain = f'gui/{os.getuid()}'
    if args.uninstall:
        subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
        PLIST.unlink(missing_ok=True)
        return
    spec = job_spec(ROOT)
    if any(not Path(path).is_file() for path in spec['ProgramArguments']):
        parser.error('请在完整衡势安装目录运行采集任务安装器')
    (ROOT/'output').mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    before = PLIST.read_bytes() if PLIST.is_file() else None

    def save(payload):
        with NamedTemporaryFile(dir=PLIST.parent, delete=False) as file:
            file.write(payload)
            temporary = Path(file.name)
        temporary.replace(PLIST)

    subprocess.run(['launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
    save(plistlib.dumps(spec))
    try:
        subprocess.run(['launchctl', 'bootstrap', domain, str(PLIST)], check=True)
    except subprocess.CalledProcessError:
        if before is None:
            PLIST.unlink(missing_ok=True)
        else:
            save(before)
            subprocess.run(['launchctl', 'bootstrap', domain, str(PLIST)], capture_output=True)
        raise
    print('已安装盘后历史采集：工作日 15:20，16:10 复查缺项。入口仍按北京时间及交易日历核验。')


if __name__ == '__main__':
    main()
