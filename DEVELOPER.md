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

## v1.20 -- Real-Time Web Search Pipeline

When a request opts in (X-Arbiter-Realtime: true or metadata.realtime), the chat endpoint pulls live web context BEFORE routing to an LLM:

```
Client request (X-Arbiter-Realtime: true)
       |
       v
+-----------------------------------+
| app/api/chat.py                   |
|   1. Extract last user message    |
|   2. TavilyClient.search(query)   |
|      (Redis-cached 5 min)         |
|   3. Render context block w/ [n]  |
|      numbered citations           |
|   4. Prepend as system message    |
+-----------------------------------+
       |
       v
+-----------------------------------+
| Existing complexity-aware router  |
|   - LLM sees the search context   |
|     in the system prompt          |
|   - Answers grounded, cites [1]+  |
+-----------------------------------+
       |
       v
Response w/ X-Arbiter-Realtime-Sources
```

Files: app/services/web_search.py (Tavily client), app/api/chat.py (auto-inject), app/providers/gemini.py (native google_search tool forwarding).

Env: TAVILY_API_KEY (free tier 1K searches/mo).

---

## v1.20 -- Multi-Key Rotation Internals (KeyPool)

Per-key counters now use anchored-TTL semantics so a continuously-used key does not get its window TTL refreshed on every call.

- RPM/TPM: SET key 0 EX 60 NX; INCRBY key by  -- the TTL is set once on first INCR of the window.
- Daily: key naming is {provider}:{key_hash}:daily:YYYY-MM-DD with a 30 h TTL safety margin so it aligns with UTC midnight rather than 24 h since last call.
- Per-model overrides: MODEL_OVERRIDES dict + get_model_limits() helper. KeyPool.get_best_key(model=...) consults per-model limits in addition to the provider aggregate, so flash-lite (1000 RPD) and pro (100 RPD) on the same key do not bottleneck each other.
- RateLimitError.retry_after: parsed from upstream 429 headers / body via parse_retry_after() in base.py, used by router as the mark_failed cooldown so a 5 s rate limit costs 7 s not 5 minutes.

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
│  - SHA256(model+messages+max_tokens+    │
│    stop+top_p) · only if temp ≤ 0.3    │
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
│   │   ├── huggingface.py            # HuggingFace (6 :fastest models)
│   │   ├── pollinations.py           # Pollinations.ai (3 free models)
│   │   ├── zai_provider.py           # Z.ai / Zhipu AI (3 free models)
│   │   ├── nvidia_provider.py        # NVIDIA NIM (5+ models, build.nvidia.com)
│   │   ├── ollama_provider.py        # Ollama (self-hosted)
│   │   ├── routeway.py               # Routeway unified gateway
│   │   └── generic_openai.py         # User-added custom OpenAI-compatible providers
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
│   │   └── auth.py                   # Gateway auth, per-token rate limiter, Cloudflare Access JWT
│   │
│   ├── observability/               # Persistent logging (v1.18.0)
│   │   ├── __init__.py
│   │   ├── persistent_log.py         # 180-day JSONL log store (api/activity/errors) + janitor
│   │   └── stats.py                  # Redis-backed real-time counters (sorted-set error log)
│   │
│   ├── services/                    # Background services
│   │   ├── announcements.py          # Dashboard banner service (v1.18.0)
│   │   ├── daily_report.py           # Daily + weekly email report with AI analysis
│   │   └── model_health.py           # Weekly model probe scheduler
│   │
│   └── api/                          # FastAPI routers
│       ├── __init__.py
│       ├── chat.py                   # POST /v1/chat/completions (+ persistent log instrumentation)
│       ├── models_api.py             # GET /v1/models
│       ├── dashboard.py              # Dashboard & stats HTML endpoints
│       ├── settings_api.py           # GET/POST/DELETE /settings/routing, /settings/cache
│       ├── keys_api.py               # GET/POST/DELETE /api/providers/* (runtime key mgmt)
│       ├── image_api.py              # POST /v1/images/generations (Pollinations)
│       ├── cloudflare_manager.py     # Workers AI management + validate endpoint
│       ├── gateway_tokens_api.py     # Gateway token CRUD
│       ├── announcements_api.py      # Dashboard banner API (v1.18.0)
│       ├── persistent_logs_api.py    # Persistent log query API (v1.18.0)
│       ├── analytics_api.py          # Analytics data endpoints
│       ├── backup_api.py             # Backup & restore
│       ├── users_api.py              # User management + SSO approval
│       ├── preferences_api.py        # Auto-route preferences
│       └── custom_providers_api.py   # User-added custom providers
│
├── static/                            # Static assets (served at /static/)
│   ├── arbiter.css                   # Shared design system (light/dark theme, components)
│   ├── arbiter.js                    # Shared JS (theme toggle, sidebar, toast, helpers)
│   ├── dashboard.html                # Web UI dashboard (/dashboard)
│   ├── developer.html               # Developer documentation (/developer)
│   ├── backup.html                  # Backup & restore UI (/backup)
│   ├── users.html                   # Users & access management (/users)
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
| `GET`    | `/api/providers` | List all providers with status, masked keys, pool stats *(admin only)* |
| `POST`   | `/api/providers/{name}/keys` | Add a key `{"key": "..."}` — writes to `.env`, hot-reloads provider |
| `DELETE` | `/api/providers/{name}/keys/{hash}` | Remove a key by MD5 hash — removes from `.env` |
| `POST`   | `/api/providers/{name}/enable` | Enable a disabled provider (clears Redis disabled flag) |
| `POST`   | `/api/providers/{name}/disable` | Disable a provider (sets Redis disabled flag, removes from routing) |
| `POST`   | `/api/providers/{name}/test` | Probe connectivity, returns latency + sample reply |
| `POST`   | `/api/providers/reload` | Hot-reload all providers from current `.env` |

