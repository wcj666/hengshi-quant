#!/bin/bash
# Python discovery for Finder-launched macOS .command scripts.
# Intended to be sourced by the installer and checker. Bash 3.2 compatible.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.pyenv/bin:$HOME/miniconda3/bin:$HOME/anaconda3/bin:$PATH"

FOUND_PYTHON=""
FOUND_VERSION=""

_check_python_candidate() {
  local candidate="$1"
  local version
  [[ -n "$candidate" && -x "$candidate" && ! -d "$candidate" ]] || return 1
  version="$("$candidate" -c 'import sys, venv; assert sys.version_info >= (3,11); print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null)" || return 1
  [[ -n "$version" ]] || return 1
  FOUND_PYTHON="$candidate"
  FOUND_VERSION="$version"
  return 0
}

find_compatible_python() {
  FOUND_PYTHON=""
  FOUND_VERSION=""
  local exe candidate dir root folder
  # Standard shell PATH, with Homebrew and python.org framework locations appended.
  for exe in python3.13 python3.12 python3.11 python3.14 python3.15 python3; do
    candidate="$(command -v "$exe" 2>/dev/null || true)"
    _check_python_candidate "$candidate" && return 0
  done
  # Python installed via Homebrew, python.org, conda, pyenv, asdf, mise, uv.
  for dir in \
    /opt/homebrew/bin /usr/local/bin \
    /opt/homebrew/opt/python@3.11/bin /opt/homebrew/opt/python@3.12/bin \
    /opt/homebrew/opt/python@3.13/bin /opt/homebrew/opt/python@3.14/bin \
    /usr/local/opt/python@3.11/bin /usr/local/opt/python@3.12/bin \
    /usr/local/opt/python@3.13/bin /usr/local/opt/python@3.14/bin \
    /opt/homebrew/opt/python@3.11/libexec/bin /opt/homebrew/opt/python@3.12/libexec/bin \
    /opt/homebrew/opt/python@3.13/libexec/bin /opt/homebrew/opt/python@3.14/libexec/bin \
    "$HOME/miniconda3/bin" "$HOME/anaconda3/bin" "$HOME/miniforge3/bin" \
    "$HOME/opt/miniconda3/bin" "$HOME/Library/Application Support/AshareAB/app/.venv/bin" \
    /usr/bin; do
    for candidate in "$dir"/python3 "$dir"/python3.[0-9]* "$dir"/python; do
      _check_python_candidate "$candidate" && return 0
    done
  done
  for dir in \
    /Library/Frameworks/Python.framework/Versions/*/bin \
    /opt/homebrew/Cellar/python@3.*/3.*/bin \
    /usr/local/Cellar/python@3.*/3.*/bin \
    "$HOME"/.pyenv/versions/*/bin \
    "$HOME"/.asdf/installs/python/*/bin \
    "$HOME"/.local/share/mise/installs/python/*/bin \
    "$HOME"/.local/share/uv/python/*/bin \
    "$HOME"/Library/Python/*/bin; do
    for candidate in "$dir"/python3 "$dir"/python3.[0-9]* "$dir"/python; do
      _check_python_candidate "$candidate" && return 0
    done
  done
  # Existing app virtual environments may be valid even when Finder has a narrow PATH.
  for root in "$HOME/Downloads" "$HOME/Documents" "$HOME/Desktop" "$HOME/文稿"; do
    for folder in \
      a_share_ab_web_v3_5_1 a_share_ab_web_v3_5 a_share_ab_web_v3_4_1 \
      a_share_ab_web_v3_4 a_share_ab_web_v3_3 a_share_ab_web_v3_2 \
      a_share_ab_web_v3_1 a_share_ab_web_v3 a_share_ab_v2 a_share_ab; do
      for candidate in "$root/$folder/.venv/bin/python" "$root/$folder/.venv/bin/python3"; do
        _check_python_candidate "$candidate" && return 0
      done
    done
  done
  # GUI launch may not load .zprofile. Ask a login shell as a final, bounded fallback.
  if [[ -x /bin/zsh ]]; then
    for exe in python3.13 python3.12 python3.11 python3.14 python3; do
      candidate="$(/bin/zsh -lc "command -v $exe" 2>/dev/null | head -n 1)"
      _check_python_candidate "$candidate" && return 0
    done
  fi
  return 1
}

print_python_diagnostics() {
  echo "检查时间：$(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "系统架构：$(uname -m)"
  echo "macOS 版本：$(sw_vers -productVersion 2>/dev/null || echo 未知)"
  echo "当前目录：$(pwd)"
  echo "检测 PATH：$PATH"
  echo "--- 已发现的 Python 命令 ---"
  local exe candidate
  for exe in python python3 python3.10 python3.11 python3.12 python3.13 python3.14 python3.15; do
    candidate="$(command -v "$exe" 2>/dev/null || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s -> %s : ' "$exe" "$candidate"
      "$candidate" --version 2>&1 || true
    fi
  done
  echo "--- python.org 常见路径 ---"
  for candidate in /Library/Frameworks/Python.framework/Versions/*/bin/python3; do
    if [[ -x "$candidate" ]]; then
      printf '%s : ' "$candidate"
      "$candidate" --version 2>&1 || true
    fi
  done
  echo "--- 旧版虚拟环境（如存在）---"
  for candidate in "$HOME"/{Downloads,Documents,Desktop}/a_share_ab*/.venv/bin/python; do
    if [[ -x "$candidate" ]]; then
      printf '%s : ' "$candidate"
      "$candidate" --version 2>&1 || true
    fi
  done
  if find_compatible_python; then
    echo "检测结果：可用 Python $FOUND_VERSION"
    echo "使用路径：$FOUND_PYTHON"
  else
    echo "检测结果：未在已扫描路径发现 Python 3.11+。不代表电脑上一定没有安装。"
    echo "可从 https://www.python.org/downloads/macos/ 安装 Python 3.13 macOS universal2。"
  fi
}
