"""
Runtime provider key management — add/remove/test keys without restart.

Keys added here are stored in Redis (arbiter:runtime:keys:{provider}) and
merged with env-var keys.  Enable/disable flags are also stored in Redis.

Routes
------
GET    /api/providers                    List all providers + key info
POST   /api/providers/{name}/keys        Add a key
DELETE /api/providers/{name}/keys/{hash} Remove a runtime key by hash
POST   /api/providers/{name}/enable      Enable a disabled provider
POST   /api/providers/{name}/disable     Disable a provider
POST   /api/providers/{name}/test        Test provider connectivity
POST   /api/providers/reload             Reload all key pools from env + Redis
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.key_management.key_pool import KeyPool, PROVIDER_LIMITS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/providers", tags=["Provider Management"])

_REDIS_KEYS_PFX     = "arbiter:runtime:keys:"
_REDIS_DISABLED_PFX = "arbiter:runtime:disabled:"

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
        "key_format": "No key required",
        "key_hint":   "",
        "signup_url": "https://pollinations.ai/",
        "free":       True,
        "models": ["mistral", "mistral-large", "openai", "claude"],
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
            "Deploy an LLM app: modal deploy my_llm.py",
            "Register the URL in Settings → Modal Endpoints",
        ],
        "models": [
            "meta-llama/Llama-3.1-8B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
        ],
    },
}


def _mask(key: str) -> str:
    if not key or key == "free":
        return "(free)"
    if len(key) <= 10:
        return key[:2] + "****"
    return key[:6] + "..." + key[-4:]


def _hash_key(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:10]


async def _redis_keys(redis, provider: str) -> List[str]:
    raw = await redis.get(f"{_REDIS_KEYS_PFX}{provider}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


async def _save_redis_keys(redis, provider: str, keys: List[str]) -> None:
    await redis.set(f"{_REDIS_KEYS_PFX}{provider}", json.dumps(keys))


async def _merged_keys(redis, provider: str) -> List[str]:
    if provider == "pollinations":
        return ["free"]
    env_keys   = settings.get_keys(provider)
    extra_keys = await _redis_keys(redis, provider)
    seen: set  = set()
    merged: List[str] = []
    for k in env_keys + extra_keys:
        if k and k not in seen:
            seen.add(k)
            merged.append(k)
    return merged


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reload_provider(name: str, request: Request) -> None:
    """Hot-reload a provider + its key pool from env + Redis state."""
    from app.providers.gemini       import GeminiProvider
    from app.providers.groq_provider import GroqProvider
    from app.providers.openrouter   import OpenRouterProvider
    from app.providers.cohere_provider import CohereProvider
    from app.providers.cloudflare   import CloudflareProvider
    from app.providers.cerebras     import CerebrasProvider
    from app.providers.huggingface  import HuggingFaceProvider
    from app.providers.pollinations import PollinationsProvider
    from app.providers.modal_provider import ModalProvider

    _classes = {
        "gemini": GeminiProvider, "groq": GroqProvider,
        "openrouter": OpenRouterProvider, "cohere": CohereProvider,
        "cloudflare": CloudflareProvider, "cerebras": CerebrasProvider,
        "huggingface": HuggingFaceProvider, "pollinations": PollinationsProvider,
        "modal": ModalProvider,
    }

    redis     = request.app.state.redis
    providers = request.app.state.providers
    key_pools = request.app.state.key_pools

    disabled = await redis.get(f"{_REDIS_DISABLED_PFX}{name}")
    if disabled:
        providers.pop(name, None)
        key_pools.pop(name, None)
        return

    all_keys = await _merged_keys(redis, name)
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
        env_keys   = settings.get_keys(name) if name != "pollinations" else []
        extra_keys = await _redis_keys(redis, name)
        disabled   = bool(await redis.get(f"{_REDIS_DISABLED_PFX}{name}"))

        pool       = key_pools.get(name)
        pool_stats = None
        if pool:
            try:
                pool_stats = await pool.get_stats()
            except Exception:
                pass

        key_list = []
        all_keys = env_keys + extra_keys
        for k in all_keys:
            key_list.append({
                "hash":   _hash_key(k),
                "masked": _mask(k),
                "source": "env" if k in env_keys else "runtime",
            })

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


@router.post("/{name}/keys", summary="Add a runtime key", status_code=201)
async def add_key(name: str, body: AddKeyBody, request: Request) -> JSONResponse:
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")
    if name == "pollinations":
        raise HTTPException(400, "Pollinations requires no key")
    k = body.key.strip()
    if not k:
        raise HTTPException(422, "Key cannot be empty")

    redis = request.app.state.redis

    # Dedup check
    existing = await _redis_keys(redis, name)
    env_k    = settings.get_keys(name)
    if k in existing or k in env_k:
        raise HTTPException(409, "Key already exists")

    existing.append(k)
    await _save_redis_keys(redis, name, existing)
    await _reload_provider(name, request)

    return JSONResponse(
        status_code=201,
        content={"success": True, "hash": _hash_key(k), "masked": _mask(k)},
    )


@router.delete("/{name}/keys/{key_hash}", summary="Remove a runtime key by hash")
async def remove_key(name: str, key_hash: str, request: Request) -> JSONResponse:
    if name not in _PROVIDER_META:
        raise HTTPException(404, f"Unknown provider: {name}")

    redis    = request.app.state.redis
    existing = await _redis_keys(redis, name)
    new_list = [k for k in existing if _hash_key(k) != key_hash]

    if len(new_list) == len(existing):
        # Check if it's an env key (cannot delete from here)
        env_k = settings.get_keys(name)
        for k in env_k:
            if _hash_key(k) == key_hash:
                raise HTTPException(
                    400,
                    "This key is set via environment variable. "
                    "Remove it from CLOUDFLARE_API_KEYS / GEMINI_API_KEYS etc. "
                    "in your .env file and restart.",
                )
        raise HTTPException(404, "Key not found")

    await _save_redis_keys(redis, name, new_list)
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
    """
    Send a minimal probe request to the provider and report latency / errors.
    Uses the first available key.
    """
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


@router.post("/reload", summary="Reload all providers from env + Redis")
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
