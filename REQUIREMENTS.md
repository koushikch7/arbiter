# Requirements — Arbiter

System requirements, API keys, and environment configuration needed to run the Arbiter LLM gateway.

---

## System Requirements

| Requirement | Minimum |
|-------------|---------|
| Docker Engine | 24.0+ |
| Docker Compose | v2 (plugin) |
| CPU | 1 vCPU |
| RAM | 1 GB (application) + 512 MB (Redis) |
| Disk | 2 GB base + storage for 180-day logs |
| OS | Any Docker-supported Linux (tested: Ubuntu 22.04/24.04) |
| Network | Outbound HTTPS to provider APIs |
| Port | 8080 (configurable) |

---

## Python Dependencies

See [requirements.txt](requirements.txt) for pinned versions. Key packages:

| Package | Version | Purpose |
|---------|---------|---------|
| fastapi | 0.135.2 | Web framework |
| uvicorn[standard] | 0.42.0 | ASGI server |
| httpx | 0.27.2 | Async HTTP client for providers |
| redis | 5.0.8 | State store / cache / rate limiting |
| pydantic | 2.9.2 | Data validation |
| pydantic-settings | 2.5.2 | Env-based config |
| tiktoken | 0.7.0 | Token estimation for TPM scoring |
| authlib | 1.6.11 | Google OAuth / SSO |
| itsdangerous | 2.2.0 | Session signing |
| PyJWT[crypto] | 2.12.0 | JWT token verification |
| jinja2 | 3.1.6 | Email templates |
| boto3 | ≥1.34.0 | OCI Object Storage backups |

---

## API Keys (by provider)

At least one provider key is required. More keys enable failover and higher throughput.

| Provider | Env Variable | Free Tier? | Notes |
|----------|-------------|------------|-------|
| NVIDIA NIM | `NVIDIA_API_KEYS` | Yes (limited) | https://build.nvidia.com |
| Google Gemini | `GEMINI_API_KEYS` | Yes (1500 RPD) | Suffix `#paid` for paid keys |
| Groq | `GROQ_API_KEYS` | Yes (30 RPM) | https://console.groq.com |
| Cerebras | `CEREBRAS_API_KEYS` | Yes | https://cloud.cerebras.ai |
| OpenRouter | `OPENROUTER_API_KEYS` | Yes (some models) | https://openrouter.ai |
| Cohere | `COHERE_API_KEYS` | Yes (trial) | https://dashboard.cohere.com |
| Cloudflare AI | `CLOUDFLARE_API_KEYS` | Yes | Format: `account_id:api_token` |
| Hugging Face | `HUGGINGFACE_API_KEYS` | Yes | https://huggingface.co/settings/tokens |
| Pollinations | `POLLINATIONS_API_KEYS` | Yes (no key needed) | Public endpoint |
| ZAI | `ZAI_API_KEYS` | Yes | — |
| Ollama | `OLLAMA_BASE_URL` | Self-hosted | Default: `http://localhost:11434` |
| Routeway | `ROUTEWAY_API_KEYS` | Yes | — |

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `SESSION_SECRET_KEY` | Random 64+ char string for HMAC signing (sessions, activity logs) |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Cloud OAuth 2.0 client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud OAuth 2.0 client secret |
| At least one `*_API_KEYS` | Provider API key(s), comma-separated |

### Recommended

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL |
| `GATEWAY_TOKEN_RATE_LIMIT_PER_MIN` | `100` | Default per-token rate limit |
| `REQUIRE_AUTH` | `true` | Require authentication for /v1/* |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `SMTP_HOST` | — | SMTP server for email reports |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASS` | — | SMTP password |
| `REPORT_RECIPIENTS` | — | Comma-separated emails for daily report |
| `ALLOWED_EMAILS` | — | Comma-separated emails allowed SSO access |
| `ADMIN_EMAILS` | — | Comma-separated admin emails |
| `ARBITER_LOG_DIR` | `/app/data/logs` | Persistent log directory |

---

## Docker Setup

### Quick Start

```bash
# 1. Clone and configure
git clone <repo-url> arbiter && cd arbiter
cp .env.example .env   # fill in your API keys

# 2. Start
docker compose up -d

# 3. Verify
curl http://localhost:8080/health
```

### Rebuild after changes

```bash
docker compose down && docker compose up -d --build
```

> **Never use `docker compose restart`** — config changes require a fresh container.

### Docker Compose services

| Service | Image | Purpose |
|---------|-------|---------|
| `gateway` | Custom (Dockerfile) | FastAPI application |
| `redis` | `redis:7-alpine` | State store (512 MB, volatile-lru) |

### Volumes

| Volume | Host path | Purpose |
|--------|-----------|---------|
| `./data` | `/app/data` | Persistent logs, user data, model toggles |
| `./.env` | `/app/.env` | Runtime key management |
| `redis_data` | Docker-managed | Redis AOF persistence |

---

## Network Requirements

Outbound HTTPS (443) to:
- `generativelanguage.googleapis.com` (Gemini)
- `api.groq.com` (Groq)
- `integrate.api.nvidia.com` (NVIDIA)
- `api.cerebras.ai` (Cerebras)
- `openrouter.ai` (OpenRouter)
- `api.cohere.ai` (Cohere)
- `api.cloudflare.com` (Cloudflare)
- `api-inference.huggingface.co` (Hugging Face)
- `text.pollinations.ai` (Pollinations)

---

## Health Check

```
GET /health → 200 {"status": "ok", "version": "1.20.0"}
```

Docker uses this internally with a 30-second interval.
