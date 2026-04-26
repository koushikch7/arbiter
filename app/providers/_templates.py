"""
Preset templates for user-added custom providers.

Each template provides the wire-format defaults so users only need to paste
an API key (and optionally a custom base URL) in the UI. The ``auth_scheme``
field distinguishes OpenAI-style bearer tokens from Anthropic-style
``x-api-key`` + version headers.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Template schema
# ---------------------------------------------------------------------------
# {
#   "id":            str,  # unique template ID (lowercase slug)
#   "label":         str,  # human-readable name
#   "base_url":      str,  # default base URL (user may override)
#   "auth_scheme":   "bearer" | "anthropic" | "header",
#   "auth_header":   str,  # header name ("Authorization" / "x-api-key" / custom)
#   "auth_prefix":   str,  # prefix on the value ("Bearer " or "")
#   "extra_headers": dict[str, str],  # static headers always sent
#   "default_models": list[str],
#   "max_context":   int,
#   "signup_url":    str,
#   "description":   str,
#   "supports_discovery": bool,  # does upstream expose GET /v1/models ?
# }

CUSTOM_PROVIDER_TEMPLATES: list[dict] = [
    {
        "id":            "openai",
        "label":         "OpenAI",
        "base_url":      "https://api.openai.com/v1",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": [
            "gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo",
        ],
        "max_context":   131_072,
        "signup_url":    "https://platform.openai.com/api-keys",
        "description":   "Direct OpenAI API (paid, no free tier).",
        "supports_discovery": True,
    },
    {
        "id":            "anthropic",
        "label":         "Anthropic (Claude)",
        "base_url":      "https://api.anthropic.com/v1",
        "auth_scheme":   "anthropic",
        "auth_header":   "x-api-key",
        "auth_prefix":   "",
        "extra_headers": {"anthropic-version": "2023-06-01"},
        "default_models": [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ],
        "max_context":   200_000,
        "signup_url":    "https://console.anthropic.com/settings/keys",
        "description":   "Direct Anthropic API (paid). Note: uses Messages API, not OpenAI /v1/chat/completions — Arbiter translates automatically.",
        "supports_discovery": False,  # Anthropic has no /v1/models endpoint
    },
    {
        "id":            "deepseek",
        "label":         "DeepSeek",
        "base_url":      "https://api.deepseek.com/v1",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": ["deepseek-chat", "deepseek-reasoner"],
        "max_context":   128_000,
        "signup_url":    "https://platform.deepseek.com/api_keys",
        "description":   "DeepSeek V3 and R1 reasoning models (paid, cheap).",
        "supports_discovery": True,
    },
    {
        "id":            "together",
        "label":         "Together AI",
        "base_url":      "https://api.together.xyz/v1",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ],
        "max_context":   131_072,
        "signup_url":    "https://api.together.ai/settings/api-keys",
        "description":   "Together AI — hosted open-source models (paid, $5 free credit).",
        "supports_discovery": True,
    },
    {
        "id":            "fireworks",
        "label":         "Fireworks AI",
        "base_url":      "https://api.fireworks.ai/inference/v1",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": [
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/deepseek-v3",
            "accounts/fireworks/models/qwen2p5-72b-instruct",
        ],
        "max_context":   131_072,
        "signup_url":    "https://fireworks.ai/account/api-keys",
        "description":   "Fireworks AI — fast inference for open-source models (paid).",
        "supports_discovery": True,
    },
    {
        "id":            "mistral",
        "label":         "Mistral AI",
        "base_url":      "https://api.mistral.ai/v1",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": [
            "mistral-large-latest",
            "mistral-small-latest",
            "codestral-latest",
        ],
        "max_context":   131_072,
        "signup_url":    "https://console.mistral.ai/api-keys/",
        "description":   "Direct Mistral AI API (paid, has free tier for testing).",
        "supports_discovery": True,
    },
    {
        "id":            "perplexity",
        "label":         "Perplexity AI",
        "base_url":      "https://api.perplexity.ai",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": [
            "llama-3.1-sonar-large-128k-online",
            "llama-3.1-sonar-small-128k-online",
        ],
        "max_context":   131_072,
        "signup_url":    "https://www.perplexity.ai/settings/api",
        "description":   "Perplexity — online-search-enhanced models (paid).",
        "supports_discovery": False,
    },
    {
        "id":            "custom",
        "label":         "Custom (OpenAI-compatible)",
        "base_url":      "",
        "auth_scheme":   "bearer",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
        "extra_headers": {},
        "default_models": [],
        "max_context":   131_072,
        "signup_url":    "",
        "description":   "Any OpenAI-compatible /v1/chat/completions endpoint (Anyscale, local LLM, etc.).",
        "supports_discovery": True,
    },
]


def get_template(template_id: str) -> dict | None:
    for t in CUSTOM_PROVIDER_TEMPLATES:
        if t["id"] == template_id:
            return t
    return None
