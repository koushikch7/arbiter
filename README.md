# Arbiter – Intelligent LLM Router & Gateway

[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)](https://redis.io/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![OpenAI Compatible](https://img.shields.io/badge/OpenAI-compatible-412991?logo=openai&logoColor=white)](#-openai-compatible-api)
[![Version](https://img.shields.io/badge/version-1.21.0-6366f1.svg)](CHANGELOG.md)

> 🌐 **Website / docs:** **https://koushikch7.github.io/arbiter/** &nbsp;·&nbsp; 📮 [Postman collection](docs/arbiter.postman_collection.json) &nbsp;·&nbsp; 📖 [User Guide](USERGUIDE.md)

A self-hosted, production-ready gateway that aggregates **12+ LLM providers** (plus unlimited custom OpenAI-compatible endpoints) behind a **single OpenAI-compatible endpoint**. Intelligently routes requests across providers and accounts using **complexity-aware scoring, model hierarchies, predictive rate limiting, and automatic fallback**.

Designed for **multi-agent frameworks** like OpenClaw that generate concurrent bursts of requests — maximizes free-tier quota usage and prevents rate-limit bottlenecks.

> **v1.21.0 (2026-06-15):** 🛡️ **Enterprise Hardening** — a full security + performance + reliability audit landed 13 fixes. 🔒 **Security**: constant-time bearer-token comparison (closes a timing side-channel), `.env`-write injection hardening, DNS-aware SSRF guard on custom-provider probe/create, Redis-backed + bounded UI-error rate limiter, fail-closed `/v1/*` limiter on Redis error, and dependency CVE patches (`python-multipart` 0.0.27, `PyJWT` 2.13.0, `authlib` 1.6.12 — `pip-audit` now clean). ⚡ **Performance**: one process-wide pooled `httpx` client reused by every provider (was a fresh TLS handshake per request — ~100-200 ms/call saved under concurrency), pipelined key-pool scoring (single Redis round-trip per key vs up to ~120), NVIDIA hot-path timeout 45 s → 25 s, and non-blocking persistent-log writes. 🔁 **Reliability**: a per-`(provider, model)` **circuit breaker** (3 hard failures in 120 s → skipped for 5 min) stops the gateway from re-attempting reliably-broken models (e.g. Pollinations 402, NVIDIA timeouts) on every request — rate-limit 429s never trip it. 🧹 Removed EOL `google/gemma-3-27b-it` from NVIDIA (was returning 410 on every call). 🐞 Cache key now includes `tools`/`response_format`/`seed` so tool/JSON-mode requests can't collide with a plain-text cached answer. **Integrators:** cache cold-starts on deploy; `/v1/*` may emit a transient 429 if Redis is down (honour `Retry-After`); circuit breaker is transparent. See CHANGELOG `[1.21.0]`.
>
> **v1.20.3 (2026-06-02):** 📅 **Monthly quota tracking** — providers with hard monthly caps (Cohere: 1 000 calls/month) now track a `{provider}:{hash}:monthly:{YYYY-MM}` Redis counter. Keys near the monthly ceiling score progressively lower; keys that hit the ceiling return -1.0 and are excluded for the rest of the month. Prevents mid-month 403s that occurred when heavy days drained the budget before the daily counter showed it. 🚦 **Daily-exhaustion-aware 429 handling** — Cloudflare "neurons exhausted" 429s now set a until-midnight-UTC cooldown instead of 62s, stopping the retry-every-minute loop that burned through the quota all day. ⚡ **Provider fast-skip** — once a provider's daily budget is confirmed exhausted, all remaining models from that provider are skipped in O(1) rather than attempting each individually. 🗑️ **Catalog cleanup** — removed four broken entries: `@cf/moonshot/kimi-k2.6`, `@cf/moonshot/kimi-k2.5` (CF 400), `ollama/deepseek-v3.1:671b-cloud` (CF 403 — paid subscription), `gemini/gemini-3.1-flash-lite-preview` (discontinued May 25). Fixed `cerebras/qwen-3-235b-a22b-instruct-2507` model name. Cloudflare daily limit corrected 1 000 → 200 calls with per-model overrides (8B → 400/day, 120B → 80/day).
> **v1.20.0 (2026-05-26):** 🌐 **Real-Time Web Search** — opt-in via `X-Arbiter-Realtime: true` header. Tavily search results are prepended to the prompt with citations; chosen LLM answers grounded with `[1]`-style references. Source URLs surfaced in `X-Arbiter-Realtime-Sources` response header and `x_arbiter.realtime_sources` JSON field. Free tier: 1 000 searches/month. 🔁 **Multi-key rotation hardening** — fixed the sliding-window TTL bug (counters were refreshing TTL on every call and never expiring), date-bucketed the daily counter to align with UTC midnight, added per-model rate limits so flash-lite (1 000 RPD) isnt capped by pro (100 RPD) on the same key. ⏱️ **`retry_after`-aware cooldowns** — `RateLimitError` now parses upstream-provided `Retry-After` seconds and uses it for `mark_failed` cooldown instead of hardcoded 300 s (typical recovery 5–10 s, not 5 min). 📊 **Verified-against-docs free-tier limits** for all 12 providers (Gemini 15 RPM/1 000 RPD on flash-lite, Cerebras tightened to 5 RPM/30 K TPM/1 M TPD per their May 2026 update, Groq 14 400 RPD on llama-3.1-8b-instant, etc.). 🧠 **Gemini Google Search grounding** — provider forwards `tools: [{google_search:{}}]` when requested (free on 2.0+/3.x). 🔌 **OpenRouter `:online`** opt-in for OR-pinned models. 🪞 **Client-side observability**: response body now echoes the actual chosen model in `model` field, plus new `x_arbiter` block (`provider`, `model`, `complexity`, `realtime_sources`) and `X-Arbiter-Complexity` header. 🩹 Removed the dead `_PRERELEASE_BRIDGE` rewrite that had been silently 404-ing every free Gemini call since Google retired the preview endpoint on 2026-05-25.
>
> **v1.19.1 (2026-05-24):** 🔧 **Tool-Call Routing Fix** — Requests with `tools`/`functions` now auto-route only to tool-capable providers (groq, nvidia, openrouter, cerebras, ollama). Prevents empty responses from Cloudflare/HuggingFace/etc. that silently ignore tool definitions. Added tool-field forwarding to OpenRouter, Cerebras, and Ollama providers. Replaced exhausted Pollinations API key.

> **v1.19.0 (2026-05-23):** 🧠 **Intelligent Complexity-Aware Routing** — New request complexity analyzer (TRIVIAL→EXPERT) that matches request difficulty to model capability. Expert requests → 120B-671B flagship models; trivial → fast 7B models. **Smart Model Upgrade** — clients hardcoding weak models automatically get upgraded for complex requests. **Provider Diversity** — 7+ providers used across varied requests (was 2). **Load Distribution Jitter** — deterministic rotation prevents traffic concentration. **Fixed Gap A** — rate limits no longer count as errors; thresholds raised to prevent false-positive provider demotion. **Fixed Perf Sort** — no longer overrides quality-based ordering.
>
> **v1.18.0 highlights (2026-05-20):** 📁 **180-day persistent file logs** — API calls, admin activity (HMAC-tagged), and errors stored as daily-rotated JSONL with automatic 180-day retention janitor. 🔒 **Admin activity audit** — every key/token/settings mutation recorded with before/after diffs and SHA-256 HMAC tamper detection. 📢 **Dashboard banners** — `POST /api/announcements` publishes severity-coloured notices (3-day TTL) that identify impacted gateways. ⚡ **Adaptive routing** — unhealthy providers (≥20% error rate) auto-demoted; TPM-aware key scoring; wait-for-RPM-reset instead of cross-provider fallback. 🚦 **Per-token rate limiting** — sliding-minute window (default 100 rpm) with Retry-After headers. 📊 **Consolidated weekly email + AI analysis** — Monday report includes 7-day summary, p50/p95 latency, and an AI-generated SRE insights paragraph. 🗜️ **Cache gzip compression** — responses ≥512B compressed before Redis write. 📈 **Sorted-set error log** — O(log N) writes replacing old O(N) list.
>
> **v1.17.0 highlights (2026-05-12):** 🚀 **NVIDIA-first routing** — NVIDIA NIM promoted to the top of the default provider chain. 🛡️ **Predictive rate limiting** — keys are skipped at 95% of RPM/daily quota so the router routes around exhaustion *before* a 429 ever reaches the user. 🩺 **Weekly model health check** — Monday 22:30 IST cron probes every model and feeds results into the daily report. 📊 **Daily report enhancements** — high-error-rate provider alerts (≥25%) and weekly model-health summary in the email. 🌊 **Native streaming for custom providers** — `GenericOpenAIProvider.complete_stream()` removes the faux-stream fallback. 🔥 **Modal + Lightning removed** — provider count 14 → 12 after deprecating unreliable / paid-only providers.
>
> **v1.15.0 highlights (2026-05-03):** 🏗️ **SSOT Provider Registry** — Single source of truth dataclass registry for all providers with limits, models, and metadata. 📧 **Daily Analytics Email** — Automated SMTP (Zoho) daily report at 22:00 IST (16:30 UTC) with KPIs, top models, provider health, and AI-classified error analysis. 🔐 **Gateway Routing Policies** — Per-token model routing: `auto` (default), `restricted` (only allowed models), `preferred` (priority models with auto fallback), plus per-token blocked models. Enterprise UI with model picker in Settings. 📝 **Developer Docs** — New `/developer` page replacing `/api-docs` with 6 tabs: Quick Start, Authentication, Endpoints, Routing, SDK Examples, Gateway Tokens. 👥 **User Invite via Email** — Admin can invite users via SMTP email from the Users page. 🕐 **5-Day Session TTL** — PWA sessions extended from 24h to 5 days to prevent mobile logout. 💾 **Backup Pagination** — Fixed infinite-row bug with 20-item paginated view.
>
> **v1.14.3 highlights (2026-05-03):** 🟢 **NVIDIA NIM provider** — 13th provider added via build.nvidia.com. 5 verified free-tier models (Nemotron-3-Super 120B, Llama-3.3-70B, Mistral-Medium-3.5-128B, Mistral-Small-4-119B, Gemma-3-27B). 40 RPM, 1000 RPD, 131K context. Auto-discovery of 140+ upstream models. Playground SSO fix (session-based auth for `/v1/*` routes). Improved error handling for non-JSON upstream responses (Cloudflare HTML 502 pages). Provider timeout reduced 60→45s.
>
> **v1.14.2 highlights (2026-05-21):** 💾 **Enterprise backup + data persistence fix** — Root-cause fix for analytics data loss: Redis eviction policy changed from `allkeys-lru` → `volatile-lru` (only evicts TTL-bearing keys); max memory 256 MB → 512 MB; `appendfsync everysec` enabled. History buckets now have explicit TTLs (7d for 5-min, 30d for hourly). New enterprise backup system: full and incremental backups to OCI Object Storage (S3-compatible) with automatic daily/weekly scheduling, 7/90-day retention enforcement, 10 GB quota monitoring, and a full management UI at `/backup`. Analytics window selector (1h/4h/24h/7d/30d/90d) now works via hourly rollup buckets. Experience-based intra-provider model reordering using historical error rates and latency.
>
> **v1.14.1 highlights (2026-05-01):** 🔒 **Security hardening + 21-issue audit** — XSS fix in `/auth/pending` (query-param `email` now HTML-escaped); SSRF protection in provider URL discovery; admin-only guards added to `GET /api/providers`, `GET /settings/routing`, and `GET /settings/cache`; 4 MB request body size limit; cache key collision fixed (now includes `max_tokens`, `stop`, `top_p`); Pydantic field bounds on all request models (`temperature` ∈ [0,2], `max_tokens` ∈ [1,128k], `model` max 256 chars); `redis.keys()` → `scan_iter` everywhere in analytics; `mget` batching for routing config.
>
> **v1.14.0 highlights:** 📱 **Installable PWA** — Arbiter now installs to your phone's home-screen / desktop dock with its own icon, splash, and offline page (Android Chrome / iOS Safari / Edge / Samsung Internet).  Service worker with 3-tier strategy (network-only for APIs, stale-while-revalidate for static, network-first for HTML).  • 🛡️ **Tiered Cloudflare cache strategy**: sensitive routes (incl. all HTML pages) emit `no-store + Cloudflare-CDN-Cache-Control: no-store + Vary: Cookie`; static assets get `public, max-age=86400` at the edge — fixes the "logged-in email leaks to incognito visitors" bug while restoring CDN performance for CSS/JS/icons.  • 🎨 **Settings UI overhaul** — Models tab gets ranked priority pills + provider colour dots, Image Gen pulls model/size catalog live from `/v1/images/models`, Cache tab redesigned with KPI strip + effectiveness donut + config card.  • 📐 Full responsive layout for mobile + safe-area support for iOS notched devices.
>
> **v1.13.3 highlights:** 🆕 **Per-key tier tagging** (`#paid` / `#free` suffix in env vars) — Gemini paid keys reserved for frontier models (3.1-pro-preview), free keys for everyday traffic. Catalog reordered to prioritize gemini-3.1-flash-lite as the top free model.
>
> **v1.11.2 highlights:** ✨ **Ollama Cloud added** as an 11th provider (6 free :cloud-tagged MoE models — gpt-oss, deepseek-v3.1, glm-4.6, qwen3-coder, minimax-m2) · explicit model selection now pins exactly (was silently falling back to default) · HuggingFace no-longer-silent model rewrite · Pollinations User-Agent fix (Cloudflare was returning 502 to bare `httpx`) · Routeway 503 no longer cooldown-cascades the whole key · model-hierarchy cleanup: removed ~12 consistently-broken models across Gemini/Groq/OpenRouter/Cloudflare/Cerebras/HuggingFace/Pollinations/Routeway (see CHANGELOG for the pruning table).
>
> **v1.11.1 highlights:** Free-tier-first strategy across all providers (Routeway now seeds with 15 `:free` models) · Playground "⚡ Auto (Smart Route)" option · 502 → 503 for cooldown-exhaustion with actionable error messages · middleware-stack ordering fix for SSO sessions · Routeway/Z.ai visible in Settings UI · Analytics page removed (redundant with Dashboard) · admin-gated mutating endpoints (custom providers, model toggles) · `_wants_json` chained-comparison fix.
>
> **v1.11.0 highlights:** Routeway provider · add-any-provider from the UI · dynamic model discovery with per-model enable/disable · Google SSO with admin approval · hardened middleware stack (CSP, SSRF, session revocation, log redaction).

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
- **Z.ai / Zhipu AI** (3 models, 32K–128K context) — ~10 RPM free tier, GLM-4.7/4.5-Flash free
- **HuggingFace** (4 models, 8K–32K context) — Limited free credits
- **Pollinations.ai** (11 models, 32K context) — Free tier with API key (enter.pollinations.ai)
- **Lightning.ai LitAI** (5 models, 128K–256K context) — Nemotron 3 Super (256K), gpt-oss-120B, DeepSeek V3.1 (164K); ~37M token welcome credit then $0.09–$0.52/M tokens
- **Routeway** (192-model unified gateway — 15 `:free` models seeded by default) — Llama 3.3 70B `:free`, GPT-OSS-120B `:free`, Kimi K2 `:free` (256K ctx), MiniMax M2 `:free`, Devstral `:free`, Gemma 4 31B `:free`, Nemotron Nano `:free` etc. Paid fallback (GPT-4o, Claude 3.5, DeepSeek) only on explicit opt-in.
- **NVIDIA NIM** (free models via build.nvidia.com, 131K context) — 40 RPM, 1,000 RPD
  - `nvidia/nemotron-3-super-120b-a12b` — 120B MoE flagship (fastest, best quality)
  - `meta/llama-3.3-70b-instruct` — 70B dense (fast, reliable)
  - `mistralai/mistral-medium-3.5-128b` — 128B dense (strong reasoning + coding)
  - `mistralai/mistral-small-4-119b-2603` — 119B MoE (fast, code-oriented)
  - *(`google/gemma-3-27b-it` retired by NVIDIA 2026-05-12 — removed in v1.21.0)*
  - Auto-discovery of 140+ upstream models via refresh
- **Multi-account support** — Unlimited accounts per provider with intelligent scoring
- **Additive capacity** — Overlapping models (e.g., GLM on both Cerebras+Z.ai) sum their rate limits
- **Per-key tier tagging** *(v1.13.3+)* — Suffix any key with `#paid` to mark it
  as a billing-enabled account. The router gates frontier paid-only models
  (`gemini-3.1-pro-preview`, `gemini-3-pro-preview`, `gemini-2.5-pro`) to
  `#paid` keys and skips free keys for those requests, so you never burn free
  quota on paid models. Free keys continue to rotate normally for everything
  else. Example: `GEMINI_API_KEYS=KEY1#paid,KEY2,KEY3`.

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
  ├─ Try gemini-3.1-flash-lite with account 1 ✓ Success → return
  │
  └─ (if account 1 hits 429): Try account 2, account 3, etc.
       └─ (if all accounts hit TPM): Try gemini-3-flash-preview
            └─ (if all Gemini models fail): Try Groq
                 ├─ Try llama-3.1-8b-instant → llama-3.3-70b-versatile → qwen3-32b
                 └─ (if Groq fails): Try Cloudflare → Cerebras → OpenRouter
```

### ✅ Semantic & Exact-Match Caching
- **Cache deterministic requests** (temperature ≤ 0.3) to Redis
- **SHA-256 keyed** on `(model, messages, max_tokens, stop, top_p, tools, tool_choice, response_format, seed)` — every parameter that affects output is included so requests with different sampling, tool, or JSON-mode settings never collide *(tool/`response_format`/`seed` added v1.21.0)*
- Configurable TTL (default 1 hour)
- Transparent — client receives cached response instantly

### ✅ Real-Time Observability & API Documentation
- **Dashboard** (`/dashboard`) — Dark-themed web UI, auto-refreshing every 10s:
  - **KPIs**: Total requests, success rate, cache hit rate
  - **Per-provider table**: Status, active accounts, request counts, success rates
  - **Per-account table**: Availability score (0–100%), RPM/TPM/daily usage bars
  - **Color-coded health**: 🟢 Healthy → 🟡 Degraded → 🔴 Unavailable
- **Analytics Dashboard** (`/analytics`) — Deep usage analytics with animated charts:
  - **6 KPI cards**: total requests, success rate, cache hit rate, avg latency, tokens, active keys
  - **Request history**: 4-hour area chart in 5-min buckets (requests/success/errors)
  - **Provider distribution**: donut chart with brand colors
  - **Latency ranking**: horizontal bar chart per provider
  - **Token consumption**: horizontal bar chart per provider
  - **Key Health Matrix**: live RPM/TPM/Daily quota gauges per API key with color thresholds
  - **Provider & model tables**: success rates, latency, tokens/req, error analysis
  - **Reset button** — clear all stats counters
- **Developer Docs** (`/developer`) — Full developer reference with Quick Start, API endpoints, SDK examples, gateway token guide, and routing explanation
- **Swagger UI** (`/docs`) — Standard OpenAPI documentation
- **JSON Stats** (`/dashboard/stats`) — Programmatic access to metrics

### ✅ API Authentication & Security
- **Gateway-level authentication** — Optional `Authorization: Bearer <key>` validation
- **Multi-key support** — Multiple gateway API keys (comma-separated in `.env`)
- **Dynamic gateway token management** (`/settings` → Gateway Keys tab):
  - Create named tokens with optional expiry dates from the admin UI
  - Tokens become active immediately — no restart required
  - Revoke or delete individual tokens without affecting others
  - Env-var keys and UI-created tokens coexist seamlessly
  - **API**: `GET/POST /api/gateway/tokens`, `DELETE/PATCH /api/gateway/tokens/{id}`
- **Cloudflare Access integration** — JWT validation via Cloudflare Zero Trust
- **JWKS caching** — 1-hour TTL for performance
- **No API keys in logs** — Keys hashed; only first 4 chars logged
- **Secure header transmission** — All secrets sent via secure channels

### ✅ Runtime API Key Management (no restart)
- **Add / remove keys at runtime** — written directly to `.env`; takes effect immediately without a restart
- **Enable / disable providers** — take a provider offline and bring it back without restarting Docker
- **Test-before-enable** — toggling a provider on runs a connectivity probe first; if the key is broken the toggle auto-reverts with an error message
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
- **Analytics** (`/analytics`) — 6 KPI cards, 5 charts, key health matrix, provider/model tables
- **Developer Docs** (`/developer`) — 6-tab layout: Quick Start, Authentication, Endpoints, Routing, SDK Examples, Gateway Tokens
- **Settings** (`/settings`) — API Keys, routing, model overrides, image gen, Cloudflare Workers, cache, **Gateway Keys**
- **Image Generation** (`/images`) — dedicated page with prompt, model selector, count, size, and seed controls
- **Playground** (`/playground`) — vendor + model drill-down with free/paid badges and rate limit display

### ✅ Cloudflare Workers AI Manager
- **Create Workers** — Provision new Workers AI instances from the gateway
- **List Models** — View available Cloudflare models
- **List/Delete Workers** — Manage deployed Workers with provisioning-state awareness (120s grace period for CF API propagation)
- **Permission validation** — `POST /cloudflare/validate` returns a full permission matrix showing which of Scripts Read / Workers AI Execute / Subdomain access your token has
- **Admin endpoints** — `/cloudflare/workers/*` routes

### ✅ Chat Playground (`/playground`)
- **Interactive chat UI** for testing every endpoint — CF workers and all gateway providers
- **Two-level model selection** — choose vendor, then pick a specific model with full metadata:
  - **Free / paid badges** per model
  - **Rate limit display** — RPM, TPM, RPD shown on selection
  - **Context window** size per model
  - Models loaded live from `/api/models/info` (only configured vendors shown)
- **Endpoint selector** grouped by type: Gateway Providers, Cloudflare Workers
- **Per-endpoint routing**: CF workers route through the gateway (`cfworker/{name}`), providers go through `/v1/chat/completions`
- **Config panel** — system prompt, temperature, max tokens; latency badge on each response
- **Markdown rendering** — assistant replies rendered as full GFM markdown (headers, code blocks, tables, lists, links)

### ✅ Real-Time Log Viewer (`/logs`)
- **In-memory log buffer** — last 5,000 records from all modules captured automatically
- **Filters**: level, logger name, full-text search, time range (since/until), tail, limit
- **Auto-refresh** (2 s / 5 s / 10 s / 30 s), sort newest/oldest, download as `.txt`, copy to clipboard
- **Expansion state preserved on refresh** — expanded log rows stay expanded even as new records load
- **API**: `GET /logs/records`, `GET /logs/loggers`, `DELETE /logs/clear`

### ✅ CF Workers — Gateway Routing
- **`cfworker/{name}` model prefix** — send `model: cfworker/my-worker` to `/v1/chat/completions` to proxy directly to that worker's `workers.dev` URL
- **Virtual models in `/v1/models`** — active CF workers (`cfworker/{name}`) appear in the model list for easy selection
- **Stale-delete fix** — Redis deletion marker (120 s TTL) suppresses workers during Cloudflare API propagation delay after deletion

### ✅ API Key Validation (all providers)
- **Auto-validation on key add** — every key is tested immediately after being saved to the gateway
- **Cloudflare permission matrix** — shows which of the three required permissions (Scripts Read, Workers AI Execute, Subdomain) are available for your token
- **Other providers** — latency probe via `POST /api/providers/{name}/test`, reports pass/fail and round-trip time
- **Manual validate button** — re-test any configured Cloudflare key on demand

### ✅ In-Memory Redis Fallback
Gateway starts successfully **even without Redis**:
- Caching disabled but routing functional
- Rate-limit tracking in memory (per-process, not distributed)
- Perfect for local development

### ✅ Custom Providers from the UI *(v1.11)*
- **Templates** for OpenAI, Anthropic, DeepSeek, Together, Fireworks, Mistral, Perplexity, or "fully custom"
- **Any OpenAI-compatible** (`/chat/completions`) or **Anthropic-compatible** (`/messages`) endpoint works out of the box
- Configure from **Settings → Custom Providers** — no code, no restart; providers are hot-loaded
- Built-in **SSRF guard** rejects `localhost`, private IPs, and cloud metadata endpoints
- API keys persisted to `.env` as `CUSTOM_PROVIDER_<NAME>_KEY`; config lives in `data/arbiter_state.json`

### ✅ Dynamic Model Discovery *(v1.11)*
- **Refresh from provider** button on each provider in Settings → Models calls the live `/models` endpoint
- Discovered **free-tier models auto-enable**; **paid models stay disabled** until an admin enables them (quota safety)
- Per-model state persisted on disk and respected by `/v1/models` and the router
- Manual refresh only — **no Redis, no periodic polling**

### ✅ Google SSO + Admin Approval *(v1.11)*
- **Google OAuth 2.0** sign-in for the dashboard; `/v1/*` API still uses Bearer tokens
- First sign-in from `ADMIN_EMAIL` is auto-approved as admin; everyone else lands in **Pending** until approved from `/users`
- Rejecting or deleting a user **revokes their session immediately** (server-side `session_version`)
- Hardened middleware: CSP, `X-Frame-Options: DENY`, CORS allowlist (wildcard forbidden), bearer-token log redaction

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
                  │  /dashboard · /developer · /settings  │
                  │  /playground · /logs                │
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
│  NVIDIA │ Gemini │ Groq │ Cerebras │ Z.ai │ Cloudflare │
│  OpenRouter │ Cohere │ HuggingFace │ Pollinations │     │
│  Ollama │ Routeway │ + Custom OpenAI-compatible          │
└──────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  External Provider APIs (12 vendors, 60+ models)         │
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

### 📱 Install on mobile / desktop (PWA)

Arbiter is a Progressive Web App. To install on your **Poco F7 / any Android**:

1. Open `https://your-arbiter-host/dashboard` in Chrome / Edge / Samsung Internet.
2. Tap the address-bar menu → **Install app** (or use the **Install app** button at the bottom of the sidebar).
3. Arbiter launches in standalone mode with its own icon, splash, theme-coloured status bar, and an offline page if the network drops.

On **iOS Safari**: Share menu → *Add to Home Screen*. On **Desktop Chrome / Edge**: address-bar install icon. The service worker keeps `/static/*` warm via stale-while-revalidate while keeping API and HTML responses uncached for security.

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
# Tag a key with `#paid` to enable paid-only models (gemini-3.1-pro-preview,
# gemini-3-pro-preview, gemini-2.5-pro). Untagged keys default to free tier.
# Example: 1 paid + 2 free accounts
GEMINI_API_KEYS=your-paid-key#paid,your-free-key-1,your-free-key-2

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

# Z.ai / Zhipu AI  (https://z.ai/manage-apikey)  — GLM-4.7-Flash is free!
ZAI_API_KEYS=your-zai-api-key

# HuggingFace  (https://huggingface.co/settings/tokens)
HUGGINGFACE_API_KEYS=hf_your-token-here

# Pollinations.ai  (free key at enter.pollinations.ai — sk_... or pk_...)
POLLINATIONS_API_KEYS=sk_your-pollinations-key
```

**Per-key tier tagging** (`#paid` suffix, v1.13.3+) — currently honored by the
Gemini provider. The router checks `provider.paid_models` and only selects
keys tagged `#paid` for those models. Free keys are skipped (no 429 burn on
free quota). Untagged keys default to `#free` and remain fully usable for the
free fallback chain.

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
| `ZAI_API_KEYS` | (empty) | Comma-separated Z.ai / Zhipu API keys |
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
| **Z.ai** | ~10 | 200K | 1K | GLM-4.7-Flash free ($0) |
| **HuggingFace** | 10 | 50K | 500 | Limited free credits |
| **Pollinations** | 5 | 100K | 1K | Free tier — key from enter.pollinations.ai |

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
  "stream": false,
  "fallback": "chain",
  "metadata": {"priority": "quality"}
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
- `model` (string, required) — Model ID (e.g., `gemini-2.5-flash`, `llama-3.3-70b-versatile:free`) or `"auto"`
- `messages` (array, required) — Message objects with `role` and `content` (`content` may be a string or a multimodal content array)
- `temperature` (float, default 0.7) — Sampling temperature (0.0–2.0). Values ≤ 0.3 are cached.
- `top_p` (float, default 1.0) — Nucleus sampling (0.0–1.0)
- `max_tokens` (integer, optional) — Max tokens to generate
- `stop` (array, optional) — Stop sequences
- `stream` (boolean, default false) — Enable SSE streaming (see [Streaming](#streaming) below)
- `fallback` (string, optional) — `none` | `same_provider` | `chain` — cross-provider fallback policy when pinning a specific model
- `metadata` (object, optional) — Routing hints: `arbiter_intent` (code|reasoning|creative|fast|balanced), `priority` (speed|quality|balanced), `prefer_provider` (provider name)

**Query parameters:** `?vendor=<name>` pins a provider; `?force_model=<id>` overrides model selection.

**Errors:**

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_request_error` | Bad request (missing field, invalid format) |
| 401 | `authentication_error` | Missing/invalid API key (if `GATEWAY_API_KEY` set) |
| 429 | `rate_limit_error` | All providers/accounts exhausted |
| 500 | `server_error` | Internal gateway error |

---

## Streaming

Arbiter supports **Server-Sent Events (SSE)** streaming for all 12 providers. Set `"stream": true` in any `/v1/chat/completions` request.

```bash
curl -sS -N \
  -H "Authorization: Bearer YOUR_GATEWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello!"}],"stream":true}' \
  http://localhost:8000/v1/chat/completions
```

The OpenAI Python/JS SDKs work without any changes — pass `stream=True` to `.create()` or use `.stream()`. LangChain `streaming=True` also works.

Each `data:` line carries a standard `chat.completion.chunk` object. The stream ends with `data: [DONE]`. An SSE comment ``: arbiter-model-used: provider/model`` identifies the serving provider.

> `cfworker/*` models do **not** support streaming and return HTTP 400 if `stream: true`.

See [USERGUIDE.md](USERGUIDE.md#streaming) for full documentation, including fallback behaviour, caching semantics, and JS/Python streaming examples.

---

### GET `/v1/models`

List available models. Includes standard provider models **plus** virtual models for active CF Workers and Modal deployments.

**Response:**
```json
{
  "object": "list",
  "data": [
    {"id": "gemini-2.5-flash-lite", "object": "model", "created": 1700000000, "owned_by": "gemini"},
    {"id": "llama-3.3-70b-instruct:free", "object": "model", "created": 1700000000, "owned_by": "openrouter"},
    {"id": "cfworker/my-worker", "object": "model", "created": 1700000000, "owned_by": "cloudflare-worker"}
  ]
}
```

Use `model: cfworker/<name>` to route a request directly to a deployed CF Worker.

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
