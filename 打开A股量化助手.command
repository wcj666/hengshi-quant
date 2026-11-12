#!/bin/zsh
cd "${0:A:h}"
/usr/bin/python3 setup_autostart.py --open
result=$?
if [[ "$result" -ne 0 ]]; then read '?按回车关闭...'; fi
exit "$result"
