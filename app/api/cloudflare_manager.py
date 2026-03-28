"""
Cloudflare Workers AI management endpoints.

Routes
──────
GET  /cloudflare/models          List available Workers AI models
POST /cloudflare/workers         Create a new AI Worker
GET  /cloudflare/workers         List existing workers
DELETE /cloudflare/workers/{name} Delete a worker

All endpoints use the first configured Cloudflare key
(settings.get_keys("cloudflare")[0]) which must be in  account_id|api_token  format.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cloudflare", tags=["Cloudflare Workers AI"])

_CF_API = "https://api.cloudflare.com/client/v4"

# ---------------------------------------------------------------------------
# Worker script template — {MODEL_ID} is replaced at creation time
# ---------------------------------------------------------------------------
_WORKER_SCRIPT_TEMPLATE = """\
export default {
  async fetch(request, env) {
    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }
    const body = await request.json();
    const messages = body.messages || [];
    const response = await env.AI.run('{MODEL_ID}', { messages });
    return Response.json({
      id: 'chatcmpl-' + Date.now(),
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: '{MODEL_ID}',
      choices: [{
        index: 0,
        message: { role: 'assistant', content: response.response || '' },
        finish_reason: 'stop'
      }],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
    });
  }
}
"""


class CreateWorkerRequest(BaseModel):
    name: str
    model: str = "@cf/meta/llama-3.1-8b-instruct"
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_credentials() -> tuple[str, str]:
    """Return (account_id, api_token) from the first configured Cloudflare key."""
    keys = settings.get_keys("cloudflare")
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No Cloudflare API keys configured (CLOUDFLARE_API_KEYS)",
        )
    raw = keys[0]
    parts = raw.split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CLOUDFLARE_API_KEYS must be in 'account_id|api_token' format",
        )
    return parts[0].strip(), parts[1].strip()


def _headers(api_token: str) -> dict:
    return {"Authorization": f"Bearer {api_token}"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/models", summary="List Workers AI models (Text Generation)")
async def list_cf_models() -> JSONResponse:
    """
    Fetch available Cloudflare Workers AI text-generation models from the
    Cloudflare API.
    """
    account_id, api_token = _get_credentials()
    url = (
        f"{_CF_API}/accounts/{account_id}/ai/models/search"
        "?task=Text+Generation"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, headers=_headers(api_token))
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Cloudflare API unreachable: {exc}",
            )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Cloudflare API error: {resp.text[:500]}",
        )

    data = resp.json()
    return JSONResponse(content=data)


@router.post("/workers", summary="Create a new Cloudflare AI Worker", status_code=201)
async def create_worker(body: CreateWorkerRequest) -> JSONResponse:
    """
    Create a new Cloudflare Worker that exposes an OpenAI-compatible /chat endpoint
    backed by the specified Workers AI model.

    Returns the worker URL:  https://{name}.{subdomain}.workers.dev
    """
    account_id, api_token = _get_credentials()

    script_name = body.name.lower().replace(" ", "-")
    model_id    = body.model
    script_code = _WORKER_SCRIPT_TEMPLATE.replace("{MODEL_ID}", model_id)

    metadata = {
        "main_module": "index.js",
        "bindings": [
            {
                "type": "ai",
                "name": "AI",
            }
        ],
        "compatibility_date": "2024-01-01",
    }

    url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}"

    # Cloudflare requires multipart/form-data for ES module workers
    import io
    files = {
        "index.js": ("index.js", io.BytesIO(script_code.encode()), "application/javascript+module"),
        "metadata": ("metadata.json", io.BytesIO(json.dumps(metadata).encode()), "application/json"),
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.put(url, headers=_headers(api_token), files=files)
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Cloudflare API unreachable: {exc}",
            )

    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Cloudflare Worker creation failed: {resp.text[:500]}",
        )

    cf_data = resp.json()
    subdomain = _get_account_subdomain(cf_data)
    worker_url = f"https://{script_name}.{subdomain}.workers.dev" if subdomain else None

    return JSONResponse(
        status_code=201,
        content={
            "success":    True,
            "name":       script_name,
            "model":      model_id,
            "worker_url": worker_url,
            "cloudflare": cf_data,
        },
    )


@router.get("/workers", summary="List Cloudflare Workers")
async def list_workers() -> JSONResponse:
    """Return all worker scripts in the configured Cloudflare account."""
    account_id, api_token = _get_credentials()
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, headers=_headers(api_token))
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Cloudflare API unreachable: {exc}",
            )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Cloudflare API error: {resp.text[:500]}",
        )

    return JSONResponse(content=resp.json())


@router.delete("/workers/{script_name}", summary="Delete a Cloudflare Worker")
async def delete_worker(script_name: str) -> JSONResponse:
    """Delete the specified worker script from the Cloudflare account."""
    account_id, api_token = _get_credentials()
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.delete(url, headers=_headers(api_token))
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Cloudflare API unreachable: {exc}",
            )

    if resp.status_code not in (200, 204):
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Cloudflare Worker deletion failed: {resp.text[:500]}",
        )

    return JSONResponse(content={"success": True, "deleted": script_name})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_account_subdomain(cf_response: dict) -> Optional[str]:
    """
    Attempt to extract the workers.dev subdomain from the Cloudflare API response.
    Falls back to None; the caller handles the None case gracefully.
    """
    try:
        result = cf_response.get("result", {})
        # The subdomain is not always in the script PUT response;
        # it's available under the account details endpoint but we do a best-effort
        # extraction here.
        return result.get("subdomain") or result.get("default_environment", {}).get("deployment", {}).get("workers_dev_url")
    except Exception:
        return None
