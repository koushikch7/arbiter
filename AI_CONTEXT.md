# AI_CONTEXT.md — Arbiter AI Gateway

> **Purpose of this file:** Complete session context for any AI assistant (Claude, GPT, Gemini, etc.) picking up work on this codebase. Read this before asking any questions. Update the "Session Log" section at the end of every work session.

---

## What Is Arbiter

Arbiter is a **self-hosted, OpenAI-compatible AI gateway** that aggregates 11 free-tier LLM providers into a single `/v1/chat/completions` endpoint. It intelligently routes requests to the best available model based on intent, complexity, quota state, and provider health — with automatic failover, rate-limit management, and model-level error tracking.

- **Live URL:** `https://arbiter.chkoushik.com`
- **Health:** `GET /health` → `{"status":"ok","version":"1.20.3"}`
- **API docs:** `/docs` (Swagger) · `/redoc` · `/openapi.json`
- **Server:** `oracle.chkoushik.com` (Ubuntu, SSH as `ubuntu@oracle.chkoushik.com`)
- **Container:** `arbiter-gateway-1` (Docker, port 8080→8000)
- **Redis:** `556ac8df0f28_arbiter-redis-1`
- **Source:** `/var/www/html/arbiter/` · GitHub: `github.com/koushikch7/arbiter` (branch: `master`)
- **Current version:** `1.20.3`

---

## Architecture

```
Client → POST /v1/chat/completions
           ↓
    IntelligentRouter (_prepare_route)
           ↓
    1. Check Redis cache (temp ≤ 0.3)
    2. Build candidate chain (auto_router.py scoring)
    3. Gap A: demote providers with >30% non-rate-limit errors (60s cache)
    4. Filter disabled models (arbiter:disabled:model:* Redis keys, 60s cache)
    5. Provider daily-skip (_prov_daily_skip set per request)
    6. Walk candidates: get_best_key → provider.complete()
       - RateLimitError → mark_failed (62s) OR mark_daily_exhausted (until midnight UTC)
       - ProviderError → record_error; if 404/403 → mark disabled 7d
       - Success → record_usage, cache response
```

**Key files:**
| File | Purpose |
|------|---------|
| `app/routing/router.py` | Main routing loop, Gap A demotion, provider fast-skip, auto-disable on permanent errors |
| `app/routing/auto_router.py` | Candidate scoring (intent × complexity × quota × diversity) |
| `app/key_management/key_pool.py` | Per-key Redis counters: RPM, TPM, daily (UTC midnight), monthly |
| `app/providers/_free_tier_catalog.py` | **Single source of truth** for all provider models and their limits |
| `app/services/model_health.py` | Weekly health probe (Mondays 17:00 UTC); auto-disables permanently failing models |
| `app/main.py` | FastAPI app, provider init, background schedulers |
| `.env` | API keys for all providers (never commit) |

---

## Providers & Limits (as of v1.20.3)

| Provider | RPM | Daily | Monthly | Notes |
|----------|-----|-------|---------|-------|
| Gemini | 15 | 1,000 (flash-lite) | — | 4 keys; paid models gated to #paid keys |
| Groq | 30 | 14,400 (8b) / 1,000 (70b) | — | 2 keys; model overrides in key_pool.py |
| OpenRouter | 20 | 50 | — | 1 key; no credits |
| Cohere | 20 | 33 | **1,000** | 1 trial key; monthly tracked since v1.20.3 |
| Cloudflare | 300 | 200 (mixed) | — | 1 key; 10K neurons/day; per-model overrides (8B→400, 120B→80) |
| Cerebras | 5 | 1,000 | — | 1 key; 30K TPM |
| HuggingFace | 10 | 100 | — | 1 key; credit-based |
| Pollinations | 4 | 1,000 | — | 1 key |
| Routeway | 60 | 10,000 | — | 1 key |
| NVIDIA | 40 | 1,000 | — | 1 key |
| Ollama | 60 | 5,000 | — | 1 key; cloud-tagged models |

---

## Redis Key Patterns

```
arbiter:stats:provider:{name}:success          — lifetime success count (Gap A calculation)
arbiter:stats:provider:{name}:errors           — lifetime error count
arbiter:stats:provider:{name}:rate_limited     — lifetime 429 count (excluded from Gap A)
{provider}:{key_hash}:rpm                      — 60s anchored TTL window
{provider}:{key_hash}:tpm                      — 60s anchored TTL window
{provider}:{key_hash}:daily:{YYYY-MM-DD}       — daily request counter (30h TTL)
{provider}:{key_hash}:monthly:{YYYY-MM}        — monthly request counter (TTL = end of month)
{provider}:{key_hash}:failed                   — cooldown flag (15s–3600s)
{provider}:{key_hash}:m:{slug}:daily:...       — per-model daily counter
arbiter:disabled:model:{provider}:{model}      — 7-day permanent-fail disable flag
arbiter:health:model:{provider}:{model}        — weekly health probe result (JSON, 14d TTL)
```

