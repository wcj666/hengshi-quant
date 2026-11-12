#!/bin/zsh
cd "${0:A:h}"
/usr/bin/python3 setup_autostart.py --uninstall
read '?按回车关闭...'
