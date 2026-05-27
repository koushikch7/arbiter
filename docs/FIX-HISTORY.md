# Fix History — Arbiter

Chronological log of all fixes, changes, and improvements.

---

## 2026-05-27 — v1.20.1 (Enterprise UI + Analytics Hardening + OpenAPI Parity)

### Audit findings addressed

- **OpenAPI was stale on v1.20.0 changes** — `tools`, `metadata.realtime`, `X-Arbiter-Realtime`, `X-Arbiter-Complexity`, `x_arbiter` body field werent exposed in the schema. Swagger/ReDoc consumers had no way to discover them.
- **Analytics dashboard missed enterprise-grade analyst capabilities** — no percentile latency (only avg), no error-type split, no cost / quota projection, no per-key live gauges, no today/yesterday/week/month presets, no export, no anomaly detection, no audit-log viewer.
- **Developer docs page drift** — hand-maintained endpoint table out-of-sync with `/openapi.json` since v1.18.
- **Zero accessibility baseline** — no skip-links, no ARIA landmarks, no focus-visible styles.
- **No frontend error reporting** — JS runtime exceptions silently swallowed; couldnt triage user-reported issues.
- **Refresh-tab thrash** — analytics polled every 5 s even when the tab was hidden, wasting ~150 req/min for 10 idle admins.

### Implemented

- All Phase A (OpenAPI parity, dev-table auto-gen, presets, percentile cards, key gauges, error breakdown).
- Phase B (persistent-log viewer, audit viewer, CSV/JSON export, comparison toggle, cost ledger, Page Visibility pause).
- Phase C (accessibility pass with skip-link/landmarks/focus-visible/reduced-motion, anomaly z-score bell).
- Phase D (frontend error reporter wired through `/api/ui-error` → `persistent_log.write_error()` → daily errors JSONL).

See CHANGELOG `[1.20.1]` for the per-file detail.

---

## 2026-05-26 — v1.20.0 (Real-Time Web Search + Multi-Key Hardening + Verified Free-Tier Limits)

### Root-cause findings

- **Free-Gemini traffic was producing 0 successful calls for ~36 h** — the `_PRERELEASE_BRIDGE` rewrote every `gemini-3.1-flash-lite` request into `gemini-3.1-flash-lite-preview`, which Google retired on 2026-05-25. The router was correctly rotating across 4 keys but every endpoint URL was dead.
- **Per-key sliding-window counters never expired under load** — `record_usage()` ran `INCR; EXPIRE 60` on every call, refreshing the TTL each time. A continuously-used keys RPM counter would accumulate monotonically until the very first 60 s gap.
- **Daily quota tracked 24 h since last call, not UTC midnight** — same TTL-refresh bug applied to the daily key.
- **Provider-default rate limits used the most-restrictive model on the key** — Gemini was hard-capped at the `gemini-2.5-pro` ceiling (5 RPM, 100 RPD) even when the request was for `gemini-2.5-flash-lite` (15 RPM, 1000 RPD), giving 1/10 of actual free quota.
- **Rate-limit cooldowns hardcoded at 300 s** — Gemini/Groq 429s typically reset within seconds; the 5-min cooldown wasted 80% of usable time on the rate-limited key.
- **No real-time / web-search capability** — every model answered purely from training data.

### Fixes & additions

**Multi-key rotation correctness (`app/key_management/key_pool.py`)**
- Removed the TTL-refresh bug — RPM/TPM counters now use `SET key 0 EX 60 NX; INCRBY key by` so the TTL anchors to the first increment in the window.
- Daily counter is now date-bucketed: `{provider}:{key_hash}:daily:YYYY-MM-DD` with a 30 h safety TTL, so it auto-rolls at UTC midnight irrespective of traffic patterns.
- New `MODEL_OVERRIDES` dict + `get_model_limits()` helper — flash-lite gets 1000 RPD on the same key that pro is capped at 100 RPD on, llama-3.1-8b-instant gets 14 400 RPD vs llama-3.3-70b at 1 000 RPD.
- `get_best_key()` / `record_usage()` / `_score_key()` accept an optional `model=` arg and respect per-model ceilings (computed as `max(provider_aggregate, per_model)`).
- `get_stats()` surfaces tier (`free` / `paid`) per key.

**Verified-against-docs free-tier limits (2026-05-26)**
- **Gemini**: 15 / 250 K / 1 000 (flash-lite); 10 / 250 K / 250 (flash); 5 / 250 K / 100 (pro — paid).
- **Groq**: 30 / 6 K / 14 400 (8b-instant); 30 / 12 K / 1 000 (70b); 60 / 6 K / 1 000 (qwen3-32b); 30 / 8 K / 1 000 (gpt-oss-120b).
- **Cerebras**: tightened from 30 RPM → 5 RPM / 30 K TPM / 1 M TPD per docs.
- **OpenRouter**: 20 RPM / 50 RPD without credits, 1 000 RPD with $10+ credits.
- **Cohere**: 20 RPM / 33 RPD (trial). **Cloudflare**: 300 RPM / 1 K daily chat calls. **NVIDIA NIM**: 40 RPM / 1 000 daily.

