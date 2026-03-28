# Arbiter – Intelligent LLM Router & Gateway

A self-hosted, production-ready gateway that aggregates **8 free-tier LLM providers** behind a **single OpenAI-compatible endpoint**. Intelligently routes requests across providers and accounts using **weighted scoring, model hierarchies, and automatic fallback**.

Designed for **multi-agent frameworks** like OpenClaw that generate concurrent bursts of requests — maximizes free-tier quota usage and prevents rate-limit bottlenecks.

---

## 🎯 Core Features

### ✅ OpenAI-Compatible API
- **Drop-in replacement** — expose `/v1/chat/completions` and `/v1/models` endpoints
- Parse incoming OpenAI-format requests, translate to vendor-specific APIs, format responses back to OpenAI standard
- Supports `temperature`, `top_p`, `max_tokens`, `stop_sequences`

### ✅ Multi-Vendor Integration & Key Pool Management
- **Gemini** (4 free-tier models, 1M context) — 5–15 RPM, 100–1,000 RPD
- **Groq** (8 models, 131K context) — 30–60 RPM, 1,000–14,400 RPD
- **Cloudflare Workers AI** (11 models, 131K–256K context) — 300 RPM free tier
- **Cerebras Inference** (4 models, 8K context) — 30 RPM, 60K TPM, 1M tokens/day
- **OpenRouter** (7 `:free` models, 128K–131K context) — 20 RPM, 50–1,000 RPD
- **Cohere** (4 models, 128–256K context) — 20 RPM, 33 RPD
- **HuggingFace** (4 models, 8K–32K context) — Limited free credits
- **Pollinations.ai** (3 models, 32K context) — Completely free, no key required
- **Multi-account support** — Unlimited accounts per provider with intelligent scoring

### ✅ Weighted Scoring Algorithm
Each API key is scored in **real-time** by remaining quota:
```
score = (rpm_available × 0.30) + (tpm_available × 0.20) + (daily_available × 0.50)
```
- **Daily quota** is most critical (no reset until midnight)
- Automatically selects the key with the **most headroom**
- Failed keys get **5-minute cooldown** before retry

### ✅ Two-Level Fallback with Model Hierarchies
When a request is made:
1. **Cache check** (Redis) — return instant cached response if temperature ≤ 0.3
2. **Provider order** — selected by token count, capabilities, or explicit model name
3. **Model hierarchy** — try each model in vendor's preferred order (e.g., Gemini Flash → Pro)
4. **Key rotation** — rotate through all accounts for the same model
5. **Cross-vendor fallback** — only move to next vendor after exhausting the current one

Example flow for a 50K-token prompt:
```
Try Gemini (large context required):
  ├─ Try gemini-3.1-flash-lite-preview with account 1 ✓ Success → return
  │
  └─ (if account 1 hits 429): Try account 2, account 3, etc.
       └─ (if all accounts hit TPM): Try gemini-3-flash-preview
            └─ (if all Gemini models fail): Try Groq
                 ├─ Try llama-3.1-8b-instant → llama-3.3-70b-versatile → qwen3-32b
                 └─ (if Groq fails): Try Cloudflare → Cerebras → OpenRouter
```

### ✅ Semantic & Exact-Match Caching
- **Cache deterministic requests** (temperature ≤ 0.3) to Redis
- **SHA-256 keyed** on (model + messages)
- Configurable TTL (default 1 hour)
- Transparent — client receives cached response instantly

### ✅ Real-Time Observability & API Documentation
- **Dashboard** (`/dashboard`) — Dark-themed web UI, auto-refreshing every 10s:
  - **KPIs**: Total requests, success rate, cache hit rate
  - **Per-provider table**: Status, active accounts, request counts, success rates
  - **Per-account table**: Availability score (0–100%), RPM/TPM/daily usage bars
  - **Color-coded health**: 🟢 Healthy → 🟡 Degraded → 🔴 Unavailable
- **Interactive API Docs** (`/api-docs`) — Full playground with live request tester, provider table, authentication guide
- **Swagger UI** (`/docs`) — Standard OpenAPI documentation
- **JSON Stats** (`/dashboard/stats`) — Programmatic access to metrics

### ✅ API Authentication & Security
- **Gateway-level authentication** — Optional `Authorization: Bearer <key>` validation
- **Multi-key support** — Multiple gateway API keys (comma-separated in `.env`)
- **Cloudflare Access integration** — JWT validation via Cloudflare Zero Trust
- **JWKS caching** — 1-hour TTL for performance
- **No API keys in logs** — Keys hashed; only first 4 chars logged
- **Secure header transmission** — All secrets sent via secure channels

