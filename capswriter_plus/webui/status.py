"""Cross-platform, read-only status exposed by the CapsWriter Plus Web UI."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StatusCollector:
    """Collect only product-owned state; deployment integrations are optional layers."""

    def __init__(
        self,
        *,
        token: str,
        instance_name: str,
        asr_status_url: str,
        asr_port: int,
        webui_host: str,
        webui_port: int,
        log_path: Path,
        build_id: str,
        webui_started_at: float,
    ) -> None:
        self.token = token
        self.instance_name = instance_name
        self.asr_status_url = asr_status_url
        self.asr_port = asr_port
        self.webui_host = webui_host
        self.webui_port = webui_port
        self.log_path = log_path
        self.build_id = build_id
        self.webui_started_at = webui_started_at

    def _log_info(self) -> dict[str, Any]:
        try:
            log_stat = self.log_path.stat()
        except OSError:
            return {
                "available": False,
                "size_bytes": 0,
                "modified_at": None,
                "filename": self.log_path.name,
            }
        return {
            "available": True,
            "size_bytes": log_stat.st_size,
            "modified_at": datetime.fromtimestamp(
                log_stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "filename": self.log_path.name,
        }

    def overview(self) -> dict[str, Any]:
        """Return the ASR self-report, degrading to unknown without false alarms."""
        from config_server import __version__

        asr = {
            "active": None,
            "ready": None,
            "state": "awaiting_runtime_status",
            "main_pid": None,
            "worker_pid": None,
            "uptime_seconds": None,
            "port": self.asr_port,
            "listening": None,
            "connections": None,
        }
        device = {
            "mode": "unknown",
            "label": "等待 ASR 运行时报告",
            "components": [],
            "telemetry": None,
        }
        runtime = self._runtime_status()
        if runtime:
            runtime_asr = runtime.get("asr")
            runtime_device = runtime.get("device")
            if isinstance(runtime_asr, dict):
                asr.update(runtime_asr)
            if isinstance(runtime_device, dict):
                device.update(runtime_device)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "instance": {"name": self.instance_name},
            "version": {
                "capswriter": __version__,
                "extension": "CapsWriter Plus Web UI",
                "build": self.build_id,
            },
            "asr": asr,
            "device": device,
            "webui": {
                "active": True,
                "state": "active",
                "pid": os.getpid(),
                "port": self.webui_port,
                "listen": self.webui_host,
                "uptime_seconds": max(
                    0, int(time.monotonic() - self.webui_started_at)
                ),
            },
            "log": self._log_info(),
        }

    def _runtime_status(self) -> dict[str, Any] | None:
        request = urllib.request.Request(
            self.asr_status_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                raw = response.read(128 * 1024 + 1)
        except (OSError, urllib.error.URLError):
            return None
        if len(raw) > 128 * 1024:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def safe_config(self) -> dict[str, Any]:
        from config_server import ServerConfig
        from capswriter_plus.runtime_status import describe_configured_device

        device = describe_configured_device(ServerConfig.model_type)

        return {
            "instance": {"name": self.instance_name},
            "server": {
                "listen": ServerConfig.addr,
                "port": int(ServerConfig.port),
                "model_type": ServerConfig.model_type,
                "log_level": ServerConfig.log_level,
                "aligner_idle_timeout_seconds": ServerConfig.aligner_idle_timeout,
                "format_numbers": ServerConfig.format_num,
                "format_spacing": ServerConfig.format_spell,
            },
            "model": {
                "type": ServerConfig.model_type,
                "configured_device": device["mode"],
                "components": " · ".join(
                    f"{component['name']}: {component['backend']} / {component['device'].upper()}"
                    for component in device["components"]
                ),
            },
            "webui": {
                "listen": self.webui_host,
                "port": self.webui_port,
                "read_only": True,
            },
            "features": {
                "webui": True,
                "http_transcription_api": False,
                "hotword_editor": False,
                "model_reload": False,
                "tts": False,
                "feedback_agent": False,
            },
            "security": {
                "token_configured": True,
                "minimum_token_length": 16,
                "token_visible": False,
            },
        }
