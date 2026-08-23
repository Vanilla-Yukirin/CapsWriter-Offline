# yukirin-server Linux deployment

This directory backs up the CapsWriter deployment-specific files used by
`yukirin-server` and its `agents` LXC container.

Snapshot date: 2026-08-23 CST.

Upstream base:

- repository: `HaujetZhao/CapsWriter-Offline`
- branch: `master`
- commit: `7d7fac3541a998be10ebf15102f7884a7dd36edb`
- latest release at snapshot time: `v2.6`

## Runtime topology

```text
yukirin-server host
└─ capswriter-server.service
   ├─ /data/CapsWriter-Offline/core_server.py
   ├─ /data/CapsWriter-Offline/.venv/bin/python-host
   ├─ Qwen3-ASR-1.7B + Vulkan / RTX 3080
   └─ WebSocket 0.0.0.0:6016

agents LXC
└─ caps-transcribe.service
   ├─ /root/scripts/caps-client/server.py
   ├─ HTTP 0.0.0.0:9600
   └─ WebSocket upstream 10.51.192.1:6016
```

The host owns the ASR model, GPU inference and WebSocket service. The container
owns only the HTTP wrapper and client-side hotword post-processing.

## Phase 0 authentication

The custom branch requires one global Bearer token for every WebSocket client.
The server validates it during the HTTP Upgrade handshake, before accepting a
WebSocket connection. Both missing configuration and tokens shorter than 16
characters fail closed before model loading.

Host configuration:

```text
~/.config/capswriter/capswriter.env
CAPSWRITER_TOKEN=<at-least-16-random-characters>
```

Create the directory with mode `0700` and the file with mode `0600`. The user
unit loads this required file with `EnvironmentFile`; it isn't optional and it
must never be committed. A non-secret template is tracked at
`config/capswriter-server.env.example`.

The `agents` LXC must receive the same token because its HTTP wrapper uses the
CapsWriter `WebSocketManager` as an ASR client:

```text
/etc/capswriter/capswriter.env
CAPSWRITER_TOKEN=<same-token-as-host>
CAPSWRITER_SERVER_URL=ws://10.51.192.1:6016
```

The LXC file must be owned by root with mode `0600`. Its tracked template is
`lxc/agents/capswriter-client.env.example`.

External clients may provide a complete URL, including TLS and a reverse-proxy
path:

```text
CAPSWRITER_SERVER_URL=wss://caps.example.com/asr
CAPSWRITER_TOKEN=<same-token-as-host>
```

`config_client.py` retains `addr` and `port` as a fallback for old local
configuration, but `CAPSWRITER_SERVER_URL` takes precedence. Tokens embedded in
URLs are rejected.

Failed handshakes are tracked per source address. The first four failures wait
250 ms, 500 ms, 1 second and 2 seconds; later failures return HTTP 429 with
`Retry-After`. Records expire after five minutes without another failure, and
a successful authentication clears that source immediately. Only new
handshakes are affected, and logs contain the source and result but never the
token.

Enabling authentication is a coordinated cutover: prepare the host and all
clients first, then restart the host and LXC services in one maintenance
window. Verify missing, incorrect and correct credentials separately. Do not
expose a token over public `ws://`; use HTTPS/WSS at the public endpoint.

## Tracked customizations

Files at repository root mirror the deployed source changes:

- `core_server.py`: Linux launcher for `CapsWriterServer`.
- `config_server.py`: headless mode, INFO logging and absolute model path.
- `config_client.py`: LXD host bridge target `10.51.192.1:6016`.
- `core/client/__init__.py`: tolerate missing GUI dependencies in headless use.

Deployment artifacts:

- `systemd/user/capswriter-server.service`: host user service.
- `scripts/caps-transcribe`: host-facing HTTP self-client.
- `lxc/agents/caps-client/server.py`: full CapsWriter HTTP wrapper.
- `lxc/agents/caps_client.py`: legacy direct WebSocket CLI.
- `lxc/agents/caps-transcribe.service`: container system service.
- `manifests/`: deployed Python packages and binary/model checksums.

## Deliberately excluded

The following runtime state is not committed to this public fork:

- model weights and `.venv` binaries;
- personal `hot.txt`, `hot-rule.txt`, `hot-server.txt` and
  `/data/hot-2.6-local`;
- logs, transcripts and generated TXT/SRT/JSON files;
- FRP configuration, tokens, credentials and private Agent memory;
- compiled Linux `.so` files.

Models are downloaded from the upstream `models` release. At this snapshot the
active model files live in:

```text
/data/CapsWriter-Offline/models/Qwen3-ASR/Qwen3-ASR-1.7B/
```

The Linux llama.cpp runtime is build `b7798`; deployed library checksums are in
`manifests/llama-b7798-linux-vulkan.sha256`.

## Interpreter split

`/data` is shared with `agents` through an LXD shifted disk mount. The shared
venv therefore has two interpreter entry points:

```text
.venv/bin/python
  -> /root/.local/bin/python3.13

.venv/bin/python-host
  -> /home/vanilla/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13
```

The host unit must use `python-host`. Do not repoint the shared default
`python`, because doing so can break the container or cause host
`status=203/EXEC`.

## Health semantics

The HTTP wrapper opens a WebSocket only for a transcription job and disconnects
afterwards. An idle response is healthy:

```json
{"status":"ok","connected":false}
```

Before restoring or deploying, verify paths, checksums, systemd units and both
network hops. Do not treat this backup branch as proof that the live service was
restarted or deployed from the branch.
