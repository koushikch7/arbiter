"""
Single Source of Truth — Provider & Model Registry.

Every other module imports provider metadata from HERE instead of maintaining
its own copy.  This eliminates configuration drift between the catalog,
key_pool PROVIDER_LIMITS, keys_api _PROVIDER_META, models_api labels, and
the Settings UI.

Architecture
────────────
  ProviderSpec        High-level provider metadata (label, color, limits, etc.)
  ModelSpec           Per-model metadata (context, rpm, rpd, tags, quality, speed)
  PROVIDERS           Dict[str, ProviderSpec] — canonical registry
  get_provider()      Accessor with KeyError on missing
  get_models()        Returns model list for a provider
  get_limits()        Returns ProviderLimits for key_pool consumption

All values here are the AUTHORITATIVE source.  If a value disagrees with
what another module had before, this module wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class ModelSpec:
    """Per-model metadata — one entry per model in a provider's catalog."""
    id: str
    context: int                          # context window (tokens)
    tags: Set[str] = field(default_factory=set)
    rpm: Optional[int] = None             # free-tier RPM for this model
    rpd: Optional[int] = None             # free-tier RPD for this model
    quality: int = 3                      # 1 (small/fast) → 5 (flagship)
    speed: int = 3                        # 1 (slow) → 5 (very fast)
    modality: str = "text"                # "text" | "vision" | "multimodal"
    notes: str = ""
    paid_only: bool = False               # True = requires #paid key


@dataclass(frozen=True)
class ProviderLimits:
    """Rate limits enforced by the key pool for this provider."""
    rpm: int        # requests per minute (per key)
    tpm: int        # tokens per minute (per key)
    daily: int      # daily request budget (per key)


@dataclass
class ProviderSpec:
    """Complete provider metadata — single authoritative definition."""
    name: str                             # internal ID ("gemini", "nvidia", etc.)
    label: str                            # human-readable display name
    color: str                            # hex color for UI
    description: str                      # short description for settings page
    is_free: bool = True                  # whether free tier exists
    signup_url: str = ""                  # where to get API keys
    key_hint: str = ""                    # placeholder text for key input
    key_env_var: str = ""                 # env variable name for keys
    limits: ProviderLimits = field(default_factory=lambda: ProviderLimits(10, 100_000, 1000))
    models: List[ModelSpec] = field(default_factory=list)
    setup_steps: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# CANONICAL PROVIDER REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