### ✅ Runtime API Key Management (no restart)
- **Add / remove keys at runtime** — stored in Redis, merged with `.env` keys automatically
- **Enable / disable providers** — take a provider offline and bring it back without restarting Docker
- **Test connectivity** — probe any provider and measure round-trip latency
- **Hot-reload all providers** in one click
- **API:** `GET/POST/DELETE /api/providers/*`

### ✅ Image Generation — Pollinations.ai (free)
- **`POST /v1/images/generations`** — OpenAI-compatible image endpoint
- Backed by FLUX models: `flux`, `flux-realism`, `flux-anime`, `flux-3d`, `turbo`
- Free, no API key required; supports prompt, negative prompt, size up to 2048×2048, seed, count
- **`GET /v1/images/models`** — list available models

### ✅ Enterprise UI/UX (unified design system)
- Shared `arbiter.css` + `arbiter.js` across all pages — consistent sidebar, topbar, components
- **Light / dark mode** — system preference detection + manual toggle, persisted in `localStorage`
- **Dashboard** — KPI cards, Chart.js line + doughnut charts, provider status table, key details accordion
- **API Docs** (`/api-docs`) — 5-tab layout with live playground, provider table, endpoint reference
- **Settings** (`/settings`) — API Keys management, routing order, model overrides, image gen, Cloudflare Workers, cache

### ✅ Cloudflare Workers AI Manager
- **Create Workers** — Provision new Workers AI instances from the gateway
- **List Models** — View available Cloudflare models
- **List/Delete Workers** — Manage deployed Workers
- **Admin endpoints** — `/cloudflare/workers/*` routes

### ✅ In-Memory Redis Fallback
Gateway starts successfully **even without Redis**:
- Caching disabled but routing functional
- Rate-limit tracking in memory (per-process, not distributed)
- Perfect for local development

---

## 🏗️ Architecture

```
                        OpenClaw / Your App
                                │
                                ▼
                  ┌─────────────────────────────────────┐
                  │        FastAPI Gateway              │
                  │  /v1/chat/completions               │
                  │  /v1/images/generations             │
                  │  /dashboard · /api-docs · /settings │
                  │  /api/providers/* (key mgmt)        │
                  │  /cloudflare/workers/* (mgr)        │
                  └─────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
    ┌────────┐         ┌─────────────┐         ┌──────────────┐
    │ Cache  │         │   Routing   │         │   Key Pool   │
    │ Redis  │         │   Engine    │         │   Scoring    │
    └────────┘         └─────────────┘         └──────────────┘
        │                   │                       │
    ┌───┴──────────────────┼──────────────────────┬┴───┐
    ▼   ▼   ▼   ▼   ▼   ▼   ▼   ▼
┌──────────────────────────────────────────────────────────┐
│  Gemini │ Groq │ Cloudflare │ Cerebras │ OpenRouter    │
│  Provider  Adapter  Workers AI  Inference  + Cohere     │
│        HuggingFace  │  Pollinations                     │
└──────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  External Provider APIs (8 vendors, 40+ models)         │
└──────────────────────────────────────────────────────────┘
```

### Request Flow

```
1. Client Request
   │ POST /v1/chat/completions
   │ {"model": "gemini-2.5-flash", "messages": [...]}
   │
2. Cache Lookup
   │ SHA256("gemini-2.5-flash" + messages) → Redis
   │ If temp ≤ 0.3 and cached → return instantly
   │
3. Intelligent Routing
   │ ├─ Estimate tokens
   │ ├─ Select provider order (token-aware, capability-aware)
   │ └─ Get model hierarchy for provider
   │
4. Multi-Account Key Selection
   │ ├─ Score all keys: (rpm_avail × 0.30) + (tpm_avail × 0.20) + (daily_avail × 0.50)
   │ ├─ Pick key with highest score
   │ └─ If 429 (rate limit): mark key failed, try next highest-scoring key
   │
5. Provider Adapter
   │ ├─ Translate OpenAI → Gemini/Groq/OpenRouter format
   │ ├─ Call vendor API
   │ └─ Translate response back to OpenAI format
   │
6. Usage Tracking
   │ ├─ Increment Redis: key_rpm, key_tpm, key_daily
   │ ├─ Cache response if temp ≤ 0.3
   │ └─ Update stats: requests_total, requests_success, etc.
   │
7. Return Response
   └─ {"id": "chatcmpl-...", "choices": [...], "usage": {...}}
```

---

## ⚡ Quick Start

