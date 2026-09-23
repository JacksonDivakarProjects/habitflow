from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    llm_base_url: str = "http://llm:9000"
    llm_timeout_seconds: float = 20
    app_timezone: str = "Asia/Kolkata"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, url: str) -> str:
        """Hosted Postgres (Render, Heroku, ...) hands out postgres:// or
        postgresql:// URLs; SQLAlchemy needs the driver named explicitly."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
