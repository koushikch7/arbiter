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
import asyncio
import time
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
# ---------------------------------------------------------------------------
# Per-provider free-tier rate limits — verified against official docs on
# 2026-05-26. These are the PER-KEY ceilings the router uses for predictive
# throttling and weighted scoring. Paid-only models (e.g. gemini-2.5-pro)
# are gated separately via Provider.paid_models, so the limits below are
# the highest free-tier ceiling the *free* models in each provider can
# legitimately use.
#
# Per-model overrides live in MODEL_OVERRIDES below — flash-lite gets
# 1000 RPD even though pro is 100 RPD on the same key.
# ---------------------------------------------------------------------------
PROVIDER_LIMITS = {
    "gemini": {
        # gemini-2.5-flash-lite — most generous free model on the key
        # (15 RPM · 250 K TPM · 1 000 RPD). Paid models gated via paid_models.
        "rpm":   15,
        "tpm":   250_000,
        "daily": 1_000,
    },
    "groq": {
        # llama-3.1-8b-instant headline limits (30 RPM · 6 K TPM · 14 400 RPD).
        # Bigger models throttle earlier per MODEL_OVERRIDES.
        "rpm":   30,
        "tpm":   6_000,
        "daily": 14_400,
    },
    "openrouter": {
        # Free models: 20 RPM, 50 RPD without credits; 1 000 RPD with $10+ credits.
        # Configure your credit state via env var if you have credits.
        "rpm":   20,
        "tpm":   500_000,     # no published TPM ceiling
        "daily": 50,
    },
    "cohere": {
        # Trial: 20 RPM, ~1 000 calls/month (≈ 33/day).
        "rpm":   20,
        "tpm":   100_000,     # no published TPM
        "daily": 33,
    },
    "cloudflare": {
        # Text-generation: 300 RPM aggregate. Daily quota = 10,000 neurons free.
        # Large models (~120B) use ~80-150 neurons/call → ~80 calls max.
        # Mixed models average ~50 neurons → ~200 calls. Set conservative limit.
        "rpm":   300,
        "tpm":   1_000_000,
        "daily": 200,   # was 1_000 — actual neuron budget is ~200 large-model calls
    },
    "cerebras": {
        # Per docs (2026-05-26): ALL free models share 5 RPM · 30 K TPM ·
        # 1 M tokens-per-hour · 1 M tokens-per-day. Significantly tightened
        # from the earlier 30 RPM. Daily = 1 000 requests is the practical
        # ceiling assuming ~1 K tokens/request average.
        "rpm":   5,
        "tpm":   30_000,
        "daily": 1_000,
    },
    "huggingface": {
        # $0.10/month credit free tier ≈ a few hundred small chat calls.
        "rpm":   10,
        "tpm":   50_000,
        "daily": 100,
    },
    "pollinations": {
        # Anonymous: 1 req/15 s. Publishable keys (pk_): 1 pollen/hour per
        # (IP + key). We treat per-key as 4 RPM (1/15 s) + generous daily.
        "rpm":   4,
        "tpm":   100_000,
        "daily": 1_000,
    },
    "zai": {
        # Free trial — concurrency-based, conservative sentinel.
        "rpm":   10,
        "tpm":   200_000,
        "daily": 1_000,
    },
    "routeway": {
        "rpm":   60,
        "tpm":   500_000,
        "daily": 10_000,
    },
    "nvidia": {
        # build.nvidia.com — 40 RPM (can request up to 200 RPM). 1 000 sign-up
        # credits, up to 5 000 on request. Treat as 40/500K/1000.
        "rpm":   40,
        "tpm":   500_000,
        "daily": 1_000,
    },
    "ollama": {
        # Ollama Cloud — generous free tier for :cloud-tagged models.
        "rpm":   60,
        "tpm":   500_000,
        "daily": 5_000,
    },
    "custom": {
        "rpm":   60,
        "tpm":   500_000,
        "daily": 10_000,
    },
}

