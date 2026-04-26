"""
Modal.com endpoint management.

Modal deployments are created externally (via `modal deploy` CLI or the Modal
dashboard), then registered here so Arbiter can route traffic to them.

Routes
------
GET    /modal/endpoints              List registered Modal endpoints
POST   /modal/endpoints              Register a new endpoint
DELETE /modal/endpoints/{name}       Unregister an endpoint
POST   /modal/endpoints/{name}/test  Test endpoint connectivity
GET    /modal/templates              Return deployment templates / setup guide
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from app.api.users_api import require_admin
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/modal", tags=["Modal"], dependencies=[Depends(require_admin)])

_REDIS_KEY = "arbiter:modal:endpoints"   # JSON list of endpoint dicts


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

async def _load_endpoints(redis) -> list:
    raw = await redis.get(_REDIS_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


async def _save_endpoints(redis, endpoints: list) -> None:
    await redis.set(_REDIS_KEY, json.dumps(endpoints))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RegisterEndpointBody(BaseModel):
    name: str                              # friendly label, e.g. "llama-8b"
    url: str                               # Modal web URL, e.g. https://org--app.modal.run
    token: Optional[str] = None           # Modal token if endpoint requires auth
    models: Optional[list] = None         # model IDs this endpoint serves
    description: Optional[str] = None     # optional note


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/endpoints", summary="List registered Modal endpoints")
async def list_endpoints(request: Request) -> JSONResponse:
    endpoints = await _load_endpoints(request.app.state.redis)
    # Mask tokens
    safe = []
    for ep in endpoints:
        masked = dict(ep)
        if masked.get("token"):
            t = masked["token"]
            masked["token_masked"] = t[:6] + "..." + t[-4:] if len(t) > 10 else "****"
            del masked["token"]
        safe.append(masked)
    return JSONResponse(content=safe)


@router.post("/endpoints", summary="Register a Modal endpoint", status_code=201)
async def register_endpoint(body: RegisterEndpointBody, request: Request) -> JSONResponse:
    """
    Register a Modal web endpoint URL with Arbiter.
    The endpoint must serve an OpenAI-compatible /v1/chat/completions API.
    """
    url = body.url.strip().rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(400, "URL must start with https://")

    redis = request.app.state.redis
    endpoints = await _load_endpoints(redis)

    # Dedup check
    if any(ep["name"] == body.name for ep in endpoints):
        raise HTTPException(409, f"Endpoint '{body.name}' already registered")

    entry = {
        "name":        body.name,
        "url":         url,
        "token":       body.token or "",
        "models":      body.models or [],
        "description": body.description or "",
        "registered_at": int(time.time()),
    }
    endpoints.append(entry)
    await _save_endpoints(redis, endpoints)

    # Add to provider key pool (url|token format)
    composite_key = f"{url}|{body.token or ''}"
    try:
        from app.api.keys_api import _save_redis_keys, _redis_keys, _reload_provider
        existing = await _redis_keys(redis, "modal")
        if composite_key not in existing:
            existing.append(composite_key)
            await _save_redis_keys(redis, "modal", existing)
            await _reload_provider("modal", request)
    except Exception as exc:
        logger.warning("Could not add Modal key to pool: %s", exc)

    return JSONResponse(status_code=201, content={"success": True, "name": body.name, "url": url})


@router.delete("/endpoints/{name}", summary="Unregister a Modal endpoint")
async def delete_endpoint(name: str, request: Request) -> JSONResponse:
    redis = request.app.state.redis
    endpoints = await _load_endpoints(redis)

    target = next((ep for ep in endpoints if ep["name"] == name), None)
    if not target:
        raise HTTPException(404, f"Endpoint '{name}' not found")

    endpoints = [ep for ep in endpoints if ep["name"] != name]
    await _save_endpoints(redis, endpoints)

    # Remove from key pool
    composite_key = f"{target['url']}|{target.get('token','')}"
    try:
        from app.api.keys_api import _redis_keys, _save_redis_keys, _reload_provider
        existing = await _redis_keys(redis, "modal")
        existing = [k for k in existing if k != composite_key]
        await _save_redis_keys(redis, "modal", existing)
        await _reload_provider("modal", request)
    except Exception as exc:
        logger.warning("Could not remove Modal key from pool: %s", exc)

    return JSONResponse(content={"success": True, "deleted": name})


@router.post("/endpoints/{name}/test", summary="Test a Modal endpoint")
async def test_endpoint(name: str, request: Request) -> JSONResponse:
    redis = request.app.state.redis
    endpoints = await _load_endpoints(redis)
    ep = next((e for e in endpoints if e["name"] == name), None)
    if not ep:
        raise HTTPException(404, f"Endpoint '{name}' not found")

    url   = ep["url"].rstrip("/") + "/v1/chat/completions"
    token = ep.get("token", "")

    payload = {
        "model":       ep.get("models", [""])[0] or "default",
        "messages":    [{"role": "user", "content": "Say 'ok' in one word."}],
        "max_tokens":  5,
        "temperature": 0.0,
    }
    headers: dict = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        latency_ms = round((time.perf_counter() - t0) * 1000)

        if resp.status_code == 200:
            data   = resp.json()
            reply  = ""
            try:
                reply = data["choices"][0]["message"]["content"]
            except Exception:
                pass
            return JSONResponse(content={
                "ok": True, "latency_ms": latency_ms, "reply": reply,
                "status": resp.status_code,
            })
        else:
            return JSONResponse(content={
                "ok": False, "latency_ms": latency_ms,
                "status": resp.status_code, "error": resp.text[:300],
            })
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000)
        return JSONResponse(content={"ok": False, "latency_ms": latency_ms, "error": str(exc)})


@router.get("/templates", summary="Modal deployment templates and setup guide")
async def get_templates() -> JSONResponse:
    """
    Return ready-to-use Modal deployment templates and setup instructions.
    """
    return JSONResponse(content={
        "info": "Deploy any of these templates on Modal to get an OpenAI-compatible LLM endpoint. Free $30/month credits.",
        "setup_steps": [
            "pip install modal",
            "modal setup   # authenticates via browser",
            "modal token new   # creates ~/.modal/config.toml with token_id + token_secret",
            "modal deploy my_llm.py   # deploys your app, prints the endpoint URL",
            "Register the URL + token in Settings → Modal Endpoints",
        ],
        "token_format": "ak-<token_id>:<token_secret>  (from ~/.modal/config.toml)",
        "key_format_in_arbiter": "https://myorg--myapp.modal.run|ak-abc123:xyz456",
        "templates": [
            {
                "name": "vLLM — Llama 3.1 8B (recommended, fast)",
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "gpu": "A10G ($0.0006/s)",
                "monthly_estimate": "~50K requests on $30 free credits",
                "modal_example_url": "https://modal.com/docs/examples/vllm_inference",
                "code": '''import subprocess
import modal

app = modal.App("llm-serve")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("vllm>=0.8.0", "huggingface_hub[hf_transfer]", "hf_transfer")
    .env({"HF_HOME": "/model-cache", "HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

model_vol = modal.Volume.from_name("arbiter-model-cache", create_if_missing=True)

@app.function(
    gpu="A10G",
    image=image,
    volumes={"/model-cache": model_vol},
    scaledown_window=300,
    timeout=600,
)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=8000, startup_timeout=600)
def serve():
    """vLLM serves OpenAI-compatible API on port 8000."""
    subprocess.Popen([
        "vllm", "serve", "meta-llama/Llama-3.1-8B-Instruct",
        "--host", "0.0.0.0", "--port", "8000",
        "--max-model-len", "32768",
        "--gpu-memory-utilization", "0.90",
        "--trust-remote-code",
        "--served-model-name", "meta-llama/Llama-3.1-8B-Instruct",
    ])
''',
            },
            {
                "name": "vLLM — Llama 3.3 70B (high quality)",
                "model": "meta-llama/Llama-3.3-70B-Instruct",
                "gpu": "A100-80GB ($0.0019/s)",
                "monthly_estimate": "~15K requests on $30 free credits",
                "modal_example_url": "https://modal.com/docs/examples/vllm_inference",
            },
            {
                "name": "vLLM — Qwen2.5 72B",
                "model": "Qwen/Qwen2.5-72B-Instruct",
                "gpu": "A100-80GB ($0.0019/s)",
                "modal_example_url": "https://modal.com/docs/examples/vllm_inference",
            },
            {
                "name": "vLLM — DeepSeek R1 32B (reasoning)",
                "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
                "gpu": "A10G × 2 ($0.0012/s)",
                "modal_example_url": "https://modal.com/docs/examples/vllm_inference",
            },
            {
                "name": "llama.cpp — Mistral 7B (very cheap)",
                "model": "mistralai/Mistral-7B-Instruct-v0.3",
                "gpu": "T4 ($0.0002/s)",
                "monthly_estimate": "~150K requests on $30 free credits",
                "modal_example_url": "https://modal.com/docs/examples/llm-serving",
            },
        ],
        "gpu_pricing": {
            "T4":       "$0.000164/s  (~$0.59/hr)",
            "L4":       "$0.000222/s  (~$0.80/hr)",
            "A10G":     "$0.000306/s  (~$1.10/hr)",
            "L40S":     "$0.000542/s  (~$1.95/hr)",
            "A100-40G": "$0.000583/s  (~$2.10/hr)",
            "A100-80G": "$0.000694/s  (~$2.50/hr)",
            "H100":     "$0.001097/s  (~$3.95/hr)",
            "note": "Modal is serverless — you only pay while a request is running, not idle time",
        },
        "links": {
            "signup":      "https://modal.com",
            "tokens":      "https://modal.com/settings/tokens",
            "examples":    "https://modal.com/docs/examples",
            "gpu_pricing": "https://modal.com/pricing",
        },
    })
