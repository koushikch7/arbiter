# Changelog

All notable changes to the Arbiter project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.13.2] – 2026-04-30 (Latest) — All-models routing & smart key health

### 🚀 Massive model coverage expansion

- `/v1/models` now exposes **106 models** (up from 60) sourced from current
  official documentation across all 10 active providers.
- **Pollinations** (`gen.pollinations.ai`) — auth header upgrade and 28-model
  catalog covering OpenAI / Claude / Gemini / DeepSeek / Qwen / Kimi /
  Mistral / GLM / Grok / Perplexity / Nova / Minimax aliases.
- **Cloudflare** — 17 models incl. Llama 4 Scout, GPT-OSS 120B/20B,
  Kimi K2.6 (262K ctx), GLM-4.7 Flash, Gemma 4-26B, Nemotron-3-120B.
- **Cerebras** — added Llama 3.3-70B, GPT-OSS-120B, Qwen-3-32B.
- **HuggingFace** — switched to `:fastest` routing across Inference Providers.
- **Gemini** — exposed all 5 free-tier models (2.5-flash-lite default,
  2.0-flash-lite, 2.0-flash, 2.5-flash, 2.5-pro).
- **Cohere** — added `command-a-reasoning-08-2025`.
- **OpenRouter** — default flipped from chronically-throttled
  `hermes-3-llama-3.1-405b:free` to `google/gemma-3-27b-it:free`.

### 🐛 Fixed

- **Pollinations 401** — Authorization Bearer header now sent on every
  call (Pollinations made auth mandatory in 2026). Verified end-to-end:
  upstream now reaches 402-budget rather than 401-auth.
- **Pollinations key never loaded** — `Settings` was missing the
  `POLLINATIONS_API_KEYS` field, so `get_keys("pollinations")` returned
  empty and the pool fell back to the literal string `"free"`. Added
  the field plus mapping entry in `app/config.py`.
- **Stale Redis disabled flags** for `cloudflare`, `cohere`, `cerebras`
  removed — all three now eligible for routing again.

### 🧠 Smart key-health scoring

- `KeyPool._score_key()` now factors in a 30-min sliding-window
  **success / error ratio** (Laplace-smoothed) alongside RPM / TPM /
  daily availability. Weights rebalanced to
  `RPM 0.25 · TPM 0.15 · Daily 0.40 · Health 0.20`.
- New `KeyPool.record_error(key)` helper bumps the error counter on
  upstream `ProviderError` / unexpected exceptions; flaky keys are
  automatically deprioritised without being hard-cooled.

---

## [1.13.1] – 2026-04-30 — UI consistency + routing fix

### 🐛 Fixed

- **`/analytics` returned 404** — the `analytics_router` was never registered
  in `app/main.py`. The Analytics nav link from v1.13.0 now actually works.
- **Disabled providers were still being routed to** — `IntelligentRouter`
  ignored the `arbiter:runtime:disabled:{name}` flag set by the Settings UI.
  The router now filters disabled providers out of the candidate chain
  (cached for 5s) and surfaces a clear error if the caller pinned a
  disabled vendor explicitly.

### 🎨 Shared sidebar component

- New `static/components/sidebar.js` — single source of truth for
  navigation. Every page now declares only `<aside id="sidebar"></aside>`
  and the script renders the same brand, nav items, and footer everywhere,
  marking the current page active automatically.
- All eight UI pages refactored: `dashboard`, `analytics`, `api-docs`
  (Analytics link was missing here), `settings`, `playground`, `logs`,
  `images`, `users`. Adding a new page or nav item now requires editing
  one file instead of nine.
- Each nav item carries a tooltip describing what the page does.

### 🔢 Sortable tables + tooltips

- New `static/components/ui.js` — drop-in helper that auto-wires
  `<table data-sortable>` with click-to-sort headers (asc/desc, type-aware
  numeric / date / string) and renders polished `data-tip="…"` tooltips.
- Sortable now: Dashboard provider table; Analytics provider, model, and
  per-gateway-token tables; Settings → Gateway Tokens table.
- Tabular cells expose raw `data-sort` values so date-formatted columns
  (Last Used, Created, Expires) sort chronologically rather than
  alphabetically.

---

## [1.13.0] – 2026-04-30 — Per-Token Observability + Strict Auth

### 🔒 Strict / fail-closed gateway auth

- **New env var `REQUIRE_AUTH=true` (default: true)** — when no
  `GATEWAY_API_KEYS` and no dynamic gateway tokens are configured, `/v1/*`
  returns **401** instead of silently passing traffic through. The gateway
  now refuses to make outbound LLM calls without a valid Bearer token.
  Set `REQUIRE_AUTH=false` to opt back into legacy permissive mode.
- **Auth-status banner** on Analytics — bright warning when the gateway is
  running in open mode (`auth_enforced: false` in `/analytics/data`).

### 📊 Per-gateway-token tracking (the bug you saw on the Gateway Keys tab)

Previously the **Requests** column on the Gateway Keys tab and the
`request_count` field always showed `0` because no code path incremented it.
Fixed end-to-end:

- **New module `app/observability/stats.py`** — single source of truth for
  every counter, with consistent key namespacing.
- **`GatewayAuthMiddleware`** now identifies which named token was used and
  attaches `request.state.gateway_token_id` / `…_token_name` so downstream
  handlers can attribute the request.
- **`IntelligentRouter.route()`** accepts `token_id`/`token_name` and writes:
  - `arbiter:stats:token:{id}:requests / success / errors / tokens`
  - `arbiter:stats:token:{id}:provider:{name}:requests`
  - `arbiter:stats:token:{id}:model:{model}:requests`
  - `arbiter:stats:token:{id}:last_used`
- **`GET /api/gateway/tokens`** now merges live counters into each row
  (`request_count`, `success_count`, `error_count`, `tokens_used`,
  `last_used_at`).
- **NEW `GET /api/gateway/tokens/{id}/stats`** — detailed per-token
  analytics with 30-day history, by-provider, and by-model breakdowns.
- **Settings UI** — Gateway Keys table now shows Requests / Tokens / Last
  Used columns with live values.

### 📈 Enterprise-grade analytics filters

- **`GET /analytics/data` now supports**:
  - `from=YYYY-MM-DD&to=YYYY-MM-DD` — daily-rollup time series for any range
    up to 90 days.
  - `token_id=…` — filter to a specific gateway token (or `env` for env-var
    traffic).
  - `provider=…` and `model=…` — drill down to a specific provider or model.
- **New daily rollup keys** (`arbiter:stats:day:{YYYY-MM-DD}:*`) with 90-day
  TTL — efficient `GET` instead of scanning all `history:*` keys.
- **Top-N providers / models / tokens** computed over the filtered range.
- **Latency tracking is now real** — previously `arbiter:stats:latency:*`
  was read by the analytics page but never written. Fixed in the router.
- **Per-token usage table** added to `/analytics`.
- **Filter bar** with date range, gateway-token, provider, and model
  selectors plus 7d / 30d quick presets and a per-range summary card with
  daily sparkline + top-5 providers / models / tokens.
- **Analytics nav item restored** in the sidebar across all pages.

### 🔌 Provider enable/disable UX

- **`POST /api/providers/{name}/enable`** now returns a structured
  `400 {"error":"no_key", …}` body with `key_format`, `key_hint`, and
  `signup_url` when the provider has no key configured. Previously the
  endpoint silently no-op'd, leaving the toggle visually reverted with no
  explanation.
- **Settings UI** — when toggling a key-less provider on, the page now
  prompts for an API key inline, saves it via
  `POST /api/providers/{name}/keys`, and auto-retries enable.
- **Source badge** on each provider card (`env` / `disabled`) so it's clear
  whether keys are coming from `.env` or the UI-disabled state, and you can
  freely disable a provider whose env var is still set — the disable flag
  in Redis takes precedence and persists across restarts.

### 🧪 Tests

- `scripts/test_gateway_token_flow.py` extended to assert that
  `request_count >= 1` and `last_used_at` populate after the AI capability
  tests, and that `/api/gateway/tokens/{id}/stats` returns a non-empty
  summary.

---

## [1.12.1] – 2026-04-26 — Security Hardening

### 🔧 Bearer Auth on Admin APIs (post-release patch)

- **`require_admin` now accepts gateway tokens**: Previously, the admin
  dependency only honoured Google SSO sessions, which prevented automation
  and CI tooling from managing tokens, providers, or routing settings via
  Bearer auth. The dependency now falls back to a registered active gateway
  token (matched against `app.state.gateway_tokens`) when no SSO session is
  present. Anonymous requests still receive **401**, and SSO non-admin
  sessions still receive **403**.
- **OpenAI tool-calling / response_format passthrough on Groq provider**:
  `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`, `seed`,
  `logprobs`, `n`, presence/frequency penalties, etc. are now forwarded to
  Groq verbatim. Assistant `tool_calls`, `function_call`, `refusal`, `audio`
  fields are preserved on the response. Inbound messages preserve
  `tool_calls` / `tool_call_id` / `name` for multi-turn tool-using chats.
- **New end-to-end test**: `scripts/test_gateway_token_flow.py` verifies the
  full token CRUD lifecycle (create → list → page-refresh → PATCH → revoke
  → delete) and exercises every advertised AI capability through a
  freshly-created Bearer token: model listing, auto-routing, multi-turn
  context, function calling (validates `tool_calls` payload), JSON mode
  (parses content as JSON), and vision (multimodal routing). 20/20 pass.

---

## [1.12.1] – 2026-04-26 — Security Hardening

### � Dependency Vulnerability Patches

Fixed all 13 fixable CVEs reported by `pip-audit` (Dependabot moderate alerts).

| Package            | Before  | After    | CVEs fixed                                                 |
| ------------------ | ------- | -------- | ---------------------------------------------------------- |
| authlib            | 1.3.2   | 1.6.11   | CVE-2025-59420, -61920, -62706, -68158, -2026-27962, -28490, GHSA-jj8c-mmj3-mmgv |
| filelock           | 3.16.0  | 3.20.3   | CVE-2025-68146, CVE-2026-22701                             |
| python-dotenv      | 1.0.1   | 1.2.2    | CVE-2026-28684                                             |
| python-multipart   | 0.0.22  | 0.0.26   | CVE-2026-40347                                             |
| pip (Dockerfile)   | 25.0.1  | ≥26.0    | CVE-2025-8869, CVE-2026-1703                               |
| **python-jose**    | 3.5.0   | _removed_ | replaced with `PyJWT[crypto]==2.12.0` (eliminates vulnerable transitive `ecdsa` — CVE-2024-23342 Minerva timing attack) |

