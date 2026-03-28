"""
Cloudflare Workers AI management endpoints.

Routes
──────
GET    /cloudflare/models                   List available Workers AI text-gen models
GET    /cloudflare/workers                  List workers (live from CF + Redis metadata)
POST   /cloudflare/workers                  Create + enable workers.dev + register in gateway
DELETE /cloudflare/workers/{name}           Delete worker from CF + remove from registry
GET    /cloudflare/workers/{name}/analytics Worker stats (local counters + CF metadata)

Credentials: first Cloudflare key from env OR from runtime Redis store.
Key format:  account_id|api_token
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cloudflare", tags=["Cloudflare Workers AI"])

_CF_API        = "https://api.cloudflare.com/client/v4"
_REDIS_WORKERS = "arbiter:cf:workers"          # JSON dict  name → {url,model,created_on,...}


# ---------------------------------------------------------------------------
# Worker script template
# ---------------------------------------------------------------------------

_WORKER_SCRIPT_TEMPLATE = """\
export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
      });
    }
    if (request.method !== 'POST') {
      return new Response(JSON.stringify({error:'Method not allowed'}),
        {status:405, headers:{'Content-Type':'application/json'}});
    }
    const body = await request.json();
    const messages  = body.messages  || [];
    const maxTokens = body.max_tokens || 512;
    const temp      = body.temperature !== undefined ? body.temperature : 0.7;
    const response  = await env.AI.run('{MODEL_ID}', {
      messages,
      max_tokens: maxTokens,
    });
    return Response.json({
      id:      'chatcmpl-' + Date.now(),
      object:  'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model:   '{MODEL_ID}',
      choices: [{
        index:         0,
        message:       { role: 'assistant', content: response.response || '' },
        finish_reason: 'stop',
      }],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    }, { headers: { 'Access-Control-Allow-Origin': '*' } });
  }
}
"""


class CreateWorkerRequest(BaseModel):
    name:        str
    model:       str = "@cf/meta/llama-3.1-8b-instruct"
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_credentials(request: Request) -> tuple[str, str]:
    """Return (account_id, api_token) from env var OR runtime Redis keys."""
    from app.api.keys_api import _merged_keys
    redis = request.app.state.redis
    keys  = await _merged_keys(redis, "cloudflare")
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No Cloudflare API keys configured. Add one in Settings → API Keys.",
        )
    raw   = keys[0]
    parts = raw.split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cloudflare key must be in 'account_id|api_token' format",
        )
    return parts[0].strip(), parts[1].strip()


def _hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _cf_get(url: str, token: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
    return r


async def _load_worker_registry(redis) -> dict:
    raw = await redis.get(_REDIS_WORKERS)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


async def _save_worker_registry(redis, registry: dict) -> None:
    await redis.set(_REDIS_WORKERS, json.dumps(registry))


async def _fetch_account_subdomain(account_id: str, token: str) -> Optional[str]:
    """Fetch the workers.dev subdomain for this account."""
    url = f"{_CF_API}/accounts/{account_id}/workers/subdomain"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            data = r.json()
            return (data.get("result") or {}).get("subdomain")
    except Exception:
        pass
    return None


async def _enable_workers_dev(account_id: str, script_name: str, token: str) -> bool:
    """Enable workers.dev subdomain access for a worker script."""
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}/subdomain"
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"enabled": True},
            )
        logger.info("Enable workers.dev for %s: HTTP %s", script_name, r.status_code)
        return r.status_code in (200, 201)
    except Exception as exc:
        logger.warning("Could not enable workers.dev for %s: %s", script_name, exc)
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/models", summary="List Workers AI text-generation models")
async def list_cf_models(request: Request) -> JSONResponse:
    """Fetch available Cloudflare Workers AI text-generation models."""
    account_id, api_token = await _get_credentials(request)
    url = f"{_CF_API}/accounts/{account_id}/ai/models/search?task=Text+Generation"

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            resp = await c.get(url, headers={"Authorization": f"Bearer {api_token}"})
        except httpx.RequestError as exc:
            raise HTTPException(502, f"Cloudflare API unreachable: {exc}")

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Cloudflare API error: {resp.text[:400]}")

    data    = resp.json()
    results = data.get("result", [])

    # Normalise: return list of {name, description, task}
    models = [
        {
            "name":        m.get("name", ""),
            "description": m.get("description", ""),
            "task":        (m.get("task") or {}).get("name", "Text Generation"),
        }
        for m in results
        if m.get("name")
    ]
    return JSONResponse(content={"success": True, "result": models, "count": len(models)})


@router.get("/workers", summary="List Cloudflare Workers (live + registry)")
async def list_workers(request: Request) -> JSONResponse:
    """
    Return all worker scripts from Cloudflare + merge with local registry metadata
    (model, URL, integration status).
    """
    account_id, api_token = await _get_credentials(request)
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts"

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            resp = await c.get(url, headers={"Authorization": f"Bearer {api_token}"},
                               params={"per_page": 100})
        except httpx.RequestError as exc:
            raise HTTPException(502, f"Cloudflare API unreachable: {exc}")

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Cloudflare API error: {resp.text[:400]}")

    cf_data  = resp.json()
    cf_list  = cf_data.get("result", [])
    registry = await _load_worker_registry(request.app.state.redis)

    # Merge CF live data with local registry metadata
    merged = []
    for w in cf_list:
        name = w.get("id") or w.get("script_name") or w.get("name", "")
        meta = registry.get(name, {})
        merged.append({
            "name":        name,
            "created_on":  w.get("created_on", ""),
            "modified_on": w.get("modified_on", ""),
            "model":       meta.get("model", ""),
            "worker_url":  meta.get("url", ""),
            "subdomain_enabled": bool(meta.get("url")),
            "description": meta.get("description", ""),
            "requests_total": meta.get("requests_total", 0),
        })

    # Workers in registry but not in CF (deleted externally — clean up)
    cf_names = {w.get("id") or w.get("script_name") or w.get("name", "") for w in cf_list}
    stale = [n for n in registry if n not in cf_names]
    if stale:
        for s in stale:
            del registry[s]
        await _save_worker_registry(request.app.state.redis, registry)
        logger.info("Cleaned up stale workers from registry: %s", stale)

    return JSONResponse(content={"success": True, "result": merged, "count": len(merged)})


@router.post("/workers", summary="Create + integrate a Cloudflare AI Worker", status_code=201)
async def create_worker(body: CreateWorkerRequest, request: Request) -> JSONResponse:
    """
    Create a Cloudflare Worker backed by Workers AI, enable its workers.dev
    subdomain, and register it in the Arbiter gateway.

    After creation, the worker is usable via Arbiter's /v1/chat/completions
    endpoint using provider=cloudflare with the specified model.
    """
    account_id, api_token = await _get_credentials(request)

    script_name = body.name.lower().replace(" ", "-").replace("_", "-")
    model_id    = body.model
    script_code = _WORKER_SCRIPT_TEMPLATE.replace("{MODEL_ID}", model_id)

    metadata = {
        "main_module": "index.js",
        "bindings": [{"type": "ai", "name": "AI"}],
        "compatibility_date": "2024-09-01",
        "compatibility_flags": ["nodejs_compat"],
    }

    import io
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}"
    files = {
        "index.js": ("index.js",
                     io.BytesIO(script_code.encode()),
                     "application/javascript+module"),
        "metadata": ("metadata.json",
                     io.BytesIO(json.dumps(metadata).encode()),
                     "application/json"),
    }

    async with httpx.AsyncClient(timeout=60.0) as c:
        try:
            resp = await c.put(url,
                               headers={"Authorization": f"Bearer {api_token}"},
                               files=files)
        except httpx.RequestError as exc:
            raise HTTPException(502, f"Cloudflare API unreachable: {exc}")

    if resp.status_code not in (200, 201):
        raise HTTPException(resp.status_code,
                            f"Worker creation failed: {resp.text[:500]}")

    # Enable workers.dev subdomain
    subdomain_ok = await _enable_workers_dev(account_id, script_name, api_token)

    # Fetch account subdomain to build the URL
    subdomain  = await _fetch_account_subdomain(account_id, api_token)
    worker_url = (f"https://{script_name}.{subdomain}.workers.dev"
                  if subdomain else None)

    # Persist to registry
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)
    registry[script_name] = {
        "model":        model_id,
        "url":          worker_url or "",
        "description":  body.description or "",
        "created_on":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requests_total": 0,
    }
    await _save_worker_registry(redis, registry)

    # Hot-reload Cloudflare provider so the model is in the active list
    # (model is already in CloudflareProvider.models list but this keeps pools in sync)
    try:
        from app.api.keys_api import _reload_provider
        await _reload_provider("cloudflare", request)
    except Exception as exc:
        logger.warning("Could not reload cloudflare provider: %s", exc)

    return JSONResponse(
        status_code=201,
        content={
            "success":          True,
            "name":             script_name,
            "model":            model_id,
            "worker_url":       worker_url,
            "subdomain_enabled": subdomain_ok,
            "gateway_info": {
                "message": (
                    f"Use via Arbiter: POST /v1/chat/completions "
                    f"with provider=cloudflare and model={model_id}"
                ),
                "provider": "cloudflare",
                "model":    model_id,
            },
        },
    )


@router.delete("/workers/{script_name}", summary="Delete a Cloudflare Worker")
async def delete_worker(script_name: str, request: Request) -> JSONResponse:
    """Delete the worker from Cloudflare and remove it from the local registry."""
    account_id, api_token = await _get_credentials(request)
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}"

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            resp = await c.delete(url, headers={"Authorization": f"Bearer {api_token}"})
        except httpx.RequestError as exc:
            raise HTTPException(502, f"Cloudflare API unreachable: {exc}")

    if resp.status_code not in (200, 204):
        raise HTTPException(resp.status_code,
                            f"Worker deletion failed: {resp.text[:300]}")

    # Remove from registry
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)
    registry.pop(script_name, None)
    await _save_worker_registry(redis, registry)

    return JSONResponse(content={"success": True, "deleted": script_name})


@router.get("/workers/{script_name}/analytics",
            summary="Worker analytics (local counters + metadata)")
async def worker_analytics(script_name: str, request: Request) -> JSONResponse:
    """
    Return usage stats for a worker: local gateway request counter + CF metadata.
    """
    account_id, api_token = await _get_credentials(request)
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)
    meta     = registry.get(script_name)

    if meta is None:
        raise HTTPException(404, f"Worker '{script_name}' not found in registry")

    # Try to fetch live worker details from CF
    cf_info = {}
    try:
        url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}"
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {api_token}"})
        if r.status_code == 200:
            cf_data = r.json()
            cf_info = cf_data.get("result", {})
    except Exception:
        pass

    # Count requests tracked by our gateway (cloudflare provider RPM/daily counters)
    import hashlib
    from app.api.keys_api import _merged_keys
    cf_keys     = await _merged_keys(redis, "cloudflare")
    first_key   = cf_keys[0] if cf_keys else ""
    h           = hashlib.md5(first_key.encode()).hexdigest()[:10] if first_key else ""
    daily_used  = int(await redis.get(f"cloudflare:{h}:daily") or 0)
    rpm_used    = int(await redis.get(f"cloudflare:{h}:rpm")   or 0)

    return JSONResponse(content={
        "name":          script_name,
        "model":         meta.get("model", ""),
        "worker_url":    meta.get("url", ""),
        "description":   meta.get("description", ""),
        "created_on":    meta.get("created_on", ""),
        "requests_total": meta.get("requests_total", 0),
        "cf_metadata":   {
            "id":          cf_info.get("id", script_name),
            "modified_on": cf_info.get("modified_on", ""),
            "etag":        cf_info.get("etag", ""),
        },
        "gateway_usage": {
            "cloudflare_daily_tokens": daily_used,
            "cloudflare_rpm":          rpm_used,
            "note": "Counts all Cloudflare AI requests through this gateway today",
        },
        "dashboard_url": (
            f"https://dash.cloudflare.com/{account_id}/workers/services/view/{script_name}"
        ),
    })
