import time
import logging
from fastapi import APIRouter, Request

from app.models.schemas import ModelInfo, ModelsResponse

logger = logging.getLogger(__name__)

router = APIRouter()

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
