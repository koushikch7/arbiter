"""
Custom providers — user-configurable OpenAI-compatible endpoints.

Each custom provider is instantiated as a ``GenericOpenAIProvider`` at
startup (from the state store) and whenever the user adds/edits one via
the UI. Keys are stored in ``.env`` (consistent with built-in providers)
under a mangled env var name ``CUSTOM_PROVIDER_{NAME}_KEY``.

Routes
------
GET    /api/custom-providers              list configured custom providers
GET    /api/custom-providers/templates    list preset templates
POST   /api/custom-providers              add a custom provider
PATCH  /api/custom-providers/{name}       update key / models / label
DELETE /api/custom-providers/{name}       remove a custom provider
POST   /api/custom-providers/{name}/test  test connectivity
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.users_api import require_admin
from app.key_management.key_pool import KeyPool, PROVIDER_LIMITS
from app.providers._templates import CUSTOM_PROVIDER_TEMPLATES, get_template
from app.providers.generic_openai import GenericOpenAIProvider
from app.state_store import (
    list_custom_providers,
    get_custom_provider,
    upsert_custom_provider,
    delete_custom_provider as _delete_custom_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/custom-providers", tags=["Custom Providers"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")

# Reserve the namespace used by built-in providers
_RESERVED_NAMES = {
    "gemini", "groq", "openrouter", "cohere", "cloudflare", "cerebras",
    "huggingface", "pollinations", "zai", "routeway", "ollama", "nvidia",
}


# ---------------------------------------------------------------------------
# .env helpers for custom provider keys
# ---------------------------------------------------------------------------

def _env_var_for(name: str) -> str:
    return f"CUSTOM_PROVIDER_{name.upper().replace('-', '_')}_KEY"


def _read_custom_key(name: str) -> str:
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return ""
    env_var = _env_var_for(name)
    content = env_file.read_text(encoding="utf-8")
    m = re.search(rf"^{env_var}=(.*)$", content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _write_custom_key(name: str, key: str) -> None:
    env_file = _PROJECT_ROOT / ".env"
    env_var  = _env_var_for(name)
    content  = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    new_line = f"{env_var}={key}"
    if re.search(rf"^{env_var}=", content, re.MULTILINE):
        content = re.sub(rf"^{env_var}=.*$", new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(content, encoding="utf-8")


def _delete_custom_key(name: str) -> None:
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    env_var = _env_var_for(name)
    content = env_file.read_text(encoding="utf-8")
    content = re.sub(rf"^{env_var}=.*\n?", "", content, flags=re.MULTILINE)
    env_file.write_text(content, encoding="utf-8")


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "****"
    return key[:6] + "..." + key[-4:]


# ---------------------------------------------------------------------------
# Provider instantiation
# ---------------------------------------------------------------------------

def _make_instance(config: dict) -> GenericOpenAIProvider:
    return GenericOpenAIProvider(
        name=config["name"],
        label=config.get("label", config["name"]),
        base_url=config["base_url"],
        auth_scheme=config.get("auth_scheme", "bearer"),
        auth_header=config.get("auth_header", "Authorization"),
        auth_prefix=config.get("auth_prefix", "Bearer "),
        extra_headers=config.get("extra_headers", {}),
        models=config.get("models", []),
        max_context=config.get("max_context", 131_072),
        supports_discovery=config.get("supports_discovery", True),
    )


def _register_in_app(request: Request, config: dict) -> None:
    """Instantiate the provider + key pool and attach to app.state."""
    providers = request.app.state.providers
    key_pools = request.app.state.key_pools
    redis     = request.app.state.redis
    name      = config["name"]

    key = _read_custom_key(name)
    if not key:
        # Remove if key missing
        providers.pop(name, None)
        key_pools.pop(name, None)
        return

    providers[name] = _make_instance(config)

    limits = PROVIDER_LIMITS.get("custom", {"rpm": 60, "tpm": 500_000, "daily": 10_000})
    key_pools[name] = KeyPool(
        provider=name,
        keys=[key],
        redis_client=redis,
        rpm_limit=limits["rpm"],
        tpm_limit=limits["tpm"],
        daily_limit=limits["daily"],
    )


async def load_custom_providers_to_app(app) -> None:
    """Startup helper — instantiate all stored custom providers."""
    providers = app.state.providers
    key_pools = app.state.key_pools
    redis     = app.state.redis

    limits = PROVIDER_LIMITS.get("custom", {"rpm": 60, "tpm": 500_000, "daily": 10_000})
    for config in list_custom_providers():
        name = config.get("name")
        if not name:
            continue
        key = _read_custom_key(name)
        if not key:
            logger.warning(
                "Custom provider %s has no API key in .env; skipping", name
            )
            continue
        try:
            providers[name] = _make_instance(config)
            key_pools[name] = KeyPool(
                provider=name,
                keys=[key],
                redis_client=redis,
                rpm_limit=limits["rpm"],
                tpm_limit=limits["tpm"],
                daily_limit=limits["daily"],
            )
            logger.info("Loaded custom provider %s", name)
        except Exception as exc:
            logger.exception("Failed to load custom provider %s: %s", name, exc)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateProviderBody(BaseModel):
    name: str = Field(..., min_length=2, max_length=32)
    label: str = Field("", max_length=64)
    template: str = "custom"
    base_url: str = ""
    api_key: str
    models: list[str] = []

    auth_scheme: str | None = None
    auth_header: str | None = None
    auth_prefix: str | None = None
    extra_headers: dict[str, str] | None = None
    max_context: int | None = None


class UpdateProviderBody(BaseModel):
    label: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    base_url: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/templates", summary="List preset templates for custom providers")
async def list_templates() -> JSONResponse:
    return JSONResponse(CUSTOM_PROVIDER_TEMPLATES)


@router.post("/probe", summary="Probe an endpoint without persisting it")
async def probe_provider(body: CreateProviderBody,
                         _admin: dict = Depends(require_admin)) -> JSONResponse:
    """Ad-hoc connectivity check used by the 'Test Connection' UI button.

    Runs the same SSRF guard as the create path, instantiates a transient
    provider, and tries fetch_models(). Returns quickly. Does not persist
    the config or store the API key.
    """
    base_url = (body.base_url or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url must start with http:// or https://")
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"):
        raise HTTPException(422, "base_url cannot point to localhost / metadata IPs")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise HTTPException(422, "base_url cannot point to a private / loopback IP")
    except ValueError:
        pass
    config = {
        "name":        body.name,
        "label":       body.label or body.name,
        "base_url":    base_url,
        "auth_scheme": body.auth_scheme,
        "auth_header": body.auth_header,
        "auth_prefix": body.auth_prefix,
        "extra_headers": body.extra_headers or {},
        "models":      body.models or [],
    }
    provider = _make_instance(config)
    started = time.perf_counter()
    try:
        models = await provider.fetch_models(body.api_key)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return JSONResponse({
            "ok":            True,
            "latency_ms":    elapsed,
            "models_found":  len(models),
            "sample":        [m.get("id") for m in models[:5]],
        })
    except NotImplementedError:
        return JSONResponse({"ok": True, "latency_ms": 0, "models_found": 0,
                             "note": "Provider does not expose a /models endpoint; skipped."})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)


@router.get("", summary="List configured custom providers")
async def list_providers(request: Request) -> JSONResponse:
    providers_state = request.app.state.providers
    out = []
    for c in list_custom_providers():
        key = _read_custom_key(c["name"])
        out.append({
            **c,
            "configured":    c["name"] in providers_state,
            "api_key_masked": _mask(key),
            "has_key":        bool(key),
        })
    return JSONResponse(out)


@router.post("", summary="Add a custom provider", status_code=201)
async def create_provider(body: CreateProviderBody, request: Request,
                          _admin: dict = Depends(require_admin)) -> JSONResponse:
    name = body.name.strip().lower()
    if not _NAME_PATTERN.match(name):
        raise HTTPException(
            422,
            "name must be lowercase alphanumeric with optional _ or - (2\u201332 chars)",
        )
    if name in _RESERVED_NAMES:
        raise HTTPException(409, f"{name!r} is reserved for built-in providers")
    if get_custom_provider(name) is not None:
        raise HTTPException(409, f"custom provider {name!r} already exists")

    tpl = get_template(body.template)
    if tpl is None:
        raise HTTPException(422, f"unknown template: {body.template!r}")

    base_url = (body.base_url or tpl["base_url"]).strip()
    if not base_url:
        raise HTTPException(422, "base_url is required")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url must start with http:// or https://")
    # SECURITY: block internal addresses to prevent SSRF / credential-stealing
    # against local services. This is a basic guard — not exhaustive.
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"):
        raise HTTPException(422, "base_url cannot point to localhost / metadata IPs")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise HTTPException(422, "base_url cannot point to a private / loopback IP")
    except ValueError:
        pass  # hostname, not IP — OK

    models = body.models or list(tpl.get("default_models", []))

    config: dict[str, Any] = {
        "name":              name,
        "label":             body.label or tpl["label"],
        "template":          body.template,
        "base_url":          base_url,
        "auth_scheme":       body.auth_scheme or tpl["auth_scheme"],
        "auth_header":       body.auth_header or tpl["auth_header"],
        "auth_prefix":       body.auth_prefix if body.auth_prefix is not None else tpl["auth_prefix"],
        "extra_headers":     body.extra_headers or dict(tpl.get("extra_headers", {})),
        "models":            models,
        "max_context":       body.max_context or tpl["max_context"],
        "supports_discovery": tpl.get("supports_discovery", True),
        "signup_url":        tpl.get("signup_url", ""),
        "created_at":        time.time(),
    }

    # Persist key first so _register_in_app can pick it up
    _write_custom_key(name, body.api_key.strip())
    upsert_custom_provider(config)
    _register_in_app(request, config)

    logger.info("Added custom provider %s (template=%s)", name, body.template)
    return JSONResponse(
        {**config, "api_key_masked": _mask(body.api_key), "configured": True},
        status_code=201,
    )


@router.patch("/{name}", summary="Update a custom provider")
async def update_provider(
    name: str, body: UpdateProviderBody, request: Request,
    _admin: dict = Depends(require_admin),
) -> JSONResponse:
    config = get_custom_provider(name)
    if config is None:
        raise HTTPException(404, f"custom provider {name!r} not found")

    changed = False
    if body.label is not None and body.label != config.get("label"):
        config["label"] = body.label
        changed = True
    if body.models is not None and body.models != config.get("models"):
        config["models"] = body.models
        changed = True
    if body.base_url is not None and body.base_url != config.get("base_url"):
        if not body.base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "base_url must start with http:// or https://")
        config["base_url"] = body.base_url
        changed = True

    if changed:
        upsert_custom_provider(config)

    if body.api_key is not None and body.api_key.strip():
        _write_custom_key(name, body.api_key.strip())

    _register_in_app(request, config)

    return JSONResponse({
        **config,
        "api_key_masked": _mask(_read_custom_key(name)),
        "configured":     name in request.app.state.providers,
    })


@router.delete("/{name}", summary="Delete a custom provider")
async def remove_provider(name: str, request: Request,
                          _admin: dict = Depends(require_admin)) -> JSONResponse:
    if get_custom_provider(name) is None:
        raise HTTPException(404, f"custom provider {name!r} not found")

    _delete_custom_provider(name)
    _delete_custom_key(name)
    request.app.state.providers.pop(name, None)
    request.app.state.key_pools.pop(name, None)

    logger.info("Deleted custom provider %s", name)
    return JSONResponse({"deleted": name})


@router.post("/{name}/test", summary="Test connectivity of a custom provider")
async def test_provider(name: str, request: Request,
                        _admin: dict = Depends(require_admin)) -> JSONResponse:
    config = get_custom_provider(name)
    if config is None:
        raise HTTPException(404, f"custom provider {name!r} not found")
    key = _read_custom_key(name)
    if not key:
        raise HTTPException(400, f"custom provider {name!r} has no API key")

    provider = _make_instance(config)

    # Minimal probe: try fetch_models (lightweight); fall back to chat
    # completion with 1 token.
    started = time.perf_counter()
    try:
        if config.get("supports_discovery", True):
            models = await provider.fetch_models(key)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            return JSONResponse({
                "ok":         True,
                "latency_ms": elapsed,
                "method":     "fetch_models",
                "models":     len(models),
            })
    except NotImplementedError:
        pass
    except Exception as exc:
        logger.debug("test_provider fetch_models failed: %s", exc)

    # Chat completion probe
    from app.models.schemas import ChatCompletionRequest, Message as _Msg
    probe = ChatCompletionRequest(
        model=config.get("models", [""])[0] or "",
        messages=[_Msg(role="user", content="ping")],
        temperature=0.0,
        max_tokens=1,
    )
    started = time.perf_counter()
    try:
        resp = await provider.complete(probe, key)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return JSONResponse({
            "ok":         True,
            "latency_ms": elapsed,
            "method":     "chat",
            "reply":      (resp.choices[0].message.content or "")[:100],
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:300]}, status_code=200)
