"""Centralised configuration for the gateway.

All settings are loaded from environment variables (or a local `.env` file).
The `Settings` object is created once at import time and reused everywhere.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed gateway configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # --- Server ---
    gateway_host: str = Field(default="127.0.0.1", alias="GATEWAY_HOST")
    gateway_port: int = Field(default=8787, alias="GATEWAY_PORT")

    # --- Auth ---
    # Stored as a comma-separated string in env; parsed into a set.
    gateway_api_keys_raw: str = Field(default="", alias="GATEWAY_API_KEYS")

    # --- Rate limiting ---
    gateway_rate_limit_rpm: int = Field(default=60, alias="GATEWAY_RATE_LIMIT_RPM")
    gateway_rate_limit_burst: int = Field(default=10, alias="GATEWAY_RATE_LIMIT_BURST")

    # --- OpenCode CLI ---
    opencode_bin: str = Field(default="opencode", alias="OPENCODE_BIN")
    opencode_workdir: str = Field(default="", alias="OPENCODE_WORKDIR")
    opencode_timeout: int = Field(default=300, alias="OPENCODE_TIMEOUT")
    opencode_default_model: str = Field(
        default="opencode/big-pickle", alias="OPENCODE_DEFAULT_MODEL"
    )
    opencode_default_agent: str = Field(default="", alias="OPENCODE_DEFAULT_AGENT")
    opencode_extra_flags: str = Field(default="", alias="OPENCODE_EXTRA_FLAGS")

    # --- Logging ---
    gateway_log_level: str = Field(default="INFO", alias="GATEWAY_LOG_LEVEL")

    # --- Derived ---
    @property
    def api_keys(self) -> set[str]:
        """Parsed set of accepted API keys (empty set ⇒ auth disabled)."""
        return {k.strip() for k in self.gateway_api_keys_raw.split(",") if k.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def workdir(self) -> Optional[Path]:
        return Path(self.opencode_workdir).resolve() if self.opencode_workdir else None

    @property
    def extra_flags(self) -> List[str]:
        return self.opencode_extra_flags.split() if self.opencode_extra_flags else []

    @field_validator("gateway_log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"GATEWAY_LOG_LEVEL must be one of {allowed}, got {v!r}")
        return upper


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
