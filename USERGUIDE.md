# Arbiter – User Guide

Complete end-to-end guide for configuring, running, and using the Arbiter.

---

## Table of Contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Starting the Gateway](#starting-the-gateway)
4. [Making Your First Request](#making-your-first-request)
5. [Understanding Rate Limits](#understanding-rate-limits)
6. [API Reference](#api-reference)
7. [Usage Examples](#usage-examples)
8. [Managing API Keys via UI](#managing-api-keys-via-ui)
9. [Image Generation](#image-generation)
10. [Settings Dashboard](#settings-dashboard)
11. [Chat Playground](#chat-playground)
12. [Log Viewer](#log-viewer)
13. [CF Workers in the Gateway](#cf-workers-in-the-gateway)
14. [Best Practices](#best-practices)
15. [Troubleshooting](#troubleshooting)
16. [Persistent Logs (180-day)](#persistent-logs-180-day)
17. [Admin Activity Audit](#admin-activity-audit)
18. [Major-Change Dashboard Banners](#major-change-dashboard-banners)
19. [Per-Token Rate Limiting](#per-token-rate-limiting)
20. [Weekly AI Analysis Email](#weekly-ai-analysis-email)
21. [Adaptive Routing (v1.18)](#adaptive-routing-v118)
22. [Complexity-Aware Routing (v1.19)](#complexity-aware-routing-v119)

---

## Managing API Keys via UI

You can add, remove, test, and enable/disable provider API keys at runtime — **no container restart required**. Keys are written directly to `.env` so they persist across restarts automatically.

### Open Settings → API Keys tab

Navigate to `/settings` and click the **API Keys** tab (shown first by default).

Each provider card shows:
- **Status badge** — Active (green) or Inactive (grey)
- **Enable / Disable toggle** — instantly remove or restore a provider from the routing pool. When enabling, a connectivity test runs automatically first — if the key is invalid the toggle reverts and an error is shown
- **Test button** — sends a minimal probe request and reports latency
- **Existing keys** — masked (e.g. `AIzaSy...Ab3c`); click ✕ to remove any key
- **Add key form** — paste a new key and click **Add Key**; the key is saved to `.env` and a connectivity test runs automatically

### Key Format by Provider

| Provider | Format | Example |
|---|---|---|
| Gemini | API key | `AIzaSy...` |
| Groq | API key | `gsk_...` |
| OpenRouter | API key | `sk-or-v1-...` |
| Cohere | API key | `...` |
| **Cloudflare Workers AI** | `account_id\|api_token` | `abc123\|your_token` |
| Cerebras | API key | `csk-...` |
| HuggingFace | Access Token | `hf_...` |
| Z.ai / Zhipu AI | API key | `your-zai-key` |
| NVIDIA NIM | API key | `nvapi-...` |
| Pollinations | API key (`sk_...` or `pk_...`) | [enter.pollinations.ai](https://enter.pollinations.ai/) |

### Cloudflare Workers AI Setup

1. Sign up at [cloudflare.com](https://cloudflare.com) (free)
2. Go to **Workers & Pages** — this activates the Workers AI free tier
3. Find your **Account ID** in the right sidebar of the Workers overview page
4. Go to **Profile → API Tokens → Create Token**
5. Create a **Custom Token** with these permissions:
   - **Workers Scripts: Edit** — required to create and delete Worker scripts
   - **Workers AI: Execute** — required to run AI inference
   - **Account: Read** — required to list Workers subdomain info
6. In the API Keys tab, add the key as: `<Account_ID>|<API_Token>`

> **Token Permission Validation:** After adding your key, Arbiter automatically tests three permissions and shows you a green ✅ or red ❌ for each:
> - **Workers Scripts (list)** — needed to show the workers list and create/delete workers
> - **Workers AI (execute)** — needed to run inference through your workers
> - **Workers Subdomain** — needed to generate the correct worker URL
>
> Click **Validate Permissions** on the Cloudflare provider card at any time to re-check.

### Runtime API (programmatic)

```bash
# List all providers
GET /api/providers

# Add a key
POST /api/providers/gemini/keys
{"key": "AIzaSy..."}

# Remove a key by hash
DELETE /api/providers/gemini/keys/{hash}

# Enable / disable
POST /api/providers/cloudflare/enable
POST /api/providers/cloudflare/disable

# Test connectivity
POST /api/providers/groq/test

# Reload all key pools
POST /api/providers/reload
```

---

## Image Generation

Arbiter includes a **free image generation endpoint** powered by [Pollinations.ai](https://pollinations.ai) — no API key required.

### Via the UI

Go to **Settings → Image Generation tab**:
1. Enter a prompt (and optional negative prompt)
2. Choose model, size, count, and seed
3. Click **Generate Images**
4. Click any image to open full-size; click ↗ to download

### API (OpenAI-compatible)

```bash
POST /v1/images/generations
Content-Type: application/json

{
  "prompt": "a red fox sitting in autumn leaves, photorealistic",
  "model": "flux",
  "size": "1024x1024",
  "n": 1,
  "seed": -1,
  "enhance": false
}
```

Response:
```json
{
  "created": 1711584000,
  "provider": "pollinations",
  "model": "flux",
  "data": [
    {"url": "https://image.pollinations.ai/prompt/..."}
  ]
}
```

### Available Models

| Model | Description |
|---|---|
| `flux` | Default — high quality, versatile |
| `flux-realism` | Photorealistic images |
| `flux-anime` | Anime / manga style |
| `flux-3d` | 3D rendered style |
| `flux-cablyai` | CablyAI fine-tune |
| `turbo` | Fast generation (SDXL Turbo) |

List models: `GET /v1/images/models`

### Size Options

Any `WxH` up to `2048x2048`. Common presets:
- `1024x1024` (square, default)
- `1280x720` (landscape / 16:9)
- `720x1280` (portrait / 9:16)
- `1920x1080` (Full HD)

---

## Settings Dashboard

Navigate to `/settings` for the full control panel.

### Tab: API Keys
Manage provider keys at runtime — see [Managing API Keys via UI](#managing-api-keys-via-ui).

### Tab: Routing
Drag providers up/down to change priority. The router falls back top → bottom.
- Click **Save Order** to persist
- Click **Reset to Default** to undo customization

### Tab: Models
Override the model hierarchy per provider — reorder, add, or remove models.
Changes apply within ~30 seconds (Redis config cache TTL).

### Tab: Image Generation
Live image generator — see [Image Generation](#image-generation).

### Tab: CF Workers
Deploy and manage Cloudflare Workers that expose an OpenAI-compatible endpoint backed by Workers AI.
- **List deployed workers** with creation date and copy URL button
- **Provisioning state** — newly created workers show "◌ Provisioning…" while Cloudflare's API propagates (can take 2–30 seconds); the list auto-refreshes every 4 seconds until all workers go active
- **Create new worker** — pick a model from the Cloudflare model list
- **Delete workers** — optimistic UI (row dims immediately); reverts if deletion fails

### Tab: Cache
- View hit rate, total hits/misses, and cached entry count
- **Clear All Cache** — deletes all `arbiter:cache:*` keys from Redis (irreversible)

---

## Chat Playground

Navigate to `/playground` (or click **Playground** in the sidebar).

### Endpoint selector

The dropdown is grouped into four categories that auto-populate at load time:

| Group | Source | How routed |
|---|---|---|
| **Gateway Providers** | `/v1/models` (standard providers) | `POST /v1/chat/completions?vendor={name}` |
| **Cloudflare Workers** | Active workers from CF registry | `POST /v1/chat/completions` with `model: cfworker/{name}` |

### Config panel

- **System prompt** — prepended as a `system` message
- **Temperature** — slider 0.0–2.0
- **Max tokens** — leave blank for provider default

### Usage

1. Select an endpoint from the dropdown
2. Optionally set system prompt / temperature
3. Type a message and press **Enter** (or click Send)
4. Each assistant reply shows a latency badge (ms)
5. Press **Shift+Enter** to insert a newline in your message

---

## Log Viewer

Navigate to `/logs` (or click **Logs** in the sidebar).

The log viewer displays all application logs captured since startup — up to 5,000 records are kept in a circular in-memory buffer.

### Filters

| Control | Description |
|---|---|
| **Level pills** | Toggle DEBUG / INFO / WARNING / ERROR / CRITICAL (inclusive above selected) |
| **Logger** | Filter by module name prefix (e.g. `app.api`, `app.routing`) |
| **Search** | Full-text search in the formatted message (300 ms debounce) |
| **Tail** | Show only the last N records after other filters |
| **Limit** | Max records displayed: 100 / 200 / 500 / 1000 / 5000 |
| **Sort** | Newest first / Oldest first |

### Auto-refresh

Select a refresh interval (2 s / 5 s / 10 s / 30 s) for live-tail monitoring. A pulse indicator appears when auto-refresh is active.

### Export

- **Download** — saves the current view as a `.txt` file
- **Copy** — copies all visible log lines to clipboard
- **Clear** — wipes the in-memory buffer (not reversible)

### REST API

```bash
# Fetch last 50 ERROR+ records
GET /logs/records?level=ERROR&limit=50

# Search for a keyword, newest first
GET /logs/records?q=rate+limit&newest_first=true

# Tail the last 20 lines from app.api.*
GET /logs/records?logger_name=app.api&tail=20

# List all logger names
GET /logs/loggers

# Clear buffer
DELETE /logs/clear
```

---

## Analytics Dashboard

Navigate to `/analytics` (or click **Analytics** in the sidebar).

The analytics page provides a comprehensive view of all gateway traffic, key health, and model usage.

### Strict Authentication Banner

If a red banner appears at the top of the page reading **"Authentication is
NOT enforced"**, your gateway is running in legacy permissive mode. Either:

- Create at least one Gateway Token under **Settings → Gateway Keys** and
  ensure `REQUIRE_AUTH=true` (default), **or**
- Set `REQUIRE_AUTH=false` explicitly in `.env` if you intend to expose
  unauthenticated `/v1/*` traffic.

In strict mode (the default), `/v1/*` returns 401 unless a valid Bearer
token is presented.

### Filter Bar

Above the KPI cards, a filter bar lets you slice analytics:

- **From / To** — date range; daily-rollup data is available for the last
  90 days.
- **Token** — restrict counters to a single Gateway Token (or the special
  `env-var keys` aggregate for traffic from `GATEWAY_API_KEYS`).
- **Provider / Model** — drill down to one provider or model.
- **Quick presets** — `7d` and `30d` buttons.
- **Apply / Clear** — apply applies a query, clear resets to lifetime view.

When a range is active, a **Range Summary** card appears below with a
sparkline of daily requests, success-rate KPI, error count, tokens used,
and three top-5 tables (top providers, top models, top tokens).

### Per-Gateway-Token Usage

A new table breaks down lifetime traffic per Gateway Token:

| Column | Description |
|--------|-------------|
| **Token** | Friendly name (or `env-var keys` for env aggregate) |
| **Token ID** | Internal short id used in `?token_id=…` filter |
| **Requests / Success / Errors** | Lifetime counters |
| **Tokens Used** | Sum of prompt + completion tokens billed |
| **Last Used** | Local timestamp of the last successful request |

Click any row to filter the rest of the page to that token.

### KPI Summary Cards

Six live cards updated on every refresh:

| Card | What it shows |
|------|--------------|
| **Total Requests** | Cumulative request count with ok/failed breakdown |
| **Success Rate** | % of successful completions; color-coded bar (green/yellow/red) |
| **Cache Hit Rate** | % of responses served from cache; hits vs misses |
| **Avg Latency** | Mean round-trip time across all providers (ms) |
| **Tokens Consumed** | Total tokens processed; top consuming provider shown |
| **Active API Keys** | Active / configured key ratio across all providers |

### Charts

| Chart | Description |
|-------|-------------|
| **Request History** | 4-hour area chart in 5-minute buckets — requests, success, errors |
| **Provider Distribution** | Donut chart showing request share per vendor |
| **Avg Latency by Provider** | Horizontal bar ranking providers by response time |
| **Token Consumption** | Horizontal bar showing tokens used per provider |
| **Error Rate Trend** | Error count over time matching the history window |

### API Key Health Matrix

One card per configured provider showing **live quota usage** for each API key:

- **Status dot** pulses green (healthy), yellow (degraded), or red (unavailable)
- **RPM bar** — requests per minute used vs limit
- **TPM bar** — tokens per minute used vs limit
- **Daily bar** — daily requests used vs limit
- Color thresholds: green < 60%, yellow 60–85%, red ≥ 85%
- **Health score** — composite availability score (0–100%)

### Provider & Model Tables

- **Provider Breakdown** — requests, success, errors, rate-limit hits, success rate bar, avg latency
- **Model Breakdown** — per-model requests, tokens, tokens/req, errors, success rate, FREE badge for free-tier models
- **Error Analysis** — error trend chart + ranked list of most error-prone models

### Auto-refresh & Reset

- Refresh intervals: 5s / 15s / 30s / 1m / Off (default 30s)
- **Reset Stats** — clears all `arbiter:stats:*` counters from Redis (cannot be undone)

---

## CF Workers in the Gateway

Deployed CF Workers are automatically available as models in the gateway — no extra configuration needed.

### Using a CF Worker via `/v1/chat/completions`

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "cfworker/my-worker-name",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

The gateway looks up `my-worker-name` in the CF worker registry, finds its `workers.dev` URL, and proxies the request directly.

### Discovering available models

```bash
curl http://localhost:8000/v1/models | jq '.data[] | select(.owned_by == "cloudflare-worker")'
```

---

## Installation

### Prerequisites

- **Python 3.10+** (or Docker)
- **Redis** (optional — gateway has built-in fallback)
- API keys from one or more providers (see [Configuration](#configuration))

### Step 1: Clone the Repository

```bash
git clone https://github.com/yourusername/arbiter.git
cd arbiter
```

### Step 2: Install Dependencies

**Option A: Local Python**
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**Option B: Docker**
```bash
docker build -t arbiter .
```

### Step 3: Verify Installation

```bash
# Local
python3 -c "from app.main import app; print('✓ Installation successful')"

# Docker
docker run --rm arbiter python3 -c "from app.main import app; print('✓ Installation successful')"
```

---

## Configuration

### Step 1: Copy Environment Template

```bash
cp .env.example .env
```

### Step 2: Obtain API Keys

#### Google Gemini

1. Go to [AI Studio](https://aistudio.google.com/app/apikey)
2. Click "Create API Key"
3. Copy the key
4. You can create **multiple keys** for different projects/accounts

```bash
# In .env — free keys only
GEMINI_API_KEYS=key1,key2,key3

# Mixed paid + free (v1.13.3+) — tag the billing-enabled key with `#paid`
GEMINI_API_KEYS=paid-key#paid,free-key-1,free-key-2
```

**Per-key tier tagging** *(v1.13.3+)*:
- Untagged keys default to **`#free`** — used for the free-tier fallback chain.
- Keys tagged **`#paid`** unlock paid-only frontier models (Gemini 2.5/3 Pro).
- A `#paid` key can serve **both** free and paid models; a `#free` key is
  automatically skipped for paid models so it never burns free-tier quota
  on a 429.

**Free-tier models** (verified May 2026, priority order):
- `gemini-3.1-flash-lite` — ⭐ **Newest, fastest, default** ✅ **Recommended**
- `gemini-2.5-flash` — Quality bump, 10 RPM, 250 requests/day
- `gemini-2.5-flash-lite` — Highest free RPD: 15 RPM, 1,000 requests/day
- `gemini-3-flash-preview` — Frontier-class flash backup
- `gemini-2.0-flash`, `gemini-2.0-flash-lite` — Legacy high-quota fallbacks

**Paid-only models** (require a key tagged `#paid` in `.env`):
- `gemini-3.1-pro-preview` — Frontier reasoning, 1 M context
- `gemini-3-pro-preview` — Premium frontier
- `gemini-2.5-pro` — Premium 2.5 quality

#### Groq

1. Go to [Groq Console](https://console.groq.com/keys)
2. Click "Create API Key"
3. Copy the key

```bash
# In .env
GROQ_API_KEYS=key1,key2
```

**Free-tier models** (best performers):
- `llama-3.1-8b-instant` — ⚡ **Fastest**, 14,400 requests/day
- `llama-3.3-70b-versatile` — Best quality, 1,000 requests/day
- `qwen/qwen3-32b` — 60 RPM (higher quota), 1,000 requests/day

#### OpenRouter

1. Go to [OpenRouter Dashboard](https://openrouter.ai/keys)
2. Click "Create Key"
3. Copy the key
4. ⚠️ **Free account**: 50 requests/day
   - **With $10+ credit**: 1,000 requests/day (recommended)

```bash
# In .env
OPENROUTER_API_KEYS=key1
```

**Free-tier models** (March 2026):
- `meta-llama/llama-3.3-70b-instruct:free` — High quality
- `nousresearch/hermes-3-llama-3.1-405b:free` — Largest (405B)
- `google/gemma-3-27b-it:free` — Good quality
- `mistralai/mistral-small-3.1-24b-instruct:free` — Balanced

#### Cohere

1. Go to [Cohere Dashboard](https://dashboard.cohere.com/api-keys)
2. Click "New Key"
3. Copy the key
4. ⚠️ **Trial key**: 1,000 calls/month (~33/day), 20 RPM

```bash
# In .env
COHERE_API_KEYS=key1
```

**Free-tier models**:
- `command-r7b-12-2024` — Fastest 7B
- `command-r-08-2024` — Balanced
- `command-r-plus-08-2024` — Highest quality
- `command-a-03-2025` — Newest flagship (256K context)

#### Cloudflare Workers AI

1. Go to [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. Navigate to **Workers AI** → Create new Workers
3. Get your **Account ID** from Settings
4. Create an **API Token** with "Workers AI (Read)" scope

```bash
# In .env — Format: account_id|api_token
CLOUDFLARE_API_KEYS=abc1234567890xyz|Bearer_token_here
# Multiple accounts: id1|token1,id2|token2
```

**Free-tier limits**: 300 RPM, up to 10K neurons/day

**Available models** (11 total, verified March 2026):
- `@cf/meta/llama-4-scout-17b-16e-instruct` — Newest Llama 4 Scout
- `@cf/meta/llama-3.3-70b-instruct-fp8-fast` — High-quality 70B
- `@cf/moonshot/kimi-k2.5` — 256K context window
- `@cf/qwen/qwen3-30b-a3b-fp8` — Qwen 3 30B
- `@cf/mistralai/mistral-small-3.1-24b-instruct` — Mistral 24B
- `@cf/deepseek/deepseek-r1-distill-qwen-32b` — Reasoning model
- `@cf/qwen/qwq-32b` — QwQ reasoning
- `@cf/qwen/qwen2.5-coder-32b-instruct` — Coding specialist
- `@cf/google/gemma-3-12b-it` — Gemma 3 12B (128K context)
- `@cf/meta/llama-3.1-8b-instruct` — Fastest 8B
- `@cf/meta/llama-3.2-3b-instruct` — Smallest 3B

#### Cerebras Inference

1. Go to [Cerebras Cloud](https://cloud.cerebras.ai/)
2. Create account → API Keys section
3. Generate a new API key

```bash
# In .env
CEREBRAS_API_KEYS=key1,key2
```

**Free tier**: 30 RPM, 60K TPM, 1M tokens/day

**Available models** (4 total, verified March 2026):
- `llama3.1-8b` — Production, fastest
- `gpt-oss-120b` — Production, large (120B params)
- `qwen-3-235b-a22b-instruct-2507` — Preview, large reasoning
- `zai-glm-4.7` — Preview, GLM model

#### HuggingFace Inference API

1. Go to [HuggingFace Tokens](https://huggingface.co/settings/tokens)
2. Create token with "Read" scope
3. Copy the token

```bash
# In .env
HUGGINGFACE_API_KEYS=hf_your-token-here
```

**Note**: Limited free credits (~$0.10/month equivalent)

**Available models** (4 total):
- `Qwen/Qwen2.5-7B-Instruct` — Most reliable
- `mistralai/Mistral-7B-Instruct-v0.3` — Mistral base
- `HuggingFaceH4/zephyr-7b-beta` — General purpose
- `google/gemma-2-2b-it` — Smallest/fastest

#### Pollinations.ai — 🔑 Free tier (key required)

Get a free API key at [enter.pollinations.ai](https://enter.pollinations.ai/) — keys start with `sk_` (secret) or `pk_` (publishable).

```bash
# In .env
POLLINATIONS_API_KEYS=sk_your-key-here
```

**Available models** (11 total):
- `openai` — GPT-based backend *(default)*
- `openai-fast` / `openai-large` — Faster or higher quality GPT
- `claude` / `claude-fast` / `claude-large` — Claude-based backends
- `gemini` / `gemini-fast` — Gemini-based backends
- `mistral` — Mistral backend
- `deepseek` — DeepSeek backend
- `qwen-coder` — Qwen coding model

### Step 3: Configure Your `.env` File

```bash
# ── Server Configuration ────────────────────────────
HOST=0.0.0.0
PORT=8000
DEBUG=false
LOG_LEVEL=INFO

# ── Redis (optional) ────────────────────────────────
REDIS_URL=redis://localhost:6379
CACHE_TTL=3600                    # Cache for 1 hour

# ── Gateway Authentication (optional) ──────────────
# Single key:
# GATEWAY_API_KEY=your-secret-key

# Multiple keys (recommended):
# GATEWAY_API_KEYS=key1,key2,key3

# ── Cloudflare Access JWT (optional) ───────────────
# ENABLE_CF_ACCESS=true
# CLOUDFLARE_ACCESS_TEAM_NAME=myteam
# CLOUDFLARE_ACCESS_AUD=aud-from-app-settings

# ── Provider API Keys (at least ONE provider required) ─
# Gemini (https://aistudio.google.com/app/apikey)
GEMINI_API_KEYS=key1,key2

# Groq (https://console.groq.com/keys)
GROQ_API_KEYS=key1

# Cloudflare Workers AI (format: account_id|api_token)
CLOUDFLARE_API_KEYS=account123|token-here

# Cerebras Inference (https://cloud.cerebras.ai)
CEREBRAS_API_KEYS=key1

# OpenRouter (https://openrouter.ai/keys)
OPENROUTER_API_KEYS=key1

# Cohere (https://dashboard.cohere.com/api-keys)
COHERE_API_KEYS=key1

# HuggingFace (https://huggingface.co/settings/tokens)
HUGGINGFACE_API_KEYS=hf_token_here

# Pollinations.ai (https://enter.pollinations.ai — free key)
POLLINATIONS_API_KEYS=sk_your-key-here
```

### Step 4: Verify Configuration

```bash
# Check .env is valid
python3 -c "from app.config import settings; print(f'Providers: {list(settings.__dict__.keys())}')"
```

---

## Starting the Gateway

### Option 1: Local Development

**Start Redis** (optional — gateway has in-memory fallback):
```bash
# Using Docker
docker run -d -p 6379:6379 redis:7-alpine

# Or if you have Redis installed locally
redis-server --port 6379
```

**Start Gateway**:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Output:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete
```

✅ **Gateway is ready!** Navigate to:
- API: `http://localhost:8000/v1/models`
- Dashboard: `http://localhost:8000/dashboard`

### Option 2: Docker Compose (Production)

```bash
docker compose up -d
docker compose logs -f gateway
```

This starts:
- **gateway** on port 8000
- **redis** on port 6379 with persistence

---

## Making Your First Request

### Smart Auto-Routing (v1.12+)

The simplest call: send `"model": "auto"` and Arbiter classifies the prompt and picks the best free-tier model for you.

```bash
curl -i -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role":"user","content":"Write a python function to reverse a string"}]
  }'
```

Look at the response — Arbiter adds an **`X-Arbiter-Model-Used: <provider>/<model>`** header showing which model fulfilled the request.

#### Per-request overrides (no Settings change required)

| Header | Body field | Values |
| --- | --- | --- |
| `X-Arbiter-Priority` | `metadata.priority` | `speed` · `quality` · `balanced` |
| `X-Arbiter-Prefer-Provider` | `metadata.prefer_provider` | any provider name (e.g. `cerebras`) |
| `X-Arbiter-Fallback` | `fallback` | `none` · `same_provider` · `chain` |
| — | `metadata.arbiter_intent` | `code` · `reasoning` · `long-context` · `vision` · `creative` · `fast` · `balanced` |

#### Explicit pin + optional fallback

```bash
# Strict pin — only use this exact model (default behaviour)
curl ... -d '{"model":"qwen/qwen3-32b","messages":[…]}'

# Walk other models on the same provider if it fails
curl ... -d '{"model":"qwen/qwen3-32b","fallback":"same_provider","messages":[…]}'

# Cross-provider fallback via the auto-router
curl ... -d '{"model":"qwen/qwen3-32b","fallback":"chain","messages":[…]}'
```

#### Deployment-wide preferences

The **Settings → Auto Routing** tab (or the `/api/preferences/auto-route` REST endpoint) lets you set:

- Priority bias (speed / quality / balanced)
- Prefer/avoid provider lists
- Per-intent preferred model order (code, reasoning, creative, vision, long-context, fast)
- Allow paid Routeway fallback when no free model can serve the request

### Test 1: List Models

```bash
curl http://localhost:8000/v1/models | jq .
```

Expected response:
```json
{
  "object": "list",
  "data": [
    {
      "id": "gemini-2.5-flash-lite",
      "object": "model",
      "created": 1700000000,
      "owned_by": "gemini"
    },
    ...
  ]
}
```

### Test 2: Simple Chat Completion

**Example with Gemini (newest model)**:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-lite",
    "messages": [
      {"role": "user", "content": "Hello! Who are you?"}
    ],
    "temperature": 0.7,
    "max_tokens": 256
  }' | jq .
```

**Example with Groq (fast)**:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.3-70b-versatile",
    "messages": [
      {"role": "user", "content": "Explain quantum computing in simple terms."}
    ],
    "temperature": 0.5,
    "max_tokens": 512
  }' | jq .
```

**Example with Cloudflare Workers AI (reasoning)**:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "@cf/deepseek/deepseek-r1-distill-qwen-32b",
    "messages": [
      {"role": "user", "content": "Solve: 2x + 5 = 13"}
    ]
  }' | jq .
```

**Example with Pollinations (free, no setup needed)**:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistral",
    "messages": [
      {"role": "user", "content": "What is machine learning?"}
    ]
  }' | jq .
```

Expected response (all providers):
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1709040000,
  "model": "gemini-3.1-flash-lite",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! I'm an AI assistant..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 8,
    "completion_tokens": 42,
    "total_tokens": 50
  }
}
```

### Test 3: Check Dashboard

Open browser:
```
http://localhost:8000/dashboard
```

You should see:
- ✅ Status: Online
- 📊 Request counts
- 🔄 Active accounts and their quota usage
- 💾 Cache hit rate

---

## Understanding Rate Limits

### How the Gateway Manages Quotas

The gateway **tracks usage per API key** in Redis:

```
Each key is scored by remaining quota:
  Score = (rpm_remaining × 0.30) + (tpm_remaining × 0.20) + (daily_remaining × 0.50)

The router always picks the key with the HIGHEST score.
```

Example:
```
Account A:  100/500 daily used  → 80% available
Account B:    5/50  daily used  → 90% available
Account C:  100/150 daily used  → 33% available

Score A = 0.8 × 0.50 = 0.40
Score B = 0.9 × 0.50 = 0.45  ← Highest → Pick Account B
Score C = 0.33 × 0.50 = 0.17
```

### Free-Tier Quotas

| Provider | Model | RPM | TPM | Daily |
|---|---|---|---|---|
| **Gemini** | gemini-2.5-flash-lite | 15 | 250K | 1,000 |
|  | gemini-2.5-flash | 10 | 250K | 250 |
|  | gemini-2.5-pro | 5 | 250K | 100 |
| **Groq** | llama-3.1-8b-instant | 30 | 6K | 14,400 |
|  | llama-3.3-70b-versatile | 30 | 12K | 1,000 |
|  | qwen/qwen3-32b | 60 | 6K | 1,000 |
| **OpenRouter** | any :free model | 20 | — | 50 / 1,000* |
| **Cohere** | any model | 20 | — | 33 |

*OpenRouter: 50/day without credits, 1,000/day with $10+

### What Happens at Limits?

| Limit | Behavior |
|---|---|
| **RPM hit** | Request delayed/queued for next minute window |
| **TPM hit** | Request routed to next available key/model/provider |
| **Daily hit** | Key skipped, tries next best key until one succeeds |
| **All keys exhausted** | 429 TooManyRequests error returned |

### Monitoring Quota Usage

**In Dashboard:**
- View per-account usage bars (RPM, TPM, Daily)
- See availability score (0–100%)
- Visual feedback: 🟢 healthy, 🟡 degraded, 🔴 unavailable

**Via API:**
```bash
curl http://localhost:8000/dashboard/stats | jq '.providers[] | {name, active_keys, keys}'
```

**Via Redis CLI:**
```bash
redis-cli
> KEYS "gemini:*:daily"
> GET gemini:a1b2c3d4:daily          # tokens used today
> GET gemini:a1b2c3d4:rpm             # requests this minute
```

### Tips to Maximize Quota

1. **Use multiple accounts**
   - Add 3 Gemini accounts → 3× the daily quota
   ```bash
   GEMINI_API_KEYS=key1,key2,key3
   ```

2. **Choose high-quota models**
   - Prefer `gemini-2.5-flash-lite` (1,000/day) over `pro` (100/day)
   - Prefer `llama-3.1-8b-instant` on Groq (14,400/day)

3. **Cache deterministic requests**
   - Set `temperature: 0.0` or low values (≤ 0.3) to cache responses
   - Same request = instant cached response (no quota used)

4. **Add OpenRouter credits**
   - Free: 50 requests/day
   - With $10 credit: 1,000 requests/day
   - Very good ROI for fallback model

5. **Use smaller models for simple tasks**
   - Groq 8B is 30× faster than 70B
   - Router automatically picks based on token count
   - Small prompts use less TPM quota

---

## API Reference

### Endpoint: POST `/v1/chat/completions`

OpenAI-compatible chat completion.

**URL:** `POST http://localhost:8000/v1/chat/completions`

**Headers:**
```
Content-Type: application/json
Authorization: Bearer {gateway_api_key}  # Only if GATEWAY_API_KEY set
```

**Request Body:**
```json
{
  "model": "gemini-2.5-flash-lite",
  "messages": [
    {
      "role": "system",
      "content": "You are a helpful assistant."
    },
    {
      "role": "user",
      "content": "What is the capital of France?"
    }
  ],
  "temperature": 0.7,
  "top_p": 1.0,
  "max_tokens": 512,
  "stop": ["END"],
  "stream": false
}
```

**Parameters:**

| Name | Type | Default | Description |
|---|---|---|---|
| `model` | string | (required) | Model ID (e.g., `gemini-2.5-flash-lite`) or `"auto"` for smart routing |
| `messages` | array | (required) | Array of message objects |
| `messages[].role` | string | (required) | `user`, `assistant`, or `system` |
| `messages[].content` | string \| array | (required) | Message text, or an array of content blocks for multimodal input |
| `temperature` | float | 0.7 | Sampling temperature (0–2) — higher = more creative. Values ≤ 0.3 are cached. |
| `top_p` | float | 1.0 | Nucleus sampling (0–1) — controls diversity |
| `max_tokens` | integer | — | Max tokens to generate (optional) |
| `stop` | array | — | Stop sequences (optional) |
| `stream` | boolean | false | Enable SSE streaming — see [Streaming](#streaming) section |
| `fallback` | string | — | `none` (strict pin) \| `same_provider` \| `chain`. Only applies when `model` is a specific model, not `"auto"`. |
| `metadata` | object | — | Routing hints: `arbiter_intent`, `priority`, `prefer_provider` — see below |

**Query parameters:**

| Param | Description |
|---|---|
| `?vendor=<name>` | Pin a specific provider (e.g. `?vendor=groq`). Tried first; falls back normally on failure. |
| `?force_model=<id>` | Override the `model` body field — bypasses automatic model selection entirely. |

**Response (200 OK):**
```json
{
  "id": "chatcmpl-abc123def456",
  "object": "chat.completion",
  "created": 1709040000,
  "model": "gemini-2.5-flash-lite",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Paris is the capital of France."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 30,
    "completion_tokens": 8,
    "total_tokens": 38
  }
}
```

**Error Responses:**

| Status | Error | Meaning |
|---|---|---|
| 400 | `invalid_request_error` | Missing/invalid field (e.g., `messages` required) |
| 401 | `authentication_error` | Invalid gateway API key (if `GATEWAY_API_KEY` set) |
| 429 | `rate_limit_error` | All accounts/models exhausted |
| 500 | `server_error` | Gateway or provider error (check logs) |

---

## Streaming

Arbiter fully supports **Server-Sent Events (SSE)** streaming for all 12 providers. Set `"stream": true` in any `/v1/chat/completions` request to receive the response as a real-time token stream. The format is identical to the OpenAI streaming API.

> **Note:** `cfworker/*` proxy models do **not** support streaming and will return HTTP 400 if `stream: true` is requested.

### Quick start — curl

```bash
curl -sS -N \
  -H "Authorization: Bearer YOUR_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello!"}],"stream":true}' \
  http://localhost:8000/v1/chat/completions
```

### Python (openai SDK)

```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="your-gateway-token",
)

with client.chat.completions.stream(
    model="auto",
    messages=[{"role": "user", "content": "Write a haiku about the sea."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
print()
```

### JavaScript (openai SDK)

```typescript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8000/v1",
  apiKey: "your-gateway-token",
});

const stream = await client.chat.completions.stream({
  model: "auto",
  messages: [{ role: "user", content: "Write a haiku about the sea." }],
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
console.log();
```

### SSE event format

```
: arbiter-model-used: gemini/gemini-2.5-flash-lite

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}

data: [DONE]
```

| Event | Meaning |
|---|---|
| `: arbiter-model-used: <provider/model>` | SSE comment identifying the serving provider. `cache/<model>` = Redis cache hit. |
| `: thinking / evaluating / generating / almost there` | Keepalive heartbeat every ~5 s. Invisible to OpenAI SDKs; prevents proxy idle-timeouts. |
| `data: {...,"delta":{"content":"..."}}` | Text delta — append to your buffer. |
| `data: {...,"finish_reason":"stop","usage":{...}}` | Final chunk — includes token usage when the provider reports it. |
| `data: [DONE]` | Stream end marker. |
| `data: {"error":{...}}` | Mid-stream error (rare — only if provider fails after first chunk sent). |

### Caching with streaming

Requests with `temperature ≤ 0.3` are cached in Redis. Cache reads replay instantly as a synthetic stream. Cache writes happen after `[DONE]`. A non-streaming request that populates the cache makes the next streaming request instant, and vice versa.

### Fallback behaviour

- **Pre-first-chunk failure** → Arbiter falls back transparently to the next provider (no stream emitted yet).
- **Post-first-chunk failure** → A `data: {"error":{...}}` event is emitted and the stream ends. Arbiter cannot redirect after bytes have been sent.

---

### Endpoint: GET `/v1/models`

List available models.

**URL:** `GET http://localhost:8000/v1/models`

**Response (200 OK):**
```json
{
  "object": "list",
  "data": [
    {
      "id": "gemini-2.5-flash-lite",
      "object": "model",
      "created": 1700000000,
      "owned_by": "gemini"
    },
    ...
  ]
}
```

### Endpoint: GET `/health`

Health check.

**URL:** `GET http://localhost:8000/health`

**Response (200 OK):**
```json
{
  "status": "ok",
  "redis": "connected",
  "providers": ["gemini", "groq", "openrouter"],
  "version": "1.0.0"
}
```

### Endpoint: GET `/dashboard`

Web dashboard (HTML).

**URL:** `GET http://localhost:8000/dashboard`

Opens an interactive dashboard showing real-time stats.

### Endpoint: GET `/dashboard/stats`

Dashboard stats (JSON).

**URL:** `GET http://localhost:8000/dashboard/stats`

**Response:**
```json
{
  "status": "online",
  "requests": {
    "total": 143,
    "success": 140,
    "failed": 3,
    "success_rate": 97.9
  },
  "cache": {
    "hits": 28,
    "misses": 115,
    "hit_rate": 19.6,
    "cached_responses": 12
  },
  "providers": [
    {
      "name": "gemini",
      "models": ["gemini-2.5-flash-lite", ...],
      "total_keys": 2,
      "active_keys": 2,
      "keys": [
        {
          "hash": "a1b2c3d4",
          "status": "active",
          "score": 0.95,
          "rpm": { "used": 2, "limit": 15 },
          "tpm": { "used": 512, "limit": 250000 },
          "daily": { "used": 125, "limit": 1000 }
        },
        ...
      ],
      ...
    }
  ]
}
```

---

## Usage Examples

### Example 1: Simple Prompt

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "Write a haiku about the moon"}],
    "temperature": 0.8,
    "max_tokens": 100
  }' | jq '.choices[0].message.content'
```

Output:
```
"Silver crescent glows,
Tides dance to ancient whispers,
Dreams in moonlight rest."
```

### Example 2: Multi-Turn Conversation

```bash
python3 << 'EOF'
import requests
import json

API_URL = "http://localhost:8000/v1/chat/completions"

messages = [
    {"role": "system", "content": "You are a Python expert."},
    {"role": "user", "content": "How do I read a JSON file?"},
]

response = requests.post(API_URL, json={
    "model": "llama-3.3-70b-instruct:free",
    "messages": messages,
    "temperature": 0.5,
    "max_tokens": 256
})

assistant_msg = response.json()["choices"][0]["message"]["content"]
print(f"Assistant: {assistant_msg}")

# Follow-up
messages.append({"role": "assistant", "content": assistant_msg})
messages.append({"role": "user", "content": "Can you show me a complete example?"})

response = requests.post(API_URL, json={
    "model": "llama-3.3-70b-instruct:free",
    "messages": messages,
    "temperature": 0.5,
    "max_tokens": 512
})

print(f"\nAssistant (follow-up): {response.json()['choices'][0]['message']['content']}")
EOF
```

### Example 3: Streaming (when available)

```python
# Not yet supported — coming in next version
# For now, use non-streaming mode and buffer responses
```

### Example 4: Caching with Low Temperature

```bash
# Request 1: First time (no cache)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "temperature": 0.0
  }' | jq '.id'

# Request 2: Identical (cached — instant response)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "What is 2+2?"}],
    "temperature": 0.0
  }' | jq '.id'

# Both return same ID (cached response)
```

### Example 5: Explicit Model Selection

```bash
# Try a specific Groq model
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b-instant",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 0.7
  }' | jq '.model'

# Output: "llama-3.1-8b-instant"
```

### Example 6: Integration with OpenClaw

```python
# In OpenClaw agent code, replace OpenAI client:

from anthropic import Anthropic

# Instead of:
# client = OpenAI(api_key="sk-...")

# Use gateway:
client = Anthropic(
    base_url="http://localhost:8000/v1",
    api_key="default"  # or your GATEWAY_API_KEY if set
)

# Now all OpenClaw agents use the gateway!
response = client.messages.create(
    model="gemini-2.5-flash-lite",
    messages=[{"role": "user", "content": "Help me with X"}],
    max_tokens=512,
)

print(response.content[0].text)
```

---

## Best Practices

### 1. Set Appropriate Temperature

- **temp = 0.0** — Deterministic (cached, best for caching)
- **temp ≤ 0.3** — Mostly deterministic (low variance, cached)
- **temp = 0.7–0.8** — Balanced (default, good quality + variety)
- **temp ≥ 1.5** — Very creative (different each time, no caching)

```bash
# Good for caching
{"temperature": 0.1}

# Good for variety (no caching)
{"temperature": 1.2}
```

### 2. Use System Prompts Sparingly

Large system prompts consume tokens. Keep them focused:

```json
{
  "messages": [
    {
      "role": "system",
      "content": "You are a Python expert. Answer concisely."
    },
    {
      "role": "user",
      "content": "How do I read a file?"
    }
  ]
}
```

NOT:
```json
{
  "messages": [
    {
      "role": "system",
      "content": "You are a world-class Python expert with 20 years of experience. You have deep knowledge of... (100+ lines)"
    }
  ]
}
```

### 3. Add Multiple API Keys

Distribute requests across accounts:

```bash
# .env
GEMINI_API_KEYS=key1,key2,key3,key4
GROQ_API_KEYS=key1,key2
OPENROUTER_API_KEYS=key1
```

The gateway automatically score each key and routes to the one with the most remaining quota.

### 4. Monitor Quota Usage

Check the dashboard regularly:

```bash
open http://localhost:8000/dashboard
```

Watch for:
- 🔴 Red accounts (exhausted quota)
- 🟡 Yellow accounts (running low)
- 🟢 Green accounts (plenty of quota)

Add more accounts before hitting limits.

### 5. Use Appropriate Models for Task

| Model | Best For | Speed | Quality |
|---|---|---|---|
| `gemini-2.5-flash-lite` | General purpose | ⚡ Fast | ⭐⭐⭐⭐ |
| `llama-3.1-8b-instant` | Fast completion | ⚡⚡ Very fast | ⭐⭐⭐ |
| `llama-3.3-70b` | Complex reasoning | ⏱️ Slow | ⭐⭐⭐⭐⭐ |
| `mistral-small-3.1:free` | Coding | ⏱️ Moderate | ⭐⭐⭐⭐ |
| `command-r-plus` | Long contexts | ⏱️ Slow | ⭐⭐⭐⭐ |

### 6. Handle Errors Gracefully

```python
import requests
import time

API_URL = "http://localhost:8000/v1/chat/completions"

for attempt in range(3):
    try:
        response = requests.post(API_URL, json={
            "model": "gemini-2.5-flash-lite",
            "messages": [{"role": "user", "content": "Hello"}],
        }, timeout=30)

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            # Rate limited — try again or use different model
            print("Rate limited, trying fallback model...")
            time.sleep(5)
        else:
            print(f"Error {response.status_code}: {response.text}")

    except requests.exceptions.Timeout:
        print("Timeout, retrying...")
        time.sleep(5)
    except Exception as e:
        print(f"Unexpected error: {e}")

raise Exception("Failed after 3 attempts")
```

### 7. Cache Aggressively

Use low temperature to cache responses:

```python
# Cache hit
response = requests.post(API_URL, json={
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "What is the capital of France?"}],
    "temperature": 0.0  # Deterministic → cached
})

# Cache miss (always new response)
response = requests.post(API_URL, json={
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "Write a creative story about..."}],
    "temperature": 1.5  # Stochastic → not cached
})
```

---

## Troubleshooting

### Problem: "Connection Refused"

```
Error: Failed to connect to http://localhost:8000
```

**Solution:**
1. Verify gateway is running:
   ```bash
   curl http://localhost:8000/health
   ```
2. If not running, start it:
   ```bash
   uvicorn app.main:app --port 8000
   ```
3. Check port isn't already in use:
   ```bash
   lsof -i :8000
   ```

### Problem: "No Available Key"

```json
{
  "error": {
    "message": "No available API key for gemini, skipping",
    "type": "rate_limit_error"
  }
}
```

**Solutions:**
1. Check `.env` has valid API keys:
   ```bash
   grep GEMINI_API_KEYS .env
   ```
2. Verify keys are working (test in vendor console)
3. Check quota on dashboard:
   ```
   http://localhost:8000/dashboard
   ```
4. If quota exhausted, wait until next day or add more keys

### Problem: "Invalid API Key"

```json
{
  "error": {
    "message": "Invalid API key",
    "type": "authentication_error"
  }
}
```

**Solution:**
1. Test API key directly with provider:
   ```bash
   # Gemini
   curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite/generateContent?key=YOUR_KEY" \
     -X POST \
     -H "Content-Type: application/json" \
     -d '{"contents": [{"parts": [{"text": "Hello"}]}]}'
   ```
2. Regenerate key if invalid (usually revoked/expired)
3. Update `.env` with new key

### Problem: "Requests Timing Out"

```
Timeout: Gateway did not respond within 30 seconds
```

**Solutions:**
1. Increase request timeout:
   ```python
   requests.post(URL, json=data, timeout=60)  # 60 second timeout
   ```
2. Check provider status (may be down):
   ```bash
   curl https://status.ai.google.com
   curl https://status.groq.com
   ```
3. Try smaller `max_tokens`:
   ```json
   {"max_tokens": 128}  # Instead of 2048
   ```

### Problem: "Cache Not Working"

**Symptom:** Same request returns different responses

**Solutions:**
1. Verify `temperature ≤ 0.3`:
   ```json
   {"temperature": 0.0}  # Will be cached
   {"temperature": 0.5}  # Won't be cached
   ```
2. Check Redis is running:
   ```bash
   redis-cli ping  # Should return PONG
   ```
3. Verify `CACHE_TTL` env var:
   ```bash
   grep CACHE_TTL .env  # Default 3600 seconds
   ```
4. Check cache stats:
   ```bash
   curl http://localhost:8000/dashboard/stats | jq '.cache'
   ```

### Problem: "Provider Not Available"

```
Provider 'gemini' not configured
```

**Solution:**
1. Add API key to `.env`:
   ```bash
   GEMINI_API_KEYS=your-key-here
   ```
2. Restart gateway:
   ```bash
   # Kill existing process
   pkill -f "uvicorn"

   # Restart
   uvicorn app.main:app --port 8000
   ```

### Problem: "Model Not Found"

```
Model 'gpt-4' is not available
```

**Solutions:**
1. Check available models:
   ```bash
   curl http://localhost:8000/v1/models | jq '.data[].id'
   ```
2. Use an available model:
   ```json
   {"model": "gemini-2.5-flash-lite"}
   ```
3. Add provider that supports the model:
   - `gpt-4` not supported (OpenAI paid-only)
   - Use `gemini-2.5-pro` or `llama-3.3-70b` instead

### Problem: "Rate Limit (429)"

```json
{
  "error": {
    "message": "All providers/models/keys failed",
    "type": "rate_limit_error"
  }
}
```

**Solutions:**
1. **Wait a minute** — RPM limits reset every 60 seconds
2. **Switch to different model** — has separate RPM counter
3. **Add more API keys** — they're scored independently
4. **Add OpenRouter credits** — $10 gives 20× daily quota boost
5. **Use lower token requests** — TPM limits are easier to hit

---

## Backup System (v1.14.2)

### Overview

Arbiter includes an enterprise-grade backup system that automatically backs up all Redis state and configuration to OCI Object Storage (S3-compatible).

### Accessing the Backup UI

Navigate to the **Backup** page from the sidebar. The UI shows:
- Storage usage bar and quota
- Last full/incremental backup timestamps
- Manual trigger buttons for full or incremental backups
- Backup history table with download/restore/delete actions

### Backup Types

| Type | Content | Schedule |
|------|---------|----------|
| **Full** | All Redis keys + data files | Weekly (Sunday 01:00 UTC) |
| **Incremental** | Config + tokens + last 48h stats | Daily (02:00 UTC) |

### Manual Backup

Click "Run Incremental" or "Run Full" in the Backup UI, or via API:

```bash
# Incremental
curl -X POST -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"type":"incremental"}' \
  http://localhost:8080/api/backup/run

# Full
curl -X POST -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"type":"full"}' \
  http://localhost:8080/api/backup/run
```

### Restore from Backup

From the UI: click the restore icon next to any backup in the list.

Via API:
```bash
curl -X POST -H "Authorization: Bearer <token>" \
  http://localhost:8080/api/backup/<key>/restore
```

> **Warning**: Restore overwrites current Redis state. Consider taking a backup first.

### Retention Policy

- Incremental backups older than **7 days** → auto-deleted
- Full backups older than **90 days** → auto-deleted
- Storage quota: **10 GB** (warning at 9 GB)

### Configuration

Add to `.env`:
```env
BACKUP_ENABLED=true
BACKUP_S3_ENDPOINT=https://<namespace>.compat.objectstorage.<region>.oraclecloud.com
BACKUP_S3_BUCKET=app-backups
BACKUP_S3_PREFIX=arbiter/backups
BACKUP_S3_ACCESS_KEY=<your-access-key>
BACKUP_S3_SECRET_KEY=<your-secret-key>
BACKUP_S3_REGION=us-chicago-1
BACKUP_MAX_GB=10
```

---

## Analytics Window Selection (v1.14.2)

The Analytics page now supports multiple time windows. Use the dropdown to select:

| Window | Granularity | Data Source |
|--------|-------------|-------------|
| 1 hour | 5 minutes | Real-time buckets |
| 4 hours | 5 minutes | Real-time buckets |
| 24 hours | 1 hour | Hourly rollups |
| 7 days | 1 hour | Hourly rollups |
| 30 days | 1 day | Daily rollups |
| 90 days | 1 day | Daily rollups |

Longer windows use progressively coarser granularity to maintain performance.

---

## Getting Help

- **Check logs**: `docker compose logs -f gateway`
- **Set DEBUG**: `LOG_LEVEL=DEBUG` in `.env`
- **Test providers** directly before filing issues
- **Check CHANGELOG.md** for known issues/workarounds

---

## Persistent Logs (180-day)

*(Added in v1.18.0)*

Arbiter writes three append-only JSONL log streams to `/app/data/logs/` (Docker-mounted volume). These persist across container restarts and are retained for **180 days** automatically.

### Log streams

| Stream | Path | Contents |
|--------|------|----------|
| API calls | `data/logs/api/YYYY-MM-DD.jsonl` | Every gateway request: token_id, provider, model, latency_ms, status_code, prompt/completion tokens, cached, client_ip |
| Activity | `data/logs/activity/YYYY-MM-DD.jsonl` | Every admin mutation (HMAC-tagged, before/after diffs, secret-redacted) |
| Errors | `data/logs/errors/YYYY-MM-DD.jsonl` | Structured upstream and internal errors by category |

### Query via API (admin only)

```bash
# 7-day summary
curl /api/logs/persistent/summary?days=7

# Last 50 API call records (paginated)
curl "/api/logs/persistent/api?days=1&limit=50&offset=0"

# Admin activity audit trail (30 days)
curl "/api/logs/persistent/activity?days=30"

# Error records
curl "/api/logs/persistent/errors?days=7"

# Force retention prune
curl -X POST /api/logs/persistent/prune
```

### Retention

A background janitor runs daily at **03:00 UTC** and deletes files older than 180 days. You can trigger manual pruning via `POST /api/logs/persistent/prune`.

### Security

- Bearer tokens, API keys, and password-shaped values are **auto-redacted** before write using a `head4…tail4 + sha256[:12]` fingerprint.
- All writes are best-effort — if disk is full the gateway continues serving traffic.

---

## Admin Activity Audit

*(Added in v1.18.0)*

Every admin mutation is recorded with an HMAC-SHA256 tamper-detection tag signed by `SESSION_SECRET_KEY`.

### Audited actions

| Endpoint | Actions logged |
|----------|---------------|
| `/api/providers/{name}/keys` | Key add, key remove |
| `/api/providers/{name}/enable\|disable` | Provider enable/disable |
| `/api/gateway/tokens` | Token create, delete, update, regenerate |
| `/settings/routing` | Routing update, reset |
| `/settings/cache` | Cache clear |
| `/cloudflare/workers` | Worker create, delete |
| `/api/announcements` | Banner create, delete |

### Record format

Each activity record includes:
- `ts` — ISO 8601 timestamp
- `actor_email` — Google SSO email or gateway token label
- `actor_role` — admin / gateway
- `action` — e.g. `provider.key.add`, `gateway_token.regenerate`
- `target` — what was changed (provider name, token ID, etc.)
- `before` / `after` — state diffs (secrets redacted)
- `client_ip` — request origin
- `hmac` — SHA-256 tag for tamper detection

### Verifying integrity

```python
import hmac, hashlib, json
SECRET = b"your-SESSION_SECRET_KEY"
with open("data/logs/activity/2026-05-12.jsonl") as f:
    for line in f:
        rec = json.loads(line)
        tag = rec.pop("hmac")
        expected = hmac.new(SECRET, json.dumps(rec, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(tag, expected), f"TAMPERED: {rec['action']}"
```

---

## Major-Change Dashboard Banners

*(Added in v1.18.0)*

Publish a banner notification visible to all dashboard users when a major upstream change occurs (e.g., model deprecation, API migration).

### Create a banner (admin)

```bash
curl -X POST /api/announcements \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Gemini 3.1 Flash Lite GA",
    "body": "Preview model discontinued May 25. Update to gemini-3.1-flash-lite.",
    "severity": "warning",
    "impacted_providers": ["gemini"],
    "action_required": "Change model string to gemini-3.1-flash-lite",
    "ttl_days": 3
  }'
```

### Properties

| Field | Required | Description |
|-------|----------|-------------|
| `title` | yes | Banner headline (3–200 chars) |
| `body` | yes | Details (3–2000 chars) |
| `severity` | no | `info` (blue), `warning` (yellow), `critical` (red). Default: `warning` |
| `impacted_providers` | no | Provider names — Arbiter resolves which gateway tokens use them |
| `action_required` | no | What the user should do |
| `docs_url` | no | Link to documentation |
| `ttl_days` | no | How long to display (1–30, default 3) |

### Behaviour

- Banners auto-expire after `ttl_days`.
- Per-browser dismissal via localStorage (reappears on other browsers).
- `GET /api/announcements/active` returns current banners with impacted token resolution.
- `DELETE /api/announcements/{id}` retracts immediately.

---

## Per-Token Rate Limiting

*(Added in v1.18.0)*

Every `/v1/*` request is checked against a sliding-minute-window rate limit after Bearer validation.

### Configuration

| Setting | Default | How to change |
|---------|---------|---------------|
| Global default | 100 req/min | `GATEWAY_TOKEN_RATE_LIMIT_PER_MIN` in `.env` |
| Per-token override | — | Set `request_limit_per_minute` via `PATCH /api/gateway/tokens/{id}` |
| Disable for a token | — | Set `request_limit_per_minute: 0` |

### Response headers on 429

```
HTTP/1.1 429 Too Many Requests
Retry-After: 42
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
```

### Fail-open

If Redis is unavailable, rate limiting is bypassed (fail-open) to avoid blocking legitimate traffic.

---

## Weekly AI Analysis Email

*(Added in v1.18.0)*

The existing daily report (22:00 IST / 16:30 UTC) is extended on **Mondays** with a weekly section. No separate weekly email is sent.

### Weekly section includes

- 7-day API call totals and error rate
- p50 / p95 latency
- Calls-by-provider breakdown
- Errors-by-category breakdown
- Admin activity change count
- **AI-generated SRE insights** (4–6 bullet points routed through Arbiter's own router)

### Configuration

No extra configuration required. The weekly section is automatically appended to the Monday daily report. The AI analysis uses `model=auto, max_tokens=600, temperature=0.2`.

---

## Adaptive Routing (v1.18)

*(Added in v1.18.0)*

### Gap A — Unhealthy provider demotion

Providers with a lifetime error rate ≥ 20% (minimum 100 requests sampled) are automatically moved to the **tail** of the routing chain. The demotion is cached for 60 seconds and re-evaluated continuously. This prevents cascading failures when a provider is experiencing sustained issues.

### Gap B — Wait-for-RPM-reset

When all keys for a provider are near their RPM cap and the minute boundary is within **10 seconds**, the gateway waits for reset instead of falling through to the next provider. This prevents unnecessary cross-provider fallbacks when the primary will be available imminently.

### TPM-aware key scoring

The key picker now considers each key's **tokens-per-minute** budget. If the estimated request size exceeds a key's remaining TPM headroom, that key is deprioritised regardless of its RPM score. The router passes its token estimate for every call.

### Updated scoring formula

```
score = (rpm_avail × 0.25) + (tpm_avail × 0.15) + (daily_avail × 0.40) + (health × 0.20)
```

Where `tpm_avail` is now computed as:
```
remaining_tpm = max(0, tpm_limit - tpm_used)
tpm_avail = max(0, (remaining_tpm - estimated_request_tokens × 1.1) / tpm_limit)
```

---

## Complexity-Aware Routing (v1.19)

*(Added in v1.19.0)*

### How It Works

Every incoming request is automatically analyzed for complexity before routing. The system scores 13 factors (message length, conversation depth, code complexity, expert-level indicators, etc.) and classifies the request into one of five tiers:

| Tier | Example | Routed To |
|------|---------|-----------|
| **TRIVIAL** | "hi", "thanks" | Fastest 7-8B models (Cerebras, Groq) |
| **SIMPLE** | "what's the capital of France?" | Fast models with decent quality |
| **MODERATE** | "explain async/await in Python with examples" | Balanced 32B-70B models |
| **COMPLEX** | "design a microservices architecture for..." | High-quality 70B-120B models |
| **EXPERT** | Multi-requirement system design with constraints | Flagship 120B-671B models |

### Smart Model Upgrade

If your client (e.g., an agent framework) hardcodes a small model name like `llama-3.1-8b` but the request is actually complex, Arbiter **automatically upgrades** to a capable model while keeping the original as a fallback. You don't need to change your client code.

### Provider Diversity

The router guarantees that at least 4 different providers appear in the top candidates. This prevents traffic concentration on a single provider and improves resilience.

### Load Distribution

A deterministic per-minute jitter rotates traffic across providers, ensuring no single provider accumulates disproportionate load over time — even for identical request patterns.

### What You Need to Do

**Nothing.** Complexity-aware routing is fully automatic. Continue sending requests as before — the gateway handles the intelligence behind the scenes.

If you want to force a specific model without upgrade, use the `@provider/model` syntax (e.g., `@cerebras/llama-3.1-8b`) which bypasses the smart upgrade.

---

**Next Steps:**
- [README.md](README.md) — Full project overview
- [DEVELOPER.md](DEVELOPER.md) — Architecture & extension guide
- [CHANGELOG.md](CHANGELOG.md) — Version history
