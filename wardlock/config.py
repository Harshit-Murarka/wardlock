"""Settings loaded from environment variables (and a local .env file, if present).

Variable names are listed in .env.example. Secrets are typed as SecretStr so they
print as '**********' in logs, reprs and tracebacks (SPEC §0 invariant 9).

Refund limits are NOT settings: they live only in policy.yaml (SPEC §5).
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class Settings(BaseSettings):
    # Field names match env vars case-insensitively: paypal_client_id <- PAYPAL_CLIENT_ID.
    # extra="ignore" so a stray variable in .env doesn't crash startup.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PayPal. The client refuses non-sandbox URLs unless allow_live is set (invariant 8, P1).
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: str = SANDBOX_BASE_URL
    paypal_webhook_id: str = ""
    allow_live: bool = False

    # LLM adapter used by wardlock/llm.py.
    llm_provider: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""

    # Wardlock's own secrets: orchestrator service key (SPEC §10) and console password (SPEC §12).
    wardlock_service_key: SecretStr = SecretStr("")
    wardlock_admin_password: SecretStr = SecretStr("")

    database_url: str = "sqlite:///./wardlock.db"
    session_ttl_minutes: int = 30
    hold_timeout_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    """Return one shared Settings instance. Tests can reset it with get_settings.cache_clear()."""
    return Settings()