### 1️⃣ Clone & Install

```bash
cd /path/to/arbiter
pip install -r requirements.txt
cp .env.example .env
```

### 2️⃣ Configure API Keys

Edit `.env` (minimum: one provider):
```bash
# Google Gemini  (https://aistudio.google.com/app/apikey)
GEMINI_API_KEYS=your-gemini-key-1,your-gemini-key-2

# Groq  (https://console.groq.com/keys)
GROQ_API_KEYS=your-groq-key

# Cloudflare Workers AI  (format: account_id|api_token)
CLOUDFLARE_API_KEYS=your-account-id|your-api-token

# Cerebras Inference  (https://cloud.cerebras.ai)
CEREBRAS_API_KEYS=your-cerebras-api-key

# OpenRouter  (https://openrouter.ai/keys)
OPENROUTER_API_KEYS=your-openrouter-key

# Cohere  (https://dashboard.cohere.com/api-keys)
COHERE_API_KEYS=your-cohere-key

# HuggingFace  (https://huggingface.co/settings/tokens)
HUGGINGFACE_API_KEYS=hf_your-token-here

# Pollinations.ai  (NO KEY NEEDED — completely free!)
# POLLINATIONS_API_KEYS=  ← leave empty
```

### 3️⃣ Run Locally (Dev)

```bash
# With Redis (optional, gateway uses in-memory fallback if Redis unavailable)
docker run -d -p 6379:6379 redis:7-alpine

# Start gateway
uvicorn app.main:app --reload --port 8000
```

### 4️⃣ Run with Docker (Production)

```bash
docker compose up -d
```

### 5️⃣ Test the Gateway

```bash
# List available models
curl http://localhost:8000/v1/models | jq .

# Make a chat completion request
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [{"role": "user", "content": "Hello, who are you?"}],
    "temperature": 0.7
  }' | jq .

# View dashboard
open http://localhost:8000/dashboard
```

---

## 🔧 Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |
| `DEBUG` | `false` | FastAPI debug mode (never use in production) |
| `LOG_LEVEL` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `CACHE_TTL` | `3600` | Cache TTL in seconds |
| **Gateway Auth** | | |
| `GATEWAY_API_KEYS` | (empty) | Comma-separated Bearer tokens (multi-key support) |
| `GATEWAY_API_KEY` | (empty) | Single Bearer token (legacy, for backward compat) |
| **Cloudflare Access** | | |
| `ENABLE_CF_ACCESS` | `false` | Enable Cloudflare Access JWT validation |
| `CLOUDFLARE_ACCESS_TEAM_NAME` | (empty) | Cloudflare Access team name (e.g. "myteam") |
| `CLOUDFLARE_ACCESS_AUD` | (empty) | Audience tag from Access application |
| **Provider Keys** | | |
| `GEMINI_API_KEYS` | (empty) | Comma-separated Gemini keys |
| `GROQ_API_KEYS` | (empty) | Comma-separated Groq keys |
| `CLOUDFLARE_API_KEYS` | (empty) | Format: `account_id\|api_token` (comma-separated) |
| `CEREBRAS_API_KEYS` | (empty) | Comma-separated Cerebras keys |
| `OPENROUTER_API_KEYS` | (empty) | Comma-separated OpenRouter keys |
| `COHERE_API_KEYS` | (empty) | Comma-separated Cohere keys |
| `HUGGINGFACE_API_KEYS` | (empty) | Comma-separated HuggingFace tokens |
| `POLLINATIONS_API_KEYS` | (empty) | Leave empty (free, no key needed) |

### Rate Limits

**Per-provider free-tier limits** (tracked per API key, verified March 2026):

| Provider | RPM | TPM | Daily | Notes |
|---|---|---|---|---|
| **Gemini** | 5–15 | 250K | 100–1K | Flash-lite highest quota |
| **Groq** | 30–60 | 6K–30K | 1K–14.4K | Varies by model |
| **Cloudflare** | 300 | 1M+ | 10K+ | Workers AI free tier |
| **Cerebras** | 30 | 60K | 1M tokens | Production tier |
| **OpenRouter** | 20 | — | 50–1K | No credits vs with credits |
| **Cohere** | 20 | — | 33 | ≈1,000/month |
| **HuggingFace** | 10 | 50K | 500 | Limited free credits |
| **Pollinations** | 5 | 100K | 1K | Completely free ✅ |

The gateway **tracks per-key usage in Redis** and automatically:
- Selects keys with the most remaining quota
- Marks keys on cooldown (5 min) after a 429 error
- Routes to the next best key/model/provider seamlessly

