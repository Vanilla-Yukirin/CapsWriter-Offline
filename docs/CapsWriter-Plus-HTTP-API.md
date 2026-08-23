# CapsWriter Plus HTTP / Agent API

CapsWriter Plus 在默认 `6018/TCP` 提供面向 Agent、CLI、CI 和普通 HTTP
客户端的文件转录 API。API 进程不会加载第二份模型，而是使用同一个全局 Bearer
token 连接现有 `6016/TCP` WebSocket ASR 服务。

## 安装与启动

安装服务端依赖，并确保 `ffmpeg`、`ffprobe` 位于 `PATH`：

```bash
python -m pip install -r requirements-server.txt
```

设置与 6016、Web UI 完全相同的 token 后启动：

```bash
export CAPSWRITER_TOKEN='replace-with-at-least-16-characters'
python core_api.py
```

默认只监听 `127.0.0.1:6018`。需要让其他机器访问时，可显式设置
`CAPSWRITER_API_HOST=0.0.0.0` 并同时配置防火墙和 HTTPS 反向代理。Bearer token
只负责鉴权，公网传输必须另外使用 HTTPS/WSS。

启动后可查看：

- Swagger UI：`http://127.0.0.1:6018/docs`
- OpenAPI：`http://127.0.0.1:6018/openapi.json`
- 存活检查：`GET /healthz`
- 就绪检查：`GET /readyz`，需要 Bearer token

`/healthz` 只表示 HTTP 进程存活；`/readyz` 还会检查 6016 ASR 自报告状态、
`ffmpeg` 和 `ffprobe`。

## 转录请求

```http
POST /v1/transcriptions?filename=sample.wav&response_format=json&language=auto
Authorization: Bearer <CAPSWRITER_TOKEN>
Content-Type: audio/wav

<原始媒体文件字节>
```

请求正文直接放音频或视频的原始字节，不使用 multipart，也不接受服务器文件路径。
例如：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $CAPSWRITER_TOKEN" \
  -H "Content-Type: audio/wav" \
  --data-binary @sample.wav \
  "http://127.0.0.1:6018/v1/transcriptions?filename=sample.wav&response_format=json"
```

可选查询参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `filename` | `upload.bin` | 仅用于临时文件后缀与诊断；目录部分会被剥离 |
| `response_format` | `json` | `json`、`text` 或 `srt` |
| `language` | `auto` | 传给 ASR 协议的语言 |
| `context` | 空 | 传给支持 context 的模型，最长 2000 字符 |

JSON 返回包含任务 ID、最终文本、媒体时长、处理时长、token、时间戳和 SRT：

```json
{
  "id": "task-id",
  "status": "completed",
  "text": "识别结果",
  "duration_seconds": 12.3,
  "processing_seconds": 2.1,
  "language": "auto",
  "tokens": ["识", "别", "结", "果"],
  "timestamps": [0.0, 0.2, 0.4, 0.6],
  "srt": "1\n00:00:00,000 --> 00:00:12,300\n识别结果\n"
}
```

`response_format=text` 返回 `text/plain`；`response_format=srt` 返回
`application/x-subrip` 下载内容。

## 默认限制

| 环境变量 | 默认值 | 用途 |
|---|---:|---|
| `CAPSWRITER_API_PORT` | `6018` | HTTP/TCP 端口 |
| `CAPSWRITER_API_ASR_URL` | `ws://127.0.0.1:6016/asr` | 6016 WebSocket 地址 |
| `CAPSWRITER_API_ASR_STATUS_URL` | `http://127.0.0.1:6016/status` | 受鉴权的 ASR 状态地址 |
| `CAPSWRITER_API_MAX_UPLOAD_BYTES` | `268435456` | 最大上传 256 MiB |
| `CAPSWRITER_API_MAX_DURATION_SECONDS` | `7200` | 最大媒体时长 2 小时 |
| `CAPSWRITER_API_MAX_CONCURRENCY` | `1` | 同时转录任务数 |
| `CAPSWRITER_API_QUEUE_TIMEOUT_SECONDS` | `5` | 等待容量的最长秒数 |
| `CAPSWRITER_API_REQUEST_TIMEOUT_SECONDS` | `3600` | 单次转录超时 |
| `CAPSWRITER_API_SEGMENT_DURATION_SECONDS` | `60` | 发送到 6016 的分段时长 |
| `CAPSWRITER_API_SEGMENT_OVERLAP_SECONDS` | `4` | 分段重叠时长 |

鉴权失败使用与 6016 和 Web UI 相同的递增延迟与 429 策略。成功鉴权会清除该来源的失败记录；token 不进入响应、访问日志或转录日志。

主要错误码：`401` 未授权、`413` 超出大小或时长限制、`415` 错误使用
multipart、`422` 无法解码媒体、`429` 鉴权/容量限制、`503` ASR 或媒体依赖不可用、
`504` 转录超时。