### Settings (Routing & Cache)

| Method | Path | Description |
|---|---|---|
| `GET`    | `/settings/routing` | Current routing config (provider order + model overrides) *(admin only)* |
| `POST`   | `/settings/routing` | Update `{"provider_order": [...], "model_overrides": {...}}` |
| `DELETE` | `/settings/routing` | Reset to built-in defaults |
| `GET`    | `/settings/cache` | Cache config (TTL, key prefix, threshold) + live stats (hits/misses/hit-rate/entries). Powers the Cache tab. *(admin only)* |
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

### UI & Monitoring

| Method | Path | Description |
|---|---|---|
| `GET` | `/dashboard` | Web dashboard (HTML) |
| `GET` | `/dashboard/stats` | Dashboard stats (JSON) |
| `GET` | `/developer` | Developer documentation (HTML) |
| `GET` | `/settings` | Settings control panel (HTML) |
| `GET` | `/playground` | Chat playground — test any endpoint (HTML) |
| `GET` | `/logs` | Real-time log viewer (HTML) |
| `GET` | `/logs/records` | Fetch log records (filterable, pageable) |
| `GET` | `/logs/loggers` | List all logger names seen in buffer |
| `DELETE` | `/logs/clear` | Clear the in-memory log buffer |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc |

### Announcements / Banners (v1.18.0+)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/announcements/active` | Active dashboard banners (authenticated) |
| `POST` | `/api/announcements` | Publish major-change banner (admin) |
| `DELETE` | `/api/announcements/{id}` | Retract a banner (admin) |

### Persistent Logs (v1.18.0+, admin)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/logs/persistent/summary` | Aggregated stats for N-day window |
| `GET` | `/api/logs/persistent/api` | Paginated API request records |
| `GET` | `/api/logs/persistent/activity` | Paginated admin activity audit records |
| `GET` | `/api/logs/persistent/errors` | Paginated error records |
| `POST` | `/api/logs/persistent/prune` | Force 180-day retention prune |

**`GET /api/logs/persistent/*` query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `days` | int | 7 | History window in days (1–180) |
| `limit` | int | 100 | Max records to return (1–1000) |
| `offset` | int | 0 | Pagination offset |

### Gateway Tokens (admin)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/gateway/tokens` | List all gateway tokens |
| `POST` | `/api/gateway/tokens` | Create a token |
| `PATCH` | `/api/gateway/tokens/{id}` | Update (policy, rate limit, labels) |
| `DELETE` | `/api/gateway/tokens/{id}` | Delete a token |
| `POST` | `/api/gateway/tokens/{id}/regenerate` | Rotate token secret |
| `GET` | `/api/gateway/tokens/{id}/stats` | Usage statistics |

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
| `arbiter:health:model:{provider}:{model}` | JSON | Weekly model-health probe result `{status, last_checked, error, latency_ms}` (14-day TTL) |
| `arbiter:health:model:last_run` | String | ISO timestamp of last weekly health check |
| `arbiter:health:model:last_summary` | JSON | Summary of last run `{providers, models, ok, fail, elapsed_s}` |

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
        """Generate cache key: SHA256(model + messages + max_tokens + stop + top_p)"""
