"""
Cloudflare Workers AI provider adapter (OpenAI-compatible endpoint).

Full model catalogue verified March 2026:
  https://developers.cloudflare.com/workers-ai/models/

Selected free-tier text-generation models (ordered best → fallback):
  @cf/meta/llama-4-scout-17b-16e-instruct   – Llama 4 Scout 17B (newest)
  @cf/meta/llama-3.3-70b-instruct-fp8-fast  – Llama 3.3 70B fast
  @cf/moonshot/kimi-k2.5                    – 256K context
  @cf/qwen/qwen3-30b-a3b-fp8               – Qwen 3 30B
  @cf/mistralai/mistral-small-3.1-24b-instruct – Mistral 24B
  @cf/deepseek/deepseek-r1-distill-qwen-32b – DeepSeek R1 reasoning
  @cf/qwen/qwq-32b                          – QwQ 32B reasoning
  @cf/qwen/qwen2.5-coder-32b-instruct      – coding specialist
  @cf/google/gemma-3-12b-it                – Gemma 3 12B (128K ctx)
  @cf/meta/llama-3.1-8b-instruct           – fastest 8B fallback
  @cf/meta/llama-3.2-3b-instruct           – smallest 3B fallback

API key format:  {account_id}|{api_token}
Endpoint:        https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions
Rate limit:      300 RPM (Workers AI free tier)
"""

import logging
import time
import uuid
from typing import List, Tuple

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

_CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"


def _split_key(api_key: str) -> Tuple[str, str]:
    """Split a  account_id|api_token  composite key."""
    parts = api_key.split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ProviderError(
            "Cloudflare API key must be in  'account_id|api_token'  format"
        )
    return parts[0].strip(), parts[1].strip()


class CloudflareProvider(BaseProvider):
    name = "cloudflare"

    models: List[str] = [
        "@cf/meta/llama-4-scout-17b-16e-instruct",        # Llama 4 Scout — newest
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",       # Llama 3.3 70B fast
        "@cf/moonshot/kimi-k2.5",                          # 256K context
        "@cf/qwen/qwen3-30b-a3b-fp8",                     # Qwen 3 30B
        "@cf/mistralai/mistral-small-3.1-24b-instruct",   # Mistral Small 24B
        "@cf/deepseek/deepseek-r1-distill-qwen-32b",      # DeepSeek R1 reasoning
        "@cf/qwen/qwq-32b",                                # QwQ reasoning
        "@cf/qwen/qwen2.5-coder-32b-instruct",            # coding specialist
        "@cf/google/gemma-3-12b-it",                       # Gemma 3 12B (128K)
        "@cf/meta/llama-3.1-8b-instruct",                 # fastest 8B fallback
        "@cf/meta/llama-3.2-3b-instruct",                 # smallest 3B fallback
    ]

    max_context_tokens = 131_072   # Kimi K2.5 and most large models
    default_model      = "@cf/meta/llama-4-scout-17b-16e-instruct"

    # ------------------------------------------------------------------
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """
        Call the Cloudflare Workers AI OpenAI-compatible endpoint.

        api_key must be in  account_id|api_token  format.
        Falls back to default_model when the requested model is not
        in the supported list.
        """
        account_id, api_token = _split_key(api_key)
        model = request.model if request.model in self.models else self.default_model

        url = _CF_API_BASE.format(account_id=account_id)

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

        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type":  "application/json",
        }

        logger.debug(f"CloudflareProvider POST model={model} account={account_id[:8]}…")

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                raise ProviderError(f"Cloudflare network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Cloudflare 429: {resp.text[:300]}")
        if resp.status_code != 200:
            raise ProviderError(f"Cloudflare {resp.status_code}: {resp.text[:500]}")

        data = resp.json()

        try:
            choice    = data["choices"][0]
            msg       = choice["message"]
            finish    = choice.get("finish_reason", "stop")
            usage_raw = data.get("usage", {})
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"Cloudflare response parse error: {exc}") from exc

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
