"""Configuration loader for BetPoly_Bot.

All runtime configuration comes from environment variables (or a ``.env``
file).  Settings are validated lazily so that importing this module never
crashes a test run or a CLI utility that does not need, for example, a bot
token.

Design notes
------------
* ``get_settings()`` is cached; :func:`reset_settings` exists for tests.
* Money is handled in **nanoTON** (integer) on the chain side and in whole
  *points* for the off-chain sandbox mode.  Both are integers, never floats.
* The bot never holds a private key.  ``ESCROW_ADDRESS`` is a public address
  only and is used to *read* the chain.
"""
from __future__ import annotations

import os
import secrets as _secrets
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

VALID_MODES = ("points", "escrow", "hybrid")


class ConfigError(RuntimeError):
    """Raised when the configuration is inconsistent or incomplete."""


# ---------------------------------------------------------------------------
# primitive readers
# ---------------------------------------------------------------------------

def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _opt_str(key: str) -> Optional[str]:
    value = (os.getenv(key) or "").strip()
    return value or None


def _int(key: str, default: int, *, minimum: Optional[int] = None,
         maximum: Optional[int] = None) -> int:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum} (got {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} must be <= {maximum} (got {value})")
    return value


def _bool(key: str, default: bool = False) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on", "si", "sí")


def _ids(key: str) -> Tuple[int, ...]:
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return ()
    out = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{key} contains a non numeric id: {chunk!r}") from exc
    return tuple(out)


# ---------------------------------------------------------------------------
# settings object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    # --- Telegram ---------------------------------------------------------
    bot_token: str
    admin_ids: Tuple[int, ...]
    log_level: str

    # --- storage ----------------------------------------------------------
    database_path: str
    timezone: str

    # --- off-chain sandbox economy (virtual points, no real money) --------
    starting_points: int
    min_bet_points: int
    max_bet_points: int
    house_rake_bps: int
    reminder_hours_before: int

    # --- product mode -----------------------------------------------------
    mode: str

    # --- TON chain (read only for the bot) --------------------------------
    ton_network: str
    ton_api_url: str
    ton_api_key: Optional[str]
    ton_public_key_api: Optional[str]
    escrow_address: Optional[str]
    escrow_admin_address: Optional[str]
    ton_proof_domain: Optional[str]
    ton_proof_max_age: int
    min_stake_nano: int
    max_options: int

    # --- indexer ----------------------------------------------------------
    indexer_enabled: bool
    indexer_poll_interval: int
    indexer_batch: int

    # --- web / mini app ---------------------------------------------------
    web_enabled: bool
    web_host: str
    web_port: int
    public_base_url: str
    secret_key: str
    allow_dev_session: bool
    environment: str
    app_title: str
    support_url: Optional[str] = field(default=None)
    frame_ancestors: str = "'self'"

    # -- derived helpers ---------------------------------------------------
    @property
    def escrow_enabled(self) -> bool:
        return self.mode in ("escrow", "hybrid") and bool(self.escrow_address)

    @property
    def points_enabled(self) -> bool:
        return self.mode in ("points", "hybrid")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("prod", "production")

    @property
    def webapp_url(self) -> str:
        """URL used by the Telegram menu button to open the Mini App."""
        return self.public_base_url.rstrip("/") + "/"

    @property
    def manifest_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/tonconnect-manifest.json"

    @property
    def proof_domain(self) -> str:
        """Host that must match ``ton_proof.domain.value``."""
        if self.ton_proof_domain:
            return self.ton_proof_domain
        base = self.public_base_url.rstrip("/")
        for prefix in ("https://", "http://"):
            if base.startswith(prefix):
                base = base[len(prefix):]
        return base.split("/")[0]

    def validate(self) -> None:
        """Raise :class:`ConfigError` when the active mode cannot run."""
        if self.mode not in VALID_MODES:
            raise ConfigError(
                f"BET_MODE must be one of {', '.join(VALID_MODES)} (got {self.mode!r})"
            )
        if self.mode in ("escrow", "hybrid") and not self.escrow_address:
            raise ConfigError(
                "ESCROW_ADDRESS is required when BET_MODE is 'escrow' or 'hybrid'. "
                "Deploy contracts/escrow.fc and set the address."
            )
        if self.escrow_enabled:
            if not self.public_base_url.startswith("https://"):
                raise ConfigError(
                    "PUBLIC_BASE_URL must be an https:// URL: Telegram Mini Apps and "
                    "TON Connect reject plain http origins."
                )
            if not self.ton_proof_domain and not self.public_base_url:
                raise ConfigError("PUBLIC_BASE_URL or TON_PROOF_DOMAIN must be set.")
        if self.web_enabled and self.allow_dev_session and self.is_production:
            raise ConfigError(
                "ALLOW_DEV_SESSION must be false in production: it disables Telegram "
                "initData verification."
            )
        if self.is_production and self.secret_key.startswith("dev-"):
            raise ConfigError("SECRET_KEY must be set to a strong random value in production.")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"Unknown TIMEZONE {self.timezone!r}") from exc


