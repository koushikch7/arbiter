# Fix History — Arbiter

Chronological log of all fixes, changes, and improvements.

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