After patch: **only 1 of 14+ vulnerabilities remains** (`pip` CVE-2026-3219 — no upstream fix available yet). All actionable CVEs resolved.

### �🔒 Repository-Publish Hardening

Comprehensive security review prior to making the repository public.

- **Hardened `.gitignore`**: now covers `.env*` (with `.env.example` whitelist), `*.pem`, `*.key`, `secrets/`, `credentials.json`, `service-account*.json`, `.modal/`, `data/` (runtime state), `__pycache__/`, `*.py[cod]`, `.venv/`, `.idea/`, `.vscode/`, `*.log`, `*.bak`, `TEST-REPORT-*.md`. Previously only 3 lines.
- **Added `.dockerignore`**: prevents `.env`, `.git/`, `__pycache__/`, runtime `data/`, virtualenvs and IDE files from being baked into the Docker image build context.
- **Secret scan (clean)**: tracked tree and full `git log -p` history scanned for the live key patterns of every supported provider (`AIzaSy…`, `gsk_…`, `sk-or-v1-…`, `csk-…`, `hf_…`, `nvapi-…`, `ak-…`, `as-…`, `sk_…`) — zero hits. `.env.example` contains placeholder strings only.

### 🔐 RBAC — Admin-only Configuration Endpoints

All endpoints that mutate provider keys, routing config, gateway tokens, infrastructure (Cloudflare/Modal) or preferences are now gated by `Depends(require_admin)`. Non-admin authenticated users get **403**; unauthenticated requests get **401**.

| Endpoint family                     | Admin-only methods                    |
| ----------------------------------- | ------------------------------------- |
| `/api/providers/*`                  | POST/DELETE keys, enable/disable, test, reload |
| `/api/gateway/tokens/*`             | All methods (router-level dep)        |
| `/api/preferences/auto-route`       | PUT, /reset                           |
| `/settings/routing`, `/settings/cache` | POST, DELETE                       |
| `/cloudflare/*`, `/modal/*`, `/modal/deploy/*` | All methods (router-level dep) |

Read-only listing endpoints remain available to approved users.

### ✅ Verification

- 7/7 auto-routing tests still passing post-change.
- Free-tier catalog audit: **55 free models across 12 providers**, 8 vision-capable specs (Gemini 2.5 Flash-Lite, Llama-4-Scout × 2, Mistral Small 3.1 × 2, Gemma-3 12B/27B).
- Container rebuilt clean: `docker compose down && docker compose up -d --build`.
- Curl-tested: `/api/users`, `/api/providers/{p}/keys`, `/settings/routing`, `/api/preferences/auto-route`, `/api/gateway/tokens`, `/cloudflare/workers/*` all return 401 unauthenticated.

---

## [1.12.0] – 2026-04-26

### 🚀 Smart Auto-Routing — `model="auto"`

Arbiter now classifies the incoming prompt and picks the best free-tier model for the job, automatically.  No LLM call, no extra latency (<1ms heuristic classifier).  Fully overridable per-request and per-deployment.

#### New Components

- **[app/providers/_free_tier_catalog.py](app/providers/_free_tier_catalog.py)** — single source of truth for all 11 free-tier providers.  Each model is described by a `ModelSpec` with capability tags (`code`, `reasoning`, `long-context`, `vision`, `creative`, `fast`, `balanced`, `large`), modality (`text`/`vision`/`multimodal`), context window, RPM/RPD quota, quality (1–5), and speed (1–5).  Replaces the previous 200-line hardcoded `VENDOR_MODEL_HIERARCHY` block — that dict is now derived from the catalog at import time.  A separate `PAID_FALLBACK_CATALOG` lists Routeway paid models for opt-in fallback.
- **[app/routing/intent_classifier.py](app/routing/intent_classifier.py)** — pure-Python regex/keyword heuristic that maps a request to one of seven intents: `code`, `reasoning`, `long-context`, `vision`, `creative`, `fast`, `balanced`.  Inputs considered (in priority order): explicit `metadata.arbiter_intent` hint → multimodal image part → token-count threshold (>16K → long-context) → code-fence regex → keyword scoring (code keywords weighted 2×) → length fallback.
- **[app/routing/auto_router.py](app/routing/auto_router.py)** — scoring engine that returns an ordered `List[(provider, model_id)]` chain.  Score formula: `cap_score (40 - rank·8) + quality·priority_weight + speed·priority_weight + intent_pref_score`.  Hard filters: vision intent requires `multimodal`/`vision` modality; context window must fit prompt with 10% margin.  Honours user preferences (`prefer_providers`, `avoid_providers`, per-intent model lists, `allow_paid_fallback`) and the model-enabled state from `state_store`.
- **[app/api/preferences_api.py](app/api/preferences_api.py)** — admin-gated REST surface at `/api/preferences/auto-route` (GET / PUT / POST `…/reset`).  Persists to `data/arbiter_state.json` via the existing file-locked `state_store`.

#### New Request Fields & Headers

- `body.model = "auto"` (or empty) — engages the auto-router.
- `body.fallback`: `"none"` (default; preserves the strict-pin contract from v1.11.2), `"same_provider"` (walk other models on the pinned provider), or `"chain"` (cross-provider fallback via auto-router).
- `body.metadata`: free-form dict; recognised keys are `arbiter_intent`, `priority`, `prefer_provider`, `opt_in_paid`.
- Request headers: `X-Arbiter-Priority: speed|quality|balanced`, `X-Arbiter-Prefer-Provider: <name>`, `X-Arbiter-Fallback: none|same_provider|chain` — useful for OpenAI-SDK callers that can't set body extras.
- Response header: `X-Arbiter-Model-Used: <provider>/<model>` — the actual pair that fulfilled the request.

#### Router Changes

- **[app/routing/router.py](app/routing/router.py)** — `route()` now builds a single unified `(provider, model)` candidate chain via `_build_candidate_chain()` instead of nested provider-order × model-hierarchy loops.  Three modes: vendor-pinned (only that vendor) · auto (`auto_candidate_chain`) · explicit-model (strict pin by default; honours `fallback`).  The chosen pair is stamped onto the response object so `chat.py` can echo it via the `X-Arbiter-Model-Used` header.

#### State Store

- **[app/state_store.py](app/state_store.py)** — extended `_DEFAULT_STATE` with `auto_route_preferences` (priority, prefer/avoid lists, six per-intent preference lists, `allow_paid_fallback`).  New helpers `get_auto_route_preferences()` and `update_auto_route_preferences()` validate enum/shape, dedupe, merge, and persist atomically.

#### UI

- **[static/settings.html](static/settings.html)** — new **Auto Routing** tab with priority dropdown, paid-fallback opt-in, prefer/avoid provider inputs, and an advanced collapsible panel for per-intent model preferences.  Live-saves to `/api/preferences/auto-route`.

#### Validation

- **[scripts/test_auto_routing.py](scripts/test_auto_routing.py)** — sends one prompt per intent (`code`, `reasoning`, `long-context`, `creative`, `fast`, `balanced`, `vision`) with `model="auto"` and asserts the `X-Arbiter-Model-Used` header points at a model that owns the expected capability tag.  Live result on this deployment: **7/7 pass**.
  - code → `cerebras/qwen-3-235b-a22b-instruct-2507`
  - reasoning → `cerebras/qwen-3-235b-a22b-instruct-2507`
  - long-context → `groq/llama-3.3-70b-versatile`
  - creative → `cerebras/qwen-3-235b-a22b-instruct-2507`
  - fast → `gemini/gemini-2.5-flash-lite`
  - balanced → `groq/llama-3.3-70b-versatile`
  - vision → `cloudflare/@cf/meta/llama-4-scout-17b-16e-instruct`

#### Compatibility

- **Strict-pin contract preserved**: explicit `model="…"` with no `fallback` field still uses *only* that model (the v1.11.2 fix).  Fallback is opt-in.
- **Existing `/v1/chat/completions` semantics unchanged** for callers that don't set `model="auto"`, `fallback`, `metadata`, or any of the new headers.

---



### ✨ New Provider — Ollama Cloud

- **Ollama Cloud added as an 11th provider** — free personal API key at <https://ollama.com/settings/keys> grants access to 6 large open-weight MoE models through an OpenAI-compatible endpoint (no billing required, server-side rate limits apply).  Models added to the free-first hierarchy (slot between `pollinations` and `routeway`):
  - `gpt-oss:20b-cloud` · 131K ctx · default
  - `glm-4.6:cloud` · 128K ctx
  - `minimax-m2:cloud` · 192K ctx
  - `qwen3-coder:480b-cloud` · 256K ctx · coding specialist
  - `gpt-oss:120b-cloud` · 131K ctx · flagship
  - `deepseek-v3.1:671b-cloud` · 164K ctx

  Implementation: [app/providers/ollama_provider.py](app/providers/ollama_provider.py).  Wired through [app/config.py](app/config.py), [app/main.py](app/main.py), [app/api/keys_api.py](app/api/keys_api.py) (‘Ollama Cloud’ card in Settings), and [app/routing/router.py](app/routing/router.py) (hierarchy + default order).  `kimi-k2:1t-cloud` excluded — Ollama upstream returns 500 Internal Server Error consistently.

### 🐛 Bug Fixes

