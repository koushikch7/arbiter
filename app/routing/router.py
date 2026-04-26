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
from typing import Dict, List, Optional, Set, Tuple

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
from app.cache.cache import CacheLayer
from app.key_management.key_pool import KeyPool

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


# Default provider priority (updated to include all new providers)
# ── Free-first strategy ───────────────────────────────────────────────────
# Providers with unlimited / generous free tiers come first. Paid-with-trial
# providers (routeway, lightning) are last-resort so unbilled user traffic
# hits zero-cost providers by default.  See VENDOR_MODEL_HIERARCHY above —
# each provider's model list is also sorted free-tier first.
_DEFAULT_PROVIDER_ORDER: List[str] = [
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
    "lightning",     # $0.09-0.52/M tokens (paid only — last resort)
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

    async def _get_custom_config(self) -> dict:
        """Load custom routing config from Redis, cached for 30s."""
        now = time.monotonic()
        if now - self._cfg_cache_ts < 30 and self._cfg_cache:
            return self._cfg_cache
        cfg: dict = {"provider_order": None, "model_overrides": {}}
        if self.redis:
            try:
                raw = await self.redis.get("arbiter:config:provider_order")
                if raw:
                    cfg["provider_order"] = json.loads(raw)
                for p in _DEFAULT_PROVIDER_ORDER:
                    raw = await self.redis.get(f"arbiter:config:models:{p}")
                    if raw:
                        cfg["model_overrides"][p] = json.loads(raw)
            except Exception as e:
                logger.debug(f"Config load error: {e}")
        self._cfg_cache = cfg
        self._cfg_cache_ts = now
        return cfg

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
        """
        # Apply overrides
        if force_model:
            request = request.model_copy(update={"model": force_model})

        cfg = await self._get_custom_config()

        # ── 1. Cache lookup ──────────────────────────────────────────
        cache_key = self.cache.make_key(request)
        if request.temperature <= 0.3:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                await self._inc("cache_hits")
                await self._inc("requests_total")
                await self._inc("requests_success")
                logger.info(f"Cache HIT  model={request.model}")
                return cached
        await self._inc("cache_misses")

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
        logger.info(
            f"Routing  model={request.model!r}  tokens≈{token_est}  "
            f"candidates={candidates[:8]}{'…' if len(candidates) > 8 else ''}"
        )

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

            while True:
                key = await key_pool.get_best_key(exclude=tried_keys)
                if key is None:
                    logger.warning(
                        f"[{provider_name}/{model_name}] "
                        f"No available key after trying {len(tried_keys)} account(s)"
                    )
                    break  # → next candidate

                tried_keys.add(key)
                routed = request.model_copy(update={"model": model_name})

                try:
                    logger.info(
                        f"→ {provider_name}/{model_name}  "
                        f"key=...{key[-4:]}  attempt={len(tried_keys)}"
                    )
                    response = await provider.complete(routed, key)

                    # ── SUCCESS ──────────────────────────────────
                    tokens_used = (
                        response.usage.total_tokens if response.usage else 0
                    )
                    await key_pool.record_usage(key, tokens_used)

                    if request.temperature <= 0.3:
                        await self.cache.set(cache_key, response)

                    await self._inc("requests_total")
                    await self._inc("requests_success")
                    await self._inc(f"provider:{provider_name}:success")

                    # Per-model analytics
                    safe_model = model_name.replace(":", "_").replace("/", "_")[:80]
                    await self._inc(f"model:{safe_model}:requests")
                    if tokens_used > 0:
                        await self._incrby(f"model:{safe_model}:tokens", tokens_used)

                    bucket = (int(time.time()) // 300) * 300
                    await self._inc(f"history:{bucket}:requests")
                    await self._inc(f"history:{bucket}:success")

                    logger.info(
                        f"✓ {provider_name}/{model_name}  tokens={tokens_used}"
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
                    await key_pool.mark_failed(key)
                    await self._inc(f"provider:{provider_name}:rate_limited")
                    last_error = exc
                    # try next account for the SAME (provider, model)

                except ProviderError as exc:
                    logger.error(
                        f"✗ ProviderError {provider_name}/{model_name}: {exc}"
                    )
                    await self._inc(f"provider:{provider_name}:errors")
                    safe_model = model_name.replace(":", "_").replace("/", "_")[:80]
                    await self._inc(f"model:{safe_model}:errors")
                    bucket = (int(time.time()) // 300) * 300
                    await self._inc(f"history:{bucket}:errors")
                    last_error = exc
                    break  # → next candidate

                except Exception as exc:
                    logger.exception(
                        f"✗ Unexpected {provider_name}/{model_name}: {exc}"
                    )
                    last_error = exc
                    break

        # ── 4. All options exhausted ──────────────────────────────────
        await self._inc("requests_total")
        await self._inc("requests_failed")
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

        # ── (c) explicit model — strict pin by default ──────────────
        fallback_mode = (request.fallback or "none").strip().lower()
        if fallback_mode not in ("none", "same_provider", "chain"):
            fallback_mode = "none"

        owning_provider = provider_of(requested) or self._infer_provider(requested)
        primary: List[Tuple[str, str]]
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