To **adjust limits per provider**, edit `app/key_management/key_pool.py`:
```python
PROVIDER_LIMITS = {
    "gemini": {"rpm": 5, "tpm": 250_000, "daily": 100},
    "groq":   {"rpm": 30, "tpm": 6_000, "daily": 1_000},
    ...
}
```

---

## 📡 API Reference

### POST `/v1/chat/completions`

OpenAI-compatible chat completion endpoint.

**Request:**
```json
{
  "model": "gemini-2.5-flash",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello!"}
  ],
  "temperature": 0.7,
  "top_p": 1.0,
  "max_tokens": 1024,
  "stop": ["END"],
  "stream": false
}
```

**Response:**
```json
{
  "id": "chatcmpl-abc123def",
  "object": "chat.completion",
  "created": 1709040000,
  "model": "gemini-2.5-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! I'm Claude, an AI assistant..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 123,
    "total_tokens": 165
  }
}
```

**Parameters:**
- `model` (string, required) — Model ID (e.g., `gemini-2.5-flash`, `llama-3.3-70b-versatile:free`)
- `messages` (array, required) — Message objects with `role` and `content`
- `temperature` (float, default 0.7) — Sampling temperature (0.0–2.0)
- `top_p` (float, default 1.0) — Nucleus sampling (0.0–1.0)
- `max_tokens` (integer, optional) — Max tokens to generate
- `stop` (array, optional) — Stop sequences
- `stream` (boolean, default false) — **Not yet supported**

**Errors:**

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_request_error` | Bad request (missing field, invalid format) |
| 401 | `authentication_error` | Missing/invalid API key (if `GATEWAY_API_KEY` set) |
| 429 | `rate_limit_error` | All providers/accounts exhausted |
| 500 | `server_error` | Internal gateway error |

### GET `/v1/models`

List available models.

**Response:**
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
    {
      "id": "llama-3.3-70b-instruct:free",
      "object": "model",
      "created": 1700000000,
      "owned_by": "openrouter"
    }
    ...
  ]
}
```

### GET `/health`

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "redis": "connected",
  "providers": ["gemini", "groq", "openrouter"],
  "version": "1.0.0"
}
```

### GET `/dashboard`

Web-based observability dashboard (HTML).

---

## 🚀 Deployment

### Docker Compose (Recommended)

```bash
docker compose up -d
```

Services:
- **gateway** — FastAPI server (port 8000)
- **redis** — Redis for caching & rate limiting (port 6379)

Volumes:
- `redis_data` — Redis persistence (AOF mode)

### Kubernetes

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: arbiter-config
data:
  REDIS_URL: redis://redis:6379
  GEMINI_API_KEYS: "your-keys-here"
  # ... other env vars

---
apiVersion: v1
kind: Service
metadata:
  name: arbiter
spec:
  type: LoadBalancer
  ports:
    - port: 80
      targetPort: 8000
  selector:
    app: arbiter

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: arbiter
spec:
  replicas: 2
  selector:
    matchLabels:
      app: arbiter
  template:
    metadata:
      labels:
        app: arbiter
    spec:
      containers:
      - name: gateway
        image: arbiter:latest
        ports:
        - containerPort: 8000
        envFrom:
        - configMapRef:
            name: arbiter-config
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 30
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: 1000m
            memory: 1Gi
```

Apply with:
```bash
kubectl apply -f k8s-manifest.yaml
kubectl port-forward svc/arbiter 8000:80
```

### systemd Service

Create `/etc/systemd/system/arbiter.service`:
```ini
[Unit]
Description=Arbiter
After=network.target redis.service

[Service]
Type=simple
User=arbiter
WorkingDirectory=/opt/arbiter
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10
EnvironmentFile=/opt/arbiter/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable arbiter
sudo systemctl start arbiter
sudo journalctl -f -u arbiter
```

---

## 📊 Monitoring & Troubleshooting

### Check Gateway Health

```bash
curl http://localhost:8000/health | jq .
```

Expected:
```json
{
  "status": "ok",
  "redis": "connected",
  "providers": ["gemini", "groq", "openrouter"],
  "version": "1.0.0"
}
```

### View Real-Time Stats

```bash
# Dashboard (browser)
http://localhost:8000/dashboard

# Via API
curl http://localhost:8000/dashboard/stats | jq .

# In Redis CLI
redis-cli
> KEYS arbiter:stats:*
> GET arbiter:stats:requests_total
> GET arbiter:stats:requests_success
> GET arbiter:stats:cache_hits
```

### Common Issues

