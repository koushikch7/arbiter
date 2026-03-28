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
from app.routing.router import IntelligentRouter
from app.cache.cache import CacheLayer
from app.api import chat, models_api, dashboard
from app.api.cloudflare_manager import router as cloudflare_router
from app.api.settings_api import router as settings_router
from app.api.keys_api import router as keys_router
from app.api.image_api import router as image_router
from app.api.modal_manager import router as modal_router
from app.api.modal_deploy import router as modal_deploy_router
from app.api.logs_api import router as logs_router, log_buffer
from app.middleware.auth import GatewayAuthMiddleware, CloudflareAccessMiddleware

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Attach in-memory log buffer to root logger so all log records are captured
logging.getLogger().addHandler(log_buffer)
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

    logger.info(
        f"Arbiter ready. Providers: {list(providers.keys())}, "
        f"Cache TTL: {settings.CACHE_TTL}s"
    )

    yield

    # Cleanup
    logger.info("Shutting down Arbiter...")
    try:
        await redis_client.aclose()
    except Exception:
        pass


_STATIC_DIR = Path(__file__).parent.parent / "static"

app = FastAPI(
    title="Arbiter",
    description=(
        "Arbiter – Intelligent LLM Router & Gateway. "
        "Single OpenAI-compatible endpoint aggregating Gemini, Groq, Cerebras, "
        "Cloudflare Workers AI, OpenRouter, Cohere, HuggingFace, and Pollinations "
        "with weighted key rotation, rate-limit tracking, and response caching."
    ),
    version="1.1.0",
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

# ── CORS middleware (permissive for self-hosted use) ────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# ── Gateway auth middleware ─────────────────────────────────────────────────
gateway_keys = settings.get_gateway_api_keys()
app.add_middleware(GatewayAuthMiddleware, allowed_keys=gateway_keys)
if gateway_keys:
    logger.info(f"GatewayAuthMiddleware enabled with {len(gateway_keys)} key(s)")
else:
    logger.info("GatewayAuthMiddleware loaded (auth disabled — no keys configured)")


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
app.include_router(keys_router, tags=["Provider Management"])
app.include_router(image_router, tags=["Images"])
app.include_router(modal_router, tags=["Modal"])
app.include_router(modal_deploy_router, tags=["Modal Deploy"])
app.include_router(logs_router, tags=["Logs"])


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
            "version": "1.0.0",
        }
    )


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
