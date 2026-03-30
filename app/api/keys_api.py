"""
Runtime provider key management — add/remove/test keys without restart.

Keys are stored directly in the .env file.  The cached settings singleton
(loaded once at startup) is bypassed for key reads — we parse .env fresh on
every operation so changes take effect immediately without a server restart.

Enable/disable flags are the only thing stored in Redis.

Routes
------
GET    /api/providers                    List all providers + key info
POST   /api/providers/{name}/keys        Add a key
DELETE /api/providers/{name}/keys/{hash} Remove a key by hash
POST   /api/providers/{name}/enable      Enable a disabled provider
POST   /api/providers/{name}/disable     Disable a provider
POST   /api/providers/{name}/test        Test provider connectivity
POST   /api/providers/reload             Reload all key pools from .env
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.key_management.key_pool import KeyPool, PROVIDER_LIMITS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["Provider Management"])

_REDIS_DISABLED_PFX = "arbiter:runtime:disabled:"

# Project root — two levels up from this file (app/api/keys_api.py)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Mapping: provider name → .env variable name
_ENV_VAR_MAP: dict = {
    "gemini":      "GEMINI_API_KEYS",
    "groq":        "GROQ_API_KEYS",
    "openrouter":  "OPENROUTER_API_KEYS",
    "cohere":      "COHERE_API_KEYS",
    "huggingface": "HUGGINGFACE_API_KEYS",
    "cloudflare":  "CLOUDFLARE_API_KEYS",
    "cerebras":    "CEREBRAS_API_KEYS",
    "zai":         "ZAI_API_KEYS",
    "lightning":   "LIGHTNING_API_KEYS",
    "modal":       "MODAL_API_KEYS",
    "pollinations": "POLLINATIONS_API_KEYS",
}

# ── Provider metadata ────────────────────────────────────────────────────────
_PROVIDER_META = {
    "gemini": {
        "label":      "Google Gemini",
        "key_format": "API key",
        "key_hint":   "AIzaSy...",
        "signup_url": "https://aistudio.google.com/app/apikey",
        "free":       True,
        "models": [
            "gemini-2.5-flash", "gemini-2.5-flash-lite",
            "gemini-2.5-pro", "gemini-2.0-flash",
        ],
    },
    "groq": {
        "label":      "Groq",
        "key_format": "API key",
        "key_hint":   "gsk_...",
        "signup_url": "https://console.groq.com/keys",
        "free":       True,
        "models": [
            "llama-3.1-8b-instant", "llama-3.3-70b-versatile",
            "llama-4-scout-17b", "qwen3-32b",
        ],
    },
    "openrouter": {
        "label":      "OpenRouter",
        "key_format": "API key",
        "key_hint":   "sk-or-v1-...",
        "signup_url": "https://openrouter.ai/keys",
        "free":       True,
        "models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "nousresearch/hermes-3-llama-3.1-405b:free",
        ],
    },
    "cohere": {
        "label":      "Cohere",
        "key_format": "API key",
        "key_hint":   "...",
        "signup_url": "https://dashboard.cohere.com/api-keys",
        "free":       True,
        "models": ["command-r7b-12-2024", "command-r-plus-08-2024"],
    },
    "cloudflare": {
        "label":      "Cloudflare Workers AI",
        "key_format": "account_id|api_token",
        "key_hint":   "abc123def|Bearer_token_here",
        "signup_url": "https://dash.cloudflare.com/profile/api-tokens",
        "free":       True,
        "setup_steps": [
            "Sign up at cloudflare.com (free account)",
            "Go to dash.cloudflare.com → Workers & Pages → Overview",
            "Note your Account ID (top-right of the page)",
            "Go to Profile → API Tokens → Create Token",
            "Use 'Cloudflare Workers AI' template or add 'AI Gateway: Read' permission",
            "Format key as: <Account_ID>|<API_Token>",
        ],
        "models": [
            "@cf/meta/llama-4-scout-17b-16e-instruct",
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "@cf/moonshot/kimi-k2.5",
        ],
    },
    "cerebras": {
        "label":      "Cerebras Inference",
        "key_format": "API key",
        "key_hint":   "csk-...",
        "signup_url": "https://cloud.cerebras.ai/",
        "free":       True,
        "models": ["llama3.1-8b", "gpt-oss-120b", "qwen-3-235b"],
    },
    "huggingface": {
        "label":      "HuggingFace Inference",
        "key_format": "Access Token",
        "key_hint":   "hf_...",
        "signup_url": "https://huggingface.co/settings/tokens",
        "free":       True,
        "models": [
            "Qwen/Qwen2.5-7B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
        ],
    },
    "pollinations": {
        "label":      "Pollinations.ai",
        "key_format": "API key",
        "key_hint":   "sk_... or pk_...",
        "signup_url": "https://enter.pollinations.ai/",
        "free":       True,
        "models": ["openai", "openai-fast", "openai-large", "claude", "claude-fast",
                   "claude-large", "gemini", "gemini-fast", "mistral", "deepseek", "qwen-coder"],
    },
    "modal": {
        "label":      "Modal.com (Serverless GPU)",
        "key_format": "endpoint_url|token",
        "key_hint":   "https://myorg--app.modal.run|ak-abc123:xyz456",
        "signup_url": "https://modal.com",
        "free":       True,
        "setup_steps": [
            "Sign up at modal.com ($30 free credits/month)",
            "pip install modal && modal setup",
            "modal token new  # creates ~/.modal/config.toml",
            "Deploy an LLM app via Settings → Modal GPU tab",
            "Endpoint is auto-registered after successful deploy",
        ],
        "models": [
            "meta-llama/Llama-3.1-8B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
        ],
    },
    "zai": {
        "label":      "Z.ai / Zhipu AI",
        "key_format": "API key",
        "key_hint":   "your-zai-api-key",
        "signup_url": "https://z.ai/manage-apikey",
        "free":       True,
        "models": [
            "glm-4.7-flash", "glm-4.5-flash", "glm-z1-flash",
        ],
    },
    "lightning": {
        "label":      "Lightning.ai (LitAI)",
        "key_format": "API key",
        "key_hint":   "your-lightning-api-key",
        "signup_url": "https://lightning.ai",
        "free":       False,
        "models": [
            "nvidia/nemotron-3-super", "lightning-ai/gpt-oss-120b",
            "deepseek/deepseek-v3.1", "lightning-ai/gpt-oss-20b",
            "meta/llama-3.3-70b",
        ],
    },
}


# ---------------------------------------------------------------------------
# .env helpers — single source of truth for all provider keys
# ---------------------------------------------------------------------------

def _is_placeholder(key: str) -> bool:
    """Return True for example/placeholder values that should never be used."""
    return any(key.startswith(p) for p in (
        "your-", "your_", "hf_your", "ak-your", "as-your",
    ))


def _ensure_env_file() -> Path:
    """Return path to .env, creating it from .env.example if it doesn't exist."""
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        example = _PROJECT_ROOT / ".env.example"
        if example.exists():
            env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("Created .env from .env.example")
    return env_file


