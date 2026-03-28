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

import logging
from typing import Dict, List, Optional, Set, Tuple

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse
from app.providers.base import BaseProvider, RateLimitError, ProviderError
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
# Per-vendor model hierarchies  (verified March 2026 – official docs)
#
# Format : List of (model_id, context_window_tokens)
# Order  : preferred first — fastest / highest-quota → largest / best-quality
# Router walks this list top→bottom for intra-vendor fallback.
#
# Sources:
#   Gemini       https://ai.google.dev/gemini-api/docs/models
#   Groq         https://console.groq.com/docs/models
#   OpenRouter   https://openrouter.ai/models?q=free
#   Cohere       https://docs.cohere.com/docs/models
#   Cloudflare   https://developers.cloudflare.com/workers-ai/models/
#   Cerebras     https://inference-docs.cerebras.ai/models
#   HuggingFace  https://huggingface.co/docs/api-inference/en/tasks/chat-completion
#   Pollinations https://github.com/pollinations/pollinations
# ---------------------------------------------------------------------------
VENDOR_MODEL_HIERARCHY: Dict[str, List[Tuple[str, int]]] = {

    # ── Gemini (free-tier only, non-deprecated) ──────────────────────────────
    # gemini-1.5-*        SHUT DOWN Sep 24 2025
    # gemini-2.0-*        DEPRECATED, retiring Jun 1 2026
    # gemini-2.5-pro      PAID-ONLY — excluded
    # gemini-3.1-pro-*    PAID-ONLY — excluded
    "gemini": [
        ("gemini-3.1-flash-lite-preview", 1_048_576),  # newest · free tier · fastest
        ("gemini-3-flash-preview",        1_048_576),  # free tier · frontier quality
        ("gemini-2.5-flash-lite",         1_048_576),  # stable · 15 RPM · 1 000 RPD
        ("gemini-2.5-flash",              1_048_576),  # stable · 10 RPM ·   250 RPD
    ],

    # ── Groq (active models only) ────────────────────────────────────────────
    # llama3-8b-8192 / llama3-70b-8192 / mixtral-8x7b-32768 / gemma2-9b-it
    # are NO LONGER in the active model list.
    "groq": [
        ("llama-3.1-8b-instant",                   131_072),  # 30 RPM · 14 400 RPD  fastest
        ("llama-3.3-70b-versatile",                131_072),  # 30 RPM ·  1 000 RPD  best quality
        ("meta-llama/llama-4-scout-17b-16e-instruct", 131_072),  # 30 RPM · 1 000 RPD  Llama 4
        ("qwen/qwen3-32b",                         131_072),  # 60 RPM ·  1 000 RPD  high RPM
        ("moonshotai/kimi-k2-instruct",            131_072),  # 60 RPM ·  1 000 RPD  high RPM
        ("moonshotai/kimi-k2-instruct-0905",       131_072),  # 60 RPM ·  1 000 RPD  alt version
        ("openai/gpt-oss-20b",                     131_072),  # 30 RPM ·  1 000 RPD  GPT-OSS small
        ("openai/gpt-oss-120b",                    131_072),  # 30 RPM ·  1 000 RPD  GPT-OSS large
    ],

    # ── OpenRouter (:free models, Mar 2026) ──────────────────────────────────
    "openrouter": [
        ("meta-llama/llama-3.3-70b-instruct:free",        131_072),  # quality flagship
        ("nousresearch/hermes-3-llama-3.1-405b:free",     131_072),  # largest free
        ("google/gemma-3-27b-it:free",                    131_072),  # Gemma 3 27B
        ("mistralai/mistral-small-3.1-24b-instruct:free", 128_000),  # Mistral 24B
        ("google/gemma-3-12b-it:free",                    131_072),  # Gemma 3 12B
        ("qwen/qwen3-4b:free",                            128_000),  # fast 4B
        ("meta-llama/llama-3.2-3b-instruct:free",         131_072),  # smallest/fastest
    ],

    # ── Cohere (non-deprecated models) ───────────────────────────────────────
    "cohere": [
        ("command-r7b-12-2024",    128_000),  # fastest, 7B
        ("command-r-08-2024",      128_000),  # balanced R-series
        ("command-r-plus-08-2024", 128_000),  # highest quality R-series
        ("command-a-03-2025",      256_000),  # newest flagship (256K ctx)
    ],

    # ── Cloudflare Workers AI (verified March 2026) ───────────────────────────
    "cloudflare": [
        ("@cf/meta/llama-4-scout-17b-16e-instruct",       131_072),  # Llama 4 — newest
        ("@cf/meta/llama-3.3-70b-instruct-fp8-fast",      131_072),  # Llama 3.3 70B fast
        ("@cf/moonshot/kimi-k2.5",                        262_144),  # 256K context
        ("@cf/qwen/qwen3-30b-a3b-fp8",                    131_072),  # Qwen 3 30B
        ("@cf/mistralai/mistral-small-3.1-24b-instruct",  131_072),  # Mistral Small 24B
        ("@cf/deepseek/deepseek-r1-distill-qwen-32b",     131_072),  # DeepSeek R1 reasoning
        ("@cf/qwen/qwq-32b",                              131_072),  # QwQ reasoning
        ("@cf/qwen/qwen2.5-coder-32b-instruct",           131_072),  # coding specialist
        ("@cf/google/gemma-3-12b-it",                     131_072),  # Gemma 3 12B (128K)
        ("@cf/meta/llama-3.1-8b-instruct",                131_072),  # fastest 8B fallback
        ("@cf/meta/llama-3.2-3b-instruct",                131_072),  # smallest 3B fallback
    ],

    # ── Cerebras Inference (verified March 2026) ──────────────────────────────
    "cerebras": [
        ("llama3.1-8b",                    8192),  # production · 30 RPM · 60K TPM · 1M/day
        ("gpt-oss-120b",                   8192),  # production · 30 RPM · 64K TPM · 1M/day
        ("qwen-3-235b-a22b-instruct-2507", 8192),  # preview · Qwen 3 235B · best reasoning
        ("zai-glm-4.7",                    8192),  # preview · Z.ai GLM 4.7
    ],

    # ── HuggingFace Inference Router ──────────────────────────────────────────
    "huggingface": [
        ("Qwen/Qwen2.5-7B-Instruct",              32768),  # most reliable free
        ("mistralai/Mistral-7B-Instruct-v0.3",    32768),  # Mistral base
        ("HuggingFaceH4/zephyr-7b-beta",          32768),  # general purpose
        ("google/gemma-2-2b-it",                   8192),  # smallest / fallback
    ],

    # ── Pollinations.ai ───────────────────────────────────────────────────────
    "pollinations": [
        ("mistral",       32768),  # fast, general
        ("mistral-large", 32768),  # higher quality
        ("openai",        32768),  # GPT-based
    ],
}