# ---------------------------------------------------------------------------
# Per-model limit overrides — used when a single key covers many models
# with different ceilings (the canonical case is Gemini, where flash-lite
# gets 1 000 RPD on the same key as pro at 100 RPD).
#
# Keys are matched as substrings of the model id, in priority order.
# Example: "gemini-2.5-flash-lite" → {rpm:15, tpm:250_000, daily:1_000}
# ---------------------------------------------------------------------------
MODEL_OVERRIDES = {
    # ── Gemini (flash-lite is the workhorse) ────────────────────────────
    "gemini-2.5-flash-lite":   {"rpm": 15, "tpm": 250_000, "daily": 1_000},
    "gemini-3.1-flash-lite":   {"rpm": 15, "tpm": 250_000, "daily": 1_000},
    "gemini-3-flash":          {"rpm": 10, "tpm": 250_000, "daily":   250},
    "gemini-2.5-flash":        {"rpm": 10, "tpm": 250_000, "daily":   250},
    "gemini-2.0-flash":        {"rpm": 10, "tpm": 250_000, "daily":   250},
    # Paid models — these go through the same KeyPool but are gated to
    # #paid keys via Provider.paid_models; quotas are billing-account based.
    "gemini-2.5-pro":          {"rpm":  5, "tpm": 250_000, "daily":   100},
    "gemini-3-pro":            {"rpm":  5, "tpm": 250_000, "daily":   100},
    "gemini-3.1-pro":          {"rpm":  5, "tpm": 250_000, "daily":   100},

    # ── Groq per-model overrides (docs 2026-05-26) ──────────────────────
    "llama-3.1-8b-instant":    {"rpm": 30, "tpm":  6_000, "daily": 14_400},
    "llama-3.3-70b-versatile": {"rpm": 30, "tpm": 12_000, "daily":  1_000},
    "qwen/qwen3-32b":          {"rpm": 60, "tpm":  6_000, "daily":  1_000},
    "qwen3-32b":               {"rpm": 60, "tpm":  6_000, "daily":  1_000},
    "openai/gpt-oss-120b":     {"rpm": 30, "tpm":  8_000, "daily":  1_000},
    "gpt-oss-120b":            {"rpm": 30, "tpm":  8_000, "daily":  1_000},

    # ── Cloudflare per-model overrides (neurons vary by model size) ──────
    # Small models (8-20B) ~20-40 neurons/call → more daily budget
    # Large models (120B+) ~100-150 neurons/call → tighter daily budget
    "@cf/meta/llama-3.1-8b-instruct-fast": {"rpm": 300, "tpm": 1_000_000, "daily": 400},
    "@cf/openai/gpt-oss-20b":              {"rpm": 300, "tpm": 1_000_000, "daily": 300},
    "@cf/meta/llama-3.3-70b-instruct-fp8": {"rpm": 300, "tpm": 1_000_000, "daily": 150},
    "@cf/openai/gpt-oss-120b":             {"rpm": 300, "tpm": 1_000_000, "daily": 80},
    "@cf/qwen/qwq-32b":                    {"rpm": 300, "tpm": 1_000_000, "daily": 120},
    "@cf/deepseek/deepseek-r1":            {"rpm": 300, "tpm": 1_000_000, "daily": 100},
}


def get_model_limits(provider: str, model: str) -> dict:
    """Return effective (rpm, tpm, daily) limits for a (provider, model).

    Walks MODEL_OVERRIDES first (longest-prefix match), then falls back to
    PROVIDER_LIMITS[provider], then PROVIDER_LIMITS['custom'].
    """
    if model:
        m = model.lower()
        # Longest-match wins so "gemini-2.5-flash-lite" beats "gemini-2.5"
        for key in sorted(MODEL_OVERRIDES.keys(), key=len, reverse=True):
            if key.lower() in m:
                return MODEL_OVERRIDES[key]
    return PROVIDER_LIMITS.get(provider, PROVIDER_LIMITS["custom"])

# Score weights
_W_RPM   = 0.25
_W_TPM   = 0.15
_W_DAILY = 0.40
_W_HEALTH = 0.20   # success-rate / error-history bonus

