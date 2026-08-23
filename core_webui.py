# coding: utf-8
"""CapsWriter Phase 1A Web UI 启动入口。"""

from capswriter_plus.security import SecurityConfigError
from capswriter_plus.webui import run


if __name__ == "__main__":
    try:
        run()
    except (SecurityConfigError, ValueError) as exc:
        raise SystemExit(f"CapsWriter Web UI 启动失败：{exc}") from exc
