try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _PYDANTIC_V2 = True
except ImportError:  # pragma: no cover - compatibility for older pydantic
    from pydantic import BaseSettings
    _PYDANTIC_V2 = False


class Settings(BaseSettings):
    WATI_AUTH_TOKEN: str
    WATI_API_ENDPOINT: str

    # Only if WATI_API_ENDPOINT does NOT already end with this client id (e.g. global host); else leave empty
    WATI_TENANT_ID: str = ""
    # WATI login email for POST /api/v1/assignOperator on handoff
    WATI_HANDOFF_ASSIGNEE_EMAIL: str = "webmaster@sakshamsenior.com"
    # Preferred assignee for human handoff confirmation flow (Saksham operator)
    WATI_SAKSHAM_ASSIGNEE_EMAIL: str = "webmaster@sakshamsenior.com"
    # WATI WhatsApp Business / channel number (digits, country code, no spaces) for updateChatStatus
    WATI_CHANNEL_NUMBER: str = "919999043434"
    # When thread is human (techsaathi), poll ext v3 GET …/messages to detect agent-closed chat
    WATI_EXT_MESSAGES_ENABLED: bool = True
    BANK_HELPLINE_URL: str = ""

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
