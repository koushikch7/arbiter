"""
Smart auto-routing — scores (provider, model) pairs against classified
intent + request complexity + admin preferences, returning a quality-ordered
candidate chain with provider diversity guarantees.

v1.19.0 — Complexity-Aware Scoring
────────────────────────────────────
The router now analyses request complexity (TRIVIAL → EXPERT) and uses
that to BOOST powerful models for hard requests and PENALISE them for
trivial ones (preserving fast-model preference for simple tasks).

Score formula (max ~145, dynamic):
    capability_match  · 35   intent ∈ model.tags
    quality_rank      · 30   weighted by complexity (trivial→10, expert→45)
    speed_rank        · 20   inverse-weighted by complexity
    provider_diversity· 15   bonus for under-utilised providers
    complexity_fit    · 15   bonus when model quality matches request complexity
    provider_pref     · 15   admin-configured preference
    quota_capacity    · 12   bonus for high-RPD/RPM models (preserves scarce quota)

Provider Diversity
──────────────────
To prevent traffic concentration on 1-2 providers, the router applies a
diversity bonus: providers that appear less frequently in the top-N
candidates get a scoring boost. This ensures all configured providers
participate in serving requests, distributing load and maximising the
aggregate free-tier capacity across all accounts.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

from app.providers._free_tier_catalog import (
    FREE_TIER_CATALOG,
    PAID_FALLBACK_CATALOG,
    ModelSpec,
    all_specs,
    find_spec,
    provider_of,
)
from app.routing.intent_classifier import Intent, classify
from app.routing.complexity_analyzer import (
    Complexity,
    analyze_complexity,
    minimum_quality_for_complexity,
)
from app.state_store import (
    filter_enabled_models,
    get_auto_route_preferences,
    is_model_enabled,
)

logger = logging.getLogger(__name__)


# Map intent → preferred capability tags (in priority order).
INTENT_TAGS: Dict[str, Tuple[str, ...]] = {
    "code":         ("code", "reasoning", "balanced"),
    "reasoning":    ("reasoning", "code", "balanced"),
    "creative":     ("creative", "reasoning", "balanced"),
    "long-context": ("long-context", "balanced"),
    "vision":       ("vision", "balanced"),
    "fast":         ("fast", "balanced"),
    "balanced":     ("balanced", "reasoning", "creative"),
}

# Map intent → which preference list overrides the catalog ranking.
INTENT_PREF_KEY: Dict[str, str] = {
    "code":         "code_models_preference",
    "reasoning":    "reasoning_models_preference",
    "creative":     "creative_models_preference",
    "long-context": "long_context_models_preference",
    "vision":       "vision_models_preference",
    "fast":         "fast_models_preference",
    "balanced":     "",
}

# Complexity-based weight adjustments for quality vs speed scoring.
# Higher complexity = we care much more about quality than speed.
_COMPLEXITY_WEIGHTS: Dict[Complexity, Tuple[float, float]] = {
    #                     (quality_w, speed_w)
    Complexity.TRIVIAL:  (8,  30),    # Speed matters most
    Complexity.SIMPLE:   (15, 22),    # Speed still important
    Complexity.MODERATE: (25, 15),    # Balanced
    Complexity.COMPLEX:  (38, 8),     # Quality dominates
    Complexity.EXPERT:   (45, 5),     # Quality almost everything
}

# Provider diversity: how many unique providers we want in the top-N candidates.
# If fewer than this many providers appear in top candidates, we boost others.
_DIVERSITY_TARGET = 5


def _score(
    spec: ModelSpec,
    provider: str,
    intent: Intent,
    complexity: Complexity,
    priority: str,
    prefer_providers: List[str],
    token_est: int,
    provider_counts: Dict[str, int],
    total_scored: int,
) -> float:
    """Score a (provider, model) candidate. Returns float score."""
    # Hard filter: vision intent requires multimodal/vision capability
    if intent == "vision" and spec.modality not in ("multimodal", "vision"):
        return 0.0
    # Hard filter: context window must fit (with 10% safety margin)
    if spec.context < int(token_est * 1.1):
        return 0.0

    # ── Capability match (max 35) ──
    preferred_tags = INTENT_TAGS.get(intent, ("balanced",))
    cap_score = 0.0
    for i, tag in enumerate(preferred_tags):
        if tag in spec.tags:
            cap_score = max(cap_score, 35.0 - i * 7.0)
    # If the model has zero tag match but is high quality, give a small baseline
    if cap_score == 0 and spec.quality >= 4:
        cap_score = 5.0

    # ── Quality / speed (complexity-weighted) ──
    # Priority override can shift weights, but complexity is the primary driver
    base_quality_w, base_speed_w = _COMPLEXITY_WEIGHTS[complexity]

    if priority == "quality":
        quality_w = min(base_quality_w * 1.3, 50)
        speed_w = base_speed_w * 0.6
    elif priority == "speed":
        quality_w = base_quality_w * 0.5
        speed_w = min(base_speed_w * 1.4, 35)
    else:  # balanced — use complexity weights directly
        quality_w = base_quality_w
        speed_w = base_speed_w

    quality_score = (spec.quality - 1) * (quality_w / 4)
    speed_score = (spec.speed - 1) * (speed_w / 4)

    # ── Complexity fit bonus (max 15) ──
    # Reward models whose quality level matches the request complexity.
    # Expert request + quality-5 model = +15. Trivial request + quality-1 model = +15.
    min_quality = minimum_quality_for_complexity(complexity)
    complexity_fit = 0.0
    if spec.quality >= min_quality:
        # Model is adequate — bonus scales with how well it matches
        if complexity >= Complexity.COMPLEX:
            # For complex/expert: strongly prefer the highest quality
            complexity_fit = (spec.quality / 5.0) * 15.0
        elif complexity <= Complexity.SIMPLE:
            # For trivial/simple: prefer fast models, don't over-allocate
            # But still give some bonus to decent models (they handle simple fine)
            if spec.quality <= 3:
                complexity_fit = 12.0  # Good match — efficient use
            else:
                complexity_fit = 5.0   # Powerful model for simple task — acceptable but not ideal
        else:
            # Moderate — balanced bonus
            complexity_fit = 10.0 if spec.quality >= 3 else 6.0
    else:
        # Model is below minimum quality for this complexity — penalise
        deficit = min_quality - spec.quality
        complexity_fit = -deficit * 8.0  # Strong penalty

    # ── Provider diversity bonus (max 15) ──
    # v1.20: only fires when the model is already at or near the quality
    # tier required by the complexity. This prevents diversity from
    # pulling a weaker model above a stronger one on EXPERT requests.
    diversity_bonus = 0.0
    min_q_for_complexity = minimum_quality_for_complexity(complexity)
    quality_gap = max(0, min_q_for_complexity - spec.quality)
    diversity_allowed = quality_gap == 0 and (
        complexity <= Complexity.MODERATE or spec.quality >= 4
    )
    if diversity_allowed and total_scored > 0:
        provider_share = provider_counts.get(provider, 0) / max(total_scored, 1)
        # If this provider has less than fair share, boost it
        fair_share = 1.0 / max(len(provider_counts), 1)
        if provider_share < fair_share:
            diversity_bonus = 12.0
        elif provider_share < fair_share * 2:
            diversity_bonus = 6.0
    elif total_scored == 0:
        # First candidates — give diversity to non-top providers
        diversity_bonus = 8.0

    # ── Provider preference (max 15) ──
    pref_score = 0.0
    if prefer_providers and provider in prefer_providers:
        rank = prefer_providers.index(provider)
        pref_score = max(0.0, 15.0 - rank * 4.0)

    # ── Quota capacity bonus (max 12) ──
    # Models/providers with generous free-tier limits get a bonus.
    # This naturally steers traffic toward high-capacity endpoints,
    # preserving scarce quota (e.g. OpenRouter 50 RPD, Cohere 33 RPD)
    # for when their unique capabilities are truly needed.
    # RPD is the primary signal (daily budget is the binding constraint).
    quota_bonus = 0.0
    if spec.rpd is not None:
        if spec.rpd >= 10_000:
            quota_bonus = 12.0   # Cerebras, Cloudflare, Routeway — massive headroom
        elif spec.rpd >= 1_000:
            quota_bonus = 9.0    # Groq, NVIDIA, Gemini flash, Ollama — generous
        elif spec.rpd >= 200:
            quota_bonus = 5.0    # OpenRouter :free, Gemini 2.5-flash — moderate
        elif spec.rpd >= 50:
            quota_bonus = 2.0    # OpenRouter no-credit — tight
        else:
            quota_bonus = 0.0    # Cohere trial (33 RPD) — very tight, save for unique tasks
    else:
        # No RPD published (HuggingFace, Pollinations, etc.) — assume moderate
        quota_bonus = 7.0

    # For high-RPM providers, give additional small boost (max +3)
    if spec.rpm is not None and spec.rpm >= 60:
        quota_bonus = min(quota_bonus + 3.0, 12.0)

    total = (cap_score + quality_score + speed_score + complexity_fit
             + diversity_bonus + pref_score + quota_bonus)

    # ── Load distribution jitter (max ±6) ──
    # Deterministic pseudo-random offset based on provider + model + minute.
    # Using model ID in the hash ensures different models from the same
    # provider get different jitter values, enabling intra-provider rotation.
    # The ±6 range is large enough to rotate among same-quality models from
    # different providers but small enough to never override a genuine
    # quality tier difference (quality gap between tiers is ~10-15 points).
    minute_seed = int(time.time() // 60)
    jitter_hash = hashlib.md5(f"{provider}:{spec.id}:{minute_seed}".encode()).digest()
    jitter = ((jitter_hash[0] % 13) - 6)  # range: -6 to +6

    return max(total + jitter, 0.1)  # Never return 0 for a valid candidate


def _ensure_provider_diversity(
    chain: List[Tuple[str, str]],
    available_providers: Optional[List[str]],
) -> List[Tuple[str, str]]:
    """
    Ensure the top portion of the candidate chain includes models from
    multiple providers. If the top-8 are dominated by ≤2 providers,
    interleave candidates from under-represented providers.
    """
    if len(chain) <= 8:
        return chain

    top_8 = chain[:8]
    rest = chain[8:]

    # Count providers in top-8
    top_providers: Dict[str, int] = {}
    for p, _ in top_8:
        top_providers[p] = top_providers.get(p, 0) + 1

    # If we already have 5+ providers in top-8, diversity is fine
    if len(top_providers) >= _DIVERSITY_TARGET:
        return chain

    # Find providers in `rest` that aren't well-represented in top
    under_represented: List[Tuple[str, str]] = []
    seen_providers_in_promote: Set[str] = set()
    for p, m in rest:
        if p not in top_providers and p not in seen_providers_in_promote:
            under_represented.append((p, m))
            seen_providers_in_promote.add(p)
            if len(under_represented) >= 3:
                break

    if not under_represented:
        return chain

    # Interleave: insert under-represented providers at positions 3, 5, 7
    # (after the top-2 candidates which should be the best matches)
    result = list(top_8[:2])
    insert_idx = 0
    for i, candidate in enumerate(top_8[2:], start=2):
        if insert_idx < len(under_represented) and i in (2, 4, 6):
            promoted = under_represented[insert_idx]
            result.append(promoted)
            if promoted in rest:
                rest.remove(promoted)
            insert_idx += 1
        result.append(candidate)

    # Append any remaining promoted items
    while insert_idx < len(under_represented):
        promoted = under_represented[insert_idx]
        result.append(promoted)
        if promoted in rest:
            rest.remove(promoted)
        insert_idx += 1

    # Append the rest
    result.extend(rest)

    # Deduplicate while preserving order
    seen: Set[Tuple[str, str]] = set()
    deduped: List[Tuple[str, str]] = []
    for item in result:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def auto_candidate_chain(
    request,
    *,
    token_est: int,
    priority_override: Optional[str] = None,
    prefer_provider_override: Optional[str] = None,
    available_providers: Optional[List[str]] = None,
) -> List[Tuple[str, str]]:
    """
    Return an ordered list of ``(provider, model_id)`` candidates for an
    auto-routed request.

    *available_providers* — restrict to providers actually configured + enabled.
    """
    prefs = get_auto_route_preferences()
    priority         = priority_override or prefs.get("priority", "balanced")
    prefer_providers = list(prefs.get("prefer_providers", []))
    avoid_providers  = set(prefs.get("avoid_providers", []))
    allow_paid       = bool(prefs.get("allow_paid_fallback", False))

    if prefer_provider_override:
        # Boost the per-request preferred provider to the very top.
        prefer_providers = [prefer_provider_override] + [
            p for p in prefer_providers if p != prefer_provider_override
        ]

    intent = classify(request)
    complexity = analyze_complexity(request)
    pref_key = INTENT_PREF_KEY.get(intent, "")
    intent_overrides: List[str] = list(prefs.get(pref_key, [])) if pref_key else []

    # For complex/expert requests, override priority to quality unless
    # explicitly set to speed by the caller.
    effective_priority = priority
    if complexity >= Complexity.COMPLEX and priority == "balanced":
        effective_priority = "quality"

    # Gather candidates with complexity-aware scoring
    provider_counts: Dict[str, int] = {}
    rows: List[Tuple[float, str, str]] = []  # (score, provider, model_id)
    total_scored = 0

    for provider, specs in FREE_TIER_CATALOG.items():
        if provider in avoid_providers:
            continue
        if available_providers is not None and provider not in available_providers:
            continue
        catalog = list(specs)
        if allow_paid and provider in PAID_FALLBACK_CATALOG:
            catalog.extend(PAID_FALLBACK_CATALOG[provider])
        for spec in catalog:
            if not is_model_enabled(provider, spec.id):
                continue
            sc = _score(
                spec, provider, intent, complexity, effective_priority,
                prefer_providers, token_est, provider_counts, total_scored,
            )
            if sc <= 0:
                continue
            rows.append((sc, provider, spec.id))
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            total_scored += 1

    rows.sort(key=lambda r: -r[0])
    chain: List[Tuple[str, str]] = [(p, m) for _, p, m in rows]

    # Apply provider diversity — ensure multiple providers in top candidates
    chain = _ensure_provider_diversity(chain, available_providers)

    # Apply intent override list (promote listed models to the top, in order).
    if intent_overrides:
        promoted: List[Tuple[str, str]] = []
        for mid in intent_overrides:
            prov = provider_of(mid)
            if prov and (available_providers is None or prov in available_providers):
                if (prov, mid) in chain:
                    chain.remove((prov, mid))
                promoted.append((prov, mid))
        chain = promoted + chain

    logger.info(
        "auto-route intent=%s complexity=%s priority=%s top8=%s",
        intent, complexity.name, effective_priority, chain[:8],
    )
    return chain