# Health factor decay
_HEALTH_TTL = 1800     # 30 min sliding window for success/error counters

# Predictive rate-limit threshold: skip a key once it has consumed this
# fraction of its RPM or daily quota. Prevents 429s by routing earlier.
# Tuned from 0.95 → 0.85 (audit finding #1) so the final 15% of capacity
# is reserved as a safety margin for sliding-window drift and concurrent
# requests, without thrashing the key pool when reset is imminent.
_PREDICTIVE_THRESHOLD = 0.85


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

    v1.20 changes:
    • Sliding window TTL bug fixed — RPM/TPM keys use SET EX NX so the TTL
      anchors to first INCR within the window instead of resetting every call.
    • Daily counter date-bucketed (provider:hash:daily:YYYY-MM-DD) so it
      aligns with UTC midnight reset rather than 24h since last call.
    • Per-model limit overrides supported via the *model* argument on
      record_usage/_score_key/get_best_key — flash-lite gets 1000 RPD even
      on the same key as pro at 100 RPD.
    """

    def __init__(
        self,
        provider: str,
        keys: List[str],
        redis_client,
        rpm_limit: int,
        tpm_limit: int,
        daily_limit: int,
        key_tiers: Optional[Dict[str, str]] = None,
    ):
        self.provider    = provider
        self.keys        = keys
        self.redis       = redis_client
        # Provider-default ceilings (used as fallback when no model context).
        self.rpm_limit   = rpm_limit
        self.tpm_limit   = tpm_limit
        self.daily_limit = daily_limit
        self.key_tiers: Dict[str, str] = key_tiers or {}

    # ------------------------------------------------------------------
    # Helpers — limit resolution + key naming
    # ------------------------------------------------------------------
    def _limits_for(self, model: Optional[str]) -> tuple[int, int, int]:
        """Return (rpm, tpm, daily) limits for the given model (or defaults)."""
        if model:
            limits = get_model_limits(self.provider, model)
            return limits["rpm"], limits["tpm"], limits["daily"]
        return self.rpm_limit, self.tpm_limit, self.daily_limit

    @staticmethod
    def _today_utc() -> str:
        """UTC date string used to bucket daily counters."""
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _daily_key(self, hash_: str) -> str:
        return f"{self.provider}:{hash_}:daily:{self._today_utc()}"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get_best_key(
        self,
        exclude: Optional[Set[str]] = None,
        required_tier: Optional[str] = None,
        estimated_request_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> Optional[str]:
        exclude = exclude or set()

        async def _pick_once() -> tuple[Optional[str], float]:
            best_key   : Optional[str] = None
            best_score : float         = -1.0
            for key in self.keys:
                if key in exclude:
                    continue
                if required_tier:
                    tier = self.key_tiers.get(key, "free")
                    if required_tier == "paid" and tier != "paid":
                        continue
                score = await self._score_key(
                    key,
                    estimated_request_tokens=estimated_request_tokens,
                    model=model,
                )
                logger.debug(
                    f"[{self.provider}] key={_hash_key(key)} score={score:.3f}"
                )
                if score > best_score:
                    best_score = score
                    best_key   = key
            return best_key, best_score

        best_key, best_score = await _pick_once()

        if (best_key is None or best_score <= 0):
            seconds_to_reset = 60 - (time.time() % 60)
            if seconds_to_reset < 10 and await self._any_rpm_throttled(exclude, required_tier, model):
                logger.info(
                    "[%s] All keys RPM-throttled; waiting %.1fs for window reset (Gap B)",
                    self.provider, seconds_to_reset,
                )
                await asyncio.sleep(seconds_to_reset + 0.1)
                best_key, best_score = await _pick_once()

        if best_key is None or best_score <= 0:
            logger.warning(
                f"[{self.provider}] No available key "
                f"(all {len(self.keys)} keys exhausted or on cooldown"
                + (f", required_tier={required_tier}" if required_tier else "")
                + (f", model={model}" if model else "")
                + ")"
            )
            return None

        logger.debug(
            f"[{self.provider}] Selected key={_hash_key(best_key)} "
            f"score={best_score:.3f}"
        )
        return best_key

    async def record_usage(
        self,
        key: str,
        tokens_used: int,
        model: Optional[str] = None,
    ) -> None:
        """Increment counters for *key* (and optionally per-model).

        Uses SET-with-NX semantics for short windows (RPM/TPM) so the TTL
        is anchored to the FIRST hit within the window, not refreshed on
        every subsequent INCR. Daily counter is date-bucketed against UTC
        midnight so it auto-rolls.
        """
        h = _hash_key(key)
        prefix = f"{self.provider}:{h}"
        daily_key = self._daily_key(h)
        rpm_key = f"{prefix}:rpm"
        tpm_key = f"{prefix}:tpm"

        async def _incr_with_anchored_ttl(redis_key: str, by: int, ttl: int):
            # Anchor TTL only on the first INCR in this window:
            # SET key 0 EX ttl NX, then INCRBY.
            await self.redis.set(redis_key, 0, ex=ttl, nx=True)
            await self.redis.incrby(redis_key, by)

        await _incr_with_anchored_ttl(rpm_key, 1, 60)
        await _incr_with_anchored_ttl(tpm_key, max(tokens_used, 1), 60)

        # Daily — bucketed by UTC date so it resets at 00:00 UTC, with a
        # 30-hour TTL safety margin (covers timezone slop / clock drift).
        pipe = self.redis.pipeline()
        pipe.incr(daily_key)
        pipe.expire(daily_key, 30 * 3600)
        # Health — 30-min sliding success counter (TTL refresh OK here:
        # we want this to track recent state, not anchor to a fixed window).
        pipe.incr(f"{prefix}:ok")
        pipe.expire(f"{prefix}:ok", _HEALTH_TTL)
        # Per-model counters (used by per-model availability scoring).
        if model:
            mh = _model_slug(model)
            pipe.incr(f"{prefix}:m:{mh}:rpm")
            pipe.expire(f"{prefix}:m:{mh}:rpm", 60)
            pipe.incrby(f"{prefix}:m:{mh}:tpm", max(tokens_used, 1))
            pipe.expire(f"{prefix}:m:{mh}:tpm", 60)
            pipe.incr(f"{prefix}:m:{mh}:daily:{self._today_utc()}")
            pipe.expire(f"{prefix}:m:{mh}:daily:{self._today_utc()}", 30 * 3600)
        await pipe.execute()

        logger.debug(
            f"[{self.provider}] key={h} model={model} recorded +1 RPM/daily/ok, +{tokens_used} TPM"
        )

    async def record_error(self, key: str) -> None:
        h = _hash_key(key)
        prefix = f"{self.provider}:{h}"
        pipe = self.redis.pipeline()
        pipe.incr(f"{prefix}:err")
        pipe.expire(f"{prefix}:err", _HEALTH_TTL)
        await pipe.execute()

    async def mark_failed(self, key: str, cooldown_seconds: int = 300) -> None:
        """Mark *key* as failed for *cooldown_seconds*.

        The caller should pass a parsed Retry-After value when available
        (e.g. parsed from a Gemini/Groq 429 body). Defaults to 300 s as a
        safety net for cases where the upstream doesn't surface one.
        """
        cooldown_seconds = max(15, min(int(cooldown_seconds), 3600))
        h = _hash_key(key)
        await self.redis.set(f"{self.provider}:{h}:failed", "1", ex=cooldown_seconds)
        logger.warning(
            f"[{self.provider}] key={h} on cooldown for {cooldown_seconds}s"
        )

    async def get_stats(self) -> Dict:
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
            daily_used  = int(await self.redis.get(self._daily_key(h)) or 0)

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
                "tier":   self.key_tiers.get(key, "free"),
                "rpm":   {"used": rpm_used,   "limit": self.rpm_limit},
                "tpm":   {"used": tpm_used,   "limit": self.tpm_limit},
                "daily": {"used": daily_used, "limit": self.daily_limit},
            })

        stats["keys"].sort(key=lambda k: k["score"], reverse=True)
        return stats

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _any_rpm_throttled(
        self, exclude: Set[str], required_tier: Optional[str], model: Optional[str] = None,
    ) -> bool:
        rpm_limit, _, _ = self._limits_for(model)
        for key in self.keys:
            if key in exclude:
                continue
            if required_tier:
                tier = self.key_tiers.get(key, "free")
                if required_tier == "paid" and tier != "paid":
                    continue
            try:
                h = _hash_key(key)
                prefix = f"{self.provider}:{h}"
                if await self.redis.get(f"{prefix}:failed"):
                    continue
                rpm_used = int(await self.redis.get(f"{prefix}:rpm") or 0)
                if rpm_used >= rpm_limit * _PREDICTIVE_THRESHOLD:
                    return True
            except Exception:
                continue
        return False

    async def _score_key(
        self,
        key: str,
        *,
        estimated_request_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> float:
        h      = _hash_key(key)
        prefix = f"{self.provider}:{h}"

        if await self.redis.get(f"{prefix}:failed"):
            return -1.0

        rpm_limit, tpm_limit, daily_limit = self._limits_for(model)

        rpm_used   = int(await self.redis.get(f"{prefix}:rpm")   or 0)
        tpm_used   = int(await self.redis.get(f"{prefix}:tpm")   or 0)
        daily_used = int(await self.redis.get(self._daily_key(h)) or 0)
        ok_count   = int(await self.redis.get(f"{prefix}:ok")    or 0)
        err_count  = int(await self.redis.get(f"{prefix}:err")   or 0)

        # If a model is given, also check per-model counters (so flash-lite
        # exhaustion on this key doesn't disqualify pro requests, and vice
        # versa).
        if model:
            mh = _model_slug(model)
            m_rpm   = int(await self.redis.get(f"{prefix}:m:{mh}:rpm")   or 0)
            m_tpm   = int(await self.redis.get(f"{prefix}:m:{mh}:tpm")   or 0)
            m_daily = int(await self.redis.get(
                f"{prefix}:m:{mh}:daily:{self._today_utc()}"
            ) or 0)
            # Use whichever is HIGHER — provider-aggregate or per-model —
            # so we respect both ceilings.
            rpm_used   = max(rpm_used, m_rpm)
            tpm_used   = max(tpm_used, m_tpm)
            daily_used = max(daily_used, m_daily)

        if daily_used >= daily_limit:
            return -1.0

        if (
            rpm_used   >= rpm_limit   * _PREDICTIVE_THRESHOLD
            or daily_used >= daily_limit * _PREDICTIVE_THRESHOLD
        ):
            logger.debug(
                f"[{self.provider}] key={h} predictively throttled "
                f"(rpm {rpm_used}/{rpm_limit}, daily {daily_used}/{daily_limit})"
            )
            return -1.0

        rpm_avail   = max(0.0, 1.0 - rpm_used   / rpm_limit)
        tpm_avail   = max(0.0, 1.0 - tpm_used   / tpm_limit)
        daily_avail = max(0.0, 1.0 - daily_used / daily_limit)

        if estimated_request_tokens and estimated_request_tokens > 0:
            remaining_tpm = max(0, tpm_limit - tpm_used)
            needed = int(estimated_request_tokens * 1.1)
            if needed > remaining_tpm:
                tpm_avail = 0.0
            else:
                tpm_avail = max(0.0, (remaining_tpm - needed) / tpm_limit)

        health = (ok_count + 1) / (ok_count + err_count + 2)

        score = (
            rpm_avail   * _W_RPM
            + tpm_avail * _W_TPM
            + daily_avail * _W_DAILY
            + health      * _W_HEALTH
        )

        return score


def _model_slug(model: str) -> str:
    """Short, redis-safe slug for a model identifier."""
    return hashlib.md5(model.lower().encode()).hexdigest()[:8]
