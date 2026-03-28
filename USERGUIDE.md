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
11. [Best Practices](#best-practices)
12. [Troubleshooting](#troubleshooting)

---

## Managing API Keys via UI

You can add, remove, test, and enable/disable provider API keys at runtime — **no container restart required**.

### Open Settings → API Keys tab

Navigate to `/settings` and click the **API Keys** tab (shown first by default).

Each provider card shows:
- **Status badge** — Active (green) or Inactive (grey)
- **Enable / Disable toggle** — instantly remove or restore a provider from the routing pool
- **Test button** — sends a minimal probe request and reports latency
- **Existing keys** — masked (e.g. `AIzaSy...Ab3c`), labelled `env` (from `.env` file) or `runtime` (added via UI)
- **Add key form** — paste a new key and click **Add Key**

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
| Pollinations | *(none needed)* | Free, anonymous |

### Cloudflare Workers AI Setup

1. Sign up at [cloudflare.com](https://cloudflare.com) (free)
2. Go to **Workers & Pages** — this activates the Workers AI free tier
3. Find your **Account ID** in the right sidebar of the Workers overview page
4. Go to **Profile → API Tokens → Create Token**
5. Use the "Cloudflare Workers AI" template (or add `AI Gateway: Read` permission)
6. In the API Keys tab, add the key as: `<Account_ID>|<API_Token>`

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
- **Create new worker** — pick a model from the Cloudflare model list
- **Delete workers** you no longer need

### Tab: Cache
- View hit rate, total hits/misses, and cached entry count
- **Clear All Cache** — deletes all `arbiter:cache:*` keys from Redis (irreversible)

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
# In .env
GEMINI_API_KEYS=key1,key2,key3
```

**Free-tier models** (verified March 2026):
- `gemini-3.1-flash-lite-preview` — ⭐ **Newest & Fastest** ✅ **Recommended**
- `gemini-3-flash-preview` — Frontier-class performance
- `gemini-2.5-flash-lite` — 15 RPM, 1,000 requests/day (stable)
- `gemini-2.5-flash` — 10 RPM, 250 requests/day (stable)

**⚠️ Paid-only models** (NOT included):
- `gemini-2.5-pro` — Requires billing
- `gemini-3.1-pro-preview` — Requires billing

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

#### Pollinations.ai — ✅ Completely Free

**🎉 NO API KEY REQUIRED!** The gateway auto-enables Pollinations.

```bash
# In .env — Leave this empty (or omit entirely)
# POLLINATIONS_API_KEYS=
```

**Available models** (3 total, completely free):
- `mistral` — Fast, general purpose
- `mistral-large` — Higher quality
- `openai` — GPT-based (OpenAI-compatible)

**Limits**: No documented rate limits, completely free tier ✨

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

# Pollinations.ai (NO KEY NEEDED — leave empty!)
# POLLINATIONS_API_KEYS=
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
    "model": "gemini-3.1-flash-lite-preview",
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
  "model": "gemini-3.1-flash-lite-preview",
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
| `model` | string | (required) | Model ID (e.g., `gemini-2.5-flash-lite`) |
| `messages` | array | (required) | Array of message objects |
| `messages[].role` | string | (required) | `user`, `assistant`, or `system` |
| `messages[].content` | string | (required) | Message text |
| `temperature` | float | 0.7 | Sampling temperature (0–2) — higher = more creative |
| `top_p` | float | 1.0 | Nucleus sampling (0–1) — controls diversity |
| `max_tokens` | integer | — | Max tokens to generate (optional) |
| `stop` | array | — | Stop sequences (optional) |
| `stream` | boolean | false | Streaming responses (not yet supported) |

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

## Getting Help

- **Check logs**: `docker compose logs -f gateway`
- **Set DEBUG**: `LOG_LEVEL=DEBUG` in `.env`
- **Test providers** directly before filing issues
- **Check CHANGELOG.md** for known issues/workarounds

---

**Next Steps:**
- [README.md](README.md) — Full project overview
- [DEVELOPER.md](DEVELOPER.md) — Architecture & extension guide
- [CHANGELOG.md](CHANGELOG.md) — Version history
