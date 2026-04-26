from abc import ABC, abstractmethod
from typing import List
from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse, Message


class RateLimitError(Exception):
    """Raised when an API key hits its rate limit."""
    pass


class ProviderError(Exception):
    """Raised when a provider returns an unexpected error."""
    pass


class BaseProvider(ABC):
    name: str = ""
    models: List[str] = []
    max_context_tokens: int = 4096

    @abstractmethod
    async def complete(
        self, request: ChatCompletionRequest, api_key: str
    ) -> ChatCompletionResponse:
        """Send a chat completion request to the provider and return an OpenAI-compatible response."""
        ...

    def supports_model(self, model: str) -> bool:
        """Return True if this provider supports the given model ID."""
        return model in self.models

    def estimate_tokens(self, messages: List[Message]) -> int:
        """Estimate the token count for a list of messages using a simple word-count heuristic."""
        total = 0
        for m in messages:
            if isinstance(m.content, str):
                total += int(len(m.content.split()) * 1.3)
            elif isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict) and "text" in part:
                        total += int(len(part["text"].split()) * 1.3)
        return total

    # ------------------------------------------------------------------
    # Optional: dynamic model discovery
    # ------------------------------------------------------------------
    async def fetch_models(self, api_key: str) -> list[dict]:
        """
        Optional — fetch the provider's live model catalogue.

        Providers that expose a `/v1/models` endpoint (or equivalent) can
        override this method. Callers must handle NotImplementedError
        gracefully (fall back to the hardcoded ``models`` list).

        Return shape::
            [{"id": str, "context": int | None, "free": bool | None}, ...]

        Raises:
            NotImplementedError — if the provider has no discovery endpoint.
            RateLimitError      — on 429.
            ProviderError       — on any other failure.
        """
        raise NotImplementedError(
            f"Provider {self.name!r} does not support dynamic model discovery"
        )
