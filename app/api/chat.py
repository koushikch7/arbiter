import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse, ErrorResponse
from app.providers.base import RateLimitError, ProviderError

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_auth(request: Request) -> None:
    """Validate the gateway API key if one is configured (legacy single-key check)."""
    from app.config import settings
    if not settings.GATEWAY_API_KEY:
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = auth_header.removeprefix("Bearer ").strip()
    if token != settings.GATEWAY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid gateway API key",
        )


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    summary="Create a chat completion",
)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    vendor: Optional[str] = Query(
        None,
        description=(
            "Force a specific provider (gemini, groq, cloudflare, cerebras, "
            "huggingface, pollinations, openrouter, cohere). "
            "The named provider is tried first before fallback to others."
        ),
    ),
    force_model: Optional[str] = Query(
        None,
        description=(
            "Force a specific model ID, bypassing automatic model selection. "
            "Overrides the model field in the request body."
        ),
    ),
) -> ChatCompletionResponse:
    """
    OpenAI-compatible chat completions endpoint.

    Use the optional **vendor** query parameter to pin a specific provider
    (e.g. `?vendor=cerebras`).  Use **force_model** to override the model
    (e.g. `?force_model=llama3.1-8b`).
    """
    _check_auth(request)

    if body.stream:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Streaming is not yet supported. Set stream=false.",
        )

    router_instance = request.app.state.router

    try:
        response = await router_instance.route(
            body,
            vendor=vendor,
            force_model=force_model,
        )
        return response
    except RateLimitError as e:
        logger.warning(f"All providers rate-limited: {e}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
        )
    except ProviderError as e:
        logger.error(f"Provider error: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )
    except Exception as e:
        logger.exception(f"Unexpected error in chat completions: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
