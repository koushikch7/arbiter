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
