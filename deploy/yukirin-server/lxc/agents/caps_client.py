#!/usr/bin/env python3
"""
CapsWriter Offline CLI Client — 短连接文件转录工具 (适配 CapsWriter 2.6)

用法:
    python3 caps_client.py <音频文件> [-o 输出目录] [-s 服务端地址:端口]
    python3 caps_client.py recording.m4a -o ./output -s 192.168.2.105:6016

新增 2.6 支持:
    --context "人名 地名 术语"    # 识别上下文（辅助 ASR 引擎）
    --hotfile hot.txt             # 从热词文件自动解析 context（可与 --context 叠加）
    --language zh                 # 识别语言（auto/zh/en/ja 等）
"""

import argparse
import asyncio
import base64
import difflib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import websockets


# ── 热词文件解析 ──────────────────────────────────────────────────

def parse_hotfile(path: str) -> str:
    """
    解析热词文件，提取有效热词为空格分隔的 context 字符串。

    规则：
    - # 开头的行为注释，跳过
    - 空行跳过
    - 支持 | 别名语法：CapsWriter | Caps Rider → 取所有别名
    - 去掉 "input xxx" 这类自定义短语（它们是快捷键展开，不是 ASR 热词）
    """
    hotwords = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('#'):
                continue
            # 跳过 "input xxx" 自定义短语（给快捷键用的，不是热词）
            if 'input ' in line.lower():
                continue
            # 提取 | 分隔的所有别名
            parts = [p.strip() for p in line.split('|')]
            hotwords.extend(parts)

    # 去重、去空、保留原顺序
    seen = set()
    result = []
    for w in hotwords:
        if w and w not in seen:
            seen.add(w)
            result.append(w)
    return ' '.join(result)


# ── 智能分行（移植自 CapsWriter 2.6 ResultHandler.smart_split） ──────

def smart_split(text: str, min_chars: int = 2) -> str:
    """
    智能分行：
    1. 保留标点符号
    2. 避免在逗号处切分过短的句子
    3. 英文标点需后跟空格才切分（避免 3.14 被切分）
    4. 句号/问号/感叹号强制换行
    """
    # 使用捕获组保留标点，英文标点需后跟空白符或结尾
    parts = re.split(r'([，。？]|[.,?!](?:\s+|$))', text)
    lines = []
    buffer = ""

    strong_punct = {'。', '？', '.', '?', '!'}
    punct_chars = set(r'，。？,.?!')

    for part in parts:
        clean_part = part.strip()
        if clean_part and clean_part in punct_chars and len(clean_part) == 1:
            buffer += part
            is_strong = clean_part in strong_punct
            if is_strong or len(buffer) > min_chars:
                lines.append(buffer)
                buffer = ""
        else:
            buffer += part

    if buffer:
        lines.append(buffer)

    return "\n".join(lines)


# ── SRT 生成（移植自 CapsWriter 2.6 srt_from_txt） ──────────────────

