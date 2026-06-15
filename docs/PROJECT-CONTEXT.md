# Project Context — Arbiter

AI-friendly context document for onboarding and continuation prompts.

---

## What is Arbiter?

A self-hosted, production-ready LLM gateway that aggregates 12+ providers behind a single OpenAI-compatible endpoint. Designed for multi-agent frameworks (like OpenClaw/JARVIS) that generate concurrent request bursts.

**Current version:** 1.21.0 (as of 2026-06-15)

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.12, FastAPI 0.135.2, Pydantic v2 |
| Database | Redis 7 (`decode_responses=True`) |
| Deployment | Docker Compose (gateway + redis) |
| Auth | Google SSO (session-based) + Bearer tokens |
| Frontend | Vanilla HTML/CSS/JS (no framework) |
| Logging | 180-day JSONL file logs + in-memory ring buffer |
| Email | Zoho SMTP (daily + weekly consolidated) |
| CDN | Cloudflare (with Access for admin routes) |

---

## Key Architecture Decisions

1. **Redis `decode_responses=True`** — all values are UTF-8 strings. Binary data (gzip cache) must be base64-wrapped with `GZ1:` prefix.
2. **Middleware ordering** — Starlette LIFO: Session must wrap GatewayAuth (added after) so `scope["session"]` is populated before auth reads it.
3. **`docker compose down && docker compose up -d`** — never `restart`. Config changes require fresh container.
4. **Best-effort logging** — persistent log writes never block request handling (run via `asyncio.to_thread` since v1.21.0). If disk is full, records are silently dropped.
5. **Rate limiting** — the per-token `/v1/*` limiter is **fail-closed** since v1.21.0 (Redis error → 429), so quota can't be silently bypassed. Non-critical limiters (e.g. `/api/ui-error`) remain fail-open with a bounded in-process fallback.
6. **Shared HTTP client (v1.21.0)** — one process-wide pooled `httpx.AsyncClient` lives in `app/providers/base.py` (`get_shared_async_client`). All providers reuse it; never wrap it in `async with`. Per-request timeouts are passed to each `.post()`/`.stream()` call.
7. **Circuit breaker (v1.21.0)** — per `(provider, model)` pair. 3 hard `ProviderError`s in 120 s opens the circuit (Redis `arbiter:circuit:open:*`) for 300 s. Rate-limit 429s do NOT trip it. Bypasses itself if every candidate is tripped.

---

## Provider Chain (default order)

```
nvidia → gemini → groq → cerebras → zai → cloudflare → openrouter → cohere → huggingface → pollinations → ollama → routeway
```

Adaptive demotion (v1.18.0): providers with ≥20% error rate (≥100 requests) moved to tail automatically.

**Complexity-aware routing (v1.19.0):** Requests are analyzed (13-factor scoring → TRIVIAL/SIMPLE/MODERATE/COMPLEX/EXPERT). Quality/speed weight balance shifts dynamically per tier. Provider diversity enforced (4+ providers in top-8). Explicit weak model selections auto-upgraded for complex requests. Load distribution jitter rotates traffic per-minute across providers.

---

## Default Model

`gemini-3.1-flash-lite` (GA) — formerly `gemini-3.1-flash-lite-preview`. Bridge in place until May 25, 2026 when Google fully deploys the GA endpoint.

---

## Key File Locations

| Purpose | Path |
|---------|------|
| App entry | `app/main.py` |
| Router | `app/routing/router.py` |
| Complexity analyzer | `app/routing/complexity_analyzer.py` |
| Auto-router (scoring) | `app/routing/auto_router.py` |
| Intent classifier | `app/routing/intent_classifier.py` |
| Key scorer | `app/key_management/key_pool.py` |
| Gemini provider | `app/providers/gemini.py` |
| Model catalog | `app/providers/_free_tier_catalog.py` |
| Provider registry | `app/providers/provider_registry.py` |
| Persistent logs | `app/observability/persistent_log.py` |
| Daily report | `app/services/daily_report.py` |
| Health probes | `app/services/model_health.py` |
| Announcements | `app/services/announcements.py` |
| Auth middleware | `app/middleware/auth.py` |
| Config | `app/config.py` |
| Cache | `app/cache/cache.py` |
| Log query API | `app/api/persistent_logs_api.py` |
| Sidebar version | `static/components/sidebar.js` |

---

## Redis Key Patterns

| Pattern | Type | Purpose |
|---------|------|---------|
| `arbiter:stats:provider:{name}:*` | STRING | Provider success/error counters |
| `arbiter:stats:token:{tid}:*` | STRING | Per-token usage counters |
| `arbiter:cache:{hash}` | STRING | Cached responses (may be GZ1: compressed) |
| `arbiter:ratelimit:token:{tid}:{min}` | STRING | Per-token rate limit counter |
| `arbiter:announcement:{id}` | STRING | Announcement JSON (TTL = ttl_days × 86400) |
| `arbiter:announcements:active` | ZSET | Active announcement IDs |
| `arbiter:error_log_z` | ZSET | Sorted-set error log (48h window) |
| `arbiter:health:model:*` | STRING | Health probe results |
| `arbiter:gateway:tokens` | HASH | Gateway token records |
| `arbiter:perf:{provider}:{model}` | STRING | Performance tracking |
| `{provider}:{key_hash}:rpm` | STRING | Per-key RPM counter |
| `{provider}:{key_hash}:tpm` | STRING | Per-key TPM counter |
| `{provider}:{key_hash}:daily` | STRING | Per-key daily counter |
| `{provider}:{key_hash}:failed` | STRING | Key cooldown flag |

---

## Environment Variables (key ones)

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEYS` | Comma-separated keys (suffix `#paid` for paid tier) |
| `GROQ_API_KEYS` | Groq API keys |
| `NVIDIA_API_KEYS` | NVIDIA NIM keys |
| `GATEWAY_API_KEYS` | Legacy static gateway keys (prefer dynamic tokens) |
| `GATEWAY_TOKEN_RATE_LIMIT_PER_MIN` | Default per-token rate limit (100) |
| `SESSION_SECRET_KEY` | HMAC key for activity log + session |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google SSO |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | Email for daily report |
| `REPORT_RECIPIENTS` | Comma-separated email addresses |
| `REDIS_URL` | Redis connection (default `redis://redis:6379`) |
| `LOG_LEVEL` | Python logging level |
| `REQUIRE_AUTH` | If true, reject unauthenticated /v1/* requests |

---

## Held / Deferred Items

- **#7** — Stream timeout (not implemented)
- **#10** — Session cookie for /v1 (not implemented)

---

## Testing

```bash
# Gateway bearer token for testing
arbiter-sk-8e2b5f05d4c07b727e2eea87c16c360aef47cd578c8906969098e49a77d19c4a

# Smoke test
curl -H "Authorization: Bearer <token>" http://localhost:8080/v1/chat/completions \
  -d '{"model":"auto","messages":[{"role":"user","content":"hi"}]}'

# Full free-model probe
python3 scripts/test_all_free_models.py http://localhost:8080
```

---

## Documentation Update Rule

Every time changes are made, update:
- `README.md` — project overview, version highlights
- `CHANGELOG.md` — version history with detailed changes
- `USERGUIDE.md` — end-user documentation
- `DEVELOPER.md` — architecture, modules, API reference
- `docs/FIX-HISTORY.md` — chronological fix/change log
- `docs/PROJECT-CONTEXT.md` — this file (AI context)
