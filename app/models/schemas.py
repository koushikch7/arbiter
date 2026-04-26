import uuid
import time
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str
    content: Union[str, list]

    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    stream: bool = False
    top_p: float = 1.0
    stop: Optional[list] = None

    # ── Arbiter routing controls (non-OpenAI extensions, all optional) ──
    fallback: Optional[str] = Field(
        default=None,
        description=(
            "Arbiter v1.12+. Fallback policy when caller pins a specific model. "
            "'none' (default): strict pin, return 502 if it fails. "
            "'same_provider': try other models on the same provider. "
            "'chain': cross-provider capability-matched fallback via auto-router."
        ),
        examples=["none", "same_provider", "chain"],
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Arbiter v1.12+. Optional routing metadata. Recognised keys: "
            "'arbiter_intent' (code|reasoning|long-context|vision|creative|fast|balanced) — "
            "force intent classification; "
            "'priority' (speed|quality|balanced) — auto-routing scoring bias; "
            "'prefer_provider' (provider name) — boost a provider in auto routing."
        ),
    )

    model_config = {"extra": "allow"}


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: str


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:8]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[Choice]
    usage: Usage


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str


class ModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


class ErrorResponse(BaseModel):
    error: Dict[str, Any]
