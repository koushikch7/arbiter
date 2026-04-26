"""
Cloudflare Workers AI management endpoints.

Routes
──────
GET    /cloudflare/models                   List available Workers AI text-gen models
GET    /cloudflare/workers                  List workers (live from CF + Redis metadata)
POST   /cloudflare/workers                  Create + enable workers.dev + register in gateway
DELETE /cloudflare/workers/{name}           Delete worker from CF + remove from registry
GET    /cloudflare/workers/{name}/analytics Worker stats (local counters + CF metadata)
POST   /cloudflare/validate                 Validate token permissions (AI, Scripts, Subdomain)

Credentials: first Cloudflare key from env OR from runtime Redis store.
Key format:  account_id|api_token

Required token permissions (use "Edit Cloudflare Workers" template):
  - Workers AI > Execute              (for AI inference)
  - Workers Scripts > Edit            (for create / delete worker scripts)
  - Workers Routes > Edit             (for enabling workers.dev subdomain)
  - Account > Workers Scripts Read    (for listing scripts — included in above)
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

router = APIRouter(prefix="/cloudflare", tags=["Cloudflare Workers AI"], dependencies=[Depends(require_admin)])

_CF_API        = "https://api.cloudflare.com/client/v4"
_REDIS_WORKERS = "arbiter:cf:workers"          # JSON dict  name → {url,model,created_on,...}
_REDIS_DELETING_PFX = "arbiter:cf:deleting:"   # name → "1"  (TTL=120s)

# Grace period: newly created workers won't be cleaned up for this many seconds.
# CF API propagation can take up to ~30 seconds.
_PROVISION_GRACE_SECS = 120


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


class ValidateKeyBody(BaseModel):
    key: Optional[str] = None  # If omitted, uses the first configured key


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


def _parse_raw_key(raw: str) -> tuple[str, str]:
    parts = raw.split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Key must be in 'account_id|api_token' format")
    return parts[0].strip(), parts[1].strip()


def _hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


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


def _registry_age_secs(meta: dict) -> float:
    """Return how many seconds ago this registry entry was created. Returns inf if unknown."""
    created = meta.get("created_on", "")
    if not created:
        return float("inf")
    try:
        import email.utils
        # Handle both ISO 8601 (our format) and CF's RFC2822 format
        if "T" in created:
            import datetime
            t = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
        ts = email.utils.parsedate_to_datetime(created).timestamp()
        return time.time() - ts
    except Exception:
        return float("inf")


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
    """
    Fetch available Cloudflare Workers AI text-generation models.

    Requires token permission: **Workers AI > Execute**
    """
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
    Return all worker scripts from Cloudflare API merged with local registry metadata.

    Newly created workers that haven't propagated to the CF API yet are included
    with status='provisioning' from the local registry.

    Workers deleted externally (not in CF API, older than grace period) are cleaned
    from the registry automatically.

    Requires token permission: **Workers Scripts > Read** (included in Edit template)
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
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)

    cf_names = {w.get("id") or w.get("script_name") or w.get("name", "") for w in cf_list}
    # Collect names that are currently being deleted (CF propagation delay)
    deleting = set()
    for name_candidate in list(cf_names):
        if await redis.get(f"{_REDIS_DELETING_PFX}{name_candidate}"):
            deleting.add(name_candidate)
    cf_names -= deleting

    # Merge CF live data with local registry metadata
    merged = []
    for w in cf_list:
        name = w.get("id") or w.get("script_name") or w.get("name", "")
        if name in deleting:
            continue
        meta = registry.get(name, {})
        merged.append({
            "name":              name,
            "status":            "active",
            "created_on":        w.get("created_on", ""),
            "modified_on":       w.get("modified_on", ""),
            "model":             meta.get("model", ""),
            "worker_url":        meta.get("url", ""),
            "subdomain_enabled": bool(meta.get("url")),
            "description":       meta.get("description", ""),
            "requests_total":    meta.get("requests_total", 0),
        })
        # If found in CF, mark registry entry as active (remove provisioning status)
        if name in registry and registry[name].get("status") == "provisioning":
            registry[name]["status"] = "active"

    # Include provisioning workers from registry not yet visible in CF API
    registry_modified = False
    for name, meta in list(registry.items()):
        if name in cf_names:
            continue  # already included above
        age = _registry_age_secs(meta)
        if age < _PROVISION_GRACE_SECS:
            # Still within grace period — show as provisioning
            merged.append({
                "name":              name,
                "status":            "provisioning",
                "created_on":        meta.get("created_on", ""),
                "modified_on":       "",
                "model":             meta.get("model", ""),
                "worker_url":        meta.get("url", ""),
                "subdomain_enabled": bool(meta.get("url")),
                "description":       meta.get("description", ""),
                "requests_total":    meta.get("requests_total", 0),
            })
        else:
            # Older than grace period and not in CF — truly stale, clean up
            del registry[name]
            registry_modified = True
            logger.info("Cleaned up stale worker from registry: %s (age=%.0fs)", name, age)

    if registry_modified:
        await _save_worker_registry(redis, registry)

    # Sort by created_on descending (newest first)
    merged.sort(key=lambda w: w.get("created_on", ""), reverse=True)

    return JSONResponse(content={"success": True, "result": merged, "count": len(merged)})


@router.post("/workers", summary="Create + integrate a Cloudflare AI Worker", status_code=201)
async def create_worker(body: CreateWorkerRequest, request: Request) -> JSONResponse:
    """
    Create a Cloudflare Worker backed by Workers AI, enable its workers.dev
    subdomain, and register it in the Arbiter gateway.

    After creation, the worker is immediately visible in the list as 'provisioning'
    and transitions to 'active' once the CF API propagates it (typically < 30s).

    Requires token permissions:
    - **Workers AI > Execute** (for AI binding)
    - **Workers Scripts > Edit** (for script upload)
    - **Workers Routes > Edit** (for enabling workers.dev)
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
        detail = resp.text[:500]
        # Try to extract useful error from CF JSON response
        try:
            err = resp.json()
            msgs = " | ".join(e.get("message", "") for e in err.get("errors", []))
            if msgs:
                detail = msgs
        except Exception:
            pass
        raise HTTPException(resp.status_code, f"Worker creation failed: {detail}")

    # Enable workers.dev subdomain
    subdomain_ok = await _enable_workers_dev(account_id, script_name, api_token)

    # Fetch account subdomain to build the URL
    subdomain  = await _fetch_account_subdomain(account_id, api_token)
    worker_url = (f"https://{script_name}.{subdomain}.workers.dev"
                  if subdomain else None)

    # Persist to registry immediately so it appears in the list during CF propagation
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)
    registry[script_name] = {
        "model":          model_id,
        "url":            worker_url or "",
        "description":    body.description or "",
        "created_on":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requests_total": 0,
        "status":         "provisioning",   # set to active once CF API confirms it
    }
    await _save_worker_registry(redis, registry)

    # Hot-reload Cloudflare provider so the model is in the active list
    try:
        from app.api.keys_api import _reload_provider
        await _reload_provider("cloudflare", request)
    except Exception as exc:
        logger.warning("Could not reload cloudflare provider: %s", exc)

    return JSONResponse(
        status_code=201,
        content={
            "success":           True,
            "name":              script_name,
            "model":             model_id,
            "worker_url":        worker_url,
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
    """
    Delete the worker script from Cloudflare and remove it from the local registry.

    Requires token permission: **Workers Scripts > Edit**

    If the Cloudflare deletion fails (e.g. 403 Forbidden), the registry entry is
    NOT removed and the error is returned to the caller.
    """
    account_id, api_token = await _get_credentials(request)
    url = f"{_CF_API}/accounts/{account_id}/workers/scripts/{script_name}"

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            resp = await c.delete(url, headers={"Authorization": f"Bearer {api_token}"})
        except httpx.RequestError as exc:
            raise HTTPException(502, f"Cloudflare API unreachable: {exc}")

    if resp.status_code not in (200, 204):
        # Extract CF error message
        detail = resp.text[:300]
        try:
            err = resp.json()
            msgs = " | ".join(e.get("message", "") for e in err.get("errors", []))
            if msgs:
                detail = msgs
            if resp.status_code == 403:
                detail = f"Permission denied: {msgs or 'token needs Workers Scripts > Edit permission'}"
            elif resp.status_code == 404:
                detail = f"Worker '{script_name}' not found in Cloudflare (may have been deleted already)"
        except Exception:
            pass
        raise HTTPException(resp.status_code, detail)

    # Remove from registry
    redis    = request.app.state.redis
    registry = await _load_worker_registry(redis)
    registry.pop(script_name, None)
    await _save_worker_registry(redis, registry)
    # Mark as deleting so list_workers skips it during CF propagation delay (up to 2 min)
    await redis.set(f"{_REDIS_DELETING_PFX}{script_name}", "1", ex=120)

    return JSONResponse(content={"success": True, "deleted": script_name})


@router.post("/validate", summary="Validate Cloudflare API token permissions")
async def validate_cf_key(body: ValidateKeyBody, request: Request) -> JSONResponse:
    """
    Check which Cloudflare permissions the configured (or provided) API token has.

    Tests performed:
    - **Workers Scripts list** — `GET /accounts/{id}/workers/scripts`
      Requires: Workers Scripts > Read (included in Edit template)
    - **Workers AI models** — `GET /accounts/{id}/ai/models/search`
      Requires: Workers AI > Execute
    - **Workers Subdomain** — `GET /accounts/{id}/workers/subdomain`
      Requires: Workers Routes > Read (included in Edit template)

    Script write access (create/delete) is implied if scripts list succeeds and
    you used the "Edit Cloudflare Workers" token template. It cannot be verified
    without performing a write operation.

    Returns a permission matrix with status codes and descriptions.
    """
    # Resolve key: use provided key or fall back to configured key
    if body.key:
        try:
            account_id, api_token = _parse_raw_key(body.key)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        account_id, api_token = await _get_credentials(request)

    checks: list[dict] = []

    async with httpx.AsyncClient(timeout=15.0) as c:

        # 1. Workers Scripts list
        try:
            r = await c.get(
                f"{_CF_API}/accounts/{account_id}/workers/scripts",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"per_page": 1},
            )
            ok = r.status_code == 200
            checks.append({
                "name":        "Workers Scripts Read",
                "permission":  "Workers Scripts > Edit (or Read)",
                "ok":          ok,
                "http_status": r.status_code,
                "note":        "Required to list and manage worker scripts" if ok
                               else _cf_perm_hint(r),
                "required_for": ["List workers", "Create worker", "Delete worker"],
            })
        except Exception as exc:
            checks.append({"name": "Workers Scripts Read", "ok": False,
                            "http_status": 0, "note": str(exc),
                            "required_for": ["List workers", "Create worker", "Delete worker"]})

        # 2. Workers AI (inference)
        try:
            r = await c.get(
                f"{_CF_API}/accounts/{account_id}/ai/models/search",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"task": "Text Generation", "per_page": 1},
            )
            ok = r.status_code == 200
            checks.append({
                "name":        "Workers AI Execute",
                "permission":  "Workers AI > Execute",
                "ok":          ok,
                "http_status": r.status_code,
                "note":        "AI inference is available" if ok else _cf_perm_hint(r),
                "required_for": ["AI inference in worker", "List AI models"],
            })
        except Exception as exc:
            checks.append({"name": "Workers AI Execute", "ok": False,
                            "http_status": 0, "note": str(exc),
                            "required_for": ["AI inference in worker", "List AI models"]})

        # 3. Workers Subdomain (workers.dev routing)
        try:
            r = await c.get(
                f"{_CF_API}/accounts/{account_id}/workers/subdomain",
                headers={"Authorization": f"Bearer {api_token}"},
            )
            ok = r.status_code == 200
            subdomain = None
            if ok:
                try:
                    subdomain = (r.json().get("result") or {}).get("subdomain")
                except Exception:
                    pass
            checks.append({
                "name":        "Workers Subdomain",
                "permission":  "Workers Routes > Edit",
                "ok":          ok,
                "http_status": r.status_code,
                "subdomain":   subdomain,
                "note":        (f"workers.dev subdomain: {subdomain}" if subdomain
                                else "Subdomain access OK" if ok
                                else _cf_perm_hint(r)),
                "required_for": ["Enable workers.dev routing for created workers"],
            })
        except Exception as exc:
            checks.append({"name": "Workers Subdomain", "ok": False,
                            "http_status": 0, "note": str(exc),
                            "required_for": ["Enable workers.dev routing"]})

    all_ok = all(c["ok"] for c in checks)

    return JSONResponse(content={
        "success":     True,
        "account_id":  account_id,
        "all_ok":      all_ok,
        "checks":      checks,
        "recommendation": (
            "All required permissions confirmed. This token can create, delete, and run Workers AI workers."
            if all_ok else
            "Some permissions are missing. Use the 'Edit Cloudflare Workers' token template at "
            "https://dash.cloudflare.com/profile/api-tokens to create a token with all required permissions."
        ),
    })


