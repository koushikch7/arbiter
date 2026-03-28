"""
Modal.com serverless GPU provider adapter.

Modal offers $30 free credits every month.  Users deploy an LLM inference app
on Modal (using the vLLM / llama.cpp template or a custom FastAPI app) and register
the resulting HTTPS endpoint URL in Arbiter.

Key format:  {endpoint_url}|{modal_token_id}:{modal_token_secret}
  endpoint_url  — HTTPS URL of the deployed Modal web endpoint
                  e.g.  https://myorg--llm-serve-web.modal.run
  modal_token   — Modal API token in  tok_id:tok_secret  format
                  e.g.  ak-abc123:as-def456
                  Leave after  |  empty for public (no-auth) endpoints.

Examples:
  https://myorg--llm.modal.run|ak-abc123:as-xyz789   (authenticated)
  https://myorg--llm.modal.run|                       (public endpoint)

The provider calls the endpoint's  /v1/chat/completions  path (OpenAI-compatible).
Any model name is passed through as-is; the endpoint handles model routing.

Suggested Modal templates to deploy:
  https://modal.com/docs/examples/vllm_inference
  https://modal.com/docs/examples/llm-serving

Free tier credits: ~$30/month
Typical cost: ~$0.0003 per 1K tokens on A10G GPU
Estimate: ~100K token requests/day on free tier
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import List, Optional, Tuple

import httpx

from app.providers.base import BaseProvider, RateLimitError, ProviderError
from app.models.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)

logger = logging.getLogger(__name__)

# Suggested model IDs to use with popular Modal templates
# These are just defaults — the actual model depends on what the user deployed
_DEFAULT_MODELS = [
    "meta-llama/Llama-3.1-8B-Instruct",       # fast, cheap on Modal
    "meta-llama/Llama-3.3-70B-Instruct",       # high quality
    "mistralai/Mistral-7B-Instruct-v0.3",      # efficient
    "Qwen/Qwen2.5-72B-Instruct",               # large, capable
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", # reasoning
]


def _parse_key(api_key: str) -> Tuple[str, Optional[str]]:
    """
    Split  endpoint_url|token  → (endpoint_url, token_or_None).
    Strips trailing slashes from the URL.
    """
    parts = api_key.split("|", 1)
    url   = parts[0].strip().rstrip("/")
    token = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    if not url.startswith("http"):
        raise ProviderError(
            "Modal key must start with the endpoint URL "
            "(e.g.  https://myorg--app.modal.run|your_token)"
        )
    return url, token


class ModalProvider(BaseProvider):
    name = "modal"

    # Override via Settings → Models tab as needed for your deployment
    models: List[str] = _DEFAULT_MODELS

    max_context_tokens = 131_072  # depends on deployed model; conservative default
    default_model      = "meta-llama/Llama-3.1-8B-Instruct"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """
        Call the Modal endpoint's /v1/chat/completions path.

        api_key format:  endpoint_url|token   (token is optional for public endpoints)
        The model name is forwarded as-is (set it to whatever your deployment serves).
        """
        endpoint_url, token = _parse_key(api_key)
        url = f"{endpoint_url}/v1/chat/completions"

        model = request.model or self.default_model

        messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "temperature": request.temperature,
            "top_p":       request.top_p,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        headers: dict = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        logger.debug(f"ModalProvider POST model={model} url={endpoint_url}")

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Modal network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Modal 429 (rate limited): {resp.text[:300]}")
        if resp.status_code in (401, 403):
            raise ProviderError(
                f"Modal auth error {resp.status_code} — check your token"
            )
        if resp.status_code == 502:
            raise ProviderError(
                "Modal 502 — endpoint may be cold-starting, retry in a few seconds"
            )
        if resp.status_code != 200:
            raise ProviderError(f"Modal {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Modal response parse error: {exc}") from exc

        prompt_tokens     = usage_raw.get("prompt_tokens",     0)
        completion_tokens = usage_raw.get("completion_tokens", 0)

        return ChatCompletionResponse(
            id      = data.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
            object  = "chat.completion",
            created = data.get("created", int(time.time())),
            model   = data.get("model", model),
            choices = [
                Choice(
                    index         = 0,
                    message       = Message(role="assistant", content=msg.get("content", "")),
                    finish_reason = finish,
                )
            ],
            usage = Usage(
                prompt_tokens     = prompt_tokens,
                completion_tokens = completion_tokens,
                total_tokens      = prompt_tokens + completion_tokens,
            ),
        )