def format_srt_timestamp(seconds: float) -> str:
    """秒数 → SRT 时间戳 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def lines_match_words(text_lines, words):
    """
    使用 SequenceMatcher 将分行文本与字级时间戳进行最优对齐。
    移植自 CapsWriter 2.6 srt_from_txt.lines_match_words。

    Args:
        text_lines: 智能分行后的文本行列表
        words: [{'word': '字', 'start': 0.0, 'end': 0.1}, ...]

    Returns:
        SRT 内容字符串
    """
    # 标点清理模式
    punct_chars = '，。！？；：、「」『』（）《》【】[]{},.!?;:"\''
    punc_pattern = re.compile(rf'[{re.escape(punct_chars)}\s\d]')

    # 建立 token_idx → 字符偏移的映射
    token_chars = []
    token_indices = []
    for i, w in enumerate(words):
        word_clean = punc_pattern.sub('', w['word'].lower())
        for char in word_clean:
            token_chars.append(char)
            token_indices.append(i)

    if not token_chars:
        return ""

    pure_tokens_text = "".join(token_chars)

    # 全局对齐
    all_lines_text = "".join(line.strip() for line in text_lines)
    clean_all_lines = punc_pattern.sub('', all_lines_text.lower())

    sm = difflib.SequenceMatcher(None, pure_tokens_text, clean_all_lines)
    matches = sm.get_matching_blocks()

    # 字符偏移 → word 索引映射
    char_to_word_map = {}
    for match in matches:
        for i in range(match.size):
            char_to_word_map[match.b + i] = token_indices[match.a + i]

    # 映射行到时间戳
    srt_parts = []
    current_char_offset = 0
    last_word_idx = 0

    for index, line in enumerate(text_lines):
        line = line.rstrip('，。？！,.?!\r\n ')
        if not line:
            continue
        line_clean = punc_pattern.sub('', line.lower())
        if not line_clean:
            continue

        line_len = len(line_clean)
        found_word_indices = [
            char_to_word_map[i]
            for i in range(current_char_offset, current_char_offset + line_len)
            if i in char_to_word_map
        ]

        if found_word_indices:
            start_word_idx = min(found_word_indices)
            end_word_idx = max(found_word_indices)
            t1 = words[start_word_idx]['start']
            t2 = words[end_word_idx]['end']
            last_word_idx = end_word_idx
        else:
            t1 = words[min(last_word_idx + 1, len(words) - 1)]['start']
            t2 = t1 + 0.5

        idx = len(srt_parts) // 4 + 1
        srt_parts.append(str(idx))
        srt_parts.append(f"{format_srt_timestamp(t1)} --> {format_srt_timestamp(t2)}")
        srt_parts.append(line)
        srt_parts.append("")

        current_char_offset += line_len

    return "\n".join(srt_parts)


# ── 转录客户端 ──────────────────────────────────────────────────────

async def transcribe_file(
    audio_path: str,
    server_host: str,
    server_port: int,
    output_dir: str,
    seg_duration: int = 60,
    seg_overlap: int = 4,
    context: str = '',
    language: str = 'auto',
):
    """连接服务端 → 发送音频 → 接收结果 → 保存文件 → 断开"""
    file = Path(audio_path)
    if not file.exists():
        print(f"错误：文件不存在 {audio_path}", file=sys.stderr)
        sys.exit(1)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    task_id = str(uuid.uuid1())

    url = f"ws://{server_host}:{server_port}"
    print(f"连接服务端 {url} ...")

    try:
        ws = await websockets.connect(url, subprotocols=["binary"], max_size=None)
    except Exception as e:
        print(f"错误：无法连接服务端 - {e}", file=sys.stderr)
        sys.exit(1)

    print(f"已连接，任务 ID: {task_id}")
    print(f"处理文件: {file}")
    if context:
        print(f"上下文提示: {context}")
    if language != 'auto':
        print(f"识别语言: {language}")

    # 获取时长
    duration = 0.0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            duration = float(stdout.decode().strip())
            print(f"音频时长: {duration:.1f}s")
    except Exception:
        pass

    # 启动 FFmpeg 提取音频 (f32le, 16kHz, mono)
    ffmpeg_cmd = ["ffmpeg", "-i", str(file), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"]
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    chunk_size = 16000 * 4 * seg_duration  # f32le 字节数
    time_start = time.time()
    bytes_sent = 0

    # 发送音频块（2.6 协议：带 context 和 language 字段）
    print("发送音频数据...")
    while True:
        data = await process.stdout.read(chunk_size)
        if not data:
            break
        bytes_sent += len(data)
        progress = bytes_sent / 4 / 16000
        msg = json.dumps({
            "task_id": task_id,
            "source": "file",
            "data": base64.b64encode(data).decode("ascii"),
            "is_final": False,
            "time_start": time_start,
            "time_frame": time.time(),
            "seg_duration": seg_duration,
            "seg_overlap": seg_overlap,
            "context": context,
            "language": language,
        })
        await ws.send(msg)

    # 发送结束标志
    await ws.send(json.dumps({
        "task_id": task_id,
        "source": "file",
        "data": "",
        "is_final": True,
        "time_start": time_start,
        "time_frame": time.time(),
        "seg_duration": seg_duration,
        "seg_overlap": seg_overlap,
        "context": context,
        "language": language,
    }))
    await process.wait()
    print(f"已发送 {bytes_sent / 4 / 16000:.1f}s 音频，等待识别结果...")

    # 接收结果
    final_msg = None
    async for raw in ws:
        msg = json.loads(raw)
        dur = msg.get("duration", 0)
        if duration > 0:
            pct = min(dur / duration * 100, 100)
            print(f"\r识别进度: {dur:.1f}s ({pct:.0f}%)", end="", flush=True)
        else:
            print(f"\r识别进度: {dur:.1f}s", end="", flush=True)
        if msg.get("is_final"):
            final_msg = msg
            break

    await ws.close()
    print()  # newline after progress

    if not final_msg:
        print("错误：未收到最终识别结果", file=sys.stderr)
        sys.exit(1)

    # 保存结果
    base_name = file.stem
    text = final_msg.get("text", "")
    text_accu = final_msg.get("text_accu", text)
    timestamps = final_msg.get("timestamps", [])
    tokens = final_msg.get("tokens", [])

    # 智能分行
    text_split = smart_split(text_accu)

    # TXT
    txt_path = out / f"{base_name}.txt"
    txt_path.write_text(text_split, encoding="utf-8")
    print(f"→ {txt_path}")

    # JSON（时间戳 + tokens）
    json_path = out / f"{base_name}.json"
    json_path.write_text(
        json.dumps({"timestamps": timestamps, "tokens": tokens}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"→ {json_path}")

    # SRT（使用 SequenceMatcher 对齐）
    if tokens and timestamps:
        words = [
            {"word": t.replace("@", ""), "start": ts, "end": ts + 0.2}
            for ts, t in zip(timestamps, tokens)
        ]
        for i in range(len(words) - 1):
            words[i]["end"] = min(words[i]["end"], words[i + 1]["start"])

        text_lines = [line for line in text_split.splitlines() if line.strip()]
        srt_content = lines_match_words(text_lines, words)

        if srt_content:
            srt_path = out / f"{base_name}.srt"
            srt_path.write_text(srt_content, encoding="utf-8")
            print(f"→ {srt_path}")

    proc_time = final_msg.get("time_complete", 0) - final_msg.get("time_start", 0)
    print(f"\n完成！处理耗时 {proc_time:.1f}s")
    print(f"识别结果: {text}")


def main():
    parser = argparse.ArgumentParser(
        description="CapsWriter Offline CLI — 短连接文件转录 (适配 CapsWriter 2.6)",
    )
    parser.add_argument("audio", help="音频/视频文件路径")
    parser.add_argument("-o", "--output-dir", default=".", help="输出目录（默认当前目录）")
    parser.add_argument("-s", "--server", default="127.0.0.1:6016", help="服务端地址:端口")
    parser.add_argument("--seg-duration", type=int, default=60, help="分段时长/秒（默认 60）")
    parser.add_argument("--seg-overlap", type=int, default=4, help="重叠时长/秒（默认 4）")
    parser.add_argument("--context", default="", help="识别上下文提示（人名、地名、术语等，辅助 ASR 引擎）")
    parser.add_argument("--hotfile", default="", help="热词文件路径（自动解析为 context，可与 --context 叠加）")
    parser.add_argument("--language", default="auto", help="识别语言（auto/zh/en/ja 等，默认 auto）")
    args = parser.parse_args()

    # 合并热词文件和手动 context
    context = args.context
    if args.hotfile:
        hot_ctx = parse_hotfile(args.hotfile)
        if hot_ctx:
            context = f"{hot_ctx} {context}".strip() if context else hot_ctx
            print(f"从热词文件加载: {args.hotfile} ({len(hot_ctx.split())} 个热词)")

    host, _, port_str = args.server.partition(":")
    port = int(port_str)

    asyncio.run(transcribe_file(
        audio_path=args.audio,
        server_host=host,
        server_port=port,
        output_dir=args.output_dir,
        seg_duration=args.seg_duration,
        seg_overlap=args.seg_overlap,
        context=context,
        language=args.language,
    ))


if __name__ == "__main__":
    main()