**Problem: "No available key for provider"**
- ✓ Check `.env` has API keys configured
- ✓ Verify API keys are valid (test in vendor's console)
- ✓ Check daily quota hasn't been exhausted (see dashboard)
- ✓ Wait 5 minutes if key is on cooldown (429 error)

**Problem: All requests fail with 500**
- ✓ Check logs: `docker compose logs -f gateway`
- ✓ Verify Redis is running: `redis-cli ping` → should return `PONG`
- ✓ Test provider endpoints manually (vendor might be down)

**Problem: Cache isn't working**
- ✓ Check `CACHE_TTL` env var (default 1 hour)
- ✓ Only requests with `temperature ≤ 0.3` are cached
- ✓ Check Redis connection: `http://localhost:8000/health`

**Problem: Rate limiting too aggressive**
- ✓ Adjust `PROVIDER_LIMITS` in `app/key_management/key_pool.py`
- ✓ Add more API keys (they're scored independently)
- ✓ Use `gemini-2.5-flash-lite` instead of `pro` (higher quotas)

### Logs

Gateway logs to stdout/stderr. In Docker:
```bash
docker compose logs -f gateway  # tail gateway logs
docker compose logs -f redis    # tail redis logs
```

Set `LOG_LEVEL=DEBUG` for verbose output:
```bash
# In .env
LOG_LEVEL=DEBUG
docker compose restart gateway
```

---

## 🛡️ Security

### Gateway-Level Authentication

Optionally require an API key for all requests:

```bash
# In .env
GATEWAY_API_KEY=your-secret-key-here

# Then clients must include:
curl -H "Authorization: Bearer your-secret-key-here" \
  http://localhost:8000/v1/models
```

### API Key Security

- **Never commit `.env`** to version control (use `.env.example` template)
- **Never log API keys** — gateway hashes keys in Redis with MD5 (first 10 chars only)
- **Rotate keys regularly** — add new key to `*_API_KEYS` env var, remove old one
- **Use per-account keys** — don't share one key across accounts/environments
- **Lock down network access** — run gateway behind a firewall or VPN

### Data Privacy

- **Requests are not cached** if `temperature > 0.3` (non-deterministic)
- **Cached responses** are stored in Redis — secure Redis with passwords/ACLs
- **Usage stats** (per-key requests/tokens) are visible in dashboard — restrict access
- **No request logs** to third parties — all processing is local

---

## 🤝 Contributing

### Adding a New Provider

1. **Create adapter** in `app/providers/new_provider.py`:
   ```python
   from app.providers.base import BaseProvider, RateLimitError, ProviderError

   class NewProvider(BaseProvider):
       name = "newprovider"
       models = ["model-1", "model-2"]
       max_context_tokens = 32_000
       default_model = "model-1"

       async def complete(self, request, api_key):
           # Translate request, call API, return ChatCompletionResponse
           ...
   ```

2. **Register in main.py**:
   ```python
   from app.providers.new_provider import NewProvider

   provider_classes = {
       "gemini": GeminiProvider,
       "groq": GroqProvider,
       "newprovider": NewProvider,  # Add here
       ...
   }
   ```

3. **Add to router hierarchy** in `app/routing/router.py`:
   ```python
   VENDOR_MODEL_HIERARCHY = {
       "gemini": [...],
       "newprovider": [
           ("model-1", 32_000),
           ("model-2", 16_000),
       ],
       ...
   }
   ```

4. **Set rate limits** in `app/key_management/key_pool.py`:
   ```python
   PROVIDER_LIMITS = {
       ...
       "newprovider": {"rpm": 20, "tpm": 100_000, "daily": 1000},
   }
   ```

### Running Tests

```bash
pytest tests/ -v
```

---

## 📄 License

MIT License — See LICENSE file

---

## 📞 Support & Feedback

- **Issues**: Open an issue on GitHub
- **Discussions**: Start a discussion for feature requests
- **Pull Requests**: Welcome! Please include tests and update CHANGELOG

---

## 📚 Further Reading

- [User Guide](USERGUIDE.md) — Configuration, API usage, examples
- [Developer Docs](DEVELOPER.md) — Architecture, extension points
- [Changelog](CHANGELOG.md) — Version history, improvements
- [Gemini API](https://ai.google.dev/gemini-api/docs)
- [Groq API](https://console.groq.com/docs/models)
- [OpenRouter](https://openrouter.ai/docs)
- [Cohere API](https://docs.cohere.com/docs/models)

---

**Made with ❤️ for multi-agent teams running on free-tier LLMs.**
