"""
Intelligent two-level router with intra-vendor model hierarchy fallback.

Routing algorithm
─────────────────
1. Cache check  → return cached response (for temp ≤ 0.3).

2. Provider order  → picked by explicit model name / token count / capability.
   Can be overridden via vendor / force_model parameters.

3. For each provider (in order):
     For each model in provider's hierarchy (context-filtered, requested model first):
       tried_keys = {}
       loop:
         key = key_pool.get_best_key(exclude=tried_keys)   ← weighted scoring
         if no key → break (all keys for this model exhausted, try next model)
         tried_keys.add(key)
         try:
           response = provider.complete(request, key)
           record_usage(key); cache(response); return  ← SUCCESS
         except RateLimitError:
           mark_failed(key)          # back off this account
           continue                  # ← try next key, SAME model
         except ProviderError:
           break                     # model-level error, try next model

4. All providers exhausted → raise ProviderError.

Model hierarchies
─────────────────
Each vendor exposes an ordered list of (model_id, context_window) tuples.
The router selects only models whose context window can accommodate the
estimated token count of the request (guaranteeing no truncation).  When the
caller names a specific model, it is placed first; remaining slots are filled
by the hierarchy in default order.
"""

import json
import logging
import time
import asyncio
from typing import AsyncIterator, Dict, List, Optional, Set, Tuple

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse
from app.providers.base import BaseProvider, RateLimitError, ProviderError
from app.providers._free_tier_catalog import (
    FREE_TIER_CATALOG,
    PAID_FALLBACK_CATALOG,
    find_spec,
    provider_of,
    vendor_model_hierarchy,
)
from app.routing.auto_router import auto_candidate_chain
from app.routing.intent_classifier import classify
from app.routing.complexity_analyzer import analyze_complexity, Complexity
from app.cache.cache import CacheLayer
from app.key_management.key_pool import KeyPool
from app.observability import stats as obs_stats
from app.streaming.sse import (
    HEARTBEAT_INTERVAL_S,
    SSE_DONE,
    faux_stream_response,
    sse_comment,
    sse_data,
    sse_error,
    status_message,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Code / programming keywords for capability-based routing
# ---------------------------------------------------------------------------
CODE_KEYWORDS: Set[str] = {
    "code", "function", "programming", "python", "javascript", "typescript",
    "java", "golang", "rust", "c++", "cpp", "sql", "algorithm", "debug",
    "implement", "refactor", "class", "method", "api", "script", "compile",
    "runtime", "syntax", "error", "bug", "fix", "unit test", "regex",
    "dockerfile", "bash", "shell", "html", "css", "react", "django", "flask",
}

# ---------------------------------------------------------------------------
# Per-vendor model hierarchies — derived from FREE_TIER_CATALOG (v1.12+).
#
# To add / remove / reorder free-tier models, edit
# ``app/providers/_free_tier_catalog.py`` — this dict is built from it at
# import time so there is exactly one place to change.  Paid fallback
# entries (Routeway only) are appended last per provider.
# ---------------------------------------------------------------------------
VENDOR_MODEL_HIERARCHY: Dict[str, List[Tuple[str, int]]] = vendor_model_hierarchy(include_paid=True)


# Default provider priority (NVIDIA-first as of v1.17.0)
# ── Free-first strategy with NVIDIA prioritisation ────────────────────────
# NVIDIA NIM is first — powerful free-tier models (Nemotron-3-Super 120B,
# Llama-3.3-70B, Mistral-Medium-3.5-128B), generous limits (40 RPM, 1000 RPD,
# 131K context), and excellent quality. Followed by other free tiers.
# See VENDOR_MODEL_HIERARCHY above — each provider's model list is also
# sorted free-tier first.
_DEFAULT_PROVIDER_ORDER: List[str] = [
    "nvidia",        # 🥇 PRIORITY — NIM free tier · 40 RPM · 1000 RPD · 131K ctx · top quality
    "gemini",        # 1M ctx · free tier · 10-15 RPM
    "groq",          # fastest · free tier · 30 RPM
    "cerebras",      # fast · free tier · 30 RPM
    "zai",           # GLM flash models are $0
    "cloudflare",    # Workers AI free tier
    "openrouter",    # all :free models in hierarchy
    "cohere",        # trial key 1000 req/month free
    "huggingface",   # free inference router
    "pollinations",  # fully free, no auth
    "ollama",        # Ollama Cloud: free :cloud models (gpt-oss, deepseek, kimi …)
    "routeway",      # free + paid mix (free-first hierarchy, 9 free models)
]


class IntelligentRouter:
    """
    Routes ChatCompletion requests across providers and API-key accounts
    with two-level fallback: key rotation within a model → model hierarchy
    within a vendor → vendor-level fallback.
    """

    def __init__(
        self,
        providers: Dict[str, BaseProvider],
        key_pools: Dict[str, KeyPool],
        cache: CacheLayer,
        redis_client=None,
    ):
        self.providers = providers
        self.key_pools = key_pools
        self.cache     = cache
        self.redis     = redis_client
        self._cfg_cache: dict = {}
        self._cfg_cache_ts: float = 0.0
        self._disabled_cache: Set[str] = set()
        self._disabled_cache_ts: float = 0.0
        self._perf_cache: dict = {}
        self._perf_cache_ts: float = 0.0
        # v1.18.0 — Gap A: adaptive provider ordering. Providers with a
        # high lifetime error rate (≥20 % over ≥100 requests) are demoted
        # to the tail of the routing list so fallbacks consistently land
        # on healthier providers first.
        self._unhealthy_cache: Set[str] = set()
        self._unhealthy_cache_ts: float = 0.0

    async def _get_disabled_providers(self) -> Set[str]:
        """Return the set of provider names disabled via the Settings UI.

        The disable flag is written to ``arbiter:runtime:disabled:{name}`` by
        ``app/api/keys_api.py``.  Cached for 5s to avoid Redis chatter on
        every routing decision.
        """
        now = time.monotonic()
        if now - self._disabled_cache_ts < 5:
            return self._disabled_cache
        disabled: Set[str] = set()
        if self.redis:
            try:
                names = list(self.providers.keys())
                # Pipeline all GETs into one round-trip (audit fix #5)
                pipe = self.redis.pipeline()
                for name in names:
                    pipe.get(f"arbiter:runtime:disabled:{name}")
                flags = await pipe.execute()
                for name, flag in zip(names, flags):
                    if flag:
                        disabled.add(name)
            except Exception as e:
                logger.debug(f"disabled-providers read error: {e}")
        self._disabled_cache = disabled
        self._disabled_cache_ts = now
        return disabled

    async def _get_custom_config(self) -> dict:
        """Load custom routing config from Redis, cached for 30s."""
        now = time.monotonic()
        if now - self._cfg_cache_ts < 30 and self._cfg_cache:
            return self._cfg_cache
        cfg: dict = {"provider_order": None, "model_overrides": {}}
        if self.redis:
            try:
                # Pipeline provider_order + all per-provider model overrides
                # into a single Redis round-trip (audit fix #5).
                pipe = self.redis.pipeline()
                pipe.get("arbiter:config:provider_order")
                for p in _DEFAULT_PROVIDER_ORDER:
                    pipe.get(f"arbiter:config:models:{p}")
                results = await pipe.execute()
                if results:
                    order_raw, *model_raws = results
                    if order_raw:
                        try:
                            cfg["provider_order"] = json.loads(order_raw)
                        except Exception:
                            pass
                    for p, raw in zip(_DEFAULT_PROVIDER_ORDER, model_raws):
                        if raw:
                            try:
                                cfg["model_overrides"][p] = json.loads(raw)
                            except Exception:
                                pass
            except Exception as e:
                logger.debug(f"Config load error: {e}")
        self._cfg_cache = cfg
        self._cfg_cache_ts = now
        return cfg

    async def _get_model_perf(self) -> dict:
        """Return per-model performance data, cached for 5 minutes."""
        now = time.monotonic()
        if now - self._perf_cache_ts < 300 and self._perf_cache:
            return self._perf_cache
        try:
            perf = await obs_stats.get_model_performance(self.redis)
            self._perf_cache = perf
            self._perf_cache_ts = now
        except Exception as e:
            logger.debug(f"perf cache load error: {e}")
        return self._perf_cache

    async def _get_unhealthy_providers(self) -> Set[str]:
        """
        Return providers whose lifetime NON-RATELIMIT error rate is ≥30%
        over ≥200 requests. Cached for 60 s so the routing hot-path never
        round-trips to Redis.

        IMPORTANT: Rate-limit errors (429s) are NOT counted as failures
        here — they're a normal part of free-tier operation and are already
        handled by key rotation. Only actual provider errors (5xx, timeouts,
        malformed responses) indicate an unhealthy provider.
        """
        now = time.monotonic()
        if now - self._unhealthy_cache_ts < 60 and self._unhealthy_cache_ts:
            return self._unhealthy_cache
        unhealthy: Set[str] = set()
        if not self.redis:
            self._unhealthy_cache = unhealthy
            self._unhealthy_cache_ts = now
            return unhealthy
        try:
            for p in _DEFAULT_PROVIDER_ORDER:
                pipe = self.redis.pipeline()
                pipe.get(f"arbiter:stats:provider:{p}:success")
                pipe.get(f"arbiter:stats:provider:{p}:errors")
                pipe.get(f"arbiter:stats:provider:{p}:rate_limited")
                results = await pipe.execute()
                succ = int(results[0]) if results[0] else 0
                err  = int(results[1]) if results[1] else 0
                rl   = int(results[2]) if results[2] else 0
                # Subtract rate-limited from error count — they're expected
                real_errors = max(0, err - rl)
                total = succ + real_errors
                if total >= 200 and (real_errors / total) >= 0.30:
                    unhealthy.add(p)
        except Exception as exc:
            logger.debug("unhealthy provider scan failed: %s", exc)
        self._unhealthy_cache = unhealthy
        self._unhealthy_cache_ts = now
        if unhealthy:
            logger.debug("Gap A demote set: %s", unhealthy)
        return unhealthy

    def _apply_health_demote(self, order: List[str]) -> List[str]:
        """Move providers in ``self._unhealthy_cache`` to the tail of *order*.

        Relative order between healthy and between unhealthy entries is
        preserved so an operator-configured priority is respected as long
        as everything is healthy.
        """
        if not self._unhealthy_cache:
            return order
        unhealthy = self._unhealthy_cache
        healthy   = [p for p in order if p not in unhealthy]
        demoted   = [p for p in order if p in unhealthy]
        return healthy + demoted

    def _sort_candidates_by_perf(
        self,
        candidates: List[Tuple[str, str]],
        perf: dict,
    ) -> List[Tuple[str, str]]:
        """Demote candidates with poor recent performance.

        Unlike the previous implementation that re-sorted within provider
        groups (which disrupted the auto-router's quality-based ordering),
        this version only DEMOTES models that have demonstrably high error
        rates (≥30%). Models with good or unknown performance keep their
        original position from the auto-router's scoring.

        This preserves the complexity-aware ordering while still avoiding
        models that are actively failing.
        """
        if not perf:
            return candidates

        good: List[Tuple[str, str]] = []
        bad: List[Tuple[str, str]] = []

        for prov, model in candidates:
            m = perf.get(model)
            if m and m.get("error_rate", 0.0) >= 0.30 and m.get("total_requests", 0) >= 10:
                bad.append((prov, model))
            else:
                good.append((prov, model))

        if bad:
            logger.debug(
                "Perf-demoted %d model(s) to tail: %s",
                len(bad), [(p, m) for p, m in bad[:5]],
            )
        return good + bad

    # ------------------------------------------------------------------
    # Shared prep — cache lookup + candidate-chain building
    # ------------------------------------------------------------------
    async def _prepare_route(
        self,
        request: ChatCompletionRequest,
        *,
        vendor: Optional[str],
        priority_override: Optional[str],
        prefer_provider_override: Optional[str],
        token_id: Optional[str],
        routing_policy: Optional[str],
        allowed_models: Optional[List[str]],
        blocked_models: Optional[List[str]],
    ) -> Dict:
        """
        Build the candidate (provider, model) chain and check the cache.

        Returns a dict with keys:
            request    – possibly updated ChatCompletionRequest
            candidates – list of (provider_name, model_id) to try in order
            cache_key  – cache key for later .set() on success
            cached     – cached ChatCompletionResponse, or None

        Side effects:
            - Records cache hit/miss stats.
            - Raises ProviderError if the user pinned a disabled vendor.
        """
        cfg = await self._get_custom_config()

        # ── 1. Cache lookup ──────────────────────────────────────────
        cache_key = self.cache.make_key(request)
        cached: Optional[ChatCompletionResponse] = None
        if request.temperature <= 0.3:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                await obs_stats.record_cache_hit(self.redis, token_id=token_id)
                logger.info(f"Cache HIT  model={request.model}")
                return {
                    "request":    request,
                    "candidates": [],
                    "cache_key":  cache_key,
                    "cached":     cached,
                }
        await obs_stats.record_cache_miss(self.redis)

        # ── 2. Build the unified (provider, model) candidate chain ───
        token_est = self._estimate_tokens(request)
        candidates = self._build_candidate_chain(
            request,
            vendor=vendor,
            cfg=cfg,
            token_est=token_est,
            priority_override=priority_override,
            prefer_provider_override=prefer_provider_override,
        )

        # Honour Settings → Providers "Disable" toggle.
        disabled_providers = await self._get_disabled_providers()
        if vendor and vendor in disabled_providers:
            await obs_stats.record_request_failed(self.redis, token_id=token_id)
            raise ProviderError(
                f"Provider {vendor!r} is currently disabled in Settings. "
                f"Re-enable it at /settings or pick another vendor."
            )
        if disabled_providers:
            before = len(candidates)
            candidates = [(p, m) for (p, m) in candidates if p not in disabled_providers]
            if before != len(candidates):
                logger.info(
                    f"Filtered {before - len(candidates)} candidate(s) from "
                    f"disabled providers: {sorted(disabled_providers)}"
                )

        # ── Tool-call aware filtering ────────────────────────────────
        # When the request includes tools/functions, only route to providers
        # that forward tool definitions to the upstream LLM API. Otherwise
        # models respond with empty content or ignore tools entirely.
        _TOOL_CAPABLE_PROVIDERS = {
            "groq", "nvidia", "openrouter", "cerebras", "ollama",
        }
        has_tools = bool(getattr(request, "tools", None) or getattr(request, "functions", None))
        if has_tools and not vendor:
            before = len(candidates)
            tool_candidates = [(p, m) for (p, m) in candidates if p in _TOOL_CAPABLE_PROVIDERS]
            if tool_candidates:
                candidates = tool_candidates
                if before != len(candidates):
                    logger.info(
                        f"Tool-call request: filtered to {len(candidates)} tool-capable "
                        f"candidates (from {before})"
                    )
            else:
                logger.warning(
                    "Tool-call request but no tool-capable providers in candidates; "
                    "proceeding with all candidates (tools may be ignored)"
                )

        # ── Experience-based intra-provider model reordering ─────────
        try:
            perf = await self._get_model_perf()
            candidates = self._sort_candidates_by_perf(candidates, perf)
        except Exception as _perf_err:
            logger.debug(f"perf sort skipped: {_perf_err}")

        # ── Gap A: demote providers with poor recent health ──────────
        # We compute the unhealthy set once per minute (cached) and push
        # any candidates from those providers to the tail of the chain,
        # preserving relative order otherwise. Vendor-pinned chains skip
        # this step so the operator's explicit choice is still honoured.
        if not vendor:
            try:
                unhealthy = await self._get_unhealthy_providers()
                if unhealthy:
                    healthy   = [(p, m) for (p, m) in candidates if p not in unhealthy]
                    demoted   = [(p, m) for (p, m) in candidates if p in unhealthy]
                    if demoted:
                        candidates = healthy + demoted
                        logger.info(
                            "Gap A demoted providers to tail: %s "
                            "(healthy=%d, demoted=%d)",
                            sorted(unhealthy), len(healthy), len(demoted),
                        )
            except Exception as _gap_a_err:
                logger.debug("Gap A demotion skipped: %s", _gap_a_err)

        # ── Gateway-level routing policy ─────────────────────────────
        if routing_policy == "restricted" and allowed_models:
            allowed_set = set(allowed_models)
            before = len(candidates)
            candidates = [(p, m) for (p, m) in candidates if m in allowed_set]
            model_priority = {m: i for i, m in enumerate(allowed_models)}
            candidates.sort(key=lambda x: model_priority.get(x[1], 999))
            if before != len(candidates):
                logger.info(
                    f"Gateway routing_policy=restricted: {before}→{len(candidates)} candidates "
                    f"(allowed: {allowed_models[:5]})"
                )
        elif routing_policy == "preferred" and allowed_models:
            preferred_set = set(allowed_models)
            preferred = [(p, m) for (p, m) in candidates if m in preferred_set]
            model_priority = {m: i for i, m in enumerate(allowed_models)}
            preferred.sort(key=lambda x: model_priority.get(x[1], 999))
            rest = [(p, m) for (p, m) in candidates if m not in preferred_set]
            candidates = preferred + rest
            if preferred:
                logger.info(
                    f"Gateway routing_policy=preferred: {len(preferred)} preferred models "
                    f"moved to front ({allowed_models[:3]}…)"
                )

        if blocked_models:
            blocked_set = set(blocked_models)
            candidates = [(p, m) for (p, m) in candidates if m not in blocked_set]

        # Log with complexity analysis for observability
        complexity = analyze_complexity(request)
        logger.info(
            f"Routing  model={request.model!r}  tokens≈{token_est}  "
            f"complexity={complexity.name}  "
            f"candidates={candidates[:8]}{'…' if len(candidates) > 8 else ''}"
        )

        return {
            "request":    request,
            "candidates": candidates,
            "cache_key":  cache_key,
            "cached":     None,
        }

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def route(
        self,
        request: ChatCompletionRequest,
        vendor: Optional[str] = None,
        force_model: Optional[str] = None,
        priority_override: Optional[str] = None,
        prefer_provider_override: Optional[str] = None,
        token_id: Optional[str] = None,
        token_name: Optional[str] = None,
        routing_policy: Optional[str] = None,
        allowed_models: Optional[List[str]] = None,
        blocked_models: Optional[List[str]] = None,
    ) -> ChatCompletionResponse:
        """
        Route *request* to the best available provider/model/key.

        Parameters
        ──────────
        vendor                    If provided, put this provider first.
        force_model               Override the model field (legacy).
        priority_override         "speed" | "quality" | "balanced" — per-request
                                  priority for auto routing.
        prefer_provider_override  Boost a specific provider in auto routing.
        routing_policy            "auto" | "restricted" | "preferred" — gateway-level.
        allowed_models            Model allowlist for restricted/preferred modes.
        blocked_models            Model blocklist (applied after candidates built).
                                  priority for auto routing.
        prefer_provider_override  Boost a specific provider in auto routing.
        """
        # Apply overrides
        if force_model:
            request = request.model_copy(update={"model": force_model})

        prep = await self._prepare_route(
            request,
            vendor=vendor,
            priority_override=priority_override,
            prefer_provider_override=prefer_provider_override,
            token_id=token_id,
            routing_policy=routing_policy,
            allowed_models=allowed_models,
            blocked_models=blocked_models,
        )
        request    = prep["request"]
        candidates = prep["candidates"]
        cache_key  = prep["cache_key"]
        cached     = prep["cached"]
        if cached is not None:
            return cached

        last_error: Optional[Exception] = None
        attempted_providers: List[str] = []

        # ── 3. Walk the chain — key rotation per (provider, model) ────
        for provider_name, model_name in candidates:
            provider = self.providers.get(provider_name)
            key_pool = self.key_pools.get(provider_name)
            if provider is None or key_pool is None:
                logger.debug(f"Provider {provider_name!r} not configured, skipping")
                continue
            if provider_name not in attempted_providers:
                attempted_providers.append(provider_name)

            tried_keys: Set[str] = set()

            # Determine if this model requires a paid tier key.  Providers
            # may declare a `paid_models` set to gate frontier/billed models.
            required_tier: Optional[str] = None
            paid_models = getattr(provider, "paid_models", None)
            if paid_models and model_name in paid_models:
                required_tier = "paid"

            while True:
                key = await key_pool.get_best_key(
                    exclude=tried_keys, required_tier=required_tier,
                    estimated_request_tokens=self._estimate_tokens(request), model=model_name)
                if key is None:
                    logger.warning(
                        f"[{provider_name}/{model_name}] "
                        f"No available key after trying {len(tried_keys)} account(s)"
                        + (f" (required_tier={required_tier})" if required_tier else "")
                    )
                    break  # → next candidate

                tried_keys.add(key)
                routed = request.model_copy(update={"model": model_name})

                attempt_t0 = time.perf_counter()
                try:
                    logger.info(
                        f"→ {provider_name}/{model_name}  "
                        f"key=...{key[-4:]}  attempt={len(tried_keys)}"
                    )
                    response = await provider.complete(routed, key)

                    # ── SUCCESS ──────────────────────────────────
                    latency_ms = round((time.perf_counter() - attempt_t0) * 1000)
                    tokens_used = (
                        response.usage.total_tokens if response.usage else 0
                    )
                    await key_pool.record_usage(key, tokens_used, model=model_name)

                    if request.temperature <= 0.3:
                        await self.cache.set(cache_key, response)

                    await obs_stats.record_success(
                        self.redis,
                        provider=provider_name,
                        model=model_name,
                        tokens_used=tokens_used,
                        latency_ms=latency_ms,
                        token_id=token_id,
                    )

                    logger.info(
                        f"✓ {provider_name}/{model_name}  tokens={tokens_used}  "
                        f"latency={latency_ms}ms"
                    )
                    # Surface chosen pair so chat.py can emit response headers
                    setattr(response, "_arbiter_provider", provider_name)
                    setattr(response, "_arbiter_model", model_name)
                    return response

                except RateLimitError as exc:
                    logger.warning(
                        f"✗ RateLimit {provider_name}/{model_name}  "
                        f"key=...{key[-4:]}: {exc}"
                    )
                    await key_pool.mark_failed(key, cooldown_seconds=int(getattr(exc, "retry_after", None) or 60) + 2)
                    await obs_stats.record_failure(
                        self.redis, provider=provider_name, model=model_name,
                        rate_limited=True, token_id=token_id,
                    )
                    await obs_stats.record_error_detail(
                        self.redis,
                        provider=provider_name,
                        model=model_name,
                        error_type="RateLimitError",
                        error_message=str(exc),
                        rate_limited=True,
                    )
                    last_error = exc
                    # try next account for the SAME (provider, model)

                except ProviderError as exc:
                    logger.error(
                        f"✗ ProviderError {provider_name}/{model_name}: {exc}"
                    )
                    await key_pool.record_error(key)
                    await obs_stats.record_failure(
                        self.redis, provider=provider_name, model=model_name,
                        rate_limited=False, token_id=token_id,
                    )
                    await obs_stats.record_error_detail(
                        self.redis,
                        provider=provider_name,
                        model=model_name,
                        error_type="ProviderError",
                        error_message=str(exc),
                        rate_limited=False,
                    )
                    last_error = exc
                    break  # → next candidate

                except Exception as exc:
                    logger.exception(
                        f"✗ Unexpected {provider_name}/{model_name}: {exc}"
                    )
                    await key_pool.record_error(key)
                    await obs_stats.record_error_detail(
                        self.redis,
                        provider=provider_name,
                        model=model_name,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        rate_limited=False,
                    )
                    last_error = exc
                    break

        # ── 4. All options exhausted ──────────────────────────────────
        await obs_stats.record_request_failed(self.redis, token_id=token_id)
        if last_error is None:
            detail = (
                f"All keys for provider(s) {attempted_providers} are currently on "
                f"cooldown or daily-quota exhausted. Try again later, add more "
                f"keys in Settings, or switch vendor."
            )
        else:
            detail = (
                f"All providers/models/keys failed for model={request.model!r}. "
                f"Last error: {last_error}"
            )
        raise ProviderError(detail)

    # ------------------------------------------------------------------
    # Streaming entry point
    # ------------------------------------------------------------------

    async def route_stream(
        self,
        request: ChatCompletionRequest,
        vendor: Optional[str] = None,
        force_model: Optional[str] = None,
        priority_override: Optional[str] = None,
        prefer_provider_override: Optional[str] = None,
        token_id: Optional[str] = None,
        token_name: Optional[str] = None,
        routing_policy: Optional[str] = None,
        allowed_models: Optional[List[str]] = None,
        blocked_models: Optional[List[str]] = None,
    ) -> AsyncIterator[bytes]:
        """
        Stream a chat completion as Server-Sent Events.

        Phase 1 strategy ("graceful streaming"):
            • Reuses the same routing/fallback/key-rotation logic as ``route()``.
            • While awaiting ``provider.complete()``, emits SSE comment
              heartbeats (``: thinking\\n\\n`` etc.) every ~5 s so reverse
              proxies keep the connection warm and clients see liveness.
            • Once the upstream returns, replays the response as a sequence of
              OpenAI-format ``chat.completion.chunk`` deltas, then ``data: [DONE]``.
            • Cache hits replay through the same chunked path.
            • Provider failures fall back transparently AS LONG AS no chunks
              have been sent yet (matches OpenAI's own SSE behavior).

        Yields:
            Raw bytes ready for ``StreamingResponse``.
        """
        if force_model:
            request = request.model_copy(update={"model": force_model})

        # ---- Prep (cache + candidate chain) -----------------------------
        try:
            prep = await self._prepare_route(
                request,
                vendor=vendor,
                priority_override=priority_override,
                prefer_provider_override=prefer_provider_override,
                token_id=token_id,
                routing_policy=routing_policy,
                allowed_models=allowed_models,
                blocked_models=blocked_models,
            )
        except ProviderError as exc:
            # Disabled-vendor pin or similar — surface as SSE error event
            yield sse_error(str(exc), error_type="provider_error", code=503)
            yield SSE_DONE
            return

        request    = prep["request"]
        candidates = prep["candidates"]
        cache_key  = prep["cache_key"]
        cached     = prep["cached"]

        # ---- Cache hit → replay as stream -------------------------------
        if cached is not None:
            async for chunk in faux_stream_response(
                cached,
                model_name=cached.model,
                arbiter_provider=getattr(cached, "_arbiter_provider", None) or "cache",
            ):
                yield chunk
            return

        last_error: Optional[Exception] = None
        attempted_providers: List[str] = []

        # ---- Walk the candidate chain -----------------------------------
        for provider_name, model_name in candidates:
            provider = self.providers.get(provider_name)
            key_pool = self.key_pools.get(provider_name)
            if provider is None or key_pool is None:
                continue
            if provider_name not in attempted_providers:
                attempted_providers.append(provider_name)

            tried_keys: Set[str] = set()
            required_tier: Optional[str] = None
            paid_models = getattr(provider, "paid_models", None)
            if paid_models and model_name in paid_models:
                required_tier = "paid"

            while True:
                key = await key_pool.get_best_key(
                    exclude=tried_keys, required_tier=required_tier,
                    estimated_request_tokens=self._estimate_tokens(request), model=model_name)
                if key is None:
                    break

                tried_keys.add(key)
                routed = request.model_copy(update={"model": model_name})

                attempt_t0 = time.perf_counter()
                logger.info(
                    f"→ [stream] {provider_name}/{model_name}  "
                    f"key=...{key[-4:]}  attempt={len(tried_keys)}"
                )

                # ── Try native SSE first ──────────────────────────────
                # If the provider implements complete_stream(), iterate it
                # directly — chunks reach the client as soon as the upstream
                # emits them (true low TTFT). On NotImplementedError fall
                # back to the faux path below.
                native_ok = False
                native_err: Optional[Exception] = None
                accumulated_text = ""
                final_finish_reason = "stop"
                final_usage: Optional[dict] = None
                first_chunk_sent = False
                provider_chunk_id: Optional[str] = None

                try:
                    aiter = provider.complete_stream(routed, key).__aiter__()
                except NotImplementedError:
                    aiter = None
                except Exception as exc:
                    aiter = None
                    native_err = exc

                if aiter is not None:
                    try:
                        # Heartbeat while waiting for the first chunk
                        first_chunk_task = asyncio.create_task(aiter.__anext__())
                        hb_index = 0
                        while not first_chunk_task.done():
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(first_chunk_task),
                                    timeout=HEARTBEAT_INTERVAL_S,
                                )
                            except asyncio.TimeoutError:
                                yield sse_comment(status_message(hb_index))
                                hb_index += 1
                            except (asyncio.CancelledError, GeneratorExit):
                                first_chunk_task.cancel()
                                raise
                            except Exception:
                                break

                        first_exc = first_chunk_task.exception()
                        if first_exc is not None:
                            raise first_exc

                        first_chunk = first_chunk_task.result()

                        # Got first chunk → commit to native streaming
                        yield sse_comment(
                            f"arbiter-model-used: {provider_name}/{model_name}"
                        )

                        from app.streaming.openai_stream import (
                            extract_delta_content, extract_finish_reason, extract_usage,
                        )

                        async def _emit(chunk: dict):
                            nonlocal accumulated_text, final_finish_reason, final_usage
                            nonlocal first_chunk_sent, provider_chunk_id
                            text = extract_delta_content(chunk)
                            if text:
                                accumulated_text += text
                            fr = extract_finish_reason(chunk)
                            if fr:
                                final_finish_reason = fr
                            usg = extract_usage(chunk)
                            if usg:
                                final_usage = usg
                            if provider_chunk_id is None:
                                provider_chunk_id = chunk.get("id")
                            first_chunk_sent = True
                            return sse_data(chunk)

                        yield await _emit(first_chunk)
                        async for chunk in aiter:
                            yield await _emit(chunk)

                        yield SSE_DONE
                        native_ok = True

                    except RateLimitError as exc:
                        if first_chunk_sent:
                            # Already streaming → can't fall back
                            logger.warning(
                                f"✗ [stream] mid-stream RateLimit "
                                f"{provider_name}/{model_name}: {exc}"
                            )
                            yield sse_error(str(exc), error_type="rate_limit", code=429)
                            yield SSE_DONE
                            await key_pool.mark_failed(key, cooldown_seconds=int(getattr(exc, "retry_after", None) or 60) + 2)
                            await obs_stats.record_failure(
                                self.redis, provider=provider_name, model=model_name,
                                rate_limited=True, token_id=token_id,
                            )
                            await obs_stats.record_error_detail(
                                self.redis, provider=provider_name, model=model_name,
                                error_type="RateLimitError", error_message=str(exc),
                                rate_limited=True,
                            )
                            return
                        # No bytes sent yet → safe to fall back
                        native_err = exc
                    except ProviderError as exc:
                        if first_chunk_sent:
                            logger.error(
                                f"✗ [stream] mid-stream ProviderError "
                                f"{provider_name}/{model_name}: {exc}"
                            )
                            yield sse_error(str(exc), error_type="provider_error", code=502)
                            yield SSE_DONE
                            await key_pool.record_error(key)
                            await obs_stats.record_failure(
                                self.redis, provider=provider_name, model=model_name,
                                rate_limited=False, token_id=token_id,
                            )
                            await obs_stats.record_error_detail(
                                self.redis, provider=provider_name, model=model_name,
                                error_type="ProviderError", error_message=str(exc),
                                rate_limited=False,
                            )
                            return
                        native_err = exc
                    except (asyncio.CancelledError, GeneratorExit):
                        raise
                    except Exception as exc:
                        if first_chunk_sent:
                            logger.exception(
                                f"✗ [stream] mid-stream Unexpected "
                                f"{provider_name}/{model_name}: {exc}"
                            )
                            yield sse_error(str(exc), error_type=type(exc).__name__, code=500)
                            yield SSE_DONE
                            await key_pool.record_error(key)
                            await obs_stats.record_error_detail(
                                self.redis, provider=provider_name, model=model_name,
                                error_type=type(exc).__name__, error_message=str(exc),
                                rate_limited=False,
                            )
                            return
                        native_err = exc

                if native_ok:
                    # ── Native SUCCESS — record stats, cache reconstruction ────
                    latency_ms = round((time.perf_counter() - attempt_t0) * 1000)
                    if final_usage:
                        prompt_tokens = int(final_usage.get("prompt_tokens", 0) or 0)
                        completion_tokens = int(final_usage.get("completion_tokens", 0) or 0)
                        tokens_used = int(final_usage.get(
                            "total_tokens", prompt_tokens + completion_tokens
                        ))
                    else:
                        prompt_tokens = self._estimate_tokens(request)
                        completion_tokens = max(1, len(accumulated_text.split()))
                        tokens_used = prompt_tokens + completion_tokens
                    await key_pool.record_usage(key, tokens_used, model=model_name)

                    # Reconstruct a synthetic ChatCompletionResponse for cache
                    if request.temperature <= 0.3:
                        try:
                            from app.models.schemas import (
                                ChatCompletionResponse as _CR, Choice as _Ch,
                                Message as _Msg, Usage as _Usg,
                            )
                            synth = _CR(
                                id=provider_chunk_id or f"chatcmpl-{int(time.time())}",
                                object="chat.completion",
                                created=int(time.time()),
                                model=model_name,
                                choices=[_Ch(
                                    index=0,
                                    message=_Msg(role="assistant", content=accumulated_text),
                                    finish_reason=final_finish_reason or "stop",
                                )],
                                usage=_Usg(
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens,
                                    total_tokens=tokens_used,
                                ),
                            )
                            setattr(synth, "_arbiter_provider", provider_name)
                            setattr(synth, "_arbiter_model", model_name)
                            await self.cache.set(cache_key, synth)
                        except Exception as cache_exc:
                            logger.debug(f"stream cache.set skipped: {cache_exc}")

                    await obs_stats.record_success(
                        self.redis,
                        provider=provider_name,
                        model=model_name,
                        tokens_used=tokens_used,
                        latency_ms=latency_ms,
                        token_id=token_id,
                    )
                    logger.info(
                        f"✓ [stream-native] {provider_name}/{model_name}  "
                        f"tokens={tokens_used}  latency={latency_ms}ms"
                    )
                    return

                # ── Native path failed pre-first-chunk OR not implemented:
                #    fall back to faux streaming via complete().
                if native_err is not None:
                    logger.info(
                        f"  [stream] native failed pre-chunk on "
                        f"{provider_name}/{model_name} ({type(native_err).__name__}: "
                        f"{native_err}) — falling back to faux"
                    )

                # Run provider.complete() as a Task and emit SSE heartbeats
                # while awaiting it. Heartbeats are SSE comments — invisible
                # to OpenAI SDKs, but keep nginx/Cloudflare from idle-killing
                # the connection during slow upstream calls.
                task = asyncio.create_task(provider.complete(routed, key))
                hb_index = 0
                response: Optional[ChatCompletionResponse] = None
                try:
                    while not task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(task), timeout=HEARTBEAT_INTERVAL_S)
                        except asyncio.TimeoutError:
                            yield sse_comment(status_message(hb_index))
                            hb_index += 1
                        except (asyncio.CancelledError, GeneratorExit):
                            task.cancel()
                            raise
                        except Exception:
                            # Task raised — break and inspect via .exception()
                            break

                    exc = task.exception()
                    if exc is None:
                        response = task.result()
                    else:
                        raise exc

                except RateLimitError as exc:
                    logger.warning(
                        f"✗ [stream] RateLimit {provider_name}/{model_name}  "
                        f"key=...{key[-4:]}: {exc}"
                    )
                    await key_pool.mark_failed(key, cooldown_seconds=int(getattr(exc, "retry_after", None) or 60) + 2)
                    await obs_stats.record_failure(
                        self.redis, provider=provider_name, model=model_name,
                        rate_limited=True, token_id=token_id,
                    )
                    await obs_stats.record_error_detail(
                        self.redis, provider=provider_name, model=model_name,
                        error_type="RateLimitError", error_message=str(exc),
                        rate_limited=True,
                    )
                    last_error = exc
                    continue  # try next key, same model

                except ProviderError as exc:
                    logger.error(f"✗ [stream] ProviderError {provider_name}/{model_name}: {exc}")
                    await key_pool.record_error(key)
                    await obs_stats.record_failure(
                        self.redis, provider=provider_name, model=model_name,
                        rate_limited=False, token_id=token_id,
                    )
                    await obs_stats.record_error_detail(
                        self.redis, provider=provider_name, model=model_name,
                        error_type="ProviderError", error_message=str(exc),
                        rate_limited=False,
                    )
                    last_error = exc
                    break  # next candidate

                except Exception as exc:
                    logger.exception(f"✗ [stream] Unexpected {provider_name}/{model_name}: {exc}")
                    await key_pool.record_error(key)
                    await obs_stats.record_error_detail(
                        self.redis, provider=provider_name, model=model_name,
                        error_type=type(exc).__name__, error_message=str(exc),
                        rate_limited=False,
                    )
                    last_error = exc
                    break

                # ── SUCCESS ────────────────────────────────────────────
                latency_ms = round((time.perf_counter() - attempt_t0) * 1000)
                tokens_used = response.usage.total_tokens if (response and response.usage) else 0
                await key_pool.record_usage(key, tokens_used, model=model_name)

                if request.temperature <= 0.3:
                    try:
                        await self.cache.set(cache_key, response)
                    except Exception:
                        pass

                await obs_stats.record_success(
                    self.redis,
                    provider=provider_name,
                    model=model_name,
                    tokens_used=tokens_used,
                    latency_ms=latency_ms,
                    token_id=token_id,
                )
                logger.info(
                    f"✓ [stream] {provider_name}/{model_name}  tokens={tokens_used}  "
                    f"latency={latency_ms}ms"
                )

                # Replay the finished response as SSE chunks
                async for chunk in faux_stream_response(
                    response,
                    model_name=model_name,
                    arbiter_provider=provider_name,
                ):
                    yield chunk
                return

        # ── All options exhausted ─────────────────────────────────────
        await obs_stats.record_request_failed(self.redis, token_id=token_id)
        if last_error is None:
            detail = (
                f"All keys for provider(s) {attempted_providers} are currently on "
                f"cooldown or daily-quota exhausted. Try again later, add more "
                f"keys in Settings, or switch vendor."
            )
        else:
            detail = (
                f"All providers/models/keys failed for model={request.model!r}. "
                f"Last error: {last_error}"
            )
        yield sse_error(detail, error_type="provider_error", code=502)
        yield SSE_DONE

    # ------------------------------------------------------------------
    # Provider ordering
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Candidate-chain builder
    # ------------------------------------------------------------------

    def _build_candidate_chain(
        self,
        request: ChatCompletionRequest,
        *,
        vendor: Optional[str],
        cfg: dict,
        token_est: int,
        priority_override: Optional[str] = None,
        prefer_provider_override: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """
        Return the unified ordered list of ``(provider, model_id)`` to try.

        Three modes:
          - vendor pinned        → ``[(vendor, m) for m in vendor_hierarchy]``
          - model="auto" / empty → smart auto-router via auto_candidate_chain()
          - explicit model       → starts with the pinned (provider, model);
                                   honours request.fallback ("none"|"same_provider"|"chain")
        """
        requested = (request.model or "").strip()
        is_auto = (not requested) or requested.lower() == "auto"
        configured = list(self.providers.keys())

        # ── (a) explicit vendor pin  → only that vendor ─────────────
        if vendor:
            if vendor not in self.providers:
                return []
            chain: List[Tuple[str, str]] = []
            for m in self._model_hierarchy(vendor, request, token_est, cfg=cfg):
                chain.append((vendor, m))
            return chain

        # ── (b) auto-routing (model="auto" / empty) ─────────────────
        if is_auto:
            return auto_candidate_chain(
                request,
                token_est=token_est,
                priority_override=priority_override,
                prefer_provider_override=prefer_provider_override,
                available_providers=configured,
            )

        # ── (c) explicit model — with smart upgrade for complex requests ──
        fallback_mode = (request.fallback or "none").strip().lower()
        if fallback_mode not in ("none", "same_provider", "chain"):
            fallback_mode = "none"

        owning_provider = provider_of(requested) or self._infer_provider(requested)

        # ── Smart Model Upgrade (v1.19.0) ──────────────────────────────
        # When a client requests a weak model (quality ≤ 2) but the request
        # is actually complex/expert-level, transparently upgrade to a more
        # capable model. This ensures hardcoded clients still get quality
        # responses for demanding queries. The original model stays as a
        # fallback so nothing breaks.
        primary: List[Tuple[str, str]]
        spec = find_spec(requested)
        complexity = analyze_complexity(request)

        if (
            spec
            and spec.quality <= 2
            and complexity >= Complexity.MODERATE
        ):
            # Build an upgraded chain: powerful models first, then original
            logger.info(
                f"Smart upgrade: model={requested!r} quality={spec.quality} "
                f"complexity={complexity.name} → upgrading candidate chain"
            )
            upgraded_chain = auto_candidate_chain(
                request,
                token_est=token_est,
                priority_override="quality" if complexity >= Complexity.COMPLEX else "balanced",
                prefer_provider_override=owning_provider,
                available_providers=configured,
            )
            # Ensure original model is still in the chain as fallback
            if owning_provider and owning_provider in self.providers:
                original = (owning_provider, requested)
                if original not in upgraded_chain:
                    upgraded_chain.append(original)
            return upgraded_chain

        # Normal explicit model routing (no upgrade)
        if owning_provider and owning_provider in self.providers:
            primary = [(owning_provider, requested)]
        else:
            # Unknown model — try every configured provider in default order.
            # The provider's own response will surface a clear 404 if invalid.
            primary = [(p, requested) for p in _DEFAULT_PROVIDER_ORDER if p in self.providers]

        if fallback_mode == "none":
            return primary

        if fallback_mode == "same_provider":
            extra: List[Tuple[str, str]] = []
            seen = {(p, m) for p, m in primary}
            if owning_provider and owning_provider in self.providers:
                for m in self._model_hierarchy(owning_provider, request, token_est, cfg=cfg):
                    if (owning_provider, m) not in seen:
                        extra.append((owning_provider, m))
                        seen.add((owning_provider, m))
            return primary + extra

        # fallback_mode == "chain"
        auto_chain = auto_candidate_chain(
            request,
            token_est=token_est,
            priority_override=priority_override,
            prefer_provider_override=prefer_provider_override,
            available_providers=configured,
        )
        seen = {(p, m) for p, m in primary}
        merged = list(primary)
        for p, m in auto_chain:
            if (p, m) not in seen:
                merged.append((p, m))
                seen.add((p, m))
        return merged

    def _infer_provider(self, model_id: str) -> Optional[str]:
        """Best-effort provider guess from a model ID prefix/suffix."""
        m = model_id.lower()
        if m.startswith("gemini"):
            return "gemini"
        if m.startswith("@cf/"):
            return "cloudflare"
        if m.startswith("command-"):
            return "cohere"
        if m.startswith("glm-") or m.startswith("zai-glm"):
            return "zai"
        if m.endswith(":cloud"):
            return "ollama"
        if m.endswith(":free"):
            return "routeway"
        if m.startswith("openai-"):
            return "pollinations"
        if "/" in m:
            return "openrouter"
        return None

    def _provider_order(
        self,
        request: ChatCompletionRequest,
        vendor: Optional[str] = None,
        cfg=None,
    ) -> List[str]:
        """Return the ordered list of providers to attempt.

        If *vendor* is supplied, it is placed unconditionally at position 0.
        """
        cfg = cfg or {}
        base_order = cfg.get("provider_order") or list(_DEFAULT_PROVIDER_ORDER)

        model     = request.model.lower()
        token_est = self._estimate_tokens(request)

        # ── Explicit vendor override ──
        # When the caller pins a vendor, use ONLY that vendor — no cross-provider
        # fallback.  Failing silently into Gemini when the user selected "Cohere"
        # is confusing and misleading.
        if vendor:
            return [vendor] if vendor in self.providers else []

        # "auto" is a magic sentinel for "let the router pick freely" —
        # skip all explicit-name heuristics and fall through to token/
        # capability routing below.
        if model == "auto":
            model = ""

        # ── Explicit model-name routing ──
        if "gemini" in model:
            return self._reorder("gemini", base_order)

        # Cloudflare Workers AI model names start with @cf/
        if "@cf/" in model:
            return self._reorder("cloudflare", base_order)

        # Cerebras native model names
        if "llama3.1" in model or "llama3.1-8b" in model or "cerebras" in model:
            return self._reorder("cerebras", base_order)

        # Groq-native model names (no slash) vs OpenRouter slash-format
        if any(k in model for k in ("llama-3.1-8b", "llama-3.3", "llama-4", "qwen3", "kimi", "gpt-oss")):
            return self._reorder("groq", base_order)

        if any(k in model for k in ("command-r", "command-a", "cohere")):
            return self._reorder("cohere", base_order)

        # Z.ai / Zhipu GLM model names
        if any(k in model for k in ("glm-", "glm4", "glm-z1", "zhipu", "zai-glm")):
            return self._reorder("zai", base_order)

        # Pollinations model names
        if "pollinations" in model or model in ("mistral", "mistral-large", "openai", "claude"):
            # "openai" and "claude" are ambiguous; prefer Pollinations only if
            # the literal model string is exactly one of their known values
            if model in ("mistral", "mistral-large"):
                return self._reorder("pollinations", base_order)

        # HuggingFace — slash-separated org/model names not handled by OpenRouter
        if any(k in model for k in ("zephyr", "hf", "huggingface", "qwen/qwen", "mistralai/mistral")):
            return self._reorder("huggingface", base_order)

        if "/" in model:                          # OpenRouter format (org/model)
            return self._reorder("openrouter", base_order)

        # ── Token-count routing ──
        if token_est > 100_000:
            # Only Gemini has 1 M+ context; others as last resort
            logger.info(f"Huge context ({token_est} tok) → Gemini primary")
            return self._reorder("gemini", base_order)

        if token_est > 16_000:
            logger.info(f"Large context ({token_est} tok) → Gemini/OpenRouter")
            return self._reorder("gemini", base_order)

        # ── Capability routing ──
        last_msg = self._last_user_message(request)
        if last_msg and self._is_code_related(last_msg):
            logger.info("Code-related request → Gemini/Groq priority")
            return self._reorder("gemini", base_order)

        # ── Speed routing (small prompts) ──
        if token_est < 4_000:
            logger.info(f"Small context ({token_est} tok) → Groq (fastest)")
            return self._reorder("groq", base_order)

        return base_order

    def _reorder(self, primary: str, base: list = None) -> List[str]:
        order = base if base is not None else list(_DEFAULT_PROVIDER_ORDER)
        return [primary] + [p for p in order if p != primary]

    # ------------------------------------------------------------------
    # Per-vendor model hierarchy
    # ------------------------------------------------------------------

    def _model_hierarchy(
        self,
        provider_name: str,
        request: ChatCompletionRequest,
        token_est: int,
        cfg=None,
    ) -> List[str]:
        """
        Return the ordered list of model IDs to try for *provider_name*.

        Steps:
        1. Start from the full VENDOR_MODEL_HIERARCHY for this provider.
        2. Filter to models whose context window ≥ token_est.
           (If nothing passes, fall back to the full list – better than nothing.)
        3. If the requested model matches one in the hierarchy, put it first.
        """
        cfg = cfg or {}
        overrides = cfg.get("model_overrides", {})
        if provider_name in overrides:
            raw_models = overrides[provider_name]
            if raw_models and isinstance(raw_models[0], list):
                full_hierarchy = [(m, c) for m, c in raw_models]
            else:
                full_hierarchy = [(m, 131_072) for m in raw_models]
        else:
            full_hierarchy = VENDOR_MODEL_HIERARCHY.get(provider_name, [])
        if not full_hierarchy:
            return []

        # Context-window filter
        eligible = [(m, ctx) for m, ctx in full_hierarchy if ctx >= token_est]
        if not eligible:
            eligible = full_hierarchy  # nothing fits → try all anyway

        model_ids = [m for m, _ in eligible]

        # ── Explicit-model pinning ────────────────────────────────────
        # If the caller named a specific model AND that model is present in
        # the provider's catalogue, try ONLY that model (with key rotation).
        # Silently falling back to a different model leads to the "I chose
        # #4 but got #1" bug the user reported in the playground.
        # "auto" is a magic value meaning "let the router pick freely".
        requested_raw = (request.model or "").strip()
        requested     = requested_raw.lower()
        if requested and requested != "auto":
            full_ids_lower = {m.lower(): m for m, _ in full_hierarchy}
            # Exact match (case-insensitive) → pin to that single model
            if requested in full_ids_lower:
                return [full_ids_lower[requested]]
            # Not in our curated list, but caller is explicit — try as-is
            # against the provider (Routeway/OpenRouter have 100+ models we
            # don't enumerate; upstream will return a clear 404 if invalid).
            return [requested_raw]

        # ── Auto-routing: bubble up a partial match if any ─────────────
        # Kept for backwards compat when callers pass e.g. "gpt-4o" without
        # knowing the exact canonical id.  Uses longest-match to avoid the
        # classic "gpt-4o" matching "gpt-4o-mini" first.
        if requested:
            matches = [
                m for m, _ in full_hierarchy
                if requested == m.lower() or requested in m.lower() or m.lower() in requested
            ]
            if matches:
                # Prefer longest match (most specific); stable otherwise
                best = max(matches, key=len)
                if best in model_ids and model_ids[0] != best:
                    model_ids.remove(best)
                    model_ids.insert(0, best)

        # ── Per-model enable/disable filter (from state_store) ──
        # User can toggle individual models off in Settings → Models;
        # those are excluded from routing here.
        try:
            from app.state_store import filter_enabled_models
            filtered = filter_enabled_models(provider_name, model_ids)
            # Keep at least one model — if the user disabled everything, fall
            # back to the full list so we never silently drop the provider.
            if filtered:
                model_ids = filtered
        except Exception as exc:
            logger.debug(f"Model-enable filter skipped for {provider_name}: {exc}")

        return model_ids

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_tokens(self, request: ChatCompletionRequest) -> int:
        return sum(
            int(len(m.content.split()) * 1.3)
            for m in request.messages
            if isinstance(m.content, str)
        )

    def _last_user_message(self, request: ChatCompletionRequest) -> Optional[str]:
        for msg in reversed(request.messages):
            if msg.role == "user" and isinstance(msg.content, str):
                return msg.content
        return None

    def _is_code_related(self, text: str) -> bool:
        text_lower = text.lower()
        return any(kw in text_lower for kw in CODE_KEYWORDS)

    async def _inc(self, stat: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.incr(f"arbiter:stats:{stat}")
        except Exception as exc:
            logger.debug(f"Stats increment failed ({stat}): {exc}")

    async def _incrby(self, stat: str, amount: int) -> None:
        if self.redis is None or amount <= 0:
            return
        try:
            await self.redis.incrby(f"arbiter:stats:{stat}", amount)
        except Exception as exc:
            logger.debug(f"Stats incrby failed ({stat}): {exc}")
