# Hermes Agent ↔ OpenCode CLI Gateway

A local API bridge that lets **Hermes Agent** (or any OpenAI-compatible
client) use the free AI models exposed by the **OpenCode CLI** — without
per-provider API keys, without per-provider accounts.

```
Hermes Agent  ──►  Gateway (OpenAI API)  ──►  opencode CLI  ──►  free models
                       (this repo)            (subprocess)        (DeepSeek,
                                                                   Gemini,
                                                                   Qwen, ...)
```

The gateway speaks the **OpenAI Chat Completions API** on the front and
launches **`opencode run --format json`** subprocesses on the back. Hermes
never knows the response came from a CLI; opencode never knows the request
came from a desktop app.

---

## Features

| Feature | Status |
|---|---|
| `POST /v1/chat/completions` (non-streaming) | ✅ |
| `POST /v1/chat/completions` (SSE streaming, `stream: true`) | ✅ |
| `GET /v1/models` (auto-discovered from `opencode models`) | ✅ |
| Bearer-token API key authentication | ✅ |
| Per-key token-bucket rate limiting | ✅ |
| OpenAI-shaped error responses | ✅ |
| Configurable timeouts + subprocess cleanup | ✅ |

---

## Quick start

### 1. Install OpenCode CLI

```bash
npm install -g opencode-ai
opencode --version        # → 1.17.x or newer
opencode models           # → should list opencode/big-pickle, opencode/deepseek-v4-flash-free, ...
```

### 2. Configure & run the gateway

```bash
cd gateway/
cp .env.example .env
# (edit .env if you want to set API keys, change the port, etc.)

./run.sh                  # creates a venv, installs deps, starts on :8787
```

Or, manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 3. Point Hermes Agent at the gateway

In Hermes, add a new OpenAI-compatible provider with:

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | any string (or whatever you set in `GATEWAY_API_KEYS`) |
| Model | any id from `GET /v1/models` (e.g. `opencode/big-pickle`) |

You can also test the gateway directly with curl:

```bash
# List available models
curl http://127.0.0.1:8787/v1/models | jq

# Non-streaming chat
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/big-pickle",
    "messages": [{"role": "user", "content": "Say hello in one word."}]
  }' | jq

# Streaming chat
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/big-pickle",
    "stream": true,
    "messages": [{"role": "user", "content": "Count from 1 to 5."}]
  }'
```

---

## How it works

### Request flow

1. **Hermes** sends a standard OpenAI Chat Completions request to `POST /v1/chat/completions`.
2. **Auth + rate limit** middleware verifies the bearer token and token bucket.
3. **Translator** flattens `messages[]` into a single role-tagged prompt string:
   ```
   [System]
   You are helpful.

   [User]
   Explain recursion.

   [Assistant]
   ...

   [User]
   Give a shorter version.
   ```
4. **OpenCodeClient** launches:
   ```bash
   opencode run --model opencode/big-pickle --format json --auto "<prompt>"
   ```
   and reads NDJSON events from stdout line-by-line.
5. **Response translator** converts the aggregated `text` events back into the OpenAI response schema (non-streaming) or emits each event as an SSE chunk (streaming).
6. **Hermes** receives a response that is byte-for-byte indistinguishable from a cloud-hosted OpenAI call.

### Event format

`opencode run --format json` emits one JSON object per stdout line:

```json
{"type":"text","timestamp":1730000000000,"sessionID":"abc","part":{"type":"text","text":"Hello!"}}
```

Event types we handle:

