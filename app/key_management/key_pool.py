"""
Key pool management with weighted scoring for multi-account key selection.

Algorithm — "Weighted Availability Score":
  For each key compute:
    rpm_score  = 1 - (rpm_used  / rpm_limit)     weight 0.30  (short-term burst)
    tpm_score  = 1 - (tpm_used  / tpm_limit)     weight 0.20  (token throughput)
    daily_score= 1 - (daily_used/ daily_limit)   weight 0.50  (most critical – no reset)

  composite = rpm_score*0.30 + tpm_score*0.20 + daily_score*0.50

  Keys with a 'failed' flag OR exhausted daily quota are excluded (score = -1).
  The key with the highest composite score is returned, ensuring load is spread
  naturally across accounts proportional to remaining capacity.
"""

import hashlib
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-provider free-tier limits – sourced from official docs (March 2026)
#
# Gemini  https://ai.google.dev/gemini-api/docs/rate-limits
#   gemini-2.5-flash-lite  15 RPM · 250K TPM · 1 000 RPD  ← most quota
#   gemini-2.5-flash       10 RPM · 250K TPM ·   250 RPD
#   gemini-2.5-pro          5 RPM · 250K TPM ·   100 RPD  ← least quota
#   We track per-key totals, so use the most conservative across models:
#   RPM=5, TPM=250K, RPD=100  (this is the 2.5-pro bottleneck)
#   ─ If you only use flash / flash-lite you can raise RPD to 250 / 1 000.
#
# Groq  https://console.groq.com/docs/rate-limits
#   llama-3.1-8b-instant     30 RPM ·  6 000 TPM · 14 400 RPD
#   llama-3.3-70b-versatile  30 RPM · 12 000 TPM ·  1 000 RPD
#   (other models 30-60 RPM, 1 000–1 000 RPD)
#   Conservative across all: RPM=30, TPM=6 000, RPD=1 000
#   ─ llama-3.1-8b-instant is tracked separately via routing (gets 14 400 RPD).
#
# OpenRouter  https://openrouter.ai/docs/api/reference/limits
#   No credits:  20 RPM ·    50 RPD  (no TPM published)
#   $10+ credits: 20 RPM · 1 000 RPD
#   Using conservative no-credit defaults; raise RPD to 1000 if you buy credits.
#
# Cohere  https://docs.cohere.com/docs/rate-limits
#   Trial key: 20 RPM · 1 000 calls/month ≈ 33/day
#   No TPM limit published; setting a safe 100K sentinel.
# ---------------------------------------------------------------------------
PROVIDER_LIMITS = {
    "gemini": {
        "rpm":   5,           # most restrictive (gemini-2.5-pro)
        "tpm":   250_000,     # shared across all 2.5 models
        "daily": 100,         # most restrictive (gemini-2.5-pro)
    },
    "groq": {
        "rpm":   30,          # all models (qwen/kimi have 60 but we're conservative)
        "tpm":   6_000,       # most restrictive (llama-3.1-8b-instant)
        "daily": 1_000,       # most restrictive (all except llama-3.1-8b)
    },
    "openrouter": {
        "rpm":   20,          # free tier
        "tpm":   500_000,     # no TPM limit published; sentinel value
        "daily": 50,          # free account without credits; raise to 1000 with $10
    },
    "cohere": {
        "rpm":   20,          # trial key chat endpoint
        "tpm":   100_000,     # no TPM limit published; sentinel value
        "daily": 33,          # ~1 000 calls/month ÷ 30 days
    },
    "cloudflare": {
        "rpm":   300,         # Workers AI free tier
        "tpm":   1_000_000,   # generous; Workers AI bills by "neurons" not tokens
        "daily": 10_000,      # ~10K neurons/day free tier estimate
    },
    "cerebras": {
        "rpm":   30,          # all free models
        "tpm":   60_000,      # most restrictive (llama3.1-8b)
        "daily": 1_000_000,   # 1M tokens/day free tier
    },
    "huggingface": {
        "rpm":   10,          # conservative — free Inference API
        "tpm":   50_000,      # ~$0.10/month credits equivalent
        "daily": 500,         # free inference budget
    },
    "pollinations": {
        "rpm":   5,           # IP-rate-limited free service
        "tpm":   100_000,     # no published TPM; sentinel
        "daily": 1_000,       # no published daily limit; conservative sentinel
    },
    "zai": {
        "rpm":   10,          # free trial — concurrency-based; ~10 RPM conservative estimate
        "tpm":   200_000,     # no published TPM limit; sentinel
        "daily": 1_000,       # no published daily limit; conservative sentinel
        # ↑ Update these after checking z.ai/manage-apikey/rate-limits in your dashboard
    },
    "modal": {
        "rpm":   60,          # serverless — no hard RPM limit; conservative sentinel
        "tpm":   500_000,     # no TPM limit; sentinel
        "daily": 100_000,     # ~$30/month free tier estimate on typical usage
    },
    "lightning": {
        # Lightning.ai LitAI — pay-per-token, no documented RPM limit
        # ~37M token welcome credit on signup
        # Pricing: $0.09–$0.52 per million tokens
        "rpm":   20,          # no published limit; conservative sentinel matching OpenRouter
        "tpm":   500_000,     # no published TPM limit; sentinel
        "daily": 1_000,       # no published daily limit; conservative sentinel
    },
    "routeway": {
        # Routeway — unified gateway. Limits are not publicly documented in
        # the provider docs; conservative sentinel values. Update after
        # checking your Routeway dashboard.
        "rpm":   60,          # sentinel — Routeway advertises "high throughput"
        "tpm":   500_000,     # sentinel
        "daily": 10_000,      # sentinel — mix of free + paid models
    },
    "custom": {
        # Default limits for user-added custom OpenAI-compatible providers.
        # Users can override per-provider in the UI in the future.
        "rpm":   60,
        "tpm":   500_000,
        "daily": 10_000,
    },
}

