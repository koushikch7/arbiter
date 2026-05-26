import logging
from typing import Optional

import httpx as _httpx
import time as _time
import uuid as _uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.models.schemas import ChatCompletionRequest, ChatCompletionResponse, ErrorResponse
from app.models.schemas import Choice, Message, Usage
from app.providers.base import RateLimitError, ProviderError
from app.observability.persistent_log import log_api_call as _persist_api_call
from app.observability import stats as _obs_stats

logger = logging.getLogger(__name__)


def _client_ip(request) -> Optional[str]:
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else None
    except Exception:
        return None


async def _safe_persist(**kwargs):
    try:
        await _persist_api_call(**kwargs)
    except Exception:
        pass

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

    Smart auto-routing
    ──────────────────
    - ``model="auto"`` (or empty) lets Arbiter classify the request and
      pick the best free-tier model based on capability tags.
    - Per-request overrides:
        * Header ``X-Arbiter-Priority: speed|quality|balanced``
        * Header ``X-Arbiter-Prefer-Provider: <name>``
        * Header ``X-Arbiter-Fallback: none|same_provider|chain``
        * Body field ``fallback`` (same values, takes precedence)
        * Body field ``metadata.arbiter_intent`` (force intent classifier)

    The response includes ``X-Arbiter-Model-Used`` header showing the
    actual ``provider/model`` that fulfilled the request.

    Streaming
    ─────────
    Set ``stream: true`` in the request body to receive an OpenAI-compatible
    Server-Sent Events stream (``text/event-stream``). The same routing,
    fallback, key-rotation, and caching logic applies; the response is
    delivered as a sequence of ``chat.completion.chunk`` events terminated
    by ``data: [DONE]``.
    """
    router_instance = request.app.state.router
    _req_start = _time.monotonic()
    _req_ip = _client_ip(request)
    _redis_ref = getattr(request.app.state, "redis", None)

    # ── CF Worker direct proxy (model = "cfworker/{name}") ────────────────
    effective_model = force_model or body.model or ""
    if effective_model.startswith("cfworker/"):
        if body.stream:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Streaming is not supported for cfworker/* direct proxies.",
            )
        _tok = (
            getattr(request.state, "gateway_token_id", None)
            or getattr(request.state, "gateway_token_name", None)
            or "anon"
        )
        await _obs_stats.inflight_increment(_redis_ref)
        try:
            _cf_resp = await _proxy_cfworker(effective_model, body, request)
            _u = getattr(_cf_resp, "usage", None)
            await _safe_persist(
                token_id=_tok, method="POST", path="/v1/chat/completions",
                model=effective_model, provider="cfworker", status_code=200,
                latency_ms=int((_time.monotonic() - _req_start) * 1000),
                prompt_tokens=getattr(_u, "prompt_tokens", None) if _u else None,
                completion_tokens=getattr(_u, "completion_tokens", None) if _u else None,
                cached=False, error=None, request_id=None, client_ip=_req_ip,
            )
            return _cf_resp
        except HTTPException as e:
            await _safe_persist(
                token_id=_tok, method="POST", path="/v1/chat/completions",
                model=effective_model, provider="cfworker",
                status_code=e.status_code,
                latency_ms=int((_time.monotonic() - _req_start) * 1000),
                prompt_tokens=None, completion_tokens=None, cached=False,
                error=str(e.detail), request_id=None, client_ip=_req_ip,
            )
            raise
        finally:
            await _obs_stats.inflight_decrement(_redis_ref)

    # ── Per-request routing overrides (headers + metadata) ────────────────
    priority_override        = request.headers.get("x-arbiter-priority")
    prefer_provider_override = request.headers.get("x-arbiter-prefer-provider")
    fallback_header          = request.headers.get("x-arbiter-fallback")

    meta = body.metadata or {}
    if isinstance(meta, dict):
        priority_override        = meta.get("priority", priority_override)
        prefer_provider_override = meta.get("prefer_provider", prefer_provider_override)

    # Body `fallback` field takes precedence over header.
    if body.fallback is None and fallback_header:
        body = body.model_copy(update={"fallback": fallback_header.lower()})

    if priority_override:
        priority_override = str(priority_override).lower().strip()
        if priority_override not in ("speed", "quality", "balanced"):
            priority_override = None

    # Identify the calling gateway token (set by GatewayAuthMiddleware)
    token_id = getattr(request.state, "gateway_token_id", None)
    token_name = getattr(request.state, "gateway_token_name", None)
    routing_policy = getattr(request.state, "gateway_routing_policy", "auto")
    allowed_models = getattr(request.state, "gateway_allowed_models", None)
    blocked_models = getattr(request.state, "gateway_blocked_models", None)

    _tok_for_log = token_id or token_name or "anon"

    # ── v1.20: Real-time web search (Tavily) auto-inject ─────────────────
    # When the caller opts in via X-Arbiter-Realtime: true (or metadata.realtime),
    # search the live web with Tavily and prepend results to the system prompt.
    # The chosen LLM then answers grounded in those sources. Source URLs are
    # echoed back in the X-Arbiter-Realtime-Sources response header.
    _realtime_hdr = (request.headers.get("x-arbiter-realtime") or "").strip().lower()
    _realtime_meta = bool(meta.get("realtime")) if isinstance(meta, dict) else False
    _realtime_on = _realtime_hdr in ("true", "1", "yes", "on") or _realtime_meta
    _realtime_sources: list = []
    if _realtime_on:
        try:
            from app.services.web_search import TavilyClient
            _client = TavilyClient(redis_client=_redis_ref)
            if _client.enabled:
                # Extract user query from last user message
                _q = ""
                for _m in reversed(body.messages or []):
                    if getattr(_m, "role", None) == "user":
                        _c = getattr(_m, "content", "")
                        if isinstance(_c, str):
                            _q = _c
                        elif isinstance(_c, list):
                            # Multimodal — pick the text parts
                            _q = " ".join(
                                p.get("text", "") for p in _c
                                if isinstance(p, dict) and p.get("type") == "text"
                            )
                        break
                if _q:
                    _sr = await _client.search(_q, max_results=5)
                    if _sr and (_sr.results or _sr.answer):
                        _ctx = _sr.as_context_block()
                        # Prepend as a fresh system message so the model
                        # consumes it before answering the user.
                        from app.models.schemas import Message as _Msg
                        body = body.model_copy(update={
                            "messages": [_Msg(role="system", content=_ctx)] + list(body.messages),
                        })
                        _realtime_sources = _sr.source_urls()
                        logger.info(
                            f"[realtime] Tavily injected {len(_realtime_sources)} sources "
                            f"({_sr.latency_ms}ms) into request for token={_tok_for_log}"
                        )
            else:
                logger.warning("[realtime] requested but TAVILY_API_KEY not configured")
        except Exception as _e:
            logger.exception(f"[realtime] Tavily lookup failed (non-fatal): {_e}")

    # ── v1.20: OpenRouter :online opt-in ──────────────────────────────────
    # If the caller forced an openrouter/* model AND set X-Arbiter-Realtime,
    # append the :online suffix so OpenRouter's web plugin kicks in. Opt-in
    # only — never auto-applied to avoid surprise charges.
    if _realtime_on and effective_model and "/" in effective_model and not effective_model.endswith(":online"):
        try:
            # Heuristic: only do this for known OpenRouter-style ids
            # (vendor/model) — never for cfworker/* or arbiter routing tags.
            if effective_model.lower().startswith(("openrouter/",)) or effective_model.count("/") == 1:
                effective_model_online = f"{effective_model}:online"
                body = body.model_copy(update={"model": effective_model_online})
                effective_model = effective_model_online
                logger.info(f"[realtime] OpenRouter :online suffix applied → {effective_model}")
        except Exception:
            pass

    # ── Streaming branch: return SSE response ─────────────────────────────
    if body.stream:
        agen = router_instance.route_stream(
            body,
            vendor=vendor,
            force_model=force_model,
            priority_override=priority_override,
            prefer_provider_override=prefer_provider_override,
            token_id=token_id,
            token_name=token_name,
            routing_policy=routing_policy,
            allowed_models=allowed_models,
            blocked_models=blocked_models,
        )

        async def _logged_stream():
            _err = None
            _status = 200
            await _obs_stats.inflight_increment(_redis_ref)
            try:
                async for chunk in agen:
                    yield chunk
            except Exception as e:
                _err = str(e)
                _status = 500
                raise
            finally:
                await _obs_stats.inflight_decrement(_redis_ref)
                await _safe_persist(
                    token_id=_tok_for_log,
                    method="POST",
                    path="/v1/chat/completions",
                    model=effective_model or "auto",
                    provider=None,
                    status_code=_status,
                    latency_ms=int((_time.monotonic() - _req_start) * 1000),
                    prompt_tokens=None,
                    completion_tokens=None,
                    cached=False,
                    error=_err,
                    request_id=None,
                    client_ip=_req_ip,
                )

        return StreamingResponse(
            _logged_stream(),
            media_type="text/event-stream",
            headers={
                # Disable nginx buffering so chunks arrive as they're yielded
                "Cache-Control":     "no-cache",
                "Connection":        "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        await _obs_stats.inflight_increment(_redis_ref)
        response = await router_instance.route(
            body,
            vendor=vendor,
            force_model=force_model,
            priority_override=priority_override,
            prefer_provider_override=prefer_provider_override,
            token_id=token_id,
            token_name=token_name,
            routing_policy=routing_policy,
            allowed_models=allowed_models,
            blocked_models=blocked_models,
        )
        chosen_provider = getattr(response, "_arbiter_provider", "") or ""
        chosen_model    = getattr(response, "_arbiter_model", response.model)
        if not chosen_provider and chosen_model:
            try:
                from app.providers._free_tier_catalog import provider_of as _po
                chosen_provider = _po(chosen_model) or ""
            except Exception:
                chosen_provider = ""
        from fastapi.responses import JSONResponse as _JR
        payload = response.model_dump()
        # Echo the actual model used into the JSON body so SDKs that only
        # read  see the truth (instead of "auto" or the user's pin).
        if chosen_model:
            payload["model"] = chosen_model
        # Embed routing metadata for client introspection.
        try:
            from app.routing.complexity_analyzer import analyze_complexity as _ac
            _complexity = _ac(body).name
        except Exception:
            _complexity = ""
        if _complexity:
            payload.setdefault("x_arbiter", {})
            payload["x_arbiter"]["complexity"] = _complexity
            payload["x_arbiter"]["provider"] = chosen_provider
            payload["x_arbiter"]["model"] = chosen_model
            if _realtime_sources:
                payload["x_arbiter"]["realtime_sources"] = _realtime_sources
        _usage = getattr(response, "usage", None)
        await _safe_persist(
            token_id=_tok_for_log,
            method="POST",
            path="/v1/chat/completions",
            model=str(chosen_model) if chosen_model else (effective_model or "auto"),
            provider=chosen_provider or None,
            status_code=200,
            latency_ms=int((_time.monotonic() - _req_start) * 1000),
            prompt_tokens=getattr(_usage, "prompt_tokens", None) if _usage else None,
            completion_tokens=getattr(_usage, "completion_tokens", None) if _usage else None,
            cached=bool(getattr(response, "_arbiter_cached", False)),
            error=None,
            request_id=None,
            client_ip=_req_ip,
        )
        _resp_headers = {
            "X-Arbiter-Model-Used": (
                f"{chosen_provider}/{chosen_model}" if chosen_provider else str(chosen_model)
            ),
        }
        if _complexity:
            _resp_headers["X-Arbiter-Complexity"] = _complexity
        if _realtime_sources:
            _resp_headers["X-Arbiter-Realtime-Sources"] = ",".join(_realtime_sources[:5])
        return _JR(content=payload, headers=_resp_headers)
    except RateLimitError as e:
        logger.warning(f"All providers rate-limited: {e}")
        await _safe_persist(
            token_id=_tok_for_log, method="POST", path="/v1/chat/completions",
            model=effective_model or "auto", provider=None, status_code=429,
            latency_ms=int((_time.monotonic() - _req_start) * 1000),
            prompt_tokens=None, completion_tokens=None, cached=False,
            error=str(e), request_id=None, client_ip=_req_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
        )
    except ProviderError as e:
        logger.error(f"Provider error: {e}")
        # "All keys on cooldown / exhausted" → 503 (service temporarily
        # unavailable) is more accurate than 502 (bad upstream response).
        msg = str(e)
        _sc = 503 if ("on cooldown" in msg or "quota exhausted" in msg) else 502
        await _safe_persist(
            token_id=_tok_for_log, method="POST", path="/v1/chat/completions",
            model=effective_model or "auto", provider=None, status_code=_sc,
            latency_ms=int((_time.monotonic() - _req_start) * 1000),
            prompt_tokens=None, completion_tokens=None, cached=False,
            error=msg, request_id=None, client_ip=_req_ip,
        )
        if _sc == 503:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=msg,
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=msg,
        )
    except Exception as e:
        logger.exception(f"Unexpected error in chat completions: {e}")
        await _safe_persist(
            token_id=_tok_for_log, method="POST", path="/v1/chat/completions",
            model=effective_model or "auto", provider=None, status_code=500,
            latency_ms=int((_time.monotonic() - _req_start) * 1000),
            prompt_tokens=None, completion_tokens=None, cached=False,
            error=str(e), request_id=None, client_ip=_req_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
    finally:
        await _obs_stats.inflight_decrement(_redis_ref)