```

**Cache Keys:**
```
arbiter:cache:{sha256_hash}  → Serialized ChatCompletionResponse (JSON)
```

**Caching Policy:**
- Only cache if `temperature ≤ 0.3` (deterministic)
- TTL: configurable (default 3600s = 1 hour)
- Hash includes: `model`, `messages`, `max_tokens`, `stop`, `top_p` — requests with different output constraints get distinct cache entries

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

### Complexity-Aware Routing (v1.19.0)

The routing pipeline now includes a **complexity analyzer** that scores every
incoming request before model selection:

```
Request → complexity_analyzer.analyze_complexity(request)
        → Complexity enum (TRIVIAL=1, SIMPLE=2, MODERATE=3, COMPLEX=4, EXPERT=5)
        → auto_router adjusts quality/speed weight balance per tier
        → Smart Model Upgrade intercepts weak model + complex request
```

**Key files:**
- [`app/routing/complexity_analyzer.py`](app/routing/complexity_analyzer.py) — 13-factor scoring
- [`app/routing/auto_router.py`](app/routing/auto_router.py) — complexity-aware candidate scoring
- [`app/routing/router.py`](app/routing/router.py) — Smart Model Upgrade in `_build_candidate_chain()`

**Scoring factors** (in `complexity_analyzer.py`):
1. Message length (char count)
2. Conversation depth (multi-turn)
3. System prompt sophistication
4. Task complexity markers (keywords)
5. Expert-level indicators
6. Code complexity signals
7. Multi-component signals
8. Reasoning depth markers
9. Quality/precision signals
10. Code blocks present
11. List item count
12. Intent classification boost
13. Numbered requirements

**Thresholds:** ≤1.5=TRIVIAL, ≤3.5=SIMPLE, ≤7=MODERATE, ≤12=COMPLEX, >12=EXPERT

**Auto-router weight balance** per complexity tier:

| Tier | quality_weight | speed_weight |
|------|---------------|-------------|
| TRIVIAL | 8 | 30 |
| SIMPLE | 15 | 22 |
| MODERATE | 25 | 15 |
| COMPLEX | 38 | 8 |
| EXPERT | 45 | 5 |

**Smart Model Upgrade:** When a client explicitly requests a model with quality≤2
AND the request complexity is ≥MODERATE, the router silently builds an auto-quality
candidate chain instead (appending the original model as a fallback).

**Provider Diversity:** `_ensure_provider_diversity()` guarantees 4+ unique providers
appear in the top-8 candidates by interleaving under-represented providers.

### Tool-Call Aware Routing (v1.19.1)

When a request includes `tools` or `functions` fields (OpenAI function-calling),
the router automatically filters candidates to **tool-capable providers only**:

```
Tool-capable providers: groq, nvidia, openrouter, cerebras, ollama
Non-tool providers:     cloudflare, gemini*, huggingface, pollinations, cohere, zai, routeway
```

\* Gemini supports tools via its native API but requires format conversion not yet implemented.

**Behaviour:**
- If tool-capable candidates exist → use only those (log: "Tool-call request: filtered to N...")
- If no tool-capable candidates → proceed with all (log warning, tools may be ignored)
- Vendor-pinned requests (`?vendor=...`) bypass this filter (user explicitly chose)

**Tool forwarding** is implemented in providers that pass `tools`, `tool_choice`,
`parallel_tool_calls`, and `response_format` to the upstream LLM API:
- `groq_provider.py` — full tool support (messages include tool_calls, tool_call_id)
- `nvidia_provider.py` — full tool support
- `openrouter.py` — forwards tool fields to upstream model
- `cerebras.py` — forwards tool fields to upstream model
- `ollama_provider.py` — forwards tool fields to Ollama Cloud API

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
        "/developer",
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
    Generate cache key from model + messages + output-shaping params.

    SHA256(json.dumps({
        "model": request.model,
        "messages": serialized(request.messages),
        "max_tokens": request.max_tokens,
        "stop": request.stop,
        "top_p": request.top_p,
    }, sort_keys=True))
    """
    data = {
        "model": request.model,
        "messages": [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ],
        "max_tokens": request.max_tokens,
        "stop": request.stop,
        "top_p": request.top_p,
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

## Observability & Stats (v1.14.2)

### Redis Key Schema

```
arbiter:stats:total:requests         ← lifetime counter (no TTL)
arbiter:stats:total:success          ← lifetime counter (no TTL)
arbiter:stats:total:cache_hits       ← lifetime counter (no TTL)
arbiter:stats:total:tokens           ← lifetime counter (no TTL)