def _build_settings() -> Settings:
    mode = (_str("BET_MODE", "hybrid") or "hybrid").lower()
    environment = _str("ENVIRONMENT", "development").lower()

    secret = _opt_str("SECRET_KEY")
    if not secret:
        # Ephemeral secret: sessions survive only while the process lives.
        secret = "dev-" + _secrets.token_hex(24)
        if environment not in ("dev", "development", "test", "testing"):
            os.environ.setdefault("BETPOLY_INSECURE_SECRET_WARNING", "1")

    settings = Settings(
        bot_token=_str("BOT_TOKEN"),
        admin_ids=_ids("ADMIN_IDS"),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        database_path=_str("DATABASE_PATH", "betpoly.db"),
        timezone=_str("TIMEZONE", "UTC"),
        starting_points=_int("STARTING_POINTS", 1000, minimum=0),
        min_bet_points=_int("MIN_BET_POINTS", 10, minimum=1),
        max_bet_points=_int("MAX_BET_POINTS", 1_000_000, minimum=1),
        house_rake_bps=_int("HOUSE_RAKE_BPS", 0, minimum=0, maximum=1000),
        reminder_hours_before=_int("REMINDER_HOURS_BEFORE", 24, minimum=0),
        mode=mode,
        ton_network=_str("TON_NETWORK", "mainnet").lower(),
        ton_api_url=_str("TON_API_URL", "https://toncenter.com/api/v2"),
        ton_api_key=_opt_str("TON_API_KEY"),
        ton_public_key_api=_opt_str("TON_PUBLIC_KEY_API"),
        escrow_address=_opt_str("ESCROW_ADDRESS"),
        escrow_admin_address=_opt_str("ESCROW_ADMIN_ADDRESS"),
        ton_proof_domain=_opt_str("TON_PROOF_DOMAIN"),
        ton_proof_max_age=_int("TON_PROOF_MAX_AGE", 600, minimum=60, maximum=3600),
        min_stake_nano=_int("MIN_STAKE_NANO", 100_000_000, minimum=1),
        max_options=_int("MAX_OPTIONS", 8, minimum=2, maximum=20),
        indexer_enabled=_bool("INDEXER_ENABLED", True),
        indexer_poll_interval=_int("INDEXER_POLL_INTERVAL", 20, minimum=5, maximum=3600),
        indexer_batch=_int("INDEXER_BATCH", 50, minimum=1, maximum=200),
        web_enabled=_bool("WEB_ENABLED", True),
        web_host=_str("WEB_HOST", "0.0.0.0"),
        web_port=_int("WEB_PORT", 8080, minimum=1, maximum=65535),
        public_base_url=_str("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/"),
        secret_key=secret,
        allow_dev_session=_bool("ALLOW_DEV_SESSION", False),
        environment=environment,
        app_title=_str("APP_TITLE", "BetPoly"),
        support_url=_opt_str("SUPPORT_URL"),
        # Telegram embeds the Mini App in an iframe; the sandbox preview does too.
        frame_ancestors=_str(
            "FRAME_ANCESTORS",
            "'self' https://*.telegram.org https://web.telegram.org https://*.e2b.app",
        ),
    )
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return _build_settings()


def reset_settings() -> None:
    """Clear the settings cache (used by tests and after env changes)."""
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Backwards compatible module level constants
# ---------------------------------------------------------------------------

def __getattr__(name: str):  # PEP 562
    """Lazy aliases so ``from config import ADMIN_IDS`` keeps working."""
    mapping = {
        "BOT_TOKEN": "bot_token",
        "ADMIN_IDS": "admin_ids",
        "DATABASE_PATH": "database_path",
        "REMINDER_HOURS_BEFORE": "reminder_hours_before",
        "STARTING_POINTS": "starting_points",
        "MIN_BET_POINTS": "min_bet_points",
        "HOUSE_RAKE_BPS": "house_rake_bps",
        "ESCROW_ADDRESS": "escrow_address",
        "PUBLIC_BASE_URL": "public_base_url",
    }
    field_name = mapping.get(name)
    if field_name:
        return getattr(get_settings(), field_name)
    raise AttributeError(name)


def require_bot_token() -> str:
    """Return the bot token or raise a helpful error."""
    token = get_settings().bot_token
    if not token:
        raise ConfigError(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill in the token "
            "you obtained from @BotFather."
        )
    return token