def _cf_perm_hint(resp: httpx.Response) -> str:
    """Return a helpful message based on the CF API error response."""
    if resp.status_code == 403:
        try:
            msgs = " | ".join(e.get("message", "") for e in resp.json().get("errors", []))
            return f"Permission denied — {msgs or 'token is missing required permission'}"
        except Exception:
            return "Permission denied — token is missing required permission"
    if resp.status_code == 401:
        return "Authentication failed — check that the API token is correct"
    if resp.status_code == 404:
        return "Account not found — check that the Account ID is correct"
    try:
        msgs = " | ".join(e.get("message", "") for e in resp.json().get("errors", []))
        return msgs or f"HTTP {resp.status_code}"
    except Exception:
        return f"HTTP {resp.status_code}"


@router.get("/workers/{script_name}/analytics",
            summary="Worker analytics (local counters + metadata)")
async def worker_analytics(script_name: str, request: Request) -> JSONResponse:
    """
    Return usage stats for a worker: local gateway request counter + CF metadata.

    Requires token permission: **Workers Scripts > Read**
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
    cf_keys    = await _merged_keys(redis, "cloudflare")
    first_key  = cf_keys[0] if cf_keys else ""
    h          = hashlib.md5(first_key.encode()).hexdigest()[:10] if first_key else ""
    daily_used = int(await redis.get(f"cloudflare:{h}:daily") or 0)
    rpm_used   = int(await redis.get(f"cloudflare:{h}:rpm")   or 0)

    return JSONResponse(content={
        "name":           script_name,
        "model":          meta.get("model", ""),
        "worker_url":     meta.get("url", ""),
        "description":    meta.get("description", ""),
        "created_on":     meta.get("created_on", ""),
        "status":         meta.get("status", "active"),
        "requests_total": meta.get("requests_total", 0),
        "cf_metadata":    {
            "id":          cf_info.get("id", script_name),
            "modified_on": cf_info.get("modified_on", ""),
            "etag":        cf_info.get("etag", ""),
        },
        "gateway_usage":  {
            "cloudflare_daily_tokens": daily_used,
            "cloudflare_rpm":          rpm_used,
            "note": "Counts all Cloudflare AI requests through this gateway today",
        },
        "dashboard_url":  (
            f"https://dash.cloudflare.com/{account_id}/workers/services/view/{script_name}"
        ),
    })
