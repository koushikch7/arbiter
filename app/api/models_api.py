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
    """Return all models available across all configured providers."""
    providers = request.app.state.providers
    created_ts = int(time.time())

    model_list = []
    seen = set()

    for provider_name, provider in providers.items():
        owned_by = PROVIDER_OWNERS.get(provider_name, provider_name)
        for model_id in provider.models:
            if model_id not in seen:
                seen.add(model_id)
                model_list.append(
                    ModelInfo(
                        id=model_id,
                        object="model",
                        created=created_ts,
                        owned_by=owned_by,
                    )
                )

    logger.debug(f"Returning {len(model_list)} models")
    return ModelsResponse(object="list", data=model_list)
