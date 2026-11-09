"""Settings loaded from environment variables (and a local .env file, if present).

Variable names are listed in .env.example. Secrets are typed as SecretStr so they
print as '**********' in logs, reprs and tracebacks (SPEC §0 invariant 9).

Refund limits are NOT settings: they live only in policy.yaml (SPEC §5).
"""

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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

    # LLM adapter used by wardlock/llm.py: any OpenAI-compatible chat API (Gemini, Groq, ...).
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""

    # Wardlock's own secrets: orchestrator service key (SPEC §10) and console password (SPEC §12).
    wardlock_service_key: SecretStr = SecretStr("")
    wardlock_admin_password: SecretStr = SecretStr("")
    # A public, read-only way into the console for a demo: user "viewer", no password. It can
    # only read (GET); approve, deny and acknowledge still need the admin password. Off by default.
    console_public_readonly: bool = False

    # Host and Origin headers the MCP endpoint accepts (DNS-rebinding protection).
    # Add your Render host in P11, e.g. ["wardlock.onrender.com"].
    # NoDecode: we parse these ourselves (see _host_list), so a hand-typed value can't crash startup.
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    mcp_allowed_origins: Annotated[list[str], NoDecode] = ["http://127.0.0.1:*", "http://localhost:*",
                                                           "http://[::1]:*"]

    database_url: str = "sqlite:///./wardlock.db"
    session_ttl_minutes: int = 30
    hold_timeout_minutes: int = 15

    @field_validator("mcp_allowed_hosts", "mcp_allowed_origins", mode="before")
    @classmethod
    def _host_list(cls, value: Any, info: ValidationInfo) -> Any:
        """Accept what people actually type into a dashboard: empty (-> the localhost default),
        a JSON list ["a.example.com"], or a plain comma list a.example.com, b.example.com.
        Curly quotes from copy-paste are treated like plain ones."""
        if not isinstance(value, str):
            return value
        text = value.strip().translate(_PLAIN_QUOTES)
        if not text:
            return cls.model_fields[info.field_name].default
        if text.startswith("["):
            try:
                return json.loads(text)
            except ValueError:
                text = text.strip("[]")
        return [part.strip().strip("'\"") for part in text.split(",") if part.strip().strip("'\"")]


_PLAIN_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


@lru_cache
def get_settings() -> Settings:
    """Return one shared Settings instance. Tests can reset it with get_settings.cache_clear()."""
    return Settings()