**`RateLimitError.retry_after` (`app/providers/base.py` + every provider)**
- `RateLimitError` now carries an optional `retry_after` seconds value.
- New `parse_retry_after()` extracts it from `Retry-After` headers (RFC 7231) and from common upstream body patterns (`try again in X.Xs`, `reset after Xms`).
- Router uses `mark_failed(key, cooldown_seconds=retry_after + 2)` — a 5 s wait becomes 7 s, not 5 minutes.

**Real-time web search (`app/services/web_search.py` — new module)**
- New `TavilyClient` — async HTTP client, redis-backed 5 min cache, 8 s timeout, structured response with numbered citations.
- Opt-in via `X-Arbiter-Realtime: true` header (or `metadata.realtime = true`). Tavily called, results prepended as a fresh system message with source URLs, chosen LLM answers grounded.
- Response includes `X-Arbiter-Realtime-Sources` header + `x_arbiter.realtime_sources` in JSON.

**Gemini native grounding (`app/providers/gemini.py`)**
- Provider forwards `{"tools":[{"google_search":{}}]}` when request contains a `tools` entry of type `google_search` / `google_search_retrieval`, or `metadata.realtime` / `web_search` / `google_search` is true.
- Free on Gemini 2.0+ and 3.x.

**OpenRouter `:online` opt-in (`app/api/chat.py`)**
- When `X-Arbiter-Realtime: true` is set AND the caller pinned an OpenRouter-style model, the router appends `:online`. Opt-in only — never auto-applied (avoids surprise per-search charges).

**Client observability (`app/api/chat.py`)**
- JSON body echoes actual chosen model into `model` field (instead of `auto`).
- New embedded `x_arbiter` object: `{provider, model, complexity, realtime_sources}`.
- New `X-Arbiter-Complexity` response header.

**Tighter diversity scoring (`app/routing/auto_router.py`)**
- Provider-diversity bonus now only fires when the candidate is at-or-above the quality tier required by complexity. On EXPERT requests, diversity can no longer pull a 7B model above a 120B flagship.

**Misc**
- `app/config.py`: `get_key_tiers()` mapping was missing `nvidia` — added. New `TAVILY_API_KEY` setting.

### Results (smoke-tested live 2026-05-26)

| Test | Routed to | Latency | Outcome |
|---|---|---|---|
| `hi` (TRIVIAL) | groq/llama-3.1-8b-instant | 392 ms | ✓ correct fast small model |
| Bitcoin price (X-Arbiter-Realtime: true) | nvidia/nemotron-3-super-120b-a12b | 7.5 s | ✓ 5 sources injected, citations |
| Prove CAP + Raft design (EXPERT) | cloudflare/@cf/openai/gpt-oss-120b | 4 s | ✓ flagship picked |
| `gemini-3.1-flash-lite` direct | gemini/gemini-3.1-flash-lite | <1 s | ✓ working (was 100% 404 before) |

---

## 2026-05-23 — v1.19.0 (Intelligent Complexity-Aware Routing)

### Root Cause Analysis

- **Problem:** 90% of all traffic was routed to just 2 models (`llama3.1-8b` @ 8B params, `gemini-2.5-flash-lite`) from only 2 providers (Cerebras, Gemini).
- **Root causes identified:**
  1. Intent classifier over-aggressively classified short messages as "fast" (< 30 chars → fast → smallest models)
  2. Performance sort completely overrode quality-based ordering (reshuffled by historical success rate → fastest model always won)
  3. Gap A incorrectly counted rate-limit 429s as "errors" → marked 4/11 providers permanently unhealthy
  4. No request complexity analysis → "hi" and "design a distributed system" got same routing
  5. Provider diversity not enforced in scoring formula

### Fixes Implemented

- **New `app/routing/complexity_analyzer.py`**: 13-factor scoring system classifying requests as TRIVIAL/SIMPLE/MODERATE/COMPLEX/EXPERT
- **Rewrote `app/routing/auto_router.py`**: Complexity-aware scoring with dynamic quality/speed weights, provider diversity guarantee, load distribution jitter
- **Fixed `intent_classifier.py`**: Removed aggressive "fast" classification for short messages
- **Fixed `router.py::_sort_candidates_by_perf`**: Changed from full re-sort to demote-only (preserves quality ordering)
- **Fixed `router.py::_get_unhealthy_providers`**: Excludes rate-limited responses from error count, raised thresholds (200 req min, 30% error rate)
- **Added Smart Model Upgrade in router.py**: Transparently upgrades weak models for complex requests
- **Reset stale Redis provider stats**: Cleared 25 poisoned lifetime counters

### Results (measured)

| Metric | Before | After |
|--------|--------|-------|
| Providers used (10 requests) | 2 | 7 |
| Models used (10 requests) | 2 | 8 |
| Expert request response | ~500 chars (8B model) | ~40K chars (120B model) |
| Trivial request model | 120B flagship | 7B fast model |
| Avg latency (trivial) | ~500ms | ~500ms |
| Complex quality | Weak (8B generation) | Expert-level (120B-235B) |

---

## 2026-05-12 — v1.18.0 + v1.18.1 (Observability Hardening)

