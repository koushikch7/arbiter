import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.key_management.key_pool import KeyPool, PROVIDER_LIMITS
from app.providers.gemini import GeminiProvider
from app.providers.groq_provider import GroqProvider
from app.providers.openrouter import OpenRouterProvider
from app.providers.cohere_provider import CohereProvider
from app.providers.cloudflare import CloudflareProvider
from app.providers.cerebras import CerebrasProvider
from app.providers.huggingface import HuggingFaceProvider
from app.providers.pollinations import PollinationsProvider
from app.providers.zai_provider import ZaiProvider
from app.providers.routeway import RoutewayProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.nvidia_provider import NvidiaProvider
from app.routing.router import IntelligentRouter
from app.cache.cache import CacheLayer
from app.api import chat, models_api, dashboard, ui_errors_api
from app.api.cloudflare_manager import router as cloudflare_router
from app.api.settings_api import router as settings_router
from app.api.preferences_api import router as preferences_router
from app.api.keys_api import router as keys_router
from app.api.image_api import router as image_router
from app.api.logs_api import router as logs_router, log_buffer
from app.api.gateway_tokens_api import router as gateway_tokens_router
from app.api.gateway_tokens_api import load_gateway_tokens_to_state
from app.api.custom_providers_api import router as custom_providers_router
from app.api.custom_providers_api import load_custom_providers_to_app
from app.api.users_api import router as users_router
from app.api.analytics_api import router as analytics_router
from app.api.backup_api import router as backup_router
from app.api.announcements_api import router as announcements_router
from app.api.persistent_logs_api import router as persistent_logs_router
from app.auth.sso import router as auth_router, register_google_oauth, sso_enabled
from app.middleware.auth import (
    GatewayAuthMiddleware,
    CloudflareAccessMiddleware,
    SecurityHeadersMiddleware,
    BearerRedactFilter,
)
from app.middleware.bot_protection import BotProtectionMiddleware
from starlette.middleware.sessions import SessionMiddleware

