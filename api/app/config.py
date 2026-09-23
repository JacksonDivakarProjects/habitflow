from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    llm_base_url: str = "http://llm:9000"
    llm_timeout_seconds: float = 20
    app_timezone: str = "Asia/Kolkata"
    # "auto": for serverless Postgres (Neon) connections are reused while busy and
    # closed after a quiet minute, so it can sleep; a normal pool otherwise.
    # "on" (normal pool), "idle" (close when idle), "off" (no pool) to force it.
    database_pool: str = "auto"

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