| `type` | Meaning | Gateway action |
|---|---|---|
| `text` | A completed text segment from the assistant | Emit as `delta.content` (streaming) or concatenate (non-streaming) |
| `reasoning` | A reasoning block (only with `--thinking`) | Emit as `[reasoning] ...` content |
| `tool_use` | A tool call completed | Currently ignored (opencode's agent loop handles tools internally) |
| `step_start` / `step_finish` | Agent step boundaries | Ignored |
| `error` | Session-level error | Surfaces as a 502 (non-streaming) or an error SSE frame (streaming) |

The CLI process exits when opencode's internal `session.status` becomes `idle`.

### Model discovery

`GET /v1/models` calls `opencode models --verbose` and parses the
`<provider>/<model_id>` header line + JSON metadata block for each model.
The response is cached per-request (no TTL); add `?refresh=1` (TODO) or
restart the gateway to pick up newly added models.

---

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | Bind address |
| `GATEWAY_PORT` | `8787` | Listen port |
| `GATEWAY_API_KEYS` | _(empty)_ | Comma-separated accepted bearer tokens; empty = auth disabled |
| `GATEWAY_RATE_LIMIT_RPM` | `60` | Requests per minute per key (token bucket) |
| `GATEWAY_RATE_LIMIT_BURST` | `10` | Max burst size |
| `OPENCODE_BIN` | `opencode` | Path to the opencode binary |
| `OPENCODE_WORKDIR` | _(empty = CWD)_ | Working directory for opencode invocations |
| `OPENCODE_TIMEOUT` | `300` | Hard timeout (seconds) per `opencode run` |
| `OPENCODE_DEFAULT_MODEL` | `opencode/big-pickle` | Fallback when client doesn't specify a model |
| `OPENCODE_DEFAULT_AGENT` | _(empty)_ | Optional `--agent` flag passed to opencode |
| `OPENCODE_EXTRA_FLAGS` | _(empty)_ | Extra CLI flags appended to every `opencode run` |
| `GATEWAY_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## Security notes

- **Bind to `127.0.0.1` by default.** Don't expose the gateway to the
  network unless you've set `GATEWAY_API_KEYS` and understand the risks.
- **`--auto` flag.** The gateway passes `--auto` to `opencode run` so
  non-interactive sessions don't hang on permission prompts. opencode's
  `run` subcommand already denies the `question`, `plan_enter`, and
  `plan_exit` permissions, but `--auto` approves file-write and similar
  permissions. If you want stricter behaviour, set
  `OPENCODE_EXTRA_FLAGS=--no-auto` — but expect some workflows to stall.
- **No persistence.** Rate-limit buckets live in-process memory and
  reset on restart. Sessions created by opencode are stored in opencode's
  own data dir (`~/.local/share/opencode` on Linux).

---

## Limitations & roadmap

**Current limitations:**
- **Token-level streaming.** `opencode run --format json` only emits
  `text` events when a text segment is *complete* — so streaming clients
  receive segment-level deltas, not token-by-token deltas. For true
  token streaming, a future version should keep a long-lived
  `opencode serve` process and connect to its HTTP/SSE event stream.
- **No tool-call translation.** If opencode's agent invokes tools
  internally, those tool calls are not exposed in the OpenAI response.
  Hermes sees only the final assistant text.
- **No usage accounting.** opencode doesn't report token counts in its
  JSON event stream, so `usage` is always `{0, 0, 0}`.
- **Single-instance rate limiter.** For multi-process deployments, swap
  `RateLimiter` for a Redis-backed implementation.

**Roadmap ideas:**
- Persistent `opencode serve` backend for true token streaming
- `?refresh=1` on `/v1/models` to invalidate the models cache
- Session reuse (`--continue` / `--session`) for cheaper multi-turn chats
- Tool-call translation (opencode tool events ↔ OpenAI `tool_calls` deltas)
- Prometheus metrics endpoint
- WebSocket transport (for clients that prefer it over SSE)

---

## Project layout

```
gateway/
├── main.py              # FastAPI app + entrypoint
├── config.py            # Pydantic settings (env-driven)
├── translator.py        # OpenAI ↔ opencode schema translation
├── streaming.py         # SSE response helpers
├── opencode/
│   ├── __init__.py
│   ├── client.py        # OpenCodeClient: subprocess + NDJSON parser
│   ├── events.py        # OpenCodeEvent dataclass + line parser
├── api/
│   ├── __init__.py
│   ├── routes.py        # /v1/chat/completions, /v1/models, /health
│   ├── auth.py          # Bearer-token auth dependency
│   ├── ratelimit.py     # In-memory token-bucket limiter
│   ├── errors.py        # OpenAI-shaped error classes
├── requirements.txt
├── .env.example
├── run.sh               # venv + install + launch
└── README.md
```

---

## License

MIT. The gateway is a thin compatibility wrapper; all model access is
provided by the [opencode](https://opencode.ai) project.
