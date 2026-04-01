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

    # ── Google OAuth2 Login (optional) ───────────────────────────────────────
    # When GOOGLE_CLIENT_ID is set, the web UI requires users to sign in with Google
    # before accessing dashboard/analytics/settings/etc.
    # Create credentials at: https://console.cloud.google.com/apis/credentials
    # Add Authorized redirect URI: http(s)://<your-host>/auth/callback
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/callback"
    # Comma-separated list of allowed email addresses (empty = allow any Google account)
    GOOGLE_ALLOWED_EMAILS: str = ""
    # Comma-separated list of allowed email domains e.g. "mycompany.com"
    GOOGLE_ALLOWED_DOMAINS: str = ""
    # Secret for signing session JWTs — auto-generated if empty, but set explicitly
    # for production so sessions survive restarts.
    SESSION_SECRET: str = ""

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