**Useful Redis commands (run inside container):**
```bash
docker exec 556ac8df0f28_arbiter-redis-1 redis-cli KEYS "arbiter:stats:provider:*"
docker exec 556ac8df0f28_arbiter-redis-1 redis-cli KEYS "arbiter:disabled:model:*"
docker exec 556ac8df0f28_arbiter-redis-1 redis-cli GET "cloudflare:49dc5409fc:daily:$(date -u +%Y-%m-%d)"
```

---

## Deployment

```bash
# Rebuild and restart (after code changes)
cd /var/www/html/arbiter && docker compose up --build -d

# View logs (excluding health pings)
docker logs arbiter-gateway-1 --since 10m 2>&1 | grep -v "GET /health"

# Health check
curl -s http://localhost:8080/health
```

**Git workflow:**
```bash
cd /var/www/html/arbiter
git add <files>
git commit -m "type(scope): description"
git push origin master
```

---

## Known Issues / Open Items

- **HuggingFace 93% error rate** — inherently unreliable inference endpoints; stays demoted by Gap A most of the time. No clean fix without paying for HF Inference Endpoints.
- **Cerebras 5 RPM** — very tight. Throttles quickly on burst traffic. Only 1 key configured.
- **Cloudflare neuron budget** — the 200/day request approximation may still diverge from real neuron consumption on long prompts. Monitor `cloudflare:*:daily:*` Redis key vs actual 429 frequency. If 429s reappear mid-day, lower `daily` in PROVIDER_LIMITS further.
- **Cohere trial key 1000/month** — once exhausted, unavailable until next calendar month (UTC). `monthly` counter now tracks this properly since v1.20.3.
- **Model catalog drift** — models are hardcoded in `_free_tier_catalog.py`. Weekly health check (Mondays) probes them and auto-disables permanent failures, but new model additions require manual catalog updates.
- **GitHub Dependabot alerts** — 2 vulnerabilities (1 high, 1 moderate) reported on the repo. Review and patch when possible.

---

## Session Log

### Session: 2026-06-02 — Koushik CH + Claude (claude-sonnet-4-6)

**Work done:**

1. **Diagnosed playground not working in FinPredict** — root cause: Yahoo Finance blocked Arbiter's custom User-Agent from inside Docker container; all stock quotes timing out.

2. **Arbiter v1.20.2** — 4 fixes:
   - Removed invalid models: `@cf/moonshot/kimi-k2.6`, `@cf/moonshot/kimi-k2.5` (CF 400), `ollama/deepseek-v3.1:671b-cloud` (requires paid sub), `gemini/gemini-3.1-flash-lite-preview` (discontinued May 25)
   - Fixed Cerebras model name: `qwen-3-235b-a22b-instruct-2507` → `qwen-3-235b-a22b-instruct`
   - Cloudflare daily limit corrected: 1,000 → 200 (10K neurons ÷ avg 50/call)
   - Added model-level auto-disable: permanent errors (404/403) write `arbiter:disabled:model:{p}:{m}` with 7-day TTL
   - Added `_get_disabled_models()` filter in routing candidate chain
   - Reset poisoned provider error counters in Redis (cerebras, gemini, ollama, groq, cloudflare, pollinations)
   - Pre-disabled 5 known-bad models in Redis immediately

3. **Arbiter v1.20.2 quota fixes:**
   - `mark_daily_exhausted()` — sets daily counter to limit+1 with TTL until midnight UTC; fixes Cloudflare 429 "neurons" cycling every 62s all day
   - `is_daily_exhausted()` — O(n-keys) check
   - `_prov_daily_skip` set per routing request — once a provider is confirmed daily-exhausted, all its remaining models are skipped with a single `continue`
   - All 3 `mark_failed` calls in both `route()` and `route_stream()` updated to detect daily-exhaustion keywords

4. **Arbiter v1.20.3** — monthly quota tracking:
   - Added `monthly: 1_000` to Cohere in PROVIDER_LIMITS
   - `_this_month_utc()`, `_seconds_until_month_end()`, `_monthly_key()`, `_monthly_limit()` helpers
   - Monthly counter in `record_usage()` with TTL = 10 min into 1st of next month
   - Monthly hard check + predictive throttle (85%) in `_score_key()`
   - `monthly_avail` clamps `effective_daily_avail` in composite score

5. **Docs** — CHANGELOG v1.20.2 + v1.20.3, README What's New updated.

**Commits today:**
- `80e2dcb` — fix: remove invalid models, fix Cloudflare quota, add model-level auto-disable
- `a11300f` — fix(quota): daily-exhaustion aware routing + provider fast-skip (v1.20.2)
- `8313e03` — feat(quota): add monthly limit tracking (v1.20.3)
- `bf42725` — docs: v1.20.3 CHANGELOG + README

**State at end of session:** v1.20.3 running healthy. All 11 providers initializing. No routing errors in logs.
