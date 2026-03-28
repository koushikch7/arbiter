import logging
import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "static",
)


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "CDN-Cache-Control": "no-store",
}


@router.get("/api-docs", response_class=HTMLResponse, summary="Interactive API documentation")
async def api_docs() -> HTMLResponse:
    """Serve the interactive API docs page."""
    path = os.path.join(_STATIC_DIR, "api-docs.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200, headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>API docs page not found</h1>", status_code=404)


@router.get("/dashboard", response_class=HTMLResponse, summary="Web dashboard")
async def dashboard(request: Request) -> HTMLResponse:
    """Serve the HTML dashboard."""
    static_path = os.path.join(_STATIC_DIR, "dashboard.html")
    try:
        with open(static_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200, headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Dashboard not found</h1>", status_code=404
        )


@router.get("/images", response_class=HTMLResponse, summary="Redirect to image generation settings")
async def images_redirect() -> HTMLResponse:
    """Redirect /images to the settings page image gen tab."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/settings#images")


@router.get("/settings", response_class=HTMLResponse, summary="Settings dashboard")
async def settings_page() -> HTMLResponse:
    """Serve the settings management page."""
    path = os.path.join(_STATIC_DIR, "settings.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200, headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Settings page not found</h1>", status_code=404)


@router.get("/playground", response_class=HTMLResponse, summary="Chat playground")
async def playground_page() -> HTMLResponse:
    """Serve the interactive chat playground."""
    path = os.path.join(_STATIC_DIR, "playground.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Playground page not found</h1>", status_code=404)


@router.get("/dashboard/stats", summary="Dashboard stats JSON")
async def dashboard_stats(request: Request) -> JSONResponse:
    """Return JSON stats for the dashboard."""
    redis = request.app.state.redis
    key_pools = request.app.state.key_pools
    cache = request.app.state.cache

    # Gather global counters from Redis
    async def get_stat(name: str) -> int:
        try:
            val = await redis.get(f"arbiter:stats:{name}")
            return int(val) if val else 0
        except Exception:
            return 0

    requests_total = await get_stat("requests_total")
    requests_success = await get_stat("requests_success")
    requests_failed = await get_stat("requests_failed")
    cache_hits = await get_stat("cache_hits")
    cache_misses = await get_stat("cache_misses")

    total_cache_lookups = cache_hits + cache_misses
    cache_hit_rate = (
        round(cache_hits / total_cache_lookups * 100, 1)
        if total_cache_lookups > 0
        else 0.0
    )

    overall_success_rate = (
        round(requests_success / requests_total * 100, 1)
        if requests_total > 0
        else 0.0
    )

    # Per-provider stats
    provider_stats = []
    for provider_name, pool in key_pools.items():
        pool_stats = await pool.get_stats()
        p_success = await get_stat(f"provider:{provider_name}:success")
        p_errors = await get_stat(f"provider:{provider_name}:errors")
        p_rate_limited = await get_stat(f"provider:{provider_name}:rate_limited")
        p_total = p_success + p_errors + p_rate_limited

        provider = request.app.state.providers.get(provider_name)
        provider_stats.append(
            {
                "name": provider_name,
                "models": provider.models if provider else [],
                "total_keys": pool_stats["total_keys"],
                "active_keys": pool_stats["active_keys"],
                "keys": pool_stats["keys"],
                "requests": {
                    "total": p_total,
                    "success": p_success,
                    "errors": p_errors,
                    "rate_limited": p_rate_limited,
                },
                "success_rate": (
                    round(p_success / p_total * 100, 1) if p_total > 0 else 0.0
                ),
                "status": _provider_health(pool_stats),
            }
        )

    cache_stats = await cache.get_stats()

    return JSONResponse(
        {
            "status": "online",
            "requests": {
                "total": requests_total,
                "success": requests_success,
                "failed": requests_failed,
                "success_rate": overall_success_rate,
            },
            "cache": {
                "hits": cache_hits,
                "misses": cache_misses,
                "hit_rate": cache_hit_rate,
                "cached_responses": cache_stats.get("cached_responses", 0),
            },
            "providers": provider_stats,
        }
    )


def _provider_health(pool_stats: dict) -> str:
    """Determine health status label for a provider based on key pool stats."""
    total = pool_stats["total_keys"]
    active = pool_stats["active_keys"]
    if total == 0:
        return "unavailable"
    ratio = active / total
    if ratio > 0.5:
        return "healthy"
    if ratio > 0:
        return "degraded"
    return "unavailable"
