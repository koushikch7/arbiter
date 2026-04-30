from typing import List
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    REDIS_URL: str = "redis://redis:6379"
    CACHE_TTL: int = 3600

    # Legacy single-key field (still read by the old chat.py _check_auth helper)
    GATEWAY_API_KEY: str = ""
    # New multi-key field for GatewayAuthMiddleware (comma-separated)
    GATEWAY_API_KEYS: str = ""
    # Strict / fail-closed auth: when True, the gateway refuses ALL outbound
    # LLM calls if no GATEWAY_API_KEYS and no dynamic gateway tokens are
    # configured. When False (legacy default), open mode is permitted with a
    # startup warning. Recommended: keep True in production.
    REQUIRE_AUTH: bool = True

    # ── Provider API Keys ────────────────────────────────────────────────────
    GEMINI_API_KEYS: str = ""
    GROQ_API_KEYS: str = ""
    OPENROUTER_API_KEYS: str = ""
    COHERE_API_KEYS: str = ""

    # HuggingFace Inference API token
    HUGGINGFACE_API_KEYS: str = ""

    # Cloudflare Workers AI — format: account_id|api_token (comma-separated)
    CLOUDFLARE_API_KEYS: str = ""
    # Dedicated account-ID field (optional; takes precedence when splitting is ambiguous)
    CLOUDFLARE_ACCOUNT_ID: str = ""

    # Cerebras Inference
    CEREBRAS_API_KEYS: str = ""

    # Z.ai / Zhipu AI — GLM-4.7-Flash, GLM-4.5-Flash (free tier)
    ZAI_API_KEYS: str = ""

    # Lightning.ai LitAI — natively hosted open-weight models (pay-per-token)
    LIGHTNING_API_KEYS: str = ""

    # Routeway — unified gateway to OpenAI, Anthropic, DeepSeek, etc.
    # https://routeway.ai (docs: https://docs.routeway.ai)
    ROUTEWAY_API_KEYS: str = ""

    # Ollama Cloud — free-tier cloud-hosted open-weight models
    # https://ollama.com/settings/keys
    OLLAMA_API_KEYS: str = ""

    # Pollinations — single key or comma-separated; required since 2026.
    POLLINATIONS_API_KEYS: str = ""

    # Modal.com serverless GPU — format: endpoint_url|token (comma-separated)
    MODAL_API_KEYS: str = ""
    # Modal account token (used by the one-click deploy feature)
    MODAL_TOKEN_ID: str = ""
    MODAL_TOKEN_SECRET: str = ""

    # ── Cloudflare Zero Trust / Access ───────────────────────────────────────
    # Team name, e.g. "myteam"  → https://myteam.cloudflareaccess.com
    CLOUDFLARE_ACCESS_TEAM_NAME: str = ""
    # Application Audience (AUD) tag from the Cloudflare Access application
    CLOUDFLARE_ACCESS_AUD: str = ""
    # Set to True to require and validate Cf-Access-Jwt-Assertion on every request
    ENABLE_CF_ACCESS: bool = False
    # ── Google SSO ──────────────────────────────────────────────────────────
    # Create OAuth 2.0 Web-application credentials at
    # https://console.cloud.google.com/apis/credentials . Authorized redirect
    # URI must be <APP_BASE_URL>/auth/callback
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    # Email address that is auto-bootstrapped as admin on first login.
    # All other users land in "pending" status and require admin approval.
    ADMIN_EMAIL: str = ""
    # Base URL used to build the OAuth redirect URI (when behind a proxy /
    # Cloudflare Tunnel the request URL may look like http://localhost).
    APP_BASE_URL: str = ""
    # Random 32+ char string used to sign the session cookie. REQUIRED when
    # SSO is enabled. Rotate to invalidate all existing sessions.
    SESSION_SECRET_KEY: str = ""
    # Set to True in production (requires HTTPS) so the session cookie is
    # only sent over TLS. Leave False for local dev over http://localhost.
    SESSION_COOKIE_SECURE: bool = False
    # Session lifetime in seconds (default 24h).
    SESSION_MAX_AGE: int = 86_400

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins. Empty value means same-origin
    # only (no CORS). "*" is rejected when SSO is enabled (insecure with
    # credentials).
    ALLOWED_CORS_ORIGINS: str = ""
    # ── Logging ──────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def get_keys(self, provider: str) -> List[str]:
        mapping = {
            "gemini":       self.GEMINI_API_KEYS,
            "groq":         self.GROQ_API_KEYS,
            "openrouter":   self.OPENROUTER_API_KEYS,
            "cohere":       self.COHERE_API_KEYS,
            "huggingface":  self.HUGGINGFACE_API_KEYS,
            "cloudflare":   self.CLOUDFLARE_API_KEYS,
            "cerebras":     self.CEREBRAS_API_KEYS,
            "zai":          self.ZAI_API_KEYS,
            "lightning":    self.LIGHTNING_API_KEYS,
            "routeway":     self.ROUTEWAY_API_KEYS,
            "ollama":       self.OLLAMA_API_KEYS,
            "pollinations": self.POLLINATIONS_API_KEYS,
            "modal":        self.MODAL_API_KEYS,
        }
        raw = mapping.get(provider, "")
        return [k.strip() for k in raw.split(",") if k.strip()]

    def get_gateway_api_keys(self) -> List[str]:
        """Return the combined list of valid gateway auth tokens."""
        keys: List[str] = []
        # Multi-key field (preferred)
        if self.GATEWAY_API_KEYS:
            keys.extend(k.strip() for k in self.GATEWAY_API_KEYS.split(",") if k.strip())
        # Legacy single-key field
        if self.GATEWAY_API_KEY and self.GATEWAY_API_KEY not in keys:
            keys.append(self.GATEWAY_API_KEY)
        return keys


settings = Settings()
