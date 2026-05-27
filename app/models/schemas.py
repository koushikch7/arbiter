import uuid
import time
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str = Field(..., max_length=64)
    content: Union[str, list]

    model_config = {"extra": "allow"}




class ChatTool(BaseModel):
    """OpenAI-format tool definition (function calling or built-in like google_search).
    Forwarded to providers that support tools (groq, nvidia, openrouter, cerebras, ollama)
    or interpreted natively (Gemini google_search grounding).
    """
    type: str = Field(..., description="Tool type: function | google_search | google_search_retrieval | web_search")
    function: Optional[Dict[str, Any]] = Field(None, description="For type=function: name, description, parameters")

    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    model: str = Field(..., max_length=256)
    messages: List[Message]
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=128_000)
    stream: bool = False
    top_p: float = Field(1.0, ge=0.0, le=1.0)
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
            "Arbiter routing metadata. Recognised keys: "
            "arbiter_intent (code|reasoning|long-context|vision|creative|fast|balanced) - force intent; "
            "priority (speed|quality|balanced) - auto-routing scoring bias; "
            "prefer_provider (provider name) - boost a provider; "
            "realtime (bool, v1.20) - enable Tavily web search auto-injection; "
            "web_search or google_search (bool, v1.20) - enable Gemini Google Search grounding when routed to Gemini."
        ),
    )
    tools: Optional[List[ChatTool]] = Field(
        default=None,
        description=(
            "OpenAI-format tool definitions. Routes to tool-capable providers only (groq, nvidia, "
            "openrouter, cerebras, ollama). For Gemini, type=google_search activates native grounding."
        ),
    )
    tool_choice: Optional[Any] = Field(default=None, description="auto | none | required | {type: function, function: {name: ...}}")
    parallel_tool_calls: Optional[bool] = Field(default=None, description="Allow the model to issue multiple tool calls in one response.")
    response_format: Optional[Dict[str, Any]] = Field(default=None, description="{type: json_object} for JSON mode, or {type: json_schema, json_schema: {...}}.")

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
