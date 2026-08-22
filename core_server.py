# coding: utf-8
"""
Linux 启动入口 launcher。
2.6 官方源码没有顶层入口（Windows 走 PyInstaller 打的 start_server.exe），
这里手写一个 launcher 供 systemd/命令行使用。
"""
from core.server.app import CapsWriterServer

if __name__ == '__main__':
    CapsWriterServer().start()
