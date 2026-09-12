"""Application configuration.

Mirrors the Java ``application.yml`` and environment variables (e.g. ``MISTRAL_API_KEY``)
so that the Python implementation can be dropped in as a replacement without renaming
configuration that already exists in the deployment environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "Users"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # --- Database (matches src/main/resources/application.yml) ---
    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "Knowledge_Gen_Agent"
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_driver: str = "postgresql"
    # Optional full override, e.g. postgresql://postgres:postgres@localhost:5433/Knowledge_Gen_Agent
    database_url: str | None = None
    # Set to true to echo SQL (mirrors spring.jpa.show-sql: true)
    database_echo_sql: bool = False

    # --- Mistral AI (matches `mistral.api.*` in application.yml) ---
    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_chat_model: str = "open-mistral-nemo"
    mistral_embedding_model: str = "mistral-embed"
    mistral_ocr_model: str = "mistral-ocr-latest"
    mistral_timeout_seconds: float = 30.0
    mistral_retries: int = 3

    # --- RAG ---
    rag_chunk_size: int = 500
    rag_top_k: int = 5

    # --- Conversation memory ---
    conversation_max_history: int = 10

    # --- CORS (matches CorsConfig.java) ---
    cors_origins: list[str] = ["http://localhost:4200"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.db_driver == "postgresql":
            return (
                f"postgresql://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return f"{self.db_driver}://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def is_postgres(self) -> bool:
        return self.sqlalchemy_database_url.startswith(("postgresql", "postgres"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
