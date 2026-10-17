from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["local", "test"] = "local"
    database_url: str = "sqlite:///storage/local.db"
    storage_root: Path = Path("storage")
    dev_user_id: str = "local-developer"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.8-flash"
    gemini_timeout_seconds: int = Field(600, ge=10)
    gemini_concurrency: int = Field(2, ge=1, le=16)
    llm_mode: Literal["gemini", "mock"] = "gemini"
    author_name: str = ""
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_upload_mb: int = Field(500, ge=1)
    max_file_mb: int = Field(250, ge=1)
    chunk_max_pages: int = Field(8, ge=1, le=8)
    chunk_max_bytes: int = Field(40_000_000, ge=1_000_000, le=40_000_000)
    worker_poll_seconds: float = 2
    worker_lease_seconds: int = 120

    @model_validator(mode="after")
    def validate_local_identity(self):
        if not self.dev_user_id.strip():
            raise ValueError("DEV_USER_ID must not be empty")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
