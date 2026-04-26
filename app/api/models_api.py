import time
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.users_api import require_admin
from app.models.schemas import ModelInfo, ModelsResponse
from app.routing.router import VENDOR_MODEL_HIERARCHY
from app.key_management.key_pool import PROVIDER_LIMITS
from app.providers.base import RateLimitError, ProviderError
from app.state_store import (
    get_model_state,
    set_model_enabled,
    record_discovered_models,
    is_model_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Which models are free-tier vs paid
_FREE_TIER_PROVIDERS = {"gemini", "groq", "openrouter", "cohere", "cloudflare",
                        "cerebras", "huggingface", "pollinations", "zai",
                        "routeway"}

# Models known to be paid-only (not in the free hierarchy)
_PAID_ONLY_MODELS = {
    "gemini-2.5-pro", "gemini-3.1-pro", "command-r-plus", "gpt-4", "claude-3-opus",
}

# Static mapping of provider to owner label
PROVIDER_OWNERS = {
    "gemini": "google",
    "groq": "groq",
    "openrouter": "openrouter",
    "cohere": "cohere-ai",
}


@router.get(
    "/v1/models",
    response_model=ModelsResponse,
    summary="List available models",
)
async def list_models(request: Request) -> ModelsResponse:
    """Return all models available across all configured providers, plus active CF workers and Modal deployments."""
    import json as _json
    providers  = request.app.state.providers
    redis      = request.app.state.redis
    created_ts = int(time.time())

    model_list = []
    seen = set()

    # Standard provider models (filtered by per-model enable state)
    for provider_name, provider in providers.items():
        owned_by = PROVIDER_OWNERS.get(provider_name, provider_name)
        # Start from hardcoded provider.models, then merge in any dynamically
        # discovered model IDs from the state store so fetched-from-upstream
        # models show up without a restart.
        ids = list(provider.models)
        for mid in get_model_state(provider_name).keys():
            if mid not in ids:
                ids.append(mid)
        for model_id in ids:
            if not is_model_enabled(provider_name, model_id):
                continue
            if model_id not in seen:
                seen.add(model_id)
                model_list.append(ModelInfo(
                    id=model_id, object="model",
                    created=created_ts, owned_by=owned_by,
                ))

    # CF Workers (active only)
    try:
        raw = await redis.get("arbiter:cf:workers")
        if raw:
            registry = _json.loads(raw)
            for worker_name, meta in registry.items():
                if meta.get("status") == "active" and meta.get("url"):
                    model_id = f"cfworker/{worker_name}"
                    if model_id not in seen:
                        seen.add(model_id)
                        model_list.append(ModelInfo(
                            id=model_id, object="model",
                            created=created_ts, owned_by="cloudflare-worker",
                        ))
    except Exception as exc:
        logger.debug("Could not load CF workers for models list: %s", exc)

    # Modal deployments (active only)
    try:
        raw = await redis.get("arbiter:modal:deployments")
        if raw:
            deployments = _json.loads(raw)
            for dep in deployments.values() if isinstance(deployments, dict) else []:
                if dep.get("status") == "active":
                    model_id = dep.get("model_id", "")
                    dep_name = dep.get("name", "")
                    # Expose both the raw model ID and a tagged version
                    tagged_id = f"modal/{dep_name}" if dep_name else model_id
                    if tagged_id and tagged_id not in seen:
                        seen.add(tagged_id)
                        model_list.append(ModelInfo(
                            id=tagged_id, object="model",
                            created=created_ts, owned_by="modal",
                        ))
    except Exception as exc:
        logger.debug("Could not load Modal deployments for models list: %s", exc)

    logger.debug(f"Returning {len(model_list)} models")
    return ModelsResponse(object="list", data=model_list)


@router.get(
    "/api/models/info",
    summary="Model info with rate limits and free/paid status",
)
async def models_info(request: Request) -> JSONResponse:
    """
    Return per-vendor model catalog with rate limits (RPM/TPM/RPD),
    context window, and free/paid status — used by the Playground UI
    for intelligent model selection.
    """
    providers = request.app.state.providers
    result = []

    for vendor, model_list in VENDOR_MODEL_HIERARCHY.items():
        if vendor not in providers:
            continue  # Only return configured providers

        limits = PROVIDER_LIMITS.get(vendor, {"rpm": 20, "tpm": 100_000, "daily": 1_000})
        is_free_provider = vendor in _FREE_TIER_PROVIDERS

        # Merge hardcoded hierarchy with any dynamically discovered models
        # from the state store (e.g., after a "Refresh Models" click).
        merged: dict[str, dict] = {}
        for model_id, ctx_window in model_list:
            merged[model_id] = {"context": ctx_window, "free": None}
        for mid, entry in get_model_state(vendor).items():
            if mid not in merged:
                merged[mid] = {
                    "context": entry.get("context") or limits.get("tpm", 131_072),
                    "free": entry.get("free"),
                }
            else:
                # Prefer state-store free flag when set (from fetch_models)
                if entry.get("free") is not None:
                    merged[mid]["free"] = entry["free"]

        vendor_models = []
        for model_id, meta in merged.items():
            # Determine free/paid:
            #   1. state-store free flag (from fetch_models pricing)
            #   2. :free suffix for OpenRouter
            #   3. provider-level free flag minus paid-only exceptions
            if meta.get("free") is not None:
                is_free = bool(meta["free"])
            elif vendor == "openrouter":
                is_free = ":free" in model_id
            else:
                is_free = is_free_provider and model_id not in _PAID_ONLY_MODELS

            vendor_models.append({
                "id":       model_id,
                "context":  meta.get("context"),
                "free":     is_free,
                "enabled":  is_model_enabled(vendor, model_id),
                "rpm":      limits["rpm"],
                "tpm":      limits["tpm"],
                "rpd":      limits["daily"],
            })

        result.append({
            "vendor":     vendor,
            "label":      _VENDOR_LABELS.get(vendor, vendor.title()),
            "free":       is_free_provider,
            "rpm":        limits["rpm"],
            "tpm":        limits["tpm"],
            "rpd":        limits["daily"],
            "models":     vendor_models,
        })

    return JSONResponse(content=result)


_VENDOR_LABELS = {
    "gemini":      "Google Gemini",
    "groq":        "Groq",
    "openrouter":  "OpenRouter",
    "cohere":      "Cohere",
    "cloudflare":  "Cloudflare Workers AI",
    "cerebras":    "Cerebras Inference",
    "huggingface": "HuggingFace",
    "pollinations":"Pollinations.ai",
    "zai":         "Z.ai / Zhipu AI",
    "modal":       "Modal.com",
    "lightning":   "Lightning.ai (LitAI)",
    "routeway":    "Routeway",
}


# ---------------------------------------------------------------------------
# Dynamic model discovery (manual refresh from UI)
# ---------------------------------------------------------------------------


class ToggleModelBody(BaseModel):
    enabled: bool


@router.post(
    "/api/models/{provider}/refresh",
    summary="Refresh a provider's model catalogue",
)
async def refresh_provider_models(provider: str, request: Request,
                                   _admin: dict = Depends(require_admin)) -> JSONResponse:
    """
    Manually fetch the provider's live model list via its ``fetch_models()``
    implementation (typically calls upstream's ``GET /v1/models``). Newly
    discovered models are merged into the state store with ``enabled=True``.

    Per-model enable/disable state is preserved across refreshes.
    """
    providers = request.app.state.providers
    key_pools = request.app.state.key_pools

    if provider not in providers:
        raise HTTPException(404, f"Provider {provider!r} is not configured")

    pool = key_pools.get(provider)
    if pool is None or not pool.keys:
        raise HTTPException(400, f"Provider {provider!r} has no API key configured")

    provider_impl = providers[provider]
    api_key = pool.keys[0]  # any active key — discovery is read-only

    try:
        fetched = await provider_impl.fetch_models(api_key)
    except NotImplementedError:
        raise HTTPException(
            501,
            f"Provider {provider!r} does not support dynamic model discovery. "
            f"Model list is hardcoded; edit app/routing/router.py to change it.",
        )
    except RateLimitError as exc:
        raise HTTPException(429, f"Rate limited while fetching models: {exc}")
    except ProviderError as exc:
        raise HTTPException(502, f"Upstream error fetching models: {exc}")
    except Exception as exc:
        logger.exception("Model refresh failed for %s", provider)
        raise HTTPException(500, f"Model refresh failed: {exc}")

    record_discovered_models(provider, fetched)
    logger.info("Refreshed %d models for provider %s", len(fetched), provider)

    return JSONResponse({
        "provider":   provider,
        "discovered": len(fetched),
        "models":     fetched,
    })


@router.post(
    "/api/models/{provider}/{model_id:path}/toggle",
    summary="Enable or disable a specific model",
)
async def toggle_model(
    provider: str, model_id: str, body: ToggleModelBody, request: Request,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    """
    Enable or disable a specific model for a provider. Disabled models are
    skipped during routing and hidden from ``/v1/models``.
    """
    if provider not in request.app.state.providers:
        raise HTTPException(404, f"Provider {provider!r} is not configured")

    set_model_enabled(provider, model_id, body.enabled)
    return JSONResponse({
        "provider": provider,
        "model":    model_id,
        "enabled":  body.enabled,
    })