### v1.18.1 — Gemini 3.1 Flash Lite GA Migration

- **Issue:** Google notified that `gemini-3.1-flash-lite-preview` will be discontinued May 25, 2026.
- **Fix:** Renamed model identifier to `gemini-3.1-flash-lite` (GA) in all runtime code.
- **Bridge:** Added `_PRERELEASE_BRIDGE` in `gemini.py` to transparently route the GA name to the still-active preview endpoint until Google's GA API is fully deployed.
- **Files:** `app/providers/gemini.py`, `app/providers/_free_tier_catalog.py`, `app/api/keys_api.py`, `scripts/test_all_free_models.py`

### v1.18.0 — Observability, Audit, Adaptive Routing

**Persistent 180-day file logs:**
- Created `app/observability/persistent_log.py` — three-stream JSONL writer (api/activity/errors)
- Daily-rotated files under `/app/data/logs/`, 180-day retention janitor at 03:00 UTC
- Secret redaction with `head4…tail4 + sha256[:12]` fingerprint
- HMAC-SHA256 tamper detection on activity records

**Admin activity audit:**
- Wired `_audit()` helper into: `keys_api.py`, `gateway_tokens_api.py`, `settings_api.py`, `cloudflare_manager.py`, `announcements_api.py`
- Captures actor email, role, action, target, before/after diffs, client IP

**Dashboard banners:**
- New service `app/services/announcements.py` — Redis ZSET-backed with TTL auto-expiry
- New API `app/api/announcements_api.py` — CRUD endpoints
- New frontend `static/components/announcements.js` — severity-coloured dismissable banners
- Impacted-token resolution from `arbiter:stats:token:*:provider:*:requests`

**Adaptive routing (Gap A + Gap B + TPM scoring):**
- Gap A: Providers with ≥20% error rate (≥100 requests) demoted to tail of routing chain
- Gap B: Wait for minute boundary (up to 10s) when all keys RPM-throttled instead of cross-provider fallback
- TPM-aware scoring: keys with insufficient TPM headroom deprioritised
- Router passes `estimated_request_tokens` to key picker

**Per-token rate limiting (#12):**
- Sliding-minute-window in `app/middleware/auth.py`
- Configurable via `GATEWAY_TOKEN_RATE_LIMIT_PER_MIN` (default 100)
- Per-token override via `request_limit_per_minute` on token record
- Returns 429 with `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`

**Consolidated email + weekly AI analysis:**
- Monday daily report extended with 7-day summary section
- AI-generated SRE insights (4–6 bullets) via Arbiter's own router
- Rate-limited responses excluded from error-rate alerts
- Secret-shaped strings stripped from outbound HTML

**Performance:**
- Cache gzip compression (#15) — `GZ1:` prefix for base64+gzipped values ≥ 512B
- Sorted-set error log (#16) — `ZADD` + `ZREMRANGEBYSCORE` replacing `LPUSH`/`LTRIM`
- Health probes (#14) — `rate_limited: true` tracked separately from errors

**OpenAPI/Swagger fixes:**
- Removed duplicate tags from `include_router()` calls in `main.py`
- Enriched API description with v1.18 feature summary, auth notes, rate limit info
- Created `app/api/persistent_logs_api.py` — 5 admin-only query endpoints

**Developer docs:**
- Updated `static/developer.html` with Observability tab, v1.18 Changes tab
- Expanded Endpoints table with all new routes
- Added Adaptive Routing and TPM-aware scoring sections

---

## 2026-05-12 — v1.17.0 (NVIDIA-first routing, predictive rate limiting)

- NVIDIA NIM promoted to top of `_DEFAULT_PROVIDER_ORDER`
- Predictive rate limiting: keys skipped at 95% of RPM/daily quota
- Weekly model health check (Monday 22:30 IST)
- Daily report: high-error-rate provider alerts (≥25%)
- Native streaming for custom providers (`complete_stream()`)
- Modal + Lightning providers removed (14 → 12)
- Consolidated daily email report with health summary

---

## 2026-05-03 — v1.15.0 (SSOT Provider Registry, Daily Email, Gateway Policies)

- Single source of truth provider registry with ModelSpec dataclass
- Automated SMTP daily report at 22:00 IST
- Per-token routing policies (auto/restricted/preferred)
- Developer docs page (`/developer`)
- User invite via email
- 5-day session TTL

---

## 2026-05-03 — v1.14.3 (NVIDIA NIM provider)

- NVIDIA NIM added as 13th provider
- 5 verified free-tier models
- Playground SSO fix

---

## 2026-05-01 — v1.14.2 (Enterprise backup, data persistence)

- Redis eviction: `allkeys-lru` → `volatile-lru`
- OCI Object Storage backup system
- Analytics window selector fix
- Experience-based intra-provider model reordering

---

## 2026-05-01 — v1.14.1 (Security audit — 21 issues)

- XSS fix in `/auth/pending`
- SSRF protection in provider URL discovery
- Admin-only guards on GET endpoints
- 4 MB request body limit
- Cache key collision fix
- Pydantic field bounds
- `redis.keys()` → `scan_iter`
