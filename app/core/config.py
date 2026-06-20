import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_version() -> str:
    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    except Exception:
        return "0.0.0"


# Numéro de version (fichier VERSION, à incrémenter à CHAQUE déploiement) + commit déployé.
APP_VERSION = _read_version()
APP_COMMIT = (os.getenv("RENDER_GIT_COMMIT") or "dev")[:7]


class Settings(BaseSettings):
    app_name: str = "Vaelan"
    database_url: str = "sqlite:///./vaelan.db"
    secret_key: str = "dev-secret-change-me"
    admin_email: str = "admin@vaelan.com"
    admin_password: str = "changeme"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
