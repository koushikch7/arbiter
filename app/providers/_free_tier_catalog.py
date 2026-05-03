"""
Curated free-tier model catalog — single source of truth for Arbiter v1.12+.

Each entry carries:
  id            — vendor-native model ID (passed verbatim to upstream API)
  context       — context-window in tokens
  tags          — capability tags used by the smart auto-router
  rpm / rpd     — published free-tier limits (None when undisclosed)
  modality      — "text" or "vision"
  quality       — 1 (small/fast) → 5 (flagship) — used for scoring
  speed         — 1 (slow) → 5 (very fast) — used for scoring
  notes         — short human-readable comment

Capability tag vocabulary
─────────────────────────
"code"          — strong on programming / code-completion benchmarks
"reasoning"     — strong on chain-of-thought / math / logic
"long-context"  — verified ≥ 128K context with reliable retrieval
"vision"        — accepts image_url message parts
"fast"          — low latency / high RPM (good for quick chat)
"creative"      — strong on open-ended writing / creative content
"balanced"      — general-purpose default

Verified live April 2026.  Models that returned 4xx / 5xx during probing
are excluded.  Use scripts/test_curated_models.py to re-validate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Set


Modality = Literal["text", "vision", "multimodal"]
Tag = Literal[
    "code", "reasoning", "long-context", "vision", "fast", "creative", "balanced", "large",
]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    context: int
    tags: Set[str] = field(default_factory=set)
    rpm: Optional[int] = None
    rpd: Optional[int] = None
    modality: Modality = "text"
    quality: int = 3            # 1 (small) … 5 (flagship)
    speed: int = 3              # 1 (slow)  … 5 (very fast)
    notes: str = ""

    @property
    def tagset(self) -> Set[str]:
        return set(self.tags)


# ---------------------------------------------------------------------------
# Per-provider free-tier catalog
# ---------------------------------------------------------------------------

FREE_TIER_CATALOG: Dict[str, List[ModelSpec]] = {

    # ── Gemini — Google AI Studio free tier ────────────────────────────
    # https://ai.google.dev/gemini-api/docs/models
    "gemini": [
        ModelSpec(
            id="gemini-2.5-flash-lite", context=1_048_576,
            tags={"long-context", "balanced", "fast", "vision"},
            rpm=15, rpd=1_000, quality=3, speed=4,
            modality="multimodal",
            notes="1M context · highest free RPD on Gemini · multimodal (vision)",
        ),
        ModelSpec(
            id="gemini-2.0-flash-lite", context=1_048_576,
            tags={"long-context", "balanced", "fast", "vision"},
            rpm=30, rpd=1_500, quality=3, speed=5,
            modality="multimodal",
            notes="highest quota on Gemini free tier · multimodal",
        ),
        ModelSpec(
            id="gemini-2.0-flash", context=1_048_576,
            tags={"long-context", "balanced", "fast", "vision"},
            rpm=15, rpd=1_500, quality=4, speed=4,
            modality="multimodal",
        ),
        ModelSpec(
            id="gemini-2.5-flash", context=1_048_576,
            tags={"long-context", "balanced", "creative", "vision"},
            rpm=10, rpd=250, quality=4, speed=4,
            modality="multimodal",
        ),
        ModelSpec(
            id="gemini-2.5-pro", context=1_048_576,
            tags={"long-context", "reasoning", "creative", "large", "vision"},
            rpm=5, rpd=100, quality=5, speed=2,
            modality="multimodal",
            notes="premium (paid only) · best Gemini 2.5 quality",
        ),
        ModelSpec(
            id="gemini-3.1-pro-preview", context=1_048_576,
            tags={"long-context", "reasoning", "creative", "large", "vision", "code"},
            rpm=10, rpd=10_000, quality=5, speed=3,
            modality="multimodal",
            notes="paid only · frontier reasoning · Gemini 3.1 Pro",
        ),
        ModelSpec(
            id="gemini-3-pro-preview", context=1_048_576,
            tags={"long-context", "reasoning", "creative", "large", "vision"},
            rpm=10, rpd=10_000, quality=5, speed=3,
            modality="multimodal",
            notes="paid only · Gemini 3 Pro frontier",
        ),
        ModelSpec(
            id="gemini-3.1-flash-lite-preview", context=1_048_576,
            tags={"long-context", "balanced", "fast", "creative", "vision"},
            rpm=15, rpd=1_500, quality=5, speed=5,
            modality="multimodal",
            notes="free · newest fast preview · TOP free Gemini",
        ),
        ModelSpec(
            id="gemini-3-flash-preview", context=1_048_576,
            tags={"long-context", "balanced", "creative", "fast", "vision"},
            rpm=10, rpd=500, quality=4, speed=4,
            modality="multimodal",
            notes="free · frontier flash preview",
        ),
    ],

    # ── Groq — fastest open-weight inference ───────────────────────────
    # https://console.groq.com/docs/models
    "groq": [
        ModelSpec(
            id="llama-3.1-8b-instant", context=131_072,
            tags={"fast", "balanced"},
            rpm=30, rpd=14_400, quality=2, speed=5,
            notes="14 400 RPD · highest free quota in the stack",
        ),
        ModelSpec(
            id="llama-3.3-70b-versatile", context=131_072,
            tags={"balanced", "reasoning", "creative", "large"},
            rpm=30, rpd=1_000, quality=4, speed=4,
            notes="best free Llama 70B available anywhere",
        ),
        ModelSpec(
            id="meta-llama/llama-4-scout-17b-16e-instruct", context=131_072,
            tags={"balanced", "creative", "reasoning", "vision"},
            rpm=30, rpd=1_000, quality=4, speed=4,
            modality="multimodal",
            notes="Llama 4 Scout MoE · multimodal (vision)",
        ),
        ModelSpec(
            id="qwen/qwen3-32b", context=131_072,
            tags={"balanced", "code", "reasoning"},
            rpm=60, rpd=1_000, quality=4, speed=4,
            notes="60 RPM (highest)",
        ),
        ModelSpec(
            id="openai/gpt-oss-20b", context=131_072,
            tags={"balanced", "code", "reasoning", "fast"},
            rpm=30, rpd=1_000, quality=3, speed=4,
        ),
        ModelSpec(
            id="openai/gpt-oss-120b", context=131_072,
            tags={"reasoning", "code", "creative", "large"},
            rpm=30, rpd=1_000, quality=5, speed=3,
            notes="GPT-OSS 120B flagship",
        ),
    ],

    # ── OpenRouter — :free tier ─────────────────────────────────────────
    # https://openrouter.ai/models?q=free
    "openrouter": [
        ModelSpec(
            id="nousresearch/hermes-3-llama-3.1-405b:free", context=131_072,
            tags={"reasoning", "creative", "large"},
            rpm=20, rpd=1_000, quality=5, speed=2,
            notes="largest free model anywhere · 405B",
        ),
        ModelSpec(
            id="google/gemma-3-27b-it:free", context=131_072,
            tags={"balanced", "creative", "vision"},
            rpm=20, rpd=200, quality=4, speed=3,
            modality="multimodal",
        ),
        ModelSpec(
            id="mistralai/mistral-small-3.1-24b-instruct:free", context=128_000,
            tags={"balanced", "creative", "vision"},
            rpm=20, rpd=200, quality=3, speed=3,
            modality="multimodal",
        ),
        ModelSpec(
            id="google/gemma-3-12b-it:free", context=131_072,
            tags={"balanced", "fast", "vision"},
            rpm=20, rpd=200, quality=3, speed=4,
            modality="multimodal",
        ),
        ModelSpec(
            id="qwen/qwen3-4b:free", context=128_000,
            tags={"fast", "balanced"},
            rpm=20, rpd=200, quality=2, speed=5,
        ),
        ModelSpec(
            id="meta-llama/llama-3.2-3b-instruct:free", context=131_072,
            tags={"fast", "balanced"},
            rpm=20, rpd=200, quality=2, speed=5,
        ),
    ],

    # ── Cohere — trial keys (1000 req / month free) ─────────────────────
    # https://docs.cohere.com/docs/models
    "cohere": [
        ModelSpec(
            id="command-r7b-12-2024", context=128_000,
            tags={"fast", "balanced"},
            rpm=20, rpd=33, quality=2, speed=5,
        ),
        ModelSpec(
            id="command-r-08-2024", context=128_000,
            tags={"balanced", "creative"},
            rpm=20, rpd=33, quality=3, speed=4,
        ),
        ModelSpec(
            id="command-r-plus-08-2024", context=128_000,
            tags={"creative", "reasoning", "large"},
            rpm=20, rpd=33, quality=4, speed=3,
        ),
        ModelSpec(
            id="command-a-03-2025", context=256_000,
            tags={"long-context", "reasoning", "creative", "large"},
            rpm=20, rpd=33, quality=5, speed=3,
            notes="newest Cohere flagship · 256K ctx",
        ),
        ModelSpec(
            id="command-a-reasoning-08-2025", context=256_000,
            tags={"long-context", "reasoning", "large"},
            rpm=20, rpd=33, quality=5, speed=2,
            notes="Cohere reasoning model · prod-key required",
        ),
    ],

    # ── Cloudflare Workers AI — free tier (300 RPM aggregate) ───────────
    # https://developers.cloudflare.com/workers-ai/models/
    "cloudflare": [
        ModelSpec(
            id="@cf/meta/llama-3.3-70b-instruct-fp8-fast", context=131_072,
            tags={"reasoning", "creative", "large", "balanced", "fast"},
            rpm=300, quality=4, speed=5,
            notes="default · 70B fp8 · 300 RPM aggregate",
        ),
        ModelSpec(
            id="@cf/openai/gpt-oss-120b", context=131_072,
            tags={"reasoning", "code", "large", "creative"},
            rpm=300, quality=5, speed=3,
        ),
        ModelSpec(
            id="@cf/openai/gpt-oss-20b", context=131_072,
            tags={"balanced", "code", "reasoning", "fast"},
            rpm=300, quality=3, speed=5,
        ),
        ModelSpec(
            id="@cf/meta/llama-4-scout-17b-16e-instruct", context=131_072,
            tags={"balanced", "creative", "reasoning", "vision"},
            rpm=300, quality=4, speed=4,
            modality="multimodal",
            notes="Llama 4 Scout · multimodal",
        ),
        ModelSpec(
            id="@cf/moonshot/kimi-k2.6", context=262_144,
            tags={"long-context", "reasoning", "creative", "vision", "large"},
            rpm=300, quality=5, speed=3,
            modality="multimodal",
            notes="Kimi K2.6 · 262K ctx · 1T params · multimodal",
        ),
        ModelSpec(
            id="@cf/moonshot/kimi-k2.5", context=262_144,
            tags={"long-context", "reasoning", "creative", "large"},
            rpm=300, quality=4, speed=3,
        ),
        ModelSpec(
            id="@cf/zhipu/glm-4.7-flash", context=131_072,
            tags={"balanced", "reasoning", "fast"},
            rpm=300, quality=4, speed=4,
            notes="GLM-4.7 Flash on Cloudflare",
        ),
        ModelSpec(
            id="@cf/nvidia/nemotron-3-120b-a12b", context=131_072,
            tags={"reasoning", "large", "balanced"},
            rpm=300, quality=4, speed=3,
        ),
        ModelSpec(
            id="@cf/qwen/qwen3-30b-a3b-fp8", context=131_072,
            tags={"reasoning", "code", "balanced"},
            rpm=300, quality=4, speed=4,
        ),
        ModelSpec(
            id="@cf/qwen/qwq-32b", context=131_072,
            tags={"reasoning", "code"},
            rpm=300, quality=4, speed=3,
            notes="QwQ — strong free reasoning",
        ),
        ModelSpec(
            id="@cf/qwen/qwen2.5-coder-32b-instruct", context=131_072,
            tags={"code", "reasoning"},
            rpm=300, quality=4, speed=4,
            notes="dedicated coding model",
        ),
        ModelSpec(
            id="@cf/mistralai/mistral-small-3.1-24b-instruct", context=131_072,
            tags={"balanced", "creative", "vision"},
            rpm=300, quality=3, speed=4,
            modality="multimodal",
        ),
        ModelSpec(
            id="@cf/google/gemma-4-26b-a4b-it", context=131_072,
            tags={"balanced", "reasoning", "vision"},
            rpm=300, quality=4, speed=4,
            modality="multimodal",
        ),
        ModelSpec(
            id="@cf/google/gemma-3-12b-it", context=131_072,
            tags={"balanced", "fast", "vision"},
            rpm=300, quality=3, speed=4,
            modality="multimodal",
        ),
        ModelSpec(
            id="@cf/deepseek/deepseek-r1-distill-qwen-32b", context=131_072,
            tags={"reasoning", "code"},
            rpm=300, quality=4, speed=3,
        ),
        ModelSpec(
            id="@cf/ibm/granite-4.0-h-micro", context=131_072,
            tags={"fast", "balanced", "code"},
            rpm=300, quality=2, speed=5,
        ),
        ModelSpec(
            id="@cf/meta/llama-3.1-8b-instruct-fast", context=131_072,
            tags={"fast", "balanced"},
            rpm=300, quality=2, speed=5,
        ),
    ],

    # ── Cerebras — extreme-speed wafer-scale inference ──────────────────
    # https://inference-docs.cerebras.ai/models
    "cerebras": [
        ModelSpec(
            id="llama3.1-8b", context=8_192,
            tags={"fast", "balanced"},
            rpm=30, rpd=14_400, quality=2, speed=5,
            notes="Cerebras Llama 8B at >2000 tok/s",
        ),
        ModelSpec(
            id="llama-3.3-70b", context=8_192,
            tags={"balanced", "reasoning", "creative", "large"},
            rpm=30, rpd=1_000, quality=4, speed=5,
            notes="70B at extreme speed",
        ),
        ModelSpec(
            id="gpt-oss-120b", context=8_192,
            tags={"reasoning", "code", "large"},
            rpm=30, rpd=1_000, quality=5, speed=5,
        ),
        ModelSpec(
            id="qwen-3-32b", context=8_192,
            tags={"reasoning", "code", "balanced"},
            rpm=30, rpd=1_000, quality=4, speed=5,
        ),
        ModelSpec(
            id="qwen-3-235b-a22b-instruct-2507", context=8_192,
            tags={"reasoning", "code", "creative", "large"},
            rpm=30, rpd=1_000, quality=5, speed=5,
            notes="Qwen 3 235B MoE — best free reasoning at 8K ctx",
        ),
    ],

    # ── Z.ai / Zhipu — GLM-Flash family ($0) ───────────────────────────
    # https://docs.z.ai/api-reference
    "zai": [
        ModelSpec(
            id="glm-4.7-flash", context=128_000,
            tags={"balanced", "fast"},
            rpm=10, quality=3, speed=4,
        ),
        ModelSpec(
            id="glm-4.5-flash", context=128_000,
            tags={"balanced", "fast"},
            rpm=10, quality=3, speed=4,
        ),
        ModelSpec(
            id="glm-z1-flash", context=32_000,
            tags={"reasoning", "fast"},
            rpm=10, quality=3, speed=4,
            notes="GLM-Z1 reasoning model",
        ),
    ],

    # ── HuggingFace Inference Providers (auto-routing across partners) ──
    # https://huggingface.co/docs/inference-providers/index — :fastest selects
    # the lowest-latency backend (Cerebras/Together/Sambanova/Groq/Novita/etc.)
    "huggingface": [
        ModelSpec(
            id="openai/gpt-oss-20b:fastest", context=131_072,
            tags={"balanced", "code", "reasoning", "fast"},
            quality=3, speed=5,
            notes="routed to fastest backend",
        ),
        ModelSpec(
            id="openai/gpt-oss-120b:fastest", context=131_072,
            tags={"reasoning", "code", "large", "creative"},
            quality=5, speed=4,
        ),
        ModelSpec(
            id="deepseek-ai/DeepSeek-V3.1:fastest", context=131_072,
            tags={"reasoning", "code", "balanced", "large"},
            quality=5, speed=3,
        ),
        ModelSpec(
            id="deepseek-ai/DeepSeek-R1:fastest", context=131_072,
            tags={"reasoning", "large"},
            quality=5, speed=2,
            notes="DeepSeek R1 reasoning",
        ),
        ModelSpec(
            id="meta-llama/Llama-3.3-70B-Instruct:fastest", context=131_072,
            tags={"balanced", "creative", "reasoning", "large"},
            quality=4, speed=4,
        ),
        ModelSpec(
            id="Qwen/Qwen3-32B:fastest", context=131_072,
            tags={"reasoning", "code", "balanced"},
            quality=4, speed=4,
        ),
        ModelSpec(
            id="Qwen/Qwen2.5-7B-Instruct", context=32_768,
            tags={"balanced", "fast"},
            quality=2, speed=4,
        ),
        ModelSpec(
            id="meta-llama/Llama-3.1-8B-Instruct", context=131_072,
            tags={"balanced", "fast"},
            quality=2, speed=4,
        ),
        ModelSpec(
            id="mistralai/Mistral-7B-Instruct-v0.3", context=32_768,
            tags={"balanced", "fast", "creative"},
            quality=2, speed=4,
        ),
    ],

    # ── Pollinations (requires API key from enter.pollinations.ai) ──────
    # Each alias routes to a different upstream backend (OpenAI/Claude/Gemini/
    # Kimi/etc.).  Effectively gives free access to many premium models.
    # https://gen.pollinations.ai/docs
    "pollinations": [
        ModelSpec(
            id="openai-fast", context=32_768,
            tags={"balanced", "code", "reasoning", "fast"},
            quality=3, speed=5,
            notes="GPT-OSS 20B routed via OVH · default",
        ),
        ModelSpec(
            id="openai", context=128_000,
            tags={"balanced", "creative", "code"},
            quality=4, speed=4,
        ),
        ModelSpec(
            id="openai-large", context=128_000,
            tags={"reasoning", "creative", "code", "large"},
            quality=5, speed=3,
            notes="premium GPT routed via Pollinations",
        ),
        ModelSpec(
            id="claude-fast", context=200_000,
            tags={"balanced", "creative", "long-context", "fast"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="claude", context=200_000,
            tags={"creative", "reasoning", "long-context"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="claude-large", context=200_000,
            tags={"creative", "reasoning", "long-context", "large"},
            quality=5, speed=2,
        ),
        ModelSpec(
            id="claude-opus-4.7", context=200_000,
            tags={"reasoning", "creative", "long-context", "large"},
            quality=5, speed=2,
            notes="Claude Opus 4.7 — premium reasoning",
        ),
        ModelSpec(
            id="gemini-flash-lite-3.1", context=1_048_576,
            tags={"long-context", "balanced", "fast"},
            quality=3, speed=5,
        ),
        ModelSpec(
            id="gemini-fast", context=1_048_576,
            tags={"long-context", "balanced", "fast"},
            quality=3, speed=5,
        ),
        ModelSpec(
            id="gemini-large", context=1_048_576,
            tags={"long-context", "reasoning", "creative", "large"},
            quality=5, speed=3,
        ),
        ModelSpec(
            id="deepseek", context=128_000,
            tags={"reasoning", "code", "balanced"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="deepseek-pro", context=128_000,
            tags={"reasoning", "code", "large"},
            quality=5, speed=2,
        ),
        ModelSpec(
            id="qwen-coder", context=131_072,
            tags={"code", "reasoning"},
            quality=4, speed=4,
        ),
        ModelSpec(
            id="qwen-coder-large", context=131_072,
            tags={"code", "reasoning", "large"},
            quality=5, speed=3,
        ),
        ModelSpec(
            id="qwen-large", context=131_072,
            tags={"reasoning", "creative", "large"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="mistral", context=128_000,
            tags={"balanced", "creative", "fast"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="mistral-large", context=128_000,
            tags={"creative", "reasoning", "large"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="kimi", context=131_072,
            tags={"long-context", "balanced", "reasoning"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="kimi-k2.6", context=262_144,
            tags={"long-context", "reasoning", "large", "creative"},
            quality=5, speed=2,
            notes="Kimi K2.6 1T params · 262K ctx",
        ),
        ModelSpec(
            id="glm", context=131_072,
            tags={"balanced", "reasoning"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="perplexity-reasoning", context=128_000,
            tags={"reasoning", "long-context"},
            quality=5, speed=2,
            notes="Perplexity reasoning model",
        ),
        ModelSpec(
            id="perplexity-fast", context=128_000,
            tags={"balanced", "fast"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="grok", context=131_072,
            tags={"balanced", "creative"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="grok-large", context=131_072,
            tags={"creative", "reasoning", "large"},
            quality=5, speed=2,
        ),
        ModelSpec(
            id="nova-fast", context=128_000,
            tags={"balanced", "fast"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="nova", context=300_000,
            tags={"long-context", "creative", "large"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="minimax", context=200_000,
            tags={"long-context", "creative", "balanced"},
            quality=4, speed=3,
        ),
    ],

    # ── Ollama Cloud — free :cloud-tagged MoE models ────────────────────
    # https://ollama.com/library  (filter by :cloud)
    "ollama": [
        ModelSpec(
            id="gpt-oss:20b-cloud", context=131_072,
            tags={"balanced", "code", "reasoning", "fast"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="glm-4.6:cloud", context=128_000,
            tags={"balanced", "creative"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="minimax-m2:cloud", context=196_608,
            tags={"long-context", "reasoning", "creative"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="qwen3-coder:480b-cloud", context=262_144,
            tags={"code", "long-context", "large", "reasoning"},
            quality=5, speed=3,
            notes="best free coding model · 480B MoE · 256K ctx",
        ),
        ModelSpec(
            id="gpt-oss:120b-cloud", context=131_072,
            tags={"reasoning", "code", "creative", "large"},
            quality=5, speed=3,
        ),
        ModelSpec(
            id="deepseek-v3.1:671b-cloud", context=163_840,
            tags={"reasoning", "code", "creative", "large", "long-context"},
            quality=5, speed=2,
            notes="671B parameters · best free reasoning model anywhere",
        ),
    ],

    # ── Lightning.ai LitAI (welcome credits, then paid) ─────────────────
    # NOTE: includes credit-only entries — kept for last-resort fallback.
    "lightning": [
        ModelSpec(
            id="nvidia/nemotron-3-super", context=256_000,
            tags={"long-context", "fast", "balanced"},
            quality=3, speed=5,
        ),
        ModelSpec(
            id="lightning-ai/gpt-oss-120b", context=131_072,
            tags={"reasoning", "code", "creative", "large"},
            quality=5, speed=3,
        ),
        ModelSpec(
            id="deepseek/deepseek-v3.1", context=164_000,
            tags={"reasoning", "code", "creative", "long-context", "large"},
            quality=5, speed=3,
        ),
        ModelSpec(
            id="lightning-ai/gpt-oss-20b", context=131_072,
            tags={"balanced", "code", "reasoning"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="meta/llama-3.3-70b", context=128_000,
            tags={"balanced", "reasoning", "creative", "large"},
            quality=4, speed=3,
        ),
    ],

    # ── Routeway — :free-tagged subset (price_per_million_t == 0) ──────
    "routeway": [
        ModelSpec(
            id="llama-3.3-70b-instruct:free", context=131_072,
            tags={"balanced", "reasoning", "creative", "large"},
            quality=4, speed=3,
        ),
        ModelSpec(
            id="devstral-2512:free", context=262_144,
            tags={"code", "long-context", "balanced"},
            quality=4, speed=3,
            notes="Mistral Devstral · 256K ctx · code specialist",
        ),
        ModelSpec(
            id="ling-2.6-flash:free", context=262_144,
            tags={"long-context", "fast", "balanced"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="step-3.5-flash:free", context=256_000,
            tags={"long-context", "fast", "balanced"},
            quality=3, speed=4,
        ),
        ModelSpec(
            id="nemotron-nano-9b-v2:free", context=128_000,
            tags={"balanced", "fast"},
            quality=2, speed=4,
        ),
        ModelSpec(
            id="llama-3.1-8b-instruct:free", context=16_384,
            tags={"fast", "balanced"},
            quality=2, speed=4,
        ),
        ModelSpec(
            id="llama-3.2-3b-instruct:free", context=16_384,
            tags={"fast"},
            quality=2, speed=5,
        ),
        ModelSpec(
            id="llama-3.2-1b-instruct:free", context=16_384,
            tags={"fast"},
            quality=1, speed=5,
        ),
        ModelSpec(
            id="mistral-nemo-instruct:free", context=16_384,
            tags={"balanced", "creative"},
            quality=2, speed=4,
        ),
    ],

    # ── NVIDIA NIM — build.nvidia.com free tier (1000 RPD) ────────────
    "nvidia": [
        ModelSpec(
            id="nvidia/nemotron-3-super-120b-a12b", context=131_072,
            tags={"reasoning", "creative", "large", "balanced"},
            rpm=10, rpd=1_000, quality=5, speed=3,
            notes="NVIDIA flagship MoE · 120B active params · 131K ctx",
        ),
        ModelSpec(
            id="meta/llama-3.3-70b-instruct", context=131_072,
            tags={"balanced", "creative", "code", "large"},
            rpm=10, rpd=1_000, quality=4, speed=3,
            notes="Meta Llama 3.3 70B via NVIDIA NIM",
        ),
        ModelSpec(
            id="mistralai/mistral-medium-3.5-128b", context=131_072,
            tags={"reasoning", "creative", "large", "code"},
            rpm=10, rpd=1_000, quality=5, speed=3,
            notes="Mistral Medium 3.5 · 128B params · 131K ctx",
        ),
        ModelSpec(
            id="mistralai/mistral-small-4-119b-2603", context=131_072,
            tags={"balanced", "code", "large"},
            rpm=10, rpd=1_000, quality=4, speed=3,
            notes="Mistral Small 4 hybrid MoE · 119B params",
        ),
        ModelSpec(
            id="google/gemma-3-27b-it", context=131_072,
            tags={"balanced", "creative"},
            rpm=10, rpd=1_000, quality=4, speed=4,
            notes="Google Gemma 3 27B instruction-tuned",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Paid fallback catalog (kept separate so auto-router never picks them
# unless caller explicitly opts in via force_model / metadata.opt_in_paid).
# ---------------------------------------------------------------------------
PAID_FALLBACK_CATALOG: Dict[str, List[ModelSpec]] = {
    "routeway": [
        ModelSpec(id="gpt-4o-mini",       context=131_072, tags={"balanced", "fast"},        quality=4, speed=4),
        ModelSpec(id="gpt-4o",            context=131_072, tags={"balanced", "creative"},    quality=5, speed=3),
        ModelSpec(id="claude-3-5-sonnet", context=200_000, tags={"reasoning", "creative", "large"}, quality=5, speed=3),
        ModelSpec(id="claude-3-haiku",    context=200_000, tags={"fast", "balanced"},        quality=3, speed=5),
        ModelSpec(id="deepseek-chat",     context=128_000, tags={"reasoning", "balanced"},   quality=4, speed=3),
        ModelSpec(id="deepseek-coder",    context=128_000, tags={"code"},                    quality=4, speed=3),
        ModelSpec(id="llama-3.3-70b",     context=131_072, tags={"balanced", "creative"},    quality=4, speed=3),
    ],
}


# ---------------------------------------------------------------------------
# Compatibility helpers — the rest of the codebase consumes these.
# ---------------------------------------------------------------------------

def vendor_model_hierarchy(include_paid: bool = True) -> Dict[str, List[tuple]]:
    """
    Compose the legacy ``VENDOR_MODEL_HIERARCHY`` dict from the catalog.
    Free models first; paid fallback (if any) appended last per provider.
    """
    out: Dict[str, List[tuple]] = {}
    for provider, specs in FREE_TIER_CATALOG.items():
        rows = [(s.id, s.context) for s in specs]
        if include_paid and provider in PAID_FALLBACK_CATALOG:
            rows.extend((s.id, s.context) for s in PAID_FALLBACK_CATALOG[provider])
        out[provider] = rows
    return out


def all_specs(provider: str) -> List[ModelSpec]:
    """Free + paid for a given provider, free-first."""
    return list(FREE_TIER_CATALOG.get(provider, [])) + list(PAID_FALLBACK_CATALOG.get(provider, []))


def find_spec(model_id: str) -> Optional[ModelSpec]:
    """Look up a model spec across all providers (case-insensitive)."""
    needle = model_id.lower()
    for provider in FREE_TIER_CATALOG:
        for s in all_specs(provider):
            if s.id.lower() == needle:
                return s
    return None


def provider_of(model_id: str) -> Optional[str]:
    """Return the provider name that owns *model_id*, or None."""
    needle = model_id.lower()
    for provider, specs in FREE_TIER_CATALOG.items():
        for s in all_specs(provider):
            if s.id.lower() == needle:
                return provider
    return None