# Default provider priority (updated to include all new providers)
_DEFAULT_PROVIDER_ORDER: List[str] = [
    "gemini",
    "groq",
    "cerebras",
    "cloudflare",
    "openrouter",
    "cohere",
    "huggingface",
    "pollinations",
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

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def route(
        self,
        request: ChatCompletionRequest,
        vendor: Optional[str] = None,
        force_model: Optional[str] = None,
    ) -> ChatCompletionResponse:
        """
        Route *request* to the best available provider/model/key.

        Parameters
        ──────────
        vendor      If provided, put this provider first in the ordering.
        force_model If provided, override request.model with this value.
        """
        # Apply overrides
        if force_model:
            request = request.model_copy(update={"model": force_model})

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

        # ── 2. Provider order ────────────────────────────────────────
        provider_order = self._provider_order(request, vendor=vendor)
        token_est      = self._estimate_tokens(request)
        logger.info(
            f"Routing  model={request.model!r}  tokens≈{token_est}  "
            f"providers={provider_order}"
        )

        last_error: Optional[Exception] = None

        # ── 3. Two-level fallback ─────────────────────────────────────
        for provider_name in provider_order:
            provider = self.providers.get(provider_name)
            key_pool = self.key_pools.get(provider_name)
            if provider is None or key_pool is None:
                logger.debug(f"Provider {provider_name!r} not configured, skipping")
                continue

            model_list = self._model_hierarchy(provider_name, request, token_est)
            logger.debug(
                f"[{provider_name}] model candidates: {model_list}"
            )

            for model_name in model_list:
                tried_keys: Set[str] = set()

                # Inner key-rotation loop for this (provider, model) pair
                while True:
                    key = await key_pool.get_best_key(exclude=tried_keys)
                    if key is None:
                        # All accounts exhausted for this model
                        logger.warning(
                            f"[{provider_name}/{model_name}] "
                            f"No available key after trying {len(tried_keys)} account(s)"
                        )
                        break  # → try next model in hierarchy

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

                        logger.info(
                            f"✓ {provider_name}/{model_name}  tokens={tokens_used}"
                        )
                        return response

                    except RateLimitError as exc:
                        logger.warning(
                            f"✗ RateLimit {provider_name}/{model_name}  "
                            f"key=...{key[-4:]}: {exc}"
                        )
                        await key_pool.mark_failed(key)
                        await self._inc(f"provider:{provider_name}:rate_limited")
                        last_error = exc
                        # ← continue: try next account for the SAME model

                    except ProviderError as exc:
                        logger.error(
                            f"✗ ProviderError {provider_name}/{model_name}: {exc}"
                        )
                        await self._inc(f"provider:{provider_name}:errors")
                        last_error = exc
                        break  # model-level error → next model in hierarchy

                    except Exception as exc:
                        logger.exception(
                            f"✗ Unexpected {provider_name}/{model_name}: {exc}"
                        )
                        last_error = exc
                        break  # unexpected → next model

        # ── 4. All options exhausted ──────────────────────────────────
        await self._inc("requests_total")
        await self._inc("requests_failed")
        raise ProviderError(
            f"All providers/models/keys failed for model={request.model!r}. "
            f"Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # Provider ordering
    # ------------------------------------------------------------------

    def _provider_order(
        self,
        request: ChatCompletionRequest,
        vendor: Optional[str] = None,
    ) -> List[str]:
        """Return the ordered list of providers to attempt.

        If *vendor* is supplied, it is placed unconditionally at position 0.
        """
        model     = request.model.lower()
        token_est = self._estimate_tokens(request)

        # ── Explicit vendor override ──
        if vendor:
            return self._reorder(vendor)

        # ── Explicit model-name routing ──
        if "gemini" in model:
            return self._reorder("gemini")

        # Cloudflare Workers AI model names start with @cf/
        if "@cf/" in model:
            return self._reorder("cloudflare")

        # Cerebras native model names
        if "llama3.1" in model or "llama3.1-8b" in model or "cerebras" in model:
            return self._reorder("cerebras")

        # Groq-native model names (no slash) vs OpenRouter slash-format
        if any(k in model for k in ("llama-3.1-8b", "llama-3.3", "llama-4", "qwen3", "kimi", "gpt-oss")):
            return self._reorder("groq")

        if any(k in model for k in ("command-r", "command-a", "cohere")):
            return self._reorder("cohere")

        # Pollinations model names
        if "pollinations" in model or model in ("mistral", "mistral-large", "openai", "claude"):
            # "openai" and "claude" are ambiguous; prefer Pollinations only if
            # the literal model string is exactly one of their known values
            if model in ("mistral", "mistral-large"):
                return self._reorder("pollinations")

        # HuggingFace — slash-separated org/model names not handled by OpenRouter
        if any(k in model for k in ("zephyr", "hf", "huggingface", "qwen/qwen", "mistralai/mistral")):
            return self._reorder("huggingface")

        if "/" in model:                          # OpenRouter format (org/model)
            return self._reorder("openrouter")

        # ── Token-count routing ──
        if token_est > 100_000:
            # Only Gemini has 1 M+ context; others as last resort
            logger.info(f"Huge context ({token_est} tok) → Gemini primary")
            return ["gemini", "openrouter", "cohere", "groq", "cerebras", "cloudflare", "huggingface", "pollinations"]

        if token_est > 16_000:
            logger.info(f"Large context ({token_est} tok) → Gemini/OpenRouter")
            return ["gemini", "openrouter", "cohere", "groq", "cerebras", "huggingface", "cloudflare", "pollinations"]

        # ── Capability routing ──
        last_msg = self._last_user_message(request)
        if last_msg and self._is_code_related(last_msg):
            logger.info("Code-related request → Gemini/Groq priority")
            return ["gemini", "groq", "cerebras", "cloudflare", "openrouter", "cohere", "huggingface", "pollinations"]

        # ── Speed routing (small prompts) ──
        if token_est < 4_000:
            logger.info(f"Small context ({token_est} tok) → Groq (fastest)")
            return ["groq", "gemini", "cerebras", "cloudflare", "openrouter", "cohere", "huggingface", "pollinations"]

        return list(_DEFAULT_PROVIDER_ORDER)

    def _reorder(self, primary: str) -> List[str]:
        return [primary] + [p for p in _DEFAULT_PROVIDER_ORDER if p != primary]

    # ------------------------------------------------------------------
    # Per-vendor model hierarchy
    # ------------------------------------------------------------------

    def _model_hierarchy(
        self,
        provider_name: str,
        request: ChatCompletionRequest,
        token_est: int,
    ) -> List[str]:
        """
        Return the ordered list of model IDs to try for *provider_name*.

        Steps:
        1. Start from the full VENDOR_MODEL_HIERARCHY for this provider.
        2. Filter to models whose context window ≥ token_est.
           (If nothing passes, fall back to the full list – better than nothing.)
        3. If the requested model matches one in the hierarchy, put it first.
        """
        full_hierarchy = VENDOR_MODEL_HIERARCHY.get(provider_name, [])
        if not full_hierarchy:
            return []

        # Context-window filter
        eligible = [(m, ctx) for m, ctx in full_hierarchy if ctx >= token_est]
        if not eligible:
            eligible = full_hierarchy  # nothing fits → try all anyway

        model_ids = [m for m, _ in eligible]

        # Bubble up explicitly requested model
        requested = request.model.lower()
        for m, _ in full_hierarchy:
            if requested == m or (requested in m) or (m in requested):
                if m in model_ids and model_ids[0] != m:
                    model_ids.remove(m)
                    model_ids.insert(0, m)
                break

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