arbiter:stats:history:{bucket}:requests   ← 5-min bucket (TTL: 7 days)
arbiter:stats:history:{bucket}:success    ← 5-min bucket (TTL: 7 days)
arbiter:stats:history:{bucket}:cache_hits ← 5-min bucket (TTL: 7 days)
arbiter:stats:history:{bucket}:tokens     ← 5-min bucket (TTL: 7 days)

arbiter:stats:hourly:{hour}:requests      ← hourly rollup (TTL: 30 days)
arbiter:stats:hourly:{hour}:success       ← hourly rollup (TTL: 30 days)
arbiter:stats:hourly:{hour}:cache_hits    ← hourly rollup (TTL: 30 days)
arbiter:stats:hourly:{hour}:tokens        ← hourly rollup (TTL: 30 days)

arbiter:stats:day:{YYYY-MM-DD}:requests   ← daily rollup (TTL: 90 days)
```

- `{bucket}` = `int(time.time()) // 300 * 300` (5-min aligned epoch)
- `{hour}` = `int(time.time()) // 3600 * 3600` (hour-aligned epoch)
- All TTLs are set via `volatile-lru` eviction policy (only keys with TTL are evicted)

### Analytics Window Parameter

`GET /analytics/data?window=<value>` supports: `1h`, `4h`, `24h`, `7d`, `30d`, `90d`

| Window  | Source         | Granularity |
|---------|----------------|-------------|
| 1h, 4h  | history:* keys | 5 minutes   |
| 24h, 7d | hourly:* keys  | 1 hour      |
| 30d, 90d| day:* keys     | 1 day       |

### Experience-Based Routing

The router maintains a 5-minute performance cache (`_perf_cache`) that tracks:
- Error rate per model (from `arbiter:perf:{provider}:{model}`)
- Average latency per model

Candidates are reordered within each provider by: `error_rate * 10 + latency_seconds`.

---

## Backup System (v1.14.2)

### Architecture

- **Storage**: OCI Object Storage (S3-compatible) via `boto3`
- **Schedule**: Daily incremental (02:00 UTC), Weekly full (Sunday 01:00 UTC)
- **Retention**: Incremental >7 days auto-deleted, Full >90 days auto-deleted
- **Quota**: 10 GB max (configurable via `BACKUP_MAX_GB`)
- **Locking**: Redis-based distributed lock (30-min TTL) prevents overlapping jobs

### S3 Client Configuration (OCI-specific)

```python
Config(
    signature_version="s3v4",
    s3={"addressing_style": "path", "payload_signing_enabled": False},
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
)
```

> **Important**: OCI Object Storage does not support `aws-chunked` transfer encoding.
> The `request_checksum_calculation="when_required"` setting is critical — without it,
> boto3 >= 1.26 uses chunked encoding which OCI rejects with `MissingContentLength`.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/backup/status` | Current backup state, storage usage |
| GET | `/api/backup/list` | List all backups from manifest |
| POST | `/api/backup/run` | Trigger manual backup `{"type":"full\|incremental"}` |
| GET | `/api/backup/{key}/download` | Download backup archive |
| POST | `/api/backup/{key}/restore` | Restore Redis state from backup |
| DELETE | `/api/backup/{key}` | Delete a single backup |
| GET | `/api/backup/storage` | Detailed storage breakdown |

### Environment Variables

```env
BACKUP_ENABLED=true
BACKUP_S3_ENDPOINT=https://namespace.compat.objectstorage.region.oraclecloud.com
BACKUP_S3_BUCKET=app-backups
BACKUP_S3_PREFIX=arbiter/backups
BACKUP_S3_ACCESS_KEY=...
BACKUP_S3_SECRET_KEY=...
BACKUP_S3_REGION=us-chicago-1
BACKUP_MAX_GB=10
```

### Object Layout

```
arbiter/backups/
  manifest.json
  full/2026/05/arbiter-full-20260501T010000Z.tar.gz
  incremental/2026/05/arbiter-incr-20260501T020000Z.tar.gz
