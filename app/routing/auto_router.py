"""
Smart auto-routing — scores (provider, model) pairs against a classified
intent + admin preferences, returning a quality-ordered candidate chain.

Score formula (max 100):
    capability_match  · 40   intent ∈ model.tags
    quality_rank      · 25   1..5 mapped to 0..25
    speed_rank        · 15   1..5 mapped to 0..15
    provider_pref     · 20   prefer-list rank (top → 20, then 15, 10, 5, 0)

`avoid_providers` zero-scores every model on those providers.
`<intent>_models_preference` lists override the result by promoting any
listed models to the front in user order.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from app.providers._free_tier_catalog import (
    FREE_TIER_CATALOG,
    PAID_FALLBACK_CATALOG,
    ModelSpec,
    all_specs,
    find_spec,
    provider_of,
)
from app.routing.intent_classifier import Intent, classify
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
    "creative":     ("creative", "balanced"),
    "long-context": ("long-context", "balanced"),
    "vision":       ("vision",),
    "fast":         ("fast", "balanced"),
    "balanced":     ("balanced",),
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


def _score(
    spec: ModelSpec,
    provider: str,
    intent: Intent,
    priority: str,
    prefer_providers: List[str],
    token_est: int,
) -> int:
    # Hard filter: vision intent requires multimodal/vision capability
    if intent == "vision" and spec.modality not in ("multimodal", "vision"):
        return 0
    # Hard filter: context window must fit (with 10% safety margin)
    if spec.context < int(token_est * 1.1):
        return 0

    # ── Capability match ──
    preferred_tags = INTENT_TAGS.get(intent, ("balanced",))
    cap_score = 0
    for i, tag in enumerate(preferred_tags):
        if tag in spec.tags:
            cap_score = max(cap_score, 40 - i * 8)
    # Always give at least a baseline if no match (let scoring continue).

    # ── Quality / speed (priority-weighted) ──
    if priority == "quality":
        quality_w, speed_w = 35, 5
    elif priority == "speed":
        quality_w, speed_w = 10, 30
    else:  # balanced
        quality_w, speed_w = 25, 15

    quality_score = (spec.quality - 1) * (quality_w / 4)
    speed_score   = (spec.speed   - 1) * (speed_w   / 4)

    # ── Provider preference ──
    pref_score = 0
    if prefer_providers and provider in prefer_providers:
        rank = prefer_providers.index(provider)
        pref_score = max(0, 20 - rank * 5)

    return int(cap_score + quality_score + speed_score + pref_score)


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
    pref_key = INTENT_PREF_KEY.get(intent, "")
    intent_overrides: List[str] = list(prefs.get(pref_key, [])) if pref_key else []

    # Gather candidates
    rows: List[Tuple[int, str, str]] = []  # (score, provider, model_id)
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
            sc = _score(spec, provider, intent, priority, prefer_providers, token_est)
            if sc <= 0:
                continue
            rows.append((sc, provider, spec.id))

    rows.sort(key=lambda r: -r[0])
    chain: List[Tuple[str, str]] = [(p, m) for _, p, m in rows]

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
        "auto-route intent=%s priority=%s top5=%s",
        intent, priority, chain[:5],
    )
    return chain
