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
from app.providers.modal_provider import ModalProvider
from app.providers.zai_provider import ZaiProvider
from app.providers.lightning_provider import LightningProvider
from app.providers.routeway import RoutewayProvider
from app.providers.ollama_provider import OllamaProvider
from app.routing.router import IntelligentRouter
from app.cache.cache import CacheLayer
from app.api import chat, models_api, dashboard
from app.api.cloudflare_manager import router as cloudflare_router
from app.api.settings_api import router as settings_router
from app.api.preferences_api import router as preferences_router
from app.api.keys_api import router as keys_router
from app.api.image_api import router as image_router
from app.api.modal_manager import router as modal_router
from app.api.modal_deploy import router as modal_deploy_router
from app.api.logs_api import router as logs_router, log_buffer
from app.api.gateway_tokens_api import router as gateway_tokens_router
from app.api.gateway_tokens_api import load_gateway_tokens_to_state
from app.api.custom_providers_api import router as custom_providers_router
from app.api.custom_providers_api import load_custom_providers_to_app
from app.api.users_api import router as users_router
from app.api.analytics_api import router as analytics_router
from app.auth.sso import router as auth_router, register_google_oauth, sso_enabled
from app.middleware.auth import (
    GatewayAuthMiddleware,
    CloudflareAccessMiddleware,
    SecurityHeadersMiddleware,
    BearerRedactFilter,
)
from starlette.middleware.sessions import SessionMiddleware

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

    # Initialize providers
    providers = {}
    provider_classes = {
        "gemini":       GeminiProvider,
        "groq":         GroqProvider,
        "openrouter":   OpenRouterProvider,
        "cohere":       CohereProvider,
        "cloudflare":   CloudflareProvider,
        "cerebras":     CerebrasProvider,
        "huggingface":  HuggingFaceProvider,
        "pollinations": PollinationsProvider,
        "zai":          ZaiProvider,
        "lightning":    LightningProvider,
        "routeway":     RoutewayProvider,
        "ollama":       OllamaProvider,
        "modal":        ModalProvider,
    }

    for name, cls in provider_classes.items():
        keys = settings.get_keys(name)

        # Pollinations is free/anonymous — inject a dummy key so the pool works
        if name == "pollinations" and not keys:
            keys = ["free"]
        # Modal keys come from Redis (registered via UI); skip until at least one is added
        if name == "modal" and not keys:
            continue

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

    # Restore Modal provider from Redis if it has registered endpoints but no env keys.
    # Modal endpoints are runtime-registered (via deploy or manual), so they live only in
    # Redis and would be lost on every restart without this restoration step.
    if "modal" not in providers:
        try:
            _modal_redis_raw = await redis_client.get("arbiter:runtime:keys:modal")
            _modal_redis_keys = []
            if _modal_redis_raw:
                import json as _json
                _modal_redis_keys = [k for k in _json.loads(_modal_redis_raw) if k.strip()]
            if _modal_redis_keys:
                providers["modal"] = ModalProvider()
                _modal_limits = PROVIDER_LIMITS.get("modal", {"rpm": 20, "tpm": 100_000, "daily": 1000})
                key_pools["modal"] = KeyPool(
                    provider="modal",
                    keys=_modal_redis_keys,
                    redis_client=redis_client,
                    rpm_limit=_modal_limits["rpm"],
                    tpm_limit=_modal_limits["tpm"],
                    daily_limit=_modal_limits["daily"],
                )
                logger.info(f"Restored Modal provider from Redis with {len(_modal_redis_keys)} endpoint(s)")
        except Exception as _e:
            logger.warning(f"Could not restore Modal provider from Redis: {_e}")

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

    yield

    # Cleanup
    logger.info("Shutting down Arbiter...")
    sync_task.cancel()
    try:
        await sync_task
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

    class _Stub:
        def __init__(self, app): self.app = app
    stub_request = _Stub(app)

    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL)
            logger.info("Weekly provider sync starting...")
            reloaded, failed = [], []
            for pname in _PROVIDER_META:
                try:
                    await _reload_provider(pname, stub_request)
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


_STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(
    title="Arbiter",
    description=(
        "Arbiter \u2013 Intelligent LLM Router & Gateway. "
        "Single OpenAI-compatible endpoint aggregating 12+ providers "
        "(Gemini, Groq, Cerebras, Cloudflare Workers AI, OpenRouter, Cohere, "
        "HuggingFace, Pollinations, Z.ai, Lightning.ai, Routeway, Ollama Cloud, Modal) plus "
        "user-added custom OpenAI-compatible providers. Features weighted "
        "key rotation, rate-limit tracking, response caching, dynamic model "
        "discovery, and Google SSO with admin-approval workflow."
    ),
    version="1.12.1",
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

# ── Security headers (outermost so they apply even to 4xx responses) ────────
app.add_middleware(SecurityHeadersMiddleware)

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
app.include_router(cloudflare_router, tags=["Cloudflare Workers AI"])
app.include_router(settings_router, tags=["Settings"])
app.include_router(preferences_router, tags=["Preferences"])
app.include_router(keys_router, tags=["Provider Management"])
app.include_router(image_router, tags=["Images"])
app.include_router(modal_router, tags=["Modal"])
app.include_router(modal_deploy_router, tags=["Modal Deploy"])
app.include_router(logs_router, tags=["Logs"])
app.include_router(gateway_tokens_router, tags=["Gateway Tokens"])
app.include_router(custom_providers_router, tags=["Custom Providers"])
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(analytics_router, tags=["Analytics"])


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
            "version": "1.12.1",
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


@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to dashboard."""
    return RedirectResponse(url="/dashboard")


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
