import logging
from typing import Optional

import httpx as _httpx
import time as _time
import uuid as _uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse, ErrorResponse
from app.models.schemas import Choice, Message, Usage
from app.providers.base import RateLimitError, ProviderError

logger = logging.getLogger(__name__)

router = APIRouter()


async def _proxy_cfworker(model_str: str, body, request) -> ChatCompletionResponse:
    """
    Proxy a chat request directly to a Cloudflare Worker URL.
    Model format: cfworker/{worker-name}
    """
    from app.api.cloudflare_manager import _load_worker_registry
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)
    worker_name = model_str[len("cfworker/"):]
    meta = registry.get(worker_name)
    if not meta:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CF Worker '{worker_name}' not found in registry. Create it in Settings → CF Workers.",
        )
    worker_url = meta.get("url", "")
    if not worker_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"CF Worker '{worker_name}' has no URL yet (still provisioning?)",
        )

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    payload = {
        "messages":    messages,
        "max_tokens":  body.max_tokens or 512,
        "temperature": body.temperature,
        "top_p":       body.top_p,
    }
    t0 = _time.perf_counter()
    try:
        async with _httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                worker_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except _httpx.RequestError as exc:
        raise HTTPException(502, f"CF Worker unreachable: {exc}")

    latency_ms = round((_time.perf_counter() - t0) * 1000)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"CF Worker error: {resp.text[:300]}")

    data = resp.json()
    try:
        choice = data["choices"][0]
        text   = choice["message"]["content"]
        finish = choice.get("finish_reason", "stop")
    except (KeyError, IndexError) as exc:
        raise HTTPException(502, f"CF Worker response parse error: {exc}")

    logger.info("CF Worker '%s' responded in %dms", worker_name, latency_ms)
    return ChatCompletionResponse(
        id      = data.get("id", f"chatcmpl-{_uuid.uuid4().hex[:8]}"),
        object  = "chat.completion",
        created = data.get("created", int(_time.time())),
        model   = model_str,
        choices = [Choice(
            index=0,
            message=Message(role="assistant", content=text),
            finish_reason=finish,
        )],
        usage = Usage(
            prompt_tokens     = (data.get("usage") or {}).get("prompt_tokens", 0),
            completion_tokens = (data.get("usage") or {}).get("completion_tokens", 0),
            total_tokens      = (data.get("usage") or {}).get("total_tokens", 0),
        ),
    )


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

    # ── CF Worker direct proxy (model = "cfworker/{name}") ────────────────
    effective_model = force_model or body.model or ""
    if effective_model.startswith("cfworker/"):
        return await _proxy_cfworker(effective_model, body, request)

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