PROVIDERS: Dict[str, ProviderSpec] = {

    # ── Gemini ────────────────────────────────────────────────────────────────
    "gemini": ProviderSpec(
        name="gemini",
        label="Google Gemini",
        color="#4285f4",
        description="Google AI Studio — 1M context, 15 RPM free tier",
        is_free=True,
        signup_url="https://aistudio.google.com/app/apikey",
        key_hint="AIza...",
        key_env_var="GEMINI_API_KEYS",
        limits=ProviderLimits(rpm=5, tpm=250_000, daily=100),
        setup_steps=[
            "Go to https://aistudio.google.com/app/apikey",
            "Create an API key (free tier: 15 RPM, 1M tokens/min)",
            "Tag with #paid for billing-enabled keys",
        ],
        models=[
            ModelSpec(id="gemini-2.5-flash", context=1_048_576, tags={"balanced", "fast", "long-context"}, rpm=10, rpd=500, quality=4, speed=5, notes="2.5 Flash — best speed/quality ratio"),
            ModelSpec(id="gemini-2.5-flash-lite-preview-06-17", context=1_048_576, tags={"fast", "balanced"}, rpm=30, rpd=10_000, quality=3, speed=5, notes="Ultra-light Flash variant"),
            ModelSpec(id="gemini-2.5-pro", context=1_048_576, tags={"reasoning", "large", "long-context"}, rpm=5, rpd=100, quality=5, speed=2, notes="2.5 Pro — best reasoning", paid_only=True),
            ModelSpec(id="gemini-2.0-flash", context=1_048_576, tags={"balanced", "fast"}, rpm=15, rpd=1500, quality=3, speed=5, notes="2.0 Flash — high throughput"),
        ],
    ),

    # ── Groq ──────────────────────────────────────────────────────────────────
    "groq": ProviderSpec(
        name="groq",
        label="Groq",
        color="#f55036",
        description="Groq LPU — fastest inference, 30 RPM free tier",
        is_free=True,
        signup_url="https://console.groq.com/keys",
        key_hint="gsk_...",
        key_env_var="GROQ_API_KEYS",
        limits=ProviderLimits(rpm=30, tpm=6_000, daily=1_000),
        setup_steps=[
            "Go to https://console.groq.com/keys",
            "Create a free API key",
            "Free tier: 30 RPM, 6K TPM, 14.4K requests/day",
        ],
        models=[
            ModelSpec(id="llama-3.3-70b-versatile", context=128_000, tags={"balanced", "creative", "code"}, rpm=30, rpd=14_400, quality=4, speed=5, notes="Llama 3.3 70B on Groq"),
            ModelSpec(id="llama-3.1-8b-instant", context=128_000, tags={"fast", "code"}, rpm=30, rpd=14_400, quality=3, speed=5, notes="8B instant — fastest"),
            ModelSpec(id="gemma2-9b-it", context=8_192, tags={"balanced"}, rpm=30, rpd=14_400, quality=3, speed=5, notes="Gemma2 9B"),
            ModelSpec(id="qwen-qwq-32b", context=128_000, tags={"reasoning", "code"}, rpm=30, rpd=1_000, quality=4, speed=4, notes="QwQ 32B — reasoning"),
        ],
    ),

    # ── Cerebras ──────────────────────────────────────────────────────────────
    "cerebras": ProviderSpec(
        name="cerebras",
        label="Cerebras",
        color="#00d4aa",
        description="Cerebras Inference — fast, 30 RPM free tier",
        is_free=True,
        signup_url="https://cloud.cerebras.ai/platform",
        key_hint="csk-...",
        key_env_var="CEREBRAS_API_KEYS",
        limits=ProviderLimits(rpm=30, tpm=60_000, daily=1_000),
        setup_steps=[
            "Go to https://cloud.cerebras.ai/platform → API Keys",
            "Create a free API key",
            "Free tier: 30 RPM, 60K TPM, 1M tokens/day",
        ],
        models=[
            ModelSpec(id="llama-3.3-70b", context=128_000, tags={"balanced", "code", "creative"}, rpm=30, rpd=1_000, quality=4, speed=5, notes="Llama 3.3 70B on Cerebras"),
            ModelSpec(id="llama3.1-8b", context=128_000, tags={"fast", "code"}, rpm=30, rpd=14_400, quality=3, speed=5, notes="8B ultra-fast"),
        ],
    ),

    # ── NVIDIA NIM ────────────────────────────────────────────────────────────
    "nvidia": ProviderSpec(
        name="nvidia",
        label="NVIDIA NIM",
        color="#76b900",
        description="NVIDIA NIM — 1000 RPD free tier, 120B+ models",
        is_free=True,
        signup_url="https://build.nvidia.com",
        key_hint="nvapi-...",
        key_env_var="NVIDIA_API_KEYS",
        limits=ProviderLimits(rpm=40, tpm=500_000, daily=1_000),
        setup_steps=[
            "Go to https://build.nvidia.com",
            "Create a free account and generate an API key",
            "Free tier: 40 RPM, 1000 requests/day for most models",
        ],
        models=[
            ModelSpec(id="nvidia/nemotron-3-super-120b-a12b", context=131_072, tags={"reasoning", "creative", "large", "balanced"}, rpm=40, rpd=1_000, quality=5, speed=3, notes="NVIDIA flagship MoE · 120B active params"),
            ModelSpec(id="meta/llama-3.3-70b-instruct", context=131_072, tags={"balanced", "creative", "code", "large"}, rpm=40, rpd=1_000, quality=4, speed=3, notes="Meta Llama 3.3 70B via NIM"),
            ModelSpec(id="mistralai/mistral-medium-3.5-128b", context=131_072, tags={"reasoning", "creative", "large", "code"}, rpm=40, rpd=1_000, quality=5, speed=3, notes="Mistral Medium 3.5 · 128B"),
            ModelSpec(id="mistralai/mistral-small-4-119b-2603", context=131_072, tags={"balanced", "code", "large"}, rpm=40, rpd=1_000, quality=4, speed=3, notes="Mistral Small 4 hybrid MoE"),
            ModelSpec(id="google/gemma-3-27b-it", context=131_072, tags={"balanced", "creative"}, rpm=40, rpd=1_000, quality=4, speed=4, notes="Gemma 3 27B instruction-tuned"),
        ],
    ),

    # ── Z.ai / Zhipu ─────────────────────────────────────────────────────────
    "zai": ProviderSpec(
        name="zai",
        label="Z.ai / Zhipu AI",
        color="#6366f1",
        description="Zhipu AI — GLM-4 Flash models, free tier",
        is_free=True,
        signup_url="https://bigmodel.cn/usercenter/apikeys",
        key_hint="(Zhipu API key)",
        key_env_var="ZAI_API_KEYS",
        limits=ProviderLimits(rpm=10, tpm=200_000, daily=1_000),
        setup_steps=[
            "Go to https://bigmodel.cn/usercenter/apikeys",
            "Create an API key",
            "GLM-4.7-Flash and GLM-4.5-Flash are free",
        ],
        models=[
            ModelSpec(id="glm-4.7-flash", context=128_000, tags={"balanced", "creative"}, rpm=10, rpd=1_000, quality=4, speed=4, notes="GLM-4.7 Flash — free tier flagship"),
            ModelSpec(id="glm-4.5-flash", context=32_000, tags={"fast", "balanced"}, rpm=10, rpd=1_000, quality=3, speed=5, notes="GLM-4.5 Flash — fast & free"),
            ModelSpec(id="glm-z1-flash", context=32_000, tags={"reasoning"}, rpm=10, rpd=1_000, quality=4, speed=3, notes="GLM-Z1 Flash — reasoning"),
        ],
    ),

    # ── Cloudflare ────────────────────────────────────────────────────────────
    "cloudflare": ProviderSpec(
        name="cloudflare",
        label="Cloudflare Workers AI",
        color="#f48120",
        description="Cloudflare Workers AI — 300 RPM, 10K neurons/day free",
        is_free=True,
        signup_url="https://dash.cloudflare.com/",
        key_hint="account_id|api_token",
        key_env_var="CLOUDFLARE_API_KEYS",
        limits=ProviderLimits(rpm=300, tpm=1_000_000, daily=10_000),
        setup_steps=[
            "Go to https://dash.cloudflare.com/ → Workers AI",
            "Get Account ID + create API token with Workers AI (Read) scope",
            "Format: account_id|api_token",
        ],
        models=[
            ModelSpec(id="@cf/meta/llama-3.3-70b-instruct-fp8-fast", context=8_192, tags={"balanced", "code"}, rpm=300, rpd=10_000, quality=4, speed=4, notes="Llama 3.3 70B FP8"),
            ModelSpec(id="@cf/meta/llama-4-scout-17b-16e-instruct", context=8_192, tags={"fast", "balanced"}, rpm=300, rpd=10_000, quality=3, speed=4, notes="Llama 4 Scout 17B"),
        ],
    ),

    # ── OpenRouter ────────────────────────────────────────────────────────────
    "openrouter": ProviderSpec(
        name="openrouter",
        label="OpenRouter",
        color="#9333ea",
        description="OpenRouter — unified gateway, free :free models",
        is_free=True,
        signup_url="https://openrouter.ai/keys",
        key_hint="sk-or-v1-...",
        key_env_var="OPENROUTER_API_KEYS",
        limits=ProviderLimits(rpm=20, tpm=500_000, daily=200),
        setup_steps=[
            "Go to https://openrouter.ai/keys",
            "Create an API key (free tier: 200 RPD for :free models)",
            "Add $10 credit to increase to 1000 RPD",
        ],
        models=[
            ModelSpec(id="meta-llama/llama-3.3-70b-instruct:free", context=128_000, tags={"balanced", "code"}, rpm=20, rpd=200, quality=4, speed=4, notes="Llama 3.3 70B :free"),
            ModelSpec(id="google/gemma-2-9b-it:free", context=8_192, tags={"fast", "balanced"}, rpm=20, rpd=200, quality=3, speed=4, notes="Gemma 2 9B :free"),
            ModelSpec(id="qwen/qwen3-235b-a22b:free", context=40_960, tags={"reasoning", "code", "large"}, rpm=20, rpd=200, quality=5, speed=3, notes="Qwen3 235B MoE :free"),
        ],
    ),

    # ── Cohere ────────────────────────────────────────────────────────────────
    "cohere": ProviderSpec(
        name="cohere",
        label="Cohere",
        color="#d97706",
        description="Cohere — Command R+, 20 RPM trial",
        is_free=True,
        signup_url="https://dashboard.cohere.com/api-keys",
        key_hint="(Cohere API key)",
        key_env_var="COHERE_API_KEYS",
        limits=ProviderLimits(rpm=20, tpm=100_000, daily=33),
        setup_steps=[
            "Go to https://dashboard.cohere.com/api-keys",
            "Create a trial API key",
            "Trial: 20 RPM, ~1000 requests/month",
        ],
        models=[
            ModelSpec(id="command-r-plus", context=128_000, tags={"reasoning", "creative", "large", "long-context"}, rpm=20, rpd=33, quality=5, speed=3, notes="Command R+ 128K"),
            ModelSpec(id="command-r", context=128_000, tags={"balanced", "creative"}, rpm=20, rpd=33, quality=4, speed=4, notes="Command R 128K"),
            ModelSpec(id="command-a-03-2025", context=256_000, tags={"reasoning", "long-context", "large"}, rpm=20, rpd=33, quality=5, speed=3, notes="Command A 256K"),
        ],
    ),

    # ── HuggingFace ───────────────────────────────────────────────────────────
    "huggingface": ProviderSpec(
        name="huggingface",
        label="HuggingFace",
        color="#fbbf24",
        description="HuggingFace Inference — limited free credits",
        is_free=True,
        signup_url="https://huggingface.co/settings/tokens",
        key_hint="hf_...",
        key_env_var="HUGGINGFACE_API_KEYS",
        limits=ProviderLimits(rpm=10, tpm=50_000, daily=500),
        setup_steps=[
            "Go to https://huggingface.co/settings/tokens",
            "Create a token with Read scope",
            "Limited free monthly inference credits",
        ],
        models=[
            ModelSpec(id="Qwen/Qwen2.5-72B-Instruct", context=32_000, tags={"balanced", "code", "reasoning"}, rpm=10, rpd=500, quality=4, speed=3, notes="Qwen 2.5 72B"),
            ModelSpec(id="meta-llama/Llama-3.3-70B-Instruct", context=8_192, tags={"balanced", "code"}, rpm=10, rpd=500, quality=4, speed=3, notes="Llama 3.3 70B"),
        ],
    ),

    # ── Pollinations ──────────────────────────────────────────────────────────
    "pollinations": ProviderSpec(
        name="pollinations",
        label="Pollinations.ai",
        color="#10b981",
        description="Pollinations — free tier with API key",
        is_free=True,
        signup_url="https://enter.pollinations.ai/",
        key_hint="pk_...",
        key_env_var="POLLINATIONS_API_KEYS",
        limits=ProviderLimits(rpm=5, tpm=100_000, daily=1_000),
        setup_steps=[
            "Go to https://enter.pollinations.ai/",
            "Get your API key",
            "Free tier with key; anonymous tier available but slower",
        ],
        models=[
            ModelSpec(id="openai", context=32_000, tags={"balanced"}, rpm=5, rpd=1_000, quality=3, speed=3, notes="Pollinations default"),
        ],
    ),

    # ── Ollama ────────────────────────────────────────────────────────────────
    "ollama": ProviderSpec(
        name="ollama",
        label="Ollama Cloud",
        color="#1d4ed8",
        description="Ollama Cloud — free :cloud models",
        is_free=True,
        signup_url="https://ollama.com/settings/keys",
        key_hint="(Ollama API key)",
        key_env_var="OLLAMA_API_KEYS",
        limits=ProviderLimits(rpm=10, tpm=100_000, daily=1_000),
        setup_steps=[
            "Go to https://ollama.com/settings/keys",
            "Create an API key",
            "All :cloud models are free",
        ],
        models=[
            ModelSpec(id="gpt-oss-120b:cloud", context=32_000, tags={"balanced", "large"}, rpm=10, rpd=1_000, quality=4, speed=3, notes="GPT-OSS 120B :cloud"),
        ],
    ),

    # ── Routeway ──────────────────────────────────────────────────────────────
    "routeway": ProviderSpec(
        name="routeway",
        label="Routeway",
        color="#ec4899",
        description="Routeway — 192-model unified gateway, 15 :free models",
        is_free=True,
        signup_url="https://routeway.ai",
        key_hint="sk-...",
        key_env_var="ROUTEWAY_API_KEYS",
        limits=ProviderLimits(rpm=60, tpm=500_000, daily=10_000),
        setup_steps=[
            "Go to https://routeway.ai",
            "Create a free account and API key",
            "15 :free models available (Llama, Gemma, Nemotron, etc.)",
        ],
        models=[
            ModelSpec(id="meta-llama/llama-3.3-70b-instruct:free", context=128_000, tags={"balanced", "code"}, rpm=60, rpd=10_000, quality=4, speed=4, notes="Llama 3.3 70B :free"),
            ModelSpec(id="google/gemma-3-27b-it:free", context=128_000, tags={"balanced"}, rpm=60, rpd=10_000, quality=3, speed=4, notes="Gemma 3 27B :free"),
        ],
    ),

    # ── Lightning ─────────────────────────────────────────────────────────────
    "lightning": ProviderSpec(
        name="lightning",
        label="Lightning.ai LitAI",
        color="#7c3aed",
        description="Lightning.ai — Nemotron, DeepSeek, GPT-OSS (paid with credits)",
        is_free=False,
        signup_url="https://lightning.ai",
        key_hint="(Lightning API key)",
        key_env_var="LIGHTNING_API_KEYS",
        limits=ProviderLimits(rpm=20, tpm=500_000, daily=1_000),
        setup_steps=[
            "Go to https://lightning.ai",
            "Sign up — ~37M token welcome credit",
            "$0.09–$0.52/M tokens after credits",
        ],
        models=[
            ModelSpec(id="nvidia/nemotron-3-super", context=256_000, tags={"reasoning", "large"}, rpm=20, rpd=1_000, quality=5, speed=3, notes="Nemotron 3 Super 256K"),
        ],
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def get_provider(name: str) -> ProviderSpec:
    """Get a provider by internal name. Raises KeyError if not found."""
    return PROVIDERS[name]


def get_limits(name: str) -> ProviderLimits:
    """Get rate limits for a provider (for key_pool consumption)."""
    return PROVIDERS[name].limits


def get_models(name: str) -> List[ModelSpec]:
    """Get model list for a provider."""
    return PROVIDERS[name].models


def get_all_model_ids(name: str) -> List[str]:
    """Get just the model ID strings for a provider."""
    return [m.id for m in PROVIDERS[name].models]


def get_provider_labels() -> Dict[str, str]:
    """Return {name: label} for all providers."""
    return {k: v.label for k, v in PROVIDERS.items()}


def get_free_providers() -> Set[str]:
    """Return set of provider names that have a free tier."""
    return {k for k, v in PROVIDERS.items() if v.is_free}


def get_provider_colors() -> Dict[str, str]:
    """Return {name: hex_color} for all providers."""
    return {k: v.color for k, v in PROVIDERS.items()}


def get_provider_order() -> List[str]:
    """Return the default provider priority order."""
    return [name for name in PROVIDERS.keys()]


def get_all_active_models() -> List[Dict]:
    """Return a flat list of all models across all providers with provider info."""
    result = []
    for prov_name, prov in PROVIDERS.items():
        for model in prov.models:
            result.append({
                "id": model.id,
                "provider": prov_name,
                "provider_label": prov.label,
                "context": model.context,
                "quality": model.quality,
                "speed": model.speed,
                "tags": list(model.tags),
                "paid_only": model.paid_only,
            })
    return result


def provider_meta_for_api() -> List[Dict]:
    """Return provider metadata suitable for the /api/providers/meta endpoint."""
    result = []
    for name, spec in PROVIDERS.items():
        result.append({
            "name": name,
            "label": spec.label,
            "color": spec.color,
            "description": spec.description,
            "is_free": spec.is_free,
            "signup_url": spec.signup_url,
            "key_hint": spec.key_hint,
            "key_env_var": spec.key_env_var,
            "limits": {
                "rpm": spec.limits.rpm,
                "tpm": spec.limits.tpm,
                "daily": spec.limits.daily,
            },
            "models": [
                {
                    "id": m.id,
                    "context": m.context,
                    "quality": m.quality,
                    "speed": m.speed,
                    "paid_only": m.paid_only,
                }
                for m in spec.models
            ],
            "setup_steps": spec.setup_steps,
        })
    return result