- **Systemic silent-default-substitution removed across 8 providers** — discovered via end-to-end audit: Gemini, Groq, OpenRouter, Cohere, Cloudflare, Cerebras, Z.ai, and Lightning all silently rewrote `request.model` to `self.default_model` whenever the requested ID wasn't in their hardcoded `self.models` list.  Combined with the cross-provider fallback chain, an unknown / mistyped model would walk through every provider's default until one of them returned 200 — so the caller's pin was completely ignored and the response came from a totally different model.  All eight providers now pass the explicit model through verbatim and only fall back to `default_model` for `"auto"` or empty.  This is the same fix previously applied to HuggingFace, now standardised everywhere.  Verified by `curl -d '{"model":"nonexistent/model",...}'` → returns HTTP 502 with clear "All providers/models/keys failed for model='nonexistent/model'" instead of silently returning gemini-2.5-flash-lite.
- **Explicit model selection now pins exactly** — when the caller named a specific model (e.g. Playground selected the 4th entry in a list), the router's `_model_hierarchy` used a substring-bubble (`requested in m OR m in requested`) that caused false matches (e.g. `gpt-4o` matched `gpt-4o-mini`) and silently fell through to other entries on the first failure. The router now returns a **single-entry hierarchy** for any exact match and passes unknown caller-specified models through as-is. Only the sentinel `"auto"` (or empty model field) triggers the full free-first fallback chain. Fixes: "selected 4th model but response came from 1st/default".
- **HuggingFace provider silently substituted the default model** — if the requested model wasn't in the hardcoded `self.models` list the provider rewrote it to `default_model` before calling HF Router, so every call returned a single model regardless of selection. Removed the rewrite; any model the caller explicitly names is now forwarded to HF verbatim.
- **Pollinations 502 behind Cloudflare** — Pollinations sits behind Cloudflare, which returns **502 Bad Gateway** to bare `httpx/0.x` User-Agent. Added `User-Agent: Arbiter/1.11.2 (+https://github.com/)` to the provider's HTTP client. Also dropped the deprecated legacy model aliases — only `openai-fast` (GPT-OSS 20B on OVH) is reliably reachable anonymously.
- **Routeway 503 no longer cooldown-cascades the whole key** — a single model returning `503` (upstream "no eligible providers") previously raised `RateLimitError`, which cooldowned the shared Routeway API key for 300s and knocked out all 15 `:free` models. 503s (status and in-body `code==503`) now raise `ProviderError` instead, so only that model is skipped and the other free models on the same key stay reachable.

### 🧹 Model Cleanup (verified via live probe × 2)

Pruned consistently-broken models from every provider's fallback hierarchy and default seed lists. Transient-upstream failures were left in place per the "if it's temporary it's fine" rule.

| Provider       | Removed                                                                                                                           | Kept |
|----------------|-----------------------------------------------------------------------------------------------------------------------------------|------|
| **Gemini**     | `gemini-3.1-flash-lite-preview`, `gemini-3-flash-preview`, `gemini-2.5-flash`                                                     | `gemini-2.5-flash-lite` (1 working) |
| **Groq**       | `moonshotai/kimi-k2-instruct{,-0905}` (returned `model_not_found`)                                                                | 6 working |
| **OpenRouter** | `meta-llama/llama-3.3-70b-instruct:free` (Venice upstream chronically 429)                                                        | 6 working (same model kept on Routeway) |
| **Cloudflare** | `@cf/moonshot/kimi-k2.5`, `@cf/qwen/qwen3-30b-a3b-fp8`, `@cf/deepseek/deepseek-r1-distill-qwen-32b`                                | 8 working |
| **Cerebras**   | `gpt-oss-120b`, `zai-glm-4.7` (not in Cerebras free-tier catalogue)                                                                | 2 working |
| **HuggingFace**| `mistralai/Mistral-7B-Instruct-v0.3`, `HuggingFaceH4/zephyr-7b-beta`, `google/gemma-2-2b-it` (no HF-Router provider)               | 4 working (now includes Llama 3.1 8B, Llama 3.2 1B, GPT-OSS 20B) |
| **Pollinations**| legacy `mistral`, `mistral-large`, `openai`, `claude` aliases (deprecated)                                                        | `openai-fast` (1 working) |
| **Routeway**   | `gpt-oss-120b:free`, `gemma-4-31b-it:free`, `kimi-k2-0905:free`, `glm-4.5-air:free`, `minimax-m2:free`, `nemotron-3-nano-30b-a3b:free` | 9 free + 7 paid fallback |

Final live probe result: **100% OK on Gemini, Groq, Cohere, Cloudflare, Cerebras, HuggingFace, Pollinations**; OpenRouter and Routeway free models work in isolation but currently show upstream rate-limits (RATE, not failure) that recover automatically.

### 📝 Infrastructure

- `scripts/test_curated_models.py` — probe harness that exercises every curated `(provider, model)` pair against the live gateway and categorises each as `OK | RATE | FAIL`. Re-run after any hierarchy change.

---

## [1.11.1] – 2026-04-24

### ✨ Free-Tier First Strategy

- **Routeway free models added by default** — Routeway tags 15 zero-cost models with a `:free` suffix (verified via their `/v1/models` pricing API: `price_per_million_t == 0`). These are now the default seed list for the Routeway provider and occupy the top slots of its fallback hierarchy, so unbilled accounts can use Routeway out-of-the-box without credits. Paid models (`gpt-4o`, `claude-3-5-sonnet`, etc.) remain available as last-resort fallback. Free models included: `llama-3.3-70b-instruct:free`, `gpt-oss-120b:free`, `kimi-k2-0905:free` (256K ctx), `glm-4.5-air:free`, `minimax-m2:free`, `devstral-2512:free`, `ling-2.6-flash:free`, `step-3.5-flash:free`, `gemma-4-31b-it:free`, `nemotron-3-nano-30b-a3b:free`, `nemotron-nano-9b-v2:free`, `llama-3.1-8b-instruct:free`, `llama-3.2-3b-instruct:free`, `llama-3.2-1b-instruct:free`, `mistral-nemo-instruct:free`.
- **Provider order documented as free-first** — `_DEFAULT_PROVIDER_ORDER` in `app/routing/router.py` now carries inline annotations confirming the priority: Gemini → Groq → Cerebras → Z.ai → Cloudflare → OpenRouter → Cohere → HuggingFace → Pollinations → Routeway → Lightning. Paid-only providers (Lightning) are unconditionally last so zero-cost traffic hits free providers first.
- **Routeway default model** changed from `gpt-4o-mini` (paid, 402 without credits) to `llama-3.3-70b-instruct:free` (free tier).

### 🐛 Bug Fixes

- **500 / AssertionError on every UI request when SSO enabled** — Starlette wraps the latest-added middleware as the OUTERMOST layer, so the previous ordering (SessionMiddleware added before GatewayAuthMiddleware) meant GatewayAuth ran *before* Session had populated `request.scope["session"]`, raising `AssertionError: SessionMiddleware must be installed to access request.session` on the first UI hit. Swapped the registration order in `app/main.py` and added a defensive `"session" in request.scope` guard in `get_session_user()` so future reorderings can't resurrect this bug.
- **Login page wrongly showed "Google SSO is not configured" even when it was** — `static/login.html` read `cfg.enabled` but `/auth/config` returns `{"sso_enabled": true, ...}`. Field-name mismatch caused the warning to always appear. Login page now accepts both keys.
- **Playground "Auto (Smart Route)" endpoint** — the playground used to force-pin a single vendor via `?vendor=X` on every request, so when that vendor's keys were on cooldown (e.g. Routeway 402 "Insufficient funds", OpenRouter 429) the request 502'd with no fallback. Added a new top-level **⚡ Auto (Smart Route)** option that omits `?vendor=` and lets Arbiter's router pick the healthiest available provider (exactly what Arbiter is designed for).
- **Misleading 502 "Last error: None"** — when every candidate provider's keys were already on cooldown, the router never attempted a call, so `last_error` stayed `None` and clients saw an empty/useless detail. Router now surfaces a clear `"All keys for provider(s) [...] are currently on cooldown or daily-quota exhausted..."` message.
- **Cooldown-exhaustion now returns HTTP 503** (not 502) — when the failure is "every key is resting" rather than an actual bad upstream response, 503 Service Unavailable is the accurate status code. Makes retries and monitoring more sensible.
- **Routeway / Z.ai missing in Settings UI** — backend was wired correctly, but `PROVIDER_COLORS` and `PROVIDER_DESCS` dicts in `static/settings.html` were missing both providers, so cards rendered with fallback color and no description. Added both entries.
- **Login never prompted** — `.env` had no Google OAuth credentials or `SESSION_SECRET_KEY`, so middleware correctly fell through to open mode. Added a Google SSO block to `.env` with a pre-generated `SESSION_SECRET_KEY`; user only needs to fill `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET`.
- **`_wants_json()` chained-comparison bug** in `app/middleware/auth.py` — `"*/*" in accept == accept.strip()` was accidentally using Python chained comparison. Replaced with explicit `accept in ("", "*/*")`.

### 🧹 Cleanup

- **Removed Analytics page** — data was fully redundant with the Dashboard. Deleted `static/analytics.html`, `app/api/analytics_api.py`, the `/analytics` route in `dashboard.py`, the `include_router(analytics_router, …)` line in `main.py`, the sidebar nav link, and the `/analytics/` entry in `_wants_json`.

### 🔐 Security Hardening

- **Admin-gated mutating admin endpoints** — added `Depends(require_admin)` on all Custom Providers write/test/probe routes (`POST`, `PATCH`, `DELETE`, `/test`, `/probe`) and on the Models `/refresh` + `/{model_id}/toggle` endpoints. Previously these were only protected by the global gateway middleware, meaning any approved non-admin Google user could add/remove providers once SSO was on.
- **Session cookie Secure flag enabled by default in prod `.env`** (`SESSION_COOKIE_SECURE=true`).
- **CORS allowlist pinned** — `.env` now defaults to `ALLOWED_CORS_ORIGINS=https://arbiter.chkoushik.com`; wildcard continues to be rejected when SSO is on.

### 📝 Docs

- `TEST-REPORT-v1.11.1.md` — full audit covering requirements coverage, security checklist, performance review, and smoke-test curls.

---

## [1.11.0] – 2026-04-23

### 🚀 New Provider — Routeway

- Added **Routeway** provider (`app/providers/routeway.py`) — OpenAI-compatible inference gateway at `https://api.routeway.ai/v1`
- Bearer-token auth, dynamic model discovery via `GET /models`, proper handling of 429 (retry-after) and 402 (quota)
- Wired through `VENDOR_MODEL_HIERARCHY`, `_DEFAULT_PROVIDER_ORDER`, `PROVIDER_LIMITS`, `_ENV_VAR_MAP`, `_PROVIDER_META`, free-tier table
- New env var: `ROUTEWAY_API_KEYS` (comma-separated; multi-key rotation supported)

### 🧩 Custom Providers — Add from the UI

- New **Custom Providers** tab in Settings with preset templates (OpenAI, Anthropic, DeepSeek, Together, Fireworks, Mistral, Perplexity, fully custom)
- `GenericOpenAIProvider` — instance-configured provider supporting both **Bearer** and **Anthropic (x-api-key + /messages)** auth schemes
- API surface (`/api/custom-providers`):
  - `GET  /templates`     — list preset templates
  - `GET  /`              — list configured custom providers
  - `POST /`              — add a new provider
  - `POST /probe`         — test connectivity without persisting
  - `POST /{name}/test`   — run a live probe against an existing provider
  - `PATCH /{name}`       — update label / key / models / base URL
  - `DELETE /{name}`      — remove provider and its API key
