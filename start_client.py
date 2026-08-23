# coding: utf-8
import os
from capswriter_plus.security import SecurityConfigError
from core.client import CapsWriterClient

if __name__ == "__main__":
    # 直接实例化并启动门面类即可
    # 环境初始化职责已下放至 CapsWriterClient
    try:
        CapsWriterClient().start()
    except SecurityConfigError as exc:
        raise SystemExit(f"CapsWriter 客户端启动失败：{exc}") from exc
