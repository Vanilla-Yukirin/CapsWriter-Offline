#!/usr/bin/env python3
"""
CapsWriter 2.6 HTTP 文件转录服务

绕过 GUI/音频/快捷键模块，直接组装 FileTranscriber + HotwordManager，
通过 HTTP POST /transcribe 接收文件路径，返回完整转录结果（含热词后处理）。

用法:
    cd /data/CapsWriter-Offline
    .venv/bin/python /root/scripts/caps-client/server.py --port 9600

API:
    POST /transcribe
    Body: {"file": "/path/to/audio.m4a"}
    Response: {"ok": true, "text": "识别结果...", "files": {"txt": "...", "srt": "...", "json": "..."}}

    GET /health
    Response: {"status": "ok", "connected": true}
"""

import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path

# CapsWriter-Offline 路径
CAPS_DIR = Path('/data/CapsWriter-Offline')
sys.path.insert(0, str(CAPS_DIR))

from config_client import ClientConfig as Config
from core.client.state import ClientState, console
from core.client.connection import WebSocketManager
from core.client.transcribe.file_transcriber import FileTranscriber
from core.client.hotword.manager import HotwordManager


# ── 最小化 App 包装 ──────────────────────────────────────────────

class TranscribeApp:
    """提供 FileTranscriber 需要的接口：state / ws / hotword / loop"""

    def __init__(self, hotword_files: dict = None):
        self.state = ClientState()
        self.state.app = self
        self.ws = WebSocketManager(self)
        self.loop = asyncio.get_event_loop()
        self.hotword = HotwordManager(
            hotword_files=hotword_files,
            threshold=Config.hot_thresh,
            similar_threshold=Config.hot_similar,
        )
        self.hotword.load_all()


# ── 转录核心 ──────────────────────────────────────────────────────

async def transcribe(app: TranscribeApp, file_path: str) -> dict:
    """执行文件转录并返回结果"""
    file = Path(file_path)

    if not file.exists():
        return {"error": f"文件不存在: {file_path}"}

    console.print(f'\n[cyan]收到转录请求: {file}[/cyan]')

    transcriber = FileTranscriber(app, file)

    if not await transcriber.check():
        return {"error": "检查失败（文件不存在或无法连接 CapsWriter 服务端）"}

    await transcriber.send()
    await transcriber.receive()
    await transcriber.close()

    # 收集输出文件路径
    results = {}
    for ext, label in [('.txt', 'txt'), ('.srt', 'srt'), ('.json', 'json')]:
        p = file.with_suffix(ext)
        if p.exists():
            results[label] = str(p)

    # 读取识别文本
    txt_path = file.with_suffix('.txt')
    text = txt_path.read_text(encoding='utf-8') if txt_path.exists() else ''

    return {"ok": True, "text": text, "files": results}


# ── HTTP Server ─────────────────────────────────────────────────────

async def handle_client(app: TranscribeApp, reader, writer):
    """简易异步 HTTP 处理器"""
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=60)
        request = data.decode('utf-8', errors='replace')

        lines = request.split('\r\n')
        if not lines:
            writer.close()
            return

        method, path, _ = lines[0].split(' ', 2)

        # 提取 body
        body = ''
        if '\r\n\r\n' in request:
            body = request.split('\r\n\r\n', 1)[1]

        status = 200
        response = ''

        if method == 'POST' and path == '/transcribe':
            try:
                req = json.loads(body) if body else {}
            except json.JSONDecodeError:
                status = 400
                response = json.dumps({"error": "无效 JSON"}, ensure_ascii=False)
            else:
                file_path = req.get('file', '')
                if not file_path:
                    status = 400
                    response = json.dumps({"error": "缺少 file 参数"}, ensure_ascii=False)
                else:
                    try:
                        result = await transcribe(app, file_path)
                        response = json.dumps(result, ensure_ascii=False)
                    except Exception as e:
                        traceback.print_exc()
                        status = 500
                        response = json.dumps({"error": str(e)}, ensure_ascii=False)

        elif method == 'GET' and path == '/health':
            response = json.dumps({
                "status": "ok",
                "connected": app.state.is_connected,
            }, ensure_ascii=False)

        else:
            status = 404
            response = json.dumps({"error": "not found"}, ensure_ascii=False)

        http_response = (
            f"HTTP/1.1 {status} OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(response.encode('utf-8'))}\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{response}"
        )
        writer.write(http_response.encode('utf-8'))
        await writer.drain()

    except asyncio.TimeoutError:
        pass
    except Exception:
        traceback.print_exc()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CapsWriter 2.6 HTTP 文件转录服务"
    )
    parser.add_argument('--host', default='127.0.0.1', help='监听地址（默认 127.0.0.1）')
    parser.add_argument('--port', type=int, default=9600, help='监听端口（默认 9600）')
    parser.add_argument('--hotfile', default='/data/hot-2.6-local/hot.txt', help='热词文件路径')
    parser.add_argument('--hotrule', default='/data/hot-2.6-local/hot-rule.txt', help='规则文件路径')
    args = parser.parse_args()

    hotword_files = {
        'hot': Path(args.hotfile),
        'rule': Path(args.hotrule),
    }

    app = TranscribeApp(hotword_files=hotword_files)

    async def serve():
        server = await asyncio.start_server(
            lambda r, w: handle_client(app, r, w),
            args.host, args.port,
        )
        addr = server.sockets[0].getsockname()
        console.print(f'[bold green]CapsWriter HTTP 转录服务已启动[/bold green]')
        console.print(f'  地址: http://{addr[0]}:{addr[1]}')
        console.print(f'  热词: {args.hotfile}')
        console.print(f'  规则: {args.hotrule}')
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


if __name__ == '__main__':
    main()