# Single source of truth for the app version — update here only.
APP_VERSION = "1.20.3"

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Attach in-memory log buffer to root logger so all log records are captured
logging.getLogger().addHandler(log_buffer)
# Scrub Bearer tokens / obvious API keys from every log record
logging.getLogger().addFilter(BearerRedactFilter())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down shared resources."""
    logger.info("Starting Arbiter...")

    # Initialize Redis
    try:
        redis_client = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
        await redis_client.ping()
        logger.info(f"Connected to Redis at {settings.REDIS_URL}")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}. Using in-memory fallback.")
        redis_client = _InMemoryRedis()

    app.state.redis = redis_client

    # Reset the in-flight request gauge on startup. It is a live counter
    # (incr on request start, decr in a finally on completion), but the
    # Redis store is persistent (appendonly), so any increments orphaned by
    # a container restart / crash / killed worker survive forever and the
    # gauge drifts upward without bound (observed: 1.2K "in-flight" with no
    # real concurrent load). At startup there are genuinely zero requests in
    # flight, so resetting to 0 is correct and self-heals accumulated drift.
    try:
        await redis_client.set("arbiter:stats:inflight", 0)
        logger.info("Reset in-flight request gauge to 0 on startup")
    except Exception as e:
        logger.warning(f"Could not reset in-flight gauge: {e}")

    # Initialize providers
    providers = {}
    provider_classes = {
        "nvidia":       NvidiaProvider,     # prioritized — powerful free-tier models, 40 RPM, 1000 RPD
        "gemini":       GeminiProvider,
        "groq":         GroqProvider,
        "openrouter":   OpenRouterProvider,
        "cohere":       CohereProvider,
        "cloudflare":   CloudflareProvider,
        "cerebras":     CerebrasProvider,
        "huggingface":  HuggingFaceProvider,
        "pollinations": PollinationsProvider,
        "zai":          ZaiProvider,
        "routeway":     RoutewayProvider,
        "ollama":       OllamaProvider,
    }

    for name, cls in provider_classes.items():
        keys = settings.get_keys(name)

        # Pollinations is free/anonymous — inject a dummy key so the pool works
        if name == "pollinations" and not keys:
            keys = ["free"]

        if keys:
            providers[name] = cls()
            logger.info(f"Initialized provider: {name} with {len(keys)} key(s)")
        else:
            logger.warning(f"No API keys for provider {name}, skipping")

    app.state.providers = providers

    # Initialize key pools
    key_pools = {}
    for name, provider in providers.items():
        keys = settings.get_keys(name)
        # Restore dummy key for Pollinations if needed
        if name == "pollinations" and not keys:
            keys = ["free"]
        limits = PROVIDER_LIMITS.get(name, {"rpm": 20, "tpm": 100_000, "daily": 1000})
        key_pools[name] = KeyPool(
            provider=name,
            keys=keys,
            redis_client=redis_client,
            rpm_limit=limits["rpm"],
            tpm_limit=limits["tpm"],
            daily_limit=limits["daily"],
            key_tiers=settings.get_key_tiers(name),
        )

    app.state.key_pools = key_pools

    # Initialize cache
    cache = CacheLayer(redis_client=redis_client, default_ttl=settings.CACHE_TTL)
    app.state.cache = cache

    # Initialize router
    intelligent_router = IntelligentRouter(
        providers=providers,
        key_pools=key_pools,
        cache=cache,
        redis_client=redis_client,
    )
    app.state.router = intelligent_router

    # Load gateway tokens from Redis into app state (for dynamic auth)
    try:
        await load_gateway_tokens_to_state(app)
    except Exception as _e:
        logger.warning(f"Could not load gateway tokens: {_e}")
        app.state.gateway_tokens = set()

    # Load user-added custom providers from state store
    try:
        await load_custom_providers_to_app(app)
    except Exception as _e:
        logger.warning(f"Could not load custom providers: {_e}")

    logger.info(
        f"Arbiter ready. Providers: {list(providers.keys())}, "
        f"Cache TTL: {settings.CACHE_TTL}s"
    )

    # Weekly background task: refresh provider key pools and stamp the
    # last-sync timestamp in Redis so the UI can display "Synced N days ago".
    # NOTE: this re-reads keys from .env, re-initialises pools, and re-runs the
    # active disabled-flag filter. It does NOT scrape upstream docs for new
    # model IDs \u2014 model catalogues live in app/providers/_free_tier_catalog.py
    # and need a code change. The weekly job logs a reminder for that.
    sync_task = asyncio.create_task(
        _weekly_provider_sync(app, redis_client),
        name="weekly-provider-sync",
    )
    app.state.sync_task = sync_task

    # Daily incremental / weekly full backup scheduler
    backup_task = asyncio.create_task(
        _backup_scheduler(app, redis_client),
        name="backup-scheduler",
    )
    app.state.backup_task = backup_task

    # Daily analytics email report scheduler
    from app.services.daily_report import start_scheduler as start_report_scheduler
    start_report_scheduler(app)

    # Weekly model health check scheduler (Mondays 17:00 UTC / 22:30 IST)
    from app.services.model_health import start_scheduler as start_health_scheduler
    start_health_scheduler(app)

    # Persistent log janitor (180-day file retention)
    from app.observability.persistent_log import start_janitor as start_log_janitor
    start_log_janitor()

    yield

    # Cleanup
    logger.info("Shutting down Arbiter...")
    sync_task.cancel()
    backup_task.cancel()
    from app.services.daily_report import stop_scheduler as stop_report_scheduler
    stop_report_scheduler()
    from app.services.model_health import stop_scheduler as stop_health_scheduler
    stop_health_scheduler()
    try:
        from app.observability.persistent_log import stop_janitor as stop_log_janitor
        stop_log_janitor()
    except Exception:
        pass
    try:
        from app.providers.generic_openai import aclose_http_client
        await aclose_http_client()
    except Exception:
        pass
    try:
        await sync_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await backup_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await redis_client.aclose()
    except Exception:
        pass


async def _weekly_provider_sync(app: FastAPI, redis_client) -> None:
    """Reload providers from .env every 7 days; stamp Redis with last-sync time.

    Runs in the background for the lifetime of the app. Cancelled on shutdown.
    """
    from app.api.keys_api import _PROVIDER_META, _reload_provider

    SYNC_INTERVAL = 7 * 24 * 3600  # one week

    # _reload_provider expects a Request-like object that exposes `.app`.
    # We use a minimal named wrapper so an AttributeError surfaces clearly
    # rather than silently failing if the function signature changes.
    class _AppRequest:
        """Minimal request stand-in that provides only request.app."""
        __slots__ = ("app",)
        def __init__(self, app_instance): self.app = app_instance
    app_request = _AppRequest(app)

    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL)
            logger.info("Weekly provider sync starting...")
            reloaded, failed = [], []
            for pname in _PROVIDER_META:
                try:
                    await _reload_provider(pname, app_request)
                    reloaded.append(pname)
                except Exception as exc:
                    logger.warning(f"Weekly sync: failed to reload {pname}: {exc}")
                    failed.append(pname)
            ts = int(time.time())
            await redis_client.set("arbiter:provider_sync:last", ts)
            await redis_client.set("arbiter:provider_sync:reloaded", ",".join(reloaded))
            await redis_client.set("arbiter:provider_sync:failed", ",".join(failed))
            logger.info(
                f"Weekly provider sync done. Reloaded: {len(reloaded)} | "
                f"Failed: {len(failed)} | reminder: review provider catalogs against "
                f"official docs (app/providers/_free_tier_catalog.py)"
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Weekly provider sync errored: {exc}")


async def _backup_scheduler(app: FastAPI, redis_client) -> None:
    """Scheduled backup driver.

    Daily incremental at 02:00 UTC.  Weekly full on Sunday at 01:00 UTC.
    Runs in the background for the lifetime of the app.

    Guards:
      - Minimum sleep of 60s prevents tight-loop if time calc goes wrong.
      - Redis "last ran" timestamp prevents duplicate runs within 23 hours.
    """
    from datetime import datetime, timedelta, timezone as _tz
    from app.api.backup_api import run_backup
    from app.config import settings as _cfg

    _MIN_SLEEP = 60          # never sleep less than 60s
    _MIN_INTERVAL = 82800    # 23 hours in seconds — dedup guard

    while True:
        try:
            now = datetime.now(_tz.utc)
            # Sleep until the next 02:00 UTC (or 01:00 on Sunday)
            if now.weekday() == 6 and now.hour < 1:
                target = now.replace(hour=1, minute=0, second=0, microsecond=0)
            else:
                target = now.replace(hour=2, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                # If next target is a Sunday 02:00, bump to 01:00
                if target.weekday() == 6:
                    target = target.replace(hour=1)
            sleep_secs = max((target - now).total_seconds(), _MIN_SLEEP)
            logger.info(
                "Backup scheduler: next run in %.0f minutes (%s)",
                sleep_secs / 60, target.isoformat(),
            )
            await asyncio.sleep(sleep_secs)

            if not _cfg.BACKUP_ENABLED or not _cfg.BACKUP_S3_ENDPOINT:
                continue

            # ── Dedup guard: skip if we already ran within 23 hours ───────────
            now = datetime.now(_tz.utc)
            btype = "full" if now.weekday() == 6 else "incremental"
            last_ts_raw = await redis_client.get(f"arbiter:backup:last_{btype}_ts")
            if last_ts_raw:
                try:
                    last_dt = datetime.fromisoformat(last_ts_raw)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=_tz.utc)
                    elapsed = (now - last_dt).total_seconds()
                    if elapsed < _MIN_INTERVAL:
                        logger.info(
                            "Backup scheduler: skipping %s — last ran %.0fh ago (< 23h)",
                            btype, elapsed / 3600,
                        )
                        continue
                except (ValueError, TypeError):
                    pass  # corrupted timestamp — proceed with backup

            logger.info("Backup scheduler: starting %s backup…", btype)
            try:
                result = await run_backup(redis_client, backup_type=btype)
                logger.info(
                    "Backup scheduler: %s backup done — size=%.1fKB",
                    btype, result.get("size_bytes", 0) / 1024,
                )
            except Exception as exc:
                logger.error("Backup scheduler: backup failed: %s", exc)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Backup scheduler error: %s", exc)
            await asyncio.sleep(3600)  # back off 1 hour on unexpected error


_STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(
    title="Arbiter",
    description=(
        "**Arbiter** — Intelligent LLM Router & Gateway (v1.18.x)\n\n"
        "Single OpenAI-compatible endpoint aggregating 12+ providers "
        "(NVIDIA NIM, Gemini, Groq, Cerebras, Cloudflare Workers AI, OpenRouter, Cohere, "
        "HuggingFace, Pollinations, Z.ai, Routeway, Ollama Cloud) plus "
        "user-added custom OpenAI-compatible providers.\n\n"
        "### Authentication\n"
        "All `/v1/*` endpoints require a Bearer token: "
        "`Authorization: Bearer <gateway-token>`. "
        "Admin endpoints (`/api/*`, `/settings/*`, `/cloudflare/*`) require a "
        "Google-SSO admin session or a Bearer token with admin privileges.\n\n"
        "### New in v1.18\n"
        "- **Persistent 180-day logs** — `GET /api/logs/persistent/*`\n"
        "- **Activity audit log** — HMAC-tagged admin mutation records\n"
        "- **Dashboard banners** — `POST /api/announcements`\n"
        "- **Per-token rate limiting** — 429 with Retry-After headers\n"
        "- **Adaptive routing** — unhealthy providers auto-demoted; TPM-aware key scoring\n"
        "- **Gemini 3.1 Flash Lite GA** — `gemini-3.1-flash-lite` (preview discontinued May 25 2026)\n\n"
        "### Rate limits\n"
        "Default: 100 requests/min per gateway token. Configurable per-token via "
        "`PATCH /api/gateway/tokens/{id}` (`request_limit_per_minute` field).\n\n"
        "Full docs: [/developer](/developer) · Swagger: [/docs](/docs) · ReDoc: [/redoc](/redoc)"
    ),
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={
        "persistAuthorization": True,
        "defaultModelsExpandDepth": -1,
        "displayRequestDuration": True,
        "tryItOutEnabled": True,
        "filter": True,
    },
)

# Serve /static/ files (CSS, JS shared across UI pages)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static_files")

# ── SSO bootstrap (must happen before middleware stack is finalized) ────────
register_google_oauth()
_SSO_ON = sso_enabled()

if _SSO_ON and not settings.SESSION_SECRET_KEY:
    logger.error(
        "GOOGLE_OAUTH_CLIENT_ID is set but SESSION_SECRET_KEY is empty \u2014 "
        "refusing to enable SSO. Set SESSION_SECRET_KEY to a 32+ char random string."
    )
    _SSO_ON = False

# ── Security headers — moved to after Session so they become outermost ────
# Middleware stack in request-processing order (Starlette LIFO — last added = outermost):
#   SecurityHeadersMiddleware (outermost — headers on ALL responses inc. 401/403)
#   BotProtectionMiddleware   (2nd — blocks bad bots before any processing)
#   SessionMiddleware         (3rd — populates scope["session"] before GatewayAuth)
#   GatewayAuthMiddleware     (4th — auth check reads session)
#   CORSMiddleware            (innermost)


# ── Request body size limit — 4 MB max (guards against memory-bomb DoS) ─────
# FastAPI/Starlette has no built-in limit; we enforce it in a thin middleware
# before any body parsing occurs.  4 MB is generous for chat payloads.
_MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MB


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": {"message": "Request body too large (max 4 MB)",
                               "type": "invalid_request_error", "code": 413}},
        )
    return await call_next(request)

# ── CORS middleware ─────────────────────────────────────────────────────────
_cors_origins = [
    o.strip() for o in (settings.ALLOWED_CORS_ORIGINS or "").split(",") if o.strip()
]
if not _cors_origins:
    # Same-origin only — no CORS headers emitted. Safe default.
    _cors_origins = []
    logger.info("CORS: same-origin only (ALLOWED_CORS_ORIGINS not set)")
elif "*" in _cors_origins and _SSO_ON:
    # Wildcard with credentials is an anti-pattern; degrade to same-origin
    logger.warning(
        "CORS: ALLOWED_CORS_ORIGINS='*' with SSO enabled is insecure \u2014 "
        "falling back to same-origin only. Specify explicit origins."
    )
    _cors_origins = []

if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )
    logger.info("CORS: enabled for %d origin(s)", len(_cors_origins))

# ── Cloudflare Access JWT middleware (optional) ─────────────────────────────
if settings.ENABLE_CF_ACCESS and settings.CLOUDFLARE_ACCESS_TEAM_NAME:
    app.add_middleware(
        CloudflareAccessMiddleware,
        team_name=settings.CLOUDFLARE_ACCESS_TEAM_NAME,
        aud=settings.CLOUDFLARE_ACCESS_AUD,
    )
    logger.info(
        f"CloudflareAccess middleware enabled "
        f"(team={settings.CLOUDFLARE_ACCESS_TEAM_NAME})"
    )

# ── Gateway auth middleware (dual-mode Bearer + session) ────────────────────
# NOTE: must be added BEFORE SessionMiddleware so that Session ends up
# OUTERMOST in the Starlette stack (Starlette wraps the latest-added middleware
# as the outermost layer). Otherwise GatewayAuth runs before Session has
# populated request.scope["session"] → AssertionError on first UI request.
gateway_keys = settings.get_gateway_api_keys()
app.add_middleware(
    GatewayAuthMiddleware,
    allowed_keys=gateway_keys,
    sso_enabled=_SSO_ON,
    require_auth=settings.REQUIRE_AUTH,
)
if _SSO_ON:
    logger.info(
        "GatewayAuthMiddleware: SSO mode (UI \u2192 Google session, /v1/* \u2192 Bearer)"
    )
elif gateway_keys:
    logger.info(
        "GatewayAuthMiddleware: Bearer-only mode with %d key(s)", len(gateway_keys)
    )
else:
    if settings.REQUIRE_AUTH:
        logger.warning(
            "GatewayAuthMiddleware: STRICT mode \u2014 no GATEWAY_API_KEYS / SSO / dynamic "
            "tokens configured. /v1/* will refuse all requests with 401 until you "
            "create a token at /settings \u2192 Gateway Keys."
        )
    else:
        logger.warning(
            "GatewayAuthMiddleware: auth DISABLED \u2014 no GATEWAY_API_KEYS and no SSO. "
            "Anyone with network access can use this gateway. Set REQUIRE_AUTH=true."
        )

# ── Session middleware (Google SSO) — MUST be added AFTER GatewayAuth ───────
# so Session wraps it as the outermost layer and populates scope["session"]
# before GatewayAuth reads it.
if _SSO_ON:
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SESSION_SECRET_KEY,
        session_cookie="arbiter_session",
        max_age=settings.SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.SESSION_COOKIE_SECURE,
    )
    logger.info(
        "SessionMiddleware enabled (secure=%s, max_age=%ds)",
        settings.SESSION_COOKIE_SECURE, settings.SESSION_MAX_AGE,
    )

# ── Security headers + Bot protection (outermost — added last) ───────────────
# These MUST be added after Session/GatewayAuth so they are truly outermost
# and apply their headers/checks to ALL responses — including 401/403s from
# inner middleware layers (auth rejects, CF Access blocks, etc.).
app.add_middleware(BotProtectionMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


# ── Request timing middleware ───────────────────────────────────────────────
@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Response-Time-Ms"] = str(duration_ms)
    return response


# ── Include routers ─────────────────────────────────────────────────────────
app.include_router(chat.router, tags=["Chat"])
app.include_router(models_api.router, tags=["Models"])
app.include_router(dashboard.router, tags=["Dashboard"])
app.include_router(cloudflare_router)
app.include_router(settings_router)
app.include_router(preferences_router)
app.include_router(keys_router)
app.include_router(image_router)
app.include_router(logs_router)
app.include_router(gateway_tokens_router)
app.include_router(custom_providers_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(analytics_router)
app.include_router(ui_errors_api.router)
app.include_router(backup_router)
app.include_router(announcements_router)
app.include_router(persistent_logs_router)


@app.get("/health", summary="Health check")
async def health(request: Request):
    """Return gateway health status."""
    redis_ok = False
    try:
        await request.app.state.redis.ping()
        redis_ok = True
    except Exception:
        pass

    providers = list(request.app.state.providers.keys())
    return JSONResponse(
        {
            "status": "ok" if redis_ok else "degraded",
            "redis": "connected" if redis_ok else "disconnected",
            "providers": providers,
            "version": APP_VERSION,
            "sso_enabled": _SSO_ON,
        }
    )


@app.get("/login", include_in_schema=False)
async def login_page():
    """Serve the SSO login page.

    Served with no-cache headers so the SSO-disabled warning never gets
    stuck in a browser / CDN cache when the admin flips SSO on.  Also
    rewrites the page's SSO flag server-side, eliminating the need for
    the client to call /auth/config just to decide which banner to show.
    """
    from fastapi.responses import HTMLResponse, FileResponse
    login_html = _STATIC_DIR / "login.html"
    if not login_html.exists():
        return RedirectResponse("/dashboard")
    try:
        html = login_html.read_text(encoding="utf-8")
    except OSError:
        return FileResponse(str(login_html))
    # Inject an authoritative SSO flag that the inline script will honour
    # FIRST, before any /auth/config fetch (defence-in-depth against a
    # future field-name drift).
    flag = "true" if _SSO_ON else "false"
    html = html.replace(
        "<head>",
        f"<head>\n  <script>window.__ARBITER_SSO_ENABLED = {flag};</script>",
        1,
    )
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":        "no-cache",
            "Expires":       "0",
        },
    )


@app.get("/users", include_in_schema=False)
async def users_page():
    """Serve the admin Users management page (access controlled by auth middleware)."""
    from fastapi.responses import FileResponse
    users_html = _STATIC_DIR / "users.html"
    if users_html.exists():
        return FileResponse(str(users_html))
    return RedirectResponse("/dashboard")


@app.get("/developer", include_in_schema=False)
async def developer_page():
    """Serve the Developer Documentation page."""
    from fastapi.responses import FileResponse
    dev_html = _STATIC_DIR / "developer.html"
    if dev_html.exists():
        return FileResponse(str(dev_html))
    return RedirectResponse("/docs")


@app.get("/api-docs", include_in_schema=False)
async def api_docs_redirect():
    """Legacy redirect — api-docs is now Developer Docs."""
    return RedirectResponse("/developer")


@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to dashboard."""
    return RedirectResponse(url="/dashboard")


