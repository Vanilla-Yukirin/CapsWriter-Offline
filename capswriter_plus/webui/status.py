"""Web UI 使用的只读运行状态采集器。"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _run_command(args: list[str], timeout: float = 2.0) -> Optional[str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in {0, 3}:
        return None
    return result.stdout


def _parse_properties(output: Optional[str]) -> dict[str, str]:
    if not output:
        return {}
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def _process_uptime(pid: int) -> Optional[int]:
    if pid <= 0 or os.name != "posix":
        return None
    try:
        process_fields = Path(f"/proc/{pid}/stat").read_text().split()
        process_started = float(process_fields[21]) / os.sysconf("SC_CLK_TCK")
        system_uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return max(0, int(system_uptime - process_started))


def _child_pids(pid: int) -> list[int]:
    if pid <= 0 or os.name != "posix":
        return []
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text().strip()
        return [int(value) for value in text.split() if value.isdigit()]
    except OSError:
        return []


def _established_connections(port: int) -> Optional[int]:
    if os.name != "posix":
        return None
    target_port = f"{port:04X}"
    count = 0
    inspected = False
    for proc_file in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = proc_file.read_text().splitlines()[1:]
        except OSError:
            continue
        inspected = True
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local_port = fields[1].rsplit(":", 1)[-1].upper()
            state = fields[3]
            if local_port == target_port and state == "01":
                count += 1
    return count if inspected else None


def _port_is_listening(port: int) -> Optional[bool]:
    """从 procfs 读取监听状态，避免用探测连接污染 ASR 握手日志。"""
    if os.name != "posix":
        return None
    target_port = f"{port:04X}"
    inspected = False
    for proc_file in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = proc_file.read_text().splitlines()[1:]
        except OSError:
            continue
        inspected = True
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local_port = fields[1].rsplit(":", 1)[-1].upper()
            if local_port == target_port and fields[3] == "0A":
                return True
    return False if inspected else None


def _gpu_status() -> list[dict[str, Any]]:
    output = _run_command(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=2.0,
    )
    if not output:
        return []

    gpus: list[dict[str, Any]] = []
    for row in csv.reader(output.splitlines()):
        if len(row) != 4:
            continue
        try:
            gpus.append(
                {
                    "name": row[0].strip(),
                    "memory_total_mb": int(row[1].strip()),
                    "memory_used_mb": int(row[2].strip()),
                    "utilization_percent": int(row[3].strip()),
                }
            )
        except ValueError:
            continue
    return gpus


def _wrapper_status(url: str) -> dict[str, Any]:
    if not url:
        return {"reachable": False, "status": "not_configured", "connected": None}
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=1.0) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return {"reachable": False, "status": "unavailable", "connected": None}
    return {
        "reachable": True,
        "status": str(payload.get("status", "unknown")),
        "connected": payload.get("connected") if isinstance(payload.get("connected"), bool) else None,
    }


class StatusCollector:
    """从固定来源采集状态；所有失败均降级为 unavailable。"""

    def __init__(
        self,
        *,
        asr_service: str,
        asr_port: int,
        webui_port: int,
        wrapper_health_url: str,
        log_path: Path,
        build_id: str,
        webui_started_at: float,
    ) -> None:
        self.asr_service = asr_service
        self.asr_port = asr_port
        self.webui_port = webui_port
        self.wrapper_health_url = wrapper_health_url
        self.log_path = log_path
        self.build_id = build_id
        self.webui_started_at = webui_started_at

    def overview(self) -> dict[str, Any]:
        output = _run_command(
            [
                "systemctl",
                "--user",
                "show",
                self.asr_service,
                "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,Result,NRestarts",
                "--no-pager",
            ]
        )
        properties = _parse_properties(output)
        try:
            main_pid = int(properties.get("MainPID", "0"))
        except ValueError:
            main_pid = 0

        try:
            log_stat = self.log_path.stat()
            log_info = {
                "available": True,
                "size_bytes": log_stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    log_stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "filename": self.log_path.name,
            }
        except OSError:
            log_info = {
                "available": False,
                "size_bytes": 0,
                "modified_at": None,
                "filename": self.log_path.name,
            }

        try:
            restart_count = int(properties.get("NRestarts", "0") or 0)
        except ValueError:
            restart_count = 0

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": {"capswriter": "2.6", "extension": "Phase 1A", "build": self.build_id},
            "asr": {
                "service": self.asr_service,
                "active": properties.get("ActiveState") == "active",
                "state": properties.get("ActiveState", "unavailable"),
                "sub_state": properties.get("SubState", "unavailable"),
                "result": properties.get("Result", "unavailable"),
                "main_pid": main_pid or None,
                "worker_pids": _child_pids(main_pid),
                "started_at": properties.get("ExecMainStartTimestamp") or None,
                "uptime_seconds": _process_uptime(main_pid),
                "restart_count": restart_count,
                "port": self.asr_port,
                "listening": _port_is_listening(self.asr_port),
                "connections": _established_connections(self.asr_port),
            },
            "webui": {
                "active": True,
                "state": "active",
                "pid": os.getpid(),
                "port": self.webui_port,
                "listen": "127.0.0.1",
                "uptime_seconds": max(0, int(time.monotonic() - self.webui_started_at)),
            },
            "wrapper": _wrapper_status(self.wrapper_health_url),
            "gpu": _gpu_status(),
            "log": log_info,
        }

    def safe_config(self) -> dict[str, Any]:
        from config_server import Qwen3ASRGGUFArgs, ServerConfig

        return {
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
                "name": "Qwen3-ASR-1.7B" if ServerConfig.model_type == "qwen_asr" else ServerConfig.model_type,
                "context_size": Qwen3ASRGGUFArgs.n_ctx,
                "chunk_seconds": Qwen3ASRGGUFArgs.chunk_size,
                "memory_segments": Qwen3ASRGGUFArgs.memory_num,
                "gpu_enabled": Qwen3ASRGGUFArgs.llm_use_gpu,
            },
            "webui": {
                "listen": "127.0.0.1",
                "port": self.webui_port,
                "read_only": True,
                "public_ingress_configured": False,
            },
            "wrapper": {
                "health_url": self.wrapper_health_url or "未配置",
                "port": 9600 if self.wrapper_health_url else None,
            },
            "features": {
                "webui": True,
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