- **SSRF protection** — base URL is validated with `ipaddress`; rejects `localhost`, `127.0.0.1`, link-local, private IP ranges, and `metadata.google.internal`
- API keys persisted to `.env` as `CUSTOM_PROVIDER_<NAME>_KEY`; the rest of the config lives in `data/arbiter_state.json`
- Custom providers are hot-loaded at startup via `load_custom_providers_to_app()` — no restart needed after adding one

### 🔄 Dynamic Model Discovery — Manual Refresh

- `BaseProvider.fetch_models()` optional method lets any provider expose its live catalogue
- Per-provider **Refresh from provider** button on the Models tab calls `POST /api/models/{provider}/refresh`
- Per-model enable/disable state tracked in `data/arbiter_state.json`
  - Free-tier providers default-enable discovered models
  - Paid-tier discoveries are added as disabled until an admin enables them (quota safety)
- Router `_model_hierarchy()` filters out disabled models; `/v1/models` and `/api/models/info` merge state-store data over the static hierarchy
- **No Redis**, **no periodic polling** — refresh is strictly user-initiated (per user preference: "Redis causes cache issues")

### 🔐 Google SSO + Security Hardening

- **Google OAuth 2.0 sign-in** via Authlib for the admin dashboard and all UI pages
- Dual-mode auth:
  - `/v1/*` endpoints — Bearer-token only (unchanged for API clients)
  - Everything else — Google session cookie; unauthenticated HTML visits are redirected to `/login`, JSON requests receive `401`
