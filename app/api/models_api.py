import time
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.schemas import ModelInfo, ModelsResponse
from app.routing.router import VENDOR_MODEL_HIERARCHY
from app.key_management.key_pool import PROVIDER_LIMITS

logger = logging.getLogger(__name__)

router = APIRouter()

# Which models are free-tier vs paid
_FREE_TIER_PROVIDERS = {"gemini", "groq", "openrouter", "cohere", "cloudflare",
                        "cerebras", "huggingface", "pollinations", "zai"}

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

    # Standard provider models
    for provider_name, provider in providers.items():
        owned_by = PROVIDER_OWNERS.get(provider_name, provider_name)
        for model_id in provider.models:
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

        vendor_models = []
        for model_id, ctx_window in model_list:
            is_free = is_free_provider and model_id not in _PAID_ONLY_MODELS
            # OpenRouter free models have ":free" suffix
            if vendor == "openrouter" and ":free" not in model_id:
                is_free = False

            vendor_models.append({
                "id":           model_id,
                "context":      ctx_window,
                "free":         is_free,
                "rpm":          limits["rpm"],
                "tpm":          limits["tpm"],
                "rpd":          limits["daily"],
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
}
