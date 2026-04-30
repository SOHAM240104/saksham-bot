try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _PYDANTIC_V2 = True
except ImportError:  # pragma: no cover - compatibility for older pydantic
    from pydantic import BaseSettings
    _PYDANTIC_V2 = False


class Settings(BaseSettings):
    WATI_AUTH_TOKEN: str
    WATI_API_ENDPOINT: str

    if _PYDANTIC_V2:
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )
    else:
        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"


settings = Settings()