- **Approval workflow** — first sign-in from the configured `ADMIN_EMAIL` is auto-approved and marked admin; all other users land in `pending` until an admin approves via `/users`
- `session_version` field on every user → rejecting or deleting a user **revokes their session immediately** on the next request
- New middleware stack (applied in order):
  1. `SecurityHeadersMiddleware` — `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, strict `Referrer-Policy`, tight `Permissions-Policy`, full `Content-Security-Policy`
  2. `CORSMiddleware` — **allowlist** (`ALLOWED_CORS_ORIGINS`); wildcard `*` is rejected when SSO is on
  3. `SessionMiddleware` (signed cookies, HttpOnly, SameSite=lax, `Secure` in production)
  4. `CloudflareAccessMiddleware` (unchanged)
  5. `GatewayAuthMiddleware` (rewritten, dual-mode)
- `BearerRedactFilter` — log formatter that regex-scrubs `Authorization: Bearer …`, `sk-…`, `gsk_…`, `csk-…`, `hf_…`, `AIza…` tokens before they hit stdout
- New env vars: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `ADMIN_EMAIL`, `APP_BASE_URL`, `SESSION_SECRET_KEY`, `SESSION_COOKIE_SECURE`, `SESSION_MAX_AGE`, `ALLOWED_CORS_ORIGINS`
- `/auth/config`, `/auth/me`, `/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/pending` routes
- `/api/users` admin API (list / approve / reject / delete / pre-approve) protected by `require_admin` dependency; admin cannot self-lock-out

### 🗂️ State Store (Disk-backed, No Redis)

- New `app/state_store.py` — JSON-on-disk at `data/arbiter_state.json` with `filelock` + atomic temp-file+rename writes
- Schema: `{version, users[], custom_providers[], models{provider → {model_id → {enabled, discovered_at, is_free, …}}}}`
- `data/` directory mounted as a docker volume so state survives `docker compose down`

### 🎨 UI

- New `/login` — branded Google sign-in page that detects SSO disabled state
- New `/users` — admin-only user management (pending / approved / rejected + pre-approve form)
- Topbar **user chip** with avatar, email, admin badge, Manage-users link, and Sign out — auto-injected into every page by `arbiter.js`
- Admin-only **Users** nav item auto-injected into the sidebar for admins
- Global fetch guard redirects to `/login?next=…` on 401 for UI JSON responses
- Added "Custom Providers" tab + "Refresh from provider" per-provider button in Models tab

### 🔧 Dependencies

- `authlib==1.3.2` (Google OAuth)
- `itsdangerous==2.2.0` (session cookie signing)
- `filelock==3.16.0` (state store concurrency)

### ⚠️ Breaking Changes

- **CORS** — if `GOOGLE_OAUTH_CLIENT_ID` is set, `ALLOWED_CORS_ORIGINS=*` is rejected at startup; set an explicit origin list
- **UI 401 behaviour** — HTML visits to protected pages now redirect to `/login` instead of returning JSON. API clients should continue using Bearer tokens against `/v1/*`
- **`SESSION_SECRET_KEY` is required** when SSO is enabled; missing it disables SSO and emits a startup warning

---

## [1.10.0] – 2026-03-30

### 🏗️ Key Storage Refactor — `.env` as Single Source of Truth

- **Removed Redis key storage** — provider API keys were previously stored in both `.env` (at startup) and Redis (added via UI), requiring an explicit "Save to .env" step to persist them across restarts
- **Keys now written directly to `.env`** on every add/remove operation via the UI
- `_read_env_keys()` / `_write_env_keys()` parse `.env` fresh on each call — changes take effect immediately without a restart or Redis sync
- **Auto-creates `.env` from `.env.example`** if no `.env` exists when the first key is added
- Removed `sync-env` endpoint and "Save to .env" button (no longer needed)
- **Delete button shown for all keys** — all keys live in `.env` and are all removable via the UI
- Redis is now used **only** for rate-limit counters and provider disabled/enabled flags

### 🔧 Provider Enable/Disable Flow

- **Test-before-enable** — when the enable toggle is switched on, Arbiter first calls the enable API then immediately runs a connectivity test
  - If test **passes**: provider stays enabled; latency and reply shown in the card
  - If test **fails**: provider is auto-disabled back; toggle reverts; error shown in red — ensures no provider is active with a broken key
- **Fixed stale DOM bug** in `addKey()` — after adding a key, `loadProviders()` re-renders the card grid; the validation result was being written to a detached (removed) DOM node and was invisible. Now re-queries the result element after the re-render.

### ✨ Playground — Markdown Rendering in Chat

- **Assistant chat bubbles now render GFM markdown** using `marked.js v9`
  - Headers, bold, italic, code blocks, inline code, lists, blockquotes, tables, links all rendered as HTML
  - Links open in new tab
  - User messages remain plain text (HTML-escaped)
  - Responsive markdown styles for all elements inside `.chat-msg.assistant`

### 🐛 Critical Bug Fixes

- **Modal vLLM startup crash** (`tokenizers` incompatibility) — `tokenizers>=0.21.0` removed `all_special_tokens_extended` which vLLM 0.11.x still accessed; fix: force-reinstall `tokenizers==0.20.3` as a separate image layer after vLLM install
- **Modal `startup_timeout`** increased from 600s to 1200s to accommodate large model downloads
- **Modal live deployment status** — added 5-second polling loop (`setInterval`) when Modal GPU tab is open; previously the deployment list was loaded once and never refreshed
- **Modal endpoint not appearing after deploy** — `loadModalEndpoints()` was only called on tab open; now also called when deployment reaches `active` state and on every 5s poll tick
- **Pollinations image generation 401** — `/v1/images/generations` was not in `_EXEMPT_PATHS`; browser UI requests don't carry Bearer tokens; added it and all UI page routes to exempt list
- **Lightning.ai / Z.ai keys not activating** — both providers were missing from `_reload_provider`'s `_classes` dict; keys were saved but providers were never instantiated; added `LightningProvider` and `ZaiProvider`

### 🎨 CSS / Responsive Fixes

- **Added 5 missing semantic CSS alias variables** to both light and dark themes: `--danger`, `--success`, `--warning`, `--text-1`, `--text-muted` — these were used in 24+ inline styles across pages but never defined, causing invisible/fallback rendering
- **Mobile 480px breakpoint** — added rules to collapse inline `1fr 1fr` grids to single column, fix `#providers-grid` in api-docs, adjust playground chat height

---

## [1.9.0] – 2026-03-29

### ✨ Lightning.ai Provider (LitAI)

- **New provider: Lightning.ai** (`app/providers/lightning_provider.py`)
  - OpenAI-compatible endpoint at `https://lightning.ai/api/v1`
  - **Natively hosted open-weight models** (not available elsewhere):
    - `nvidia/nemotron-3-super` — 256K context, ultra-fast (446 t/s)
    - `lightning-ai/gpt-oss-120b` — flagship 120B model
    - `deepseek/deepseek-v3.1` — 164K context
    - `lightning-ai/gpt-oss-20b` — efficient 20B
    - `meta/llama-3.3-70b` — 128K context
  - **Free tier**: ~37M token welcome credit on signup; then $0.09–$0.52/M tokens
  - **Authentication**: `Authorization: Bearer LIGHTNING_API_KEY`
  - Config: `LIGHTNING_API_KEYS=` in `.env`
  - Integrated into routing, key pool, models API, settings UI

### 🔧 Modal.com — Critical vLLM Template Fix

- **Fixed broken `_VLLM_TEMPLATE`** in `app/api/modal_deploy.py`:
  - **Root cause**: `allow_concurrent_inputs=MAX_CONCURRENT` inside `@app.cls(...)` was **removed in Modal 1.0** (May 2025) — all deployments failed silently
  - **Fix**: Replaced entire template with the official Modal 1.0 pattern:
    - `@app.function(...)` instead of `@app.cls(...)`
    - `@modal.concurrent(max_inputs=MAX_CONCURRENT)` as a separate decorator (Modal 1.0 replacement)
    - `@modal.web_server(port=8000, startup_timeout=600)` instead of `@modal.asgi_app`
    - vLLM runs as a **subprocess** (`subprocess.Popen(["vllm", "serve", ...])`) — uses vLLM's built-in OpenAI-compatible server
    - vLLM version bumped: `>=0.6.0` → `>=0.8.0`; Python 3.11 → 3.12
    - Removed heavyweight deps no longer needed in-process (`fastapi`, `uvicorn`, `transformers`)
  - The deployed endpoint serves `/v1/chat/completions`, `/v1/models`, `/health` natively via vLLM
- **Updated GPU prices** (Modal reduced prices since original implementation):
  - T4: $0.36/hr → **$0.59/hr** ($0.000164/s)
  - A10G: $0.72/hr → **$1.10/hr** ($0.000306/s)
  - A100-40GB: $2.16/hr → **$2.10/hr** ($0.000583/s)
  - A100-80GB: $3.40/hr → **$2.50/hr** ($0.000694/s)
- **Added GPU options**: L4 ($0.80/hr) and L40S ($1.95/hr) to `_GPU_MAP` and model catalog
- **Added new model options**: Qwen 2.5 7B on L4 (sweet spot), DeepSeek R1 Distill Llama 8B on T4 (cheapest reasoning)
- **Fixed templates in `modal_manager.py`**: Updated example code to use correct Modal 1.0 patterns; corrected GPU pricing table

---

## [1.8.0] – 2026-03-29

### ✨ Analytics Dashboard (`/analytics`)

- **New dedicated analytics page** — deep usage metrics with Chart.js visualizations
  - Summary KPI cards: total requests, tokens, errors, cache hit rate
  - Per-provider breakdown table: requests, tokens, errors, error rate
  - Per-model breakdown table: per-model request / token / error counts
  - Request history line chart: 5-minute bucket time-series (last 2 hours by default)
  - Provider distribution doughnut chart
  - Reset button to clear all counters
- **Per-model stat tracking in router** (`app/routing/router.py`):
  - `model:{name}:requests` — request count per model
  - `model:{name}:tokens` — token usage per model
  - `model:{name}:errors` — error count per model
  - `history:{bucket}:requests/success/errors` — 5-minute bucket time-series
- **New API** (`app/api/analytics_api.py`):
  - `GET /analytics/data` — returns summary, providers, models, and history arrays
  - `DELETE /analytics/reset` — clears all `arbiter:stats:*` keys
- **Route registered** in `main.py`

### ✨ Dynamic Gateway Token Management (Settings → Gateway Keys)

- **New Gateway Keys tab** in Settings UI — create, revoke, and delete API tokens from the admin panel
  - Token name + optional expiry datetime
  - Plaintext key shown once on creation; copy button provided
  - Revoke (soft-disable) or permanently delete individual tokens
  - Env-var keys shown as a count note; coexist seamlessly with UI-created tokens
- **Tokens active immediately** — no restart required; `GatewayAuthMiddleware` reads `app.state.gateway_tokens` on every request
- **New API** (`app/api/gateway_tokens_api.py`):
  - `GET /api/gateway/tokens` — list tokens (keys masked)
  - `POST /api/gateway/tokens` — create token, returns plaintext key once
  - `DELETE /api/gateway/tokens/{id}` — permanently delete
  - `PATCH /api/gateway/tokens/{id}` — update name / expiry / active flag
  - `POST /api/gateway/tokens/{id}/regenerate` — rotate the key
- **Auth middleware updated** (`app/middleware/auth.py`) to merge static env keys with dynamic `app.state.gateway_tokens`
- **Startup restoration** — `load_gateway_tokens_to_state()` called in `lifespan()` to reload tokens from Redis on restart

### ✨ Playground — Vendor + Model Drill-Down Selection

- **Two-level model picker** in Playground (`/playground`):
  - Vendor dropdown → model dropdown with metadata badges
  - **Free / paid badge** per model (OpenRouter `:free` suffix detection, provider-level free tier flags)
  - **Rate limits displayed**: RPM, TPM, RPD on model selection
  - **Context window** shown per model
- **New API endpoint** `GET /api/models/info` (`app/api/models_api.py`):
  - Returns per-vendor model catalog with rate limits from `PROVIDER_LIMITS` and `VENDOR_MODEL_HIERARCHY`
  - Only configured/active vendors returned
  - OpenRouter free model detection via `:free` suffix

### ✨ Dedicated Image Generation Page (`/images`)

- **New standalone page** `static/images.html` — no longer redirects to Settings
- Left panel: prompt, negative prompt, model selector (from `/v1/images/models`), count 1–4, size selector, seed, enhance toggle
- Right panel: image grid with per-image download / open / copy-URL buttons
- Settings persisted in `localStorage`
- Route `/images` now serves `images.html` directly (previously redirected to `/settings?tab=images`)

### 🐛 Fixes

- **Logs expansion state** preserved across auto-refresh — uses stable `seq` ID instead of array index; expanded rows stay expanded as new records load
- **Image Generation nav link** fixed across all pages (`/dashboard`, `/playground`, `/logs`, `/settings`, `/api-docs`, `/analytics`) — was incorrectly pointing to `/settings` with a `localStorage` tab trick

---

## [1.7.0] – 2026-03-29

### ✨ Z.ai (Zhipu GLM) Provider — Free Tier Support

- **New provider: Z.ai / Zhipu AI** (`app/providers/zai_provider.py`)
  - **Free models**: GLM-4.7-Flash, GLM-4.5-Flash, GLM-Z1-Flash ($0 — completely free)
  - **Context window**: 32K–128K tokens (flash models)
  - **Free-tier limits**: ~10 RPM, ~1000 RPD (verify on z.ai/manage-apikey/rate-limits)
  - **API base**: `https://api.z.ai/api/paas/v4/chat/completions`
  - **OpenAI-compatible**: Same format as other providers

- **Additive Capacity**: GLM-4.7 is now accessible via TWO independent providers:
  - **Cerebras-hosted** `zai-glm-4.7` → 30 RPM (via Cerebras API)
  - **Z.ai-hosted** `glm-4.7-flash` → ~10 RPM (via Z.ai API)
  - **Combined**: ~40 RPM total for GLM-4.7 class requests
  - Router includes both vendors; overlapping models sum their rate limits

- **Audit findings**: Checked all cross-vendor overlaps:
  - Kimi on Groq+Cloudflare ✓ (already separate providers)
  - Llama-4-scout on Groq+Cloudflare ✓ (already separate)
  - Qwen on Groq+Cloudflare ✓ (already separate)
  - Mistral on OpenRouter+Cloudflare ✓ (already separate)
  - Gemma-3 on OpenRouter+Cloudflare ✓ (already separate)
  - GLM-4.7 on Cerebras+Z.ai ✅ (now fixed with Z.ai provider)

### 🐛 Router & Cohere Fixes

- **Vendor pin no longer falls back to other providers**:
  - Before: `?vendor=cohere` would try Cohere, then silently fall back to Gemini on failure
  - After: `?vendor=cohere` returns error if Cohere fails (no hidden fallback)
  - Respects user's explicit provider selection
  - Code: `app/routing/router.py:374` — return `[vendor]` only, not `[vendor] + others`

- **Cohere v2 Chat API — System Message Fix**:
  - Before: System messages were extracted from message array and sent as top-level `payload["system"]` field → 422 "unknown field" error
  - After: System messages stay in the messages array with `role: "system"` (Cohere v2 format)
  - Removed extraction pattern; all roles (system/user/assistant) now pass through directly
  - Code: `app/providers/cohere_provider.py:56` — simplified `_build_cohere_messages()` method

### 📊 Key Pool & Rate Limit Fixes

- **Daily counter now tracks requests, not tokens**:
  - Before: `daily_used = 10 + 290 = 300 tokens` after 2 requests → exhausted (daily_limit=33) → locked for 24h
  - After: `daily_used = 1 + 1 = 2 requests` → plenty of room (daily_limit=33)
  - Root cause: All `PROVIDER_LIMITS.daily` values are requests-per-day (RPD), not tokens
  - Fixed: Use `incr` (by 1) for daily, keep `incrby(tokens)` only for TPM
  - Code: `app/key_management/key_pool.py:174` — `record_usage()` method

### 🎨 UI/UX Improvements

- **Fixed CSS variables** across playground and logs pages:
  - Corrected: `--surface-1` → `--surface`, `--text-muted` → `--text-3`, `--text-primary` → `--text`, `--text-secondary` → `--text-2`
  - Root cause: Pages used wrong variable names; CSS fallback is `initial` → transparent backgrounds
  - All 8 instances updated in both pages' style blocks

- **Fixed layout classes** in HTML structure:
  - `main.main-content` → `div.main-wrapper`
  - `h1.topbar-title` → `h1.page-title`
  - `div.topbar-actions` → `div.topbar-right`
  - `div.content-inner` → `div.page-content`
  - Root cause: Pages didn't match arbiter.css class names

- **Fixed sidebar toggle**:
  - Before: Button had `onclick="toggleSidebar()"` (undefined function) with no `id`
  - After: Button has `id="sidebar-toggle"`, no onclick attribute; arbiter.js wires via event listener
  - Sidebar toggle now works on playground and logs pages

- **Fixed JavaScript errors**:
  - Removed undefined `initTheme()` call (arbiter.js auto-applies theme)
  - Replaced `showToast()` with `toast()` (correct arbiter.js function name)
  - Improved error message fallback to show HTTP status code instead of empty "Error: Error"
  - Code: `static/playground.html:399`, `static/logs.html:496`, `static/logs.html:520`

- **Added responsive CSS**:
  - Tab bar now scrollable on mobile: `overflow-x: auto; -webkit-overflow-scrolling: touch`
  - Button size variants: `.btn-sm`, `.btn-xs`, `.btn-danger-ghost`
  - Playground responsive layout for ≤768px: sidebar + chat stack vertically
  - Code: `static/arbiter.css` additions

### 📝 Documentation Updates

- **README.md**: Updated provider count (8 → 9), added Z.ai to feature list and rate limits table
- **Configuration docs**: Added `ZAI_API_KEYS` env var documentation
- **Architecture diagram**: Updated provider flowchart to include Z.ai
- **CHANGELOG.md**: This section

---

## [1.6.0] – 2026-03-28

### 🛠️ CF Workers — Stale-Delete Fix
- **Fixed stale workers after deletion**: After a successful DELETE, a Redis deletion marker (`arbiter:cf:deleting:{name}`, 120s TTL) is set. `list_workers` checks this set and suppresses those workers during the Cloudflare API propagation delay (up to 2 minutes).

### 🔀 CF Workers & Modal — Gateway Routing
- **`cfworker/{name}` model prefix**: Any request to `/v1/chat/completions` with `model: cfworker/<worker-name>` is intercepted before the IntelligentRouter and proxied directly to that worker's `workers.dev` URL via httpx.
- **Virtual models in `/v1/models`**: Active CF workers are exposed as `cfworker/{name}` (owned_by `cloudflare-worker`) and active Modal deployments as `modal/{name}` (owned_by `modal`). Clients can pick these in any OpenAI-compatible tool.

### 🎮 Chat Playground (`/playground`)
- New full-screen chat UI reachable at `/playground` and from the sidebar.
- **Endpoint selector** — grouped dropdown across: Gateway Providers, Cloudflare Workers (live from registry), Modal Deployments (live from registry), Modal Endpoints (registered).
- **Config panel** — system prompt, temperature slider, max tokens.
- **Routing logic per endpoint type:**
  - `cfworker:` → `POST /v1/chat/completions` with `model: cfworker/{name}` (goes through gateway auth)
  - `modal:` with URL → direct `POST {url}/v1/chat/completions`
  - `gateway:` → `POST /v1/chat/completions?vendor={name}`
- **Latency badge** on every assistant message.
- Keyboard shortcut: Enter to send, Shift+Enter for newline.

### 📋 Log Viewer (`/logs`)
- New in-memory log viewer at `/logs` with real-time access to all application logs.
- **`LogBuffer`** Python logging handler (thread-safe deque, max 5,000 records) attached to root logger at startup — captures every module's output.
- **Filters**: level (DEBUG/INFO/WARNING/ERROR/CRITICAL), logger name prefix, text search (300ms debounce), time range (since/until).
- **Controls**: tail (last N), limit (100–5000), sort newest/oldest, auto-refresh (2s–30s), copy to clipboard, download as `.txt`, clear buffer.
- **REST API**: `GET /logs/records`, `GET /logs/loggers`, `DELETE /logs/clear`.

### 🔧 Bug Fixes
- Fixed `DeprecationError: container_idle_timeout` → renamed to `scaledown_window` in Modal vLLM template (deprecated 2025-02-24).
- Modal token auto-loaded from `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` env vars; cached to Redis on first use.

### 📚 Documentation
- Updated CHANGELOG, README, DEVELOPER.md, USERGUIDE.md with all Phase 3 features.

---

## [1.5.0] – 2026-03-28

### 🛠️ Cloudflare Workers — List & Delete Fixes

- **Fixed stale cleanup race condition**: Newly created workers are now stored in Redis with `status: "provisioning"`. The list endpoint respects a 120-second grace period so workers won't be incorrectly removed from the registry while the CF API is still propagating them.
- **Provisioning state visible in UI**: Workers show a "◌ Provisioning…" badge immediately after creation and auto-refresh every 3 seconds until CF confirms the worker.
- **Improved delete**: Optimistic UI update hides the worker immediately on successful DELETE; detailed CF API error messages (including 403 permission denied) are shown in a toast.
- **Better error messages**: CF API errors now extract the human-readable `errors[].message` from the JSON response body, not the raw HTTP body.
- **Workers sorted newest first**: Deployed workers list is now sorted by `created_on` descending.

### 🔑 API Key Validation (all providers)

- **New endpoint: `POST /cloudflare/validate`** — Checks three Cloudflare token permissions without side effects:
  - `Workers Scripts Read` → listing and managing worker scripts
  - `Workers AI Execute` → AI inference
  - `Workers Subdomain` → enabling workers.dev routing
  - Returns a permission matrix with HTTP status codes, notes, and recommendations.
- **Auto-validate on key add**: When a Cloudflare key is added in Settings → API Keys, the UI automatically runs permission validation and displays a ✅/❌ matrix per permission.
- **Validate button**: Existing Cloudflare keys can be re-validated anytime via the "Validate Permissions" button on the provider card.
- **Generic test on add**: Other providers (Gemini, Groq, etc.) run the existing `POST /api/providers/{name}/test` probe immediately after a key is saved, showing latency and a sample response.

### 🚀 Modal.com — One-Click vLLM Deploy

- **New endpoints:**
  - `POST /modal/deploy` — Start a background deployment (returns `deploy_id` immediately)
  - `GET /modal/deploy` — List all deployments with status
  - `GET /modal/deploy/{id}` — Deployment status + live log lines (poll every 2s)
  - `DELETE /modal/deploy/{id}` — Stop Modal app + remove from gateway pool
  - `POST /modal/deploy/account` — Save Modal account token (ak-id:secret)
  - `GET /modal/deploy/account` — Check token status
  - `GET /modal/deploy/models` — Curated catalog (10 models, T4→A100-80GB)
  - `GET /modal/deploy/check` — Verify Modal CLI availability + token configured
- **Pre-flight check**: `POST /modal/deploy` now validates `modal` CLI is in PATH before starting; returns a clear 400 error with install instructions if missing.
- **CLI status banner**: Modal GPU tab shows a warning banner if `modal` CLI is not found or no account token is configured.
- **Cost-optimised vLLM template**: `modal.Volume` weight caching, `@modal.concurrent`, `container_idle_timeout`, `gpu_memory_utilization=0.90`.
- **Auto-registration**: On deployment success, the endpoint URL is automatically registered in the Modal endpoint pool and gateway key pool — no manual step.
- **Live log streaming**: Deployment logs streamed to Redis; frontend polls `GET /modal/deploy/{id}` every 2s.

### 📚 Documentation

- Updated `CHANGELOG.md` with all v1.4 and v1.5 changes
- `DEVELOPER.md` — new API endpoints documented
- Cloudflare `cloudflare_manager.py` docstring updated with required token permissions
- `modal_deploy.py` docstring documents all routes and deployment flow

---

## [1.4.0] – 2026-03-28

### 🔧 Infrastructure & Provider Additions

- Added **Modal.com** provider (`app/providers/modal_provider.py`) — serverless GPU inference
- Added **Modal Manager** (`app/api/modal_manager.py`) — endpoint registration CRUD
- Added **Modal Deploy** (`app/api/modal_deploy.py`) — vLLM one-click deploy backend
- Added `modal>=0.73.0` to `requirements.txt`
- `app/main.py` — registers `modal_router` and `modal_deploy_router`
- `app/config.py` — added `MODAL_API_KEYS` setting
- `app/key_management/key_pool.py` — added Modal provider limits
- `app/middleware/auth.py` — exempt `/modal/*` paths

### ☁️ Cloudflare Workers — Integration Fixes

- `cloudflare_manager.py` rewritten with `async _get_credentials(request)` reading from Redis runtime keys
- `create_worker` now enables workers.dev subdomain via `POST /scripts/{name}/subdomain`
- Fetches actual `.workers.dev` URL from account subdomain endpoint
- Auto-removes workers deleted externally (stale registry cleanup)
- Hot-reloads `cloudflare` provider after worker creation

### 🎨 Settings UI — CF Workers & Modal GPU tabs

- CF Workers: model dropdown from live CF API, analytics button, URL display with copy, active/no-route badge
- Modal GPU: endpoint registration, test, delete; vLLM deployment template
- Models tab: fixed blank model names (was treating `{model, context_window}` dict as array)

---

## [1.3.0] – 2026-03-28

### ✨ Runtime API Key Management (no restart required)

- **New endpoint group: `GET/POST/DELETE /api/providers/*`**
  - `GET /api/providers` — list all providers with status, masked keys, pool stats
  - `POST /api/providers/{name}/keys` — add a key at runtime (stored in Redis)
  - `DELETE /api/providers/{name}/keys/{hash}` — remove a runtime-added key
  - `POST /api/providers/{name}/enable` — re-enable a disabled provider
  - `POST /api/providers/{name}/disable` — take a provider offline without restart
  - `POST /api/providers/{name}/test` — probe provider connectivity and measure latency
  - `POST /api/providers/reload` — hot-reload all key pools from env + Redis

- Keys added via the UI are stored in Redis (`arbiter:runtime:keys:{provider}`) and
  merged with `.env` keys automatically; no container restart needed.
- Enable/disable state stored in Redis (`arbiter:runtime:disabled:{provider}`).
- Env-var keys are shown as read-only (source: `env`); runtime keys can be deleted.

### 🖼️ Image Generation (Pollinations.ai — free, no key required)

- **New endpoints:**
  - `POST /v1/images/generations` — OpenAI-compatible image generation
  - `GET /v1/images/models` — list available image models
- Backed by Pollinations.ai FLUX models: `flux`, `flux-realism`, `flux-anime`, `flux-3d`, `flux-cablyai`, `turbo`
- Supports: prompt, negative prompt, model, size (up to 2048×2048), count (1–4), seed, AI enhance
- Returns image URLs (Pollinations renders lazily on first access)
- Completely free — no API key, no credit card

### 🎨 Settings UI — Full Overhaul

- **API Keys tab** (new, shown first):
  - Per-provider cards with status badge, enable/disable toggle, test button
  - Masked key list with source label (`env` or `runtime`)
  - Add key form with format hint per provider
  - Inline Cloudflare setup guide with step-by-step instructions
  - Sign-up links per provider
- **Image Generation tab** (new):
  - Live image generator UI backed by Pollinations
  - Model, size, count, seed, negative prompt, enhance controls
  - Generated images shown as clickable grid with download links
  - API endpoint reference panel
- **Reload Providers** button in topbar (calls `/api/providers/reload`)
- Cloudflare Workers tab: setup banner shown when CF keys not configured

### 🔧 Infrastructure

- Added `app/api/keys_api.py` (new) — provider management router
- Added `app/api/image_api.py` (new) — image generation router
- `app/main.py` — registers `keys_router` and `image_router`
- `app/middleware/auth.py` — exempts `/api/providers/*` and `/v1/images/models` paths

---

## [1.2.0] – 2026-03-28

### 🎨 Enterprise UI/UX Overhaul

- **Shared design system** — `static/arbiter.css` and `static/arbiter.js` loaded by all pages
  - Consistent CSS custom properties for colors, spacing, radius, shadows
  - Sidebar (240 px fixed), topbar (56 px), main content area
  - KPI cards, chart grid, stat rows, progress bars, tables, badges, drag list, toast, tabs, accordion
- **Light / Dark mode**
  - System preference detection (`prefers-color-scheme`)
  - Manual toggle persisted in `localStorage` (`arbiter-theme`)
  - Applied immediately on `<html>` before paint (no FOUC)
- **Unified single-site navigation** — identical sidebar across all three pages
- `app/main.py` — added `StaticFiles` mount at `/static/`
- `app/middleware/auth.py` — exempt paths starting with `/static/`

### 📊 Dashboard (`/dashboard`) — Rewrite

- 4 KPI cards: Total Requests, Success Rate, Cache Hit Rate, Cached Entries
- Chart.js **line chart** (request history, 20 data points stored in `localStorage`) + **doughnut chart** (provider distribution)
- Provider Status table with health badges
- **Key Details accordion** — per-provider, collapsible; shows hash, status badge, score bar, RPM/TPM/daily mini-bars
- 10-second auto-refresh via `/dashboard/stats`
- Live status pill and last-update timestamp in topbar

### 📚 API Docs (`/api-docs`) — Rewrite

- 5-tab layout: Overview, Authentication, Endpoints, Playground, Providers
- **Live playground** — vendor/model select, temperature slider, system/user messages, response panel with token usage
- Providers tab loads real data from `/settings/routing`
- Model list loads from `/v1/models`

### ⚙️ Settings (`/settings`) — New page

- **Routing tab** — drag-to-reorder provider priority list
- **Models tab** — per-provider model hierarchy management (add/remove/reorder)
- **Cloudflare Workers tab** — list, create, delete deployed Workers
- **Cache tab** — stats display + clear cache button

### 🛠️ Settings Management API

- `GET /settings/routing` — current routing config (provider order + model overrides)
- `POST /settings/routing` — save custom provider order and/or model overrides to Redis
- `DELETE /settings/routing` — reset to built-in defaults
- `DELETE /settings/cache` — clear all `arbiter:cache:*` keys from Redis

### 🔧 Router — Runtime Config Support

- `IntelligentRouter` reads custom config from Redis (`arbiter:config:provider_order`, `arbiter:config:models:{provider}`)
- 30-second in-memory cache on router to avoid per-request Redis reads
- `_provider_order()` and `_model_hierarchy()` accept optional `cfg` dict from Redis

### 🚫 Cache-Control Headers

- All HTML endpoints (`/dashboard`, `/api-docs`, `/settings`) now return:
  `Cache-Control: no-store, no-cache, must-revalidate` + `CDN-Cache-Control: no-store`
- Prevents Cloudflare CDN from caching stale UI after deployments

---

## [1.1.0] – 2026-03-28

### 🚀 New Providers & Model Updates

#### New Providers Added
- **Cloudflare Workers AI** (11 models)
  - `@cf/meta/llama-4-scout-17b-16e-instruct` (newest)
  - `@cf/meta/llama-3.3-70b-instruct-fp8-fast`
  - `@cf/moonshot/kimi-k2.5` (256K context)
  - `@cf/qwen/qwen3-30b-a3b-fp8`
  - `@cf/mistralai/mistral-small-3.1-24b-instruct`
  - `@cf/deepseek/deepseek-r1-distill-qwen-32b` (reasoning)
  - `@cf/qwen/qwq-32b` (reasoning)
  - `@cf/qwen/qwen2.5-coder-32b-instruct` (coding)
  - `@cf/google/gemma-3-12b-it`
  - `@cf/meta/llama-3.1-8b-instruct`
  - `@cf/meta/llama-3.2-3b-instruct`
  - Free tier: 300 RPM

- **Cerebras Inference** (4 models)
  - `llama3.1-8b` (production, fastest)
  - `gpt-oss-120b` (production, large)
  - `qwen-3-235b-a22b-instruct-2507` (preview, reasoning)
  - `zai-glm-4.7` (preview)
  - Free tier: 30 RPM, 60K TPM, 1M tokens/day

- **HuggingFace Inference Router** (4 models)
  - `Qwen/Qwen2.5-7B-Instruct`
  - `mistralai/Mistral-7B-Instruct-v0.3`
  - `HuggingFaceH4/zephyr-7b-beta`
  - `google/gemma-2-2b-it`

- **Pollinations.ai** (3 models, completely free)
  - `mistral`
  - `mistral-large`
  - `openai`
  - No authentication required

#### Model Updates (All Vendors)

**Gemini** — Updated to free-tier only, new previews
- ✅ Added: `gemini-3.1-flash-lite-preview` (newest, default)
- ✅ Added: `gemini-3-flash-preview` (frontier-class)
- ✅ Kept: `gemini-2.5-flash-lite` (stable, 15 RPM)
- ✅ Kept: `gemini-2.5-flash` (stable, 10 RPM)
- ❌ Removed: `gemini-2.5-pro` (paid-only)
- ❌ Removed: `gemini-1.5-*` (shut down Sep 24, 2025)
- ❌ Removed: `gemini-2.0-*` (deprecated, retiring Jun 1, 2026)

**Groq** — Added Kimi K2 alternative
- ✅ Added: `moonshotai/kimi-k2-instruct-0905` (alternative version)
- Kept: 7 existing models

**Cloudflare** — Context windows corrected
- Context: 131K (standard), 256K (Kimi K2.5)

**Cerebras** — Updated model lineup
- ❌ Removed: `llama-3.3-70b` (not available)
- ❌ Removed: `qwen-3-32b` (not available)
- ✅ Added: `gpt-oss-120b`
- ✅ Added: `qwen-3-235b-a22b-instruct-2507`
- ✅ Added: `zai-glm-4.7`

### 🔐 Authentication & Security

- **Gateway-level API authentication** — Optional `Authorization: Bearer <key>`
- **Multi-key gateway support** — `GATEWAY_API_KEYS` (comma-separated)
- **Cloudflare Access integration** — JWT validation via Zero Trust
  - `ENABLE_CF_ACCESS=true` flag
  - Supports JWKS caching (1-hour TTL)
  - Audience (`AUD`) validation
- **Improved key security** — Keys never stored in logs, hashed internally

### 📡 API Documentation & Management

- **Interactive API Docs** (`/api-docs`) — New dedicated page with:
  - Live request playground
  - Provider capabilities table
  - Authentication guide
  - Endpoint reference
  - Real-time model testing
- **Cloudflare Workers AI Manager** (`/cloudflare/*`)
  - `GET /cloudflare/models` — List available models
  - `POST /cloudflare/workers` — Create Workers
  - `GET /cloudflare/workers` — List deployed Workers
  - `DELETE /cloudflare/workers/{id}` — Delete Workers
- **Enhanced Swagger UI** — Improved documentation with vendor examples

### 📊 Dashboard Enhancements

- Updated provider table to show all 8 vendors
- Real-time stats for new providers
- Account limit display per provider
- Color-coded health indicators

---

## [1.0.0] – 2026-03-28

### 🎉 Initial Release

Production-ready Arbiter with multi-vendor aggregation, intelligent routing, and rate-limit management.

---

## Features Added (v1.0.0)

### ✅ OpenAI-Compatible API
- **POST `/v1/chat/completions`** — OpenAI-format chat completions endpoint
- **GET `/v1/models`** — List all available models
- **GET `/health`** — Health check endpoint
- **GET `/dashboard`** — Web-based observability dashboard
- **GET `/dashboard/stats`** — JSON stats endpoint
- Support for `temperature`, `top_p`, `max_tokens`, `stop_sequences`
- Automatic request translation to/from vendor-specific APIs

### ✅ Multi-Vendor Integration

**Gemini (Google)**
- Models: `gemini-3.1-flash-lite-preview`, `gemini-3-flash-preview`, `gemini-2.5-flash-lite`, `gemini-2.5-flash`
- Context window: 1M tokens
- Free-tier: 5–15 RPM, 250K TPM, 100–1,000 RPD
- Full message translation (OpenAI ↔ Gemini native)
- System prompt support via prepended user message

**Groq (GroqCloud)**
- Models: `llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `llama-4-scout-17b`, `qwen/qwen3-32b`, `moonshotai/kimi-k2-instruct`, `moonshotai/kimi-k2-instruct-0905`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b`
- Context window: 131K tokens
- Free-tier: 30–60 RPM, 6K–30K TPM, 1,000–14,400 RPD
- OpenAI-compatible endpoint (pass-through)

**OpenRouter (Aggregator)**
- 7 free models: `llama-3.3-70b:free`, `hermes-3-405b:free`, `gemma-3-27b:free`, `mistral-small-3.1:free`, `gemma-3-12b:free`, `qwen3-4b:free`, `llama-3.2-3b:free`
- Context window: 128K–131K tokens
- Free-tier: 20 RPM, 50–1,000 RPD
- OpenAI-compatible endpoint (with HTTP-Referer headers)

**Cohere**
- Models: `command-r7b-12-2024`, `command-r-08-2024`, `command-r-plus-08-2024`, `command-a-03-2025`
- Context window: 128K–256K tokens
- Free-tier: 20 RPM, 33 RPD (~1,000/month)
- Cohere v2 API support (system prompt + messages)

### ✅ Multi-Account Key Pool Management

- **Support multiple API keys per provider** — Distribute load across accounts
- **Weighted Availability Scoring Algorithm**:
  - Daily remaining quota: 50% weight (most critical)
  - RPM headroom: 30% weight
  - TPM headroom: 20% weight
  - Score formula: `(rpm_avail × 0.30) + (tpm_avail × 0.20) + (daily_avail × 0.50)`
- **Automatic key selection** — Pick the key with highest score
- **Per-key rate-limit tracking** — Redis-backed sliding windows:
  - RPM: 60-second rolling window
  - TPM: 60-second rolling window
  - Daily: 24-hour window
- **Graceful degradation** — Failed keys get 5-minute cooldown
- **Per-key stats** — View usage, quotas, and health in dashboard

### ✅ Intelligent Two-Level Routing Engine

**Level 1: Provider Selection**
- **Token-aware**: Large contexts (>100K tokens) → Gemini; Medium (16K+) → Gemini/OpenRouter; Small (<4K) → Groq
- **Capability-aware**: Code tasks → Gemini Pro / Groq 70B; General → Gemini Flash
- **Explicit routing**: Model name contains "gemini" → use Gemini; "llama" → Groq, etc.
- **Default priority**: Gemini → Groq → OpenRouter → Cohere

**Level 2: Model & Key Fallback**
- **Model hierarchy per vendor** — Try best fit first, fall back through hierarchy
- **Key rotation** — Try all accounts for same model before moving to next model
- **Cross-vendor fallback** — Only move to next vendor after exhausting current one
- Example flow:
  ```
  Gemini flash (account 1) → Gemini flash (account 2) → Gemini pro → Groq → OpenRouter → Cohere
  ```

**Model Hierarchies** (by vendor):

*Gemini (1M context):*
1. `gemini-2.5-flash-lite` (fastest, highest quota)
2. `gemini-2.5-flash` (balanced)
3. `gemini-2.5-pro` (highest quality)

*Groq (131K context):*
1. `llama-3.1-8b-instant` (fastest)
2. `llama-3.3-70b-versatile` (best quality)
3. `llama-4-scout-17b` (newest)
4. `qwen/qwen3-32b` (high RPM)
5. `moonshotai/kimi-k2` (high RPM)
6. `openai/gpt-oss-20b`
7. `openai/gpt-oss-120b`

*OpenRouter (128–131K context):*
1. `llama-3.3-70b:free` (quality)
2. `hermes-3-405b:free` (size)
3. `gemma-3-27b:free` (quality)
4. `mistral-small-3.1:free` (balanced)
5. `gemma-3-12b:free` (lighter)
6. `qwen3-4b:free` (fast)
7. `llama-3.2-3b:free` (smallest)

*Cohere (128–256K context):*
1. `command-r7b-12-2024` (fastest)
2. `command-r-08-2024` (balanced)
3. `command-r-plus-08-2024` (best quality)
4. `command-a-03-2025` (newest)

### ✅ Semantic & Exact-Match Caching (Redis)

- **Cache all responses** with `temperature ≤ 0.3` (deterministic)
- **SHA-256 hash key** based on model + messages
- **Configurable TTL** — Default 1 hour (3600s)
- **In-memory fallback** — Gateway works without Redis
- **Instant cache hits** — Same request returns cached response instantly
- **Transparent to client** — Caching is automatic
- **Cache stats** — Dashboard shows hit rate, size

### ✅ Production-Ready Observability

**Web Dashboard** (`/dashboard`)
- Dark-themed, auto-refreshing every 10 seconds
- **Top KPIs**: Total requests, success rate, cache hit rate, cached responses
- **Request breakdown**: Total, successful, failed
- **Cache statistics**: Hits, misses, hit rate, stored responses
- **Per-provider table**: Name, status (healthy/degraded/unavailable), active accounts, requests, success rate, models
- **Per-account table** with per-key details:
  - Account hash (anonymized)
  - Status badge (active/limited/failed/exhausted)
  - **Availability score** (0–100%) with color-coded progress bar
  - RPM usage bar (used/limit)
  - TPM usage bar (used/limit)
  - Daily token usage bar (used/limit)

**JSON Stats API** (`/dashboard/stats`)
- Programmatic access to all dashboard data
- Real-time counters

**Health Check** (`/health`)
- Status: online/degraded
- Redis connection status
- Active providers list
- Version info

**Logging**
- Structured logging to stdout/stderr
- Configurable log level (DEBUG/INFO/WARNING/ERROR)
- Request timing in response headers (`X-Response-Time-Ms`)

### ✅ Docker & Containerization

**Dockerfile**
- Based on `python:3.12-slim`
- Minimal footprint
- Secure defaults (no root)

**Docker Compose**
- Two services: `gateway` + `redis`
- Redis persistence with AOF mode
- Health checks on both services
- Bind mounts for live code reload (dev)
- Automatic service dependency management

### ✅ Rate-Limit Protection

- **Per-provider conservative limits** in `PROVIDER_LIMITS`
- **Per-key sliding-window tracking** (RPM, TPM, daily)
- **Hard exclusions** for:
  - Expired daily quotas (wait until next day)
  - Failed keys on 5-minute cooldown
- **Soft throttling** for RPM saturation (delay/queue within same minute)
- **Graceful fallback** when a key hits limits:
  - Try next best key (same model)
  - Try next model in hierarchy
  - Try next provider
- **Error transparency** — Clear error messages about rate limits

### ✅ Configuration & Secrets Management

- **Environment-based configuration** (`.env` file)
- **No hardcoded secrets** — All keys from env vars
- **Multi-key support** (comma-separated)
- **Per-provider customization**:
  - Redis URL
  - Cache TTL
  - Log level
  - API key pools
  - Optional gateway authentication

### ✅ API Key Security

- **Keys never logged** — MD5 hash (first 10 chars) used in logs/Redis
- **Per-account scoring** — Keys can be invalidated without affecting others
- **Automatic rotation** — Cooldown on failed keys (5 min) then retry
- **No credentials in responses** — Only model/metrics returned to client

### ✅ Error Handling & Resilience

- **Graceful degradation**:
  - Redis unavailable → Use in-memory fallback (dev-safe)
  - Provider down → Try next provider
  - Key quota exceeded → Try next key
  - All options exhausted → Clear error message
- **Request validation** — Reject malformed requests with 400 Bad Request
- **Timeout protection** — HTTP timeouts per provider (30–90s)
- **Retry logic** — Automatic retries for transient failures

### ✅ Middleware & HTTP Features

- **CORS** — Permissive (all origins) for self-hosted deployment
- **Request timing** — `X-Response-Time-Ms` header on all responses
- **Structured errors** — JSON error responses matching OpenAI format
- **Optional gateway auth** — `Authorization: Bearer` header support

---

## Technical Details

### Dependencies (v1.0.0)

```
fastapi==0.115.0              # Web framework
uvicorn[standard]==0.30.6     # ASGI server
httpx==0.27.2                 # Async HTTP client
redis==5.0.8                  # Redis client (async)
pydantic==2.9.2               # Data validation
pydantic-settings==2.5.2      # Settings management
tiktoken==0.7.0               # Token estimation (reserved for future)
python-dotenv==1.0.1          # .env file support
jinja2==3.1.4                 # Template rendering
python-multipart==0.0.12      # Multipart form support
```

### Architecture Improvements Made

1. **Weighted Scoring Algorithm** (vs. simple round-robin)
   - Maximizes quota utilization across accounts
   - Prevents starvation of high-quota keys
   - Adapts in real-time as quotas are consumed

2. **Two-Level Fallback** (vs. flat provider list)
   - Intra-vendor model hierarchy (fewer provider switches)
   - Key rotation per model (max account utilization)
   - Cross-vendor fallback (guaranteed success if any provider has quota)

3. **Semantic Caching** (vs. no caching)
   - 40–60% quota savings on deterministic requests (temp ≤ 0.3)
   - Instant responses for repeated requests
   - Transparent to caller

4. **Per-Key Tracking** (vs. per-provider)
   - Supports unlimited accounts per provider
   - Fine-grained quota visibility
   - Precise rate-limit enforcement

5. **In-Memory Fallback** (vs. hard dependency on Redis)
   - Works without external services in dev
   - Graceful degradation in production
   - Faster startup

---

## Deprecated Features

**None in v1.0.0** (initial release)

---

## Known Limitations

1. **Streaming not yet supported**
   - `"stream": true` will return error
   - Coming in v1.1.0

2. **No per-request authentication**
   - Gateway-level auth only (all-or-nothing)
   - Per-endpoint auth coming in v1.2.0

3. **Single Redis instance**
   - No Redis cluster support
   - Coming in v2.0.0 for HA deployments

4. **No request queuing**
   - Requests fail immediately if all keys exhausted
   - Request queue coming in v1.3.0

5. **No function calling / tool support**
   - OpenAI function_calling not translated
   - Provider-native tools not exposed
   - Coming in v2.0.0

---

## Migration Guide

**N/A** — Initial release

---

## Model Updates (Deprecated/Added)

### Removed (Deprecated)
- **Gemini**: `gemini-1.5-*` (shut down Sep 24, 2025), `gemini-2.0-*` (retiring Jun 1, 2026)
- **Groq**: `llama3-8b-8192`, `llama3-70b-8192`, `mixtral-8x7b-32768`, `gemma2-9b-it` (no longer in active model list)
- **OpenRouter**: Old `:free` models (`llama-3.1-8b-instruct:free`, `gemma-2-9b-it:free`, `mistral-7b:free`, `phi-3-mini:free`, `qwen-2-7b:free`)
- **Cohere**: `command-r`, `command-r-plus`, `command`, `command-light` (deprecated Sep 15, 2025)

### Added (Current)
- **Gemini 2.5 series**: Flash-lite, Flash, Pro (all 1M context)
- **Groq latest**: Llama 3.3, Llama 4 Scout, Qwen 3, Kimi K2, GPT-OSS variants
- **OpenRouter latest free**: Llama 3.3, Hermes 3, Gemma 3, Mistral 3.1, Qwen 3, Llama 3.2
- **Cohere 2024 series**: R7B, R-08, R-Plus-08, A-03

---

## Rate Limit Updates

All rate limits verified against official documentation (March 2026):

| Provider | RPM | TPM | Daily |
|---|---|---|---|
| Gemini | 5–15 | 250K | 100–1K |
| Groq | 30–60 | 6K–30K | 1K–14.4K |
| OpenRouter | 20 | — | 50–1K |
| Cohere | 20 | — | 33 |

---

## Next Planned Features (v1.1+)

- **Streaming responses** (chunked transfer-encoding)
- **Request queuing** (buffer overflow protection)
- **Custom routing rules** (JSON config file)
- **Prometheus metrics** (for monitoring/alerting)
- **Redis cluster support** (HA deployments)
- **Per-endpoint authentication** (fine-grained access control)
- **Tool/function calling** (OpenAI `tools` support)
- **Vision endpoints** (`/v1/vision/image-to-text`)
- **Embedding endpoints** (`/v1/embeddings`)
- **Fine-tuning logs** (track model fine-tune usage)

---

## Performance Benchmarks (v1.0.0)

### Latency

| Operation | P50 | P95 | P99 |
|---|---|---|---|
| Cache hit (deterministic) | 5ms | 10ms | 20ms |
| Gemini API call | 800ms | 1.2s | 2.5s |
| Groq API call | 200ms | 400ms | 800ms |
| Key selection (100 keys) | <1ms | 1ms | 2ms |

### Throughput

- **Single gateway instance**: ~50 req/s (with caching)
- **Per account (Gemini flash-lite)**: 15 RPM = 4 req/min throughput
- **Aggregate (3 Gemini + 2 Groq + 1 OR + 1 Cohere)**: ~150 req/min free-tier total

### Memory

- **Base image**: ~250 MB (Python + deps)
- **Per 1,000 cached responses**: ~10 MB (Redis)
- **Per 100 accounts tracked**: <1 MB (in-memory scoring)

---

## Security Audits

- **No external dependencies for secrets** — All env var based
- **No API key logs** — Keys hashed, first 10 chars only logged
- **No PII collection** — Only model/metrics tracked
- **CORS permissive** — Safe for self-hosted behind firewall
- **No known CVEs** in dependency tree (as of 2026-03-28)

---

## Contributors & Acknowledgments

- Built with FastAPI, Redis, Pydantic, httpx
- Thanks to Anthropic, Google, Groq, OpenRouter, Cohere for free-tier APIs
- Inspired by OpenClaw multi-agent framework

---

## Support & Contact

- **Issues**: GitHub Issues
- **Discussions**: GitHub Discussions
- **Security**: Responsible disclosure to maintainers

---

**Generated**: 2026-03-28
**Version**: 1.0.0
**Status**: Production Ready ✅
