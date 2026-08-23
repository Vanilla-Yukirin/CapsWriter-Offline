"""Product-owned runtime status for CapsWriter Plus server components."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any


def _normalized_provider(provider: object) -> str:
    value = str(provider or "unknown").strip()
    return value.upper() if value else "UNKNOWN"


def _provider_device(provider: str) -> str:
    if provider in {"CPU", "CPUEXECUTIONPROVIDER"}:
        return "cpu"
    if provider in {
        "CUDA",
        "CUDAEXECUTIONPROVIDER",
        "DML",
        "DMLEXECUTIONPROVIDER",
        "TENSORRT",
        "TENSORRTEXECUTIONPROVIDER",
        "COREML",
        "COREMLEXECUTIONPROVIDER",
    }:
        return "gpu"
    return "unknown"


def describe_configured_device(model_type: str | None = None) -> dict[str, Any]:
    """Describe the device choices passed to the selected ASR engine."""
    from config_server import (
        FunASRNanoGGUFArgs,
        ParaformerArgs,
        Qwen3ASRGGUFArgs,
        SenseVoiceArgs,
        ServerConfig,
    )

    selected_model = (model_type or ServerConfig.model_type).lower()
    components: list[dict[str, str]] = []

    if selected_model in {"qwen_asr", "fun_asr_nano"}:
        args = (
            Qwen3ASRGGUFArgs
            if selected_model == "qwen_asr"
            else FunASRNanoGGUFArgs
        )
        provider = _normalized_provider(args.onnx_provider)
        components.append(
            {
                "name": "encoder",
                "backend": f"ONNX Runtime ({provider})",
                "device": _provider_device(provider),
            }
        )
        components.append(
            {
                "name": "decoder",
                "backend": "GGUF / llama.cpp",
                "device": "gpu" if bool(args.llm_use_gpu) else "cpu",
            }
        )
    elif selected_model == "sensevoice":
        provider = _normalized_provider(SenseVoiceArgs.onnx_provider)
        components.append(
            {
                "name": "recognizer",
                "backend": f"ONNX Runtime ({provider})",
                "device": _provider_device(provider),
            }
        )
    elif selected_model == "paraformer":
        provider = _normalized_provider(ParaformerArgs.provider)
        components.append(
            {
                "name": "recognizer",
                "backend": f"sherpa-onnx ({provider})",
                "device": _provider_device(provider),
            }
        )

    devices = {component["device"] for component in components}
    if "gpu" in devices:
        mode = "gpu"
    elif devices == {"cpu"}:
        mode = "cpu"
    else:
        mode = "unknown"

    component_labels = [
        f"{component['name']} {component['device'].upper()}"
        for component in components
    ]
    label = " · ".join(component_labels) if component_labels else "设备配置未知"
    return {
        "mode": mode,
        "label": label,
        "components": components,
        "source": "engine_configuration",
        "telemetry": None,
    }


def build_server_runtime_status(app: Any) -> dict[str, Any]:
    """Build a cross-platform snapshot from the running CapsWriter server."""
    from config_server import ServerConfig, __version__

    process_manager = app.process_manager
    worker = process_manager.process
    worker_alive = bool(worker and worker.is_alive())
    listening = bool(app.socket_manager.is_running)
    ready = bool(process_manager.models_ready and worker_alive and listening)
    if ready:
        state = "ready"
    elif app.is_alive:
        state = "starting"
    else:
        state = "stopped"

    sockets = app.state.sockets_id
    try:
        connection_count = len(sockets) if sockets is not None else 0
    except (OSError, TypeError):
        connection_count = 0

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": {"capswriter": __version__},
        "model": {"type": ServerConfig.model_type},
        "asr": {
            "active": bool(app.is_alive),
            "ready": ready,
            "state": state,
            "main_pid": os.getpid(),
            "worker_pid": worker.pid if worker_alive else None,
            "uptime_seconds": max(0, int(time.monotonic() - app.started_at)),
            "port": int(ServerConfig.port),
            "listening": listening,
            "connections": connection_count,
        },
        "device": describe_configured_device(ServerConfig.model_type),
    }