# ---------------------------------------------------------------------------
# PWA — service worker, manifest, favicon must live at root scope
# ---------------------------------------------------------------------------

@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Serve the service worker from root scope so it can control all of /.

    Browsers also require the SW response itself to NOT be cached at the edge,
    otherwise rolled-out updates would never reach users — see the
    SecurityHeadersMiddleware which sets `no-store` for /sw.js explicitly.
    """
    from fastapi.responses import FileResponse
    sw = _STATIC_DIR / "sw.js"
    if not sw.exists():
        return JSONResponse({"error": "sw.js missing"}, status_code=404)
    return FileResponse(
        str(sw),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
@app.get("/manifest.json", include_in_schema=False)
async def webmanifest():
    """Serve the PWA manifest from root scope."""
    from fastapi.responses import FileResponse
    mf = _STATIC_DIR / "manifest.webmanifest"
    if not mf.exists():
        return JSONResponse({"error": "manifest missing"}, status_code=404)
    return FileResponse(str(mf), media_type="application/manifest+json")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon from root for legacy browsers."""
    from fastapi.responses import FileResponse
    fav = _STATIC_DIR / "favicon.ico"
    if not fav.exists():
        return JSONResponse({"error": "favicon missing"}, status_code=404)
    return FileResponse(str(fav), media_type="image/x-icon")


