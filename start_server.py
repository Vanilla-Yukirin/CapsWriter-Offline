# coding: utf-8
from multiprocessing import freeze_support
from capswriter_plus.security import SecurityConfigError
from core.server.app import CapsWriterServer

if __name__ == '__main__':
    # 启用对 PyInstaller 打包后的多进程支持
    freeze_support()
    
    # 直接实例化并启动门面类即可
    # 环境初始化职责已下放至 CapsWriterServer
    try:
        CapsWriterServer().start()
    except SecurityConfigError as exc:
        raise SystemExit(f"CapsWriter 服务端启动失败：{exc}") from exc