```

---

## Observability & Persistent Logging (v1.18.0)

### Architecture

```
Request Flow (additions in v1.18.0 shown with ★)
    │
    ▼
┌─ GatewayAuthMiddleware ─────────────────┐
│  ★ Per-token rate limit check           │
│    (sliding-minute window in Redis)     │
│    429 with Retry-After if exceeded     │
└─────────────────────────────────────────┘
    │
    ▼
┌─ /v1/chat/completions ──────────────────┐
│  ★ _req_start = time.monotonic()        │
│  ★ _req_ip = client IP                  │
│  ... (routing, provider call) ...       │
│  ★ log_api_call() → JSONL file          │
└─────────────────────────────────────────┘

★ Admin mutation endpoints (keys, tokens, settings, workers, announcements):
    → log_activity() → HMAC-tagged JSONL record
```

### File system layout

```
/app/data/logs/
├── api/
│   ├── 2026-05-12.jsonl       # gateway request records
│   └── 2026-05-13.jsonl
├── activity/
│   ├── 2026-05-12.jsonl       # admin mutations (HMAC-tagged)
│   └── 2026-05-13.jsonl
└── errors/
    └── 2026-05-12.jsonl       # structured errors
```

### Key module: `app/observability/persistent_log.py`

| Function | Purpose |
|----------|---------|
| `log_api_call(...)` | Write an API request record (async, best-effort) |
| `log_activity(...)` | Write an HMAC-tagged admin mutation record |
| `log_error(...)` | Write a structured error record |
| `iter_records(stream, days)` | Async generator yielding records from a stream |
| `summarise(days)` | Aggregate stats for the email report |
| `prune_now()` | Delete files older than 180 days |
| `start_janitor()` / `stop_janitor()` | Lifecycle management (called from lifespan) |
| `resolve_actor(request)` | Extract (email, role) from SSO session or Bearer |
| `client_ip_of(request)` | Extract client IP (X-Forwarded-For aware) |

### Redis keys added in v1.18.0

| Key pattern | Type | Purpose |
|-------------|------|---------|
| `arbiter:ratelimit:token:{tid}:{minute}` | STRING (int) | Per-token sliding-minute counter |
| `arbiter:announcement:{id}` | STRING (JSON) | Individual announcement record |
| `arbiter:announcements:active` | ZSET | Active announcement IDs (score = created_at) |
| `arbiter:error_log_z` | ZSET | Sorted-set error log (score = timestamp) |
| `arbiter:health:model:last_summary` | STRING (JSON) | Latest weekly health summary |

### Adaptive routing internals

**Gap A** — `router.py::_get_unhealthy_providers()`:
- Scans `arbiter:stats:provider:{p}:success/errors` for each provider
- Marks unhealthy if total ≥ 100 AND error_rate ≥ 20%
- 60-second cache on `self._unhealthy_cache`
- `_apply_health_demote(candidates)` moves unhealthy providers to tail

**Gap B** — `key_pool.py::get_best_key()`:
- After first pick returns None, checks `_any_rpm_throttled()`
- If all keys are at ≥ 85% RPM AND seconds_to_reset < 10: `await asyncio.sleep(seconds_to_reset + 0.1)` then re-pick
- One retry only (prevents unbounded blocking)

**TPM-aware scoring** — `key_pool.py::_score_key(estimated_request_tokens)`:
```python
remaining_tpm = max(0, tpm_limit - tpm_used)
needed = int(estimated_request_tokens * 1.1)  # 10% safety margin
if needed > remaining_tpm:
    tpm_avail = 0.0
else:
    tpm_avail = max(0.0, (remaining_tpm - needed) / tpm_limit)
```

### Announcements service internals

- Key: `arbiter:announcement:{id}` with TTL = ttl_days × 86400
- Sorted set: `arbiter:announcements:active` (score = created_at)
- `_resolve_impacted_tokens()` scans `arbiter:stats:token:*:provider:*:requests` to find which gateway tokens actually use the affected providers
- Expired announcements are cleaned up on every read via `ZRANGEBYSCORE` vs key existence

---

## Next Steps

- Read [README.md](README.md) for user overview
- Read [USERGUIDE.md](USERGUIDE.md) for API usage
- Contribute a new provider!
- Submit issues/PRs

---

**Made with ❤️ for extensible, self-hosted AI infrastructure.**
