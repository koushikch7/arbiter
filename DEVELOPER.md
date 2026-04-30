# Arbiter – Developer Documentation

Complete guide to the architecture, code structure, and extension points for developers.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Structure](#project-structure)
3. [Core Concepts](#core-concepts)
4. [How to Add a New Provider](#how-to-add-a-new-provider)
5. [How to Extend the Router](#how-to-extend-the-router)
6. [Rate Limiting Internals](#rate-limiting-internals)
7. [Caching Internals](#caching-internals)
8. [Development Setup](#development-setup)
9. [Testing](#testing)
10. [Deployment Architecture](#deployment-architecture)

---

## Architecture Overview

### High-Level Data Flow

```
Client Request
    │
    ▼
┌─────────────────────────────────────────┐
│  FastAPI Endpoint Handler               │
│  (/v1/chat/completions)                 │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  Request Validation (Pydantic)          │
│  - Parse ChatCompletionRequest          │
│  - Validate schema                      │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  cfworker/ Early Intercept              │
│  - If model starts with "cfworker/"     │
│  - Look up URL in arbiter:cf:workers    │
│  - Proxy directly via httpx             │
│  - Return response (bypasses router)    │
└─────────────────────────────────────────┘
    │ Not a cfworker/ model
    ▼
┌─────────────────────────────────────────┐
│  Cache Lookup (Redis)                   │
│  - SHA256(model + messages)             │
│  - Only if temp ≤ 0.3                   │
└─────────────────────────────────────────┘
    │ Cache Miss
    ▼
┌─────────────────────────────────────────┐
│  IntelligentRouter.route()              │
│  ├─ _provider_order(request)            │
│  │  └─ Determine provider priority      │
│  ├─ _model_hierarchy(provider, request) │
│  │  └─ Get model fallback chain         │
│  │                                      │
│  └─ For each provider/model:            │
│     ├─ KeyPool.get_best_key()           │
│     │  └─ Score & select key            │
│     ├─ Provider.complete(request, key)  │
│     │  └─ Call vendor API               │
│     ├─ KeyPool.record_usage()           │
│     │  └─ Update Redis counters         │
│     └─ Cache response if temp ≤ 0.3     │
└─────────────────────────────────────────┘
    │
    ▼ Success
┌─────────────────────────────────────────┐
│  Response Formatting                    │
│  - Transform vendor response →          │
│    ChatCompletionResponse (OpenAI)      │
└─────────────────────────────────────────┘
    │
    ▼
Return JSON to Client
```

### Request Context Flow

```
┌─ Request ─────────────────────────────┐
│                                       │
│  ChatCompletionRequest                │
│  ├─ model: str                        │
│  ├─ messages: List[Message]           │
│  ├─ temperature: float                │
│  ├─ top_p: float                      │
│  ├─ max_tokens: Optional[int]         │
│  ├─ stop: Optional[List[str]]         │
│  └─ extra fields allowed              │
│                                       │
└───────────────────────────────────────┘
         │
         ▼
┌─ Router Decision Tree ─────────────────┐
│                                        │
│  1. Estimate tokens                   │
│     └─ len(words) × 1.3 heuristic    │
│                                        │
│  2. Select provider order             │
│     ├─ Token-aware: >100K→Gemini      │
│     ├─ Capability-aware: code→Pro     │
│     ├─ Explicit routing: "@cf/"→CF    │
│     └─ Default: Gemini→Groq→Cerebras →
│        Cloudflare→OpenRouter→Cohere→  │
│        HuggingFace→Pollinations       │
│                                        │
│  3. Get model hierarchy               │
│     └─ Filter by context window       │
│                                        │
│  4. Try each model in hierarchy       │
│     ├─ Try all keys (score order)     │
│     ├─ On RateLimitError: next key    │
│     └─ On ProviderError: next model   │
│                                        │
└────────────────────────────────────────┘
```

---

## Project Structure

```
arbiter/
│
├── app/                              # Main application code
│   ├── __init__.py
│   ├── main.py                       # FastAPI app setup, lifespan
│   ├── config.py                     # Pydantic BaseSettings
│   │
│   ├── models/                       # Data models
│   │   ├── __init__.py
│   │   └── schemas.py                # OpenAI-compatible Pydantic models
│   │
│   ├── providers/                    # Vendor adapters (11 total)
│   │   ├── __init__.py
│   │   ├── base.py                   # BaseProvider abstract class
│   │   ├── gemini.py                 # Google Gemini (4 models)
│   │   ├── groq_provider.py          # Groq (8 models)
│   │   ├── cloudflare.py             # Cloudflare Workers AI (11 models)
│   │   ├── cerebras.py               # Cerebras Inference (4 models)
│   │   ├── openrouter.py             # OpenRouter (7 free models)
│   │   ├── cohere_provider.py        # Cohere (4 models)
│   │   ├── huggingface.py            # HuggingFace (4 models)
│   │   ├── pollinations.py           # Pollinations.ai (3 free models)
│   │   ├── zai_provider.py           # Z.ai / Zhipu AI (3 free models)
│   │   ├── lightning_provider.py     # Lightning.ai LitAI (5 models)
│   │   └── modal_provider.py         # Modal.com vLLM GPU provider
│   │
│   ├── routing/                      # Routing logic
│   │   ├── __init__.py
│   │   └── router.py                 # IntelligentRouter class
│   │
│   ├── key_management/               # Rate limiting & key pool
│   │   ├── __init__.py
│   │   └── key_pool.py               # KeyPool class, scoring algorithm
│   │
│   ├── cache/                        # Caching layer
│   │   ├── __init__.py
│   │   └── cache.py                  # CacheLayer class
│   │
│   ├── middleware/                   # Request/response middleware
│   │   ├── __init__.py
│   │   └── auth.py                   # Gateway auth & Cloudflare Access JWT
│   │
│   └── api/                          # FastAPI routers
│       ├── __init__.py
│       ├── chat.py                   # POST /v1/chat/completions
│       ├── models_api.py             # GET /v1/models
│       ├── dashboard.py              # Dashboard & stats HTML endpoints
│       ├── settings_api.py           # GET/POST/DELETE /settings/routing, /settings/cache
│       ├── keys_api.py               # GET/POST/DELETE /api/providers/* (runtime key mgmt)
│       ├── image_api.py              # POST /v1/images/generations (Pollinations)
│       ├── cloudflare_manager.py     # Workers AI management + validate endpoint
│       ├── modal_manager.py          # Modal account/model info endpoints
│       └── modal_deploy.py           # Modal vLLM one-click deploy endpoints
│
├── static/                            # Static assets (served at /static/)
│   ├── arbiter.css                   # Shared design system (light/dark theme, components)
│   ├── arbiter.js                    # Shared JS (theme toggle, sidebar, toast, helpers)
│   ├── dashboard.html                # Web UI dashboard (/dashboard)
│   ├── api-docs.html                 # Interactive API documentation (/api-docs)
│   └── settings.html                 # Settings control panel (/settings)
│
├── Dockerfile                         # Docker image
├── docker-compose.yml                # Compose config
├── requirements.txt                  # Dependencies
├── .env.example                      # Environment template
│
├── README.md                         # Project overview
├── USERGUIDE.md                      # End-to-end user guide
├── CHANGELOG.md                      # Version history
└── DEVELOPER.md                      # This file
```

---

## Full API Reference

### Chat & Models (OpenAI-compatible)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/chat/completions` | Chat completions (OpenAI format) |
| `GET`  | `/v1/models` | List all available models |

### Image Generation

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/images/generations` | Generate images via Pollinations.ai (free) |
| `GET`  | `/v1/images/models` | List available image models |

### Provider Key Management

Keys are stored in `.env` and read fresh on every operation — no Redis layer.

| Method | Path | Description |
|---|---|---|
| `GET`    | `/api/providers` | List all providers with status, masked keys, pool stats |
| `POST`   | `/api/providers/{name}/keys` | Add a key `{"key": "..."}` — writes to `.env`, hot-reloads provider |
| `DELETE` | `/api/providers/{name}/keys/{hash}` | Remove a key by MD5 hash — removes from `.env` |
| `POST`   | `/api/providers/{name}/enable` | Enable a disabled provider (clears Redis disabled flag) |
| `POST`   | `/api/providers/{name}/disable` | Disable a provider (sets Redis disabled flag, removes from routing) |
| `POST`   | `/api/providers/{name}/test` | Probe connectivity, returns latency + sample reply |
| `POST`   | `/api/providers/reload` | Hot-reload all providers from current `.env` |

### Settings (Routing & Cache)

| Method | Path | Description |
|---|---|---|
| `GET`    | `/settings/routing` | Current routing config (provider order + model overrides) |
| `POST`   | `/settings/routing` | Update `{"provider_order": [...], "model_overrides": {...}}` |
| `DELETE` | `/settings/routing` | Reset to built-in defaults |
| `DELETE` | `/settings/cache` | Clear all cached responses from Redis |

### Cloudflare Workers AI Management

| Method | Path | Description |
|---|---|---|
| `GET`    | `/cloudflare/models` | List available Workers AI text-generation models |
| `POST`   | `/cloudflare/workers` | Create a Worker `{"name": "...", "model": "@cf/..."}` |
| `GET`    | `/cloudflare/workers` | List deployed workers (includes provisioning state) |
| `DELETE` | `/cloudflare/workers/{name}` | Delete a deployed worker |
| `POST`   | `/cloudflare/validate` | Validate CF API token permissions — returns permission matrix |

**`POST /cloudflare/validate` body (optional):**
```json
{ "key": "account_id|api_token" }
```
Omit body to validate the currently configured key. Response:
```json
{
  "all_ok": false,
  "checks": [
    {"name": "Workers Scripts (list)", "permission": "Workers Scripts:Read", "ok": true, "required_for": "list/create/delete workers"},
    {"name": "Workers AI (models)", "permission": "Workers AI:Execute", "ok": false, "http_status": 403, "note": "Token lacks AI Execute permission", "required_for": "Workers AI inference"},
    {"name": "Workers Subdomain", "permission": "Workers Subdomain:Read", "ok": true, "required_for": "worker URL generation"}
  ],
  "recommendation": "Add Workers AI:Execute permission to your API token"
}
```

### Modal.com GPU Deployment

| Method | Path | Description |
|---|---|---|
| `GET`    | `/modal/deploy/check` | Check Modal CLI availability and token configuration |
| `POST`   | `/modal/deploy` | Start a vLLM deployment on Modal GPU |
| `GET`    | `/modal/deploy/{deploy_id}` | Get deployment status and logs |
| `GET`    | `/modal/deploy` | List all active deployments |
| `DELETE` | `/modal/deploy/{deploy_id}` | Stop/delete a deployment |
| `GET`    | `/modal/account` | Get Modal account info |
| `GET`    | `/modal/models` | List available GPU models for deployment |

**`GET /modal/deploy/check` response:**
```json
{
  "cli_found": true,
  "cli_path": "/usr/local/bin/modal",
  "token_configured": true,
  "token_id_masked": "ak-xxxx...****",
  "ready": true,
  "issues": []
}
```

**`POST /modal/deploy` body:**
```json
{
  "model_id": "meta-llama/Llama-3.1-8B-Instruct",
  "gpu": "A10G",
  "num_gpus": 1,
  "max_model_len": 8192,
  "deployment_name": "my-llm"
}
```
Returns `deploy_id`; logs stream to Redis and are polled by the frontend every 2 seconds.

### UI & Monitoring

| Method | Path | Description |
|---|---|---|
| `GET` | `/dashboard` | Web dashboard (HTML) |
| `GET` | `/dashboard/stats` | Dashboard stats (JSON) |
| `GET` | `/api-docs` | Interactive API documentation (HTML) |
| `GET` | `/settings` | Settings control panel (HTML) |
| `GET` | `/playground` | Chat playground — test any endpoint (HTML) |
| `GET` | `/logs` | Real-time log viewer (HTML) |
| `GET` | `/logs/records` | Fetch log records (filterable, pageable) |
| `GET` | `/logs/loggers` | List all logger names seen in buffer |
| `DELETE` | `/logs/clear` | Clear the in-memory log buffer |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc |

**`GET /logs/records` query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `level` | string | DEBUG | Minimum level: DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `logger_name` | string | — | Filter by logger name prefix (e.g. `app.api`) |
| `since` | float | — | Unix epoch lower bound |
| `until` | float | — | Unix epoch upper bound |
| `q` | string | — | Full-text search in formatted message |
| `tail` | int | 0 | Return only the last N records after filters |
| `limit` | int | 200 | Max records returned (max 5000) |
| `newest_first` | bool | true | Sort order |

### Redis Key Schema

| Key Pattern | Type | Description |
|---|---|---|
| `arbiter:stats:requests_total` | Counter | Global request count |
| `arbiter:stats:provider:{name}:success` | Counter | Per-provider success count |
| `{provider}:{keyhash}:rpm` | Counter (60s TTL) | Per-key RPM usage |
| `{provider}:{keyhash}:tpm` | Counter (60s TTL) | Per-key TPM usage |
| `{provider}:{keyhash}:daily` | Counter (24h TTL) | Per-key daily token usage |
| `{provider}:{keyhash}:failed` | String (5m TTL) | Key on cooldown flag |
| `arbiter:cache:{sha256}` | JSON | Cached response |
| `arbiter:config:provider_order` | JSON | Custom provider order |
| `arbiter:config:models:{provider}` | JSON | Custom model hierarchy |
| `arbiter:runtime:disabled:{provider}` | String | Provider disabled flag |
| `arbiter:cf:workers:registry` | JSON | Cloudflare worker registry (provisioning state + metadata) |
| `arbiter:cf:deleting:{name}` | String (120s TTL) | Deletion marker — suppresses worker from list during CF propagation delay |
| `arbiter:cf:workers` | JSON | Active CF workers with URLs (used for gateway routing) |
| `arbiter:modal:deploy:{id}:logs` | List | Streaming deploy log lines from `modal deploy` subprocess |
| `arbiter:modal:deploy:{id}:status` | JSON | Deployment status: pending/running/failed/complete |
| `arbiter:modal:deployments` | JSON | Active Modal deployments with endpoint URLs (used for gateway routing) |
| `arbiter:modal:token` | String | Cached Modal token (id:secret) — loaded from env or set via UI |

---

## Core Concepts

### 1. BaseProvider (Abstract Base Class)

All vendor adapters inherit from `BaseProvider`:

```python
from app.providers.base import BaseProvider, RateLimitError, ProviderError

class MyProvider(BaseProvider):
    name = "myprovider"
    models = ["model-1", "model-2"]
    max_context_tokens = 32_000
    default_model = "model-1"

    async def complete(
        self,
        request: ChatCompletionRequest,
        api_key: str
    ) -> ChatCompletionResponse:
        """
        Core method: translate OpenAI → provider format, call API, translate back.
        """
        # 1. Validate model
        model = request.model if request.model in self.models else self.default_model

        # 2. Translate request to provider format
        messages = self._translate_to_provider_format(request.messages)

        # 3. Call provider API
        response = await self._call_provider_api(model, messages, api_key)

        # 4. Handle errors
        if response.status_code == 429:
            raise RateLimitError(f"Rate limited: {response.text}")
        if response.status_code != 200:
            raise ProviderError(f"Error {response.status_code}: {response.text}")

        # 5. Translate response back to OpenAI format
        return self._translate_to_openai_format(response.json())
```

**Key Methods:**
- `name` (str) — Provider identifier
- `models` (List[str]) — Supported model IDs
- `max_context_tokens` (int) — Maximum context window
- `default_model` (str) — Fallback model if not specified
- `complete(request, api_key)` (async) — Main implementation
- `estimate_tokens(messages)` (optional) — Override token estimation

**Error Types:**
- `RateLimitError` — 429 errors, quota exceeded
- `ProviderError` — All other errors

### 2. IntelligentRouter

Routes requests across providers, models, and accounts:

```python
class IntelligentRouter:
    def __init__(self, providers, key_pools, cache, redis_client):
        self.providers = providers      # Dict[provider_name, BaseProvider]
        self.key_pools = key_pools      # Dict[provider_name, KeyPool]
        self.cache = cache              # CacheLayer
        self.redis = redis_client       # Redis client

    async def route(self, request: ChatCompletionRequest):
        """
        Main routing entry point.
        Returns ChatCompletionResponse or raises ProviderError.
        """
        # 1. Try cache
        # 2. Get provider order
        # 3. For each provider:
        #    - Get model hierarchy
        #    - For each model:
        #      - Try all keys (by score)
        # 4. Return response or raise error
```

**Key Methods:**
- `route(request)` — Main entry point
- `_provider_order(request)` — Determine provider priority
- `_model_hierarchy(provider, request, token_est)` — Get fallback chain
- `_estimate_tokens(request)` — Word-count heuristic
- `_is_code_related(text)` — Detect code vs. general request

### 3. KeyPool (Weighted Scoring)

Manages multiple API keys with intelligent selection:

```python
class KeyPool:
    def __init__(self, provider, keys, redis_client, rpm_limit, tpm_limit,
                 daily_limit, key_tiers: Optional[Dict[str, str]] = None):
        """
        key_tiers maps {api_key: "free" | "paid"}.  Tags come from `.env`
        via the `KEY#tier` syntax (see config.get_key_tiers).  Default tier
        for any unmapped key is "free".
        """

    async def get_best_key(
        self,
        exclude: Set[str] = None,
        required_tier: Optional[str] = None,
    ) -> Optional[str]:
        """
        Select the key with the highest availability score.

        Score = (rpm_available × 0.30)
              + (tpm_available × 0.20)
              + (daily_available × 0.50)

        Tier filtering (v1.13.3+):
          required_tier=None    → any key is eligible
          required_tier="paid"  → only keys whose tier == "paid" are eligible
                                  (free keys silently skipped → no 429 burn)

        Returns: Raw API key string or None if all exhausted.
        """

    async def record_usage(self, key: str, tokens_used: int):
        """
        Increment sliding-window counters:
        - rpm_used: +1 (60s window)
        - tpm_used: +tokens_used (60s window)
        - daily_used: +tokens_used (86400s window)
        """

    async def mark_failed(self, key: str, cooldown_seconds: int = 300):
        """
        Set failed flag (429 received).
        Key excluded from selection for cooldown period.
        """
```

**Per-key tiers (v1.13.3+):**
The router consults `provider.paid_models` (a `set[str]` declared on the
provider class) to decide whether to pass `required_tier="paid"` to
`get_best_key()`. Today the Gemini provider declares:

```python
paid_models = {
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-2.5-pro",
    "gemini-pro-latest",
}
```

Other providers can opt in by adding the same class attribute.

**Redis Keys:**
```
{provider}:{key_hash}:rpm    → Requests this minute
{provider}:{key_hash}:tpm    → Tokens this minute
{provider}:{key_hash}:daily  → Tokens today
{provider}:{key_hash}:failed → Cooldown flag
```

### 4. CacheLayer (Redis)

Simple deterministic request caching:

```python
class CacheLayer:
    async def get(self, key: str) -> Optional[ChatCompletionResponse]:
        """Retrieve cached response."""

    async def set(self, key: str, response: ChatCompletionResponse, ttl: int):
        """Cache response with TTL."""

    def make_key(self, request: ChatCompletionRequest) -> str:
        """Generate cache key: SHA256(model + json(messages))"""
```

**Cache Keys:**
```
arbiter:cache:{sha256_hash}  → Serialized ChatCompletionResponse (JSON)
```

**Caching Policy:**
- Only cache if `temperature ≤ 0.3` (deterministic)
- TTL: configurable (default 3600s = 1 hour)
- Hash includes: model + messages (not temperature/other params)

---

## How to Add a New Provider

### Step 1: Create Provider Adapter

Create `app/providers/mynewprovider.py`:

```python
"""
MyNewProvider adapter.

Models: model-1, model-2
Context: 32K tokens
Free-tier: 20 RPM, 100K TPM, 1000 daily

Source: https://docs.mynewprovider.com
"""

import logging
import time
import uuid
from typing import List

import httpx

from app.providers.base import BaseProvider, RateLimitError, ProviderError
from app.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)

logger = logging.getLogger(__name__)

MYNEW_API_BASE = "https://api.mynewprovider.com/v1/chat"


class MyNewProvider(BaseProvider):
    name = "mynew"
    models = ["model-1", "model-2"]
    max_context_tokens = 32_000
    default_model = "model-1"

    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """Translate OpenAI → MyNew format, call API, translate back."""

        model = request.model if request.model in self.models else self.default_model

        # Translate to provider format
        messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]

        payload = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "top_p": request.top_p,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(f"MyNewProvider POST model={model}")

        # Call API
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(MYNEW_API_BASE, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"MyNew network error: {exc}") from exc

        # Handle rate limits
        if resp.status_code == 429:
            raise RateLimitError(f"MyNew 429: {resp.text[:300]}")

        # Handle other errors
        if resp.status_code != 200:
            raise ProviderError(f"MyNew {resp.status_code}: {resp.text[:500]}")

        # Parse response
        data = resp.json()

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish = choice.get("finish_reason", "stop")
            usage = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"MyNew response parse error: {exc}") from exc

        # Translate to OpenAI format
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content=text),
                    finish_reason=finish,
                )
            ],
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
        )
```

### Step 2: Register in Main App

Edit `app/main.py`:

```python
from app.providers.mynewprovider import MyNewProvider

# In lifespan() function, update provider_classes:
provider_classes = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
    "cohere": CohereProvider,
    "mynew": MyNewProvider,  # ← Add here
}
```

### Step 3: Add to Router Hierarchy

Edit `app/routing/router.py`:

```python
VENDOR_MODEL_HIERARCHY = {
    "gemini": [...],
    "groq": [...],
    "openrouter": [...],
    "cohere": [...],
    "mynew": [  # ← Add here
        ("model-1", 32_000),
        ("model-2", 16_000),
    ],
}
```

### Step 4: Set Rate Limits

Edit `app/key_management/key_pool.py`:

```python
PROVIDER_LIMITS = {
    "gemini": {...},
    "groq": {...},
    "openrouter": {...},
    "cohere": {...},
    "mynew": {  # ← Add here
        "rpm": 20,
        "tpm": 100_000,
        "daily": 1_000,
    },
}
```

### Step 5: Update Router Keywords

Edit `app/routing/router.py`, in `_provider_order()`:

```python
if any(k in model for k in ("mynew", "specific-model-name")):
    return self._reorder("mynew")
```

### Step 6: Add to Dashboard (Optional)

The dashboard auto-discovers providers from `key_pools`, so no code changes needed.

### Step 7: Update Documentation

- Add provider to README.md
- Add models to CHANGELOG.md
- Update rate limits table in USERGUIDE.md

---

## How to Extend the Router

### Change Routing Strategy

Edit `_provider_order()` in `app/routing/router.py`:

```python
def _provider_order(self, request: ChatCompletionRequest) -> List[str]:
    """
    Current logic:
    1. Explicit model name → use that provider
    2. Token count → large = Gemini, small = Groq
    3. Capability → code = Pro models
    4. Default → Gemini → Groq → OpenRouter → Cohere

    To customize:
    - Check request.model
    - Check request.messages (for content analysis)
    - Return ordered list of provider names
    """

    # Example: Always try Groq first
    return ["groq", "gemini", "openrouter", "cohere"]

    # Example: Alternate by time
    import time
    if time.time() % 2 == 0:
        return ["gemini", "groq", "openrouter", "cohere"]
    else:
        return ["groq", "gemini", "openrouter", "cohere"]
```

### Change Model Hierarchy

> **v1.12+**: hierarchies are derived from a single catalog at
> [`app/providers/_free_tier_catalog.py`](app/providers/_free_tier_catalog.py).
> The `VENDOR_MODEL_HIERARCHY` dict in `router.py` is now generated from it
> by `vendor_model_hierarchy(include_paid=True)` — *do not edit the dict
> directly*; edit the catalog instead.

```python
# app/providers/_free_tier_catalog.py
"groq": [
    ModelSpec(
        id="qwen/qwen3-32b", context=131_072,
        tags={"balanced", "code", "reasoning"},
        rpm=60, rpd=1_000, quality=4, speed=4,
        notes="60 RPM (highest)",
    ),
    # … add a new entry, reorder, or change tags here
],
```

The auto-router (see [`app/routing/auto_router.py`](app/routing/auto_router.py))
uses the `tags`, `quality`, `speed`, `context`, and `modality` fields to
score candidates per intent.  The `intent_classifier` module categorises
the prompt into one of: `code`, `reasoning`, `long-context`, `vision`,
`creative`, `fast`, `balanced`.

To extend the classifier with a new keyword, edit
[`app/routing/intent_classifier.py`](app/routing/intent_classifier.py) and
add to the appropriate `*_KEYWORDS` set; the regex is rebuilt at module
import.

#### Legacy direct edit (still supported for ad-hoc overrides)

```python
VENDOR_MODEL_HIERARCHY = {
    "gemini": [
        ("gemini-2.5-flash-lite", 1_048_576),  # ← Reorder or add
        ("gemini-2.5-flash", 1_048_576),
        ("gemini-2.5-pro", 1_048_576),
    ],
}
```

### Change Scoring Algorithm

Edit `_score_key()` in `app/key_management/key_pool.py`:

```python
async def _score_key(self, key: str) -> float:
    """
    Current: composite score with daily=0.50, rpm=0.30, tpm=0.20

    To change:
    - Modify weights: _W_DAILY, _W_RPM, _W_TPM
    - Add new factors (e.g., provider latency, success rate)
    - Return value in [-1, 1] where 1 = best
    """

    # Example: Weight daily 70% instead of 50%
    _W_DAILY = 0.70
    _W_RPM = 0.20
    _W_TPM = 0.10
```

### Add Custom Routing Logic

In `router.py`, add new methods:

```python
def _get_user_timezone(self, request: ChatCompletionRequest) -> str:
    """Extract timezone from message metadata."""
    # Custom header or message parsing
    return "UTC"

def _select_by_time_of_day(self, tz: str) -> str:
    """Route differently by time of day."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)

    if now.hour < 12:
        return "gemini"  # Morning: use Gemini (careful quota)
    else:
        return "groq"    # Afternoon: use fast Groq
```

---

## Authentication & Security

### Gateway-Level Authentication

Enable optional Bearer token validation on all endpoints:

```python
# In app/middleware/auth.py
class GatewayAuthMiddleware:
    """Validates Authorization: Bearer <key> header."""

    # Exempt paths (no auth required):
    EXEMPT_PATHS = {
        "/health",
        "/docs",
        "/api-docs",
        "/dashboard",
        "/openapi.json",
    }

    # Configuration from .env:
    GATEWAY_API_KEYS = ["key1", "key2", "key3"]  # GATEWAY_API_KEYS (preferred)
    GATEWAY_API_KEY = "single-key"                # GATEWAY_API_KEY (legacy)
```

**Usage:**

```bash
# Enable in .env
GATEWAY_API_KEYS=key1,key2,key3

# Request with auth
curl -H "Authorization: Bearer key1" \
  http://localhost:8000/v1/chat/completions
```

### Cloudflare Access Integration

Enterprise-grade authentication via Cloudflare Zero Trust:

```python
# In app/middleware/auth.py
class CloudflareAccessMiddleware:
    """Validates Cf-Access-Jwt-Assertion header (Cloudflare Access)."""

    # Configuration:
    ENABLE_CF_ACCESS = True
    CLOUDFLARE_ACCESS_TEAM_NAME = "myteam"  # Team domain
    CLOUDFLARE_ACCESS_AUD = "aud-tag"       # Application AUD from Access settings

    # JWKS caching (1 hour TTL):
    # Fetches public keys from:
    # https://{team}.cloudflareaccess.com/cdn-cgi/access/certs
```

**Usage:**

```bash
# Enable in .env
ENABLE_CF_ACCESS=true
CLOUDFLARE_ACCESS_TEAM_NAME=myteam
CLOUDFLARE_ACCESS_AUD=your-aud-tag-here

# Requests via Cloudflare Access tunnel automatically include JWT header
# Tunnel config: https://developers.cloudflare.com/cloudflare-one/
```

---

## Rate Limiting Internals

### Sliding Window Implementation

```python
# RPM tracking (60-second window)
key_rpm_key = f"{provider}:{key_hash}:rpm"

# When usage recorded:
await redis.incr(key_rpm_key)       # Increment counter
await redis.expire(key_rpm_key, 60) # Reset in 60 seconds

# Check if at limit:
rpm_used = int(await redis.get(key_rpm_key) or 0)
if rpm_used >= rpm_limit:
    # Hit limit
```

### Per-Model Rate Limits (Future Enhancement)

Currently tracked per-key globally. To track per-model-per-key:

```python
# Instead of: {provider}:{key_hash}:rpm
# Use:        {provider}:{key_hash}:{model}:rpm

# This would require:
# 1. Pass model to record_usage()
# 2. Per-model PROVIDER_LIMITS
# 3. Check limits by model in get_best_key()
```

### Handling Quota Exhaustion

```python
# Daily quota exhaustion (hard limit)
if daily_used >= daily_limit:
    score = -1.0  # Exclude key entirely
    # Key will be retried tomorrow (midnight)

# RPM quota exhaustion (soft limit)
if rpm_used >= rpm_limit:
    score = rpm_avail * weight  # score = 0
    # Key is deprioritized but can still be selected
    # Request may experience delay within same minute
```

---

## Caching Internals

### Cache Key Generation

```python
import hashlib
import json

def make_key(self, request: ChatCompletionRequest) -> str:
    """
    Generate cache key from model + messages.

    SHA256(json.dumps({
        "model": request.model,
        "messages": serialized(request.messages),
    }))
    """
    data = {
        "model": request.model,
        "messages": [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]
    }
    json_str = json.dumps(data, sort_keys=True)
    sha256_hash = hashlib.sha256(json_str.encode()).hexdigest()
    return f"arbiter:cache:{sha256_hash}"
```

### Cache Serialization

```python
# Store: ChatCompletionResponse → JSON
cached_json = response.model_dump_json()
await redis.set(cache_key, cached_json, ex=ttl)

# Retrieve: JSON → ChatCompletionResponse
cached_json = await redis.get(cache_key)
response = ChatCompletionResponse.model_validate_json(cached_json)
```

### Cache Invalidation Strategy

Current: **Time-based TTL only** (no invalidation by model/provider change)

To add manual invalidation:

```python
# On provider/model change:
await redis.delete(f"arbiter:cache:*")  # Clear all

# Or selective:
for key in await redis.scan_iter("arbiter:cache:*gemini*"):
    await redis.delete(key)
```

---

## Cloudflare Workers AI Manager API

Manage Cloudflare Workers AI instances directly from the gateway.

### Endpoints

**List Available Models**
```
GET /cloudflare/models
Authorization: Bearer {api_key}

Response:
{
  "data": [
    {
      "name": "@cf/meta/llama-4-scout-17b-16e-instruct",
      "description": "Llama 4 Scout 17B"
    },
    ...
  ]
}
```

**Create a Worker**
```
POST /cloudflare/workers
Authorization: Bearer {api_key}

Request:
{
  "name": "my-worker",
  "model": "@cf/meta/llama-3.1-8b-instruct",
  "public": true
}

Response:
{
  "id": "worker-id",
  "name": "my-worker",
  "url": "https://my-worker.{account}.workers.dev"
}
```

**List Deployed Workers**
```
GET /cloudflare/workers
Authorization: Bearer {api_key}

Response:
{
  "data": [
    {
      "id": "worker-id-1",
      "name": "my-worker",
      "created_on": "2026-03-28T10:00:00Z"
    }
  ]
}
```

**Delete a Worker**
```
DELETE /cloudflare/workers/{worker_id}
Authorization: Bearer {api_key}

Response: 204 No Content
```

### Implementation Details

Located in `app/api/cloudflare_manager.py`:

```python
from app.api.cloudflare_manager import router as cf_router

# In app.main (router registration):
app.include_router(cf_router, prefix="/cloudflare", tags=["cloudflare"])
```

**Configuration:**

```python
# In .env
CLOUDFLARE_API_KEYS=account123|token-here
CLOUDFLARE_ACCOUNT_ID=account123  # Optional, for manager operations
```

---

## Development Setup

### Prerequisites

- Python 3.10+
- Redis (optional for dev)
- API keys (at least one provider)

### Local Environment

```bash
# Clone
git clone <repo>
cd arbiter

# Virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install (dev mode)
pip install -e ".[dev]"
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-cov black mypy

# Setup
cp .env.example .env
# Edit .env with your API keys
```

### Running Locally

```bash
# Start Redis (optional)
docker run -d -p 6379:6379 redis:7-alpine

# Or use built-in in-memory fallback (no Redis)
export REDIS_URL=redis://localhost:6379  # Even if offline, gateway will use fallback

# Start gateway
uvicorn app.main:app --reload --port 8000

# Test
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
```

### Code Style

```bash
# Format
black app/ tests/

# Lint
mypy app/ --ignore-missing-imports

# Type checking
mypy app/
```

### Debugging

```python
# In any module:
import logging
logger = logging.getLogger(__name__)

# Use logger.debug, logger.info, logger.warning, logger.error

# Set LOG_LEVEL=DEBUG in .env for verbose output
```

---

## Testing

### Test Structure (Recommended)

```
tests/
├── __init__.py
├── conftest.py                 # Fixtures
├── unit/
│   ├── test_key_pool.py       # KeyPool scoring
│   ├── test_router.py         # Routing logic
│   ├── test_cache.py          # Cache layer
│   └── test_providers.py       # Provider adapters
├── integration/
│   ├── test_gemini_api.py     # Real API calls
│   └── test_chat_endpoint.py  # Full flow
└── fixtures/
    └── mock_responses.py       # Mock API responses
```

### Running Tests

```bash
# All tests
pytest tests/ -v

# Coverage
pytest tests/ --cov=app --cov-report=html
open htmlcov/index.html

# Specific test
pytest tests/unit/test_key_pool.py::test_weighted_scoring -v

# Watch mode
pytest-watch tests/ -c
```

### Mock Example

```python
# tests/unit/test_router.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_router_selects_best_key():
    """Test that router picks key with highest score."""

    # Mock KeyPool
    key_pool_mock = AsyncMock()
    key_pool_mock.get_best_key.return_value = "key-with-highest-score"

    # Create router with mocked pools
    router = IntelligentRouter(
        providers={"gemini": GeminiProvider()},
        key_pools={"gemini": key_pool_mock},
        cache=CacheLayerMock(),
        redis_client=None,
    )

    # Make request
    request = ChatCompletionRequest(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "Hello"}],
    )

    # Mock provider response
    with patch.object(GeminiProvider, "complete") as mock_complete:
        mock_complete.return_value = ChatCompletionResponse(...)

        result = await router.route(request)

        # Assertions
        assert result.choices[0].message.content is not None
        key_pool_mock.get_best_key.assert_called_once()
```

---

## Deployment Architecture

### Single-Instance (Development)

```
Your Machine
├── Python 3.12
├── FastAPI (uvicorn) :8000
├── Redis :6379 (optional)
└── .env file
```

### Docker Compose (Small Production)

```
Host Machine
├── Docker
│   ├── gateway:latest
│   │   ├── FastAPI :8000
│   │   └── App code
│   └── redis:7-alpine
│       └── Redis :6379
├── .env file
└── redis_data volume (persistence)
```

### Kubernetes (Enterprise)

```
K8s Cluster
├── arbiter Deployment (replicas: 2+)
│   ├── FastAPI app
│   ├── Liveness probe: /health
│   ├── Readiness probe: /health
│   └── Requests: 250m CPU, 256Mi RAM
│
├── redis StatefulSet (1 replica)
│   ├── Redis with RDB + AOF
│   ├── PersistentVolume: 10Gi
│   └── Service: ClusterIP
│
├── ConfigMap
│   ├── REDIS_URL=redis:6379
│   ├── LOG_LEVEL=INFO
│   └── CACHE_TTL=3600
│
└── Secrets
    ├── GEMINI_API_KEYS
    ├── GROQ_API_KEYS
    ├── OPENROUTER_API_KEYS
    └── COHERE_API_KEYS
```

### Load Balancer Configuration

```nginx
# Nginx example
upstream llm_gateway {
    server gateway-1:8000;
    server gateway-2:8000;
    server gateway-3:8000;
}

server {
    listen 80;
    server_name llm-api.yourdomain.com;

    location / {
        proxy_pass http://llm_gateway;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header X-Forwarded-For $remote_addr;

        # Timeouts for LLM API calls
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }

    location /health {
        access_log off;
        proxy_pass http://llm_gateway;
    }
}
```

### Monitoring & Logging

```yaml
# Prometheus metrics (future v1.1+)
/metrics endpoint with:
- requests_total (counter)
- request_duration_seconds (histogram)
- cache_hits_total
- key_score (gauge per key)

# Logging to file
/var/log/arbiter/app.log
- JSON format for parsing
- Rotation: daily, 30-day retention
```

---

## Performance Optimization Tips

### 1. Cache Aggressive ly

```python
# Always set temperature low for repeatable requests
{"temperature": 0.0}  # Guarantee cache hit
{"temperature": 0.1}  # ~90% cache hit rate
```

### 2. Use Small Models for Simple Tasks

```python
# Code analysis: Use fast 8B instead of 70B
{"model": "llama-3.1-8b-instant"}  # 10× faster, 30% quota cost

# Complex reasoning: Use 70B
{"model": "llama-3.3-70b-versatile"}  # Slower but smarter
```

### 3. Batch Requests

```python
# Instead of 100 sequential requests:
# Use asyncio to make 10 parallel batches

import asyncio

requests = [...]  # 100 ChatCompletionRequest objects

# Sequential: 100 × 0.5s = 50s
results = [await call_api(r) for r in requests]

# Parallel (batch=10): 10 × 0.5s = 5s
batches = [requests[i:i+10] for i in range(0, 100, 10)]
results = [
    await asyncio.gather(*[call_api(r) for r in batch])
    for batch in batches
]
```

### 4. Monitor Key Pool Health

```bash
# Check which keys are doing heavy lifting
redis-cli
> HGETALL arbiter:key_stats:gemini:a1b2c3d4
```

### 5. Pre-Warm Cache

```python
# Load frequently-asked questions into cache
# before peak traffic

common_questions = [
    "What is 2+2?",
    "How do I read a file?",
    ...
]

for q in common_questions:
    request = ChatCompletionRequest(
        model="gemini-2.5-flash-lite",
        messages=[{"role": "user", "content": q}],
        temperature=0.0,
    )
    await router.route(request)
```

---

## Common Pitfalls

### ❌ Hardcoding API Keys

```python
# Bad
API_KEY = "sk-..."

# Good
from app.config import settings
api_key = settings.get_keys("gemini")[0]
```

### ❌ Not Handling Async Errors

```python
# Bad
response = await provider.complete(request, key)

# Good
try:
    response = await provider.complete(request, key)
except RateLimitError:
    # Handle and retry
    pass
except ProviderError:
    # Handle and continue to next provider
    pass
```

### ❌ Blocking Calls in Async Code

```python
# Bad
import time
await redis.get(key)
time.sleep(1)  # Blocks entire event loop!

# Good
import asyncio
await redis.get(key)
await asyncio.sleep(1)
```

### ❌ Not Respecting Model Context Windows

```python
# Bad
request.max_tokens = 100_000  # Exceeds model's context!

# Good
request.max_tokens = min(100_000, provider.max_context_tokens)
```

---

## Troubleshooting Development

### "Module not found"

```bash
# Ensure app/ is in Python path
export PYTHONPATH="${PYTHONPATH}:/path/to/arbiter"

# Or install in editable mode
pip install -e .
```

### "Redis connection refused"

```bash
# Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Or disable Redis requirement by not setting REDIS_URL
# Gateway will auto-fallback to in-memory
```

### "API key invalid"

```bash
# Test key directly with provider
curl "https://api.provider.com/v1/models" \
  -H "Authorization: Bearer YOUR_KEY"

# If fails, key is invalid or expired
```

### "Type errors with mypy"

```bash
# Add to pyrightconfig.json
{
  "typeCheckingMode": "basic",
  "pythonVersion": "3.10"
}

# Run
mypy app/ --ignore-missing-imports
```

---

## Next Steps

- Read [README.md](README.md) for user overview
- Read [USERGUIDE.md](USERGUIDE.md) for API usage
- Contribute a new provider!
- Submit issues/PRs

---

**Made with ❤️ for extensible, self-hosted AI infrastructure.**
