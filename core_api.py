"""CapsWriter Plus HTTP/Agent API entry point."""

from capswriter_plus.api.server import run
from capswriter_plus.security import SecurityConfigError


if __name__ == "__main__":
    try:
        run()
    except (SecurityConfigError, ValueError) as exc:
        raise SystemExit(f"CapsWriter HTTP API 启动失败：{exc}") from exc
