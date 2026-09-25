"""Application configuration using pydantic-settings."""

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Path to config file for API key persistence
CONFIG_FILE_PATH = Path(__file__).parent.parent / "data" / "config.json"
ALLOWED_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _read_config_json() -> dict[str, Any]:
    """Raw read of config.json (no key injection)."""
    if CONFIG_FILE_PATH.exists():
        try:
            return json.loads(CONFIG_FILE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_config_json(config: dict[str, Any]) -> None:
    """Raw write of config.json (no secret stripping)."""
    CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE_PATH.write_text(json.dumps(config, indent=2))


def load_config_file(user_id: str) -> dict[str, Any]:
    """Load non-secret configuration, with the caller's decrypted API keys injected.

    ``config.json`` holds shared, non-secret app settings (provider/model
    choice, language, feature prompts). API keys are per-account and live in
    the encrypted Postgres store, not config.json. They are injected here
    under ``api_keys`` so ``resolve_api_key(stored, provider)`` keeps
    resolving per-provider keys everywhere ``stored`` is built from this
    function. ``save_config_file`` strips them again, so they never round-trip
    back to disk.
    """
    config = _read_config_json()
    config["api_keys"] = get_api_keys_from_config(user_id)
    return config


def save_config_file(config: dict[str, Any]) -> None:
    """Save non-secret configuration to config.json.

    Secrets (``api_keys`` map and the legacy single ``api_key``) are stripped
    before writing — they belong to the encrypted store only.
    """
    config = dict(config)
    config.pop("api_keys", None)
    config.pop("api_key", None)
    _write_config_json(config)


def get_api_keys_from_config(user_id: str) -> dict[str, str]:
    """Get ``user_id``'s decrypted API keys from the encrypted Postgres store.

    Returns:
        Dictionary with key-store provider names as keys and plaintext keys as
        values (entries that fail to decrypt are omitted).
    """
    from app.crypto import decrypt
    from app.database import db

    decrypted: dict[str, str] = {}
    for provider, ciphertext in db.get_api_key_ciphertexts(user_id).items():
        plaintext = decrypt(ciphertext)
        if plaintext:
            decrypted[provider] = plaintext
    return decrypted


def save_api_keys_to_config(user_id: str, api_keys: dict[str, str]) -> None:
    """Replace ``user_id``'s encrypted key store with ``api_keys`` (encrypting each).

    Replace-all semantics mirror the legacy ``config["api_keys"] = api_keys``;
    the config router reads-merges-saves the full map.
    """
    from app.crypto import encrypt
    from app.database import db

    # Encrypt everything first, then swap in a single transaction, so a partial
    # failure (encryption error or DB write) can never wipe previously stored
    # keys mid-replace.
    ciphertexts = {provider: encrypt(key) for provider, key in api_keys.items() if key}
    db.replace_api_keys(user_id, ciphertexts)


def delete_api_key_from_config(user_id: str, provider: str) -> None:
    """Delete a specific API key from ``user_id``'s encrypted store."""
    from app.database import db

    db.delete_api_key(user_id, provider)


def clear_all_api_keys(user_id: str) -> None:
    """Clear all of ``user_id``'s API keys and any legacy config slots.

    The legacy plaintext ``config.json`` slots are shared/global leftovers
    from the single-tenant era; clearing them here is harmless (they are
    re-derived from the encrypted per-user store, never the other way).
    """
    from app.database import db

    db.clear_api_keys(user_id)
    # Defensively clear any legacy plaintext remnants from config.json.
    config = _read_config_json()
    if "api_keys" in config or "api_key" in config:
        config.pop("api_keys", None)
        config.pop("api_key", None)
        _write_config_json(config)


def migrate_legacy_keys() -> None:
    """No-op since the API key store became per-account.

    This used to fold legacy plaintext keys from the single-tenant
    ``config.json`` into the (then-global) encrypted store at startup. Keys
    are now scoped per Supabase account, and this function runs with no
    request context — there is no user to attribute a legacy global key to,
    so guessing an owner would silently hand one account's old key to
    whichever account happens to trigger the next lookup. Any pre-migration
    plaintext keys must be migrated explicitly per account (see
    ``app/scripts/migrate_sqlite_to_supabase.py`` / the deployment runbook)
    rather than automatically at boot.
    """
    config = _read_config_json()
    if config.get("api_keys") or config.get("api_key"):
        logging.getLogger(__name__).info(
            "Found legacy plaintext API key(s) in config.json but skipping "
            "automatic migration now that keys are per-account. Run the "
            "documented per-account key migration instead."
        )


# Mirror of llm._PROVIDER_KEY_MAP, duplicated to avoid importing llm.py (which
# pulls in litellm) at config import time.
_LEGACY_PROVIDER_KEY_MAP: dict[str, str] = {
    "openai": "openai",
    "openai_compatible": "openai_compatible",
    "azure_foundry": "azure_foundry",
    "anthropic": "anthropic",
    "gemini": "google",
    "openrouter": "openrouter",
    "deepseek": "deepseek",
    "groq": "groq",
    "ollama": "ollama",
}


def _get_llm_api_key_with_fallback() -> str:
    """Get LLM API key from the environment only.

    This helper backs ``Settings.get_effective_api_key()``, which has no
    request context and therefore no ``user_id`` — it can no longer fall
    back to the per-account encrypted key store (see
    ``app.llm.get_llm_config(user_id)`` / ``resolve_api_key`` for the
    request-scoped, per-account resolution path actually used at runtime).
    """
    import os

    return os.environ.get("LLM_API_KEY", "")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM Configuration
    llm_provider: Literal[
        "openai",
        "openai_compatible",
        "azure_foundry",
        "anthropic",
        "openrouter",
        "gemini",
        "deepseek",
        "groq",
        "ollama",
    ] = "openai"
    llm_model: str = "gpt-5-nano-2025-08-07"
    llm_api_key: str = ""
    llm_api_base: str | None = None  # For Ollama or custom endpoints
    log_llm: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "WARNING"

    @field_validator("llm_provider", mode="before")
    @classmethod
    def set_default_provider(cls, v: Any) -> str:
        """Handle empty string provider by defaulting to openai."""
        if not v or (isinstance(v, str) and not v.strip()):
            return "openai"
        return v

    @field_validator("log_llm", mode="before")
    @classmethod
    def normalize_log_llm_level(cls, v: Any) -> str:
        """Normalize LiteLLM log level from environment values."""
        value = "WARNING" if not v else str(v).strip().upper()
        if value not in ALLOWED_LOG_LEVELS:
            raise ValueError(f"Invalid LOG_LLM: {value}. Allowed: {ALLOWED_LOG_LEVELS}")
        return value

    # Server Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"
    frontend_base_url: str = "http://localhost:3000"

    # Hard timeout (seconds) for a single resume tailoring/improve request — the
    # backend wraps the improve flow in asyncio.wait_for(timeout=this). It MUST be
    # kept in sync with the two frontend layers (Next.js `proxyTimeout` and the
    # client AbortController, both driven by NEXT_PUBLIC_REQUEST_TIMEOUT_MS):
    # whichever layer is shortest aborts first, so raising only one silently fails
    # (this is why issue #776's backend-only workaround didn't work). Local LLMs
    # (Ollama, llama.cpp, …) often need longer than the 240s default; bounded to
    # [30, 1800]s so a stuck request can't hold a worker indefinitely.
    request_timeout_seconds: int = 240

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def clamp_request_timeout(cls, v: Any) -> int:
        """Clamp to [30, 1800] seconds; fall back to 240 on blank/invalid input."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return 240
        try:
            seconds = int(float(str(v).strip()))
        except (TypeError, ValueError, OverflowError):
            # OverflowError guards against inf (int(float("inf"))); ValueError
            # against nan/garbage. A bad env value must never crash startup.
            return 240
        return max(30, min(1800, seconds))

    # Reasoning effort for models that support it (OpenAI gpt-5 family,
    # Anthropic Claude 3.7+, DeepSeek R1, etc.). None means "do not send the
    # param" — the default for maximum compatibility. LiteLLM drops this
    # parameter for providers that don't support it (via drop_params=True).
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def normalize_reasoning_effort(cls, v: Any) -> Any:
        """Treat empty string (common when env var is blank) as None."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, v: Any) -> str:
        """Normalize application log level from environment values."""
        value = "INFO" if not v else str(v).strip().upper()
        if value not in ALLOWED_LOG_LEVELS:
            raise ValueError(f"Invalid LOG_LEVEL: {value}. Allowed: {ALLOWED_LOG_LEVELS}")
        return value

    # CORS Configuration
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    @property
    def effective_cors_origins(self) -> list[str]:
        """CORS origins including frontend_base_url for production deployments."""
        origins = list(self.cors_origins)
        url = self.frontend_base_url.strip().rstrip("/")
        if url and url not in origins:
            origins.append(url)
        return origins

    # Paths
    data_dir: Path = Path(__file__).parent.parent / "data"

    @property
    def db_path(self) -> Path:
        """Path to the legacy TinyDB database file (migration source only)."""
        return self.data_dir / "database.json"

    @property
    def sqlite_path(self) -> Path:
        """Path to the legacy SQLite database file (pre-Supabase migration source only)."""
        return self.data_dir / "resume_matcher.db"

    # -- Supabase Auth + Postgres --------------------------------------------
    # DATABASE_URL is a direct Postgres connection string using the
    # `authenticated` role (NOT the service_role/superuser). It must be async
    # (postgresql+asyncpg://...); the sync variant is derived automatically
    # for the api_keys hot path. Never point this at a service-role/superuser
    # connection — the app relies on RLS being enforced for this role.
    database_url: str = ""

    @property
    def async_database_url(self) -> str:
        """Async (asyncpg) DSN, normalized to the asyncpg SQLAlchemy driver."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    @property
    def sync_database_url(self) -> str:
        """Sync (psycopg2) DSN, normalized to the psycopg2 SQLAlchemy driver."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql+asyncpg://"):
            url = "postgresql+psycopg2://" + url[len("postgresql+asyncpg://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://") :]
        return url

    # Supabase project URL, e.g. https://<project-ref>.supabase.co
    supabase_url: str = ""
    # Publishable (anon) client key — safe to ship to the browser. Never put
    # the service_role key in any setting the app reads at request time.
    supabase_publishable_key: str = ""
    # JWKS endpoint used to verify Supabase Auth access tokens (asymmetric
    # signing, the current Supabase default). Derived from supabase_url when
    # left blank.
    supabase_jwks_url: str = ""

    @property
    def effective_jwks_url(self) -> str:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        base = self.supabase_url.strip().rstrip("/")
        return f"{base}/auth/v1/.well-known/jwks.json" if base else ""

    # Legacy HS256 shared secret, only needed for older Supabase projects that
    # still sign access tokens symmetrically. Optional; asymmetric JWKS
    # verification is preferred and does not need this.
    supabase_jwt_secret: str = ""

    @property
    def config_path(self) -> Path:
        """Path to config storage file."""
        return self.data_dir / "config.json"

    def get_effective_api_key(self) -> str:
        """Get the effective API key with config file fallback.

        Priority: Environment/settings value > config.json > empty string
        """
        if self.llm_api_key:
            return self.llm_api_key
        return _get_llm_api_key_with_fallback()


settings = Settings()
