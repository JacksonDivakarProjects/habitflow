from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    llm_base_url: str = "http://llm:9000"
    llm_timeout_seconds: float = 20
    app_timezone: str = "Asia/Kolkata"


settings = Settings()