# Score weights
_W_RPM   = 0.30
_W_TPM   = 0.20
_W_DAILY = 0.50


def _hash_key(api_key: str) -> str:
    """Short, stable identifier for a key (never stored in plaintext)."""
    return hashlib.md5(api_key.encode()).hexdigest()[:10]


class KeyPool:
    """
    Manages *multiple* API keys for a single provider.

    Key selection uses a weighted composite availability score so that:
    • Keys with more remaining daily quota are preferred over freshly-reset ones.
    • RPM bursts on one account don't block requests – other accounts absorb them.
    • Failed/rate-limited keys are transparently skipped and retried after cooldown.
    """

    def __init__(
        self,
        provider: str,
        keys: List[str],
        redis_client,
        rpm_limit: int,
        tpm_limit: int,
        daily_limit: int,
    ):
        self.provider    = provider
        self.keys        = keys          # raw API key strings
        self.redis       = redis_client
        self.rpm_limit   = rpm_limit
        self.tpm_limit   = tpm_limit
        self.daily_limit = daily_limit

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get_best_key(self, exclude: Optional[Set[str]] = None) -> Optional[str]:
        """
        Return the key with the highest weighted availability score,
        skipping any keys in *exclude* (already tried this request).

        Returns None when every key is exhausted or on cooldown.
        """
        exclude = exclude or set()
        best_key   : Optional[str] = None
        best_score : float         = -1.0

        for key in self.keys:
            if key in exclude:
                continue
            score = await self._score_key(key)
            logger.debug(
                f"[{self.provider}] key={_hash_key(key)} score={score:.3f}"
            )
            if score > best_score:
                best_score = score
                best_key   = key

        if best_key is None or best_score <= 0:
            logger.warning(
                f"[{self.provider}] No available key "
                f"(all {len(self.keys)} keys exhausted or on cooldown)"
            )
            return None

        logger.debug(
            f"[{self.provider}] Selected key={_hash_key(best_key)} "
            f"score={best_score:.3f}"
        )
        return best_key

    async def record_usage(self, key: str, tokens_used: int) -> None:
        """Increment sliding-window RPM, TPM and daily counters for *key*.

        daily counter tracks API *calls* (not tokens) because all per-provider
        daily limits in PROVIDER_LIMITS are expressed as requests-per-day (RPD).
        TPM already captures token throughput on a 60s window.
        """
        h      = _hash_key(key)
        prefix = f"{self.provider}:{h}"

        pipe = self.redis.pipeline()
        # RPM – 60 s sliding window (request count)
        pipe.incr(f"{prefix}:rpm")
        pipe.expire(f"{prefix}:rpm", 60)
        # TPM – 60 s sliding window (token count)
        pipe.incrby(f"{prefix}:tpm", max(tokens_used, 1))
        pipe.expire(f"{prefix}:tpm", 60)
        # Daily – 24 h window (request count — matches RPD limits in PROVIDER_LIMITS)
        pipe.incr(f"{prefix}:daily")
        pipe.expire(f"{prefix}:daily", 86_400)
        await pipe.execute()

        logger.debug(
            f"[{self.provider}] key={h} recorded +1 RPM/daily, +{tokens_used} TPM"
        )

    async def mark_failed(self, key: str, cooldown_seconds: int = 300) -> None:
        """
        Mark *key* as failed.  It will be excluded from selection for
        *cooldown_seconds* (default 5 min).  On 429s this backs off the
        specific account without blocking others.
        """
        h = _hash_key(key)
        await self.redis.set(f"{self.provider}:{h}:failed", "1", ex=cooldown_seconds)
        logger.warning(
            f"[{self.provider}] key={h} on cooldown for {cooldown_seconds}s"
        )

    async def get_stats(self) -> Dict:
        """Return per-key stats for the dashboard."""
        stats: Dict = {
            "provider":    self.provider,
            "total_keys":  len(self.keys),
            "active_keys": 0,
            "keys":        [],
        }

        for key in self.keys:
            h      = _hash_key(key)
            prefix = f"{self.provider}:{h}"

            failed      = await self.redis.get(f"{prefix}:failed")
            rpm_used    = int(await self.redis.get(f"{prefix}:rpm")   or 0)
            tpm_used    = int(await self.redis.get(f"{prefix}:tpm")   or 0)
            daily_used  = int(await self.redis.get(f"{prefix}:daily") or 0)

            score  = await self._score_key(key)
            status = "failed" if failed else (
                "active"  if score > 0.1 else
                "limited" if score > 0   else
                "exhausted"
            )
            if status == "active":
                stats["active_keys"] += 1

            stats["keys"].append({
                "hash":   h,
                "status": status,
                "score":  round(score, 3),
                "rpm":   {"used": rpm_used,   "limit": self.rpm_limit},
                "tpm":   {"used": tpm_used,   "limit": self.tpm_limit},
                "daily": {"used": daily_used, "limit": self.daily_limit},
            })

        # Sort by score descending so the dashboard shows best key first
        stats["keys"].sort(key=lambda k: k["score"], reverse=True)
        return stats

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _score_key(self, key: str) -> float:
        """
        Compute the weighted availability score for *key*.

        Returns -1.0 if the key is on cooldown or has exhausted its daily quota
        (making it ineligible).  Otherwise returns a value in (0, 1].
        """
        h      = _hash_key(key)
        prefix = f"{self.provider}:{h}"

        # Hard exclusions
        if await self.redis.get(f"{prefix}:failed"):
            return -1.0

        rpm_used   = int(await self.redis.get(f"{prefix}:rpm")   or 0)
        tpm_used   = int(await self.redis.get(f"{prefix}:tpm")   or 0)
        daily_used = int(await self.redis.get(f"{prefix}:daily") or 0)

        # Daily exhaustion is a hard exclusion (no reset until midnight)
        if daily_used >= self.daily_limit:
            return -1.0

        # Compute availability ratios (clipped to [0, 1])
        rpm_avail   = max(0.0, 1.0 - rpm_used   / self.rpm_limit)
        tpm_avail   = max(0.0, 1.0 - tpm_used   / self.tpm_limit)
        daily_avail = max(0.0, 1.0 - daily_used / self.daily_limit)

        score = rpm_avail * _W_RPM + tpm_avail * _W_TPM + daily_avail * _W_DAILY

        # If RPM is fully saturated the key can still work after the minute resets,
        # but we deprioritise it heavily.  A score > 0 means the key is usable.
        return score