# ---------------------------------------------------------------------------
# SEO — robots.txt + sitemap.xml
# Both are public (no auth).  robots.txt disallows all crawlers from
# authenticated pages; sitemap lists only the public /login entry.
# ---------------------------------------------------------------------------

@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """Serve robots.txt — instructs crawlers to avoid the admin dashboard."""
    from fastapi.responses import PlainTextResponse
    from app.config import settings as _cfg
    base = (_cfg.APP_BASE_URL or "").rstrip("/")
    sitemap_line = f"Sitemap: {base}/sitemap.xml" if base else ""
    content = f"""# Arbiter LLM Gateway — robots.txt
# Only the sign-in page is publicly accessible.
# All other routes require authentication and must not be indexed.

User-agent: *
Disallow: /dashboard
Disallow: /analytics
Disallow: /settings
Disallow: /logs
Disallow: /images
Disallow: /playground
Disallow: /backup
Disallow: /users
Disallow: /developer
Disallow: /v1/
Disallow: /api/
Disallow: /docs
Disallow: /redoc
Disallow: /openapi.json
Allow: /login
Allow: /static/
Allow: /sw.js
Allow: /manifest.webmanifest
Allow: /health

# Block AI training crawlers explicitly
User-agent: GPTBot
Disallow: /

User-agent: ChatGPT-User
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: Claude-Web
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: AhrefsBot
Disallow: /

User-agent: SemrushBot
Disallow: /

User-agent: DotBot
Disallow: /

User-agent: MJ12bot
Disallow: /
{chr(10) + sitemap_line if sitemap_line else ''}
""".strip()
    return PlainTextResponse(content, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    """Serve a minimal sitemap listing only the public login page."""
    from fastapi.responses import Response as _R
    from app.config import settings as _cfg
    from datetime import date
    base = (_cfg.APP_BASE_URL or "").rstrip("/")
    if not base:
        # Can't build a useful sitemap without a base URL
        return _R(content="", status_code=204)
    today = date.today().isoformat()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base}/login</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>"""
    return _R(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# Minimal in-memory Redis fallback (for local dev without Redis)
# ---------------------------------------------------------------------------

class _InMemoryRedis:
    """Minimal async Redis-like in-memory store for running without Redis."""

    def __init__(self):
        self._store: dict = {}
        self._expiry: dict = {}

    async def ping(self):
        return True

    async def get(self, key: str):
        self._evict(key)
        return self._store.get(key)

    async def mget(self, *keys: str) -> list:
        return [self._store.get(k) if k in self._store else None for k in keys]

    async def set(self, key: str, value, ex: int = None):
        self._store[key] = value
        if ex is not None:
            self._expiry[key] = time.time() + ex

    async def incr(self, key: str) -> int:
        self._evict(key)
        val = int(self._store.get(key, 0)) + 1
        self._store[key] = str(val)
        return val

    async def incrby(self, key: str, amount: int) -> int:
        self._evict(key)
        val = int(self._store.get(key, 0)) + amount
        self._store[key] = str(val)
        return val

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                self._expiry.pop(key, None)
                count += 1
        return count

    async def expire(self, key: str, seconds: int) -> int:
        self._expiry[key] = time.time() + seconds
        return 1

    def pipeline(self):
        return _InMemoryPipeline(self)

    async def aclose(self):
        pass

    async def keys(self, pattern: str = "*") -> list:
        import fnmatch
        result = []
        for key in list(self._store.keys()):
            self._evict(key)
            if key in self._store and fnmatch.fnmatch(key, pattern):
                result.append(key)
        return result

    async def scan_iter(self, pattern: str = "*", count: int = 100):
        import fnmatch
        for key in list(self._store.keys()):
            self._evict(key)
            if key in self._store and fnmatch.fnmatch(key, pattern):
                yield key

    def _evict(self, key: str):
        exp = self._expiry.get(key)
        if exp is not None and time.time() > exp:
            self._store.pop(key, None)
            self._expiry.pop(key, None)


class _InMemoryPipeline:
    def __init__(self, store: _InMemoryRedis):
        self._store = store
        self._commands = []

    def incr(self, key: str):
        self._commands.append(("incr", key, None))
        return self

    def expire(self, key: str, seconds: int):
        self._commands.append(("expire", key, seconds))
        return self

    def incrby(self, key: str, amount: int):
        self._commands.append(("incrby", key, amount))
        return self

    async def execute(self):
        results = []
        for cmd, key, arg in self._commands:
            if cmd == "incr":
                results.append(await self._store.incr(key))
            elif cmd == "expire":
                results.append(await self._store.expire(key, arg))
            elif cmd == "incrby":
                results.append(await self._store.incrby(key, arg))
        return results


# v1.20: persistent logs + audit viewer
@app.get("/logs/persistent", summary="Persistent logs & audit viewer", tags=["Dashboard"])
async def logs_persistent_page():
    from fastapi.responses import FileResponse, HTMLResponse as _HTMLResponse
    import pathlib
    p = pathlib.Path(_STATIC_DIR) / "logs-persistent.html"
    if p.exists():
        return FileResponse(str(p))
    return _HTMLResponse("<h1>Page not found</h1>", status_code=404)

