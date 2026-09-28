"""Configuración: todo sale de variables de entorno (.env)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    TELEGRAM_BOT_TOKEN: str
    SUPPORT_CHAT_ID: int
    ADMIN_CHAT_IDS: str = ""

    GROQ_API_KEY: str
    # Sin valor por defecto a propósito: los modelos NUNCA se hardcodean.
    GROQ_MODEL_STT: str
    GROQ_MODEL_LLM: str

    DATABASE_URL: str = "postgresql+asyncpg://sgi:sgi@localhost:5432/sgi"
    AUDIO_DIR: str = "/data/audio"
    TIMEZONE: str = "America/Bogota"
    LOG_LEVEL: str = "INFO"

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_SECURITY: str = "starttls"  # starttls | ssl
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    JEFE_EMAIL: str = ""

    # Rutas DENTRO del contenedor (montadas desde la carpeta local que sincroniza OneDrive; ver docker-compose.yml)
    ONEDRIVE_INVENTARIO_XLSX: str = "/data/onedrive/Inventario Oficina Pasaportes - copia.xlsx"
    ONEDRIVE_SOPORTE_XLSX: str = "/data/onedrive/Reporte_Gestion_Soporte_TI.xlsx"

    # Sincronización directa API Nube (Microsoft Graph / OneDrive / n8n Webhook)
    ENABLE_CLOUD_API_SYNC: bool = False
    N8N_WEBHOOK_URL: str = ""
    MICROSOFT_TENANT_ID: str = "common"
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""
    MICROSOFT_REFRESH_TOKEN: str = ""
    ONEDRIVE_REMOTE_FOLDER: str = "/SGI-Pasaportes"

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.ADMIN_CHAT_IDS.replace(" ", "").split(",") if x]


settings = Settings()
