# yukirin-server deployment profile

This directory contains the host- and LXC-specific deployment profile used to
validate the cross-platform CapsWriter Plus product. Private topology belongs
here; it is not a requirement or a concept exposed by the generic Web UI.

Snapshot date: 2026-08-23 CST. Before every operation, verify the live paths,
units, listeners, process owners and file hashes again.

## Product and deployment boundary

The repository implements three independent server-side entry points:

| Port | Protocol | Product role | Authentication |
| --- | --- | --- | --- |
| 6016/TCP | WebSocket | low-latency ASR for CapsWriter clients | required Bearer token |
| 6017/TCP | HTTP | loopback-only read-only Web UI | same token, then session cookie |
| 6018/TCP | HTTP | uploaded-file API for Agents, CLI and CI | same Bearer token |

All ports are configurable. Official optional `6017/UDP` result broadcast and
`6018/UDP` recording control are client features and do not conflict with these
TCP listeners.

The host runs the model exactly once, in the 6016 process. The 6018 API streams
uploaded media into temporary storage, decodes it with ffmpeg and calls 6016;
it does not load another model. The Web UI gets ASR/device state from the
running ASR process. It does not infer health from systemd, containers,
`nvidia-smi`, process names or fixed hardware vendors.

## Target host topology

```text
yukirin-server host
├─ capswriter-server.service
│  ├─ /data/CapsWriter-Offline/core_server.py
│  ├─ Qwen3-ASR-1.7B and the configured CPU/GPU backends
│  └─ WebSocket 0.0.0.0:6016
├─ capswriter-webui.service
│  └─ HTTP 127.0.0.1:6017
└─ capswriter-api.service
   ├─ /data/CapsWriter-Offline/core_api.py
   ├─ FastAPI/Uvicorn, no model copy
   └─ HTTP 0.0.0.0:6018 for the private host/LXC network

agents LXC
├─ /root/scripts/caps-transcribe-api -> host 6018 (primary after migration)
└─ caps-transcribe.service -> HTTP 0.0.0.0:9600 (legacy rollback path)
```

The LXC neither starts `core_server.py` nor loads an ASR model. The old 9600
wrapper accepts a shared server path and depends on `/data`; it is retained only
for compatibility while callers move to the standard upload API.

## One global token

Every protected product endpoint uses exactly one `CAPSWRITER_TOKEN`. There are
no separate Web UI, API, Agent or client tokens. The token must contain at least
16 characters, is never committed, and is never accepted in a URL.

Host file (mode `0600`):

```text
~/.config/capswriter/capswriter.env
CAPSWRITER_TOKEN=<at-least-16-random-characters>
```

LXC file (root-owned, mode `0600`):

```text
/etc/capswriter/capswriter.env
CAPSWRITER_TOKEN=<same-token-as-host>
CAPSWRITER_SERVER_URL=ws://10.51.192.1:6016
CAPSWRITER_API_URL=http://10.51.192.1:6018
```

Missing or short tokens fail before startup. Invalid authentication attempts
use per-source delays of 250 ms, 500 ms, 1 second and 2 seconds; the fifth and
later attempts return 429 with `Retry-After`. Records decay after five minutes
and successful authentication clears the source. Logs never include token
values. Public traffic must additionally use HTTPS/WSS; Bearer authentication
is not transport encryption.

## 6018 HTTP/Agent API

The generic contract is documented in
[`docs/CapsWriter-Plus-HTTP-API.md`](../../docs/CapsWriter-Plus-HTTP-API.md).
It accepts raw media bytes rather than a server pathname and has explicit size,
duration, queue, concurrency and request timeout limits. Swagger/OpenAPI are
served by the same process.

The deployment profile uses `config/api.env.example` for non-secret settings.
The required token remains only in `capswriter.env`. Install and start the user
unit independently:

```text
systemctl --user daemon-reload
systemctl --user enable --now capswriter-api.service
```

An Agent can use the tracked one-shot wrapper after installing it as
`/root/scripts/caps-transcribe-api`:

```text
/root/scripts/caps-transcribe-api recording.m4a --format json
```

The wrapper sources the root-only environment file and invokes the generic
streaming Python client. It does not put the token in arguments or a URL.

## 6017 Web UI

`capswriter-webui.service` is a separate standard-library HTTP process and is
hard-limited by the application to a loopback listener. It provides token
login, ASR/model/device self-report, optional 6018 readiness, incremental logs,
read-only configuration and a minimal unauthenticated health endpoint. It does
not display the private LXC or legacy wrapper.

Use `config/webui.env.example` for non-secret metadata. To access it without a
public ingress:

```text
ssh -L 16017:127.0.0.1:6017 yukirin-server
```

Then open `http://127.0.0.1:16017/`. A future HTTPS ingress must set
`CAPSWRITER_WEBUI_SECURE_COOKIE=1`. This profile does not configure Puck, FRP,
certificates, public listeners, hotword editing, model reload or TTS.

## Development and promotion workflow

The development checkout is `/home/vanilla/repos/CapsWriter-Offline`. Pull the
feature branch there, use the existing host Python/GPU for isolated tests, and
commit and push small changes. Run Windows compatibility checks from a separate
clone. `/data/CapsWriter-Offline` remains production and is not a Git checkout.

Only after both sides pass:

1. re-check the live baseline and create a timestamped backup;
2. install pinned API dependencies into the production host interpreter;
3. copy the reviewed source without overwriting `.venv`, models, logs or local
   configuration;
4. install/reload units and verify 6016, then 6018, then 6017;
5. perform an authenticated real transcription through 6018;
6. back up active Agent skills/config and migrate them to 6018;
7. keep 9600 running until a later, separately approved retirement.

Rollback restores the timestamped production source and user units. Agent
rollback restores the timestamped skill/config backup and immediately returns
callers to the still-running 9600 service.

## Interpreter split

`/data` is shared with `agents` through an LXD shifted disk mount. Do not change
ownership or repoint either interpreter:

```text
host:   /data/CapsWriter-Offline/.venv/bin/python-host
agents: /data/CapsWriter-Offline/.venv/bin/python
```

The host units must use `python-host`; LXC commands must use `python`.

## Tracked artifacts and exclusions

- `systemd/user/`: three independent host user services.
- `config/`: non-secret host environment templates.
- `lxc/agents/caps-transcribe-api`: primary one-shot 6018 client.
- `lxc/agents/caps-client/server.py` and `caps-transcribe.service`: unchanged
  legacy 9600 rollback path.
- `manifests/`: deployed package and binary/model checksums.

Model weights, virtual environments, personal hotwords, logs, transcripts,
generated outputs, credentials, private Agent memory, FRP configuration and
compiled Linux libraries are deliberately excluded from the public fork.