def _read_env_keys(provider: str) -> List[str]:
    """
    Read provider keys directly from the .env file.

    Bypasses the cached settings singleton so changes written via the UI
    are visible immediately without a server restart.
    """
    env_var = _ENV_VAR_MAP.get(provider)
    if not env_var:
        return []
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return []
    content = env_file.read_text(encoding="utf-8")
    m = re.search(rf"^{env_var}=(.*)$", content, re.MULTILINE)
    if not m:
        return []
    return [
        k.strip() for k in m.group(1).split(",")
        if k.strip() and not _is_placeholder(k.strip())
    ]


def _write_env_keys(provider: str, keys: List[str]) -> None:
    """Write the key list for a provider into .env (creates file if needed)."""
    env_var = _ENV_VAR_MAP.get(provider)
    if not env_var:
        return
    env_file = _ensure_env_file()
    content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    value    = ",".join(keys)
    new_line = f"{env_var}={value}"
    if re.search(rf"^{env_var}=", content, re.MULTILINE):
        content = re.sub(rf"^{env_var}=.*$", new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"
    env_file.write_text(content, encoding="utf-8")
    logger.debug("Updated %s in .env (%d key(s))", env_var, len(keys))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _mask(key: str) -> str:
    if not key or key == "free":
        return "(free)"
    if len(key) <= 10:
        return key[:2] + "****"
    return key[:6] + "..." + key[-4:]


def _hash_key(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Provider hot-reload
# ---------------------------------------------------------------------------

async def _reload_provider(name: str, request: Request) -> None:
    """Hot-reload a provider + its key pool from the current .env file."""
    from app.providers.gemini           import GeminiProvider
    from app.providers.groq_provider    import GroqProvider
    from app.providers.openrouter       import OpenRouterProvider
    from app.providers.cohere_provider  import CohereProvider
    from app.providers.cloudflare       import CloudflareProvider
    from app.providers.cerebras         import CerebrasProvider
    from app.providers.huggingface      import HuggingFaceProvider
    from app.providers.pollinations     import PollinationsProvider
    from app.providers.modal_provider   import ModalProvider
    from app.providers.lightning_provider import LightningProvider
    from app.providers.zai_provider     import ZaiProvider

    _classes = {
        "gemini": GeminiProvider, "groq": GroqProvider,
        "openrouter": OpenRouterProvider, "cohere": CohereProvider,
        "cloudflare": CloudflareProvider, "cerebras": CerebrasProvider,
        "huggingface": HuggingFaceProvider, "pollinations": PollinationsProvider,
        "modal": ModalProvider, "lightning": LightningProvider,
        "zai": ZaiProvider,
    }

    redis     = request.app.state.redis
    providers = request.app.state.providers
    key_pools = request.app.state.key_pools

    disabled = await redis.get(f"{_REDIS_DISABLED_PFX}{name}")
    if disabled:
        providers.pop(name, None)
        key_pools.pop(name, None)
        return

    all_keys = _read_env_keys(name)
    if not all_keys:
        providers.pop(name, None)
        key_pools.pop(name, None)
        return

    if name not in providers and name in _classes:
        providers[name] = _classes[name]()

    limits = PROVIDER_LIMITS.get(name, {"rpm": 20, "tpm": 100_000, "daily": 1000})
    if name in key_pools:
        key_pools[name].keys = all_keys
    else:
        key_pools[name] = KeyPool(
            provider=name,
            keys=all_keys,
            redis_client=redis,
            rpm_limit=limits["rpm"],
            tpm_limit=limits["tpm"],
            daily_limit=limits["daily"],
        )
    logger.info("Reloaded provider %s with %d key(s)", name, len(all_keys))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", summary="List all providers with status and key info")
async def list_providers(request: Request) -> JSONResponse:
    redis     = request.app.state.redis
    key_pools = request.app.state.key_pools
    providers = request.app.state.providers

    result = []
    for name, meta in _PROVIDER_META.items():
        env_keys = _read_env_keys(name)
        disabled = bool(await redis.get(f"{_REDIS_DISABLED_PFX}{name}"))

        pool       = key_pools.get(name)
        pool_stats = None
        if pool:
            try:
                pool_stats = await pool.get_stats()
            except Exception:
                pass

        key_list = [
            {"hash": _hash_key(k), "masked": _mask(k)}
            for k in env_keys
        ]

        result.append({
            "name":        name,
            "label":       meta["label"],
            "key_format":  meta["key_format"],
            "key_hint":    meta["key_hint"],
            "signup_url":  meta.get("signup_url", ""),
            "setup_steps": meta.get("setup_steps", []),
            "free":        meta["free"],
            "configured":  name in providers,
            "disabled":    disabled,
            "key_count":   len(key_list),
            "keys":        key_list,
            "models":      meta.get("models", []),
            "pool":        pool_stats,
        })

    return JSONResponse(content=result)


class AddKeyBody(BaseModel):
    key: str


@router.post("/{name}/keys", summary="Add a key", status_code=201)
async def add_key(name: str, body: AddKeyBody, request: Request) -> JSONResponse:
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")
    k = body.key.strip()
    if not k:
        raise HTTPException(422, "Key cannot be empty")

    existing = _read_env_keys(name)
    if k in existing:
        if name == "modal":
            raise HTTPException(409,
                "This endpoint is already registered. It may have been added "
                "automatically when you deployed via the Modal GPU tab.")
        raise HTTPException(409, "Key already exists")

    existing.append(k)
    _write_env_keys(name, existing)
    await _reload_provider(name, request)

    return JSONResponse(
        status_code=201,
        content={"success": True, "hash": _hash_key(k), "masked": _mask(k)},
    )


@router.delete("/{name}/keys/{key_hash}", summary="Remove a key by hash")
async def remove_key(name: str, key_hash: str, request: Request) -> JSONResponse:
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")

    existing = _read_env_keys(name)
    new_list = [k for k in existing if _hash_key(k) != key_hash]

    if len(new_list) == len(existing):
        raise HTTPException(404, "Key not found")

    _write_env_keys(name, new_list)
    await _reload_provider(name, request)
    return JSONResponse(content={"success": True})


@router.post("/{name}/enable", summary="Enable a provider")
async def enable_provider(name: str, request: Request) -> JSONResponse:
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")
    redis = request.app.state.redis
    await redis.delete(f"{_REDIS_DISABLED_PFX}{name}")
    await _reload_provider(name, request)
    return JSONResponse(content={"success": True, "provider": name, "enabled": True})


@router.post("/{name}/disable", summary="Disable a provider")
async def disable_provider(name: str, request: Request) -> JSONResponse:
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")
    redis = request.app.state.redis
    await redis.set(f"{_REDIS_DISABLED_PFX}{name}", "1")
    request.app.state.providers.pop(name, None)
    request.app.state.key_pools.pop(name, None)
    return JSONResponse(content={"success": True, "provider": name, "enabled": False})


@router.post("/{name}/test", summary="Test provider connectivity")
async def test_provider(name: str, request: Request) -> JSONResponse:
    """Send a minimal probe request and report latency / errors."""
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")

    providers = request.app.state.providers
    key_pools = request.app.state.key_pools

    if name not in providers:
        return JSONResponse(content={"ok": False, "error": "Provider not configured (no keys)"})
    if name not in key_pools:
        return JSONResponse(content={"ok": False, "error": "No key pool for provider"})

    from app.models.schemas import ChatCompletionRequest, Message

    probe = ChatCompletionRequest(
        model="",
        messages=[Message(role="user", content="Say 'ok' in one word.")],
        max_tokens=5,
        temperature=0.0,
    )

    key = await key_pools[name].get_best_key()
    if not key:
        return JSONResponse(content={"ok": False, "error": "All keys exhausted or on cooldown"})

    t0 = time.perf_counter()
    try:
        resp = await providers[name].complete(probe, key)
        latency_ms = round((time.perf_counter() - t0) * 1000)
        return JSONResponse(content={
            "ok":         True,
            "latency_ms": latency_ms,
            "model":      resp.model,
            "reply":      resp.choices[0].message.content if resp.choices else "",
        })
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000)
        return JSONResponse(content={
            "ok":         False,
            "latency_ms": latency_ms,
            "error":      str(exc)[:300],
        })


@router.post("/reload", summary="Reload all providers from .env")
async def reload_all(request: Request) -> JSONResponse:
    reloaded, failed = [], []
    for name in _PROVIDER_META:
        try:
            await _reload_provider(name, request)
            reloaded.append(name)
        except Exception as exc:
            logger.warning("Failed to reload %s: %s", name, exc)
            failed.append(name)
    return JSONResponse(content={"success": True, "reloaded": reloaded, "failed": failed})
