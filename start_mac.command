#!/bin/zsh
set -e
cd "${0:A:h}"
if ! command -v python3 >/dev/null 2>&1; then
  echo '未找到 python3。请先安装 Python 3.11 或更新版本。'
  exit 1
fi
if [[ ! -f .venv/bin/python ]]; then
  echo '首次安装：创建独立 Python 环境'
  python3 -m venv .venv
fi
source .venv/bin/activate
if ! python -c 'import flask, pandas, numpy, akshare' >/dev/null 2>&1; then
  echo '安装依赖（需要联网）...'
  python -m pip install -r requirements.txt
fi
echo '浏览器将打开 http://127.0.0.1:8765'
echo '关闭服务：在此终端按 Control + C。'
python web_dashboard.py --open
